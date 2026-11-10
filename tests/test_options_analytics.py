import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from engine.analytics import compute_metrics, ranking_score
from engine.catalog import FRONT, models_for
from engine.core import BracketSpec, ClosedTrade, Side
from engine.options import analyze, black_scholes, template
from engine.strategies import SmaCrossTrend, backtest


def test_put_call_parity_and_delta_bounds():
    s, k, t, r, v = 100.0, 105.0, 0.5, 0.10, 0.30
    call, put = black_scholes(s, k, t, r, v, "C"), black_scholes(s, k, t, r, v, "P")
    assert call.price - put.price == pytest.approx(s - k * math.exp(-r * t), abs=1e-9)
    assert 0 < call.delta < 1 and put.delta == pytest.approx(call.delta - 1)
    assert call.gamma == pytest.approx(put.gamma) and call.vega == pytest.approx(put.vega)
    assert call.theta < 0


def test_straddle_breakevens_are_strike_plus_minus_premium():
    res = analyze(template("long_straddle", 100.0), 100.0, 0.10, 30 / 252, 0.30)
    premium = res["net_premium"]
    assert res["breakevens"] == pytest.approx([100 - premium, 100 + premium], abs=1e-6)
    assert res["max_profit"] is None  # ilimitado na alta
    assert res["max_loss"] == pytest.approx(-premium)


def test_iron_condor_is_bounded_by_credit_and_wing_width():
    res = analyze(template("iron_condor", 100.0, 0.05), 100.0, 0.10, 30 / 252, 0.30)
    credit = -res["net_premium"]
    assert credit > 0
    assert res["max_profit"] == pytest.approx(credit)
    assert res["max_loss"] == pytest.approx(-(5 - credit))


def _trade(pnl, day):
    ts = datetime(2026, 11, day, 15, tzinfo=timezone.utc)
    return ClosedTrade("a", "WINZ26", Side.BUY, Decimal(1), Decimal(1), Decimal(1), ts, ts,
                       Decimal(pnl), Decimal(0), "MANUAL")


def test_journal_metrics():
    trades = [_trade(100, 2), _trade(-50, 2), _trade(200, 3), _trade(-50, 4)]
    t0 = datetime(2026, 11, 2, tzinfo=timezone.utc)
    curve = [(t0 + timedelta(hours=i), Decimal(v)) for i, v in enumerate([1000, 1100, 1050, 1250, 1200])]
    m = compute_metrics(trades, curve, Decimal(1000))
    assert (m.trades, m.win_rate, m.profit_factor, m.payoff, m.expectancy) == (4, 0.5, 3.0, 3.0, 50.0)
    assert m.max_drawdown == 50 and m.max_drawdown_pct == pytest.approx(50 / 1100)
    assert m.return_pct == pytest.approx(0.2) and m.trading_days == 3
    assert m.consistency == pytest.approx(1 - 200 / 200)  # melhor dia = 100% do lucro
    score, eligible = ranking_score(m, 1000, breached=False, min_trades=4, min_days=3)
    assert 0 < score <= 100 and eligible


def test_backtest_is_deterministic_and_uses_the_live_engine():
    def run():
        factory = lambda eng, acc_id: SmaCrossTrend(
            eng, acc_id, FRONT["WIN"], 8, 30, Decimal("2"),
            BracketSpec(stop_loss_ticks=60, take_profit_ticks=150), sample_every=2)
        return backtest(factory, models_for([FRONT["WIN"]]), 3000, seed=7)
    acc1, trades1, _ = run()
    acc2, trades2, curve = run()
    assert trades1 and [t.net_pnl_base for t in trades1] == [t.net_pnl_base for t in trades2]
    assert acc1.balance == acc2.balance and len(curve) > 100
    assert {t.symbol for t in trades1} == {FRONT["WIN"]}
