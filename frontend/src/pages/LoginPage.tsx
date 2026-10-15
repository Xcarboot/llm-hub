import { LockOutlined, ReceiptLongOutlined, VisibilityOffOutlined, VisibilityOutlined } from '@mui/icons-material';
import { Alert, Box, Button, Card, CardContent, IconButton, InputAdornment, Stack, TextField, Typography } from '@mui/material';
import { FormEvent, useState } from 'react';
import { Link, Navigate, useNavigate } from 'react-router';
import { useAuth } from '../auth/AuthContext';

export function LoginPage() {
  const { authenticated, login } = useAuth();
  const navigate = useNavigate();
  const [token, setToken] = useState('');
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  if (authenticated) return <Navigate to="/" replace />;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      await login(token);
      setToken('');
      navigate('/', { replace: true });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '验证失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box sx={{ minHeight: '100vh', display: 'grid', placeItems: 'center', p: 2, background: 'radial-gradient(circle at 15% 10%, #eaf3ff 0, transparent 32%), #f7f9fc' }}>
      <Card sx={{ width: '100%', maxWidth: 430 }}>
        <CardContent sx={{ p: { xs: 3, sm: 5 } }}>
          <Stack alignItems="center" spacing={1} mb={4}>
            <Box sx={{ display: 'grid', placeItems: 'center', width: 52, height: 52, borderRadius: 2.5, bgcolor: 'primary.main', color: 'white' }}><ReceiptLongOutlined /></Box>
            <Typography variant="h5">LLM Gateway</Typography>
            <Typography color="text.secondary">管理员安全登录</Typography>
          </Stack>
          <Box component="form" onSubmit={submit}>
            <Stack spacing={2.5}>
              {error && <Alert severity="error">{error}</Alert>}
              <TextField
                label="管理员 Token"
                type={show ? 'text' : 'password'}
                value={token}
                onChange={(event) => setToken(event.target.value)}
                autoComplete="off"
                autoFocus
                fullWidth
                slotProps={{
                  input: {
                    startAdornment: <InputAdornment position="start"><LockOutlined fontSize="small" /></InputAdornment>,
                    endAdornment: <InputAdornment position="end"><IconButton aria-label={show ? '隐藏 Token' : '显示 Token'} onClick={() => setShow((value) => !value)} edge="end">{show ? <VisibilityOffOutlined /> : <VisibilityOutlined />}</IconButton></InputAdornment>
                  },
                  htmlInput: { 'data-form-type': 'other' }
                }}
              />
              <Button type="submit" size="large" variant="contained" disabled={busy || !token.trim()}>{busy ? '正在验证…' : '登录管理后台'}</Button>
              <Typography variant="caption" color="text.secondary" textAlign="center">Token 仅保存在当前页面内存，刷新页面即退出</Typography>
              <Button component={Link} to="/portal/login">客户门户登录</Button>
            </Stack>
          </Box>
        </CardContent>
      </Card>
    </Box>
  );
}
