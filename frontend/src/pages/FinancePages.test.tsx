import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SWRConfig } from 'swr';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { configureApiAuth } from '../api/client';
import { MOCK_NOTICE } from '../customer/shared';
import { AdminPaymentsPage, AdminRefundsPage } from './FinancePages';

const originalPayment = { id: 'pay-a', tenant_id: 'tenant-a', tenant: { id: 'tenant-a', name: '支付账户', email: 'a@example.com' }, email: 'wrong-root@example.com', mode: 'mock', status: 'PAID', amount_micro_cny: '10000000', refundable_micro_cny: '8000000', refunded_micro_cny: '0', refund_frozen_micro_cny: '2000000', out_trade_no: 'trade-a', created_at: '2026-01-01T00:00:00Z' };
let refund: { id: string; tenant_id: string; tenant?: { id: string; name: string; email: string }; mode?: string; status: string; payment_order_id: string; amount_micro_cny: string; reason: string; decision_reason: string | null; out_request_no: string; created_at: string };
let payment: typeof originalPayment;
let fetcher: ReturnType<typeof vi.fn>;
const json = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), { status });
const writes = () => fetcher.mock.calls.filter(([, init]) => init.method === 'POST');
function view(refunds = true) { render(<SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false }} >{refunds ? <AdminRefundsPage /> : <AdminPaymentsPage />}</SWRConfig>); }
beforeEach(() => {
  payment = { ...originalPayment };
  refund = { id: 'refund-a', tenant_id: 'tenant-a', status: 'PENDING', payment_order_id: 'pay-a', amount_micro_cny: '2000000', reason: '未用额度', decision_reason: null, out_request_no: 'req-a', created_at: payment.created_at };
  configureApiAuth(() => 'admin-memory', vi.fn());
  fetcher = vi.fn(async (url: string) => url.startsWith('/api/admin/refunds') ? json({ items: [refund], has_more: false }) : json({ items: [payment], has_more: false }));
  vi.stubGlobal('fetch', fetcher);
});
afterEach(() => { configureApiAuth(() => null, () => undefined); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('管理员资金审批入口', () => {
  it.each(['approve', 'reject'] as const)('%s先核实账户/金额/mock，再确认；pending禁止重复提交并刷新权威记录', async (action) => {
    const user = userEvent.setup(); const implementation = fetcher.getMockImplementation()!;
    let release!: (value: Response) => void; const late = new Promise<Response>((done) => { release = done; });
    fetcher.mockImplementation((url: string, init: RequestInit) => init.method === 'POST' ? late : implementation(url));
    view(); await user.click(await screen.findByRole('button', { name: action === 'approve' ? '批准' : '拒绝' }));
    const dialog = screen.getByRole('dialog');
    expect(await within(dialog).findByText(MOCK_NOTICE)).toBeInTheDocument();
    expect(within(dialog).getByText(/tenant-a/)).toBeInTheDocument(); expect(within(dialog).getByText('本次退款金额：¥2.000000')).toBeInTheDocument();
    expect(within(dialog).getByText(/原订单金额：¥10.000000/)).toBeInTheDocument(); expect(writes()).toHaveLength(0);
    expect(within(dialog).getByText(/a@example.com/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/wrong-root@example.com/)).not.toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/审批说明/), '核对完成');
    const confirm = within(dialog).getByRole('button', { name: action === 'approve' ? '确认批准' : '确认拒绝' }); await user.click(confirm);
    expect(within(dialog).getByRole('button', { name: '提交中…' })).toBeDisabled();
    fireEvent.submit(confirm.closest('form')!); expect(writes()).toHaveLength(1);
    expect(writes()[0][0]).toBe(`/api/admin/refunds/refund-a/${action}`); expect(JSON.parse(writes()[0][1].body)).toEqual({ reason: '核对完成' });
    expect(writes()[0][1].headers.get('X-Admin-Token')).toBe('admin-memory'); expect(writes()[0][1].headers.has('X-CSRF-Token')).toBe(false);
    refund = { ...refund, status: action === 'approve' ? 'PROCESSING' : 'REJECTED', decision_reason: '核对完成' };
    await act(async () => { release(json(refund)); });
    expect(await screen.findByText(action === 'approve' ? '原路退款处理中' : '已拒绝')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(fetcher.mock.calls.filter(([url]) => url.startsWith('/api/admin/refunds?'))).toHaveLength(2);
  });
  it('真实/沙箱模式禁用审批，不捏造退款成功', async () => {
    payment = { ...payment, mode: 'alipay_sandbox' }; view();
    await userEvent.click(await screen.findByRole('button', { name: '批准' }));
    const dialog = screen.getByRole('dialog'); await within(dialog).findByText(/支付宝沙箱尚未连接/);
    await userEvent.type(within(dialog).getByLabelText(/审批说明/), '核实');
    expect(within(dialog).getByRole('button', { name: '确认批准' })).toBeDisabled(); expect(writes()).toHaveLength(0);
  });
  it('503失败保留确认表单，不展示服务端secret或假成功', async () => {
    const implementation = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((url: string, init: RequestInit) => init.method === 'POST' ? json({ detail: 'sk-private-secret' }, 503) : implementation(url));
    view(); await userEvent.click(await screen.findByRole('button', { name: '拒绝' }));
    const dialog = screen.getByRole('dialog'); await within(dialog).findByText(MOCK_NOTICE);
    await userEvent.type(within(dialog).getByLabelText(/审批说明/), '核实'); await userEvent.click(within(dialog).getByRole('button', { name: '确认拒绝' }));
    expect(await within(dialog).findByText(/服务尚未启用/)).toBeInTheDocument(); expect(screen.queryByText('sk-private-secret')).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: '确认拒绝' })).toBeEnabled();
  });
  it.each([false, true])('refunds=%s只筛选账户/状态，分页参数白名单且显示tenant.email', async (refunds) => {
    refund = { ...refund, tenant: { id: 'tenant-a', name: '退款账户', email: 'refund@example.com' } };
    const endpoint = refunds ? '/api/admin/refunds' : '/api/admin/payments';
    fetcher.mockImplementation(async (url: string) => json({ items: url.startsWith('/api/admin/refunds') ? [refund] : [payment], next_cursor: 'next-page', has_more: true }));
    view(refunds);
    expect(await screen.findByText(refunds ? /refund@example.com/ : /a@example.com/)).toBeInTheDocument();
    expect(screen.queryByText(/wrong-root@example.com/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText('模式')).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    if (!refunds) expect(screen.getByText('¥8.000000 / ¥0.000000 / ¥2.000000')).toBeInTheDocument();
    const params = (url: string) => new URL(url, 'http://localhost').searchParams;
    await userEvent.click(screen.getByRole('button', { name: '下一页' }));
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => params(url).get('cursor') === 'next-page')).toBe(true));
    fireEvent.change(screen.getByLabelText('账户 ID'), { target: { value: 'tenant-a' } });
    fireEvent.change(screen.getByLabelText('状态筛选'), { target: { value: refunds ? 'PENDING' : 'PAID' } });
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => {
      const query = params(url);
      return query.get('tenant_id') === 'tenant-a' && query.get('status') === (refunds ? 'PENDING' : 'PAID') && !query.has('cursor');
    })).toBe(true));
    if (refunds) {
      await userEvent.click(await screen.findByRole('button', { name: '批准' }));
      const dialog = screen.getByRole('dialog');
      await within(dialog).findByText(MOCK_NOTICE);
      expect(within(dialog).getByText(/refund@example.com/)).toBeInTheDocument();
      expect(within(dialog).queryByText(/a@example.com|wrong-root@example.com/)).not.toBeInTheDocument();
    }
    expect(fetcher.mock.calls.some(([url]) => url === `${endpoint}?limit=25`)).toBe(true);
    for (const [url, init] of fetcher.mock.calls) {
      // Fetch defaults to GET when apiRequest does not specify a method.
      expect(init.method ?? 'GET').toBe('GET');
      expect(['/api/admin/payments', '/api/admin/refunds']).toContain(new URL(url, 'http://localhost').pathname);
      expect(params(url).get('limit')).toBe('25');
      expect([...params(url).keys()].every((key) => ['tenant_id', 'status', 'cursor', 'limit'].includes(key))).toBe(true);
    }
  });
});
