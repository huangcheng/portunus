import { createHash } from 'node:crypto';
import { constants as fsConstants } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { parseBoundedJsonResponse } from './api-client.mjs';
import { CLIENT_VERSION, agentRequestHeaders } from './config.mjs';
import { assertCredentialSession } from './credential-store.mjs';
import {
  DOWNLOAD_TARGET_EXISTS_MESSAGE,
  assertRemoteName,
  transferItemId,
  resolveUploadWorkItem,
  assertUploadSourceUnchanged,
  isOutsideRelativePath,
  guardDownloadPath,
  openSourceFile,
  removeDisposableFile,
  downloadTemporaryPath,
  ownedDownloadTemporaryPath,
} from './local-files.mjs';
import { SkillError } from './protocol.mjs';

const DOWNLOAD_STATE_FLUSH_BYTES = 16 * 1024 * 1024;
const DOWNLOAD_STATE_FLUSH_INTERVAL_MS = 1_000;

export class TransferEngine {
  constructor({
    store,
    credentials,
    refreshCoordinator,
    driveApi,
    config,
    fetchImpl = fetch,
    uploadSdk,
    publicationFs = fs,
  }) {
    this.store = store;
    this.credentials = credentials;
    this.refreshCoordinator = refreshCoordinator;
    this.files = driveApi;
    this.config = config;
    this.fetch = fetchImpl;
    this.uploadSdk = uploadSdk;
    this.publicationFs = publicationFs;
    this.stopping = false;
    this.activeClient = undefined;
    this.activeJob = undefined;
    this.downloadAbort = undefined;
  }

  async stop() {
    this.stopping = true;
    this.downloadAbort?.abort();
    if (this.activeClient && this.activeJob) {
      await this.activeClient.cancel(this.activeJob.id).catch(() => {});
    }
  }

  async execute(batch, work, driveApi = this.files) {
    if (await this.shouldStop(batch)) {
      throw new SkillError(await this.stopCode(batch), '任务已停止');
    }
    const credentials = await this.credentials.get();
    if (!credentials || credentials.accountId !== batch.account_id) {
      throw new SkillError('LOGIN_REQUIRED', '登录账号与批次账号不一致');
    }
    const plan = batch.local_plan.find((item) => item.operation_id === work.operation_id);
    if (!plan) throw new SkillError('LOCAL_PLAN_MISSING', '本地传输计划不存在');
    if (plan.type === 'upload' || plan.type === 'upload_directory') {
      return this.upload(batch, work, plan);
    }
    if (plan.type === 'download') return this.download(batch, work, plan, driveApi);
    throw new SkillError('UNSUPPORTED_TRANSFER', '不支持的本地传输类型');
  }

  async acknowledge(batchId, work) {
    const relative = work.item?.relative_path;
    if (!relative) return;
    const subitemId = work.item.subitem_id || transferItemId(relative);
    await this.store.deleteCheckpoint(batchId, work.operation_id, subitemId);
  }

  async upload(batch, work, plan) {
    const session = await this.credentials.getSession();
    const credentials = session.credentials;
    if (!credentials || credentials.accountId !== batch.account_id) {
      throw new SkillError('LOGIN_REQUIRED', '登录账号与批次账号不一致');
    }
    const sdk = this.uploadSdk ?? (await import('@guangyapan/upload-sdk-node-multioss'));
    const item = await resolveUploadWorkItem(plan, work.item);
    const localPath =
      plan.type === 'upload'
        ? plan.root
        : path.join(plan.root, ...item.relative_path.split('/'));
    assertWithin(plan.root, localPath);
    await assertUploadSourceUnchanged(localPath, item, plan);
    const source = await openSourceFile(localPath, item);
    try {
      return await this.uploadOpened(batch, work, item, session, sdk, source);
    } finally {
      await source.close();
    }
  }

