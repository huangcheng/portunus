#!/usr/bin/env node
import { spawn } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { FileCredentialBackend, macosKeychain, linuxSecretService } from '../src/credential-store.mjs';
import { acquireProcessLock, windowsDpapi } from '../src/platform.mjs';
import { assertSupportedPlatform } from '../src/config.mjs';
import { removeDisposableFile } from '../src/local-files.mjs';

const SERVICE = 'com.guangyapan.drive';
const testPlatform = process.env.NODE_ENV === 'test' ? process.env.GUANGYAPAN_DRIVE_CREDENTIAL_PLATFORM : undefined;
const platform = testPlatform || process.platform;
// All storage paths come from the same resolved CLI scope.
let ACCOUNT;
let fallback;
let dpapi;
let credentialLock;
try {
  assertSupportedPlatform(platform);
  const operation = process.argv[2];
  if (!['get', 'set', 'delete'].includes(operation)) fail(2, 'unsupported operation');
  const payload = await readPayload();
  if (payload.service !== SERVICE) fail(2, 'invalid service');
  if (!/^[a-f0-9]{64}$/.test(payload.scope_id || '')) fail(2, 'agent scope is required');
  ACCOUNT = `agent-${payload.scope_id}`;
  for (const file of [payload.account_state_dir, payload.credential_file, payload.dpapi_file]) {
    if (typeof file !== 'string' || !path.isAbsolute(file)) fail(2, 'credential path must be absolute');
  }
  fallback = new FileCredentialBackend(payload.credential_file);
  dpapi = new FileCredentialBackend(payload.dpapi_file);

  const controls = {
    lockPath: path.join(payload.account_state_dir, 'credential-helper.lock'),
    generationPath: path.join(payload.account_state_dir, 'credential-generation.json'),
  };
  credentialLock = await acquireProcessLock(controls.lockPath, {
    timeoutMs: 60_000,
    busyCode: 'CREDENTIAL_HELPER_BUSY',
    busyMessage: '等待其他账号凭据操作超时',
  });
  if (operation === 'set' || operation === 'delete') {
    const generation = await readCredentialGeneration(controls.generationPath);
    if (operation === 'set') {
      const expected = payload.credential_generation;
      if (!Number.isSafeInteger(expected) || expected < 0) fail(2, 'invalid credential generation');
      if (expected !== generation) {
        const error = new Error('credential write was invalidated by logout');
        error.exitCode = 5;
        throw error;
      }
    } else {
      await writeCredentialGeneration(controls.generationPath, generation + 1);
    }
  }

  if (platform === 'darwin') {
    await macos(operation, payload);
  } else if (platform === 'linux') {
    await linux(operation, payload);
  } else {
    await windows(operation, payload);
  }
} catch (error) {
  if (error?.silent !== true) {
    process.stderr.write(`${error?.message || 'credential helper failed'}\n`);
  }
  process.exitCode = error?.exitCode || 4;
} finally {
  // This one-shot helper exits now; stale claims are ignored once its PID dies.
  // A denied lock cleanup must not replace a completed credential operation.
  await credentialLock?.close().catch(() => {});
}

async function readCredentialGeneration(file) {
  try {
    const value = JSON.parse(await fs.readFile(file, 'utf8'));
    if (!Number.isSafeInteger(value?.generation) || value.generation < 0) {
      throw new Error('invalid credential generation file');
    }
    return value.generation;
  } catch (error) {
    if (error?.code === 'ENOENT') return 0;
    throw error;
  }
}

async function writeCredentialGeneration(file, generation) {
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const temporary = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
  const handle = await fs.open(temporary, 'wx', 0o600);
  try {
    await handle.writeFile(`${JSON.stringify({ generation })}\n`, 'utf8');
    await handle.sync();
  } finally {
    await handle.close();
  }
  try {
    await fs.rename(temporary, file);
  } finally {
    await removeDisposableFile(temporary);
  }
}

async function windows(operation, payload) {
  if (operation === 'get') {
    if (await dpapi.isLoggedOut()) {
      await dpapi.delete().catch(() => {});
      notFound();
    }
    const record = await dpapi.get();
    if (!record) notFound();
    const protectedValue = validateDpapiRecord(record);
    const secret = await invokeDpapi('unprotect', protectedValue);
    process.stdout.write(credentialJson(JSON.parse(secret)));
    return;
  }
  if (operation === 'set') {
    const secret = credentialJson(payload.credentials);
    await setDpapiSecret(secret);
    return;
  }
  try {
    await dpapi.delete();
  } catch (error) {
    await dpapi.markLoggedOut().catch(() => {});
    throw error;
  }
}

