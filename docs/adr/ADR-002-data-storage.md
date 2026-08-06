# ADR-002 Data Storage

## Context

Need raw documents, structured facts, event metadata, time series, vectors, and audit.

## Options

- Postgres-only.
- Postgres + Parquet/Object storage.
- Full stack: Postgres + ClickHouse + Vector DB + Graph DB + Kafka.

## Evidence

SEC spike produced large compressed JSON; EdgarTools supports local storage. First phase does not require high-frequency streaming.

## Trade-offs

Postgres-only is simple but poor for immutable raw files and large bars. Full stack is premature.

## Decision

Use Postgres + filesystem/MinIO-compatible object storage + Parquet/DuckDB for Phase 1.

## Rejected Alternatives

ClickHouse/Neo4j/Kafka in Phase 1.

## Confidence

Medium.

## Revisit Condition

When minute/tick data volume or graph queries exceed simple storage.

