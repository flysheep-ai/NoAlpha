from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from statistics import mean, pstdev

from noalpha.domain import DetectedEvent, EventType, Evidence, EvidenceType, Instrument, stable_id
from noalpha.quality import comparable_period_key


@dataclass(frozen=True)
class EventDetectionConfig:
    abnormal_return_abs: Decimal = Decimal("0.08")
    abnormal_volume_ratio: Decimal = Decimal("3.0")
    abnormal_return_zscore: float = 2.0
    abnormal_volume_zscore: float = 2.0
    news_burst_count: int = 3
    financial_change_abs: Decimal = Decimal("0.20")


def detect_events(instrument: Instrument, evidence: list[Evidence], config: EventDetectionConfig | None = None) -> tuple[DetectedEvent, ...]:
    cfg = config or EventDetectionConfig()
    events: list[DetectedEvent] = []
    events.extend(_detect_market_events(instrument, evidence, cfg))
    events.extend(_detect_new_filings(instrument, evidence))
    events.extend(_detect_financial_changes(instrument, evidence, cfg))
    events.extend(_detect_news_bursts(instrument, evidence, cfg))
    events.sort(key=lambda event: (event.event_time, event.event_id))
    return tuple(events)


def _detect_market_events(instrument: Instrument, evidence: list[Evidence], cfg: EventDetectionConfig) -> list[DetectedEvent]:
    bars = [item for item in evidence if item.evidence_type == EvidenceType.MARKET_BAR]
    bars.sort(key=lambda item: item.event_time)
    events: list[DetectedEvent] = []
    returns: list[float] = []
    volumes: list[int] = []
    for idx in range(1, len(bars)):
        prev = bars[idx - 1]
        curr = bars[idx]
        prev_close = Decimal(str(prev.metadata["close"]))
        curr_close = Decimal(str(curr.metadata["close"]))
        if prev_close == 0:
            continue
        ret = (curr_close / prev_close) - Decimal("1")
        volume = int(curr.metadata["volume"])
        prev_volume = max(int(prev.metadata["volume"]), 1)
        volume_ratio = Decimal(volume) / Decimal(prev_volume)
        returns.append(float(ret))
        volumes.append(volume)
        return_z = _zscore(float(ret), returns[:-1])
        volume_z = _zscore(float(volume), [float(v) for v in volumes[:-1]])
        refs = (prev.evidence_id, curr.evidence_id)
        if abs(ret) >= cfg.abnormal_return_abs or abs(return_z) >= cfg.abnormal_return_zscore:
            events.append(
                DetectedEvent(
                    event_id=stable_id("event", "return", instrument.symbol, curr.event_time.isoformat()),
                    event_type=EventType.ABNORMAL_RETURN,
                    instrument=instrument,
                    event_time=curr.event_time,
                    description=f"{instrument.symbol} moved {ret:.2%} versus previous bar.",
                    severity=min(1.0, float(abs(ret) / cfg.abnormal_return_abs)),
                    evidence_refs=refs,
                    metadata={"return": str(ret), "return_zscore": return_z},
                )
            )
        if volume_ratio >= cfg.abnormal_volume_ratio or volume_z >= cfg.abnormal_volume_zscore:
            events.append(
                DetectedEvent(
                    event_id=stable_id("event", "volume", instrument.symbol, curr.event_time.isoformat()),
                    event_type=EventType.ABNORMAL_VOLUME,
                    instrument=instrument,
                    event_time=curr.event_time,
                    description=f"{instrument.symbol} volume was {volume_ratio:.2f}x previous bar.",
                    severity=min(1.0, float(volume_ratio / cfg.abnormal_volume_ratio)),
                    evidence_refs=refs,
                    metadata={"volume_ratio": str(volume_ratio), "volume_zscore": volume_z},
                )
            )
    return events


def _detect_new_filings(instrument: Instrument, evidence: list[Evidence]) -> list[DetectedEvent]:
    events = []
    for item in evidence:
        if item.evidence_type != EvidenceType.FILING:
            continue
        form = item.metadata.get("filing_type")
        if form in {"10-K", "10-Q", "8-K", "6-K", "20-F"}:
            events.append(
                DetectedEvent(
                    event_id=stable_id("event", "filing", instrument.symbol, item.evidence_id),
                    event_type=EventType.NEW_FILING,
                    instrument=instrument,
                    event_time=item.event_time,
                    description=f"New important filing: {form}.",
                    severity=0.6 if form == "8-K" else 0.8,
                    evidence_refs=(item.evidence_id,),
                    metadata={"filing_type": form},
                )
            )
    return events


