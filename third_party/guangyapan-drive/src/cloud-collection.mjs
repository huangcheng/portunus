import path from 'node:path';
import { TERMINAL_BATCH_STATES, TERMINAL_REMOTE_TASK_STATUSES } from './batch-store.mjs';
import { openSourceFile } from './local-files.mjs';
import { SkillError, toSkillError } from './protocol.mjs';

const MAX_TORRENT_BYTES = 10 * 1024 * 1024;
const DEFAULT_TREE_DEPTH = 32;
const DEFAULT_TREE_ENTRIES = 100_000;

const STATUS_NAMES = {
  0: 'PENDING',
  1: 'RUNNING',
  2: 'COMPLETED',
  3: 'FAILED',
  5: 'PARTIAL_COMPLETED',
};

export function normalizeCloudResource(envelope, fallbackUrl, limits = {}, expectedResType) {
  const data = envelope?.data ?? envelope;
  const resType = cloudResourceType(data, fallbackUrl, expectedResType);
  const info = resType === 3
    ? data?.emuleResInfo
    : resType === 1 || resType === 2
      ? data?.btResInfo
      : data?.urlResInfo;
  const isDirectUrl = resType === 0;
  if (
    !Number.isInteger(resType) || resType < 0 || resType > 3 ||
    (!isDirectUrl && !info?.fileName)
  ) {
    throw new SkillError('INVALID_API_RESPONSE', '云添加解析结果不完整');
  }
  const files = resType === 1 || resType === 2
    ? normalizeCloudTree(info.subfiles, limits)
    : [];
  const fileIndexes = uniqueIntegers(leafFiles(files).map((file) => file.file_index));
  const availableIndexes = new Set(fileIndexes);
  const excludedFileIndexes = uniqueIntegers(info?.excludeIndices ?? [])
    .filter((index) => availableIndexes.has(index));
  const excludedIndexes = new Set(excludedFileIndexes);
  const url = data.url || info?.url || fallbackUrl || torrentMagnet(info?.infoHash);
  if (typeof url !== 'string' || !url) {
    throw new SkillError('INVALID_API_RESPONSE', '云添加解析结果缺少可提交的资源链接');
  }
  return {
    res_type: resType,
    url,
    name: info?.fileName,
    size: finiteNumber(info?.fileSize),
    is_directory: files.length > 0,
    file_indexes: fileIndexes,
    recommended_file_indexes: fileIndexes.filter((index) => !excludedIndexes.has(index)),
    excluded_file_indexes: excludedFileIndexes,
    files,
    content_trust: 'untrusted',
  };
}

export async function readTorrentFile(file) {
  if (!path.isAbsolute(file)) {
    throw new SkillError('INVALID_LOCAL_PATH', '种子文件路径必须是绝对路径');
  }
  if (path.extname(file).toLowerCase() !== '.torrent') {
    throw new SkillError('INVALID_LOCAL_PATH', 'torrent_path 必须指向 .torrent 普通文件');
  }
  const source = await openSourceFile(file);
  try {
    if (source.size <= 0 || source.size > MAX_TORRENT_BYTES) {
      throw new SkillError('INVALID_LOCAL_PATH', '种子文件必须大于 0 且不超过 10 MiB');
    }
    const data = Buffer.alloc(source.size);
    let offset = 0;
    while (offset < data.length) {
      const { bytesRead } = await source.reader.read(data, offset, data.length - offset, offset);
      if (!bytesRead) throw new SkillError('LOCAL_SOURCE_CHANGED', '种子文件读取不完整');
      offset += bytesRead;
    }
    await source.reader.stat();
    return { name: path.basename(file), data };
  } finally {
    await source.close();
  }
}

export function validateCloudSelection(resource, fileIndexes) {
  const isBt = resource.res_type === 1 || resource.res_type === 2;
  if (!fileIndexes) return;
  if (!isBt) {
    throw new SkillError('INVALID_REQUEST', '只有磁力链或 BT 资源可以指定 file_indexes');
  }
  const available = new Set(resource.file_indexes);
  const invalid = fileIndexes.filter((value) => !available.has(value));
  if (invalid.length) {
    throw new SkillError('INVALID_REQUEST', 'file_indexes 包含资源中不存在的文件索引', {
      data: { invalid_file_indexes: invalid },
    });
  }
}

