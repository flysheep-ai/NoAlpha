# ADR-008 Vector Database

## Context

Need retrieval over filings, calls, and news.

## Options

- No vector DB, BM25/FTS only.
- pgvector.
- Qdrant/Milvus/Weaviate.

## Evidence

Finance RAG papers show retrieval matters, but SEC numbers should use structured data. No internal RAG benchmark yet.

## Trade-offs

pgvector is simple; standalone vector DB may be premature.

## Decision

Use Postgres FTS + optional pgvector in Phase 1.

## Rejected Alternatives

Standalone vector DB before benchmark.

## Confidence

Medium.

## Revisit Condition

When corpus size/latency/recall exceeds pgvector/FTS.

