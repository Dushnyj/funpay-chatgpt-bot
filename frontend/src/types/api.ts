export interface StatusResponse {
  status: string
}

export interface Tier {
  id: number
  code?: string
  name: string
  description: string | null
  is_active: boolean
  system_managed?: boolean
  is_sellable?: boolean
  sort_order?: number
  usage_multiplier?: number | null
}

export interface TierCreate {
  name: string
  description?: string
  is_active?: boolean
}

export interface TierUpdate {
  is_active?: boolean
  is_sellable?: boolean
}

export interface Duration {
  id: number
  minutes: number
  is_enabled: boolean
}

export interface DurationCreate {
  minutes: number
  is_enabled?: boolean
}

export interface DurationUpdate {
  is_enabled?: boolean
}

export interface LimitScope {
  id: number
  code: string
  name: string
  is_enabled: boolean
}

export interface LimitScopeUpdate {
  is_enabled?: boolean
}

export interface AccountLimits {
  account_id: number
  plan_type?: string | null
  plan_window_status: 'unknown' | 'ok' | 'mismatch'
  expected_long_window_seconds: number | null
  codex_5h_remaining_pct: number | null
  codex_weekly_remaining_pct: number | null
  codex_primary_remaining_pct: number | null
  codex_primary_window_seconds: number | null
  codex_primary_resets_at: string | null
  codex_secondary_remaining_pct: number | null
  codex_secondary_window_seconds: number | null
  codex_secondary_resets_at: string | null
  refresh_status: string
  measured_at: string | null
}

export interface Account {
  id: number
  login: string
  tier_id: number | null
  email: string | null
  subscription_expires_at: string | null
  max_active_rentals: number | null
  active_rentals_count: number
  replacement_reserved: boolean
  status: string
  operator_status_override?: 'maintenance' | 'disabled' | null
  notes: string | null
  plan_raw_type?: string | null
  plan_source?: string | null
  plan_confidence?: number | null
  plan_detected_at?: string | null
  email_oauth_connected?: boolean
  email_oauth_provider?: string | null
  email_oauth_status?: string | null
  validation_job?: AccountValidationJob | null
  limits?: AccountLimits | null
}

export interface EmailOAuthStart {
  authorization_url: string
  expires_at: string
}

export interface AccountValidationJob {
  id: number
  status: string
  job_type: string
  stage?: string | null
  error_code?: string | null
  error_detail?: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface AccountCreate {
  login: string
  password: string
  totp_secret?: string
  email?: string
  email_password?: string
  subscription_expires_at?: string
  max_active_rentals?: number
  notes?: string
}

export interface AccountUpdate {
  subscription_expires_at?: string | null
  max_active_rentals?: number | null
  status?: 'maintenance' | 'disabled'
  notes?: string | null
}

export interface AccountCredentialsUpdate {
  login?: string
  password?: string
  totp_secret?: string | null
  email?: string | null
  email_password?: string | null
}

export interface TotpExport {
  secret: string
  otpauth_uri: string
  qr_png_base64: string
}

export interface TotpCode {
  code: string
  seconds_remaining: number
}

export interface DeviceAuthSession {
  session_id: string
  verification_url: string
  user_code: string
  expires_at: string
  interval_seconds: number
}

export type DeviceAuthStatusValue = 'pending' | 'completed' | 'failed' | 'expired'

export interface DeviceAuthStatus {
  status: DeviceAuthStatusValue
  error_code?: string | null
  error_detail?: string | null
  account?: Account | null
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
  allowed_fields: string[]
  default_content: string | null
  is_custom: boolean
}

export interface LotTemplate {
  id: number
  key: string
  name: string
  tier_id: number | null
  limit_scope_id: number | null
  title_ru: string
  title_en: string
  description_ru: string
  description_en: string
  enabled: boolean
  system_managed: boolean
  is_custom: boolean
  default_title_ru: string | null
  default_title_en: string | null
  default_description_ru: string | null
  default_description_en: string | null
  allowed_fields: string[]
}

export interface LotTemplateUpdate {
  title_ru: string
  title_en: string
  description_ru: string
  description_en: string
  enabled: boolean
}

export interface LotTemplateCreate extends LotTemplateUpdate {
  key: string
  name: string
  tier_id: number | null
  limit_scope_id: number | null
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

export interface LotCreate {
  funpay_node_id?: number
  tier_id: number
  duration_id: number
  limit_scope_id: number
  min_limit_pct?: number
  max_5h_pct?: number
  max_weekly_pct?: number
  price: number
  title_ru: string
  title_en: string
  description_ru?: string
  description_en?: string
}

export interface LotStatusUpdate {
  status: 'active' | 'paused'
}

export interface LotSyncResult {
  status?: string
  created?: number
  updated?: number
  paused?: number
  total?: number
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
  min_limit_pct: number | null
  max_5h_pct: number | null
  max_weekly_pct: number | null
  price: number
  status: string
  fulfillment_attempts: number
  fulfillment_next_attempt_at: string | null
  fulfillment_last_error: string | null
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
  min_limit_pct: number | null
  max_5h_pct: number | null
  max_weekly_pct: number | null
  lang: string
  started_at: string
  expires_at: string
  status: string
  replacement_count: number
  credentials_delivery_status: 'sending' | 'sent' | 'failed' | 'manual'
  credentials_delivery_template: string
  credentials_delivery_started_at: string | null
  credentials_delivery_next_attempt_at: string | null
  credentials_delivered_at: string | null
  credentials_delivery_attempts: number
  credentials_delivery_last_error: string | null
  issued_codex_primary_pct: number | null
  issued_codex_primary_window_seconds: number | null
  issued_codex_primary_resets_at: string | null
  issued_codex_secondary_pct: number | null
  issued_codex_secondary_window_seconds: number | null
  issued_codex_secondary_resets_at: string | null
  issued_plan_window_status: string | null
  issued_expected_long_window_seconds: number | null
  issued_limits_measured_at: string | null
}

export interface Settings {
  funpay_node_id: number | null
  auto_bump_enabled: boolean
  bump_interval_hours: number
  default_max_active_rentals: number
  funpay_commission_percent: number
  check_interval_minutes: number
  limits_check_interval_minutes: number
  refresh_recover_concurrency: number
  refresh_max_attempts: number
  refresh_retry_delay_minutes: number
  check_delay_seconds: number
  limits_warn_threshold_pct: number
  graph_configured?: boolean
}

export interface Metrics {
  active_rentals: number
  available_accounts: number
  orders_today: number
  revenue_brutto: number
  revenue_netto: number
  bot_status: string
}

export interface FunPayKeyStatus {
  configured: boolean
  connected: boolean
  last4: string | null
}

export interface TelegramConfigStatus {
  configured: boolean
  token_last4: string | null
  seller_chat_id: string | null
}

export interface ChatSummary {
  id: number
  funpay_chat_id: string
  buyer_funpay_id: string | null
  funpay_order_id: string | null
  order_id: number | null
  unread_count: number
  last_message_text: string | null
  last_message_direction: 'incoming' | 'outgoing' | null
  last_message_at: string | null
}

export interface ChatMessage {
  id: number
  conversation_id: number
  funpay_message_id: string | null
  direction: 'incoming' | 'outgoing'
  sender_funpay_id: string | null
  text: string
  delivery_status: 'received' | 'pending' | 'sent' | 'failed'
  is_read: boolean
  created_at: string
}
