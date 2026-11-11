import fs from 'node:fs/promises';
import path from 'node:path';
import {
  syncDirectory,
  removePlannedDownloadPart,
  removeDisposableFile,
  removeEmptyDirectory,
} from './local-files.mjs';
import { acquireProcessLock, isProcessAlive } from './platform.mjs';
import { SkillError } from './protocol.mjs';

export const TERMINAL_BATCH_STATES = new Set(['SUCCEEDED', 'PARTIAL', 'FAILED', 'STOPPED']);
export const TERMINAL_REMOTE_TASK_STATUSES = new Set(['COMPLETED', 'FAILED', 'PARTIAL_COMPLETED']);
export const CLOUD_TRACKING_PAUSE_CODES = new Set(['CLOUD_TASK_NOT_VISIBLE', 'CLOUD_TASK_NO_PROGRESS_TIMEOUT']);

export class StateStore {
  constructor(root) {
    this.root = root;
  }

  async init() {
    await fs.mkdir(this.batchesDir(), { recursive: true, mode: 0o700 });
    if (process.platform !== 'win32') await fs.chmod(this.root, 0o700);
  }

  batchesDir() {
    return path.join(this.root, 'batches');
  }

  batchDir(batchId) {
    safeId(batchId);
    return path.join(this.batchesDir(), batchId);
  }

  async createBatch(record) {
    const dir = this.batchDir(record.batch_id);
    await fs.mkdir(path.join(dir, 'checkpoints'), { recursive: true, mode: 0o700 });
    return this.writeJsonExclusive(path.join(dir, 'runtime.json'), record);
  }

  async getBatch(batchId) {
    try {
      return JSON.parse(await fs.readFile(path.join(this.batchDir(batchId), 'runtime.json'), 'utf8'));
    } catch (error) {
      if (error?.code === 'ENOENT') return undefined;
      throw error;
    }
  }

  async updateBatch(batchId, update) {
    const dir = this.batchDir(batchId);
    const lock = await acquireUpdateLock(dir);
    try {
      const file = path.join(dir, 'runtime.json');
      const current = await this.getBatch(batchId);
      if (!current) throw new SkillError('BATCH_NOT_FOUND', '本地批次不存在');
      const next = typeof update === 'function' ? update(current) : { ...current, ...update };
      next.updated_at = new Date().toISOString();
      await this.writeJson(file, next);
      return next;
    } finally {
      await lock.close();
    }
  }

  async updateItem(batchId, operationId, update) {
    return this.updateBatch(batchId, (current) => ({
      ...current,
      items: current.items.map((item) =>
        item.operation_id === operationId ? { ...item, ...update } : item),
    }));
  }

  async recordPendingUploadReceipt(batchId, operationId, subitemId, receipt) {
    return this.updateBatch(batchId, (current) => ({
      ...current,
      items: current.items.map((item) => item.operation_id === operationId
        ? { ...item, pending_upload_receipts: { ...item.pending_upload_receipts, [subitemId]: receipt } }
        : item),
    }));
  }

  async clearPendingUploadReceipt(batchId, operationId, subitemId) {
    return this.updateBatch(batchId, (current) => ({
      ...current,
      items: current.items.map((item) => {
        if (item.operation_id !== operationId || !Object.hasOwn(item.pending_upload_receipts ?? {}, subitemId)) return item;
        const receipts = { ...item.pending_upload_receipts };
        delete receipts[subitemId];
        return { ...item, pending_upload_receipts: Object.keys(receipts).length ? receipts : undefined };
      }),
    }));
  }

  async cleanupExpired({ retentionMs = 7 * 24 * 60 * 60 * 1000, excludeBatchIds = [] } = {}) {
    await this.init();
    const lock = await acquireProcessLock(path.join(this.root, 'account-operation.lock'));
    if (!lock) return;
    try {
      await this.cleanupExpiredExclusive(retentionMs, new Set(excludeBatchIds));
    } finally {
      await lock.close();
    }
  }

