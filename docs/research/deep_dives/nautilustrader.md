# NautilusTrader Deep Dive

## Repository

https://github.com/nautechsystems/nautilus_trader

Current activity observed locally: `692410c 2026-07-06 Fix DeFi replay crossed-tick drift`.

## Architecture

Rust-first event-driven trading platform with Python bindings. Core is organized into crates for backtest, common/message bus, execution, adapters, model, and cache.

## Core Modules

- `crates/backtest/src/*`: backtest engine, simulated exchange, execution client.
- `crates/common/src/msgbus/core.rs`: typed and Any-based message bus.
- `crates/common/src/messages/execution/submit.rs`: submit order commands with command/correlation IDs.
- `crates/execution/src/engine/mod.rs`: execution engine.
- `crates/execution/src/matching_engine/engine.rs`: matching engine.
- `crates/execution/src/order_manager/manager.rs`: order lifecycle and contingency handling.
- `crates/execution/src/protection.rs`: protection price calculation.
- `crates/adapters/*`: exchange/broker adapters including Interactive Brokers paths.

## Execution Flow

Strategies/clients publish commands on the message bus; execution engine/order manager route commands through risk/emulation/execution; matching engines or adapters produce order events; cache and portfolio consume state updates.

## Data Flow

Market data and execution events are routed through a typed message bus for hot paths, with Any-based routing for extensibility/Python interop.

## Strengths

- Very serious event-driven architecture.
- Strong order and execution abstractions.
- Performance-conscious message bus.
- Good testing footprint and adapter test data.

## Weaknesses

- More complex than needed for Phase 1-3.
- Rust/Python mixed stack raises integration burden.
- License and adapter maturity must be reviewed.

## Production Readiness

High for experienced quant engineering teams; medium/low for solo early project adoption.

## What We Should Reuse

- Message bus design concepts.
- Command IDs, correlation IDs, causation IDs.
- Order manager lifecycle.
- Matching engine realism ideas.

## What We Should Reimplement

- A lightweight event bus first.
- Minimal OMS in Python/TypeScript before adopting full engine.

## What We Should Avoid

- Prematurely adopting Nautilus as core before data/research needs are stable.

