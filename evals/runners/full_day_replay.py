"""Accelerated full-trading-day replay for the ContinuousIntelligenceLoop.

Simulates: CN close → daily report → cross-market → overnight → next pre-market.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from noalpha.market.calendar import global_snapshot, market_status, Market, MARKET_LABEL
from noalpha.trading.autonomous import LiveMarketFeed
from noalpha.trading.continuous_loop import ContinuousIntelligenceLoop
from noalpha.trading.session import SessionStatus, TradingSession, TradingStore
from noalpha.trading.virtual_broker import VirtualBroker


# ── replay scenario: pick a recent CN trading day ──
# 2026-07-07 is a Tuesday, trading day for CN
CN_CLOSE = datetime(2026, 7, 7, 7, 1, 0, tzinfo=timezone.utc)   # 15:01 Beijing
US_OPEN = datetime(2026, 7, 7, 13, 31, 0, tzinfo=timezone.utc)   # 21:31 Beijing = 09:31 ET
US_MID = datetime(2026, 7, 7, 16, 0, 0, tzinfo=timezone.utc)     # ~US midday
CN_OVERNIGHT = datetime(2026, 7, 7, 20, 0, 0, tzinfo=timezone.utc)  # 04:00 Beijing
CN_NEXT_PRE = datetime(2026, 7, 8, 1, 16, 0, tzinfo=timezone.utc)  # 09:16 Beijing next day

STEPS = [
    ("收盘复盘", CN_CLOSE, "POST_MARKET_REVIEW"),
    ("美股开盘-跨市场", US_OPEN, "CROSS_MARKET_MONITORING"),
    ("美股盘中-跨市场", US_MID, "CROSS_MARKET_MONITORING"),
    ("夜间研究", CN_OVERNIGHT, "OVERNIGHT_RESEARCH"),
    ("次日盘前", CN_NEXT_PRE, "PRE_MARKET_BRIEF"),
]


def run():
    db_path = Path(".cache/api/trading_replay.sqlite3")
    store = TradingStore(db_path)
    session = TradingSession(
        session_id="replay-full-day",
        mode="simulation",
        status=SessionStatus.CREATED,
        initial_capital=100000.0,
        current_equity=100000.0,
        cash=100000.0,
        currency="CNY",
        allowed_markets=["CN"],
    )
    store.save_session(session)
    broker = VirtualBroker(initial_cash=100000.0, currency="CNY")
    loop = ContinuousIntelligenceLoop(
        session, store, broker,
        feed=LiveMarketFeed(),
        home_market="CN",
        monitored_markets=["CN", "US", "HK"],
        replay_time=CN_CLOSE,
    )

    print("# 全流程回放：CN 交易日完整 24/7 生命周期\n")
    print(f"Session: {session.session_id}")
    print(f"Home: CN | Monitored: CN, US, HK\n")

    for idx, (label, target_time, expected_phase) in enumerate(STEPS, 1):
        loop._replay_time = target_time
        snap = global_snapshot(utc_now=target_time)

        # Show market state
        clues = []
        for m in (Market.CN, Market.US, Market.HK):
            ms = snap.markets.get(m)
            if ms:
                icon = "●" if ms.session.value == "open" else "○"
                clues.append(f"{icon} {MARKET_LABEL.get(m, m.value)} {ms.session.value} ({ms.local_time.strftime('%H:%M')})")
        print(f"## Step {idx}: {label}")
        print(f"   UTC {target_time.strftime('%Y-%m-%d %H:%M')} | {' | '.join(clues)}")

        summary = loop.run_intelligence_cycle()
        phase = summary.get("phase", "?")
        action = summary.get("action", "?")
        match = "✓" if phase.lower() == expected_phase.lower() else f"✗ (expected {expected_phase})"
        print(f"   Phase: {phase} {match}")
        print(f"   Action: {action}")

        detail = ""
        if phase == "post_market_review":
            detail = f"report_id={summary.get('report_id', '?')} | daily_pnl={summary.get('daily_pnl', 0)} | trades={summary.get('trade_count', 0)}"
        elif phase == "cross_market_monitoring":
            detail = f"open_markets={summary.get('open_markets', [])} | events={summary.get('events', 0)}"
        elif phase == "pre_market_brief":
            detail = f"revalidated={summary.get('pending_revalidated', 0)} | overnight_events={summary.get('overnight_relevant_events', 0)}"
        print(f"   Detail: {detail}\n")

    # ── verify the generated daily report ──
    print("## 验证: 每日报告\n")
    reports = store.list_daily_reports(session.session_id)
    print(f"   报告数: {len(reports)}")
    for r in reports[:2]:
        print(f"   - {r['date']} | market_regime={r['market_regime']} | pnl={r['daily_pnl']} | trades={r['trade_count']}")
        print(f"     successful={len(r.get('successful_reviews', []))} failed={len(r.get('failed_reviews', []))}")
        print(f"     lessons={len(r.get('lessons', []))} missed={len(r.get('missed_opportunities', []))}")
        print(f"     tomorrow_unknowns={r.get('tomorrow_unknowns', [])}")

    # ── verify operational state ──
    print(f"\n## 最终 Agent 操作状态")
    os = loop.op_state.to_dict()
    print(f"   phase: {os['phase_label_zh']}")
    print(f"   daily_reports: {os['daily_report_count']}")
    print(f"   overnight_events: {os['overnight_event_count']}")

    # ── verify events ──
    print(f"\n## 事件列表 (最近10)")
    events = store.list_events(session.session_id, limit=10)
    for e in events:
        print(f"   {e['event_type']:>15s} | {e['message'][:80]}")

    return {"reports": len(reports), "phases_completed": idx}


if __name__ == "__main__":
    result = run()
    print(f"\n✓ 回放完成。报告: {result['reports']}, 阶段: {result['phases_completed']}")
