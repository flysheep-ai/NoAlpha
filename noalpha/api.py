from __future__ import annotations

import json
import os
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from noalpha.domain import AsOfContext, parse_datetime
from noalpha.env import load_env
from noalpha.llm.config import LLMConfig
from noalpha.pipeline import InvestigationPipeline, InvestigationResult
from noalpha.progress import PersistentProgressReporter
from noalpha.research_mode import ResearchMode
from noalpha.provider_health import ProviderHealth
from noalpha.conversation import (
    ChatAgent, ConversationStore, event_bus, register_conversation_routes,
)
from noalpha.market import market_gateway
from noalpha.market.routes import register_market_routes
from noalpha.quant.routes import register_quant_routes
from noalpha.market.ws import register_market_ws
from noalpha.providers import MarketDataAppPrototypeMarketDataProvider, SecFilingProvider, YahooFinanceNewsProvider, YahooPrototypeMarketDataProvider
from noalpha.trading.routes import register_trading_routes
from noalpha.broker.routes import register_broker_routes
from noalpha.repositories import SQLiteRepository
from noalpha.watch import AutonomousWatchService
from noalpha.watch_models import ScanPolicy


class InvestigationCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    lookback_days: int = Field(default=60, ge=1, le=3650)
    as_of: str | None = None
    research_mode: str = "deterministic"
    report_language: str = "en-US"


class CredentialUpdateBody(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(default="", max_length=512)  # empty value clears the credential


class WatchAddRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    watchlist: str = "default"
    lookback_days: int = Field(default=60, ge=1, le=3650)
    return_threshold: float = 0.06
    volume_threshold: float = 2.5
    news_burst_threshold: int = 3
    cooldown_seconds: int = 21600
    priority: int = 0


class WatchRunRequest(BaseModel):
    watchlist: str = "default"
    as_of: str | None = None


@dataclass
class InvestigationJob:
    investigation_id: str
    symbol: str
    status: str
    created_at: datetime
    updated_at: datetime
    steps: list[dict[str, Any]]
    result: dict[str, Any] | None = None
    error: str | None = None
    research_mode: str = "deterministic"
    llm_model: str = ""
    llm_provider: str = ""
    progress: dict[str, Any] | None = None
    _reporter: Any = None

    def to_dict(self, include_result: bool = True) -> dict[str, Any]:
        payload = {
            "investigation_id": self.investigation_id,
            "symbol": self.symbol,
            "status": self.status,
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
            "steps": self.steps,
            "error": self.error,
            "research_mode": self.research_mode,
            "llm_model": self.llm_model,
            "llm_provider": self.llm_provider,
            "progress": self.progress,
        }
        if include_result:
            payload["result"] = self.result
        elif self.result:
            payload["report"] = self.result.get("report")
            payload["quality_report"] = self.result.get("quality_report")
            payload["provider_warnings"] = self.result.get("provider_warnings", [])
            payload["evidence_count"] = len(self.result.get("evidence", []))
        return payload


@dataclass
class ApiState:
    state_dir: Path
    lock: threading.Lock = field(default_factory=threading.Lock)
    jobs: dict[str, InvestigationJob] = field(default_factory=dict)

    @property
    def investigations_dir(self) -> Path:
        path = self.state_dir / "investigations"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def provider_cache_dir(self) -> Path:
        path = self.state_dir / "providers"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def watch_db_path(self) -> Path:
        path = self.state_dir / "watch.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def load(self) -> None:
        for path in self.investigations_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text())
                created = parse_datetime(raw["created_at"])
                updated = parse_datetime(raw["updated_at"])
                if created and updated:
                    self.jobs[raw["investigation_id"]] = InvestigationJob(
                        investigation_id=raw["investigation_id"],
                        symbol=raw["symbol"],
                        status=raw["status"],
                        created_at=created,
                        updated_at=updated,
                        steps=raw.get("steps", []),
                        result=raw.get("result"),
                        error=raw.get("error"),
                    )
            except Exception:
                continue

    def save(self, job: InvestigationJob) -> None:
        path = self.investigations_dir / f"{job.investigation_id}.json"
        path.write_text(json.dumps(job.to_dict(include_result=True), indent=2, sort_keys=True))


