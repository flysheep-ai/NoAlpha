# ADR-003 Event Architecture

## Context

Market monitoring eventually needs event triggers, replay, and worker orchestration.

## Options

- In-process events.
- Postgres outbox/jobs.
- Redis Streams.
- Kafka/Redpanda.

## Evidence

Nautilus uses a high-performance message bus (`crates/common/src/msgbus/core.rs`), but Phase 1 requirements are not hot-path.

## Trade-offs

Kafka is powerful but costly. In-process events are simple but weak for replay.

## Decision

Use Postgres event/outbox for Phase 1-2. Revisit Redis/Kafka in Phase 3.

## Rejected Alternatives

Kafka from day one.

## Confidence

Medium.

## Revisit Condition

Need multi-worker real-time monitoring or replaying high-volume streams.