  async uploadOpened(batch, work, item, session, sdk, source) {
    const { createUploadClient } = sdk;
    const credentials = session.credentials;
    const subitemId = work.item?.subitem_id || transferItemId(item.relative_path);
    const saved = await this.store.readCheckpoint(
      batch.batch_id,
      work.operation_id,
      subitemId,
    );
    const usable =
      saved &&
      saved.account_id === batch.account_id &&
      saved.size === item.size &&
      saved.mtime_ms === item.mtime_ms;
    const completedBytes = progressBase(batch.progress?.current_bytes, usable ? saved : undefined);
    if (usable && saved.completed === true && saved.result) {
      const completedItems = this.completedItemsTarget(saved);
      await this.updateProgress(batch.batch_id, {
        phase: 'VALIDATING',
        current_bytes: completedBytes + item.size,
        completed_items: completedItems,
        current_item: item.relative_path,
      });
      return saved.result;
    }
    const resumedUploadedBytes = usable ? checkpointTransferredBytes(saved, item.size) : 0;
    if (saved && !usable) {
      await this.store.deleteCheckpoint(batch.batch_id, work.operation_id, subitemId);
    }
    if (usable && saved.creation_pending === true && !saved.server_task_id) {
      throw uploadResultUnknown();
    }
    let writes = Promise.resolve();
    let lastUploadedSize = resumedUploadedBytes;
    let committedState = usable && saved.retain_remote === true
      ? { serverTaskId: saved.server_task_id, source: saved.source }
      : undefined;
    const client = createUploadClient({
      accessToken: credentials.accessToken,
      refreshToken: credentials.refreshToken,
      baseUrl: this.config.apiBaseUrl,
      authBaseUrl: this.config.authBaseUrl,
      clientId: this.config.clientId,
      headers: agentRequestHeaders(this.config.agentName),
      concurrentFileCount: 1,
      onUploadCommitted: async ({ serverTaskId, source }) => {
        committedState = { serverTaskId, source };
        await writes;
        await this.store.recordPendingUploadReceipt(batch.batch_id, work.operation_id, subitemId, {
          server_task_id: serverTaskId, source,
        });
        await this.store.writeCheckpoint(batch.batch_id, work.operation_id, subitemId, {
          account_id: batch.account_id,
          request_id: batch.request_id,
          batch_id: batch.batch_id,
          operation_id: work.operation_id,
          subitem_id: subitemId,
          relative_path: item.relative_path,
          size: item.size,
          mtime_ms: item.mtime_ms,
          progress_base_bytes: completedBytes,
          uploaded_size: item.size,
          server_task_id: serverTaskId,
          retain_remote: true,
          source,
        });
        await this.updateProgress(batch.batch_id, {
          phase: 'VALIDATING',
          current_bytes: completedBytes + item.size,
          current_item: item.relative_path,
        });
      },
      refreshTokens: async (currentRefreshToken, signal) => {
        if (!this.refreshCoordinator) {
          throw new SkillError('TOKEN_REFRESH_UNAVAILABLE', '缺少跨进程 token 刷新协调器');
        }
        const next = await this.refreshCoordinator.run(
          currentRefreshToken,
          async (current) => refreshAccountTokens(
            this.config.authBaseUrl,
            this.config.clientId,
            current.refreshToken,
            this.fetch,
            signal,
            this.config.maxApiResponseBytes,
            agentRequestHeaders(this.config.agentName),
          ),
          session,
        );
        assertCredentialSession(await this.credentials.getSession({ fresh: true }), session);
        return { accessToken: next.accessToken, refreshToken: next.refreshToken };
      },
    });
    this.activeClient = client;
    try {
      if (await this.shouldStop(batch)) {
        throw new SkillError(await this.stopCode(batch), '任务已停止');
      }
      if (resumedUploadedBytes > 0) {
        await this.updateProgress(batch.batch_id, {
          phase: committedState ? 'VALIDATING' : 'UPLOADING',
          current_bytes: completedBytes + resumedUploadedBytes,
          current_item: item.relative_path,
        });
      }
      if (
        !work.item ||
        !Object.hasOwn(work.item, 'target_parent_id') ||
        typeof work.item.target_parent_id !== 'string'
      ) {
        throw new SkillError(
          'INVALID_API_RESPONSE',
          '服务端上传工作缺少 target_parent_id',
        );
      }
      if (item.size === 0 && !(usable && saved.server_task_id)) {
        await this.store.writeCheckpoint(batch.batch_id, work.operation_id, subitemId, {
          account_id: batch.account_id,
          size: item.size,
          mtime_ms: item.mtime_ms,
          relative_path: item.relative_path,
          progress_base_bytes: completedBytes,
          uploaded_size: 0,
          creation_pending: true,
        });
      }
      const job = client.upload({
        filePath: source.path,
        fileHandle: source.reader,
        parentId: work.item.target_parent_id,
        name: path.posix.basename(item.relative_path),
        size: item.size,
        checkpoint: usable ? saved.checkpoint : undefined,
        serverTaskId: usable ? saved.server_task_id : undefined,
        validateOnly: usable && saved.retain_remote === true,
        committedSource: usable ? saved.source : undefined,
        deleteRemoteOnCancel: true,
      });
      this.activeJob = job;
      job.on('progress', (event) => {
        lastUploadedSize = Math.max(lastUploadedSize, Number(event.uploadedSize || 0));
        writes = writes.then(async () => {
          if (event.checkpoint && event.serverTaskId) {
            await this.store.writeCheckpoint(
              batch.batch_id,
              work.operation_id,
              subitemId,
              {
                account_id: batch.account_id,
                request_id: batch.request_id,
                batch_id: batch.batch_id,
                operation_id: work.operation_id,
                subitem_id: subitemId,
                relative_path: item.relative_path,
                size: item.size,
                mtime_ms: item.mtime_ms,
                progress_base_bytes: completedBytes,
                uploaded_size: lastUploadedSize,
                checkpoint: event.checkpoint,
                server_task_id: event.serverTaskId,
                ...(committedState?.serverTaskId === event.serverTaskId
                  ? { retain_remote: true, source: committedState.source }
                  : {}),
              },
            );
          }
          await this.updateProgress(batch.batch_id, {
            phase: committedState ? 'VALIDATING' : 'UPLOADING',
            current_bytes: completedBytes + lastUploadedSize,
            current_item: item.relative_path,
          });
        });
        // 在 SDK 完成前也观察写入失败，稍后 await writes 仍会返回原错误。
        void writes.catch(() => {});
      });
      let done;
      try {
        done = await job.promise;
      } catch (error) {
        await writes;
        if (this.stopping) throw new SkillError('USER_STOPPED', '任务已停止');
        if (error?.code === 'upload_result_unknown') throw uploadResultUnknown(error);
        const hint = client.getResumeHint(job.id);
        if (hint?.serverTaskId && (hint.checkpoint || hint.retainRemote)) {
          await this.store.writeCheckpoint(
            batch.batch_id,
            work.operation_id,
            subitemId,
            {
              account_id: batch.account_id,
              request_id: batch.request_id,
              batch_id: batch.batch_id,
              operation_id: work.operation_id,
              subitem_id: subitemId,
              relative_path: item.relative_path,
              size: item.size,
              mtime_ms: item.mtime_ms,
              progress_base_bytes: completedBytes,
              uploaded_size: lastUploadedSize,
              checkpoint: hint.checkpoint,
              server_task_id: hint.serverTaskId,
              retain_remote: hint.retainRemote === true,
              source: committedState?.source ?? (usable ? saved.source : undefined),
            },
          );
        }
        if (hint?.serverTaskId || sdk.isResumeKeepError(error)) {
          const validatingEmpty = item.size === 0 && hint?.retainRemote === true;
          throw new SkillError(validatingEmpty ? 'UPLOAD_VALIDATION_PENDING' : 'UPLOAD_PAUSED',
            validatingEmpty ? '秒传已提交，文件结果尚未确认' : '上传可从断点恢复', {
            retryable: true,
            cause: error,
            data: hint?.retainRemote === true ? {
              server_task_id: hint.serverTaskId,
              upload_committed: true,
              remote_execution_may_continue: true,
            } : {},
          });
        }
        throw new SkillError('UPLOAD_FAILED', '上传失败', { cause: error });
      }
      await writes;
      this.activeJob = undefined;
      const result = { file: done.fileInfo, subitem_id: subitemId, ...(done.source ? { source: done.source } : {}) };
      await this.store.writeCheckpoint(
        batch.batch_id,
        work.operation_id,
        subitemId,
        {
          account_id: batch.account_id,
          size: item.size,
          mtime_ms: item.mtime_ms,
          relative_path: item.relative_path,
          progress_base_bytes: completedBytes,
          progress_completed_items: Number(batch.progress?.completed_items || 0) + 1,
          completed: true,
          result,
        },
      );
      await this.updateProgress(batch.batch_id, {
        phase: 'VALIDATING',
        current_bytes: completedBytes + item.size,
        completed_items: Number(batch.progress?.completed_items || 0) + 1,
        current_item: item.relative_path,
      });
      return result;
    } finally {
      await client.close();
      this.activeJob = undefined;
      this.activeClient = undefined;
    }
  }

