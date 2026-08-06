import React from "react";
import { Activity, ArrowDown, ArrowUp, ChevronDown, ChevronUp, Plus, Radio, Search, Trash2, TrendingDown, TrendingUp, WifiOff, X } from "lucide-react";
import { api } from "./api";
import type { KlineBar, MarketAnomaly, MarketQuote, MarketSearchItem, WatchlistItem } from "./api";

// ── freshness badge: only real states from the backend, never fabricated ──

const FRESHNESS_COLORS: Record<string, string> = {
  live: "var(--good)", delayed: "var(--warn)", stale: "var(--bad)",
  closed: "var(--muted)", unknown: "var(--faint)", replay: "var(--accent)",
};

function FreshnessBadge({ quote, zh }: { quote: MarketQuote; zh: boolean }) {
  const color = FRESHNESS_COLORS[quote.freshness] ?? "var(--faint)";
  const label = quote.freshness.toUpperCase();
  const delay = quote.delay_seconds >= 0 ? ` ${Math.round(quote.delay_seconds)}s` : "";
  const title = zh
    ? `交易所时间 ${quote.timestamp || "未知"} · 接收 ${quote.received_at} · 来源 ${quote.source}`
    : `Exchange time ${quote.timestamp || "unknown"} · received ${quote.received_at} · source ${quote.source}`;
  return (
    <span title={title}
      style={{ fontSize: 11, fontFamily: "var(--font-mono)", color, display: "inline-flex", alignItems: "center", gap: 4 }}>
      <Activity size={10} /> {label}{quote.freshness === "delayed" ? delay : ""}
    </span>
  );
}

function SessionBadge({ session, zh }: { session: string; zh: boolean }) {
  const map: Record<string, [string, string, string]> = {
    open: ["交易中", "Open", "var(--good)"], closed: ["已收盘", "Closed", "var(--muted)"],
    pre_market: ["盘前", "Pre-market", "var(--warn)"], post_market: ["盘后", "Post-market", "var(--warn)"],
    break: ["午间休市", "Lunch break", "var(--muted)"], unknown: ["未知", "Unknown", "var(--faint)"],
  };
  const e = map[session] ?? map.unknown;
  const [label, color] = [zh ? e[0] : e[1], e[2]];
  return <span style={{ fontSize: 11, color }}>{label}</span>;
}

// CN convention groups by 万/亿; everything else by K/M/B.
function fmtVol(v: number, zh: boolean): string {
  if (zh) {
    if (v >= 1e8) return (v / 1e8).toFixed(2) + "亿";
    if (v >= 1e4) return (v / 1e4).toFixed(1) + "万";
  } else {
    if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
  }
  return String(Math.round(v));
}
function fmtNum(v: number | undefined | null, digits = 2): string {
  return v === undefined || v === null || Number.isNaN(v) ? "—" : v.toFixed(digits);
}
function pctColor(v: number) { return v >= 0 ? "var(--good)" : "var(--bad)"; }

// ── candlestick chart (SVG, incremental updates via props) ──