@dataclass
class CredentialStore:
    """Persist provider API keys to a JSON file so users can configure them at runtime."""

    path: Path
    allowed_keys: tuple[str, ...] = (
        "MARKETDATA_APP_TOKEN",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_TRADING_MODEL",
    )

    def __post_init__(self) -> None:
        self._data: dict[str, str] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
                self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except Exception:
                self._data = {}

    def get(self, key: str) -> str | None:
        self._validate_key(key)
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._validate_key(key)
        self._data[key] = value
        self._write()

    def delete(self, key: str) -> None:
        self._validate_key(key)
        if key in self._data:
            del self._data[key]
            self._write()

    def list_keys(self) -> dict[str, bool]:
        """Return which known credential keys are set."""
        return {k: bool(self._data.get(k)) for k in self.allowed_keys}

    @property
    def marketdata_token(self) -> str | None:
        return self.get("MARKETDATA_APP_TOKEN")

    def _validate_key(self, key: str) -> None:
        if key not in self.allowed_keys:
            raise ValueError(f"unsupported credential key: {key}")

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


def require_admin(request: Request) -> None:
    """Protect mutation and broker endpoints.

    Local desktop usage remains frictionless. If NOALPHA_ADMIN_TOKEN is set,
    or a request arrives from a non-loopback client, a Bearer token is required.
    """
    configured = os.environ.get("NOALPHA_ADMIN_TOKEN", "")
    host = request.client.host if request.client else ""
    loopback = host in {"127.0.0.1", "::1", "localhost", "testclient"}
    if not configured and loopback:
        return
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    if configured and token == configured:
        return
    raise HTTPException(status_code=401, detail="admin authorization required")


def resolve_llm_config(credentials: CredentialStore) -> LLMConfig:
    """Runtime LLM config: user-configured credentials take precedence over env vars."""
    env = LLMConfig.from_env()
    return LLMConfig(
        base_url=credentials.get("LLM_BASE_URL") or env.base_url,
        api_key=credentials.get("LLM_API_KEY") or env.api_key,
        model=credentials.get("LLM_MODEL") or env.model,
        timeout=env.timeout,
        max_tokens=env.max_tokens,
        temperature=env.temperature,
    )


