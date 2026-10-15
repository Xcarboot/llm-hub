import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SWRConfig } from 'swr';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Tenant } from '../types';
import { TenantsPage } from './TenantsPage';

const mocks = vi.hoisted(() => ({ apiRequest: vi.fn(), swrFetcher: vi.fn() }));
vi.mock('../api/client', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api/client')>(),
  apiRequest: mocks.apiRequest,
  swrFetcher: mocks.swrFetcher
}));

const tenantA: Tenant = {
  id: 'tenant-a', name: '租户 A', active: true, created_at: '2026-01-01T00:00:00Z',
  key_count: 0, request_count: 0, available_micro_cny: '0', frozen_micro_cny: '0'
};
const tenantB: Tenant = { ...tenantA, id: 'tenant-b', name: '租户 B' };
const keyA = { id: 'key-a', prefix: 'gw_a', api_key: 'gw_one_time_A' };
let tenants: Tenant[];

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function renderPage() {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0, errorRetryCount: 0, revalidateOnFocus: false }}>
      <TenantsPage />
    </SWRConfig>
  );
}

async function openTenant(name = tenantA.name) {
  const row = await screen.findByRole('row', { name: new RegExp(name) });
  await userEvent.click(within(row).getByRole('button', { name: '查看详情' }));
  await screen.findByRole('button', { name: '创建 Key' });
}

beforeEach(() => {
  tenants = [tenantA, tenantB];
  mocks.apiRequest.mockReset();
  mocks.swrFetcher.mockReset();
  mocks.swrFetcher.mockImplementation(async (path: string) => {
    if (path === '/admin/tenants') return { items: tenants };
    if (path.endsWith('/keys')) return { items: [] };
    const tenant = tenants.find((item) => path === `/admin/tenants/${item.id}`);
    if (tenant) return tenant;
    throw new Error(`Unexpected request: ${path}`);
  });
});

