import { CLIENT_VERSION, agentRequestHeaders } from './config.mjs';
import { assertCredentialSession } from './credential-store.mjs';
import { SkillError, toSkillError } from './protocol.mjs';

const AUTH_CODES = new Set([110, 117, 118]);
// Known validation/permission/resource rejections. Infrastructure and unknown
// business codes can arrive after a write committed, even with HTTP 200.
const REQUEST_REJECTION_CODES = new Set([
  111, 112, 113, 114, 115, 116, 119, 120, 121, 122, 123, 124, 125, 127, 128,
  140, 141, 142, 143, 144, 146, 150, 153, 154, 157, 158, 159, 160, 166,
  350, 351, 352, 353, 354, 430,
]);
const DEFAULT_MAX_JSON_BYTES = 32 * 1024 * 1024;
const NUMERIC_API_ERROR_CODES = new Map([
  [142, 'REMOTE_DIRECTORY_NOT_FOUND'],
  [143, 'REMOTE_FILE_NOT_FOUND'],
  [146, 'REMOTE_FILE_NOT_FOUND'],
  [154, 'REMOTE_TARGET_NOT_DIRECTORY'],
  [160, 'REMOTE_TARGET_CONFLICT'],
]);

export class DriveApiClient {
  constructor({
    baseUrl,
    authBaseUrl,
    clientId,
    credentials,
    refreshCoordinator,
    fetchImpl = fetch,
    timeoutMs = 30_000,
    maxResponseBytes = DEFAULT_MAX_JSON_BYTES,
    agentName,
  }) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.authBaseUrl = authBaseUrl.replace(/\/+$/, '');
    this.clientId = clientId;
    this.credentials = credentials;
    this.refreshCoordinator = refreshCoordinator;
    this.fetch = fetchImpl;
    this.timeoutMs = timeoutMs;
    this.maxResponseBytes = maxResponseBytes;
    this.refreshPromise = undefined;
    this.agentHeaders = agentRequestHeaders(agentName);
  }

  async post(path, body, options = {}) {
    const first = await this.requestOnce(path, body, options, options.expectedSession);
    if (!first.authExpired) return first.envelope;
    try { await this.refresh(first.refreshToken, first.session); }
    catch (error) { throw requestNotAccepted(error); }
    const second = await this.requestOnce(path, body, options, first.session);
    if (second.authExpired) {
      throw new SkillError('LOGIN_REQUIRED', '登录状态已失效', { data: { request_not_accepted: true } });
    }
    return second.envelope;
  }

  async requestOnce(path, body, { allowCodes = [], signal, formData = false } = {}, expectedSession) {
    let session;
    try {
      session = await this.credentials.getSession({ fresh: Boolean(expectedSession) });
      assertCredentialSession(session, expectedSession);
      if (!session.credentials) throw new SkillError('LOGIN_REQUIRED', '需要登录光鸭账号');
    } catch (error) {
      throw requestNotAccepted(error);
    }
    const credentials = session.credentials;
    const { response, envelope } = await requestWithTimeout(
      this.fetch,
      `${this.baseUrl}${path}`,
      {
        method: 'POST',
        headers: {
          ...this.agentHeaders,
          ...(formData ? {} : { 'Content-Type': 'application/json' }),
          Authorization: `Bearer ${credentials.accessToken}`,
          dt: '13',
        },
        body: formData ? body : JSON.stringify(body ?? {}),
        signal,
      },
      this.timeoutMs,
      async (currentResponse) => ({
        response: currentResponse,
        envelope: await parseEnvelope(currentResponse, this.maxResponseBytes),
      }),
    );
    if (response.status === 401 || (response.ok && AUTH_CODES.has(envelope.code))) {
      return { authExpired: true, envelope, refreshToken: credentials.refreshToken, session };
    }
    if (!response.ok || (envelope.code && !allowCodes.includes(envelope.code))) {
      const code = apiErrorCode(envelope.code);
      throw new SkillError(code, code === 'REMOTE_TARGET_CONFLICT'
        ? '远端已存在同名文件或目录，请改名或先清理'
        : envelope.msg || `HTTP ${response.status}`, {
        retryable: response.status === 429 || response.status >= 500,
        data: {
          api_code: envelope.code,
          api_message: envelope.msg || undefined,
          http_status: response.status,
          request_not_accepted: requestWasRejected(response, envelope.code) || undefined,
          content_trust: envelope.msg ? 'untrusted' : undefined,
        },
      });
    }
    return { authExpired: false, envelope };
  }

  async refresh(expectedRefreshToken, expectedSession = undefined) {
    expectedSession ??= await this.credentials.getSession();
    if (!this.refreshPromise) {
      this.refreshPromise = this.refreshNow(expectedRefreshToken, expectedSession).finally(() => {
        this.refreshPromise = undefined;
      });
    }
    await this.refreshPromise;
    assertCredentialSession(await this.credentials.getSession({ fresh: true }), expectedSession);
  }

  async refreshNow(expectedRefreshToken, expectedSession) {
    if (this.refreshCoordinator) {
      await this.refreshCoordinator.run(expectedRefreshToken, (current) =>
        this.requestRefresh(current.refreshToken), expectedSession,
      );
      return;
    }
    const session = await this.credentials.getSession({ fresh: true });
    assertCredentialSession(session, expectedSession);
    const current = session.credentials;
    if (!current) throw new SkillError('LOGIN_REQUIRED', '需要登录光鸭账号');
    if (current.refreshToken !== expectedRefreshToken) return;
    const tokens = await this.requestRefresh(current.refreshToken);
    assertCredentialSession(await this.credentials.getSession({ fresh: true }), session);
    await this.credentials.set({ ...tokens, accountId: current.accountId }, { generation: session.generation });
  }

  async requestRefresh(refreshToken) {
    const { response, data } = await requestWithTimeout(
      this.fetch,
      `${this.authBaseUrl}/v1/auth/token`,
      {
        method: 'POST',
        headers: {
          ...this.agentHeaders,
          'Content-Type': 'application/json',
          'x-client-id': this.clientId,
          'x-sdk-version': CLIENT_VERSION,
          'x-protocol-version': '301',
          'x-action': '401',
        },
        body: JSON.stringify({
          grant_type: 'refresh_token',
          refresh_token: refreshToken,
          client_id: this.clientId,
        }),
      },
      this.timeoutMs,
      async (currentResponse) => ({
        response: currentResponse,
        data: await parseBoundedJsonResponse(currentResponse, this.maxResponseBytes).catch((error) => {
          if (error?.code === 'API_RESPONSE_TOO_LARGE') throw error;
          return {};
        }),
      }),
    );
    if (response.status === 429 || response.status >= 500) {
      throw new SkillError('API_UNAVAILABLE', '账号服务暂时无法刷新登录状态', { retryable: true });
    }
    if ([401, 403].includes(response.status) ||
        ['invalid_grant', 'invalid_token'].includes(data.error) || AUTH_CODES.has(data.code)) {
      throw new SkillError('LOGIN_REQUIRED', '账号刷新失败');
    }
    if (!response.ok || typeof data.access_token !== 'string' || !data.access_token ||
        typeof data.refresh_token !== 'string' || !data.refresh_token) {
      throw new SkillError('INVALID_API_RESPONSE', '账号刷新响应无效，暂时无法验证登录状态', { retryable: true });
    }
    return {
      accessToken: data.access_token,
      refreshToken: data.refresh_token,
    };
  }
}

