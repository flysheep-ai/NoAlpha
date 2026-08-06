"""Signal evaluation (IC family) and net-of-cost portfolio backtest.

Two deliberately separate layers (第三十八条):
  * prediction/signal metrics — daily IC, Rank IC, ICIR, Rank ICIR
    (logic equivalent to qlib/contrib/eva/alpha.py::calc_ic);
  * trading metrics — top-k rebalanced portfolio, net of explicit costs,
    with executable labels (T+1 open entry), never same-bar fills.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


# ── signal metrics ──


def daily_ic(pred: pd.Series, label: pd.Series) -> pd.DataFrame:
    """pred/label share a MultiIndex (date, secid). Returns per-date ic/rank_ic."""
    df = pd.DataFrame({"pred": pred, "label": label}).dropna()
    grouped = df.groupby(level="date")
    ic = grouped.apply(lambda g: g["pred"].corr(g["label"]))
    rank_ic = grouped.apply(lambda g: g["pred"].corr(g["label"], method="spearman"))
    return pd.DataFrame({"ic": ic, "rank_ic": rank_ic})


def ic_summary(pred: pd.Series, label: pd.Series) -> dict[str, float]:
    table = daily_ic(pred, label).dropna()
    if table.empty:
        return {"ic": float("nan"), "rank_ic": float("nan"), "icir": float("nan"),
                "rank_icir": float("nan"), "ic_positive_ratio": float("nan"), "n_days": 0}
    ic, ric = table["ic"], table["rank_ic"]
    return {
        "ic": float(ic.mean()),
        "rank_ic": float(ric.mean()),
        "icir": float(ic.mean() / ic.std()) if ic.std() > 0 else 0.0,
        "rank_icir": float(ric.mean() / ric.std()) if ric.std() > 0 else 0.0,
        "ic_positive_ratio": float((ic > 0).mean()),
        "n_days": int(len(table)),
    }


def prediction_metrics(pred: pd.Series, label: pd.Series) -> dict[str, float]:
    df = pd.DataFrame({"pred": pred, "label": label}).dropna()
    if df.empty:
        return {"mse": float("nan"), "mae": float("nan")}
    err = df["pred"] - df["label"]
    return {"mse": float((err ** 2).mean()), "mae": float(err.abs().mean())}


# ── cost model ──


@dataclass
class CostModel:
    """Explicit per-side costs in basis points of traded notional.

    CN defaults reflect the configured current rules (sell-side stamp duty
    0.05% since 2023-08; verify before changing markets) — they are inputs,
    not facts baked into results.
    """
    commission_bps: float = 5.0        # per side
    slippage_bps: float = 10.0         # per side
    sell_tax_bps: float = 0.0          # CN stamp duty on sells; set 5.0 for CN

    def round_trip_cost(self) -> float:
        return (2 * (self.commission_bps + self.slippage_bps) + self.sell_tax_bps) / 1e4

    def to_dict(self) -> dict[str, float]:
        return {"commission_bps": self.commission_bps, "slippage_bps": self.slippage_bps,
                "sell_tax_bps": self.sell_tax_bps}


CN_COST = CostModel(commission_bps=5.0, slippage_bps=10.0, sell_tax_bps=5.0)
US_COST = CostModel(commission_bps=5.0, slippage_bps=10.0, sell_tax_bps=0.0)


# ── top-k portfolio backtest ──


@dataclass
class TopKBacktestResult:
    period_returns: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    holdings: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"metrics": self.metrics,
                "period_returns": {str(k): float(v) for k, v in self.period_returns.items()},
                "holdings": self.holdings}


def topk_backtest(pred: pd.Series, executable_return: pd.Series, *,
                  k: int = 5, horizon_days: int = 5, cost: CostModel,
                  weighting: str = "equal", inv_vol: pd.Series | None = None,
                  periods_per_year: float | None = None) -> TopKBacktestResult:
    """Hold-top-k portfolio rebalanced every horizon_days.

    pred: MultiIndex (date, secid) prediction scores.
    executable_return: same index; realized open(T+1)→open(T+1+h) return —
        the label BEFORE any cross-sectional transformation.
    Costs are charged on the fraction of the book replaced at each rebalance
    (turnover penalty, 第六十五条).
    """
    df = pd.DataFrame({"pred": pred, "ret": executable_return}).dropna()
    if inv_vol is not None:
        df["inv_vol"] = inv_vol
    dates = sorted(df.index.get_level_values("date").unique())
    rebalance_dates = dates[::horizon_days]

    prev: set[str] = set()
    rows = []
    holdings_log = []
    for d in rebalance_dates:
        cs = df.xs(d, level="date")
        if len(cs) < k:
            continue
        top = cs.nlargest(k, "pred")
        names = set(top.index)
        if weighting == "inverse_vol" and "inv_vol" in top:
            w = top["inv_vol"].clip(lower=0.0).fillna(0.0)
            weights = (w / w.sum()) if w.sum() > 0 else pd.Series(1.0 / k, index=top.index)
        else:
            weights = pd.Series(1.0 / k, index=top.index)
        gross = float((top["ret"] * weights).sum())
        turnover = 1.0 if not prev else len(names - prev) / k
        # entering & exiting names pay one side each; full rebalance = round trip
        cost_paid = turnover * cost.round_trip_cost()
        net = gross - cost_paid
        rows.append((d, gross, net, turnover))
        holdings_log.append({"date": d, "symbols": sorted(names), "gross": gross,
                             "net": net, "turnover": turnover})
        prev = names

    if not rows:
        return TopKBacktestResult(metrics={"n_periods": 0})

    table = pd.DataFrame(rows, columns=["date", "gross", "net", "turnover"]).set_index("date")
    net = table["net"]
    ppy = periods_per_year or (245 / horizon_days)
    cum = float((1 + net).prod() - 1)
    ann_ret = float((1 + net.mean()) ** ppy - 1)
    sharpe = float(net.mean() / net.std() * np.sqrt(ppy)) if net.std() > 0 else 0.0
    curve = (1 + net).cumprod()
    max_dd = float((curve / curve.cummax() - 1).min())
    metrics = {
        "n_periods": int(len(net)),
        "cumulative_net_return": cum,
        "cumulative_gross_return": float((1 + table["gross"]).prod() - 1),
        "annualized_net_return": ann_ret,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "avg_turnover": float(table["turnover"].mean()),
        "total_cost_drag": float((1 + table["gross"]).prod() - (1 + net).prod()),
    }
    return TopKBacktestResult(period_returns=net, holdings=holdings_log, metrics=metrics)


def equal_weight_benchmark(executable_return: pd.Series, *, horizon_days: int = 5,
                           cost: CostModel) -> TopKBacktestResult:
    """Hold the whole universe equally — the honest benchmark (第一百一十条)."""
    df = executable_return.dropna()
    dates = sorted(df.index.get_level_values("date").unique())
    rows = []
    for d in dates[::horizon_days]:
        cs = df.xs(d, level="date")
        rows.append((d, float(cs.mean())))
    table = pd.DataFrame(rows, columns=["date", "gross"]).set_index("date")
    net = table["gross"]  # buy-and-hold-ish: negligible rebalance cost, charged once
    if len(net):
        net.iloc[0] -= cost.round_trip_cost()
    ppy = 245 / horizon_days
    sharpe = float(net.mean() / net.std() * np.sqrt(ppy)) if net.std() > 0 else 0.0
    curve = (1 + net).cumprod()
    metrics = {
        "n_periods": int(len(net)),
        "cumulative_net_return": float((1 + net).prod() - 1),
        "sharpe": sharpe,
        "max_drawdown": float((curve / curve.cummax() - 1).min()) if len(net) else 0.0,
        "avg_turnover": 0.0,
    }
    return TopKBacktestResult(period_returns=net, metrics=metrics)