  async download(batch, work, plan, driveApi = this.files) {
    const item = work.item;
    if (!item?.relative_path || !['file', 'directory'].includes(item.type)) {
      throw new SkillError('INVALID_API_RESPONSE', '服务端下载工作不完整');
    }
    for (const segment of item.relative_path.split('/')) {
      assertRemoteName(segment, this.config.platform);
    }
    const requestedOutput = path.join(plan.local_dir, ...item.relative_path.split('/'));
    assertWithin(plan.local_dir, requestedOutput);
    const expectedDirectories = [];
    const parents = item.relative_path.split('/').slice(0, -1);
    for (let depth = 1; depth <= parents.length; depth += 1) {
      const relativePath = parents.slice(0, depth).join('/');
      const marker = await this.store.readCheckpoint(batch.batch_id, work.operation_id, transferItemId(relativePath));
      if (marker?.directory_created !== true || marker.relative_path !== relativePath || !validDownloadIdentity(marker.directory_identity)) {
        throw new SkillError('INVALID_LOCAL_STATE', '下载子项的父目录缺少已确认的目录身份');
      }
      expectedDirectories.push({ relative_path: relativePath, ...marker.directory_identity });
    }
    const directory = await guardDownloadPath(plan, requestedOutput, expectedDirectories);
    const output = directory.output;
    const subitemId = item.subitem_id || transferItemId(item.relative_path);
    if (item.type === 'directory') {
      const marker = await this.store.readCheckpoint(
        batch.batch_id,
        work.operation_id,
        subitemId,
      );
      if (marker?.directory_created === true) {
        if (!validDownloadIdentity(marker.directory_identity)) {
          throw new SkillError('INVALID_LOCAL_STATE', '下载目录断点缺少有效的目录身份，不能复用现有目录');
        }
        await directory.check();
        const stat = await fs.lstat(output).catch((error) => {
          if (error.code === 'ENOENT') return undefined;
          throw error;
        });
        if (stat?.isDirectory() && !stat.isSymbolicLink()) {
          if (marker.directory_identity.device !== String(stat.dev) || marker.directory_identity.inode !== String(stat.ino)) {
            throw new SkillError('LOCAL_TARGET_CONFLICT', '下载目录已被替换，不能复用其他目录');
          }
          return { directory: item.relative_path, subitem_id: subitemId };
        }
        throw new SkillError('LOCAL_TARGET_CONFLICT', '下载目录已消失或被替换，不能继续写入');
      }
      // A pending marker only proves intent. A crash before saving the created
      // directory's identity cannot authorize adopting someone else's directory.
      await ensureAbsent(output);
      await this.store.writeCheckpoint(
        batch.batch_id,
        work.operation_id,
        subitemId,
        { directory_pending: true, relative_path: item.relative_path },
      );
      let directoryIdentity;
      try {
        directoryIdentity = await directory.run(async () => {
          await fs.mkdir(output, { mode: 0o700 });
          const stat = await fs.lstat(output);
          if (!stat.isDirectory() || stat.isSymbolicLink()) {
            throw new SkillError('LOCAL_TARGET_CONFLICT', '下载目录在创建后发生变化');
          }
          return { device: String(stat.dev), inode: String(stat.ino) };
        });
      } catch (cause) {
        if (cause?.code === 'EEXIST') {
          throw new SkillError('LOCAL_TARGET_CONFLICT', DOWNLOAD_TARGET_EXISTS_MESSAGE, { cause });
        }
        throw cause;
      }
      await this.store.writeCheckpoint(
        batch.batch_id,
        work.operation_id,
        subitemId,
        { directory_created: true, directory_identity: directoryIdentity, relative_path: item.relative_path },
      );
      return { directory: item.relative_path, subitem_id: subitemId };
    }
    if (!item.remote_id || !Number.isSafeInteger(item.size) || item.size < 0) {
      throw new SkillError('INVALID_API_RESPONSE', '服务端下载文件属性非法');
    }
    const completedBytes = Number(batch.progress?.current_bytes || 0);
    return this.downloadFile(
      batch,
      work,
      {
        fileId: item.remote_id,
        fileSize: item.size,
        md5: item.md5,
        relative_path: item.relative_path,
      },
      output,
      completedBytes,
      directory,
      driveApi,
    );
  }