function requestWasRejected(response, apiCode) {
  if (response.status >= 500 || response.status === 408 || response.status === 499) return false;
  if (response.status >= 400) return true;
  return response.ok && REQUEST_REJECTION_CODES.has(Number(apiCode));
}

function requestNotAccepted(cause) {
  const failure = toSkillError(cause);
  return new SkillError(failure.code, failure.message, {
    retryable: failure.retryable,
    data: { ...failure.data, request_not_accepted: true },
    cause,
  });
}

async function requestWithTimeout(fetchImpl, url, init, timeoutMs, consume = async (response) => response) {
  const timeout = new AbortController();
  const timer = setTimeout(
    () => timeout.abort(new DOMException('Request timed out', 'TimeoutError')),
    timeoutMs,
  );
  const signal = init.signal
    ? AbortSignal.any([init.signal, timeout.signal])
    : timeout.signal;
  try {
    const response = await fetchImpl(url, { ...init, signal });
    const result = await consume(response);
    if (timeout.signal.aborted && !init.signal?.aborted) throw timeout.signal.reason;
    return result;
  } catch (cause) {
    if (timeout.signal.aborted && !init.signal?.aborted) {
      throw new SkillError('API_TIMEOUT', '服务端请求超时', {
        retryable: true,
        cause,
      });
    }
    if (init.signal?.aborted) throw cause;
    if (cause instanceof SkillError) throw cause;
    throw new SkillError('API_UNAVAILABLE', '服务端网络请求失败', {
      retryable: true,
      cause,
    });
  } finally {
    clearTimeout(timer);
  }
}

