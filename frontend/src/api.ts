import type { CaseSummary, EvalPayload, Evidence, Investigation, InvestigationSummary, ProviderHealth, ProviderSetting, SettingsPayload, WatchItem } from "./types";

export interface MarketSearchItem {
  secid: string; symbol: string; name: string; pinyin: string;
  market: string; exchange: string; currency: string; security_type: string;
}
export interface MarketQuote {
  secid: string; symbol: string; exchange: string; market: string; canonical: string;
  name: string; price: number; open: number; high: number; low: number;
  previous_close: number; change: number; change_percent: number;
  volume: number; turnover: number; timestamp: string; received_at: string;
  source: string; currency: string; asset_type: string;
  freshness: string; delay_seconds: number; market_session: string;
}
export interface KlineBar { time: string; open: number; high: number; low: number; close: number; volume: number; turnover: number }
export interface MarketAnomaly {
  secid: string; symbol: string; name: string; window_seconds: number;
  window_return: number; volume_ratio: number; severity: string; price: number;
  detected_at: string; coverage: string;
}
export interface WatchlistItem {
  secid: string; symbol: string; name: string; market: string; exchange: string;
  currency: string; enabled: number; sort_order: number; quote?: MarketQuote | null;
}
export interface TradingSessionInfo {
  session_id: string; mode: string; status: string;
  initial_capital: number; current_equity: number; cash: number; currency: string;
  started_at: string; stopped_at: string; created_at: string;
  strategy_version: string; research_mode: string; allowed_markets: string[];
  universe: string; risk_profile: string; scan_interval: number;
  max_position_weight: number; max_daily_loss_pct: number; max_orders_per_day: number;
  allow_closed_market_fills: boolean;
  cycle_count: number; risk_rejections: number; last_cycle_at: string; next_cycle_at: string;
  error: string;
  portfolio?: any; theses?: any[];
  operational_state?: {
    phase: string; phase_label_zh: string; phase_label_en: string;
    home_market: string; monitored_markets: string[]; execution_markets: string[];
    last_heartbeat: string; current_activity: string;
    daily_report_count: number; overnight_event_count: number;
    thesis_updates_since_close: number;
  };
}
export interface DecisionEventInfo {
  event_id: string; session_id: string; cycle: number; timestamp: string;
  event_type: string; secid: string; symbol: string; message: string;
  payload: Record<string, any>;
}
export interface EquityPoint { session_id: string; cycle: number; timestamp: string; equity: number; cash: number; market_value: number; benchmark_value: number | null }

export interface PositionInfo {
  symbol: string; secid: string; name: string; market: string; currency: string;
  quantity: number; average_cost: number; market_price: number;
  market_value: number; unrealized_pnl: number; realized_pnl: number;
  weight: number; active_thesis_id: string; hold_rationale: string;
  opened_at?: string; updated_at?: string;
}

export interface DecisionStory {
  event_id: string; session_id: string; cycle: number; timestamp: string;
  symbol: string; message: string;
  payload: { proposal_id: string; decision_id: string; action: string; symbol: string;
    final_weight: number; quantity: number; price: number; amount: number;
    decision_quality: string; research_mode: string; status: string; };
  proposal_payload?: Record<string, any> | null;
}

export interface ThesisInfo {
  thesis_id: string; session_id: string; symbol: string; status: string;
  thesis_text: string; supporting_evidence_ids: string[]; risks: string[];
  invalidation_conditions: string[]; expected_horizon: string;
  confidence: number; proposal_id: string; created_at: string;
  updated_at: string; closed_at: string | null; exit_reason: string | null;
  exit_rationale: string | null; realized_pnl: number | null;
}

