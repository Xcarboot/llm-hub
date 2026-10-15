import { isInteger, isLosslessNumber, parse } from 'lossless-json';

const API_BASE = '/api';

type UnauthorizedHandler = () => void;
type TokenGetter = () => string | null;

let getToken: TokenGetter = () => null;
let handleUnauthorized: UnauthorizedHandler = () => undefined;
let sessionGeneration = 0;
const pendingRequests = new Set<AbortController>();

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;

  constructor(status: number, message: string, code?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

export function invalidateApiSession(): void {
  sessionGeneration += 1;
  for (const controller of pendingRequests) controller.abort();
  pendingRequests.clear();
}

export function configureApiAuth(tokenGetter: TokenGetter, unauthorized: UnauthorizedHandler): void {
  invalidateApiSession();
  getToken = tokenGetter;
  handleUnauthorized = unauthorized;
}

interface ApiOptions extends Omit<RequestInit, 'body'> {
  body?: unknown;
  token?: string;
  skipAuth?: boolean;
}

export function parseResponse(text: string): unknown {
  return parse(text, (key, value) => {
    if (!isLosslessNumber(value)) return value;
    if (key.includes('micro_cny')) return BigInt(value.value).toString();
    const number = Number(value.value);
    // Preserve other large integers too, while keeping ordinary numeric fields as numbers.
    return isInteger(value.value) && !Number.isSafeInteger(number) ? BigInt(value.value) : number;
  });
}

export async function apiRequest<T>(path: string, options: ApiOptions = {}): Promise<T> {
  const { body, token: explicitToken, skipAuth = false, headers, signal, ...init } = options;
  const generation = sessionGeneration;
  const unauthorized = handleUnauthorized;
  const token = explicitToken ?? getToken();
  const controller = new AbortController();
  const abort = () => controller.abort();
  const assertCurrentSession = () => {
    if (generation !== sessionGeneration || controller.signal.aborted) {
      throw new DOMException('请求所属会话已结束', 'AbortError');
    }
  };
  if (signal?.aborted) controller.abort();
  else signal?.addEventListener('abort', abort, { once: true });
  pendingRequests.add(controller);

  try {
    assertCurrentSession();
    const requestHeaders = new Headers(headers);
    requestHeaders.set('Accept', 'application/json');
    if (body !== undefined) requestHeaders.set('Content-Type', 'application/json');
    if (!skipAuth && token) requestHeaders.set('X-Admin-Token', token);

    let response: Response;
    try {
      response = await fetch(`${API_BASE}${path}`, {
        ...init,
        signal: controller.signal,
        headers: requestHeaders,
        body: body === undefined ? undefined : JSON.stringify(body)
      });
    } catch {
      assertCurrentSession();
      throw new ApiError(0, '网络连接失败，请稍后重试');
    }

    // Also discard late results from transports that ignore AbortController.
    assertCurrentSession();
    if (response.status === 401) {
      // A failed candidate login or public request must not invalidate an active session.
      if (explicitToken === undefined && !skipAuth && token) unauthorized();
      throw new ApiError(401, '管理员会话已失效');
    }

    if (!response.ok) {
      // Server error bodies may contain credentials or internal details; never render them.
      const message = response.status === 503 ? '服务尚未启用或暂不可用，请稍后重试' : `请求失败（${response.status}），请核对输入或稍后重试`;
      assertCurrentSession();
      throw new ApiError(response.status, message);
    }

    if (response.status === 204) return undefined as T;
    let responseText: string;
    try { responseText = await response.text(); }
    catch { assertCurrentSession(); throw new ApiError(0, '响应读取失败，请刷新记录确认操作结果'); }
    assertCurrentSession();
    try { return responseText ? parseResponse(responseText) as T : undefined as T; }
    catch { throw new ApiError(0, '响应格式异常，请刷新记录确认操作结果'); }
  } finally {
    pendingRequests.delete(controller);
    signal?.removeEventListener('abort', abort);
  }
}

export const swrFetcher = <T>(path: string): Promise<T> => apiRequest<T>(path);

export function queryString(values: Record<string, string | undefined>): string {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  const encoded = params.toString();
  return encoded ? `?${encoded}` : '';
}
