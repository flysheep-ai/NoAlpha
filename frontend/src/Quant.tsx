import React from "react";
import { request } from "./api";

/** Phase Q: Quant Intelligence dashboard.
 * Everything rendered here is real registry/experiment output; predictions
 * are always labeled as predictions (预测), never as facts. */

interface ActiveModel {
  model_id: string; name: string; version: number; market_scope: string;
  horizon_days: number; label_id: string; promoted_at: string;
}
interface Overview {
  models_total: number; active_models: ActiveModel[]; experiments_total: number;
  feature_count: number; factor_count: number;
}
interface ModelRecord {
  model_id: string; name: string; version: number; status: string;
  market_scope: string; horizon_days: number; label_id: string;
  created_at: string; metrics?: any; calibration?: any; status_history: any[];
}
interface ExperimentRow {
  experiment_id: string; name: string; model: string; status: string;
  n_folds?: number; seed?: number; test_period?: string[];
  signal?: { ic: number; rank_ic: number; icir: number; rank_icir: number; n_days: number };
  trading?: { cumulative_net_return: number; sharpe: number; max_drawdown: number; avg_turnover: number };
  equal_weight_benchmark?: { cumulative_net_return: number; sharpe: number };
  survivorship_bias_risk?: boolean;
  created_at: string;
}
interface FeatureMeta {
  feature_id: string; category: string; description: string; formula: string; lookback: number;
}
interface MarketResults {
  market: string;
  factor_ic: Record<string, { ic: number; rank_ic: number; icir: number; rank_icir: number; n_days: number }>;
  hybrid_comparison: Record<string, { signal: any; trading: any }>;
  experiment_count: number; multiple_testing_note: string;
  dataset: { n_rows: number; universe: string[]; start_date: string; end_date: string; survivorship_bias_risk: boolean };
}

const pct = (v: number | undefined | null, digits = 2) =>
  v == null || Number.isNaN(v) ? "—" : `${(v * 100).toFixed(digits)}%`;
const num = (v: number | undefined | null, digits = 3) =>
  v == null || Number.isNaN(v) ? "—" : v.toFixed(digits);
const signColor = (v: number | undefined | null) =>
  v == null ? undefined : v > 0 ? "var(--good)" : v < 0 ? "var(--bad)" : undefined;

function useFetch<T>(fn: () => Promise<T>, deps: any[] = []): { data: T | null; error: string } {
  const [data, setData] = React.useState<T | null>(null);
  const [error, setError] = React.useState("");
  React.useEffect(() => {
    let alive = true;
    fn().then((d) => alive && setData(d)).catch((e) => alive && setError(String(e?.message ?? e)));
    return () => { alive = false; };
  }, deps);
  return { data, error };
}

