import { act, render, screen, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AuthProvider, useAuth } from '../auth/AuthContext';
import { CustomerAuthProvider, useCustomerAuth } from './CustomerAuthContext';

const profile = { id: 'customer-a', tenant_id: 'tenant-a', email: 'a@example.com', tenant_name: '账户 A', csrf_token: 'csrf-A' };
const response = (payload: unknown = {}, status = 200) => new Response(status === 204 ? null : JSON.stringify(payload), { status });
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; };
let auth!: ReturnType<typeof useCustomerAuth>;
let cache!: ReturnType<typeof useSWRConfig>['cache'];
function Capture() {
  auth = useCustomerAuth(); cache = useSWRConfig().cache;
  const data = useSWR<{ name: string }>(auth.profile ? '/portal/probe' : null);
  return <><span>{auth.loading ? '加载' : auth.profile ? auth.profile.tenant_name : '未登录'}</span><span>{data.data?.name ?? '无数据'}</span></>;
}
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

describe('客户会话恢复与隔离', () => {
  it('刷新从GET session恢复CSRF，不访问secret storage，logout清cookie后新缓存且下一次刷新未登录', async () => {
    const storage = vi.spyOn(Storage.prototype, 'setItem');
    let active = true;
    const fetcher = vi.fn(async (url: string) => {
      if (url === '/api/portal/session') return active ? response(profile) : response({}, 401);
      if (url === '/api/portal/logout') { active = false; return response(null, 204); }
      return response({ name: '客户数据' });
    }); vi.stubGlobal('fetch', fetcher);
    const view = render(<CustomerAuthProvider><Capture /></CustomerAuthProvider>);
    expect(await screen.findByText('账户 A')).toBeInTheDocument();
    expect(await screen.findByText('客户数据')).toBeInTheDocument();
    const oldCache = cache;
    await act(async () => { await auth.client.request('/portal/keys', { method: 'POST' }); await auth.logout(); });
    expect(cache).not.toBe(oldCache); expect(screen.getByText('未登录')).toBeInTheDocument();
    const calls = fetcher.mock.calls as unknown as [string, RequestInit][];
    expect(calls.find(([url]) => url.endsWith('/logout'))?.[1].headers && new Headers(calls.find(([url]) => url.endsWith('/logout'))![1].headers).get('X-CSRF-Token')).toBe('csrf-A');
    expect(storage).not.toHaveBeenCalled(); expect([...cache.keys()].join(' ')).not.toContain('csrf-A');
    view.unmount(); render(<CustomerAuthProvider><Capture /></CustomerAuthProvider>);
    expect(await screen.findByText('未登录')).toBeInTheDocument();
  });
  it('每次login隔离SWR，取消旧读，迟到旧401不得清掉新客户', async () => {
    const late = deferred<Response>(); let probes = 0;
    const fetcher = vi.fn(async (url: string) => {
      if (url.endsWith('/session')) return response(profile);
      if (url.endsWith('/login')) return response({ ...profile, tenant_name: '账户 B', csrf_token: 'csrf-B' });
      if (url.endsWith('/probe')) { probes += 1; return probes === 1 ? late.promise : response({ name: '新客户数据' }); }
      return response();
    }); vi.stubGlobal('fetch', fetcher);
    render(<CustomerAuthProvider><Capture /></CustomerAuthProvider>);
    await screen.findByText('账户 A');
    const oldCache = cache;
    await act(async () => { await auth.login('b@example.com', 'private-password'); });
    expect(cache).not.toBe(oldCache); expect(await screen.findByText('新客户数据')).toBeInTheDocument();
    const calls = fetcher.mock.calls as unknown as [string, RequestInit][];
    expect(calls.find(([url]) => url.endsWith('/probe'))![1].signal?.aborted).toBe(true);
    await act(async () => { late.resolve(response({}, 401)); });
    expect(screen.getByText('账户 B')).toBeInTheDocument(); expect(oldCache.get('/portal/probe')?.data).toBeUndefined();
    await act(async () => { await auth.client.request('/portal/keys', { method: 'POST' }); });
    expect(new Headers(calls.at(-1)![1].headers).get('X-CSRF-Token')).toBe('csrf-B');
  });
  it('客户当前401清除客户缓存，但管理员仍登录；客户退出不会调用管理员logout', async () => {
    let admin!: ReturnType<typeof useAuth>;
    function Combined() { admin = useAuth(); return <CustomerAuthProvider><Capture /></CustomerAuthProvider>; }
    const fetcher = vi.fn(async (url: string) => url.endsWith('/session') && url.includes('/portal/') ? response(profile) : url.endsWith('/fail') ? response({}, 401) : response({ name: '客户数据' }));
    vi.stubGlobal('fetch', fetcher); render(<AuthProvider><Combined /></AuthProvider>);
    await screen.findByText('账户 A');
    await act(async () => { await admin.login('admin-memory'); });
    await screen.findByText('账户 A'); const previous = cache;
    await act(async () => { await expect(auth.client.request('/portal/fail')).rejects.toMatchObject({ status: 401 }); });
    expect(screen.getByText('未登录')).toBeInTheDocument(); expect(cache).not.toBe(previous); expect(admin.authenticated).toBe(true);
  });
  it('logout网络失败不谎称已退出，保留会话以便重试', async () => {
    const fetcher = vi.fn(async (url: string) => {
      if (url.endsWith('/session')) return response(profile);
      if (url.endsWith('/logout')) throw new Error('network-secret');
      return response();
    }); vi.stubGlobal('fetch', fetcher); render(<CustomerAuthProvider><Capture /></CustomerAuthProvider>);
    await screen.findByText('账户 A');
    await act(async () => { await expect(auth.logout()).rejects.toThrow('网络连接失败'); });
    expect(auth.profile?.tenant_name).toBe('账户 A'); expect(auth.logoutPending).toBe(false);
  });
  it.each(['/portal/activate', '/portal/activate/', '/PORTAL/ACTIVATE'])('StrictMode %s清理fragment、保留失败token，重试只用POST body', async (pathname) => {
    window.history.replaceState({ invitation: true }, '', `${pathname}?source=invite#token=one-time-activation`);
    const storage = vi.spyOn(Storage.prototype, 'setItem');
    let attempts = 0;
    const fetcher = vi.fn(async (url: string) => {
      if (url.endsWith('/session')) return response({}, 401);
      if (url.endsWith('/activate')) return ++attempts === 1 ? response({}, 503) : response(profile);
      return response();
    });
    vi.stubGlobal('fetch', fetcher);
    render(<StrictMode><CustomerAuthProvider><Capture /></CustomerAuthProvider></StrictMode>);
    expect(window.location.hash).toBe('');
    expect(window.location.pathname).toBe(pathname); expect(window.location.search).toBe('?source=invite');
    expect(window.history.state).toEqual({ invitation: true });
    await screen.findByText('未登录');
    expect(auth.activationToken).toBe('one-time-activation');
    await act(async () => { await expect(auth.activate(auth.activationToken, 'twelve-characters-password')).rejects.toMatchObject({ status: 503 }); });
    expect(auth.activationToken).toBe('one-time-activation');
    expect(window.location.hash).toBe('');
    await act(async () => { await auth.activate(auth.activationToken, 'twelve-characters-password'); });
    expect(auth.activationToken).toBe(''); expect(await screen.findByText('账户 A')).toBeInTheDocument();
    const calls = fetcher.mock.calls as unknown as [string, RequestInit][];
    const activations = calls.filter(([url]) => url === '/api/portal/activate');
    expect(activations).toHaveLength(2);
    for (const [, init] of activations) {
      expect(init.method).toBe('POST');
      expect(JSON.parse(String(init.body))).toEqual({ token: 'one-time-activation', password: 'twelve-characters-password' });
    }
    expect(calls.every(([url]) => !url.includes('one-time-activation') && !url.includes('token='))).toBe(true);
    expect(storage).not.toHaveBeenCalled(); expect([...cache.keys()].join(' ')).not.toContain('one-time-activation');
  });
  it('退出取消迟到login验证，旧profile不能恢复', async () => {
    const late = deferred<Response>();
    vi.stubGlobal('fetch', vi.fn(async (url: string) => url.endsWith('/session') ? response(profile) : url.endsWith('/login') ? late.promise : response(null, 204)));
    render(<CustomerAuthProvider><Capture /></CustomerAuthProvider>); await screen.findByText('账户 A');
    let login!: Promise<unknown>;
    await act(async () => { login = auth.login('a@example.com', 'password').catch((error: unknown) => error); await auth.logout(); });
    await act(async () => { late.resolve(response(profile)); });
    expect(await login).toMatchObject({ name: 'AbortError' }); expect(screen.getByText('未登录')).toBeInTheDocument();
    await waitFor(() => expect(auth.logoutPending).toBe(false));
  });
});