function apiErrorCode(code) {
  const numericCode = typeof code === 'number'
    ? code
    : typeof code === 'string' && /^\d{1,9}$/.test(code)
      ? Number(code)
      : undefined;
  if (Number.isSafeInteger(numericCode) && NUMERIC_API_ERROR_CODES.has(numericCode)) {
    return NUMERIC_API_ERROR_CODES.get(numericCode);
  }
  if (Number.isSafeInteger(numericCode) && numericCode >= 0) {
    return `REMOTE_API_ERROR_${numericCode}`;
  }
  return typeof code === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/.test(code)
    ? code
    : 'API_ERROR';
}

async function parseEnvelope(response, limit) {
  try {
    return await parseBoundedJsonResponse(response, limit);
  } catch (cause) {
    if (cause?.code === 'API_RESPONSE_TOO_LARGE') throw cause;
    throw new SkillError('INVALID_API_RESPONSE', '服务端响应不是合法 JSON', {
      retryable: response.status >= 500,
      cause,
    });
  }
}

export async function parseBoundedJsonResponse(response, limit = DEFAULT_MAX_JSON_BYTES) {
  const boundedLimit = Number.isSafeInteger(limit) && limit > 0 ? limit : DEFAULT_MAX_JSON_BYTES;
  const declared = Number(response.headers?.get?.('content-length'));
  if (Number.isFinite(declared) && declared > boundedLimit) {
    await response.body?.cancel?.().catch(() => {});
    throw responseTooLarge(boundedLimit, declared);
  }
  if (!response.body?.getReader) {
    const text = await response.text();
    const bytes = Buffer.byteLength(text);
    if (bytes > boundedLimit) throw responseTooLarge(boundedLimit, bytes);
    return JSON.parse(text);
  }
  const reader = response.body.getReader();
  const chunks = [];
  let bytes = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > boundedLimit) {
        await reader.cancel().catch(() => {});
        throw responseTooLarge(boundedLimit, bytes);
      }
      chunks.push(Buffer.from(value));
    }
  } finally {
    reader.releaseLock();
  }
  return JSON.parse(Buffer.concat(chunks, bytes).toString('utf8'));
}

function responseTooLarge(limit, observed) {
  return new SkillError('API_RESPONSE_TOO_LARGE', '服务端响应超过本机安全上限', {
    data: { response_limit: { limit, observed } },
  });
}

export class DriveApi {
  constructor(client) {
    this.client = client;
  }