export function QuantPage({ lang }: { lang: string }) {
  const zh = lang.startsWith("zh");
  const [market, setMarket] = React.useState<"cn" | "us">("cn");
  const overview = useFetch<Overview>(() => request("/api/quant/overview"), []);
  const models = useFetch<{ models: ModelRecord[] }>(() => request("/api/quant/models"), []);
  const experiments = useFetch<{ experiments: ExperimentRow[] }>(() => request("/api/quant/experiments"), []);
  const features = useFetch<{ features: FeatureMeta[]; factors: any[]; labels: any[] }>(
    () => request("/api/quant/features"), []);
  const results = useFetch<MarketResults>(() => request(`/api/quant/results/${market}`), [market]);

  return (
    <div className="page" data-testid="quant-page">
      <div className="panel wide" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 10 }}>
        <div>
          <h2 style={{ margin: 0 }}>{zh ? "量化智能层" : "Quant Intelligence"}</h2>
          <div style={{ fontSize: 12, opacity: 0.7 }}>
            {zh ? "所有数值为模型预测与历史回验结果，非事实陈述；宇宙为当前成分（存在幸存者偏差风险）。"
                : "All values are model predictions / walk-forward results, not facts. Universe is today's list (survivorship bias risk)."}
          </div>
        </div>
        {overview.data && (
          <div style={{ display: "flex", gap: 18, fontSize: 13 }}>
            <Stat label={zh ? "模型" : "Models"} value={overview.data.models_total} />
            <Stat label={zh ? "实验" : "Experiments"} value={overview.data.experiments_total} />
            <Stat label={zh ? "特征" : "Features"} value={overview.data.feature_count} />
            <Stat label={zh ? "因子" : "Factors"} value={overview.data.factor_count} />
          </div>
        )}
      </div>

      {/* active models */}
      <div className="panel wide" data-testid="quant-active-models">
        <div className="section-title">{zh ? "生效模型（驱动模拟交易）" : "Active models (feeding the simulated loop)"}</div>
        {overview.data?.active_models?.length ? (
          <table className="data-table" style={{ width: "100%", fontSize: 13 }}>
            <thead><tr>
              <th>{zh ? "模型" : "Model"}</th><th>{zh ? "市场" : "Market"}</th>
              <th>{zh ? "预测目标" : "Label"}</th><th>{zh ? "周期" : "Horizon"}</th>
              <th>{zh ? "激活时间" : "Promoted"}</th>
            </tr></thead>
            <tbody>
              {overview.data.active_models.map((m) => (
                <tr key={m.model_id}>
                  <td><b>{m.name} v{m.version}</b> <span style={{ opacity: 0.6, fontSize: 11 }}>{m.model_id}</span></td>
                  <td>{m.market_scope}</td><td>{m.label_id}</td>
                  <td>{m.horizon_days}D</td><td>{m.promoted_at?.slice(0, 10)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <div style={{ opacity: 0.7, fontSize: 13 }}>{zh ? "暂无 ACTIVE 模型" : "No ACTIVE model"}</div>}
        {models.data && (
          <div style={{ fontSize: 12, opacity: 0.75, marginTop: 8 }}>
            {zh ? "全部注册模型：" : "All registered: "}
            {models.data.models.map((m) => `${m.name} v${m.version} (${m.market_scope}, ${m.status})`).join(" · ")}
          </div>
        )}
      </div>

      {/* walk-forward experiments */}
      <div className="panel wide" data-testid="quant-experiments">
        <div className="section-title">{zh ? "Walk-Forward 实验（真实历史数据，净成本后）" : "Walk-forward experiments (real history, net of cost)"}</div>
        {experiments.data?.experiments?.length ? (
          <table className="data-table" style={{ width: "100%", fontSize: 12 }}>
            <thead><tr>
              <th>{zh ? "实验" : "Experiment"}</th><th>{zh ? "折数" : "Folds"}</th>
              <th>IC</th><th>Rank IC</th><th>Rank ICIR</th>
              <th>{zh ? "净收益" : "Net"}</th><th>Sharpe</th><th>MDD</th><th>{zh ? "换手" : "Turnover"}</th>
              <th>{zh ? "等权基准" : "EW bench"}</th>
            </tr></thead>
            <tbody>
              {experiments.data.experiments.map((e) => (
                <tr key={e.experiment_id}>
                  <td>{e.name}</td><td>{e.n_folds ?? "—"}</td>
                  <td style={{ color: signColor(e.signal?.ic) }}>{num(e.signal?.ic, 4)}</td>
                  <td style={{ color: signColor(e.signal?.rank_ic) }}>{num(e.signal?.rank_ic, 4)}</td>
                  <td>{num(e.signal?.rank_icir)}</td>
                  <td style={{ color: signColor(e.trading?.cumulative_net_return) }}>{pct(e.trading?.cumulative_net_return)}</td>
                  <td>{num(e.trading?.sharpe, 2)}</td>
                  <td>{pct(e.trading?.max_drawdown)}</td>
                  <td>{num(e.trading?.avg_turnover, 2)}</td>
                  <td>{pct(e.equal_weight_benchmark?.cumulative_net_return)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <div style={{ opacity: 0.7, fontSize: 13 }}>{experiments.error || (zh ? "暂无实验" : "No experiments")}</div>}
      </div>

      {/* factor explorer + hybrid comparison per market */}
      <div className="panel wide" data-testid="quant-factors">
        <div className="section-title" style={{ display: "flex", justifyContent: "space-between" }}>
          <span>{zh ? "因子表现（全样本 IC，无拟合）" : "Factor performance (full-sample IC, no fitting)"}</span>
          <span>
            <button className={`btn ${market === "cn" ? "primary" : ""}`} onClick={() => setMarket("cn")}>{zh ? "A股" : "A-shares"}</button>{" "}
            <button className={`btn ${market === "us" ? "primary" : ""}`} onClick={() => setMarket("us")}>US</button>
          </span>
        </div>
        {results.data ? (
          <>
            <table className="data-table" style={{ width: "100%", fontSize: 12 }}>
              <thead><tr><th>{zh ? "特征 / 因子" : "Feature / Factor"}</th><th>IC</th><th>Rank IC</th><th>Rank ICIR</th><th>{zh ? "样本天数" : "Days"}</th></tr></thead>
              <tbody>
                {Object.entries(results.data.factor_ic)
                  .sort((a, b) => Math.abs(b[1].rank_ic) - Math.abs(a[1].rank_ic))
                  .map(([k, v]) => (
                    <tr key={k}>
                      <td style={{ fontWeight: k.includes("Factor") ? 600 : 400 }}>{k}</td>
                      <td style={{ color: signColor(v.ic) }}>{num(v.ic, 4)}</td>
                      <td style={{ color: signColor(v.rank_ic) }}>{num(v.rank_ic, 4)}</td>
                      <td>{num(v.rank_icir)}</td>
                      <td>{v.n_days}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
            <div style={{ fontSize: 11, opacity: 0.65, marginTop: 6 }}>
              {zh ? "注意：feature importance / IC ≠ 因果关系；小 universe 下单因子 IC 噪声很大。"
                  : "Note: importance/IC is not causality; single-factor IC is noisy on a small universe."}
            </div>
          </>
        ) : <div style={{ opacity: 0.7, fontSize: 13 }}>{results.error || "…"}</div>}
      </div>

      {results.data?.hybrid_comparison && (
        <div className="panel wide" data-testid="quant-hybrid">
          <div className="section-title">{zh ? "Quant / Research / Hybrid 同期对比（诚实呈现，含失败者）" : "Quant vs Research vs Hybrid (honest, losers included)"}</div>
          <table className="data-table" style={{ width: "100%", fontSize: 12 }}>
            <thead><tr><th>{zh ? "策略" : "Strategy"}</th><th>Rank IC</th><th>{zh ? "净收益" : "Net"}</th><th>Sharpe</th><th>{zh ? "换手" : "Turnover"}</th></tr></thead>
            <tbody>
              {Object.entries(results.data.hybrid_comparison).map(([k, v]) => (
                <tr key={k}>
                  <td>{k}</td>
                  <td style={{ color: signColor(v.signal?.rank_ic) }}>{num(v.signal?.rank_ic, 4)}</td>
                  <td style={{ color: signColor(v.trading?.cumulative_net_return) }}>{pct(v.trading?.cumulative_net_return)}</td>
                  <td>{num(v.trading?.sharpe, 2)}</td>
                  <td>{num(v.trading?.avg_turnover, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ fontSize: 11, opacity: 0.65, marginTop: 6 }}>
            {results.data.multiple_testing_note} · {zh ? "实验总数" : "experiment count"}: {results.data.experiment_count}
          </div>
        </div>
      )}

      {/* feature registry */}
      <div className="panel wide" data-testid="quant-features">
        <div className="section-title">{zh ? "特征注册表（版本化）" : "Feature registry (versioned)"}</div>
        {features.data && (
          <table className="data-table" style={{ width: "100%", fontSize: 12 }}>
            <thead><tr><th>ID</th><th>{zh ? "类别" : "Category"}</th><th>{zh ? "公式" : "Formula"}</th><th>{zh ? "回看" : "Lookback"}</th></tr></thead>
            <tbody>
              {features.data.features.map((f) => (
                <tr key={f.feature_id}>
                  <td>{f.feature_id}</td><td>{f.category}</td>
                  <td style={{ fontFamily: "monospace", fontSize: 11 }}>{f.formula}</td>
                  <td>{f.lookback}d</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {features.data?.labels && (
          <div style={{ fontSize: 12, opacity: 0.75, marginTop: 8 }}>
            {zh ? "决策时点协议：" : "Decision-time protocol: "}
            {features.data.labels.map((l: any) => `${l.label_id}（${l.decision_time} → ${l.execution_time}）`).join(" · ")}
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{ textAlign: "center" }}>
      <div style={{ fontSize: 18, fontWeight: 700 }}>{value}</div>
      <div style={{ fontSize: 11, opacity: 0.65 }}>{label}</div>
    </div>
  );
}

/** Compact panel embedded in the Trading page. */
export function QuantIntelligencePanel({ sessionId, lang }: { sessionId: string; lang: string }) {
  const zh = lang.startsWith("zh");
  const [data, setData] = React.useState<any>(null);
  React.useEffect(() => {
    let alive = true;
    const load = () =>
      request(`/api/quant/predictions/${sessionId}`).then((d) => alive && setData(d)).catch(() => {});
    load();
    const t = setInterval(load, 15000);
    return () => { alive = false; clearInterval(t); };
  }, [sessionId]);
  if (!data) return null;
  return (
    <div className="panel wide" data-testid="quant-panel">
      <div className="section-title">{zh ? "量化智能面板" : "Quant Intelligence"}</div>
      {data.error ? (
        <div style={{ fontSize: 12, opacity: 0.7 }}>
          {zh ? "量化推理不可用：" : "Quant inference unavailable: "}{data.error}
        </div>
      ) : (
        <>
          <div style={{ display: "flex", gap: 16, fontSize: 12, flexWrap: "wrap", marginBottom: 8 }}>
            <span>{zh ? "模型" : "Model"}: <b>{data.model?.display}</b> ({data.model?.status})</span>
            <span>{zh ? "预测周期" : "Horizon"}: {data.model?.horizon_days}D</span>
            <span>Universe: {data.predictions?.length}</span>
            <span>{zh ? "更新时间" : "As of"}: {data.as_of?.slice(11, 19)}</span>
          </div>
          <table className="data-table" style={{ width: "100%", fontSize: 12 }}>
            <thead><tr>
              <th>{zh ? "排名" : "Rank"}</th><th>{zh ? "标的" : "Symbol"}</th>
              <th>{zh ? "预计超额收益（预测值）" : "Pred. excess return"}</th>
              <th>{zh ? "上涨概率（校准）" : "P(up) calibrated"}</th>
            </tr></thead>
            <tbody>
              {(data.predictions ?? []).slice(0, 8).map((p: any) => (
                <tr key={p.instrument_id}>
                  <td>{p.predicted_rank}/{p.universe_size}</td>
                  <td>{p.symbol}</td>
                  <td style={{ color: signColor(p.predicted_return) }}>{pct(p.predicted_return)}</td>
                  <td>{p.probability_up != null ? pct(p.probability_up, 1) : (zh ? "未校准" : "uncalibrated")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
