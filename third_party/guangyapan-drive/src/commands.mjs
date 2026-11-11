import { createHash, randomUUID } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createDriveApiClient, AccountAuthApi, DriveApi, createShareTitle } from './api-client.mjs';
import {
  stoppedTransferState,
  workerRecoveryCount,
  cloudTaskRecoveryCount,
  remoteExecutionMayContinue,
  transfersSucceeded,
  StateStore,
  assertTerminalBatchConsistency,
  buildTerminalBatch,
  settleSuccessfulBatch,
  hasPendingUploadReceipts,
  MAX_WORKER_RECOVERIES,
  CLOUD_TRACKING_PAUSE_CODES,
  workerRecoveryLimitError,
  TERMINAL_BATCH_STATES,
} from './batch-store.mjs';
import {
  refreshCloudTaskStatuses,
  cloudResourceFileSelection,
  cloudResourceSummary,
  normalizeCloudResource,
  readTorrentFile,
  validateCloudSelection,
} from './cloud-collection.mjs';
import { assertCredentialSession, CredentialRefreshCoordinator, accountRequestGuard } from './credential-store.mjs';
import { ReadExecutor, WriteExecutor } from './file-operations.mjs';
import {
  readOptionalJson, removeEmptyDirectory, preflightRequest, planDigest,
  captureDownloadRoot, guardDownloadPath, assertRemoteName, DOWNLOAD_TARGET_EXISTS_MESSAGE,
} from './local-files.mjs';
import { createLoginDependencies, cancelledLoginResponse, loginWorkerStatePath, loginStartedResponse } from './login.mjs';
import { backgroundStartFailure, launchBackgroundProcess, acquireProcessLock } from './platform.mjs';
import { parseRunRequest, parseShareReference, response, SkillError } from './protocol.mjs';

const MAX_CLOUD_TASK_RECOVERIES = 1;
const MIN_WORKER_RECOVERY_PENDING_MS = 30_000;

export function createDependencies(overrides = {}) {
  const config = overrides.config;
  if (!config) throw new SkillError('AGENT_INSTANCE_REQUIRED', '必须先绑定当前 Agent 的调用配置');
  const login = createLoginDependencies(config, overrides);
  const { credentials } = login;
  const refreshCoordinator = overrides.refreshCoordinator ?? new CredentialRefreshCoordinator(config.accountStateDir, credentials);
  const apiClient = overrides.apiClient ?? createDriveApiClient(config, credentials, refreshCoordinator);
  const driveApi = overrides.driveApi ?? new DriveApi(apiClient);
  return {
    ...login,
    config,
    refreshCoordinator,
    apiClient,
    driveApi,
    accountAuthApi: overrides.accountAuthApi ?? new AccountAuthApi({
      authBaseUrl: config.authBaseUrl,
      clientId: config.clientId,
      projectId: config.loginProjectId,
      agentName: config.agentName,
      credentials,
      timeoutMs: config.apiRequestTimeoutMs,
      maxResponseBytes: config.maxApiResponseBytes,
    }),
    store: overrides.store ?? new StateStore(config.agentStateDir),
    readExecutor: overrides.readExecutor ?? new ReadExecutor(driveApi, fetch, config.apiRequestTimeoutMs, config),
    spawnWorker: overrides.spawnWorker ?? ((...args) => startBackgroundWorker(...args, config)),
  };
}

