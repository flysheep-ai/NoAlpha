from __future__ import annotations

from dataclasses import dataclass, field

from noalpha.domain import AsOfContext, Evidence


@dataclass
class InMemoryEvidenceStore:
    _items: dict[str, Evidence] = field(default_factory=dict)

    def add_many(self, evidence: list[Evidence]) -> None:
        for item in evidence:
            self._items[item.evidence_id] = item

    def all(self) -> list[Evidence]:
        return list(self._items.values())

    def as_of(self, context: AsOfContext) -> list[Evidence]:
        return [item for item in self._items.values() if context.allows(item.available_at)]

    def get(self, evidence_id: str) -> Evidence | None:
        return self._items.get(evidence_id)
