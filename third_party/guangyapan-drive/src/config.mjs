import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { SkillError } from './protocol.mjs';

const { version } = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
export const CLIENT_VERSION = `guangyapan-drive/${version}`;
export const AGENT_PLATFORM_VERSION = '13';

const FALLBACK_NAME = 'Agent';
// Compatibility tokens let the account UA parser read the agent name via OS().
const BROWSER_COMPATIBILITY = 'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';
export function resolveAgentName(value) {
  if (typeof value !== 'string') return FALLBACK_NAME;
  const name = value.trim();
  // Reject control characters and unpaired surrogates before using a name in headers or env.
  if (!name || name.length > 80 || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(name)) return FALLBACK_NAME;
  return name.replace(/\s+/gu, '-');
}

export function agentRequestHeaders(agentName) {
  const name = resolveAgentName(agentName);
  const encodedName = encodeURIComponent(name);
  // Escape comment delimiters so a custom name cannot change the UA structure.
  const uaName = encodedName.replace(/[()]/g, (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`);
  return {
    // Fetch headers must be ByteStrings. Account device headers use URI encoding too.
    'User-Agent': `Mozilla/5.0 (X11; ${uaName}) ${BROWSER_COMPATIBILITY} ${CLIENT_VERSION}`,
    'x-device-name': encodedName,
    'x-device-model': encodedName,
    'x-platform-version': AGENT_PLATFORM_VERSION,
  };
}

export function absoluteIdentityPath(value, { platform = process.platform } = {}) {
  const paths = platform === 'win32' ? path.win32 : path;
  if (typeof value !== 'string' || !paths.isAbsolute(value) || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(value)) {
    throw new SkillError('INVALID_AGENT_INSTANCE', 'Agent 配置目录和状态目录必须使用绝对路径');
  }
  const normalized = paths.normalize(value);
  const canonical = normalized === paths.parse(normalized).root
    ? normalized : normalized.replace(platform === 'win32' ? /[\\/]+$/ : /\/+$/, '');
  return platform === 'win32' ? canonical.toLowerCase() : canonical;
}

export function resolveAgentScope({
  environment = process.env,
  platform = process.platform,
  stateRoot,
  namespace,
  agentName,
  agentInstanceId,
  agentProfileDir,
  inheritWorkerContext = false,
} = {}) {
  // This context is created by the parent launcher, never selected by public calls.
  if (inheritWorkerContext) {
    let context;
    try { context = JSON.parse(environment.GUANGYAPAN_DRIVE_WORKER_CONTEXT); } catch {}
    if (!context || typeof context.name !== 'string' || context.name !== resolveAgentName(context.name) ||
        typeof context.scopeId !== 'string' || !/^[a-f0-9]{64}$/.test(context.scopeId)) {
      throw new SkillError('INVALID_AGENT_INSTANCE', '后台 Agent 实例标识非法');
    }
    return { name: context.name, scopeId: context.scopeId };
  }
  const name = resolveAgentName(agentName);
  if (agentProfileDir !== undefined && agentInstanceId !== undefined) {
    throw new SkillError('INVALID_AGENT_INSTANCE', '--agent-profile-dir 和 --agent-instance-id 只需提供其中一个');
  }
  let instance;
  if (agentProfileDir !== undefined) {
    instance = ['profile', absoluteIdentityPath(agentProfileDir, { platform })];
  } else if (agentInstanceId !== undefined) {
    const id = agentInstanceId;
    if (typeof id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[47][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id)) {
      throw new SkillError('INVALID_AGENT_INSTANCE', '--agent-instance-id 必须是持久保存的 UUIDv4 或 UUIDv7');
    }
    instance = ['instance', id.toLowerCase()];
  } else {
    throw new SkillError('AGENT_INSTANCE_REQUIRED', '请通过当前 Agent 的专属调用配置传入 --agent-profile-dir（绝对路径）或固定的 --agent-instance-id');
  }
  const scopeId = createHash('sha256').update(JSON.stringify([
    'agent-scope-v2', namespace, name.toLowerCase(), instance, absoluteIdentityPath(stateRoot, { platform }),
  ])).digest('hex');
  return { name, scopeId };
}

export function scopedCredentialFile(file, scopeId, { platform = process.platform } = {}) {
  const paths = platform === 'win32' ? path.win32 : path;
  absoluteIdentityPath(file, { platform });
  return paths.join(paths.dirname(file), 'agents', scopeId, paths.basename(file));
}

export function agentWorkerEnvironment(config) {
  return {
    GUANGYAPAN_DRIVE_WORKER_CONTEXT: JSON.stringify({ name: config.agentName, scopeId: config.agentScopeId }),
    GUANGYAPAN_DRIVE_STATE_ROOT: config.stateRoot,
  };
}

const IDENTITY_OPTIONS = new Map([
  ['--agent-name', 'agentName'],
  ['--agent-instance-id', 'agentInstanceId'],
  ['--agent-profile-dir', 'agentProfileDir'],
]);

export function parseAgentOptions(args) {
  const context = {};
  const commandArgs = [];
  for (let index = 0; index < args.length; index++) {
    const option = args[index];
    const field = IDENTITY_OPTIONS.get(option);
    if (!field) {
      commandArgs.push(option);
      continue;
    }
    if (Object.hasOwn(context, field)) throw new SkillError('INVALID_REQUEST', `${option} 重复`);
    const value = args[++index];
    if (value === undefined || value.startsWith('--')) {
      throw new SkillError('INVALID_REQUEST', `${option} 缺少值`);
    }
    context[field] = value;
  }
  return { args: commandArgs, context };
}

function positiveInt(environment, name, fallback) {
  const raw = environment[name];
  if (raw === undefined) return fallback;
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`${name} 必须是正整数`);
  }
  return value;
}

export function assertSupportedPlatform(platform = process.platform) {
  if (!['darwin', 'win32', 'linux'].includes(platform)) {
    throw new SkillError('UNSUPPORTED_PLATFORM', '支持 macOS、Windows 10/11 和 Linux');
  }
}

export function defaultStateRoot({
  platform = process.platform,
  environment = process.env,
  homeDirectory = os.homedir(),
} = {}) {
  assertSupportedPlatform(platform);
  if (platform === 'win32' && environment.LOCALAPPDATA) {
    return path.win32.join(environment.LOCALAPPDATA, 'GuangyapanDrive');
  }
  const paths = platform === 'win32' ? path.win32 : path;
  return paths.join(homeDirectory, '.guangyapan-drive');
}

export function loadConfig({
  platform = process.platform,
  environment = process.env,
  homeDirectory = os.homedir(),
  agentName,
  agentInstanceId,
  agentProfileDir,
  inheritWorkerContext = false,
} = {}) {
  assertSupportedPlatform(platform);
  const userRoot = environment.GUANGYAPAN_DRIVE_STATE_ROOT || defaultStateRoot({
    platform,
    environment,
    homeDirectory,
  });
  const paths = platform === 'win32' ? path.win32 : path;
  absoluteIdentityPath(userRoot, { platform });
  const clientId = environment.GUANGYAPAN_DRIVE_CLIENT_ID || 'atcFkeidp8ndm9yC';
  const apiBaseUrl = environment.GUANGYAPAN_DRIVE_API_BASE_URL || 'https://api.guangyapan.com';
  const authBaseUrl = environment.GUANGYAPAN_DRIVE_AUTH_BASE_URL || 'https://account.guangyapan.com';
  const identity = resolveAgentScope({
    environment, platform, stateRoot: userRoot,
    agentName, agentInstanceId, agentProfileDir, inheritWorkerContext,
    namespace: ['com.guangyapan.drive', clientId, apiBaseUrl.replace(/\/+$/, ''), authBaseUrl.replace(/\/+$/, '')],
  });
  const agentStateDir = paths.join(userRoot, 'agents', identity.scopeId);
  const accountStateDir = paths.join(agentStateDir, 'account');
  const loginMode = environment.GUANGYAPAN_DRIVE_LOGIN_MODE ||
    (platform === 'linux' && !environment.DISPLAY && !environment.WAYLAND_DISPLAY ? 'device' : 'browser');
  if (!['browser', 'device'].includes(loginMode)) {
    throw new SkillError('INVALID_CONFIG', 'GUANGYAPAN_DRIVE_LOGIN_MODE 必须为 browser 或 device');
  }
  return {
    platform,
    loginMode,
    clientId,
    apiBaseUrl,
    authBaseUrl,
    loginProjectId:
      environment.GUANGYAPAN_DRIVE_LOGIN_PROJECT_ID || '356jld6jjlbjvygi5v9',
    agentName: identity.name,
    agentScopeId: identity.scopeId,
    stateRoot: userRoot,
    agentStateDir,
    accountStateDir,
    credentialFile: environment.GUANGYAPAN_DRIVE_CREDENTIAL_FILE
      ? scopedCredentialFile(environment.GUANGYAPAN_DRIVE_CREDENTIAL_FILE, identity.scopeId, { platform })
      : paths.join(accountStateDir, 'credentials.json'),
    dpapiFile: environment.GUANGYAPAN_DRIVE_DPAPI_FILE
      ? scopedCredentialFile(environment.GUANGYAPAN_DRIVE_DPAPI_FILE, identity.scopeId, { platform })
      : paths.join(accountStateDir, 'credentials.dpapi.json'),
    credentialHelper:
      environment.GUANGYAPAN_DRIVE_CREDENTIAL_HELPER ||
      fileURLToPath(new URL('../bin/guangyapan-credential-helper.mjs', import.meta.url)),
    maxDirectoryDepth: positiveInt(environment, 'GUANGYAPAN_DRIVE_MAX_DIRECTORY_DEPTH', 32),
    maxDirectoryEntries: positiveInt(environment, 'GUANGYAPAN_DRIVE_MAX_DIRECTORY_ENTRIES', 100_000),
    maxManifestBytes: positiveInt(environment, 'GUANGYAPAN_DRIVE_MAX_MANIFEST_BYTES', 64 * 1024 * 1024),
    workerStartupTimeoutMs: positiveInt(environment, 'GUANGYAPAN_DRIVE_WORKER_STARTUP_TIMEOUT_MS', 10_000),
    workerHeartbeatStaleMs: positiveInt(environment, 'GUANGYAPAN_DRIVE_WORKER_HEARTBEAT_STALE_MS', 5_000),
    cloudTaskPollIntervalMs: positiveInt(
      environment,
      'GUANGYAPAN_DRIVE_CLOUD_TASK_POLL_INTERVAL_MS',
      5_000,
    ),
    cloudTaskVisibilityTimeoutMs: positiveInt(
      environment,
      'GUANGYAPAN_DRIVE_CLOUD_TASK_VISIBILITY_TIMEOUT_MS',
      10 * 60 * 1000,
    ),
    cloudTaskNoProgressWarningMs: positiveInt(environment, 'GUANGYAPAN_DRIVE_CLOUD_TASK_NO_PROGRESS_WARNING_MS', 120_000),
    cloudTaskNoProgressTimeoutMs: positiveInt(environment, 'GUANGYAPAN_DRIVE_CLOUD_TASK_NO_PROGRESS_TIMEOUT_MS', 600_000),
    remoteTaskStatusTimeoutMs: positiveInt(
      environment,
      'GUANGYAPAN_DRIVE_REMOTE_TASK_STATUS_TIMEOUT_MS',
      10 * 60 * 1000,
    ),
    apiRequestTimeoutMs: positiveInt(environment, 'GUANGYAPAN_DRIVE_API_REQUEST_TIMEOUT_MS', 30_000),
    maxApiResponseBytes: positiveInt(environment, 'GUANGYAPAN_DRIVE_MAX_API_RESPONSE_BYTES', 32 * 1024 * 1024),
  };
}

const DEFAULT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

export async function assertRuntimeReady(skillRoot = DEFAULT_ROOT) {
  const packageInfo = await readJson(path.join(skillRoot, 'package.json'));
  const runtime = packageInfo.guangyapanRuntime;
  if (runtime?.markerRequired !== true) return;

  let marker;
  try {
    marker = await readJson(path.join(skillRoot, '.runtime-ready'));
  } catch (cause) {
    throw new SkillError(
      'RUNTIME_NOT_READY',
      runtime.edition === 'lite'
        ? '运行依赖未安装，请执行 node scripts/install-runtime.mjs'
        : '内置依赖版运行文件不完整，请重新安装内置依赖版',
      { cause },
    );
  }
  if (marker.version !== packageInfo.version || marker.edition !== runtime.edition) {
    throw new SkillError('RUNTIME_NOT_READY', '运行依赖与当前 Skill 版本不匹配，请重新安装', {
      data: { expected_version: packageInfo.version, edition: runtime.edition },
    });
  }
}

export async function writeRuntimeReady(skillRoot, edition) {
  const packageInfo = await readJson(path.join(skillRoot, 'package.json'));
  await fs.writeFile(
    path.join(skillRoot, '.runtime-ready'),
    `${JSON.stringify({ version: packageInfo.version, edition })}\n`,
    { mode: 0o644 },
  );
}

async function readJson(file) {
  return JSON.parse(await fs.readFile(file, 'utf8'));
}