export function cloudResourceSummary(resource, selectedIndexes) {
  const selectedSet = selectedIndexes ? new Set(selectedIndexes) : undefined;
  const selected = selectedIndexes
    ? leafFiles(resource.files).filter((file) => selectedSet.has(file.file_index))
    : undefined;
  return {
    total_bytes: selected
      ? selected.reduce((sum, file) => sum + finiteNumber(file.size), 0)
      : finiteNumber(resource.size),
    transfer_items: selected === undefined
      ? resource.file_indexes.length || 1
      : selected.length,
  };
}

export function cloudResourceFileSelection(resource, selectedIndexes) {
  const availableIndexes = new Set(resource.file_indexes);
  const seenIndexes = new Set();
  const files = leafFilesWithPath(resource.files).filter((file) => {
    if (!availableIndexes.has(file.file_index) || seenIndexes.has(file.file_index)) return false;
    seenIndexes.add(file.file_index);
    return true;
  });
  if (!files.length) return undefined;

  const selectedSet = selectedIndexes === undefined
    ? new Set(resource.file_indexes)
    : new Set(selectedIndexes);
  const recommendedSet = new Set(resource.recommended_file_indexes);
  const excludedSet = new Set(resource.excluded_file_indexes);
  const selected = files.filter((file) => selectedSet.has(file.file_index));
  const recommended = files.filter((file) => recommendedSet.has(file.file_index));

  return {
    selection_mode: selectionMode(selectedSet, recommendedSet, files.length),
    total_count: files.length,
    selected_count: selected.length,
    recommended_count: recommended.length,
    excluded_count: files.filter((file) => excludedSet.has(file.file_index)).length,
    files,
    selected_indexes: selected.map((file) => file.file_index),
    recommended_indexes: recommended.map((file) => file.file_index),
  };
}

export function normalizeCloudTask(value) {
  if (!value?.taskId) throw new SkillError('INVALID_API_RESPONSE', '云添加任务结果缺少 taskId');
  const status = Number(value.status);
  if (!Object.hasOwn(STATUS_NAMES, status)) {
    throw new SkillError('INVALID_API_RESPONSE', '云添加任务状态非法');
  }
  const sourceFiles = Array.isArray(value.files) ? value.files : [];
  return {
    content_trust: 'untrusted',
    task_id: value.taskId,
    status,
    status_name: STATUS_NAMES[status],
    progress: Math.max(0, Math.min(100, finiteNumber(value.progress))),
    name: boundedText(value.fileName, 512),
    size: finiteNumber(value.totalSize),
    file_id: value.fileId || undefined,
    is_directory: value.isDir === true,
    error_code: Number(value.errCode || 0),
    error_message: boundedText(value.errMsg, 1024),
    files: sourceFiles.slice(0, 200).map(normalizeCloudTaskFile),
    files_truncated: sourceFiles.length > 200 || undefined,
  };
}

function normalizeCloudTree(values, limits) {
  const source = Array.isArray(values) ? values : [];
  const output = [];
  const maxDepth = positiveLimit(limits.maxDirectoryDepth, DEFAULT_TREE_DEPTH);
  const maxEntries = positiveLimit(limits.maxDirectoryEntries, DEFAULT_TREE_ENTRIES);
  enforceTreeLimit('max_directory_entries', maxEntries, source.length);
  const stack = [];
  for (let index = source.length - 1; index >= 0; index -= 1) {
    stack.push({ value: source[index], target: output, depth: 1 });
  }
  let entries = 0;
  while (stack.length) {
    const { value, target, depth } = stack.pop();
    entries += 1;
    enforceTreeLimit('max_directory_entries', maxEntries, entries);
    enforceTreeLimit('max_directory_depth', maxDepth, depth);
    const isDirectory = value?.isDir === true || value?.isDir === 1;
    const normalized = {
      name: value?.fileName,
      is_directory: isDirectory,
      // protobuf JSON may omit uint32 zero values; the Web client also treats omission as index 0.
      file_index: isDirectory ? undefined : normalizeFileIndex(value?.fileIndex),
      size: finiteNumber(value?.fileSize),
      files: [],
    };
    target.push(normalized);
    if (!isDirectory) continue;
    const children = Array.isArray(value?.subfiles) ? value.subfiles : [];
    enforceTreeLimit(
      'max_directory_entries',
      maxEntries,
      entries + stack.length + children.length,
    );
    for (let index = children.length - 1; index >= 0; index -= 1) {
      stack.push({ value: children[index], target: normalized.files, depth: depth + 1 });
    }
  }
  return output;
}