export async function runCommand(raw, dependencies) {
  const request = parseRunRequest(raw);
  const session = await dependencies.credentials.getSession({ fresh: true });
  const credentials = session.credentials;
  if (!credentials) {
    const loginState = await readOptionalJson(loginWorkerStatePath(dependencies.config));
    if (loginState?.state === 'CANCELLED') return cancelledLoginResponse('run', request.request_id);
    const state = await dependencies.startLogin();
    const result = await loginStartedResponse('run', state, dependencies, request.request_id);
    return result.data.logged_in === true ? runCommand(raw, dependencies) : result;
  }
  if (request.mode === 'read') {
    const items = await dependencies.readExecutor.execute(request, {
      requestGuard: accountRequestGuard({
        store: dependencies.store, credentials: dependencies.credentials, expectedSession: session,
        readOnly: true,
      }),
    });
    const succeeded = items.filter((item) => item.state === 'SUCCEEDED');
    const failed = items.filter((item) => item.state === 'FAILED');
    return response('run', {
      code: failed.length === 0
        ? 'OK'
        : succeeded.length > 0
          ? 'READ_PARTIAL'
          : failed.length === 1 ? failed[0].code : 'READ_FAILED',
      retryable: succeeded.length === 0 && failed.length > 0 && failed.every((item) => item.retryable),
      request_id: request.request_id,
      data: { items },
    });
  }

  await dependencies.store.init();
  const requestHash = hashRequest(request);
  let batch = await dependencies.store.getBatch(request.request_id);
  let pinnedCloudOperations = new Set();
  if (batch) {
    assertReusable(batch, requestHash, credentials.accountId);
    if (
      TERMINAL_BATCH_STATES.has(batch.batch_state) ||
      await isWorkerAlive(dependencies.store, batch, dependencies.config) ||
      isWorkerRecoveryPending(batch, dependencies.config)
    ) {
      return dependencies.store.runAccountExclusive(async () => {
        const freshSession = await dependencies.credentials.getSession({ fresh: true });
        assertCredentialSession(freshSession, session);
        const fresh = freshSession.credentials;
        if (!fresh) throw new SkillError('LOGIN_REQUIRED', '需要登录光鸭账号');
        const current = await dependencies.store.getBatch(request.request_id);
        assertReusable(current, requestHash, fresh.accountId);
        const currentWorkerAlive = await isWorkerAlive(dependencies.store, current, dependencies.config);
        const currentRecoveryPending = isWorkerRecoveryPending(current, dependencies.config);
        if (!TERMINAL_BATCH_STATES.has(current.batch_state) && !currentWorkerAlive && !currentRecoveryPending) {
          throw new SkillError('WORKER_EXITED', '后台任务已退出，请重试 run 以重新校验执行计划', {
            retryable: true,
          });
        }
        if (!TERMINAL_BATCH_STATES.has(current.batch_state) && currentRecoveryPending) {
          return buildBatchResponse('run', current);
        }
        return resumeBatch(current, dependencies);
      });
    }
    const recoveryLimit = recoveryLimitFor(batch, dependencies.config);
    if (recoveryLimit) {
      batch = await finalizeRecoveryLimit(dependencies.store, batch, dependencies.config);
      return buildBatchResponse('run', batch);
    }
  }

  const retainedPlanIds = reusablePlanOperationIds(batch);
  const preflightInput = {
    ...request,
    operations: request.operations.filter((operation) => !retainedPlanIds.has(operation.id)),
  };
  if (batch) {
    pinnedCloudOperations = pinCloudSelections(preflightInput, batch);
    pinShareRestoreSelections(preflightInput, batch);
    pinShareTitles(preflightInput, batch);
  }

  const preflightRoot = path.join(dependencies.config.agentStateDir, 'preflight', request.request_id);
  await fs.mkdir(preflightRoot, { recursive: true, mode: 0o700 });
  const preflightDir = await fs.mkdtemp(path.join(preflightRoot, 'attempt-'));
  const preflightOwner = await captureDownloadRoot(preflightDir);
  const preflightRootOwner = await captureDownloadRoot(preflightRoot);
  let keepPreflight = false;
  try {
    const files = dependencies.driveApi.withRequestGuard(accountRequestGuard({
      store: dependencies.store, credentials: dependencies.credentials, expectedSession: session,
      readOnly: true,
    }));
    const lookup = new WriteExecutor({ driveApi: files, config: dependencies.config });
    const local = await preflightRequest(preflightInput, dependencies.config, preflightDir, {
      beforeDirectoryScan: async (operation) => {
        // Earlier writes can change the destination. The executor checks again
        // in operation order, and also handles accepted directory receipts.
        if (batch || request.operations[0].id !== operation.id) return;
        const existing = await lookup.findChild(operation.remote_parent_id, path.basename(operation.local_path));
        if (existing) {
          // Another initial run may have registered this same request and its
          // worker created the directory while the lookup was in flight.
          const current = await dependencies.store.getBatch(request.request_id);
          if (current) {
            assertReusable(current, requestHash, credentials.accountId);
            return;
          }
          throw new SkillError('REMOTE_NAME_CONFLICT', existing.type === 'directory'
            ? '远端已存在同名目录，请改名或先清理'
            : '远端已存在同名文件，请改名或先清理');
        }
      },
    });
    const remote = await inspectRemote(
      preflightInput,
      files,
      dependencies.config,
      pinnedCloudOperations,
      credentials.accountId,
    );
    const summaries = [...local.summaries, ...remote.summaries];
    return await dependencies.store.runAccountExclusive(async () => {
      const freshSession = await dependencies.credentials.getSession({ fresh: true });
      assertCredentialSession(freshSession, session);
      const fresh = freshSession.credentials;
      if (!fresh) throw new SkillError('LOGIN_REQUIRED', '需要登录光鸭账号');
      let current = await dependencies.store.getBatch(request.request_id);
      let recoveryLock;
      let trustedDigest;
      try {
        if (current) {
          assertReusable(current, requestHash, fresh.accountId);
          const currentWorkerAlive = await isWorkerAlive(dependencies.store, current, dependencies.config);
          const currentRecoveryPending = isWorkerRecoveryPending(current, dependencies.config);
          if (TERMINAL_BATCH_STATES.has(current.batch_state) || currentWorkerAlive || currentRecoveryPending) {
            if (!TERMINAL_BATCH_STATES.has(current.batch_state) && currentRecoveryPending) {
              return buildBatchResponse('run', current);
            }
            return resumeBatch(current, dependencies);
          }
          // A missing heartbeat does not prove the old worker released ownership.
          // Serialize replanning and recovery-budget changes with its actual lock.
          recoveryLock = await acquireProcessLock(path.join(dependencies.store.batchDir(request.request_id), 'worker.lock'));
          if (!recoveryLock) {
            return buildBatchResponse('run', await dependencies.store.getBatch(request.request_id));
          }
          current = await dependencies.store.getBatch(request.request_id);
          assertReusable(current, requestHash, fresh.accountId);
          if (TERMINAL_BATCH_STATES.has(current.batch_state) || isWorkerRecoveryPending(current, dependencies.config)) {
            return buildBatchResponse('run', current);
          }
          const recoveryLimit = recoveryLimitFor(current, dependencies.config);
          if (recoveryLimit) {
            await recoveryLock.close();
            recoveryLock = undefined;
            const failed = await finalizeRecoveryLimit(dependencies.store, current, dependencies.config);
            return buildBatchResponse('run', failed);
          }
          const latestRetainedIds = reusablePlanOperationIds(current);
          const unfinished = {
            ...preflightInput,
            operations: preflightInput.operations.filter((operation) =>
              !latestRetainedIds.has(operation.id)),
          };
          assertPreflightSelections(unfinished, current);
          pinShareTitles(unfinished, current, remote.operations);
        }

        // Check only a new batch's first operation: prior operations can change
        // names, and recovery may legitimately own an existing download target.
        const first = request.operations[0];
        if (!current && first.type === 'download') {
          const plan = local.localPlan.find(value => value.operation_id === first.id);
          const source = remote.operations.find(value => value.operation_id === first.id).source;
          assertRemoteName(source.name, dependencies.config.platform);
          const target = await guardDownloadPath(plan, path.join(plan.local_dir, source.name));
          const existing = await target.run(() => fs.lstat(target.output).catch(error => {
            if (error.code === 'ENOENT') return undefined;
            throw error;
          }));
          if (existing) throw new SkillError('LOCAL_TARGET_CONFLICT', DOWNLOAD_TARGET_EXISTS_MESSAGE);
        }
        const accountGeneration = await dependencies.store.currentAccountGeneration();
        const now = new Date();
        const latestRetainedIds = reusablePlanOperationIds(current);
        // Remote acceptance or completion may commit during preflight. Keep the
        // original plan and runtime facts once replay is no longer permitted.
        const operations = request.operations.map((operation) => latestRetainedIds.has(operation.id)
          ? current.operations.find((previous) => previous.id === operation.id)
          : operation);
        const operationDetails = operations.map((operation) =>
          (latestRetainedIds.has(operation.id) ? current.operation_details : remote.operations)
            .find((detail) => detail.operation_id === operation.id));
        const localPlan = operations.flatMap((operation) =>
          (latestRetainedIds.has(operation.id) ? current.local_plan : local.localPlan)
            .filter((plan) => plan.operation_id === operation.id));
        const plannedItems = request.operations.map((operation) => {
          const summary = summaries.find((value) => value.operation_id === operation.id);
          const previous = current?.items?.find((value) => value.operation_id === operation.id);
          if (latestRetainedIds.has(operation.id)) return previous;
          return {
            ...previous,
            operation_id: operation.id,
            state: previous?.state ?? 'PENDING',
            planned_bytes: Number(summary?.total_bytes || 0),
            planned_items: Number(summary?.transfer_items || 0),
          };
        });
        const totals = plannedItems.reduce((sum, item) => ({
          bytes: sum.bytes + Number(item.planned_bytes || 0),
          items: sum.items + Number(item.planned_items || 0),
        }), { bytes: 0, items: 0 });
        const record = {
          ...current,
          request_id: request.request_id,
          request_hash: requestHash,
          batch_id: request.request_id,
          account_id: fresh.accountId,
          account_generation: accountGeneration,
          batch_state: 'RUNNING',
          code: 'OK',
          retryable: false,
          local_error: undefined,
          worker_pid: undefined,
          worker_instance_id: undefined,
          worker_recovery_pending_at: undefined,
          worker_recovery_count: current
            ? workerRecoveryCount(current) + (isCloudTrackingRecovery(current) ? 0 : 1)
            : 0,
          cloud_task_recovery_count: current
            ? cloudTaskRecoveryCount(current) + (isCloudTrackingRecovery(current) ? 1 : 0)
            : 0,
          plan_summary: {
            operation_count: request.operations.length,
            total_bytes: totals.bytes || undefined,
            total_items: totals.items || undefined,
          },
          operation_details: operationDetails,
          operations,
          items: plannedItems,
          transfer_state: !localPlan.length ? 'NONE' : transfersSucceeded(localPlan, plannedItems) ? 'SUCCEEDED' : 'IDLE',
          local_plan: localPlan,
          progress: {
            phase: 'RUNNING',
            current_bytes: Number(current?.progress?.current_bytes || 0),
            total_bytes: totals.bytes || undefined,
            completed_items: Number(current?.progress?.completed_items || 0),
            total_items: totals.items || undefined,
            updated_at: now.toISOString(),
          },
          created_at: current?.created_at || now.toISOString(),
          updated_at: now.toISOString(),
        };
        trustedDigest = planDigest(record);
        record.plan_digest = trustedDigest;
        if (current) {
          batch = await dependencies.store.updateBatch(request.request_id, record);
        } else {
          const created = await dependencies.store.createBatch(record);
          if (!created) throw new SkillError('LOCAL_STATE_BUSY', '本地批次登记冲突', { retryable: true });
          batch = record;
        }
        keepPreflight = localPlan.some((plan) =>
          plan.type === 'upload_directory' && path.dirname(plan.manifest_path) === preflightDir);
      } finally {
        await recoveryLock?.close();
      }
      return resumeBatch(batch, dependencies, trustedDigest);
    });
  } finally {
    if (!keepPreflight) {
      try {
        await (dependencies.cleanupPreflight ?? cleanupPreflight)(preflightDir, preflightRoot, {
          attempt: preflightOwner, root: preflightRootOwner,
          manifests: preflightInput.operations.filter((operation) => operation.type === 'upload')
            .map((operation) => `${operation.id}.manifest.ndjson`),
        });
      } catch {
        // 宿主可能拦截递归删除；临时目录残留不应覆盖主操作结果。
      }
    }
  }
}

