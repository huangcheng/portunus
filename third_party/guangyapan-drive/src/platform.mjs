import { execFile, spawn } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { agentWorkerEnvironment, assertSupportedPlatform } from './config.mjs';
import { removeDisposableFile } from './local-files.mjs';
import { SkillError } from './protocol.mjs';

const execFileAsync = promisify(execFile);

// kill(pid, 0) also succeeds for an unreaped zombie. Only a confirmed exit or
// zombie state permits reclaiming a lock; unavailable OS inspection fails closed.
export async function isProcessAlive(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); }
  catch (error) { return error?.code !== 'ESRCH'; }
  if (pid === process.pid || process.platform === 'win32') return true;
  try {
    const { stdout } = await execFileAsync('/bin/ps', ['-p', String(pid), '-o', 'stat='], {
      timeout: 1_000, maxBuffer: 4_096, windowsHide: true,
    });
    const state = stdout.trim().split(/\s+/)[0];
    return !/^[ZX]/.test(state ?? '');
  } catch {
    return true;
  }
}

const CHOOSING_PATTERN = /^choosing-(\d+)-([0-9a-f-]+)$/i;
const CLAIM_PATTERN = /^(\d+)-(\d+)-([0-9a-f-]+)\.claim$/i;
const PROCESS_INSTANCE_ID = crypto.randomUUID();
const PROCESS_MARKER_REFERENCES = new Map();
const PROCESS_MARKER_OPERATIONS = new Map();

export async function acquireProcessLock(file, {
  timeoutMs = 0,
  pollMs = 50,
  busyCode = 'LOCAL_STATE_BUSY',
  busyMessage = '本地状态正被其他进程使用',
} = {}) {
  const deadline = Date.now() + timeoutMs;
  const claimsDir = `${file}.claims`;
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  await retainProcessMarker(claimsDir);

  const token = crypto.randomUUID();
  let choosingPath = path.join(claimsDir, `choosing-${process.pid}-${token}`);
  let claimPath;
  let acquired = false;
  try {
    await publishInstanceFile(choosingPath);
    const claims = await liveClaims(claimsDir);
    const ticket = claims.reduce(
      (highest, claim) => claim.ticket > highest ? claim.ticket : highest,
      0n,
    ) + 1n;
    const name = `${ticket}-${process.pid}-${token}.claim`;
    claimPath = path.join(claimsDir, name);
    await fs.rename(choosingPath, claimPath);
    choosingPath = undefined;

    for (;;) {
      const choosing = await liveChoosing(claimsDir);
      const contenders = await liveClaims(claimsDir);
      contenders.sort(compareClaims);
      if (choosing.length === 0 && contenders[0]?.name === name) {
        acquired = true;
        return new ProcessLock(claimPath, claimsDir);
      }
      if (timeoutMs === 0) return undefined;
      await waitUntil(deadline, pollMs, busyCode, busyMessage);
    }
  } finally {
    if (choosingPath) await releaseParticipation(choosingPath);
    if (!acquired && claimPath) await releaseParticipation(claimPath);
    if (!acquired) await releaseProcessMarker(claimsDir);
  }
}

class ProcessLock {
  constructor(claimPath, claimsDir) {
    this.claimPath = claimPath;
    this.claimsDir = claimsDir;
    this.closed = false;
  }

  async close() {
    if (this.closed) return;
    this.closed = true;
    try {
      await releaseParticipation(this.claimPath);
    } catch (error) {
      this.closed = false;
      throw error;
    }
    await releaseProcessMarker(this.claimsDir);
  }
}

async function liveChoosing(directory) {
  const choosing = [];
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    if (!entry.isFile()) continue;
    const match = CHOOSING_PATTERN.exec(entry.name);
    if (!match) continue;
    const pid = Number(match[1]);
    const instanceId = await readInstanceFile(path.join(directory, entry.name));
    if (await isProcessAlive(pid) && await processMarkerMatches(directory, pid, instanceId)) choosing.push(entry.name);
    else {
      await removeDisposableFile(path.join(directory, entry.name));
      await removeDeadProcessMarker(directory, pid);
    }
  }
  return choosing;
}