function normalizeCloudTaskFile(value) {
  return {
    content_trust: 'untrusted',
    name: boundedText(value?.name, 512),
    size: finiteNumber(value?.size),
    file_index: Number(value?.btIndex),
    progress: Math.max(0, Math.min(100, finiteNumber(value?.progress))),
    status: Number(value?.status),
    error_code: Number(value?.errCode || 0),
    error_message: boundedText(value?.errMsg, 1024),
    selected: value?.selected === true,
  };
}

function boundedText(value, maxChars) {
  if (typeof value !== 'string' || !value) return undefined;
  return value.length > maxChars ? value.slice(0, maxChars) : value;
}

function leafFiles(files) {
  const output = [];
  const stack = [...files].reverse();
  while (stack.length) {
    const file = stack.pop();
    if (!file.is_directory) {
      output.push(file);
      continue;
    }
    for (let index = file.files.length - 1; index >= 0; index -= 1) {
      stack.push(file.files[index]);
    }
  }
  return output;
}

function leafFilesWithPath(files) {
  const output = [];
  const stack = [];
  for (let index = files.length - 1; index >= 0; index -= 1) {
    stack.push({ file: files[index], parent: '' });
  }
  while (stack.length) {
    const { file, parent } = stack.pop();
    const name = typeof file.name === 'string' && file.name ? file.name : '未命名文件';
    const filePath = parent ? `${parent}/${name}` : name;
    if (file.is_directory) {
      for (let index = file.files.length - 1; index >= 0; index -= 1) {
        stack.push({ file: file.files[index], parent: filePath });
      }
      continue;
    }
    output.push({
      file_index: file.file_index,
      name,
      path: filePath,
      size: file.size,
    });
  }
  return output;
}

function positiveLimit(value, fallback) {
  return Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

function enforceTreeLimit(name, limit, observed) {
  if (observed > limit) {
    throw new SkillError('DIRECTORY_LIMIT_EXCEEDED', '云添加目录超过配置限制', {
      data: { directory_limit: { name, limit, observed } },
    });
  }
}

function uniqueIntegers(values) {
  return [...new Set(values.map(Number).filter((value) => Number.isSafeInteger(value) && value >= 0))];
}

function normalizeFileIndex(value) {
  const index = Number(value ?? 0);
  return Number.isSafeInteger(index) && index >= 0 ? index : undefined;
}

function selectionMode(selected, recommended, totalCount) {
  if (selected.size === 0) return 'none';
  if (selected.size === recommended.size && [...selected].every((value) => recommended.has(value))) {
    return 'recommended';
  }
  if (selected.size === totalCount) return 'all';
  return 'custom';
}

function finiteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : 0;
}

function cloudResourceType(data, fallbackUrl, expectedResType) {
  if (expectedResType !== undefined) return expectedResType;
  if (data?.resType !== undefined && data?.resType !== null) {
    return data.resType === '' ? Number.NaN : Number(data.resType);
  }
  const sections = [
    ['urlResInfo', 0],
    ['emuleResInfo', 3],
    ['btResInfo', /^magnet:/i.test(fallbackUrl || '') ? 1 : 2],
  ].filter(([key]) => data?.[key] !== undefined && data?.[key] !== null);
  return sections.length === 1 ? sections[0][1] : Number.NaN;
}

function torrentMagnet(infoHash) {
  return infoHash ? `magnet:?xt=urn:btih:${infoHash}` : undefined;
}

