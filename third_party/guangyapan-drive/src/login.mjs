import { randomUUID, randomBytes } from 'node:crypto';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath, URL } from 'node:url';
import { MemoryStorage } from '../assets/account-sdk.mjs';
import { toQrSvg, toQrPng } from '../assets/qrcode.mjs';
import { loadConfig, AGENT_PLATFORM_VERSION, CLIENT_VERSION, agentRequestHeaders, resolveAgentName } from './config.mjs';
import { createCredentialStore, assertCredentialSession } from './credential-store.mjs';
import { readOptionalJson } from './local-files.mjs';
import {
  acquireProcessLock,
  launchBackgroundProcess,
  removeLaunchdJob,
  openBrowser,
  isProcessAlive,
  backgroundStartFailure,
  waitForPidExit,
} from './platform.mjs';
import { response, SkillError } from './protocol.mjs';

const DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code';
const RETRYABLE_AUTH_ERRORS = new Set(['authorization_pending', 'slow_down']);
const LOGIN_AUTHORIZATION_NOTICE = '登录即视为授权 Agent 调用、搜索、管理您的光鸭云盘';
const LOGIN_AGREEMENT_NOTICE = '登录表示您已阅读并同意《用户协议》和《隐私政策》';
const USER_AGREEMENT_URL = 'https://app.guangyapan.com/pan/policy/user-agreement';
const PRIVACY_POLICY_URL = 'https://app.guangyapan.com/pan/policy/privacy';

export class DeviceLogin {
  constructor({
    authBaseUrl,
    clientId,
    projectId,
    credentials,
    openBrowser,
    timeoutMs = 30_000,
    sdkLoader,
    loginServerStarter = startLoginServer,
    onLoginReady,
    successServerGraceMs = 20_000,
    failureDisplayMs = 1_500,
    cancelDisplayMs = 5_000,
    sessionTimeoutMs = 10 * 60 * 1000,
    agentName,
    loginMode = 'browser',
  }) {
    if (!['browser', 'device'].includes(loginMode)) throw new SkillError('INVALID_CONFIG', '登录模式非法');
    this.loginMode = loginMode;
    this.authBaseUrl = authBaseUrl.replace(/\/+$/, '');
    this.clientId = clientId;
    this.projectId = projectId;
    this.credentials = credentials;
    this.openBrowser = openBrowser;
    this.timeoutMs = timeoutMs;
    this.sdkLoader = sdkLoader ?? (() => import('../assets/account-sdk.mjs'));
    this.loginServerStarter = loginServerStarter;
    this.onLoginReady = onLoginReady;
    this.successServerGraceMs = successServerGraceMs;
    this.failureDisplayMs = failureDisplayMs;
    this.cancelDisplayMs = cancelDisplayMs;
    this.sessionTimeoutMs = sessionTimeoutMs;
    this.agentName = resolveAgentName(agentName);
    this.agentHeaders = agentRequestHeaders(this.agentName);
  }

