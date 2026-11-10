from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from engine.core import (Account, AccountStatus, BracketSpec, ExecutionEngine, FillModel, FxConverter,
                         Instrument, MarketRegime, Order, OrderStatus, OrderType, Quote, RiskLimits, Side)

D = Decimal
NOW = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)  # 11:00 em São Paulo
WIN = Instrument("WINZ26", "BRL", D("5"), D("1.00"), D("0.30"), D("100"))
EURUSD = Instrument("EURUSD", "USD", D("0.00001"), D("1.00"), D("3.50"), D("1170"), D("0.01"))
PETR4 = Instrument("PETR4", "BRL", D("0.01"), D("0.01"), D("0"), D("38"), D("100"))


def limits(**overrides):
    base = dict(daily_loss_limit=D("1500"), max_drawdown=D("3000"), trailing_drawdown=True,
                reset_tz="America/Sao_Paulo", reset_time=time(18, 0))
    return RiskLimits(**{**base, **overrides})


def make(lim=None, balance="50000", instruments=(WIN,)):
    events = []
    eng = ExecutionEngine({i.symbol: i for i in instruments}, FxConverter(), FillModel(),
                          emit=lambda topic, payload: events.append((topic, payload)))
    acc = eng.add_account(Account("acc", "BRL", D(balance), D(balance), lim or limits()), NOW)
    return eng, acc, events


def tick(eng, bid, ask, size="50", symbol="WINZ26", now=NOW):
    eng.on_quote(Quote(symbol, D(bid), D(ask), D(size), D(size), now), now)


def market(eng, side, qty, symbol="WINZ26", now=NOW, **kw):
    return eng.submit(Order("acc", symbol, side, OrderType.MARKET, D(qty), **kw), now)


def test_rejects_order_whose_stop_would_breach_daily_loss():
    eng, acc, _ = make()
    tick(eng, "128000", "128005")
    o = market(eng, Side.BUY, "100", bracket=BracketSpec(stop_loss_ticks=200))
    assert o.status is OrderStatus.REJECTED and o.reject_code == "DAILY_LOSS_AT_RISK"
    assert not eng.positions


def test_oco_breakeven_then_trailing_exit_with_gap():
    eng, acc, _ = make()
    tick(eng, "128000", "128005")
    market(eng, Side.BUY, "10", bracket=BracketSpec(
        stop_loss_ticks=60, take_profit_ticks=120, breakeven_trigger_ticks=30,
        breakeven_offset_ticks=2, trailing_ticks=40, trailing_activation_ticks=50))
    pos = eng.positions[("acc", "WINZ26")]
    sl, tp = eng.orders[pos.sl_order_id], eng.orders[pos.tp_order_id]
    assert (sl.stop_price, tp.limit_price) == (D("127705"), D("128605"))

    tick(eng, "128200", "128205")  # +39 ticks
    assert sl.stop_price == D("128015") and sl.role == "BREAKEVEN"
    tick(eng, "128400", "128405")  # +79 ticks
    assert sl.stop_price == D("128200") and sl.role == "TRAILING"
    tick(eng, "128150", "128155")  # gap abaixo do stop

    assert sl.status is OrderStatus.FILLED and sl.fill_price == D("128150")
    assert tp.status is OrderStatus.CANCELED  # OCO
    assert not eng.positions and not eng.orders
    trade = eng.trades["acc"][-1]
    assert trade.exit_reason == "TRAILING" and trade.fees_base == D("6.00")
    assert trade.net_pnl_base == D("284.00")
    assert acc.balance == D("50284.00")


def test_daily_loss_flattens_and_locks_until_next_session():
    eng, acc, events = make()
    tick(eng, "128000", "128005")
    market(eng, Side.BUY, "10")
    tick(eng, "127255", "127260")  # equity 48.497 ≤ piso diário 48.500
    assert acc.status is AccountStatus.DAILY_LOCKED and acc.breach_reason == "DAILY_LOSS"
    assert not eng.positions and acc.balance == D("48494.00")
    assert any(t == "risk.breach" for t, _ in events)
    assert market(eng, Side.BUY, "1").reject_code == "ACCOUNT_LOCKED"

    tomorrow = NOW + timedelta(days=1)
    assert market(eng, Side.BUY, "1", now=tomorrow).status is OrderStatus.FILLED
    assert acc.status is AccountStatus.ACTIVE and acc.day_anchor == D("48494.00")


def test_max_drawdown_is_permanent():
    eng, acc, _ = make(limits(daily_loss_limit=D("5000"), max_drawdown=D("1000"), trailing_drawdown=False))
    tick(eng, "128000", "128005")
    market(eng, Side.BUY, "10")
    tick(eng, "127500", "127505")  # equity 48.987 ≤ 49.000
    assert acc.status is AccountStatus.BREACHED and not eng.positions
    assert market(eng, Side.BUY, "1", now=NOW + timedelta(days=1)).reject_code == "ACCOUNT_LOCKED"


def test_trailing_floor_follows_equity_and_locks_at_initial_balance():
    eng, acc, _ = make(limits(daily_loss_limit=D("5000"), max_drawdown=D("1000")))
    tick(eng, "128000", "128005")
    market(eng, Side.BUY, "10")
    tick(eng, "128505", "128510")  # equity 50.997
    assert eng.risk.total_floor(acc) == D("49997")
    tick(eng, "129005", "129010")  # equity 51.997
    assert eng.risk.total_floor(acc) == D("50000")


