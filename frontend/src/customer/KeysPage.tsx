import { Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, Table, TableBody, TableCell, TableContainer, TableHead, TableRow } from '@mui/material';
import { useEffect, useRef, useState } from 'react';
import { formatDate, PageHeader, StatusChip } from '../components/Common';
import { OneTimeKeyDialog } from '../components/OneTimeKeyDialog';
import type { ApiKeySummary } from '../types';
import { useCustomerAuth } from './CustomerAuthContext';
import { safeError } from './api';
import { ListCard, useCursorPage } from './shared';

export function CustomerKeysPage() {
  const { client } = useCustomerAuth();
  const list = useCursorPage<ApiKeySummary>('/portal/keys');
  const [secret, setSecret] = useState<string | null>(null);
  const [selected, setSelected] = useState<ApiKeySummary | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const lock = useRef(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const create = async () => {
    if (lock.current || secret) return;
    lock.current = true; setPending(true); setError('');
    try {
      const result = await client.request<{ id: string; prefix: string; api_key: string }>('/portal/keys', { method: 'POST' });
      if (!alive.current) return;
      setSecret(result.api_key); // Never pass the full response into SWR.
      try { await list.mutate(); } catch { setError('Key 已创建，列表刷新失败。请先保存一次性 Key。'); }
    } catch (reason) { if (alive.current) setError(safeError(reason, '创建 Key 失败，请稍后重试')); }
    finally { lock.current = false; if (alive.current) setPending(false); }
  };
  const revoke = async () => {
    if (lock.current || !selected) return;
    lock.current = true; setPending(true); setError('');
    try {
      await client.request(`/portal/keys/${encodeURIComponent(selected.id)}/revoke`, { method: 'POST' });
      setSelected(null);
      try { await list.mutate(); } catch { setError('撤销请求已完成，列表刷新失败，请刷新核对。'); }
    } catch (reason) { setError(safeError(reason)); }
    finally { lock.current = false; setPending(false); }
  };
  return <><PageHeader title="API Key" description="凭据仅展示一次；撤销后不可恢复" action={<Button variant="contained" onClick={() => void create()} disabled={pending || secret !== null || selected !== null}>{pending && !selected ? '创建中…' : '创建 Key'}</Button>} />
    {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
    <ListCard list={list}><TableContainer><Table><TableHead><TableRow><TableCell>前缀</TableCell><TableCell>状态</TableCell><TableCell>创建时间</TableCell><TableCell>操作</TableCell></TableRow></TableHead><TableBody>{list.data?.items.map((row) => <TableRow key={row.id}><TableCell>{row.prefix}</TableCell><TableCell><StatusChip value={!row.revoked} /></TableCell><TableCell>{formatDate(row.created_at)}</TableCell><TableCell><Button color="error" disabled={pending || row.revoked || secret !== null} onClick={() => { setError(''); setSelected(row); }}>撤销</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer></ListCard>
    <OneTimeKeyDialog open={secret !== null} secret={secret} onClose={() => setSecret(null)} />
    <Dialog open={selected !== null} onClose={() => !pending && setSelected(null)}><DialogTitle>确认撤销 API Key</DialogTitle><DialogContent>将永久撤销 {selected?.prefix}，使用此 Key 的调用将立即失效。</DialogContent><DialogActions><Button disabled={pending} onClick={() => setSelected(null)}>取消</Button><Button color="error" variant="contained" disabled={pending} onClick={() => void revoke()}>{pending ? '撤销中…' : '确认撤销'}</Button></DialogActions></Dialog>
  </>;
}