async function setDpapiSecret(secret) {
  const protectedValue = (await invokeDpapi('protect', secret)).trim();
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(protectedValue)) {
    throw new Error('DPAPI returned invalid data');
  }
  await dpapi.set({ version: 1, protected: protectedValue });
}

function validateDpapiRecord(record) {
  if (
    record?.version !== 1 ||
    typeof record.protected !== 'string' ||
    !/^[A-Za-z0-9+/]+={0,2}$/.test(record.protected)
  ) {
    throw new Error('invalid DPAPI credential file');
  }
  return record.protected;
}

async function invokeDpapi(operation, input) {
  try {
    return await windowsDpapi(operation, input);
  } catch {
    const failure = new Error('Windows 凭据加密或解密失败，请检查系统 PowerShell、.NET 及应用控制策略');
    failure.exitCode = 6;
    throw failure;
  }
}

async function macos(operation, payload) {
  const keychain = async (action, secret) => {
    const testHelper = process.env.NODE_ENV === 'test' && process.env.GUANGYAPAN_DRIVE_MACOS_KEYCHAIN_HELPER;
    if (testHelper) return run(process.execPath, [testHelper, action, SERVICE, ACCOUNT], secret, new Set([44]));
    return macosKeychain(action, { service: SERVICE, account: ACCOUNT, secret, run });
  };
  if (operation === 'get') {
    if (await fallback.isLoggedOut()) {
      try {
        await keychain('delete');
        await fallback.delete();
      } catch {}
      notFound();
    }
    const fileCredentials = await fallback.get();
    if (fileCredentials) {
      const secret = credentialJson(fileCredentials);
      try {
        await keychain('set', secret);
        await fallback.delete();
      } catch {}
      process.stdout.write(secret);
      return;
    }
    try {
      const result = await keychain('get');
      if (result.code === 0) {
        process.stdout.write(result.stdout.trim());
        return;
      }
      notFound();
    } catch {
      notFound();
    }
  }
  if (operation === 'set') {
    const secret = credentialJson(payload.credentials);
    try {
      await keychain('set', secret);
      await fallback.delete();
    } catch {
      await fallback.set(payload.credentials);
    }
    return;
  }
  try {
    await keychain('delete');
    await fallback.delete();
  } catch (error) {
    await fallback.markLoggedOut();
  }
}

