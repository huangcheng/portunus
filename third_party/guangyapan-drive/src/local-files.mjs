import { createHash } from 'node:crypto';
import { constants, createReadStream } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline';
import { SkillError } from './protocol.mjs';

export const DOWNLOAD_TARGET_EXISTS_MESSAGE = '下载目标已存在同名文件或目录，请改用空目录或先处理冲突';

export const sourceIdentity = (stat) => ({ device: String(stat.dev), inode: String(stat.ino) });
const sameIdentity = (stat, expected) => expected?.device === String(stat.dev) && expected?.inode === String(stat.ino);
const sourcePathChanged = (cause) => new SkillError('LOCAL_SOURCE_CHANGED', '本地源文件或父目录发生变化，已停止读取', { cause });

/** Pin canonical ancestors before opening. POSIX ancestors must resist other OS users' renames. */
export async function inspectSourcePath(file) {
  try {
    return await captureSourcePath(file);
  } catch (cause) {
    if (cause instanceof SkillError) throw cause;
    throw new SkillError('LOCAL_PATH_NOT_FOUND', '本地源路径不存在或不可访问', { cause });
  }
}

async function captureSourcePath(file) {
  const requested = path.resolve(file);
  const requestedParent = path.dirname(requested);
  await sourceDirectoryChain(requestedParent, true);
  const parent = await fs.realpath(requestedParent);
  const chain = await sourceDirectoryChain(parent);
  const canonical = path.join(parent, path.basename(requested));
  const check = async () => {
    await sourceDirectoryChain(requestedParent, true);
    if (await fs.realpath(requestedParent) !== parent) throw sourcePathChanged();
    const current = await sourceDirectoryChain(parent);
    if (current.length !== chain.length || current.some((entry, index) =>
      entry.path !== chain[index].path || entry.device !== chain[index].device || entry.inode !== chain[index].inode)) throw sourcePathChanged();
  };
  await check();
  const stat = await fs.lstat(canonical);
  if (stat.isSymbolicLink()) throw new SkillError('SYMLINK_NOT_ALLOWED', '不允许符号链接作为本地源');
  if (!stat.isFile() && !stat.isDirectory()) throw new SkillError('INVALID_LOCAL_PATH', '本地源必须是普通文件或目录');
  await check();
  return { path: canonical, stat, check };
}

/** Caller owns close(); reader is borrowed by the SDK and exposes no mutable path. */
export async function openSourceFile(file, expected) {
  let captured;
  let handle;
  try {
    captured = await inspectSourcePath(file);
    const initial = captured.stat;
    if (!initial.isFile()) throw new SkillError('INVALID_LOCAL_PATH', '本地源必须是普通文件');
    if (expected && (!sameIdentity(initial, expected) ||
        initial.size !== expected.size || Math.trunc(initial.mtimeMs) !== expected.mtime_ms)) throw sourcePathChanged();
    handle = await fs.open(captured.path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0));
    const checkStat = (stat) => {
      if (!stat.isFile() || !sameIdentity(stat, sourceIdentity(initial)) || stat.size !== initial.size ||
          stat.mtimeMs !== initial.mtimeMs || stat.ctimeMs !== initial.ctimeMs) throw sourcePathChanged();
      return stat;
    };
    checkStat(await handle.stat());
    await captured.check();
    const reader = {
      async stat() { return checkStat(await handle.stat()); },
      async read(buffer, offset, length, position) {
        if (!Number.isSafeInteger(position) || position < 0) throw sourcePathChanged();
        checkStat(await handle.stat());
        const result = await handle.read(buffer, offset, length, position);
        checkStat(await handle.stat());
        return result;
      },
    };
    return { path: captured.path, reader, size: initial.size, close: () => handle.close() };
  } catch (cause) {
    await handle?.close().catch(() => {});
    if (cause instanceof SkillError) throw cause;
    throw sourcePathChanged(cause);
  }
}

async function sourceDirectoryChain(directory, allowTrustedLinks = false) {
  const chain = [];
  for (let current = directory;; current = path.dirname(current)) {
    const stat = await fs.lstat(current);
    const symbolic = stat.isSymbolicLink();
    if (!stat.isDirectory() && !(allowTrustedLinks && symbolic)) throw sourcePathChanged();
    if (process.platform !== 'win32') {
      if (!(stat.uid === process.geteuid() || stat.uid === 0) ||
          (!symbolic && (stat.mode & 0o022) && !(stat.mode & 0o1000))) {
        throw new SkillError('UNSAFE_SOURCE_DIRECTORY', '源文件父目录可被其他用户修改，请先移到当前用户的私有目录');
      }
    }
    chain.push({ path: current, ...sourceIdentity(stat) });
    if (path.dirname(current) === current) return chain;
  }
}

