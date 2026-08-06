# AI Hedge Fund Deep Dive

## Repository

https://github.com/virattt/ai-hedge-fund

Current activity observed locally: `3a18702 2026-07-03 Bump version to 2026.7.3`.

## Architecture

App-style project with backend, frontend flow UI, services, portfolio/backtest services, and agent flow concepts.

## Core Modules

- `app/backend/main.py`: FastAPI backend entry.
- `app/backend/services/agent_service.py`: source read pending.
- `app/backend/services/backtest_service.py`: source read pending.
- `app/backend/services/graph.py`: source read pending.
- `app/backend/services/portfolio.py`: source read pending.
- `app/frontend/src/components/Flow.tsx`: visual flow.
- `app/frontend/src/nodes/components/*`: agent/output/portfolio nodes.

## Execution Flow

Likely visual node/flow driven analysis and backtest execution through backend services. Full source read blocked by partial clone/SSL.

## Data Flow

Frontend flow config -> backend services -> agent/backtest/portfolio outputs -> UI nodes.

## Strengths

- Product/UI inspiration for flow-based research.
- Shows how retail AI investing apps package agents.

## Weaknesses

- Not yet verified as production-grade.
- Needs source read before borrowing architecture.
- Likely demo-heavy.

## Production Readiness

Unknown/low until source and tests are inspected.

## What We Should Reuse

- UI interaction ideas only.

## What We Should Reimplement

- Backend architecture, data, risk, trading.

## What We Should Avoid

- Treating polished app shape as evidence of financial correctness.

