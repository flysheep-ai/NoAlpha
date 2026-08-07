"""Hard-risk invariants, with an emphasis on what "daily" actually means.

The bug: `max_daily_loss_pct` was evaluated against
`sum(position.realized_pnl)` — an all-time session figure that never reset.
Consequences, both of them wrong in the dangerous direction:

  * a loss booked last week kept blocking trading forever
  * a profit booked last week silently financed today's blow-up

These tests also cover the limits that were configured but never enforced
(`price_deviation_pct`) and the ones an LLM must never be able to route
around (kill switch, stale quotes).
"""
from __future__ import annotations

import pytest

from noalpha.trading.domain import Account, Position
from noalpha.trading.proposal import TradeAction, TradeProposal
from noalpha.trading.risk import PreTradeRiskEngine, TradingDayRiskState


def _account(equity=100_000.0, cash=100_000.0, initial=100_000.0) -> Account:
    return Account(account_id="acc-test", cash=cash, equity=equity,
                   initial_capital=initial)


def _position(symbol="AAA", qty=10.0, realized=0.0, price=100.0) -> Position:
    """A 1%-of-equity holding, so the position-weight cap never fires by
    accident and these tests only exercise the daily-loss path."""
    pos = Position(symbol=symbol, quantity=qty, average_cost=price,
                   market_price=price, realized_pnl=realized)
    pos.market_value = qty * price
    return pos


def _proposal(weight=0.05, action=TradeAction.BUY, symbol="AAA") -> TradeProposal:
    return TradeProposal(symbol=symbol, action=action, target_weight=weight)


def _check(engine, *, positions=None, account=None, price=100.0, **kw):
    return engine.check(_proposal(), account or _account(), positions or {}, [],
                        price, **kw)


# ── TradingDayRiskState ──


def test_state_rolls_on_a_new_trading_date():
    state = TradingDayRiskState()
    state.observe(trading_date="2026-08-05", equity=100_000, realized_pnl_total=0.0)
    state.observe(trading_date="2026-08-05", equity=95_000, realized_pnl_total=-5_000)
    assert state.realized_pnl_today == pytest.approx(-5_000)

    rolled = state.observe(trading_date="2026-08-06", equity=95_000,
                           realized_pnl_total=-5_000)
    assert rolled is True
    assert state.realized_pnl_today == pytest.approx(0.0), \
        "yesterday's loss is still counted as today's"
    assert state.start_of_day_equity == pytest.approx(95_000)
    assert state.rollovers == 2


def test_the_four_daily_quantities_stay_distinct():
    state = TradingDayRiskState()
    state.observe(trading_date="2026-08-06", equity=100_000, realized_pnl_total=0.0)
    state.observe(trading_date="2026-08-06", equity=110_000, realized_pnl_total=0.0)
    state.observe(trading_date="2026-08-06", equity=99_000, realized_pnl_total=-1_000)

    assert state.realized_daily_loss_pct == pytest.approx(-0.01)   # realized only
    assert state.equity_daily_drawdown == pytest.approx(-0.01)     # vs day open
    assert state.session_drawdown == pytest.approx(-0.10)          # vs day peak
    payload = state.to_dict()
    assert {"realized_daily_loss_pct", "equity_daily_drawdown",
            "session_drawdown"} <= set(payload)


# ── the daily-loss gate ──


def test_yesterdays_loss_does_not_block_todays_trading():
    engine = PreTradeRiskEngine(max_daily_loss_pct=0.05)
    # day 1 opens flat, then books a −20% realized loss
    _check(engine, positions={}, trading_date="2026-08-05")
    blocked = _check(engine, positions={"AAA": _position(realized=-20_000)},
                     trading_date="2026-08-05")
    assert blocked.rejected, "a −20% day should be blocked on the day it happens"

    # day 2 opens with the SAME cumulative figure — nothing was lost today
    fresh = _check(engine, positions={"AAA": _position(realized=-20_000)},
                   trading_date="2026-08-06")
    assert fresh.approved, "yesterday's realized loss is still gating today"
    assert engine.day_state.realized_pnl_today == pytest.approx(0.0)


