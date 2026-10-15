import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiRequest, configureApiAuth } from '../api/client';
import { portalAmountToMicro, refundableWholeMicro } from '../utils/money';
import { createCustomerClient } from './api';

const response = (payload: unknown = {}, status = 200) => new Response(status === 204 ? null : JSON.stringify(payload), { status });
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; };
afterEach(() => { configureApiAuth(() => null, () => undefined); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('独立 cookie + CSRF 客户 client', () => {
  it('GET 和 POST 使用同源 cookie，写请求仅附内存 CSRF，不附管理员 Token', async () => {
    configureApiAuth(() => 'admin-secret', vi.fn());
    const fetcher = vi.fn().mockImplementation(async () => response()); vi.stubGlobal('fetch', fetcher);
    const client = createCustomerClient(() => 'csrf-memory', vi.fn());
    await client.request('/portal/balance');
    await client.request('/portal/keys', { method: 'POST' });
    await client.request('/portal/login', { method: 'POST', body: { email: 'a@example.com', password: 'private-password' }, public: true });
    for (const [url, options] of fetcher.mock.calls) {
      expect(options.credentials).toBe('same-origin'); expect(options.headers.has('X-Admin-Token')).toBe(false);
      expect(String(url)).not.toMatch(/secret|password|csrf-memory/);
    }
    expect(fetcher.mock.calls[0][1].headers.has('X-CSRF-Token')).toBe(false);
    expect(fetcher.mock.calls[1][1].headers.get('X-CSRF-Token')).toBe('csrf-memory');
    expect(fetcher.mock.calls[2][1].headers.has('X-CSRF-Token')).toBe(false);
  });
  it('客户401只通知客户；候选登录401不注销现有会话；admin401不通知客户', async () => {
    const admin = vi.fn(); const customer = vi.fn();
    configureApiAuth(() => 'admin-secret', admin);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ detail: 'secret-body' }, 401)));
    const client = createCustomerClient(() => 'csrf', customer);
    await expect(client.request('/portal/login', { method: 'POST', public: true })).rejects.toMatchObject({ status: 401 });
    expect(customer).not.toHaveBeenCalled();
    await expect(client.request('/portal/balance')).rejects.toMatchObject({ status: 401 });
    expect(customer).toHaveBeenCalledTimes(1); expect(admin).not.toHaveBeenCalled();
    await expect(apiRequest('/admin/session')).rejects.toMatchObject({ status: 401 });
    expect(admin).toHaveBeenCalledTimes(1); expect(customer).toHaveBeenCalledTimes(1);
  });
  it.each([401, 403, 503])('错误状态 %s 主动关闭未读取响应流，不回显内容', async (status) => {
    const cancel = vi.fn();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new TextEncoder().encode('private-error-body')); },
      cancel
    });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { status })));
    const client = createCustomerClient(() => 'csrf', vi.fn());
    await expect(client.request('/portal/session', { public: true })).rejects.toMatchObject({ status });
    expect(cancel).toHaveBeenCalledTimes(1);
  });
  it('关闭错误响应期间会话切换，旧401不得退出新会话', async () => {
    const closed = deferred<void>(); const started = deferred<void>(); const unauthorized = vi.fn();
    const stream = new ReadableStream<Uint8Array>({ cancel() { started.resolve(); return closed.promise; } });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { status: 401 })));
    const client = createCustomerClient(() => 'csrf', unauthorized);
    const request = client.request('/portal/balance').catch((error: unknown) => error);
    await started.promise; client.invalidate(); closed.resolve();
    expect(await request).toMatchObject({ name: 'AbortError' }); expect(unauthorized).not.toHaveBeenCalled();
  });
  it('代次改变取消在途请求；迟到401不触发新会话退出', async () => {
    const late = deferred<Response>(); const unauthorized = vi.fn();
    const fetcher = vi.fn().mockReturnValue(late.promise); vi.stubGlobal('fetch', fetcher);
    const client = createCustomerClient(() => 'csrf', unauthorized);
    const request = client.request('/portal/balance').catch((error: unknown) => error);
    client.invalidate();
    expect(fetcher.mock.calls[0][1].signal.aborted).toBe(true);
    late.resolve(response({}, 401));
    expect(await request).toMatchObject({ name: 'AbortError' }); expect(unauthorized).not.toHaveBeenCalled();
  });
  it('响应体读取期间旧代次失效也丢弃数据，取消不伪装成网络错误', async () => {
    const body = deferred<string>(); const started = deferred<void>();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, text: () => { started.resolve(); return body.promise; } }));
    const client = createCustomerClient(() => 'csrf', vi.fn());
    const request = client.request('/portal/keys').catch((error: unknown) => error);
    await started.promise; client.invalidate(); body.resolve('{"items":[{"id":"old"}]}');
    expect(await request).toMatchObject({ name: 'AbortError' });
  });
  it('无损金额解析共用可靠实现；错误绝不回显服务端secret', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response('{"available_micro_cny":9007199254740993,"nested":{"amount_micro_cny":-9223372036854775807},"count":25}'))
      .mockResolvedValueOnce(response({ detail: 'private-password X-Admin-Token sk-secret' }, 503));
    vi.stubGlobal('fetch', fetcher);
    const client = createCustomerClient(() => 'csrf', vi.fn());
    expect(await client.request('/portal/balance')).toEqual({ available_micro_cny: '9007199254740993', nested: { amount_micro_cny: '-9223372036854775807' }, count: 25 });
    await expect(client.request('/portal/payments')).rejects.toThrow('503');
  });
  it('无CSRF禁止写请求；不允许客户client访问admin', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    const client = createCustomerClient(() => null, vi.fn());
    await expect(client.request('/portal/logout', { method: 'POST' })).rejects.toMatchObject({ status: 401 });
    await expect(client.request('/admin/session')).rejects.toThrow('客户 API 路径无效');
    expect(fetcher).not.toHaveBeenCalled();
  });
});
describe('门户整分资金边界', () => {
  it('精确转换，不接受超1000元、零、负数、指数或分以下金额', () => {
    expect(portalAmountToMicro('999.99')).toBe('999990000'); expect(portalAmountToMicro('1000')).toBe('1000000000');
    for (const input of ['1000.01', '0', '-1', '1e2', '0.001', 'NaN']) expect(() => portalAmountToMicro(input)).toThrow();
    expect(() => portalAmountToMicro('1.01', '1009999')).toThrow();
    expect(refundableWholeMicro('9007199254740993')).toBe('9007199254740000');
  });
});