async function liveClaims(directory) {
  const claims = [];
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    if (!entry.isFile()) continue;
    const match = CLAIM_PATTERN.exec(entry.name);
    if (!match) continue;
    const claim = {
      name: entry.name,
      ticket: BigInt(match[1]),
      pid: Number(match[2]),
      token: match[3],
    };
    const instanceId = await readInstanceFile(path.join(directory, entry.name));
    if (await isProcessAlive(claim.pid) && await processMarkerMatches(directory, claim.pid, instanceId)) claims.push(claim);
    else {
      await removeDisposableFile(path.join(directory, entry.name));
      await removeDeadProcessMarker(directory, claim.pid);
    }
  }
  return claims;
}

function compareClaims(left, right) {
  if (left.ticket < right.ticket) return -1;
  if (left.ticket > right.ticket) return 1;
  return left.token.localeCompare(right.token);
}

async function publishInstanceFile(file) {
  // Scanners must never see a partially written choosing file or process marker.
  const temporary = `${file}.${crypto.randomUUID()}.tmp`;
  try {
    await fs.writeFile(temporary, PROCESS_INSTANCE_ID, { mode: 0o600, flag: 'wx' });
    await fs.rename(temporary, file);
  } finally {
    await removeDisposableFile(temporary);
  }
}

async function processMarkerMatches(directory, pid, instanceId) {
  if (!instanceId) return false;
  const current = await readInstanceFile(path.join(directory, `process-${pid}.instance`));
  return current === instanceId;
}

async function readInstanceFile(file) {
  try {
    return await fs.readFile(file, 'utf8');
  } catch (error) {
    if (error?.code === 'ENOENT') return '';
    throw error;
  }
}

async function removeDeadProcessMarker(directory, pid) {
  if (await isProcessAlive(pid)) return;
  await removeDisposableFile(path.join(directory, `process-${pid}.instance`));
}

async function retainProcessMarker(directory) {
  await serializeProcessMarker(directory, async () => {
    const references = PROCESS_MARKER_REFERENCES.get(directory) || 0;
    if (references === 0) {
      await fs.mkdir(directory, { recursive: true, mode: 0o700 });
      await publishInstanceFile(path.join(directory, `process-${process.pid}.instance`));
    }
    PROCESS_MARKER_REFERENCES.set(directory, references + 1);
  });
}

async function releaseProcessMarker(directory) {
  await serializeProcessMarker(directory, async () => {
    const remaining = (PROCESS_MARKER_REFERENCES.get(directory) || 1) - 1;
    if (remaining > 0) {
      PROCESS_MARKER_REFERENCES.set(directory, remaining);
      return;
    }
    PROCESS_MARKER_REFERENCES.delete(directory);
    const marker = path.join(directory, `process-${process.pid}.instance`);
    const current = await fs.readFile(marker, 'utf8').catch(() => '');
    if (current === PROCESS_INSTANCE_ID) await removeDisposableFile(marker);
    // Keep the empty directory: another process may be registering its marker.
  });
}

async function releaseParticipation(file) {
  // Leaving the election is required; deleting the resulting artifact is not.
  // Rename first so a cleanup policy (including custom host deletion errors)
  // cannot strand an active claim or replace the caller's actual result.
  const released = `${file}.released`;
  try {
    await fs.rename(file, released);
  } catch (error) {
    if (error?.code === 'ENOENT') return;
    // A failure to leave the election remains fatal: never pretend the lock
    // was released while another process still sees our live claim.
    throw error;
  }
  await removeDisposableFile(released);
}

async function serializeProcessMarker(directory, action) {
  const previous = PROCESS_MARKER_OPERATIONS.get(directory) ?? Promise.resolve();
  const operation = previous.catch(() => {}).then(action);
  PROCESS_MARKER_OPERATIONS.set(directory, operation);
  try {
    return await operation;
  } finally {
    if (PROCESS_MARKER_OPERATIONS.get(directory) === operation) {
      PROCESS_MARKER_OPERATIONS.delete(directory);
    }
  }
}

