

export class SkillError extends Error {
  constructor(code, message, options = {}) {
    super(message, options.cause ? { cause: options.cause } : undefined);
    this.name = 'SkillError';
    this.code = code;
    this.retryable = options.retryable === true;
    this.data = options.data ?? {};
  }
}

export function toSkillError(error) {
  if (error instanceof SkillError) return error;
  const code = errorCauseCode(error);
  return new SkillError('INTERNAL_ERROR', '内部错误', {
    cause: error,
    data: code === undefined ? {} : { cause_code: code },
  });
}

export function errorCauseCode(error) {
  const code = error?.code;
  if (typeof code === 'number' && Number.isFinite(code)) return String(code);
  // Error messages, stacks, paths and arbitrary objects may contain secrets.
  if (typeof code === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(code)) return code;
  // Some stream implementations expose AbortError without a numeric/string code.
  if (error?.name === 'AbortError') return 'ABORT_ERR';
  return undefined;
}

// Pending is a successful submission/query, not completion of the requested work.
export function resultExitCode(result) {
  if (['FAILED', 'PARTIAL'].includes(result.batch_state)) return 1;
  if (result.code === 'LOGIN_REQUIRED' && result.command === 'login-status') return 0;
  if (['LOGIN_PENDING', 'WORKER_STOP_PENDING'].includes(result.code)) return 0;
  if (result.data?.remote_execution_may_continue && result.batch_state === 'STOPPED') return 1;
  if (result.code === 'USER_STOPPED' && ['stop', 'status'].includes(result.command)) return 0;
  return result.code === 'OK' ? 0 : 1;
}

const MAX_REQUEST_BYTES = 1024 * 1024;
const MAX_OPERATIONS = 100;
const READ_TYPES = new Set([
  'list',
  'search',
  'search_share',
  'return_text',
  'resolve_cloud_collection',
  'account_rights',
]);

const ROOT_FIELDS = new Set(['version', 'request_id', 'operations']);
const COMMON_FIELDS = new Set(['id', 'type']);
const OP_FIELDS = {
  list: ['remote_parent_id', 'file_types', 'page', 'page_size'],
  search: ['keyword', 'remote_parent_id', 'file_types', 'page', 'page_size'],
  search_share: ['share_url', 'keyword', 'code', 'page', 'page_size'],
  upload: ['local_path', 'remote_parent_id'],
  download: ['remote_id', 'local_dir'],
  move: ['remote_id', 'target_parent_id'],
  copy: ['remote_id', 'target_parent_id'],
  rename: ['remote_id', 'new_name'],
  create_folder: ['remote_parent_id', 'name'],
  create_share: [
    'remote_ids',
    'title',
    'validity_seconds',
    'share_type',
    'code',
    'download_type',
  ],
  save_share: ['share_url', 'target_parent_id', 'code', 'remote_ids'],
  return_text: ['remote_id'],
  resolve_cloud_collection: ['url', 'torrent_path'],
  account_rights: [],
  cloud_collection_add: [
    'url', 'torrent_path', 'remote_parent_id', 'selection_mode', 'new_name',
  ],
  cloud_collection_delete: ['task_ids', 'delete_files'],
};

export function parseRunRequest(raw) {
  const bytes = Buffer.byteLength(raw, 'utf8');
  if (bytes > MAX_REQUEST_BYTES) {
    throw invalid('请求超过 1 MiB', { bytes, limit: MAX_REQUEST_BYTES });
  }
  let value;
  try {
    value = JSON.parse(raw);
  } catch (cause) {
    throw invalid('请求不是合法 JSON', {}, cause);
  }
  assertObject(value, 'request');
  rejectUnknown(value, ROOT_FIELDS, 'request');
  if (value.version !== 1) invalidThrow('version 必须为 1');
  if (!isUuidV4OrV7(value.request_id)) {
    invalidThrow('request_id 必须为 UUIDv4 或 UUIDv7');
  }
  if (!Array.isArray(value.operations) || value.operations.length === 0) {
    invalidThrow('operations 必须是非空数组');
  }
  if (value.operations.length > MAX_OPERATIONS) {
    invalidThrow(`operations 不能超过 ${MAX_OPERATIONS} 项`);
  }

  const ids = new Set();
  let mode;
  const operations = value.operations.map((operation, index) => {
    const path = `operations[${index}]`;
    assertObject(operation, path);
    assertString(operation.id, `${path}.id`, 1, 128);
    if (!/^[A-Za-z0-9._:-]+$/.test(operation.id)) {
      invalidThrow(`${path}.id 只能包含字母、数字、点、下划线、冒号和连字符`);
    }
    if (ids.has(operation.id)) invalidThrow(`${path}.id 重复`);
    ids.add(operation.id);
    assertString(operation.type, `${path}.type`, 1, 64);
    const allowedFields = OP_FIELDS[operation.type];
    if (!allowedFields) invalidThrow(`${path}.type 不受 V1 支持`);
    rejectUnknown(
      operation,
      new Set([...COMMON_FIELDS, ...allowedFields]),
      path,
    );
    const operationMode = READ_TYPES.has(operation.type) ? 'read' : 'write';
    mode ??= operationMode;
    if (mode !== operationMode) invalidThrow('同一批次不能混合只读和写操作');
    return validateOperation({ ...operation }, path);
  });

  const textCount = operations.filter((item) => item.type === 'return_text').length;
  if (textCount > 10) invalidThrow('return_text 每批最多 10 项');
  return {
    version: 1,
    request_id: value.request_id.toLowerCase(),
    operations,
    mode,
  };
}

