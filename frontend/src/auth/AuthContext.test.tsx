import { act, render, screen } from '@testing-library/react';
import useSWR, { SWRConfig, useSWRConfig } from 'swr';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiRequest, swrFetcher } from '../api/client';
import { AuthProvider, useAuth } from './AuthContext';

const dataKey = '/admin/tenants';

function Harness() {
  const auth = useAuth();
  return <><span>{auth.authenticated ? '已登录' : '未登录'}</span><button onClick={() => void auth.login('memory-only-token')}>登录</button><button onClick={auth.logout}>退出</button><DataProbe /></>;
}

let currentCache: ReturnType<typeof useSWRConfig>['cache'];
function DataProbe() {
  const auth = useAuth();
  currentCache = useSWRConfig().cache;
  const { data } = useSWR<{ name: string }>(auth.authenticated ? dataKey : null, swrFetcher);
  return <span>{data?.name ?? '无数据'}</span>;
}

function renderAuth() {
  return render(<SWRConfig value={{ provider: () => new Map() }}><AuthProvider><Harness /></AuthProvider></SWRConfig>);
}

function response(payload: unknown = {}, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

async function login() {
  await act(async () => screen.getByRole('button', { name: '登录' }).click());
  expect(await screen.findByText('已登录')).toBeInTheDocument();
}

async function logout() {
  await act(async () => screen.getByRole('button', { name: '退出' }).click());
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('管理员会话', () => {
  it('Token 仅保存在 React 内存，不进入 URL 或 SWR key', async () => {
    const storageSpy = vi.spyOn(Storage.prototype, 'setItem');
    const fetchMock = vi.fn().mockImplementation(async () => response());
    vi.stubGlobal('fetch', fetchMock);
    renderAuth();
    await login();
    expect(storageSpy).not.toHaveBeenCalled();
    expect(document.cookie).toBe('');
    expect([...currentCache.keys()].every((key) => !key.includes('memory-only-token'))).toBe(true);
    expect(fetchMock.mock.calls.every(([url]) => !String(url).includes('memory-only-token'))).toBe(true);
  });

  it('当前 API 返回 401 时立即清空会话', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response())
      .mockResolvedValueOnce(response({ name: '当前租户' }))
      .mockResolvedValueOnce(response({}, 401));
    vi.stubGlobal('fetch', fetchMock);
    renderAuth();
    await login();
    await act(async () => { await expect(apiRequest('/admin/dashboard')).rejects.toThrow('会话已失效'); });
    expect(screen.getByText('未登录')).toBeInTheDocument();
  });

  it('退出取消在途请求，忽略不遵守 abort 的迟到响应', async () => {
    const late = deferred<Response>();
    const fetchMock = vi.fn().mockResolvedValueOnce(response()).mockReturnValueOnce(late.promise);
    vi.stubGlobal('fetch', fetchMock);
    renderAuth();
    await login();
    const oldCache = currentCache;
    const requestSignal = fetchMock.mock.calls[1][1].signal as AbortSignal;
    await logout();
    expect(requestSignal.aborted).toBe(true);
    expect(currentCache).not.toBe(oldCache);
    await act(async () => late.resolve(response({ name: '迟到旧租户' })));
    expect(screen.getByText('未登录')).toBeInTheDocument();
    expect(screen.queryByText('迟到旧租户')).not.toBeInTheDocument();
    expect(oldCache.get(dataKey)?.data).toBeUndefined();
    expect(currentCache.get(dataKey)?.data).toBeUndefined();
  });

  it('即使同 Token 再登录，也不复用上一会话的已缓存数据', async () => {
    const next = deferred<Response>();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response()).mockResolvedValueOnce(response({ name: '旧租户' }))
      .mockResolvedValueOnce(response()).mockReturnValueOnce(next.promise);
    vi.stubGlobal('fetch', fetchMock);
    renderAuth();
    await login();
    expect(await screen.findByText('旧租户')).toBeInTheDocument();
    const oldCache = currentCache;
    await login();
    expect(currentCache).not.toBe(oldCache);
    expect(screen.queryByText('旧租户')).not.toBeInTheDocument();
    expect(screen.getByText('无数据')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(4);
    await act(async () => next.resolve(response({ name: '新租户' })));
    expect(await screen.findByText('新租户')).toBeInTheDocument();
  });

  it('新登录不复用旧在途请求，迟到响应不得覆盖新数据', async () => {
    const late = deferred<Response>();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response()).mockReturnValueOnce(late.promise)
      .mockResolvedValueOnce(response()).mockResolvedValueOnce(response({ name: '新租户' }));
    vi.stubGlobal('fetch', fetchMock);
    renderAuth();
    await login();
    await logout();
    await login();
    expect(await screen.findByText('新租户')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(4);
    await act(async () => late.resolve(response({ name: '迟到旧租户' })));
    expect(screen.getByText('新租户')).toBeInTheDocument();
    expect(screen.queryByText('迟到旧租户')).not.toBeInTheDocument();
  });

  it('旧请求迟到 401 不得清除新登录', async () => {
    const late = deferred<Response>();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response()).mockResolvedValueOnce(response({ name: '旧租户' }))
      .mockReturnValueOnce(late.promise)
      .mockResolvedValueOnce(response()).mockResolvedValueOnce(response({ name: '新租户' }));
    vi.stubGlobal('fetch', fetchMock);
    renderAuth();
    await login();
    const oldRequest = apiRequest('/admin/old-request').catch((error: unknown) => error);
    await login();
    await act(async () => late.resolve(response({}, 401)));
    expect(await oldRequest).toMatchObject({ name: 'AbortError' });
    expect(screen.getByText('已登录')).toBeInTheDocument();
    expect(await screen.findByText('新租户')).toBeInTheDocument();
  });

  it('退出期间迟到的登录验证不能恢复会话', async () => {
    let auth!: ReturnType<typeof useAuth>;
    function Capture() { auth = useAuth(); return <Harness />; }
    const late = deferred<Response>();
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(late.promise));
    render(<AuthProvider><Capture /></AuthProvider>);
    const attempt = auth.login('candidate-token').catch((error: unknown) => error);
    await logout();
    await act(async () => late.resolve(response()));
    expect(await attempt).toMatchObject({ name: 'AbortError' });
    expect(screen.getByText('未登录')).toBeInTheDocument();
  });
});