  async downloadFile(batch, work, item, output, currentBytes, directory, driveApi = this.files) {
    await directory.check();
    const subitemId = work.item?.subitem_id || transferItemId(item.relative_path);
    const saved = await this.store.readCheckpoint(batch.batch_id, work.operation_id, subitemId);
    const checkpointMatches = saved?.account_id === batch.account_id && saved?.remote_id === item.fileId;
    const completedBaseBytes = progressBase(currentBytes, checkpointMatches ? saved : undefined);
    if (checkpointMatches && saved.completed === true && saved.result) {
      if (!validDownloadIdentity(saved.output_identity) || !validContentHash(saved.content_sha256)) {
        throw new SkillError('INVALID_LOCAL_STATE', '已完成下载断点缺少有效的目标身份或内容摘要');
      }
      await verifyDownloadedOutput(output, item, saved.output_identity, saved.content_sha256, directory);
      const completedItems = this.completedItemsTarget(saved);
      await this.updateProgress(batch.batch_id, {
        phase: 'DOWNLOADING',
        current_bytes: completedBaseBytes + Number(item.fileSize),
        completed_items: completedItems,
        current_item: item.relative_path,
      });
      return saved.result;
    }
    const ownedTemporary = checkpointMatches
      ? ownedDownloadTemporaryPath(saved, output, batch.batch_id, subitemId)
      : undefined;
    const savedMatches = Boolean(ownedTemporary);
    const temporary = ownedTemporary ?? downloadTemporaryPath(output, batch.batch_id, subitemId);
    const baseBytes = progressBase(currentBytes, savedMatches ? saved : undefined);
    if (savedMatches && saved.publishing === true) {
      const recovered = await recoverPublishedDownload(
        output,
        temporary,
        item,
        saved,
        directory,
      );
      if (recovered) {
        const result = {
          file_id: item.fileId,
          relative_path: item.relative_path,
          bytes: Number(item.fileSize),
          subitem_id: subitemId,
        };
        await this.store.writeCheckpoint(batch.batch_id, work.operation_id, subitemId, {
          account_id: batch.account_id,
          remote_id: item.fileId,
          relative_path: item.relative_path,
          progress_base_bytes: baseBytes,
          progress_completed_items: Number(batch.progress?.completed_items || 0) + 1,
          completed: true,
          result,
          output_identity: recovered,
          content_sha256: saved.content_sha256,
        });
        await this.updateProgress(batch.batch_id, {
          phase: 'DOWNLOADING',
          current_bytes: baseBytes + Number(item.fileSize),
          completed_items: Number(batch.progress?.completed_items || 0) + 1,
          current_item: item.relative_path,
        });
        return result;
      }
    }
    await ensureAbsent(output);
    let temporaryStat;
    let offset = 0;
    if (savedMatches) {
      temporaryStat = await inspectDownloadTemporary(temporary, saved.temporary_identity);
      offset = temporaryStat?.size ?? 0;
    } else {
      await ensureTemporaryAbsent(temporary);
    }
    const expectedSize = Number(item.fileSize);
    if (savedMatches && (offset > expectedSize || offset === 0 || saved.restart_required === true)) {
      temporaryStat = await resetDownloadTemporary(temporary, saved.temporary_identity, directory);
      offset = 0;
    }
    let written = offset;
    let downloadRequestId = savedMatches ? saved.download_request_id : undefined;
    const discardTemporary = async () => {
      const identity = downloadTemporaryIdentity(temporaryStat);
      // Retain ownership and the restart instruction even if cleanup is denied.
      // A retry must neither append to rejected content nor treat it as foreign.
      await this.store.writeCheckpoint(batch.batch_id, work.operation_id, subitemId, {
        account_id: batch.account_id,
        remote_id: item.fileId,
        relative_path: item.relative_path,
        bytes: written,
        progress_base_bytes: baseBytes,
        temporary_path: temporary,
        temporary_identity: identity,
        download_request_id: downloadRequestId,
        restart_required: true,
      });
      await removeDownloadTemporary(temporary, identity, directory);
    };
    if (offset < expectedSize) {
      const link = await driveApi.downloadUrl(
        item.fileId,
        savedMatches && typeof saved.download_request_id === 'string'
          ? saved.download_request_id
          : '',
      );
      const signedUrl = link.data?.signedURL;
      if (typeof signedUrl !== 'string' || !signedUrl) {
        throw new SkillError('DOWNLOAD_URL_MISSING', '服务端未返回下载地址');
      }
      this.downloadAbort = new AbortController();
      let response;
      try {
        response = await this.fetch(signedUrl, {
          headers: offset ? { Range: `bytes=${offset}-` } : {},
          redirect: 'error',
          signal: this.downloadAbort.signal,
        });
      } catch (cause) {
        if (this.stopping || await this.shouldStop(batch)) {
          throw new SkillError(await this.stopCode(batch), '任务已停止', { cause });
        }
        throw new SkillError('DOWNLOAD_FAILED', '下载网络请求失败', {
          retryable: true,
          cause,
        });
      }
      if (!response.ok || (!offset && response.status !== 200) || (offset && response.status !== 206 && response.status !== 200)) {
        throw new SkillError('DOWNLOAD_FAILED', `下载失败: HTTP ${response.status}`, { retryable: response.status >= 500 });
      }
      if (offset && response.status === 206) {
        const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers.get('content-range') || '');
        if (!match || Number(match[1]) !== offset || Number(match[3]) !== expectedSize) {
          throw new SkillError('DOWNLOAD_RANGE_MISMATCH', '下载断点响应范围不匹配', {
            retryable: true,
          });
        }
      }
      if (offset && response.status === 200) {
        offset = 0;
        written = 0;
        temporaryStat = await resetDownloadTemporary(temporary, saved.temporary_identity, directory);
      }
      downloadRequestId = typeof link.data?.requestId === 'string'
        ? link.data.requestId
        : downloadRequestId;
      if (!response.body) {
        throw new SkillError('INVALID_API_RESPONSE', '下载响应缺少内容');
      }
      await directory.check();
      const { handle, stat } = await openDownloadTemporary(temporary, temporaryStat);
      temporaryStat = stat;
      const temporaryIdentity = downloadTemporaryIdentity(stat);
      let persistedBytes = written;
      let lastStateFlushAt = Date.now();
      const flushDownloadState = async (force = false) => {
        if (written === persistedBytes) return;
        const now = Date.now();
        if (
          !force &&
          written - persistedBytes < DOWNLOAD_STATE_FLUSH_BYTES &&
          now - lastStateFlushAt < DOWNLOAD_STATE_FLUSH_INTERVAL_MS
        ) return;
        await handle.sync();
        await this.store.writeCheckpoint(batch.batch_id, work.operation_id, subitemId, {
          account_id: batch.account_id,
          remote_id: item.fileId,
          relative_path: item.relative_path,
          bytes: written,
          progress_base_bytes: baseBytes,
          temporary_path: temporary,
          temporary_identity: temporaryIdentity,
          download_request_id: downloadRequestId,
        });
        await this.updateProgress(batch.batch_id, {
          phase: 'DOWNLOADING',
          current_bytes: baseBytes + written,
          current_item: item.relative_path,
        });
        persistedBytes = written;
        lastStateFlushAt = now;
      };
      let streamError;
      try {
        await directory.check();
        await this.store.writeCheckpoint(batch.batch_id, work.operation_id, subitemId, {
          account_id: batch.account_id,
          remote_id: item.fileId,
          relative_path: item.relative_path,
          bytes: written,
          progress_base_bytes: baseBytes,
          temporary_path: temporary,
          temporary_identity: temporaryIdentity,
          download_request_id: downloadRequestId,
        });
        for await (const chunk of response.body) {
          if (await this.shouldStop(batch)) {
            this.downloadAbort.abort();
            throw new SkillError(await this.stopCode(batch), '任务已停止');
          }
          written += await writeAll(handle, chunk);
          if (written > expectedSize) {
            this.downloadAbort.abort();
            throw new SkillError('DOWNLOAD_SIZE_MISMATCH', '下载文件大小校验失败', { retryable: true });
          }
          await flushDownloadState();
        }
        await flushDownloadState(true);
      } catch (error) {
        streamError = error;
        if (written <= expectedSize) {
          await flushDownloadState(true).catch(() => {});
        }
      } finally {
        await handle.close();
        this.downloadAbort = undefined;
      }
      if (streamError) {
        if (written > expectedSize) {
          await discardTemporary();
        }
        if (this.stopping || await this.shouldStop(batch)) {
          throw new SkillError(await this.stopCode(batch), '任务已停止', { cause: streamError });
        }
        throw streamError;
      }
    } else if (expectedSize === 0) {
      await directory.check();
      const { handle, stat } = await openDownloadTemporary(temporary, temporaryStat);
      temporaryStat = stat;
      try {
        await handle.sync();
      } finally {
        await handle.close();
      }
    }
    if (written !== expectedSize) {
      throw new SkillError('DOWNLOAD_SIZE_MISMATCH', '下载文件大小校验失败', { retryable: true });
    }
    await directory.check();
    await inspectDownloadTemporary(temporary, downloadTemporaryIdentity(temporaryStat), true);
    const hasMd5 = typeof item.md5 === 'string' && /^[0-9a-f]{32}$/i.test(item.md5);
    const hashes = await hashDownloadFile(temporary, downloadTemporaryIdentity(temporaryStat), hasMd5);
    if (hasMd5) {
      if (hashes.md5 !== item.md5.toLowerCase()) {
        await discardTemporary();
        throw new SkillError('DOWNLOAD_HASH_MISMATCH', '下载文件内容校验失败', {
          retryable: true,
        });
      }
    }
    let publishingCheckpoint = {
      account_id: batch.account_id,
      remote_id: item.fileId,
      relative_path: item.relative_path,
      bytes: written,
      progress_base_bytes: baseBytes,
      temporary_path: temporary,
      temporary_identity: downloadTemporaryIdentity(temporaryStat),
      download_request_id: downloadRequestId,
      publishing: true,
      publishing_mode: 'link',
      content_sha256: hashes.sha256,
    };
    await this.store.writeCheckpoint(
      batch.batch_id,
      work.operation_id,
      subitemId,
      publishingCheckpoint,
    );
    try {
      await directory.run(async () => {
        await inspectDownloadTemporary(temporary, publishingCheckpoint.temporary_identity, true);
        await this.publicationFs.link(temporary, output);
      });
    } catch (cause) {
      if (cause?.code === 'EEXIST') {
        throw new SkillError('LOCAL_TARGET_CONFLICT', DOWNLOAD_TARGET_EXISTS_MESSAGE, { cause });
      }
      if (!isHardLinkUnavailable(cause)) {
        if (cause instanceof SkillError) throw cause;
        throw new SkillError('DOWNLOAD_PUBLISH_FAILED', '无法发布下载文件', {
          cause,
          retryable: true,
        });
      }
      publishingCheckpoint = { ...publishingCheckpoint, publishing_mode: 'copy' };
      await this.store.writeCheckpoint(
        batch.batch_id,
        work.operation_id,
        subitemId,
        publishingCheckpoint,
      );
      try {
        await directory.run(async () => {
          await inspectDownloadTemporary(temporary, publishingCheckpoint.temporary_identity, true);
          await this.publicationFs.copyFile(temporary, output, fsConstants.COPYFILE_EXCL);
        });
      } catch (copyCause) {
        if (copyCause?.code === 'EEXIST') {
          throw new SkillError('LOCAL_TARGET_CONFLICT', DOWNLOAD_TARGET_EXISTS_MESSAGE, {
            cause: copyCause,
          });
        }
        if (copyCause instanceof SkillError) throw copyCause;
        throw new SkillError('DOWNLOAD_PUBLISH_FAILED', '无法发布下载文件', {
          cause: copyCause,
          retryable: true,
        });
      }
    }
    const outputIdentity = await recoverPublishedDownload(output, temporary, item, publishingCheckpoint, directory);
    if (!outputIdentity) throw new SkillError('LOCAL_TARGET_CONFLICT', '发布后的下载目标已消失');
    const result = {
      file_id: item.fileId,
      relative_path: item.relative_path,
      bytes: written,
      subitem_id: subitemId,
    };
    await this.store.writeCheckpoint(
      batch.batch_id,
      work.operation_id,
      subitemId,
      {
        account_id: batch.account_id,
        remote_id: item.fileId,
        relative_path: item.relative_path,
        progress_base_bytes: baseBytes,
        progress_completed_items: Number(batch.progress?.completed_items || 0) + 1,
        completed: true,
        result,
        output_identity: outputIdentity,
        content_sha256: hashes.sha256,
      },
    );
    await this.updateProgress(batch.batch_id, {
      phase: 'DOWNLOADING',
      current_bytes: baseBytes + written,
      completed_items: Number(batch.progress?.completed_items || 0) + 1,
      current_item: item.relative_path,
    });
    return result;
  }

  async shouldStop(batch) {
    return this.store.isStopRequested(batch.batch_id, batch.account_generation);
  }

  async stopCode(batch) {
    return await this.store.stopCode(batch.batch_id, batch.account_generation) || 'USER_STOPPED';
  }

  completedItemsTarget(checkpoint) {
    const stored = checkpoint.progress_completed_items;
    if (Number.isSafeInteger(stored) && stored > 0) return stored;
    throw new SkillError('INVALID_LOCAL_STATE', '已完成断点缺少有效的完成项进度');
  }

  async updateProgress(batchId, delta) {
    await this.store.updateBatch(batchId, (current) => {
      const progress = { ...(current.progress ?? {}) };
      if (delta.current_bytes !== undefined) {
        progress.current_bytes = Math.max(progress.current_bytes ?? 0, delta.current_bytes);
      }
      if (delta.completed_items !== undefined) {
        progress.completed_items = Math.max(progress.completed_items ?? 0, delta.completed_items);
      } else if (delta.completed_items_delta) {
        progress.completed_items = (progress.completed_items ?? 0) + delta.completed_items_delta;
      }
      progress.phase = delta.phase;
      progress.current_item = delta.current_item;
      progress.updated_at = new Date().toISOString();
      if (progress.total_bytes) progress.percent = Math.min(100, Math.floor(progress.current_bytes * 100 / progress.total_bytes));
      return { ...current, transfer_state: 'RUNNING', progress };
    });
  }
}

