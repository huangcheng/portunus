import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import { createDriveApiClient, DriveApi } from './api-client.mjs';
import {
  stoppedTransferState,
  workerRecoveryCount,
  transfersSucceeded,
  StateStore,
  buildTerminalBatch,
  settleSuccessfulBatch,
  TERMINAL_BATCH_STATES,
  MAX_WORKER_RECOVERIES,
  CLOUD_TRACKING_PAUSE_CODES,
  workerRecoveryLimitError,
} from './batch-store.mjs';
import { refreshCloudTaskStatuses } from './cloud-collection.mjs';
import { loadConfig } from './config.mjs';
import { createCredentialStore, CredentialRefreshCoordinator } from './credential-store.mjs';
import { WriteExecutor } from './file-operations.mjs';
import { assertPlanIntegrity, verifyUploadManifests } from './local-files.mjs';
import { acquireProcessLock } from './platform.mjs';
import { SkillError, toSkillError, errorCauseCode } from './protocol.mjs';
import { TransferEngine } from './transfer.mjs';

const LOCALLY_RESUMABLE = new Set(['upload', 'download', 'create_folder', 'rename']);
const STOP_POLL_INTERVAL_MS = 500;

export async function runWorker(batchId, dependencies = {}) {
  const config = dependencies.config ?? loadConfig({ inheritWorkerContext: true });
  const store = dependencies.store ?? new StateStore(config.agentStateDir);
  await store.init();
  const lock = await acquireWorkerLock(store.batchDir(batchId));
  if (!lock) throw new SkillError('WORKER_LOCK_BUSY', '已有后台任务持有批次锁，未启动重复 worker', { retryable: true });
  const credentials = dependencies.credentials ?? createCredentialStore(config);
  const refreshCoordinator = dependencies.refreshCoordinator ?? new CredentialRefreshCoordinator(config.accountStateDir, credentials);
  const client = dependencies.client ?? createDriveApiClient(config, credentials, refreshCoordinator);
  const driveApi = dependencies.driveApi ?? new DriveApi(client);
  const transfer = dependencies.transfer ?? new TransferEngine({ store, credentials, refreshCoordinator, driveApi, config });
  const executor = dependencies.executor ?? new WriteExecutor({ driveApi, store, transfer, config, credentials });
  const expectedPlanDigest = dependencies.expectedPlanDigest;
  const workerInstanceId = dependencies.workerInstanceId;
  let stopMonitor;
  let heartbeatTimer;
  let ownsWorkerState = false;
  let approvedStopBatch;
  try {
    await store.cleanupTemporaryWrites(batchId);
    let batch = await store.getBatch(batchId);
    if (!batch) throw new SkillError('BATCH_NOT_FOUND', '本地批次不存在');
    assertWorkerInstanceId(workerInstanceId);
    assertPlanIntegrity(batch, expectedPlanDigest);
    approvedStopBatch = structuredClone(batch);
    await verifyUploadManifests(uploadPlansToVerify(batch), config);
    batch = await store.updateBatch(batchId, {
      worker_pid: process.pid,
      worker_instance_id: workerInstanceId,
      worker_started_at: new Date().toISOString(),
      worker_exited_at: undefined,
    });
    ownsWorkerState = true;
    await store.writeWorkerHeartbeat(batchId, {
      pid: process.pid,
      instance_id: workerInstanceId,
    });
    batch = await store.updateBatch(batchId, (current) => (
      current.worker_instance_id === workerInstanceId
        ? { ...current, worker_recovery_pending_at: undefined }
        : current
    ));
    heartbeatTimer = setInterval(() => {
      void store.writeWorkerHeartbeat(batchId, {
        pid: process.pid,
        instance_id: workerInstanceId,
      }).catch(() => {});
    }, 1_000);
    heartbeatTimer.unref?.();
    const initialStopCode = await store.stopCode(batchId, batch.account_generation);
    if (initialStopCode) {
      await stopBatch(store, batch, initialStopCode);
      await writeReady(store, batchId, {
        instanceId: workerInstanceId,
        planDigest: expectedPlanDigest,
      });
      return;
    }
    if (TERMINAL_BATCH_STATES.has(batch.batch_state)) {
      await writeReady(store, batchId, {
        instanceId: workerInstanceId,
        planDigest: expectedPlanDigest,
      });
      return;
    }
    if (batch.batch_state !== 'RUNNING') {
      throw new SkillError('INVALID_LOCAL_STATE', `不可恢复的批次状态: ${batch.batch_state}`);
    }
    const approvedBatch = structuredClone(batch);
    assertPlanIntegrity(approvedBatch, expectedPlanDigest);
    const manifestSnapshots = await verifyUploadManifests(uploadPlansToVerify(approvedBatch), config);
    await writeReady(store, batchId, {
      instanceId: workerInstanceId,
      planDigest: expectedPlanDigest,
    });
    stopMonitor = setInterval(() => {
      void store.isStopRequested(batchId, approvedBatch.account_generation)
        .then((stopping) => { if (stopping) void transfer.stop(); });
    }, STOP_POLL_INTERVAL_MS);
    await executeBatch(batchId, approvedBatch, store, executor, manifestSnapshots);
  } catch (error) {
    const failure = toSkillError(error);
    const current = await store.getBatch(batchId).catch(() => undefined);
    if (current && !TERMINAL_BATCH_STATES.has(current.batch_state)) {
      const stopCode = await store.stopCode(batchId, current.account_generation).catch(() => undefined);
      if (failure.code === 'ACCOUNT_LOGGED_OUT' || failure.code === 'USER_STOPPED' || stopCode) {
        await stopBatch(store, current, stopCode || failure.code).catch(() => {});
        await store.clearRecovery(batchId, current.local_plan).catch(() => {});
        return;
      }
      const updated = await store.updateBatch(batchId, (latest) => {
        latest = settleSuccessfulBatch(latest);
        if (TERMINAL_BATCH_STATES.has(latest.batch_state)) return latest;
        const values = {
          transfer_state: failureTransferState(latest, failure),
          retryable: failure.retryable,
          local_error: failureRecord(failure),
        };
        if (failure.retryable || failure.code === 'LOGIN_REQUIRED') {
          return { ...latest, ...values, batch_state: 'RUNNING', code: failure.code };
        }
        const state = latest.items.some((item) => ['SUCCEEDED', 'PARTIAL'].includes(item.state))
          ? 'PARTIAL' : 'FAILED';
        return buildTerminalBatch(latest, state, failure.code, values);
      }).catch(() => undefined);
      if (updated?.batch_state === 'FAILED' || updated?.batch_state === 'STOPPED' || updated?.batch_state === 'PARTIAL') {
        await store.clearRecovery(batchId, updated.local_plan).catch(() => {});
      }
    }
  } finally {
    if (stopMonitor) clearInterval(stopMonitor);
    // A late create response still needs its real state recorded. This only
    // queries; the API has no remote cancellation operation.
    if (approvedStopBatch &&
        await store.stopCode(batchId, approvedStopBatch.account_generation).catch(() => undefined) === 'USER_STOPPED') {
      await refreshCloudTaskStatuses(approvedStopBatch, { store, credentials, driveApi }).catch(() => {});
    }
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    if (ownsWorkerState) {
      await store.updateBatch(batchId, (current) => (
        current.worker_instance_id === workerInstanceId
          ? { ...current, worker_exited_at: new Date().toISOString() }
          : current
      )).catch(() => {});
      await store.clearWorkerHeartbeat(batchId, workerInstanceId).catch(() => {});
    }
    await lock.close();
  }
}