  withRequestGuard(guard) {
    const client = this.client;
    return new DriveApi({
      credentials: client.credentials,
      post: (url, body, options = {}) => guard((session) =>
        client.post(url, body, { ...options, expectedSession: session })),
    });
  }

  list(operation) {
    const filesOnly = operation.remote_parent_id === '*' || Boolean(operation.file_types?.length);
    return this.client.post('/userres/v1/file/get_file_list', {
      parentId: operation.remote_parent_id,
      page: operation.page,
      pageSize: operation.page_size,
      dirType: 0,
      orderBy: filesOnly ? 3 : 0,
      sortType: filesOnly ? 1 : 0,
      fileTypes: operation.file_types ?? [],
      resType: filesOnly ? 1 : 0,
      needPlayRecord: false,
      needSubFolderStat: !filesOnly,
    });
  }

  search(operation) {
    return this.client.post('/userres/v1/file/search_files', {
      name: operation.keyword,
      ...(operation.remote_parent_id === undefined
        ? {}
        : { parentId: operation.remote_parent_id }),
      page: operation.page,
      pageSize: operation.page_size,
      fileTypes: operation.file_types ?? [],
    });
  }

  resolveCloudCollectionResource(operation, torrent) {
    if (torrent) {
      const form = new FormData();
      form.append('torrent', new Blob([torrent.data]), torrent.name);
      return this.client.post('/cloudcollection/v1/resolve_torrent', form, { formData: true });
    }
    return this.client.post('/cloudcollection/v1/resolve_res', {
      url: operation.url,
    });
  }

  createCloudCollectionTask(operation) {
    return this.client.post('/cloudcollection/v1/create_task', {
      url: operation.resolved_url || operation.url,
      parentId: operation.remote_parent_id,
      resType: operation.res_type,
      fileIndexes: operation.file_indexes ?? [],
      newName: operation.new_name || '',
    });
  }

  listCloudCollectionTasks(taskIds, options = {}) {
    return this.client.post('/cloudcollection/v1/list_task', {
      cursor: '',
      pageSize: Math.max(1, Math.min(taskIds.length, 100)),
      taskIds,
    }, options);
  }

  deleteCloudCollectionTasks(operation, options = {}) {
    // Always scope deletion to explicit IDs, never an account-wide selector.
    const ids = operation.task_ids;
    if (!Array.isArray(ids) || ids.length < 1 || ids.length > 100 ||
        ids.some((id) => typeof id !== 'string' || !id.trim() || id.length > 40) ||
        new Set(ids).size !== ids.length ||
        (operation.delete_files !== undefined && typeof operation.delete_files !== 'boolean')) {
      throw new SkillError('INVALID_REQUEST', '删除云添加任务需要 1–100 个不同的任务 ID 和布尔型 delete_files');
    }
    return this.client.post('/cloudcollection/v2/delete_task', {
      taskIds: ids,
      deleteFile: operation.delete_files ?? false,
    }, options);
  }

  async shareAccess(operation, parseShareReference) {
    const share = parseShareReference(operation.share_url, operation.code);
    const token = await sharePost(this.client, '/userres/v1/get_share_access_token', {
      shareId: share.shareId,
      code: share.code,
    });
    return { share, accessToken: token.data.accessToken };
  }

  getShareSummary(operation, parseShareReference) {
    const share = parseShareReference(operation.share_url, operation.code);
    return sharePost(this.client, '/userres/v1/get_share_summary', {
      shareId: share.shareId,
      needFileNum: false,
    });
  }

  async searchShare(operation, parseShareReference) {
    const { accessToken } = await this.shareAccess(operation, parseShareReference);
    return this.client.post('/userres/v1/share_page_search_files', {
      accessToken,
      keyword: operation.keyword,
      page: operation.page,
      pageSize: operation.page_size,
    });
  }

  async shareFileDetails(accessToken, fileIds) {
    const result = await this.client.post('/userres/v1/get_share_file_detail', {
      accessToken,
      fileIds,
    });
    return result.data?.list ?? [];
  }

