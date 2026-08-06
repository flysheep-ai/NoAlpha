import React from "react";
import { createRoot } from "react-dom/client";
import { Activity, Archive, BarChart3, BellRing, ClipboardList, Cpu, DollarSign, FileSearch, Gauge, Globe, HeartPulse, Key, MessageCircle, Search, Settings, ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ChatPage } from "./Chat";
import { MarketPage } from "./Market";
import { TradingPage } from "./Trading";
import { QuantPage } from "./Quant";
import { api } from "./api";
import type { Claim, Evidence, Investigation, InvestigationSummary, LLMSettings, ProviderHealth } from "./types";
import "./i18n";
import "./styles.css";

type Route =
  | { name: "dashboard" }
  | { name: "investigate" }
  | { name: "investigations" }
  | { name: "investigation"; id: string; tab?: string }
  | { name: "providers" }
  | { name: "watchlist" }
  | { name: "cases" }
  | { name: "case"; id: string }
  | { name: "evals" }
  | { name: "settings" }
  | { name: "chat" }
  | { name: "market" }
  | { name: "trading" }
  | { name: "quant" };

function parseRoute(): Route {
  const path = window.location.pathname;
  const parts = path.split("/").filter(Boolean);
  if (parts[0] === "investigate") return { name: "investigate" };
  if (parts[0] === "investigations" && parts[1]) return { name: "investigation", id: parts[1], tab: parts[2] };
  if (parts[0] === "investigations") return { name: "investigations" };
  if (parts[0] === "providers") return { name: "providers" };
  if (parts[0] === "watchlist") return { name: "watchlist" };
  if (parts[0] === "cases" && parts[1]) return { name: "case", id: parts[1] };
  if (parts[0] === "cases") return { name: "cases" };
  if (parts[0] === "evals") return { name: "evals" };
  if (parts[0] === "settings") return { name: "settings" };
  if (parts[0] === "chat") return { name: "chat" };
  if (parts[0] === "market") return { name: "market" };
  if (parts[0] === "trading") return { name: "trading" };
  if (parts[0] === "quant") return { name: "quant" };
  return { name: "dashboard" };
}