async function executeBatch(batchId, approvedBatch, store, executor, manifestSnapshots = new Map()) {
  let batch = await store.getBatch(batchId);
  for (let index = 0; index < approvedBatch.operations.length; index += 1) {
    const operation = approvedBatch.operations[index];
    if (await store.isStopRequested(batchId, approvedBatch.account_generation)) {
      await stopBatch(
        store,
        executionBatch(batch, approvedBatch, manifestSnapshots),
        await store.stopCode(batchId, approvedBatch.account_generation) || 'USER_STOPPED',
      );
      return;
    }
    batch = await store.getBatch(batchId);
    if (operation.type === 'cloud_collection_add' && approvedBatch.operations[index + 1]?.type === operation.type) {
      const group = [operation];
      while (approvedBatch.operations[index + 1]?.type === operation.type) group.push(approvedBatch.operations[++index]);
      await executor.executeCloudCollectionGroup(executionBatch(batch, approvedBatch, manifestSnapshots), group);
      batch = await store.getBatch(batchId);
      const groupItems = batch.items.filter((item) => group.some((op) => op.id === item.operation_id));
      if (!groupItems.every((item) => item.state === 'SUCCEEDED')) {
        await finishCloudItems(store, batchId, groupItems);
        await store.clearRecovery(batchId, approvedBatch.local_plan).catch(() => {});
        return;
      }
      continue;
    }
    const item = batch.items.find((value) => value.operation_id === operation.id);
    if (item?.state === 'SUCCEEDED') continue;
    if (operation.type === 'cloud_collection_add' && ['PARTIAL', 'FAILED', 'STOPPED'].includes(item?.state)) {
      if (item.state === 'STOPPED') await stopBatch(store, batch, item.code || 'USER_STOPPED');
      else await finishCloudItems(store, batchId, [item]);
      await store.clearRecovery(batchId, approvedBatch.local_plan).catch(() => {});
      return;
    }
    const receivedCloudDelete = operation.type === 'cloud_collection_delete' && item?.cloud_delete_receipt;
    const cloudRequestNotSubmitted = ['cloud_collection_add', 'cloud_collection_delete'].includes(operation.type) &&
      item?.remote_submission_unconfirmed === false;
    if (item?.state === 'RUNNING' && !item.remote_task_id && !LOCALLY_RESUMABLE.has(operation.type) &&
        !receivedCloudDelete && !cloudRequestNotSubmitted) {
      await failUnknown(store, batchId, operation.id);
      return;
    }
    await store.updateItem(batchId, operation.id, {
      state: 'RUNNING', started_at: item?.started_at || new Date().toISOString(),
      ...(operation.type === 'cloud_collection_add' && item?.state === 'PENDING'
        ? { remote_submission_unconfirmed: false } : {}),
    });
    const isLocalTransfer = approvedBatch.local_plan.some((plan) => plan.operation_id === operation.id);
    batch = await store.updateBatch(batchId, (current) => ({
      ...current,
      batch_state: 'RUNNING',
      transfer_state: isLocalTransfer ? 'RUNNING' : current.transfer_state,
      code: 'OK', retryable: false, local_error: undefined,
      progress: isLocalTransfer
        ? localTransferProgress(current, approvedBatch.local_plan, operation)
        : current.progress,
    }));
    try {
      const result = await executor.execute(
        executionBatch(batch, approvedBatch, manifestSnapshots),
        operation,
      );
      if (await store.isStopRequested(batchId, approvedBatch.account_generation)) {
        await stopBatch(
          store,
          executionBatch(batch, approvedBatch, manifestSnapshots),
          await store.stopCode(batchId, approvedBatch.account_generation) || 'USER_STOPPED',
        );
        return;
      }
      if (result?.partial === true) {
        await store.updateBatch(batchId, (current) => buildTerminalBatch(
          current, 'PARTIAL', result.code ?? 'CLOUD_COLLECTION_PARTIAL', {
            items: current.items.map((value) => value.operation_id === operation.id
              ? {
                ...value, state: 'PARTIAL', result,
                code: result.code ?? 'CLOUD_COLLECTION_PARTIAL', error: result.error,
                completed_at: new Date().toISOString(),
              }
              : value),
            local_error: result.error,
            transfer_state: current.transfer_state === 'RUNNING' ? 'FAILED' : current.transfer_state,
            progress: cloudProgress(current.progress, {
              content_trust: 'untrusted',
              phase: 'PARTIAL',
              percent: result.progress,
              updated_at: new Date().toISOString(),
            }),
          },
        ));
        await store.clearRecovery(batchId, approvedBatch.local_plan).catch(() => {});
        return;
      }
      await store.updateBatch(batchId, (current) => settleSuccessfulBatch({
        ...current,
        items: current.items.map((value) => value.operation_id === operation.id
          ? { ...value, state: 'SUCCEEDED', result, completed_at: new Date().toISOString() }
          : value),
      }));
      await finishSuccessfulOperation(batchId, operation.id, approvedBatch.local_plan, store);
    } catch (error) {
      // Publication can succeed before directory fsync or lock release fails.
      // Reconcile the saved result before assigning an execution failure.
      const recorded = await store.getBatch(batchId);
      const recordedItem = recorded?.items.find((value) => value.operation_id === operation.id);
      if (recordedItem?.state === 'PARTIAL' ||
          (operation.type === 'cloud_collection_add' && recordedItem?.state === 'FAILED')) {
        const stopCode = await store.stopCode(batchId, approvedBatch.account_generation);
        if (stopCode) await stopBatch(store, recorded, stopCode);
        else if (recorded.batch_state === 'RUNNING' && operation.type === 'cloud_collection_add') {
          await finishCloudItems(store, batchId, [recordedItem]);
        }
        await store.clearRecovery(batchId, approvedBatch.local_plan).catch(() => {});
        return;
      }
      if (recordedItem?.state === 'SUCCEEDED') {
        await finishSuccessfulOperation(batchId, operation.id, approvedBatch.local_plan, store);
        if (TERMINAL_BATCH_STATES.has(recorded.batch_state)) return;
        continue;
      }
      const failure = toSkillError(error);
      if (
        failure.code === 'USER_STOPPED' ||
        failure.code === 'ACCOUNT_LOGGED_OUT' ||
        await store.isStopRequested(batchId, approvedBatch.account_generation)
      ) {
        const code = await store.stopCode(batchId, approvedBatch.account_generation) || failure.code;
        await stopBatch(store, executionBatch(batch, approvedBatch, manifestSnapshots), code);
        await store.clearRecovery(batchId, approvedBatch.local_plan).catch(() => {});
        return;
      }
      if (failure.retryable || failure.code === 'LOGIN_REQUIRED') throw failure;
      await store.updateItem(batchId, operation.id, {
        state: 'FAILED',
        code: failure.code,
        message: failure.message,
        error: failureRecord(failure),
      });
      const current = await store.getBatch(batchId);
      const succeeded = current.items.some((value) => value.state === 'SUCCEEDED');
      const values = {
        transfer_state: current.transfer_state === 'RUNNING' ? 'FAILED' : current.transfer_state,
        local_error: failureRecord(failure),
      };
      const updated = await store.updateBatch(batchId, (latest) => buildTerminalBatch(
        latest, succeeded ? 'PARTIAL' : 'FAILED', failure.code, values,
      ));
      if (updated.batch_state === 'PARTIAL' || updated.batch_state === 'FAILED' || updated.batch_state === 'STOPPED') {
        await store.clearRecovery(batchId, updated.local_plan).catch(() => {});
      }
      return;
    }
  }
  batch = await store.getBatch(batchId);
  await store.updateBatch(batchId, {
    batch_state: 'SUCCEEDED', code: 'OK',
    transfer_state: approvedBatch.local_plan.length ? 'SUCCEEDED' : 'NONE',
    progress: { ...batch.progress, phase: 'SUCCEEDED', percent: 100, updated_at: new Date().toISOString() },
  });
  await store.clearRecovery(batchId, approvedBatch.local_plan).catch(() => {});
}

