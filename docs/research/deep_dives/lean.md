# QuantConnect Lean Deep Dive

## Repository

https://github.com/QuantConnect/Lean

Current activity observed locally: `a4bea04 2026-07-02 Auto-subscribe symbols when registering indicators and consolidators (#9582)`.

## Architecture

Institutional-grade algorithmic trading engine in C# with Python/C# strategy support. It has distinct engine, data feed, transaction handler, brokerage, risk, execution, portfolio, and market hours modules.

## Core Modules

- `Engine/Engine.cs`: main job runner and handler orchestration.
- `Engine/AlgorithmManager.cs`: time-slice algorithm loop.
- `Engine/TransactionHandlers/BrokerageTransactionHandler.cs`: order queues, order events, tickets.
- `Engine/DataFeeds/FileSystemDataFeed.cs`: historical file data feed and subscription enumerators.
- `Brokerages/Backtesting/BacktestingBrokerage.cs`: simulated brokerage.
- `Brokerages/Paper/PaperBrokerage.cs`: paper trading brokerage over backtesting mechanics.
- `Common/Orders/Order.cs`: order model.
- `Common/Orders/OrderEvent.cs`: order state change event.
- `Common/Securities/SecurityExchangeHours.cs`: market sessions/holidays.
- `Algorithm.Framework/Risk/*.cs`: risk management models.
- `Algorithm.Framework/Execution/*.cs`: execution models.

## Execution Flow

`Engine.Run` creates algorithm instance, object store, data permissions, brokerage, security service, data manager, subscriptions, synchronizer, then `AlgorithmManager.Run` streams time slices and invokes user algorithm events. Orders are processed via `BrokerageTransactionHandler`.

## Data Flow

Data provider/cache -> subscription enumerators -> synchronizer/time slices -> algorithm -> transactions -> brokerage -> order events -> portfolio/results.

## Strengths

- Complete order lifecycle with order IDs, tickets, status updates, fills.
- Market hours and corporate action ecosystem.
- Backtest/paper/live concepts are first-class.
- Risk/execution/portfolio framework exists.

## Weaknesses

- Heavy for a solo Phase 1 research tool.
- Data format and local setup have learning curve.
- Python Agent integration should remain outside engine.

## Production Readiness

High as a trading/backtesting engine; too heavy as first research prototype.

## What We Should Reuse

- Order and `OrderEvent` model.
- Separation of algorithm, data feed, brokerage, transaction handler.
- Risk model interface ideas.
- Market calendar handling.

## What We Should Reimplement

- Research Agent layer.
- Evidence/thesis layer.
- Simple initial backtester if Lean setup is too slow.

## What We Should Avoid

- Embedding LLM calls inside the time-critical Lean event loop.

