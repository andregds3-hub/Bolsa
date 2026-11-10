"""
Automação (EAs): estratégias programadas rodando contra o MESMO motor do ao vivo,
o que garante paridade entre backtest e conta real simulada.

Produção: código de usuário roda isolado (gVisor/Firecracker, sem rede, cota de CPU)
e fala com o motor só pela API de ordens; aqui as estratégias são internas.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from engine.core import (Account, AccountStatus, BracketSpec, ExecutionEngine, FillModel, FxConverter,
                         Order, OrderType, Position, Quote, RiskLimits, Side)
from engine.market import MarketSimulator, SymbolModel


class Strategy:
    name = "base"

    def __init__(self, engine: ExecutionEngine, account_id: str, symbol: str):
        self.engine, self.account_id, self.symbol = engine, account_id, symbol

    def on_quote(self, q: Quote, now: datetime) -> None:
        raise NotImplementedError

    def position(self) -> Optional[Position]:
        return self.engine.positions.get((self.account_id, self.symbol))

    def enter(self, side: Side, qty: Decimal, now: datetime, bracket: Optional[BracketSpec] = None) -> Order:
        order = Order(self.account_id, self.symbol, side, OrderType.MARKET, qty,
                      bracket=bracket, source=f"EA:{self.name}")
        return self.engine.submit(order, now)


class SmaCrossTrend(Strategy):
    """Seguidor de tendência: cruzamento de médias móveis com bracket OCO."""
    name = "SMA-Cross"

    def __init__(self, engine: ExecutionEngine, account_id: str, symbol: str, fast: int = 10,
                 slow: int = 40, qty: Decimal = Decimal("1"), bracket: Optional[BracketSpec] = None,
                 sample_every: int = 4):
        super().__init__(engine, account_id, symbol)
        if fast >= slow:
            raise ValueError("A média rápida deve ser menor que a lenta.")
        self.fast, self.slow, self.qty, self.bracket = fast, slow, qty, bracket
        self.sample_every = sample_every
        self.prices: deque[Decimal] = deque(maxlen=slow)
        self._ticks = 0
        self._signal = 0

    def on_quote(self, q: Quote, now: datetime) -> None:
        if q.symbol != self.symbol:
            return
        self._ticks += 1
        if self._ticks % self.sample_every:
            return
        self.prices.append((q.bid + q.ask) / 2)
        if len(self.prices) < self.slow:
            return
        fast = sum(list(self.prices)[-self.fast:]) / self.fast
        slow = sum(self.prices) / self.slow
        signal = 1 if fast > slow else -1
        if signal == self._signal:
            return
        first, self._signal = self._signal == 0, signal
        if first:  # espera um cruzamento de verdade
            return
        side = Side.BUY if signal > 0 else Side.SELL
        pos = self.position()
        if pos and pos.side is side:
            return
        if pos:
            self.engine.close_position(self.account_id, self.symbol, now)
        if self.engine.accounts[self.account_id].status is AccountStatus.ACTIVE:
            self.enter(side, self.qty, now, self.bracket)


def backtest(strategy_factory: Callable[[ExecutionEngine, str], Strategy], models: list[SymbolModel],
             steps: int, *, dt_s: float = 1.0, seed: int = 42,
             initial_balance: Decimal = Decimal("100000"), base_ccy: str = "BRL",
             limits: Optional[RiskLimits] = None, start: Optional[datetime] = None):
    """Roda a estratégia num motor isolado sobre o mercado sintético (determinístico pelo seed)."""
    from engine.catalog import update_fx  # evita import circular

    instruments = {m.instrument.symbol: m.instrument for m in models}
    fx = FxConverter()
    engine = ExecutionEngine(instruments, fx, FillModel())
    sim = MarketSimulator(models, seed=seed)
    now = start or datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)
    limits = limits or RiskLimits(daily_loss_limit=initial_balance, max_drawdown=initial_balance,
                                  block_orders_risking_breach=False)
    acc = engine.add_account(Account("backtest", base_ccy, initial_balance, initial_balance, limits), now)
    strategy = strategy_factory(engine, acc.id)
    for _ in range(steps):
        result = sim.step(now, dt_s)
        update_fx(fx, result.quotes)
        engine.regimes.update(result.regimes)
        for q in result.quotes:
            engine.on_quote(q, now)
            strategy.on_quote(q, now)
        now += timedelta(seconds=dt_s)
    for p in engine.positions_of(acc.id):  # encerra o que ficou aberto no fim do teste
        engine.close_position(acc.id, p.symbol, now)
    return acc, engine.trades[acc.id], list(engine.equity_history[acc.id])
