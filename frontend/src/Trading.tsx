import React from "react";
import {
  AlertTriangle, Bot, ChevronRight, Pause, Play, Square, X,
  ShieldCheck, BarChart3, TrendingUp, TrendingDown, Target,
  Clock, Zap, FileSearch, Cpu, Globe, Sun, Moon, Eye,
} from "lucide-react";
import { api } from "./api";
import { QuantIntelligencePanel } from "./Quant";
import type {
  DecisionEventInfo, DecisionStory, EquityPoint, PositionInfo,
  ProposalRow, SystemTruth, ThesisInfo, TradingSessionInfo,
} from "./api";

function fmtMoney(v: number | undefined | null, ccy = ""): string {
  if (v === undefined || v === null || Number.isNaN(v)) return "—";
  return `${ccy}${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}
function pctColor(v: number) { return v >= 0 ? "var(--good)" : "var(--bad)"; }

// ── decision-mode + proposal-status vocabularies (single source of truth) ──

const DECISION_MODE: Record<string, { label: [string, string]; color: string; short: [string, string] }> = {
  deterministic_signal:   { label: ["确定性信号策略", "Deterministic signal strategy"], color: "var(--warn)", short: ["确定性信号", "Deterministic"] },
  quant_only:             { label: ["量化模型", "Quant model"], color: "var(--accent)", short: ["量化", "Quant"] },
  quant_deterministic:    { label: ["量化排名 + 确定性研究", "Quant ranking + deterministic research"], color: "var(--accent)", short: ["量化+规则", "Quant+rules"] },
  llm_research:           { label: ["LLM 深度研究", "LLM deep research"], color: "var(--good)", short: ["LLM", "LLM"] },
  quant_llm_hybrid:       { label: ["量化 + LLM 混合", "Quant + LLM hybrid"], color: "var(--good)", short: ["量化+LLM", "Quant+LLM"] },
  quant_only_fallback:    { label: ["量化（LLM 降级）", "Quant (LLM degraded)"], color: "var(--warn)", short: ["量化(降级)", "Quant (deg.)"] },
  deterministic_fallback: { label: ["确定性信号（降级）", "Deterministic signal (degraded)"], color: "var(--bad)", short: ["确定性(降级)", "Determ. (deg.)"] },
};
function modeInfo(mode: string | undefined, zh: boolean) {
  const m = DECISION_MODE[mode ?? ""];
  const i = zh ? 0 : 1;
  if (!m) return { label: mode ?? (zh ? "未知" : "Unknown"), color: "var(--muted)", short: mode ?? "?" };
  return { label: m.label[i], color: m.color, short: m.short[i] };
}

const PROPOSAL_STATUS: Record<string, [string, string, string]> = {
  pending: ["待风控", "Pending risk", "var(--warn)"], approved: ["已核准", "Approved", "var(--accent)"],
  adjusted: ["已调整", "Adjusted", "var(--accent)"], rejected: ["已拒绝", "Rejected", "var(--bad)"],
  executed: ["已成交", "Executed", "var(--good)"], expired: ["已过期", "Expired", "var(--muted)"],
};
function statusInfo(status: string, zh: boolean): [string, string] {
  const s = PROPOSAL_STATUS[status];
  return s ? [zh ? s[0] : s[1], s[2]] : [status, "var(--muted)"];
}

// System-truth status → color + user-facing degradation explanation
const TRUTH_STATUS: Record<string, string> = {
  HEALTHY: "var(--good)", ACTIVE: "var(--good)", DEGRADED: "var(--warn)",
  UNAVAILABLE: "var(--bad)", HALTED: "var(--bad)", NOT_USED: "var(--muted)",
};
function truthColor(s: string) { return TRUTH_STATUS[s] ?? "var(--muted)"; }

// ── System Truth Bar: the un-fakeable "which brains are real" strip ──

function SystemTruthBar({ truth, zh }: { truth: SystemTruth | null; zh: boolean }) {
  if (!truth) return null;
  const c = truth.components;
  const rows: [string, typeof c.quant][] = [
    [zh ? "行情数据" : "Market data", c.market_data], [zh ? "量化模型" : "Quant model", c.quant],
    [zh ? "LLM 研究" : "LLM research", c.llm], [zh ? "确定性研究" : "Deterministic research", c.research],
    [zh ? "风控" : "Risk control", c.risk], [zh ? "虚拟券商" : "Virtual broker", c.virtual_broker],
  ];
  const mi = modeInfo(truth.effective_mode, zh);
  // Degraded components get a plain-language explanation.
  const degraded = rows.filter(([, comp]) => comp.status === "UNAVAILABLE" || comp.status === "DEGRADED");
  return (
    <div className="truth-strip" data-testid="system-truth-bar" style={{ borderLeft: `3px solid ${mi.color}` }}>
      <div className="truth-strip-head">
        <div className="truth-title">
          <ShieldCheck size={16} style={{ color: mi.color }} />
          <strong>{zh ? "系统真相 · 当前真实决策链" : "System truth · live decision chain"}</strong>
        </div>
        <div className="truth-mode">
          <span style={{ color: "var(--muted)" }}>{zh ? "有效决策模式：" : "Effective mode: "}</span>
          <span data-testid="effective-mode" className="mode-chip" style={{ color: mi.color, borderColor: mi.color }}>
            {mi.label}
          </span>
        </div>
      </div>
      <div className="truth-grid">
        {rows.map(([label, comp]) => (
          <div key={label} className="truth-cell" title={comp.detail} style={{ borderColor: `${truthColor(comp.status)}33` }}>
            <span className="truth-dot" style={{ background: truthColor(comp.status) }} />
            <span style={{ fontWeight: 600 }}>{label}</span>
            <span className="truth-status" style={{ color: truthColor(comp.status) }}>{comp.status}</span>
          </div>
        ))}
      </div>
      {degraded.length > 0 && (
        <div className="truth-degraded">
          {degraded.map(([label, comp]) => (
            <div key={label} style={{ marginBottom: 4 }}>
              <span style={{ color: truthColor(comp.status), fontWeight: 600 }}>
                {zh ? `${label}未参与本轮决策` : `${label} did not take part in this cycle`}
              </span>
              {" — "}{comp.detail}
            </div>
          ))}
          <div style={{ marginTop: 4, color: "var(--faint)" }}>
            {zh ? <>系统已如实降级为 <b style={{ color: mi.color }}>{mi.label}</b>，不会伪装拥有未启用的能力。</>
                : <>The system has honestly downgraded to <b style={{ color: mi.color }}>{mi.label}</b>; it does not pretend to capabilities that are not enabled.</>}
          </div>
        </div>
      )}
    </div>
  );
}

// ── helpers ──

function PositionName({ pos }: { pos: PositionInfo }) {
  if (pos.name && pos.name !== pos.symbol) return <span title={`${pos.market} · ${pos.symbol}`}>{pos.name} <span style={{ fontSize: 10, color: "var(--faint)", fontFamily: "var(--font-mono)" }}>{pos.symbol}</span></span>;
  return <strong style={{ fontFamily: "var(--font-mono)" }}>{pos.symbol}</strong>;
}

function DecisionQualityBadge({ quality, size, zh }: { quality: string; size?: number; zh: boolean }) {
  const s = size ?? 10;
  const map: Record<string, [string, string, string]> = {
    signal_only: ["信号驱动", "Signal-driven", "var(--warn)"],
    evidence_grounded: ["证据支撑", "Evidence-grounded", "var(--good)"],
    hybrid: ["混合研究", "Hybrid research", "var(--accent)"],
  };
  const e = map[quality];
  const [label, color] = e ? [zh ? e[0] : e[1], e[2]] : [quality, "var(--muted)"];
  return <span style={{ fontSize: s, color, fontWeight: 600 }}>{label}</span>;
}

// ── session config ──

function SessionConfig({ onCreated, zh }: { onCreated: (s: TradingSessionInfo) => void; zh: boolean }) {
  const [capital, setCapital] = React.useState(100000);
  const [markets, setMarkets] = React.useState<string[]>(["CN", "US"]);
  const [risk, setRisk] = React.useState("balanced");
  const [maxPos, setMaxPos] = React.useState(10);
  const [maxLoss, setMaxLoss] = React.useState(5);
  const [interval, setIntervalSec] = React.useState(300);
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const toggleMarket = (m: string) =>
    setMarkets(prev => prev.includes(m) ? prev.filter(x => x !== m) : [...prev, m]);
  const create = async () => {
    setBusy(true); setError("");
    try {
      const s = await api.tradingSessionCreate({
        initial_capital: capital, mode: "simulation", allowed_markets: markets,
        universe: "watchlist", risk_profile: risk, scan_interval: interval,
        max_position_weight: maxPos / 100, max_daily_loss_pct: maxLoss / 100,
        allow_closed_market_fills: false,
        // Narratives are written by the background loop, so the language is
        // fixed on the session rather than read per request.
        lang: zh ? "zh-CN" : "en-US",
      });
      onCreated(s);
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };
  return (
    <div className="trading-launch" data-testid="session-config">
      <div className="launch-copy">
        <span className="eyebrow">AUTONOMOUS DESK</span>
        <h2><Bot size={20} /> {zh ? "配置 AI 自主模拟交易" : "Configure AI autonomous simulated trading"}</h2>
        <p>{zh ? "设定资金与约束，扫描、研究、提案、风控、下单全由 Agent 自主完成。"
              : "Set capital and constraints; the agent handles scanning, research, proposals, risk checks and order placement on its own."}</p>
      </div>
      <div className="launch-form">
        <label style={{ fontSize: 13 }}>{zh ? "初始资金" : "Initial capital"}
          <input data-testid="cfg-capital" type="number" value={capital} onChange={e => setCapital(Number(e.target.value))} min={1000} step={10000} />
        </label>
        <div style={{ fontSize: 13 }}>{zh ? "允许市场" : "Allowed markets"}
          <div style={{ display: "flex", gap: 10, marginTop: 6 }}>
            {[["CN", zh ? "A 股" : "A-shares"], ["US", zh ? "美股" : "US equities"]].map(([k, label]) => (
              <label key={k} style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 13 }}>
                <input type="checkbox" checked={markets.includes(k)} onChange={() => toggleMarket(k)} /> {label}
              </label>
            ))}
          </div>
        </div>
        <label style={{ fontSize: 13 }}>{zh ? "风险偏好" : "Risk profile"}
          <select value={risk} onChange={e => setRisk(e.target.value)}>
            <option value="conservative">{zh ? "保守（单股≤5%）" : "Conservative (≤5% per name)"}</option>
            <option value="balanced">{zh ? "平衡（单股≤10%）" : "Balanced (≤10% per name)"}</option>
            <option value="aggressive">{zh ? "激进（单股≤15%）" : "Aggressive (≤15% per name)"}</option>
          </select>
        </label>
        <label style={{ fontSize: 13 }}>{zh ? "最大单股仓位 %" : "Max weight per name %"}
          <input type="number" value={maxPos} onChange={e => setMaxPos(Number(e.target.value))} min={1} max={50} />
        </label>
        <label style={{ fontSize: 13 }}>{zh ? "最大日亏损 %" : "Max daily loss %"}
          <input type="number" value={maxLoss} onChange={e => setMaxLoss(Number(e.target.value))} min={1} max={50} />
        </label>
        <label style={{ fontSize: 13 }}>{zh ? "扫描周期（秒）" : "Scan interval (seconds)"}
          <input type="number" value={interval} onChange={e => setIntervalSec(Number(e.target.value))} min={10} step={30} />
        </label>
      </div>
      <div className="launch-actions">
        <span style={{ fontSize: 12, color: "var(--muted)" }}>
          {zh ? "交易范围：我的自选股（跨市场）· SIMULATION 模式" : "Universe: my watchlist (cross-market) · SIMULATION mode"}
        </span>
        <button className="primary" data-testid="create-session" onClick={create} disabled={busy || !markets.length}>
          <Play size={14} /> {zh ? "创建并启动" : "Create and start"}
        </button>
      </div>
      {error && <div className="error-box" style={{ marginTop: 10 }}>{error}</div>}
    </div>
  );
}

// ── equity curve ──

function EquityCurve({ points, initial, ccy, zh }: { points: EquityPoint[]; initial: number; ccy: string; zh: boolean }) {
  if (points.length < 2) return <div className="empty" style={{ fontSize: 12 }}>{zh ? "至少 2 个周期后显示资产曲线" : "Equity curve appears after 2 cycles"}</div>;
  const w = 720, h = 160;
  const vals = points.map(p => p.equity);
  const min = Math.min(...vals, initial), max = Math.max(...vals, initial);
  const range = max - min || 1;
  const x = (i: number) => (i / (points.length - 1)) * (w - 50) + 40;
  const y = (v: number) => h - 22 - ((v - min) / range) * (h - 34);
  const path = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ");
  const last = vals[vals.length - 1];
  // X axis labels (first 3 + last)
  const labels = [0, Math.floor(points.length / 3), Math.floor(points.length * 2 / 3), points.length - 1].filter((v, i, a) => a.indexOf(v) === i);
  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%" }}>
      <line x1={40} x2={w - 10} y1={y(initial)} y2={y(initial)} stroke="var(--muted)" strokeDasharray="4 4" strokeWidth={0.7} />
      <text x={42} y={y(initial) - 4} fontSize={9} fill="var(--muted)">{zh ? "初始" : "Initial"} {fmtMoney(initial, ccy)}</text>
      <path d={path} fill="none" stroke={last >= initial ? "var(--good)" : "var(--bad)"} strokeWidth={1.6} />
      {/* cycle labels */}
      {labels.map(i => (
        <text key={i} x={x(i)} y={h - 6} fontSize={8} fill="var(--faint)" textAnchor="middle">
          #{points[i].cycle}
        </text>
      ))}
      <text x={w - 10} y={y(last) - 6} fontSize={10} textAnchor="end" fill={last >= initial ? "var(--good)" : "var(--bad)"}>
        {fmtMoney(last, ccy)}
      </text>
    </svg>
  );
}

// ── risk check matrix ──

function RiskCheckMatrix({ riskResult, zh }: { riskResult?: Record<string, any>; zh: boolean }) {
  const checks = riskResult?.checks ?? [];
  if (!checks.length) {
    // Fallback: reconstruct from basic fields
    if (!riskResult) return <div style={{ fontSize: 12, color: "var(--muted)" }}>{zh ? "无风险检查记录" : "No risk-check record"}</div>;
    return (
      <div style={{ fontSize: 12 }}>
        {riskResult.approved ? (
          <span style={{ color: "var(--good)" }}>✓ {zh ? "风控通过" : "Risk approved"} — {riskResult.reason ?? "All checks passed"}</span>
        ) : (
          <span style={{ color: "var(--bad)" }}>✕ {zh ? "风控拒绝" : "Risk rejected"} — {riskResult.reason ?? ""}</span>
        )}
        {riskResult.adjusted && (
          <div style={{ marginTop: 6, fontSize: 13, color: "var(--warn)" }}>
            ⚡ {zh ? "仓位已调整：" : "Position adjusted: "}{(riskResult.original_target_weight * 100).toFixed(1)}% → {(riskResult.adjusted_target_weight * 100).toFixed(1)}%
          </div>
        )}
        {riskResult.violations?.length > 0 && (
          <ul style={{ marginTop: 4, fontSize: 11, color: "var(--bad)" }}>{riskResult.violations.map((v: string, i: number) => <li key={i}>{v}</li>)}</ul>
        )}
      </div>
    );
  }
  return (
    <div className="risk-matrix" style={{ display: "grid", gap: 6 }}>
      {checks.map((c: { name: string; passed: boolean; current: string; limit: string; detail?: string }, i: number) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: "18px 1fr", gap: 4, alignItems: "start", fontSize: 12 }}>
          <span style={{ color: c.passed ? "var(--good)" : "var(--bad)", fontWeight: 700, marginTop: 1 }}>
            {c.passed ? "✓" : "✕"}
          </span>
          <div>
            <span style={{ fontWeight: 600 }}>{c.name.replace(/_/g, " ")}</span>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--muted)", marginLeft: 8 }}>
              {c.current} · {zh ? "限制" : "limit"} {c.limit}
            </span>
            {c.detail && <div style={{ fontSize: 10, color: "var(--faint)" }}>{c.detail}</div>}
          </div>
        </div>
      ))}
      <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px solid var(--border)", fontSize: 13, fontWeight: 700, color: riskResult?.approved ? "var(--good)" : "var(--bad)" }}>
        {riskResult?.approved
          ? (zh ? "✓ 风控通过" : "✓ Risk approved")
          : `✕ ${zh ? "风控拒绝" : "Risk rejected"} — ${riskResult?.reason ?? ""}`}
        {riskResult?.adjusted && <span style={{ color: "var(--warn)", marginLeft: 8 }}>{zh ? "（仓位已调整）" : "(position adjusted)"}</span>}
      </div>
    </div>
  );
}

// ── decision story card ──

function DecisionCard({ dec, onOpen, zh }: { dec: DecisionStory; onOpen: () => void; zh: boolean }) {
  const p = dec.payload;
  const rp = dec.proposal_payload?.rationale ?? {};
  return (
    <div className="decision-card" style={{ borderLeftColor: p.action === "buy" ? "var(--good)" : "var(--bad)" }} onClick={onOpen}>
      <div className="decision-card-head">
        <div>
          <strong style={{ fontFamily: "var(--font-mono)", fontSize: 14 }}>{p.symbol || dec.symbol}</strong>
          <span className="micro-chip">
            {p.action?.toUpperCase()} · {zh ? "权重" : "weight"} {((p.final_weight ?? 0) * 100).toFixed(1)}%
          </span>
          {(() => { const dm = dec.proposal_payload?.decision_mode; return dm ? (
            <span style={{ fontSize: 9, color: modeInfo(dm, zh).color, fontWeight: 600, marginLeft: 6 }}>{modeInfo(dm, zh).short}</span>
          ) : null; })()}
        </div>
        <div className="decision-time">
          {new Date(dec.timestamp).toLocaleTimeString()}
          <br />
          {fmtMoney(p.amount, "¥")}
        </div>
      </div>
      <div className="decision-summary">
        {rp.decision_summary ? rp.decision_summary.slice(0, 200) : dec.message}
      </div>
      {rp.catalysts?.length > 0 && (
        <div className="decision-tags">
          {rp.catalysts.slice(0, 2).map((c: any, i: number) => (
            <span key={i} style={{ fontSize: 10, background: "var(--bg)", borderRadius: 4, padding: "2px 6px", color: "var(--accent)" }}>{c.title}</span>
          ))}
        </div>
      )}
    </div>
  );
}

// ── proposal detail drawer with tabs ──

function ProposalDetailDrawer({ sessionId, proposalId, onClose, zh }: { sessionId: string; proposalId: string; onClose: () => void; zh: boolean }) {
  const [chain, setChain] = React.useState<DecisionEventInfo[] | null>(null);
  const [theses, setTheses] = React.useState<ThesisInfo[] | null>(null);
  const [error, setError] = React.useState("");
  const [tab, setTab] = React.useState<"summary" | "agents" | "evidence" | "risk" | "sizing" | "execution">("summary");
  React.useEffect(() => {
    Promise.all([
      api.tradingTradeDetail(sessionId, proposalId).then(d => d.chain),
      api.tradingTheses(sessionId).then(d => d.items),
    ]).then(([c, t]) => { setChain(c); setTheses(t.filter(x => x.proposal_id === proposalId)); })
      .catch(e => setError(String(e)));
  }, [sessionId, proposalId]);

  const proposalEv = chain?.find(e => e.event_type === "proposal");
  const proposal = proposalEv?.payload?.proposal ?? {};
  const rationale = proposal.rationale ?? {};
  const agentView = rationale.agent_view ?? chain?.find(e => e.event_type === "agent")?.payload ?? {};
  const risk = proposal.risk_result ?? {};
  const sizingEv = chain?.find(e => e.event_type === "sizing");
  const sizing = sizingEv?.payload ?? {};
  const fillEv = chain?.find(e => e.event_type === "fill");
  const fill = fillEv?.payload?.fill ?? {};
  const decisionEv = chain?.find(e => e.event_type === "decision");

  const tabs: { key: typeof tab; label: string }[] = [
    { key: "summary", label: zh ? "决策摘要" : "Summary" },
    { key: "agents", label: zh ? "Agent链路" : "Agent chain" },
    { key: "evidence", label: zh ? `证据（${rationale.supporting_evidence_ids?.length ?? 0}）` : `Evidence (${rationale.supporting_evidence_ids?.length ?? 0})` },
    { key: "risk", label: zh ? "风险检查" : "Risk checks" },
    { key: "sizing", label: zh ? "仓位计算" : "Sizing" },
    { key: "execution", label: zh ? "执行记录" : "Execution" },
  ];

  return (
    <div className="drawer" data-testid="trade-detail" style={{ maxWidth: 680 }}>
      <button className="drawer-close" onClick={onClose}><X size={14} /> {zh ? "关闭" : "Close"}</button>
      <h2>{proposal.symbol ?? ""} {proposal.action ?? ""}
        {proposal.decision_mode && <span style={{ fontSize: 12, color: modeInfo(proposal.decision_mode, zh).color, fontWeight: 700, marginLeft: 8, border: `1px solid ${modeInfo(proposal.decision_mode, zh).color}`, borderRadius: 5, padding: "1px 8px" }}>{modeInfo(proposal.decision_mode, zh).label}</span>}
      </h2>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--faint)", marginBottom: 8, wordBreak: "break-all" }}>{proposalId}</div>

      {error && <div className="error-box">{error}</div>}
      {!chain && !error && <div className="empty">{zh ? "加载中…" : "Loading…"}</div>}

      {chain && (
        <>
          {/* tabs */}
          <div style={{ display: "flex", gap: 2, marginBottom: 14, borderBottom: "1px solid var(--border)" }}>
            {tabs.map(({ key, label }) => (
              <button key={key} onClick={() => setTab(key)}
                style={{ fontSize: 12, padding: "6px 12px", border: "none", background: tab === key ? "var(--bg)" : "none", color: tab === key ? "var(--accent)" : "var(--muted)", borderBottom: tab === key ? "2px solid var(--accent)" : "2px solid transparent", fontWeight: tab === key ? 700 : 400 }}>
                {label}
              </button>
            ))}
          </div>

          {tab === "summary" && (
            <div style={{ display: "grid", gap: 12, fontSize: 13, lineHeight: 1.7 }}>
              {agentView?.framework && (
                <div data-testid="proposal-agent-view" style={{ background: "var(--bg)", borderRadius: 6, padding: "8px 10px", borderLeft: "3px solid var(--accent)" }}>
                  <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>{zh ? "TradingAgents 角色链路" : "TradingAgents role chain"}</h4>
                  <div style={{ fontSize: 12, color: "var(--muted)", display: "flex", gap: 12, flexWrap: "wrap" }}>
                    <span>{zh ? "框架" : "Framework"} <b>{agentView.framework}</b></span>
                    <span>{zh ? "共识" : "Consensus"} <b>{agentView.debate?.consensus ?? "—"}</b></span>
                    <span>{zh ? "冲突" : "Conflict"} <b>{(agentView.debate?.conflict_level ?? 0).toFixed(2)}</b></span>
                    <span>{zh ? "交易员" : "Trader"} <b>{agentView.trader_plan?.action_hint ?? "—"}</b></span>
                    <span>{zh ? "基金经理" : "Fund manager"} <b>{agentView.fund_manager?.verdict ?? "—"}</b></span>
                  </div>
                </div>
              )}
              {rationale.quant_view && Object.keys(rationale.quant_view).length > 0 && (
                <div data-testid="proposal-quant-view" style={{ background: "var(--bg)", borderRadius: 6, padding: "8px 10px", borderLeft: "3px solid var(--accent)" }}>
                  <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>{zh ? "量化视角（模型预测，非事实）" : "Quant view (model prediction, not fact)"}</h4>
                  <div style={{ fontSize: 12, color: "var(--muted)", display: "flex", gap: 14, flexWrap: "wrap" }}>
                    <span>{zh ? "横截面排名" : "Cross-sectional rank"} <b>{rationale.quant_view.rank}/{rationale.quant_view.universe_size}</b></span>
                    <span>{zh ? `预计${rationale.quant_view.horizon_days ?? 5}日超额收益` : `Predicted ${rationale.quant_view.horizon_days ?? 5}d excess return`} <b>{((rationale.quant_view.predicted_return ?? 0) * 100).toFixed(2)}%</b></span>
                    {rationale.quant_view.probability_up != null && <span>{zh ? "上涨概率(校准)" : "P(up), calibrated"} <b>{(rationale.quant_view.probability_up * 100).toFixed(1)}%</b></span>}
                    <span>{zh ? "模型" : "Model"} <b>{rationale.quant_view.model || rationale.quant_view.model_id}</b></span>
                  </div>
                  {rationale.quant_view.ensemble?.components && (
                    <div style={{ fontSize: 11, color: "var(--faint)", marginTop: 4 }}>
                      {zh ? "融合：" : "Ensemble: "}quant {rationale.quant_view.ensemble.components.quant?.score} × research {rationale.quant_view.ensemble.components.research?.score}
                      {" → "}{rationale.quant_view.ensemble.action_hint}
                      {zh ? `（综合 ${rationale.quant_view.ensemble.combined_score}）` : ` (combined ${rationale.quant_view.ensemble.combined_score})`}
                    </div>
                  )}
                </div>
              )}
              {rationale.llm_view && Object.keys(rationale.llm_view).length > 0 && (
                <div data-testid="proposal-llm-view" style={{ background: "var(--bg)", borderRadius: 6, padding: "8px 10px", borderLeft: "3px solid var(--good)" }}>
                  <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>{zh ? "真LLM研究视角（证据驱动，非预测）" : "Real LLM research view (evidence-driven, not prediction)"}</h4>
                  <div style={{ fontSize: 12, color: "var(--muted)", display: "flex", gap: 14, flexWrap: "wrap" }}>
                    <span>{zh ? "方向" : "Direction"} <b>{rationale.llm_view.thesis_direction}</b></span>
                    <span>{zh ? "看多强度" : "Bull strength"} <b>{(rationale.llm_view.bull_strength ?? 0).toFixed(2)}</b></span>
                    <span>{zh ? "看空强度" : "Bear strength"} <b>{(rationale.llm_view.bear_strength ?? 0).toFixed(2)}</b></span>
                    <span>{zh ? "证据置信" : "Evidence confidence"} <b>{(rationale.llm_view.thesis_confidence ?? 0).toFixed(2)}</b></span>
                    <span>{zh ? "引用支持率" : "Citation support"} <b>{((rationale.llm_view.citation_support ?? 0) * 100).toFixed(0)}%</b></span>
                  </div>
                  <div style={{ fontSize: 11, color: "var(--faint)", marginTop: 4 }}>
                    {zh ? "模型" : "Model"} <b>{rationale.llm_view.model || "—"}</b>
                    {" · "}investigation <b>{rationale.llm_view.investigation_id?.slice(0, 8)}</b>
                    {" · "}context <b>{rationale.llm_view.context_id?.slice(0, 8)}</b>
                    {" · "}PIT-safe <b>{rationale.llm_view.pit_safe ? "✓" : "✗"}</b>
                  </div>
                  {rationale.llm_view.fusion && (
                    <div style={{ fontSize: 11, color: "var(--faint)", marginTop: 4 }}>
                      {zh ? "融合决策：" : "Fused decision: "}{rationale.llm_view.fusion.final_signal}
                      {" · "}{zh ? "冲突" : "conflict"} <b>{rationale.llm_view.fusion.conflict_state}</b>
                      {" · "}{zh ? "模式" : "mode"} <b>{rationale.llm_view.fusion.effective_mode}</b>
                      {" · "}{zh ? "强度" : "strength"} <b>{(rationale.llm_view.fusion.final_strength ?? 0).toFixed(2)}</b>
                    </div>
                  )}
                </div>
              )}
              <div>
                <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>{zh ? "为什么是这只股票？" : "Why this instrument?"}</h4>
                <p style={{ color: "var(--muted)", margin: 0, fontSize: 13 }}>{rationale.why_this_instrument || "—"}</p>
              </div>
              <div>
                <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>{zh ? "为什么现在？" : "Why now?"}</h4>
                <p style={{ color: "var(--muted)", margin: 0, fontSize: 13 }}>{(rationale.why_now ?? []).join(zh ? "；" : "; ") || rationale.signal_reason || "—"}</p>
              </div>
              {rationale.catalysts?.length > 0 && (
                <div>
                  <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>{zh ? `催化剂（${rationale.catalysts.length}）` : `Catalysts (${rationale.catalysts.length})`}</h4>
                  {rationale.catalysts.map((c: any, i: number) => (
                    <div key={i} style={{ background: "var(--bg)", borderRadius: 6, padding: "8px 10px", marginBottom: 6 }}>
                      <div style={{ fontWeight: 600 }}>{c.title}</div>
                      <div style={{ fontSize: 12, color: "var(--muted)" }}>{c.description}</div>
                      {c.evidence_ids?.length > 0 && <div style={{ fontSize: 10, color: "var(--faint)", fontFamily: "var(--font-mono)", marginTop: 2 }}>{zh ? "证据：" : "Evidence: "}{c.evidence_ids.join(", ")}</div>}
                      <div style={{ fontSize: 10, color: "var(--faint)", marginTop: 2 }}>
                        {zh ? "预期窗口：" : "Expected window: "}{c.expected_time_horizon ?? "—"} · {zh ? "置信：" : "confidence: "}{(c.confidence ?? 0).toFixed(2)}
                      </div>
                    </div>
                  ))}
                </div>
              )}
              {rationale.counter_arguments?.length > 0 && (
                <div>
                  <h4 style={{ margin: "0 0 4px", fontSize: 13, color: "var(--bad)" }}>{zh ? "反方证据" : "Counter-arguments"}</h4>
                  <ul style={{ paddingLeft: 20, margin: 0, fontSize: 12 }}>
                    {rationale.counter_arguments.map((c: string, i: number) => <li key={i}>{c}</li>)}
                  </ul>
                </div>
              )}
              {rationale.risks?.length > 0 && (
                <div>
                  <h4 style={{ margin: "0 0 4px", fontSize: 13, color: "var(--warn)" }}>{zh ? "关键风险" : "Key risks"}</h4>
                  <ul style={{ paddingLeft: 20, margin: 0, fontSize: 12 }}>
                    {rationale.risks.map((r: string, i: number) => <li key={i}>{r}</li>)}
                  </ul>
                </div>
              )}
              {rationale.invalidation_conditions?.length > 0 && (
                <div>
                  <h4 style={{ margin: "0 0 4px", fontSize: 13, color: "var(--bad)" }}>{zh ? "什么情况下会卖？" : "What would invalidate this?"}</h4>
                  <ul style={{ paddingLeft: 20, margin: 0, fontSize: 12 }}>
                    {rationale.invalidation_conditions.map((ic: string, i: number) => <li key={i}>{ic}</li>)}
                  </ul>
                </div>
              )}
              {rationale.confidence_reason && (
                <div>
                  <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>{zh ? "置信度" : "Confidence"} {(proposal.confidence ?? 0).toFixed(2)}</h4>
                  {rationale.confidence_reason.positive?.length > 0 && (
                    <div style={{ fontSize: 12 }}>
                      <span style={{ color: "var(--good)" }}>{zh ? "正面因素：" : "Positive: "}</span>
                      {rationale.confidence_reason.positive.join(zh ? "；" : "; ")}
                    </div>
                  )}
                  {rationale.confidence_reason.negative?.length > 0 && (
                    <div style={{ fontSize: 12 }}>
                      <span style={{ color: "var(--bad)" }}>{zh ? "负面因素：" : "Negative: "}</span>
                      {rationale.confidence_reason.negative.join(zh ? "；" : "; ")}
                    </div>
                  )}
                </div>
              )}
              {rationale.decision_summary && (
                <div style={{ background: "var(--bg)", borderRadius: 6, padding: 10, fontSize: 13 }}>
                  <strong>{zh ? "AI 决策总结：" : "AI decision summary: "}</strong>{rationale.decision_summary}
                </div>
              )}
            </div>
          )}

          {tab === "agents" && (
            <div style={{ display: "grid", gap: 12, fontSize: 13 }}>
              {!agentView?.framework && <div className="empty">{zh ? "暂无 Agent 链路记录" : "No agent-chain record"}</div>}
              {agentView?.framework && (
                <>
                  <div style={{ background: "var(--bg)", borderRadius: 6, padding: 10 }}>
                    <div style={{ fontSize: 11, color: "var(--faint)", marginBottom: 4 }}>FRAMEWORK</div>
                    <strong>{agentView.framework}</strong>
                    <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 4 }}>
                      {agentView.debate?.balance_note}
                    </div>
                  </div>
                  <div>
                    <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>{zh ? "分析师团队" : "Analyst team"}</h4>
                    <div style={{ display: "grid", gap: 6 }}>
                      {(agentView.analysts ?? []).map((a: any) => {
                        const color = a.stance === "bullish" ? "var(--good)" : a.stance === "bearish" ? "var(--bad)" : "var(--muted)";
                        return (
                          <div key={a.role} style={{ border: "1px solid var(--border)", borderRadius: 6, padding: "8px 10px" }}>
                            <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                              <strong>{a.role}</strong>
                              <span style={{ color, fontWeight: 700 }}>{a.stance} · {a.status}</span>
                            </div>
                            <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 3 }}>{a.summary}</div>
                            {a.risks?.length > 0 && <div style={{ fontSize: 11, color: "var(--warn)", marginTop: 3 }}>{a.risks.join(zh ? "；" : "; ")}</div>}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                  {agentView.debate && (
                    <div>
                      <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>{zh ? "多空研究辩论" : "Bull vs. bear debate"}</h4>
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                        {[agentView.debate.bull, agentView.debate.bear].map((side: any) => (
                          <div key={side.role} style={{ background: "var(--bg)", borderRadius: 6, padding: "8px 10px" }}>
                            <strong>{side.role}</strong>
                            <div style={{ fontSize: 12, color: "var(--muted)" }}>{side.summary}</div>
                            <div style={{ fontSize: 11, color: "var(--faint)" }}>confidence {(side.confidence ?? 0).toFixed(2)}</div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  {agentView.trader_plan && (
                    <div style={{ background: "var(--bg)", borderRadius: 6, padding: 10, borderLeft: "3px solid var(--accent)" }}>
                      <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>{zh ? "交易员计划" : "Trader plan"}</h4>
                      <div style={{ color: "var(--muted)", fontSize: 12 }}>{agentView.trader_plan.thesis}</div>
                      <div style={{ marginTop: 4, fontSize: 12 }}>
                        {zh ? "动作" : "Action"} <b>{agentView.trader_plan.action_hint}</b>
                        {" · "}{zh ? "目标" : "target"} {((agentView.trader_plan.target_weight_hint ?? 0) * 100).toFixed(2)}%
                        {" · "}{zh ? "置信" : "confidence"} {(agentView.trader_plan.confidence ?? 0).toFixed(2)}
                      </div>
                    </div>
                  )}
                  <div>
                    <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>{zh ? "风控守卫与基金经理" : "Risk guards and fund manager"}</h4>
                    {(agentView.risk_reviews ?? []).map((r: any) => (
                      <div key={r.name} style={{ fontSize: 12, borderBottom: "1px solid var(--border)", padding: "5px 0" }}>
                        <strong>{r.name}</strong>
                        {" · "}<span style={{ color: r.verdict === "reject" ? "var(--bad)" : r.verdict === "downsize" ? "var(--warn)" : "var(--good)" }}>{r.verdict}</span>
                        {" · "}{r.reason}
                      </div>
                    ))}
                    <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 6 }}>
                      {zh ? "基金经理：" : "Fund manager: "}<b>{agentView.fund_manager?.verdict}</b> · {agentView.fund_manager?.reason}
                    </div>
                  </div>
                </>
              )}
            </div>
          )}

          {tab === "evidence" && (
            <div style={{ display: "grid", gap: 8 }}>
              {(!rationale.supporting_evidence_ids || rationale.supporting_evidence_ids.length === 0) ? (
                <div className="empty" style={{ fontSize: 12 }}>
                  {zh ? "本提案为技术信号驱动，无可引用基本面证据。决策质量："
                      : "This proposal is technical-signal driven, with no citable fundamental evidence. Decision quality: "}
                  <DecisionQualityBadge quality={proposal.decision_quality ?? "signal_only"} zh={zh} />
                </div>
              ) : (
                rationale.supporting_evidence_ids.map((eid: string, i: number) => (
                  <div key={i} style={{ display: "flex", gap: 8, alignItems: "center", fontFamily: "var(--font-mono)", fontSize: 12, background: "var(--bg)", borderRadius: 4, padding: "6px 10px" }}>
                    <span style={{ color: "var(--accent)", fontWeight: 600 }}>{eid}</span>
                  </div>
                ))
              )}
              {/* Show raw evidence objects from research event */}
              {chain?.find(e => e.event_type === "research")?.payload?.evidence?.map((ev: any, i: number) => (
                <div key={i} style={{ fontSize: 12, borderBottom: "1px solid var(--border)", padding: "6px 0" }}>
                  <div style={{ fontFamily: "var(--font-mono)", color: "var(--accent)", fontWeight: 600, fontSize: 11 }}>[{ev.evidence_id}]</div>
                  <div>{ev.content}</div>
                  <div style={{ fontSize: 10, color: "var(--faint)" }}>as_of {ev.as_of?.slice(0, 16)} · {ev.bars_used} bars</div>
                </div>
              ))}
            </div>
          )}

          {tab === "risk" && <RiskCheckMatrix riskResult={risk} zh={zh} />}

          {tab === "sizing" && (
            <div style={{ display: "grid", gap: 10, fontSize: 13 }}>
              <div className="risk-matrix">
                {[{ label: zh ? "当前总资产" : "Current equity", value: fmtMoney(sizing.equity, "¥") },
                  { label: zh ? "目标权重" : "Target weight", value: `${((sizing.final_weight ?? proposal.target_weight ?? 0) * 100).toFixed(2)}%` },
                  { label: zh ? "当前价格" : "Current price", value: `${sizing.price ?? 0}` },
                  { label: zh ? "理论数量" : "Raw quantity", value: zh ? `${(sizing.raw_quantity ?? 0).toFixed(1)} 股` : `${(sizing.raw_quantity ?? 0).toFixed(1)} sh` },
                  { label: zh ? "实际数量" : "Actual quantity", value: zh ? `${sizing.actual_quantity ?? 0} 股（最小单位 ${sizing.lot_size ?? 1} 股）` : `${sizing.actual_quantity ?? 0} sh (lot size ${sizing.lot_size ?? 1})` },
                  { label: zh ? "预计金额" : "Expected amount", value: fmtMoney(sizing.expected_amount, "¥") },
                  { label: zh ? "预估手续费" : "Estimated commission", value: fmtMoney(sizing.commission_estimate, "¥") },
                ].map(({ label, value }, i) => (
                  <div key={i} style={{ display: "flex", justifyContent: "space-between", padding: "4px 0", borderBottom: "1px solid var(--border)" }}>
                    <span style={{ color: "var(--muted)" }}>{label}</span>
                    <strong style={{ fontFamily: "var(--font-mono)" }}>{String(value)}</strong>
                  </div>
                ))}
              </div>
              {rationale.target_weight_reason && (
                <div style={{ fontSize: 11, color: "var(--faint)", background: "var(--bg)", borderRadius: 4, padding: 8 }}>
                  {zh ? "计算：基准权重 " : "Calculation: base weight "}{((rationale.target_weight_reason.base_weight ?? 0) * 100).toFixed(1)}%
                  {zh ? " × 置信因子 " : " × confidence multiplier "}{(rationale.target_weight_reason.confidence_multiplier ?? 0).toFixed(2)}
                  = {(rationale.target_weight_reason.raw_weight ?? 0).toFixed(4)}
                  {zh ? " → 上限 " : " → cap "}{(rationale.target_weight_reason.cap ?? 0).toFixed(4)}
                  {zh ? " = 最终 " : " = final "}{((rationale.target_weight_reason.final_weight ?? 0) * 100).toFixed(2)}%
                </div>
              )}
            </div>
          )}

          {tab === "execution" && (
            <div style={{ display: "grid", gap: 6 }}>
              {chain.map(e => {
                const labels: Record<string, [string, string, string]> = {
                  proposal: ["提案", "Proposal", "var(--warn)"], risk: ["风控", "Risk", "var(--warn)"],
                  sizing: ["仓位计算", "Sizing", "var(--accent)"], order: ["下单", "Order", "var(--text)"],
                  fill: ["成交", "Fill", "var(--good)"], position: ["持仓更新", "Position update", "var(--muted)"],
                  decision: ["决策完成", "Decision", "var(--good)"], thesis: ["论点", "Thesis", "var(--accent)"],
                  agent: ["Agent链路", "Agent chain", "var(--accent)"],
                };
                const le = labels[e.event_type];
                const [label, color] = le ? [zh ? le[0] : le[1], le[2]] : [e.event_type, "var(--muted)"];
                return (
                  <div key={e.event_id} style={{ borderLeft: `2px solid ${color}`, paddingLeft: 12, marginBottom: 3 }}>
                    <div style={{ fontSize: 11, color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
                      {new Date(e.timestamp).toLocaleTimeString()} · <span style={{ color }}>{label}</span>
                    </div>
                    <div style={{ fontSize: 13 }}>{e.message}</div>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}

      {/* raw JSON — collapsed by default */}
      <details style={{ marginTop: 16, fontSize: 11 }}>
        <summary style={{ cursor: "pointer", color: "var(--faint)" }}>{zh ? "技术详情（Raw JSON）" : "Technical details (raw JSON)"}</summary>
        <pre className="json-block" style={{ marginTop: 8, maxHeight: 200, overflow: "auto", fontSize: 10 }}>
          {JSON.stringify({ proposal, risk, sizing, fill }, null, 2)}
        </pre>
      </details>
    </div>
  );
}

// ── position intelligence drawer ──

function PositionDrawer({ pos, thesis, onClose, zh }: { pos: PositionInfo; thesis: ThesisInfo | null; onClose: () => void; zh: boolean }) {
  const thesisLabelMap: Record<string, [string, string, string]> = {
    active: ["有效", "Active", "var(--good)"], strengthened: ["增强", "Strengthened", "var(--good)"],
    weakened: ["减弱", "Weakened", "var(--warn)"], challenged: ["受挑战", "Challenged", "var(--warn)"],
    invalidated: ["已失效", "Invalidated", "var(--bad)"], proposed: ["提案", "Proposed", "var(--accent)"],
    closed: ["已关闭", "Closed", "var(--muted)"],
  };
  const tl = thesisLabelMap[thesis?.status ?? ""];
  const [tsLabel, tsColor] = tl ? [zh ? tl[0] : tl[1], tl[2]] : [thesis?.status ?? "—", "var(--muted)"];
  const ret = pos.average_cost > 0 ? ((pos.market_price - pos.average_cost) / pos.average_cost * 100) : 0;

  return (
    <div className="drawer" style={{ maxWidth: 540 }}>
      <button className="drawer-close" onClick={onClose}><X size={14} /> {zh ? "关闭" : "Close"}</button>
      <h2 style={{ marginBottom: 4 }}><PositionName pos={pos} /></h2>
      <div style={{ fontSize: 13, color: "var(--muted)", marginBottom: 12 }}>
        {pos.market} · {pos.secid ? <code style={{ fontSize: 10 }}>{pos.secid}</code> : pos.symbol}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 16 }}>
        {[{ l: zh ? "持有数量" : "Quantity", v: zh ? `${pos.quantity} 股` : `${pos.quantity} sh` }, { l: zh ? "成本" : "Avg cost", v: fmtMoney(pos.average_cost) },
          { l: zh ? "现价" : "Last price", v: fmtMoney(pos.market_price) }, { l: zh ? "持仓价值" : "Market value", v: fmtMoney(pos.market_value) },
          { l: zh ? "收益率" : "Return", v: `${ret >= 0 ? "+" : ""}${ret.toFixed(2)}%`, c: pctColor(ret) },
          { l: zh ? "浮动盈亏" : "Unrealized P&L", v: `${(pos.unrealized_pnl ?? 0) >= 0 ? "+" : ""}${fmtMoney(pos.unrealized_pnl)}`, c: pctColor(pos.unrealized_pnl ?? 0) },
          { l: zh ? "仓位权重" : "Weight", v: `${(pos.weight * 100).toFixed(1)}%` }, { l: zh ? "持仓天数" : "Days held", v: pos.opened_at ? `${Math.floor((Date.now() - new Date(pos.opened_at).getTime()) / 86400000)}` : "—" },
        ].map(({ l, v, c }, i) => (
          <div key={i} className="panel" style={{ padding: "10px 12px" }}>
            <div style={{ fontSize: 10, color: "var(--muted)" }}>{l}</div>
            <div style={{ fontSize: 15, fontFamily: "var(--font-mono)", fontWeight: 700, color: c ?? "var(--text)" }}>{String(v)}</div>
          </div>
        ))}
      </div>

      {/* why hold */}
      <div className="panel" style={{ padding: 14, marginBottom: 12, borderLeft: "3px solid var(--good)" }}>
        <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>{zh ? "为什么继续持有？" : "Why still hold?"}</h4>
        <p style={{ fontSize: 13, color: "var(--text)", margin: 0 }}>{pos.hold_rationale || thesis?.thesis_text || "—"}</p>
      </div>

      {/* thesis */}
      {thesis && (
        <div className="panel" style={{ padding: 14, marginBottom: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
            <h4 style={{ margin: 0, fontSize: 13 }}>{zh ? "投资论点" : "Investment thesis"}</h4>
            <span style={{ color: tsColor, fontWeight: 700, fontSize: 12, border: `1px solid ${tsColor}`, borderRadius: 4, padding: "1px 6px" }}>{tsLabel}</span>
          </div>
          <p style={{ fontSize: 12, color: "var(--muted)", margin: 0 }}>{thesis.thesis_text}</p>
          {thesis.risks?.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <span style={{ fontSize: 11, color: "var(--warn)", fontWeight: 600 }}>{zh ? "风险：" : "Risks: "}</span>
              {thesis.risks.map((r: string, i: number) => <span key={i} style={{ fontSize: 11, marginLeft: 6, background: "var(--bg)", borderRadius: 4, padding: "1px 5px" }}>{r}</span>)}
            </div>
          )}
          {thesis.invalidation_conditions?.length > 0 && (
            <div style={{ marginTop: 6 }}>
              <span style={{ fontSize: 11, color: "var(--bad)", fontWeight: 600 }}>{zh ? "失效条件：" : "Invalidation conditions: "}</span>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18, fontSize: 11 }}>{thesis.invalidation_conditions.map((ic: string, i: number) => <li key={i}>{ic}</li>)}</ul>
            </div>
          )}
        </div>
      )}

      {/* embedded TradeDetail for the opening proposal */}
      {thesis?.proposal_id && (
        <details style={{ fontSize: 10 }}>
          <summary style={{ cursor: "pointer", color: "var(--faint)" }}>
            {zh ? `查看完整交易记录（Proposal ${thesis.proposal_id.slice(0, 8)}）` : `View full trade record (proposal ${thesis.proposal_id.slice(0, 8)})`}
          </summary>
        </details>
      )}
    </div>
  );
}

// ── daily report drawer ──

function DailyReportDrawer({ report, onClose, zh }: { report: any; onClose: () => void; zh: boolean }) {
  const regimeLabels: Record<string, [string, string]> = {
    trending_up: ["趋势上行", "Trending up"], trending_down: ["趋势下行", "Trending down"],
    range_bound: ["区间震荡", "Range-bound"], high_volatility: ["高波动", "High volatility"],
    risk_on: ["风险偏好", "Risk-on"], risk_off: ["风险回避", "Risk-off"], rotation: ["风格轮动", "Rotation"],
  };
  const regime = (k: string) => { const r = regimeLabels[k]; return r ? (zh ? r[0] : r[1]) : k; };
  const dqLabels: Record<string, [string, string]> = {
    good_good: ["正确·好结果", "Right call · good outcome"], good_bad: ["正确·坏结果", "Right call · bad outcome"],
    bad_good: ["错误·好结果", "Wrong call · good outcome"], bad_bad: ["错误·坏结果", "Wrong call · bad outcome"],
  };
  const dq = (k: string) => { const d = dqLabels[k]; return d ? (zh ? d[0] : d[1]) : k; };
  const [tab, setTab] = React.useState<"market"|"account"|"trades"|"lessons"|"tomorrow">("market");
  const tabs = [
    { k: "market", l: zh ? "市场复盘" : "Market review" }, { k: "account", l: zh ? "账户表现" : "Account" },
    { k: "trades", l: zh ? `交易复盘 (${report.trade_count ?? 0})` : `Trades (${report.trade_count ?? 0})` },
    { k: "lessons", l: zh ? `经验 (${report.lessons?.length ?? 0})` : `Lessons (${report.lessons?.length ?? 0})` },
    { k: "tomorrow", l: zh ? "明日计划" : "Tomorrow" },
  ] as const;

  return (
    <div className="drawer" style={{ maxWidth: 680 }}>
      <button className="drawer-close" onClick={onClose}><X size={14} /> {zh ? "关闭" : "Close"}</button>
      <h2>{zh ? "每日报告" : "Daily report"} · {report.date}</h2>
      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 8 }}>
        {zh ? "市场" : "Market"}: {report.market} · regime: {regime(report.market_regime)}
      </div>

      {/* tabs */}
      <div style={{ display: "flex", gap: 2, marginBottom: 12, borderBottom: "1px solid var(--border)" }}>
        {tabs.map(({ k, l }) => (
          <button key={k} onClick={() => setTab(k)}
            style={{ fontSize: 12, padding: "6px 10px", border: "none", background: tab === k ? "var(--bg)" : "none",
              color: tab === k ? "var(--accent)" : "var(--muted)", borderBottom: tab === k ? "2px solid var(--accent)" : "2px solid transparent" }}>
            {l}
          </button>
        ))}
      </div>

      {tab === "market" && (
        <div style={{ display: "grid", gap: 10, fontSize: 13 }}>
          <div><strong>{zh ? "市场环境:" : "Market regime:"}</strong> {regime(report.market_regime)}</div>
          <div><strong>{zh ? "判断依据:" : "Rationale:"}</strong> {report.market_regime_reason || "—"}</div>
          {report.index_performance && Object.keys(report.index_performance).length > 0 && (
            <div>
              <strong>{zh ? "指数表现:" : "Index performance:"}</strong>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 10px", marginTop: 4 }}>
                {Object.entries(report.index_performance as Record<string, number>).map(([k, v]) => (
                  <span key={k} style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: pctColor(v) }}>
                    {k}: {(v * 100).toFixed(2)}%
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {tab === "account" && (
        <div style={{ display: "grid", gap: 6, fontSize: 13 }}>
          <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0", borderBottom: "1px solid var(--border)" }}>
            <span>{zh ? "开盘资产" : "Opening equity"}</span><strong style={{ fontFamily: "var(--font-mono)" }}>{report.opening_equity?.toLocaleString()}</strong></div>
          <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0", borderBottom: "1px solid var(--border)" }}>
            <span>{zh ? "收盘资产" : "Closing equity"}</span><strong style={{ fontFamily: "var(--font-mono)" }}>{report.closing_equity?.toLocaleString()}</strong></div>
          <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0", borderBottom: "1px solid var(--border)" }}>
            <span>{zh ? "日内盈亏" : "Daily P&L"}</span><strong style={{ fontFamily: "var(--font-mono)", color: pctColor(report.daily_pnl ?? 0) }}>{(report.daily_pnl ?? 0) >= 0 ? "+" : ""}{report.daily_pnl?.toFixed(2)}</strong></div>
          <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0", borderBottom: "1px solid var(--border)" }}>
            <span>{zh ? "收益率" : "Daily return"}</span><strong style={{ fontFamily: "var(--font-mono)", color: pctColor(report.daily_return ?? 0) }}>{((report.daily_return ?? 0) * 100).toFixed(2)}%</strong></div>
          <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0" }}>
            <span>{zh ? "最大回撤" : "Max drawdown"}</span><strong style={{ fontFamily: "var(--font-mono)", color: "var(--bad)" }}>{((report.max_intraday_drawdown ?? 0) * 100).toFixed(2)}%</strong></div>
          {report.benchmark_return !== undefined && (<div style={{ fontSize: 11, color: "var(--muted)", marginTop: 4 }}>{zh ? "基准" : "Benchmark"}: {((report.benchmark_return ?? 0) * 100).toFixed(2)}% · {zh ? "超额" : "excess"}: {((report.excess_return ?? 0) * 100).toFixed(2)}%</div>)}
        </div>
      )}

      {tab === "trades" && (
        <div style={{ display: "grid", gap: 8, fontSize: 13 }}>
          <div style={{ display: "flex", gap: 16, marginBottom: 8 }}>
            <span style={{ color: "var(--good)" }}>✓ {zh ? "成功" : "Wins"} {report.successful_reviews?.length ?? 0}</span>
            <span style={{ color: "var(--bad)" }}>✕ {zh ? "失败" : "Losses"} {report.failed_reviews?.length ?? 0}</span>
            <span style={{ color: "var(--warn)" }}>{zh ? "错过" : "Missed"} {report.missed_opportunities?.length ?? 0}</span>
          </div>
          {[...(report.successful_reviews ?? []), ...(report.failed_reviews ?? [])].slice(0, 10).map((tr: any, i: number) => (
            <div key={i} style={{ borderBottom: "1px solid var(--border)", padding: "6px 0", fontSize: 12, borderLeft: `3px solid ${tr.pnl_contribution >= 0 ? "var(--good)" : "var(--bad)"}`, paddingLeft: 8 }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <strong style={{ fontFamily: "var(--font-mono)" }}>{tr.name || tr.symbol} {tr.action}</strong>
                <span style={{ fontFamily: "var(--font-mono)", color: pctColor(tr.pnl_contribution ?? 0) }}>{(tr.pnl_contribution ?? 0) >= 0 ? "+" : ""}{tr.pnl_contribution?.toFixed(2)}</span>
              </div>
              <div style={{ fontSize: 11, color: "var(--muted)" }}>{(tr.thesis ?? "").slice(0, 80)}</div>
              <div style={{ fontSize: 10, color: "var(--faint)" }}>{dq(tr.decision_quality)} · {tr.quality_reason}</div>
            </div>
          ))}
          {(report.missed_opportunities ?? []).slice(0, 5).map((mo: any, i: number) => (
            <div key={"mo" + i} style={{ fontSize: 11, color: "var(--warn)", padding: "4px 8px", background: "var(--bg)", borderRadius: 4 }}>
              <strong>{mo.name || mo.symbol}</strong>: {mo.reason_missed} — {mo.lesson}
            </div>
          ))}
        </div>
      )}

      {tab === "lessons" && (
        <div style={{ display: "grid", gap: 10, fontSize: 13 }}>
          {(report.lessons ?? []).length === 0 && <div className="empty">{zh ? "今日无新经验" : "No new lessons today"}</div>}
          {(report.lessons ?? []).map((l: any, i: number) => (
            <div key={i} style={{ background: "var(--bg)", borderRadius: 6, padding: 12 }}>
              <div style={{ fontSize: 12, fontWeight: 600 }}>{l.title}</div>
              <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 4 }}>{l.description}</div>
              <div style={{ fontSize: 10, color: "var(--faint)", marginTop: 6, display: "flex", gap: 10 }}>
                <span>confidence: {l.confidence}</span>
                <span>recurrence: {l.recurrence_status}</span>
                <span>status: {l.status}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {tab === "tomorrow" && (
        <div style={{ display: "grid", gap: 10, fontSize: 13 }}>
          <div><strong>{zh ? "明日计划:" : "Tomorrow's plan:"}</strong>
            <div style={{ whiteSpace: "pre-wrap", color: "var(--muted)", marginTop: 4 }}>{report.tomorrow_plan || "—"}</div>
          </div>
          {report.tomorrow_watchlist?.length > 0 && (
            <div><strong>{zh ? `重点关注 (${report.tomorrow_watchlist.length}):` : `Focus list (${report.tomorrow_watchlist.length}):`}</strong>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 4 }}>{report.tomorrow_watchlist.map((s: string) => <code key={s} style={{ fontSize: 11 }}>{s}</code>)}</div>
            </div>
          )}
          {report.tomorrow_pending_actions?.length > 0 && (
            <div><strong>{zh ? `待验证操作 (${report.tomorrow_pending_actions.length}):` : `Pending actions (${report.tomorrow_pending_actions.length}):`}</strong>
              {report.tomorrow_pending_actions.map((pa: any, i: number) => (
                <div key={i} style={{ fontSize: 11, padding: "4px 8px", background: "var(--bg)", borderRadius: 4, marginTop: 4 }}>
                  {pa.symbol}: {pa.action} — {pa.reason}
                </div>
              ))}
            </div>
          )}
          {report.tomorrow_unknowns?.length > 0 && (
            <div><strong>{zh ? "不确定事项:" : "Unknowns:"}</strong>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{(report.tomorrow_unknowns ?? []).map((u: string, i: number) => <li key={i} style={{ fontSize: 12, color: "var(--warn)" }}>{u}</li>)}</ul>
            </div>
          )}
        </div>
      )}

      <details style={{ marginTop: 14, fontSize: 10 }}>
        <summary style={{ cursor: "pointer", color: "var(--faint)" }}>{zh ? "原始 JSON" : "Raw JSON"}</summary>
        <pre className="json-block" style={{ maxHeight: 200, overflow: "auto", fontSize: 9 }}>{JSON.stringify(report, null, 2)}</pre>
      </details>
    </div>
  );
}

// ── main dashboard ──

function SessionDashboard({ session: initial, onExit, zh }: { session: TradingSessionInfo; onExit: () => void; zh: boolean }) {
  const [session, setSession] = React.useState(initial);
  const [portfolio, setPortfolio] = React.useState<any>(null);
  const [events, setEvents] = React.useState<DecisionEventInfo[]>([]);
  const [decisions, setDecisions] = React.useState<DecisionStory[]>([]);
  const [curve, setCurve] = React.useState<EquityPoint[]>([]);
  const [theses, setTheses] = React.useState<ThesisInfo[]>([]);
  const [truth, setTruth] = React.useState<SystemTruth | null>(null);
  const [proposals, setProposals] = React.useState<ProposalRow[]>([]);
  const [dailyReports, setDailyReports] = React.useState<any[]>([]);
  const [reportDrawer, setReportDrawer] = React.useState<any | null>(null);
  const [detailPid, setDetailPid] = React.useState<string | null>(null);
  const [posDrawer, setPosDrawer] = React.useState<PositionInfo | null>(null);
  const [busy, setBusy] = React.useState("");
  const [showDebug, setShowDebug] = React.useState(false);

  const refresh = React.useCallback(async () => {
    try {
      const [s, ev, eq, dec, th, dr, tr, pr] = await Promise.all([
        api.tradingSession(initial.session_id),
        api.tradingEvents(initial.session_id, 120),
        api.tradingEquityCurve(initial.session_id),
        api.tradingDecisions(initial.session_id),
        api.tradingTheses(initial.session_id),
        api.tradingDailyReports(initial.session_id),
        api.tradingSystemTruth(initial.session_id).catch(() => null),
        api.tradingProposals(initial.session_id).catch(() => ({ items: [] })),
      ]);
      setSession(s); setPortfolio(s.portfolio ?? null); setEvents(ev.items);
      setCurve(eq.items); setDecisions(dec.items); setTheses(th.items);
      setDailyReports(dr.items ?? []); setTruth(tr); setProposals(pr.items ?? []);
    } catch { /* transient */ }
  }, [initial.session_id]);

  React.useEffect(() => {
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [refresh]);

  const act = async (action: "start" | "pause" | "resume" | "stop" | "emergency-stop" | "run-cycle") => {
    setBusy(action);
    // Optimistic local status update — the button changes immediately.
    // If the API call fails we refresh to get the real state back.
    const optimistic: Record<string, string> = {
      start: "running", pause: "paused", resume: "running",
      stop: "stopped", "emergency-stop": "halted", "run-cycle": session.status,
    };
    const prevStatus = session.status;
    setSession(s => s ? { ...s, status: optimistic[action] ?? s.status } : s);
    try { await api.tradingSessionAction(session.session_id, action); await refresh(); }
    catch (e) { alert(String(e)); setSession(s => s ? { ...s, status: prevStatus } : s); }
    setBusy("");
  };

  // ── live heartbeat: detect cycle_count changes and compute staleness ──
  const prevCycleRef = React.useRef(session.cycle_count);
  const [pulse, setPulse] = React.useState(false);
  const [lastCycleAgo, setLastCycleAgo] = React.useState<string>("");
  React.useEffect(() => {
    const now = Date.now();
    if (session.cycle_count !== prevCycleRef.current) {
      prevCycleRef.current = session.cycle_count;
      setPulse(true);
      const t = setTimeout(() => setPulse(false), 600);
      return () => clearTimeout(t);
    }
    // Compute staleness from last_cycle_at
    if (session.last_cycle_at) {
      const seconds = Math.round((now - new Date(session.last_cycle_at).getTime()) / 1000);
      setLastCycleAgo(seconds <= 5 ? (zh ? "刚刚" : "just now") : (zh ? `${seconds}秒前` : `${seconds}s ago`));
    }
  }, [session.cycle_count, session.last_cycle_at, zh]);
  // tick staleness every 2s even when cycle_count hasn't changed
  React.useEffect(() => {
    const t = setInterval(() => {
      if (session.last_cycle_at) {
        const s = Math.round((Date.now() - new Date(session.last_cycle_at).getTime()) / 1000);
        setLastCycleAgo(s <= 5 ? (zh ? "刚刚" : "just now") : (zh ? `${s}秒前` : `${s}s ago`));
      }
    }, 2000);
    return () => clearInterval(t);
  }, [session.last_cycle_at, zh]);

  const STATUS_LABEL: Record<string, [string, string, string]> = {
    created: ["未启动", "Not started", "var(--muted)"], starting: ["启动中", "Starting", "var(--warn)"],
    running: ["运行中", "Running", "var(--good)"], paused: ["已暂停", "Paused", "var(--warn)"],
    halted: ["紧急停止", "Halted", "var(--bad)"], stopped: ["已停止", "Stopped", "var(--muted)"],
  };
  const [snapshot, setSnapshot] = React.useState<any>(null);
  React.useEffect(() => { api.tradingGlobalSnapshot().then(setSnapshot).catch(() => {}); }, [session.session_id]);
  React.useEffect(() => {
    const t = setInterval(() => { api.tradingGlobalSnapshot().then(setSnapshot).catch(() => {}); }, 30000);
    return () => clearInterval(t);
  }, [session.session_id]);

  const sl = STATUS_LABEL[session.status];
  const [statusLabel, statusColor] = sl ? [zh ? sl[0] : sl[1], sl[2]] : [session.status, "var(--muted)"];
  const opState = session.operational_state;
  const totalPnl = session.current_equity - session.initial_capital;
  const totalReturn = (totalPnl / session.initial_capital) * 100;
  const positions: PositionInfo[] = portfolio?.positions ?? [];
  const marketValue = positions.reduce((s: number, p: any) => s + (p.market_value ?? 0), 0);
  const maxDd = React.useMemo(() => {
    let peak = -Infinity, dd = 0;
    for (const p of curve) { peak = Math.max(peak, p.equity); dd = Math.min(dd, (p.equity - peak) / peak); }
    return dd * 100;
  }, [curve]);
  const ccy = session.currency === "CNY" ? "¥" : "$";

  return (
    <div className="trading-cockpit">
      {/* ── control bar ── */}
      <div className="trade-command-bar">
        <div className="command-identity">
          <div className="command-title-row">
            <span className="eyebrow">LIVE SIMULATION CONTROL</span>
            <h2>{zh ? "AI 投资组合管理器" : "AI portfolio manager"}</h2>
            <span data-testid="session-status" style={{
              color: statusColor, fontWeight: 700, fontSize: 13,
              border: `1px solid ${statusColor}`, borderRadius: 12, padding: "2px 10px",
              display: "inline-flex", alignItems: "center", gap: 6,
            }}>
              {/* Pulsing heartbeat dot when running */}
              {session.status === "running" && (
                <span style={{
                  display: "inline-block", width: 8, height: 8, borderRadius: "50%",
                  background: pulse ? "var(--good)" : "rgba(0,200,0,0.3)",
                  boxShadow: pulse ? "0 0 6px var(--good)" : "none",
                  transition: "all 0.2s ease",
                }} />
              )}
              {statusLabel}
            </span>
          </div>
          <div className="command-meta">
            SIMULATION · {zh ? "周期" : "cycle"} #{session.cycle_count} · {session.allowed_markets.join("+")} · {session.risk_profile} · {zh ? "扫描" : "scan"} {session.scan_interval}s
            {truth && <span style={{ color: modeInfo(truth.effective_mode, zh).color, marginLeft: 6 }}>· {modeInfo(truth.effective_mode, zh).short}</span>}
            {session.status === "running" && lastCycleAgo && (
              <span style={{ color: parseInt(lastCycleAgo) > 30 ? "var(--warn)" : "var(--muted)" }}>
                {" · "}{zh ? "上次周期" : "last cycle"} {lastCycleAgo}
              </span>
            )}
            {session.status === "paused" && lastCycleAgo && (
              <span style={{ color: "var(--warn)" }}>
                {" · "}{zh ? `已暂停（最后周期 ${lastCycleAgo}）` : `paused (last cycle ${lastCycleAgo})`}
              </span>
            )}
            {session.next_cycle_at && session.status === "running" && <> · {zh ? "下次" : "next"} {new Date(session.next_cycle_at).toLocaleTimeString()}</>}
          </div>
        </div>
        <div className="command-actions">
          {(session.status === "created" || session.status === "stopped") && (
            <button className="primary" data-testid="start-trading" onClick={() => act("start")} disabled={!!busy}><Play size={14} /> {zh ? "启动" : "Start"}</button>
          )}
          {session.status === "running" && <button onClick={() => act("pause")} disabled={!!busy}>{busy === "pause" ? (zh ? "暂停中…" : "Pausing…") : <><Pause size={14} /> {zh ? "暂停" : "Pause"}</>}</button>}
          {session.status === "paused" && <button className="primary" onClick={() => act("resume")} disabled={!!busy}>{busy === "resume" ? (zh ? "恢复中…" : "Resuming…") : <><Play size={14} /> {zh ? "继续" : "Resume"}</>}</button>}
          {(session.status === "running" || session.status === "paused") && <button onClick={() => act("stop")} disabled={!!busy}>{busy === "stop" ? (zh ? "停止中…" : "Stopping…") : <><Square size={14} /> {zh ? "停止" : "Stop"}</>}</button>}
          {(session.status === "created" || session.status === "stopped" || session.status === "paused") && (
            <button data-testid="run-cycle" onClick={() => act("run-cycle")} disabled={!!busy}>{busy === "run-cycle" ? (zh ? "执行中…" : "Running…") : (zh ? "单轮执行" : "Run one cycle")}</button>
          )}
          {session.status !== "halted" && (
            <button data-testid="emergency-stop" style={{ borderColor: "var(--bad)", color: "var(--bad)" }}
              onClick={() => { if (confirm(zh ? "紧急停止将阻断所有新提案与订单。确认？" : "An emergency stop blocks all new proposals and orders. Confirm?")) act("emergency-stop"); }}
              disabled={!!busy}><AlertTriangle size={14} /> {zh ? "紧急停止" : "Emergency stop"}</button>
          )}
          <button onClick={onExit} style={{ fontSize: 12 }}>{zh ? "会话列表" : "Sessions"}</button>
        </div>
      </div>

      {session.status === "halted" && <div className="error-box">
        {zh ? "⚠ 会话已紧急停止：调度器与 Kill Switch 均已激活。" : "⚠ Session emergency-stopped: scheduler and kill switch are both active."}
      </div>}

      {/* ── System Truth Bar: which brains are actually participating ── */}
      <SystemTruthBar truth={truth} zh={zh} />

      {/* ── global market clocks + agent operational state ── */}
      <div className="market-ops-row">
        <div className="market-clock-strip">
          <Globe size={16} style={{ color: "var(--accent)" }} />
          <span style={{ fontSize: 12, fontWeight: 700 }}>{zh ? "全球市场" : "Global markets"}</span>
          {(snapshot?.markets ? Object.entries(snapshot.markets) as [string, any][] : []).map(([mkt, info]: [string, any]) => {
            const isOpen = info.session === "open";
            const localT = info.local_time?.slice(0, 5) ?? "";
            return (
              <span key={mkt} title={info.label} style={{
                fontSize: 11, fontFamily: "var(--font-mono)",
                background: isOpen ? "rgba(0,200,0,0.12)" : "var(--bg)",
                borderRadius: 6, padding: "4px 10px",
                border: `1px solid ${isOpen ? "var(--good)" : "var(--border)"}`,
                color: isOpen ? "var(--good)" : "var(--muted)",
                display: "flex", alignItems: "center", gap: 4,
              }}>
                {isOpen ? <Sun size={12} /> : <Moon size={12} />}
                {zh ? (mkt === "CN" ? "A股" : mkt === "US" ? "美股" : mkt === "HK" ? "港股" : mkt)
                    : (mkt === "CN" ? "A-shares" : mkt === "US" ? "US" : mkt === "HK" ? "HK" : mkt)}
                {" "}{localT}
              </span>
            );
          })}
          {!snapshot?.markets && <span style={{ fontSize: 11, color: "var(--faint)" }}>{zh ? "获取中..." : "Loading…"}</span>}
        </div>
        <div className="agent-phase-strip">
          <Eye size={14} style={{ color: opState ? "var(--good)" : "var(--muted)" }} />
          <span style={{ fontWeight: 600, color: "var(--accent)" }}>
            {(zh ? opState?.phase_label_zh : (opState?.phase_label_en ?? opState?.phase_label_zh)) ?? "—"}
          </span>
          {opState?.current_activity && <span style={{ color: "var(--muted)", fontSize: 11 }}>{opState.current_activity}</span>}
          {opState?.thesis_updates_since_close !== undefined && opState.thesis_updates_since_close > 0 && (
            <span style={{ fontSize: 11, color: "var(--warn)" }}>{zh ? "Thesis 更新" : "Thesis updates"} {opState.thesis_updates_since_close}</span>
          )}
        </div>
      </div>

      {/* ── metrics ── */}
      <div className="trade-kpi-grid">
        {[
          [zh ? "初始资金" : "Initial capital", fmtMoney(session.initial_capital, ccy)], [zh ? "当前总资产" : "Current equity", fmtMoney(session.current_equity, ccy), pctColor(totalPnl)],
          [zh ? "现金" : "Cash", fmtMoney(session.cash, ccy)], [zh ? "持仓市值" : "Position value", fmtMoney(marketValue, ccy)],
          [zh ? "总盈亏" : "Total P&L", `${totalPnl >= 0 ? "+" : ""}${fmtMoney(totalPnl, ccy)}`, pctColor(totalPnl)], [zh ? "总收益率" : "Total return", `${totalReturn >= 0 ? "+" : ""}${totalReturn.toFixed(2)}%`, pctColor(totalReturn)],
          [zh ? "最大回撤" : "Max drawdown", `${maxDd.toFixed(2)}%`, maxDd < -3 ? "var(--bad)" : "var(--muted)"], [zh ? "持仓数" : "Positions", `${positions.length}`, "var(--muted)"],
        ].map(([label, value, color], i) => (
          <div key={i} className="trade-kpi">
            <div>{String(label)}</div>
            <strong style={{ color: (color as string) ?? "var(--text)" }}>{String(value)}</strong>
          </div>
        ))}
      </div>

      <div className="trade-workbench">
        {/* ── left: positions + agent status ── */}
        <div className="trade-primary">
          {/* ── equity curve ── */}
          <div className="desk-panel equity-panel">
            <div className="section-title">
              <h2><BarChart3 size={16} style={{ verticalAlign: -2 }} /> {zh ? "资产曲线" : "Equity curve"}</h2>
              <p>{zh ? `每周期记录 · ${curve.length} 点` : `One point per cycle · ${curve.length} points`}</p>
            </div>
            <EquityCurve points={curve} initial={session.initial_capital} ccy={ccy} zh={zh} />
          </div>

          {/* agent current activity — real stage from system truth */}
          <div className="desk-panel" data-testid="agent-activity">
            <div className="section-title"><h2><Cpu size={14} style={{ verticalAlign: -2 }} /> {zh ? "Agent 当前活动" : "Agent activity"}</h2></div>
            {(() => {
              const stageLabels: Record<string, [string, string]> = {
                idle: ["空闲", "Idle"], scan: ["扫描市场", "Scanning market"], quant_inference: ["量化推理", "Quant inference"],
                position_review: ["持仓复核", "Position review"], candidate_research: ["候选研究", "Candidate research"],
              };
              const stage = truth?.activity?.stage ?? "idle";
              const sg = stageLabels[stage];
              const stageText = sg ? (zh ? sg[0] : sg[1]) : stage;
              const st = STATUS_LABEL[session.status];
              const mi = modeInfo(truth?.effective_mode, zh);
              const rows = [
                { l: zh ? "当前阶段" : "Current stage", v: session.status === "running" ? stageText : (st ? (zh ? st[0] : st[1]) : session.status) },
                { l: zh ? "正在研究" : "Researching", v: truth?.activity?.target || "—" },
                { l: zh ? "本轮周期" : "Cycle", v: `#${session.cycle_count}` },
                { l: zh ? "自选股" : "Watchlist", v: zh ? `${events.find(e => e.event_type === "scan")?.payload?.universe_size ?? "—"} 只` : `${events.find(e => e.event_type === "scan")?.payload?.universe_size ?? "—"} names` },
                { l: zh ? "决策模式" : "Decision mode", v: mi.label, c: mi.color },
                { l: zh ? "上次决策" : "Last decision", v: decisions[0] ? `${decisions[0].payload?.action?.toUpperCase()} ${decisions[0].symbol}` : "—" },
                { l: zh ? "风控拒绝" : "Risk rejections", v: zh ? `${session.risk_rejections} 次` : `${session.risk_rejections}` },
                { l: zh ? "下次扫描" : "Next scan", v: session.next_cycle_at && session.status === "running" ? new Date(session.next_cycle_at).toLocaleTimeString() : "—" },
              ];
              return (
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 12px", fontSize: 12 }}>
                  {rows.map(({ l, v, c }, i) => (
                    <div key={i}>
                      <span style={{ color: "var(--muted)" }}>{l}</span>
                      <span style={{ color: (c as string) ?? "var(--text)", fontWeight: 600, marginLeft: 6 }}>{String(v)}</span>
                    </div>
                  ))}
                </div>
              );
            })()}
          </div>

          {/* positions */}
          <div className="desk-panel" data-testid="positions-panel">
            <div className="section-title"><h2><Target size={14} style={{ verticalAlign: -2 }} /> {zh ? "持仓" : "Positions"} ({positions.length})</h2></div>
            {!positions.length ? <div className="empty">{zh ? "暂无持仓" : "No positions"}</div> : (
              <div className="data-table">
                <div className="row" style={{ gridTemplateColumns: "80px minmax(70px,1fr) 50px 50px 50px 70px 50px 50px", fontSize: 10, color: "var(--muted)" }}>
                  <span>{zh ? "代码" : "Symbol"}</span><span>{zh ? "名称" : "Name"}</span><span>{zh ? "数量" : "Qty"}</span>
                  <span>{zh ? "成本" : "Cost"}</span><span>{zh ? "现价" : "Last"}</span><span>{zh ? "浮动盈亏" : "P&L"}</span>
                  <span>{zh ? "权重" : "Weight"}</span><span>{zh ? "论点" : "Thesis"}</span>
                </div>
                {positions.map(p => {
                  const t = theses.find(x => x.thesis_id === p.active_thesis_id);
                  const tLabel = t?.status === "active" ? (zh ? "有效" : "Active")
                    : t?.status === "weakened" ? (zh ? "减弱" : "Weakened") : t?.status ?? "…";
                  return (
                    <div key={p.symbol} className="row clickable"
                      style={{ gridTemplateColumns: "80px minmax(70px,1fr) 50px 50px 50px 70px 50px 50px", fontSize: 11 }}
                      onClick={() => setPosDrawer(p)}>
                      <strong style={{ fontFamily: "var(--font-mono)" }}>{p.symbol}</strong>
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{p.name || p.symbol}</span>
                      <span>{p.quantity}</span>
                      <span style={{ fontFamily: "var(--font-mono)" }}>{p.average_cost?.toFixed(2)}</span>
                      <span style={{ fontFamily: "var(--font-mono)" }}>{p.market_price?.toFixed(2)}</span>
                      <span style={{ fontFamily: "var(--font-mono)", color: pctColor(p.unrealized_pnl ?? 0) }}>
                        {(p.unrealized_pnl ?? 0) >= 0 ? "+" : ""}{p.unrealized_pnl?.toFixed(2)}
                      </span>
                      <span>{((p.weight ?? 0) * 100).toFixed(1)}%</span>
                      <span style={{ color: p.active_thesis_id ? "var(--good)" : "var(--faint)", fontSize: 10 }}>{p.active_thesis_id ? tLabel : "—"}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        {/* ── right: decision stories + proposals ── */}
        <div className="trade-secondary">
          {/* decision stories */}
          <div className="desk-panel" data-testid="decision-feed">
            <div className="section-title">
              <h2><FileSearch size={14} style={{ verticalAlign: -2 }} /> {zh ? "决策故事" : "Decision stories"}</h2>
              <p>{zh ? "按决策聚合，非原始事件流" : "Aggregated per decision, not a raw event stream"}</p>
            </div>
            <div style={{ maxHeight: 500, overflowY: "auto", display: "grid", gap: 8 }}>
              {!decisions.length && <div className="empty" style={{ fontSize: 12 }}>
                {zh ? "启动后每次交易完成后显示决策故事" : "Decision stories appear after each completed trade"}
              </div>}
              {decisions.map(d => (
                <DecisionCard key={d.event_id} dec={d} zh={zh} onOpen={() => {
                  const pid = d.payload?.proposal_id;
                  if (pid) setDetailPid(pid);
                }} />
              ))}
            </div>
          </div>

          {/* proposals — first-class rows with LIVE status (executed ≠ pending) */}
          <div className="desk-panel" data-testid="proposals-panel">
            <div className="section-title">
              <h2>{zh ? "提案" : "Proposals"}</h2>
              <p>{zh ? "状态实时同步 · 每标的去重" : "Live status · de-duplicated per instrument"}</p>
            </div>
            {proposals.slice(0, 8).map(p => {
              const [stLabel, stColor] = statusInfo(p.status, zh);
              const mi = modeInfo(p.decision_mode, zh);
              return (
                <div key={p.proposal_id} style={{ borderBottom: "1px solid var(--border)", padding: "6px 0", fontSize: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <strong style={{ fontFamily: "var(--font-mono)" }}>{p.symbol} {p.action?.toUpperCase()}</strong>
                    <span data-testid={`proposal-status-${p.symbol}`} style={{ color: stColor, fontWeight: 700 }}>{stLabel}</span>
                  </div>
                  <div style={{ color: "var(--muted)", fontFamily: "var(--font-mono)", fontSize: 11, display: "flex", gap: 8, flexWrap: "wrap" }}>
                    <span>{zh ? "目标" : "target"} {(p.target_weight * 100).toFixed(1)}%</span>
                    <span>{zh ? "置信" : "conf."} {Number(p.confidence ?? 0).toFixed(2)}</span>
                    {p.cycle != null && <span>#{p.cycle}</span>}
                    <span style={{ color: mi.color }}>{mi.short}</span>
                  </div>
                  <button style={{ fontSize: 10, padding: "1px 6px", marginTop: 2 }} onClick={() => setDetailPid(p.proposal_id)}>
                    {zh ? "完整链路" : "Full chain"}
                  </button>
                </div>
              );
            })}
            {!proposals.length && <div className="empty" style={{ fontSize: 12 }}>{zh ? "暂无提案" : "No proposals"}</div>}
          </div>

          {/* daily reports */}
          <div className="desk-panel">
            <div className="section-title">
              <h2>{zh ? "每日报告" : "Daily reports"}</h2>
              <p>{zh ? "收盘后自动生成" : "Generated automatically after the close"}</p>
            </div>
            {!dailyReports.length && <div className="empty" style={{ fontSize: 12 }}>
              {zh ? "暂无报告——每日收盘后自动生成" : "No reports yet — generated after each close"}
            </div>}
            {dailyReports.slice(0, 3).map(r => (
              <div key={r.report_id} style={{ borderBottom: "1px solid var(--border)", padding: "8px 0", fontSize: 12, cursor: "pointer" }}
                   onClick={() => setReportDrawer(r)}>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <strong>{r.date}</strong>
                  <span style={{ fontFamily: "var(--font-mono)", color: pctColor(r.daily_pnl ?? 0) }}>
                    {(r.daily_pnl ?? 0) >= 0 ? "+" : ""}{r.daily_pnl?.toFixed(2)}
                  </span>
                </div>
                <div style={{ color: "var(--muted)", fontSize: 11 }}>
                  {zh
                    ? (r.market_regime === "trending_up" ? "趋势上行" : r.market_regime === "trending_down" ? "趋势下行" :
                       r.market_regime === "range_bound" ? "区间震荡" : r.market_regime === "high_volatility" ? "高波动" :
                       r.market_regime === "risk_on" ? "风险偏好" : r.market_regime === "risk_off" ? "风险回避" : r.market_regime ?? "—")
                    : (r.market_regime === "trending_up" ? "Trending up" : r.market_regime === "trending_down" ? "Trending down" :
                       r.market_regime === "range_bound" ? "Range-bound" : r.market_regime === "high_volatility" ? "High volatility" :
                       r.market_regime === "risk_on" ? "Risk-on" : r.market_regime === "risk_off" ? "Risk-off" : r.market_regime ?? "—")}
                  {" · "}{zh ? `${r.trade_count ?? 0} 笔交易` : `${r.trade_count ?? 0} trades`}
                  {r.session_id && <span style={{ marginLeft: 8, fontSize: 10, color: "var(--faint)", fontFamily: "var(--font-mono)" }}>{r.session_id.slice(0, 6)}</span>}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ── quant intelligence (Phase Q) ── */}
      <div className="quant-band">
        <QuantIntelligencePanel sessionId={session.session_id} lang={zh ? "zh-CN" : "en-US"} />
      </div>

      {/* ── debug tool, collapsed by default ── */}
      <div className="desk-panel debug-panel">
        <button style={{ fontSize: 11 }} onClick={() => setShowDebug(v => !v)}>
          {zh ? `${showDebug ? "隐藏" : "显示"}开发者调试工具（手工提案——非产品路径）`
              : `${showDebug ? "Hide" : "Show"} developer debug tool (manual proposal — not a product path)`}
        </button>
        {showDebug && <DebugProposalForm onDone={refresh} zh={zh} />}
      </div>

      {detailPid && <ProposalDetailDrawer sessionId={session.session_id} proposalId={detailPid} onClose={() => setDetailPid(null)} zh={zh} />}
      {posDrawer && <PositionDrawer pos={posDrawer} thesis={theses.find(t => t.thesis_id === posDrawer.active_thesis_id) ?? null} onClose={() => setPosDrawer(null)} zh={zh} />}
      {reportDrawer && <DailyReportDrawer report={reportDrawer} onClose={() => setReportDrawer(null)} zh={zh} />}
    </div>
  );
}

function DebugProposalForm({ onDone, zh }: { onDone: () => void; zh: boolean }) {
  const [sym, setSym] = React.useState("AAPL");
  const [weight, setWeight] = React.useState(5);
  const [thesis, setThesis] = React.useState("");
  const [msg, setMsg] = React.useState("");
  const submit = async () => {
    setMsg("");
    try {
      const r = await api.tradingDebugProposal({ symbol: sym, action: "buy", target_weight: weight / 100, thesis, confidence: 0.6 });
      setMsg(JSON.stringify({ executed: r.executed, risk: r.risk_result?.reason, price_source: r.price_source }, null, 2));
      onDone();
    } catch (e) { setMsg(String(e)); }
  };
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 11, color: "var(--warn)", marginBottom: 6 }}>
        {zh ? "⚠ 仅用于测试风控/撮合。成交价强制取自实时行情网关，不可自填价格；受风控与 kill switch 约束。"
            : "⚠ For testing risk checks and fills only. The fill price is forced to come from the live quote gateway — it cannot be typed in — and remains subject to risk control and the kill switch."}
      </div>
      <div className="investigate-form" style={{ gridTemplateColumns: "110px 90px 1fr auto" }}>
        <label>{zh ? "股票" : "Symbol"}<input value={sym} onChange={e => setSym(e.target.value.toUpperCase())} /></label>
        <label>{zh ? "权重%" : "Weight %"}<input type="number" value={weight} onChange={e => setWeight(Number(e.target.value))} /></label>
        <label>{zh ? "说明" : "Note"}<input value={thesis} onChange={e => setThesis(e.target.value)} /></label>
        <button onClick={submit}>{zh ? "提交调试提案" : "Submit debug proposal"}</button>
      </div>
      {msg && <pre className="json-block" style={{ fontSize: 11, marginTop: 8 }}>{msg}</pre>}
    </div>
  );
}

// ── page root ──

export function TradingPage({ lang }: { lang: string }) {
  const zh = lang.startsWith("zh");
  const [sessions, setSessions] = React.useState<TradingSessionInfo[] | null>(null);
  const [active, setActive] = React.useState<TradingSessionInfo | null>(null);

  const load = React.useCallback(() => {
    api.tradingSessions().then(d => {
      setSessions(d.items);
      const running = d.items.find(s => s.status === "running" || s.status === "paused");
      if (running && !active) setActive(running);
    }).catch(() => setSessions([]));
  }, [active]);
  React.useEffect(() => { load(); }, []);

  if (active) return <SessionDashboard session={active} onExit={() => { setActive(null); load(); }} zh={zh} />;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <SessionConfig onCreated={s => setActive(s)} zh={zh} />
      <div className="panel wide">
        <div className="section-title"><h2>{zh ? "历史会话" : "Past sessions"}</h2></div>
        {sessions === null && <div className="empty">{zh ? "加载中…" : "Loading…"}</div>}
        {sessions !== null && !sessions.length && <div className="empty">{zh ? "暂无会话" : "No sessions yet"}</div>}
        {(sessions ?? []).map(s => {
          const SESSION_STATUS: Record<string, [string, string, string]> = {
            created: ["未启动", "Not started", "var(--muted)"], running: ["运行中", "Running", "var(--good)"],
            paused: ["已暂停", "Paused", "var(--warn)"], halted: ["紧急停止", "Halted", "var(--bad)"],
            stopped: ["已停止", "Stopped", "var(--muted)"],
          };
          const ss = SESSION_STATUS[s.status];
          const [label, color] = ss ? [zh ? ss[0] : ss[1], ss[2]] : [s.status, "var(--muted)"];
          const pnl = s.current_equity - s.initial_capital;
          return (
            <button key={s.session_id} className="row clickable" onClick={() => setActive(s)}
              style={{ display: "grid", gridTemplateColumns: "100px 80px 100px 100px 100px 1fr", width: "100%", alignItems: "center" }}>
              <code style={{ fontSize: 11 }}>{s.session_id.slice(0, 8)}</code>
              <span style={{ color, fontSize: 12 }}>{label}</span>
              <span style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{fmtMoney(s.initial_capital)}</span>
              <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: pctColor(pnl) }}>{pnl >= 0 ? "+" : ""}{fmtMoney(pnl)}</span>
              <span style={{ fontSize: 11, color: "var(--muted)" }}>{s.allowed_markets.join("+")} · {s.risk_profile}</span>
              <span style={{ fontSize: 11, color: "var(--faint)" }}>{zh ? "周期" : "cycles"} {s.cycle_count} · {new Date(s.created_at).toLocaleString()}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