async function resumeBatch(batch, dependencies, trustedDigest) {
  if (TERMINAL_BATCH_STATES.has(batch.batch_state)) return buildBatchResponse('run', batch);
  await dependencies.spawnWorker(
    batch.batch_id,
    dependencies.store,
    dependencies.config.workerStartupTimeoutMs,
    trustedDigest,
    dependencies.config.workerHeartbeatStaleMs,
  );
  const current = await dependencies.store.getBatch(batch.batch_id);
  return buildBatchResponse('run', current);
}

export async function reloginCommand(dependencies) {
  const logout = await logoutCommand(dependencies);
  if (logout.data.local_credential_deleted !== true) {
    return response('relogin', {
      code: 'RELOGIN_FAILED',
      retryable: logout.retryable,
      data: {
        logout_result: logout.code,
        message: '本机账号凭据未能删除，未启动重新登录',
      },
    });
  }
  const state = await dependencies.startLogin();
  const result = await loginStartedResponse('relogin', state, dependencies);
  return { ...result, data: { ...result.data, logout_result: logout.code } };
}

export async function statusCommand(batchId, dependencies) {
  assertId(batchId, 'batch_id');
  let batch;
  try {
    batch = await dependencies.store.getBatch(batchId);
  } catch (cause) {
    if (['EACCES', 'EPERM'].includes(cause?.code)) {
      throw new SkillError('LOCAL_STATE_PERMISSION_DENIED', '无法读取本地批次状态', { cause });
    }
    throw cause;
  }
  if (!batch) throw new SkillError('BATCH_NOT_FOUND', '本地批次不存在');
  if (!TERMINAL_BATCH_STATES.has(batch.batch_state) && batch.items?.length && batch.items.every((item) => item.state === 'SUCCEEDED')) {
    batch = await dependencies.store.updateBatch(batchId, settleSuccessfulBatch);
  }
  const workerAlive = await isWorkerAlive(dependencies.store, batch, dependencies.config);
  if (batch.batch_state === 'STOPPED') {
    if (workerAlive) return buildBatchResponse('status', { ...batch, code: 'WORKER_STOP_PENDING', retryable: true });
    await refreshCloudTaskStatuses(batch, dependencies);
    batch = await dependencies.store.getBatch(batchId);
  }
  const recoveryPending = isWorkerRecoveryPending(batch, dependencies.config);
  if (!workerAlive && !recoveryPending) {
    try {
      await dependencies.store.cleanupWorkerArtifacts(
        batch,
        dependencies.config?.workerHeartbeatStaleMs ?? 5_000,
      );
    } catch {
      // Artifact cleanup is best effort and must not hide the batch status.
    }
  }
  if (!TERMINAL_BATCH_STATES.has(batch.batch_state) && !workerAlive && !recoveryPending) {
    const expectedTrackingExit = isCloudTrackingRecovery(batch);
    const expectedReportedExit = Boolean(batch.worker_exited_at) &&
      batch.local_error?.code === batch.code;
    const recoveryLimit = recoveryLimitFor(batch, dependencies.config);
    if (recoveryLimit) {
      batch = await finalizeRecoveryLimit(dependencies.store, batch, dependencies.config);
    } else if (!expectedTrackingExit && !expectedReportedExit) {
      batch = {
        ...batch,
        code: 'WORKER_EXITED',
        retryable: true,
        transfer_state: batch.transfer_state === 'RUNNING' ? 'PAUSED' : batch.transfer_state,
        local_error: {
          code: 'WORKER_EXITED',
          message: '后台任务进程已退出，请使用原 request_id 重新执行 run 以恢复任务',
          retryable: true,
        },
      };
    }
  }
  return buildBatchResponse('status', batch);
}

