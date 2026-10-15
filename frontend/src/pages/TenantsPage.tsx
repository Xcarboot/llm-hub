import { AddOutlined, ContentCopyOutlined, KeyOutlined, PaymentsOutlined, SearchOutlined, WarningAmberOutlined } from '@mui/icons-material';
import {
  Alert, Box, Button, Card, Dialog, DialogActions, DialogContent, DialogTitle, Drawer, FormControl,
  InputAdornment, InputLabel, MenuItem, Select, Stack, Table, TableBody, TableCell, TableContainer,
  TableHead, TableRow, TextField, ToggleButton, ToggleButtonGroup, Typography
} from '@mui/material';
import { FormEvent, useMemo, useRef, useState } from 'react';
import useSWR from 'swr';
import { apiRequest, queryString, swrFetcher } from '../api/client';
import { CursorPager, EmptyState, ErrorState, formatDate, LoadingState, PageHeader, StatusChip } from '../components/Common';
import type { ApiKeySummary, PageResult, Tenant } from '../types';
import { CustomerActivationDialog } from '../components/CustomerActivationDialog';
import { safeError } from '../customer/api';
import { formatMicroCny, parseMicroInput, yuanToMicro } from '../utils/money';

interface KeyResponse { id: string; prefix: string; api_key: string }

