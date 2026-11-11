import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { syncDirectory } from './local-files.mjs';
import { acquireProcessLock } from './platform.mjs';
import { SkillError, toSkillError } from './protocol.mjs';

const SERVICE = 'com.guangyapan.drive';

export class CredentialStore {
  constructor(helperCommand, { accountStateDir, agentScopeId, credentialFile, dpapiFile, helperTimeoutMs = 30_000 } = {}) {
    this.helperCommand = helperCommand;
    this.helperTimeoutMs = helperTimeoutMs;
    if (typeof accountStateDir !== 'string' || !accountStateDir ||
        typeof agentScopeId !== 'string' || !/^[a-f0-9]{64}$/.test(agentScopeId)) {
      throw new SkillError('INVALID_AGENT_INSTANCE', '凭据存储必须绑定 Agent 作用域');
    }
    this.accountStateDir = accountStateDir;
    this.agentScopeId = agentScopeId;
    this.credentialFile = credentialFile ?? path.join(accountStateDir, 'credentials.json');
    this.dpapiFile = dpapiFile ?? path.join(accountStateDir, 'credentials.dpapi.json');
    this.generationPath = path.join(this.accountStateDir, 'credential-generation.json');
    this.cacheLoaded = false;
    this.cached = undefined;
    this.loadPromise = undefined;
  }

  async get({ fresh = false } = {}) {
    return (await this.getSession({ fresh })).credentials;
  }

  async getSession({ fresh = false } = {}) {
    const generation = await readCredentialGeneration(this.generationPath);
    if (!fresh && this.cacheLoaded && this.cachedGeneration === generation) {
      return { credentials: cloneCredential(this.cached), generation };
    }
    if (!fresh && this.loadPromise) return await this.loadPromise;

    const load = (async () => {
      let result;
      let before;
      let after;
      do {
        before = await readCredentialGeneration(this.generationPath);
        result = await invokeHelper(
          this.helperCommand, 'get', this.helperPayload(), this.helperTimeoutMs,
        );
        after = await readCredentialGeneration(this.generationPath);
      } while (before !== after);
      if (result) validateCredential(result);
      this.cached = cloneCredential(result);
      this.cachedGeneration = after;
      this.cacheLoaded = true;
      return { credentials: cloneCredential(result), generation: after };
    })();
    this.loadPromise = load;
    try {
      return await load;
    } finally {
      if (this.loadPromise === load) this.loadPromise = undefined;
    }
  }

  async set(credentials, { generation: expectedGeneration } = {}) {
    validateCredential(credentials);
    if (!Number.isSafeInteger(expectedGeneration) || expectedGeneration < 0) {
      throw new SkillError('INVALID_CREDENTIAL_STATE', '保存凭据必须绑定登录开始时的会话代次');
    }
    const generation = expectedGeneration;
    await invokeHelper(this.helperCommand, 'set', {
      ...this.helperPayload(),
      credential_generation: generation,
      credentials,
    }, this.helperTimeoutMs);
    this.cached = cloneCredential(credentials);
    this.cachedGeneration = generation;
    this.cacheLoaded = true;
  }

  async delete() {
    await invokeHelper(this.helperCommand, 'delete', this.helperPayload(), this.helperTimeoutMs);
    this.cached = undefined;
    this.cachedGeneration = await readCredentialGeneration(this.generationPath);
    this.cacheLoaded = true;
  }

  helperPayload() {
    return {
      service: SERVICE,
      scope_id: this.agentScopeId,
      account_state_dir: this.accountStateDir,
      credential_file: this.credentialFile,
      dpapi_file: this.dpapiFile,
    };
  }
}

async function readCredentialGeneration(file) {
  try {
    const value = JSON.parse(await fs.readFile(file, 'utf8'));
    if (!Number.isSafeInteger(value?.generation) || value.generation < 0) {
      throw new SkillError('INVALID_CREDENTIAL_STATE', '本机账号代次文件非法');
    }
    return value.generation;
  } catch (error) {
    if (error?.code === 'ENOENT') return 0;
    throw error;
  }
}

