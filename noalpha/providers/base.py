from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from noalpha.domain import FinancialFact, Filing, Instrument, MarketBar, NewsItem


class MarketDataProvider(ABC):
    @abstractmethod
    def resolve_instrument(self, symbol: str) -> Instrument:
        raise NotImplementedError

    @abstractmethod
    def get_bars(self, instrument: Instrument, start: datetime, end: datetime) -> list[MarketBar]:
        raise NotImplementedError


class FilingProvider(ABC):
    @abstractmethod
    def get_filings(self, instrument: Instrument, limit: int = 10) -> list[Filing]:
        raise NotImplementedError

    @abstractmethod
    def get_company_facts(self, instrument: Instrument) -> list[FinancialFact]:
        raise NotImplementedError


class NewsProvider(ABC):
    @abstractmethod
    def search_news(self, instrument: Instrument, start: datetime, end: datetime, limit: int = 20) -> list[NewsItem]:
        raise NotImplementedError