async function finishCloudItems(store, batchId, items) {
  const problem = items.find((item) => item.remote_submission_unconfirmed && !item.remote_task_id) ??
    items.find((item) => item.state === 'FAILED') ?? items.find((item) => item.state === 'PARTIAL');
  const code = problem?.code ?? 'CLOUD_COLLECTION_PARTIAL';
  await store.updateBatch(batchId, (current) => {
    const state = current.items.some((item) => ['SUCCEEDED', 'PARTIAL'].includes(item.state))
      ? 'PARTIAL' : 'FAILED';
    return buildTerminalBatch(current, state, code, {
      retryable: false, local_error: problem?.error,
      transfer_state: current.transfer_state === 'RUNNING' ? 'FAILED' : current.transfer_state,
    });
  });
}

async function finishSuccessfulOperation(batchId, operationId, localPlan, store) {
  await store.clearOperationRecovery(batchId, operationId, localPlan).catch(() => {});
  const completed = await store.getBatch(batchId);
  if (localPlan.some(plan => plan.operation_id === operationId) &&
      transfersSucceeded(localPlan, completed.items) && completed.transfer_state !== 'SUCCEEDED') {
    await store.updateBatch(batchId, { transfer_state: 'SUCCEEDED' });
  }
}

function uploadPlansToVerify(batch) {
  const completed = new Set(batch.items.filter((item) => item.state === 'SUCCEEDED').map((item) => item.operation_id));
  return batch.local_plan.filter((plan) => !completed.has(plan.operation_id));
}