  async login({ expectedGeneration } = {}) {
    const session = await this.credentials.getSession({ fresh: true });
    if (expectedGeneration !== undefined && session.generation !== expectedGeneration) {
      throw new SkillError('LOGIN_ACCOUNT_CHANGED', '登录会话已撤销，请重新发起登录');
    }
    if (session.credentials) return session.credentials;

    const { OAuth2Client, Auth } = await this.loadSdk();
    const oauth = new OAuth2Client({
      apiOrigins: [this.authBaseUrl],
      clientId: this.clientId,
      storage: new MemoryStorage(),
      timeout: this.timeoutMs,
      headers: this.agentHeaders,
      statInfo: {
        clientVersion: CLIENT_VERSION,
        platformVersion: AGENT_PLATFORM_VERSION,
      },
    });
    const auth = new Auth({
      apiOrigins: [this.authBaseUrl],
      clientId: this.clientId,
      credentialsClient: oauth,
    });
    const initialDevice = await this.createDevice(auth);
    let loginState = { state: 'WAITING', message: '等待登录' };
    let runtime;
    let completed = false;
    let terminalError;
    let completionPromise;
    let refreshPromise;
    let pollAbort;
    let sessionTimer;
    let resolveLogin;
    let rejectLogin;
    const loginResult = new Promise((resolve, reject) => {
      resolveLogin = resolve;
      rejectLogin = reject;
    });

    const complete = async (value) => {
      if (completed) {
        if (terminalError) throw terminalError;
        return false;
      }
      if (completionPromise) {
        await completionPromise;
        return false;
      }
      completionPromise = (async () => {
        const credentials = normalizeCredentials(value);
        await this.credentials.set(credentials, { generation: session.generation });
        completed = true;
        pollAbort?.abort();
        loginState = { state: 'SUCCEEDED', message: '登录成功' };
        resolveLogin(credentials);
        return true;
      })();
      try {
        return await completionPromise;
      } catch (error) {
        if (error?.code === 'LOGIN_ACCOUNT_CHANGED') await fail(error);
        throw error;
      } finally {
        completionPromise = undefined;
      }
    };

    const fail = async (error) => {
      if (completed) return;
      if (completionPromise) {
        try { await completionPromise; } catch {}
        if (completed) return;
      }
      completed = true;
      terminalError = error;
      pollAbort?.abort();
      loginState = {
        state: error?.code === 'LOGIN_DENIED' ? 'CANCELLED'
          : error?.code === 'LOGIN_EXPIRED' ? 'EXPIRED' : 'FAILED',
        message: error?.message || '登录失败，请重新发起',
      };
      rejectLogin(error);
    };

    const extendSession = (deviceDeadline) => {
      clearTimeout(sessionTimer);
      const sessionDeadline = deviceDeadline + this.sessionTimeoutMs;
      sessionTimer = setTimeout(() => {
        void fail(new SkillError('LOGIN_EXPIRED', '登录页面已过期，请重新发起', { retryable: true }));
      }, Math.max(1, sessionDeadline - Date.now()));
      return new Date(sessionDeadline).toISOString();
    };

    const startPolling = (device) => {
      pollAbort?.abort();
      const controller = new AbortController();
      pollAbort = controller;
      loginState = { state: 'WAITING', message: '等待扫码确认' };
      this.pollDevice(
        auth,
        device.deviceCode,
        device.deadline,
        device.intervalMs,
        complete,
        controller.signal,
      ).catch((error) => {
        if (completed || controller.signal.aborted) return;
        if (error?.code === 'LOGIN_EXPIRED') {
          if (this.loginMode === 'device') {
            void fail(error);
            return;
          }
          loginState = { state: 'EXPIRED', message: '二维码已过期，请刷新二维码' };
          return;
        }
        void fail(error);
      });
    };

    const refreshDevice = async () => {
      if (completed) throw new SkillError('LOGIN_ALREADY_COMPLETED', '登录已经结束');
      if (!refreshPromise) {
        refreshPromise = (async () => {
          const device = await this.createDevice(auth);
          if (completed) throw new SkillError('LOGIN_ALREADY_COMPLETED', '登录已经结束');
          startPolling(device);
          const sessionExpiresAt = extendSession(device.deadline);
          await this.onLoginReady?.(runtime?.url, sessionExpiresAt);
          return { deviceUrl: device.url, expiresAt: new Date(device.deadline).toISOString() };
        })().finally(() => { refreshPromise = undefined; });
      }
      return await refreshPromise;
    };

    try {
      if (this.loginMode === 'browser') runtime = await this.loginServerStarter({
        deviceUrl: initialDevice.url,
        expiresAt: new Date(initialDevice.deadline).toISOString(),
        authBaseUrl: this.authBaseUrl,
        clientId: this.clientId,
        projectId: this.projectId,
        getStatus: () => loginState,
        completeLogin: complete,
        refreshDevice,
        onBrowserClosed: () => fail(new SkillError('LOGIN_DENIED', '登录页面已关闭，已取消本次登录')),
      });
      await this.onLoginReady?.(runtime?.url ?? initialDevice.url, this.loginMode === 'device'
        ? new Date(initialDevice.deadline).toISOString() : extendSession(initialDevice.deadline));
      if (runtime) this.openBrowser(runtime.url);
      startPolling(initialDevice);
      try {
        const credentials = await loginResult;
        if (runtime && this.successServerGraceMs > 0) await delay(this.successServerGraceMs);
        return credentials;
      } catch (error) {
        const displayMs = error?.code === 'LOGIN_DENIED' ? this.cancelDisplayMs : this.failureDisplayMs;
        if (runtime && displayMs > 0) await delay(displayMs);
        throw error;
      }
    } finally {
      clearTimeout(sessionTimer);
      pollAbort?.abort();
      await runtime?.close?.();
    }
  }

  async createDevice(auth) {
    const device = await auth.genDeviceCode({
      scope: 'user',
      meta: { scene: 'skill_desktop_login', device: this.agentName },
    });
    const url = selectAuthorizationUrl(device, this.authBaseUrl);
    if (!url || !device.device_code) {
      throw new SkillError('INVALID_API_RESPONSE', '账号服务未返回设备授权地址');
    }
    return {
      url,
      deviceCode: device.device_code,
      deadline: Date.now() + positiveNumber(device.expires_in, 300) * 1000,
      intervalMs: positiveNumber(device.interval, 3) * 1000,
    };
  }

  async pollDevice(auth, deviceCode, deadline, intervalMs, complete, signal) {
    while (Date.now() < deadline) {
      const waited = await abortableDelay(
        Math.min(intervalMs, Math.max(1, deadline - Date.now())),
        signal,
      );
      if (!waited) return;
      try {
        const value = await auth.grantToken({
          grant_type: DEVICE_GRANT,
          device_code: deviceCode,
        });
        if (signal?.aborted) return;
        if (await complete(value)) return;
      } catch (cause) {
        const code = String(cause?.error || '').toLowerCase();
        if (code === 'slow_down') intervalMs += 5_000;
        if (RETRYABLE_AUTH_ERRORS.has(code)) continue;
        if (code === 'access_denied') {
          throw new SkillError('LOGIN_DENIED', '已取消登录', { cause });
        }
        if (code === 'expired_token') break;
        if (isRetryable(cause)) continue;
        throw new SkillError('LOGIN_FAILED', '光鸭账号登录失败', {
          retryable: isRetryable(cause),
          cause,
        });
      }
    }
    throw new SkillError('LOGIN_EXPIRED', '登录确认已过期，请重新发起', {
      retryable: true,
    });
  }

  async loadSdk() {
    try {
      return await this.sdkLoader();
    } catch (cause) {
      throw new SkillError('LOGIN_SDK_UNAVAILABLE', '账号 SDK 不可用', { cause });
    }
  }
}

function selectAuthorizationUrl(device, authBaseUrl) {
  let expectedOrigin;
  try {
    expectedOrigin = new URL(authBaseUrl).origin;
  } catch {
    return undefined;
  }
  for (const candidate of [device?.verification_uri_complete, device?.short_uri_complete]) {
    if (!candidate) continue;
    try {
      const url = new URL(candidate);
      if (url.origin === expectedOrigin && url.protocol === 'https:' && !url.username && !url.password) return url.href;
    } catch {
      // Ignore malformed service responses and try the fallback URL.
    }
  }
  return undefined;
}

