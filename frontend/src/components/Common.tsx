import { Alert, Box, Button, Chip, CircularProgress, Stack, Typography } from '@mui/material';
import type { PropsWithChildren, ReactNode } from 'react';

export function PageHeader({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return (
    <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" alignItems={{ xs: 'stretch', sm: 'center' }} spacing={2} mb={3}>
      <Box>
        <Typography variant="h4">{title}</Typography>
        <Typography color="text.secondary" mt={0.5}>{description}</Typography>
      </Box>
      {action}
    </Stack>
  );
}

export function LoadingState() {
  return <Stack alignItems="center" py={8}><CircularProgress size={32} aria-label="加载中" /></Stack>;
}

export function ErrorState({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : '加载失败';
  return <Alert severity="error">{message}</Alert>;
}

export function EmptyState({ children = '暂无数据' }: PropsWithChildren) {
  return <Typography color="text.secondary" align="center" py={5}>{children}</Typography>;
}

export function StatusChip({ value }: { value: string | boolean }) {
  const normalized = String(value).toUpperCase();
  const success = value === true || ['ACTIVE', 'COMPLETED', 'SETTLED', 'RELEASED', 'SUCCESS', 'READY', 'LIVE'].includes(normalized);
  const warning = ['PROCESSING', 'RESERVED', 'DISPATCHED', 'NEW', 'CONSERVATIVE_SETTLED'].includes(normalized);
  const labelMap: Record<string, string> = {
    ACTIVE: '有效', INACTIVE: '停用', COMPLETED: '完成', SETTLED: '已结算', RELEASED: '已释放',
    PROCESSING: '处理中', RESERVED: '已预占', DISPATCHED: '已派发', NEW: '新建', FAILED: '失败',
    REVERSED: '已冲正', CONSERVATIVE_SETTLED: '保守结算', TRUE: '正常', FALSE: '停用'
  };
  return <Chip size="small" variant="outlined" color={success ? 'success' : warning ? 'warning' : 'default'} label={labelMap[normalized] ?? value} />;
}

export function CursorPager({ canBack, canNext, onBack, onNext }: { canBack: boolean; canNext: boolean; onBack: () => void; onNext: () => void }) {
  return (
    <Stack direction="row" justifyContent="flex-end" spacing={1} mt={2}>
      <Button variant="outlined" disabled={!canBack} onClick={onBack}>上一页</Button>
      <Button variant="outlined" disabled={!canNext} onClick={onNext}>下一页</Button>
    </Stack>
  );
}

export function formatDate(value?: string | null): string {
  if (!value) return '—';
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));
}