function executionBatch(current, approved, manifestSnapshots = new Map()) {
  return {
    ...current,
    account_id: approved.account_id,
    account_generation: approved.account_generation,
    operations: approved.operations,
    local_plan: approved.local_plan.map((plan) => (
      plan.type === 'upload_directory'
        ? { ...plan, verified_manifest_items: manifestSnapshots.get(plan.operation_id) }
        : plan
    )),
  };
}

async function stopBatch(store, batch, code) {
  await store.updateBatch(batch.batch_id, (current) => buildTerminalBatch(
    current,
    'STOPPED',
    code,
    { transfer_state: stoppedTransferState(current), retryable: false, local_error: undefined },
  ));
}

async function failUnknown(store, batchId, operationId) {
  await store.updateItem(batchId, operationId, { state: 'FAILED', code: 'REMOTE_RESULT_UNKNOWN' });
  const batch = await store.getBatch(batchId);
  const succeeded = batch.items.some((item) => item.state === 'SUCCEEDED');
  const transferState = batch.transfer_state === 'RUNNING' ? 'FAILED' : batch.transfer_state;
  const updated = await store.updateBatch(batchId, (current) => buildTerminalBatch(
    current, succeeded ? 'PARTIAL' : 'FAILED', 'REMOTE_RESULT_UNKNOWN', { transfer_state: transferState },
  ));
  if (TERMINAL_BATCH_STATES.has(updated.batch_state)) {
    await store.clearRecovery(batchId, updated.local_plan).catch(() => {});
  }
}

