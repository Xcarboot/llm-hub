import { AutoAwesomeOutlined, LockOutlined } from '@mui/icons-material';
import { Alert, Box, Button, Card, CardContent, Stack, TextField, Typography } from '@mui/material';
import { useRef, useState, type FormEvent } from 'react';
import { Link, Navigate } from 'react-router';
import { LoadingState } from '../components/Common';
import { useCustomerAuth } from './CustomerAuthContext';
import { safeError } from './api';

export function CustomerLoginPage({ activation = false }: { activation?: boolean }) {
  const auth = useCustomerAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const lock = useRef(false);
  if (auth.loading) return <LoadingState />;
  if (auth.profile) return <Navigate to="/portal" replace />;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (lock.current) return;
    setError('');
    if (activation && (password.length < 12 || password !== confirm)) { setError('密码至少 12 位，两次输入须一致'); return; }
    lock.current = true; setPending(true);
    try {
      if (activation) await auth.activate(auth.activationToken, password);
      else await auth.login(email, password);
      setPassword(''); setConfirm('');
    } catch (reason) { setError(safeError(reason, '验证失败，请稍后重试')); }
    finally { lock.current = false; setPending(false); }
  };
  return <Box sx={{ minHeight: '100vh', display: 'grid', placeItems: 'center', p: 2, background: 'radial-gradient(ellipse at top left, #d9f3ff, transparent 55%)' }}>
    <Card sx={{ width: '100%', maxWidth: 470 }}><CardContent sx={{ p: { xs: 3, sm: 5 } }}>
      <Stack alignItems="center" spacing={1} mb={3}><AutoAwesomeOutlined color="primary" sx={{ fontSize: 38 }} /><Typography variant="h5">{activation ? '激活客户账户' : '欢迎回到客户门户'}</Typography><Typography color="text.secondary">独立账户 · 安全会话 · 清晰用量</Typography></Stack>
      <Box component="form" onSubmit={submit}><Stack spacing={2.5}>
        {error && <Alert severity="error">{error}</Alert>}
        {activation ? <Alert severity={auth.activationToken ? 'info' : 'warning'}>{auth.activationToken ? '激活凭据已安全读取并从地址栏移除。请设置自己的密码；失败后可在本页重试。' : '缺少激活凭据。请向管理员索取新的激活链接；刷新后需重新打开链接。'}</Alert> : <TextField label="邮箱" type="email" autoComplete="username" required value={email} onChange={(event) => setEmail(event.target.value)} disabled={pending} fullWidth />}
        <TextField label={activation ? '设置密码（至少 12 位）' : '密码'} type="password" autoComplete={activation ? 'new-password' : 'current-password'} required value={password} onChange={(event) => setPassword(event.target.value)} disabled={pending} slotProps={{ htmlInput: { minLength: activation ? 12 : undefined, maxLength: 256 } }} fullWidth />
        {activation && <TextField label="确认密码" type="password" autoComplete="new-password" required value={confirm} onChange={(event) => setConfirm(event.target.value)} disabled={pending} fullWidth />}
        <Button startIcon={<LockOutlined />} type="submit" variant="contained" size="large" disabled={pending || (activation && !auth.activationToken)}>{pending ? '正在验证…' : activation ? '激活并进入门户' : '登录客户门户'}</Button>
        <Typography variant="caption" color="text.secondary">账户由管理员开通，无默认密码。登录凭据不会写入浏览器存储。</Typography>
        <Button component={Link} to={activation ? '/portal/login' : '/login'}>{activation ? '已有账户，前往登录' : '管理员登录入口'}</Button>
      </Stack></Box>
    </CardContent></Card>
  </Box>;
}