export async function stopCommand(batchId, dependencies) {
  assertId(batchId, 'batch_id');
  await dependencies.store.init();
  let batch = await dependencies.store.getBatch(batchId);
  if (!batch) throw new SkillError('BATCH_NOT_FOUND', '本地批次不存在');
  if (!TERMINAL_BATCH_STATES.has(batch.batch_state)) {
    await dependencies.store.requestStop(batchId);
  }
  if (batch.batch_state !== 'SUCCEEDED') {
    await waitForWorkerExit(batch, dependencies.store, 5000);
    if (await isWorkerAlive(dependencies.store, batch, dependencies.config)) {
      batch = await dependencies.store.getBatch(batchId);
      return buildBatchResponse('stop', { ...batch, code: 'WORKER_STOP_PENDING', retryable: true });
    }
  }
  batch = await dependencies.store.getBatch(batchId);
  if (!TERMINAL_BATCH_STATES.has(batch.batch_state)) {
    const lock = await acquireProcessLock(path.join(dependencies.store.batchDir(batchId), 'worker.lock'));
    if (!lock) {
      batch = await dependencies.store.getBatch(batchId);
      return buildBatchResponse('stop', { ...batch, code: 'WORKER_STOP_PENDING', retryable: true });
    }
    try {
      batch = await dependencies.store.updateBatch(batchId, (current) => TERMINAL_BATCH_STATES.has(current.batch_state)
        ? current
        : buildTerminalBatch(current, 'STOPPED', 'USER_STOPPED', {
          transfer_state: stoppedTransferState(current), retryable: false, local_error: undefined,
        }));
      if (batch.batch_state === 'STOPPED') await dependencies.store.clearRecovery(batchId, batch.local_plan);
    } finally {
      await lock.close();
    }
  }
  if (batch.batch_state !== 'SUCCEEDED') {
    await refreshCloudTaskStatuses(batch, dependencies);
    batch = await dependencies.store.getBatch(batchId);
  }
  return buildBatchResponse('stop', batch);
}

