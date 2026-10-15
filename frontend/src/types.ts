export interface PageResult<T> {
  items: T[];
  next_cursor?: string | null;
  has_more?: boolean;
}

export interface PortalBalance {
  available_micro_cny: string;
  frozen_micro_cny: string;
  refund_frozen_micro_cny: string;
  total_micro_cny: string;
}

export interface PaymentConfig { mode: string; enabled: boolean; mock: boolean; notice: string }
export interface PaymentOrder {
  id: string;
  mode: string;
  status: string;
  amount_micro_cny: string;
  refundable_micro_cny: string;
  refunded_micro_cny: string;
  refund_frozen_micro_cny: string;
  out_trade_no: string;
  created_at: string;
  tenant_id?: string;
  tenant?: { id: string; name: string; email?: string | null };
}
export interface RefundRequest {
  id: string;
  status: string;
  amount_micro_cny: string;
  payment_order_id: string;
  reason: string;
  decision_reason?: string | null;
  created_at: string;
  out_request_no: string;
  mode?: string;
  tenant_id?: string;
  tenant?: { id: string; name: string; email?: string | null };
}

export interface Tenant {
  id: string;
  name: string;
  active: boolean;
  created_at: string;
  key_count: number;
  request_count: number;
  last_request_at?: string | null;
  available_micro_cny: string;
  frozen_micro_cny: string;
}

export interface ApiKeySummary {
  id: string;
  prefix: string;
  revoked: boolean;
  created_at: string;
}

export interface ModelRoute {
  id: string;
  public_name: string;
  litellm_model: string;
  route_order: number;
  active: boolean;
  created_at: string;
  prices: PriceVersion[];
}

export interface PriceVersion {
  id: string;
  input_micro_cny_per_million: string;
  output_micro_cny_per_million: string;
  effective_at: string;
}

export interface LedgerEntry {
  account_kind: string;
  account_scope: string;
  amount_micro_cny: string;
}

export interface LedgerTransfer {
  transfer: {
    id: string;
    kind: string;
    external_ref: string;
    posted: boolean;
    created_at: string;
  };
  reversal?: { reversal_of_id?: string | null; reversed_by_id?: string | null } | null;
  entries: LedgerEntry[];
}

export interface GatewayRequest {
  id: string;
  tenant: { id: string; name: string };
  model: string;
  stream: boolean;
  status: string;
  usage_json?: Record<string, string | number | boolean> | null;
  error_code?: string | null;
  created_at: string;
}

export interface Reservation {
  id: string;
  tenant_id: string;
  request_id: string;
  amount_micro_cny: string;
  status: string;
  selected_price?: PriceVersion | null;
  expires_at: string;
  created_at: string;
}

export interface DashboardData {
  kpis: {
    tenant_count: number;
    active_key_count: number;
    request_count: number;
    success_count: number;
    success_rate: number;
    charged_micro_cny: string;
    prompt_tokens: number;
    completion_tokens: number;
  };
  trend: Array<{
    date: string;
    requests: number;
    successes: number;
    charged_micro_cny: string;
    prompt_tokens: number;
    completion_tokens: number;
  }>;
}
