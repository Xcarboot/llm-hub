import { Box, Card, Drawer, FormControl, InputLabel, MenuItem, Select, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography } from '@mui/material';
import { useMemo, useState } from 'react';
import useSWR from 'swr';
import { queryString, swrFetcher } from '../api/client';
import { CursorPager, EmptyState, ErrorState, formatDate, LoadingState, PageHeader, StatusChip } from '../components/Common';
import type { LedgerTransfer, PageResult } from '../types';
import { formatMicroCny } from '../utils/money';

export function LedgerPage() {
  const [kind, setKind] = useState('');
  const [tenant, setTenant] = useState('');
  const [cursor, setCursor] = useState<string>();
  const [history, setHistory] = useState<string[]>([]);
  const [selected, setSelected] = useState<LedgerTransfer | null>(null);
  const key = useMemo(() => `/admin/ledger/transfers${queryString({ kind: kind || undefined, tenant_id: tenant || undefined, cursor })}`, [kind, tenant, cursor]);
  const list = useSWR<PageResult<LedgerTransfer>>(key, swrFetcher);
  const reset = () => { setCursor(undefined); setHistory([]); };
  const next = () => { if (list.data?.next_cursor) { setHistory((items) => [...items, cursor ?? '']); setCursor(list.data.next_cursor); } };
  const back = () => { const value = history.at(-1); setHistory((items) => items.slice(0, -1)); setCursor(value || undefined); };

  return <>
    <PageHeader title="账本" description="查看不可变资金流水与双重记账分录" />
    <Card><Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} p={2}><FormControl size="small" sx={{ minWidth: 180 }}><InputLabel>流水类型</InputLabel><Select value={kind} label="流水类型" onChange={(event) => { setKind(event.target.value); reset(); }}><MenuItem value="">全部类型</MenuItem>{['RECHARGE', 'RESERVE', 'SETTLE', 'RELEASE', 'REVERSAL'].map((value) => <MenuItem key={value} value={value}>{value}</MenuItem>)}</Select></FormControl><TextField label="租户 ID" value={tenant} onChange={(event) => { setTenant(event.target.value); reset(); }} sx={{ minWidth: 300 }} /></Stack>
      {list.isLoading ? <LoadingState /> : list.error ? <Box p={2}><ErrorState error={list.error} /></Box> : <Box p={2} pt={0}><TableContainer><Table><TableHead><TableRow><TableCell>流水 ID</TableCell><TableCell>类型</TableCell><TableCell>外部引用</TableCell><TableCell>状态</TableCell><TableCell>时间</TableCell></TableRow></TableHead><TableBody>{list.data?.items.map((row) => <TableRow hover key={row.transfer.id} onClick={() => setSelected(row)} sx={{ cursor: 'pointer' }}><TableCell>{row.transfer.id}</TableCell><TableCell>{row.transfer.kind}</TableCell><TableCell>{row.transfer.external_ref}</TableCell><TableCell><StatusChip value={row.transfer.posted} /></TableCell><TableCell>{formatDate(row.transfer.created_at)}</TableCell></TableRow>)}</TableBody></Table></TableContainer>{!list.data?.items.length && <EmptyState />}<CursorPager canBack={history.length > 0} canNext={Boolean(list.data?.next_cursor)} onBack={back} onNext={next} /></Box>}
    </Card>
    <Drawer anchor="right" open={Boolean(selected)} onClose={() => setSelected(null)} PaperProps={{ sx: { width: { xs: '100%', sm: 620 }, p: 3 } }}>{selected && <Stack spacing={2}><Typography variant="h5">流水详情</Typography><Typography variant="body2" color="text.secondary">{selected.transfer.id}</Typography><Stack direction="row" spacing={1}><StatusChip value={selected.transfer.posted} /><StatusChip value={selected.transfer.kind} /></Stack><Typography>外部引用：{selected.transfer.external_ref}</Typography>{selected.reversal?.reversal_of_id && <Typography>冲正原流水：{selected.reversal.reversal_of_id}</Typography>}{selected.reversal?.reversed_by_id && <Typography>冲正流水：{selected.reversal.reversed_by_id}</Typography>}<Typography variant="h6" pt={2}>借贷分录</Typography><Table size="small"><TableHead><TableRow><TableCell>账户</TableCell><TableCell>类别</TableCell><TableCell align="right">金额</TableCell></TableRow></TableHead><TableBody>{selected.entries.map((entry) => <TableRow key={`${entry.account_scope}-${entry.amount_micro_cny}`}><TableCell>{entry.account_scope}</TableCell><TableCell>{entry.account_kind}</TableCell><TableCell align="right" sx={{ color: BigInt(entry.amount_micro_cny) < 0n ? 'error.main' : 'success.main' }}>{formatMicroCny(entry.amount_micro_cny)}</TableCell></TableRow>)}</TableBody></Table></Stack>}</Drawer>
  </>;
}