export async function logoutCommand(dependencies) {
  return dependencies.store.runAccountExclusive(() => logoutExclusive(dependencies));
}

async function logoutExclusive(dependencies) {
  let serverResult;
  let serverError;
  let credentialError;
  let accountOperationError;
  let loginStopError;
  let activeStopError;
  let activeStopResult;
  let serverLogoutAttempted = false;
  let credentialDeleted = false;
  try {
    activeStopResult = await revokeActiveWork(dependencies.store, true);
  } catch (error) {
    activeStopError = error;
  }
  try { await dependencies.stopLogin(); } catch (error) { loginStopError = error; }
  const revokeAndDelete = async () => {
    serverLogoutAttempted = true;
    try { serverResult = await dependencies.accountAuthApi.logout(); } catch (error) { serverError = error; }
    try {
      await dependencies.credentials.delete();
      credentialDeleted = true;
    } catch (error) {
      credentialError = error;
    }
  };
  try {
    await dependencies.refreshCoordinator.runLogout(revokeAndDelete);
  } catch (error) {
    accountOperationError = error;
  }
  const serverRejected = serverResult?.code !== undefined && serverResult.code !== 0;
  const partial = Boolean(
    loginStopError || activeStopError || activeStopResult?.pending > 0 || serverError ||
    serverRejected || credentialError || accountOperationError || !credentialDeleted
  );
  return response('logout', {
    code: partial ? 'LOGOUT_PARTIAL' : 'OK',
    retryable: [loginStopError, activeStopError, serverError, credentialError, accountOperationError]
      .some((error) => error?.retryable === true),
    data: {
      pending_login_stop_result: loginStopError?.code ?? (loginStopError ? 'LOGIN_STOP_FAILED' : 'OK'),
      active_batch_stop_result: activeStopError?.code ?? (activeStopResult?.pending ? 'WORKER_STOP_PENDING' : 'OK'),
      stopped_batch_count: activeStopResult?.stopped ?? 0,
      pending_batch_count: activeStopResult?.pending ?? 0,
      remote_execution_may_continue: activeStopResult?.remote_execution_may_continue || undefined,
      server_logout_result: !serverLogoutAttempted
        ? 'LOGOUT_NOT_RUN'
        : serverError
          ? serverError.code ?? 'LOGOUT_FAILED'
          : serverResult?.code === 0 ? 'OK' : serverResult?.code ?? 'OK',
      account_operation_error: accountOperationError?.code,
      local_credential_deleted: credentialDeleted,
      local_credential_error: credentialError ? credentialError.code ?? 'CREDENTIAL_DELETE_FAILED' : undefined,
    },
  });
}

async function inspectRemote(
  request,
  files,
  config,
  pinnedCloudOperations = new Set(),
  accountId,
) {
  const cache = new Map();
  const projectedNames = new Map();
  const info = async (id) => {
    if (!id) return { fileId: '', fileName: '根目录', resType: 2 };
    if (!cache.has(id)) cache.set(id, files.getInfo(id).then((result) => result.data?.fileInfo || result.data));
    const value = await cache.get(id);
    if (!value?.fileId) throw new SkillError('REMOTE_FILE_NOT_FOUND', '远端对象不存在');
    return projectedNames.has(id) ? { ...value, fileName: projectedNames.get(id) } : value;
  };
  const operations = [];
  const summaries = [];
  for (const operation of request.operations) {
    const detail = operationDetail(operation);
    if (operation.remote_id) detail.source = fileDetail(await info(operation.remote_id));
    if (operation.remote_ids && operation.type !== 'save_share') {
      const sourceFiles = await Promise.all(operation.remote_ids.map((id) => info(id)));
      if (operation.type === 'create_share') {
        const rejected = sourceFiles.find((file) =>
          Number(file.auditStatus) === 3 || Number(file.shareAuditStatus) === 3,
        );
        if (rejected) {
          throw new SkillError('SHARE_FILE_REJECTED', '违规文件不能创建分享', {
            data: {
              file_id: rejected.fileId,
              file_name: rejected.fileName,
              content_trust: 'untrusted',
            },
          });
        }
        operation.title ??= createShareTitle(sourceFiles);
        detail.title = operation.title;
      }
      detail.sources = sourceFiles.map(fileDetail);
    }
    const parentId = operation.target_parent_id ?? operation.remote_parent_id;
    if (parentId !== undefined) {
      const target = await info(parentId);
      if (Number(target.resType) !== 2) throw new SkillError('REMOTE_TARGET_NOT_DIRECTORY', '远端目标不是目录');
      detail.target = fileDetail(target);
    }
    if (operation.type === 'save_share') {
      const { accessToken } = await files.shareAccess(operation, parseShareReference);
      const summary = await files.getShareSummary(operation, parseShareReference);
      if (summary.data?.userId && summary.data.userId === accountId) {
        throw new SkillError('SHARE_SELF_RESTORE', '不能转存当前账号创建的分享');
      }
      if (operation.remote_ids) {
        const shared = await files.shareFileDetails(accessToken, operation.remote_ids);
        const byId = new Map(shared.map((file) => [file.fileId, file]));
        detail.sources = operation.remote_ids.map((id) => {
          const file = byId.get(id);
          if (!file) throw new SkillError('SHARE_FILE_NOT_FOUND', '分享中的指定文件不存在');
          return fileDetail(file);
        });
      } else {
        // Resolve the visible roots before starting a non-idempotent restore task.
        operation.remote_ids = await files.shareRootIds(accessToken);
        detail.sources = operation.remote_ids.map((id) => ({ id }));
      }
    }
    if (operation.type === 'cloud_collection_add') {
      const resource = normalizeCloudResource(
        await files.resolveCloudCollectionResource(
          operation,
          operation.torrent_path ? await readTorrentFile(operation.torrent_path) : undefined,
        ),
        operation.url,
        config,
        operation.torrent_path ? 2 : undefined,
      );
      if (operation.file_indexes === undefined && resource.file_indexes.length) {
        operation.file_indexes = operation.selection_mode === 'all'
          ? [...resource.file_indexes]
          : [...resource.recommended_file_indexes];
        if (!operation.file_indexes.length) {
          throw new SkillError(
            'CLOUD_COLLECTION_SELECTION_REQUIRED',
            '服务器推荐列表为空，请明确要求全部下载',
          );
        }
      }
      try {
        validateCloudSelection(resource, operation.file_indexes);
      } catch (error) {
        if (pinnedCloudOperations.has(operation.id) && error?.code === 'INVALID_REQUEST') {
          throw new SkillError(
            'CLOUD_RESOURCE_CHANGED',
            '云添加资源内容已变化，请使用新的 request_id 重新解析并执行',
            { retryable: false },
          );
        }
        throw error;
      }
      operation.resolved_url = resource.url;
      operation.res_type = resource.res_type;
      detail.cloud_resource = {
        name: resource.name,
        size: resource.size,
        is_directory: resource.is_directory,
        normalized_url: resource.url,
        resource_type: resource.res_type,
        selected_files: operation.file_indexes?.length ?? (resource.file_indexes.length || 1),
        file_selection: cloudResourceFileSelection(resource, operation.file_indexes),
      };
      summaries.push({
        operation_id: operation.id,
        ...cloudResourceSummary(resource, operation.file_indexes),
      });
    }
    operations.push(detail);
    if (operation.type === 'rename') projectedNames.set(operation.remote_id, operation.new_name);
  }
  return { operations, summaries };
}

