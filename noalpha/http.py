from __future__ import annotations

import gzip
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from noalpha.errors import MalformedResponseError, ProviderError, ProviderTimeoutError, RateLimitError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    latency_ms: int
    retry_count: int
    from_cache: bool = False

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MalformedResponseError(f"Malformed JSON from {self.url}: {exc}") from exc

    def text(self) -> str:
        try:
            return self.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedResponseError(f"Malformed text from {self.url}: {exc}") from exc


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 20.0,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
        cache_dir: Path | None = None,
        min_interval_sec: float = 0.0,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.cache_dir = cache_dir
        self.min_interval_sec = min_interval_sec
        self._last_request_at = 0.0
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cache_key: str | None = None,
        cache_ttl_seconds: float | None = None,
    ) -> HttpResponse:
        cache_path = self._cache_path(cache_key) if cache_key else None
        if cache_path and cache_path.exists():
            cached = self._read_cache(cache_path, cache_ttl_seconds)
            if cached is not None:
                return HttpResponse(url, 200, cached.get("headers", {}), cached["body"], 0, 0, from_cache=True)

        merged_headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
        }
        if headers:
            merged_headers.update(headers)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            started = time.monotonic()
            try:
                req = urllib.request.Request(url, headers=merged_headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    raw = response.read()
                    status = int(response.status)
                    response_headers = {k: v for k, v in response.headers.items()}
                body = self._decode_body(raw, response_headers)
                latency_ms = int((time.monotonic() - started) * 1000)
                logger.info(
                    "provider_request_ok",
                    extra={"provider": "http", "latency_ms": latency_ms, "retry_count": attempt},
                )
                if cache_path:
                    self._write_cache(cache_path, body, response_headers)
                return HttpResponse(url, status, response_headers, body, latency_ms, attempt)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 429:
                    if attempt >= self.max_retries:
                        raise RateLimitError(f"Rate limited by provider for {url}") from exc
                    time.sleep(self.retry_backoff * (attempt + 1))
                    continue
                if 500 <= exc.code < 600 and attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (attempt + 1))
                    continue
                raise ProviderError(f"HTTP {exc.code} from {url}") from exc
            except TimeoutError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise ProviderTimeoutError(f"Timeout fetching {url}") from exc
                time.sleep(self.retry_backoff * (attempt + 1))
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    if isinstance(exc.reason, TimeoutError):
                        raise ProviderTimeoutError(f"Timeout fetching {url}") from exc
                    raise ProviderError(f"Network error fetching {url}: {exc}") from exc
                time.sleep(self.retry_backoff * (attempt + 1))
        raise ProviderError(f"Provider failed for {url}: {last_error}")

    def _cache_path(self, cache_key: str) -> Path | None:
        if not self.cache_dir:
            return None
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in cache_key)
        return self.cache_dir / safe

    def _read_cache(self, cache_path: Path, ttl_seconds: float | None) -> dict[str, Any] | None:
        try:
            raw = cache_path.read_bytes()
            if raw.startswith(b"{"):
                data = json.loads(raw.decode("utf-8"))
                fetched_at = float(data.get("fetched_at", 0))
                if ttl_seconds is not None and time.time() - fetched_at > ttl_seconds:
                    return None
                return {
                    "headers": data.get("headers", {}),
                    "body": bytes.fromhex(data["body_hex"]),
                }
            if ttl_seconds is not None and time.time() - cache_path.stat().st_mtime > ttl_seconds:
                return None
            return {"headers": {}, "body": raw}
        except Exception:
            return None

    def _write_cache(self, cache_path: Path, body: bytes, headers: dict[str, str]) -> None:
        payload = {
            "fetched_at": time.time(),
            "headers": headers,
            "body_hex": body.hex(),
        }
        cache_path.write_text(json.dumps(payload, sort_keys=True))

    def _decode_body(self, raw: bytes, headers: dict[str, str]) -> bytes:
        encoding = headers.get("Content-Encoding", "").lower()
        if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
            return gzip.decompress(raw)
        return raw

    def _throttle(self) -> None:
        if self.min_interval_sec <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval_sec:
            time.sleep(self.min_interval_sec - elapsed)
        self._last_request_at = time.monotonic()
