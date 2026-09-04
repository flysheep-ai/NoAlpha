## What changed

<!-- Describe the problem and the smallest useful change. -->

## Verification

<!-- List the exact commands run and summarize their results. -->

- [ ] `pytest tests/ -q`
- [ ] `cd frontend && npm test && npm run build` (when frontend behavior is affected)

## Measurement integrity

- [ ] I did not weaken point-in-time, leakage, execution, attribution, or risk gates.
- [ ] Metric changes include their data scope, cost assumptions, and known limitations.
- [ ] Fallbacks and unavailable evidence remain explicit.
- [ ] No credentials, local runtime data, or generated model artifacts are included.
