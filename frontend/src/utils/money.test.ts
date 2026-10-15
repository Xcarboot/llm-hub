import { describe, expect, it } from 'vitest';
import { formatMicroCny, microCnyToChartYuan, parseMicroInput, yuanToMicro } from './money';

describe('micro-CNY 金额工具', () => {
  it('使用整数精确格式化正负金额', () => {
    expect(formatMicroCny('1234567890123')).toBe('¥1,234,567.890123');
    expect(formatMicroCny('-42')).toBe('-¥0.000042');
    expect(formatMicroCny(0n)).toBe('¥0.000000');
  });

  it('图表转换保留整数与余数，不截断六位小数', () => {
    expect(microCnyToChartYuan('86')).toBe(0.000086);
    expect(microCnyToChartYuan('1900000')).toBe(1.9);
    expect(microCnyToChartYuan('-86')).toBe(-0.000086);
    expect(microCnyToChartYuan('-1900000')).toBe(-1.9);
    expect(microCnyToChartYuan('0')).toBe(0);
    expect(() => microCnyToChartYuan(Number.MAX_SAFE_INTEGER + 1)).toThrow('安全整数');
  });

  it('把人民币十进制字符串精确转换为 micro-CNY', () => {
    expect(yuanToMicro('12.3456')).toBe('12345600');
    expect(yuanToMicro('0.000001')).toBe('1');
    expect(yuanToMicro('0002.1')).toBe('2100000');
  });

  it('拒绝浮点精度和非法 micro-CNY 输入', () => {
    expect(() => yuanToMicro('1.0000001')).toThrow('最多 6 位小数');
    expect(() => yuanToMicro('-1')).toThrow();
    expect(() => parseMicroInput('1.2')).toThrow('必须是非负整数');
  });
});
