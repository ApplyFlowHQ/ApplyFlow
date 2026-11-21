export type SetupStatusCode =
  | 'verified'
  | 'saved_not_tested'
  | 'needs_attention'
  | 'not_configured'
  | 'skipped'
  | 'disabled'
  | 'inherit'
  | string;

export interface ValidationEntry {
  ok?: boolean;
  status: SetupStatusCode;
  message: string;
  checked_at?: string;
  details?: Record<string, any>;
}

export interface ModelOption {
  id: string;
  label: string;
}

export interface SetupStatusResponse {
  claimed: boolean;
  requires_claim: boolean;
  setup_state: 'unclaimed' | 'in_progress' | 'completed';
  installation_kind: 'fresh' | 'resumed' | 'existing';
  can_manage_installation: boolean;
  is_owner?: boolean;
  google_signin_enabled: boolean;
  current_step?: number;
  completed_steps?: number[];
  skipped_steps?: number[];
  deployment_mode?: 'local' | 'hosted';
  default_locale?: 'en' | 'ar';
  default_timezone?: string;
  supported_models?: {
    openai: ModelOption[];
    gemini: ModelOption[];
  };
  shared_capabilities?: {
    allow_shared_ai_for_users: boolean;
    allow_shared_search_for_users: boolean;
    google_oauth_available: boolean;
    ms_graph_available: boolean;
  };
  user_integrations?: {
    ai: {
      provider: 'inherit' | 'openai' | 'gemini' | 'none';
      model: string;
      has_openai_key: boolean;
      masked_openai_key: string;
      has_gemini_key: boolean;
      masked_gemini_key: string;
      effective_provider: string;
      effective_model: string;
      effective_source: string;
      locked_by_env: boolean;
      validation?: ValidationEntry | null;
    };
    search: {
      provider: 'inherit' | 'tavily' | 'brave' | 'both' | 'none';
      has_tavily_key: boolean;
      masked_tavily_key: string;
      has_brave_key: boolean;
      masked_brave_key: string;
      effective_provider: string;
      effective_source: string;
      locked_by_env: boolean;
      validation?: ValidationEntry | null;
    };
  };
  readiness?: {
    database: { status: SetupStatusCode; dialect: string; message: string };
    worker: { status: SetupStatusCode; mode: string; broker_reachable: boolean; workers_online: number; message: string };
    ai: { status: SetupStatusCode; provider: string; model: string; source: string; message: string };
    search: { status: SetupStatusCode; provider: string; source: string; message: string };
    mailbox: { status: SetupStatusCode; connected_count: number; granted_scopes: string[]; message: string };
    transactional_email: { status: SetupStatusCode; provider: string; message: string };
    optional_sheets: { status: SetupStatusCode; message: string };
    live_sending: { status: SetupStatusCode; enabled: boolean; source: string; message: string };
  };
  installation?: {
    config_version: number;
    lock_env_config: boolean;
    allow_shared_ai_for_users: boolean;
    allow_shared_search_for_users: boolean;
    live_sending_enabled: boolean;
    live_sending_source: string;
    ai: {
      provider: 'openai' | 'gemini' | 'none';
      model: string;
      effective_provider: string;
      effective_source: string;
      locked_by_env: boolean;
      has_openai_key: boolean;
      masked_openai_key: string;
      has_gemini_key: boolean;
      masked_gemini_key: string;
      validation?: ValidationEntry | null;
    };
    search: {
      provider: 'tavily' | 'brave' | 'both' | 'none';
      effective_provider: string;
      effective_source: string;
      locked_by_env: boolean;
      has_tavily_key: boolean;
      masked_tavily_key: string;
      has_brave_key: boolean;
      masked_brave_key: string;
      validation?: ValidationEntry | null;
    };
    google_oauth: {
      client_id: string;
      has_client_secret: boolean;
      masked_client_secret: string;
      redirect_uri_auth: string;
      redirect_uri_gmail: string;
      redirect_uri_sheets: string;
      source: string;
      locked_by_env: boolean;
      validation?: ValidationEntry | null;
    };
    ms_graph: {
      client_id: string;
      tenant_id: string;
      has_client_secret: boolean;
      masked_client_secret: string;
      redirect_uri: string;
      source: string;
      locked_by_env: boolean;
      validation?: ValidationEntry | null;
    };
    transactional_email: {
      provider: 'console' | 'smtp';
      from_email: string;
      smtp_host: string;
      smtp_port: number;
      smtp_username: string;
      smtp_use_tls: boolean;
      has_smtp_password: boolean;
      masked_smtp_password: string;
      source: string;
      locked_by_env: boolean;
      validation?: ValidationEntry | null;
    };
  };
}
