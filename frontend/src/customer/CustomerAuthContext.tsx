import { createContext, useCallback, useContext, useLayoutEffect, useMemo, useRef, useState, type PropsWithChildren } from 'react';
import { SWRConfig } from 'swr';
import { createCustomerClient, validateProfile, type CustomerProfile } from './api';

interface CustomerAuth {
  profile: CustomerProfile | null;
  loading: boolean;
  logoutPending: boolean;
  activationToken: string;
  login: (email: string, password: string) => Promise<void>;
  activate: (token: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  restore: () => Promise<void>;
  client: ReturnType<typeof createCustomerClient>;
}
const Context = createContext<CustomerAuth | null>(null);
// Match Router's default case-insensitive routing and optional trailing slashes.
const isActivationPath = (pathname: string) => pathname.replace(/\/+$/, '').toLowerCase() === '/portal/activate';

export function CustomerAuthProvider({ children }: PropsWithChildren) {
  const profileRef = useRef<CustomerProfile | null>(null);
  // Read without side effects (StrictMode may run initializers twice); scrub in the layout effect.
  const [activationToken, setActivationToken] = useState(() => isActivationPath(window.location.pathname) ? new URLSearchParams(window.location.hash.slice(1)).get('token') ?? '' : '');
  const epoch = useRef(0);
  const logoutLock = useRef(false);
  const [state, setState] = useState({ profile: null as CustomerProfile | null, loading: true, generation: 0 });
  const [logoutPending, setLogoutPending] = useState(false);
  const clearRef = useRef<() => void>(() => undefined);
  const [client] = useState(() => createCustomerClient(() => profileRef.current?.csrf_token ?? null, () => clearRef.current()));
  const clear = useCallback(() => {
    epoch.current += 1;
    client.invalidate();
    profileRef.current = null;
    setState({ profile: null, loading: false, generation: epoch.current });
  }, [client]);
  clearRef.current = clear;

  const establish = useCallback(async (path: string, body?: unknown, signal?: AbortSignal) => {
    if (logoutLock.current) throw new DOMException('正在退出', 'AbortError');
    const generation = ++epoch.current;
    client.invalidate();
    const result = validateProfile(await client.request<CustomerProfile>(path, { method: body ? 'POST' : 'GET', body, public: true, signal }));
    if (generation !== epoch.current) throw new DOMException('会话请求已过期', 'AbortError');
    client.invalidate();
    profileRef.current = result;
    setActivationToken('');
    setState({ profile: result, loading: false, generation });
  }, [client]);

  const restore = useCallback(async () => {
    try { await establish('/portal/session'); }
    catch (error) {
      if (!(error instanceof DOMException && error.name === 'AbortError')) clear();
      throw error;
    }
  }, [establish, clear]);

  useLayoutEffect(() => {
    if (isActivationPath(window.location.pathname) && window.location.hash) {
      window.history.replaceState(window.history.state, '', window.location.pathname + window.location.search);
    }
    const controller = new AbortController();
    void establish('/portal/session', undefined, controller.signal).catch((error) => {
      if (!controller.signal.aborted && !(error instanceof DOMException && error.name === 'AbortError')) clear();
    });
    return () => {
      controller.abort();
      epoch.current += 1;
      profileRef.current = null;
      client.invalidate();
    };
  }, [establish, clear, client]);

  const logout = useCallback(async () => {
    if (logoutLock.current) return;
    logoutLock.current = true;
    setLogoutPending(true);
    // Stop all reads first; do not claim logout until the cookie has been cleared by the server.
    client.invalidate();
    try {
      await client.request('/portal/logout', { method: 'POST' });
      clear();
    } finally {
      logoutLock.current = false;
      setLogoutPending(false);
    }
  }, [client, clear]);

  const config = useMemo(() => {
    const cache = new Map();
    return { provider: () => cache, fetcher: client.request, shouldRetryOnError: false, revalidateOnFocus: true };
  }, [client, state.generation]);
  const value = useMemo<CustomerAuth>(() => ({
    profile: state.profile, loading: state.loading, logoutPending, activationToken, client, restore, logout,
    login: (email, password) => establish('/portal/login', { email: email.trim(), password }),
    activate: (token, password) => establish('/portal/activate', { token, password })
  }), [state.profile, state.loading, logoutPending, activationToken, client, restore, logout, establish]);
  return <Context.Provider value={value}><SWRConfig key={state.generation} value={config}>{children}</SWRConfig></Context.Provider>;
}

export function useCustomerAuth() {
  const value = useContext(Context);
  if (!value) throw new Error('客户认证上下文不可用');
  return value;
}