  async cleanupExpiredExclusive(retentionMs, excluded) {
    await this.init();
    const cutoff = Date.now() - retentionMs;
    for (const entry of await fs.readdir(this.batchesDir(), { withFileTypes: true })) {
      if (!entry.isDirectory() || excluded.has(entry.name)) continue;
      const batch = await this.getBatch(entry.name).catch(() => undefined);
      if (!batch || !['SUCCEEDED', 'PARTIAL', 'FAILED', 'STOPPED'].includes(batch.batch_state)) continue;
      const finished = Date.parse(batch.updated_at || batch.created_at || '');
      if (!Number.isFinite(finished) || finished >= cutoff) continue;
      if (hasPendingUploadReceipts(batch) || hasUntrackedRemoteResult(batch)) continue;
      if ((batch.items ?? []).some((item) => item.remote_submission_unconfirmed || (item.remote_task_id && !TERMINAL_REMOTE_TASK_STATUSES.has(item.remote_status)))) continue;
      if (await this.isWorkerAlive(batch) || (batch.worker_pid && await isProcessAlive(batch.worker_pid))) continue;
      const lock = await acquireProcessLock(path.join(this.batchDir(entry.name), 'worker.lock'));
      if (!lock) continue;
      try {
        await this.clearRecovery(entry.name, batch.local_plan);
        await fs.rm(this.batchDir(entry.name), { recursive: true, force: true });
      } catch {
        // Best effort: retain records if owned artifacts cannot be cleaned up.
      } finally {
        await lock.close();
      }
    }
    const preflight = path.join(this.root, 'preflight');
    for (const entry of await fs.readdir(preflight, { withFileTypes: true }).catch(() => [])) {
      if (!entry.isDirectory()) continue;
      if (excluded.has(entry.name) || await this.getBatch(entry.name).catch(() => true)) continue;
      const full = path.join(preflight, entry.name);
      const stat = await fs.stat(full).catch(() => undefined);
      if (stat?.mtimeMs < cutoff) {
        await fs.rm(full, { recursive: true, force: true }).catch(() => {});
      }
    }
  }

  async currentAccountGeneration() {
    try {
      const value = JSON.parse(await fs.readFile(this.accountGenerationPath(), 'utf8'));
      if (!Number.isSafeInteger(value?.generation) || value.generation < 0) {
        throw new SkillError('INVALID_LOCAL_STATE', '本机账号任务代次非法');
      }
      return value.generation;
    } catch (error) {
      if (error?.code === 'ENOENT') return 0;
      throw error;
    }
  }

  async runAccountExclusive(action) {
    await this.init();
    const lock = await acquireProcessLock(path.join(this.root, 'account-operation.lock'), {
      timeoutMs: 60_000,
      pollMs: 10,
      busyCode: 'ACCOUNT_OPERATION_BUSY',
      busyMessage: '等待本机账号操作结束超时',
    });
    try {
      return await action();
    } finally {
      await lock.close();
    }
  }