async function linux(operation, payload) {
  const secretService = async (action, options = {}) => {
    const testHelper = process.env.NODE_ENV === 'test' && process.env.GUANGYAPAN_DRIVE_LINUX_SECRET_HELPER;
    const invoke = testHelper
      ? (_command, args, input, allowed) => run(process.execPath, [testHelper, ...args], input, allowed)
      : run;
    return linuxSecretService(action, { service: SERVICE, account: ACCOUNT, ...options, run: invoke });
  };
  const generation = await readCredentialGeneration(path.join(payload.account_state_dir, 'credential-generation.json'));
  const record = await fallback.get();
  if (record && (record.version !== 1 || !['file', 'secret-service', 'revoked'].includes(record.backend) ||
      !Number.isSafeInteger(record.generation) || record.generation < 0 ||
      (record.backend === 'secret-service' && !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(record.entry ?? '')))) {
    fail(7, 'invalid Linux credential record');
  }
  const hasSystemRecord = record?.backend === 'secret-service' || record?.system_cleanup_required === true;
  const cleanup = (current) => secretService('cleanup', {
    entry: current?.generation === generation && current.backend === 'secret-service' && !current.pending ? current.entry : undefined,
  });
  if (operation === 'get') {
    if (!record) notFound();
    if (record.generation !== generation || record.backend === 'revoked' || record.pending === true) {
      if (hasSystemRecord) await cleanup().catch(() => {});
      if (record.generation !== generation || record.backend === 'revoked') notFound();
      fail(7, 'Linux credential write was interrupted');
    }
    const credentials = record.backend === 'file' ? record.credentials
      : JSON.parse((await secretService('get', { entry: record.entry })).stdout);
    if (hasSystemRecord) await cleanup(record).catch(() => {});
    process.stdout.write(credentialJson(credentials));
    return;
  }
  if (operation === 'set') {
    const secret = credentialJson(payload.credentials);
    const systemBackend = record?.generation === generation && record.backend === 'secret-service';
    let cleanupRequired = hasSystemRecord;
    if (systemBackend || ((record?.generation !== generation || record.backend !== 'file') && process.env.DBUS_SESSION_BUS_ADDRESS)) {
      const next = { version: 1, generation, backend: 'secret-service', entry: crypto.randomUUID() };
      let attemptedWrite = false;
      try {
        // Refuse another system write if stale entries cannot be removed.
        // This bounds abandoned entries even when the keyring denies deletion.
        if (hasSystemRecord) await cleanup(record);
        if (!systemBackend) await fallback.set({ ...next, pending: true });
        attemptedWrite = true;
        await secretService('set', { entry: next.entry, secret });
        // Only this atomic reference makes an immutable entry the active login.
        // An orphan store/clear can only affect its own UUID, never a later one.
        await fallback.set(next);
        await cleanup(next).catch(() => {});
        return;
      } catch (error) {
        cleanupRequired ||= attemptedWrite && !error.unavailableBeforeWrite;
        // A selected keyring stays selected when it is temporarily locked.
        if (systemBackend) {
          await cleanup(await fallback.get()).catch(() => {});
          throw error;
        }
      }
    }
    await fallback.set({ version: 1, generation, backend: 'file', credentials: payload.credentials,
      ...(cleanupRequired ? { system_cleanup_required: true } : {}) });
    if (cleanupRequired) await cleanup().catch(() => {});
    return;
  }
  if (hasSystemRecord) {
    // Retain the non-secret revocation record after cleanup. A killed helper's
    // late store is then discovered and removed on subsequent access as well.
    await fallback.set({ version: 1, generation, backend: 'revoked', system_cleanup_required: true });
    await cleanup();
    return;
  }
  await fallback.delete();
}

function credentialJson(value) {
  if (
    !value ||
    typeof value.accessToken !== 'string' || !value.accessToken ||
    typeof value.refreshToken !== 'string' || !value.refreshToken ||
    typeof value.accountId !== 'string' || !value.accountId
  ) fail(2, 'invalid credentials');
  return JSON.stringify(value);
}

async function readPayload() {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > 64 * 1024) fail(2, 'payload too large');
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    fail(2, 'invalid JSON');
  }
}

function run(command, args, input, allowed = new Set()) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    const stdout = [], stderr = [];
    let outputBytes = 0;
    let outputError;
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new Error('credential store timed out'));
    }, 30_000);
    const collect = (destination, chunk) => {
      if (outputError) return;
      outputBytes += chunk.length;
      // Search attributes drive keyring cleanup: partial output cannot be
      // treated as a complete list. Bound memory and fail without diagnostics.
      if (outputBytes > 1024 * 1024) {
        outputError = new Error('credential store output exceeded limit');
        child.kill('SIGKILL');
        return;
      }
      destination.push(chunk);
    };
    child.stdout.on('data', (chunk) => collect(stdout, chunk));
    child.stderr.on('data', (chunk) => collect(stderr, chunk));
    child.once('error', (error) => {
      clearTimeout(timer);
      error.stderr = Buffer.concat(stderr).toString('utf8');
      reject(error);
    });
    child.stdin.on('error', () => {});
    child.once('close', (code) => {
      clearTimeout(timer);
      if (outputError) { reject(outputError); return; }
      const result = { code: code ?? 4, stdout: Buffer.concat(stdout).toString('utf8'), stderr: Buffer.concat(stderr).toString('utf8') };
      if (result.code === 0 || allowed.has(result.code)) resolve(result);
      else {
        const error = new Error(result.stderr.trim() || 'credential store command failed');
        error.exitCode = result.code;
        error.stderr = result.stderr;
        reject(error);
      }
    });
    child.stdin.end(input);
  });
}

function fail(code, message) {
  const error = new Error(message);
  error.exitCode = code;
  throw error;
}

function notFound() {
  const error = new Error('credential not found');
  error.exitCode = 3;
  error.silent = true;
  throw error;
}