async function cleanupPreflight(preflightDir, preflightRoot, owner) {
  // No directory enumeration or recursive deletion. Check the original attempt
  // before unlinking each exact generated path; replaced directories are retained.
  const attemptPlan = { local_dir: preflightDir, download_root: owner.attempt };
  for (const name of owner.manifests) {
    const manifest = path.join(preflightDir, name);
    await guardDownloadPath(attemptPlan, manifest);
    await fs.unlink(manifest).catch(() => {});
  }
  await guardDownloadPath(attemptPlan, path.join(preflightDir, '.cleanup'));
  await removeEmptyDirectory(preflightDir);
  await guardDownloadPath({ local_dir: preflightRoot, download_root: owner.root }, path.join(preflightRoot, '.cleanup'));
  await removeEmptyDirectory(preflightRoot);
}

function operationDetail(operation) {
  const detail = { operation_id: operation.id, type: operation.type };
  for (const key of [
    'local_path', 'local_dir', 'torrent_path', 'new_name', 'name', 'title',
    'validity_seconds', 'share_type', 'download_type',
    'share_url', 'url',
    'task_ids', 'delete_files',
  ]) {
    if (operation[key] !== undefined) detail[key] = operation[key];
  }
  if (operation.type === 'create_share') detail.extraction_code_set = Boolean(operation.code);
  return detail;
}

async function revokeActiveWork(store, accountLockHeld = false) {
  const revoked = await store.revokeActiveBatches({ accountLockHeld });
  await Promise.all(revoked.stopped.map((batch) => waitForWorkerExit(batch, store, 5000)));
  let stopped = 0;
  let pending = 0;
  for (const entry of revoked.stopped) {
    if (await isWorkerAlive(store, entry)) {
      pending += 1;
      continue;
    }
    const lock = await acquireProcessLock(path.join(store.batchDir(entry.batch_id), 'worker.lock'));
    if (!lock) {
      pending += 1;
      continue;
    }
    try {
      const batch = await store.getBatch(entry.batch_id);
      if (!batch || TERMINAL_BATCH_STATES.has(batch.batch_state)) {
        stopped += 1;
        continue;
      }
      const updated = await store.updateBatch(entry.batch_id, (current) => TERMINAL_BATCH_STATES.has(current.batch_state)
        ? current
        : buildTerminalBatch(current, 'STOPPED', 'ACCOUNT_LOGGED_OUT', {
          transfer_state: stoppedTransferState(current),
        }));
      if (updated.batch_state === 'STOPPED') await store.clearRecovery(entry.batch_id, updated.local_plan);
      stopped += 1;
    } finally {
      await lock.close();
    }
  }
  return {
    stopped,
    pending,
    remote_execution_may_continue: revoked.remote_execution_may_continue === true,
  };
}

function fileDetail(file) {
  return { id: file.fileId, name: file.fileName || '根目录', type: Number(file.resType) === 2 ? 'directory' : 'file', size: file.fileSize };
}

function assertReusable(batch, requestHash, accountId) {
  if (!batch || batch.request_hash !== requestHash) throw new SkillError('REQUEST_ID_CONFLICT', 'request_id 已被其他请求使用');
  if (batch.account_id !== accountId) throw new SkillError('LOGIN_ACCOUNT_CHANGED', '当前登录账号与批次账号不一致');
}

