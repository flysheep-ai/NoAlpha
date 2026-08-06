# NoAlpha

**Trustworthy measurement for LLM trading research.**

Most AI trading projects cannot tell you whether their edge is real. The tooling
to answer that question — point-in-time discipline, leakage detection, honest
attribution of what the LLM actually contributed — is either missing or assumed.
This project is that tooling, wrapped around a working A-share / US-equity
research and simulated-trading system.

**Why "NoAlpha"?** Because claiming alpha is easy and proving it is hard. This
project refuses to claim what it cannot prove — including about itself.

[中文说明](README_zh.md) · MIT licensed

---

## What's new here

Four things this repository does that we could not find in existing open-source
trading projects:

### 1. Forward-only LLM feature collection

Guarding the *inputs* to an LLM is not enough. An LLM asked today what it thought
about a stock in 2024 does not reason from the evidence you hand it — it recalls
what happened, because the outcome is in its pretraining corpus. The literature
calls this **parametric look-ahead**, and it is an active 2026 research problem:

- [Detecting Lookahead Bias in LLM Forecasts](https://arxiv.org/abs/2512.23847) — models recall in-cutoff prices verbatim
- [Look-Ahead-Bench](https://arxiv.org/abs/2601.13770) — significant temporal bias in general-purpose models
- [Chronologically Consistent LMs](https://arxiv.org/pdf/2510.11677) — mitigates it by retraining on time-stamped corpora

The academic fix is to retrain the model, which is out of reach for most
practitioners. `noalpha/quant/llm_features.py` takes the engineering route
instead: an append-only store whose `available_at` is the wall-clock completion
time of a real LLM call, which **refuses to backfill**. Retro-generated rows are
forced to `pit_safe=false` and excluded from readiness counts.

It is slow — readiness is months, not minutes — and as far as we can tell it is
the only open-source implementation of the discipline.

### 2. A backtest that can manufacture its own fraud

```bash
python experiments/leakage_demo.py
```

Two identical LightGBM models, identical purged walk-forward folds, identical
real A-share bars. One label swapped:

```
                           Rank IC   Rank ICIR    net return    sharpe
  ────────────────────────────────────────────────────────────────────
  clean protocol           +0.0274      +0.115       +11.45%     +0.21
  leaky same-bar           +0.3644      +1.488       -33.07%     -0.22

  Rank IC inflation: 13.3x
```

`close[T+4]/close[T-1]` is one of the most common label formulations in public
backtesting code. It spans decision day `T`, which the model's own `return_1d`
feature already knows. It looks **13x better** and loses a third of the account.

The demo exits non-zero if the leak stops reproducing, so it runs as a regression
test on the point-in-time machinery — not a screenshot.

### 3. Infrastructure to measure what the LLM actually contributes

Three arms run against the same quotes, the same cost assumptions and the same
cycle timing: `quant_only`, `deterministic_hybrid`, `llm_hybrid`. Only one drives
the broker; the others are shadow portfolios. Every divergence is logged with
`llm_changed_decision`, so the LLM's contribution is an attributable number
rather than a vibe.

Most projects can't answer "did the LLM help?" because they never ran the
counterfactual. This one is built to.

### 4. Promotion is a gate, not a habit

`EXPERIMENTAL → VALIDATED → SHADOW → PAPER → ACTIVE`, one step at a time,
enforced in code. Training a model never activates it. The US model failed to
beat its benchmark, so it sits at `SHADOW` and **does not trade** — the gate
working as designed, not an oversight.

Related: the LLM never places an order. It emits a structured signal into a
versioned deterministic fusion policy that can damp or veto but not originate a
trade. There is no "you are a trader" prompt anywhere in the codebase — the
inverse of the prevailing agent-first design.

---

## Architecture

| Layer | Module | What it does |
|---|---|---|
| Market data | `noalpha/market/` | Multi-host provider fallback, exchange-timestamp freshness, trading calendars |
| Evidence research | `noalpha/research/`, `pit.py` | Point-in-time guard, evidence normalization, timeline, event detection, citation validation |
| Quant | `noalpha/quant/` | Versioned labels, purged walk-forward, experiment store, model registry + promotion gate, calibration, ensembling |
| Trading | `noalpha/trading/` | Autonomous loop, LLM fusion policy, hard risk engine, virtual broker, three-arm shadow comparison |

Labels encode execution assumptions: `open[T+1+h]/open[T+1]-1` — decide at `T`
close, fill at `T+1` open. Same-bar execution is structurally impossible rather
than merely discouraged.

---

## What the instruments measured

Real walk-forward, 2019-01 → 2026-07, 11 folds, expanding train 500d / valid 60d /
test 120d / purge 11d, net of cost (5bp commission + 10bp slippage, plus 5bp
sell-side stamp duty for A-shares).

### A-shares, top-5, 5-day rebalance

| Model | IC | Rank IC | Rank ICIR | Net return | Sharpe | MDD | Turnover |
|---|---|---|---|---|---|---|---|
| random | −0.006 | +0.002 | +0.01 | −47.2% | −0.45 | −54% | 0.83 |
| simple_momentum | +0.021 | +0.018 | +0.06 | **+71.5%** | +0.58 | −35% | 0.37 |
| simple_reversal | +0.007 | +0.002 | +0.01 | −31.0% | −0.16 | −43% | 0.76 |
| prior heuristic (`opportunity_score`) | +0.014 | +0.013 | +0.04 | −26.1% | −0.14 | −57% | 0.58 |
| linear_ridge | +0.015 | −0.003 | −0.01 | −31.3% | −0.18 | −47% | 0.49 |
| **lightgbm** (champion, ACTIVE) | +0.033 | +0.027 | +0.115 | +11.5% | +0.21 | −30% | 0.46 |
| xgboost | +0.030 | **+0.035** | **+0.154** | −2.4% | +0.09 | −36% | 0.52 |
| equal-weight benchmark | — | — | — | +2.9% | +0.13 | −32% | 0 |

The champion beats the benchmark on return, drawdown and turnover, and loses on
return to a five-line momentum rule. That tradeoff is recorded rather than
hidden — which is the point of building the measurement layer in the first place.

On US equities no model beat the equal-weight benchmark (+149.8%, Sharpe 1.02),
so none was promoted past `SHADOW`.

### Does adding a research layer help?

| Strategy | A-shares | US |
|---|---|---|
| Quant only (LightGBM) | **+11.5%** | **+145.7%** |
| Deterministic research proxy only | −13.3% | +96.4% |
| Quant + deterministic research | −1.8% | +13.4% |
| **Quant + real LLM research** | *not yet measurable* | *not yet measurable* |

**Read this table carefully — it is easy to misread.** The measured hybrid arm
uses a *deterministic* research proxy (a momentum restatement), not an LLM. That
proxy correlates with the quant signal while carrying more noise, so blending it
diluted the ranking.

**The real-LLM arm has not been measured.** Its shadow comparison currently
reports `INSUFFICIENT_SAMPLE`, and forward collection is ongoing. Anyone telling
you whether LLM research adds alpha — positively or negatively — on evidence
like this is overclaiming. We are not going to.

What the LLM layer *is* observed to do today is intercept conflicts: when the
quant and research signals disagree, it downgrades to reduce/no_action instead of
averaging them away.

---

## Limitations you should read before trusting anything here

1. **The universe is 24 instruments per market.** A Rank IC of +0.027 over 24
   names is statistically thin. Treat these numbers as a demonstration that the
   *protocol* works, not as evidence that the *model* works. Widening the
   universe is the highest-priority open issue.
2. **Survivorship bias is present and flagged.** The universe is today's liquid
   names, not point-in-time index constituents. Every dataset carries
   `survivorship_bias_risk=true`.
3. **Market data comes from unofficial endpoints** (Eastmoney / Sina / Tencent).
   They rate-limit, they change without notice, and their terms of service may
   restrict your use. There is no commercial data licence here. Check your own
   jurisdiction and the providers' terms before any non-personal use.
4. **LLM features are not in any model yet**, by design, and will not be for months.
5. **No live broker path is validated.** An Alpaca adapter exists; it has never
   traded real money.
6. `citation_support` returned 0.0 on every run until 2026-08 due to an
   evidence-id format mismatch. Fixed, with regression tests, but any LLM-fusion
   result recorded before that date understated the research signal.

---

## Quick start

```bash
pip install -e ".[quant]"          # core install omits the ML extras
cp .env.example .env               # optional: add an LLM key to enable research
uvicorn noalpha.api:app --port 8000

cd frontend && npm install && npm run dev   # console at http://127.0.0.1:5173
```

Or `docker compose up`.

**Configuration.** Everything in `.env.example` is optional — with no keys the
system runs in deterministic mode and says so, rather than silently pretending an
LLM was involved. Secrets resolve in this order, highest first:

1. `.cache/api/credentials.json` — set at runtime in the Settings UI, gitignored
2. exported environment variables — what CI and containers use
3. `.env` — local development, gitignored

`.env` is never committed; `.env.example` documents every variable the code reads
and is enforced by a test.

### Reproduce

```bash
python experiments/leakage_demo.py                    # the 13.3x inflation demo
python experiments/quant_phase_q.py --market cn       # full 7-model walk-forward
python -m evals.runners.autonomous_replay             # historical replay, closed loops
```

### Simulated trading

```bash
noalpha trading create --initial-capital 100000 --markets CN,US
noalpha trading run-once
noalpha trading status
```

### Tests

```bash
pytest tests/ -q     # 141 passed, 3 skipped
```

Includes 7 leakage-attack tests that try to sneak future information past the
point-in-time guard, and hard invariant tests asserting that an executed proposal
can never report as pending.

## Further reading

- [`docs/adr/`](docs/adr/) — 12 architecture decision records: why LangGraph, a
  knowledge graph, and a vector database were each rejected for now
- [`docs/audit/phase_q_report.md`](docs/audit/phase_q_report.md) — the full
  walk-forward run behind the results tables above, including the leakage demo
- [`docs/audit/phase_q5_report.md`](docs/audit/phase_q5_report.md) — the LLM
  fusion work and why the LLM arm still reports `INSUFFICIENT_SAMPLE`
- [`docs/research/deep_dives/`](docs/research/deep_dives/) — source-level reads
  of TradingAgents, Qlib, Lean, NautilusTrader, OpenBB, Freqtrade and others

## Disclaimer

Research and educational software. Not financial, investment, or trading advice.
Simulated results are not indicative of real performance. Do not point this at
real money.
