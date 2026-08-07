# NoAlpha Measurement Integrity Audit

Date: 2026-08-07 · Scope: full repository · Baseline commit: `3e1014f`
Test baseline before this work: **141 passed, 4 skipped**
Test result after this work: **241 passed, 4 skipped**

---

## 1. Executive Summary

NoAlpha's stated purpose is trustworthy measurement, not returns. This audit
asked the two questions the system exists to answer, and answers them
separately and without hedging.

### Does Quant have alpha?

**Not answerable from this repository right now — and, separately, the live
system has not actually been running a quant model at all.**

Two distinct findings:

1. **Offline, the walk-forward evidence is weak but honest.** The CN
   walk-forward (24 instruments, 11 expanding folds, purge 11d, net of cost)
   gives `lightgbm` Rank IC +0.0274, Rank ICIR +0.115, net +11.45% vs an
   equal-weight benchmark of +2.92%. `simple_momentum` beats it on net return
   (+71.54%) with a *worse* Rank ICIR (+0.063). Seven models were run with no
   hyperparameter search, and the universe carries `survivorship_bias_risk=True`.
   A Rank ICIR of 0.115 over 11 folds on a 24-name survivorship-biased universe
   is not evidence of generalisable alpha. It is a signal worth continuing to
   measure. That was true before this audit and remains true.

2. **The ACTIVE model in the registry cannot be loaded.** Both registered
   artifacts were pickled under the pre-rename `stock_agent` package and fail
   with `ModuleNotFoundError: No module named 'stock_agent'`. Before this audit
   that raised straight out of `QuantInferenceService.infer` and up through
   `run_cycle`; the loop has in reality been running on the momentum heuristic
   the entire time while the Quant panel displayed "生效模型（驱动模拟交易）".
   This is now detected, degraded gracefully, and displayed as a red warning on
   the Quant page and as `quant: UNAVAILABLE` in System Truth. **Remediation is
   left to the operator** (see §8) rather than done here, because the honest fix
   is retraining through the new evidence gate, not resurrecting a stale pickle.

### Does LLM Research add incremental alpha?

**No, and before this audit the system could not have found out.**

The three arms were not three arms:

```python
arm_targets["llm_hybrid"] = arm_targets["deterministic_hybrid"]   # autonomous.py:1574
```

Arms B and C were the *same dict object*, and both were copied from the real
broker's live positions rather than constructed from each arm's own decisions.
The "counterfactual portfolios" were mirrors of whichever arm happened to be
driving. On top of that, the per-cycle return was
`market_price / average_cost - 1` — cumulative-since-entry return, re-applied
and re-compounded every single cycle.

Both are fixed (§2, §4). The system can now ask the question. It does not yet
have an answer, and says so: with no LLM credentials configured, Arm C runs Arm
B's decisions and the report returns `LLM_UNAVAILABLE` rather than presenting a
0.00% delta as a finding. With LLM configured but a short forward sample, it
returns `INSUFFICIENT_SAMPLE`.

**Verdict: the measurement apparatus is now trustworthy enough to be attacked.
The results are not yet strong enough to claim anything.**

---

## 2. Problems Found

Every problem below was confirmed by reading the code, not assumed from the
brief. Two items in the brief turned out **not** to be problems and are recorded
as such in §2.10.

### 2.1 The three shadow arms were one arm — P0 / Critical

**Location** `noalpha/trading/autonomous.py:1562-1575` (pre-fix)

**Root cause**
```python
arm_targets["deterministic_hybrid"] = {           # ← the REAL broker's book
    pos.symbol: pos.market_value / equity for pos in self.broker.positions.values()}
arm_targets["llm_hybrid"] = arm_targets["deterministic_hybrid"]   # ← same object
```
Arm A was built from quant signals, but B and C both read live broker positions,
and C was bound to the same dict as B.

**Why it matters** The entire "does the LLM add incremental alpha" claim rests on
three independent counterfactual books. With B ≡ C the C−B delta is
*identically zero by construction*, and neither is a counterfactual — both track
whatever the live arm did. Any reported LLM effect would have been an artefact.

**Fix** `noalpha/trading/shadow.py` rewritten. Each arm owns a `ShadowPortfolio`
with its own `cash`, `shares`, `marks`, `turnover`, `fees_paid`, `equity` and
`history`. Targets come from each arm's *own* decisions through one shared
`ShadowPortfolioPolicy`. `ShadowArena` holds no reference to a broker, account
or position store.

**Regression test** `tests/test_shadow_attribution.py::test_the_three_books_are_distinct_objects`,
`::test_different_decisions_produce_different_positions_cash_and_equity`,
`::test_no_arm_reads_the_real_broker`,
`tests/test_measurement_integrity.py::test_attack_an_arm_cannot_be_handed_another_arms_targets`

---

### 2.2 Shadow period return re-compounded the same move forever — P0 / Critical

**Location** `noalpha/trading/autonomous.py:1561` (pre-fix)