async function waitUntil(deadline, pollMs, code, message) {
  if (Date.now() >= deadline) {
    throw new SkillError(code, message, { retryable: true });
  }
  await new Promise((resolve) => setTimeout(resolve, pollMs));
}

// Fixed commands only. Credentials, paths and arguments arrive on stdin as JSON;
// no caller-controlled value becomes PowerShell source or a process argument.
const SYSTEM_SCRIPT = String.raw`
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
  $utf8 = [System.Text.UTF8Encoding]::new($false)
  [Console]::InputEncoding = $utf8
  [Console]::OutputEncoding = $utf8
  $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
  if ($request.operation -eq 'protect' -or $request.operation -eq 'unprotect') {
    Add-Type -AssemblyName System.Security
    $bytes = [Convert]::FromBase64String($request.data)
    $scope = [System.Security.Cryptography.DataProtectionScope]::CurrentUser
    if ($request.operation -eq 'protect') {
      $result = [System.Security.Cryptography.ProtectedData]::Protect($bytes, $null, $scope)
    } else {
      $result = [System.Security.Cryptography.ProtectedData]::Unprotect($bytes, $null, $scope)
    }
    [Console]::Out.Write([Convert]::ToBase64String($result))
  } elseif ($request.operation -eq 'spawn') {
    $startup = New-CimInstance -Namespace root/cimv2 -ClassName Win32_ProcessStartup -ClientOnly -Property @{
      ShowWindow = [uint16]0
      # No console, new process group, Unicode environment, and leave the WMI provider job.
      CreateFlags = [uint32]0x09000600
      EnvironmentVariables = [string[]]$request.environment
    }
    $result = Invoke-CimMethod -Namespace root/cimv2 -ClassName Win32_Process -MethodName Create -Arguments @{
      CommandLine = [string]$request.commandLine
      CurrentDirectory = [string]$request.cwd
      ProcessStartupInformation = $startup
    }
    if ($result.ReturnValue -ne 0 -or $result.ProcessId -le 0) { throw 'Process creation failed' }
    [Console]::Out.Write([string]$result.ProcessId)
  } else { throw 'Unsupported operation' }
} catch {
  [Console]::Error.Write('Windows system operation unavailable')
  exit 6
}
`;

export function invokeWindowsSystem(operation, payload, {
  environment = process.env,
  spawnProcess = spawn,
  timeoutMs = 10_000,
} = {}) {
  if (!['protect', 'unprotect', 'spawn'].includes(operation)) throw new Error('invalid Windows system operation');
  const systemRoot = Object.entries(environment).find(([name]) => name.toUpperCase() === 'SYSTEMROOT')?.[1] || 'C:\\Windows';
  const executable = path.win32.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  const testHelper = environment.NODE_ENV === 'test' && environment.GUANGYAPAN_DRIVE_WINDOWS_SYSTEM_HELPER;
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawnProcess(testHelper ? process.execPath : executable,
        testHelper ? [testHelper] : ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', SYSTEM_SCRIPT],
        { env: environment, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    } catch {
      reject(windowsSystemFailure(operation));
      return;
    }
    let settled = false;
    const chunks = [];
    let outputBytes = 0;
    const finish = (error, output) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error);
      else resolve(output);
    };
    const timer = setTimeout(() => {
      child.kill();
      finish(windowsSystemFailure(operation));
    }, timeoutMs);
    timer.unref?.();
    child.stdin.on('error', () => {});
    // System diagnostics can contain sensitive paths. Never propagate them to callers.
    child.stderr.resume();
    child.stdout.on('data', (chunk) => {
      outputBytes += chunk.length;
      if (outputBytes > 2 * 1024 * 1024) {
        child.kill();
        finish(windowsSystemFailure(operation));
      } else chunks.push(chunk);
    });
    child.once('error', () => finish(windowsSystemFailure(operation)));
    child.once('close', (code) => {
      if (code !== 0) return finish(windowsSystemFailure(operation));
      finish(undefined, Buffer.concat(chunks).toString('utf8').trim());
    });
    child.stdin.end(JSON.stringify({ ...payload, operation }));
  });
}