// Submit one request at a time and durably save its receipt before the next
// write. Completion is independent: one slow download cannot hold up submission.
export async function executeCloudCollectionGroup({ batch, operations, files, store, config }) {
  const trackingStartedAt = Date.now();
  for (const operation of operations) {
    await assertRunning(store, batch);
    let item = await currentItem(store, batch, operation);
    if (!item.remote_task_id && (item.remote_submission_unconfirmed ||
        (item.state === 'RUNNING' && item.remote_submission_unconfirmed !== false))) {
      if (!TERMINAL_BATCH_STATES.has(item.state)) await settle(store, batch, operation, failureOutcome(unknownSubmission(operation)));
      break;
    }
    if (TERMINAL_BATCH_STATES.has(item.state) || item.remote_task_id) continue;
    await store.updateItem(batch.batch_id, operation.id, {
      state: 'RUNNING', started_at: item.started_at || new Date().toISOString(),
      remote_submission_unconfirmed: true,
    });
    let response;
    try {
      response = await files.createCloudCollectionTask(operation);
    } catch (error) {
      const failure = toSkillError(error);
      if (failure.data.request_not_accepted === true) {
        await store.updateItem(batch.batch_id, operation.id, { remote_submission_unconfirmed: false });
        // Only definite, non-transient business rejections are item failures.
        // Stop, auth, session and local lock errors must abort this execution.
        if (!failure.retryable && /^REMOTE_API_ERROR_\d+$/.test(failure.code)) {
          await settle(store, batch, operation, failureOutcome(failure));
          continue;
        }
        throw failure;
      }
      await settle(store, batch, operation, failureOutcome(unknownSubmission(operation, failure)));
      break;
    }
    const taskId = response?.data?.taskId;
    if (typeof taskId !== 'string' || !taskId.trim() || taskId.length > 128) {
      await settle(store, batch, operation, failureOutcome(unknownSubmission(operation)));
      break;
    }
    try {
      await store.updateItem(batch.batch_id, operation.id, {
        remote_task_id: taskId, remote_submission_unconfirmed: false,
      });
    } catch (error) {
      // rename may have published the receipt before fsync/lock release failed.
      item = await currentItem(store, batch, operation);
      if (item.remote_task_id !== taskId) throw error;
    }
  }

  const missingSince = new Map();
  for (;;) {
    await assertRunning(store, batch);
    const current = await store.getBatch(batch.batch_id);
    const pending = operations.map((operation) => ({
      operation, item: current.items.find((item) => item.operation_id === operation.id),
    })).filter(({ item }) => item.remote_task_id && !TERMINAL_BATCH_STATES.has(item.state));
    if (!pending.length) return;
    await store.updateBatch(batch.batch_id, (value) => ({
      ...value,
      progress: groupProgress(value, operations, 'CLOUD_COLLECTION_RUNNING'),
    }));
    const taskIds = [...new Set(pending.map(({ item }) => item.remote_task_id))];
    if (taskIds.length > 100) throw new SkillError('INVALID_LOCAL_STATE', '云添加查询不能超过 100 个任务');
    const envelope = await files.listCloudCollectionTasks(taskIds);
    const tasks = new Map((Array.isArray(envelope?.data?.list) ? envelope.data.list : [])
      .filter((task) => taskIds.includes(task?.taskId)).map((task) => [task.taskId, task]));
    let unavailable;
    for (const { operation, item } of pending) {
      let task;
      try { task = normalizeCloudTask(tasks.get(item.remote_task_id)); }
      catch { /* A missing or malformed row does not discard other results. */ }
      if (!task) {
        if (!missingSince.has(operation.id)) missingSince.set(operation.id, Date.now());
        await store.updateItem(batch.batch_id, operation.id, { remote_status: 'NOT_VISIBLE' });
        if (Date.now() - missingSince.get(operation.id) >= (config.cloudTaskVisibilityTimeoutMs ?? 600_000)) {
          unavailable ??= new SkillError('CLOUD_TASK_NOT_VISIBLE', '云添加任务暂时无法查询，服务端仍可能继续执行', {
            retryable: true,
            data: { task_id: item.remote_task_id, remote_execution_may_continue: true },
          });
        }
        continue;
      }
      missingSince.delete(operation.id);
      const progress = task.status === 2 ? 100 : task.progress;
      const now = Date.now();
      const changed = item.remote_last_observed_status !== task.status_name || item.remote_progress !== progress;
      const previousChangedAt = Date.parse(item.remote_last_changed_at || '');
      const lastChangedAt = changed || !Number.isFinite(previousChangedAt) ? now : previousChangedAt;
      const noProgress = ![2, 3, 5].includes(task.status) &&
        now - lastChangedAt >= (config.cloudTaskNoProgressWarningMs ?? 120_000);
      await store.updateItem(batch.batch_id, operation.id, {
        remote_status: task.status_name, remote_progress: progress,
        remote_last_observed_status: task.status_name,
        remote_last_changed_at: new Date(lastChangedAt).toISOString(),
        remote_last_observed_at: new Date(now).toISOString(), remote_no_progress: noProgress,
      });
      if (task.status === 2 || task.status === 5) {
        const result = { ...task, progress, ...(task.status === 5 ? { partial: true } : {}) };
        await settle(store, batch, operation, {
          state: task.status === 2 ? 'SUCCEEDED' : 'PARTIAL', result,
          ...(task.status === 5 ? { code: 'CLOUD_COLLECTION_PARTIAL' } : {}),
        });
      } else if (task.status === 3) {
        await settle(store, batch, operation, failureOutcome(new SkillError(
          'CLOUD_COLLECTION_FAILED', task.error_message || '云添加失败，服务端未提供原因', {
            data: {
              remote_code: task.error_code, task_id: item.remote_task_id,
              remote_reason_reported: Boolean(task.error_message),
              content_trust: task.error_message ? 'untrusted' : undefined,
            },
          },
        )));
      } else if (now - Math.max(lastChangedAt, trackingStartedAt) >= (config.cloudTaskNoProgressTimeoutMs ?? 600_000)) {
        unavailable ??= new SkillError('CLOUD_TASK_NO_PROGRESS_TIMEOUT', '云添加长时间无进展，已暂停本地跟踪；云端任务可能仍在执行，请勿重复提交', {
          retryable: true,
          data: { task_id: item.remote_task_id, remote_execution_may_continue: true },
        });
      }
    }
    await store.updateBatch(batch.batch_id, (value) => ({
      ...value,
      progress: groupProgress(value, operations, missingSince.size ? 'CLOUD_TASK_PENDING_VISIBILITY' : 'CLOUD_COLLECTION_RUNNING'),
    }));
    if (unavailable) throw unavailable;
    const latest = await store.getBatch(batch.batch_id);
    if (operations.every((op) => {
      const item = latest.items.find((item) => item.operation_id === op.id);
      return TERMINAL_BATCH_STATES.has(item.state) || !item.remote_task_id;
    })) return;
    await delay(config.cloudTaskPollIntervalMs ?? 5_000);
  }
}