function normalizeCredentials(value) {
  if (!value?.access_token || !value?.refresh_token || !value?.sub) {
    throw new SkillError('INVALID_API_RESPONSE', '账号登录结果缺少凭据');
  }
  return {
    accessToken: value.access_token,
    refreshToken: value.refresh_token,
    accountId: value.sub,
  };
}

function positiveNumber(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : fallback;
}

function isRetryable(error) {
  const status = Number(error?.status || error?.statusCode || 0);
  return status === 429 || status >= 500 || ['network_error', 'temporarily_unavailable'].includes(error?.error);
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function abortableDelay(ms, signal) {
  if (signal?.aborted) return Promise.resolve(false);
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', abort);
      resolve(true);
    }, ms);
    const abort = () => {
      clearTimeout(timer);
      resolve(false);
    };
    signal?.addEventListener('abort', abort, { once: true });
  });
}

const LOGIN_CLIENT_PATH = new URL('../assets/login-client.js', import.meta.url);
const LOCAL_PAGES_CSS_PATH = new URL('../assets/local-pages.css', import.meta.url);
const BRAND_LOGO_PATH = new URL('../assets/brand-logo.svg', import.meta.url);
const MAX_COMPLETE_BODY_BYTES = 16 * 1024;

export async function startLoginServer({
  deviceUrl,
  expiresAt,
  authBaseUrl,
  clientId,
  projectId,
  getStatus,
  completeLogin,
  refreshDevice,
  onBrowserClosed,
  pageCloseGraceMs = 5_000,
}) {
  const entryToken = randomBytes(32).toString('base64url');
  const sessionToken = randomBytes(32).toString('base64url');
  // Cookies are shared across loopback ports. Each login owns its cookie name.
  const sessionCookie = `gy_login_session_${randomBytes(12).toString('hex')}`;
  const csrfToken = randomBytes(32).toString('base64url');
  let qrSvg = await createQrSvg(deviceUrl);
  let qrExpiresAt = expiresAt;
  let qrVersion = 1;
  let qrRefreshPromise;
  const qrSnapshot = () => ({ version: qrVersion, qr_svg: qrSvg, expires_at: qrExpiresAt });
  let completionPromise;
  const presence = new LoginPagePresence(async () => {
    const status = await getStatus?.();
    if (presence.hasOpenPages() || ['SUCCEEDED', 'CANCELLED', 'FAILED'].includes(status?.state)) return;
    await onBrowserClosed?.();
  }, { graceMs: pageCloseGraceMs });

  const server = http.createServer(async (request, response) => {
    const base = `http://127.0.0.1:${server.address().port}`;
    const url = new URL(request.url, base);
    setSecurityHeaders(response, authBaseUrl);

    // The account SDK reads the redirect URL inside its own same-origin frame.
    // Serve no login UI or script here, and require no cross-site session cookie.
    if (request.method === 'GET' && url.pathname === '/captcha-callback') {
      response.setHeader('Content-Security-Policy', "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'");
      // This SDK parses the full URL as a query string and loses its first key.
      // Keep a disposable first parameter regardless of the account redirect order.
      if (url.searchParams.has('captcha_token') && url.searchParams.keys().next().value !== 'sdk_callback') {
        response.statusCode = 302;
        response.setHeader('Location', `/captcha-callback?sdk_callback=1&${url.search.slice(1)}`);
        return response.end();
      }
      return sendHtml(response, '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>安全验证完成</title></html>');
    }

    if (request.method === 'GET' && url.pathname === `/entry/${entryToken}`) {
      response.statusCode = 302;
      response.setHeader('Set-Cookie', `${sessionCookie}=${sessionToken}; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600`);
      response.setHeader('Location', '/login');
      return response.end();
    }

    if (!hasSession(request, sessionCookie, sessionToken)) return sendText(response, 403, '禁止访问');

    if (request.method === 'GET' && url.pathname === '/login') {
      const pageId = presence.createPage();
      if (!pageId) return sendText(response, 429, '打开的登录页面过多，请重新发起登录');
      return sendHtml(response, loginPage({
        pageId,
        expiresAt: qrExpiresAt,
        qrSvg,
        qrVersion,
        authBaseUrl,
        clientId,
        projectId,
        csrfToken,
      }));
    }

    if (request.method === 'GET' && url.pathname === '/login-client.js') {
      return sendJavaScript(response, await fs.readFile(LOGIN_CLIENT_PATH));
    }

    if (request.method === 'GET' && url.pathname === '/local-pages.css') {
      return sendAsset(response, 'text/css; charset=utf-8', await fs.readFile(LOCAL_PAGES_CSS_PATH));
    }

    if (request.method === 'GET' && url.pathname === '/brand-logo.svg') {
      return sendAsset(response, 'image/svg+xml', await fs.readFile(BRAND_LOGO_PATH));
    }

    if (request.method === 'GET' && url.pathname === '/api/status') {
      const status = sanitizeStatus(await getStatus?.());
      return sendJson(response, 200, {
        ...status,
        ...(['WAITING', 'EXPIRED'].includes(status.state) ? {
          qr_version: qrVersion,
          ...(request.headers['x-qr-version'] !== String(qrVersion) ? { qr: qrSnapshot() } : {}),
        } : {}),
      });
    }

    if (request.method === 'POST' && url.pathname === '/api/presence') {
      if (request.headers.origin !== base) return sendJson(response, 403, { code: 'FORBIDDEN' });
      try {
        const body = await readJson(request, 4 * 1024);
        if (body.csrf_token !== csrfToken) return sendJson(response, 403, { code: 'FORBIDDEN' });
        if (!presence.report(body.page_id, body.sequence, body.active)) {
          return sendJson(response, 400, { code: 'INVALID_LOGIN_PAGE' });
        }
        return sendJson(response, 200, { code: 'OK' });
      } catch (error) {
        return sendJson(response, error?.code === 'BODY_TOO_LARGE' ? 413 : 400, { code: 'INVALID_REQUEST' });
      }
    }

    if (request.method === 'POST' && url.pathname === '/api/refresh-qr') {
      if (request.headers.origin !== base || request.headers['x-csrf-token'] !== csrfToken) {
        return sendJson(response, 403, { code: 'FORBIDDEN' });
      }
      if (typeof refreshDevice !== 'function') {
        return sendJson(response, 503, { code: 'QR_REFRESH_UNAVAILABLE' });
      }
      try {
        // A stale tab first catches up with the current code instead of replacing it.
        const knownVersion = request.headers['x-qr-version'];
        if (knownVersion === undefined || knownVersion === String(qrVersion)) {
          if (!qrRefreshPromise) {
            qrRefreshPromise = (async () => {
              const next = await refreshDevice();
              const nextSvg = await createQrSvg(next.deviceUrl);
              qrSvg = nextSvg;
              qrExpiresAt = next.expiresAt;
              qrVersion += 1;
            })().finally(() => { qrRefreshPromise = undefined; });
          }
          await qrRefreshPromise;
        }
        return sendJson(response, 200, {
          code: 'OK',
          data: qrSnapshot(),
        });
      } catch (error) {
        return sendJson(response, 502, { code: error?.code || 'QR_REFRESH_FAILED' });
      }
    }

    if (request.method === 'POST' && url.pathname === '/api/complete') {
      if (request.headers.origin !== base || request.headers['x-csrf-token'] !== csrfToken) {
        return sendJson(response, 403, { code: 'FORBIDDEN' });
      }
      if (typeof completeLogin !== 'function') {
        return sendJson(response, 503, { code: 'LOGIN_COMPLETION_UNAVAILABLE' });
      }
      try {
        const body = await readJson(request, MAX_COMPLETE_BODY_BYTES);
        if (!completionPromise) {
          completionPromise = Promise.resolve(completeLogin(body.credentials)).catch((error) => {
            completionPromise = undefined;
            throw error;
          });
        }
        await completionPromise;
        return sendJson(response, 200, { code: 'OK' });
      } catch (error) {
        const status = error?.code === 'BODY_TOO_LARGE'
          ? 413
          : ['LOGIN_DENIED', 'LOGIN_ALREADY_COMPLETED'].includes(error?.code)
            ? 409
            : error?.code === 'INVALID_API_RESPONSE'
              ? 400
              : 500;
        return sendJson(response, status, { code: error?.code || 'LOGIN_COMPLETION_FAILED' });
      }
    }

    return sendText(response, 404, 'Not found');
  });

  server.requestTimeout = 10_000;
  server.headersTimeout = 10_000;
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  return {
    server,
    url: `http://127.0.0.1:${address.port}/entry/${entryToken}`,
    close: () => { presence.dispose(); return closeServer(server); },
  };
}

