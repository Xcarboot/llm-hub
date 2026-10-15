import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiRequest, configureApiAuth, invalidateApiSession } from './client';

function mockJson(text: string) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(text, { status: 200 })));
}

afterEach(() => {
  configureApiAuth(() => null, () => undefined);
  vi.unstubAllGlobals();
});

describe('无损 API 金额解析', () => {
  it('租户名称中包含 JSON 金额片段时不修改业务字符串', async () => {
    const name = '租户 "amount_micro_cny":123';
    mockJson(JSON.stringify({ name, amount_micro_cny: 86, notes: '{"balance_micro_cny":-99}' }));
    expect(await apiRequest('/admin/tenants')).toEqual({
      name, amount_micro_cny: '86', notes: '{"balance_micro_cny":-99}'
    });
  });

  it('精确保留大于 MAX_SAFE_INTEGER 的金额，普通安全整数仍为 number', async () => {
    mockJson('{"amount_micro_cny":9007199254740993,"charged_micro_cny":9223372036854775807,"request_count":123,"safe_limit":9007199254740991,"success_rate":0.99}');
    expect(await apiRequest('/admin/overview')).toEqual({
      amount_micro_cny: '9007199254740993', charged_micro_cny: '9223372036854775807',
      request_count: 123, safe_limit: Number.MAX_SAFE_INTEGER, success_rate: 0.99
    });
  });

  it('递归解析 nested ledger entries 中的正负金额，不改变已有字符串', async () => {
    mockJson('{"items":[{"amount_micro_cny":9007199254740993,"entries":[{"amount_micro_cny":-9007199254740993,"metadata":{"available_micro_cny":86}},{"amount_micro_cny":"1900000","sequence":2}]}],"total":1}');
    expect(await apiRequest('/admin/ledger')).toEqual({
      items: [{
        amount_micro_cny: '9007199254740993',
        entries: [
          { amount_micro_cny: '-9007199254740993', metadata: { available_micro_cny: '86' } },
          { amount_micro_cny: '1900000', sequence: 2 }
        ]
      }], total: 1
    });
  });

  it('非金额大整数也不被浮点舍入', async () => {
    mockJson('{"id":9007199254740993,"count":12}');
    expect(await apiRequest('/admin/example')).toEqual({ id: 9007199254740993n, count: 12 });
  });
});

describe('在途 API 会话控制', () => {
  it('读取响应体期间换会话，迟到数据也必须丢弃', async () => {
    let resolve!: (value: string) => void;
    const text = new Promise<string>((done) => { resolve = done; });
    let bodyStarted!: () => void;
    const started = new Promise<void>((done) => { bodyStarted = done; });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      status: 200, ok: true, text: () => { bodyStarted(); return text; }
    }));
    const request = apiRequest('/admin/tenants').catch((error: unknown) => error);
    await started;
    invalidateApiSession();
    resolve('{"name":"旧租户"}');
    expect(await request).toMatchObject({ name: 'AbortError' });
  });

  it('保留调用方的 AbortSignal，取消不伪装成网络错误', async () => {
    const external = new AbortController();
    vi.stubGlobal('fetch', vi.fn().mockImplementation((_url, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('cancelled', 'AbortError')));
    })));
    const request = apiRequest('/admin/tenants', { signal: external.signal }).catch((error: unknown) => error);
    external.abort();
    expect(await request).toMatchObject({ name: 'AbortError' });
  });

  it('候选 Token 验证的 401 不触发当前会话退出', async () => {
    const unauthorized = vi.fn();
    configureApiAuth(() => 'active-memory-token', unauthorized);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })));
    await expect(apiRequest('/admin/session', { token: 'invalid-candidate' })).rejects.toMatchObject({ status: 401 });
    expect(unauthorized).not.toHaveBeenCalled();
  });
});