export function windowsSystemFailure(operation) {
  const error = new Error(operation === 'spawn'
    ? 'Windows 后台启动不可用，请检查系统 PowerShell、CIM 服务及应用控制策略'
    : 'Windows 凭据加密或解密失败，请检查系统 PowerShell、.NET 及应用控制策略');
  error.code = operation === 'spawn' ? 'WINDOWS_BACKGROUND_UNAVAILABLE' : 'WINDOWS_SYSTEM_UNAVAILABLE';
  return error;
}

export async function windowsDpapi(operation, input, options = {}) {
  if (!['protect', 'unprotect'].includes(operation)) throw new Error(`不支持的 DPAPI 操作: ${operation}`);
  const bytes = operation === 'protect' ? Buffer.from(input, 'utf8') : decodeBase64(input);
  if (!bytes.length || bytes.length > 1024 * 1024) throw new Error('DPAPI 输入大小无效');
  const output = await invokeWindowsSystem(operation, { data: bytes.toString('base64') }, options);
  const result = decodeBase64(output);
  if (!result.length) throw windowsSystemFailure(operation);
  return operation === 'protect' ? result.toString('base64') : result.toString('utf8');
}

const SYSTEM_ENVIRONMENT = new Set([
  'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'COMSPEC', 'PATH', 'PATHEXT',
  'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH',
  'USERNAME', 'USERDOMAIN', 'ALLUSERSPROFILE', 'PROGRAMDATA', 'PROGRAMFILES',
  'PROGRAMFILES(X86)', 'COMMONPROGRAMFILES', 'OS', 'PROCESSOR_ARCHITECTURE',
  'NUMBER_OF_PROCESSORS', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
  'NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE',
]);

export async function spawnWindowsBackground(executable, args, {
  environment = process.env,
  workerEnvironment = {},
  cwd = process.cwd(),
  ...options
} = {}) {
  const commandLine = buildWindowsCommandLine(executable, args);
  if (typeof cwd !== 'string' || !cwd || cwd.includes('\0')) throw new Error('Windows 后台目录无效');
  const forwarded = Object.fromEntries(Object.entries(environment).filter(([name]) => SYSTEM_ENVIRONMENT.has(name.toUpperCase())));
  Object.assign(forwarded, workerEnvironment);
  const values = Object.entries(forwarded).map(([name, value]) => {
    if (!name || /[=\0]/u.test(name) || typeof value !== 'string' || value.includes('\0')) throw new Error('Windows 后台环境变量无效');
    return `${name}=${value}`;
  });
  if (values.reduce((length, entry) => length + entry.length + 1, 1) > 32_767) throw new Error('Windows 后台环境变量过长');
  const output = await invokeWindowsSystem('spawn', { commandLine, cwd, environment: values }, { environment, ...options });
  const pid = /^\d+$/u.test(output) ? Number(output) : NaN;
  if (!Number.isSafeInteger(pid) || pid <= 0 || pid > 0xffffffff) throw windowsSystemFailure('spawn');
  return pid;
}

export function buildWindowsCommandLine(executable, args = []) {
  const values = [executable, ...args];
  if (!executable || values.some((value) => typeof value !== 'string' || value.includes('\0'))) {
    throw new Error('Windows 后台进程参数必须是有效字符串');
  }
  const commandLine = values.map(quoteWindowsArgument).join(' ');
  if (!commandLine || commandLine.length > 32_766) throw new Error('Windows 后台进程命令行过长');
  return commandLine;
}

function decodeBase64(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9+/]+={0,2}$/u.test(value)) throw windowsSystemFailure('unprotect');
  const result = Buffer.from(value, 'base64');
  if (result.toString('base64') !== value) throw windowsSystemFailure('unprotect');
  return result;
}