  async revokeActiveBatches({ accountLockHeld = false } = {}) {
    if (!accountLockHeld) {
      return this.runAccountExclusive(() => this.revokeActiveBatches({ accountLockHeld: true }));
    }
    await this.init();
    const generation = await this.currentAccountGeneration() + 1;
    await this.writeJson(this.accountGenerationPath(), {
      generation,
      revoked_at: new Date().toISOString(),
    });
    const stopped = [];
    let remoteExecutionMayContinue = false;
    for (const entry of await fs.readdir(this.batchesDir(), { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const batch = await this.getBatch(entry.name);
      if (!batch) continue;
      if (batch.batch_state !== 'SUCCEEDED') {
        remoteExecutionMayContinue ||= hasPendingUploadReceipts(batch) || hasUntrackedRemoteResult(batch);
        remoteExecutionMayContinue ||= (batch.items || []).some((item) =>
          item.remote_submission_unconfirmed || (
            item.remote_task_id && item.state !== 'SUCCEEDED' && !TERMINAL_REMOTE_TASK_STATUSES.has(item.remote_status)
          ));
      }
      if (['SUCCEEDED', 'PARTIAL', 'FAILED', 'STOPPED'].includes(batch.batch_state)) continue;
      await this.requestStop(entry.name, 'ACCOUNT_LOGGED_OUT');
      stopped.push({
        batch_id: entry.name,
        worker_pid: batch.worker_pid,
        worker_instance_id: batch.worker_instance_id,
      });
    }
    return { generation, stopped, remote_execution_may_continue: remoteExecutionMayContinue };
  }

  accountGenerationPath() {
    return path.join(this.root, 'account-generation.json');
  }

  checkpointPath(batchId, operationId, subitemId = 'single') {
    safeId(operationId);
    safeId(subitemId);
    return path.join(
      this.batchDir(batchId),
      'checkpoints',
      `${operationId}.${subitemId}.json`,
    );
  }

  async writeCheckpoint(batchId, operationId, subitemId, checkpoint) {
    await this.writeJson(
      this.checkpointPath(batchId, operationId, subitemId),
      checkpoint,
    );
  }

  async readCheckpoint(batchId, operationId, subitemId) {
    try {
      return JSON.parse(
        await fs.readFile(
          this.checkpointPath(batchId, operationId, subitemId),
          'utf8',
        ),
      );
    } catch (error) {
      if (error?.code === 'ENOENT') return undefined;
      throw error;
    }
  }

  async listOperationCheckpoints(batchId, operationId) {
    safeId(operationId);
    const directory = path.join(this.batchDir(batchId), 'checkpoints');
    const prefix = `${operationId}.`;
    const checkpoints = [];
    for (const entry of await fs.readdir(directory, { withFileTypes: true }).catch(() => [])) {
      if (!entry.isFile() || !entry.name.startsWith(prefix) || !entry.name.endsWith('.json')) continue;
      checkpoints.push(JSON.parse(await fs.readFile(path.join(directory, entry.name), 'utf8')));
    }
    return checkpoints;
  }

  async deleteCheckpoint(batchId, operationId, subitemId) {
    await removeDisposableFile(this.checkpointPath(batchId, operationId, subitemId));
  }

  async clearRecovery(batchId, localPlan = []) {
    const directory = path.join(this.batchDir(batchId), 'checkpoints');
    const downloadPlans = localPlan.filter((plan) => plan?.type === 'download');
    for (const entry of await fs.readdir(directory, { withFileTypes: true }).catch(() => [])) {
      if (!entry.isFile() || !entry.name.endsWith('.json')) continue;
      const checkpointPath = path.join(directory, entry.name);
      const plan = downloadPlans.find((candidate) =>
        entry.name.startsWith(`${candidate.operation_id}.`));
      if (plan) await removePlannedDownloadPart(batchId, plan, entry.name, checkpointPath);
      await removeDisposableFile(checkpointPath);
    }
    await this.clearUploadManifests(localPlan);
  }

  workerHeartbeatPath(batchId) {
    return path.join(this.batchDir(batchId), 'worker-heartbeat.json');
  }

  async writeWorkerHeartbeat(batchId, worker) {
    await this.writeVolatileJson(this.workerHeartbeatPath(batchId), {
      pid: worker.pid,
      instance_id: worker.instance_id,
      updated_at: new Date().toISOString(),
    });
  }

  async readWorkerHeartbeat(batchId) {
    try {
      return JSON.parse(await fs.readFile(this.workerHeartbeatPath(batchId), 'utf8'));
    } catch (error) {
      if (error?.code === 'ENOENT') return undefined;
      throw error;
    }
  }

  async isWorkerAlive(batch, staleMs = 5_000) {
    if (!batch?.worker_instance_id || !Number.isInteger(batch.worker_pid)) return false;
    const heartbeat = await this.readWorkerHeartbeat(batch.batch_id).catch(() => undefined);
    if (
      heartbeat?.instance_id !== batch.worker_instance_id ||
      heartbeat?.pid !== batch.worker_pid ||
      !await isProcessAlive(batch.worker_pid)
    ) return false;
    const updatedAt = Date.parse(heartbeat.updated_at || '');
    // Stale heartbeats indicate lack of progress, not process death (SIGSTOP,
    // busy event loop). Never recover or delete checkpoints of a live owner.
    return Number.isFinite(updatedAt);
  }

  async clearWorkerHeartbeat(batchId, instanceId) {
    const heartbeat = await this.readWorkerHeartbeat(batchId).catch(() => undefined);
    if (heartbeat?.instance_id === instanceId) {
      await removeDisposableFile(this.workerHeartbeatPath(batchId));
    }
  }

  async cleanupWorkerArtifacts(batch, staleMs = 5_000) {
    if (!batch) return false;
    // A caller's snapshot can predate worker startup. The actual lock protects
    // both its new heartbeat and the checkpoint files from stale cleanup.
    const lock = await acquireProcessLock(path.join(this.batchDir(batch.batch_id), 'worker.lock'));
    if (!lock) return false;
    try {
      const current = await this.getBatch(batch.batch_id);
      if (!current || (current.worker_pid && await isProcessAlive(current.worker_pid))) return false;
      const heartbeat = await this.readWorkerHeartbeat(batch.batch_id).catch(() => undefined);
      if (heartbeat?.pid && await isProcessAlive(heartbeat.pid)) return false;
      if (!current.worker_instance_id || heartbeat?.instance_id === current.worker_instance_id) {
        await removeDisposableFile(this.workerHeartbeatPath(batch.batch_id));
      }
      await this.cleanupTemporaryWrites(batch.batch_id);
      return !(await this.isWorkerAlive(current, staleMs));
    } finally {
      await lock.close();
    }
  }

  async cleanupTemporaryWrites(batchId) {
    const root = this.batchDir(batchId);
    for (const directory of [root, path.join(root, 'checkpoints')]) {
      for (const entry of await fs.readdir(directory, { withFileTypes: true }).catch(() => [])) {
        const match = /^.+\.json\.(\d+)\.[0-9a-f-]{36}\.tmp$/i.exec(entry.name);
        if (!entry.isFile() || !match || await isProcessAlive(Number(match[1]))) continue;
        await removeDisposableFile(path.join(directory, entry.name));
      }
    }
  }

  async clearOperationRecovery(batchId, operationId, localPlan = []) {
    safeId(operationId);
    const directory = path.join(this.batchDir(batchId), 'checkpoints');
    const prefix = `${operationId}.`;
    for (const entry of await fs.readdir(directory, { withFileTypes: true }).catch(() => [])) {
      if (entry.isFile() && entry.name.startsWith(prefix) && entry.name.endsWith('.json')) {
        await removeDisposableFile(path.join(directory, entry.name));
      }
    }
    await this.clearUploadManifests(
      localPlan.filter((plan) => plan?.operation_id === operationId),
    );
  }

  async clearUploadManifests(localPlan = []) {
    const preflightRoot = path.resolve(this.root, 'preflight');
    for (const plan of localPlan) {
      if (plan?.type !== 'upload_directory' || typeof plan.manifest_path !== 'string') continue;
      const manifestPath = path.resolve(plan.manifest_path);
      if (!manifestPath.startsWith(`${preflightRoot}${path.sep}`)) continue;
      await removeDisposableFile(manifestPath);
      const attemptDir = path.dirname(manifestPath);
      await removeEmptyDirectory(attemptDir);
      await removeEmptyDirectory(path.dirname(attemptDir));
    }
  }

  async requestStop(batchId, code = 'USER_STOPPED') {
    await this.writeJson(path.join(this.batchDir(batchId), 'stop.json'), {
      code,
      requested_at: new Date().toISOString(),
    });
  }

  async isStopRequested(batchId, accountGeneration) {
    assertAccountGeneration(accountGeneration);
    try {
      await fs.access(path.join(this.batchDir(batchId), 'stop.json'));
      return true;
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error;
    }
    return await this.currentAccountGeneration() !== accountGeneration;
  }

  async stopCode(batchId, accountGeneration) {
    assertAccountGeneration(accountGeneration);
    try {
      const value = JSON.parse(await fs.readFile(path.join(this.batchDir(batchId), 'stop.json'), 'utf8'));
      return value?.code || 'USER_STOPPED';
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error;
    }
    if (await this.currentAccountGeneration() !== accountGeneration) {
      return 'ACCOUNT_LOGGED_OUT';
    }
    return undefined;
  }

  async writeJson(file, value) {
    await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    const temporary = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
    const handle = await fs.open(temporary, 'wx', 0o600);
    try {
      try {
        await handle.writeFile(`${JSON.stringify(value)}\n`, 'utf8');
        await handle.sync();
      } finally {
        await handle.close();
      }
      await fs.rename(temporary, file);
      await syncDirectory(path.dirname(file));
    } finally {
      await removeDisposableFile(temporary);
    }
  }

  async writeVolatileJson(file, value) {
    await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    const temporary = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
    try {
      await fs.writeFile(temporary, `${JSON.stringify(value)}\n`, { mode: 0o600, flag: 'wx' });
      await fs.rename(temporary, file);
    } finally {
      await removeDisposableFile(temporary);
    }
  }

  async writeJsonExclusive(file, value) {
    await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    const temporary = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
    const handle = await fs.open(temporary, 'wx', 0o600);
    let created = false;
    try {
      try {
        await handle.writeFile(`${JSON.stringify(value)}\n`, 'utf8');
        await handle.sync();
      } finally {
        await handle.close();
      }
      try {
        await fs.link(temporary, file);
        created = true;
      } catch (error) {
        if (error?.code !== 'EEXIST') throw error;
      }
    } finally {
      await removeDisposableFile(temporary);
    }
    if (created) {
      await syncDirectory(path.dirname(file));
    }
    return created;
  }
}

function safeId(value) {
  if (typeof value !== 'string' || value === '.' || value === '..' || !/^[A-Za-z0-9._:-]{1,200}$/.test(value)) {
    throw new SkillError('INVALID_ID', '状态文件标识非法');
  }
}

function assertAccountGeneration(value) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new SkillError('INVALID_LOCAL_STATE', '批次缺少有效的账号任务代次');
  }
}