async function currentItem(store, batch, operation) {
  const current = await store.getBatch(batch.batch_id);
  const item = current?.items.find((value) => value.operation_id === operation.id);
  if (!item) throw new SkillError('INVALID_LOCAL_STATE', '云添加执行项不存在');
  return item;
}

async function assertRunning(store, batch) {
  const code = await store.stopCode(batch.batch_id, batch.account_generation);
  if (code) throw new SkillError(code, '任务已停止');
}

function unknownSubmission(operation, cause) {
  return new SkillError('REMOTE_RESULT_UNKNOWN', '云添加提交结果未确认，请先核对任务列表，不重复提交', {
    cause,
    data: { operation_id: operation.id, remote_execution_may_continue: true },
  });
}

function failureOutcome(failure) {
  return {
    state: 'FAILED', code: failure.code, message: failure.message,
    error: { ...failure.data, code: failure.code, message: failure.message, retryable: failure.retryable },
  };
}

async function settle(store, batch, operation, outcome) {
  const updates = { ...outcome, completed_at: new Date().toISOString() };
  try { await store.updateItem(batch.batch_id, operation.id, updates); }
  catch (error) {
    const saved = await currentItem(store, batch, operation);
    if (saved.state !== outcome.state || saved.completed_at !== updates.completed_at) throw error;
  }
}

