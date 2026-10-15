import { CheckCircleOutline, KeyOutlined, PeopleOutline, ReceiptLongOutlined, TokenOutlined, TrendingUpOutlined } from '@mui/icons-material';
import { Alert, Box, Card, CardContent, Chip, Grid, Stack, Typography } from '@mui/material';
import { BarChart, LineChart } from '@mui/x-charts';
import useSWR from 'swr';
import { swrFetcher } from '../api/client';
import { ErrorState, LoadingState, PageHeader } from '../components/Common';
import type { DashboardData } from '../types';
import { formatMicroCny, microCnyToChartYuan } from '../utils/money';

const fallback: DashboardData = {
  kpis: { tenant_count: 0, active_key_count: 0, request_count: 0, success_count: 0, success_rate: 0, charged_micro_cny: '0', prompt_tokens: 0, completion_tokens: 0 },
  trend: []
};

export function DashboardPage() {
  const dashboard = useSWR<DashboardData>('/admin/overview?days=7', swrFetcher, { refreshInterval: 30_000 });
  const live = useSWR<{ status?: string }>('/health/live', swrFetcher, { refreshInterval: 30_000 });
  const ready = useSWR<{ status?: string }>('/health/ready', swrFetcher, { refreshInterval: 30_000 });
  if (dashboard.isLoading) return <LoadingState />;
  if (dashboard.error) return <ErrorState error={dashboard.error} />;
  const data = dashboard.data ?? fallback;
  const totalTokens = BigInt(data.kpis.prompt_tokens) + BigInt(data.kpis.completion_tokens);
  const cards = [
    { label: '租户', value: data.kpis.tenant_count.toLocaleString('zh-CN'), icon: <PeopleOutline color="primary" /> },
    { label: '有效 Key', value: data.kpis.active_key_count.toLocaleString('zh-CN'), icon: <KeyOutlined color="primary" /> },
    { label: '请求', value: data.kpis.request_count.toLocaleString('zh-CN'), icon: <TrendingUpOutlined color="primary" /> },
    { label: '成功率', value: `${(data.kpis.success_rate * 100).toFixed(2)}%`, icon: <CheckCircleOutline color="success" /> },
    { label: '消费', value: formatMicroCny(data.kpis.charged_micro_cny), icon: <ReceiptLongOutlined color="warning" /> },
    { label: 'Token', value: totalTokens.toLocaleString('zh-CN'), icon: <TokenOutlined color="primary" /> }
  ];
  const dates = data.trend.map((item) => item.date.slice(5));
  const spend = data.trend.map((item) => microCnyToChartYuan(item.charged_micro_cny));

  return (
    <>
      <PageHeader title="运行总览" description="近 7 日网关流量、计费与服务状态" action={<Stack direction="row" spacing={1}><Chip color={live.error ? 'error' : 'success'} variant="outlined" label={`Live ${live.error ? '异常' : '正常'}`} /><Chip color={ready.error ? 'error' : 'success'} variant="outlined" label={`Ready ${ready.error ? '异常' : '正常'}`} /></Stack>} />
      {ready.error && <Alert severity="warning" sx={{ mb: 2 }}>API 就绪检查异常，请检查数据库、Redis 与上游配置。</Alert>}
      <Grid container spacing={2.5}>
        {cards.map((card) => <Grid key={card.label} size={{ xs: 12, sm: 6, lg: 4 }}><Card><CardContent><Stack direction="row" justifyContent="space-between"><Box><Typography color="text.secondary" variant="body2">{card.label}</Typography><Typography variant="h5" mt={1}>{card.value}</Typography></Box>{card.icon}</Stack></CardContent></Card></Grid>)}
        <Grid size={{ xs: 12, lg: 7 }}><Card><CardContent><Typography variant="h6" mb={2}>每日请求数</Typography><LineChart height={300} xAxis={[{ scaleType: 'point', data: dates }]} series={[{ data: data.trend.map((item) => item.requests), label: '请求', area: true, showMark: false }]} /></CardContent></Card></Grid>
        <Grid size={{ xs: 12, lg: 5 }}><Card><CardContent><Typography variant="h6" mb={2}>每日消费（人民币）</Typography><BarChart height={300} skipAnimation xAxis={[{ scaleType: 'band', data: dates }]} yAxis={[{ width: 92, tickLabelStyle: { fontSize: 11 }, valueFormatter: (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 6 }) }]} series={[{ data: spend, label: '人民币', valueFormatter: (_value, { dataIndex }) => formatMicroCny(data.trend[dataIndex].charged_micro_cny) }]} /></CardContent></Card></Grid>
      </Grid>
    </>
  );
}