export function TenantsPage() {
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [cursor, setCursor] = useState<string>();
  const [history, setHistory] = useState<string[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createPending, setCreatePending] = useState(false);
  const [createError, setCreateError] = useState('');
  const createInFlight = useRef(false);
  const [refreshError, setRefreshError] = useState('');
  const [rechargeOpen, setRechargeOpen] = useState(false);
  const [keyResult, setKeyResult] = useState<{ tenantId: string; tenantName: string; secret: string } | null>(null);
  const [keyPending, setKeyPending] = useState(false);
  const [keyError, setKeyError] = useState('');
  // Keep the drawer locked until the one-time secret is explicitly dismissed.
  const keyLocked = useRef(false);
  const [activationOpen, setActivationOpen] = useState(false);
  const [revokeKey, setRevokeKey] = useState<ApiKeySummary | null>(null);
  const [revokePending, setRevokePending] = useState(false);
  const revokeLock = useRef(false);
  const drawerLocked = keyPending || keyResult !== null || activationOpen || revokeKey !== null || revokePending;
  const active = status === 'active' ? 'true' : status === 'inactive' ? 'false' : undefined;
  const listKey = useMemo(() => `/admin/tenants${queryString({ search: search || undefined, active, cursor })}`, [search, active, cursor]);
  const list = useSWR<PageResult<Tenant>>(listKey, swrFetcher);
  const detail = useSWR<Tenant>(selectedId ? `/admin/tenants/${selectedId}` : null, swrFetcher);
  const keys = useSWR<PageResult<ApiKeySummary>>(selectedId ? `/admin/tenants/${selectedId}/keys` : null, swrFetcher);

  const resetCursor = () => { setCursor(undefined); setHistory([]); };
  const next = () => {
    if (!list.data?.next_cursor) return;
    setHistory((value) => [...value, cursor ?? '']);
    setCursor(list.data.next_cursor);
  };
  const back = () => {
    const previous = history.at(-1);
    setHistory((value) => value.slice(0, -1));
    setCursor(previous || undefined);
  };

  const selectTenant = (tenantId: string | null) => {
    if (keyLocked.current || activationOpen || revokeKey || revokeLock.current) return;
    setKeyError('');
    setSelectedId(tenantId);
  };

  const closeCreate = () => {
    if (!createInFlight.current) setCreateOpen(false);
  };

  const createTenant = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (createInFlight.current) return;
    const form = new FormData(event.currentTarget);
    createInFlight.current = true;
    setCreatePending(true);
    setCreateError('');
    setRefreshError('');
    try {
      await apiRequest('/admin/tenants', { method: 'POST', body: { name: String(form.get('name')) } });
      // A refresh failure must not invite a second POST for an already-created tenant.
      try { await list.mutate(); }
      catch { setRefreshError('租户已创建，但列表刷新失败，请刷新页面查看。'); }
      setCreateOpen(false);
    } catch (reason) {
      setCreateError(reason instanceof Error ? reason.message : '创建租户失败');
    } finally {
      createInFlight.current = false;
      setCreatePending(false);
    }
  };

  const createKey = async () => {
    if (keyLocked.current || !selectedId || detail.data?.id !== selectedId) return;
    const tenant = { id: selectedId, name: detail.data.name };
    keyLocked.current = true;
    setKeyPending(true);
    setKeyError('');
    try {
      const result = await apiRequest<KeyResponse>(`/admin/tenants/${tenant.id}/keys`, { method: 'POST' });
      setKeyResult({ tenantId: tenant.id, tenantName: tenant.name, secret: result.api_key });
      // Preserve the secret even if refreshing key summaries fails.
      try { await keys.mutate(); }
      catch { setKeyError('Key 已创建，但列表刷新失败。请先保存一次性 Key。'); }
    } catch (reason) {
      keyLocked.current = false;
      setKeyError(reason instanceof Error ? reason.message : '创建 Key 失败');
    } finally {
      setKeyPending(false);
    }
  };

  const closeSecret = () => {
    if (keyPending) return;
    setKeyResult(null);
    keyLocked.current = false;
  };

  const confirmRevoke = async () => {
    if (revokeLock.current || !selectedId || !revokeKey) return;
    revokeLock.current = true; setRevokePending(true); setKeyError('');
    try {
      await apiRequest(`/admin/tenants/${encodeURIComponent(selectedId)}/keys/${encodeURIComponent(revokeKey.id)}/revoke`, { method: 'POST' });
      setRevokeKey(null);
      try { await keys.mutate(); } catch { setKeyError('撤销已受理，列表刷新失败，请刷新核对。'); }
    } catch (reason) { setKeyError(safeError(reason)); }
    finally { revokeLock.current = false; setRevokePending(false); }
  };

  return (
    <>
      <PageHeader title="租户与 API Key" description="管理租户余额、访问凭据与人工充值" action={<Button variant="contained" startIcon={<AddOutlined />} disabled={drawerLocked || createPending} onClick={() => { setCreateError(''); setCreateOpen(true); }}>创建租户</Button>} />
      {refreshError && <Alert severity="error" sx={{ mb: 2 }}>{refreshError}</Alert>}
      <Card>
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} p={2}>
          <TextField placeholder="搜索租户名称" value={search} onChange={(event) => { setSearch(event.target.value); resetCursor(); }} slotProps={{ input: { startAdornment: <InputAdornment position="start"><SearchOutlined /></InputAdornment> } }} sx={{ minWidth: 260 }} />
          <FormControl size="small" sx={{ minWidth: 160 }}><InputLabel>状态</InputLabel><Select value={status} label="状态" onChange={(event) => { setStatus(event.target.value); resetCursor(); }}><MenuItem value="">全部状态</MenuItem><MenuItem value="active">有效</MenuItem><MenuItem value="inactive">停用</MenuItem></Select></FormControl>
        </Stack>
        {list.isLoading ? <LoadingState /> : list.error ? <Box p={2}><ErrorState error={list.error} /></Box> : (
          <Box p={2} pt={0}>
            <TableContainer><Table><TableHead><TableRow><TableCell>租户</TableCell><TableCell>状态</TableCell><TableCell>余额</TableCell><TableCell>创建时间</TableCell><TableCell align="right">操作</TableCell></TableRow></TableHead><TableBody>
              {list.data?.items.map((tenant) => <TableRow hover key={tenant.id}><TableCell><Typography fontWeight={650}>{tenant.name}</Typography><Typography variant="caption" color="text.secondary">{tenant.id}</Typography></TableCell><TableCell><StatusChip value={tenant.active} /></TableCell><TableCell>{formatMicroCny(tenant.available_micro_cny)}</TableCell><TableCell>{formatDate(tenant.created_at)}</TableCell><TableCell align="right"><Button disabled={drawerLocked} onClick={() => selectTenant(tenant.id)}>查看详情</Button></TableCell></TableRow>)}
            </TableBody></Table></TableContainer>
            {!list.data?.items.length && <EmptyState />}
            <CursorPager canBack={history.length > 0} canNext={Boolean(list.data?.next_cursor)} onBack={back} onNext={next} />
          </Box>
        )}
      </Card>

      <Drawer anchor="right" open={Boolean(selectedId)} onClose={() => selectTenant(null)} PaperProps={{ sx: { width: { xs: '100%', sm: 560 }, p: 3 } }}>
        {keyError && <Alert severity="error" sx={{ mb: 2 }}>{keyError}</Alert>}
        {keyPending && <Alert severity="info" sx={{ mb: 2 }}>正在创建 Key，请等待响应。为避免丢失一次性 Key，暂时无法关闭或切换租户。</Alert>}
        {detail.isLoading ? <LoadingState /> : detail.error ? <ErrorState error={detail.error} /> : detail.data && <Stack spacing={3}>
          <Box><Typography variant="h5">{detail.data.name}</Typography><Typography color="text.secondary" variant="body2">{detail.data.id}</Typography></Box>
          <Card variant="outlined"><Box p={2}><Typography color="text.secondary">可用余额</Typography><Typography variant="h4" mt={1}>{formatMicroCny(detail.data.available_micro_cny)}</Typography><Typography color="text.secondary" mt={1}>冻结：{formatMicroCny(detail.data.frozen_micro_cny)}</Typography></Box></Card>
          <Stack direction="row" spacing={1}><Button variant="contained" startIcon={<KeyOutlined />} disabled={drawerLocked || detail.data.id !== selectedId} onClick={createKey}>{keyPending ? '创建中…' : '创建 Key'}</Button><Button variant="outlined" startIcon={<PaymentsOutlined />} disabled={drawerLocked} onClick={() => setRechargeOpen(true)}>充值</Button><Button variant="outlined" disabled={drawerLocked || detail.data.id !== selectedId} onClick={() => setActivationOpen(true)}>开通客户门户</Button><Button disabled={drawerLocked} onClick={() => selectTenant(null)}>关闭详情</Button></Stack>
          <Box><Typography variant="h6" mb={1.5}>API Key</Typography>{keys.error && <ErrorState error={keys.error} />}<Table size="small"><TableHead><TableRow><TableCell>前缀</TableCell><TableCell>状态</TableCell><TableCell>创建时间</TableCell><TableCell>操作</TableCell></TableRow></TableHead><TableBody>{keys.data?.items.map((key) => <TableRow key={key.id}><TableCell>{key.prefix}</TableCell><TableCell><StatusChip value={!key.revoked} /></TableCell><TableCell>{formatDate(key.created_at)}</TableCell><TableCell><Button color="error" disabled={drawerLocked || key.revoked} onClick={() => { setKeyError(''); setRevokeKey(key); }}>撤销</Button></TableCell></TableRow>)}</TableBody></Table>{!keys.data?.items.length && <EmptyState>尚未创建 Key</EmptyState>}</Box>
        </Stack>}
      </Drawer>

      <Dialog open={createOpen} onClose={closeCreate} fullWidth maxWidth="xs">
        <Box component="form" onSubmit={createTenant}>
          <DialogTitle>创建租户</DialogTitle>
          <DialogContent>
            {createError && <Alert severity="error" sx={{ mt: 1 }}>{createError}</Alert>}
            <TextField name="name" label="租户名称" required fullWidth autoFocus disabled={createPending} sx={{ mt: 1 }} inputProps={{ maxLength: 200 }} />
          </DialogContent>
          <DialogActions><Button disabled={createPending} onClick={closeCreate}>取消</Button><Button type="submit" variant="contained" disabled={createPending}>{createPending ? '创建中…' : '创建'}</Button></DialogActions>
        </Box>
      </Dialog>
      {selectedId && <CustomerActivationDialog key={selectedId} open={activationOpen} tenantId={selectedId} tenantName={detail.data?.name ?? selectedId} onClose={() => setActivationOpen(false)} />}
      <Dialog open={revokeKey !== null} onClose={() => !revokePending && setRevokeKey(null)}><DialogTitle>确认撤销租户 Key</DialogTitle><DialogContent><Typography>账户：{detail.data?.name}（{selectedId}）</Typography><Typography>Key：{revokeKey?.prefix}。撤销不可恢复，调用将立即失效。</Typography>{keyError && <Alert severity="error">{keyError}</Alert>}</DialogContent><DialogActions><Button disabled={revokePending} onClick={() => setRevokeKey(null)}>取消</Button><Button color="error" variant="contained" disabled={revokePending} onClick={() => void confirmRevoke()}>{revokePending ? '撤销中…' : '确认撤销'}</Button></DialogActions></Dialog>
      <RechargeDialog open={rechargeOpen} tenantId={selectedId} onClose={() => setRechargeOpen(false)} onSuccess={async () => { setRechargeOpen(false); await Promise.all([detail.mutate(), list.mutate()]); }} />
      <Dialog open={keyResult !== null} onClose={closeSecret} maxWidth="sm" fullWidth>
        <DialogTitle>请立即保存 API Key</DialogTitle>
        <DialogContent>
          <Stack spacing={2} pt={1}>
            <Alert severity="info">归属租户：{keyResult?.tenantName}（ID：{keyResult?.tenantId}）</Alert>
            <Alert severity="warning" icon={<WarningAmberOutlined />}>此 Key 仅展示一次。关闭后将从页面内存清除，无法再次查看。</Alert>
            <TextField label="API Key" value={keyResult?.secret ?? ''} slotProps={{ input: { readOnly: true } }} fullWidth />
            <Button startIcon={<ContentCopyOutlined />} onClick={() => keyResult && navigator.clipboard.writeText(keyResult.secret)} disabled={!keyResult}>复制 Key</Button>
          </Stack>
        </DialogContent>
        <DialogActions><Button variant="contained" disabled={keyPending} onClick={closeSecret}>我已保存，关闭</Button></DialogActions>
      </Dialog>
    </>
  );
}