const identity = (stat) => ({ device: String(stat.dev), inode: String(stat.ino) });
const matches = (stat, expected) => expected.device === String(stat.dev) && expected.inode === String(stat.ino);

export async function captureDownloadRoot(directory) {
  await directoryChain(path.resolve(directory), true);
  const stat = await fs.lstat(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw changed();
  const realPath = await fs.realpath(directory);
  if (!matches(await fs.lstat(realPath), identity(stat))) throw changed();
  await directoryChain(realPath);
  return { real_path: realPath, ...identity(stat) };
}

export async function guardDownloadPath(plan, output, expectedDirectories = []) {
  assertDownloadRoot(plan.download_root);
  const root = await captureDownloadRoot(plan.local_dir);
  if (
    root.real_path !== plan.download_root.real_path ||
    root.device !== plan.download_root.device || root.inode !== plan.download_root.inode
  ) throw changed();
  const relative = path.relative(plan.local_dir, path.dirname(output));
  if (isOutsideRelativePath(relative)) throw changed();
  const parent = path.join(root.real_path, relative);
  const chain = await directoryChain(parent);
  for (const expected of expectedDirectories) {
    const expectedPath = path.join(root.real_path, ...expected.relative_path.split('/'));
    const actual = chain.find((entry) => entry.path === expectedPath);
    if (!actual || actual.device !== expected.device || actual.inode !== expected.inode) throw changed();
  }
  const check = async () => {
    await directoryChain(path.resolve(plan.local_dir), true);
    if (await fs.realpath(plan.local_dir) !== root.real_path) throw changed();
    const current = await directoryChain(parent);
    if (current.length !== chain.length || current.some((entry, i) => (
      entry.path !== chain[i].path || entry.device !== chain[i].device || entry.inode !== chain[i].inode
    ))) throw changed();
    const rootStat = await fs.lstat(plan.local_dir);
    if (rootStat.isSymbolicLink() || !matches(rootStat, root)) throw changed();
  };
  await check();
  return {
    // Actual filesystem operations must never traverse the caller's aliases
    // again. Their intermediate symlink targets may be mutable by other users.
    output: path.join(parent, path.basename(output)),
    check,
    async run(action) {
      await check();
      const result = await action();
      await check();
      return result;
    },
  };
}

export function assertDownloadRoot(root) {
  if (!root || typeof root.real_path !== 'string' || !path.isAbsolute(root.real_path) ||
      typeof root.device !== 'string' || !/^\d+$/.test(root.device) ||
      typeof root.inode !== 'string' || !/^\d+$/.test(root.inode)) {
    throw new SkillError('INVALID_LOCAL_STATE', '下载计划缺少有效的预检目录身份');
  }
}

async function directoryChain(directory, allowTrustedLinks = false) {
  const chain = [];
  let current = directory;
  for (;;) {
    const stat = await fs.lstat(current).catch((error) => {
      if (['ENOENT', 'ENOTDIR', 'ELOOP'].includes(error.code)) throw changed();
      throw error;
    });
    const symbolic = stat.isSymbolicLink();
    if (!stat.isDirectory() && !(allowTrustedLinks && symbolic)) throw changed();
    // Without directory-relative no-follow APIs, other OS users must not be
    // able to rename an ancestor between validation and a filesystem operation.
    // Owners can change permissions and rename children even with mode 0755
    // or 0555. Every directory in the chain must belong to us or root.
    if (process.platform !== 'win32') {
      const ownDirectory = stat.uid === process.geteuid() || stat.uid === 0;
      if (!ownDirectory || (!symbolic && (stat.mode & 0o022) && !(stat.mode & 0o1000))) {
        throw new SkillError('UNSAFE_DOWNLOAD_DIRECTORY', '下载目录的父目录可被其他用户修改，请选择当前用户的私有目录');
      }
    }
    chain.push({ path: current, ...identity(stat) });
    const parent = path.dirname(current);
    if (parent === current) return chain;
    current = parent;
  }
}

function changed() {
  return new SkillError('LOCAL_DIRECTORY_CHANGED', '下载目录或父目录已被替换或含符号链接，已停止写入');
}

export function downloadTemporaryPath(output, batchId, subitemId) {
  const ownerId = createHash('sha256')
    .update(`${batchId}\0${subitemId}`)
    .digest('hex')
    .slice(0, 32);
  return `${output}.guangyapan-${ownerId}.part`;
}

export function ownedDownloadTemporaryPath(checkpoint, output, batchId, subitemId) {
  const candidate = checkpoint?.temporary_path;
  if (typeof candidate !== 'string' || !path.isAbsolute(candidate)) return undefined;
  const resolved = path.resolve(candidate);
  const current = path.resolve(downloadTemporaryPath(output, batchId, subitemId));
  return resolved === current ? resolved : undefined;
}

// Only for disposable artifacts. Credential revocation and active lock release
// must still report failures, since their removal changes application state.
export async function removeDisposableFile(file) {
  await fs.unlink(file).catch(() => {});
}

export async function removeEmptyDirectory(directory) {
  await fs.rmdir(directory).catch(() => {});
}

export async function preflightRequest(request, config, temporaryDir, { beforeDirectoryScan } = {}) {
  const localPlan = [];
  const summaries = [];
  await fs.mkdir(temporaryDir, { recursive: true, mode: 0o700 });
  for (const operation of request.operations) {
    if (operation.type === 'upload') {
      const result = await preflightUpload(operation, config, temporaryDir, beforeDirectoryScan);
      localPlan.push(result.plan);
      summaries.push(result.summary);
    } else if (operation.type === 'download') {
      const plan = await preflightDownload(operation);
      localPlan.push(plan);
      summaries.push({ operation_id: operation.id, local_dir_ready: true });
    }
  }
  return { localPlan, summaries };
}

async function preflightUpload(operation, config, temporaryDir, beforeDirectoryScan) {
  if (!path.isAbsolute(operation.local_path)) {
    throw new SkillError('INVALID_LOCAL_PATH', '上传路径必须是绝对路径');
  }
  const source = await inspectSourcePath(operation.local_path);
  const stat = source.stat;
  if (stat.isFile()) {
    const item = manifestItem(operation.local_path, path.basename(operation.local_path), stat, 0);
    return {
      plan: {
        operation_id: operation.id,
        type: 'upload',
        root: operation.local_path,
        remote_parent_id: operation.remote_parent_id,
        items: [item],
      },
      summary: uploadSummary(
        operation.id,
        path.basename(operation.local_path),
        [item],
        hashLines([publicManifestItem(item)]),
      ),
    };
  }
  if (!stat.isDirectory()) {
    throw new SkillError('INVALID_LOCAL_PATH', '上传源必须是普通文件或目录');
  }

  await beforeDirectoryScan?.(operation);
  const manifestPath = path.join(temporaryDir, `${operation.id}.manifest.ndjson`);
  const handle = await fs.open(manifestPath, 'wx', 0o600);
  const hash = createHash('sha256');
  let entries = 0;
  let transferItems = 0;
  let bytes = 0;
  let totalBytes = 0;
  let maxDepth = 0;
  try {
    const stack = [{ absolute: source.path, relative: '', depth: 0 }];
    while (stack.length) {
      const current = stack.pop();
      const checkedDirectory = await inspectSourcePath(current.absolute);
      if (!checkedDirectory.stat.isDirectory()) throw manifestMismatch();
      const directory = await fs.opendir(current.absolute);
      for await (const entry of directory) {
        const absolute = path.join(current.absolute, entry.name);
        const relative = path.join(current.relative, entry.name);
        const itemStat = (await inspectSourcePath(absolute)).stat;
        const depth = current.depth + 1;
        maxDepth = Math.max(maxDepth, depth);
        enforceLimit('max_directory_depth', config.maxDirectoryDepth, maxDepth);
        const item = manifestItem(absolute, relative, itemStat, depth);
        const line = `${JSON.stringify(item)}\n`;
        entries += 1;
        bytes += Buffer.byteLength(line);
        if (item.type === 'file') {
          transferItems += 1;
          totalBytes += item.size;
        }
        enforceLimit('max_directory_entries', config.maxDirectoryEntries, entries);
        enforceLimit('max_manifest_bytes', config.maxManifestBytes, bytes);
        await handle.write(line);
        hash.update(line);
        if (item.type === 'directory') {
          stack.push({ absolute, relative, depth });
        }
      }
      await checkedDirectory.check();
    }
    await source.check();
    const rootNow = await fs.lstat(source.path);
    if (String(rootNow.dev) !== String(stat.dev) || String(rootNow.ino) !== String(stat.ino)) throw manifestMismatch();
    await handle.sync();
  } catch (error) {
    await handle.close();
    await fs.rm(manifestPath, { force: true });
    throw error;
  }
  await handle.close();
  const manifestSha256 = hash.digest('hex');
  return {
    plan: {
      operation_id: operation.id,
      type: 'upload_directory',
      root: operation.local_path,
      root_device: String(stat.dev),
      root_inode: String(stat.ino),
      remote_parent_id: operation.remote_parent_id,
      manifest_path: manifestPath,
      manifest_sha256: manifestSha256,
      manifest_bytes: bytes,
      manifest_entries: entries,
      manifest_transfer_items: transferItems,
      manifest_total_bytes: totalBytes,
      manifest_max_depth: maxDepth,
    },
    summary: {
      operation_id: operation.id,
      root_name: path.basename(operation.local_path),
      entries,
      transfer_items: transferItems,
      total_bytes: totalBytes,
      max_depth: maxDepth,
      manifest_bytes: bytes,
      manifest_sha256: manifestSha256,
    },
  };
}

export async function verifyUploadManifests(localPlan, config) {
  const snapshots = new Map();
  for (const plan of localPlan || []) {
    if (plan?.type === 'upload_directory') {
      snapshots.set(plan.operation_id, await verifyUploadManifest(plan, config));
    }
  }
  return snapshots;
}

export async function verifyUploadManifest(plan, config = {}) {
  if (!path.isAbsolute(plan.root || '') || !path.isAbsolute(plan.manifest_path || '')) {
    throw manifestMismatch();
  }
  const rootStat = await fs.lstat(plan.root).catch(() => undefined);
  if (
    !rootStat?.isDirectory() || rootStat.isSymbolicLink() ||
    String(rootStat.dev) !== plan.root_device || String(rootStat.ino) !== plan.root_inode
  ) {
    throw manifestMismatch();
  }
  const stat = await fs.lstat(plan.manifest_path).catch(() => undefined);
  if (!stat?.isFile() || stat.isSymbolicLink() || stat.size !== plan.manifest_bytes) {
    throw manifestMismatch();
  }
  const hash = createHash('sha256');
  const items = [];
  const paths = new Set();
  const totals = { entries: 0, transferItems: 0, bytes: 0, totalBytes: 0, maxDepth: 0 };
  const lines = readline.createInterface({
    input: createReadStream(plan.manifest_path),
    crlfDelay: Infinity,
  });
  try {
    for await (const line of lines) {
      if (!line) throw manifestMismatch();
      const encoded = `${line}\n`;
      totals.bytes += Buffer.byteLength(encoded);
      totals.entries += 1;
      hash.update(encoded);
      const item = parseManifestItem(line, plan.root);
      if (paths.has(item.relative_path)) throw manifestMismatch();
      paths.add(item.relative_path);
      items.push(Object.freeze(item));
      totals.maxDepth = Math.max(totals.maxDepth, item.depth);
      if (item.type === 'file') {
        totals.transferItems += 1;
        totals.totalBytes += item.size;
      }
      enforceManifestBound(config.maxDirectoryEntries, totals.entries);
      enforceManifestBound(config.maxManifestBytes, totals.bytes);
      enforceManifestBound(config.maxDirectoryDepth, totals.maxDepth);
    }
  } catch (cause) {
    if (cause?.code === 'MANIFEST_MISMATCH') throw cause;
    throw manifestMismatch(cause);
  }
  if (
    hash.digest('hex') !== plan.manifest_sha256 ||
    totals.bytes !== plan.manifest_bytes ||
    totals.entries !== plan.manifest_entries ||
    totals.transferItems !== plan.manifest_transfer_items ||
    totals.totalBytes !== plan.manifest_total_bytes ||
    totals.maxDepth !== plan.manifest_max_depth
  ) {
    throw manifestMismatch();
  }
  return Object.freeze(items);
}

function parseManifestItem(line, root) {
  let item;
  try {
    item = JSON.parse(line);
  } catch (cause) {
    throw manifestMismatch(cause);
  }
  if (!item || typeof item !== 'object' || Array.isArray(item)) throw manifestMismatch();
  if (!['file', 'directory'].includes(item.type)) throw manifestMismatch();
  if (!Number.isSafeInteger(item.size) || item.size < 0) throw manifestMismatch();
  if (typeof item.device !== 'string' || typeof item.inode !== 'string' ||
      !/^\d+$/.test(item.device) || !/^\d+$/.test(item.inode)) throw manifestMismatch();
  if (!Number.isSafeInteger(item.mtime_ms) || !Number.isSafeInteger(item.depth) || item.depth < 1) {
    throw manifestMismatch();
  }
  const segments = typeof item.relative_path === 'string' ? item.relative_path.split('/') : [];
  if (
    segments.length !== item.depth ||
    segments.some((segment) => !segment || segment === '.' || segment === '..' || /[\\\0]/.test(segment))
  ) {
    throw manifestMismatch();
  }
  const candidate = path.join(root, ...segments);
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  if (isOutsideRelativePath(relative)) throw manifestMismatch();
  return item;
}

function enforceManifestBound(limit, observed) {
  if (Number.isSafeInteger(limit) && limit > 0 && observed > limit) throw manifestMismatch();
}

function manifestMismatch(cause) {
  return new SkillError('MANIFEST_MISMATCH', '目录上传清单与预检时不一致', {
    retryable: true,
    cause,
  });
}

async function preflightDownload(operation) {
  if (!path.isAbsolute(operation.local_dir)) {
    throw new SkillError('INVALID_LOCAL_PATH', '下载目录必须是绝对路径');
  }
  try {
    await fs.mkdir(operation.local_dir, { recursive: true, mode: 0o700 });
  } catch (cause) {
    if (cause?.code !== 'EEXIST') {
      throw new SkillError('LOCAL_PATH_NOT_FOUND', '下载目录无法创建或访问', { cause });
    }
  }
  const stat = await lstatRegular(operation.local_dir);
  if (!stat.isDirectory()) {
    throw new SkillError('INVALID_LOCAL_PATH', '下载目标必须是目录');
  }
  await fs.access(operation.local_dir, fs.constants.W_OK);
  return {
    operation_id: operation.id,
    type: 'download',
    local_dir: operation.local_dir,
    download_root: await captureDownloadRoot(operation.local_dir),
    remote_id: operation.remote_id,
  };
}

async function lstatRegular(value) {
  let stat;
  try {
    stat = await fs.lstat(value);
  } catch (cause) {
    throw new SkillError('LOCAL_PATH_NOT_FOUND', '本地路径不存在或不可访问', { cause });
  }
  if (stat.isSymbolicLink()) {
    throw new SkillError('SYMLINK_NOT_ALLOWED', 'V1 不允许符号链接');
  }
  if (!stat.isFile() && !stat.isDirectory()) {
    throw new SkillError('INVALID_LOCAL_PATH', 'V1 只允许普通文件或目录');
  }
  return stat;
}

function manifestItem(absolute, relative, stat, depth) {
  return {
    relative_path: relative.split(path.sep).join('/'),
    type: stat.isDirectory() ? 'directory' : 'file',
    size: stat.isFile() ? stat.size : 0,
    mtime_ms: Math.trunc(stat.mtimeMs),
    ...sourceIdentity(stat),
    depth,
  };
}

function uploadSummary(operationId, rootName, items, digest) {
  return {
    operation_id: operationId,
    root_name: rootName,
    entries: items.length,
    transfer_items: items.filter((item) => item.type === 'file').length,
    total_bytes: items.reduce((sum, item) => sum + item.size, 0),
    max_depth: Math.max(...items.map((item) => item.depth)),
    manifest_sha256: digest,
    manifest_inline: items.map(publicManifestItem),
  };
}

function publicManifestItem(item) {
  return {
    relative_path: item.relative_path,
    type: item.type,
    size: item.size,
    mtime_ms: item.mtime_ms,
    depth: item.depth,
  };
}

function hashLines(items) {
  const hash = createHash('sha256');
  items.forEach((item) => hash.update(`${JSON.stringify(item)}\n`));
  return hash.digest('hex');
}

function enforceLimit(name, limit, observed) {
  if (observed > limit) {
    throw new SkillError('DIRECTORY_LIMIT_EXCEEDED', '目录任务超过配置限制', {
      data: { directory_limit: { name, limit, observed } },
    });
  }
}

const DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const DETAIL_FIELDS = [
  'local_path', 'local_dir', 'torrent_path', 'new_name', 'name', 'title',
  'validity_seconds', 'share_type', 'download_type',
  'share_url', 'url',
  'task_ids', 'delete_files',
];

export function planDigest(batch) {
  const payload = {
    version: 1,
    request_id: batch.request_id,
    request_hash: batch.request_hash,
    batch_id: batch.batch_id,
    account_id: batch.account_id,
    account_generation: batch.account_generation,
    plan_summary: batch.plan_summary,
    operation_details: batch.operation_details,
    operations: batch.operations,
    local_plan: batch.local_plan,
  };
  return createHash('sha256').update(JSON.stringify(payload)).digest('hex');
}

export function assertPlanIntegrity(batch, expectedDigest) {
  if (!DIGEST_PATTERN.test(expectedDigest || '')) {
    throw new SkillError('PLAN_INTEGRITY_REQUIRED', '后台任务缺少可信执行计划摘要', { retryable: true });
  }
  if (planDigest(batch) !== expectedDigest) {
    integrityFailure('digest_mismatch');
  }
  assertPlanSemantics(batch);
}

function assertPlanSemantics(batch) {
  if (!Number.isSafeInteger(batch.account_generation) || batch.account_generation < 0) {
    throw new SkillError('INVALID_LOCAL_STATE', '本地批次缺少有效的账号任务代次');
  }
  const operations = batch.operations;
  const details = batch.operation_details;
  const items = batch.items;
  const localPlan = batch.local_plan;
  if (![operations, details, items, localPlan].every(Array.isArray)) {
    integrityFailure('invalid_plan_shape');
  }
  if (!operations.length || details.length !== operations.length || items.length !== operations.length) {
    integrityFailure('operation_count_mismatch');
  }

  const operationIds = uniqueMap(operations, 'id', 'duplicate_operation');
  const detailById = uniqueMap(details, 'operation_id', 'duplicate_detail');
  const itemById = uniqueMap(items, 'operation_id', 'duplicate_item');
  if (operationIds.size !== detailById.size || operationIds.size !== itemById.size) {
    integrityFailure('operation_set_mismatch');
  }

  for (const operation of operations) {
    const detail = detailById.get(operation.id);
    if (!detail || !itemById.has(operation.id) || detail.type !== operation.type) {
      integrityFailure('operation_identity_mismatch');
    }
    for (const field of DETAIL_FIELDS) {
      if (operation[field] !== undefined && !sameValue(detail[field], operation[field])) {
        integrityFailure('detail_field_mismatch');
      }
    }
    if (
      operation.type === 'create_share' &&
      detail.extraction_code_set !== Boolean(operation.code)
    ) {
      integrityFailure('share_detail_mismatch');
    }
    if (operation.remote_id !== undefined && detail.source?.id !== operation.remote_id) {
      integrityFailure('source_mismatch');
    }
    if (operation.remote_ids !== undefined) {
      const displayedIds = detail.sources?.map((source) => source?.id);
      if (!sameValue(displayedIds, operation.remote_ids)) integrityFailure('sources_mismatch');
    }
    const targetId = operation.target_parent_id ?? operation.remote_parent_id;
    if (targetId !== undefined && detail.target?.id !== targetId) {
      integrityFailure('target_mismatch');
    }
    if (operation.type === 'cloud_collection_add') assertCloudSelection(operation, detail);
  }

  const localOperations = operations.filter(({ type }) => type === 'upload' || type === 'download');
  if (localPlan.length !== localOperations.length) integrityFailure('local_plan_count_mismatch');
  const localPlanById = uniqueMap(localPlan, 'operation_id', 'duplicate_local_plan');
  for (const operation of localOperations) {
    const plan = localPlanById.get(operation.id);
    if (!plan) integrityFailure('local_plan_missing');
    if (operation.type === 'upload') {
      if (
        !['upload', 'upload_directory'].includes(plan.type) ||
        plan.root !== operation.local_path ||
        plan.remote_parent_id !== operation.remote_parent_id
      ) {
        integrityFailure('upload_plan_mismatch');
      }
      if (plan.type === 'upload_directory' && (
        typeof plan.root_device !== 'string' || !plan.root_device ||
        typeof plan.root_inode !== 'string' || !plan.root_inode ||
        !DIGEST_PATTERN.test(plan.manifest_sha256 || '') ||
        !isNonNegativeInteger(plan.manifest_bytes) ||
        !isNonNegativeInteger(plan.manifest_entries) ||
        !isNonNegativeInteger(plan.manifest_transfer_items) ||
        !isNonNegativeInteger(plan.manifest_total_bytes) ||
        !isNonNegativeInteger(plan.manifest_max_depth)
      )) integrityFailure('upload_manifest_metadata_missing');
      const item = itemById.get(operation.id);
      const plannedBytes = plan.type === 'upload_directory'
        ? plan.manifest_total_bytes
        : plan.items?.reduce((sum, entry) => sum + Number(entry.size || 0), 0);
      const plannedItems = plan.type === 'upload_directory'
        ? plan.manifest_transfer_items
        : plan.items?.filter((entry) => entry.type === 'file').length;
      if (item.planned_bytes !== plannedBytes || item.planned_items !== plannedItems) {
        integrityFailure('upload_summary_mismatch');
      }
    } else if (
      plan.type !== 'download' ||
      plan.local_dir !== operation.local_dir ||
      plan.remote_id !== operation.remote_id
    ) {
      integrityFailure('download_plan_mismatch');
    }
    if (operation.type === 'download') assertDownloadRoot(plan.download_root);
  }

  const summary = batch.plan_summary;
  if (!summary || summary.operation_count !== operations.length) {
    integrityFailure('summary_count_mismatch');
  }
  // Download sizes are discovered by the worker after the plan is accepted and
  // are therefore mutable runtime facts, not part of the preflight summary.
  const dynamicDownloadIds = new Set(
    operations.filter((operation) => operation.type === 'download').map((operation) => operation.id),
  );
  const totals = items.reduce((result, item) => {
    if (dynamicDownloadIds.has(item.operation_id)) return result;
    return {
      bytes: result.bytes + Number(item.planned_bytes || 0),
      items: result.items + Number(item.planned_items || 0),
    };
  }, { bytes: 0, items: 0 });
  if (!dynamicDownloadIds.size && (
    !sameValue(summary.total_bytes, totals.bytes || undefined) ||
    !sameValue(summary.total_items, totals.items || undefined)
  )) {
    integrityFailure('summary_totals_mismatch');
  }
}

function assertCloudSelection(operation, detail) {
  const expectedIndexes = operation.file_indexes || [];
  const expectedCount = operation.file_indexes === undefined ? 1 : expectedIndexes.length;
  if (
    !detail.cloud_resource ||
    detail.cloud_resource.selected_files !== expectedCount ||
    detail.cloud_resource.normalized_url !== operation.resolved_url ||
    detail.cloud_resource.resource_type !== operation.res_type
  ) {
    integrityFailure('cloud_selection_mismatch');
  }
  if (operation.file_indexes === undefined) return;
  const selection = detail.cloud_resource.file_selection;
  if (
    !selection ||
    selection.selected_count !== expectedCount ||
    !Array.isArray(selection.files) ||
    !Array.isArray(selection.selected_indexes) ||
    selection.selected_indexes.length !== expectedCount ||
    selection.total_count !== selection.files.length ||
    new Set(selection.selected_indexes).size !== selection.selected_indexes.length ||
    new Set(selection.files.map((file) => file?.file_index)).size !== selection.files.length
  ) {
    integrityFailure('cloud_selection_mismatch');
  }
  const displayedIndexes = [...selection.selected_indexes].sort((left, right) => left - right);
  const availableIndexes = new Set(selection.files.map((file) => file?.file_index));
  if (
    !sameValue(displayedIndexes, [...expectedIndexes].sort((left, right) => left - right)) ||
    expectedIndexes.some((index) => !availableIndexes.has(index))
  ) {
    integrityFailure('cloud_selection_mismatch');
  }
}

function isNonNegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function uniqueMap(values, key, reason) {
  const result = new Map();
  for (const value of values) {
    const id = value?.[key];
    if (typeof id !== 'string' || !id || result.has(id)) integrityFailure(reason);
    result.set(id, value);
  }
  return result;
}

function sameValue(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function integrityFailure(reason) {
  throw new SkillError('PLAN_INTEGRITY_FAILED', '预检内容与执行计划不一致', {
    retryable: true,
    data: { reason },
  });
}

export async function assertUploadSourceUnchanged(localPath, item, plan) {
  if (plan.type === 'upload_directory') {
    const rootStat = await fs.lstat(plan.root).catch(() => undefined);
    if (
      !rootStat?.isDirectory() || rootStat.isSymbolicLink() ||
      String(rootStat.dev) !== plan.root_device || String(rootStat.ino) !== plan.root_inode
    ) {
      throw new SkillError('LOCAL_SOURCE_CHANGED', '上传目录在预检后发生变化');
    }
    const relative = path.relative(plan.root, localPath);
    let current = plan.root;
    for (const segment of relative.split(path.sep).slice(0, -1)) {
      current = path.join(current, segment);
      const parentStat = await fs.lstat(current).catch(() => undefined);
      if (!parentStat?.isDirectory() || parentStat.isSymbolicLink()) {
        throw new SkillError('LOCAL_SOURCE_CHANGED', '上传目录在预检后发生变化');
      }
    }
  }
  const stat = await fs.lstat(localPath);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size !== item.size || Math.trunc(stat.mtimeMs) !== item.mtime_ms) {
    throw new SkillError('LOCAL_SOURCE_CHANGED', '上传源在预检后发生变化');
  }
}

export function isOutsideRelativePath(relative) {
  return relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative);
}