function CandleChart({ bars, height = 320, zh }: { bars: KlineBar[]; height?: number; zh: boolean }) {
  const [hover, setHover] = React.useState<number | null>(null);
  const width = 860;
  if (!bars.length) return <div className="empty">{zh ? "无K线数据" : "No candlestick data"}</div>;
  const view = bars.slice(-120);
  const min = Math.min(...view.map(b => b.low));
  const max = Math.max(...view.map(b => b.high));
  const range = max - min || 1;
  const maxVol = Math.max(...view.map(b => b.volume)) || 1;
  const chartH = height - 70;
  const volH = 50;
  const bw = width / view.length;
  const y = (p: number) => chartH - ((p - min) / range) * (chartH - 10) - 5;
  const h = hover !== null ? view[hover] : view[view.length - 1];
  return (
    <div>
      <div style={{ display: "flex", gap: 16, fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--muted)", marginBottom: 4, flexWrap: "wrap" }}>
        <span>{h.time}</span>
        <span>{zh ? "开" : "O"} {fmtNum(h.open)}</span><span>{zh ? "高" : "H"} {fmtNum(h.high)}</span>
        <span>{zh ? "低" : "L"} {fmtNum(h.low)}</span><span style={{ color: h.close >= h.open ? "var(--good)" : "var(--bad)" }}>{zh ? "收" : "C"} {fmtNum(h.close)}</span>
        <span>{zh ? "量" : "Vol"} {fmtVol(h.volume, zh)}</span>
        {h.turnover > 0 && <span>{zh ? "额" : "Turnover"} {fmtVol(h.turnover, zh)}</span>}
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", display: "block" }}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const rect = (e.target as SVGElement).closest("svg")!.getBoundingClientRect();
          const idx = Math.floor(((e.clientX - rect.left) / rect.width) * view.length);
          setHover(Math.max(0, Math.min(view.length - 1, idx)));
        }}>
        {[0.25, 0.5, 0.75].map(f => (
          <line key={f} x1={0} x2={width} y1={chartH * f} y2={chartH * f} stroke="var(--border)" strokeWidth={0.5} />
        ))}
        {view.map((b, i) => {
          const up = b.close >= b.open;
          const color = up ? "#e05555" : "#2eae6c"; // CN convention: red up / green down
          const cx = i * bw + bw / 2;
          const bodyTop = y(Math.max(b.open, b.close));
          const bodyH = Math.max(1, Math.abs(y(b.open) - y(b.close)));
          return (
            <g key={b.time}>
              <line x1={cx} x2={cx} y1={y(b.high)} y2={y(b.low)} stroke={color} strokeWidth={1} />
              <rect x={i * bw + bw * 0.18} width={bw * 0.64} y={bodyTop} height={bodyH} fill={color} />
              <rect x={i * bw + bw * 0.18} width={bw * 0.64}
                y={chartH + 4 + (volH - (b.volume / maxVol) * volH)} height={(b.volume / maxVol) * volH}
                fill={color} opacity={0.5} />
            </g>
          );
        })}
        {hover !== null && (
          <line x1={hover * bw + bw / 2} x2={hover * bw + bw / 2} y1={0} y2={height} stroke="var(--muted)" strokeWidth={0.5} strokeDasharray="3 3" />
        )}
        <text x={4} y={12} fontSize={10} fill="var(--muted)">{fmtNum(max)}</text>
        <text x={4} y={chartH - 2} fontSize={10} fill="var(--muted)">{fmtNum(min)}</text>
      </svg>
    </div>
  );
}

// ── live quotes via WebSocket ──

function useMarketStream(secids: string[]) {
  const [quotes, setQuotes] = React.useState<Record<string, MarketQuote>>({});
  const [connected, setConnected] = React.useState(false);
  const secidsKey = secids.slice().sort().join(",");
  React.useEffect(() => {
    if (!secidsKey) return;
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 0;
    const connect = () => {
      ws = new WebSocket(`${protocol}//${window.location.host}/api/v1/market/stream`);
      ws.onopen = () => {
        setConnected(true); retry = 0;
        ws!.send(JSON.stringify({ action: "subscribe", symbols: secidsKey.split(",") }));
      };
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "snapshot") {
            const map: Record<string, MarketQuote> = {};
            for (const q of msg.quotes || []) if (q.secid) map[q.secid] = q;
            setQuotes(prev => ({ ...prev, ...map }));
          } else if (msg.type === "quote_update" && msg.quote?.secid) {
            setQuotes(prev => ({ ...prev, [msg.quote.secid]: msg.quote }));
          }
        } catch { /* ignore malformed frames */ }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closed && retry < 8) { retry += 1; setTimeout(connect, Math.min(1000 * 2 ** retry, 15000)); }
      };
    };
    connect();
    return () => { closed = true; ws?.close(); };
  }, [secidsKey]);
  return { quotes, connected };
}

// ── instrument search ──