function downloadTemporaryIdentity(stat) {
  return { device: String(stat.dev), inode: String(stat.ino) };
}

async function inspectDownloadTemporary(file, identity, required = false) {
  if (!validDownloadIdentity(identity)) throw temporaryConflict();
  let stat;
  try {
    stat = await fs.lstat(file);
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
    if (!required) return undefined;
  }
  if (
    !stat?.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 ||
    !identity || identity.device !== String(stat.dev) || identity.inode !== String(stat.ino)
  ) {
    throw new SkillError('LOCAL_TEMP_CONFLICT', '下载临时文件已被替换或不是独立的普通文件，未继续写入');
  }
  return stat;
}

async function removeDownloadTemporary(file, identity, directory) {
  await directory.run(async () => {
    if (await inspectDownloadTemporary(file, identity)) await removeDisposableFile(file);
  });
}

async function resetDownloadTemporary(file, identity, directory) {
  return directory.run(async () => {
    const current = await inspectDownloadTemporary(file, identity);
    if (!current) return undefined;
    const { handle } = await openDownloadTemporary(file, current);
    try {
      // Reset only the verified, exclusively owned inode, without deleting it.
      await directory.check();
      await inspectDownloadTemporary(file, identity, true);
      const beforeReset = await handle.stat();
      if (!beforeReset.isFile() || beforeReset.nlink !== 1 ||
          !matchesDownloadIdentity(beforeReset, identity) || beforeReset.size !== current.size) {
        throw temporaryConflict();
      }
      await handle.truncate(0);
      await handle.sync();
      await inspectDownloadTemporary(file, identity, true);
      return await handle.stat();
    } finally {
      await handle.close();
    }
  });
}

