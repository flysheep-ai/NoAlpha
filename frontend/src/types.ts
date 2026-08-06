export type ProviderHealth = {
  provider: string;
  status: "healthy" | "degraded" | "unavailable" | "unauthorized" | "rate_limited";
  requires_credentials: boolean;
  supports_historical: boolean;
  supports_realtime: boolean;
  supports_intraday: boolean;
  coverage: string;
  reason?: string | null;
};

export type Evidence = {
  evidence_id: string;
  evidence_type: "market_bar" | "filing" | "financial_fact" | "news" | "event";
  source: string;
  source_url?: string | null;
  event_time: string;
  published_at?: string | null;
  available_at: string;
  retrieved_at: string;
  content: string;
  metadata: Record<string, unknown>;
};

export type Claim = {
  claim_id: string;
  text: string;
  claim_type: "observation" | "inference" | "hypothesis";
  evidence_ids: string[];
  inference_level: string;
  confidence: number;
};

export type ResearchReport = {
  investigation_id: string;
  subject: {
    symbol: string;
    company_name: string;
    exchange: string;
    market: string;
    currency: string;
    cik?: string | null;
  };
  generated_at: string;
  as_of: string;
  summary: string;
  bull_case: string[];
  bear_case: string[];
  risks: string[];
  unknowns: string[];
  evidence_refs: string[];
  confidence: number;
  confidence_reasons: string[];
  confidence_breakdown: Record<string, number>;
  claims: Claim[];
  quality_report: Record<string, unknown>;
  timeline: TimelineItem[];
  detected_events: DetectedEvent[];
};

export type TimelineItem = {
  timestamp: string;
  evidence_id: string;
  title: string;
  source: string;
  source_url?: string | null;
  metadata: Record<string, unknown>;
};

export type DetectedEvent = {
  event_id: string;
  event_type: string;
  event_time: string;
  description: string;
  severity: number;
  evidence_refs: string[];
  metadata: Record<string, unknown>;
};

export type StageProgressItem = {
  stage: string;
  status: "pending" | "running" | "completed" | "skipped" | "degraded" | "failed";
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number;
  message: string;
  metrics: Record<string, unknown>;
  error: string | null;
};

export type InvestigationProgress = {
  investigation_id: string;
  status: string;
  current_stage: string;
  stage_index: number;
  total_stages: number;
  started_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
  stages: Record<string, StageProgressItem>;
  provider_status: Record<string, string>;
  warnings: string[];
  degraded: boolean;
};

export type Investigation = {
  investigation_id: string;
  symbol: string;
  status: "queued" | "running" | "completed" | "failed";
  created_at: string;
  updated_at: string;
  steps: Array<{ key: string; label: string; status: string }>;
  error?: string | null;
  report?: ResearchReport;
  quality_report?: Record<string, unknown>;
  provider_warnings?: string[];
  evidence_count?: number;
  research_mode?: string;
  llm_model?: string;
  llm_provider?: string;
  progress?: InvestigationProgress;
};

export type InvestigationSummary = {
  investigation_id: string;
  symbol: string;
  status: string;
  created_at: string;
  updated_at: string;
  generated_at?: string;
  confidence?: number;
  evidence_count: number;
  claim_count: number;
  provider_statuses: Record<string, string>;
  unknowns_count: number;
};

export type CaseSummary = {
  case_id: string;
  symbol: string;
  company_name: string;
  status: string;
  severity: number;
  primary_trigger: string;
  updated_at: string;
  opened_at: string;
  confidence: number;
  current_thesis: string;
  counter_thesis?: string;
  unknowns?: string[];
  latest_report_id?: string;
  evidence_ids?: string[];
  trigger_cluster_ids?: string[];
  cooldown_until?: string | null;
};

export type WatchItem = {
  symbol: string;
  enabled: boolean;
  priority: number;
  policy: Record<string, unknown>;
  last_scanned_at?: string | null;
  cooldown_until?: string | null;
};

export type EvalPayload = Record<string, unknown>;

export type ProviderSetting = {
  provider: string;
  name: string;
  description: string;
  requires_credentials: boolean;
  credential_key: string | null;
  configured: boolean;
  credential_preview: string | null;
};

export type LLMSettings = {
  base_url: string;
  model: string;
  api_key_configured: boolean;
  api_key_preview: string;
  api_key_source: "credentials" | "env" | "none";
  credential_keys: { base_url: string; api_key: string; model: string };
};

export type SettingsPayload = {
  llm: LLMSettings;
  providers: ProviderSetting[];
};