function hashRequest(request) {
  return createHash('sha256').update(JSON.stringify(request)).digest('hex');
}

function reusablePlanOperationIds(batch) {
  const operations = new Map((batch?.operations || []).map((operation) => [operation.id, operation]));
  return new Set((batch?.items || []).filter((item) => item.state === 'SUCCEEDED' || (
    operations.get(item.operation_id)?.type === 'cloud_collection_add' && (
      item.remote_task_id || item.remote_submission_unconfirmed === true || TERMINAL_BATCH_STATES.has(item.state)
    )
  )).map((item) => item.operation_id));
}

function pinCloudSelections(request, batch) {
  const previous = new Map((batch.operations || []).map((operation) => [operation.id, operation]));
  const pinned = new Set();
  for (const operation of request.operations) {
    const prior = previous.get(operation.id);
    if (operation.type !== 'cloud_collection_add' || !Array.isArray(prior?.file_indexes)) continue;
    operation.file_indexes = [...prior.file_indexes];
    pinned.add(operation.id);
  }
  return pinned;
}

function pinShareRestoreSelections(request, batch) {
  const previous = new Map(batch.operations.map((operation) => [operation.id, operation]));
  for (const operation of request.operations) {
    if (operation.type !== 'save_share' || operation.remote_ids !== undefined) continue;
    const ids = previous.get(operation.id)?.remote_ids;
    if (!Array.isArray(ids) || !ids.length || ids.some((id) => typeof id !== 'string' || !id)) {
      throw new SkillError('INVALID_LOCAL_STATE', '本地转存计划缺少有效文件选择');
    }
    operation.remote_ids = [...ids];
  }
}

function assertPreflightSelections(request, batch) {
  const previous = new Map(batch.operations.map((operation) => [operation.id, operation]));
  for (const operation of request.operations) {
    const field = operation.type === 'cloud_collection_add'
      ? 'file_indexes'
      : operation.type === 'save_share' ? 'remote_ids' : undefined;
    if (!field) continue;
    if (JSON.stringify(operation[field]) !== JSON.stringify(previous.get(operation.id)?.[field])) {
      // A concurrent run registered the first plan after this run began preflight.
      // Retry from the original request so persisted selections are pinned before remote I/O.
      throw new SkillError('PREFLIGHT_SELECTION_CHANGED', '文件选择已由另一次请求固定，请重试原请求', {
        retryable: true,
        data: { operation_id: operation.id },
      });
    }
  }
}

function pinShareTitles(request, batch, details = []) {
  const previous = new Map(batch.operations.map((operation) => [operation.id, operation]));
  for (const operation of request.operations) {
    if (operation.type !== 'create_share') continue;
    const title = previous.get(operation.id)?.title;
    if (typeof title !== 'string' || title.length === 0 || title.length > 255) {
      throw new SkillError('INVALID_LOCAL_STATE', '本地分享计划缺少有效标题');
    }
    operation.title = title;
    const detail = details.find((value) => value.operation_id === operation.id);
    if (detail) detail.title = title;
  }
}

function buildBatchResponse(command, batch) {
  assertTerminalBatchConsistency(batch);
  const remoteMayContinue = remoteExecutionMayContinue(batch);
  const stopped = batch.batch_state === 'STOPPED';
  return response(command, {
    request_id: batch.request_id,
    batch_id: batch.batch_id,
    batch_state: batch.batch_state,
    transfer_state: batch.transfer_state,
    code: batch.code ?? 'OK',
    retryable: batch.retryable,
    data: {
      items: batch.items ?? [],
      progress: batch.progress,
      error: batch.local_error,
      recovery: {
        worker_recovery_count: workerRecoveryCount(batch),
        worker_recovery_limit: MAX_WORKER_RECOVERIES,
        cloud_task_recovery_count: cloudTaskRecoveryCount(batch),
        cloud_task_recovery_limit: MAX_CLOUD_TASK_RECOVERIES,
      },
      remote_execution_may_continue: remoteMayContinue || undefined,
      remote_stop_supported: stopped ? false : undefined,
      message: stopMessage(batch, remoteMayContinue),
    },
  });
}

function stopMessage(batch, remoteMayContinue) {
  if (batch.code === 'WORKER_STOP_PENDING') {
    return '已请求停止本地任务，正在等待后台退出；当前接口不支持取消已提交的云端任务，云端可能仍在运行';
  }
  if (batch.batch_state !== 'STOPPED') return undefined;
  if (remoteMayContinue) {
    if (batch.local_error?.upload_committed === true || batch.local_error?.upload_result_unknown === true || hasPendingUploadReceipts(batch)) {
      return '本地上传与跟踪已停止；已提交的上传结果尚未确认，云端可能继续处理，请在客户端核对生成的文件';
    }
    return '本地执行与跟踪已停止；当前接口不支持取消已提交的云端任务，云端可能继续执行。可查询 status 核对远端最终状态';
  }
  if ((batch.items ?? []).some((item) => item.remote_task_id)) {
    return '本地执行与跟踪已停止，已知云端任务已结束；请查看各项 remote_status。这不表示任务已取消，已生成的文件保留';
  }
  return '本地任务已停止';
}

function isCloudTrackingRecovery(batch) {
  return CLOUD_TRACKING_PAUSE_CODES.has(batch?.code) && Boolean(batch?.worker_exited_at);
}

