# FinGPT Deep Dive

## Repository

https://github.com/AI4Finance-Foundation/FinGPT

Current activity observed locally: `608a496 2026-06-01 Merge pull request #244 from octo-patch/feature/upgrade-minimax-m3`.

## Architecture

Collection of financial LLM datasets, training scripts, benchmarks, forecasters, RAG demos, and notebooks.

## Core Modules

- `fingpt/FinGPT_Benchmark/benchmarks/benchmarks.py`: PEFT model benchmark runner.
- `fingpt/FinGPT_Forecaster/data_pipeline.py`: data acquisition, GPT-4 prompt generation, dataset creation.
- `fingpt/FinGPT_Forecaster/prompt.py`: prompt templates using Finnhub/yfinance/news.
- `fingpt/FinGPT_FinancialReportAnalysis/utils/rag.py`: RAPTOR-style clustering/RAG utility.
- `fingpt/FinGPT_FinancialReportAnalysis/utils/earning_calls.py`: earnings transcript API helper.

## Execution Flow

Mostly notebook/script-based: acquire data, generate prompts/labels, fine-tune/evaluate models, run demos.

## Data Flow

External APIs such as Finnhub/yfinance/discountingcashflows -> CSV/prompts -> model training/evaluation.

## Strengths

- Useful benchmark and prompt ideas.
- Shows financial LLM data curation patterns.
- Has RAG and report-analysis experiments.

## Weaknesses

- Not a production library.
- Many scripts are research/demo style.
- Some data providers/API assumptions may be stale or paid.
- Forecasting prompts risk leakage if not carefully timestamped.

## Production Readiness

Low as a dependency; medium as research reference.

## What We Should Reuse

- Benchmark awareness.
- Financial report prompt patterns after rewriting.
- RAG clustering idea only after evaluation.

## What We Should Reimplement

- All production ingestion, RAG, evaluation, and model serving.

## What We Should Avoid

- Fine-tuning early.
- Copying forecast prompts that include future label framing.

