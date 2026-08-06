from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProviderHealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNAUTHORIZED = "unauthorized"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    status: ProviderHealthStatus
    requires_credentials: bool
    supports_historical: bool
    supports_realtime: bool
    supports_intraday: bool
    coverage: str
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "status": self.status.value,
            "requires_credentials": self.requires_credentials,
            "supports_historical": self.supports_historical,
            "supports_realtime": self.supports_realtime,
            "supports_intraday": self.supports_intraday,
            "coverage": self.coverage,
            "reason": self.reason,
        }
