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

const DECISION_MODE: Record<string, { label: string; color: string; short: string }> = {
  deterministic_signal:   { label: "确定性信号策略", color: "var(--warn)", short: "确定性信号" },
  quant_only:             { label: "量化模型", color: "var(--accent)", short: "量化" },
  quant_deterministic:    { label: "量化排名 + 确定性研究", color: "var(--accent)", short: "量化+规则" },
  llm_research:           { label: "LLM 深度研究", color: "var(--good)", short: "LLM" },
  quant_llm_hybrid:       { label: "量化 + LLM 混合", color: "var(--good)", short: "量化+LLM" },
  quant_only_fallback:    { label: "量化（LLM 降级）", color: "var(--warn)", short: "量化(降级)" },
  deterministic_fallback: { label: "确定性信号（降级）", color: "var(--bad)", short: "确定性(降级)" },
};
function modeInfo(mode: string | undefined) {
  return DECISION_MODE[mode ?? ""] ?? { label: mode ?? "未知", color: "var(--muted)", short: mode ?? "?" };
}

const PROPOSAL_STATUS: Record<string, [string, string]> = {
  pending: ["待风控", "var(--warn)"], approved: ["已核准", "var(--accent)"],
  adjusted: ["已调整", "var(--accent)"], rejected: ["已拒绝", "var(--bad)"],
  executed: ["已成交", "var(--good)"], expired: ["已过期", "var(--muted)"],
};

// System-truth status → color + user-facing degradation explanation
const TRUTH_STATUS: Record<string, string> = {
  HEALTHY: "var(--good)", ACTIVE: "var(--good)", DEGRADED: "var(--warn)",
  UNAVAILABLE: "var(--bad)", HALTED: "var(--bad)", NOT_USED: "var(--muted)",
};
function truthColor(s: string) { return TRUTH_STATUS[s] ?? "var(--muted)"; }

// ── System Truth Bar: the un-fakeable "which brains are real" strip ──

function SystemTruthBar({ truth }: { truth: SystemTruth | null }) {
  if (!truth) return null;
  const c = truth.components;
  const rows: [string, typeof c.quant][] = [
    ["行情数据", c.market_data], ["量化模型", c.quant], ["LLM 研究", c.llm],
    ["确定性研究", c.research], ["风控", c.risk], ["虚拟券商", c.virtual_broker],
  ];
  const mi = modeInfo(truth.effective_mode);
  // Degraded components get a plain-language explanation.
  const degraded = rows.filter(([, comp]) => comp.status === "UNAVAILABLE" || comp.status === "DEGRADED");
  return (
    <div className="truth-strip" data-testid="system-truth-bar" style={{ borderLeft: `3px solid ${mi.color}` }}>
      <div className="truth-strip-head">
        <div className="truth-title">
          <ShieldCheck size={16} style={{ color: mi.color }} />
          <strong>系统真相 · 当前真实决策链</strong>
        </div>
        <div className="truth-mode">
          <span style={{ color: "var(--muted)" }}>有效决策模式：</span>
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
              <span style={{ color: truthColor(comp.status), fontWeight: 600 }}>{label}未参与本轮决策</span>
              {" — "}{comp.detail}
            </div>
          ))}
          <div style={{ marginTop: 4, color: "var(--faint)" }}>
            系统已如实降级为 <b style={{ color: mi.color }}>{mi.label}</b>，不会伪装拥有未启用的能力。
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

function DecisionQualityBadge({ quality, size }: { quality: string; size?: number }) {
  const s = size ?? 10;
  const map: Record<string, [string, string]> = {
    signal_only: ["信号驱动", "var(--warn)"],
    evidence_grounded: ["证据支撑", "var(--good)"],
    hybrid: ["混合研究", "var(--accent)"],
  };
  const [label, color] = map[quality] ?? [quality, "var(--muted)"];
  return <span style={{ fontSize: s, color, fontWeight: 600 }}>{label}</span>;
}

// ── session config ──

function SessionConfig({ onCreated }: { onCreated: (s: TradingSessionInfo) => void }) {
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
      });
      onCreated(s);
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };
  return (
    <div className="trading-launch" data-testid="session-config">
      <div className="launch-copy">
        <span className="eyebrow">AUTONOMOUS DESK</span>
        <h2><Bot size={20} /> 配置 AI 自主模拟交易</h2>
        <p>设定资金与约束，扫描、研究、提案、风控、下单全由 Agent 自主完成。</p>
      </div>
      <div className="launch-form">
        <label style={{ fontSize: 13 }}>初始资金
          <input data-testid="cfg-capital" type="number" value={capital} onChange={e => setCapital(Number(e.target.value))} min={1000} step={10000} />
        </label>
        <div style={{ fontSize: 13 }}>允许市场
          <div style={{ display: "flex", gap: 10, marginTop: 6 }}>
            {[["CN", "A 股"], ["US", "美股"]].map(([k, label]) => (
              <label key={k} style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 13 }}>
                <input type="checkbox" checked={markets.includes(k)} onChange={() => toggleMarket(k)} /> {label}
              </label>
            ))}
          </div>
        </div>
        <label style={{ fontSize: 13 }}>风险偏好
          <select value={risk} onChange={e => setRisk(e.target.value)}>
            <option value="conservative">保守（单股≤5%）</option>
            <option value="balanced">平衡（单股≤10%）</option>
            <option value="aggressive">激进（单股≤15%）</option>
          </select>
        </label>
        <label style={{ fontSize: 13 }}>最大单股仓位 %
          <input type="number" value={maxPos} onChange={e => setMaxPos(Number(e.target.value))} min={1} max={50} />
        </label>
        <label style={{ fontSize: 13 }}>最大日亏损 %
          <input type="number" value={maxLoss} onChange={e => setMaxLoss(Number(e.target.value))} min={1} max={50} />
        </label>
        <label style={{ fontSize: 13 }}>扫描周期（秒）
          <input type="number" value={interval} onChange={e => setIntervalSec(Number(e.target.value))} min={10} step={30} />
        </label>
      </div>
      <div className="launch-actions">
        <span style={{ fontSize: 12, color: "var(--muted)" }}>交易范围：我的自选股（跨市场）· SIMULATION 模式</span>
        <button className="primary" data-testid="create-session" onClick={create} disabled={busy || !markets.length}>
          <Play size={14} /> 创建并启动
        </button>
      </div>
      {error && <div className="error-box" style={{ marginTop: 10 }}>{error}</div>}
    </div>
  );
}

// ── equity curve ──