function validateOperation(op, path) {
  switch (op.type) {
    case 'list':
      requiredParentId(op, 'remote_parent_id', path, true);
      fileTypes(op, path);
      pagination(op, path);
      break;
    case 'search':
      requiredString(op, 'keyword', path);
      optionalString(op, 'remote_parent_id', path);
      fileTypes(op, path);
      pagination(op, path);
      break;
    case 'search_share':
      shareUrl(op.share_url, `${path}.share_url`);
      requiredString(op, 'keyword', path);
      optionalString(op, 'code', path, 64);
      pagination(op, path);
      break;
    case 'upload':
      requiredString(op, 'local_path', path, 4096);
      requiredParentId(op, 'remote_parent_id', path);
      break;
    case 'download':
      requiredString(op, 'remote_id', path, 40);
      requiredString(op, 'local_dir', path, 4096);
      break;
    case 'move':
    case 'copy':
      requiredString(op, 'remote_id', path, 40);
      requiredParentId(op, 'target_parent_id', path);
      break;
    case 'rename':
      requiredString(op, 'remote_id', path, 40);
      requiredString(op, 'new_name', path, 255);
      singleName(op.new_name, `${path}.new_name`);
      break;
    case 'create_folder':
      requiredParentId(op, 'remote_parent_id', path);
      requiredString(op, 'name', path, 255);
      singleName(op.name, `${path}.name`);
      break;
    case 'create_share':
      stringArray(op.remote_ids, `${path}.remote_ids`, 1, 100, 40);
      optionalString(op, 'title', path, 255);
      if (op.validity_seconds === undefined) op.validity_seconds = 0;
      integer(op.validity_seconds, `${path}.validity_seconds`, 0, 31536000);
      if (op.share_type === undefined) op.share_type = 1;
      integer(op.share_type, `${path}.share_type`, 0, 2);
      optionalString(op, 'code', path, 32);
      if (op.share_type === 2 && op.code === undefined) {
        invalidThrow(`${path}.code 在自定义提取码模式下必传`);
      }
      if (op.share_type === 2 && op.code !== undefined && !/^[A-Za-z0-9]{4}$/.test(op.code)) {
        invalidThrow(`${path}.code 必须是 4 位字母或数字`);
      }
      if (op.share_type !== 2 && op.code !== undefined) {
        invalidThrow(`${path}.code 只能用于自定义提取码模式`);
      }
      if (op.download_type === undefined) op.download_type = 1;
      integer(op.download_type, `${path}.download_type`, 0, 1);
      break;
    case 'save_share':
      shareUrl(op.share_url, `${path}.share_url`);
      requiredParentId(op, 'target_parent_id', path);
      optionalString(op, 'code', path, 64);
      if (op.remote_ids !== undefined) {
        stringArray(op.remote_ids, `${path}.remote_ids`, 1, 100, 40);
      }
      break;
    case 'return_text':
      requiredString(op, 'remote_id', path, 40);
      break;
    case 'resolve_cloud_collection':
      cloudResourceInput(op, path);
      break;
    case 'account_rights':
      break;
    case 'cloud_collection_add':
      cloudResourceInput(op, path);
      requiredParentId(op, 'remote_parent_id', path);
      optionalString(op, 'new_name', path, 255);
      if (op.new_name !== undefined) singleName(op.new_name, `${path}.new_name`);
      if (op.selection_mode !== undefined && !['recommended', 'all'].includes(op.selection_mode)) {
        invalidThrow(`${path}.selection_mode 必须是 recommended 或 all`);
      }
      op.selection_mode ??= 'recommended';
      break;
    case 'cloud_collection_delete':
      stringArray(op.task_ids, `${path}.task_ids`, 1, 100, 40);
      if (op.task_ids.some((id) => !id.trim())) invalidThrow(`${path}.task_ids 不能包含空白 ID`);
      if (op.delete_files !== undefined && typeof op.delete_files !== 'boolean') {
        invalidThrow(`${path}.delete_files 必须是布尔值`);
      }
      op.delete_files ??= false;
      break;
  }
  return op;
}

function fileTypes(op, path) {
  if (op.file_types === undefined) return;
  if (!Array.isArray(op.file_types) || op.file_types.length > 20) {
    invalidThrow(`${path}.file_types 必须是不超过 20 项的数组`);
  }
  for (const value of op.file_types) integer(value, `${path}.file_types[]`, 1, 11);
}

function pagination(op, path) {
  if (op.page === undefined) op.page = 0;
  if (op.page_size === undefined) op.page_size = 100;
  integer(op.page, `${path}.page`, 0, 10_000_000);
  integer(op.page_size, `${path}.page_size`, 1, 100);
}

