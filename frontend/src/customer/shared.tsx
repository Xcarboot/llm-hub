import { Alert, Box, Chip, Card } from '@mui/material';
import { useRef, useState, type ReactNode } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import { queryString } from '../api/client';
import { CursorPager, EmptyState, ErrorState, LoadingState } from '../components/Common';
import type { PageResult } from '../types';

export const MOCK_NOTICE = '本地模拟，不收真实资金，仅用于测试；不会跳转支付宝';
export const financialStatuses: Record<string, string> = {
  NEW: '新建', CREATED: '待支付', PENDING: '待处理', PENDING_PAYMENT: '待支付', UNPAID: '待支付',
  PAID: '已支付', SUCCESS: '成功', COMPLETED: '已完成', CONFIRMED: '已确认', CLOSED: '已关闭',
  CANCELLED: '已取消', EXPIRED: '已过期', FAILED: '失败', REFUNDED: '已退款', PARTIALLY_REFUNDED: '部分退款',
  REQUESTED: '待审批', PENDING_REVIEW: '待审批', PENDING_APPROVAL: '待审批', APPROVED: '已批准，待退款',
  REJECTED: '已拒绝', PROCESSING: '原路退款处理中', REFUNDING: '原路退款处理中', SUCCEEDED: '已退款',
  REFUND_FAILED: '退款失败', MANUAL_REVIEW: '待人工核查'
};
export function FinancialStatus({ value }: { value: string }) {
  const status = value.toUpperCase();
  return <Chip size="small" variant="outlined" label={financialStatuses[status] ?? '未知状态（请联系管理员）'} color={['PAID', 'SUCCESS', 'SUCCEEDED', 'REFUNDED', 'COMPLETED'].includes(status) ? 'success' : ['FAILED', 'REJECTED', 'REFUND_FAILED'].includes(status) ? 'error' : 'default'} />;
}
export function ModeNotice({ mode }: { mode?: string }) {
  return mode === 'mock' ? <Alert severity="warning">{MOCK_NOTICE}</Alert> : <Alert severity="info">{mode === 'alipay_sandbox' ? '支付宝沙箱尚未连接，不可用；不会发起真实支付或退款。' : '真实支付及退款尚不可用，请联系管理员。'}</Alert>;
}
export function useCursorPage<T>(path: string, filters: Record<string, string | undefined> = {}, refreshInterval = 0) {
  const [cursor, setCursor] = useState<string>();
  const [history, setHistory] = useState<string[]>([]);
  const key = `${path}${queryString({ ...filters, limit: '25', cursor })}`;
  const list = useSWR<PageResult<T>>(key, { refreshInterval });
  const reset = () => { setCursor(undefined); setHistory([]); };
  const pager = <CursorPager canBack={history.length > 0} canNext={Boolean(list.data?.next_cursor) && list.data?.has_more !== false}
    onBack={() => { setCursor(history.at(-1) || undefined); setHistory((old) => old.slice(0, -1)); }}
    onNext={() => { if (list.data?.next_cursor) { setHistory((old) => [...old, cursor ?? '']); setCursor(list.data.next_cursor); } }} />;
  return { ...list, key, pager, reset };
}
export function ListCard({ list, children }: { list: { isLoading: boolean; error?: unknown; data?: PageResult<unknown>; pager: ReactNode }; children: ReactNode }) {
  return <Card><Box p={2}>{list.isLoading ? <LoadingState /> : list.error ? <ErrorState error={list.error} /> : <>{children}{!list.data?.items.length && <EmptyState />}{list.pager}</>}</Box></Card>;
}
export function useFinancialRefresh() {
  const { mutate } = useSWRConfig();
  return () => mutate((key) => typeof key === 'string' && ['/portal/balance', '/portal/payments', '/portal/refunds', '/portal/ledger/transfers'].some((prefix) => key === prefix || key.startsWith(`${prefix}?`) || key.startsWith(`${prefix}/`)), undefined, { revalidate: true });
}
export function useStableIntent() {
  const intent = useRef<{ fingerprint: string; id: string } | null>(null);
  return {
    key: (payload: unknown) => {
      const fingerprint = JSON.stringify(payload);
      if (!intent.current || intent.current.fingerprint !== fingerprint) intent.current = { fingerprint, id: crypto.randomUUID() };
      return intent.current.id;
    },
    reset: () => { intent.current = null; }
  };
}
