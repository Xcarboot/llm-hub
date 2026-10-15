import { createContext, useCallback, useContext, useLayoutEffect, useMemo, useRef, useState, type PropsWithChildren } from 'react';
import { SWRConfig } from 'swr';
import { apiRequest, configureApiAuth, invalidateApiSession } from '../api/client';

interface AuthValue {
  token: string | null;
  authenticated: boolean;
  login: (token: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: PropsWithChildren) {
  const [session, setSession] = useState({ token: null as string | null, generation: 0 });
  const tokenRef = useRef<string | null>(null);
  const generationRef = useRef(0);

  const clearSession = useCallback(() => {
    generationRef.current += 1;
    invalidateApiSession();
    tokenRef.current = null;
    setSession({ token: null, generation: generationRef.current });
  }, []);

  useLayoutEffect(() => {
    configureApiAuth(() => tokenRef.current, clearSession);
    return () => {
      generationRef.current += 1;
      tokenRef.current = null;
      configureApiAuth(() => null, () => undefined);
    };
  }, [clearSession]);

  const login = useCallback(async (candidate: string) => {
    const trimmed = candidate.trim();
    if (!trimmed) throw new Error('请输入管理员 Token');
    const generation = ++generationRef.current;
    invalidateApiSession();
    await apiRequest('/admin/session', { method: 'GET', token: trimmed });
    if (generation !== generationRef.current) {
      throw new DOMException('登录请求已过期', 'AbortError');
    }
    invalidateApiSession();
    tokenRef.current = trimmed;
    setSession({ token: trimmed, generation });
  }, []);

  const value = useMemo<AuthValue>(() => ({
    token: session.token,
    authenticated: session.token !== null,
    login,
    logout: clearSession
  }), [session.token, login, clearSession]);
  // A new provider identity isolates SWR's cache AND its in-flight deduplication state.
  const swrConfig = useMemo(() => {
    const cache = new Map();
    return { provider: () => cache, shouldRetryOnError: false, revalidateOnFocus: false };
  }, [session.generation]);

  return <AuthContext.Provider value={value}>
    <SWRConfig key={session.generation} value={swrConfig}>{children}</SWRConfig>
  </AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth 必须在 AuthProvider 内使用');
  return value;
}