async function openDownloadTemporary(file, existingStat) {
  const flags = fsConstants.O_WRONLY | (fsConstants.O_NOFOLLOW ?? 0) |
    (existingStat ? fsConstants.O_APPEND : fsConstants.O_CREAT | fsConstants.O_EXCL);
  let handle;
  try {
    handle = await fs.open(file, flags, 0o600);
    const stat = await handle.stat();
    if (
      !stat.isFile() || stat.nlink !== 1 ||
      existingStat && (
        stat.dev !== existingStat.dev || stat.ino !== existingStat.ino || stat.size !== existingStat.size
      )
    ) {
      throw new SkillError('LOCAL_TEMP_CONFLICT', '下载临时文件在打开前发生变化，未继续写入');
    }
    return { handle, stat };
  } catch (cause) {
    await handle?.close();
    if (['ELOOP', 'EMLINK', 'EEXIST', 'ENOENT'].includes(cause?.code)) {
      throw new SkillError('LOCAL_TEMP_CONFLICT', '下载临时文件在打开前发生变化，未继续写入', { cause });
    }
    throw cause;
  }
}

async function hashDownloadFile(file, identity, withMd5 = false) {
  const handle = await fs.open(file, fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW ?? 0));
  try {
    const stat = await handle.stat();
    if (!stat.isFile() || !matchesDownloadIdentity(stat, identity)) throw temporaryConflict();
    const sha256 = createHash('sha256');
    const md5 = withMd5 ? createHash('md5') : undefined;
    for await (const chunk of handle.createReadStream({ autoClose: false })) {
      sha256.update(chunk);
      md5?.update(chunk);
    }
    return { sha256: sha256.digest('hex'), md5: md5?.digest('hex') };
  } finally {
    await handle.close();
  }
}

