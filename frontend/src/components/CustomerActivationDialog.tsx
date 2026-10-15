import { Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, TextField, Typography } from '@mui/material';
import { useRef, useState, type FormEvent } from 'react';
import { apiRequest } from '../api/client';
import { safeError } from '../customer/api';
import { formatDate } from './Common';

interface Activation { activation_url: string; expires_at: string; email: string; tenant_id: string }
export function CustomerActivationDialog({ open, tenantId, tenantName, onClose }: { open: boolean; tenantId: string; tenantName: string; onClose: () => void }) {
  const [email, setEmail] = useState('');
  const [result, setResult] = useState<Activation | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const lock = useRef(false);
  const close = () => {
    if (lock.current) return;
    setResult(null); setEmail(''); setError(''); onClose();
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (lock.current || result) return;
    lock.current = true; setPending(true); setError('');
    try {
      const response = await apiRequest<Activation>(`/admin/tenants/${encodeURIComponent(tenantId)}/customer-activation`, { method: 'POST', body: { email: email.trim() } });
      const url = new URL(response.activation_url, window.location.origin);
      if (!['http:', 'https:'].includes(url.protocol) || url.pathname !== '/portal/activate' || !new URLSearchParams(url.hash.slice(1)).get('token') || response.tenant_id !== tenantId) throw new Error('Invalid activation response');
      setResult(response); // This one-time credential never enters SWR or persistent storage.
    } catch (reason) { setError(safeError(reason, '激活链接生成失败，请核对邮箱后重试')); }
    finally { lock.current = false; setPending(false); }
  };
  return <Dialog open={open} onClose={close} fullWidth maxWidth="sm"><Box component="form" onSubmit={submit}><DialogTitle>开通客户门户</DialogTitle><DialogContent><Stack spacing={2} pt={1}>
    <Alert severity="info">账户：{tenantName}（{tenantId}）。客户通过链接自行设置至少 12 位密码，无默认密码。</Alert>
    {error && <Alert severity="error">{error}</Alert>}
    {result ? <><Alert severity="warning">此激活链接只展示一次，含一次性凭据。请通过安全渠道交给客户；关闭后清空，勿发送到公共群。</Alert><Typography>客户邮箱：{result.email} · 到期：{formatDate(result.expires_at)}</Typography><TextField label="一次性激活链接" value={result.activation_url} fullWidth slotProps={{ input: { readOnly: true } }} /><Button onClick={() => void navigator.clipboard.writeText(result.activation_url).catch(() => setError('复制失败，请手动复制链接'))}>复制激活链接</Button></> : <TextField label="客户邮箱" type="email" required value={email} disabled={pending} onChange={(event) => setEmail(event.target.value)} autoComplete="off" slotProps={{ htmlInput: { maxLength: 254 } }} />}
  </Stack></DialogContent><DialogActions><Button disabled={pending} onClick={close}>{result ? '已安全交付，关闭' : '取消'}</Button>{!result && <Button variant="contained" type="submit" disabled={pending}>{pending ? '生成中…' : '生成激活链接'}</Button>}</DialogActions></Box></Dialog>;
}