async function acquireUpdateLock(directory) {
  return acquireProcessLock(path.join(directory, 'runtime.update.lock'), {
    timeoutMs: 30_000,
    pollMs: 10,
    busyCode: 'LOCAL_STATE_BUSY',
    busyMessage: '本地批次状态正被其他进程更新',
  });
}

const ITEM_TERMINAL = new Set(['SUCCEEDED', 'PARTIAL', 'FAILED', 'STOPPED', 'SKIPPED']);

export function settleSuccessfulBatch(batch) {
  if (!batch.items?.length || !batch.items.every((item) => item.state === 'SUCCEEDED')) return batch;
  return {
    ...batch, batch_state: 'SUCCEEDED', code: 'OK', retryable: false, local_error: undefined,
    transfer_state: batch.local_plan?.length ? 'SUCCEEDED' : 'NONE',
    progress: { ...batch.progress, phase: 'SUCCEEDED', percent: 100, updated_at: new Date().toISOString() },
  };
}

export function buildTerminalBatch(batch, batchState, code, values = {}) {
  if (!['FAILED', 'PARTIAL', 'STOPPED'].includes(batchState)) return { ...batch, ...values };
  const next = { ...batch, ...values };
  const updatedAt = new Date().toISOString();
  // Stopping local tracking cannot revoke an upload already committed remotely.
  // Keep its receipt after recovery checkpoints and ordinary local errors clear.
  const pendingRemoteError = batchState === 'STOPPED' && values.local_error === undefined &&
    batch.local_error?.remote_execution_may_continue === true
    ? batch.local_error
    : undefined;
  return {
    ...next,
    ...(pendingRemoteError ? { local_error: pendingRemoteError } : {}),
    batch_state: batchState,
    transfer_state: terminalTransferState(next, batchState),
    code,
    items: terminalItems(next.items, batchState, code),
    progress: {
      ...(next.progress ?? {}),
      phase: batchState,
      updated_at: updatedAt,
    },
  };
}