function hasSession(request, cookieName, token) {
  return (request.headers.cookie || '')
    .split(';')
    .map((item) => item.trim())
    .includes(`${cookieName}=${token}`);
}

function sanitizeStatus(status) {
  const state = ['WAITING', 'SUCCEEDED', 'CANCELLED', 'FAILED', 'EXPIRED'].includes(status?.state)
    ? status.state
    : 'WAITING';
  return {
    state,
    message: typeof status?.message === 'string' ? status.message.slice(0, 200) : '',
  };
}

function createQrSvg(deviceUrl) {
  return toQrSvg(deviceUrl, {
    type: 'svg',
    errorCorrectionLevel: 'M',
    margin: 1,
    width: 240,
  });
}

function setSecurityHeaders(response, authBaseUrl) {
  const authOrigin = safeOrigin(authBaseUrl);
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('X-Content-Type-Options', 'nosniff');
  response.setHeader('Referrer-Policy', 'no-referrer');
  response.setHeader('Content-Security-Policy', [
    "default-src 'none'",
    "style-src 'self'",
    "script-src 'self'",
    `connect-src 'self'${authOrigin ? ` ${authOrigin}` : ''}`,
    "frame-src 'self' https:",
    "img-src 'self' data:",
    "form-action 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
  ].join('; '));
}

