"""
Trading Journal: métricas de performance e score do ranking.

As métricas usam float: são relatório, não ledger (o saldo continua em Decimal no motor).
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from engine.core import ClosedTrade


@dataclass(frozen=True)
class Metrics:
    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    net_pnl: float
    profit_factor: Optional[float]  # None quando não há perdas
    avg_win: float
    avg_loss: float
    payoff: Optional[float]
    expectancy: float               # ganho médio esperado por trade
    max_drawdown: float
    max_drawdown_pct: float
    return_pct: float
    sortino: Optional[float]        # por trade: média / desvio das perdas
    best_day: float
    consistency: Optional[float]    # 1 − (melhor dia / lucro total)
    trading_days: int


def compute_metrics(trades: Sequence[ClosedTrade],
                    equity_curve: Iterable[tuple[datetime, Decimal]],
                    initial_balance: Decimal) -> Metrics:
    pnl = [float(t.net_pnl_base) for t in trades]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x < 0]
    gross_profit, gross_loss = sum(wins), -sum(losses)
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0

    initial = float(initial_balance)
    peak = initial
    max_dd = max_dd_pct = 0.0
    last = initial
    for _, equity in equity_curve:
        last = float(equity)
        peak = max(peak, last)
        if peak - last > max_dd:
            max_dd, max_dd_pct = peak - last, (peak - last) / peak if peak else 0.0

    sortino = None
    if len(pnl) >= 2:
        downside = math.sqrt(sum(min(0.0, x) ** 2 for x in pnl) / len(pnl))
        if downside > 0:
            sortino = (sum(pnl) / len(pnl)) / downside

    by_day: dict = defaultdict(float)
    for t in trades:
        by_day[t.closed_at.date()] += float(t.net_pnl_base)
    net = sum(pnl)
    best_day = max(by_day.values()) if by_day else 0.0
    consistency = None
    if net > 0 and best_day > 0:
        consistency = max(0.0, 1 - best_day / net)

    return Metrics(
        trades=len(pnl), wins=len(wins), losses=len(losses),
        win_rate=len(wins) / len(pnl) if pnl else 0.0,
        gross_profit=gross_profit, gross_loss=gross_loss, net_pnl=net,
        profit_factor=gross_profit / gross_loss if gross_loss > 0 else None,
        avg_win=avg_win, avg_loss=avg_loss,
        payoff=avg_win / avg_loss if avg_loss > 0 else None,
        expectancy=net / len(pnl) if pnl else 0.0,
        max_drawdown=max_dd, max_drawdown_pct=max_dd_pct,
        return_pct=(last - initial) / initial if initial else 0.0,
        sortino=sortino, best_day=best_day, consistency=consistency,
        trading_days=len(by_day),
    )


def ranking_score(m: Metrics, max_drawdown_limit: float, breached: bool,
                  min_trades: int = 20, min_days: int = 10) -> tuple[float, bool]:
    """Score 0–100 que premia consistência e gestão de risco, não sorte."""
    clamp = lambda x: max(0.0, min(1.0, x))
    pf = m.profit_factor if m.profit_factor is not None else (3.0 if m.gross_profit > 0 else 0.0)
    score = 100 * (
        0.30 * clamp((m.sortino or 0.0) / 3)
        + 0.25 * clamp(1 - m.max_drawdown / max_drawdown_limit if max_drawdown_limit else 0.0)
        + 0.20 * (m.consistency or 0.0)
        + 0.15 * clamp((pf - 1) / 2)
        + 0.10 * clamp(m.return_pct / 0.10)
    )
    eligible = not breached and m.trades >= min_trades and m.trading_days >= min_days
    return round(score, 2), eligible