// Index only the verified worker snapshot; never reopen an untrusted manifest per file.
const uploadManifestIndexes = new WeakMap();

export function resolveUploadWorkItem(plan, requested) {
  if (!requested?.relative_path) {
    throw new SkillError('INVALID_API_RESPONSE', '服务端上传工作缺少 relative_path');
  }
  if (plan.type === 'upload') {
    const item = plan.items[0];
    if (item.relative_path !== requested.relative_path) {
      throw new SkillError('MANIFEST_MISMATCH', '服务端上传项与本地清单不一致');
    }
    return item;
  }
  const items = plan.verified_manifest_items;
  if (!Array.isArray(items)) throw manifestMismatch();
  let index = uploadManifestIndexes.get(items);
  if (!index) {
    index = new Map(items.filter(item => item.type === 'file').map(item => [item.relative_path, item]));
    uploadManifestIndexes.set(items, index);
  }
  const item = index.get(requested.relative_path);
  if (!item || Number(requested.size) !== item.size || Number(requested.mtime_ms) !== item.mtime_ms) {
    throw manifestMismatch();
  }
  return item;
}

export async function readOptionalJson(file) {
  try { return JSON.parse(await fs.readFile(file, 'utf8')); }
  catch (error) { if (error?.code === 'ENOENT') return undefined; throw error; }
}

