# Microsoft Qlib Deep Dive

## Repository

https://github.com/microsoft/qlib

Current activity observed locally: `d5379c5 2026-04-22 docs: replace broken RD-Agent demo links in README (#2150)`.

## Architecture

AI-oriented quant research platform: data providers, calendars, datasets, workflow/recorder, model training, signal strategy, backtesting.

## Core Modules

- `qlib/data/data.py`: calendar/provider abstractions and cached data access.
- `qlib/workflow/recorder.py`: MLflow-like experiment recorder.
- `qlib/backtest/backtest.py`: backtest loop and collect-data loop.
- `qlib/contrib/strategy/signal_strategy.py`: signal-to-trade-decision strategy.
- `examples/benchmarks/*`: many model configs and benchmark workflows.
- `docs/advanced/PIT.rst`: PIT documentation exists and needs deeper reading.

## Execution Flow

Model/dataset produce signals; strategy consumes signals and generates trade decisions; executor/backtest loop simulates execution and collects portfolio/indicator metrics.

## Data Flow

Qlib data provider -> dataset/feature handler -> model -> signal -> strategy -> executor/backtest -> recorder/report.

## Strengths

- Good for factor/ML alpha research.
- Has experiment recorder discipline.
- Has PIT documentation and A-share oriented benchmark ecosystem.

## Weaknesses

- Not a live trading/OMS platform.
- Complex data format.
- Does not solve LLM research, news, SEC parsing.

## Production Readiness

Medium for research, low for execution.

## What We Should Reuse

- Experiment recorder concepts.
- Dataset/feature pipeline ideas.
- Signal strategy separation.

## What We Should Reimplement

- Data ingestion and unified metadata.
- Agent/research workflow.
- Trading OMS/risk.

## What We Should Avoid

- Treating ML score as automatically tradable without PIT and cost validation.