async function recoverPublishedDownload(output, temporary, item, checkpoint, directory) {
  const { publishing_mode: publishingMode, temporary_identity: identity, content_sha256: contentHash } = checkpoint;
  if (!['link', 'copy'].includes(publishingMode) || !validContentHash(contentHash)) {
    throw new SkillError('INVALID_LOCAL_STATE', '发布断点缺少有效的发布方式或内容摘要');
  }
  if (!validDownloadIdentity(identity)) throw temporaryConflict();
  await directory.check();
  const outputStat = await optionalFileStat(output);
  if (!outputStat) return false;
  if (!outputStat.isFile() || outputStat.isSymbolicLink()) {
    throw new SkillError('LOCAL_TARGET_CONFLICT', DOWNLOAD_TARGET_EXISTS_MESSAGE);
  }
  const temporaryStat = await optionalFileStat(temporary);
  const checkTemporary = (stat) => {
    if (stat && (!stat.isFile() || stat.isSymbolicLink() || !matchesDownloadIdentity(stat, identity) ||
        stat.nlink !== (publishingMode === 'link' ? 2 : 1))) throw temporaryConflict();
  };
  checkTemporary(temporaryStat);
  if (outputStat.size !== Number(item.fileSize) ||
      publishingMode === 'copy' && outputStat.nlink !== 1) {
    throw new SkillError('LOCAL_TARGET_CONFLICT', DOWNLOAD_TARGET_EXISTS_MESSAGE);
  }
  if (publishingMode === 'link' && !matchesDownloadIdentity(outputStat, identity)) {
    throw new SkillError('LOCAL_TARGET_CONFLICT', '下载目标与临时文件不一致');
  }
  const outputIdentity = downloadTemporaryIdentity(outputStat);
  await verifyDownloadedOutput(output, item, outputIdentity, contentHash, directory);
  await directory.run(async () => {
    const currentOutput = await optionalFileStat(output);
    if (!currentOutput || currentOutput.isSymbolicLink() ||
        !matchesDownloadIdentity(currentOutput, downloadTemporaryIdentity(outputStat))) {
      throw new SkillError('LOCAL_TARGET_CONFLICT', '下载目标在恢复时发生变化');
    }
    const currentTemporary = await optionalFileStat(temporary);
    checkTemporary(currentTemporary);
    if (currentTemporary) await removeDisposableFile(temporary);
  });
  return outputIdentity;
}

