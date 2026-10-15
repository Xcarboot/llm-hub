import { Alert, Box, Button, Card, MenuItem, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography } from '@mui/material';
import { useRef, useState, type FormEvent } from 'react';
import { useSearchParams } from 'react-router';
import useSWR from 'swr';
import { ErrorState, formatDate, PageHeader } from '../components/Common';
import type { PaymentConfig, PaymentOrder, RefundRequest } from '../types';
import { formatMicroCny, portalAmountToMicro, refundableWholeMicro } from '../utils/money';
import { useCustomerAuth } from './CustomerAuthContext';
import { safeError } from './api';
import { FinancialStatus, ListCard, ModeNotice, useCursorPage, useFinancialRefresh, useStableIntent } from './shared';

export function CustomerRefundsPage() {
  const { client } = useCustomerAuth();
  const [search] = useSearchParams();
  const [paymentId, setPaymentId] = useState(search.get('payment_order_id') ?? '');
  const payments = useCursorPage<PaymentOrder>('/portal/payments');
  const payment = useSWR<PaymentOrder>(paymentId ? `/portal/payments/${encodeURIComponent(paymentId)}` : null);
  const config = useSWR<PaymentConfig>('/portal/payments/config');
  const list = useCursorPage<RefundRequest>('/portal/refunds', {}, 5000);
  const [amount, setAmount] = useState('');
  const [reason, setReason] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [submitted, setSubmitted] = useState<RefundRequest | null>(null);
  const lock = useRef(false);
  const intent = useStableIntent();
  const refresh = useFinancialRefresh();
  const enabled = config.data?.enabled === true && config.data.mode === 'mock' && config.data.mock && payment.data?.mode === 'mock';
  const maximum = payment.data ? refundableWholeMicro(payment.data.refundable_micro_cny) : '0';
  const choices = payments.data?.items ?? [];
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (lock.current || !enabled || !payment.data || submitted) return;
    setError('');
    let micro: string;
    try {
      micro = portalAmountToMicro(amount, maximum);
      if (!reason.trim()) throw new Error('请填写退款原因');
    } catch (failure) { setError(failure instanceof Error ? failure.message : '请检查输入'); return; }
    lock.current = true; setPending(true);
    try {
      const payload = { payment_order_id: payment.data.id, amount_micro_cny: micro, reason: reason.trim() };
      const result = await client.request<RefundRequest>('/portal/refunds', { method: 'POST', body: { ...payload, idempotency_key: intent.key(payload) } });
      setSubmitted(result);
      try { await Promise.all([refresh(), payment.mutate(), payments.mutate()]); }
      catch { setError('申请已提交，记录刷新失败，请刷新后查看；不要重复提交。'); }
    } catch (failure) { setError(safeError(failure)); }
    finally { lock.current = false; setPending(false); }
  };
  return <><PageHeader title="退款申请与跟踪" description="申请即冻结相应余额，由管理员审批后原路退款；提交不代表退款完成" />
    <ModeNotice mode={config.data?.mode} />
    <Card sx={{ p: 3, my: 3 }}><Box component="form" onSubmit={submit}><Stack spacing={2}>
      {payments.error && <ErrorState error={payments.error} />}{config.error && <ErrorState error={config.error} />}{payment.error && <ErrorState error={payment.error} />}
      {error && <Alert severity="error">{error}</Alert>}
      {submitted && <Alert severity="info">退款申请已提交：{submitted.id}。请在下方跟踪审批及原路退款状态。</Alert>}
      <TextField select label="原支付订单" value={paymentId} disabled={pending || submitted !== null} onChange={(event) => { setPaymentId(event.target.value); setAmount(''); setError(''); }}>
        <MenuItem value="">请选择订单</MenuItem>{paymentId && !choices.some((row) => row.id === paymentId) && <MenuItem value={paymentId}>{payment.data?.out_trade_no ?? paymentId}</MenuItem>}
        {choices.map((row) => <MenuItem key={row.id} value={row.id} disabled={BigInt(row.refundable_micro_cny) < 10000n}>{row.out_trade_no} · 可退 {formatMicroCny(refundableWholeMicro(row.refundable_micro_cny))}</MenuItem>)}
      </TextField>
      {payments.pager}
      <Typography color="text.secondary">当前订单整分可退上限：{formatMicroCny(maximum)}；每次最多 1000 元。冻结额度由服务器校验。</Typography>
      <TextField label="退款金额（元）" value={amount} required disabled={pending || submitted !== null} onChange={(event) => setAmount(event.target.value)} slotProps={{ htmlInput: { inputMode: 'decimal', maxLength: 16 } }} />
      <TextField label="退款原因" value={reason} required multiline minRows={2} disabled={pending || submitted !== null} onChange={(event) => setReason(event.target.value)} slotProps={{ htmlInput: { maxLength: 500 } }} />
      {!enabled && <Alert severity="warning">请选择可退的本地模拟订单；未启用或真实/沙箱退款当前不可用。</Alert>}
      <Button type="submit" variant="contained" disabled={!enabled || BigInt(maximum) < 10000n || pending || submitted !== null}>{pending ? '提交中…' : '提交退款申请（冻结余额）'}</Button>
      {submitted && <Button onClick={() => { setSubmitted(null); setAmount(''); setReason(''); intent.reset(); setError(''); }}>明确发起另一笔申请</Button>}
    </Stack></Box></Card>
    <ListCard list={list}><TableContainer><Table><TableHead><TableRow><TableCell>申请 / 原支付订单</TableCell><TableCell>金额</TableCell><TableCell>状态</TableCell><TableCell>原因 / 审批说明</TableCell><TableCell>时间</TableCell></TableRow></TableHead><TableBody>{list.data?.items.map((row) => <TableRow key={row.id}><TableCell>{row.out_request_no || row.id}<Typography variant="caption" display="block">{row.payment_order_id}</Typography></TableCell><TableCell>{formatMicroCny(row.amount_micro_cny)}</TableCell><TableCell><FinancialStatus value={row.status} /></TableCell><TableCell sx={{ maxWidth: 300, overflowWrap: 'anywhere' }}>{row.reason}<Typography variant="caption" display="block">审批：{row.decision_reason || '等待管理员处理'}</Typography></TableCell><TableCell>{formatDate(row.created_at)}</TableCell></TableRow>)}</TableBody></Table></TableContainer></ListCard>
  </>;
}
