import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { OneTimeKeyDialog } from './OneTimeKeyDialog';

function Harness() {
  const [open, setOpen] = useState(true);
  const [secret, setSecret] = useState<string | null>('gw_one_time_secret');
  return <><button onClick={() => setOpen(true)}>重新打开</button><OneTimeKeyDialog open={open} secret={secret} onClose={() => { setOpen(false); setSecret(null); }} /></>;
}

describe('一次性 API Key 对话框', () => {
  it('关闭时清空且重新打开不可再看', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    expect(screen.getByDisplayValue('gw_one_time_secret')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '我已保存，关闭' }));
    expect(screen.queryByDisplayValue('gw_one_time_secret')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: '重新打开' })).toBeVisible());
    await user.click(screen.getByRole('button', { name: '重新打开' }));
    expect(screen.queryByDisplayValue('gw_one_time_secret')).not.toBeInTheDocument();
  });
});