function rejectUnknown(value, allowed, path) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length) invalidThrow(`${path} 包含未知字段: ${unknown.join(', ')}`);
}

function assertObject(value, path) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    invalidThrow(`${path} 必须是对象`);
  }
}

function assertString(value, path, min = 1, max = 512) {
  if (typeof value !== 'string' || value.length < min || value.length > max) {
    invalidThrow(`${path} 必须是长度 ${min}-${max} 的字符串`);
  }
}

function requiredString(op, key, path, max) {
  assertString(op[key], `${path}.${key}`, 1, max);
}

function requiredParentId(op, key, path, allowAll = false) {
  if (!Object.hasOwn(op, key) || typeof op[key] !== 'string' || op[key].length > 512) {
    invalidThrow(`${path}.${key} 必须是不超过 512 字符的字符串，空字符串表示根目录`);
  }
  if (op[key].includes('*') && !(allowAll && op[key] === '*')) {
    invalidThrow(`${path}.${key} 的 * 全盘范围仅用于 list`);
  }
}

function optionalString(op, key, path, max) {
  if (op[key] !== undefined) assertString(op[key], `${path}.${key}`, 1, max);
}

function integer(value, path, min, max) {
  if (!Number.isInteger(value) || value < min || value > max) {
    invalidThrow(`${path} 必须是 ${min}-${max} 的整数`);
  }
}

function stringArray(value, path, min, max, itemMax = 512) {
  if (!Array.isArray(value) || value.length < min || value.length > max) {
    invalidThrow(`${path} 必须是 ${min}-${max} 项的数组`);
  }
  const unique = new Set();
  value.forEach((item, index) => {
    assertString(item, `${path}[${index}]`, 1, itemMax);
    if (unique.has(item)) invalidThrow(`${path}[${index}] 重复`);
    unique.add(item);
  });
}

function singleName(value, path) {
  if (value === '.' || value === '..' || /[\\/\0]/.test(value)) {
    invalidThrow(`${path} 必须是合法单段名称`);
  }
}

function shareUrl(value, path) {
  assertString(value, path, 1, 2048);
  let url;
  try {
    url = new URL(value);
  } catch {
    invalidThrow(`${path} 不是合法 URL`);
  }
  if (url.protocol !== 'https:') invalidThrow(`${path} 必须使用 HTTPS`);
  const hosts = configuredShareHosts();
  if (!hosts.has(url.hostname.toLowerCase())) {
    invalidThrow(`${path} 域名不在允许列表`);
  }
  const parts = url.pathname.split('/').filter(Boolean);
  const index = parts.indexOf('s');
  if (index < 0 || !parts[index + 1]) invalidThrow(`${path} 缺少分享 ID`);
}

function cloudResourceUrl(value, path) {
  assertString(value, path, 1, 8192);
  const scheme = value.match(/^([a-z][a-z0-9+.-]*):/i)?.[1]?.toLowerCase();
  if (!['http', 'https', 'ftp', 'magnet', 'ed2k', 'thunder'].includes(scheme)) {
    invalidThrow(`${path} 只支持 HTTP、HTTPS、FTP、磁力链、电驴链和迅雷链`);
  }
}

function cloudResourceInput(op, path) {
  const inputs = [op.url, op.torrent_path].filter((value) => value !== undefined);
  if (inputs.length !== 1) {
    invalidThrow(`${path} 必须且只能提供 url 或 torrent_path`);
  }
  if (op.url !== undefined) cloudResourceUrl(op.url, `${path}.url`);
  if (op.torrent_path !== undefined) requiredString(op, 'torrent_path', path, 4096);
}

export function parseShareReference(value, explicitCode) {
  const url = new URL(value);
  const parts = url.pathname.split('/').filter(Boolean);
  const index = parts.indexOf('s');
  return {
    shareId: parts[index + 1],
    code: explicitCode || url.searchParams.get('code') || undefined,
    shareCode: url.searchParams.get('shareCode') || undefined,
  };
}

function configuredShareHosts() {
  const values = (process.env.GUANGYAPAN_DRIVE_SHARE_HOSTS || 'guangyapan.com,www.guangyapan.com')
    .split(',')
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
  return new Set(values);
}

function isUuidV4OrV7(value) {
  return typeof value === 'string' &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[47][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

export function response(command, values = {}) {
  const result = {
    version: 1,
    command,
    code: values.code ?? 'OK',
    retryable: values.retryable === true,
    data: values.data ?? {},
  };
  for (const key of ['request_id', 'batch_id', 'batch_state', 'transfer_state']) {
    if (values[key] !== undefined) result[key] = values[key];
  }
  return result;
}

function invalid(message, data = {}, cause) {
  return new SkillError('INVALID_REQUEST', message, { data, cause });
}

function invalidThrow(message) {
  throw invalid(message);
}

export function formatLocalDateTime(value, timeZone) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
    ...(timeZone ? { timeZone } : {}),
  }).format(date);
}
