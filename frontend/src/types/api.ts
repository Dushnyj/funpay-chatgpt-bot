export interface StatusResponse {
  status: string
}

export interface Tier {
  id: number
  name: string
  description: string | null
  is_active: boolean
}

export interface TierCreate {
  name: string
  description?: string
  is_active?: boolean
}

export interface Duration {
  id: number
  days: number
  is_enabled: boolean
  sort_order: number
}

export interface LimitScope {
  id: number
  code: string
  name: string
}

export interface AccountLimits {
  account_id: number
  chat_5h_remaining_pct: number | null
  chat_weekly_remaining_pct: number | null
  codex_5h_remaining_pct: number | null
  codex_weekly_remaining_pct: number | null
  refresh_status: string
  measured_at: string | null
}

export interface Account {
  id: number
  login: string
  tier_id: number
  email: string | null
  subscription_expires_at: string | null
  max_active_rentals: number | null
  status: string
  notes: string | null
}

export interface AccountCreate {
  login: string
  password: string
  totp_secret?: string
  email?: string
  email_password?: string
  tier_id: number
  subscription_expires_at?: string
  max_active_rentals?: number
  notes?: string
}

export interface TotpExport {
  secret: string
  otpauth_uri: string
  qr_png_base64: string
}

export interface PriceMatrixItem {
  tier_id: number
  duration_id: number
  limit_scope_id: number
  min_limit_pct?: number
  max_5h_pct?: number
  max_weekly_pct?: number
  price: number
}

export interface MessageTemplate {
  key: string
  lang: string
  content: string
}

export interface Lot {
  id: number
  funpay_id: string | null
  funpay_node_id: number | null
  tier_id: number
  duration_id: number
  limit_scope_id: number
  min_limit_pct: number | null
  max_5h_pct: number | null
  max_weekly_pct: number | null
  price: number
  title_ru: string
  title_en: string
  status: string
  auto_created: boolean
}

export interface Order {
  id: number
  funpay_order_id: string
  funpay_chat_id: string
  buyer_funpay_id: string
  buyer_locale: string
  lot_id: number | null
  tier_id: number | null
  duration_id: number | null
  limit_scope_id: number | null
  price: number
  status: string
  created_at: string
}

export interface Rental {
  id: number
  order_id: number
  account_id: number
  buyer_funpay_id: string
  buyer_funpay_chat_id: string
  tier_id: number
  duration_id: number
  limit_scope_id: number
  lang: string
  started_at: string
  expires_at: string
  status: string
  replacement_count: number
}

export interface Settings {
  funpay_node_id: number | null
  auto_bump_enabled: boolean
  bump_interval_hours: number
  default_max_active_rentals: number
  funpay_commission_percent: number
  check_interval_minutes: number
  limits_check_interval_minutes: number
  limits_warn_threshold_pct: number
}

export interface Metrics {
  active_rentals: number
  available_accounts: number
  orders_today: number
  revenue_brutto: number
  revenue_netto: number
  bot_status: string
}