  getInfo(fileId) {
    return this.client.post('/userres/v1/file/get_info_by_file_id', { fileId });
  }

  getAccountAssets() {
    return this.client.post('/assets/v1/get_assets', {
      needRights: true,
      needTrafficData: true,
    });
  }

  getAccountRights() {
    return this.client.post('/nd.bizassets.s/v1/get_user_rights', {});
  }

  downloadUrl(fileId, requestId) {
    return this.client.post('/userres/v1/get_res_download_url', {
      fileId,
      requestId,
    });
  }

  createFolder(parentId, name) {
    return this.client.post('/userres/v1/file/create_dir', {
      dirName: name,
      parentId,
      failIfNameExist: true,
    });
  }

  rename(fileId, newName) {
    return this.client.post('/userres/v1/file/rename', { fileId, newName });
  }

  copy(fileId, parentId) {
    return this.client.post('/userres/v1/file/copy_file', {
      fileIds: [fileId],
      parentId,
    });
  }

  move(fileId, parentId) {
    return this.client.post('/userres/v1/file/move_file', {
      fileIds: [fileId],
      parentId,
    });
  }

  async createShare(operation) {
    let title = operation.title;
    if (title === undefined) {
      const files = await Promise.all(operation.remote_ids.map(async (id) => {
        const result = await this.getInfo(id);
        return result.data?.fileInfo ?? result.data;
      }));
      title = createShareTitle(files);
    }
    const result = await this.client.post('/userres/v1/share_file', {
      title,
      fileIds: operation.remote_ids,
      shareType: operation.share_type,
      code: operation.code || '',
      validateDuration: operation.validity_seconds,
      maxRestoreCount: 0,
      downloadType: operation.download_type,
    });
    return { ...result, data: { ...result.data, title } };
  }

  async saveShare(operation, parseShareReference) {
    const { share, accessToken } = await this.shareAccess(operation, parseShareReference);
    let fileIds = operation.remote_ids;
    if (!fileIds?.length) fileIds = await this.shareRootIds(accessToken);
    const result = await this.client.post('/userres/v1/restore_share', {
      accessToken,
      fileIds,
      parentId: operation.target_parent_id,
      shareCode: share.shareCode || '',
    }, { allowCodes: [204] });
    if (Number(result.code) === 204) {
      throw new SkillError('SHARE_SELF_RESTORE', result.msg || '不能转存当前账号创建的分享', {
        data: { api_code: 204, content_trust: result.msg ? 'untrusted' : undefined },
      });
    }
    return result;
  }

  async shareRootIds(accessToken) {
    const ids = [];
    const cursors = new Set();
    let cursor = 0;
    while (true) {
      if (cursors.has(cursor)) throw new SkillError('INVALID_API_RESPONSE', '分享分页游标未推进');
      cursors.add(cursor);
      const page = await this.client.post('/userres/v1/get_share_page_files_list', {
        accessToken,
        cursor,
        pageSize: 100,
        orderBy: 0,
        sortType: 0,
      });
      const data = page.data || {};
      for (const item of data.list || []) ids.push(item.fileId);
      if (ids.length > 100) throw new SkillError('TOO_MANY_SHARE_ROOTS', '分享根目录超过 100 项，请显式指定 remote_ids');
      if (!data.hasMore) break;
      cursor = data.cursor;
    }
    if (!ids.length) {
      throw new SkillError('SHARE_CONTENT_UNAVAILABLE', '分享当前没有可见文件，可能尚未完成分享审核，请稍后重试', {
        retryable: true,
      });
    }
    return ids;
  }

  taskStatus(taskId, options = {}) {
    return this.client.post('/userres/v1/get_task_status', { taskId }, options);
  }
}

