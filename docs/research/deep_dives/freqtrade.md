# Freqtrade Deep Dive

## Repository

https://github.com/freqtrade/freqtrade

Current activity observed locally: `4de16ac 2026-07-06 Merge pull request #13323 from freqtrade/dependabot/pip/develop/fastapi-0.138.1`.

## Architecture

Crypto trading bot with exchange adapters, backtesting, protections, persistence, API server, and FreqAI. Not directly a stock system, but useful trading-engine reference.

## Core Modules

- `freqtrade/exchange/exchange.py`: CCXT/ccxt.pro exchange abstraction, retries, capabilities.
- `freqtrade/optimize/backtesting.py`: backtesting engine.
- `freqtrade/plugins/protectionmanager.py`: protection layer, source read pending.
- `freqtrade/persistence/trade_model.py`: trade persistence, source read pending.
- `docs/lookahead-analysis.md`: lookahead tool.
- `docs/includes/protections.md`: protection docs.

## Execution Flow

Strategy produces entry/exit signals; exchange adapter executes or dry-runs; persistence stores trades; protections can lock pairs/trading; backtesting simulates signals over historical data.

## Data Flow

Exchange OHLCV/trades/order book -> data provider -> strategy/backtesting -> trade/order models -> API/UI.

## Strengths

- Mature operational bot patterns.
- Explicit exchange capability matrix.
- Dry-run and backtesting are first-class.
- Lookahead analysis is directly relevant.

## Weaknesses

- Crypto-specific.
- Stock market sessions, corporate actions, fundamentals not core.
- Not designed for LLM research.

## Production Readiness

High in its crypto domain; medium as inspiration for stocks.

## What We Should Reuse

- Exchange capability flags.
- Dry-run discipline.
- Lookahead-analysis concept.
- Protection/lock concept.

## What We Should Reimplement

- Stock broker adapter.
- Stock calendar/corporate actions.
- Research/Agent layer.

## What We Should Avoid

- Copying crypto assumptions into equities.