export function assertRemoteName(value, platform = process.platform) {
  if (!value || value === '.' || value === '..' || /[\\/\0]/.test(value)) {
    throw new SkillError('INVALID_REMOTE_NAME', '远端对象名称非法');
  }
  if (platform === 'win32' && (
    /[<>:"|?*\x00-\x1f]/.test(value) ||
    /[. ]$/.test(value) ||
    /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/i.test(value)
  )) {
    throw new SkillError('INVALID_REMOTE_NAME', `远端对象名称无法写入 Windows: ${value}`);
  }
}

export function transferItemId(relative) {
  return createHash('sha256').update(relative).digest('hex').slice(0, 32);
}

export async function removePlannedDownloadPart(batchId, plan, checkpointName, checkpointPath) {
  if (typeof plan.local_dir !== 'string' || !path.isAbsolute(plan.local_dir)) return;
  let checkpoint;
  try {
    checkpoint = JSON.parse(await fs.readFile(checkpointPath, 'utf8'));
  } catch {
    return;
  }
  const relative = checkpoint?.relative_path;
  if (typeof relative !== 'string') return;
  const segments = relative.split('/');
  if (!segments.length || segments.some((segment) =>
    !segment || segment === '.' || segment === '..' || /[\\\0]/.test(segment))) return;
  const subitemId = transferItemId(relative);
  if (checkpointName !== `${plan.operation_id}.${subitemId}.json`) return;
  const identity = checkpoint.temporary_identity;
  // A matching name alone does not prove that a file still belongs to this
  // checkpoint. Deletion requires the recorded file identity.
  if (typeof identity?.device !== 'string' || typeof identity?.inode !== 'string') return;
  try {
    const directory = await guardDownloadPath(plan, path.join(plan.local_dir, ...segments));
    const temporary = ownedDownloadTemporaryPath(checkpoint, directory.output, batchId, subitemId);
    if (!temporary) return;
    const within = path.relative(plan.download_root.real_path, temporary);
    if (isOutsideRelativePath(within)) return;
    await directory.run(async () => {
      const stat = await fs.lstat(temporary).catch(() => undefined);
      if (stat?.isFile() && !stat.isSymbolicLink() &&
        String(stat.dev) === identity.device && String(stat.ino) === identity.inode) {
        await removeDisposableFile(temporary);
      }
    });
  } catch {
    // A replaced ancestor must never redirect cleanup into another directory.
  }
}

export async function syncDirectory(directory) {
  if (process.platform === 'win32') return;
  const handle = await fs.open(directory, 'r');
  try {
    await handle.sync();
  } finally {
    await handle.close();
  }
}