function RechargeDialog({ open, tenantId, onClose, onSuccess }: { open: boolean; tenantId: string | null; onClose: () => void; onSuccess: () => Promise<void> }) {
  const [mode, setMode] = useState<'yuan' | 'micro'>('yuan');
  const [amount, setAmount] = useState('');
  const [externalRef, setExternalRef] = useState('');
  const [error, setError] = useState('');
  const submit = async () => {
    if (!tenantId) return;
    try {
      const micro = mode === 'yuan' ? yuanToMicro(amount) : parseMicroInput(amount);
      if (BigInt(micro) <= 0n) throw new Error('充值金额必须大于 0');
      await apiRequest('/admin/ledger/recharge', { method: 'POST', body: { tenant_id: tenantId, amount_micro_cny: micro, external_ref: externalRef } });
      setAmount(''); setExternalRef(''); setError('');
      await onSuccess();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '充值失败'); }
  };
  return <Dialog open={open} onClose={onClose} fullWidth maxWidth="xs"><DialogTitle>人工充值</DialogTitle><DialogContent><Stack spacing={2} pt={1}>{error && <Alert severity="error">{error}</Alert>}<ToggleButtonGroup exclusive size="small" value={mode} onChange={(_, value) => value && setMode(value)}><ToggleButton value="yuan">人民币</ToggleButton><ToggleButton value="micro">micro-CNY</ToggleButton></ToggleButtonGroup><TextField label={mode === 'yuan' ? '金额（元，最多 6 位小数）' : '金额（micro-CNY 整数）'} value={amount} onChange={(event) => setAmount(event.target.value)} required /><TextField label="外部幂等引用" value={externalRef} onChange={(event) => setExternalRef(event.target.value)} required inputProps={{ maxLength: 180 }} /></Stack></DialogContent><DialogActions><Button onClick={onClose}>取消</Button><Button variant="contained" onClick={submit}>确认充值</Button></DialogActions></Dialog>;
}
