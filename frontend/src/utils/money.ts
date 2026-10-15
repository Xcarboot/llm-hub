const MICRO_SCALE = 6;

function normalizeInteger(value: string | bigint | number): string {
  if (typeof value === 'bigint') return value.toString();
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) throw new Error('金额必须是安全整数或字符串');
    return String(value);
  }
  if (!/^-?\d+$/.test(value.trim())) throw new Error('无效的 micro-CNY 金额');
  return value.trim();
}

export function formatMicroCny(value: string | bigint | number): string {
  const raw = normalizeInteger(value);
  const negative = raw.startsWith('-');
  const digits = (negative ? raw.slice(1) : raw).replace(/^0+(?=\d)/, '');
  const padded = digits.padStart(MICRO_SCALE + 1, '0');
  const integer = padded.slice(0, -MICRO_SCALE).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const fraction = padded.slice(-MICRO_SCALE);
  return `${negative ? '-' : ''}¥${integer}.${fraction}`;
}

// Approximate only at the chart boundary; accounting and tooltips use exact integers.
export function microCnyToChartYuan(value: string | bigint | number): number {
  const micro = BigInt(normalizeInteger(value));
  return Number(micro / 1_000_000n) + Number(micro % 1_000_000n) / 1_000_000;
}

export function yuanToMicro(value: string): string {
  const trimmed = value.trim();
  const match = /^(\d+)(?:\.(\d{1,6}))?$/.exec(trimmed);
  if (!match) throw new Error('请输入非负人民币金额，最多 6 位小数');
  const integer = match[1].replace(/^0+(?=\d)/, '');
  const fraction = (match[2] ?? '').padEnd(MICRO_SCALE, '0');
  return (BigInt(integer) * 1_000_000n + BigInt(fraction || '0')).toString();
}

export function parseMicroInput(value: string): string {
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) throw new Error('micro-CNY 必须是非负整数');
  return BigInt(trimmed).toString();
}

export function portalAmountToMicro(value: string, maximum = '1000000000'): string {
  if (!/^\d+(?:\.\d{1,2})?$/.test(value.trim())) throw new Error('请输入整分人民币金额，最多两位小数');
  const micro = yuanToMicro(value);
  if (BigInt(micro) <= 0n || BigInt(micro) > BigInt(maximum) || BigInt(micro) > 1_000_000_000n) {
    throw new Error('金额须大于 0、不超过可退额度且最高 1000 元');
  }
  return micro;
}

export function refundableWholeMicro(value: string): string {
  const micro = BigInt(normalizeInteger(value));
  return (micro > 0n ? micro / 10_000n * 10_000n : 0n).toString();
}
