from __future__ import annotations

import argparse
import json
from pathlib import Path

from noalpha.domain import AsOfContext, parse_datetime
from noalpha.env import load_env
from noalpha.logging_utils import configure_logging
from noalpha.pipeline import InvestigationPipeline
from noalpha.providers import MarketDataAppPrototypeMarketDataProvider
from noalpha.repositories import SQLiteRepository
from noalpha.watch import AutonomousWatchService
from noalpha.watch_models import ScanPolicy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noalpha",
        description="Leakage-resistant evaluation and simulated trading for LLM research",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="Run reproducible measurement demonstrations")
    demo_sub = demo.add_subparsers(dest="demo_command", required=True)
    leakage = demo_sub.add_parser(
        "leakage", help="Reproduce and reject a look-ahead-inflated backtest"
    )
    leakage.add_argument("--market", choices=["cn", "us"], default="cn")
    leakage.add_argument("--top-k", type=int, default=5)
    leakage.add_argument("--cache-dir", type=Path)
    leakage.add_argument("--log-level", default="WARNING")

    investigate = sub.add_parser("investigate", help="Investigate a US equity symbol")
    investigate.add_argument("symbol", help="Ticker symbol, e.g. NVDA")
    investigate.add_argument("--as-of", help="Point-in-time boundary, ISO datetime")
    investigate.add_argument("--lookback-days", type=int, default=90)
    investigate.add_argument("--cache-dir", default=".cache/noalpha")
    investigate.add_argument("--log-level", default="INFO")
    watch = sub.add_parser("watch", help="Manage and run autonomous watchlists")
    watch_sub = watch.add_subparsers(dest="watch_command", required=True)
    watch_add = watch_sub.add_parser("add", help="Add a symbol to a watchlist")
    watch_add.add_argument("symbol")
    watch_add.add_argument("--watchlist", default="default")
    watch_add.add_argument("--db", default=".cache/noalpha/watch.sqlite3")
    watch_add.add_argument("--cache-dir", default=".cache/noalpha")
    watch_add.add_argument("--lookback-days", type=int, default=7)
    watch_add.add_argument("--return-threshold", type=float, default=0.06)
    watch_add.add_argument("--volume-threshold", type=float, default=2.5)
    watch_add.add_argument("--news-burst-threshold", type=int, default=3)
    watch_add.add_argument("--cooldown-seconds", type=int, default=21600)
    watch_add.add_argument("--priority", type=int, default=0)
    watch_add.add_argument("--log-level", default="INFO")

    watch_list = watch_sub.add_parser("list", help="List watch items")
    watch_list.add_argument("--watchlist", default="default")
    watch_list.add_argument("--db", default=".cache/noalpha/watch.sqlite3")
    watch_list.add_argument("--log-level", default="INFO")

    watch_run = watch_sub.add_parser("run-once", help="Run one autonomous watch scan")
    watch_run.add_argument("--watchlist", default="default")
    watch_run.add_argument("--db", default=".cache/noalpha/watch.sqlite3")
    watch_run.add_argument("--cache-dir", default=".cache/noalpha")
    watch_run.add_argument("--as-of")
    watch_run.add_argument("--log-level", default="INFO")

    trading = sub.add_parser("trading", help="Autonomous simulation trading")
    trading_sub = trading.add_subparsers(dest="trading_command", required=True)
    for name, help_text in (
        ("run-once", "Run one autonomous trading cycle for a session"),
        ("start", "Run the autonomous trading loop continuously (Ctrl-C to stop)"),
        ("status", "Show session status, portfolio and recent decisions"),
    ):
        cmd = trading_sub.add_parser(name, help=help_text)
        cmd.add_argument("--session", help="Session id (defaults to most recent)")
        cmd.add_argument("--state-dir", default=".cache/api")
        cmd.add_argument("--log-level", default="INFO")
    trading_create = trading_sub.add_parser("create", help="Create a simulation trading session")
    trading_create.add_argument("--initial-capital", type=float, default=100000.0)
    trading_create.add_argument("--markets", default="CN,US")
    trading_create.add_argument("--risk-profile", default="balanced")
    trading_create.add_argument("--scan-interval", type=int, default=300)
    trading_create.add_argument("--allow-closed-market-fills", action="store_true")
    trading_create.add_argument("--state-dir", default=".cache/api")
    trading_create.add_argument("--log-level", default="INFO")

    cases = sub.add_parser("cases", help="Inspect investigation cases")
    cases_sub = cases.add_subparsers(dest="cases_command", required=True)
    cases_list = cases_sub.add_parser("list", help="List cases")
    cases_list.add_argument("--db", default=".cache/noalpha/watch.sqlite3")
    cases_list.add_argument("--log-level", default="INFO")
    cases_show = cases_sub.add_parser("show", help="Show a case")
    cases_show.add_argument("case_id")
    cases_show.add_argument("--db", default=".cache/noalpha/watch.sqlite3")
    cases_show.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    if args.command == "demo":
        from noalpha.demos.leakage import main as run_leakage_demo

        demo_argv = ["--market", args.market, "--top-k", str(args.top_k)]
        if args.cache_dir is not None:
            demo_argv.extend(["--cache-dir", str(args.cache_dir)])
        return run_leakage_demo(demo_argv)
    if args.command == "investigate":
        as_of_dt = parse_datetime(args.as_of) if args.as_of else None
        context = AsOfContext(as_of_dt) if as_of_dt else AsOfContext.now()
        pipeline = InvestigationPipeline.default(cache_dir=Path(args.cache_dir))
        result = pipeline.investigate(args.symbol, as_of=context, lookback_days=args.lookback_days)
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "watch":
        if args.watch_command == "add":
            repo = SQLiteRepository(Path(args.db))
            watchlist = repo.get_watchlist(args.watchlist) or repo.create_watchlist(args.watchlist)
            resolver = MarketDataAppPrototypeMarketDataProvider(cache_dir=Path(args.cache_dir) / "market")
            instrument = resolver.resolve_instrument(args.symbol)
            policy = ScanPolicy(
                lookback_days=args.lookback_days,
                return_threshold=args.return_threshold,
                volume_threshold=args.volume_threshold,
                news_burst_threshold=args.news_burst_threshold,
                cooldown_seconds=args.cooldown_seconds,
            )
            item = repo.add_watch_item(watchlist, instrument, policy, priority=args.priority)
            print(json.dumps({"watchlist": watchlist.name, "symbol": item.instrument.symbol, "enabled": item.enabled}, indent=2, sort_keys=True))
            repo.close()
            return 0
        if args.watch_command == "list":
            repo = SQLiteRepository(Path(args.db))
            watchlist = repo.get_watchlist(args.watchlist) or repo.create_watchlist(args.watchlist)
            items = repo.list_watch_items(watchlist.watchlist_id)
            print(json.dumps([{"symbol": item.instrument.symbol, "enabled": item.enabled, "priority": item.priority, "cooldown_until": item.cooldown_until.isoformat().replace("+00:00", "Z") if item.cooldown_until else None} for item in items], indent=2, sort_keys=True))
            repo.close()
            return 0
        if args.watch_command == "run-once":
            as_of_dt = parse_datetime(args.as_of) if args.as_of else None
            context = AsOfContext(as_of_dt) if as_of_dt else AsOfContext.now()
            service = AutonomousWatchService.default(db_path=Path(args.db), cache_dir=Path(args.cache_dir))
            summary = service.run_once(args.watchlist, as_of=context)
            print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False, sort_keys=True))
            service.repo.close()
            return 0
    if args.command == "trading":
        return _run_trading_command(args, parser)
    if args.command == "cases":
        repo = SQLiteRepository(Path(args.db))
        if args.cases_command == "list":
            cases = repo.list_cases()
            print(json.dumps([{"case_id": case.case_id, "symbol": case.instrument.symbol, "status": case.status.value, "confidence": case.confidence, "updated_at": case.updated_at.isoformat().replace("+00:00", "Z")} for case in cases], indent=2, sort_keys=True))
            repo.close()
            return 0
        if args.cases_command == "show":
            case = repo.get_case(args.case_id)
            if case is None:
                repo.close()
                parser.error(f"case not found: {args.case_id}")
            snapshots = repo.list_snapshots(args.case_id)
            print(
                json.dumps(
                    {
                        "case": {
                            "case_id": case.case_id,
                            "symbol": case.instrument.symbol,
                            "status": case.status.value,
                            "opened_at": case.opened_at.isoformat().replace("+00:00", "Z"),
                            "updated_at": case.updated_at.isoformat().replace("+00:00", "Z"),
                            "primary_trigger": case.primary_trigger,
                            "severity": case.severity,
                            "current_thesis": case.current_thesis,
                            "counter_thesis": case.counter_thesis,
                            "unknowns": list(case.unknowns),
                            "confidence": case.confidence,
                            "latest_report_id": case.latest_report_id,
                            "trigger_cluster_ids": list(case.trigger_cluster_ids),
                            "evidence_count": len(case.evidence_ids),
                            "cooldown_until": case.cooldown_until.isoformat().replace("+00:00", "Z") if case.cooldown_until else None,
                        },
                        "snapshots": [
                            {
                                "snapshot_id": snapshot.snapshot_id,
                                "created_at": snapshot.created_at.isoformat().replace("+00:00", "Z"),
                                "confidence": snapshot.confidence,
                                "thesis_change": snapshot.thesis_change,
                                "summary": snapshot.summary,
                            }
                            for snapshot in snapshots
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            repo.close()
            return 0
    parser.error("unknown command")
    return 2


def _run_trading_command(args, parser) -> int:
    import time

    from noalpha.market.watchlist_store import MarketWatchlistStore
    from noalpha.trading.autonomous import AutonomousTradingLoop, LiveMarketFeed
    from noalpha.trading.session import SessionStatus, TradingSession, TradingStore
    from noalpha.trading.virtual_broker import VirtualBroker

    state_dir = Path(args.state_dir)
    store = TradingStore(state_dir / "trading.sqlite3")
    wl_store = MarketWatchlistStore(state_dir / "watch.sqlite3")

    if args.trading_command == "create":
        markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
        session = TradingSession(
            initial_capital=args.initial_capital,
            current_equity=args.initial_capital, cash=args.initial_capital,
            currency="CNY" if markets == ["CN"] else "USD",
            allowed_markets=markets, risk_profile=args.risk_profile,
            scan_interval=args.scan_interval,
            allow_closed_market_fills=args.allow_closed_market_fills,
        )
        store.save_session(session)
        print(json.dumps(session.to_dict(), indent=2, ensure_ascii=False))
        return 0

    sessions = store.list_sessions()
    session = None
    if getattr(args, "session", None):
        session = store.load_session(args.session)
    elif sessions:
        session = sessions[0]
    if session is None:
        parser.error("no trading session; run `trading create` first")

    state = store.load_broker_state(session.session_id)
    broker = VirtualBroker.from_state(state) if state else VirtualBroker(
        initial_cash=session.initial_capital, currency=session.currency)
    loop = AutonomousTradingLoop(
        session, store, broker, feed=LiveMarketFeed(),
        universe_provider=lambda: wl_store.list(enabled_only=True),
    )

    if args.trading_command == "status":
        print(json.dumps({
            "session": session.to_dict(),
            "portfolio": broker.get_portfolio_summary(),
            "recent_events": store.list_events(session.session_id, limit=15),
        }, indent=2, ensure_ascii=False, default=str))
        return 0

    if args.trading_command == "run-once":
        session.status = SessionStatus.RUNNING
        summary = loop.run_cycle()
        session.status = SessionStatus.STOPPED
        store.save_session(session)
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        return 0

    if args.trading_command == "start":
        session.status = SessionStatus.RUNNING
        store.save_session(session)
        print(f"Autonomous trading loop started (session {session.session_id}, "
              f"interval {session.scan_interval}s). Ctrl-C to stop.")
        try:
            while session.status == SessionStatus.RUNNING:
                summary = loop.run_cycle()
                print(json.dumps({"cycle": summary.get("cycle"),
                                  "actions": summary.get("actions"),
                                  "equity": summary.get("equity")}, ensure_ascii=False))
                time.sleep(session.scan_interval)
        except KeyboardInterrupt:
            session.status = SessionStatus.STOPPED
            store.save_session(session)
            print("stopped")
        return 0
    return 2