def test_stop_out_closes_position_when_margin_level_collapses():
    eng, acc, _ = make(limits(daily_loss_limit=D("100000"), max_drawdown=D("100000"),
                              block_orders_risking_breach=False), balance="1000")
    tick(eng, "128000", "128005")
    assert market(eng, Side.BUY, "10").status is OrderStatus.FILLED  # margem 1.000
    tick(eng, "127755", "127760")  # equity 497 / margem 1.000 < 50%
    assert not eng.positions and acc.status is AccountStatus.ACTIVE
    assert eng.trades["acc"][-1].exit_reason == "LIQUIDATION"
    assert acc.balance == D("494.00")


def test_multi_currency_pnl_is_converted_to_base():
    eng, acc, _ = make(limits(daily_loss_limit=D("5000"), max_drawdown=D("10000")), balance="100000",
                       instruments=(WIN, EURUSD))
    eng.fx.update("USD", "BRL", D("5"))
    tick(eng, "1.17000", "1.17010", symbol="EURUSD")
    market(eng, Side.BUY, "1", symbol="EURUSD")
    tick(eng, "1.17110", "1.17120", symbol="EURUSD")
    assert eng.close_position("acc", "EURUSD", NOW)
    trade = eng.trades["acc"][-1]
    assert trade.gross_pnl_base == D("500.00")       # 100 ticks × US$1 × 5,00
    assert acc.balance == D("100465.00")             # − 2 × US$3,50 de corretagem


def test_passive_limit_fills_at_limit_and_stop_gaps_through():
    eng, acc, _ = make()
    tick(eng, "128000", "128005")
    lim = eng.submit(Order("acc", "WINZ26", Side.BUY, OrderType.LIMIT, D("10"), limit_price=D("127900")), NOW)
    assert lim.status is OrderStatus.WORKING
    tick(eng, "127890", "127895")
    assert lim.status is OrderStatus.FILLED and lim.fill_price == D("127900")

    stop = eng.submit(Order("acc", "WINZ26", Side.SELL, OrderType.STOP, D("10"), stop_price=D("127800")), NOW)
    tick(eng, "127700", "127705")
    assert stop.status is OrderStatus.FILLED and stop.fill_price == D("127700")


def test_news_regime_widens_spread_and_thins_book():
    eng, acc, _ = make()
    eng.regimes["WINZ26"] = MarketRegime(D("3"), D("0.2"))
    tick(eng, "128000", "128010")
    q = eng.quotes["WINZ26"]
    assert (q.bid, q.ask, q.ask_size) == (D("127990"), D("128020"), D("10.0"))
    assert market(eng, Side.BUY, "10").fill_price == D("128025")  # consumiu o topo: +1 tick


def test_news_blackout_blocks_new_positions():
    eng, acc, _ = make(limits(news_blackout=True))
    tick(eng, "128000", "128005")
    eng.blackouts.append((NOW - timedelta(minutes=1), NOW + timedelta(minutes=1), frozenset({"WINZ26"})))
    assert market(eng, Side.BUY, "1").reject_code == "NEWS_BLACKOUT"


@pytest.mark.parametrize("order, code", [
    (Order("acc", "WINZ26", Side.BUY, OrderType.LIMIT, D("1"), limit_price=D("128003")), "INVALID_TICK"),
    (Order("acc", "PETR4", Side.BUY, OrderType.MARKET, D("50")), "INVALID_LOT"),
    (Order("acc", "WINZ26", Side.BUY, OrderType.STOP, D("1")), "MISSING_PRICE"),
])
def test_order_validation(order, code):
    eng, acc, _ = make(instruments=(WIN, PETR4))
    tick(eng, "128000", "128005")
    tick(eng, "38.00", "38.01", symbol="PETR4")
    assert eng.submit(order, NOW).reject_code == code


def test_flip_cancels_orphan_bracket_orders():
    eng, acc, _ = make()
    tick(eng, "128000", "128005")
    market(eng, Side.BUY, "10", bracket=BracketSpec(stop_loss_ticks=60, take_profit_ticks=120))
    assert len(eng.orders) == 2
    market(eng, Side.SELL, "20")
    pos = eng.positions[("acc", "WINZ26")]
    assert pos.side is Side.SELL and pos.qty == D("10") and not eng.orders


def test_reduce_only_without_position_is_canceled():
    eng, acc, _ = make()
    tick(eng, "128000", "128005")
    o = eng.submit(Order("acc", "WINZ26", Side.SELL, OrderType.STOP, D("1"), stop_price=D("127000"),
                         reduce_only=True), NOW)
    tick(eng, "126995", "127000")
    assert o.status is OrderStatus.CANCELED


def test_profit_target_passes_challenge_once_flat():
    eng, acc, _ = make(limits(daily_loss_limit=D("5000"), max_drawdown=D("5000"), trailing_drawdown=False,
                              profit_target=D("200"), min_trading_days=1))
    tick(eng, "128000", "128005")
    market(eng, Side.BUY, "10")
    tick(eng, "128205", "128210")
    assert acc.status is AccountStatus.ACTIVE  # posição aberta: ainda não conta
    eng.close_position("acc", "WINZ26", NOW)
    assert acc.status is AccountStatus.PASSED and acc.balance == D("50394.00")
    assert market(eng, Side.BUY, "1").reject_code == "ACCOUNT_LOCKED"


def test_reset_restores_initial_state():
    eng, acc, _ = make()
    tick(eng, "128000", "128005")
    market(eng, Side.BUY, "10")
    tick(eng, "127255", "127260")
    assert acc.status is AccountStatus.DAILY_LOCKED
    eng.reset_account("acc", NOW)
    assert acc.status is AccountStatus.ACTIVE and acc.balance == D("50000") and acc.reset_count == 1
    assert eng.trades["acc"] == [] and market(eng, Side.BUY, "1").status is OrderStatus.FILLED