function recoveryLimitFor(batch, config = {}) {
  if (isWorkerRecoveryPending(batch, config)) return undefined;
  const causeCode = recordedCauseCode(batch);
  if (isCloudTrackingRecovery(batch)) {
    const count = cloudTaskRecoveryCount(batch);
    if (count < MAX_CLOUD_TASK_RECOVERIES) return undefined;
    return {
      code: 'CLOUD_TASK_RECOVERY_LIMIT_REACHED',
      retryable: false,
      local_error: {
        code: 'CLOUD_TASK_RECOVERY_LIMIT_REACHED',
        message: '云添加任务仍无法确认完成，已达到一次续跟踪上限；远端任务可能仍在执行',
        retryable: false,
        recovery_count: count,
        recovery_limit: MAX_CLOUD_TASK_RECOVERIES,
        ...(causeCode ? { cause_code: causeCode } : {}),
      },
    };
  }
  const count = workerRecoveryCount(batch);
  if (count < MAX_WORKER_RECOVERIES) return undefined;
  return {
    code: 'WORKER_RECOVERY_LIMIT_REACHED',
    retryable: false,
    local_error: workerRecoveryLimitError(count, causeCode),
  };
}

function recordedCauseCode(batch) {
  const value = batch.local_error?.cause_code ?? batch.local_error?.code ?? batch.code;
  if ((typeof value !== 'string' && typeof value !== 'number') || value === 'OK') return undefined;
  return String(value);
}

async function finalizeRecoveryLimit(store, batch, config = {}) {
  const lock = await acquireProcessLock(path.join(store.batchDir(batch.batch_id), 'worker.lock'));
  if (!lock) return await store.getBatch(batch.batch_id);
  try {
    const updated = await store.updateBatch(batch.batch_id, (current) => {
      current = settleSuccessfulBatch(current);
      if (TERMINAL_BATCH_STATES.has(current.batch_state)) return current;
      const limit = recoveryLimitFor(current, config);
      return limit
        ? buildTerminalBatch(current, 'FAILED', limit.code, limit)
        : current;
    });
    if (updated.batch_state === 'FAILED' || updated.batch_state === 'STOPPED') {
      await store.clearRecovery(updated.batch_id, updated.local_plan).catch(() => {});
    }
    return updated;
  } finally {
    await lock.close();
  }
}

function isWorkerRecoveryPending(batch, config = {}) {
  const startedAt = Date.parse(batch?.worker_recovery_pending_at || '');
  if (!Number.isFinite(startedAt)) return false;
  const startupTimeoutMs = Number(config.workerStartupTimeoutMs) || 10_000;
  const heartbeatStaleMs = Number(config.workerHeartbeatStaleMs) || 5_000;
  const timeoutMs = Math.max(
    MIN_WORKER_RECOVERY_PENDING_MS,
    startupTimeoutMs * 2,
    heartbeatStaleMs * 2,
  );
  const age = Date.now() - startedAt;
  return age >= -5_000 && age <= timeoutMs;
}

async function startBackgroundWorker(batchId, store, timeoutMs, trustedDigest, heartbeatStaleMs = 5_000, config) {
  const startLock = await acquireProcessLock(path.join(store.batchDir(batchId), 'worker-start.lock'), {
    timeoutMs: Math.max(30_000, timeoutMs * 2),
    pollMs: 25,
    busyCode: 'WORKER_START_BUSY',
    busyMessage: '等待后台 worker 启动超时',
  });
  try {
    const batch = await store.getBatch(batchId);
    const readyPath = path.join(store.batchDir(batchId), 'ready.json');
    if (await isWorkerAlive(store, batch, { workerHeartbeatStaleMs: heartbeatStaleMs })) {
      const ready = await readOptionalJson(readyPath);
      if (
        ready?.instance_id === batch.worker_instance_id &&
        ready?.pid === batch.worker_pid
      ) return { ...ready, already_running: true };
    }
    if (!/^[0-9a-f]{64}$/.test(trustedDigest || '')) {
      throw new SkillError('PLAN_INTEGRITY_REQUIRED', '重启后台任务前必须重新校验执行计划', { retryable: true });
    }
    await fs.rm(readyPath, { force: true });
    const instanceId = randomUUID();
    const bin = fileURLToPath(new URL('../bin/guangyapan.mjs', import.meta.url));
    try {
      await launchBackgroundProcess({
        config,
        kind: 'transfer',
        bin,
        args: [
          '__worker', '--batch-id', batchId,
          '--plan-digest', trustedDigest,
          '--instance-id', instanceId,
        ],
      });
    } catch (cause) {
      throw backgroundStartFailure('WORKER_START_FAILED', '后台 worker 启动失败', cause);
    }
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const ready = await readOptionalJson(readyPath);
      if (ready?.instance_id === instanceId && ready?.plan_digest === trustedDigest) return ready;
      await delay(50);
    }
    throw new SkillError('WORKER_START_FAILED', '后台 worker 启动就绪超时', { retryable: true });
  } finally {
    await startLock.close();
  }
}

function assertId(value, name) {
  if (typeof value !== 'string' || !value || value.length > 200) throw new SkillError('INVALID_REQUEST', `${name} 非法`);
}

async function waitForWorkerExit(worker, store, timeoutMs) {
  if (!worker?.worker_pid) return;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline && await isWorkerAlive(store, worker)) await delay(50);
}

async function isWorkerAlive(store, batch, config = {}) {
  if (!batch) return false;
  return store.isWorkerAlive(batch, config?.workerHeartbeatStaleMs ?? 5_000);
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