**Root cause**
```python
day_returns[sym] = (pos.market_price / pos.average_cost) - 1.0
```
`price / average_cost` is the cumulative return since entry. It was passed to
`ShadowPortfolio.mark()` every cycle as if it were that cycle's return. A stock
bought at 100 sitting flat at 110 for ten cycles compounded +10% ten times into
+159%. Worse, `day_returns` was only computed for symbols the *real broker* held,
so Arm A's independent holdings were silently marked at 0% forever.

**Why it matters** Every equity curve, every drawdown and every C−B delta was
arithmetically meaningless.

**Fix** `ShadowPortfolio` holds shares and marks. Per cycle: `mark(prices)`
re-values last cycle's holdings at this cycle's prices, then `rebalance(targets,
prices)` trades at those same prices. `r[t] = Σ w[t-1]·(p[t]/p[t-1] − 1)`,
applied exactly once. There is no `average_cost` anywhere in the module.

**Regression test** `tests/test_shadow_attribution.py::test_period_return_uses_consecutive_marks_not_average_cost`
(asserts four consecutive flat cycles return exactly 0.0),
`::test_portfolio_return_is_previous_weights_times_this_cycle_returns`,
`tests/test_measurement_integrity.py::test_attack_repeated_marking_cannot_compound_one_move`

---

### 2.3 Shadow rebalancing could harvest the same-cycle move — P0 / Critical

**Location** implicit in the pre-fix `ShadowArena.step` ordering

**Root cause** Marking and rebalancing were interleaved per arm without a
guaranteed order relative to the decision, so a target set at cycle *t* using
`p[t]` could collect the `p[t-1] → p[t]` move.

**Why it matters** This is textbook look-ahead. It inflates whichever arm trades
most, which is exactly the LLM arm when the LLM is active.

**Fix** `ShadowPortfolio.step()` enforces mark-then-trade in one method that
callers cannot reorder. `ShadowArena.step()` is the only entry point and takes
one shared price map.

