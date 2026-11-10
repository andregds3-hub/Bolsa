from datetime import datetime, timezone
from decimal import Decimal

from engine.analytics import compute_metrics
from engine.core import Account, AccountStatus, ClosedTrade, RiskLimits, Side
from engine.gamification import LEVELS, level_for, progress

D = Decimal
ACC = Account("a", "BRL", D("50000"), D("50000"), RiskLimits(daily_loss_limit=D("2500"), max_drawdown=D("5000")))
CATEGORY = {"WINZ26": "FUTURO", "PETR4": "ACAO", "MXRF11": "FII"}.get


def trade(symbol, pnl, reason, day=10):
    ts = datetime(2026, 11, day, 15, tzinfo=timezone.utc)
    return ClosedTrade("a", symbol, Side.BUY, D(1), D(1), D(1), ts, ts, D(pnl), D(0), reason)


def test_new_account_starts_at_level_one_with_nothing_unlocked():
    p = progress([], compute_metrics([], [], ACC.initial_balance), ACC, CATEGORY)
    assert (p["xp"], p["level"], p["title"], p["unlocked"]) == (0, 1, "Aprendiz", 0)


def test_discipline_and_diversification_earn_xp():
    trades = [trade("WINZ26", 100, "TAKE_PROFIT"), trade("PETR4", -40, "STOP_LOSS"),
              trade("MXRF11", 30, "TRAILING", day=11)]
    p = progress(trades, compute_metrics(trades, [], ACC.initial_balance), ACC, CATEGORY)
    done = {a["id"] for a in p["achievements"] if a["done"]}
    assert done == {"primeiro_trade", "respeitou_stop", "alvo_na_mosca", "surfista", "dia_verde",
                    "diversificado", "fii", "derivativos"}
    # conquistas + 5 XP por trade + 20 XP por dia positivo (2 dias)
    assert p["xp"] == 50 + 60 + 60 + 80 + 70 + 100 + 40 + 40 + 3 * 5 + 2 * 20
    assert (p["level"], p["title"]) == (4, "Trader") and 0 <= p["progress"] <= 1


def test_passed_challenge_and_level_cap():
    passed = Account("b", "BRL", D("50000"), D("54000"), ACC.limits, status=AccountStatus.PASSED)
    p = progress([], compute_metrics([], [], passed.initial_balance), passed, CATEGORY)
    assert next(a for a in p["achievements"] if a["id"] == "aprovado")["done"]
    top = level_for(10_000)
    assert top[0] == len(LEVELS) and top[2] == top[3]
