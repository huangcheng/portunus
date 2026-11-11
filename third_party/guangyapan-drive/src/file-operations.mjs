import { createHash } from 'node:crypto';
import path from 'node:path';
import {
  normalizeCloudResource,
  readTorrentFile,
  executeCloudCollectionGroup,
  cloudDeleteResult,
} from './cloud-collection.mjs';
import { accountRequestGuard } from './credential-store.mjs';
import { assertRemoteName, transferItemId } from './local-files.mjs';
import { SkillError, parseShareReference, toSkillError } from './protocol.mjs';

const MAX_TEXT_BYTES = 1024 * 1024;
const MAX_TEXT_CHARS = 200_000;

export class ReadExecutor {
  constructor(driveApi, fetchImpl = fetch, timeoutMs = 30_000, config = {}) {
    this.files = driveApi;
    this.fetch = fetchImpl;
    this.timeoutMs = timeoutMs;
    this.config = config;
  }

  async execute(request, { requestGuard } = {}) {
    if (requestGuard) {
      const scoped = new ReadExecutor(this.files.withRequestGuard(requestGuard), this.fetch, this.timeoutMs, this.config);
      const items = await scoped.execute(request);
      // Signed text downloads bypass the API client; do not deliver their late
      // results if the user logged out or switched sessions while fetching.
      await requestGuard(() => undefined);
      return items;
    }
    const items = [];
    let totalTextBytes = 0;
    let totalTextChars = 0;
    for (const operation of request.operations) {
      try {
        let result;
        switch (operation.type) {
          case 'list':
            if (operation.remote_parent_id !== '*') await this.validateRemoteDirectory(operation.remote_parent_id);
            result = pageResult(await this.files.list(operation), operation, true);
            break;
          case 'search':
            await this.validateRemoteDirectory(operation.remote_parent_id);
            result = pageResult(await this.files.search(operation), operation, true);
            break;
          case 'search_share':
            result = pageResult(
              await this.files.searchShare(operation, parseShareReference),
              operation,
              true,
            );
            break;
          case 'return_text': {
            const text = await this.returnText(operation);
            totalTextBytes += text.bytes;
            totalTextChars += text.content.length;
            if (totalTextBytes > MAX_TEXT_BYTES || totalTextChars > MAX_TEXT_CHARS) {
              throw new SkillError('TEXT_LIMIT_EXCEEDED', '文本内容超过 V1 批次限制');
            }
            result = text;
            break;
          }
          case 'resolve_cloud_collection':
            result = normalizeCloudResource(
              await this.files.resolveCloudCollectionResource(
                operation,
                operation.torrent_path ? await readTorrentFile(operation.torrent_path) : undefined,
              ),
              operation.url,
              this.config,
              operation.torrent_path ? 2 : undefined,
            );
            break;
          case 'account_rights': {
            const [assets, rights] = await Promise.all([
              this.files.getAccountAssets(),
              this.files.getAccountRights(),
            ]);
            result = accountRightsResult(assets.data, rights.data);
            break;
          }
          default:
            throw new SkillError('INVALID_REQUEST', '只读执行器收到写操作');
        }
        items.push({
          operation_id: operation.id,
          state: 'SUCCEEDED',
          content_trust: 'untrusted',
          result,
        });
      } catch (error) {
        if (!(error instanceof SkillError)) throw error;
        items.push({
          operation_id: operation.id,
          state: 'FAILED',
          code: error.code,
          message: error.message,
          retryable: error.retryable,
          error: { ...error.data, content_trust: error.data.content_trust || 'untrusted' },
        });
      }
    }
    return items;
  }

  async validateRemoteDirectory(remoteParentId) {
    if (!remoteParentId) return;
    const envelope = await this.files.getInfo(remoteParentId);
    const file = envelope?.data?.fileInfo ?? envelope?.data;
    if (!file?.fileId) throw new SkillError('REMOTE_FILE_NOT_FOUND', '远端目录不存在');
    if (Number(file.resType) !== 2) {
      throw new SkillError('REMOTE_TARGET_NOT_DIRECTORY', '远端对象不是目录');
    }
  }

