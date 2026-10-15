import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { SWRConfig, useSWRConfig } from 'swr';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { configureApiAuth } from '../api/client';
import { CustomerActivationDialog } from './CustomerActivationDialog';

let cache!: ReturnType<typeof useSWRConfig>['cache'];
function Harness() {
  cache = useSWRConfig().cache;
  const [open, setOpen] = useState(true);
  return <><button onClick={() => setOpen(true)}>再次开通</button><CustomerActivationDialog open={open} tenantId="tenant-a" tenantName="客户A" onClose={() => setOpen(false)} /></>;
}
afterEach(() => { configureApiAuth(() => null, () => undefined); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe('管理员一次性客户激活链接', () => {
  it('只POST email不设默认密码，pending阻止重复，link仅弹窗内存，关闭立即清空', async () => {
    configureApiAuth(() => 'admin-memory', vi.fn()); const storage = vi.spyOn(Storage.prototype, 'setItem');
    let release!: (value: Response) => void;
    const late = new Promise<Response>((done) => { release = done; }); const fetcher = vi.fn().mockReturnValue(late); vi.stubGlobal('fetch', fetcher);
    const user = userEvent.setup(); render(<SWRConfig value={{ provider: () => new Map() }}><Harness /></SWRConfig>);
    await user.type(screen.getByLabelText(/客户邮箱/), 'a@example.com');
    await user.click(screen.getByRole('button', { name: '生成激活链接' }));
    const pending = screen.getByRole('button', { name: '生成中…' }); expect(pending).toBeDisabled(); fireEvent.submit(pending.closest('form')!); expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][0]).toBe('/api/admin/tenants/tenant-a/customer-activation'); expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ email: 'a@example.com' });
    const link = 'http://localhost:3000/portal/activate#token=one-time-private';
    release(new Response(JSON.stringify({ activation_url: link, tenant_id: 'tenant-a', email: 'a@example.com', expires_at: '2026-12-01T00:00:00Z' })));
    expect(await screen.findByDisplayValue(link)).toHaveAttribute('readonly'); expect(storage).not.toHaveBeenCalled();
    expect(JSON.stringify([...cache.keys()].map((key) => [key, cache.get(key)]))).not.toContain('one-time-private');
    await user.click(screen.getByRole('button', { name: '已安全交付，关闭' })); expect(screen.queryByDisplayValue(link)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: '再次开通' })); expect(screen.queryByDisplayValue(link)).not.toBeInTheDocument();
  });
});
