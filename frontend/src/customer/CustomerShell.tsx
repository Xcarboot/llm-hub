import { AccountBalanceWalletOutlined, AutoAwesomeOutlined, KeyOutlined, LogoutOutlined, MenuBookOutlined, PaymentsOutlined, ReceiptLongOutlined, TimelineOutlined } from '@mui/icons-material';
import { Alert, AppBar, Box, Button, Container, Stack, Toolbar, Typography, createTheme, ThemeProvider } from '@mui/material';
import { useState, type PropsWithChildren } from 'react';
import { NavLink, Navigate, Outlet } from 'react-router';
import { LoadingState } from '../components/Common';
import { useCustomerAuth } from './CustomerAuthContext';
import { safeError } from './api';

const portalTheme = createTheme({
  palette: { mode: 'light', primary: { main: '#087dbb' }, secondary: { main: '#089fa4' }, background: { default: '#f3f9fc', paper: '#ffffff' } },
  typography: { fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif', h4: { fontWeight: 750, fontSize: '1.8rem' }, h5: { fontWeight: 700 }, button: { textTransform: 'none', fontWeight: 600 } },
  shape: { borderRadius: 12 },
  components: { MuiCard: { styleOverrides: { root: { border: '1px solid #e0edf4', boxShadow: '0 4px 20px #0e749008' } } }, MuiButton: { defaultProps: { disableElevation: true } } }
});
export function CustomerTheme({ children }: PropsWithChildren) {
  return <ThemeProvider theme={portalTheme}><Box sx={{ minHeight: '100vh', bgcolor: 'background.default' }}>{children}</Box></ThemeProvider>;
}
const nav = [
  ['/portal', '账户总览', <AccountBalanceWalletOutlined />], ['/portal/keys', 'API Key', <KeyOutlined />],
  ['/portal/usage', '用量', <TimelineOutlined />], ['/portal/payments', '充值订单', <PaymentsOutlined />],
  ['/portal/refunds', '退款跟踪', <ReceiptLongOutlined />], ['/portal/docs', '接入文档', <MenuBookOutlined />]
] as const;
export function CustomerProtected() {
  const { profile, loading } = useCustomerAuth();
  return loading ? <LoadingState /> : profile ? <Outlet /> : <Navigate to="/portal/login" replace />;
}
export function CustomerShell() {
  const { profile, logout, logoutPending, restore } = useCustomerAuth();
  const [error, setError] = useState('');
  return <>
    <AppBar position="static" elevation={0} color="inherit" sx={{ borderBottom: '1px solid #dcebf3' }}><Toolbar sx={{ gap: 1.5, flexWrap: 'wrap', py: 1 }}>
      <AutoAwesomeOutlined color="primary" /><Box flexGrow={1}><Typography fontWeight={800}>LLM Gateway <Typography component="span" color="primary">客户门户</Typography></Typography><Typography variant="caption" color="text.secondary">{profile?.tenant_name} · {profile?.email}</Typography></Box>
      <Button size="small" onClick={() => { setError(''); void restore().catch((reason) => setError(safeError(reason))); }} disabled={logoutPending}>刷新安全会话</Button>
      <Button startIcon={<LogoutOutlined />} disabled={logoutPending} onClick={() => { setError(''); void logout().catch((reason) => setError(safeError(reason, '退出未完成，请重试；服务器会话仍可能有效'))); }}>{logoutPending ? '正在退出…' : '退出客户门户'}</Button>
    </Toolbar></AppBar>
    <Container maxWidth="lg" sx={{ py: 3 }}>
      <Stack component="nav" aria-label="客户导航" direction="row" flexWrap="wrap" gap={1} mb={3}>{nav.map(([path, label, icon]) => <Button key={path} component={NavLink} to={path} end={path === '/portal'} startIcon={icon} sx={{ color: 'text.secondary', '&.active': { bgcolor: '#dff2fb', color: 'primary.main' } }}>{label}</Button>)}</Stack>
      <Alert severity="info" sx={{ mb: 3 }}>安全提示：登录使用 HttpOnly 安全会话；API Key 仅展示一次，请勿分享。客户退出不影响管理员会话。</Alert>
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      <Outlet />
    </Container>
  </>;
}