export function assertTerminalBatchConsistency(batch) {
  if (!['FAILED', 'PARTIAL', 'STOPPED'].includes(batch.batch_state)) return;
  if (!Array.isArray(batch.items) || batch.items.some((item) => !ITEM_TERMINAL.has(item.state)) ||
      batch.progress?.phase !== batch.batch_state || ['RUNNING', 'PAUSED'].includes(batch.transfer_state)) {
    throw new SkillError('INVALID_LOCAL_STATE', '本地批次终态数据不完整或不一致');
  }
}

function terminalTransferState(batch, state) {
  if (!batch.local_plan?.length) return 'NONE';
  if (['RUNNING', 'PAUSED'].includes(batch.transfer_state)) {
    return state === 'STOPPED' ? 'STOPPED' : 'FAILED';
  }
  return batch.transfer_state;
}

function terminalItems(items, state, code) {
  return items.map((item) => {
    if (ITEM_TERMINAL.has(item.state)) return item;
    if (state !== 'STOPPED' && item.state === 'PENDING' && !item.started_at &&
        !item.remote_task_id && !item.remote_submission_unconfirmed && !item.cloud_delete_receipt) {
      return {
        ...item, state: 'SKIPPED', code: 'OPERATION_NOT_EXECUTED',
        message: '批次已终止，本项未执行',
        error: { code: 'OPERATION_NOT_EXECUTED', cause_code: code, message: '批次已终止，本项未执行' },
      };
    }
    return { ...item, state: state === 'STOPPED' ? 'STOPPED' : 'FAILED', code };
  });
}