  async returnText(operation) {
    const info = await this.files.getInfo(operation.remote_id);
    const file = info.data;
    if (!file || file.resType === 2) {
      throw new SkillError('NOT_A_TEXT_FILE', 'return_text 只支持文件');
    }
    if (Number(file.fileSize) > MAX_TEXT_BYTES) {
      throw new SkillError('TEXT_LIMIT_EXCEEDED', '文本文件超过 1 MiB');
    }
    const link = await this.files.downloadUrl(operation.remote_id, '');
    const signedUrl = link.data?.signedURL;
    if (!signedUrl) throw new SkillError('DOWNLOAD_URL_MISSING', '服务端未返回下载地址');
    const timeout = new AbortController();
    const timer = setTimeout(
      () => timeout.abort(new DOMException('Request timed out', 'TimeoutError')),
      this.timeoutMs,
    );
    let response;
    try {
      response = await this.fetch(signedUrl, {
        redirect: 'error',
        signal: timeout.signal,
      });
    } catch (cause) {
      clearTimeout(timer);
      if (timeout.signal.aborted) {
        throw new SkillError('API_TIMEOUT', '读取文本超时', {
          retryable: true,
          cause,
        });
      }
      throw new SkillError('DOWNLOAD_FAILED', '读取文本网络请求失败', {
        retryable: true,
        cause,
      });
    }
    if (!response.ok) {
      clearTimeout(timer);
      throw new SkillError('DOWNLOAD_FAILED', `读取文本失败: HTTP ${response.status}`, {
        retryable: response.status >= 500,
      });
    }
    const declared = Number(response.headers.get('content-length'));
    if (Number.isFinite(declared) && declared > MAX_TEXT_BYTES) {
      clearTimeout(timer);
      timeout.abort();
      throw new SkillError('TEXT_LIMIT_EXCEEDED', '文本文件超过 1 MiB');
    }
    if (!response.body) {
      clearTimeout(timer);
      throw new SkillError('INVALID_API_RESPONSE', '文本下载响应缺少内容');
    }
    const chunks = [];
    let received = 0;
    try {
      for await (const chunk of response.body) {
        received += chunk.length;
        if (received > MAX_TEXT_BYTES) {
          timeout.abort();
          throw new SkillError('TEXT_LIMIT_EXCEEDED', '文本文件超过 1 MiB');
        }
        chunks.push(chunk);
      }
    } finally {
      clearTimeout(timer);
    }
    const bytes = Buffer.concat(chunks, received);
    let content;
    try {
      content = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    } catch (cause) {
      throw new SkillError('TEXT_DECODE_FAILED', '文本不是合法 UTF-8', { cause });
    }
    if (content.length > MAX_TEXT_CHARS) {
      throw new SkillError('TEXT_LIMIT_EXCEEDED', '文本字符数超过 200,000');
    }
    return {
      content_trust: 'untrusted',
      artifact_id: file.fileId,
      name: file.fileName,
      content,
      sha256: createHash('sha256').update(bytes).digest('hex'),
      bytes: bytes.length,
    };
  }
}