function groupProgress(batch, operations, phase) {
  const ids = new Set(operations.map((op) => op.id));
  const items = batch.items.filter((item) => ids.has(item.operation_id));
  const percent = Math.floor(items.reduce((sum, item) => sum + (
    TERMINAL_BATCH_STATES.has(item.state) ? 100 : Number(item.remote_progress) || 0
  ), 0) / items.length);
  const noProgressItems = items.filter((item) => item.remote_no_progress && !TERMINAL_BATCH_STATES.has(item.state));
  return {
    content_trust: 'untrusted', phase, percent, updated_at: new Date().toISOString(),
    ...(noProgressItems.length ? {
      warning: {
        code: 'CLOUD_TASK_NO_PROGRESS', task_ids: noProgressItems.map((item) => item.remote_task_id),
        message: '云添加长时间无进展，后台仍在查询；可稍后查看状态，请勿重复提交',
      },
    } : {}),
  };
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// A successful delete returns only the IDs changed by that call. An omitted ID
// may already be gone. Reconcile it with a read; never repeat the deletion.
export async function cloudDeleteResult(operation, receipt, files) {
  const deleted = new Set(receipt.task_ids);
  const omitted = operation.task_ids.filter((id) => !deleted.has(id));
  let present;
  let queryError;
  if (omitted.length) {
    try {
      present = listedTaskIds(await files.listCloudCollectionTasks(omitted), omitted);
    } catch (error) {
      const failure = toSkillError(error);
      if (['USER_STOPPED', 'ACCOUNT_LOGGED_OUT', 'LOGIN_REQUIRED', 'LOGIN_ACCOUNT_CHANGED', 'ACCOUNT_OPERATION_BUSY'].includes(failure.code)) {
        throw failure;
      }
      queryError = failure;
    }
  }
  const absent = omitted.filter((id) => present && !present.has(id));
  const remaining = omitted.filter((id) => present?.has(id));
  const unconfirmed = queryError ? omitted : [];
  const taskResults = operation.task_ids.map((id) => {
    if (deleted.has(id)) return { task_id: id, status: 'DELETED', code: 'OK', retryable: false };
    if (queryError) return {
      task_id: id, status: 'UNKNOWN', code: 'CLOUD_COLLECTION_DELETE_UNCONFIRMED', retryable: false,
      message: '删除回执未确认该任务，后续查询也未能确认；不要重复删除',
      error: { ...queryError.data, code: queryError.code, message: queryError.message, retryable: queryError.retryable },
    };
    if (present.has(id)) return {
      task_id: id, status: 'NOT_DELETED', code: 'CLOUD_COLLECTION_TASK_NOT_DELETED', retryable: false,
      message: '删除回执未包含该任务，后续查询仍存在，不自动重试删除',
      error: { code: 'CLOUD_COLLECTION_TASK_NOT_DELETED', message: '后续查询中任务仍然存在', retryable: false },
    };
    return {
      task_id: id, status: 'NOT_FOUND', code: 'CLOUD_COLLECTION_TASK_NOT_FOUND', retryable: false,
      message: '当前账号的任务列表中已不存在，无需再次删除任务记录',
      ...(receipt.delete_files ? { file_delete_status: 'UNCONFIRMED' } : {}),
    };
  });
  const incomplete = remaining.length > 0 || unconfirmed.length > 0 || (receipt.delete_files && omitted.length > 0);
  return {
    requested_task_ids: operation.task_ids,
    deleted_task_ids: receipt.task_ids,
    absent_task_ids: absent,
    not_deleted_task_ids: remaining,
    unconfirmed_task_ids: unconfirmed,
    task_results: taskResults,
    delete_files: receipt.delete_files,
    file_delete_task_id: receipt.file_task_id,
    file_delete_status: !receipt.delete_files ? 'NOT_REQUESTED'
      : omitted.length ? 'UNCONFIRMED' : receipt.file_task_id ? 'COMPLETED' : 'NO_TASK',
    remote_stop_supported: false,
    ...(incomplete ? {
      partial: true, code: 'CLOUD_COLLECTION_DELETE_PARTIAL',
      error: {
        code: 'CLOUD_COLLECTION_DELETE_PARTIAL', retryable: false,
        message: '部分任务记录或对应文件的删除未确认，请查看 task_results；不自动重复删除',
      },
    } : {}),
  };
}

function listedTaskIds(envelope, requested) {
  const data = envelope?.data;
  const list = data?.list === undefined ? [] : data.list;
  if (!data || typeof data !== 'object' || Array.isArray(data) || !Array.isArray(list) ||
      list.some((task) => typeof task?.taskId !== 'string' || !requested.includes(task.taskId))) {
    throw new SkillError('INVALID_API_RESPONSE', '云添加任务核对结果无效');
  }
  const ids = new Set(list.map((task) => task.taskId));
  if (ids.size !== list.length ||
      (data.total !== undefined && (!Number.isSafeInteger(data.total) || data.total !== ids.size)) ||
      (data.hasMore !== undefined && typeof data.hasMore !== 'boolean') ||
      (data.hasMore === true && ids.size < requested.length)) {
    throw new SkillError('INVALID_API_RESPONSE', '云添加任务核对结果不完整，不能确认任务不存在');
  }
  return ids;
}

// The current API supports querying cloud tasks, not cancelling them. Deleting a
// task record is not cancellation and would destroy our ability to reconcile it.
export async function refreshCloudTaskStatuses(batch, { store, credentials, driveApi }) {
  const cloudIds = new Set((batch.operations ?? []).filter((op) => op.type === 'cloud_collection_add').map((op) => op.id));
  const pending = (await store.getBatch(batch.batch_id))?.items?.filter((item) =>
    cloudIds.has(item.operation_id) && item.remote_task_id && !TERMINAL_REMOTE_TASK_STATUSES.has(item.remote_status)) ?? [];
  if (!pending.length) return;
  try {
    await store.runAccountExclusive(async () => {
      for (const previous of pending) {
        const current = await store.getBatch(batch.batch_id);
        const item = current?.items.find((value) => value.operation_id === previous.operation_id);
        if (!item?.remote_task_id || TERMINAL_REMOTE_TASK_STATUSES.has(item.remote_status)) continue;
        try {
          const session = await credentials?.getSession({ fresh: true });
          if (!session?.credentials) throw new SkillError('LOGIN_REQUIRED', '需要原账号登录态才能查询云添加状态');
          if (session.credentials.accountId !== batch.account_id ||
              await store.currentAccountGeneration() !== batch.account_generation) {
            throw new SkillError('LOGIN_ACCOUNT_CHANGED', '当前账号或任务代次已变更，未查询原账号的云添加任务');
          }
          const envelope = await driveApi.listCloudCollectionTasks([item.remote_task_id], { expectedSession: session });
          const value = (envelope.data?.list ?? []).find((task) => task.taskId === item.remote_task_id);
          if (!value) throw new SkillError('CLOUD_TASK_NOT_VISIBLE', '暂时无法查询云添加任务，不能确认远端已结束');
          const task = normalizeCloudTask(value);
          await store.updateItem(batch.batch_id, item.operation_id, {
            remote_status: task.status_name,
            remote_progress: task.status === 2 ? 100 : task.progress,
            remote_check: { state: TERMINAL_REMOTE_TASK_STATUSES.has(task.status_name) ? 'TERMINAL' : 'ACTIVE', checked_at: new Date().toISOString() },
          });
        } catch (error) {
          await recordUnavailable(store, batch.batch_id, item, error);
        }
      }
    });
  } catch (error) {
    for (const item of pending) await recordUnavailable(store, batch.batch_id, item, error);
  }
}

function recordUnavailable(store, batchId, item, error) {
  return store.updateItem(batchId, item.operation_id, {
    remote_check: { state: 'UNAVAILABLE', code: toSkillError(error).code, checked_at: new Date().toISOString() },
  });
}
