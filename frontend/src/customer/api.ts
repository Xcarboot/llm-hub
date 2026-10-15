import { ApiError, parseResponse } from '../api/client';

export interface CustomerProfile {
  id: string;
  tenant_id: string;
  email: string;
  tenant_name: string;
  csrf_token: string;
}

interface Options {
  method?: 'GET' | 'POST';
  body?: unknown;
  signal?: AbortSignal;
  public?: boolean;
}

export function safeError(error: unknown, fallback = '操作失败，请稍后重试'): string {
  return error instanceof ApiError ? error.message : fallback;
}

// Instance-scoped: never configures, reads or invalidates the administrator client.
export function createCustomerClient(getCsrf: () => string | null, onUnauthorized: () => void) {
  let generation = 0;
  const pending = new Set<AbortController>();
  const invalidate = () => {
    generation += 1;
    pending.forEach((controller) => controller.abort());
    pending.clear();
  };
  const request = async <T,>(path: string, options: Options = {}): Promise<T> => {
    if (!path.startsWith('/portal/')) throw new Error('客户 API 路径无效');
    const epoch = generation;
    const controller = new AbortController();
    const abort = () => controller.abort();
    const assertCurrent = () => {
      if (epoch !== generation || controller.signal.aborted) throw new DOMException('客户会话已结束', 'AbortError');
    };
    if (options.signal?.aborted) abort();
    else options.signal?.addEventListener('abort', abort, { once: true });
    pending.add(controller);
    try {
      assertCurrent();
      const method = options.method ?? 'GET';
      const headers = new Headers({ Accept: 'application/json' });
      if (options.body !== undefined) headers.set('Content-Type', 'application/json');
      if (method === 'POST' && !options.public) {
        const csrf = getCsrf();
        if (!csrf) throw new ApiError(401, '客户会话已失效，请重新登录');
        headers.set('X-CSRF-Token', csrf);
      }
      let response: Response;
      try {
        response = await fetch(`/api${path}`, {
          method, headers, credentials: 'same-origin', signal: controller.signal,
          body: options.body === undefined ? undefined : JSON.stringify(options.body)
        });
      } catch {
        assertCurrent();
        throw new ApiError(0, '网络连接失败，请稍后重试');
      }
      assertCurrent();
      if (!response.ok) {
        // Error bodies are never exposed; explicitly close their unread streams.
        // Otherwise Chromium can keep the fetch active until garbage collection.
        try { await response.body?.cancel(); } catch { /* Already aborted/closed. */ }
        assertCurrent();
      }
      if (response.status === 401) {
        if (!options.public) onUnauthorized();
        throw new ApiError(401, options.public ? '验证失败，请检查登录信息或激活链接' : '客户会话已失效，请重新登录');
      }
      if (!response.ok) {
        const messages: Record<number, string> = {
          403: '安全验证失败，请刷新会话后重试', 409: '当前状态不允许此操作，请刷新记录后重试',
          400: '请求未被接受，请检查输入或激活链接', 422: '输入不符合要求，请检查后重试',
          429: '请求过于频繁，请稍后重试', 503: '支付或退款服务尚未启用或暂不可用（503），未完成资金操作'
        };
        throw new ApiError(response.status, messages[response.status] ?? `请求失败（${response.status}），请稍后重试`);
      }
      if (response.status === 204) return undefined as T;
      let text: string;
      try { text = await response.text(); }
      catch { assertCurrent(); throw new ApiError(0, '响应读取失败，请刷新记录确认操作结果'); }
      assertCurrent();
      try { return (text ? parseResponse(text) : undefined) as T; }
      catch { throw new ApiError(0, '响应格式异常，请刷新记录确认操作结果'); }
    } finally {
      pending.delete(controller);
      options.signal?.removeEventListener('abort', abort);
    }
  };
  return { request, invalidate };
}

export function validateProfile(value: CustomerProfile): CustomerProfile {
  if (!value || !['id', 'tenant_id', 'email', 'tenant_name', 'csrf_token'].every((key) => typeof value[key as keyof CustomerProfile] === 'string') || !value.csrf_token) {
    throw new ApiError(0, '会话响应异常，请重新登录');
  }
  return value;
}