async function invokeHelper(command, operation, payload, timeoutMs) {
  return await new Promise((resolve, reject) => {
    const isNodeScript = command.endsWith('.mjs') || command.endsWith('.js');
    const child = spawn(isNodeScript ? process.execPath : command, isNodeScript ? [command, operation] : [operation], {
      stdio: ['pipe', 'pipe', 'ignore'],
      windowsHide: true,
      detached: process.platform !== 'win32',
    });
    const stdout = [];
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      callback(value);
    };
    const timeout = setTimeout(() => {
      // The one-shot helper can be waiting on a system credential subprocess.
      // Kill its POSIX process group so an expired operation cannot keep writing.
      if (process.platform !== 'win32' && child.pid) {
        try { process.kill(-child.pid, 'SIGKILL'); } catch { child.kill('SIGKILL'); }
      } else child.kill('SIGKILL');
      finish(reject, new SkillError(
        'CREDENTIAL_HELPER_TIMEOUT',
        '系统凭据助手执行超时',
        { retryable: true },
      ));
    }, timeoutMs);
    timeout.unref?.();
    child.stdout.on('data', (chunk) => stdout.push(chunk));
    child.stdin.on('error', () => {});
    child.once('error', (cause) => {
      finish(reject,
        new SkillError('CREDENTIAL_HELPER_UNAVAILABLE', credentialFailureMessage(), {
          cause,
        }),
      );
    });
    child.once('exit', (code) => {
      if (code === 3 && operation === 'get') return finish(resolve, undefined);
      if (code === 6) {
        return finish(reject, new SkillError(
          'WINDOWS_CREDENTIAL_UNAVAILABLE',
          'Windows 凭据加密或解密失败，请检查系统 PowerShell、.NET 及应用控制策略',
        ));
      }
      if (code === 7) {
        return finish(reject, new SkillError(
          'LINUX_CREDENTIAL_UNAVAILABLE',
          'Linux 系统凭据无法读写或清理；原登录记录已保留，退出失败时本地登录已撤销，请检查 Secret Service',
        ));
      }
      if (code === 5) return finish(reject, accountChanged());
      if (code !== 0) {
        return finish(reject,
          new SkillError('CREDENTIAL_HELPER_FAILED', credentialFailureMessage()),
        );
      }
      if (operation !== 'get') return finish(resolve, undefined);
      try {
        finish(resolve, JSON.parse(Buffer.concat(stdout).toString('utf8')));
      } catch (cause) {
        finish(reject,
          new SkillError('CREDENTIAL_HELPER_FAILED', '系统凭据助手返回非法数据', {
            cause,
          }),
        );
      }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

export class CredentialRefreshCoordinator {
  constructor(accountStateDir, credentials) {
    this.lockPath = path.join(accountStateDir, 'credential-account.lock');
    this.credentials = credentials;
  }

  async run(expectedRefreshToken, refresh, expectedSession) {
    return this.runExclusive(async () => {
      const session = await this.credentials.getSession({ fresh: true });
      assertCredentialSession(session, expectedSession);
      const current = session.credentials;
      if (!current) throw new SkillError('LOGIN_REQUIRED', '需要登录光鸭账号');
      if (current.refreshToken !== expectedRefreshToken) return current;
      const next = await refresh(current);
      const value = { ...next, accountId: current.accountId };
      assertCredentialSession(await this.credentials.getSession({ fresh: true }), session);
      await this.credentials.set(value, { generation: session.generation });
      return value;
    }, 60_000, 'TOKEN_REFRESH_BUSY', '等待账号刷新超时');
  }

  async runLogout(logout) {
    return this.runExclusive(
      logout,
      60_000,
      'LOGOUT_BUSY',
      '等待账号刷新结束超时',
    );
  }

  async runExclusive(action, timeoutMs, busyCode, busyMessage) {
    await fs.mkdir(path.dirname(this.lockPath), { recursive: true, mode: 0o700 });
    const handle = await acquireProcessLock(this.lockPath, {
      timeoutMs,
      busyCode,
      busyMessage,
    });
    try {
      return await action();
    } finally {
      await handle.close();
    }
  }
}

export function assertCredentialSession(current, expected) {
  if (expected && (
    current.credentials?.accountId !== expected.credentials?.accountId ||
    current.generation !== expected.generation
  )) throw accountChanged();
}

function accountChanged() {
  return new SkillError('LOGIN_ACCOUNT_CHANGED', '登录账号或登录会话已变更，请重新发起操作');
}

function validateCredential(value) {
  if (
    !value ||
    typeof value.accessToken !== 'string' ||
    !value.accessToken ||
    typeof value.refreshToken !== 'string' ||
    !value.refreshToken ||
    typeof value.accountId !== 'string' ||
    !value.accountId
  ) {
    throw new SkillError('INVALID_CREDENTIAL', '账号凭据不完整');
  }
}

function cloneCredential(value) {
  return value ? { ...value } : undefined;
}

function credentialFailureMessage() {
  if (process.platform === 'win32') {
    return 'Windows 凭据读写失败，请确认用户目录可写且系统 PowerShell、.NET 可用';
  }
  return '系统凭据助手执行失败';
}

export class FileCredentialBackend {
  constructor(file) {
    this.file = path.resolve(file);
    this.loggedOutFile = `${this.file}.logged-out`;
  }

  async get() {
    let stat;
    try {
      stat = await fs.lstat(this.file);
    } catch (error) {
      if (error?.code === 'ENOENT') return undefined;
      throw error;
    }
    assertPrivateFile(stat);
    if (process.platform !== 'win32' && (stat.mode & 0o077) !== 0) {
      await fs.chmod(this.file, 0o600);
    }
    const raw = await fs.readFile(this.file, 'utf8');
    return JSON.parse(raw);
  }

  async set(value) {
    await writePrivateFile(this.file, `${JSON.stringify(value)}\n`);
    await removeFilesAndSyncDirectory([this.loggedOutFile]);
  }

  async isLoggedOut() {
    try {
      const stat = await fs.lstat(this.loggedOutFile);
      assertPrivateFile(stat);
      if (process.platform !== 'win32' && (stat.mode & 0o077) !== 0) {
        await fs.chmod(this.loggedOutFile, 0o600);
      }
      return true;
    } catch (error) {
      if (error?.code === 'ENOENT') return false;
      throw error;
    }
  }

  async markLoggedOut() {
    await writePrivateFile(this.loggedOutFile, 'logged-out\n');
    await removeFilesAndSyncDirectory([this.file]);
  }

  async delete() {
    await removeFilesAndSyncDirectory([this.file, this.loggedOutFile]);
  }
}

async function writePrivateFile(file, contents) {
  const directory = path.dirname(file);
  await ensurePrivateDirectory(directory);
  const temporary = path.join(directory, `.${path.basename(file)}.${process.pid}.${randomUUID()}.tmp`);
  let handle;
  try {
    handle = await fs.open(temporary, 'wx', 0o600);
    await handle.writeFile(contents);
    await handle.sync();
    await handle.close();
    handle = undefined;
    await fs.rename(temporary, file);
    if (process.platform !== 'win32') await fs.chmod(file, 0o600);
    await syncDirectory(directory);
  } finally {
    await handle?.close().catch(() => {});
    await fs.rm(temporary, { force: true }).catch(() => {});
  }
}

async function removeFilesAndSyncDirectory(files) {
  let removed = false;
  for (const file of files) {
    try {
      await fs.unlink(file);
      removed = true;
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error;
    }
  }
  if (removed) await syncDirectory(path.dirname(files[0]));
}

async function ensurePrivateDirectory(directory) {
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const stat = await fs.lstat(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error('credential directory must be a real directory');
  }
  assertCurrentUser(stat, 'credential directory');
  if (process.platform !== 'win32' && (stat.mode & 0o077) !== 0) {
    await fs.chmod(directory, 0o700);
  }
}

function assertPrivateFile(stat) {
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error('credential file must be a regular file');
  }
  assertCurrentUser(stat, 'credential file');
}

function assertCurrentUser(stat, label) {
  if (typeof process.getuid === 'function' && stat.uid !== process.getuid()) {
    throw new Error(`${label} must be owned by the current user`);
  }
}

// Use Apple's system tool so downloaded Skill packages need no native macOS executable.
export async function macosKeychain(operation, { service, account, secret, run }) {
  if (!/^[A-Za-z0-9._-]+$/.test(service) || !/^[A-Za-z0-9._-]+$/.test(account)) {
    throw new Error('invalid Keychain item identifier');
  }
  const command = '/usr/bin/security';
  const identity = ['-s', service, '-a', account];
  if (operation === 'get') {
    const result = await run(command, ['find-generic-password', ...identity, '-w'], undefined, new Set([44]));
    // security prints non-ASCII password bytes as hex even with -w.
    const value = result.stdout.trim();
    if (result.code === 0 && /^(?:[0-9a-f]{2})+$/i.test(value)) {
      return { ...result, stdout: Buffer.from(value, 'hex').toString('utf8') };
    }
    return result;
  }
  if (operation === 'delete') {
    return run(command, ['delete-generic-password', ...identity], undefined, new Set([44]));
  }
  if (operation !== 'set' || typeof secret !== 'string') {
    throw new Error('invalid Keychain operation');
  }

  // security's interactive mode accepts commands on stdin. Hex keeps arbitrary
  // credential text out of its command parser and out of process arguments.
  const input = `add-generic-password -U ${identity.join(' ')} -X ${Buffer.from(secret).toString('hex')}\n`;
  // security reads at most 4095 bytes per line. Never allow a truncated write.
  if (Buffer.byteLength(input) >= 4095) {
    throw new Error('credential exceeds Keychain command input limit');
  }
  try {
    await run(command, ['-i', '-q'], input);
    const saved = await macosKeychain('get', { service, account, run });
    if (saved.code !== 0 || saved.stdout.trim() !== secret) throw new Error('Keychain write did not persist');
    return { code: 0, stdout: '' };
  } catch {
    // Interactive diagnostics can echo input, so never expose them to logs.
    throw new Error('macOS Keychain write failed');
  }
}

export async function linuxSecretService(operation, { service, account, entry, secret, run }) {
  const scope = ['service', service, 'account', account];
  try {
    if (operation === 'cleanup') {
      // libsecret prints item attributes on stderr. Never return its search
      // output: stdout can contain secrets and stderr can contain diagnostics.
      const found = await run('secret-tool', ['search', '--all', ...scope]);
      const entries = new Set([...String(found.stderr ?? '').matchAll(
        /^attribute\.entry = ([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\r?$/gm,
      )].map(match => match[1]));
      for (const candidate of entries) {
        if (candidate === entry) continue;
        const cleared = await run('secret-tool', ['clear', ...scope, 'entry', candidate], undefined, new Set([1]));
        // No match is exit 1 without diagnostics; real failures also use exit 1.
        if (cleared.code !== 0 && (cleared.stderr ?? '').trim()) throw new Error('keyring cleanup failed');
      }
      return { code: 0, stdout: '' };
    }
    if (!/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(entry ?? '')) {
      throw new Error('invalid keyring entry');
    }
    const identity = [...scope, 'entry', entry];
    if (operation === 'get') {
      return await run('secret-tool', ['lookup', ...identity]);
    }
    if (operation !== 'set' || typeof secret !== 'string') throw new Error('invalid operation');
    await run('secret-tool', ['store', '--label=Guangyapan Drive', ...identity], secret);
    const saved = await linuxSecretService('get', { service, account, entry, run });
    if (saved.stdout.trim() !== secret) throw new Error('credential write did not persist');
    return { code: 0, stdout: '' };
  } catch (cause) {
    // System diagnostics must never expose credential input.
    const error = new Error('Linux Secret Service unavailable');
    error.exitCode = 7;
    error.unavailableBeforeWrite = cause?.code === 'ENOENT';
    throw error;
  }
}

// Writes are serialized with logout. Read-only calls can run concurrently;
// both retain the original session through all helper calls and auth retries.
export function accountRequestGuard({ store, credentials, batch, expectedSession, readOnly = false }) {
  let session = expectedSession;
  return async (send) => {
    let sendStarted = false;
    try {
      const action = async () => {
        if (batch) {
          const code = await store.stopCode(batch.batch_id, batch.account_generation);
          if (code) throw new SkillError(code, '原任务已停止，不再发起网盘请求');
        }
        const current = await credentials.getSession({ fresh: true });
        assertCredentialSession(current, session);
        if (!current.credentials) throw new SkillError('LOGIN_REQUIRED', '需要原账号登录态才能继续操作');
        if (batch && current.credentials.accountId !== batch.account_id) {
          throw new SkillError('LOGIN_ACCOUNT_CHANGED', '登录账号与原任务账号不一致');
        }
        session ??= current;
        sendStarted = true;
        return send(session);
      };
      return await (readOnly ? action() : store.runAccountExclusive(action));
    } catch (error) {
      // Once send began, even a later unlock failure can follow a committed
      // write. Only pre-send failures are safe to classify as not submitted.
      if (sendStarted) throw error;
      const failure = toSkillError(error);
      throw new SkillError(failure.code, failure.message, {
        cause: failure,
        retryable: failure.retryable,
        data: { ...failure.data, request_not_accepted: true },
      });
    }
  };
}

export function createCredentialStore(config) {
  return new CredentialStore(config.credentialHelper, {
    accountStateDir: config.accountStateDir,
    agentScopeId: config.agentScopeId,
    credentialFile: config.credentialFile,
    dpapiFile: config.dpapiFile,
  });
}