describe('租户创建与一次性 Key 回归', () => {
  it('管理员撤销租户Key必须确认，锁定账户且只POST所属租户路径', async () => {
    const user = userEvent.setup();
    mocks.swrFetcher.mockImplementation(async (path: string) => path.endsWith('/keys') ? { items: [{ id: 'key-a', prefix: 'gw_a', revoked: false, created_at: tenantA.created_at }] } : path === '/admin/tenants' ? { items: tenants } : tenantA);
    const late = deferred<void>(); mocks.apiRequest.mockReturnValue(late.promise);
    renderPage(); await openTenant();
    const closeDetail = screen.getByRole('button', { name: '关闭详情' });
    await user.click(screen.getByRole('button', { name: '撤销' }));
    const dialog = screen.getByRole('dialog', { name: '确认撤销租户 Key' });
    expect(within(dialog).getByText(/账户：租户 A（tenant-a）/)).toBeInTheDocument(); expect(mocks.apiRequest).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole('button', { name: '确认撤销' }));
    expect(within(dialog).getByRole('button', { name: '撤销中…' })).toBeDisabled();
    expect(closeDetail).toBeDisabled();
    expect(mocks.apiRequest).toHaveBeenCalledWith('/admin/tenants/tenant-a/keys/key-a/revoke', { method: 'POST' });
    await act(async () => late.resolve()); await waitFor(() => expect(screen.queryByRole('dialog', { name: '确认撤销租户 Key' })).not.toBeInTheDocument());
  });

  it('延迟 Key 请求锁住关闭/切换/重复创建，响应明确归属 A，关闭后 B 不显示 A 的 Key', async () => {
    const user = userEvent.setup();
    const request = deferred<typeof keyA>();
    mocks.apiRequest.mockReturnValue(request.promise);
    renderPage();
    const bRow = await screen.findByRole('row', { name: /租户 B/ });
    const switchToB = within(bRow).getByRole('button', { name: '查看详情' });
    await openTenant();
    const create = screen.getByRole('button', { name: '创建 Key' });
    await user.click(create);

    expect(screen.getByRole('button', { name: '创建中…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '关闭详情' })).toBeDisabled();
    expect(switchToB).toBeDisabled();
    fireEvent.click(create);
    fireEvent.click(switchToB);
    await user.keyboard('{Escape}');
    fireEvent.click(document.querySelector('.MuiDrawer-root .MuiBackdrop-root')!);
    expect(screen.getByRole('button', { name: '关闭详情' })).toBeInTheDocument();
    expect(mocks.apiRequest).toHaveBeenCalledTimes(1);
    expect(mocks.apiRequest).toHaveBeenCalledWith('/admin/tenants/tenant-a/keys', { method: 'POST' });
    expect(mocks.swrFetcher).not.toHaveBeenCalledWith('/admin/tenants/tenant-b');

    await act(async () => request.resolve(keyA));
    const dialog = await screen.findByRole('dialog', { name: '请立即保存 API Key' });
    expect(within(dialog).getByText('归属租户：租户 A（ID：tenant-a）')).toBeInTheDocument();
    expect(within(dialog).queryByText(/租户 B/)).not.toBeInTheDocument();
    expect(within(dialog).getByDisplayValue(keyA.api_key)).toHaveAttribute('readonly');
    expect(switchToB).toBeDisabled();
    expect(mocks.swrFetcher.mock.calls.filter(([path]) => path === '/admin/tenants/tenant-a/keys')).toHaveLength(2);
    await user.click(within(dialog).getByRole('button', { name: '我已保存，关闭' }));
    // Clear the controlled secret immediately, before the Dialog exit animation completes.
    expect(screen.queryByDisplayValue(keyA.api_key)).not.toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: '关闭详情' }));
    await waitFor(() => expect(switchToB).toBeVisible());
    await openTenant(tenantB.name);
    expect(screen.queryByDisplayValue(keyA.api_key)).not.toBeInTheDocument();
    expect(screen.queryByText('归属租户：租户 A（ID：tenant-a）')).not.toBeInTheDocument();
  });

  it('Key 网络异常用 Alert 反馈并解除 pending，重试时清除旧错误且仍不能重复提交', async () => {
    const user = userEvent.setup();
    const first = deferred<typeof keyA>();
    const retry = deferred<typeof keyA>();
    mocks.apiRequest.mockReturnValueOnce(first.promise).mockReturnValueOnce(retry.promise);
    renderPage();
    await openTenant();
    await user.click(screen.getByRole('button', { name: '创建 Key' }));
    await act(async () => first.reject(new Error('网络连接失败，请稍后重试')));
    expect(screen.getByRole('alert')).toHaveTextContent('网络连接失败，请稍后重试');
    expect(screen.getByRole('button', { name: '创建 Key' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '关闭详情' })).toBeEnabled();
    expect(screen.queryByRole('dialog', { name: '请立即保存 API Key' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '创建 Key' }));
    expect(screen.queryByText('网络连接失败，请稍后重试')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '创建中…' }));
    expect(mocks.apiRequest).toHaveBeenCalledTimes(2);
    await act(async () => retry.resolve(keyA));
    expect(await screen.findByDisplayValue(keyA.api_key)).toBeInTheDocument();
  });

  it('未知 Key 异常也有失败反馈，切换租户不会携带旧错误', async () => {
    mocks.apiRequest.mockRejectedValue('failure');
    renderPage();
    await openTenant();
    await userEvent.click(screen.getByRole('button', { name: '创建 Key' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('创建 Key 失败');
    await userEvent.click(screen.getByRole('button', { name: '关闭详情' }));
    await openTenant(tenantB.name);
    expect(screen.queryByText('创建 Key 失败')).not.toBeInTheDocument();
  });

  it('Key 列表刷新失败也保留一次性 Key 与租户归属，Escape 关闭清空', async () => {
    const user = userEvent.setup();
    mocks.apiRequest.mockResolvedValue(keyA);
    renderPage();
    await openTenant();
    mocks.swrFetcher.mockImplementation(async (path: string) => {
      if (path.endsWith('/keys')) throw new Error('刷新失败');
      return path === '/admin/tenants' ? { items: tenants } : tenantA;
    });
    await user.click(screen.getByRole('button', { name: '创建 Key' }));
    expect(await screen.findByDisplayValue(keyA.api_key)).toBeInTheDocument();
    expect(screen.getByText('归属租户：租户 A（ID：tenant-a）')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: '我已保存，关闭' })).toBeEnabled());
    await user.keyboard('{Escape}');
    expect(screen.queryByDisplayValue(keyA.api_key)).not.toBeInTheDocument();
    expect(mocks.apiRequest).toHaveBeenCalledTimes(1);
  });

  it('创建租户期间按钮 pending、禁止重复提交/取消，成功刷新列表并关闭弹窗', async () => {
    const user = userEvent.setup();
    const request = deferred<Tenant>();
    mocks.apiRequest.mockReturnValue(request.promise);
    renderPage();
    await screen.findByRole('row', { name: /租户 A/ });
    await user.click(screen.getByRole('button', { name: '创建租户' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByRole('textbox', { name: '租户名称' }), '新租户');
    const form = within(dialog).getByRole('button', { name: '创建' }).closest('form')!;
    await user.click(within(dialog).getByRole('button', { name: '创建' }));
    expect(within(dialog).getByRole('button', { name: '创建中…' })).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: '取消' })).toBeDisabled();
    expect(within(dialog).getByRole('textbox', { name: '租户名称' })).toBeDisabled();
    fireEvent.submit(form);
    fireEvent.click(within(dialog).getByRole('button', { name: '创建中…' }));
    await user.keyboard('{Escape}');
    fireEvent.click(document.querySelector('.MuiDialog-root .MuiBackdrop-root')!);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(mocks.apiRequest).toHaveBeenCalledTimes(1);
    expect(mocks.apiRequest).toHaveBeenCalledWith('/admin/tenants', { method: 'POST', body: { name: '新租户' } });

    const created = { ...tenantA, id: 'tenant-new', name: '新租户' };
    tenants = [...tenants, created];
    await act(async () => request.resolve(created));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(await screen.findByRole('row', { name: /新租户/ })).toBeInTheDocument();
    expect(mocks.swrFetcher.mock.calls.filter(([path]) => path === '/admin/tenants')).toHaveLength(2);
  });

  it('创建租户失败用 Alert 反馈并保留输入，重试恢复 pending 并成功刷新', async () => {
    const user = userEvent.setup();
    const request = deferred<Tenant>();
    mocks.apiRequest.mockRejectedValueOnce(new Error('租户名称已存在')).mockReturnValueOnce(request.promise);
    renderPage();
    await user.click(screen.getByRole('button', { name: '创建租户' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByRole('textbox', { name: '租户名称' }), '新租户');
    await user.click(within(dialog).getByRole('button', { name: '创建' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('租户名称已存在');
    expect(within(dialog).getByDisplayValue('新租户')).toBeEnabled();
    expect(within(dialog).getByRole('button', { name: '创建' })).toBeEnabled();
    expect(mocks.swrFetcher.mock.calls.filter(([path]) => path === '/admin/tenants')).toHaveLength(1);
    await user.click(within(dialog).getByRole('button', { name: '创建' }));
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: '创建中…' })).toBeDisabled();
    await act(async () => request.resolve(tenantA));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mocks.apiRequest).toHaveBeenCalledTimes(2);
    expect(mocks.swrFetcher.mock.calls.filter(([path]) => path === '/admin/tenants')).toHaveLength(2);
  });

  it('非 Error 创建租户异常有默认 Alert，关闭重开不保留旧错误', async () => {
    const user = userEvent.setup();
    mocks.apiRequest.mockRejectedValue(null);
    renderPage();
    await user.click(screen.getByRole('button', { name: '创建租户' }));
    let dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByRole('textbox', { name: '租户名称' }), '新租户');
    await user.click(within(dialog).getByRole('button', { name: '创建' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('创建租户失败');
    await user.click(within(dialog).getByRole('button', { name: '取消' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: '创建租户' }));
    dialog = screen.getByRole('dialog');
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
  });
});
