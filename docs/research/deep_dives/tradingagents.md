# TradingAgents Deep Dive

## Repository

https://github.com/TauricResearch/TradingAgents

Current activity observed locally: `01477f9 2026-07-05 chore: release v0.3.1`.

## Architecture

TradingAgents is a LangGraph-based multi-agent research workflow. It is not a production OMS or broker execution engine.

## Core Modules

- `main.py`: likely CLI/demo entry.
- `tradingagents/graph/trading_graph.py`: top-level orchestrator, LLM clients, tool nodes, memory log, graph compile.
- `tradingagents/graph/setup.py`: constructs `StateGraph(AgentState)`.
- `tradingagents/graph/conditional_logic.py`: routes analyst tool calls, bull/bear debate, risk debate.
- `tradingagents/agents/analysts/*.py`: market/news/fundamental/sentiment agents.
- `tradingagents/agents/researchers/*.py`: bull/bear researchers.
- `tradingagents/agents/risk_mgmt/*.py`: aggressive/conservative/neutral debators.
- `tradingagents/agents/utils/core_stock_tools.py`: LangChain tool wrappers.
- `tradingagents/dataflows/*.py`: vendor integrations.
- `tests/test_checkpoint_resume.py`, `tests/test_news_lookahead.py`, `tests/test_yfinance_stale_ohlcv_guard.py`: important hardening tests.

## Important Source Files

- `tradingagents/graph/trading_graph.py`: creates `ToolNode`s for market/social/news/fundamentals, LLM clients, checkpointer.
- `tradingagents/graph/setup.py`: adds analyst nodes sequentially, then Bull/Bear -> Research Manager -> Trader -> Risk Analysts -> Portfolio Manager.
- `tradingagents/graph/conditional_logic.py`: string-based debate routing and max rounds.
- `tradingagents/agents/utils/core_stock_tools.py`: `get_stock_data` delegates to `route_to_vendor`.

## Execution Flow

`TradingAgentsGraph.__init__` creates LLMs, tool nodes, `GraphSetup`, `Propagator`, `Reflector`, `SignalProcessor`, then compiles the StateGraph. Analysts run in selected sequence; tool calls loop until no tools are needed; bull/bear debate and risk debate follow.

## Data Flow

Agent tools call abstract functions that route to vendors. Data is not persisted as a canonical PIT store by default; it is used as tool output inside graph state and reports.

## Strengths

- Concrete reference for multi-agent investment committee workflow.
- Good role taxonomy: analyst, bull/bear, risk, portfolio manager.
- Uses LangGraph state machine rather than free-form chat.
- Tests mention checkpoint, no-data handling, lookahead, stale OHLCV guard.

## Weaknesses

- Risk agents are still LLM debators, not deterministic risk engine.
- String speaker labels in `conditional_logic.py` are brittle.
- Vendor data flow is tool-oriented, not warehouse/PIT-oriented.
- Does not solve broker safety, OMS, paper/live execution.

## Production Readiness

Research workflow: medium.

Trading/execution: low for live trading.

## What We Should Reuse

- Role decomposition.
- Bull/Bear/Critic style debate.
- LangGraph graph pattern if Phase 2 complexity warrants it.
- Tests around stale data and lookahead as inspiration.

## What We Should Reimplement

- Data storage, source registry, PIT ingestion.
- Risk engine as deterministic rules.
- OMS/order model.
- Evidence pack schema.

## What We Should Avoid

- Treating portfolio manager LLM output as trade authorization.
- Letting debate output become signal without quant validation.

