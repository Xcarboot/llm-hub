import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { DashboardData } from '../types';
import { DashboardPage } from './DashboardPage';

const mocks = vi.hoisted(() => ({ useSWR: vi.fn(), barChart: vi.fn() }));
vi.mock('swr', () => ({ default: mocks.useSWR }));
vi.mock('@mui/x-charts', () => ({
  LineChart: () => null,
  BarChart: (props: unknown) => { mocks.barChart(props); return null; }
}));

const data: DashboardData = {
  kpis: { tenant_count: 1, active_key_count: 1, request_count: 3, success_count: 3, success_rate: 1, charged_micro_cny: '9007199254740993', prompt_tokens: 1, completion_tokens: 1 },
  trend: [
    { date: '2026-01-01', requests: 1, successes: 1, prompt_tokens: 1, completion_tokens: 1, charged_micro_cny: '86' },
    { date: '2026-01-02', requests: 1, successes: 1, prompt_tokens: 1, completion_tokens: 1, charged_micro_cny: '1900000' },
    { date: '2026-01-03', requests: 1, successes: 1, prompt_tokens: 1, completion_tokens: 1, charged_micro_cny: '9007199254740993' }
  ]
};

type SpendChart = {
  series: Array<{ data: number[]; valueFormatter: (value: number | null, context: { dataIndex: number }) => string }>;
  yAxis: Array<{ valueFormatter: (value: number) => string }>;
};

beforeEach(() => {
  mocks.barChart.mockClear();
  mocks.useSWR.mockImplementation((key: string) => ({ data: key.startsWith('/admin/') ? data : { status: 'ok' } }));
});

describe('Dashboard 每日消费图表', () => {
  it('86 micro-CNY 显示 0.000086，1900000 显示 1.9，保留六位小数', () => {
    render(<DashboardPage />);
    const chart = mocks.barChart.mock.calls[0][0] as SpendChart;
    expect(chart.series[0].data.slice(0, 2)).toEqual([0.000086, 1.9]);
    expect(chart.yAxis[0].valueFormatter(chart.series[0].data[0])).toBe('0.000086');
    expect(chart.yAxis[0].valueFormatter(chart.series[0].data[1])).toBe('1.9');
    expect(screen.getByText('每日消费（人民币）')).toBeInTheDocument();
    expect(screen.queryByText(/人民币整数/)).not.toBeInTheDocument();
  });

  it('tooltip 用原始 micro-CNY 精确格式化，不从浮点图表值反推', () => {
    render(<DashboardPage />);
    const { series } = mocks.barChart.mock.calls[0][0] as SpendChart;
    expect(series[0].valueFormatter(series[0].data[0], { dataIndex: 0 })).toBe('¥0.000086');
    expect(series[0].valueFormatter(series[0].data[1], { dataIndex: 1 })).toBe('¥1.900000');
    expect(series[0].valueFormatter(0, { dataIndex: 2 })).toBe('¥9,007,199,254.740993');
  });
});
