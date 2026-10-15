import { Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography } from '@mui/material';
import { useRef, useState, type FormEvent } from 'react';
import { SWRConfig, useSWRConfig } from 'swr';
import { apiRequest, swrFetcher } from '../api/client';
import { formatDate, PageHeader } from '../components/Common';
import { safeError } from '../customer/api';
import { FinancialStatus, ListCard, ModeNotice, useCursorPage } from '../customer/shared';
import type { PaymentOrder, RefundRequest } from '../types';
import { formatMicroCny } from '../utils/money';

const reviewable = (status: string) => ['PENDING', 'REQUESTED', 'PENDING_REVIEW', 'PENDING_APPROVAL'].includes(status.toUpperCase());
const accountId = (row: PaymentOrder | RefundRequest) => row.tenant_id ?? row.tenant?.id;
export function AdminPaymentsPage() { return <SWRConfig value={{ fetcher: swrFetcher }}><FinanceList refunds={false} /></SWRConfig>; }
export function AdminRefundsPage() { return <SWRConfig value={{ fetcher: swrFetcher }}><FinanceList refunds /></SWRConfig>; }
function FinanceList({ refunds }: { refunds: boolean }) {
  const [status, setStatus] = useState('');
  const [tenantId, setTenantId] = useState('');
  const [decision, setDecision] = useState<{ refund: RefundRequest; action: 'approve' | 'reject' } | null>(null);
  const list = useCursorPage<PaymentOrder | RefundRequest>(refunds ? '/admin/refunds' : '/admin/payments', { status: status || undefined, tenant_id: tenantId || undefined }, 5000);
  return <><PageHeader title={refunds ? '退款审批' : '充值订单'} description={refunds ? '核对账户、原支付订单与退款金额后审批；真实模式尚不可用' : '支付订单与退款冻结明细；这里不会发起真实支付宝支付'} />
    <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} mb={2}>
      <TextField label="状态筛选" value={status} onChange={(event) => { setStatus(event.target.value); list.reset(); }} helperText="如 PAID、PENDING、REJECTED" />
      <TextField label="账户 ID" value={tenantId} onChange={(event) => { setTenantId(event.target.value); list.reset(); }} />
    </Stack>
    <ListCard list={list}><TableContainer><Table><TableHead><TableRow><TableCell>订单 / 账户</TableCell><TableCell>金额</TableCell><TableCell>模式 / 状态</TableCell><TableCell>{refunds ? '原因 / 审批说明' : '可退 / 已退 / 退款冻结'}</TableCell><TableCell>创建时间</TableCell>{refunds && <TableCell>审批</TableCell>}</TableRow></TableHead><TableBody>{list.data?.items.map((item) => {
      const payment = item as PaymentOrder;
      const refund = item as RefundRequest;
      return <TableRow key={item.id}><TableCell>{item.id}<Typography variant="caption" display="block">{refunds ? refund.payment_order_id : payment.out_trade_no}</Typography><Typography variant="body2">{item.tenant?.name ?? accountId(item) ?? '待核对账户'} {item.tenant?.email ?? ''}</Typography></TableCell><TableCell>{formatMicroCny(item.amount_micro_cny)}</TableCell><TableCell>{item.mode === 'mock' ? '本地模拟' : item.mode ? '真实/沙箱不可用' : '模式待核对'}<Box mt={1}><FinancialStatus value={item.status} /></Box></TableCell><TableCell>{refunds ? <>{refund.reason}<Typography variant="caption" display="block">{refund.decision_reason ?? '待审批'}</Typography></> : `${formatMicroCny(payment.refundable_micro_cny)} / ${formatMicroCny(payment.refunded_micro_cny)} / ${formatMicroCny(payment.refund_frozen_micro_cny)}`}</TableCell><TableCell>{formatDate(item.created_at)}</TableCell>{refunds && <TableCell><Stack direction="row"><Button disabled={!reviewable(item.status)} onClick={() => setDecision({ refund, action: 'approve' })}>批准</Button><Button color="error" disabled={!reviewable(item.status)} onClick={() => setDecision({ refund, action: 'reject' })}>拒绝</Button></Stack></TableCell>}</TableRow>;
    })}</TableBody></Table></TableContainer></ListCard>
    {decision && <RefundDecisionDialog refund={decision.refund} action={decision.action} onClose={() => setDecision(null)} />}
  </>;
}
function RefundDecisionDialog({ refund, action, onClose }: { refund: RefundRequest; action: 'approve' | 'reject'; onClose: () => void }) {
  const [reason, setReason] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [accepted, setAccepted] = useState(false);
  const lock = useRef(false);
  const { mutate } = useSWRConfig();
  // Resolve optional refund mode/account from the original payment using the contracted list endpoint.
  const orders = useCursorPage<PaymentOrder>('/admin/payments', { tenant_id: accountId(refund) });
  const original = orders.data?.items.find((order) => order.id === refund.payment_order_id);
  const mode = original?.mode ?? refund.mode;
  const account = accountId(refund) ?? (original ? accountId(original) : undefined);
  const enabled = mode === 'mock' && Boolean(account) && reviewable(refund.status);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (lock.current || !enabled || !reason.trim() || accepted) return;
    lock.current = true; setPending(true); setError('');
    try {
      await apiRequest(`/admin/refunds/${encodeURIComponent(refund.id)}/${action}`, { method: 'POST', body: { reason: reason.trim() } });
      setAccepted(true);
      try {
        await mutate((key) => typeof key === 'string' && ['/admin/refunds', '/admin/payments', '/admin/tenants', '/admin/ledger'].some((prefix) => key === prefix || key.startsWith(`${prefix}?`) || key.startsWith(`${prefix}/`)), undefined, { revalidate: true });
        onClose();
      } catch { setError('审批请求已受理，但记录刷新失败；请刷新核对最终状态，勿重复审批。'); }
    } catch (failure) { setError(safeError(failure)); }
    finally { lock.current = false; setPending(false); }
  };
  return <Dialog open onClose={() => !pending && onClose()} fullWidth maxWidth="sm"><Box component="form" onSubmit={submit}><DialogTitle>{action === 'approve' ? '确认批准退款' : '确认拒绝退款'}</DialogTitle><DialogContent><Stack spacing={2} pt={1}>
    <ModeNotice mode={mode} />
    <Typography>账户：{refund.tenant?.name ?? original?.tenant?.name ?? account ?? '尚未核实'}（{account ?? '未知'}） · {refund.tenant?.email ?? original?.tenant?.email ?? ''}</Typography>
    <Typography>退款申请：{refund.id} · 原支付订单：{refund.payment_order_id}</Typography>
    <Typography fontWeight={700}>本次退款金额：{formatMicroCny(refund.amount_micro_cny)}</Typography>
    {original && <Typography>原订单金额：{formatMicroCny(original.amount_micro_cny)} · 可退：{formatMicroCny(original.refundable_micro_cny)} · 退款冻结：{formatMicroCny(original.refund_frozen_micro_cny)}</Typography>}
    <Typography>申请原因：{refund.reason}</Typography>
    {!enabled && <Alert severity="warning">未核实账户/模拟模式或真实模式不可用，不能提交审批。可翻页核对原支付订单。</Alert>}
    {!original && orders.pager}
    {orders.error && <Alert severity="error">原支付订单加载失败，请稍后重试。</Alert>}
    {error && <Alert severity="error">{error}</Alert>}
    {accepted && <Alert severity="info">审批已受理，请查看最新记录确认状态。</Alert>}
    <TextField label="审批说明" value={reason} onChange={(event) => setReason(event.target.value)} required multiline minRows={2} disabled={pending || accepted} slotProps={{ htmlInput: { maxLength: 500 } }} />
    <Alert severity="info">{action === 'approve' ? '批准后由后台执行原路退款；仅本地模拟，不退真实资金。' : '拒绝后由后台解除相应退款冻结。'}</Alert>
  </Stack></DialogContent><DialogActions><Button disabled={pending} onClick={onClose}>关闭</Button><Button type="submit" variant="contained" color={action === 'approve' ? 'primary' : 'error'} disabled={pending || !enabled || !reason.trim() || accepted}>{pending ? '提交中…' : action === 'approve' ? '确认批准' : '确认拒绝'}</Button></DialogActions></Box></Dialog>;
}