function EquityCurve({ points, initial, ccy }: { points: EquityPoint[]; initial: number; ccy: string }) {
  if (points.length < 2) return <div className="empty" style={{ fontSize: 12 }}>至少 2 个周期后显示资产曲线</div>;
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
      <text x={42} y={y(initial) - 4} fontSize={9} fill="var(--muted)">初始 {fmtMoney(initial, ccy)}</text>
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

function RiskCheckMatrix({ riskResult }: { riskResult?: Record<string, any> }) {
  const checks = riskResult?.checks ?? [];
  if (!checks.length) {
    // Fallback: reconstruct from basic fields
    if (!riskResult) return <div style={{ fontSize: 12, color: "var(--muted)" }}>无风险检查记录</div>;
    return (
      <div style={{ fontSize: 12 }}>
        {riskResult.approved ? (
          <span style={{ color: "var(--good)" }}>✓ 风控通过 — {riskResult.reason ?? "All checks passed"}</span>
        ) : (
          <span style={{ color: "var(--bad)" }}>✕ 风控拒绝 — {riskResult.reason ?? ""}</span>
        )}
        {riskResult.adjusted && (
          <div style={{ marginTop: 6, fontSize: 13, color: "var(--warn)" }}>
            ⚡ 仓位已调整：{(riskResult.original_target_weight * 100).toFixed(1)}% → {(riskResult.adjusted_target_weight * 100).toFixed(1)}%
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
              {c.current} · 限制 {c.limit}
            </span>
            {c.detail && <div style={{ fontSize: 10, color: "var(--faint)" }}>{c.detail}</div>}
          </div>
        </div>
      ))}
      <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px solid var(--border)", fontSize: 13, fontWeight: 700, color: riskResult?.approved ? "var(--good)" : "var(--bad)" }}>
        {riskResult?.approved ? "✓ 风控通过" : `✕ 风控拒绝 — ${riskResult?.reason ?? ""}`}
        {riskResult?.adjusted && <span style={{ color: "var(--warn)", marginLeft: 8 }}>（仓位已调整）</span>}
      </div>
    </div>
  );
}

// ── decision story card ──

function DecisionCard({ dec, onOpen }: { dec: DecisionStory; onOpen: () => void }) {
  const p = dec.payload;
  const rp = dec.proposal_payload?.rationale ?? {};
  return (
    <div className="decision-card" style={{ borderLeftColor: p.action === "buy" ? "var(--good)" : "var(--bad)" }} onClick={onOpen}>
      <div className="decision-card-head">
        <div>
          <strong style={{ fontFamily: "var(--font-mono)", fontSize: 14 }}>{p.symbol || dec.symbol}</strong>
          <span className="micro-chip">
            {p.action?.toUpperCase()} · 权重 {((p.final_weight ?? 0) * 100).toFixed(1)}%
          </span>
          {(() => { const dm = dec.proposal_payload?.decision_mode; return dm ? (
            <span style={{ fontSize: 9, color: modeInfo(dm).color, fontWeight: 600, marginLeft: 6 }}>{modeInfo(dm).short}</span>
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

function ProposalDetailDrawer({ sessionId, proposalId, onClose }: { sessionId: string; proposalId: string; onClose: () => void }) {
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
    { key: "summary", label: "决策摘要" },
    { key: "agents", label: "Agent链路" },
    { key: "evidence", label: `证据（${rationale.supporting_evidence_ids?.length ?? 0}）` },
    { key: "risk", label: "风险检查" },
    { key: "sizing", label: "仓位计算" },
    { key: "execution", label: "执行记录" },
  ];

  return (
    <div className="drawer" data-testid="trade-detail" style={{ maxWidth: 680 }}>
      <button className="drawer-close" onClick={onClose}><X size={14} /> 关闭</button>
      <h2>{proposal.symbol ?? ""} {proposal.action ?? ""}
        {proposal.decision_mode && <span style={{ fontSize: 12, color: modeInfo(proposal.decision_mode).color, fontWeight: 700, marginLeft: 8, border: `1px solid ${modeInfo(proposal.decision_mode).color}`, borderRadius: 5, padding: "1px 8px" }}>{modeInfo(proposal.decision_mode).label}</span>}
      </h2>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--faint)", marginBottom: 8, wordBreak: "break-all" }}>{proposalId}</div>

      {error && <div className="error-box">{error}</div>}
      {!chain && !error && <div className="empty">加载中…</div>}

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
                  <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>TradingAgents 角色链路</h4>
                  <div style={{ fontSize: 12, color: "var(--muted)", display: "flex", gap: 12, flexWrap: "wrap" }}>
                    <span>框架 <b>{agentView.framework}</b></span>
                    <span>共识 <b>{agentView.debate?.consensus ?? "—"}</b></span>
                    <span>冲突 <b>{(agentView.debate?.conflict_level ?? 0).toFixed(2)}</b></span>
                    <span>交易员 <b>{agentView.trader_plan?.action_hint ?? "—"}</b></span>
                    <span>基金经理 <b>{agentView.fund_manager?.verdict ?? "—"}</b></span>
                  </div>
                </div>
              )}
              {rationale.quant_view && Object.keys(rationale.quant_view).length > 0 && (
                <div data-testid="proposal-quant-view" style={{ background: "var(--bg)", borderRadius: 6, padding: "8px 10px", borderLeft: "3px solid var(--accent)" }}>
                  <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>量化视角（模型预测，非事实）</h4>
                  <div style={{ fontSize: 12, color: "var(--muted)", display: "flex", gap: 14, flexWrap: "wrap" }}>
                    <span>横截面排名 <b>{rationale.quant_view.rank}/{rationale.quant_view.universe_size}</b></span>
                    <span>预计{rationale.quant_view.horizon_days ?? 5}日超额收益 <b>{((rationale.quant_view.predicted_return ?? 0) * 100).toFixed(2)}%</b></span>
                    {rationale.quant_view.probability_up != null && <span>上涨概率(校准) <b>{(rationale.quant_view.probability_up * 100).toFixed(1)}%</b></span>}
                    <span>模型 <b>{rationale.quant_view.model || rationale.quant_view.model_id}</b></span>
                  </div>
                  {rationale.quant_view.ensemble?.components && (
                    <div style={{ fontSize: 11, color: "var(--faint)", marginTop: 4 }}>
                      融合：quant {rationale.quant_view.ensemble.components.quant?.score} × research {rationale.quant_view.ensemble.components.research?.score}
                      {" → "}{rationale.quant_view.ensemble.action_hint}（综合 {rationale.quant_view.ensemble.combined_score}）
                    </div>
                  )}
                </div>
              )}
              {rationale.llm_view && Object.keys(rationale.llm_view).length > 0 && (
                <div data-testid="proposal-llm-view" style={{ background: "var(--bg)", borderRadius: 6, padding: "8px 10px", borderLeft: "3px solid var(--good)" }}>
                  <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>真LLM研究视角（证据驱动，非预测）</h4>
                  <div style={{ fontSize: 12, color: "var(--muted)", display: "flex", gap: 14, flexWrap: "wrap" }}>
                    <span>方向 <b>{rationale.llm_view.thesis_direction}</b></span>
                    <span>看多强度 <b>{(rationale.llm_view.bull_strength ?? 0).toFixed(2)}</b></span>
                    <span>看空强度 <b>{(rationale.llm_view.bear_strength ?? 0).toFixed(2)}</b></span>
                    <span>证据置信 <b>{(rationale.llm_view.thesis_confidence ?? 0).toFixed(2)}</b></span>
                    <span>引用支持率 <b>{((rationale.llm_view.citation_support ?? 0) * 100).toFixed(0)}%</b></span>
                  </div>
                  <div style={{ fontSize: 11, color: "var(--faint)", marginTop: 4 }}>
                    模型 <b>{rationale.llm_view.model || "—"}</b>
                    {" · "}investigation <b>{rationale.llm_view.investigation_id?.slice(0, 8)}</b>
                    {" · "}context <b>{rationale.llm_view.context_id?.slice(0, 8)}</b>
                    {" · "}PIT-safe <b>{rationale.llm_view.pit_safe ? "✓" : "✗"}</b>
                  </div>
                  {rationale.llm_view.fusion && (
                    <div style={{ fontSize: 11, color: "var(--faint)", marginTop: 4 }}>
                      融合决策：{rationale.llm_view.fusion.final_signal}
                      {" · "}冲突 <b>{rationale.llm_view.fusion.conflict_state}</b>
                      {" · "}模式 <b>{rationale.llm_view.fusion.effective_mode}</b>
                      {" · "}强度 <b>{(rationale.llm_view.fusion.final_strength ?? 0).toFixed(2)}</b>
                    </div>
                  )}
                </div>
              )}
              <div>
                <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>为什么是这只股票？</h4>
                <p style={{ color: "var(--muted)", margin: 0, fontSize: 13 }}>{rationale.why_this_instrument || "—"}</p>
              </div>
              <div>
                <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>为什么现在？</h4>
                <p style={{ color: "var(--muted)", margin: 0, fontSize: 13 }}>{(rationale.why_now ?? []).join("；") || rationale.signal_reason || "—"}</p>
              </div>
              {rationale.catalysts?.length > 0 && (
                <div>
                  <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>催化剂（{rationale.catalysts.length}）</h4>
                  {rationale.catalysts.map((c: any, i: number) => (
                    <div key={i} style={{ background: "var(--bg)", borderRadius: 6, padding: "8px 10px", marginBottom: 6 }}>
                      <div style={{ fontWeight: 600 }}>{c.title}</div>
                      <div style={{ fontSize: 12, color: "var(--muted)" }}>{c.description}</div>
                      {c.evidence_ids?.length > 0 && <div style={{ fontSize: 10, color: "var(--faint)", fontFamily: "var(--font-mono)", marginTop: 2 }}>证据：{c.evidence_ids.join(", ")}</div>}
                      <div style={{ fontSize: 10, color: "var(--faint)", marginTop: 2 }}>预期窗口：{c.expected_time_horizon ?? "—"} · 置信：{(c.confidence ?? 0).toFixed(2)}</div>
                    </div>
                  ))}
                </div>
              )}
              {rationale.counter_arguments?.length > 0 && (
                <div>
                  <h4 style={{ margin: "0 0 4px", fontSize: 13, color: "var(--bad)" }}>反方证据</h4>
                  <ul style={{ paddingLeft: 20, margin: 0, fontSize: 12 }}>
                    {rationale.counter_arguments.map((c: string, i: number) => <li key={i}>{c}</li>)}
                  </ul>
                </div>
              )}
              {rationale.risks?.length > 0 && (
                <div>
                  <h4 style={{ margin: "0 0 4px", fontSize: 13, color: "var(--warn)" }}>关键风险</h4>
                  <ul style={{ paddingLeft: 20, margin: 0, fontSize: 12 }}>
                    {rationale.risks.map((r: string, i: number) => <li key={i}>{r}</li>)}
                  </ul>
                </div>
              )}
              {rationale.invalidation_conditions?.length > 0 && (
                <div>
                  <h4 style={{ margin: "0 0 4px", fontSize: 13, color: "var(--bad)" }}>什么情况下会卖？</h4>
                  <ul style={{ paddingLeft: 20, margin: 0, fontSize: 12 }}>
                    {rationale.invalidation_conditions.map((ic: string, i: number) => <li key={i}>{ic}</li>)}
                  </ul>
                </div>
              )}
              {rationale.confidence_reason && (
                <div>
                  <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>置信度 {(proposal.confidence ?? 0).toFixed(2)}</h4>
                  {rationale.confidence_reason.positive?.length > 0 && (
                    <div style={{ fontSize: 12 }}>
                      <span style={{ color: "var(--good)" }}>正面因素：</span>
                      {rationale.confidence_reason.positive.join("；")}
                    </div>
                  )}
                  {rationale.confidence_reason.negative?.length > 0 && (
                    <div style={{ fontSize: 12 }}>
                      <span style={{ color: "var(--bad)" }}>负面因素：</span>
                      {rationale.confidence_reason.negative.join("；")}
                    </div>
                  )}
                </div>
              )}
              {rationale.decision_summary && (
                <div style={{ background: "var(--bg)", borderRadius: 6, padding: 10, fontSize: 13 }}>
                  <strong>AI 决策总结：</strong>{rationale.decision_summary}
                </div>
              )}
            </div>
          )}

          {tab === "agents" && (
            <div style={{ display: "grid", gap: 12, fontSize: 13 }}>
              {!agentView?.framework && <div className="empty">暂无 Agent 链路记录</div>}
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
                    <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>分析师团队</h4>
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
                            {a.risks?.length > 0 && <div style={{ fontSize: 11, color: "var(--warn)", marginTop: 3 }}>{a.risks.join("；")}</div>}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                  {agentView.debate && (
                    <div>
                      <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>多空研究辩论</h4>
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
                      <h4 style={{ margin: "0 0 4px", fontSize: 13 }}>交易员计划</h4>
                      <div style={{ color: "var(--muted)", fontSize: 12 }}>{agentView.trader_plan.thesis}</div>
                      <div style={{ marginTop: 4, fontSize: 12 }}>
                        动作 <b>{agentView.trader_plan.action_hint}</b>
                        {" · "}目标 {((agentView.trader_plan.target_weight_hint ?? 0) * 100).toFixed(2)}%
                        {" · "}置信 {(agentView.trader_plan.confidence ?? 0).toFixed(2)}
                      </div>
                    </div>
                  )}
                  <div>
                    <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>风控守卫与基金经理</h4>
                    {(agentView.risk_reviews ?? []).map((r: any) => (
                      <div key={r.name} style={{ fontSize: 12, borderBottom: "1px solid var(--border)", padding: "5px 0" }}>
                        <strong>{r.name}</strong>
                        {" · "}<span style={{ color: r.verdict === "reject" ? "var(--bad)" : r.verdict === "downsize" ? "var(--warn)" : "var(--good)" }}>{r.verdict}</span>
                        {" · "}{r.reason}
                      </div>
                    ))}
                    <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 6 }}>
                      基金经理：<b>{agentView.fund_manager?.verdict}</b> · {agentView.fund_manager?.reason}
                    </div>
                  </div>
                </>
              )}
            </div>
          )}

          {tab === "evidence" && (
            <div style={{ display: "grid", gap: 8 }}>
              {(!rationale.supporting_evidence_ids || rationale.supporting_evidence_ids.length === 0) ? (
                <div className="empty" style={{ fontSize: 12 }}>本提案为技术信号驱动，无可引用基本面证据。决策质量：<DecisionQualityBadge quality={proposal.decision_quality ?? "signal_only"} /></div>
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

          {tab === "risk" && <RiskCheckMatrix riskResult={risk} />}

          {tab === "sizing" && (
            <div style={{ display: "grid", gap: 10, fontSize: 13 }}>
              <div className="risk-matrix">
                {[{ label: "当前总资产", value: fmtMoney(sizing.equity, "¥") },
                  { label: "目标权重", value: `${((sizing.final_weight ?? proposal.target_weight ?? 0) * 100).toFixed(2)}%` },
                  { label: "当前价格", value: `${sizing.price ?? 0}` },
                  { label: "理论数量", value: `${(sizing.raw_quantity ?? 0).toFixed(1)} 股` },
                  { label: "实际数量", value: `${sizing.actual_quantity ?? 0} 股（最小单位 ${sizing.lot_size ?? 1} 股）` },
                  { label: "预计金额", value: fmtMoney(sizing.expected_amount, "¥") },
                  { label: "预估手续费", value: fmtMoney(sizing.commission_estimate, "¥") },
                ].map(({ label, value }, i) => (
                  <div key={i} style={{ display: "flex", justifyContent: "space-between", padding: "4px 0", borderBottom: "1px solid var(--border)" }}>
                    <span style={{ color: "var(--muted)" }}>{label}</span>
                    <strong style={{ fontFamily: "var(--font-mono)" }}>{String(value)}</strong>
                  </div>
                ))}
              </div>
              {rationale.target_weight_reason && (
                <div style={{ fontSize: 11, color: "var(--faint)", background: "var(--bg)", borderRadius: 4, padding: 8 }}>
                  计算：基准权重 {((rationale.target_weight_reason.base_weight ?? 0) * 100).toFixed(1)}%
                  × 置信因子 {(rationale.target_weight_reason.confidence_multiplier ?? 0).toFixed(2)}
                  = {(rationale.target_weight_reason.raw_weight ?? 0).toFixed(4)}
                  → 上限 {(rationale.target_weight_reason.cap ?? 0).toFixed(4)}
                  = 最终 {((rationale.target_weight_reason.final_weight ?? 0) * 100).toFixed(2)}%
                </div>
              )}
            </div>
          )}

          {tab === "execution" && (
            <div style={{ display: "grid", gap: 6 }}>
              {chain.map(e => {
                const labels: Record<string, [string, string]> = {
                  proposal: ["提案", "var(--warn)"], risk: ["风控", "var(--warn)"],
                  sizing: ["仓位计算", "var(--accent)"], order: ["下单", "var(--text)"],
                  fill: ["成交", "var(--good)"], position: ["持仓更新", "var(--muted)"],
                  decision: ["决策完成", "var(--good)"], thesis: ["论点", "var(--accent)"],
                  agent: ["Agent链路", "var(--accent)"],
                };
                const [label, color] = labels[e.event_type] ?? [e.event_type, "var(--muted)"];
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
        <summary style={{ cursor: "pointer", color: "var(--faint)" }}>技术详情（Raw JSON）</summary>
        <pre className="json-block" style={{ marginTop: 8, maxHeight: 200, overflow: "auto", fontSize: 10 }}>
          {JSON.stringify({ proposal, risk, sizing, fill }, null, 2)}
        </pre>
      </details>
    </div>
  );
}

// ── position intelligence drawer ──

function PositionDrawer({ pos, thesis, onClose }: { pos: PositionInfo; thesis: ThesisInfo | null; onClose: () => void }) {
  const thesisLabelMap: Record<string, [string, string]> = {
    active: ["有效", "var(--good)"], strengthened: ["增强", "var(--good)"], weakened: ["减弱", "var(--warn)"],
    challenged: ["受挑战", "var(--warn)"], invalidated: ["已失效", "var(--bad)"],
    proposed: ["提案", "var(--accent)"], closed: ["已关闭", "var(--muted)"],
  };
  const [tsLabel, tsColor] = thesisLabelMap[thesis?.status ?? ""] ?? [thesis?.status ?? "—", "var(--muted)"];
  const ret = pos.average_cost > 0 ? ((pos.market_price - pos.average_cost) / pos.average_cost * 100) : 0;

  return (
    <div className="drawer" style={{ maxWidth: 540 }}>
      <button className="drawer-close" onClick={onClose}><X size={14} /> 关闭</button>
      <h2 style={{ marginBottom: 4 }}><PositionName pos={pos} /></h2>
      <div style={{ fontSize: 13, color: "var(--muted)", marginBottom: 12 }}>
        {pos.market} · {pos.secid ? <code style={{ fontSize: 10 }}>{pos.secid}</code> : pos.symbol}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 16 }}>
        {[{ l: "持有数量", v: `${pos.quantity} 股` }, { l: "成本", v: fmtMoney(pos.average_cost) },
          { l: "现价", v: fmtMoney(pos.market_price) }, { l: "持仓价值", v: fmtMoney(pos.market_value) },
          { l: "收益率", v: `${ret >= 0 ? "+" : ""}${ret.toFixed(2)}%`, c: pctColor(ret) },
          { l: "浮动盈亏", v: `${(pos.unrealized_pnl ?? 0) >= 0 ? "+" : ""}${fmtMoney(pos.unrealized_pnl)}`, c: pctColor(pos.unrealized_pnl ?? 0) },
          { l: "仓位权重", v: `${(pos.weight * 100).toFixed(1)}%` }, { l: "持仓天数", v: pos.opened_at ? `${Math.floor((Date.now() - new Date(pos.opened_at).getTime()) / 86400000)}` : "—" },
        ].map(({ l, v, c }, i) => (
          <div key={i} className="panel" style={{ padding: "10px 12px" }}>
            <div style={{ fontSize: 10, color: "var(--muted)" }}>{l}</div>
            <div style={{ fontSize: 15, fontFamily: "var(--font-mono)", fontWeight: 700, color: c ?? "var(--text)" }}>{String(v)}</div>
          </div>
        ))}
      </div>

      {/* why hold */}
      <div className="panel" style={{ padding: 14, marginBottom: 12, borderLeft: "3px solid var(--good)" }}>
        <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>为什么继续持有？</h4>
        <p style={{ fontSize: 13, color: "var(--text)", margin: 0 }}>{pos.hold_rationale || thesis?.thesis_text || "—"}</p>
      </div>

      {/* thesis */}
      {thesis && (
        <div className="panel" style={{ padding: 14, marginBottom: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
            <h4 style={{ margin: 0, fontSize: 13 }}>投资论点</h4>
            <span style={{ color: tsColor, fontWeight: 700, fontSize: 12, border: `1px solid ${tsColor}`, borderRadius: 4, padding: "1px 6px" }}>{tsLabel}</span>
          </div>
          <p style={{ fontSize: 12, color: "var(--muted)", margin: 0 }}>{thesis.thesis_text}</p>
          {thesis.risks?.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <span style={{ fontSize: 11, color: "var(--warn)", fontWeight: 600 }}>风险：</span>
              {thesis.risks.map((r: string, i: number) => <span key={i} style={{ fontSize: 11, marginLeft: 6, background: "var(--bg)", borderRadius: 4, padding: "1px 5px" }}>{r}</span>)}
            </div>
          )}
          {thesis.invalidation_conditions?.length > 0 && (
            <div style={{ marginTop: 6 }}>
              <span style={{ fontSize: 11, color: "var(--bad)", fontWeight: 600 }}>失效条件：</span>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18, fontSize: 11 }}>{thesis.invalidation_conditions.map((ic: string, i: number) => <li key={i}>{ic}</li>)}</ul>
            </div>
          )}
        </div>
      )}

      {/* embedded TradeDetail for the opening proposal */}
      {thesis?.proposal_id && (
        <details style={{ fontSize: 10 }}>
          <summary style={{ cursor: "pointer", color: "var(--faint)" }}>查看完整交易记录（Proposal {thesis.proposal_id.slice(0, 8)}）</summary>
        </details>
      )}
    </div>
  );
}

// ── daily report drawer ──

function DailyReportDrawer({ report, onClose }: { report: any; onClose: () => void }) {
  const regimeLabels: Record<string, string> = {
    trending_up: "趋势上行", trending_down: "趋势下行", range_bound: "区间震荡",
    high_volatility: "高波动", risk_on: "风险偏好", risk_off: "风险回避", rotation: "风格轮动",
  };
  const dqLabels: Record<string, string> = {
    good_good: "正确·好结果", good_bad: "正确·坏结果", bad_good: "错误·好结果", bad_bad: "错误·坏结果",
  };
  const [tab, setTab] = React.useState<"market"|"account"|"trades"|"lessons"|"tomorrow">("market");
  const tabs = [
    { k: "market", l: "市场复盘" }, { k: "account", l: "账户表现" },
    { k: "trades", l: `交易复盘 (${report.trade_count ?? 0})` },
    { k: "lessons", l: `经验 (${report.lessons?.length ?? 0})` },
    { k: "tomorrow", l: "明日计划" },
  ] as const;

  return (
    <div className="drawer" style={{ maxWidth: 680 }}>
      <button className="drawer-close" onClick={onClose}><X size={14} /> 关闭</button>
      <h2>每日报告 · {report.date}</h2>
      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 8 }}>
        市场: {report.market} · regime: {regimeLabels[report.market_regime] ?? report.market_regime}
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
          <div><strong>市场环境:</strong> {regimeLabels[report.market_regime] ?? report.market_regime}</div>
          <div><strong>判断依据:</strong> {report.market_regime_reason || "—"}</div>
          {report.index_performance && Object.keys(report.index_performance).length > 0 && (
            <div>
              <strong>指数表现:</strong>
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
            <span>开盘资产</span><strong style={{ fontFamily: "var(--font-mono)" }}>{report.opening_equity?.toLocaleString()}</strong></div>
          <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0", borderBottom: "1px solid var(--border)" }}>
            <span>收盘资产</span><strong style={{ fontFamily: "var(--font-mono)" }}>{report.closing_equity?.toLocaleString()}</strong></div>
          <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0", borderBottom: "1px solid var(--border)" }}>
            <span>日内盈亏</span><strong style={{ fontFamily: "var(--font-mono)", color: pctColor(report.daily_pnl ?? 0) }}>{(report.daily_pnl ?? 0) >= 0 ? "+" : ""}{report.daily_pnl?.toFixed(2)}</strong></div>
          <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0", borderBottom: "1px solid var(--border)" }}>
            <span>收益率</span><strong style={{ fontFamily: "var(--font-mono)", color: pctColor(report.daily_return ?? 0) }}>{((report.daily_return ?? 0) * 100).toFixed(2)}%</strong></div>
          <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0" }}>
            <span>最大回撤</span><strong style={{ fontFamily: "var(--font-mono)", color: "var(--bad)" }}>{((report.max_intraday_drawdown ?? 0) * 100).toFixed(2)}%</strong></div>
          {report.benchmark_return !== undefined && (<div style={{ fontSize: 11, color: "var(--muted)", marginTop: 4 }}>基准: {((report.benchmark_return ?? 0) * 100).toFixed(2)}% · 超额: {((report.excess_return ?? 0) * 100).toFixed(2)}%</div>)}
        </div>
      )}

      {tab === "trades" && (
        <div style={{ display: "grid", gap: 8, fontSize: 13 }}>
          <div style={{ display: "flex", gap: 16, marginBottom: 8 }}>
            <span style={{ color: "var(--good)" }}>✓ 成功 {report.successful_reviews?.length ?? 0}</span>
            <span style={{ color: "var(--bad)" }}>✕ 失败 {report.failed_reviews?.length ?? 0}</span>
            <span style={{ color: "var(--warn)" }}>错过 {report.missed_opportunities?.length ?? 0}</span>
          </div>
          {[...(report.successful_reviews ?? []), ...(report.failed_reviews ?? [])].slice(0, 10).map((tr: any, i: number) => (
            <div key={i} style={{ borderBottom: "1px solid var(--border)", padding: "6px 0", fontSize: 12, borderLeft: `3px solid ${tr.pnl_contribution >= 0 ? "var(--good)" : "var(--bad)"}`, paddingLeft: 8 }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <strong style={{ fontFamily: "var(--font-mono)" }}>{tr.name || tr.symbol} {tr.action}</strong>
                <span style={{ fontFamily: "var(--font-mono)", color: pctColor(tr.pnl_contribution ?? 0) }}>{(tr.pnl_contribution ?? 0) >= 0 ? "+" : ""}{tr.pnl_contribution?.toFixed(2)}</span>
              </div>
              <div style={{ fontSize: 11, color: "var(--muted)" }}>{(tr.thesis ?? "").slice(0, 80)}</div>
              <div style={{ fontSize: 10, color: "var(--faint)" }}>{dqLabels[tr.decision_quality] ?? tr.decision_quality} · {tr.quality_reason}</div>
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
          {(report.lessons ?? []).length === 0 && <div className="empty">今日无新经验</div>}
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
          <div><strong>明日计划:</strong>
            <div style={{ whiteSpace: "pre-wrap", color: "var(--muted)", marginTop: 4 }}>{report.tomorrow_plan || "—"}</div>
          </div>
          {report.tomorrow_watchlist?.length > 0 && (
            <div><strong>重点关注 ({report.tomorrow_watchlist.length}):</strong>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 4 }}>{report.tomorrow_watchlist.map((s: string) => <code key={s} style={{ fontSize: 11 }}>{s}</code>)}</div>
            </div>
          )}
          {report.tomorrow_pending_actions?.length > 0 && (
            <div><strong>待验证操作 ({report.tomorrow_pending_actions.length}):</strong>
              {report.tomorrow_pending_actions.map((pa: any, i: number) => (
                <div key={i} style={{ fontSize: 11, padding: "4px 8px", background: "var(--bg)", borderRadius: 4, marginTop: 4 }}>
                  {pa.symbol}: {pa.action} — {pa.reason}
                </div>
              ))}
            </div>
          )}
          {report.tomorrow_unknowns?.length > 0 && (
            <div><strong>不确定事项:</strong>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{(report.tomorrow_unknowns ?? []).map((u: string, i: number) => <li key={i} style={{ fontSize: 12, color: "var(--warn)" }}>{u}</li>)}</ul>
            </div>
          )}
        </div>
      )}

      <details style={{ marginTop: 14, fontSize: 10 }}>
        <summary style={{ cursor: "pointer", color: "var(--faint)" }}>原始 JSON</summary>
        <pre className="json-block" style={{ maxHeight: 200, overflow: "auto", fontSize: 9 }}>{JSON.stringify(report, null, 2)}</pre>
      </details>
    </div>
  );
}

// ── main dashboard ──

function SessionDashboard({ session: initial, onExit }: { session: TradingSessionInfo; onExit: () => void }) {
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
      setLastCycleAgo(seconds <= 5 ? "刚刚" : `${seconds}秒前`);
    }
  }, [session.cycle_count, session.last_cycle_at]);
  // tick staleness every 2s even when cycle_count hasn't changed
  React.useEffect(() => {
    const t = setInterval(() => {
      if (session.last_cycle_at) {
        const s = Math.round((Date.now() - new Date(session.last_cycle_at).getTime()) / 1000);
        setLastCycleAgo(s <= 5 ? "刚刚" : `${s}秒前`);
      }
    }, 2000);
    return () => clearInterval(t);
  }, [session.last_cycle_at]);

  const STATUS_LABEL: Record<string, [string, string]> = {
    created: ["未启动", "var(--muted)"], starting: ["启动中", "var(--warn)"],
    running: ["运行中", "var(--good)"], paused: ["已暂停", "var(--warn)"],
    halted: ["紧急停止", "var(--bad)"], stopped: ["已停止", "var(--muted)"],
  };
  const [snapshot, setSnapshot] = React.useState<any>(null);
  React.useEffect(() => { api.tradingGlobalSnapshot().then(setSnapshot).catch(() => {}); }, [session.session_id]);
  React.useEffect(() => {
    const t = setInterval(() => { api.tradingGlobalSnapshot().then(setSnapshot).catch(() => {}); }, 30000);
    return () => clearInterval(t);
  }, [session.session_id]);

  const [statusLabel, statusColor] = STATUS_LABEL[session.status] ?? [session.status, "var(--muted)"];
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
            <h2>AI 投资组合管理器</h2>
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
            SIMULATION · 周期 #{session.cycle_count} · {session.allowed_markets.join("+")} · {session.risk_profile} · 扫描 {session.scan_interval}s
            {truth && <span style={{ color: modeInfo(truth.effective_mode).color, marginLeft: 6 }}>· {modeInfo(truth.effective_mode).short}</span>}
            {session.status === "running" && lastCycleAgo && (
              <span style={{ color: parseInt(lastCycleAgo) > 30 ? "var(--warn)" : "var(--muted)" }}>
                {" · "}上次周期 {lastCycleAgo}
              </span>
            )}
            {session.status === "paused" && lastCycleAgo && (
              <span style={{ color: "var(--warn)" }}>{" · "}已暂停（最后周期 {lastCycleAgo}）</span>
            )}
            {session.next_cycle_at && session.status === "running" && <> · 下次 {new Date(session.next_cycle_at).toLocaleTimeString()}</>}
          </div>
        </div>
        <div className="command-actions">
          {(session.status === "created" || session.status === "stopped") && (
            <button className="primary" data-testid="start-trading" onClick={() => act("start")} disabled={!!busy}><Play size={14} /> 启动</button>
          )}
          {session.status === "running" && <button onClick={() => act("pause")} disabled={!!busy}>{busy === "pause" ? "暂停中…" : <><Pause size={14} /> 暂停</>}</button>}
          {session.status === "paused" && <button className="primary" onClick={() => act("resume")} disabled={!!busy}>{busy === "resume" ? "恢复中…" : <><Play size={14} /> 继续</>}</button>}
          {(session.status === "running" || session.status === "paused") && <button onClick={() => act("stop")} disabled={!!busy}>{busy === "stop" ? "停止中…" : <><Square size={14} /> 停止</>}</button>}
          {(session.status === "created" || session.status === "stopped" || session.status === "paused") && (
            <button data-testid="run-cycle" onClick={() => act("run-cycle")} disabled={!!busy}>{busy === "run-cycle" ? "执行中…" : "单轮执行"}</button>
          )}
          {session.status !== "halted" && (
            <button data-testid="emergency-stop" style={{ borderColor: "var(--bad)", color: "var(--bad)" }}
              onClick={() => { if (confirm("紧急停止将阻断所有新提案与订单。确认？")) act("emergency-stop"); }}
              disabled={!!busy}><AlertTriangle size={14} /> 紧急停止</button>
          )}
          <button onClick={onExit} style={{ fontSize: 12 }}>会话列表</button>
        </div>
      </div>

      {session.status === "halted" && <div className="error-box">⚠ 会话已紧急停止：调度器与 Kill Switch 均已激活。</div>}

      {/* ── System Truth Bar: which brains are actually participating ── */}
      <SystemTruthBar truth={truth} />

      {/* ── global market clocks + agent operational state ── */}
      <div className="market-ops-row">
        <div className="market-clock-strip">
          <Globe size={16} style={{ color: "var(--accent)" }} />
          <span style={{ fontSize: 12, fontWeight: 700 }}>全球市场</span>
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
                {mkt === "CN" ? "A股" : mkt === "US" ? "美股" : mkt === "HK" ? "港股" : mkt}
                {" "}{localT}
              </span>
            );
          })}
          {!snapshot?.markets && <span style={{ fontSize: 11, color: "var(--faint)" }}>获取中...</span>}
        </div>
        <div className="agent-phase-strip">
          <Eye size={14} style={{ color: opState ? "var(--good)" : "var(--muted)" }} />
          <span style={{ fontWeight: 600, color: "var(--accent)" }}>{opState?.phase_label_zh ?? "—"}</span>
          {opState?.current_activity && <span style={{ color: "var(--muted)", fontSize: 11 }}>{opState.current_activity}</span>}
          {opState?.thesis_updates_since_close !== undefined && opState.thesis_updates_since_close > 0 && (
            <span style={{ fontSize: 11, color: "var(--warn)" }}>Thesis 更新 {opState.thesis_updates_since_close}</span>
          )}
        </div>
      </div>

      {/* ── metrics ── */}
      <div className="trade-kpi-grid">
        {[
          ["初始资金", fmtMoney(session.initial_capital, ccy)], ["当前总资产", fmtMoney(session.current_equity, ccy), pctColor(totalPnl)],
          ["现金", fmtMoney(session.cash, ccy)], ["持仓市值", fmtMoney(marketValue, ccy)],
          ["总盈亏", `${totalPnl >= 0 ? "+" : ""}${fmtMoney(totalPnl, ccy)}`, pctColor(totalPnl)], ["总收益率", `${totalReturn >= 0 ? "+" : ""}${totalReturn.toFixed(2)}%`, pctColor(totalReturn)],
          ["最大回撤", `${maxDd.toFixed(2)}%`, maxDd < -3 ? "var(--bad)" : "var(--muted)"], ["持仓数", `${positions.length}`, "var(--muted)"],
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
              <h2><BarChart3 size={16} style={{ verticalAlign: -2 }} /> 资产曲线</h2>
              <p>每周期记录 · {curve.length} 点</p>
            </div>
            <EquityCurve points={curve} initial={session.initial_capital} ccy={ccy} />
          </div>

          {/* agent current activity — real stage from system truth */}
          <div className="desk-panel" data-testid="agent-activity">
            <div className="section-title"><h2><Cpu size={14} style={{ verticalAlign: -2 }} /> Agent 当前活动</h2></div>
            {(() => {
              const stageLabels: Record<string, string> = {
                idle: "空闲", scan: "扫描市场", quant_inference: "量化推理",
                position_review: "持仓复核", candidate_research: "候选研究",
              };
              const stage = truth?.activity?.stage ?? "idle";
              const mi = modeInfo(truth?.effective_mode);
              const rows = [
                { l: "当前阶段", v: session.status === "running" ? (stageLabels[stage] ?? stage) : (STATUS_LABEL[session.status]?.[0] ?? session.status) },
                { l: "正在研究", v: truth?.activity?.target || "—" },
                { l: "本轮周期", v: `#${session.cycle_count}` },
                { l: "自选股", v: `${events.find(e => e.event_type === "scan")?.payload?.universe_size ?? "—"} 只` },
                { l: "决策模式", v: mi.label, c: mi.color },
                { l: "上次决策", v: decisions[0] ? `${decisions[0].payload?.action?.toUpperCase()} ${decisions[0].symbol}` : "—" },
                { l: "风控拒绝", v: `${session.risk_rejections} 次` },
                { l: "下次扫描", v: session.next_cycle_at && session.status === "running" ? new Date(session.next_cycle_at).toLocaleTimeString() : "—" },
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
            <div className="section-title"><h2><Target size={14} style={{ verticalAlign: -2 }} /> 持仓 ({positions.length})</h2></div>
            {!positions.length ? <div className="empty">暂无持仓</div> : (
              <div className="data-table">
                <div className="row" style={{ gridTemplateColumns: "80px minmax(70px,1fr) 50px 50px 50px 70px 50px 50px", fontSize: 10, color: "var(--muted)" }}>
                  <span>代码</span><span>名称</span><span>数量</span><span>成本</span><span>现价</span><span>浮动盈亏</span><span>权重</span><span>论点</span>
                </div>
                {positions.map(p => {
                  const t = theses.find(x => x.thesis_id === p.active_thesis_id);
                  const tLabel = t?.status === "active" ? "有效" : t?.status === "weakened" ? "减弱" : t?.status ?? "…";
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
            <div className="section-title"><h2><FileSearch size={14} style={{ verticalAlign: -2 }} /> 决策故事</h2><p>按决策聚合，非原始事件流</p></div>
            <div style={{ maxHeight: 500, overflowY: "auto", display: "grid", gap: 8 }}>
              {!decisions.length && <div className="empty" style={{ fontSize: 12 }}>启动后每次交易完成后显示决策故事</div>}
              {decisions.map(d => (
                <DecisionCard key={d.event_id} dec={d} onOpen={() => {
                  const pid = d.payload?.proposal_id;
                  if (pid) setDetailPid(pid);
                }} />
              ))}
            </div>
          </div>

          {/* proposals — first-class rows with LIVE status (executed ≠ pending) */}
          <div className="desk-panel" data-testid="proposals-panel">
            <div className="section-title"><h2>提案</h2><p>状态实时同步 · 每标的去重</p></div>
            {proposals.slice(0, 8).map(p => {
              const [stLabel, stColor] = PROPOSAL_STATUS[p.status] ?? [p.status, "var(--muted)"];
              const mi = modeInfo(p.decision_mode);
              return (
                <div key={p.proposal_id} style={{ borderBottom: "1px solid var(--border)", padding: "6px 0", fontSize: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <strong style={{ fontFamily: "var(--font-mono)" }}>{p.symbol} {p.action?.toUpperCase()}</strong>
                    <span data-testid={`proposal-status-${p.symbol}`} style={{ color: stColor, fontWeight: 700 }}>{stLabel}</span>
                  </div>
                  <div style={{ color: "var(--muted)", fontFamily: "var(--font-mono)", fontSize: 11, display: "flex", gap: 8, flexWrap: "wrap" }}>
                    <span>目标 {(p.target_weight * 100).toFixed(1)}%</span>
                    <span>置信 {Number(p.confidence ?? 0).toFixed(2)}</span>
                    {p.cycle != null && <span>#{p.cycle}</span>}
                    <span style={{ color: mi.color }}>{mi.short}</span>
                  </div>
                  <button style={{ fontSize: 10, padding: "1px 6px", marginTop: 2 }} onClick={() => setDetailPid(p.proposal_id)}>完整链路</button>
                </div>
              );
            })}
            {!proposals.length && <div className="empty" style={{ fontSize: 12 }}>暂无提案</div>}
          </div>

          {/* daily reports */}
          <div className="desk-panel">
            <div className="section-title"><h2>每日报告</h2><p>收盘后自动生成</p></div>
            {!dailyReports.length && <div className="empty" style={{ fontSize: 12 }}>暂无报告——每日收盘后自动生成</div>}
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
                  {r.market_regime === "trending_up" ? "趋势上行" : r.market_regime === "trending_down" ? "趋势下行" :
                   r.market_regime === "range_bound" ? "区间震荡" : r.market_regime === "high_volatility" ? "高波动" :
                   r.market_regime === "risk_on" ? "风险偏好" : r.market_regime === "risk_off" ? "风险回避" : r.market_regime ?? "—"}
                  {" · "}{r.trade_count ?? 0} 笔交易
                  {r.session_id && <span style={{ marginLeft: 8, fontSize: 10, color: "var(--faint)", fontFamily: "var(--font-mono)" }}>{r.session_id.slice(0, 6)}</span>}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ── quant intelligence (Phase Q) ── */}
      <div className="quant-band">
        <QuantIntelligencePanel sessionId={session.session_id} lang="zh" />
      </div>

      {/* ── debug tool, collapsed by default ── */}
      <div className="desk-panel debug-panel">
        <button style={{ fontSize: 11 }} onClick={() => setShowDebug(v => !v)}>
          {showDebug ? "隐藏" : "显示"}开发者调试工具（手工提案——非产品路径）
        </button>
        {showDebug && <DebugProposalForm onDone={refresh} />}
      </div>

      {detailPid && <ProposalDetailDrawer sessionId={session.session_id} proposalId={detailPid} onClose={() => setDetailPid(null)} />}
      {posDrawer && <PositionDrawer pos={posDrawer} thesis={theses.find(t => t.thesis_id === posDrawer.active_thesis_id) ?? null} onClose={() => setPosDrawer(null)} />}
      {reportDrawer && <DailyReportDrawer report={reportDrawer} onClose={() => setReportDrawer(null)} />}
    </div>
  );
}

function DebugProposalForm({ onDone }: { onDone: () => void }) {
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
        ⚠ 仅用于测试风控/撮合。成交价强制取自实时行情网关，不可自填价格；受风控与 kill switch 约束。
      </div>
      <div className="investigate-form" style={{ gridTemplateColumns: "110px 90px 1fr auto" }}>
        <label>股票<input value={sym} onChange={e => setSym(e.target.value.toUpperCase())} /></label>
        <label>权重%<input type="number" value={weight} onChange={e => setWeight(Number(e.target.value))} /></label>
        <label>说明<input value={thesis} onChange={e => setThesis(e.target.value)} /></label>
        <button onClick={submit}>提交调试提案</button>
      </div>
      {msg && <pre className="json-block" style={{ fontSize: 11, marginTop: 8 }}>{msg}</pre>}
    </div>
  );
}

// ── page root ──

export function TradingPage({ lang }: { lang: string }) {
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

  if (active) return <SessionDashboard session={active} onExit={() => { setActive(null); load(); }} />;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <SessionConfig onCreated={s => setActive(s)} />
      <div className="panel wide">
        <div className="section-title"><h2>历史会话</h2></div>
        {sessions === null && <div className="empty">加载中…</div>}
        {sessions !== null && !sessions.length && <div className="empty">暂无会话</div>}
        {(sessions ?? []).map(s => {
          const STATUS_LABEL: Record<string, [string, string]> = {
            created: ["未启动", "var(--muted)"], running: ["运行中", "var(--good)"],
            paused: ["已暂停", "var(--warn)"], halted: ["紧急停止", "var(--bad)"],
            stopped: ["已停止", "var(--muted)"],
          };
          const [label, color] = STATUS_LABEL[s.status] ?? [s.status, "var(--muted)"];
          const pnl = s.current_equity - s.initial_capital;
          return (
            <button key={s.session_id} className="row clickable" onClick={() => setActive(s)}
              style={{ display: "grid", gridTemplateColumns: "100px 80px 100px 100px 100px 1fr", width: "100%", alignItems: "center" }}>
              <code style={{ fontSize: 11 }}>{s.session_id.slice(0, 8)}</code>
              <span style={{ color, fontSize: 12 }}>{label}</span>
              <span style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{fmtMoney(s.initial_capital)}</span>
              <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: pctColor(pnl) }}>{pnl >= 0 ? "+" : ""}{fmtMoney(pnl)}</span>
              <span style={{ fontSize: 11, color: "var(--muted)" }}>{s.allowed_markets.join("+")} · {s.risk_profile}</span>
              <span style={{ fontSize: 11, color: "var(--faint)" }}>周期 {s.cycle_count} · {new Date(s.created_at).toLocaleString()}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
