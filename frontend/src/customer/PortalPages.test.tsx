import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { AuthProvider } from '../auth/AuthContext';
import { CustomerRoutes } from './CustomerRoutes';
import { MOCK_NOTICE } from './shared';

const profile = { id: 'c', tenant_id: 't', tenant_name: '测试客户', email: 'a@example.com', csrf_token: 'csrf-test' };
const baseOrder = { id: 'pay-1', mode: 'mock', status: 'CREATED', amount_micro_cny: '12340000', refundable_micro_cny: '0', refunded_micro_cny: '0', refund_frozen_micro_cny: '0', out_trade_no: 'trade-1', created_at: '2026-01-01T00:00:00Z' };
const json = (payload: unknown, status = 200) => new Response(status === 204 ? null : JSON.stringify(payload), { status });
let order: typeof baseOrder;
let mode: string;
let enabled: boolean;
let fetcher: ReturnType<typeof vi.fn>;
const writes = () => fetcher.mock.calls.filter(([, init]) => init.method === 'POST');
function view(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/portal/*" element={<CustomerRoutes />} /></Routes></MemoryRouter>);
}
beforeEach(() => {
  order = { ...baseOrder }; mode = 'mock'; enabled = true;
  fetcher = vi.fn(async (url: string, init: RequestInit) => {
    const path = url.split('?')[0];
    if (path === '/api/portal/session') return json(profile);
    if (path === '/api/portal/payments/config') return json({ mode, enabled, mock: mode === 'mock', notice: '' });
    if (path === '/api/portal/payments/pay-1/mock-confirm') { order = { ...order, status: 'PAID', refundable_micro_cny: order.amount_micro_cny }; return json(order); }
    if (path === '/api/portal/payments/pay-1') return json(order);
    if (path === '/api/portal/payments') return init.method === 'POST' ? json(order) : json({ items: [order], next_cursor: null, has_more: false });
    if (path === '/api/portal/keys') return init.method === 'POST' ? json({ id: 'key-1', prefix: 'gw_x', api_key: 'gw_one_time_private' }) : json({ items: [{ id: 'key-1', prefix: 'gw_x', revoked: false, created_at: baseOrder.created_at }] });
    if (path.endsWith('/revoke')) return json(null, 204);
    if (path === '/api/portal/balance') return json({ available_micro_cny: '0', frozen_micro_cny: '0', refund_frozen_micro_cny: '0', total_micro_cny: '0' });
    return json({ items: [], next_cursor: null, has_more: false });
  }); vi.stubGlobal('fetch', fetcher);
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

describe('客户门户交互', () => {
  it('没有管理员context也能进入客户页面；Key仅展示一次，关闭清空，不写storage，撤销须确认', async () => {
    const storage = vi.spyOn(Storage.prototype, 'setItem'); const user = userEvent.setup();
    view('/portal/keys');
    await user.click(await screen.findByRole('button', { name: '创建 Key' }));
    const dialog = await screen.findByRole('dialog', { name: '请立即保存 API Key' });
    expect(within(dialog).getByDisplayValue('gw_one_time_private')).toHaveAttribute('readonly');
    await user.click(within(dialog).getByRole('button', { name: '我已保存，关闭' }));
    expect(screen.queryByDisplayValue('gw_one_time_private')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: '撤销' }));
    expect(writes()).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: '确认撤销' }));
    await waitFor(() => expect(writes()).toHaveLength(2));
    expect(writes()[1][0]).toBe('/api/portal/keys/key-1/revoke'); expect(storage).not.toHaveBeenCalled();
    expect(writes().every(([, init]) => init.headers.get('X-CSRF-Token') === 'csrf-test' && !init.headers.has('X-Admin-Token'))).toBe(true);
  });
  it('mock橙色声明清晰，创建订单不是到账；只能模拟完成支付并刷新真实状态', async () => {
    const user = userEvent.setup(); view('/portal/payments');
    expect(await screen.findByText(MOCK_NOTICE)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/充值金额（元）/), '12.34');
    await user.click(screen.getByRole('button', { name: '创建充值订单' }));
    const dialog = await screen.findByRole('dialog', { name: '充值订单详情' });
    expect(within(dialog).getByText(MOCK_NOTICE)).toBeInTheDocument(); expect(within(dialog).getByText('待支付')).toBeInTheDocument();
    expect(JSON.parse(writes()[0][1].body)).toMatchObject({ amount_micro_cny: '12340000', idempotency_key: expect.any(String) });
    expect(screen.queryByRole('button', { name: /支付宝支付|真实支付|跳转支付宝/ })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: '模拟完成支付' }));
    expect(await within(dialog).findByText('已支付')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: '模拟完成支付' })).toBeDisabled();
    expect(writes().filter(([url]) => url.endsWith('/mock-confirm'))).toHaveLength(1);
  });
  it.each(['disabled', 'alipay_sandbox'])('%s不可用：无真实支付宝按钮，充值禁用不假成功', async (unavailable) => {
    mode = unavailable; enabled = unavailable !== 'disabled'; order = { ...order, mode: unavailable };
    const user = userEvent.setup(); view('/portal/payments');
    expect(await screen.findByRole('button', { name: '创建充值订单' })).toBeDisabled();
    await user.click(await screen.findByRole('button', { name: '详情' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).queryByRole('button', { name: '模拟完成支付' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /支付宝支付|真实支付/ })).not.toBeInTheDocument(); expect(writes()).toHaveLength(0);
  });
  it('订单503失败可重试，同金额同意图复用UUID，pending禁止重复提交且不展示原始error', async () => {
    const original = fetcher.getMockImplementation()!;
    let attempts = 0; let release!: (value: Response) => void;
    const waiting = new Promise<Response>((done) => { release = done; });
    fetcher.mockImplementation((url: string, init: RequestInit) => {
      if (url === '/api/portal/payments' && init.method === 'POST') { attempts += 1; return attempts === 1 ? json({ detail: 'secret-password' }, 503) : waiting; }
      return original(url, init);
    });
    const user = userEvent.setup(); view('/portal/payments');
    await screen.findByText(MOCK_NOTICE); await user.type(screen.getByLabelText(/充值金额（元）/), '12.34');
    await user.click(screen.getByRole('button', { name: '创建充值订单' }));
    expect(await screen.findByText(/未完成资金操作/)).toBeInTheDocument(); expect(screen.queryByText('secret-password')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '创建充值订单' }));
    const pending = screen.getByRole('button', { name: '创建中…' }); expect(pending).toBeDisabled(); fireEvent.submit(pending.closest('form')!);
    expect(writes()).toHaveLength(2);
    expect(JSON.parse(writes()[0][1].body).idempotency_key).toBe(JSON.parse(writes()[1][1].body).idempotency_key);
    await act(async () => { release(json(order)); }); expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });
  it('退款整分额度检查与申请冻结提示，跟踪审批且没有自助退款成功按钮', async () => {
    order = { ...order, status: 'PAID', refundable_micro_cny: '1009999' };
    const original = fetcher.getMockImplementation()!;
    let refund: Record<string, unknown> | null = null;
    fetcher.mockImplementation((url: string, init: RequestInit) => {
      if (url.split('?')[0] === '/api/portal/refunds') {
        if (init.method === 'POST') { refund = { ...JSON.parse(String(init.body)), id: 'refund-1', status: 'PENDING', decision_reason: null, created_at: baseOrder.created_at, out_request_no: 'req-1' }; return json(refund); }
        return json({ items: refund ? [refund] : [] });
      }
      return original(url, init);
    });
    const user = userEvent.setup(); view('/portal/refunds?payment_order_id=pay-1');
    await screen.findByText(/当前订单整分可退上限：¥1.000000/);
    await user.type(screen.getByLabelText(/退款金额（元）/), '1.01'); await user.type(screen.getByLabelText(/退款原因/), '余额未使用');
    await user.click(screen.getByRole('button', { name: '提交退款申请（冻结余额）' }));
    expect(await screen.findByText(/金额须大于 0/)).toBeInTheDocument(); expect(writes()).toHaveLength(0);
    await user.clear(screen.getByLabelText(/退款金额（元）/)); await user.type(screen.getByLabelText(/退款金额（元）/), '1.00');
    await user.click(screen.getByRole('button', { name: '提交退款申请（冻结余额）' }));
    expect(await screen.findByText(/退款申请已提交：refund-1/)).toBeInTheDocument(); expect(await screen.findByText('待处理')).toBeInTheDocument();
    expect(JSON.parse(writes()[0][1].body).amount_micro_cny).toBe('1000000');
    expect(screen.queryByRole('button', { name: /模拟.*退款|退款成功|确认退款/ })).not.toBeInTheDocument();
  });
  it('usage分页明确limit25，不显示对话内容；未登录客户独立redirect登录', async () => {
    view('/portal/usage'); await screen.findByText('请求与用量');
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => url === '/api/portal/requests?limit=25')).toBe(true));
  });
  it.each(['/portal/activate', '/portal/activate/', '/PORTAL/ACTIVATE'])('%s激活密码检查、失败可重试，与Router匹配一致', async (pathname) => {
    window.history.replaceState(null, '', `${pathname}#token=activation-private`);
    const original = fetcher.getMockImplementation()!;
    let attempts = 0;
    fetcher.mockImplementation((url: string, init: RequestInit) => {
      if (url === '/api/portal/session') return json({}, 401);
      if (url === '/api/portal/activate') { attempts += 1; return attempts === 1 ? json({ detail: 'activation-private' }, 400) : json(profile); }
      return original(url, init);
    });
    const user = userEvent.setup(); view(pathname); await screen.findByText('激活客户账户');
    expect(window.location.hash).toBe('');
    const password = screen.getByLabelText(/设置密码/); const confirm = screen.getByLabelText(/确认密码/);
    await user.type(password, 'short'); await user.type(confirm, 'short');
    fireEvent.submit(screen.getByRole('button', { name: '激活并进入门户' }).closest('form')!);
    expect(await screen.findByText('密码至少 12 位，两次输入须一致')).toBeInTheDocument(); expect(writes()).toHaveLength(0);
    await user.clear(password); await user.type(password, 'long-secure-password'); await user.clear(confirm); await user.type(confirm, 'long-secure-password');
    await user.click(screen.getByRole('button', { name: '激活并进入门户' }));
    expect(await screen.findByText('请求未被接受，请检查输入或激活链接')).toBeInTheDocument(); expect(screen.getByLabelText(/设置密码/)).toHaveValue('long-secure-password');
    await user.click(screen.getByRole('button', { name: '激活并进入门户' }));
    expect(await screen.findByText('你好，测试客户')).toBeInTheDocument();
    expect(writes()).toHaveLength(2); expect(JSON.parse(writes()[0][1].body)).toEqual({ token: 'activation-private', password: 'long-secure-password' });
    expect(writes().every(([url]) => !String(url).includes('activation-private'))).toBe(true);
  });
  it('未登录portal不需要管理员context，跳到客户登录', async () => {
    const original = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((url: string, init: RequestInit) => url === '/api/portal/session' ? json({}, 401) : original(url, init));
    view('/portal/usage'); expect(await screen.findByText('欢迎回到客户门户')).toBeInTheDocument(); expect(screen.getByLabelText(/邮箱/)).toBeInTheDocument();
  });
  it.each([
    ['/unknown', false, '/portal/login'],
    ['/portal/unknown', false, '/portal/login'],
    ['/portal/unknown/deep', true, '/portal'],
    ['/', false, '/login'],
    ['/login', false, '/login'],
    ['/payments', false, '/login']
  ] as const)('%s回退保持会话归属和管理员入口', async (pathname, customerSession, target) => {
    const original = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((url: string, init: RequestInit) => url === '/api/portal/session' && !customerSession ? json({}, 401) : original(url, init));
    function Location() { return <output data-testid="current-path">{useLocation().pathname}</output>; }
    render(<MemoryRouter initialEntries={[pathname]}><AuthProvider><App /><Location /></AuthProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByTestId('current-path')).toHaveTextContent(new RegExp(`^${target}$`)));
    if (target === '/login') {
      expect(screen.getByText('管理员安全登录')).toBeInTheDocument();
      expect(fetcher).not.toHaveBeenCalled();
    } else {
      expect(await screen.findByText(customerSession ? '你好，测试客户' : '欢迎回到客户门户')).toBeInTheDocument();
      expect(screen.queryByText('管理员安全登录')).not.toBeInTheDocument();
      expect(fetcher.mock.calls.some(([url, init]) => url === '/api/portal/session' && init.method === 'GET')).toBe(true);
      expect(fetcher.mock.calls.every(([url]) => url.startsWith('/api/portal/'))).toBe(true);
    }
  });
});