function navigate(path: string) {
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function App() {
  const [route, setRoute] = React.useState<Route>(parseRoute());
  React.useEffect(() => {
    const onPop = () => setRoute(parseRoute());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const { t, i18n } = useTranslation();

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">NA</div>
          <div>
            <strong>{t("app.brand")}</strong>
            <span>{t("app.subtitle")}</span>
          </div>
        </div>
        <nav>
          <NavItem icon={<Gauge />} label={t("nav.dashboard")} path="/" active={route.name === "dashboard"} />
          <NavItem icon={<Search />} label={t("nav.investigate")} path="/investigate" active={route.name === "investigate"} />
          <NavItem icon={<FileSearch />} label={t("nav.investigations")} path="/investigations" active={route.name === "investigations" || route.name === "investigation"} />
          <NavItem icon={<BellRing />} label={t("nav.watchlist")} path="/watchlist" active={route.name === "watchlist"} />
          <NavItem icon={<ClipboardList />} label={t("nav.cases")} path="/cases" active={route.name === "cases" || route.name === "case"} />
          <NavItem icon={<HeartPulse />} label={t("nav.providers")} path="/providers" active={route.name === "providers"} />
          <NavItem icon={<BarChart3 />} label={t("nav.evaluations")} path="/evals" active={route.name === "evals"} />
          <NavItem icon={<Activity />} label={t("nav.market")} path="/market" active={route.name === "market"} />
          <NavItem icon={<DollarSign />} label={t("nav.trading")} path="/trading" active={route.name === "trading"} />
          <NavItem icon={<Cpu />} label={t("nav.quant")} path="/quant" active={route.name === "quant"} />
          <NavItem icon={<MessageCircle />} label={t("nav.chat")} path="/chat" active={route.name === "chat"} />
          <NavItem icon={<Settings />} label={t("nav.settings")} path="/settings" active={route.name === "settings"} />
        </nav>
        <div className="lang-switcher">
          <LangSwitch />
        </div>
      </aside>
      <main className="main">
        <TopBar routeName={route.name} />
        {route.name === "dashboard" && <Dashboard />}
        {route.name === "investigate" && <Investigate />}
        {route.name === "investigations" && <Investigations />}
        {route.name === "investigation" && <InvestigationDetail id={route.id} tab={route.tab ?? "report"} />}
        {route.name === "providers" && <Providers />}
        {route.name === "watchlist" && <Watchlist />}
        {route.name === "cases" && <Cases />}
        {route.name === "case" && <CaseDetail id={route.id} />}
        {route.name === "evals" && <Evals />}
        {route.name === "settings" && <SettingsPage />}
        {route.name === "chat" && <ChatPage lang={i18n.language} />}
        {route.name === "market" && <MarketPage lang={i18n.language} />}
        {route.name === "trading" && <TradingPage lang={i18n.language} />}
        {route.name === "quant" && <QuantPage lang={i18n.language} />}
      </main>
    </div>
  );
}

function LangSwitch() {
  const { i18n } = useTranslation();
  const toggle = () => i18n.changeLanguage(i18n.language.startsWith("zh") ? "en-US" : "zh-CN");
  return (
    <button className="nav-item lang-switch-btn" onClick={toggle} title={i18n.language === "zh" ? "Switch to English" : "切换到中文"}>
      <Globe size={17} />
      <span>{i18n.language.startsWith("zh") ? "English" : "中文"}</span>
    </button>
  );
}

function NavItem({ icon, label, path, active }: { icon: React.ReactNode; label: string; path: string; active: boolean }) {
  return (
    <button className={`nav-item ${active ? "active" : ""}`} onClick={() => navigate(path)}>
      {icon}
      <span>{label}</span>
    </button>
  );
}

function TopBar({ routeName }: { routeName: Route["name"] }) {
  const { t } = useTranslation();
  const routeKey = routeName === "investigation" ? "investigations" : routeName === "case" ? "cases" : routeName;
  return (
    <header className="topbar">
      <div>
        <span className="eyebrow">{t(`topbar.pages.${routeKey}.eyebrow`, t("topbar.eyebrow"))}</span>
        <h1>{t(`topbar.pages.${routeKey}.title`, t("topbar.title"))}</h1>
      </div>
      <div className="topbar-pill"><Activity size={16} /> {t(`topbar.pages.${routeKey}.pill`, t("topbar.pill"))}</div>
    </header>
  );
}

function Dashboard() {
  const { t } = useTranslation();
  const { data, error, loading, refresh } = useAsync(api.systemStatus, []);
  if (loading) return <Loading title={t("dashboard.loading")} />;
  if (error) return <ErrorState error={error} onRetry={refresh} />;
  if (!data) return null;
  const providerCounts = countBy(data.providers, (p) => p.status);
  return (
    <section className="page-grid">
      <Panel className="wide">
        <SectionTitle title={t("dashboard.systemStatus")} subtitle={t("dashboard.systemStatusSub")} />
        <div className="metric-row">
          <Metric label={t("dashboard.healthyProviders")} value={providerCounts.healthy ?? 0} tone="good" />
          <Metric label={t("dashboard.degraded")} value={providerCounts.degraded ?? 0} tone="warn" />
          <Metric label={t("dashboard.unauthorized")} value={providerCounts.unauthorized ?? 0} tone="bad" />
          <Metric label={t("dashboard.openCases")} value={data.cases.open} tone="accent" />
        </div>
      </Panel>
      <Panel>
        <SectionTitle title={t("dashboard.providerHealth")} subtitle={t("dashboard.providerHealthSub")} />
        <div className="stack">
          {data.providers.map((provider) => <ProviderRow key={provider.provider} provider={provider} />)}
        </div>
      </Panel>
      <Panel>
        <SectionTitle title={t("dashboard.watchlistSummary")} subtitle={t("dashboard.watchlistSummarySub")} />
        <div className="metric-row compact">
          <Metric label={t("dashboard.lists")} value={data.watchlist.count} />
          <Metric label={t("dashboard.symbols")} value={data.watchlist.items} />
          <Metric label={t("dashboard.enabled")} value={data.watchlist.enabled} />
        </div>
      </Panel>
      <Panel className="wide">
        <SectionTitle title={t("dashboard.recentInvestigations")} subtitle={t("dashboard.recentInvestigationsSub")} />
        <InvestigationTable items={data.recent_investigations} />
      </Panel>
      <Panel className="wide">
        <SectionTitle title={t("dashboard.recentCases")} subtitle={t("dashboard.recentCasesSub")} />
        <CaseTable items={data.recent_cases} />
      </Panel>
    </section>
  );
}

function Investigate() {
  const { t, i18n } = useTranslation();
  const [symbol, setSymbol] = React.useState("AAPL");
  const [lookback, setLookback] = React.useState(60);
  const [asOf, setAsOf] = React.useState("");
  const [researchMode, setResearchMode] = React.useState("llm");
  const [jobId, setJobId] = React.useState<string | null>(null);
  const [job, setJob] = React.useState<Investigation | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  async function start() {
    setError(null);
    setJob(null);
    const created = await api.createInvestigation({ symbol, lookback_days: lookback, as_of: asOf || null, research_mode: researchMode, report_language: i18n.language });
    setJobId(created.investigation_id);
  }

  React.useEffect(() => {
    if (!jobId) return;
    let stopped = false;
    const tick = async () => {
      try {
        const next = await api.getInvestigation(jobId);
        if (stopped) return;
        setJob(next);
        if (next.status === "completed") {
          navigate(`/investigations/${next.investigation_id}`);
          return;
        }
        if (next.status !== "failed") setTimeout(tick, 1200);
      } catch (err) {
        if (!stopped) setError(String(err));
      }
    };
    tick();
    return () => { stopped = true; };
  }, [jobId]);

  return (
    <section className="page-grid">
      <Panel className="wide">
        <SectionTitle title={t("investigate.title")} subtitle={t("investigate.subtitle")} />
        <div className="investigate-form">
          <label>{t("investigate.symbol")}<input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} /></label>
          <label>{t("investigate.lookbackDays")}<input type="number" value={lookback} onChange={(e) => setLookback(Number(e.target.value))} /></label>
          <label>{t("investigate.asOf")}<input placeholder={t("investigate.asOfPlaceholder")} value={asOf} onChange={(e) => setAsOf(e.target.value)} /></label>
          <label>{t("researchMode.label")}
            <select value={researchMode} onChange={(e) => setResearchMode(e.target.value)}>
              <option value="deterministic">{t("researchMode.deterministic")} — {t("researchMode.deterministicDesc")}</option>
              <option value="llm">{t("researchMode.llm")} — {t("researchMode.llmDesc")}</option>
              <option value="hybrid">{t("researchMode.hybrid")} — {t("researchMode.hybridDesc")}</option>
            </select>
          </label>
          <button className="primary" onClick={start}>{t("investigate.start")}</button>
        </div>
        {error && <div className="error-box">{error}</div>}
      </Panel>
      {!job && !jobId && (
        <Panel className="wide">
          <SectionTitle title={t("investigate.pipelineStatus")} subtitle={t("investigate.pipelineStatusSub")} />
          <EmptyState text={t("investigate.emptyState")} />
        </Panel>
      )}
      {job && jobId && <InvestigationWorkspace job={job} />}
    </section>
  );
}

const STAGE_DISPLAY_ORDER = [
  "resolving_instrument", "fetching_market_data", "fetching_sec_facts",
  "fetching_filings", "fetching_news", "normalizing_evidence", "canonicalizing",
  "applying_pit_filter", "building_timeline", "detecting_events",
  "building_research_context", "planning_research", "analyzing_market",
  "analyzing_fundamentals", "analyzing_filings", "analyzing_news",
  "cross_source_synthesis", "building_bull_case", "building_bear_case",
  "running_critic", "generating_claims", "validating_citations",
  "calculating_confidence", "final_synthesis",
];

function InvestigationWorkspace({ job }: { job: Investigation }) {
  const { t } = useTranslation();
  const progress = job.progress;
  const stages = progress?.stages ?? {};
  const stageIndex = progress?.stage_index ?? 0;
  const totalStages = progress?.total_stages ?? 28;
  const elapsed = job.created_at ? Math.round((Date.now() - new Date(job.created_at).getTime()) / 1000) : 0;

  return (
    <>
      {/* Header */}
      <Panel className="wide">
        <div className="workspace-header">
          <div>
            <span className={`badge provider ${job.status}`}>{job.status.toUpperCase()}</span>
            <span style={{ marginLeft: 12, color: "var(--muted)", fontSize: 14 }}>
              {stageIndex}/{totalStages} stages · {elapsed}s · {t(`researchMode.${job.research_mode || "deterministic"}`)}
            </span>
            {job.llm_model && (
              <span className="topbar-pill" style={{ marginLeft: 12 }}><Cpu size={14} /> {job.llm_model}</span>
            )}
          </div>
          {job.error && <div className="error-box">{job.error}</div>}
        </div>
      </Panel>

      {/* Stage Timeline + Metrics side by side */}
      <div className="workspace-grid">
        {/* Stage Timeline */}
        <Panel>
          <SectionTitle title="Investigation Stages" subtitle={progress?.current_stage ? `Current: ${t(`stages.${progress.current_stage}`, progress.current_stage)}` : ""} />
          <div className="stage-timeline">
            {STAGE_DISPLAY_ORDER.map((key) => {
              const s = stages[key];
              const status = s?.status ?? "pending";
              const icon = status === "completed" ? "✓" : status === "running" ? "◉" : status === "degraded" ? "⚠" : status === "failed" ? "✕" : "○";
              const cls = `stage-row ${status}`;
              return (
                <div key={key} className={cls}>
                  <span className="stage-icon">{icon}</span>
                  <span className="stage-name">{t(`stages.${key}`, key)}</span>
                  <span className="stage-msg">{s?.message || ""}</span>
                  {s?.duration_ms > 0 && <span className="stage-dur">{(s.duration_ms / 1000).toFixed(1)}s</span>}
                </div>
              );
            })}
          </div>
        </Panel>

        {/* Metrics Panel */}
        <div className="workspace-side">
          {/* Evidence Metrics */}
          <Panel>
            <SectionTitle title="Evidence" />
            <div className="metric-list">
              {stages.fetching_market_data?.metrics?.market_bars !== undefined && (
                <div className="metric-item">
                  <span>Market Bars</span>
                  <strong>{String(stages.fetching_market_data.metrics.market_bars)}</strong>
                </div>
              )}
              {stages.fetching_sec_facts?.metrics?.raw_financial_facts !== undefined && (
                <div className="metric-item">
                  <span>SEC Facts (raw)</span>
                  <strong>{String(stages.fetching_sec_facts.metrics.raw_financial_facts)}</strong>
                </div>
              )}
              {stages.canonicalizing?.metrics?.canonical_evidence !== undefined && (
                <div className="metric-item">
                  <span>Canonical Evidence</span>
                  <strong>{String(stages.canonicalizing.metrics.canonical_evidence)}</strong>
                </div>
              )}
              {stages.canonicalizing?.metrics?.duplicates_removed !== undefined && (
                <div className="metric-item">
                  <span>Duplicates Removed</span>
                  <strong>{String(stages.canonicalizing.metrics.duplicates_removed)}</strong>
                </div>
              )}
              {stages.canonicalizing?.metrics?.high_value_evidence !== undefined && (
                <div className="metric-item">
                  <span>High Value</span>
                  <strong>{String(stages.canonicalizing.metrics.high_value_evidence)}</strong>
                </div>
              )}
              {stages.detecting_events?.metrics?.detected_events !== undefined && (
                <div className="metric-item">
                  <span>Events Detected</span>
                  <strong>{String(stages.detecting_events.metrics.detected_events)}</strong>
                </div>
              )}
            </div>
          </Panel>

          {/* Provider Health */}
          {progress?.provider_status && Object.keys(progress.provider_status).length > 0 && (
            <Panel>
              <SectionTitle title="Data Sources" />
              <div className="metric-list">
                {Object.entries(progress.provider_status).map(([name, status]) => (
                  <div className="metric-item" key={name}>
                    <span>{name}</span>
                    <span className={`badge provider ${status}`}>{status}</span>
                  </div>
                ))}
              </div>
            </Panel>
          )}

          {/* Warnings */}
          {progress?.degraded && (
            <div className="unknowns">
              <ShieldAlert size={16} />
              <div>Some data sources are degraded. Investigation continues with available evidence.</div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function Investigations() {
  const { t } = useTranslation();
  const { data, error, loading, refresh } = useAsync(api.listInvestigations, []);
  if (loading) return <Loading title={t("investigations.loading")} />;
  if (error) return <ErrorState error={error} onRetry={refresh} />;
  return (
    <Panel className="wide">
      <SectionTitle title={t("investigations.title")} subtitle={t("investigations.subtitle")} />
      <InvestigationTable items={data?.items ?? []} />
    </Panel>
  );
}

function InvestigationDetail({ id, tab }: { id: string; tab: string }) {
  const { t } = useTranslation();
  const { data, error, loading, refresh } = useAsync(() => api.getInvestigation(id), [id]);
  const [selectedClaim, setSelectedClaim] = React.useState<Claim | null>(null);
  if (loading) return <Loading title={t("investigation.loading")} />;
  if (error) return <ErrorState error={error} onRetry={refresh} />;
  if (!data?.report) return <EmptyState text={t("investigation.notComplete")} />;
  const report = data.report;
  return (
    <section className="detail">
      <Panel>
        <div className="detail-header">
          <div>
            <span className="eyebrow">{t("investigation.eyebrow")} {report.investigation_id}</span>
            <h2>{report.subject.symbol} · {report.subject.company_name}</h2>
            <p>{t("investigation.generated")} {formatDate(report.generated_at)} · {t("investigation.asOf")} {formatDate(report.as_of)}</p>
          </div>
          <div className="detail-meta">
            <ConfidenceBadge value={report.confidence} />
            {data.research_mode && (
              <span className="topbar-pill"><Cpu size={16} />{t(`researchMode.${data.research_mode}`)} · {data.llm_model || t("researchMode.deterministic")}</span>
            )}
          </div>
        </div>
        <UnknownsPanel unknowns={report.unknowns} />
        <SectionTitle title={t("investigation.confidenceBreakdown")} subtitle={t("investigation.confidenceBreakdownSub")} />
        <ConfidenceBreakdown breakdown={report.confidence_breakdown} />
      </Panel>
      <div className="tabs">
        {(["report", "evidence", "timeline", "events", "quality"] as const).map((name) => (
          <button key={name} className={tab === name ? "active" : ""} onClick={() => navigate(`/investigations/${id}/${name}`)}>{t(`investigation.tabs.${name}`)}</button>
        ))}
      </div>
      {tab === "report" && <ReportTab report={report} onClaim={setSelectedClaim} />}
      {tab === "evidence" && <EvidenceExplorer id={id} />}
      {tab === "timeline" && <TimelineTab id={id} />}
      {tab === "events" && <EventsTab id={id} />}
      {tab === "quality" && <QualityTab report={report} quality={data.quality_report ?? report.quality_report} />}
      {selectedClaim && <ClaimDrawer investigationId={id} claim={selectedClaim} onClose={() => setSelectedClaim(null)} />}
    </section>
  );
}

function ReportTab({ report, onClaim }: { report: NonNullable<Investigation["report"]>; onClaim: (claim: Claim) => void }) {
  const { t } = useTranslation();
  const groups = groupBy(report.claims, (claim) => claim.claim_type);
  return (
    <section className="page-grid">
      <Panel className="wide">
        <SectionTitle title={t("investigation.executiveSummary")} subtitle={t("investigation.executiveSummarySub")} />
        <p className="summary-text">{report.summary}</p>
      </Panel>
      <Panel>
        <SectionTitle title={t("investigation.bullCase")} />
        <TextList items={report.bull_case} />
      </Panel>
      <Panel>
        <SectionTitle title={t("investigation.bearCase")} />
        <TextList items={report.bear_case} />
      </Panel>
      <Panel className="wide">
        <SectionTitle title={t("investigation.claims")} subtitle={t("investigation.claimsSub")} />
        {(["observation", "inference", "hypothesis"] as const).map((type) => (
          <div className="claim-group" key={type}>
            <h3>{t(`investigation.claimTypes.${type}`)}</h3>
            {(groups[type] ?? []).map((claim) => <ClaimCard key={claim.claim_id} claim={claim} onClick={() => onClaim(claim)} />)}
          </div>
        ))}
      </Panel>
    </section>
  );
}

function EvidenceExplorer({ id }: { id: string }) {
  const { t } = useTranslation();
  const [type, setType] = React.useState("");
  const [source, setSource] = React.useState("");
  const [minImportance, setMinImportance] = React.useState("");
  const [offset, setOffset] = React.useState(0);
  const { data, error, loading, refresh } = useAsync(() => api.evidence(id, { evidence_type: type, source, min_importance: minImportance, limit: 50, offset }), [id, type, source, minImportance, offset]);
  return (
    <Panel className="wide">
      <SectionTitle title={t("evidence.title")} subtitle={t("evidence.subtitle")} />
      <div className="filters">
        <select value={type} onChange={(e) => { setType(e.target.value); setOffset(0); }}>
          <option value="">{t("evidence.allTypes")}</option>
          <option value="market_bar">market_bar</option>
          <option value="financial_fact">financial_fact</option>
          <option value="filing">filing</option>
          <option value="news">news</option>
        </select>
        <input placeholder={t("evidence.sourceFilter")} value={source} onChange={(e) => setSource(e.target.value)} />
        <input placeholder={t("evidence.minImportance")} value={minImportance} onChange={(e) => setMinImportance(e.target.value)} />
      </div>
      {loading && <Loading title={t("evidence.loading")} />}
      {error && <ErrorState error={error} onRetry={refresh} />}
      {data && (
        <>
          <div className="table-note">{t("evidence.showing")} {data.items.length} {t("evidence.of")} {data.total}</div>
          <div className="evidence-list">{data.items.map((item) => <EvidenceCard key={item.evidence_id} item={item} />)}</div>
          <div className="pager">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>{t("evidence.previous")}</button>
            <button disabled={offset + 50 >= data.total} onClick={() => setOffset(offset + 50)}>{t("evidence.next")}</button>
          </div>
        </>
      )}
    </Panel>
  );
}

function TimelineTab({ id }: { id: string }) {
  const { t } = useTranslation();
  const { data, loading, error, refresh } = useAsync(() => api.timeline(id), [id]);
  if (loading) return <Loading title={t("timeline.loading")} />;
  if (error) return <ErrorState error={error} onRetry={refresh} />;
  return <Panel className="wide"><SectionTitle title={t("timeline.title")} /><div className="timeline">{(data?.items as any[] ?? []).map((item) => <TimelineNode key={item.evidence_id} item={item} />)}</div></Panel>;
}

function EventsTab({ id }: { id: string }) {
  const { t } = useTranslation();
  const { data, loading, error, refresh } = useAsync(() => api.events(id), [id]);
  if (loading) return <Loading title={t("events.loading")} />;
  if (error) return <ErrorState error={error} onRetry={refresh} />;
  return <Panel className="wide"><SectionTitle title={t("events.title")} subtitle={t("events.subtitle")} /><div className="event-grid">{(data?.items as any[] ?? []).map((item) => <EventCard key={item.event_id} event={item} />)}</div></Panel>;
}

function QualityTab({ report, quality }: { report: ResearchReportLike; quality: Record<string, unknown> }) {
  const { t } = useTranslation();
  const citation = (quality.citation_support ?? {}) as Record<string, unknown>;
  return (
    <section className="page-grid">
      <Panel className="wide">
        <SectionTitle title={t("quality.title")} subtitle={t("quality.subtitle")} />
        <div className="metric-row">
          <Metric label={t("quality.rawEvidence")} value={num(quality.raw_evidence_count)} />
          <Metric label={t("quality.canonicalEvidence")} value={num(quality.canonical_evidence_count)} />
          <Metric label={t("quality.duplicatesRemoved")} value={num(quality.duplicate_count)} tone="warn" />
          <Metric label={t("quality.staleFactsRemoved")} value={num(quality.stale_financial_facts)} />
          <Metric label={t("quality.highValueEvidence")} value={num(quality.high_value_evidence)} tone="good" />
        </div>
        <div className="quality-bars">
          <Bar label={t("quality.citationSupport")} value={Number(citation.citation_support_rate ?? 0)} />
          <Bar label={t("quality.overallConfidence")} value={report.confidence} />
        </div>
      </Panel>
      <Panel>
        <SectionTitle title={t("quality.claimsAudit")} />
        <Metric label={t("quality.claims")} value={report.claims.length} />
        <Metric label={t("quality.supported")} value={num(citation.supported_claims)} tone="good" />
        <Metric label={t("quality.unsupported")} value={num(citation.unsupported_claims)} tone="bad" />
      </Panel>
    </section>
  );
}

type ResearchReportLike = NonNullable<Investigation["report"]>;

function Providers() {
  const { t } = useTranslation();
  const { data, error, loading, refresh } = useAsync(api.providers, []);
  if (loading) return <Loading title={t("providers.loading")} />;
  if (error) return <ErrorState error={error} onRetry={refresh} />;
  return <Panel className="wide"><SectionTitle title={t("nav.providers")} subtitle={t("providers.subtitle")} />{data?.items.map((provider) => <ProviderRow key={provider.provider} provider={provider} detailed />)}</Panel>;
}

function Watchlist() {
  const { t } = useTranslation();
  const [symbol, setSymbol] = React.useState("AAPL");
  const [message, setMessage] = React.useState("");
  const { data, error, loading, refresh } = useAsync(api.watchlist, [message]);
  async function add() {
    await api.addWatchItem({ symbol, lookback_days: 60 });
    setMessage(`${symbol.toUpperCase()} added`);
  }
  async function run() {
    const result = await api.runWatchOnce();
    setMessage(`Scan complete: ${result.cases_created ?? 0} created, ${result.cases_updated ?? 0} updated`);
  }
  return (
    <section className="page-grid">
      <Panel className="wide">
        <SectionTitle title={t("watchlist.title")} subtitle={t("watchlist.subtitle")} />
        <div className="investigate-form">
          <label>{t("watchlist.symbol")}<input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} /></label>
          <button className="primary" onClick={add}>{t("watchlist.addSymbol")}</button>
          <button onClick={run}>{t("watchlist.runScan")}</button>
        </div>
        {message && <div className="status-line">{message}</div>}
        {loading && <Loading title={t("watchlist.loading")} />}
        {error && <ErrorState error={error} onRetry={refresh} />}
        <div className="data-table">
          {(data?.items ?? []).map((item) => <div className="row" key={item.symbol}><strong>{item.symbol}</strong><span>{item.enabled ? t("watchlist.enabled") : t("watchlist.disabled")}</span><span>{t("watchlist.priority")} {item.priority}</span><span>{t("watchlist.cooldown")} {item.cooldown_until ?? t("watchlist.none")}</span></div>)}
        </div>
      </Panel>
    </section>
  );
}

function Cases() {
  const { t } = useTranslation();
  const { data, error, loading, refresh } = useAsync(api.cases, []);
  if (loading) return <Loading title={t("cases.loading")} />;
  if (error) return <ErrorState error={error} onRetry={refresh} />;
  return <Panel className="wide"><SectionTitle title={t("cases.title")} subtitle={t("cases.subtitle")} /><CaseTable items={data?.items ?? []} /></Panel>;
}

function CaseDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const { data, error, loading, refresh } = useAsync(() => api.caseDetail(id), [id]);
  if (loading) return <Loading title={t("cases.loading")} />;
  if (error) return <ErrorState error={error} onRetry={refresh} />;
  if (!data) return null;
  return (
    <section className="page-grid">
      <Panel className="wide">
        <SectionTitle title={`${data.case.symbol} ${t("cases.case")}`} subtitle={data.case.status} />
        <div className="metric-row">
          <Metric label={t("cases.severity")} value={data.case.severity.toFixed(2)} tone="warn" />
          <Metric label={t("cases.confidence")} value={data.case.confidence.toFixed(2)} />
          <Metric label={t("cases.snapshots")} value={data.snapshots.length} />
        </div>
        <h3>{t("cases.currentThesis")}</h3><p className="summary-text">{data.case.current_thesis}</p>
        <h3>{t("cases.counterThesis")}</h3><p>{data.case.counter_thesis}</p>
        <UnknownsPanel unknowns={data.case.unknowns ?? []} />
      </Panel>
      <Panel className="wide">
        <SectionTitle title={t("cases.thesisHistory")} />
        <div className="timeline">{data.snapshots.map((snapshot) => <TimelineNode key={String(snapshot.snapshot_id)} item={{ timestamp: String(snapshot.created_at), title: String(snapshot.summary), evidence_id: String(snapshot.snapshot_id), source: String(snapshot.thesis_change) }} />)}</div>
      </Panel>
    </section>
  );
}

function Evals() {
  const { t } = useTranslation();
  const { data, error, loading, refresh } = useAsync(api.evals, []);
  if (loading) return <Loading title={t("evals.loading")} />;
  if (error) return <ErrorState error={error} onRetry={refresh} />;
  const replay = data?.replay_results as any[] | undefined;
  const watchReplay = (data?.watch_replay_results as any)?.aggregate;
  const watchScenarios = (data?.watch_scenarios as any)?.summary;
  const statusCounts = countBy(replay ?? [], (row) => row.metrics?.status ?? "unknown");
  return (
    <section className="page-grid">
      <Panel className="wide">
        <SectionTitle title={t("evals.title")} subtitle={t("evals.subtitle")} />
        <div className="metric-row">
          <Metric label={t("evals.historicalCases")} value={replay?.length ?? 0} />
          <Metric label={t("evals.pass")} value={statusCounts.pass ?? 0} tone="good" />
          <Metric label={t("evals.review")} value={statusCounts.review ?? 0} tone="warn" />
          <Metric label={t("evals.futureEvidence")} value={(replay ?? []).reduce((sum, row) => sum + (row.metrics?.future_evidence_count ?? 0), 0)} tone="good" />
        </div>
      </Panel>
      <Panel>
        <SectionTitle title={t("evals.watchReplay")} />
        <JsonBlock value={watchReplay ?? {}} />
      </Panel>
      <Panel>
        <SectionTitle title={t("evals.syntheticScenarios")} />
        <JsonBlock value={watchScenarios ?? {}} />
      </Panel>
      <Panel className="wide">
        <SectionTitle title={t("evals.reviewCases")} />
        {(replay ?? []).filter((row) => row.metrics?.status === "review").map((row) => <EvidenceCard key={row.metrics.case_id} item={{ evidence_id: row.metrics.case_id, evidence_type: "event", source: "Eval", event_time: row.case.as_of, available_at: row.case.as_of, retrieved_at: row.case.as_of, content: `Missing events: ${(row.metrics.missing_expected_events ?? []).join(", ") || "none"}`, metadata: row.metrics }} />)}
      </Panel>
    </section>
  );
}

function LLMSettingsPanel({ llm, onSaved }: { llm: LLMSettings; onSaved: () => void }) {
  const { t } = useTranslation();
  const [baseUrl, setBaseUrl] = React.useState("");
  const [model, setModel] = React.useState("");
  const [apiKey, setApiKey] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [msg, setMsg] = React.useState<{ text: string; ok: boolean } | null>(null);

  async function saveAll() {
    setSaving(true);
    setMsg(null);
    try {
      if (apiKey.trim()) await api.saveCredential(llm.credential_keys.api_key, apiKey.trim());
      if (baseUrl.trim()) await api.saveCredential(llm.credential_keys.base_url, baseUrl.trim());
      if (model.trim()) await api.saveCredential(llm.credential_keys.model, model.trim());
      setMsg({ text: t("settings.llmSaved"), ok: true });
      setApiKey(""); setBaseUrl(""); setModel("");
      onSaved();
    } catch (e) {
      setMsg({ text: String(e), ok: false });
    } finally {
      setSaving(false);
    }
  }

  async function clearKey() {
    setSaving(true);
    setMsg(null);
    try {
      await api.saveCredential(llm.credential_keys.api_key, "");
      setMsg({ text: t("settings.llmCleared"), ok: true });
      onSaved();
    } catch (e) {
      setMsg({ text: String(e), ok: false });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Panel className="wide">
      <div className="settings-provider-header">
        <div>
          <h3>{t("settings.llmTitle")}</h3>
          <p className="settings-desc">{t("settings.llmDesc")}</p>
        </div>
        <span className={`badge provider ${llm.api_key_configured ? "healthy" : "unauthorized"}`}>
          {llm.api_key_configured ? t("settings.configured") : t("settings.notConfigured")}
        </span>
      </div>
      <div className="settings-credential">
        {llm.api_key_configured && (
          <div className="settings-credential-preview">
            <Key size={14} /> {llm.api_key_preview} · {llm.model} · {llm.base_url}
            {llm.api_key_source === "env" && <span> ({t("settings.llmFromEnv")})</span>}
          </div>
        )}
        <div className="investigate-form settings-form">
          <label>{t("settings.llmApiKey")}
            <input
              type="password"
              placeholder={llm.api_key_configured ? t("settings.updateTokenPlaceholder") : "sk-…"}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </label>
          <label>{t("settings.llmBaseUrl")}
            <input
              placeholder={llm.base_url}
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </label>
          <label>{t("settings.llmModel")}
            <input
              placeholder={llm.model}
              value={model}
              onChange={(e) => setModel(e.target.value)}
            />
          </label>
          <button className="primary" disabled={saving || (!apiKey.trim() && !baseUrl.trim() && !model.trim())} onClick={saveAll}>
            {saving ? t("settings.saving") : t("settings.save")}
          </button>
          {llm.api_key_source === "credentials" && (
            <button disabled={saving} onClick={clearKey}>{t("settings.llmClear")}</button>
          )}
        </div>
        {msg && <div className={msg.ok ? "unknowns good" : "error-box"}>{msg.text}</div>}
      </div>
    </Panel>
  );
}

function SettingsPage() {
  const { t } = useTranslation();
  const { data, error, loading, refresh } = useAsync(api.settings, []);
  const [token, setToken] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [msg, setMsg] = React.useState<{ text: string; ok: boolean } | null>(null);

  async function save(key: string) {
    if (!token.trim()) return;
    setSaving(true);
    setMsg(null);
    try {
      await api.saveCredential(key, token.trim());
      setMsg({ text: t("settings.saved"), ok: true });
      setToken("");
      refresh();
    } catch (e) {
      setMsg({ text: String(e), ok: false });
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <Loading title={t("settings.loading")} />;
  if (error) return <ErrorState error={error} onRetry={refresh} />;
  const providers = data?.providers ?? [];

  return (
    <section className="page-grid">
      <Panel className="wide">
        <SectionTitle title={t("settings.title")} subtitle={t("settings.subtitle")} />
      </Panel>
      {data?.llm && <LLMSettingsPanel llm={data.llm} onSaved={refresh} />}
      {providers.map((provider) => (
        <Panel key={provider.provider} className={provider.requires_credentials ? "wide" : ""}>
          <div className="settings-provider-header">
            <div>
              <h3>{provider.name}</h3>
              <p className="settings-desc">{provider.description}</p>
            </div>
            <span className={`badge provider ${provider.configured ? "healthy" : "unauthorized"}`}>
              {provider.configured ? t("settings.configured") : t("settings.notConfigured")}
            </span>
          </div>
          {provider.requires_credentials && (
            <div className="settings-credential">
              {provider.configured && provider.credential_preview && (
                <div className="settings-credential-preview">
                  <Key size={14} /> {provider.credential_preview}
                </div>
              )}
              <div className="investigate-form settings-form">
                <label>{t("settings.tokenLabel", { key: provider.credential_key ?? "" })}
                  <input
                    type="password"
                    placeholder={provider.configured ? t("settings.updateTokenPlaceholder") : t("settings.tokenPlaceholder")}
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                  />
                </label>
                <button className="primary" disabled={!token.trim() || saving} onClick={() => save(provider.credential_key!)}>
                  {saving ? t("settings.saving") : t("settings.save")}
                </button>
              </div>
            </div>
          )}
        </Panel>
      ))}
      {msg && <Panel className="wide"><div className={msg.ok ? "unknowns good" : "error-box"}>{msg.text}</div></Panel>}
    </section>
  );
}

function ClaimDrawer({ investigationId, claim, onClose }: { investigationId: string; claim: Claim; onClose: () => void }) {
  const { t } = useTranslation();
  const { data, loading, error } = useAsync(() => api.evidence(investigationId, { evidence_id: claim.evidence_ids, limit: 20 }), [investigationId, claim.claim_id]);
  return (
    <div className="drawer">
      <button className="drawer-close" onClick={onClose}>{t("investigation.close")}</button>
      <ClaimTypeBadge type={claim.claim_type} />
      <h2>{claim.text}</h2>
      <p>{t("investigation.confidence")} {claim.confidence.toFixed(2)} · {claim.inference_level}</p>
      <h3>{t("investigation.supportingEvidence")}</h3>
      {loading && <Loading title={t("evidence.loading")} />}
      {error && <div className="error-box">{error}</div>}
      <div className="evidence-list">{data?.items.map((item) => <EvidenceCard key={item.evidence_id} item={item} />)}</div>
    </div>
  );
}

function InvestigationTable({ items }: { items: InvestigationSummary[] }) {
  const { t } = useTranslation();
  return <div className="data-table">{items.map((item) => <button className="row clickable" key={item.investigation_id} onClick={() => navigate(`/investigations/${item.investigation_id}`)}><strong>{item.symbol}</strong><span>{item.status}</span><span>{item.confidence ?? t("investigation.n/a")}</span><span>{item.evidence_count} {t("investigation.evidenceCount")}</span><span>{item.unknowns_count} {t("investigation.unknowns")}</span></button>)}</div>;
}

function CaseTable({ items }: { items: any[] }) {
  return <div className="data-table">{items.map((item) => <button className="row clickable" key={item.case_id} onClick={() => navigate(`/cases/${item.case_id}`)}><strong>{item.symbol}</strong><span>{item.status}</span><span>{item.primary_trigger}</span><span>{Number(item.severity ?? 0).toFixed(2)}</span><span>{Number(item.confidence ?? 0).toFixed(2)}</span></button>)}</div>;
}

function ProviderRow({ provider, detailed }: { provider: ProviderHealth; detailed?: boolean }) {
  const { t } = useTranslation();
  return <div className="provider-row"><ProviderStatusBadge status={provider.status} /><strong>{provider.provider}</strong><span>{provider.coverage}</span>{provider.requires_credentials && <span className="credential">{t("common.credentialRequired")}</span>}{detailed && provider.reason && <p>{provider.reason}</p>}</div>;
}

function ConfidenceBreakdown({ breakdown }: { breakdown: Record<string, number> }) {
  return <div className="confidence-grid">{Object.entries(breakdown).map(([key, value]) => <Bar key={key} label={labelize(key)} value={Number(value)} />)}</div>;
}

function UnknownsPanel({ unknowns }: { unknowns: string[] }) {
  const { t } = useTranslation();
  if (!unknowns.length) return <div className="unknowns good"><ShieldAlert size={16} /> {t("investigation.noUnknowns")}</div>;
  return <div className="unknowns"><ShieldAlert size={16} /><div><strong>{t("investigation.unknowns")}</strong>{unknowns.map((item) => <p key={item}>{item}</p>)}</div></div>;
}

function ClaimCard({ claim, onClick }: { claim: Claim; onClick: () => void }) {
  const { t } = useTranslation();
  return <button className={`claim-card ${claim.claim_type}`} onClick={onClick}><ClaimTypeBadge type={claim.claim_type} /><p>{claim.text}</p><span>{claim.evidence_ids.length} {t("investigation.evidenceCount")} · {claim.confidence.toFixed(2)}</span></button>;
}

function EvidenceCard({ item }: { item: Evidence }) {
  const { t } = useTranslation();
  const metadata = item.metadata ?? {};
  return <details className="evidence-card"><summary><span className="evidence-type">{item.evidence_type}</span><strong>{item.evidence_id}</strong><span>{item.source}</span><span>{formatDate(item.event_time)}</span></summary><p>{item.content}</p><div className="metadata-grid">{Object.entries(metadata).slice(0, 24).map(([key, value]) => <div key={key}><span>{key}</span><code>{String(value)}</code></div>)}</div>{item.source_url && <a href={item.source_url} target="_blank">{t("evidence.openSource")}</a>}</details>;
}

function TimelineNode({ item }: { item: any }) {
  return <div className="timeline-node"><time>{formatDate(item.timestamp)}</time><strong>{item.title}</strong><span>{item.source}</span><code>{item.evidence_id}</code></div>;
}

function EventCard({ event }: { event: any }) {
  const { t } = useTranslation();
  return <div className="event-card"><span className="evidence-type">{event.event_type}</span><h3>{event.description}</h3><Bar label={t("events.severity")} value={Number(event.severity)} /><JsonBlock value={event.metadata} /><p>{event.evidence_refs?.length ?? 0} {t("events.supportingEvidence")}</p></div>;
}

function StepList({ steps, status, error }: { steps: Array<{ key: string; label: string; status: string }>; status: string; error?: string | null }) {
  const { t } = useTranslation();
  return <div className="steps"><div className={`status-line ${status}`}>{t("investigate.status")}: {status}</div>{error && <div className="error-box">{error}</div>}{steps.map((step) => <div className={`step ${step.status}`} key={step.key}><span />{step.label}<em>{step.status}</em></div>)}</div>;
}

function ProviderStatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  return <span className={`badge provider ${status}`}>{t(`providers.status.${status}`, status)}</span>;
}
function ClaimTypeBadge({ type }: { type: string }) {
  const { t } = useTranslation();
  return <span className={`badge claim ${type}`}>{t(`investigation.claimTypes.${type}`, type)}</span>;
}
function ConfidenceBadge({ value }: { value: number }) {
  const { t } = useTranslation();
  return <div className="confidence-badge"><span>{t("investigation.overall")}</span><strong>{value.toFixed(2)}</strong></div>;
}
function Metric({ label, value, tone }: { label: string; value: string | number; tone?: string }) { return <div className={`metric ${tone ?? ""}`}><span>{label}</span><strong>{value}</strong></div>; }
function Bar({ label, value }: { label: string; value: number }) { return <div className="bar"><div><span>{label}</span><strong>{value.toFixed(2)}</strong></div><div className="bar-track"><span style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} /></div></div>; }
function Panel({ children, className = "" }: { children: React.ReactNode; className?: string }) { return <section className={`panel ${className}`}>{children}</section>; }
function SectionTitle({ title, subtitle }: { title: string; subtitle?: string }) { return <div className="section-title"><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>; }
function Loading({ title }: { title: string }) { return <div className="loading"><Archive className="spin" />{title}</div>; }
function EmptyState({ text }: { text: string }) { return <div className="empty">{text}</div>; }
function ErrorState({ error, onRetry }: { error: string; onRetry: () => void }) {
  const { t } = useTranslation();
  return <div className="error-box"><strong>{t("common.requestFailed")}</strong><p>{error}</p><button onClick={onRetry}>{t("common.retry")}</button></div>;
}
function TextList({ items }: { items: string[] }) { return <ul className="text-list">{items.map((item) => <li key={item}>{item}</li>)}</ul>; }
function JsonBlock({ value }: { value: unknown }) { return <pre className="json-block">{JSON.stringify(value, null, 2)}</pre>; }

function useAsync<T>(fn: () => Promise<T>, deps: React.DependencyList) {
  const [data, setData] = React.useState<T | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [nonce, setNonce] = React.useState(0);
  React.useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fn().then((value) => { if (!cancelled) setData(value); }).catch((err) => { if (!cancelled) setError(String(err)); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [...deps, nonce]);
  return { data, error, loading, refresh: () => setNonce((v) => v + 1) };
}

function countBy<T>(items: T[], key: (item: T) => string) { return items.reduce<Record<string, number>>((acc, item) => { const k = key(item); acc[k] = (acc[k] ?? 0) + 1; return acc; }, {}); }
function groupBy<T>(items: T[], key: (item: T) => string) { return items.reduce<Record<string, T[]>>((acc, item) => { const k = key(item); (acc[k] ??= []).push(item); return acc; }, {}); }
function labelize(value: string) { return value.replace(/_/g, " ").replace(/\b\w/g, (m: string) => m.toUpperCase()); }
function formatDate(value?: string | null) { return value ? new Date(value).toLocaleString() : "n/a"; }
function num(value: unknown) { return Number(value ?? 0); }

createRoot(document.getElementById("root")!).render(<App />);