async function writeReady(store, batchId, worker = {}) {
  await store.writeJson(path.join(store.batchDir(batchId), 'ready.json'), {
    pid: process.pid,
    instance_id: worker.instanceId,
    plan_digest: worker.planDigest,
    ready_at: new Date().toISOString(),
  });
}

function assertWorkerInstanceId(value) {
  if (typeof value !== 'string' || !/^[0-9a-f-]{36}$/i.test(value)) {
    throw new SkillError('WORKER_INSTANCE_REQUIRED', '后台任务缺少 worker 实例标识', { retryable: true });
  }
}

async function acquireWorkerLock(batchDir) {
  return acquireProcessLock(path.join(batchDir, 'worker.lock'));
}

function failureTransferState(batch, failure) {
  if (!batch.local_plan?.length) return 'NONE';
  if (batch.transfer_state !== 'RUNNING') return batch.transfer_state;
  return failure.retryable || failure.code === 'LOGIN_REQUIRED' ? 'PAUSED' : 'FAILED';
}

function failureRecord(failure) {
  const causeCode = errorCauseCode({ code: failure.data.cause_code }) ?? errorCauseCode(failure.cause);
  return {
    ...failure.data,
    code: failure.code,
    message: failure.message,
    retryable: failure.retryable,
    ...(
      causeCode !== undefined
        ? { cause_code: causeCode }
        : {}
    ),
  };
}

function cloudProgress(current, updates) {
  const progress = { ...(current || {}), ...updates };
  delete progress.current_bytes;
  delete progress.total_bytes;
  delete progress.completed_items;
  delete progress.total_items;
  return progress;
}

function localTransferProgress(batch, localPlan, operation) {
  const transferIds = new Set(localPlan.map((plan) => plan.operation_id));
  const transferItems = (batch.items || []).filter((item) => transferIds.has(item.operation_id));
  const completed = transferItems.filter((item) => item.state === 'SUCCEEDED');
  const totalBytes = transferItems.reduce((sum, item) => sum + Number(item.planned_bytes || 0), 0);
  const totalItems = transferItems.reduce((sum, item) => sum + Number(item.planned_items || 0), 0);
  const committedBytes = completed.reduce((sum, item) => sum + Number(item.planned_bytes || 0), 0);
  const committedItems = completed.reduce((sum, item) => sum + Number(item.planned_items || 0), 0);
  const currentBytes = progressFloor(batch.progress?.current_bytes, committedBytes, totalBytes);
  const completedItems = progressFloor(batch.progress?.completed_items, committedItems, totalItems);
  const progress = {
    phase: operation.type === 'upload' ? 'UPLOADING' : 'DOWNLOADING',
    current_bytes: currentBytes,
    total_bytes: totalBytes || undefined,
    completed_items: completedItems,
    total_items: totalItems || undefined,
    updated_at: new Date().toISOString(),
  };
  if (totalBytes) progress.percent = Math.min(100, Math.floor(currentBytes * 100 / totalBytes));
  return progress;
}

