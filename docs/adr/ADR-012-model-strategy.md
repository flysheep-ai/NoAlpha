# ADR-012 Model Strategy

## Context

Need choose between general LLM + RAG, finance LLM, fine-tuning, and local models.

## Options

- General frontier model + RAG/tools.
- FinGPT/finance-tuned models.
- Fine-tune custom model.
- Local small model only.

## Evidence

FinGPT source is research/training heavy, not production-ready. FinanceBench/Fin-RATE show retrieval and temporal comparison are hard.

## Trade-offs

Fine-tuning early distracts from data correctness. General models with tools are faster.

## Decision

Use general LLM + structured tools + RAG; no fine-tuning in Phase 1-2.

## Rejected Alternatives

Training a financial LLM before data/eval maturity.

## Confidence

High.

## Revisit Condition

After internal benchmark shows persistent domain failures not solvable by retrieval/tools.

