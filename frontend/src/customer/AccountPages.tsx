import { Alert, Box, Button, Card, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography } from '@mui/material';
import { useState } from 'react';
import { Link } from 'react-router';
import useSWR from 'swr';
import { ErrorState, formatDate, LoadingState, PageHeader, StatusChip } from '../components/Common';
import type { GatewayRequest, LedgerTransfer, ModelRoute, PortalBalance } from '../types';
import { formatMicroCny } from '../utils/money';
import { useCustomerAuth } from './CustomerAuthContext';
import { ListCard, useCursorPage } from './shared';

export function CustomerHomePage() {
  const { profile } = useCustomerAuth();
  const balance = useSWR<PortalBalance>('/portal/balance', { refreshInterval: 15000 });
  const ledger = useCursorPage<LedgerTransfer>('/portal/ledger/transfers');
  return <>
    <PageHeader title={`你好，${profile?.tenant_name ?? '客户'}`} description="账户资金与最近账单，一目了然" action={<Button component={Link} to="/portal/payments" variant="contained">查看充值订单</Button>} />
    {balance.isLoading ? <LoadingState /> : balance.error ? <ErrorState error={balance.error} /> : balance.data && <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, 1fr)', md: 'repeat(4, 1fr)' }, gap: 2, mb: 3 }}>{([
      ['可用余额', balance.data.available_micro_cny], ['用量冻结', balance.data.frozen_micro_cny], ['退款冻结', balance.data.refund_frozen_micro_cny], ['账户总额', balance.data.total_micro_cny]
    ] as const).map(([label, amount]) => <Card key={label} sx={{ p: 2.5 }}><Typography color="text.secondary">{label}</Typography><Typography variant="h5" color="primary" mt={1}>{formatMicroCny(amount)}</Typography></Card>)}</Box>}
    <Typography variant="h6" mb={2}>资金流水</Typography>
    <ListCard list={ledger}><TableContainer><Table size="small"><TableHead><TableRow><TableCell>流水 / 时间</TableCell><TableCell>类型</TableCell><TableCell>记账金额</TableCell></TableRow></TableHead><TableBody>{ledger.data?.items.map((row) => <TableRow key={row.transfer.id}><TableCell>{row.transfer.id}<Typography variant="caption" display="block">{formatDate(row.transfer.created_at)}</Typography></TableCell><TableCell>{row.transfer.kind}</TableCell><TableCell>{row.entries.map((entry, index) => <Typography variant="body2" key={index}>{entry.account_kind}：{formatMicroCny(entry.amount_micro_cny)}</Typography>)}</TableCell></TableRow>)}</TableBody></Table></TableContainer></ListCard>
  </>;
}
export function CustomerUsagePage() {
  const [model, setModel] = useState('');
  const list = useCursorPage<GatewayRequest>('/portal/requests', { model: model || undefined });
  return <><PageHeader title="请求与用量" description="每页最多 25 条，仅显示请求元数据与 Token 用量，不展示对话内容" />
    <TextField label="筛选模型" value={model} onChange={(event) => { setModel(event.target.value); list.reset(); }} sx={{ mb: 2 }} />
    <ListCard list={list}><TableContainer><Table><TableHead><TableRow><TableCell>请求 / 时间</TableCell><TableCell>模型</TableCell><TableCell>状态</TableCell><TableCell>Token 用量</TableCell></TableRow></TableHead><TableBody>{list.data?.items.map((row) => <TableRow key={row.id}><TableCell>{row.id}<Typography variant="caption" display="block">{formatDate(row.created_at)} · {row.stream ? '流式' : '非流式'}</Typography></TableCell><TableCell>{row.model}</TableCell><TableCell><StatusChip value={row.status} /></TableCell><TableCell>{['prompt_tokens', 'completion_tokens', 'total_tokens'].filter((key) => row.usage_json?.[key] !== undefined).map((key) => <Typography variant="caption" key={key} display="block">{key}：{String(row.usage_json?.[key])}</Typography>)}</TableCell></TableRow>)}</TableBody></Table></TableContainer></ListCard>
  </>;
}
export function CustomerDocsPage() {
  const list = useCursorPage<ModelRoute>('/portal/models');
  return <><PageHeader title="快速接入" description="兼容 OpenAI API，以门户创建的 API Key 调用网关" />
    <Card sx={{ p: 3, mb: 3 }}><Stack spacing={2}>
      <Alert severity="warning">不要将 API Key 写入前端代码、URL、聊天消息或公共仓库；请仅在你的服务端安全使用。</Alert>
      <Typography>接口：POST /v1/chat/completions（网关服务地址）；鉴权：Authorization: Bearer &lt;你的 API Key&gt;</Typography>
      <Typography>请求字段：model、messages，可按需启用 stream。请求开始时预占余额，完成后按实际用量结算。</Typography>
      <Typography>请先在「API Key」创建凭据，再选择下方模型。登录会话与 API Key 是不同的凭据，不可混用。</Typography>
    </Stack></Card>
    <ListCard list={list}><TableContainer><Table><TableHead><TableRow><TableCell>模型</TableCell><TableCell>状态</TableCell><TableCell>输入 / 输出（每百万 Token）</TableCell></TableRow></TableHead><TableBody>{list.data?.items.map((row) => <TableRow key={row.id}><TableCell>{row.public_name}</TableCell><TableCell><StatusChip value={row.active} /></TableCell><TableCell>{row.prices?.map((price) => <Typography variant="body2" key={price.id}>{formatMicroCny(price.input_micro_cny_per_million)} / {formatMicroCny(price.output_micro_cny_per_million)} · {formatDate(price.effective_at)}</Typography>) ?? '—'}</TableCell></TableRow>)}</TableBody></Table></TableContainer></ListCard>
  </>;
}
