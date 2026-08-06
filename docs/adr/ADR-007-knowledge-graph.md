# ADR-007 Knowledge Graph

## Context

Need industry/supply-chain reasoning.

## Options

- No graph.
- Relational edge table.
- Graph DB/GraphRAG.

## Evidence

First round only gave theoretical value. No spike proves graph DB necessity.

## Trade-offs

Graph DB helps multi-hop queries but adds entity resolution and ops burden.

## Decision

Use relational temporal edge table first. Graph DB is NEEDS_MORE_RESEARCH.

## Rejected Alternatives

Neo4j/Kuzu in Phase 1.

## Confidence

Medium.

## Revisit Condition

After edge-table query pain or GraphRAG spike shows clear benefit.