def test_yesterdays_profit_cannot_offset_todays_loss():
    """The dangerous direction. With an all-time sum, +30% booked last week let
    a −20% day sail straight through the limit."""
    engine = PreTradeRiskEngine(max_daily_loss_pct=0.05)
    # day 1: big profit
    _check(engine, positions={"AAA": _position(realized=+30_000)},
           trading_date="2026-08-05")
    # day 2 opens carrying that profit …
    _check(engine, positions={"AAA": _position(realized=+30_000)},
           trading_date="2026-08-06")
    # … then loses 20% today. The session total is still +10_000.
    result = _check(engine, positions={"AAA": _position(realized=+10_000)},
                    trading_date="2026-08-06")
    assert result.rejected, "a historical profit absorbed today's loss"
    assert engine.day_state.realized_pnl_today == pytest.approx(-20_000)
    assert "2026-08-06" in result.reason


def test_a_restart_cannot_clear_todays_realized_loss():
    """Otherwise "crash and restart" is a working bypass of a hard limit."""
    engine = PreTradeRiskEngine(max_daily_loss_pct=0.05)
    _check(engine, positions={"AAA": _position(realized=0.0)}, trading_date="2026-08-06")
    blocked = _check(engine, positions={"AAA": _position(realized=-20_000)},
                     trading_date="2026-08-06")
    assert blocked.rejected

    restarted = PreTradeRiskEngine(max_daily_loss_pct=0.05)
    restarted.day_state = TradingDayRiskState.from_dict(engine.day_state.to_dict())
    still_blocked = _check(restarted, positions={"AAA": _position(realized=-20_000)},
                           trading_date="2026-08-06")
    assert still_blocked.rejected, "a restart reset today's realized-loss anchor"


def test_daily_equity_drawdown_is_a_separate_enforced_limit():
    engine = PreTradeRiskEngine(max_daily_loss_pct=0.05, max_daily_drawdown_pct=0.03)
    # open the day, then lose 4% of equity with nothing realized
    _check(engine, account=_account(equity=100_000), trading_date="2026-08-06")
    result = _check(engine, account=_account(equity=96_000, cash=96_000),
                    trading_date="2026-08-06")
    assert result.rejected
    assert "drawdown" in result.reason.lower()
    names = {c["name"] for c in result.checks}
    assert {"realized_daily_loss", "equity_daily_drawdown"} <= names, \
        "the two limits must be reported separately, never merged"


def test_every_result_carries_the_day_state_it_was_judged_against():
    engine = PreTradeRiskEngine()
    result = _check(engine, trading_date="2026-08-06")
    assert result.to_payload()["day_state"]["trading_date"] == "2026-08-06"


# ── limits that must not be bypassable ──


def test_kill_switch_rejects_before_anything_else():
    engine = PreTradeRiskEngine()
    result = _check(engine, kill_switch=True)
    assert result.rejected
    assert not result.approved
    assert any(c["name"] == "kill_switch" and not c["passed"] for c in result.checks)


def test_stale_market_data_cannot_trade():
    engine = PreTradeRiskEngine()
    result = _check(engine, market_data_fresh=False)
    assert result.rejected
    assert "stale" in result.reason.lower()


def test_price_deviation_limit_is_actually_enforced():
    """`price_deviation_pct` was configured and never read — a bad tick could
    size and fill an order at an absurd price."""
    engine = PreTradeRiskEngine(price_deviation_pct=0.10)
    ok = _check(engine, price=105.0, reference_price=100.0)
    assert ok.approved

    bad = _check(engine, price=180.0, reference_price=100.0)
    assert bad.rejected
    assert "deviates" in bad.reason
    assert any(c["name"] == "price_deviation" and not c["passed"] for c in bad.checks)


def test_no_reference_price_is_recorded_as_skipped_not_silently_passed():
    engine = PreTradeRiskEngine()
    result = _check(engine, reference_price=0.0)
    dev = next(c for c in result.checks if c["name"] == "price_deviation")
    assert dev["limit"] == "skipped"


def test_open_order_and_position_limits_still_hold():
    engine = PreTradeRiskEngine(max_position_weight=0.05)
    at_cap = {"AAA": _position(qty=100, price=100.0)}
    at_cap["AAA"].market_value = 90_000.0     # 90% of equity
    result = engine.check(_proposal(0.05), _account(), at_cap, [], 100.0,
                          trading_date="2026-08-06")
    assert result.rejected