async function sharePost(client, path, body) {
  const result = await client.post(path, body, { allowCodes: [200, 201, 202, 203, 209] });
  if ([200, 201, 202].includes(Number(result.code))) {
    throw new SkillError('SHARE_INVALID', result.msg || '分享已失效', {
      data: { api_code: Number(result.code), content_trust: result.msg ? 'untrusted' : undefined },
    });
  }
  if ([203, 209].includes(Number(result.code))) {
    throw new SkillError('SHARE_CODE_INVALID', result.msg || '提取码错误', {
      data: { api_code: Number(result.code), content_trust: result.msg ? 'untrusted' : undefined },
    });
  }
  return result;
}

export class AccountAuthApi {
  constructor({
    authBaseUrl,
    clientId,
    credentials,
    fetchImpl = fetch,
    timeoutMs = 30_000,
    maxResponseBytes = DEFAULT_MAX_JSON_BYTES,
    agentName,
  }) {
    this.authBaseUrl = authBaseUrl.replace(/\/+$/, '');
    this.clientId = clientId;
    this.credentials = credentials;
    this.fetch = fetchImpl;
    this.timeoutMs = timeoutMs;
    this.maxResponseBytes = maxResponseBytes;
    this.agentHeaders = agentRequestHeaders(agentName);
  }

  async logout() {
    const credentials = await this.credentials.get();
    if (!credentials) return { code: 0, msg: '', data: {} };
    const { response, result } = await requestWithTimeout(
      this.fetch,
      `${this.authBaseUrl}/v1/auth/revoke`,
      {
        method: 'POST',
        headers: {
          ...this.agentHeaders,
          'Content-Type': 'application/json',
          'x-client-id': this.clientId,
          'x-sdk-version': CLIENT_VERSION,
          'x-protocol-version': '301',
        },
        body: JSON.stringify({
          token: credentials.refreshToken,
          token_type_hint: 'refresh_token',
        }),
      },
      this.timeoutMs,
      async (currentResponse) => ({
        response: currentResponse,
        result: currentResponse.status === 204
          ? { code: 0, msg: '', data: {} }
          : await parseBoundedJsonResponse(currentResponse, this.maxResponseBytes).catch((error) => {
            if (error?.code === 'API_RESPONSE_TOO_LARGE') throw error;
            return { code: 0, msg: '', data: {} };
          }),
      }),
    );
    if (!response.ok) {
      throw new SkillError('LOGOUT_FAILED', `账号服务退出失败: HTTP ${response.status}`, {
        retryable: response.status === 429 || response.status >= 500,
      });
    }
    if (result.code) {
      throw new SkillError('LOGOUT_FAILED', result.msg || '账号服务退出失败');
    }
    return result;
  }
}

// Android truncates the first selected name before adding the shared Web/Android count label.
const MAX_DEFAULT_NAME_LENGTH = 112;

function truncateToLength(value, maxLength) {
  const truncated = value.slice(0, maxLength);
  const lastCodeUnit = truncated.charCodeAt(truncated.length - 1);
  return lastCodeUnit >= 0xd800 && lastCodeUnit <= 0xdbff
    ? truncated.slice(0, -1)
    : truncated;
}

export function createShareTitle(files) {
  const firstName = files[0]?.fileName;
  if (typeof firstName !== 'string' || firstName.length === 0) {
    throw new SkillError('INVALID_API_RESPONSE', '分享文件缺少有效名称');
  }
  const name = truncateToLength(firstName, MAX_DEFAULT_NAME_LENGTH);
  return files.length === 1 ? name : `${name}等${files.length}个文件`;
}

export function createDriveApiClient(config, credentials, refreshCoordinator) {
  return new DriveApiClient({
    baseUrl: config.apiBaseUrl,
    authBaseUrl: config.authBaseUrl,
    clientId: config.clientId,
    agentName: config.agentName,
    credentials,
    refreshCoordinator,
    timeoutMs: config.apiRequestTimeoutMs,
    maxResponseBytes: config.maxApiResponseBytes,
  });
}
