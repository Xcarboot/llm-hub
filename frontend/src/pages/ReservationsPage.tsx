import { Box, Card, FormControl, InputLabel, MenuItem, Select, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography } from '@mui/material';
import { useMemo, useState } from 'react';
import useSWR from 'swr';
import { queryString, swrFetcher } from '../api/client';
import { CursorPager, EmptyState, ErrorState, formatDate, LoadingState, PageHeader, StatusChip } from '../components/Common';
import type { PageResult, Reservation } from '../types';
import { formatMicroCny } from '../utils/money';

export function ReservationsPage() {
  const [status, setStatus] = useState('');
  const [tenant, setTenant] = useState('');
  const [cursor, setCursor] = useState<string>();
  const [history, setHistory] = useState<string[]>([]);
  const key = useMemo(() => `/admin/reservations${queryString({ status: status || undefined, tenant_id: tenant || undefined, cursor })}`, [status, tenant, cursor]);
  const list = useSWR<PageResult<Reservation>>(key, swrFetcher);
  const reset = () => { setCursor(undefined); setHistory([]); };
  const next = () => { if (list.data?.next_cursor) { setHistory((items) => [...items, cursor ?? '']); setCursor(list.data.next_cursor); } };
  const back = () => { const previous = history.at(-1); setHistory((items) => items.slice(0, -1)); setCursor(previous || undefined); };
  return <>
    <PageHeader title="预占" description="追踪请求资金预占、到期时间与绑定价格版本" />
    <Card><Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} p={2}><FormControl size="small" sx={{ minWidth: 200 }}><InputLabel>预占状态</InputLabel><Select value={status} label="预占状态" onChange={(event) => { setStatus(event.target.value); reset(); }}><MenuItem value="">全部状态</MenuItem>{['NEW', 'RESERVED', 'DISPATCHED', 'SETTLED', 'CONSERVATIVE_SETTLED', 'RELEASED', 'REVERSED'].map((value) => <MenuItem key={value} value={value}>{value}</MenuItem>)}</Select></FormControl><TextField label="租户 ID" value={tenant} onChange={(event) => { setTenant(event.target.value); reset(); }} sx={{ minWidth: 300 }} /></Stack>
      {list.isLoading ? <LoadingState /> : list.error ? <Box p={2}><ErrorState error={list.error} /></Box> : <Box p={2} pt={0}><TableContainer><Table><TableHead><TableRow><TableCell>预占 ID</TableCell><TableCell>租户</TableCell><TableCell>状态</TableCell><TableCell align="right">金额</TableCell><TableCell>价格版本</TableCell><TableCell>过期时间</TableCell><TableCell>创建时间</TableCell></TableRow></TableHead><TableBody>{list.data?.items.map((row) => <TableRow key={row.id}><TableCell><Typography variant="body2">{row.id}</Typography><Typography variant="caption" color="text.secondary">请求 {row.request_id}</Typography></TableCell><TableCell>{row.tenant_id}</TableCell><TableCell><StatusChip value={row.status} /></TableCell><TableCell align="right">{formatMicroCny(row.amount_micro_cny)}</TableCell><TableCell>{row.selected_price?.id ?? '待绑定'}</TableCell><TableCell>{formatDate(row.expires_at)}</TableCell><TableCell>{formatDate(row.created_at)}</TableCell></TableRow>)}</TableBody></Table></TableContainer>{!list.data?.items.length && <EmptyState />}<CursorPager canBack={history.length > 0} canNext={Boolean(list.data?.next_cursor)} onBack={back} onNext={next} /></Box>}
    </Card>
  </>;
}