function accountRightsResult(assets = {}, rights = {}) {
  assets = objectValue(assets) ?? {};
  rights = objectValue(rights) ?? {};
  const totalSpace = byteValue(assets.totalSpaceSize);
  const usedSpace = byteValue(assets.usedSpaceSize);
  const uploadLimit = ownByteValue(assets, 'monthNonFlashUploadLimitSize');
  const uploadUsed = ownByteValue(assets, 'monthNonFlashUploadSize');
  const uploadReported = uploadLimit !== null && uploadUsed !== null;
  const highSpeed = objectValue(assets.highSpeedTraffic);
  const vipStatus = integerValue(assets.vipStatus);
  const svipStatus = integerValue(assets.svipStatus);
  const vipExpiresAt = integerValue(assets.vipExpireTime);
  const serverTime = integerValue(assets.systemTime);
  const playbackSpeedPercent = integerValue(rights.playbackSpeed);

  return {
    content_trust: 'untrusted',
    space: {
      total_bytes: decimalString(totalSpace),
      used_bytes: decimalString(usedSpace),
      remaining_bytes: decimalString(nonNegativeDifference(totalSpace, usedSpace)),
    },
    upload_traffic: {
      status: uploadReported ? 'available' : 'not_reported_by_standard_api',
      reported: uploadReported,
      total_bytes: uploadReported ? decimalString(uploadLimit) : null,
      used_bytes: uploadReported ? decimalString(uploadUsed) : null,
      remaining_bytes: uploadReported
        ? decimalString(nonNegativeDifference(uploadLimit, uploadUsed))
        : null,
    },
    high_speed_download_traffic: {
      status: highSpeed !== null
        ? 'available'
        : vipStatus === 2
          ? 'not_reported_for_active_vip'
          : 'not_reported',
      reported: highSpeed !== null,
      total_bytes: decimalString(byteValue(highSpeed?.total)),
      remaining_bytes: decimalString(byteValue(highSpeed?.remained)),
      details: Array.isArray(highSpeed?.details)
        ? highSpeed.details.map((detail) => ({
            platform: integerValue(detail?.platform),
            used_bytes: decimalString(byteValue(detail?.used)),
          }))
        : [],
    },
    direct_link_traffic: {
      total_bytes: decimalString(byteValue(assets.totalDirectLinkTraffic)),
      remaining_bytes: decimalString(byteValue(assets.freeDirectLinkTraffic)),
      expires_at: integerValue(assets.directLinkTrafficExpireTime),
    },
    share_guest_traffic: {
      total_bytes: decimalString(byteValue(assets.totalShareGuestTraffic)),
      remaining_bytes: decimalString(byteValue(assets.freeShareGuestTraffic)),
    },
    membership: {
      vip_status: vipStatus,
      vip_status_text: membershipStatus(vipStatus),
      svip_status: svipStatus,
      svip_status_text: membershipStatus(svipStatus),
      vip_expires_at: vipExpiresAt,
      server_time: serverTime,
      vip_remaining_seconds: vipExpiresAt === null || serverTime === null
        ? null
        : Math.max(vipExpiresAt - serverTime, 0),
    },
    capabilities: {
      batch_upload_files: decimalString(byteValue(rights.batchUploadFiles)),
      big_file_size_bytes: decimalString(byteValue(rights.bigFileSize)),
      playback_speed: rights.playbackSpeed ?? null,
      playback_speed_rate: playbackSpeedPercent === null
        ? null
        : playbackSpeedPercent / 100,
      direct_link: booleanValue(rights.directLinkFeature),
      share_code: booleanValue(rights.shareCodeFeature),
    },
  };
}

function membershipStatus(status) {
  if (status === 1) return 'none';
  if (status === 2) return 'active';
  if (status === 3) return 'expired';
  return 'unknown';
}

