# Contributing to NoAlpha

NoAlpha values contributions that make measurement stricter, results easier to
reproduce, and unavailable evidence more visible. A higher metric is not an
improvement if it weakens a point-in-time or execution guard.

## Before opening a pull request

For a large change, open an issue first so the protocol and evidence requirements
can be agreed before implementation. Small bug fixes and documentation corrections
can go directly to a pull request.

Do not include credentials, paid datasets, generated model artifacts, or local
runtime state in a contribution.

## Development setup

```bash
git clone https://github.com/flysheep-ai/NoAlpha.git
cd NoAlpha
python -m venv .venv
source .venv/bin/activate
pip install -e ".[quant,dev]"

cd frontend
npm ci
cd ..
```

Everything in `.env.example` is optional. Tests and deterministic mode do not
require an LLM credential.

## Required checks

```bash
pytest tests/ -q
cd frontend && npm test && npm run build
```

Changes to labels, feature timing, dataset construction, execution assumptions,
or backtest accounting should add an adversarial regression test. If a result
depends on external data, document the source, time range, adjustment mode, cost
assumptions, universe construction, and known bias flags.

## Pull request notes

Describe:

- what changed and why;
- which commands were run;
- whether any reported metric changed;
- which point-in-time, leakage, attribution, and execution invariants were checked;
- any limitation or unavailable evidence that remains.

Code and documentation must not present deterministic fallbacks as LLM output,
historical reconstruction as forward evidence, or simulated trading as live-money
validation.