async function verifyDownloadedOutput(output, item, identity, contentHash, directory) {
  await directory.check();
  const stat = await optionalFileStat(output);
  if (!stat?.isFile() || stat.isSymbolicLink() || stat.size !== Number(item.fileSize) ||
      !matchesDownloadIdentity(stat, identity)) {
    throw new SkillError('LOCAL_TARGET_CONFLICT', '下载目标已被替换或发生变化');
  }
  const hashes = await hashDownloadFile(output, identity);
  if (hashes.sha256 !== contentHash) {
    throw new SkillError('DOWNLOAD_HASH_MISMATCH', '已发布下载文件内容校验失败');
  }
  await directory.check();
  const current = await optionalFileStat(output);
  if (!current?.isFile() || current.isSymbolicLink() || current.size !== stat.size ||
      !matchesDownloadIdentity(current, identity)) {
    throw new SkillError('LOCAL_TARGET_CONFLICT', '下载目标在校验时发生变化');
  }
}

function validContentHash(value) {
  return typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
}

function validDownloadIdentity(identity) {
  return typeof identity?.device === 'string' && /^\d+$/.test(identity.device) &&
    typeof identity?.inode === 'string' && /^\d+$/.test(identity.inode);
}

function matchesDownloadIdentity(stat, identity) {
  return validDownloadIdentity(identity) &&
    identity.device === String(stat.dev) && identity.inode === String(stat.ino);
}

function temporaryConflict() {
  return new SkillError('LOCAL_TEMP_CONFLICT', '下载临时文件身份缺失或已被替换，未继续写入或清理');
}

async function optionalFileStat(file) {
  try { return await fs.lstat(file); }
  catch (error) { if (error?.code === 'ENOENT') return undefined; throw error; }
}

function isHardLinkUnavailable(error) {
  return ['EPERM', 'EACCES', 'ENOTSUP', 'EOPNOTSUPP', 'EXDEV', 'EINVAL'].includes(error?.code);
}

function uploadResultUnknown(cause) {
  return new SkillError('UPLOAD_RESULT_UNKNOWN', '上传创建结果未知，请核对远端文件后再决定是否重新上传', {
    cause,
    data: { upload_result_unknown: true, remote_execution_may_continue: true },
  });
}

function progressBase(currentBytes, checkpoint) {
  const value = checkpoint === undefined ? currentBytes ?? 0 : checkpoint.progress_base_bytes;
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new SkillError('INVALID_LOCAL_STATE', '传输进度或断点进度基数缺失或非法');
  }
  return value;
}

function checkpointTransferredBytes(checkpoint, itemSize) {
  const transferred = checkpoint.uploaded_size;
  if (!Number.isSafeInteger(transferred) || transferred < 0 || transferred > itemSize) {
    throw new SkillError('INVALID_LOCAL_STATE', '上传断点缺少有效的已上传字节数');
  }
  return transferred;
}

async function refreshAccountTokens(
  authBaseUrl,
  clientId,
  refreshToken,
  fetchImpl,
  signal,
  maxResponseBytes,
  agentHeaders,
) {
  let response;
  try {
    response = await fetchImpl(`${authBaseUrl.replace(/\/+$/, '')}/v1/auth/token`, {
      method: 'POST',
      headers: {
        ...agentHeaders,
        'Content-Type': 'application/json',
        'x-client-id': clientId,
        'x-sdk-version': CLIENT_VERSION,
        'x-protocol-version': '301',
        'x-action': '401',
      },
      body: JSON.stringify({
        grant_type: 'refresh_token',
        refresh_token: refreshToken,
        client_id: clientId,
      }),
      signal,
    });
  } catch (cause) {
    if (signal?.aborted) throw cause;
    throw new SkillError('API_UNAVAILABLE', '账号服务网络请求失败', {
      retryable: true,
      cause,
    });
  }
  const data = await parseBoundedJsonResponse(response, maxResponseBytes).catch((error) => {
    if (error?.code === 'API_RESPONSE_TOO_LARGE') throw error;
    return {};
  });
  if (!response.ok || !data.access_token || !data.refresh_token) {
    throw new SkillError('LOGIN_REQUIRED', '账号刷新失败');
  }
  return { accessToken: data.access_token, refreshToken: data.refresh_token };
}

export async function writeAll(handle, chunk) {
  let offset = 0;
  while (offset < chunk.byteLength) {
    const remaining = chunk.byteLength - offset;
    const result = await handle.write(chunk, offset, remaining);
    const bytesWritten = Number(result?.bytesWritten);
    if (!Number.isSafeInteger(bytesWritten) || bytesWritten <= 0 || bytesWritten > remaining) {
      throw new SkillError('DOWNLOAD_WRITE_FAILED', '下载文件写入不完整', { retryable: true });
    }
    offset += bytesWritten;
  }
  return offset;
}

function assertWithin(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  if (isOutsideRelativePath(relative)) {
    throw new SkillError('INVALID_REMOTE_PATH', '远端路径逃逸下载目录');
  }
}

async function ensureAbsent(value) {
  try {
    await fs.lstat(value);
  } catch (error) {
    if (error?.code === 'ENOENT') return;
    throw error;
  }
  throw new SkillError('LOCAL_TARGET_CONFLICT', DOWNLOAD_TARGET_EXISTS_MESSAGE);
}

async function ensureTemporaryAbsent(value) {
  try {
    await fs.lstat(value);
  } catch (error) {
    if (error?.code === 'ENOENT') return;
    throw error;
  }
  throw new SkillError('LOCAL_TEMP_CONFLICT', '下载临时文件已存在，未覆盖或删除该文件');
}
