import { ContentCopyOutlined, WarningAmberOutlined } from '@mui/icons-material';
import { Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, TextField } from '@mui/material';
import { useState } from 'react';

interface Props {
  open: boolean;
  secret: string | null;
  onClose: () => void;
}

export function OneTimeKeyDialog({ open, secret, onClose }: Props) {
  const [copyError, setCopyError] = useState(false);
  // No duplicate secret state: clearing the parent's value immediately clears the exiting dialog.
  const visibleSecret = open ? secret : null;
  const close = () => { setCopyError(false); onClose(); };
  const copy = async () => {
    if (!visibleSecret) return;
    try { await navigator.clipboard.writeText(visibleSecret); setCopyError(false); }
    catch { setCopyError(true); }
  };
  return <Dialog open={open} onClose={close} maxWidth="sm" fullWidth>
    <DialogTitle>请立即保存 API Key</DialogTitle>
    <DialogContent><Stack spacing={2} pt={1}>
      <Alert severity="warning" icon={<WarningAmberOutlined />}>此 Key 仅展示一次。关闭后将从页面内存清除，无法再次查看。</Alert>
      {copyError && <Alert severity="error">复制失败，请手动复制并安全保存。</Alert>}
      <TextField label="API Key" value={visibleSecret ?? ''} slotProps={{ input: { readOnly: true } }} fullWidth />
      <Button startIcon={<ContentCopyOutlined />} onClick={() => void copy()} disabled={!visibleSecret}>复制 Key</Button>
    </Stack></DialogContent>
    <DialogActions><Button variant="contained" onClick={close}>我已保存，关闭</Button></DialogActions>
  </Dialog>;
}
