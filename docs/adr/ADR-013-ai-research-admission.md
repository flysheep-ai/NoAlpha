# ADR-013: Admit AI evidence before allowing it to influence execution

Date: 2026-10-29
Status: Accepted

## Problem

The research layer already separates evidence, bullish/bearish analysis,
deterministic fusion and hard risk. Its decision boundaries nevertheless
allowed four observable failures:

1. An expired or event-invalidated research snapshot was returned when the
   refresh budget was exhausted. Future and timezone-free timestamps were
   also not reliably rejected.
2. Internal AI disagreement could halve a holding before the evidence-quality
   check ran. `pit_safe` was carried but not enforced by fusion, and
   `research_only` was lost during signal conversion.
3. AI confirmation could open a position below the pure-quant arm's 0.6 entry
   threshold. Even an explicit fusion veto could fall through to the
   deterministic buy branch in the main account.
4. Cache reuse and budget denial were counted as new AI calls; fallback
   decisions could be attributed to AI or labelled as hybrid proposals.

## External references and design choice

Reviewed on 2026-10-29:

- [FreqAI model expiration](https://www.freqtrade.io/en/stable/freqai-running/#defining-model-expirations)
  prevents entries based on expired predictions. NoAlpha applies the same
  eligibility principle to research snapshots: inability to refresh does not
  make invalidated evidence usable again.
- [FreqAI prediction eligibility](https://www.freqtrade.io/en/stable/freqai-configuration/#important-dataframe-key-patterns)
  distinguishes usable predictions from rejected predictions. NoAlpha similarly
  checks evidence admission before interpreting bullish/bearish direction.
- [TradingAgents](https://github.com/TauricResearch/TradingAgents) separates
  analysts, bullish/bearish research and risk review. NoAlpha already has this
  role structure; the useful next step is enforcing the handoff to deterministic
  execution, rather than adding more model-generated opinions.

These are architectural references; no upstream implementation was copied and
no dependency was added. More agents or a different predictive model would need
new forward evidence before any performance claim.

## Decision

- Version fusion as `fusion-policy:v2`. Keep eligible-signal sizing rules and
  hard risk checks, but share the pure-quant 0.6 entry floor with AI fusion.
  This floor applies to new entries, not management of existing positions.
- Reject unsafe temporal provenance and invalid AI scores; check low evidence
  before internal disagreement. Ineligible AI falls back to the existing
  quant-only rule (75% entry target; quant-led holding management), with the
  rejection reason recorded. `research_only` also makes a signal ineligible.
- Require timezone-aware `as_of <= generated_at <= available_at <= now`.
  Measure research age from `as_of`, so a slow investigation cannot renew old
  evidence. Keep price-move and quant-reversal refresh triggers.
- When invalidated research cannot be refreshed, return no signal and a status
  identifying both the budget denial and invalidation reason. Check new
  snapshots before caching them or collecting forward features.
- When fusion is active, its no-buy result cannot fall through to another
  policy's buy branch. The deterministic arm still runs as a counterfactual.
- Persist per-candidate research status and fallback provenance. Cache hits
  and denied refreshes add no calls, tokens or latency. Only admitted AI can
  receive decision-change or confirmation credit. Legacy records without
  participation provenance do not count as confirmations.

## Validation and limits

`tests/test_research_fusion.py` exercises weak/missing/invalid quant signals,
unsafe AI evidence, contradictory low-quality research, future/naive/malformed
timestamps, slow investigations, refresh denial/failure and successful reuse.
It also runs the real trading loop, virtual broker and shadow books to prove
that a bearish AI veto blocks an otherwise executable deterministic buy, and
that fallback is not credited to AI. Positive tests preserve eligible entry
sizing and position management.

Existing historical results are not regenerated or relabelled. v1/v2 runs must
be treated as different policies. No profitability improvement is claimed:
the verified improvement is decision integrity and attribution correctness.
Validation uses synthetic/local inputs, not paid LLM calls or live funds.

Local verification on 2026-10-29: 291 backend tests passed, 4 external-service
tests skipped; 2 frontend tests passed and the production build succeeded.
The new admission suite adds 48 cases. Before the fixes, the first 31-case
regression suite reproduced 27 failures, including the broker veto bypass.

The existing `calls` counter measures research attempts, not individual model
HTTP requests. Snapshot token usage still comes from the provider's most recent
response; complete multi-pass/retry cost accounting remains separate work.
Price-move invalidation still uses an absolute daily threshold and can request
repeated refreshes during a volatile session, subject to the existing budget.