def _detect_financial_changes(instrument: Instrument, evidence: list[Evidence], cfg: EventDetectionConfig) -> list[DetectedEvent]:
    by_comparable: dict[tuple, list[Evidence]] = defaultdict(list)
    for item in evidence:
        if item.evidence_type != EvidenceType.FINANCIAL_FACT:
            continue
        metric = str(item.metadata.get("canonical_metric") or item.metadata.get("concept"))
        if metric not in {"revenue", "net_income", "operating_income"}:
            continue
        by_comparable[comparable_period_key(item)].append(item)
    events: list[DetectedEvent] = []
    for comparable_key, facts in by_comparable.items():
        facts.sort(key=lambda item: item.event_time)
        if len(facts) < 2:
            continue
        prev, curr = _latest_distinct_period_pair(facts)
        if not prev or not curr:
            continue
        prev_value = Decimal(str(prev.metadata.get("value")))
        curr_value = Decimal(str(curr.metadata.get("value")))
        if prev_value == 0:
            continue
        change = (curr_value / prev_value) - Decimal("1")
        if abs(change) >= cfg.financial_change_abs:
            metric = str(curr.metadata.get("canonical_metric") or curr.metadata.get("concept"))
            events.append(
                DetectedEvent(
                    event_id=stable_id("event", "financial_change", instrument.symbol, comparable_key, curr.evidence_id),
                    event_type=EventType.MAJOR_FINANCIAL_CHANGE,
                    instrument=instrument,
                    event_time=curr.event_time,
                    description=f"{metric} changed {change:.2%} versus previous comparable reported fact.",
                    severity=min(1.0, float(abs(change) / cfg.financial_change_abs)),
                    evidence_refs=(prev.evidence_id, curr.evidence_id),
                    metadata={"metric": metric, "change": str(change), "comparable_period_key": list(comparable_key)},
                )
            )
    return _limit_financial_events(events)


def _latest_distinct_period_pair(facts: list[Evidence]) -> tuple[Evidence | None, Evidence | None]:
    if len(facts) < 2:
        return None, None
    curr = facts[-1]
    curr_period = curr.metadata.get("period_end")
    for candidate in reversed(facts[:-1]):
        if candidate.metadata.get("period_end") != curr_period:
            return candidate, curr
    return None, None


def _limit_financial_events(events: list[DetectedEvent]) -> list[DetectedEvent]:
    by_metric: dict[str, DetectedEvent] = {}
    for event in events:
        metric = str(event.metadata.get("metric") or "")
        previous = by_metric.get(metric)
        if previous is None or (event.event_time, event.severity) > (previous.event_time, previous.severity):
            by_metric[metric] = event
    return sorted(by_metric.values(), key=lambda event: (event.event_time, event.event_id))


def _detect_news_bursts(instrument: Instrument, evidence: list[Evidence], cfg: EventDetectionConfig) -> list[DetectedEvent]:
    by_day: dict[str, list[Evidence]] = defaultdict(list)
    for item in evidence:
        if item.evidence_type == EvidenceType.NEWS:
            by_day[item.event_time.date().isoformat()].append(item)
    events: list[DetectedEvent] = []
    for day, items in by_day.items():
        if len(items) >= cfg.news_burst_count:
            events.append(
                DetectedEvent(
                    event_id=stable_id("event", "news_burst", instrument.symbol, day),
                    event_type=EventType.NEWS_BURST,
                    instrument=instrument,
                    event_time=min(item.event_time for item in items),
                    description=f"{len(items)} news items appeared for {instrument.symbol} on {day}.",
                    severity=min(1.0, len(items) / (cfg.news_burst_count * 2)),
                    evidence_refs=tuple(item.evidence_id for item in items),
                    metadata={"count": len(items), "day": day},
                )
            )
    return events


def _zscore(value: float, history: list[float]) -> float:
    if len(history) < 5:
        return 0.0
    sigma = pstdev(history)
    if sigma == 0:
        return 0.0
    return (value - mean(history)) / sigma