function objectValue(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

function byteValue(value) {
  if (value === undefined || value === null || value === '') return null;
  try {
    const bytes = BigInt(value);
    return bytes < 0n ? 0n : bytes;
  } catch {
    return null;
  }
}

function ownByteValue(object, key) {
  return Object.hasOwn(object, key) ? byteValue(object[key]) : null;
}

function decimalString(value) {
  return value === null ? null : value.toString();
}

function nonNegativeDifference(total, used) {
  if (total === null || used === null) return null;
  return total > used ? total - used : 0n;
}

function integerValue(value) {
  if (value === undefined || value === null || value === '') return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

function booleanValue(value) {
  return typeof value === 'boolean' ? value : null;
}

function pageResult(envelope, operation, emptyTotalIsZero = false) {
  const data = envelope.data ?? {};
  const sourceItems = data.list ?? [];
  const page = nonNegativeInteger(data.page) ?? operation.page;
  const pageSize = positiveInteger(data.pageSize) ?? operation.page_size;
  const total = nonNegativeInteger(data.total) ??
    (emptyTotalIsZero && sourceItems.length === 0 ? 0 : null);
  const hasMore = typeof data.hasMore === 'boolean'
    ? data.hasMore
    : sourceItems.length > 0 && total !== null
      ? page * pageSize + sourceItems.length < total
      : false;
  return {
    content_trust: 'untrusted',
    items: sourceItems.map((item) => ({
      id: item.fileId,
      name: item.fileName,
      type: item.resType === 2 ? 'directory' : 'file',
      size: remoteFileSize(item.fileSize),
      parent_id: item.parentId === undefined ? '' : item.parentId,
    })),
    page,
    page_size: pageSize,
    total,
    cursor: data.cursor,
    has_more: hasMore,
  };
}

function nonNegativeInteger(value) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
}

function positiveInteger(value) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

const REMOTE_TASK_STATUS_NAMES = new Map([
  [0, 'PENDING'],
  [1, 'RUNNING'],
  [2, 'COMPLETED'],
  [3, 'FAILED'],
]);

export class WriteExecutor {
  constructor({ driveApi, store, transfer, config, credentials = driveApi?.client?.credentials }) {
    this.files = driveApi;
    this.store = store;
    this.transfer = transfer;
    this.config = config;
    this.credentials = credentials;
  }

  async execute(batch, operation) {
    if (await this.store.isStopRequested(batch.batch_id, batch.account_generation)) {
      throw new SkillError(await this.stopCode(batch), '任务已停止');
    }
    const scoped = new WriteExecutor({
      driveApi: this.files.withRequestGuard(accountRequestGuard({
        store: this.store, credentials: this.credentials, batch,
      })),
      credentials: this.credentials, store: this.store, transfer: this.transfer, config: this.config,
    });
    return scoped.executeOperation(batch, operation);
  }

  async executeOperation(batch, operation) {
    switch (operation.type) {
      case 'upload': return this.upload(batch, operation);
      case 'download': return this.download(batch, operation);
      case 'rename': return this.rename(operation);
      case 'create_folder': return this.createRequestedFolder(batch, operation);
      case 'copy': return this.taskOperation(batch, operation, () => this.files.copy(operation.remote_id, operation.target_parent_id));
      case 'move': return this.taskOperation(batch, operation, () => this.files.move(operation.remote_id, operation.target_parent_id));
      case 'create_share': return (await this.files.createShare(operation)).data;
      case 'save_share': return this.taskOperation(batch, operation, () => this.files.saveShare(operation, parseShareReference));
      case 'cloud_collection_add': return this.cloudAdd(batch, operation);
      case 'cloud_collection_delete': return this.cloudCollectionDelete(batch, operation);
      default: throw new SkillError('INVALID_REQUEST', `不支持的写操作: ${operation.type}`);
    }
  }

  async cloudCollectionDelete(batch, operation) {
    const item = batch.items.find((value) => value.operation_id === operation.id);
    let receipt = item?.cloud_delete_receipt;
    if (!receipt) {
      if (item?.remote_submission_unconfirmed) {
        throw new SkillError('REMOTE_RESULT_UNKNOWN', '云添加任务删除结果未确认，请先核对任务列表，不重复提交删除');
      }
      await this.store.updateItem(batch.batch_id, operation.id, { remote_submission_unconfirmed: true });
      let response;
      try {
        response = await this.files.deleteCloudCollectionTasks(operation);
      } catch (error) {
        if (error.data?.request_not_accepted === true) {
          await this.store.updateItem(batch.batch_id, operation.id, { remote_submission_unconfirmed: false });
          throw error;
        }
        throw new SkillError('REMOTE_RESULT_UNKNOWN', '云添加任务删除结果未确认，请先核对任务列表，不重复提交删除', {
          cause: error,
          data: { requested_task_ids: operation.task_ids, delete_files: operation.delete_files },
        });
      }
      // The backend's proto omits an empty repeated field. This confirms no IDs,
      // rather than confirming every requested ID or making the response unknown.
      const data = response?.data;
      const ids = data && typeof data === 'object' && !Array.isArray(data) ? data.taskIds ?? [] : undefined;
      const fileTaskId = response?.data?.deleteFileTaskId;
      if (!Array.isArray(ids) || ids.some((id) => typeof id !== 'string' || !operation.task_ids.includes(id)) ||
          new Set(ids).size !== ids.length ||
          (fileTaskId !== undefined && typeof fileTaskId !== 'string') ||
          (fileTaskId && (!operation.delete_files || fileTaskId.length > 128))) {
        throw new SkillError('REMOTE_RESULT_UNKNOWN', '云添加删除接口未返回有效回执，请先核对任务列表，不重复提交删除');
      }
      receipt = { task_ids: ids, delete_files: operation.delete_files ?? false, file_task_id: fileTaskId || undefined };
      await this.store.updateItem(batch.batch_id, operation.id, {
        cloud_delete_receipt: receipt,
        remote_submission_unconfirmed: false,
        remote_task_id: receipt.file_task_id,
      });
    }
    // A stored receipt resumes only the optional file-deletion task, never the POST.
    if (receipt.file_task_id) {
      const current = await this.executionBatch(batch);
      await this.taskOperation(current, operation, () => {
        throw new SkillError('INVALID_LOCAL_STATE', '文件删除任务 ID 丢失，不能重新提交删除');
      });
    }
    return cloudDeleteResult(operation, receipt, this.files);
  }

  async taskOperation(batch, operation, start) {
    const item = batch.items.find((value) => value.operation_id === operation.id);
    let taskId = item?.remote_task_id;
    if (!taskId) {
      const started = await start();
      taskId = started.data?.taskId;
      if (!taskId) throw new SkillError('INVALID_API_RESPONSE', '标准 API 未返回任务 ID');
      await this.store.updateItem(batch.batch_id, operation.id, {
        remote_task_id: taskId,
        ...remoteTaskStatusFields(started.data?.status),
      });
    }
    const statusTimeoutMs = this.config.remoteTaskStatusTimeoutMs ?? 10 * 60 * 1000;
    let statusUnavailableSince;
    for (;;) {
      if (await this.store.isStopRequested(batch.batch_id, batch.account_generation)) {
        throw new SkillError(await this.stopCode(batch), '任务已停止');
      }
      const envelope = await this.files.taskStatus(taskId);
      const rawStatus = envelope?.data?.status;
      const status = typeof rawStatus === 'number'
        ? rawStatus
        : typeof rawStatus === 'string' && rawStatus.trim() !== ''
          ? Number(rawStatus)
          : Number.NaN;
      const statusName = REMOTE_TASK_STATUS_NAMES.get(status);
      if (!statusName) {
        statusUnavailableSince ??= Date.now();
        await this.store.updateItem(batch.batch_id, operation.id, {
          remote_task_id: taskId,
          remote_status: 'UNKNOWN',
          remote_status_reported: false,
        });
        if (Date.now() - statusUnavailableSince >= statusTimeoutMs) {
          throw new SkillError(
            'REMOTE_TASK_STATUS_UNAVAILABLE',
            '远端任务状态暂时不可用，任务可能仍在执行',
            {
              retryable: true,
              data: { task_id: taskId, status_reported: false },
            },
          );
        }
        await delay(1000);
        continue;
      }
      statusUnavailableSince = undefined;
      await this.store.updateItem(batch.batch_id, operation.id, {
        remote_task_id: taskId,
        remote_status: statusName,
        remote_status_reported: true,
      });
      if (status === 2) return operationResult(operation, taskId, envelope.data);
      if (status === 3) {
        throw new SkillError('REMOTE_OPERATION_FAILED', envelope.data?.detail?.msg || '远端任务执行失败', {
          data: {
            remote_code: envelope.data?.detail?.code,
            content_trust: envelope.data?.detail?.msg ? 'untrusted' : undefined,
          },
        });
      }
      await delay(1000);
    }
  }

  async executeCloudCollectionGroup(batch, operations) {
    const files = this.files.withRequestGuard(accountRequestGuard({
      store: this.store, credentials: this.credentials, batch,
    }));
    try {
      await executeCloudCollectionGroup({ batch, operations, files, store: this.store, config: this.config });
    } catch (error) {
      const failure = toSkillError(error);
      throw new SkillError(failure.code, failure.message, {
        cause: failure, retryable: failure.retryable,
        data: { ...failure.data, cloud_group: true },
      });
    }
  }

  async cloudAdd(batch, operation) {
    await executeCloudCollectionGroup({
      batch, operations: [operation], files: this.files, store: this.store, config: this.config,
    });
    const item = (await this.store.getBatch(batch.batch_id)).items.find((value) => value.operation_id === operation.id);
    if (item.state === 'FAILED' || item.state === 'STOPPED') {
      throw new SkillError(item.code, item.message, { data: item.error, retryable: item.error?.retryable });
    }
    return item.result;
  }

  async upload(batch, operation) {
    const plan = planFor(batch, operation.id);
    if (plan.type === 'upload') {
      const item = plan.items[0];
      const work = uploadWork(operation, item, operation.remote_parent_id);
      const result = await this.transfer.execute(batch, work, this.files);
      await this.verifyUpload(result, operation.remote_parent_id, path.posix.basename(item.relative_path), item.size);
      await this.store.clearPendingUploadReceipt(batch.batch_id, operation.id, work.item.subitem_id);
      return result;
    }
    if (!Array.isArray(plan.verified_manifest_items)) {
      throw new SkillError('MANIFEST_MISMATCH', '目录上传缺少已验证清单快照');
    }
    const rootName = path.basename(plan.root);
    const rootId = await this.ensureUploadFolder(batch.batch_id, operation.id, '.', operation.remote_parent_id, rootName);
    const directories = new Map([['', rootId]]);
    let uploadedFiles = 0;
    let uploadedBytes = 0;
    for await (const item of manifestItems(plan)) {
      if (item.type === 'directory') {
        const parent = posixParent(item.relative_path);
        const parentId = directories.get(parent);
        if (!parentId) throw new SkillError('MANIFEST_MISMATCH', '目录清单父目录缺失');
        directories.set(item.relative_path, await this.ensureUploadFolder(
          batch.batch_id,
          operation.id,
          item.relative_path,
          parentId,
          path.posix.basename(item.relative_path),
        ));
      } else {
        const parentId = directories.get(posixParent(item.relative_path));
        if (!parentId) throw new SkillError('MANIFEST_MISMATCH', '文件清单父目录缺失');
        const work = uploadWork(operation, item, parentId);
        const result = await this.transfer.execute(await this.executionBatch(batch), work, this.files);
        await this.verifyUpload(result, parentId, path.posix.basename(item.relative_path), item.size);
        await this.store.clearPendingUploadReceipt(batch.batch_id, operation.id, work.item.subitem_id);
        uploadedFiles += 1;
        uploadedBytes += Number(item.size || 0);
      }
    }
    return { root_id: rootId, uploaded_files: uploadedFiles, uploaded_bytes: uploadedBytes };
  }

  async verifyUpload(result, parentId, _requestedName, size) {
    const fileId = result?.file?.fileId;
    if (!fileId) throw new SkillError('INVALID_UPLOAD_RESULT', '上传 SDK 未返回文件 ID');
    const envelope = await this.files.getInfo(fileId);
    const file = normalizeFile(envelope.data);
    const uploadParentId = result.file.parentId === undefined ? '' : result.file.parentId;
    const parentMismatch = typeof uploadParentId !== 'string' ||
      uploadParentId !== parentId ||
      file?.parentId !== parentId;
    const sdkName = result.file.fileName;
    const nameMismatch = typeof file?.name !== 'string' || !file.name ||
      (sdkName !== undefined && (
        typeof sdkName !== 'string' || !sdkName || sdkName !== file.name
      ));
    if (
      !file ||
      file.id !== fileId ||
      file.type !== 'file' ||
      parentMismatch ||
      nameMismatch ||
      Number(file.size) !== Number(size)
    ) {
      throw new SkillError('UPLOAD_VALIDATION_FAILED', '上传结果与目标不一致');
    }
  }

  async createFolder(parentId, name) {
    const envelope = await this.files.createFolder(parentId, name);
    const id = envelope.data?.fileId;
    if (!id) throw new SkillError('INVALID_API_RESPONSE', '创建目录未返回文件 ID');
    return id;
  }

  async ensureUploadFolder(batchId, operationId, key, parentId, name) {
    let batch = await this.store.getBatch(batchId);
    let item = batch.items.find((value) => value.operation_id === operationId);
    const known = item?.remote_directories?.[key];
    if (known) return known;
    const pending = item?.pending_directory;
    if (pending?.key === key && pending.parent_id === parentId && pending.name === name) {
      const recovered = await this.findChild(parentId, name);
      if (recovered) {
        if (recovered.type !== 'directory') throw new SkillError('REMOTE_NAME_CONFLICT', '远端已存在同名文件，请改名或先清理');
        await this.rememberDirectory(batchId, operationId, key, recovered.id);
        return recovered.id;
      }
    } else {
      const existing = await this.findChild(parentId, name);
      if (existing) throw new SkillError('REMOTE_NAME_CONFLICT', existing.type === 'directory'
        ? '远端已存在同名目录，请改名或先清理'
        : '远端已存在同名文件，请改名或先清理');
      await this.store.updateItem(batchId, operationId, {
        pending_directory: { key, parent_id: parentId, name },
      });
    }
    const id = await this.createFolder(parentId, name);
    await this.rememberDirectory(batchId, operationId, key, id);
    return id;
  }

  async rememberDirectory(batchId, operationId, key, id) {
    const current = await this.store.getBatch(batchId);
    const item = current.items.find((value) => value.operation_id === operationId);
    await this.store.updateItem(batchId, operationId, {
      remote_directories: { ...(item?.remote_directories || {}), [key]: id },
      pending_directory: undefined,
    });
  }

  async createRequestedFolder(batch, operation) {
    return {
      fileId: await this.ensureUploadFolder(
        batch.batch_id,
        operation.id,
        '.',
        operation.remote_parent_id,
        operation.name,
      ),
    };
  }

  async rename(operation) {
    const current = normalizeFile((await this.files.getInfo(operation.remote_id)).data);
    if (!current) throw new SkillError('REMOTE_FILE_NOT_FOUND', '远端对象不存在');
    if (current.name !== operation.new_name) {
      await this.files.rename(operation.remote_id, operation.new_name);
    }
    return { file_id: current.id, name: operation.new_name };
  }

  async findChild(parentId, name) {
    return this.findChildMatching(parentId, (item) => item?.name === name);
  }

  async findChildMatching(parentId, predicate) {
    let seen = 0;
    const pageSignatures = new Set();
    const maxPages = maxListPages(this.config);
    for (let page = 0; ; page += 1) {
      if (page >= maxPages) throw invalidPagination();
      const envelope = await this.files.list({ remote_parent_id: parentId, page, page_size: 100 });
      const list = envelope.data?.list || [];
      const found = list.map(normalizeFile).find(predicate);
      if (found) return found;
      rememberPage(pageSignatures, list);
      seen += list.length;
      enforce('max_directory_entries', maxDirectoryEntries(this.config), seen);
      if (!hasNextPage(envelope.data, list.length, seen, 100)) return undefined;
    }
  }

  async download(batch, operation) {
    planFor(batch, operation.id);
    const root = normalizeFile((await this.files.getInfo(operation.remote_id)).data);
    if (!root) throw new SkillError('REMOTE_FILE_NOT_FOUND', '远端对象不存在');
    const workItems = [];
    const count = { entries: 0, files: 0, bytes: 0, maxDepth: 0, manifestBytes: 0 };
    await this.collectDownload(root, root.name, workItems, count, 0);
    await this.store.updateItem(batch.batch_id, operation.id, {
      planned_bytes: count.bytes,
      planned_items: count.files,
    });
    await this.store.updateBatch(batch.batch_id, (current) => ({
      ...current,
      progress: {
        ...current.progress,
        phase: 'DOWNLOADING',
        total_bytes: current.items.reduce((sum, item) => sum + Number(item.planned_bytes || 0), 0),
        total_items: current.items.reduce((sum, item) => sum + Number(item.planned_items || 0), 0),
        updated_at: new Date().toISOString(),
      },
    }));
    let downloadedFiles = 0;
    let downloadedBytes = 0;
    for (const item of workItems) {
      const work = { operation_id: operation.id, item: { ...item, subitem_id: transferItemId(item.relative_path) } };
      const result = await this.transfer.execute(await this.executionBatch(batch), work, this.files);
      if (item.type === 'file') {
        downloadedFiles += 1;
        downloadedBytes += Number(result.bytes || item.size || 0);
      }
    }
    return { root_id: root.id, downloaded_files: downloadedFiles, downloaded_bytes: downloadedBytes };
  }

  async collectDownload(file, relative, output, count, depth) {
    assertRemoteName(file.name, this.config.platform);
    count.entries += 1;
    count.manifestBytes = (count.manifestBytes || 0) + Buffer.byteLength(JSON.stringify({
      id: file.id, name: file.name, parentId: file.parentId,
      size: file.size, md5: file.md5, type: file.type, relative,
    })) + 1;
    count.maxDepth = Math.max(count.maxDepth, depth);
    enforce('max_directory_entries', this.config.maxDirectoryEntries, count.entries);
    enforce('max_directory_depth', this.config.maxDirectoryDepth, count.maxDepth);
    enforce('max_manifest_bytes', this.config.maxManifestBytes, count.manifestBytes);
    if (file.type === 'file') {
      count.files += 1;
      count.bytes += Number(file.size || 0);
      output.push({ type: 'file', relative_path: relative, remote_id: file.id, size: Number(file.size || 0), md5: file.md5 });
      return;
    }
    output.push({ type: 'directory', relative_path: relative });
    let seen = 0;
    const pageSignatures = new Set();
    const maxPages = maxListPages(this.config);
    for (let page = 0; ; page += 1) {
      if (page >= maxPages) throw invalidPagination();
      const envelope = await this.files.list({ remote_parent_id: file.id, page, page_size: 100 });
      const list = envelope.data?.list || [];
      rememberPage(pageSignatures, list);
      for (const child of list.map(normalizeFile)) {
        if (child) await this.collectDownload(child, `${relative}/${child.name}`, output, count, depth + 1);
      }
      seen += list.length;
      if (!hasNextPage(envelope.data, list.length, seen, 100)) break;
    }
  }

  async stopCode(batch) {
    return await this.store.stopCode(batch.batch_id, batch.account_generation) || 'USER_STOPPED';
  }

  async executionBatch(approved) {
    return {
      ...await this.store.getBatch(approved.batch_id),
      account_id: approved.account_id,
      account_generation: approved.account_generation,
      local_plan: approved.local_plan,
    };
  }
}

function operationResult(operation, taskId, data = {}) {
  const result = {
    ...data,
    task_id: taskId,
    status_name: REMOTE_TASK_STATUS_NAMES.get(Number(data.status)) || undefined,
  };
  if (operation.type === 'move') {
    return {
      ...result,
      file_id: operation.remote_id,
      parent_id: operation.target_parent_id,
    };
  }
  if (operation.type === 'copy') {
    return {
      ...result,
      source_file_id: operation.remote_id,
      parent_id: operation.target_parent_id,
      created_file_id: null,
      created_file_id_reported: false,
    };
  }
  return result;
}

function remoteTaskStatusFields(value) {
  const statusName = REMOTE_TASK_STATUS_NAMES.get(Number(value));
  return statusName ? { remote_status: statusName } : {};
}

function planFor(batch, operationId) {
  const plan = batch.local_plan.find((value) => value.operation_id === operationId);
  if (!plan) throw new SkillError('LOCAL_PLAN_MISSING', '本地传输计划不存在');
  return plan;
}

function uploadWork(operation, item, parentId) {
  return { operation_id: operation.id, item: { ...item, target_parent_id: parentId, subitem_id: transferItemId(item.relative_path) } };
}

async function* manifestItems(plan) {
  if (!Array.isArray(plan.verified_manifest_items)) {
    throw new SkillError('MANIFEST_MISMATCH', '目录上传缺少已验证清单快照');
  }
  for (const item of plan.verified_manifest_items) yield item;
}

function normalizeFile(value) {
  const file = value?.fileInfo || value;
  if (!file?.fileId) return undefined;
  return {
    id: file.fileId,
    name: file.fileName,
    parentId: file.parentId === undefined ? '' : file.parentId,
    size: remoteFileSize(file.fileSize),
    md5: file.md5,
    type: Number(file.resType) === 2 ? 'directory' : 'file',
  };
}

function posixParent(relative) {
  const parent = path.posix.dirname(relative);
  return parent === '.' ? '' : parent;
}

function enforce(name, limit, observed) {
  if (observed > limit) throw new SkillError('DIRECTORY_LIMIT_EXCEEDED', '目录任务超过配置限制', { data: { directory_limit: { name, limit, observed } } });
}

function hasNextPage(data, itemCount, seen, requestedSize) {
  if (itemCount === 0) return false;
  if (data?.hasMore === true) return true;
  if (data?.hasMore === false) return false;
  const total = Number(data?.total);
  if (Number.isSafeInteger(total) && total >= 0) return seen < total;
  const responseSize = Number(data?.pageSize);
  const pageSize = Number.isSafeInteger(responseSize) && responseSize > 0
    ? responseSize
    : requestedSize;
  return itemCount >= pageSize;
}

function maxDirectoryEntries(config) {
  const value = Number(config?.maxDirectoryEntries);
  return Number.isSafeInteger(value) && value > 0 ? value : 100_000;
}

function maxListPages(config) {
  return Math.min(10_000, maxDirectoryEntries(config));
}

function rememberPage(signatures, list) {
  if (!list.length) return;
  const signature = createHash('sha256')
    .update(JSON.stringify(list.map((item) => item?.fileId)))
    .digest('hex');
  if (signatures.has(signature)) throw invalidPagination();
  signatures.add(signature);
}

function invalidPagination() {
  return new SkillError('INVALID_API_RESPONSE', '服务端分页结果没有有效推进');
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** 当前 FileInfo 协议省略 proto3 零值；null、负数等非法值不能当成空文件。 */
export function remoteFileSize(value) {
  if (value === undefined) return 0;
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new SkillError('INVALID_API_RESPONSE', '服务端文件大小非法');
  }
  return value;
}
