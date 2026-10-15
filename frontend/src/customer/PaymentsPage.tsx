import { Alert, Box, Button, Card, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography } from '@mui/material';
import { useRef, useState, type FormEvent } from 'react';
import { Link } from 'react-router';
import useSWR from 'swr';
import { ErrorState, formatDate, LoadingState, PageHeader } from '../components/Common';
import type { PaymentConfig, PaymentOrder } from '../types';
import { formatMicroCny, portalAmountToMicro } from '../utils/money';
import { useCustomerAuth } from './CustomerAuthContext';
import { safeError } from './api';
import { FinancialStatus, ListCard, ModeNotice, useCursorPage, useFinancialRefresh, useStableIntent } from './shared';

const canConfirm = (order: PaymentOrder) => ['NEW', 'CREATED', 'PENDING', 'PENDING_PAYMENT', 'UNPAID'].includes(order.status.toUpperCase());
export function CustomerPaymentsPage() {
  const { client } = useCustomerAuth();
  const config = useSWR<PaymentConfig>('/portal/payments/config');
  const list = useCursorPage<PaymentOrder>('/portal/payments', {}, 5000);
  const refresh = useFinancialRefresh();
  const intent = useStableIntent();
  const [amount, setAmount] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const detail = useSWR<PaymentOrder>(selectedId ? `/portal/payments/${encodeURIComponent(selectedId)}` : null, { refreshInterval: 5000 });
  const [created, setCreated] = useState<PaymentOrder | null>(null);
  const [pending, setPending] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState('');
  const lock = useRef(false);
  const enabled = config.data?.enabled === true && config.data.mode === 'mock' && config.data.mock === true;
  const order = detail.data ?? (created?.id === selectedId ? created : null);
  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (lock.current || !enabled || created) return;
    setError('');
    let micro: string;
    try { micro = portalAmountToMicro(amount); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '金额无效'); return; }
    lock.current = true; setPending(true);
    try {
      const payload = { amount_micro_cny: micro };
      const result = await client.request<PaymentOrder>('/portal/payments', { method: 'POST', body: { ...payload, idempotency_key: intent.key(payload) } });
      setCreated(result); setSelectedId(result.id);
      try { await refresh(); } catch { setError('订单已创建，记录刷新失败，请稍后刷新。不要重复下单。'); }
    } catch (reason) { setError(safeError(reason)); }
    finally { lock.current = false; setPending(false); }
  };
  const mockConfirm = async () => {
    if (lock.current || !enabled || !order || order.mode !== 'mock' || !canConfirm(order)) return;
    lock.current = true; setConfirming(true); setError('');
    try {
      await client.request(`/portal/payments/${encodeURIComponent(order.id)}/mock-confirm`, { method: 'POST' });
      // No optimistic paid state: always read the authoritative order and balance.
      try { await Promise.all([refresh(), detail.mutate()]); }
      catch { setError('模拟确认请求已受理，请刷新订单与余额确认最终状态。'); }
    } catch (reason) { setError(safeError(reason)); }
    finally { lock.current = false; setConfirming(false); }
  };
  return <><PageHeader title="充值订单" description="创建订单不等于到账；资金状态以服务器记录为准" />
    {config.isLoading ? <LoadingState /> : config.error ? <ErrorState error={config.error} /> : <><ModeNotice mode={config.data?.mode} />{!enabled && <Alert severity="warning" sx={{ mt: 2 }}>充值未启用或模式尚未连接，不可下单；不会显示虚假充值成功。</Alert>}{config.data?.notice && <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{config.data.notice}</Typography>}</>}
    {error && <Alert severity="error" sx={{ my: 2 }}>{error}</Alert>}
    <Card sx={{ p: 3, my: 3 }}><Box component="form" onSubmit={create}><Stack spacing={2} direction={{ xs: 'column', sm: 'row' }} alignItems="start">
      <TextField label="充值金额（元）" value={amount} onChange={(event) => setAmount(event.target.value)} disabled={!enabled || pending || created !== null} required helperText="整分金额，0.01–1000 元。失败重试复用同一订单意图。" slotProps={{ htmlInput: { inputMode: 'decimal', maxLength: 16 } }} />
      <Button type="submit" variant="contained" disabled={!enabled || pending || created !== null}>{pending ? '创建中…' : '创建充值订单'}</Button>
      {created && <Button onClick={() => { setCreated(null); setAmount(''); intent.reset(); setError(''); }} disabled={pending || confirming}>明确创建另一笔订单</Button>}
    </Stack></Box></Card>
    <ListCard list={list}><TableContainer><Table><TableHead><TableRow><TableCell>订单 / 时间</TableCell><TableCell>模式</TableCell><TableCell>金额</TableCell><TableCell>可退 / 已退 / 退款冻结</TableCell><TableCell>状态</TableCell><TableCell>操作</TableCell></TableRow></TableHead><TableBody>{list.data?.items.map((row) => <TableRow key={row.id}><TableCell>{row.out_trade_no}<Typography variant="caption" display="block">{formatDate(row.created_at)}</Typography></TableCell><TableCell>{row.mode === 'mock' ? '本地模拟' : '不可用模式'}</TableCell><TableCell>{formatMicroCny(row.amount_micro_cny)}</TableCell><TableCell>{formatMicroCny(row.refundable_micro_cny)} / {formatMicroCny(row.refunded_micro_cny)} / {formatMicroCny(row.refund_frozen_micro_cny)}</TableCell><TableCell><FinancialStatus value={row.status} /></TableCell><TableCell><Button onClick={() => { setSelectedId(row.id); setError(''); }}>详情</Button><Button component={Link} to={`/portal/refunds?payment_order_id=${encodeURIComponent(row.id)}`} disabled={BigInt(row.refundable_micro_cny) < 10000n}>申请退款</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer></ListCard>
    <Dialog open={selectedId !== null} onClose={() => !confirming && setSelectedId(null)} fullWidth maxWidth="sm"><DialogTitle>充值订单详情</DialogTitle><DialogContent><Stack spacing={2} pt={1}>
      {detail.error && <ErrorState error={detail.error} />}
      {error && <Alert severity="error">{error}</Alert>}
      {!order ? <LoadingState /> : <><ModeNotice mode={order.mode} /><Typography>订单：{order.id} / {order.out_trade_no}</Typography><Typography>金额：{formatMicroCny(order.amount_micro_cny)}</Typography><FinancialStatus value={order.status} /><Typography>可退：{formatMicroCny(order.refundable_micro_cny)} · 已退：{formatMicroCny(order.refunded_micro_cny)} · 冻结：{formatMicroCny(order.refund_frozen_micro_cny)}</Typography>
        {order.mode === 'mock' && <Button variant="contained" color="warning" disabled={!enabled || confirming || !canConfirm(order)} onClick={() => void mockConfirm()}>{confirming ? '模拟确认中…' : '模拟完成支付'}</Button>}
      </>}
    </Stack></DialogContent><DialogActions><Button disabled={confirming} onClick={() => setSelectedId(null)}>关闭</Button></DialogActions></Dialog>
  </>;
}