def create_app(state_dir: Path | None = None) -> FastAPI:
    load_env()
    app = FastAPI(title="NoAlpha Research Console API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:5174", "http://127.0.0.1:5174",
            "http://localhost:5199", "http://127.0.0.1:5199",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    state = ApiState(state_dir or Path(os.environ.get("NOALPHA_API_STATE_DIR", ".cache/api")))
    state.state_dir.mkdir(parents=True, exist_ok=True)
    state.load()
    credentials = CredentialStore(state.state_dir / "credentials.json")
    conv_store = ConversationStore(str(state.state_dir / "conversations.db"))
    chat_agent = ChatAgent(
        store=conv_store,
        llm_config=resolve_llm_config(credentials),
        llm_config_factory=lambda: resolve_llm_config(credentials),
        investigation_lookup=lambda inv_id: state.jobs.get(inv_id),
        pipeline_factory=lambda **kwargs: app.state.pipeline_factory(**kwargs),
    )

    app.state.api_state = state
    app.state.credential_store = credentials
    app.state.conversation_store = conv_store
    app.state.chat_agent = chat_agent
    app.state.pipeline_factory = lambda **kwargs: InvestigationPipeline.default(
        cache_dir=state.provider_cache_dir, marketdata_token=credentials.marketdata_token, **kwargs
    )
    app.state.watch_service_factory = lambda: AutonomousWatchService.default(db_path=state.watch_db_path, cache_dir=state.provider_cache_dir)

    @app.get("/api/v1/system/status")
    def system_status() -> dict[str, Any]:
        repo = SQLiteRepository(state.watch_db_path)
        cases = repo.list_cases()
        watchlist = repo.get_watchlist("default") or repo.create_watchlist("default")
        items = repo.list_watch_items(watchlist.watchlist_id)
        repo.close()
        jobs = list(state.jobs.values())
        completed = [job for job in jobs if job.status == "completed"]
        return {
            "providers": provider_health(marketdata_token=credentials.marketdata_token),
            "watchlist": {"count": 1, "items": len(items), "enabled": sum(1 for item in items if item.enabled)},
            "cases": {"total": len(cases), "open": sum(1 for case in cases if case.status.value in {"OPEN", "MONITORING", "UPDATED", "DETECTED", "INVESTIGATING"})},
            "investigations": {"total": len(jobs), "completed": len(completed), "running": sum(1 for job in jobs if job.status == "running")},
            "recent_investigations": [_job_summary(job) for job in sorted(jobs, key=lambda item: item.updated_at, reverse=True)[:8]],
            "recent_cases": [_case_summary(case) for case in cases[:8]],
        }

    @app.post("/api/v1/investigations", status_code=202)
    def create_investigation(payload: InvestigationCreate, background_tasks: BackgroundTasks) -> dict[str, Any]:
        created = datetime.now(timezone.utc)
        temp_id = str(uuid4())
        requested_mode = payload.research_mode if payload.research_mode in {"deterministic", "llm", "hybrid"} else "deterministic"
        llm_cfg = resolve_llm_config(credentials)
        # Never advertise LLM mode unless an API key is actually configured.
        mode = requested_mode if (requested_mode == "deterministic" or llm_cfg.api_key) else "deterministic"
        job = InvestigationJob(
            investigation_id=temp_id,
            symbol=payload.symbol.upper(),
            status="queued",
            created_at=created,
            updated_at=created,
            steps=_initial_steps(),
            research_mode=mode,
            llm_model=llm_cfg.model if mode in ("llm", "hybrid") else "",
        )
        with state.lock:
            state.jobs[job.investigation_id] = job
            state.save(job)
        background_tasks.add_task(_run_investigation_job, app, job.investigation_id, payload)
        return {"investigation_id": job.investigation_id, "status": job.status}

    @app.get("/api/v1/investigations")
    def list_investigations() -> dict[str, Any]:
        jobs = sorted(state.jobs.values(), key=lambda item: item.updated_at, reverse=True)
        return {"items": [_job_summary(job) for job in jobs], "total": len(jobs)}

    @app.get("/api/v1/investigations/{investigation_id}")
    def get_investigation(investigation_id: str) -> dict[str, Any]:
        job = _get_job(state, investigation_id)
        result = job.to_dict(include_result=False)
        # Include live progress from running reporter
        if job._reporter is not None and job.status == "running":
            try:
                result["progress"] = job._reporter.progress.to_dict()
            except Exception:
                pass
        return result

    @app.get("/api/v1/investigations/{investigation_id}/evidence")
    def get_evidence(
        investigation_id: str,
        evidence_type: str | None = None,
        source: str | None = None,
        evidence_id: list[str] = Query(default=[]),
        min_importance: float | None = None,
        availability_uncertain: bool | None = None,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        job = _get_completed_job(state, investigation_id)
        evidence = list(job.result.get("evidence", [])) if job.result else []
        if evidence_id:
            ids = set(evidence_id)
            evidence = [item for item in evidence if item.get("evidence_id") in ids]
        if evidence_type:
            evidence = [item for item in evidence if item.get("evidence_type") == evidence_type]
        if source:
            evidence = [item for item in evidence if source.lower() in str(item.get("source", "")).lower()]
        if min_importance is not None:
            evidence = [item for item in evidence if float(item.get("metadata", {}).get("importance_score") or 0.0) >= min_importance]
        if availability_uncertain is not None:
            evidence = [item for item in evidence if bool(item.get("metadata", {}).get("availability_uncertain")) == availability_uncertain]
        total = len(evidence)
        return {"items": evidence[offset : offset + limit], "total": total, "limit": limit, "offset": offset}

    @app.get("/api/v1/investigations/{investigation_id}/timeline")
    def get_timeline(investigation_id: str) -> dict[str, Any]:
        job = _get_completed_job(state, investigation_id)
        report = job.result.get("report", {}) if job.result else {}
        return {"items": report.get("timeline", []), "total": len(report.get("timeline", []))}

    @app.get("/api/v1/investigations/{investigation_id}/events")
    def get_events(investigation_id: str) -> dict[str, Any]:
        job = _get_completed_job(state, investigation_id)
        report = job.result.get("report", {}) if job.result else {}
        return {"items": report.get("detected_events", []), "total": len(report.get("detected_events", []))}

    @app.get("/api/v1/providers/health")
    def providers() -> dict[str, Any]:
        return {"items": provider_health(marketdata_token=credentials.marketdata_token)}

    @app.get("/api/v1/evals")
    def evals() -> dict[str, Any]:
        return read_eval_reports()

    @app.get("/api/v1/watchlists")
    def list_watchlists() -> dict[str, Any]:
        repo = SQLiteRepository(state.watch_db_path)
        watchlist = repo.get_watchlist("default") or repo.create_watchlist("default")
        items = repo.list_watch_items(watchlist.watchlist_id)
        repo.close()
        return {"items": [{"symbol": item.instrument.symbol, "enabled": item.enabled, "priority": item.priority, "policy": item.scan_policy.to_dict(), "last_scanned_at": _dt(item.last_scanned_at), "cooldown_until": _dt(item.cooldown_until)} for item in items]}

    @app.post("/api/v1/watchlists")
    def add_watch_item(payload: WatchAddRequest) -> dict[str, Any]:
        service = app.state.watch_service_factory()
        watchlist = service.repo.get_watchlist(payload.watchlist) or service.repo.create_watchlist(payload.watchlist)
        instrument = service.market_data.resolve_instrument(payload.symbol)
        item = service.repo.add_watch_item(
            watchlist,
            instrument,
            ScanPolicy(
                lookback_days=payload.lookback_days,
                return_threshold=payload.return_threshold,
                volume_threshold=payload.volume_threshold,
                news_burst_threshold=payload.news_burst_threshold,
                cooldown_seconds=payload.cooldown_seconds,
            ),
            priority=payload.priority,
        )
        service.repo.close()
        return {"symbol": item.instrument.symbol, "enabled": item.enabled, "watchlist": payload.watchlist}

    @app.post("/api/v1/watch/run-once")
    def watch_run_once(payload: WatchRunRequest) -> dict[str, Any]:
        service = app.state.watch_service_factory()
        as_of = parse_datetime(payload.as_of) if payload.as_of else None
        summary = service.run_once(payload.watchlist, as_of=AsOfContext(as_of) if as_of else AsOfContext.now())
        service.repo.close()
        return summary.to_dict()

    @app.get("/api/v1/cases")
    def list_cases() -> dict[str, Any]:
        repo = SQLiteRepository(state.watch_db_path)
        cases = repo.list_cases()
        repo.close()
        return {"items": [_case_summary(case) for case in cases], "total": len(cases)}

    @app.get("/api/v1/cases/{case_id}")
    def get_case(case_id: str) -> dict[str, Any]:
        repo = SQLiteRepository(state.watch_db_path)
        case = repo.get_case(case_id)
        if case is None:
            repo.close()
            raise HTTPException(status_code=404, detail="case not found")
        snapshots = repo.list_snapshots(case_id)
        repo.close()
        return {"case": _case_summary(case, detailed=True), "snapshots": [snapshot.__dict__ | {"created_at": _dt(snapshot.created_at)} for snapshot in snapshots]}

    @app.get("/api/v1/settings")
    def get_settings() -> dict[str, Any]:
        token = credentials.marketdata_token
        llm = resolve_llm_config(credentials)
        llm_key_source = "credentials" if credentials.get("LLM_API_KEY") else ("env" if os.environ.get("LLM_API_KEY") else "none")
        return {
            "llm": {
                "base_url": llm.base_url,
                "model": llm.model,
                "api_key_configured": bool(llm.api_key),
                "api_key_preview": f"{llm.api_key[:6]}…{llm.api_key[-4:]}" if len(llm.api_key) > 12 else ("***" if llm.api_key else ""),
                "api_key_source": llm_key_source,
                "credential_keys": {"base_url": "LLM_BASE_URL", "api_key": "LLM_API_KEY", "model": "LLM_MODEL"},
            },
            "providers": [
                {
                    "provider": "marketdata_app",
                    "name": "MarketData.app",
                    "description": "美股日K线数据源。免费套餐每天 100 次请求，注册地址 https://www.marketdata.app/",
                    "requires_credentials": True,
                    "credential_key": "MARKETDATA_APP_TOKEN",
                    "configured": bool(token),
                    "credential_preview": f"{token[:8]}…{token[-4:]}" if token and len(token) > 16 else (token if token else ""),
                },
                {
                    "provider": "yahoo_charts",
                    "name": "Yahoo Finance Charts",
                    "description": "免费美股日K线（OHLCV），通过 Yahoo Finance chart API，无需 API Key",
                    "requires_credentials": False,
                    "credential_key": None,
                    "configured": True,
                    "credential_preview": None,
                },
                {
                    "provider": "yahoo_news",
                    "name": "Yahoo Finance RSS",
                    "description": "免费 RSS 新闻数据源，无需 API Key。注：原型数据源，非正式授权新闻 feed",
                    "requires_credentials": False,
                    "credential_key": None,
                    "configured": True,
                    "credential_preview": None,
                },
                {
                    "provider": "sec_edgar",
                    "name": "SEC EDGAR",
                    "description": "美国证监会公开财报与披露文件，无需 API Key",
                    "requires_credentials": False,
                    "credential_key": None,
                    "configured": True,
                    "credential_preview": None,
                },
            ]
        }

    @app.put("/api/v1/settings/credentials", dependencies=[Depends(require_admin)])
    def update_credential(payload: CredentialUpdateBody) -> dict[str, Any]:
        try:
            if payload.value:
                credentials.set(payload.key, payload.value)
            else:
                credentials.delete(payload.key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        app.state.pipeline_factory = lambda **kwargs: InvestigationPipeline.default(
            cache_dir=state.provider_cache_dir,
            marketdata_token=credentials.marketdata_token,
            **kwargs,
        )
        return {"ok": True, "key": payload.key, "configured": bool(payload.value)}

    # Register conversation routes
    register_conversation_routes(app, conv_store, chat_agent)
    # Register market WebSocket
    register_market_ws(app)
    # Register market REST routes (search / quotes / klines / watchlist)
    register_market_routes(app, state.watch_db_path, admin_dependency=require_admin)
    # Register trading routes
    register_trading_routes(app, state.state_dir, admin_dependency=require_admin)
    # Register quant routes (Phase Q)
    register_quant_routes(app)
    register_broker_routes(app, state.state_dir, admin_dependency=require_admin)

    @app.get("/api/v1/market/quotes")
    def market_quotes() -> dict[str, Any]:
        return {"quotes": market_gateway.get_all_latest()}

    # Set event bus loop on first request
    @app.on_event("startup")
    async def _startup() -> None:
        import asyncio
        loop = asyncio.get_running_loop()
        event_bus.set_loop(loop)
        market_gateway.set_loop(loop)
        # Track everything on the persisted watchlist (cross-market) plus indices
        watch_secids = [item["secid"] for item in app.state.market_watchlist_store.list(enabled_only=True)]
        if watch_secids:
            market_gateway.subscribe(watch_secids)

    return app


def _run_investigation_job(app: FastAPI, temp_id: str, payload: InvestigationCreate) -> None:
    state: ApiState = app.state.api_state
    with state.lock:
        job = state.jobs[temp_id]
        job.status = "running"
        job.updated_at = datetime.now(timezone.utc)
        _mark_step(job, "resolve", "running")
        state.save(job)
    try:
        as_of_dt = parse_datetime(payload.as_of) if payload.as_of else None
        context = AsOfContext(as_of_dt) if as_of_dt else AsOfContext.now()
        requested = ResearchMode(payload.research_mode) if payload.research_mode in {"deterministic", "llm", "hybrid"} else ResearchMode.DETERMINISTIC
        credentials: CredentialStore = app.state.credential_store
        llm_cfg = resolve_llm_config(credentials) if requested in (ResearchMode.LLM, ResearchMode.HYBRID) else None
        llm_active = bool(llm_cfg and llm_cfg.api_key)
        mode = requested if (requested is ResearchMode.DETERMINISTIC or llm_active) else ResearchMode.DETERMINISTIC
        fallback_warning: str | None = None
        if mode is not requested:
            fallback_warning = (
                f"Research mode '{requested.value}' was requested but no LLM API key is configured "
                "(set it in Settings → LLM, or via the LLM_API_KEY env var); "
                "this report was generated by the deterministic template engine instead."
            )
        pipeline: InvestigationPipeline = app.state.pipeline_factory(
            research_mode=mode,
            llm_config=llm_cfg if llm_active else None,
        )
        reporter = PersistentProgressReporter(job.investigation_id)
        with state.lock:
            job._reporter = reporter
            job.progress = reporter.progress.to_dict()
        _mark_step_sequence(state, job, ["resolve", "market", "sec_facts", "filings", "news", "canonicalize", "timeline", "events", "claims", "confidence"], "running")
        result: InvestigationResult = pipeline.investigate(
            payload.symbol, as_of=context, lookback_days=payload.lookback_days, progress=reporter,
            report_language=payload.report_language,
        )
        # Sync final progress to job
        with state.lock:
            job.progress = reporter.progress.to_dict()
            job._reporter = reporter
            job.updated_at = datetime.now(timezone.utc)
            state.save(job)
        result_dict = result.to_dict()
        result_dict["investigation_id"] = job.investigation_id
        result_dict["report"]["investigation_id"] = job.investigation_id
        if fallback_warning:
            result_dict["provider_warnings"] = list(result_dict.get("provider_warnings", [])) + [fallback_warning]
            report_dict = result_dict.get("report") or {}
            report_dict["unknowns"] = list(report_dict.get("unknowns", [])) + [fallback_warning]
        with state.lock:
            job.status = "completed"
            job.updated_at = datetime.now(timezone.utc)
            job.result = result_dict
            job.research_mode = mode.value
            job.llm_model = llm_cfg.model if llm_active else ""
            job.llm_provider = llm_cfg.base_url if llm_active else ""
            for step in job.steps:
                step["status"] = "completed"
            state.save(job)
    except Exception as exc:
        with state.lock:
            job = state.jobs[temp_id]
            job.status = "failed"
            job.error = str(exc)
            job.updated_at = datetime.now(timezone.utc)
            for step in job.steps:
                if step["status"] in {"queued", "running"}:
                    step["status"] = "failed"
                    break
            state.save(job)


def _initial_steps() -> list[dict[str, Any]]:
    names = [
        ("resolve", "Resolving Instrument"),
        ("market", "Fetching Market Data"),
        ("sec_facts", "Fetching SEC Facts"),
        ("filings", "Fetching Filings"),
        ("news", "Fetching News"),
        ("canonicalize", "Canonicalizing Evidence"),
        ("dedupe", "Removing Duplicates"),
        ("timeline", "Building Timeline"),
        ("events", "Detecting Events"),
        ("claims", "Generating Claims"),
        ("confidence", "Calculating Confidence"),
        ("report", "Building Report"),
    ]
    return [{"key": key, "label": label, "status": "queued"} for key, label in names]


def _mark_step(job: InvestigationJob, key: str, status: str) -> None:
    for step in job.steps:
        if step["key"] == key:
            step["status"] = status


def _mark_step_sequence(state: ApiState, job: InvestigationJob, keys: list[str], status: str) -> None:
    for key in keys:
        _mark_step(job, key, status)
    state.save(job)


def _get_job(state: ApiState, investigation_id: str) -> InvestigationJob:
    job = state.jobs.get(investigation_id)
    if not job:
        raise HTTPException(status_code=404, detail="investigation not found")
    return job


def _get_completed_job(state: ApiState, investigation_id: str) -> InvestigationJob:
    job = _get_job(state, investigation_id)
    if job.status != "completed" or not job.result:
        raise HTTPException(status_code=409, detail="investigation not completed")
    return job


def _job_summary(job: InvestigationJob) -> dict[str, Any]:
    report = job.result.get("report", {}) if job.result else {}
    quality = job.result.get("quality_report", {}) if job.result else {}
    provider_health = quality.get("provider_health", {})
    return {
        "investigation_id": job.investigation_id,
        "symbol": job.symbol,
        "status": job.status,
        "created_at": _dt(job.created_at),
        "updated_at": _dt(job.updated_at),
        "generated_at": report.get("generated_at"),
        "confidence": report.get("confidence"),
        "evidence_count": len(job.result.get("evidence", [])) if job.result else 0,
        "claim_count": len(report.get("claims", [])),
        "provider_statuses": {key: value.get("status") for key, value in provider_health.items()},
        "unknowns_count": len(report.get("unknowns", [])),
    }


def _case_summary(case: Any, detailed: bool = False) -> dict[str, Any]:
    payload = {
        "case_id": case.case_id,
        "symbol": case.instrument.symbol,
        "company_name": case.instrument.company_name,
        "status": case.status.value,
        "severity": case.severity,
        "primary_trigger": case.primary_trigger,
        "updated_at": _dt(case.updated_at),
        "opened_at": _dt(case.opened_at),
        "confidence": case.confidence,
        "current_thesis": case.current_thesis,
    }
    if detailed:
        payload.update(
            {
                "counter_thesis": case.counter_thesis,
                "unknowns": list(case.unknowns),
                "latest_report_id": case.latest_report_id,
                "evidence_ids": list(case.evidence_ids),
                "trigger_cluster_ids": list(case.trigger_cluster_ids),
                "cooldown_until": _dt(case.cooldown_until),
            }
        )
    return payload


def provider_health(marketdata_token: str | None = None) -> list[dict[str, Any]]:
    providers: list[ProviderHealth] = []
    providers.append(MarketDataAppPrototypeMarketDataProvider(token=marketdata_token).health())
    providers.append(YahooPrototypeMarketDataProvider().health())
    providers.append(SecFilingProvider().health())
    providers.append(YahooFinanceNewsProvider().health())
    return [item.to_dict() for item in providers]


def read_eval_reports() -> dict[str, Any]:
    reports: dict[str, Any] = {}
    for name in ("replay_results", "watch_replay_results", "watch_scenarios"):
        path = Path("evals/reports") / f"{name}.json"
        if path.exists():
            reports[name] = json.loads(path.read_text())
    return reports


def _dt(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


app = create_app()


def main() -> None:
    uvicorn.run("noalpha.api:app", host="127.0.0.1", port=int(os.environ.get("NOALPHA_API_PORT", "8000")), reload=False)


if __name__ == "__main__":
    main()
