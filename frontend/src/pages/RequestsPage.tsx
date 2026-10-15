import { Box, Card, FormControl, InputLabel, MenuItem, Select, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Tooltip, Typography } from '@mui/material';
import { useMemo, useState } from 'react';
import useSWR from 'swr';
import { queryString, swrFetcher } from '../api/client';
import { CursorPager, EmptyState, ErrorState, formatDate, LoadingState, PageHeader, StatusChip } from '../components/Common';
import type { GatewayRequest, PageResult } from '../types';

export function RequestsPage() {
  const [status, setStatus] = useState('');
  const [model, setModel] = useState('');
  const [tenant, setTenant] = useState('');
  const [cursor, setCursor] = useState<string>();
  const [history, setHistory] = useState<string[]>([]);
  const key = useMemo(() => `/admin/requests${queryString({ status: status || undefined, model: model || undefined, tenant_id: tenant || undefined, cursor })}`, [status, model, tenant, cursor]);
  const list = useSWR<PageResult<GatewayRequest>>(key, swrFetcher);
  const reset = () => { setCursor(undefined); setHistory([]); };
  const next = () => { if (list.data?.next_cursor) { setHistory((items) => [...items, cursor ?? '']); setCursor(list.data.next_cursor); } };
  const back = () => { const previous = history.at(-1); setHistory((items) => items.slice(0, -1)); setCursor(previous || undefined); };
  return <>
    <PageHeader title="请求" description="仅展示元数据、用量与错误；不读取或展示 prompt 内容" />
    <Card><Stack direction={{ xs: 'column', md: 'row' }} spacing={2} p={2}><FormControl size="small" sx={{ minWidth: 170 }}><InputLabel>状态</InputLabel><Select value={status} label="状态" onChange={(event) => { setStatus(event.target.value); reset(); }}><MenuItem value="">全部状态</MenuItem>{['PROCESSING', 'COMPLETED', 'FAILED'].map((value) => <MenuItem key={value} value={value}>{value}</MenuItem>)}</Select></FormControl><TextField label="模型" value={model} onChange={(event) => { setModel(event.target.value); reset(); }} /><TextField label="租户 ID" value={tenant} onChange={(event) => { setTenant(event.target.value); reset(); }} sx={{ minWidth: 280 }} /></Stack>
      {list.isLoading ? <LoadingState /> : list.error ? <Box p={2}><ErrorState error={list.error} /></Box> : <Box p={2} pt={0}><TableContainer><Table><TableHead><TableRow><TableCell>请求</TableCell><TableCell>租户</TableCell><TableCell>模型</TableCell><TableCell>状态</TableCell><TableCell>Usage</TableCell><TableCell>错误</TableCell><TableCell>时间</TableCell></TableRow></TableHead><TableBody>{list.data?.items.map((row) => <TableRow key={row.id}><TableCell><Typography variant="body2">{row.id}</Typography><Typography variant="caption" color="text.secondary">{row.stream ? '流式' : '非流式'}</Typography></TableCell><TableCell><Typography variant="body2">{row.tenant.name}</Typography><Typography variant="caption" color="text.secondary">{row.tenant.id}</Typography></TableCell><TableCell>{row.model}</TableCell><TableCell><StatusChip value={row.status} /></TableCell><TableCell><Usage usage={row.usage_json} /></TableCell><TableCell>{row.error_code ? <Tooltip title={row.error_code}><Typography color="error" noWrap maxWidth={160}>{row.error_code}</Typography></Tooltip> : '—'}</TableCell><TableCell>{formatDate(row.created_at)}</TableCell></TableRow>)}</TableBody></Table></TableContainer>{!list.data?.items.length && <EmptyState />}<CursorPager canBack={history.length > 0} canNext={Boolean(list.data?.next_cursor)} onBack={back} onNext={next} /></Box>}
    </Card>
  </>;
}

function Usage({ usage }: { usage?: Record<string, string | number | boolean> | null }) {
  if (!usage) return <>—</>;
  const allowed = ['prompt_tokens', 'completion_tokens', 'total_tokens'];
  return <Stack>{allowed.filter((key) => usage[key] !== undefined).map((key) => <Typography variant="caption" key={key}>{key}: {String(usage[key])}</Typography>)}</Stack>;
}