export interface TruthComponent {
  status: string; detail: string;
  [k: string]: any;
}
export interface SystemTruth {
  effective_mode: string;
  components: {
    market_data: TruthComponent; quant: TruthComponent; llm: TruthComponent;
    research: TruthComponent; risk: TruthComponent; virtual_broker: TruthComponent;
  };
  activity: { stage: string; target: string; cycle: number };
}
export interface ProposalRow {
  proposal_id: string; symbol: string; action: string; target_weight: number;
  confidence: number; status: string; decision_mode: string; created_at: string;
  updated_at?: string; cycle?: number; rationale?: Record<string, any>;
  risk_result?: Record<string, any>;
}

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  systemStatus: () => request<{
    providers: ProviderHealth[];
    watchlist: { count: number; items: number; enabled: number };
    cases: { total: number; open: number };
    investigations: { total: number; completed: number; running: number };
    recent_investigations: InvestigationSummary[];
    recent_cases: CaseSummary[];
  }>("/api/v1/system/status"),

  createInvestigation: (payload: { symbol: string; lookback_days: number; as_of?: string | null; research_mode?: string; report_language?: string }) =>
    request<{ investigation_id: string; status: string }>("/api/v1/investigations", {
      method: "POST",
      body: JSON.stringify(payload)
    }),

  listInvestigations: () => request<{ items: InvestigationSummary[]; total: number }>("/api/v1/investigations"),
  getInvestigation: (id: string) => request<Investigation>(`/api/v1/investigations/${id}`),
  evidence: (id: string, params: Record<string, string | number | boolean | string[] | undefined>) => {
    const search = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (Array.isArray(value)) value.forEach((item) => search.append(key, item));
      else if (value !== undefined && value !== "") search.set(key, String(value));
    });
    return request<{ items: Evidence[]; total: number; limit: number; offset: number }>(`/api/v1/investigations/${id}/evidence?${search}`);
  },
  timeline: (id: string) => request<{ items: unknown[]; total: number }>(`/api/v1/investigations/${id}/timeline`),
  events: (id: string) => request<{ items: unknown[]; total: number }>(`/api/v1/investigations/${id}/events`),
  providers: () => request<{ items: ProviderHealth[] }>("/api/v1/providers/health"),
  evals: () => request<EvalPayload>("/api/v1/evals"),
  watchlist: () => request<{ items: WatchItem[] }>("/api/v1/watchlists"),
  addWatchItem: (payload: { symbol: string; lookback_days: number }) =>
    request<{ symbol: string; enabled: boolean; watchlist: string }>("/api/v1/watchlists", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  runWatchOnce: () =>
    request<Record<string, unknown>>("/api/v1/watch/run-once", {
      method: "POST",
      body: JSON.stringify({ watchlist: "default" })
    }),
  cases: () => request<{ items: CaseSummary[]; total: number }>("/api/v1/cases"),
  caseDetail: (id: string) => request<{ case: CaseSummary; snapshots: Array<Record<string, unknown>> }>(`/api/v1/cases/${id}`),
  settings: () => request<SettingsPayload>("/api/v1/settings"),
  // ── market ──
  marketSearch: (q: string) => request<{ items: MarketSearchItem[] }>(`/api/v1/market/search?q=${encodeURIComponent(q)}`),
  marketOverview: () => request<{ cn_indices: MarketQuote[]; us_indices: MarketQuote[] }>("/api/v1/market/overview"),
  marketQuote: (secid: string) => request<MarketQuote>(`/api/v1/market/quote/${secid}`),
  marketKlines: (secid: string, period: string, limit = 200) =>
    request<{ bars: KlineBar[]; source: string; error: string | null }>(`/api/v1/market/klines/${secid}?period=${period}&limit=${limit}`),
  marketMovers: (market: string, rank: string) =>
    request<{ items: MarketQuote[]; coverage: string }>(`/api/v1/market/movers?market=${market}&rank=${rank}&limit=10`),
  marketAnomalies: () => request<{ items: MarketAnomaly[]; note: string }>("/api/v1/market/anomalies"),
  marketWatchlist: () => request<{ items: WatchlistItem[] }>("/api/v1/market/watchlist"),
  watchlistAdd: (secid: string) => request<WatchlistItem>("/api/v1/market/watchlist", { method: "POST", body: JSON.stringify({ secid }) }),
  watchlistRemove: (secid: string) => request<{ ok: boolean }>(`/api/v1/market/watchlist/${secid}`, { method: "DELETE" }),
  watchlistToggle: (secid: string, enabled: boolean) =>
    request<{ ok: boolean }>(`/api/v1/market/watchlist/${secid}`, { method: "PATCH", body: JSON.stringify({ enabled }) }),
  watchlistReorder: (secids: string[]) =>
    request<{ ok: boolean }>("/api/v1/market/watchlist/reorder", { method: "POST", body: JSON.stringify({ secids }) }),
  // ── autonomous trading ──
  tradingSessions: () => request<{ items: TradingSessionInfo[] }>("/api/v1/trading/sessions"),
  tradingSessionCreate: (payload: Record<string, unknown>) =>
    request<TradingSessionInfo>("/api/v1/trading/sessions", { method: "POST", body: JSON.stringify(payload) }),
  tradingSession: (id: string) => request<TradingSessionInfo & { portfolio?: any; theses?: any[] }>(`/api/v1/trading/sessions/${id}`),
  tradingSessionAction: (id: string, action: "start" | "pause" | "resume" | "stop" | "emergency-stop" | "run-cycle") =>
    request<any>(`/api/v1/trading/sessions/${id}/${action}`, { method: "POST", body: "{}" }),
  tradingEvents: (id: string, limit = 100, type?: string) =>
    request<{ items: DecisionEventInfo[] }>(`/api/v1/trading/sessions/${id}/events?limit=${limit}${type ? `&event_type=${type}` : ""}`),
  tradingEquityCurve: (id: string) => request<{ items: EquityPoint[] }>(`/api/v1/trading/sessions/${id}/equity-curve`),
  tradingTradeDetail: (id: string, proposalId: string) =>
    request<{ proposal_id: string; chain: DecisionEventInfo[] }>(`/api/v1/trading/sessions/${id}/trades/${proposalId}`),
  tradingDecisions: (id: string) => request<{ items: DecisionStory[] }>(`/api/v1/trading/sessions/${id}/decisions`),
  tradingSystemTruth: (id: string) => request<SystemTruth>(`/api/v1/trading/sessions/${id}/system-truth`),
  tradingProposals: (id: string, limit = 50) => request<{ items: ProposalRow[] }>(`/api/v1/trading/sessions/${id}/proposals?limit=${limit}`),
  tradingTheses: (id: string, symbol?: string) =>
    request<{ items: ThesisInfo[] }>(`/api/v1/trading/sessions/${id}/theses${symbol ? `?symbol=${symbol}` : ""}`),
  tradingOperationalState: (id: string) => request<{ session_id: string; operational_state: any }>(`/api/v1/trading/sessions/${id}/operational-state`),
  tradingGlobalSnapshot: () => request<{ utc_time: string; markets: Record<string, any> }>("/api/v1/trading/global-market-snapshot"),
  tradingDailyReports: (id?: string) => request<{ items: any[] }>(id ? `/api/v1/trading/sessions/${id}/daily-reports` : "/api/v1/trading/daily-reports"),
  tradingOvernightEvents: (id: string) => request<{ items: any[] }>(`/api/v1/trading/sessions/${id}/overnight-events`),
  tradingDebugProposal: (payload: Record<string, unknown>) =>
    request<any>("/api/v1/trading/debug/proposals", { method: "POST", body: JSON.stringify(payload) }),
  saveCredential: (key: string, value: string) =>
    request<{ ok: boolean; key: string; configured: boolean }>("/api/v1/settings/credentials", {
      method: "PUT",
      body: JSON.stringify({ key, value })
    }),
};