// A committed upload can outlive local execution and its recovery checkpoint.
// Receipts remain separate from transient errors and are cleared after validation.
export function hasPendingUploadReceipts(batch) {
  return (batch.items ?? []).some((item) => item.state !== 'SUCCEEDED' &&
    Object.values(item.pending_upload_receipts ?? {}).some((receipt) =>
      typeof receipt?.server_task_id === 'string' && receipt.server_task_id.length > 0));
}

export function hasUntrackedRemoteResult(batch) {
  const error = batch.local_error;
  return error?.remote_execution_may_continue === true && (
    error.upload_committed === true || error.upload_result_unknown === true ||
    !(batch.items ?? []).some((item) => item.remote_task_id)
  );
}

export const MAX_WORKER_RECOVERIES = 1;

export function workerRecoveryLimitError(count, causeCode) {
  return {
    code: 'WORKER_RECOVERY_LIMIT_REACHED',
    message: '后台任务恢复后再次退出，已达到一次恢复上限',
    retryable: false,
    recovery_count: count,
    recovery_limit: MAX_WORKER_RECOVERIES,
    ...(causeCode ? { cause_code: causeCode } : {}),
  };
}

export function stoppedTransferState(batch) {
  if (!batch.local_plan?.length) return 'NONE';
  return batch.transfer_state === 'SUCCEEDED' ? 'SUCCEEDED' : 'STOPPED';
}

export function remoteExecutionMayContinue(batch) {
  if (hasPendingUploadReceipts(batch)) return true;
  const unresolved = (batch.items || []).some((item) =>
    item.remote_submission_unconfirmed || (item.remote_task_id && !TERMINAL_REMOTE_TASK_STATUSES.has(item.remote_status)),
  );
  if (batch.local_error?.remote_execution_may_continue === true) {
    return hasUntrackedRemoteResult(batch) || unresolved;
  }
  const detachedCodes = new Set([
    ...CLOUD_TRACKING_PAUSE_CODES,
    'CLOUD_TASK_RECOVERY_LIMIT_REACHED',
    'WORKER_EXITED',
    'WORKER_RECOVERY_LIMIT_REACHED',
  ]);
  if (!TERMINAL_BATCH_STATES.has(batch.batch_state) && batch.code !== 'WORKER_STOP_PENDING' && !detachedCodes.has(batch.code)) return false;
  return unresolved;
}

export function workerRecoveryCount(batch) {
  return recoveryCount(batch.worker_recovery_count, '后台任务恢复次数非法');
}

export function cloudTaskRecoveryCount(batch) {
  return recoveryCount(batch.cloud_task_recovery_count, '云添加续跟踪次数非法');
}

function recoveryCount(value, message) {
  if (!Number.isSafeInteger(value) || value < 0) throw new SkillError('INVALID_LOCAL_STATE', message);
  return value;
}

export function transfersSucceeded(localPlan, items) {
  const completed = new Set(items.filter(item => item.state === 'SUCCEEDED').map(item => item.operation_id));
  return localPlan.length > 0 && localPlan.every(plan => completed.has(plan.operation_id));
}