function InstrumentSearch({ onSelect, zh }: { onSelect: (item: MarketSearchItem) => void; zh: boolean }) {
  const [q, setQ] = React.useState("");
  const [items, setItems] = React.useState<MarketSearchItem[]>([]);
  const [open, setOpen] = React.useState(false);
  const [loading, setLoading] = React.useState(false);
  const timer = React.useRef<number | undefined>(undefined);
  React.useEffect(() => {
    window.clearTimeout(timer.current);
    if (q.trim().length < 1) { setItems([]); setOpen(false); return; }
    timer.current = window.setTimeout(async () => {
      setLoading(true);
      try {
        const r = await api.marketSearch(q.trim());
        setItems(r.items); setOpen(true);
      } catch { setItems([]); }
      setLoading(false);
    }, 250);
    return () => window.clearTimeout(timer.current);
  }, [q]);
  return (
    <div style={{ position: "relative", flex: 1, maxWidth: 460 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, border: "1px solid var(--border)", borderRadius: 8, padding: "8px 12px", background: "var(--panel)" }}>
        <Search size={16} color="var(--muted)" />
        <input data-testid="market-search" value={q} onChange={e => setQ(e.target.value)}
          onFocus={() => items.length && setOpen(true)}
          placeholder={zh ? "搜索股票：AAPL / 苹果 / 600519 / 贵州茅台…" : "Search: AAPL / Apple / 600519 / Kweichow Moutai…"}
          style={{ border: "none", outline: "none", background: "transparent", flex: 1, color: "var(--text)", fontSize: 14 }} />
        {q && <X size={14} style={{ cursor: "pointer" }} onClick={() => { setQ(""); setOpen(false); }} />}
      </div>
      {open && (
        <div style={{ position: "absolute", top: "110%", left: 0, right: 0, zIndex: 30, background: "var(--panel)", border: "1px solid var(--border)", borderRadius: 8, maxHeight: 320, overflowY: "auto", boxShadow: "0 8px 24px rgba(0,0,0,0.4)" }}>
          {loading && <div style={{ padding: 12, color: "var(--muted)", fontSize: 13 }}>{zh ? "搜索中…" : "Searching…"}</div>}
          {!loading && !items.length && <div style={{ padding: 12, color: "var(--muted)", fontSize: 13 }}>{zh ? "无匹配结果" : "No matches"}</div>}
          {items.map(item => (
            <button key={item.secid} data-testid={`search-result-${item.symbol}`}
              onClick={() => { onSelect(item); setOpen(false); setQ(""); }}
              style={{ display: "flex", width: "100%", gap: 10, padding: "10px 12px", background: "none", border: "none", borderBottom: "1px solid var(--border)", cursor: "pointer", color: "var(--text)", alignItems: "center", textAlign: "left" }}>
              <strong style={{ fontFamily: "var(--font-mono)", minWidth: 70 }}>{item.symbol}</strong>
              <span style={{ flex: 1 }}>{item.name}</span>
              <span style={{ fontSize: 11, color: "var(--muted)" }}>{item.market} · {item.exchange} · {item.currency}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ── stock detail (quote + kline + actions) ──

const KLINE_PERIODS = [
  { key: "1m", label: ["1分", "1m"] }, { key: "5m", label: ["5分", "5m"] }, { key: "15m", label: ["15分", "15m"] },
  { key: "60m", label: ["60分", "60m"] }, { key: "1d", label: ["日K", "1D"] },
];

export function StockDetail({ secid, onClose, onWatchlistChange, inWatchlist, liveQuote, zh }: {
  secid: string; onClose: () => void; onWatchlistChange: () => void;
  inWatchlist: boolean; liveQuote?: MarketQuote; zh: boolean;
}) {
  const [quote, setQuote] = React.useState<MarketQuote | null>(null);
  const [period, setPeriod] = React.useState("1d");
  const [bars, setBars] = React.useState<KlineBar[]>([]);
  const [klineSource, setKlineSource] = React.useState("");
  const [klineError, setKlineError] = React.useState<string | null>(null);
  const [disabledPeriods, setDisabledPeriods] = React.useState<Set<string>>(new Set());
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    api.marketQuote(secid).then(setQuote).catch(() => setQuote(null));
  }, [secid]);
  // live update from stream
  React.useEffect(() => { if (liveQuote) setQuote(liveQuote); }, [liveQuote]);

  React.useEffect(() => {
    let cancelled = false;
    setKlineError(null);
    api.marketKlines(secid, period).then(r => {
      if (cancelled) return;
      if (r.error) {
        setKlineError(r.error); setBars([]);
        setDisabledPeriods(prev => new Set(prev).add(period));
      } else { setBars(r.bars); setKlineSource(r.source); }
    }).catch(e => { if (!cancelled) { setKlineError(String(e)); setBars([]); } });
    return () => { cancelled = true; };
  }, [secid, period]);

  // incremental: refresh only the latest bar periodically instead of refetching history
  React.useEffect(() => {
    if (!bars.length || period === "1d") return;
    const t = setInterval(async () => {
      try {
        const r = await api.marketKlines(secid, period, 2);
        if (!r.error && r.bars.length) {
          setBars(prev => {
            const next = [...prev];
            for (const nb of r.bars) {
              const idx = next.findIndex(b => b.time === nb.time);
              if (idx >= 0) next[idx] = nb; else if (nb.time > next[next.length - 1].time) next.push(nb);
            }
            return next;
          });
        }
      } catch { /* keep existing bars */ }
    }, 30000);
    return () => clearInterval(t);
  }, [secid, period, bars.length > 0]);

  const q = quote;
  return (
    <div className="panel wide" data-testid="stock-detail" style={{ position: "relative" }}>
      <button onClick={onClose} style={{ position: "absolute", top: 12, right: 12 }}><X size={14} /></button>
      {!q ? <div className="loading">{zh ? "加载报价…" : "Loading quote…"}</div> : (
        <>
          <div style={{ display: "flex", alignItems: "baseline", gap: 14, flexWrap: "wrap" }}>
            <h2 style={{ margin: 0 }}>{q.name} <span style={{ fontFamily: "var(--font-mono)", fontSize: 15, color: "var(--muted)" }}>{q.symbol}</span></h2>
            <span style={{ fontSize: 12, color: "var(--muted)" }}>{q.market} · {q.exchange} · {q.currency}</span>
            <SessionBadge session={q.market_session} zh={zh} />
            <FreshnessBadge quote={q} zh={zh} />
            <span style={{ fontSize: 11, color: "var(--faint)" }}>
              {zh ? "来源" : "Source"} {q.source} · {zh ? "数据时间" : "as of"} {q.timestamp ? new Date(q.timestamp).toLocaleString() : (zh ? "未知" : "unknown")}
            </span>
          </div>
          <div style={{ display: "flex", gap: 20, alignItems: "baseline", margin: "10px 0", flexWrap: "wrap" }}>
            <span style={{ fontSize: 34, fontFamily: "var(--font-mono)", fontWeight: 700, color: pctColor(q.change_percent) }}>
              {fmtNum(q.price)}
            </span>
            <span style={{ fontSize: 16, fontFamily: "var(--font-mono)", color: pctColor(q.change_percent) }}>
              {q.change_percent >= 0 ? <TrendingUp size={15} style={{ display: "inline" }} /> : <TrendingDown size={15} style={{ display: "inline" }} />}
              {" "}{q.change >= 0 ? "+" : ""}{fmtNum(q.change)} ({q.change_percent >= 0 ? "+" : ""}{fmtNum(q.change_percent)}%)
            </span>
            <div style={{ display: "flex", gap: 8 }}>
              {inWatchlist ? (
                <button onClick={async () => { setBusy(true); await api.watchlistRemove(secid); onWatchlistChange(); setBusy(false); }} disabled={busy} data-testid="remove-watchlist">
                  <Trash2 size={13} /> {zh ? "移出自选" : "Remove from watchlist"}
                </button>
              ) : (
                <button className="primary" onClick={async () => { setBusy(true); await api.watchlistAdd(secid); onWatchlistChange(); setBusy(false); }} disabled={busy} data-testid="add-watchlist">
                  <Plus size={13} /> {zh ? "加自选" : "Add to watchlist"}
                </button>
              )}
              <button onClick={() => {
                const context = zh
                  ? `${q.name} 当前价 ${q.price} (${q.change_percent}%)`
                  : `${q.name} last ${q.price} (${q.change_percent}%)`;
                const params = new URLSearchParams({ symbol: q.symbol, secid, context });
                window.history.pushState({}, "", `/chat?${params}`);
                window.dispatchEvent(new PopStateEvent("popstate"));
              }} data-testid="ask-ai">🔍 {zh ? "AI 调查" : "AI investigate"}</button>
            </div>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 8, fontSize: 13, marginBottom: 14 }}>
            {[
              [zh ? "开盘" : "Open", fmtNum(q.open)], [zh ? "最高" : "High", fmtNum(q.high)], [zh ? "最低" : "Low", fmtNum(q.low)],
              [zh ? "昨收" : "Prev close", fmtNum(q.previous_close)], [zh ? "成交量" : "Volume", fmtVol(q.volume, zh)],
              [zh ? "成交额" : "Turnover", q.turnover > 0 ? fmtVol(q.turnover, zh) : (zh ? "不可用" : "n/a")],
              [zh ? "延迟" : "Delay", q.delay_seconds >= 0 ? `${Math.round(q.delay_seconds)}s` : (zh ? "未知" : "unknown")],
            ].map(([label, value]) => (
              <div key={label as string} style={{ background: "var(--bg)", borderRadius: 6, padding: "8px 10px" }}>
                <div style={{ fontSize: 11, color: "var(--muted)" }}>{label}</div>
                <div style={{ fontFamily: "var(--font-mono)" }}>{value}</div>
              </div>
            ))}
          </div>
          <div style={{ display: "flex", gap: 6, marginBottom: 8, alignItems: "center" }}>
            {KLINE_PERIODS.map(p => (
              <button key={p.key} onClick={() => setPeriod(p.key)} disabled={disabledPeriods.has(p.key)}
                title={disabledPeriods.has(p.key) ? (zh ? "数据源当前不提供该周期" : "The data source does not currently serve this period") : ""}
                style={{ fontSize: 12, padding: "4px 10px", opacity: disabledPeriods.has(p.key) ? 0.4 : 1, background: period === p.key ? "var(--accent)" : undefined, color: period === p.key ? "#fff" : undefined }}>
                {zh ? p.label[0] : p.label[1]}
              </button>
            ))}
            {klineSource && <span style={{ fontSize: 11, color: "var(--faint)" }}>
              {zh ? `K线来源: ${klineSource}（真实历史数据）` : `Candles from ${klineSource} (real history)`}
            </span>}
          </div>
          {klineError
            ? <div className="error-box" style={{ fontSize: 12 }}>
                {zh ? `该周期K线暂不可用（数据源报错，不显示伪造数据）：${klineError}`
                    : `Candles unavailable for this period (data-source error; no synthetic data is shown): ${klineError}`}
              </div>
            : <CandleChart bars={bars} zh={zh} />}
        </>
      )}
    </div>
  );
}

// ── quote row used by watchlist ──

const QuoteRow = React.memo(function QuoteRow({ item, quote, onOpen, onRemove, onToggle, onMove, zh }: {
  item: WatchlistItem; quote?: MarketQuote; onOpen: () => void; onRemove: () => void;
  onToggle: () => void; onMove: (dir: -1 | 1) => void; zh: boolean;
}) {
  const q = quote ?? item.quote ?? null;
  return (
    <div className="row" data-testid={`watch-row-${item.symbol}`} style={{ gridTemplateColumns: "90px 130px 60px 90px 90px 90px 90px 110px 100px", opacity: item.enabled ? 1 : 0.45, alignItems: "center" }}>
      <strong style={{ cursor: "pointer", fontFamily: "var(--font-mono)" }} onClick={onOpen}>{item.symbol}</strong>
      <span style={{ cursor: "pointer" }} onClick={onOpen}>{item.name}</span>
      <span style={{ fontSize: 11, color: "var(--muted)" }}>{item.market}</span>
      <strong style={{ fontFamily: "var(--font-mono)" }}>{q ? fmtNum(q.price) : "—"}</strong>
      <span style={{ fontFamily: "var(--font-mono)", color: q ? pctColor(q.change_percent) : "var(--faint)" }}>
        {q ? `${q.change_percent >= 0 ? "+" : ""}${fmtNum(q.change_percent)}%` : "—"}
      </span>
      <span style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{q ? fmtVol(q.volume, zh) : "—"}</span>
      <span style={{ fontSize: 11 }}>{q ? <SessionBadge session={q.market_session} zh={zh} /> : null}</span>
      <span>{q ? <FreshnessBadge quote={q} zh={zh} /> : <span style={{ fontSize: 11, color: "var(--faint)" }}>{zh ? "无数据" : "no data"}</span>}</span>
      <span style={{ display: "flex", gap: 4 }}>
        <button title={zh ? "上移" : "Move up"} onClick={() => onMove(-1)} style={{ padding: 2 }}><ChevronUp size={12} /></button>
        <button title={zh ? "下移" : "Move down"} onClick={() => onMove(1)} style={{ padding: 2 }}><ChevronDown size={12} /></button>
        <button title={item.enabled ? (zh ? "停用" : "Disable") : (zh ? "启用" : "Enable")} onClick={onToggle} style={{ padding: 2, fontSize: 10 }}>
          {item.enabled ? (zh ? "停" : "off") : (zh ? "启" : "on")}
        </button>
        <button title={zh ? "删除" : "Remove"} onClick={onRemove} style={{ padding: 2 }}><Trash2 size={12} /></button>
      </span>
    </div>
  );
});

// ── index cards ──

function IndexCard({ q, zh }: { q: MarketQuote; zh: boolean }) {
  return (
    <div className="panel" style={{ padding: "14px 16px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, color: "var(--muted)", marginBottom: 6 }}>
        <span>{q.name}</span><FreshnessBadge quote={q} zh={zh} />
      </div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
        <span style={{ fontSize: 22, fontFamily: "var(--font-mono)", fontWeight: 700 }}>{q.price.toLocaleString()}</span>
        <span style={{ fontSize: 14, fontFamily: "var(--font-mono)", color: pctColor(q.change_percent) }}>
          {q.change_percent >= 0 ? "+" : ""}{fmtNum(q.change_percent)}%
        </span>
      </div>
    </div>
  );
}

// ── movers & anomalies ──

function MoversPanel({ onOpen, zh }: { onOpen: (secid: string) => void; zh: boolean }) {
  const [market, setMarket] = React.useState("CN");
  const [rank, setRank] = React.useState("gainers");
  const [data, setData] = React.useState<{ items: MarketQuote[]; coverage: string } | null>(null);
  const [error, setError] = React.useState("");
  React.useEffect(() => {
    let cancelled = false;
    api.marketMovers(market, rank).then(d => { if (!cancelled) { setData(d); setError(""); } })
      .catch(e => !cancelled && setError(String(e)));
    return () => { cancelled = true; };
  }, [market, rank]);
  return (
    <div className="panel">
      <div className="section-title" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2>{zh ? "市场榜单" : "Market movers"}</h2>
        <div style={{ display: "flex", gap: 4 }}>
          {["CN", "US"].map(m => <button key={m} onClick={() => setMarket(m)} style={{ fontSize: 11, padding: "2px 8px", background: market === m ? "var(--accent)" : undefined, color: market === m ? "#fff" : undefined }}>
            {zh ? (m === "CN" ? "A股" : "美股") : (m === "CN" ? "A-shares" : "US")}
          </button>)}
        </div>
      </div>
      <div style={{ display: "flex", gap: 4, marginBottom: 8 }}>
        {([["gainers", "涨幅", "Gainers"], ["losers", "跌幅", "Losers"], ["volume", "成交量", "Volume"], ["turnover", "成交额", "Turnover"]] as const).map(([k, zhLabel, enLabel]) => (
          <button key={k} onClick={() => setRank(k)} style={{ fontSize: 11, padding: "2px 8px", background: rank === k ? "var(--panel-2, #2a2a35)" : undefined }}>{zh ? zhLabel : enLabel}</button>
        ))}
      </div>
      {error && <div className="error-box" style={{ fontSize: 12 }}>{zh ? "榜单不可用：" : "Movers unavailable: "}{error}</div>}
      {data && (
        <>
          <div style={{ fontSize: 10, color: "var(--faint)", marginBottom: 6 }}>
            {zh ? "覆盖范围: " : "Coverage: "}
            {data.coverage === "full_market" ? (zh ? "全市场（真实数据）" : "full market (real data)") : data.coverage}
          </div>
          {data.items.map(q => (
            <button key={q.secid} onClick={() => onOpen(q.secid)}
              style={{ display: "flex", width: "100%", gap: 8, padding: "6px 4px", background: "none", border: "none", borderBottom: "1px solid var(--border)", cursor: "pointer", color: "var(--text)", fontSize: 12, alignItems: "center" }}>
              <strong style={{ fontFamily: "var(--font-mono)", minWidth: 62, textAlign: "left" }}>{q.symbol}</strong>
              <span style={{ flex: 1, textAlign: "left", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{q.name}</span>
              <span style={{ fontFamily: "var(--font-mono)" }}>{fmtNum(q.price)}</span>
              <span style={{ fontFamily: "var(--font-mono)", color: pctColor(q.change_percent), minWidth: 62, textAlign: "right" }}>
                {q.change_percent >= 0 ? "+" : ""}{fmtNum(q.change_percent)}%
              </span>
            </button>
          ))}
        </>
      )}
    </div>
  );
}

function AnomalyPanel({ onOpen, zh }: { onOpen: (secid: string) => void; zh: boolean }) {
  const [items, setItems] = React.useState<MarketAnomaly[]>([]);
  const [note, setNote] = React.useState("");
  React.useEffect(() => {
    const load = () => api.marketAnomalies().then(d => { setItems(d.items); setNote(d.note); }).catch(() => {});
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, []);
  const sevColor: Record<string, string> = { high: "var(--bad)", medium: "var(--warn)", low: "var(--muted)" };
  return (
    <div className="panel">
      <div className="section-title"><h2>{zh ? "实时异动" : "Live anomalies"}</h2><p style={{ fontSize: 10 }}>{note}</p></div>
      {!items.length && <div className="empty" style={{ fontSize: 12 }}>
        {zh ? "暂无异动（基于已追踪标的的真实盘中数据）" : "No anomalies (based on real intraday data for tracked instruments)"}
      </div>}
      {items.map(a => (
        <div key={a.secid} style={{ borderBottom: "1px solid var(--border)", padding: "8px 4px", fontSize: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <strong style={{ fontFamily: "var(--font-mono)" }}>{a.symbol} <span style={{ fontWeight: 400 }}>{a.name}</span></strong>
            <span style={{ color: sevColor[a.severity] ?? "var(--muted)", fontSize: 10, fontFamily: "var(--font-mono)" }}>{a.severity.toUpperCase()}</span>
          </div>
          <div style={{ display: "flex", gap: 12, color: "var(--muted)", fontFamily: "var(--font-mono)", marginTop: 2 }}>
            <span>
              {zh ? `${Math.round(a.window_seconds / 60)}分收益` : `${Math.round(a.window_seconds / 60)}m return`}
              {" "}<b style={{ color: pctColor(a.window_return) }}>{(a.window_return * 100).toFixed(2)}%</b>
            </span>
            {a.volume_ratio > 0 && <span>{zh ? "量比" : "vol ratio"} {a.volume_ratio}x</span>}
          </div>
          <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
            <button style={{ fontSize: 10, padding: "2px 8px" }} onClick={() => onOpen(a.secid)}>{zh ? "详情" : "Details"}</button>
            <button style={{ fontSize: 10, padding: "2px 8px" }} onClick={() => {
              const mins = Math.round(a.window_seconds / 60);
              const pct = (a.window_return * 100).toFixed(2);
              const context = zh
                ? `异动: ${mins}分钟收益 ${pct}%, 量比 ${a.volume_ratio}x, 检测时间 ${a.detected_at}`
                : `Anomaly: ${mins}-minute return ${pct}%, volume ratio ${a.volume_ratio}x, detected at ${a.detected_at}`;
              const params = new URLSearchParams({ symbol: a.symbol, secid: a.secid, context });
              window.history.pushState({}, "", `/chat?${params}`);
              window.dispatchEvent(new PopStateEvent("popstate"));
            }}>{zh ? "调查异动" : "Investigate"}</button>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── page ──

export function MarketPage({ lang }: { lang: string }) {
  const zh = lang.startsWith("zh");
  const [watchlist, setWatchlist] = React.useState<WatchlistItem[]>([]);
  const [detail, setDetail] = React.useState<string | null>(null);
  const [overview, setOverview] = React.useState<{ cn_indices: MarketQuote[]; us_indices: MarketQuote[] } | null>(null);
  const watchSecids = React.useMemo(() => watchlist.filter(w => w.enabled).map(w => w.secid), [watchlist]);
  const streamIds = React.useMemo(() => detail ? [...watchSecids, detail] : watchSecids, [watchSecids, detail]);
  const { quotes, connected } = useMarketStream(streamIds);

  const loadWatchlist = React.useCallback(() => {
    api.marketWatchlist().then(d => setWatchlist(d.items)).catch(() => {});
  }, []);
  React.useEffect(() => {
    loadWatchlist();
    // Index names come from the backend, so refetch when the language changes.
    api.marketOverview(lang).then(setOverview).catch(() => {});
    const t = setInterval(() => api.marketOverview(lang).then(setOverview).catch(() => {}), 30000);
    return () => clearInterval(t);
  }, [loadWatchlist, lang]);

  const move = async (secid: string, dir: -1 | 1) => {
    const ids = watchlist.map(w => w.secid);
    const i = ids.indexOf(secid);
    const j = i + dir;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    await api.watchlistReorder(ids);
    loadWatchlist();
  };

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
        <InstrumentSearch onSelect={item => setDetail(item.secid)} zh={zh} />
        <span style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--muted)", fontSize: 12 }}>
          {connected ? <Radio size={13} color="var(--good)" /> : <WifiOff size={13} color="var(--bad)" />}
          {connected ? (zh ? "行情流已连接" : "Quote stream connected") : (zh ? "重连中…" : "Reconnecting…")}
        </span>
      </div>

      {detail && (
        <StockDetail secid={detail} onClose={() => setDetail(null)}
          inWatchlist={watchlist.some(w => w.secid === detail)}
          onWatchlistChange={loadWatchlist}
          liveQuote={quotes[detail]} zh={zh} />
      )}

      {overview && (
        <>
          <div>
            <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>{zh ? "A股指数" : "A-share indices"}</div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 10 }}>
              {overview.cn_indices.map(q => <IndexCard key={q.secid} q={q} zh={zh} />)}
              {!overview.cn_indices.length && <div className="empty" style={{ fontSize: 12 }}>
                {zh ? "A股指数暂不可用（不显示伪造数据）" : "A-share indices unavailable (no synthetic data is shown)"}
              </div>}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>{zh ? "美股指数" : "US indices"}</div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 10 }}>
              {overview.us_indices.map(q => <IndexCard key={q.secid} q={q} zh={zh} />)}
              {!overview.us_indices.length && <div className="empty" style={{ fontSize: 12 }}>
                {zh ? "美股指数暂不可用（不显示伪造数据）" : "US indices unavailable (no synthetic data is shown)"}
              </div>}
            </div>
          </div>
        </>
      )}

      <div className="panel wide">
        <div className="section-title">
          <h2>{zh ? "自选股（跨市场）" : "Watchlist (cross-market)"}</h2>
          <p>{zh ? "持久化存储 · 实时行情来源 Eastmoney · 支持增删/停启/排序"
                : "Persisted · live quotes from Eastmoney · add, remove, enable/disable and reorder"}</p>
        </div>
        <div className="data-table" data-testid="watchlist-table">
          <div className="row" style={{ gridTemplateColumns: "90px 130px 60px 90px 90px 90px 90px 110px 100px", fontSize: 11, color: "var(--muted)" }}>
            <span>{zh ? "代码" : "Symbol"}</span><span>{zh ? "名称" : "Name"}</span><span>{zh ? "市场" : "Market"}</span>
            <span>{zh ? "现价" : "Last"}</span><span>{zh ? "涨跌幅" : "Change"}</span><span>{zh ? "成交量" : "Volume"}</span>
            <span>{zh ? "状态" : "Session"}</span><span>{zh ? "数据" : "Data"}</span><span>{zh ? "操作" : "Actions"}</span>
          </div>
          {watchlist.map(item => (
            <QuoteRow key={item.secid} item={item} quote={quotes[item.secid]}
              onOpen={() => setDetail(item.secid)}
              onRemove={async () => { await api.watchlistRemove(item.secid); loadWatchlist(); }}
              onToggle={async () => { await api.watchlistToggle(item.secid, !item.enabled); loadWatchlist(); }}
              onMove={dir => move(item.secid, dir)} zh={zh} />
          ))}
          {!watchlist.length && <div className="empty">
            {zh ? "自选股为空——使用上方搜索添加 A 股或美股" : "Watchlist is empty — use the search above to add A-shares or US equities"}
          </div>}
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
        <MoversPanel onOpen={setDetail} zh={zh} />
        <AnomalyPanel onOpen={setDetail} zh={zh} />
      </div>
    </div>
  );
}