function loginPage({ expiresAt, qrSvg, qrVersion, authBaseUrl, clientId, projectId, csrfToken, pageId }) {
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>登录光鸭</title>
  <link rel="stylesheet" href="/local-pages.css">
</head>
<body class="local-page login-page">
<main class="surface login-shell" data-login-root data-auth-origin="${escapeAttribute(authBaseUrl)}" data-client-id="${escapeAttribute(clientId)}" data-project-id="${escapeAttribute(projectId)}" data-csrf-token="${escapeAttribute(csrfToken)}" data-page-id="${escapeAttribute(pageId)}" data-qr-version="${qrVersion}">
  <div class="brand"><img src="/brand-logo.svg" alt=""><span>光鸭云盘</span></div>
  <h1>欢迎登录光鸭云盘</h1>
  <div id="login-form">
  <div class="tabs" role="tablist">
    <button class="tab active" role="tab" aria-selected="true" data-tab="qr">扫码登录</button>
    <button class="tab" role="tab" aria-selected="false" data-tab="phone">手机号登录</button>
  </div>
  <section class="panel" data-panel="qr">
    <p class="panel-intro">请使用手机扫码并确认登录</p>
    <div class="qr-frame"><div id="login-qr" class="qr">${qrSvg}</div></div>
    <button id="refresh-qr" class="button button-secondary qr-refresh" type="button">刷新二维码</button>
    <p id="qr-status" class="status" aria-live="polite"></p>
  </section>
  <section class="panel" data-panel="phone" hidden>
    <label class="field"><span class="sr-only">手机号</span><input id="phone" type="tel" inputmode="numeric" autocomplete="tel-national" maxlength="11" placeholder="手机号码"></label>
    <label class="field"><span class="sr-only">短信验证码</span><span class="code-row"><input id="code" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="短信验证码"><button id="send-code" class="button" type="button" disabled>获取验证码</button></span></label>
    <p id="phone-error" class="error" aria-live="polite"></p>
    <button id="phone-login" class="button primary" type="button" disabled>登录</button>
  </section>
  </div>
  <p id="status" class="status" aria-live="polite"></p>
  <section id="login-result" class="login-result" aria-live="polite" hidden>
    <h2 id="login-result-title">登录成功</h2>
    <p id="login-result-message">光鸭账号已登录。</p>
    <p id="close-status" class="muted">页面将在 <strong id="close-countdown">15</strong> 秒后尝试自动关闭。</p>
    <button id="close-page" class="button close-page" type="button">立即关闭</button>
  </section>
  <p class="login-protocol">${LOGIN_AUTHORIZATION_NOTICE}<br>登录表示您已阅读并同意<a href="${USER_AGREEMENT_URL}" target="_blank" rel="noopener noreferrer">《用户协议》</a>和<a href="${PRIVACY_POLICY_URL}" target="_blank" rel="noopener noreferrer">《隐私政策》</a></p>
  <small class="page-note">登录页面仅在本机运行<span id="qr-expiry-note"><br>二维码有效期至 <span id="qr-expires-at" data-expires-at="${escapeAttribute(expiresAt)}"></span></span></small>
</main>
<script src="/login-client.js" defer></script>
</body>
</html>`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}

function escapeAttribute(value) {
  return escapeHtml(value);
}

function safeOrigin(value) {
  try {
    return new URL(value).origin;
  } catch {
    return undefined;
  }
}

async function readJson(request, limit) {
  const declaredLength = Number(request.headers['content-length'] || 0);
  if (declaredLength > limit) throw Object.assign(new Error('body too large'), { code: 'BODY_TOO_LARGE' });
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) throw Object.assign(new Error('body too large'), { code: 'BODY_TOO_LARGE' });
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

function sendHtml(response, body) {
  response.statusCode = 200;
  response.setHeader('Content-Type', 'text/html; charset=utf-8');
  response.end(body);
}

function sendJson(response, status, body) {
  response.statusCode = status;
  response.setHeader('Content-Type', 'application/json; charset=utf-8');
  response.end(JSON.stringify(body));
}

function sendJavaScript(response, body) {
  response.statusCode = 200;
  response.setHeader('Content-Type', 'text/javascript; charset=utf-8');
  response.end(body);
}

function sendAsset(response, contentType, body) {
  response.statusCode = 200;
  response.setHeader('Content-Type', contentType);
  response.end(body);
}

function sendText(response, status, body) {
  response.statusCode = status;
  response.setHeader('Content-Type', 'text/plain; charset=utf-8');
  response.end(body);
}

function closeServer(server) {
  return new Promise((resolve) => {
    server.close(() => resolve());
    server.closeIdleConnections?.();
  });
}

// Only an explicit pagehide notification starts the grace period. Missing
// heartbeats alone can mean a suspended/background tab, not a cancelled login.
export class LoginPagePresence {
  constructor(onClosed, { graceMs = 5_000, schedule = setTimeout, cancel = clearTimeout } = {}) {
    this.onClosed = onClosed;
    this.graceMs = graceMs;
    this.schedule = schedule;
    this.cancel = cancel;
    this.pages = new Map();
    this.disposed = false;
  }

  createPage() {
    if (this.disposed) return undefined;
    if (this.pages.size >= 128) {
      for (const [id, page] of this.pages) {
        if (!page.active) this.pages.delete(id);
      }
    }
    if (this.pages.size >= 128) return undefined;
    const pageId = randomBytes(24).toString('base64url');
    this.pages.set(pageId, { active: true, sequence: 0 });
    this.cancel(this.timer);
    return pageId;
  }

  report(pageId, sequence, active) {
    const page = this.pages.get(pageId);
    if (this.disposed || !page || !Number.isSafeInteger(sequence) || sequence < 1 || typeof active !== 'boolean') return false;
    if (sequence <= page.sequence) return true;
    page.sequence = sequence;
    page.active = active;
    this.cancel(this.timer);
    if (!this.hasOpenPages()) {
      this.timer = this.schedule(() => {
        if (!this.disposed && !this.hasOpenPages()) {
          void Promise.resolve().then(() => this.onClosed()).catch(() => {});
        }
      }, this.graceMs);
      this.timer?.unref?.();
    }
    return true;
  }

  hasOpenPages() {
    return [...this.pages.values()].some(page => page.active);
  }

  dispose() {
    this.disposed = true;
    this.cancel(this.timer);
    this.pages.clear();
  }
}

export async function loginCommand(dependencies) {
  const existing = await verifiedLoginResponse('login', dependencies);
  if (existing) return existing;
  const state = await dependencies.startLogin();
  return loginStartedResponse('login', state, dependencies);
}

export async function loginStartedResponse(command, state, dependencies, requestId) {
  if (state?.state === 'CANCELLED') return cancelledLoginResponse(command, requestId);
  if (state?.state === 'SUCCEEDED') {
    const existing = await verifiedLoginResponse(command, dependencies);
    return { ...(existing ?? response(command, {
      code: 'LOGIN_REQUIRED', data: { state: 'IDLE', logged_in: false },
    })), ...(requestId ? { request_id: requestId } : {}) };
  }
  return response(command, {
    code: 'LOGIN_PENDING',
    retryable: true,
    request_id: requestId,
    data: pendingLoginData(state, dependencies.config),
  });
}

export function pendingLoginData(state, config) {
  const data = {
    logged_in: false,
    message: `请在已打开的本机页面完成登录。${LOGIN_AUTHORIZATION_NOTICE}。${LOGIN_AGREEMENT_NOTICE}。`,
    user_agreement_url: USER_AGREEMENT_URL,
    privacy_policy_url: PRIVACY_POLICY_URL,
  };
  if (state?.expires_at) data.expires_at = state.expires_at;
  if (state?.login_mode === 'device') {
    if (state.qr_code_path !== loginQrCodePath(config)) {
      throw new SkillError('INVALID_LOCAL_STATE', '当前 Agent 的登录二维码尚未就绪');
    }
    Object.assign(data, {
      login_mode: 'device', qr_code_path: state.qr_code_path,
      message: `请用光鸭 App 的扫一扫扫描二维码，或保存图片后从扫一扫的相册识别并确认登录；手机与 Agent 无需同网。${LOGIN_AUTHORIZATION_NOTICE}。${LOGIN_AGREEMENT_NOTICE}。`,
    });
  }
  return data;
}

export async function loginStatusCommand(dependencies) {
  const existing = await verifiedLoginResponse('login-status', dependencies);
  if (existing) return existing;

  const state = await readOptionalJson(loginWorkerStatePath(dependencies.config));
  if (
    state?.state === 'WAITING' &&
    Date.parse(state.expires_at) > Date.now() &&
    await isLoginWorkerAlive(state, dependencies.config)
  ) {
    return response('login-status', {
      code: 'LOGIN_PENDING',
      retryable: true,
      data: {
        ...pendingLoginData(state, dependencies.config),
        state: 'WAITING',
      },
    });
  }
  if (state?.state === 'CANCELLED') return cancelledLoginResponse('login-status');
  if (state?.state === 'FAILED') {
    return response('login-status', {
      code: state.code || 'LOGIN_FAILED',
      retryable: true,
      data: {
        state: state.state,
        logged_in: false,
      },
    });
  }
  return response('login-status', {
    code: 'LOGIN_REQUIRED',
    data: {
      state: 'IDLE',
      logged_in: false,
    },
  });
}

async function verifiedLoginResponse(command, dependencies) {
  const session = await dependencies.credentials.getSession({ fresh: true });
  if (!session.credentials) return undefined;
  try {
    // A protected read validates the access token and uses the standard
    // coordinated refresh/retry path. A local token alone proves no validity.
    await dependencies.apiClient.post('/assets/v1/get_assets', {
      needRights: false, needTrafficData: false,
    }, { expectedSession: session });
    assertCredentialSession(await dependencies.credentials.getSession({ fresh: true }), session);
    return response(command, {
      data: { state: 'SUCCEEDED', logged_in: true, account_id: session.credentials.accountId },
    });
  } catch (error) {
    // Never attribute an old validation result to a replacement login session.
    assertCredentialSession(await dependencies.credentials.getSession({ fresh: true }), session);
    if (error?.code === 'LOGIN_REQUIRED') {
      return response(command, {
        code: 'LOGIN_REQUIRED',
        data: { state: 'EXPIRED', logged_in: false, message: '登录状态已失效，请显式执行 relogin 重新登录' },
      });
    }
    if (error?.retryable === true || ['API_UNAVAILABLE', 'API_TIMEOUT'].includes(error?.code)) {
      return response(command, {
        code: 'LOGIN_STATUS_UNAVAILABLE', retryable: true,
        data: { state: 'UNKNOWN', logged_in: null, message: '暂时无法验证登录状态，请稍后重试；本地凭据已保留' },
      });
    }
    throw error;
  }
}

export function cancelledLoginResponse(command, requestId) {
  return response(command, {
    code: 'LOGIN_DENIED',
    retryable: false,
    request_id: requestId,
    data: {
      state: 'CANCELLED',
      logged_in: false,
      next_action: 'WAIT_FOR_USER',
      message: '用户已取消登录，请停止当前流程；仅在用户明确要求后重新发起登录。',
    },
  });
}

export async function refreshLoginCommand(dependencies) {
  const session = await dependencies.credentials.getSession({ fresh: true });
  const current = session.credentials;
  if (!current) return loginRequiredResponse();
  try {
    await dependencies.apiClient.refresh(current.refreshToken, session);
  } catch (error) {
    assertCredentialSession(await dependencies.credentials.getSession({ fresh: true }), session);
    if (error?.code === 'LOGIN_REQUIRED') return loginRequiredResponse();
    throw error;
  }
  const refreshedSession = await dependencies.credentials.getSession({ fresh: true });
  assertCredentialSession(refreshedSession, session);
  const refreshed = refreshedSession.credentials;
  if (!refreshed) return loginRequiredResponse();
  return response('refresh-login', {
    data: {
      logged_in: true,
      account_id: refreshed.accountId,
      message: '登录状态已刷新',
    },
  });
}

function loginRequiredResponse() {
  return response('refresh-login', {
    code: 'LOGIN_REQUIRED',
    data: {
      logged_in: false,
      message: '当前凭据不存在或已失效，无法静默刷新；如需重新登录，请显式执行 relogin',
    },
  });
}

export async function runLoginWorker(overrides = {}) {
  const config = overrides.config ?? loadConfig({ inheritWorkerContext: true });
  const statePath = loginWorkerStatePath(config);
  const heartbeatPath = loginWorkerHeartbeatPath(config);
  const workerInstanceId = overrides.workerInstanceId;
  const lock = await acquireLoginWorkerLock(config);
  if (!lock) return;
  let heartbeatTimer;
  let ownsLaunch = false;
  try {
    const launch = await readOptionalJson(statePath);
    // A delayed process must not revive a stopped or superseded login attempt.
    if (!workerInstanceId || launch?.instance_id !== workerInstanceId || launch.state !== 'STARTING' ||
        !Number.isSafeInteger(launch.credential_generation) || launch.credential_generation < 0) return;
    ownsLaunch = true;
    await writeLoginWorkerState(heartbeatPath, {
      pid: process.pid,
      instance_id: workerInstanceId,
      launchd_label: process.env.GUANGYAPAN_DRIVE_LAUNCHD_LABEL,
      updated_at: new Date().toISOString(),
    });
    heartbeatTimer = setInterval(() => {
      void writeLoginWorkerState(heartbeatPath, {
        pid: process.pid,
        instance_id: workerInstanceId,
        launchd_label: process.env.GUANGYAPAN_DRIVE_LAUNCHD_LABEL,
        updated_at: new Date().toISOString(),
      }).catch(() => {});
    }, 1_000);
    heartbeatTimer.unref?.();
    const dependencies = createLoginDependencies(config, {
      ...overrides,
      config,
      onLoginReady: async (url, expiresAt) => {
        const deviceMode = config.loginMode === 'device';
        if (deviceMode) {
          const deviceUrl = selectAuthorizationUrl({ verification_uri_complete: url }, config.authBaseUrl);
          if (!deviceUrl) throw new SkillError('INVALID_API_RESPONSE', '设备授权地址不属于当前账号服务');
          const png = await toQrPng(deviceUrl, { errorCorrectionLevel: 'M', margin: 4, width: 360 });
          await fs.rm(loginQrCodePath(config), { force: true });
          await fs.writeFile(loginQrCodePath(config), png, { flag: 'wx', mode: 0o600 });
        }
        await writeLoginWorkerState(statePath, {
          pid: process.pid,
          instance_id: workerInstanceId,
          launchd_label: process.env.GUANGYAPAN_DRIVE_LAUNCHD_LABEL,
          state: 'WAITING',
          login_mode: config.loginMode ?? 'browser',
          ...(deviceMode ? { qr_code_path: loginQrCodePath(config) } : { url }),
          expires_at: expiresAt,
        });
      },
    });
    try {
      const credentials = await dependencies.deviceLogin.login({ expectedGeneration: launch.credential_generation });
      await writeLoginWorkerState(statePath, {
        pid: process.pid,
        instance_id: workerInstanceId,
        launchd_label: process.env.GUANGYAPAN_DRIVE_LAUNCHD_LABEL,
        state: 'SUCCEEDED',
        account_id: credentials.accountId,
        updated_at: new Date().toISOString(),
      });
    } catch (error) {
      await writeLoginWorkerState(statePath, {
        pid: process.pid,
        instance_id: workerInstanceId,
        launchd_label: process.env.GUANGYAPAN_DRIVE_LAUNCHD_LABEL,
        state: error?.code === 'LOGIN_DENIED' ? 'CANCELLED' : 'FAILED',
        code: error?.code || 'LOGIN_FAILED',
        updated_at: new Date().toISOString(),
      });
      throw error;
    }
  } finally {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    try {
      if (ownsLaunch) {
        await fs.rm(loginQrCodePath(config), { force: true });
        await clearLoginWorkerHeartbeat(heartbeatPath, workerInstanceId);
      }
    } finally {
      await lock.close();
    }
  }
}

export async function startBackgroundLogin(config, dependencies = {}) {
  const timeoutMs = config.workerStartupTimeoutMs ?? 10_000;
  const statePath = loginWorkerStatePath(config);
  const startLock = await acquireProcessLock(loginWorkerStartLockPath(config), {
    timeoutMs: Math.max(30_000, timeoutMs * 2),
    pollMs: 25,
    busyCode: 'LOGIN_WORKER_START_BUSY',
    busyMessage: '等待本机登录服务启动超时',
  });
  try {
    const current = await readOptionalJson(statePath);
    if (
      current?.state === 'WAITING' &&
      Date.parse(current.expires_at) > Date.now() &&
      await isLoginWorkerAlive(current, config)
    ) {
      return current;
    }

    // A device code can expire while its final token request or credential save
    // is still running. Only replace its files after the worker releases them.
    let preparationLock;
    try {
      preparationLock = await acquireProcessLock(loginWorkerLockPath(config), {
        timeoutMs,
        busyCode: 'LOGIN_WORKER_FINISHING',
        busyMessage: '上一次登录尚未结束，请稍后查询登录状态或重试',
      });
    } catch (cause) {
      if (cause?.code !== 'LOGIN_WORKER_FINISHING') throw cause;
      throw new SkillError(cause.code, cause.message, { retryable: true, cause });
    }
    try {
      const settled = await readOptionalJson(statePath);
      if (['STARTING', 'WAITING'].includes(current?.state) && settled?.instance_id === current.instance_id &&
          ['SUCCEEDED', 'CANCELLED'].includes(settled?.state)) return settled;
      await fs.rm(statePath, { force: true });
      await fs.rm(loginWorkerHeartbeatPath(config), { force: true });
      await fs.rm(loginQrCodePath(config), { force: true });
    } finally {
      await preparationLock.close();
    }
    const bin = fileURLToPath(new URL('../bin/guangyapan.mjs', import.meta.url));
    const instanceId = randomUUID();
    const credentials = dependencies.credentials ?? createCredentialStore(config);
    const session = await credentials.getSession({ fresh: true });
    await writeLoginWorkerState(statePath, {
      instance_id: instanceId,
      state: 'STARTING',
      credential_generation: session.generation,
    });
    try {
      await (dependencies.launchLoginWorker ?? launchLoginWorker)(bin, instanceId, config);
    } catch (cause) {
      throw backgroundStartFailure('LOGIN_WORKER_START_FAILED', '本机登录服务启动失败', cause);
    }

    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const state = await readOptionalJson(statePath);
      if (state?.instance_id !== instanceId) {
        await delay(50);
        continue;
      }
      if (['SUCCEEDED', 'CANCELLED'].includes(state.state)) return state;
      if (
        state?.state === 'WAITING' &&
        (state.login_mode === 'device' ? state.qr_code_path === loginQrCodePath(config) : state.url) &&
        await isLoginWorkerAlive(state, config)
      ) return state;
      if (state?.state === 'FAILED') {
        throw new SkillError(state.code || 'LOGIN_FAILED', '登录服务启动失败', { retryable: true });
      }
      await delay(50);
    }
    throw new SkillError('LOGIN_WORKER_START_FAILED', '本机登录服务启动超时', { retryable: true });
  } finally {
    await startLock.close();
  }
}

async function launchLoginWorker(bin, instanceId, config) {
  await launchBackgroundProcess({
    config,
    kind: 'login',
    bin,
    args: ['__login-worker', '--instance-id', instanceId],
  });
}

export function loginWorkerStatePath(config) {
  return path.join(config.agentStateDir, 'login-worker.json');
}

function loginQrCodePath(config) {
  return path.join(config.agentStateDir, 'login-qr.png');
}

function loginWorkerHeartbeatPath(config) {
  return path.join(config.agentStateDir, 'login-worker-heartbeat.json');
}

function loginWorkerLockPath(config) {
  return path.join(config.agentStateDir, 'login-worker.lock');
}

function loginWorkerStartLockPath(config) {
  return path.join(config.agentStateDir, 'login-worker-start.lock');
}

async function acquireLoginWorkerLock(config) {
  const file = loginWorkerLockPath(config);
  return acquireProcessLock(file);
}

export async function stopPendingLogin(config) {
  const startLock = await acquireProcessLock(loginWorkerStartLockPath(config), {
    timeoutMs: Math.max(30_000, (config.workerStartupTimeoutMs ?? 10_000) * 2),
    pollMs: 25,
    busyCode: 'LOGIN_STOP_FAILED',
    busyMessage: '等待登录启动流程结束超时',
  });
  try {
    const statePath = loginWorkerStatePath(config);
    let state = await readOptionalJson(statePath);
    if (state?.state === 'STARTING') {
      const heartbeat = await readOptionalJson(loginWorkerHeartbeatPath(config));
      state = { ...state, pid: heartbeat?.pid, launchd_label: heartbeat?.launchd_label };
    }
    if (['STARTING', 'WAITING'].includes(state?.state) && await isLoginWorkerAlive(state, config)) {
      let removed = false;
      try { removed = await removeLaunchdJob(state.launchd_label); } catch {}
      if (!removed) {
        try { process.kill(state.pid, 'SIGTERM'); } catch (error) {
          if (error?.code !== 'ESRCH') throw error;
        }
      }
      await waitForPidExit(state.pid, 5000);
      if (await isProcessAlive(state.pid)) {
        throw new SkillError('LOGIN_STOP_FAILED', '等待登录进程退出超时', { retryable: true });
      }
    }
    const workerLock = await acquireProcessLock(loginWorkerLockPath(config), {
      timeoutMs: 5_000,
      busyCode: 'LOGIN_STOP_FAILED',
      busyMessage: '等待登录进程释放状态超时',
    });
    try {
      await fs.rm(statePath, { force: true });
      await fs.rm(loginWorkerHeartbeatPath(config), { force: true });
      await fs.rm(loginQrCodePath(config), { force: true });
    } finally {
      await workerLock.close();
    }
  } finally {
    await startLock.close();
  }
}

async function writeLoginWorkerState(file, value) {
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const temp = `${file}.${process.pid}.tmp`;
  await fs.writeFile(temp, `${JSON.stringify(value)}\n`, { mode: 0o600 });
  await fs.rename(temp, file);
}

async function isLoginWorkerAlive(state, config) {
  if (!state?.instance_id || !Number.isInteger(state.pid) || !await isProcessAlive(state.pid)) return false;
  const heartbeat = await readOptionalJson(loginWorkerHeartbeatPath(config)).catch(() => undefined);
  const updatedAt = Date.parse(heartbeat?.updated_at || '');
  return heartbeat?.pid === state.pid &&
    heartbeat?.instance_id === state.instance_id &&
    Number.isFinite(updatedAt) &&
    Date.now() - updatedAt <= (config.workerHeartbeatStaleMs ?? 5_000);
}

async function clearLoginWorkerHeartbeat(file, instanceId) {
  const heartbeat = await readOptionalJson(file).catch(() => undefined);
  if (heartbeat?.instance_id === instanceId) await fs.rm(file, { force: true });
}

export function createLoginDependencies(config, overrides = {}) {
  const credentials = overrides.credentials ?? createCredentialStore(config);
  const browserOpener = overrides.openBrowser ?? openBrowser;
  return {
    credentials,
    openBrowser: browserOpener,
    startLogin: overrides.startLogin ?? (() => startBackgroundLogin(config, { credentials })),
    stopLogin: overrides.stopLogin ?? (() => stopPendingLogin(config)),
    deviceLogin: overrides.deviceLogin ?? new DeviceLogin({
      authBaseUrl: config.authBaseUrl,
      clientId: config.clientId,
      projectId: config.loginProjectId,
      agentName: config.agentName,
      loginMode: config.loginMode,
      credentials,
      openBrowser: browserOpener,
      timeoutMs: config.apiRequestTimeoutMs,
      sdkLoader: overrides.sdkLoader,
      onLoginReady: overrides.onLoginReady,
    }),
  };
}