function progressFloor(reported, committed, total) {
  const value = Number(reported);
  const floor = Number.isSafeInteger(value) && value >= 0 ? Math.max(value, committed) : committed;
  return total > 0 ? Math.min(floor, total) : floor;
}

// launchd keeps this small supervisor alive while the actual worker can be
// replaced once after an unexpected exit.
export async function runWorkerSupervisor({
  batchId,
  planDigest,
  instanceId,
  bin,
  execPath = process.execPath,
  spawnProcess = spawn,
  environment = process.env,
  config = loadConfig({ environment, inheritWorkerContext: true }),
}) {
  const store = new StateStore(config.agentStateDir);
  await store.init();
  let currentInstanceId = instanceId;
  for (;;) {
    await runChild({
      execPath,
      bin,
      batchId,
      planDigest,
      instanceId: currentInstanceId,
      spawnProcess,
      environment,
    });
    // A heartbeat can expire while a live worker is paused. Only acquiring its
    // actual lock proves that recovery may change the batch or its checkpoints.
    const lock = await acquireProcessLock(path.join(store.batchDir(batchId), 'worker.lock'));
    if (!lock) return;
    try {
      let batch = await store.getBatch(batchId);
      if (!batch || TERMINAL_BATCH_STATES.has(batch.batch_state) || CLOUD_TRACKING_PAUSE_CODES.has(batch.code)) return;
      if (batch.items?.length && batch.items.every((item) => item.state === 'SUCCEEDED')) {
        batch = await store.updateBatch(batchId, settleSuccessfulBatch);
        if (batch.batch_state === 'SUCCEEDED') return;
      }
      const count = workerRecoveryCount(batch);
      if (count >= MAX_WORKER_RECOVERIES) {
        const failed = await store.updateBatch(batchId, (current) => {
          current = settleSuccessfulBatch(current);
          if (TERMINAL_BATCH_STATES.has(current.batch_state)) return current;
          return buildTerminalBatch(current, 'FAILED', 'WORKER_RECOVERY_LIMIT_REACHED', {
            worker_recovery_pending_at: undefined,
            retryable: false,
            local_error: workerRecoveryLimitError(count),
          });
        });
        if (failed.batch_state === 'FAILED' || failed.batch_state === 'STOPPED') {
          await store.clearRecovery(batchId, failed.local_plan).catch(() => {});
        }
        return;
      }
      const next = await store.updateBatch(batchId, (current) => {
        current = settleSuccessfulBatch(current);
        if (TERMINAL_BATCH_STATES.has(current.batch_state) || CLOUD_TRACKING_PAUSE_CODES.has(current.code)) return current;
        return {
          ...current,
          worker_recovery_count: workerRecoveryCount(current) + 1,
          worker_recovery_pending_at: new Date().toISOString(),
          worker_pid: undefined,
          worker_instance_id: undefined,
        };
      });
      if (TERMINAL_BATCH_STATES.has(next.batch_state) || CLOUD_TRACKING_PAUSE_CODES.has(next.code)) return;
    } finally {
      await lock.close();
    }
    currentInstanceId = randomUUID();
  }
}

function runChild({ execPath, bin, batchId, planDigest, instanceId, spawnProcess, environment }) {
  return new Promise((resolve) => {
    const child = spawnProcess(execPath, [
      bin,
      '__worker',
      '--batch-id', batchId,
      '--plan-digest', planDigest,
      '--instance-id', instanceId,
    ], {
      stdio: 'ignore',
      windowsHide: true,
      env: { ...environment, GUANGYAPAN_DRIVE_NO_LAUNCHD_CLEANUP: '1' },
    });
    child.once('error', () => resolve());
    child.once('exit', () => resolve());
  });
}