**Regression test** `tests/test_shadow_attribution.py::test_mark_happens_before_rebalance_so_a_new_buy_earns_nothing_this_cycle`,
`tests/test_measurement_integrity.py::test_attack_same_cycle_price_move_cannot_be_harvested`
(price 10×'s, then the arm buys — gross return must be 0.00%)

---

### 2.4 A momentum heuristic gated the ACTIVE quant model — P0 / High

**Location** `noalpha/trading/autonomous.py:879` (pre-fix)

**Root cause**
```python
threshold = self.profile["buy_score_threshold"]        # 0.60 for "balanced"
for cand in candidates:
    if cand.score < threshold:                          # opportunity_score(mom5, mom20, MA20, vol)
        continue
```
This ran unconditionally. Quant ranking was used only to *sort* the list that the
heuristic then filtered. The real pipeline was `Heuristic Momentum Gate → Quant`,
while the docstrings and UI said `Quant → Research`.

**Why it matters** The model's #1 ranked name never reached Research if its
momentum score happened to be weak, so "does Quant have alpha" was being measured
on a heuristic-filtered subset. It also makes every quant-branded decision
provenance partly false.

**Fix** New `noalpha/trading/candidate_policy.py`. `CandidateSelectionPolicy`
gates on quant rank/strength when quant is available (heuristic score demoted to
a tiebreak, `heuristic_threshold_applied: false`), and falls back to the
heuristic gate only when quant is unavailable. Every candidate carries
`candidate_source` ∈ {`quant`, `heuristic_fallback`}, surfaced in the
`selection` event, System Truth and the UI.

**Regression test** `tests/test_candidate_selection.py::test_quant_top_candidate_survives_a_weak_heuristic_score`,
`::test_heuristic_score_is_only_a_tiebreak_under_quant`,
`tests/test_measurement_integrity.py::test_a_top_quant_name_with_a_weak_heuristic_score_still_reaches_research`
(drives every heuristic score to 0.01 and asserts research still happens),
`::test_attack_a_heuristic_gate_cannot_be_slipped_in_front_of_quant`

---

### 2.5 "Daily loss" was all-time realized PnL — P0 / High

**Location** `noalpha/trading/risk.py:108` (pre-fix)

**Root cause**
```python
total_realized = sum(p.realized_pnl for p in positions.values())
loss_ok = total_realized >= -limit_amount
```
Never reset. `self._daily_pnl` existed and was never read.

**Why it matters** Two failures in opposite directions, the second dangerous:
- a loss booked last week blocked trading forever;
- **a profit booked last week silently financed today's blow-up.** With +30%
  banked, a −20% day passed the "5% daily loss limit" untouched.

**Fix** `TradingDayRiskState` anchors to a trading date derived from the *data*
(so replays roll like live trading) and keeps four explicitly separate
quantities: `realized_daily_loss`, `equity_daily_drawdown`, `session_drawdown`,
and start-of-day anchors. Realized daily loss and daily equity drawdown are now
two independently enforced limits, reported separately. The state is persisted on
the session so a restart cannot re-anchor it.

**Regression test** `tests/test_risk_daily_state.py::test_yesterdays_loss_does_not_block_todays_trading`,
`::test_yesterdays_profit_cannot_offset_todays_loss`,
`::test_the_four_daily_quantities_stay_distinct`,
`::test_a_restart_cannot_clear_todays_realized_loss`,
`tests/test_measurement_integrity.py::test_attack_a_restart_cannot_clear_the_daily_loss_limit`

---

### 2.6 `price_deviation_pct` was configured and never enforced — P0 / Medium

**Location** `noalpha/trading/risk.py:81` (pre-fix) — assigned in `__init__`, never
read in `check()`.

**Why it matters** A bad tick could size and fill an order at an absurd price with
no gate. A limit that exists only in the constructor is worse than no limit,
because the check matrix implies it ran.

**Fix** `check()` takes `reference_price` (the quote's previous close, converted
through the same FX path) and hard-rejects on deviation. When no reference is
available the check is recorded as `skipped`, not as passed.

**Regression test** `tests/test_risk_daily_state.py::test_price_deviation_limit_is_actually_enforced`,
`::test_no_reference_price_is_recorded_as_skipped_not_silently_passed`

---

### 2.7 Promotion was a state gate, not an evidence gate — P1 / High

**Location** `noalpha/quant/registry.py:58` (pre-fix)

**Root cause** `promote()` checked one thing: `tgt_i == cur_i + 1`. Five API calls
took a freshly trained model to ACTIVE with no out-of-sample folds, no leakage
suite, no baseline, no forward observation.

**Why it matters** The five-stage lifecycle implied rigour the code did not
implement, which is the most misleading kind of gate.

**Fix** New `noalpha/quant/promotion.py`: `PromotionEvidenceBundle` (every field
defaults pessimistic — absent evidence fails), a single `REQUIREMENTS` table
keyed by `(current, target)`, `PromotionPolicy.evaluate()` returning a
`PromotionDecision` that names every unmet requirement. `registry.promote()`
takes the bundle and refuses without it. An `override_reason` exists but is
recorded permanently alongside the requirements it bypassed. See §5 for the
per-stage rules.

**Regression test** `tests/test_promotion_evidence.py` (21 tests), notably
`::test_a_model_without_forward_evidence_stays_in_shadow`,
`::test_an_empty_bundle_fails_every_stage`,
`::test_unmeasured_risk_and_execution_are_failures_not_passes`,
`::test_an_override_is_recorded_not_hidden`

---

### 2.8 `dataset_id` described the shape of the data, not the data — P1 / High

**Location** `noalpha/quant/dataset.py:84` (pre-fix)

**Root cause**
```python
version.content_hash = content_hash({
    "universe": ..., "features": ..., "label": ..., "range": ..., "n_rows": ...})
```
Change one close price, keep the row count, and `dataset_id` is byte-identical.

**Why it matters** Two experiments that disagree can cite the same dataset, and
six months later there is no way to find out why they differ. That is precisely
the question NoAlpha exists to be able to answer.

**Fix** New `noalpha/quant/provenance.py`. `DataSnapshot.from_history()` hashes
the raw bars per symbol and combined, records `provider`, `adjustment_mode` and
`retrieved_at`, and exposes `diff()` to localise which symbol changed. The raw
hash, provider and adjustment mode all feed `content_hash`, so `dataset_id`
changes when the data changes. `ExperimentProvenance` records git commit +
dirty flag, Python version, platform, dependency versions, seed, fold
definitions, cost model and universe snapshot, and `missing_fields()` names
whatever was never captured.

**Regression test** `tests/test_provenance.py::test_changing_one_price_changes_the_dataset_hash`
(asserts `n_rows` is unchanged first, or the test proves nothing),
`::test_per_symbol_hashes_localise_the_change`,
`::test_a_dirty_working_tree_is_not_reproducible`

---

### 2.9 The Quant Analyst role read the momentum heuristic — P1 / Medium

**Location** `noalpha/trading/autonomous.py:333` (pre-fix)

**Root cause** `self.agents.run(...)` was called without `quant_view`, so
`TradingAgentsOrchestrator._quant_analyst` always took the
`if not quant_view:` branch: `summary="使用候选评分 {score}"`, `status="degraded"`.
A role named `quant_analyst` was reading `opportunity_score`.

**Why it matters** Direct violation of SYSTEM TRUTH: the displayed capability did
not match the executed capability.

**Fix** `DecisionEngine.research()` takes `quant_view` and passes it through. When
quant genuinely has no view the role still reports `status="degraded"` and says
so — the fallback was not removed, it was made truthful.

**Regression test** `tests/test_measurement_integrity.py::test_the_quant_analyst_role_reads_the_real_model_not_the_heuristic`,
`::test_unavailable_analysts_still_report_unavailable` (wiring quant in must not
start fabricating fundamental/news/sentiment)

---

### 2.10 Claims in the brief that were NOT problems

Confirmed by reading the code and left unchanged:

- **"LLM might be wired directly to the broker."** It is not. `FusionPolicy`
  (`research_fusion.py`) is deterministic, versioned (`fusion-policy:v1`) and
  covers every edge case the brief listed (quant-bull/LLM-bear, quant-bear/
  LLM-bull, `UNCERTAIN`, `low_evidence`, `MISSING_LLM`). There is no
  "you are a trader, decide BUY/SELL" prompt anywhere. The LLM produces a
  structured `TradingResearchSnapshot`; a deterministic policy converts it to a
  size multiplier; hard risk runs after that and can only say no.
- **"Fundamental/news/sentiment analysts might be fabricating analysis."** They
  are not. All three return `status="unavailable"`, `confidence=0.0` and record
  themselves as risk gaps in the bear researcher's report.
- **"Walk-forward splits might be shuffled."** They are not. The only splitter is
  chronological `WalkForwardSplitter` with `purge = horizon + 1 + embargo`.
- **"Features might be non-causal."** They are not. `tests/test_quant_leakage.py`
  already asserted causality, same-bar-execution inflation, oracle detection and
  purge correctness, and the leakage demo reproduces a 13.3× Rank IC inflation and
  rejects it. All 25 quant leakage/core tests still pass.

---

### 2.11 Problems found by the attack suite itself

Two defects were discovered by writing the attacks in §2, not by reading code:

- **A tampered model artifact crashed the trading loop.** `registry._load_model`
  raises `PromotionError` on hash mismatch; that propagated out of
  `QuantInferenceService.infer` and killed `run_cycle`. Fixed in
  `noalpha/quant/inference.py` — an unloadable ACTIVE model now degrades the
  cycle to `quant: UNAVAILABLE` with the exact reason. Crashing would take the
  session down; running anyway would keep quant-branded provenance the decision
  no longer deserves. Test:
  `tests/test_measurement_integrity.py::test_attack_a_tampered_artifact_degrades_the_loop_instead_of_running`.
  **This fix is what revealed §1's finding that the live ACTIVE model has never
  been loadable.**

- **Cost-induced churn in the shadow books.** Charging the transaction cost out of
  cash after sizing against gross equity left every book levered by exactly the
  cost, which the next cycle unwound — a fee loop on arms whose decisions never
  changed. Fixed with two-pass cost-aware sizing plus a 5 bps no-trade band,
  identical across arms. Test:
  `::test_transaction_cost_is_charged_on_traded_notional` asserts an unchanged
  target generates zero fees and zero trades.

- **Empty cycles could inflate the sample count.** Spinning the loop on an empty
  universe would have added zero-return cycles toward `MIN_CYCLES_FOR_ATTRIBUTION`.
  `_shadow_step` now skips cycles with no priced instrument and no decision.
  Test: `::test_attack_shadow_cannot_reach_sample_sufficiency_on_empty_cycles`.

---

## 3. Architecture Before / After

### Before

```
Market Data
    ↓
Cheap scan ──→ opportunity_score(mom5, mom20, MA20, volume)
    ↓
Quant inference ──→ used only to SORT the candidate list
    ↓
HEURISTIC GATE   score < buy_score_threshold → dropped        ← the real gate
    ↓
Deterministic research (quant_view NOT passed → "quant analyst" reads score)
    ↓
LLM research → FusionPolicy                                    ← this part was sound
    ↓
Portfolio → Risk (daily loss = ALL-TIME realized; price deviation unused)
    ↓
Broker
    ↓
Shadow:  Arm A = quant top-5
         Arm B = copy of the REAL broker book
         Arm C = the same object as Arm B
         return = price/average_cost − 1, re-compounded every cycle
```

### After

```
Market Data ──→ ONE price map per cycle (shared by every arm)
    ↓
Cheap scan ──→ opportunity_score  (now a FEATURE / tiebreak / fallback)
    ↓
Quant inference ──→ rank, strength, predicted return
    ↓
CandidateSelectionPolicy
      quant available   → rank/strength gate, top-K   [candidate_source=quant]
      quant unavailable → heuristic threshold gate    [candidate_source=heuristic_fallback]
    ↓
Arm A decision recorded HERE (before any research can suppress it)
    ↓
Deterministic research (quant_view passed → quant analyst reads the real model)
    ↓                                        ↓
Arm B decision                        LLM research → FusionPolicy → Arm C decision
    ↓
Portfolio construction
    ↓
PreTradeRiskEngine   TradingDayRiskState: realized_daily_loss | equity_daily_drawdown
                     | session_drawdown ; price deviation ; kill switch ; stale quotes
                     (persisted on the session — a restart cannot clear it)
    ↓
Broker
    ↓
ShadowArena.step(cycle, ONE price map, per-arm targets)
      for each arm:  mark(prices)   ← last cycle's holdings, this cycle's prices
                     rebalance(targets, prices)   ← then trade, then charge cost
      three independent books: cash, shares, marks, turnover, cost, equity, history
    ↓
Decision Attribution  (what the LLM changed)
Portfolio Attribution (what those changes did)
    ↓
verdict ∈ { SAMPLE_OK | INSUFFICIENT_SAMPLE | LLM_UNAVAILABLE }
```

---

## 4. Shadow Attribution Validation

### How the three arms are independent

Each arm is a `ShadowPortfolio` instance owning `cash: float`,
`shares: dict[str, float]`, `marks: dict[str, float]`, `turnover_total`,
`fees_paid` and `history: list[ShadowCycleRow]`. `ShadowArena` constructs three
of them and holds no reference to a broker, account or position store — asserted
structurally in `test_no_arm_reads_the_real_broker`.

Targets are produced per arm by `ShadowArena.targets_for(arm, decisions)`, which
reads *that arm's* current weights and *that arm's* decisions. The three target
dicts are separate objects even when their contents coincide, and mutating one
arm's book provably does not touch another's.

Arm A's decision is recorded **before** deterministic research runs. If it were
recorded after the `research.confidence < 0.5` gate, Arm A would silently be a
subset of Arm B and would no longer be "quant alone decides".

### How the return is calculated

Per cycle, in this order, inside a single method callers cannot reorder:

1. `mark(prices)` — re-values **last cycle's** holdings at **this cycle's**
   prices. `gross_return = equity_after_mark / equity_before − 1`, which equals
   `Σ w[t-1] · (p[t]/p[t-1] − 1)`.
2. `rebalance(targets, prices)` — trades toward the new targets at those same
   prices and charges the cost.
3. `net_return = equity_after_trade / equity_before − 1`.

A holding with no price this cycle keeps its previous mark and is reported in
`unpriced`, never silently valued at zero. There is no `average_cost` in the
module; the quantity that caused the original bug does not exist here.

### How transaction cost is calculated

One-way `cost_bps` (default 15 bps ≈ 30 bps round trip, matching the pre-rewrite
assumption) applied to traded notional, counting buys and sells. Sizing is
cost-aware in two passes: sizing against gross equity and paying the cost out of
cash leaves the book levered by exactly the cost, which the next cycle unwinds
into a permanent fee loop. A 5 bps no-trade band suppresses floating-point churn.
Both the cost rate and the band are identical for every arm.

### When rebalancing happens

Once per cycle, in `_finish_cycle`, after the real broker has been marked and
persisted. Every arm steps in the same `ShadowArena.step(cycle, prices,
arm_targets)` call with the same `cycle` number and the same `prices` dict —
literally the same object, so two arms cannot be marked on different quotes.

Prices come from `_shadow_prices()`: the quotes the decisions were actually made
on, plus a fetch for any instrument an arm holds that was not scanned this cycle
(otherwise that arm's holding would freeze at a stale mark and its return would
be understated).

### How look-ahead is prevented

Marking strictly precedes rebalancing. A decision made at *t* can never collect
the return that carried the book into *t*. Asserted directly: a price that 10×'s
before the arm buys must produce `gross_return == 0.00%`
(`test_attack_same_cycle_price_move_cannot_be_harvested`).

### Stated limits

Shadow books ignore lot sizes, partial fills and intraday microstructure. They
measure signal and decision quality, not execution quality. Cycles are scan
cycles, not calendar periods, so Sharpe is reported **per cycle** and never
annualised — `compute_arm_metrics` deliberately has no `annualized_return` field
and a test asserts its absence.

### Sample sufficiency

`MIN_CYCLES_FOR_ATTRIBUTION = 20` **and**
`MIN_DECISION_CHANGES_FOR_ATTRIBUTION = 10`. Below either, `verdict` is
`INSUFFICIENT_SAMPLE`. The numeric deltas are still shown — they are facts about
three real books — but `verdict` is the only field the UI renders as a
conclusion, and `tokens_per_return_point` is suppressed to `null`. Logging 200
`llm_changed_decision=True` deltas does not move the verdict
(`test_attack_decision_changes_alone_do_not_produce_an_alpha_claim`).

---

## 5. Promotion Rules

Policy version `promotion-policy:v1`. Stage order is still enforced (one step at
a time) **and** the target stage's evidence must be satisfied. Both gates are
hard. Absent evidence fails.

### EXPERIMENTAL → VALIDATED
| Requirement | Field |
|---|---|
| dataset point-in-time checks passed | `dataset_pit_checks_passed` |
| leakage attack suite passed | `leakage_suite_passed` |
| ≥ 3 out-of-sample folds | `oos_folds` |
| ≥ 500 out-of-sample rows | `oos_rows` |
| experiment reproduces from recorded provenance | `experiment_reproducible` |
| model artifact hash verifies | `artifact_hash_valid` |

### VALIDATED → SHADOW
| Requirement | Field |
|---|---|
| out-of-sample metrics recorded | `oos_metrics` |
| a baseline measured on the same window | `baseline_metrics` |
| backtest is net of the cost model | `cost_adjusted_backtest` |
| no critical data-quality warning outstanding | `critical_data_quality_warnings` |

### SHADOW → PAPER
| Requirement | Field |
|---|---|
| ≥ 20 forward shadow cycles | `forward_shadow_cycles` |
| ≥ 30 forward decisions observed | `forward_decisions` |
| ≥ 2 market regimes observed | `market_regimes_observed` |
| stability checks passed | `stability_checks_passed` |
| no severe feature/prediction drift | `severe_drift_detected` |

### PAPER → ACTIVE
| Requirement | Field |
|---|---|
| ≥ 30 paper decisions | `paper_decisions` |
| risk limits measured and never breached | `risk_limit_breaches == 0` |
| execution success rate ≥ 95% | `execution_success_rate` |
| paper performance recorded | `paper_return is not None` |
| paper return beats the benchmark on the same window | `paper_return > benchmark_return` |

`RETIRED` / `REJECTED` need no evidence — stopping a model is always allowed.

**Unmeasured is not passed.** `risk_limit_breaches` defaults to `-1` and
`execution_success_rate` to `-1.0`; both fail. `evidence_from_experiment()`
reads folds, rows, metrics and cost model from a stored experiment but leaves
`leakage_suite_passed`, `dataset_pit_checks_passed`, `experiment_reproducible`
and `artifact_hash_valid` `False` — a file cannot certify that a suite ran. In
`experiments/train_champion.py` that attestation now comes from actually
invoking `pytest tests/test_quant_leakage.py` and reading the exit code.

**Consequence, by design:** a freshly trained model with excellent offline
numbers reaches SHADOW and stops. `train_champion.py` walks as far as the
evidence carries and prints `HELD at shadow: …` rather than promoting on.

**Overrides** are possible (`override_reason=`) and are recorded permanently in
`status_history` together with every requirement they bypassed. The two test
fixtures that need an ACTIVE model use an explicit override rather than a
fabricated evidence bundle.

**Legacy audit.** `registry.audit_promotions()` classifies every model's current
status as `EVIDENCED` / `OVERRIDDEN` / `UNVERIFIED_LEGACY` / `NOT_PROMOTED` and
additionally reports artifacts that cannot be loaded. It is surfaced in
`/api/quant/overview` and rendered on the Quant page.

---

## 6. Reproducibility

To rerun an experiment and understand any difference, the following is now
recorded per run (`QuantExperiment.provenance`, from
`noalpha/quant/provenance.py`):

**Data identity** — `dataset_id` (now includes the raw-data hash),
`raw_data_hash`, `data_snapshot_id`, and a `DataSnapshot` carrying `provider`,
`adjustment_mode`, `retrieved_at`, `start_date`, `end_date`, per-symbol content
hash and per-symbol bar count. `DataSnapshot.diff()` answers "which symbol
changed?".

**Definitions** — `feature_set_version`, `label_version`, `universe_snapshot`.

**Procedure** — `model_params`, `random_seed`, full `fold_definitions`,
`cost_model`.

**Environment** — git commit, branch and **dirty flag**, Python version,
platform, and versions of pandas / numpy / scikit-learn / lightgbm.

**Honesty about gaps** — `provenance_missing` lists anything never captured and
`reproducible` is `False` if anything is missing **or the working tree was
dirty**, because a dirty tree means the recorded commit does not describe the
code that ran. `model_artifact_hash` is deliberately *not* required for a
walk-forward run (each fold trains a fresh model and persists none); artifact
integrity belongs to the registry and is enforced there.

Verified on a real run: `exp-8c92dcee340d` records
`raw_data_hash=e2700f02410f650e`, 11 fold definitions, seed 42, Python 3.13.9,
commit `3e1014f` with `dirty=true`, and correctly reports `reproducible: False`.

---

## 7. Test Results

### Unit + integration

```
$ .venv/bin/python -m pytest tests/ -q
241 passed, 4 skipped, 25 warnings in 23.42s
```
Baseline before this work was 141 passed / 4 skipped. **100 tests added**, none
removed, no existing assertion weakened.

| New file | Tests | Covers |
|---|---|---|
| `tests/test_shadow_attribution.py` | 19 | period return, mark/rebalance order, cost, arm independence, attribution, sample sufficiency, restart survival |
| `tests/test_measurement_integrity.py` | 23 | the fixes inside the real loop (11) + the attack suite (12) |
| `tests/test_promotion_evidence.py` | 21 | every stage's requirements, overrides, artifact hash, introspection |
| `tests/test_risk_daily_state.py` | 12 | day rollover, the four distinct quantities, price deviation, kill switch, restart |
| `tests/test_provenance.py` | 10 | one-price hash change, per-symbol diff, environment capture, dirty tree |
| `tests/test_pit_invariants.py` | 8 | evidence filtering, retro LLM features not trainable |
| `tests/test_candidate_selection.py` | 7 | quant-led gate, tiebreak, fallback, provenance |

### Leakage suite

```
$ .venv/bin/python -m pytest tests/test_quant_leakage.py tests/test_quant_core.py -q
25 passed
```

### Leakage demo (real data)

```
$ .venv/bin/python experiments/leakage_demo.py
                           Rank IC   Rank ICIR    net return    sharpe
  clean protocol           +0.0274      +0.115       +11.45%     +0.21
  leaky same-bar           +0.3644      +1.488       -33.07%     -0.22
  Rank IC inflation: 13.3x
OK: leakage reproduced and rejected (13.3x inflation).
```

### Quant walk-forward (real CN data, 24 instruments, 11 folds, net of cost)

```
$ .venv/bin/python experiments/quant_phase_q.py --market cn
  simple_momentum              RankIC +0.0184 RankICIR +0.063  net +71.54% sharpe +0.58
  lightgbm                     RankIC +0.0274 RankICIR +0.115  net +11.45% sharpe +0.21
  xgboost                      RankIC +0.0350 RankICIR +0.154  net  -2.43% sharpe +0.09
  equal_weight_benchmark                                        net  +2.92% sharpe +0.13
```
Recorded as-is, including the fact that the model with the best Rank ICIR is not
the model with the best net return, and that a trivial momentum rule beats both
on return. No experiment was deleted.

### Autonomous replay (real historical bars)

```
$ .venv/bin/python -m evals.runners.autonomous_replay
trading days replayed : 25 (2026-07-06 → 2026-08-07)
autonomous BUY fills  : 9    SELL fills : 4
exit reasons          : ['take_profit', 'take_profit', 'stop_loss', 'take_profit']
future_evidence_total : 0
VERDICT: PASS (BUY+SELL+NO_ACTION closed loops, zero leakage)
```
Note: this run executed in **heuristic fallback** because the ACTIVE model
artifact does not load (§1). Before this audit the same condition raised an
uncaught `ModuleNotFoundError` inside `run_cycle`.

### Shadow comparison (new runner, real bars, 40 cycles)

```
$ .venv/bin/python experiments/shadow_comparison.py --days 40
arm                      cum ret    max dd  turnover      cost  trades  sharpe/cyc
  quant_only              +0.00%    +0.00%      0.00         0       0         n/a
  deterministic_hybrid    +1.06%    -1.91%      0.57        86     112      +0.063
  llm_hybrid              +1.06%    -1.91%      0.57        86     112      +0.063

decisions observed      : 13
LLM changed a decision  : 0 (0.0%)
sample: 40/20 cycles, 0/10 decision changes
VERDICT: LLM_UNAVAILABLE
  Arm C fell back to Arm B's deterministic decisions for every cycle, so no
  incremental-LLM question was asked and none is answered here.
```
Arm A trades nothing because quant is unavailable — "quant only" with no quant is
correctly no decisions, not a fabricated book.

### Frontend

```
$ npx tsc --noEmit      → clean
$ npx vitest run        → 1 file, 2 tests passed
```

### Browser verification

Backend (`uvicorn`, port 8000) + frontend (`vite`, port 5199), real session with
45 replayed cycles.

- **System Truth bar** renders the new row: `候选来源 = 启发式回退`,
  `三臂样本 = INSUFFICIENT_SAMPLE`, `当日风控 = 0.00%`, and states
  `量化模型未参与本轮决策 — ACTIVE model unusable: No module named 'stock_agent'`.
- **Shadow panel** renders `LLM_UNAVAILABLE` as the headline badge, the three arms
  with cum return / max DD / turnover / cost / trades / Sharpe-per-cycle,
  decision attribution `0/18 (0.0%) · veto 0 · reduce 0`, and the C−B delta
  explicitly labelled `0.00%（未达样本门槛）`. **No "LLM alpha +X%" headline is
  reachable.**
- **Quant page** now shows a red banner
  `ACTIVE 模型的 artifact 无法加载 — 交易循环实际未使用量化模型` with the exact
  errors, an amber banner naming the ACTIVE model promoted without recorded
  evidence, and `UNVERIFIED_LEGACY` in the model row.

---

## 8. Remaining Limitations

Kept deliberately. None of these were removed to make the project look mature.

**Both registered model artifacts are unloadable.** `ModuleNotFoundError: No
module named 'stock_agent'` — pickled before the package rename. The system now
detects and reports this instead of crashing, but **the quant layer is not
running**. This was not "fixed" here: resurrecting a stale pickle through a
compatibility shim would restore a number without restoring the evidence behind
it. The correct remediation is `experiments/train_champion.py --market cn`, which
retrains under the current package and, under the new gate, will stop at
**SHADOW** for lack of forward evidence — leaving the system with **no ACTIVE
model** and the UI honestly showing `quant: UNAVAILABLE`. That is an operator
decision, not an audit decision.

**One ACTIVE model carries `UNVERIFIED_LEGACY`.** `mdl-8c861fa32271` (lightgbm
v1, CN) was promoted under the old state gate. Its status is real; its evidence
was never recorded. Surfaced in `/api/quant/overview` and on the Quant page.

**Survivorship bias.** `DatasetVersion.survivorship_bias_risk` is `True`
throughout. The universe is today's watchlist, not point-in-time index
constituents. Every experiment record carries the flag.

**Small universe.** 24 instruments for the walk-forward, 6 for replay. Rank IC
on a 24-name cross-section is noisy; Rank ICIR +0.115 over 11 folds is not
evidence of generalisable alpha.

**Unofficial data providers.** Tencent `fqkline`/`usfqkline` and Eastmoney, with
no SLA, no restatement notice and no adjustment-factor history. `DataSnapshot`
now makes a silent restatement *detectable* after the fact; it does not prevent
one. During this audit the Eastmoney quote endpoint repeatedly returned
`Remote end closed connection without response`.

**LLM forward sample shortage.** No LLM credentials are configured in this
environment, so Arm C has never diverged from Arm B. Zero decision changes over
40 cycles. The incremental-LLM question is **unasked**, not answered — reported
as `LLM_UNAVAILABLE`, never as a 0.00% finding.

**Live broker unvalidated.** All trading is against `VirtualBroker`. The Alpaca
adapter exists but no execution path has been validated against a real venue.
"ACTIVE" in this registry means "feeds the simulated loop".

**Shadow books ignore microstructure.** No lot sizes, no partial fills, no
intraday fills. They measure signal and decision quality only.

**Sharpe is per-cycle.** A scan cycle is not a calendar period. Nothing here is
annualised, so cross-study comparison against annualised Sharpe figures is
invalid.

**Multiple-testing exposure.** 7 models × 2 markets were evaluated. No
correction is applied; `multiple_testing_note` is recorded in the results file.

**Market-regime coverage is unmeasured.** `market_regimes_observed` is a required
field for SHADOW → PAPER, but nothing in the repository currently classifies
regimes. Until something does, no model can satisfy that requirement — which is
the intended failure direction.

---

## 9. Against the Final Standard

> Could a quant researcher who does not trust this project confirm the following
> from code, logs, experiment records and tests?

1. **No peeking at the future.** Yes. Causal features, purged walk-forward with
   embargo, `ReplayMarketFeed` structurally date-bounded, evidence filtered by
   `available_at` *and* `event_time`, retro LLM snapshots excluded from
   `trainable_rows()`, shadow marks strictly before rebalances. The leakage demo
   reproduces a 13.3× inflation and rejects it. 33 dedicated tests.
2. **No cherry-picked experiments.** Partially. All 7 CN models are recorded
   including the losers, nothing was deleted, and the champion criterion is
   documented as pre-declared. But 7 models × 2 markets with no multiple-testing
   correction is real selection exposure, and it is stated rather than corrected.
3. **The LLM's contribution is not overstated.** Yes — because it is stated as
   unmeasured. Decision attribution and portfolio attribution are separate, and
   `llm_changed_decision` alone provably cannot produce an alpha claim.
4. **The three strategies are compared fairly.** Yes, now. One price map, one cost
   model, one sizing policy, one cycle clock; only the decision differs; each arm
   owns its book. 19 + 12 tests, several of which are attacks.
5. **Every return has real provenance.** Improved but not complete. Dataset
   identity now includes the raw data; experiment provenance includes the
   environment and names its own gaps. Live-session equity provenance flows
   through the event store. But `reproducible: False` on the current runs
   (dirty tree) is the honest state.
6. **A model cannot reach trading unvalidated.** Yes, going forward — evidence
   gate plus stage order, with overrides permanently recorded. **Not
   retroactively**: one ACTIVE model predates the gate and is flagged
   `UNVERIFIED_LEGACY`.
7. **The LLM can never bypass deterministic risk.** Yes. `PreTradeRiskEngine`
   reads no model score and no research snapshot; it runs after fusion and can
   only reject or downsize. Asserted by forcing the engine to reject and
   checking that nothing fills.
8. **A pretty number can be reproduced, attacked and falsified.** Mostly. The
   attack suite is real and found three defects while being written.
   `experiments/shadow_comparison.py` makes the three-arm claim executable by
   anyone. The gap is external data: an unofficial provider can restate history
   and the best the system can do is *detect* it afterwards.

**Items 2, 5 and 6 are not fully clean.** They are stated here rather than
smoothed over, which is the point.

---

## 10. Files Changed

**New** — `noalpha/trading/candidate_policy.py`, `noalpha/quant/promotion.py`,
`noalpha/quant/provenance.py`, `experiments/shadow_comparison.py`, and seven test
files.

**Rewritten** — `noalpha/trading/shadow.py`.

**Modified** — `noalpha/trading/autonomous.py`, `risk.py`, `routes.py`,
`session.py`; `noalpha/quant/dataset.py`, `domain.py`, `experiment.py`,
`inference.py`, `llm_features.py`, `registry.py`, `routes.py`;
`experiments/train_champion.py`; `frontend/src/Trading.tsx`, `Quant.tsx`,
`api.ts`; two test fixtures updated for changed signatures (no assertion
weakened).