function quoteWindowsArgument(value) {
  if (value && !/[\s"]/u.test(value)) return value;
  return `"${value.replace(/(\\*)"/gu, '$1$1\\"').replace(/(\\*)$/u, '$1$1')}"`;
}

const WORKER_ENV_ALLOWLIST = [
  'GUANGYAPAN_DRIVE_CLIENT_ID',
  'GUANGYAPAN_DRIVE_API_BASE_URL',
  'GUANGYAPAN_DRIVE_AUTH_BASE_URL',
  'GUANGYAPAN_DRIVE_LOGIN_PROJECT_ID',
  'GUANGYAPAN_DRIVE_STATE_ROOT',
  'GUANGYAPAN_DRIVE_CREDENTIAL_HELPER',
  'GUANGYAPAN_DRIVE_CREDENTIAL_FILE',
  'GUANGYAPAN_DRIVE_DPAPI_FILE',
  'GUANGYAPAN_DRIVE_MAX_DIRECTORY_DEPTH',
  'GUANGYAPAN_DRIVE_MAX_DIRECTORY_ENTRIES',
  'GUANGYAPAN_DRIVE_MAX_MANIFEST_BYTES',
  'GUANGYAPAN_DRIVE_WORKER_STARTUP_TIMEOUT_MS',
  'GUANGYAPAN_DRIVE_WORKER_HEARTBEAT_STALE_MS',
  'GUANGYAPAN_DRIVE_CLOUD_TASK_POLL_INTERVAL_MS',
  'GUANGYAPAN_DRIVE_CLOUD_TASK_VISIBILITY_TIMEOUT_MS',
  'GUANGYAPAN_DRIVE_CLOUD_TASK_NO_PROGRESS_WARNING_MS',
  'GUANGYAPAN_DRIVE_CLOUD_TASK_NO_PROGRESS_TIMEOUT_MS',
  'GUANGYAPAN_DRIVE_REMOTE_TASK_STATUS_TIMEOUT_MS',
  'GUANGYAPAN_DRIVE_API_REQUEST_TIMEOUT_MS',
  'GUANGYAPAN_DRIVE_MAX_API_RESPONSE_BYTES',
  'GUANGYAPAN_DRIVE_SHARE_HOSTS',
  'GUANGYAPAN_DRIVE_NO_OPEN_BROWSER',
  'GUANGYAPAN_DRIVE_LOGIN_MODE',
  'DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR',
];

const LINUX_WORKER_ENV_ALLOWLIST = [
  'HOME', 'PATH', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TMPDIR',
  'XAUTHORITY', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
  'http_proxy', 'https_proxy', 'no_proxy', 'NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE',
];

export async function launchBackgroundProcess({
  kind,
  bin,
  args,
  platform = process.platform,
  environment = process.env,
  config,
  execPath = process.execPath,
  spawnProcess = spawn,
  spawnWindowsProcess = spawnWindowsBackground,
  pid = process.pid,
  now = Date.now(),
}) {
  assertSupportedPlatform(platform);
  // Every backend receives the same identity already bound by the public command.
  const workerEnvironment = {
    ...Object.fromEntries(WORKER_ENV_ALLOWLIST
      .filter((name) => environment[name] !== undefined)
      .map((name) => [name, environment[name]])),
    ...agentWorkerEnvironment(config),
  };
  if (platform === 'darwin') {
    const label = `com.guangyapan.drive.${kind}.${pid}.${now}`;
    const forwarded = Object.entries(workerEnvironment).map(([name, value]) => `${name}=${value}`);
    try {
      await runProcess(spawnProcess, 'launchctl', [
        'submit',
        '-l', label,
        '-o', '/dev/null',
        '-e', '/dev/null',
        '--',
        '/usr/bin/env',
        ...forwarded,
        `GUANGYAPAN_DRIVE_LAUNCHD_LABEL=${label}`,
        execPath,
        bin,
        ...buildWorkerArgs(kind, args),
      ]);
    } catch (launchctlError) {
      try {
        await spawnDetached(
          spawnProcess,
          execPath,
          [bin, '__background-launcher', ...buildWorkerArgs(kind, args)],
          false,
          { ...environment, ...workerEnvironment },
        );
      } catch (detachedError) {
        throw new AggregateError(
          [launchctlError, detachedError],
          'launchctl 和 detached 后台启动均失败',
        );
      }
    }
    return;
  }

  if (platform === 'win32') {
    await spawnWindowsProcess(execPath, [bin, ...buildWorkerArgs(kind, args)], {
      environment,
      workerEnvironment,
    });
    return;
  }
  const linuxEnvironment = Object.fromEntries(LINUX_WORKER_ENV_ALLOWLIST
    .filter((name) => environment[name] !== undefined).map((name) => [name, environment[name]]));
  await spawnDetached(spawnProcess, execPath, [bin, ...buildWorkerArgs(kind, args)], true,
    { ...linuxEnvironment, ...workerEnvironment,
      GUANGYAPAN_DRIVE_LOGIN_MODE: config.loginMode });
}

function buildWorkerArgs(kind, args) {
  return kind === 'transfer' && args[0] === '__worker'
    ? ['__worker-supervisor', ...args.slice(1)]
    : args;
}

function spawnDetached(spawnProcess, command, args, detached = true, environment) {
  return new Promise((resolve, reject) => {
    const child = spawnProcess(command, args, {
      ...(detached ? { detached: true } : {}),
      stdio: 'ignore',
      windowsHide: true,
      ...(environment ? { env: environment } : {}),
    });
    child.once('error', reject);
    child.once('spawn', () => {
      child.unref();
      resolve();
    });
  });
}

export async function removeLaunchdJob(label, {
  platform = process.platform,
  spawnProcess = spawn,
} = {}) {
  if (platform !== 'darwin' || !isGuangyapanLaunchdLabel(label)) return false;
  await runProcess(spawnProcess, 'launchctl', ['remove', label]);
  return true;
}

export function isGuangyapanLaunchdLabel(value) {
  return typeof value === 'string' &&
    /^com\.guangyapan\.drive\.[a-z0-9-]+\.\d+\.\d+$/.test(value);
}

function runProcess(spawnProcess, command, args) {
  return new Promise((resolve, reject) => {
    const child = spawnProcess(command, args, { stdio: 'ignore', windowsHide: true });
    child.once('error', reject);
    child.once('exit', (code, signal) => {
      if (code === 0) resolve();
      else reject(new Error(`${command} failed (${signal || code})`));
    });
  });
}

export function openBrowser(url, {
  platform = process.platform,
  environment = process.env,
  spawnProcess = spawn,
} = {}) {
  assertSupportedPlatform(platform);
  if (!url || environment.GUANGYAPAN_DRIVE_NO_OPEN_BROWSER === '1') return;
  validateLocalUrl(url);
  if (platform === 'win32') {
    const child = spawnProcess('explorer.exe', [url], {
      detached: true,
      stdio: 'ignore',
      windowsHide: true,
    });
    child.on('error', () => {});
    child.unref();
    return;
  }

  if (platform === 'linux' && !environment.DISPLAY && !environment.WAYLAND_DISPLAY) return;
  const child = spawnProcess(platform === 'linux' ? 'xdg-open' : 'open', [url], {
    detached: true,
    stdio: 'ignore',
    windowsHide: true,
  });
  child.on('error', () => {});
  child.unref();
}

function validateLocalUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error('无效的本机页面地址');
  }
  if (
    url.protocol !== 'http:' ||
    url.username ||
    url.password ||
    !['127.0.0.1', '[::1]'].includes(url.hostname)
  ) {
    throw new Error('只能打开本机 HTTP 页面');
  }
}

export function backgroundStartFailure(code, message, cause) {
  const denied = cause?.code === 'WINDOWS_JOB_BREAKAWAY_DENIED';
  const systemUnavailable = cause?.code === 'WINDOWS_BACKGROUND_UNAVAILABLE';
  return new SkillError(
    code,
    denied ? 'Agent 宿主禁止启动持久后台进程'
      : systemUnavailable ? 'Windows 后台启动不可用，请检查系统 PowerShell、CIM 服务及应用控制策略'
        : message,
    {
      retryable: !denied && !systemUnavailable,
      cause,
      data: cause?.code ? { reason: cause.code } : undefined,
    },
  );
}

export async function waitForPidExit(pid, timeoutMs) {
  if (!pid) return;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline && await isProcessAlive(pid)) await delay(50);
}

const delay = (ms) => new Promise(resolve => setTimeout(resolve, ms));
