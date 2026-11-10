"""
Núcleo do motor de execução + guarda de risco estilo Prop Firm.

Princípios:
- Single-writer por conta: em produção cada shard (ator asyncio) é dono de um
  subconjunto de contas e processa comandos/ticks em série → sem locks no saldo.
- Event-time: toda função recebe `now`; o mesmo código roda ao vivo, em replay e backtest.
- Decimal em todo cálculo monetário. `emit` publica eventos (NATS → event store / WebSocket).
"""
from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from enum import Enum
from typing import Callable, Optional
from zoneinfo import ZoneInfo

ZERO = Decimal("0")
EQUITY_SAMPLE_EVERY = timedelta(seconds=1)
ORDER_HISTORY_LIMIT = 5_000
EQUITY_HISTORY_LIMIT = 50_000


# ───────────────────────────── Domínio ─────────────────────────────
class Side(int, Enum):
    BUY = 1
    SELL = -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderStatus(str, Enum):
    WORKING = "WORKING"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class AccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DAILY_LOCKED = "DAILY_LOCKED"  # bateu Daily Loss: travada até o próximo reset
    BREACHED = "BREACHED"          # bateu Max Drawdown: desafio reprovado (definitivo)
    PASSED = "PASSED"              # atingiu a meta do desafio


class OrderRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Instrument:
    symbol: str
    quote_ccy: str                # moeda em que o P&L nasce (WIN→BRL, XAUUSD→USD, USDJPY→JPY)
    tick_size: Decimal            # WIN 5 | WDO 0.5 | EURUSD 0.00001
    tick_value: Decimal           # valor de 1 tick por unidade (WIN R$1 | WDO R$5 | EURUSD 1 lote = US$1)
    commission_per_unit: Decimal  # por contrato/lote, por lado, na quote_ccy
    margin_per_unit: Decimal      # simplificação; FX real usa notional / alavancagem
    lot_step: Decimal = Decimal("1")  # B3 lote padrão 100 | FX 0.01
    venue: str = ""
    description: str = ""

    def to_tick(self, price: Decimal, rounding: str = ROUND_HALF_EVEN) -> Decimal:
        return (price / self.tick_size).to_integral_value(rounding=rounding) * self.tick_size

    def pnl(self, side: Side, entry: Decimal, exit_: Decimal, qty: Decimal) -> Decimal:
        return (exit_ - entry) / self.tick_size * self.tick_value * qty * side.value


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    ts: datetime


@dataclass
class MarketRegime:
    """Escrito pelo Simulador Macro: notícia de alto impacto alarga o spread e retira
    liquidez do topo do book; ambos decaem exponencialmente de volta a 1."""
    spread_mult: Decimal = Decimal("1")
    depth_mult: Decimal = Decimal("1")  # 0.2 = só 20% da liquidez normal

    def apply(self, q: Quote, inst: Instrument) -> Quote:
        if self.spread_mult == 1 and self.depth_mult == 1:
            return q
        mid, half = (q.bid + q.ask) / 2, (q.ask - q.bid) / 2 * self.spread_mult
        return Quote(q.symbol,
                     bid=inst.to_tick(mid - half, ROUND_FLOOR),
                     ask=inst.to_tick(mid + half, ROUND_CEILING),
                     bid_size=q.bid_size * self.depth_mult,
                     ask_size=q.ask_size * self.depth_mult,
                     ts=q.ts)


@dataclass(frozen=True)
class BracketSpec:
    """OCO anexado à entrada. Distâncias em ticks a partir do preço de execução."""
    stop_loss_ticks: Optional[int] = None
    take_profit_ticks: Optional[int] = None
    trailing_ticks: Optional[int] = None      # distância do trailing stop
    trailing_activation_ticks: int = 0        # lucro mínimo para o trailing começar a andar
    breakeven_trigger_ticks: Optional[int] = None
    breakeven_offset_ticks: int = 0           # +N ticks para cobrir custos


@dataclass
class Order:
    account_id: str
    symbol: str
    side: Side
    kind: OrderType
    qty: Decimal
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    reduce_only: bool = False
    oco_group: Optional[str] = None
    bracket: Optional[BracketSpec] = None
    role: str = "ENTRY"      # ENTRY | STOP_LOSS | BREAKEVEN | TRAILING | TAKE_PROFIT | MANUAL | LIQUIDATION
    source: str = "MANUAL"   # MANUAL | EA:<nome> | RISK_ENGINE
    id: str = ""
    status: OrderStatus = OrderStatus.WORKING
    fill_price: Optional[Decimal] = None
    reject_code: Optional[str] = None
    reject_reason: Optional[str] = None

    def __post_init__(self) -> None:
        self.id = self.id or uuid.uuid4().hex


@dataclass
class Position:  # modo NETTING: uma posição por (conta, símbolo)
    account_id: str
    symbol: str
    side: Side
    qty: Decimal
    avg_price: Decimal
    bracket: Optional[BracketSpec] = None
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    breakeven_done: bool = False
    opened_at: Optional[datetime] = None
    fees_base: Decimal = ZERO   # custos de entrada ainda não atribuídos a um trade fechado


@dataclass(frozen=True)
class ClosedTrade:
    """Round-trip fechado: a matéria-prima do Trading Journal."""
    account_id: str
    symbol: str
    side: Side
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    gross_pnl_base: Decimal
    fees_base: Decimal
    exit_reason: str

    @property
    def net_pnl_base(self) -> Decimal:
        return self.gross_pnl_base - self.fees_base


@dataclass
class RiskLimits:
    daily_loss_limit: Decimal
    max_drawdown: Decimal
    trailing_drawdown: bool = False            # True = trailing intradiário, trava no saldo inicial
    stop_out_level: Decimal = Decimal("0.5")   # equity / margem usada
    block_orders_risking_breach: bool = True
    profit_target: Optional[Decimal] = None
    min_trading_days: int = 0
    news_blackout: bool = False                # proíbe abrir posição na janela de notícia HIGH
    reset_tz: str = "America/New_York"
    reset_time: time = time(17, 0)


@dataclass
class Account:
    id: str
    base_ccy: str
    initial_balance: Decimal
    balance: Decimal
    limits: RiskLimits
    name: str = ""
    plan: str = "PRACTICE"
    status: AccountStatus = AccountStatus.ACTIVE
    equity: Decimal = ZERO
    used_margin: Decimal = ZERO
    high_water_mark: Decimal = ZERO
    day_key: Optional[date] = None
    day_anchor: Decimal = ZERO
    breach_reason: Optional[str] = None
    reset_count: int = 0
    trading_days: set[date] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.equity = self.equity or self.balance
        self.high_water_mark = self.high_water_mark or self.balance


# ───────────────────────────── Serviços ─────────────────────────────
class FxConverter:
    def __init__(self) -> None:
        self._mid: dict[tuple[str, str], Decimal] = {}  # (EUR, USD) = 1.08 → 1 EUR = 1.08 USD

    def update(self, base: str, quote: str, mid: Decimal) -> None:
        self._mid[(base, quote)] = mid

    def convert(self, amount: Decimal, src: str, dst: str) -> Decimal:
        if src == dst:
            return amount
        if (src, dst) in self._mid:
            return amount * self._mid[(src, dst)]
        if (dst, src) in self._mid:
            return amount / self._mid[(dst, src)]
        if "USD" not in (src, dst):  # triangulação: JPY → USD → BRL
            return self.convert(self.convert(amount, src, "USD"), "USD", dst)
        raise KeyError(f"Sem cotação {src}/{dst}")


class FillModel:
    """Executa no lado agressor do book + slippage proporcional à profundidade consumida.
    Produção: 'varrer' os níveis reais do DOM em vez de só o topo."""

    def __init__(self, base_slippage_ticks: Decimal = ZERO,
                 impact_ticks_per_depth: Decimal = Decimal("1")):
        self.base, self.impact = base_slippage_ticks, impact_ticks_per_depth

    def market_price(self, inst: Instrument, side: Side, qty: Decimal, q: Quote) -> Decimal:
        touch, depth = (q.ask, q.ask_size) if side is Side.BUY else (q.bid, q.bid_size)
        consumed = qty / depth if depth > 0 else Decimal("10")
        slip = (self.base + self.impact * consumed).to_integral_value(rounding=ROUND_FLOOR)
        return touch + side.value * slip * inst.tick_size


class RiskGuard:
    """Regras de mesa proprietária: Daily Loss, Max Drawdown (estático/trailing), margem, meta."""

    def __init__(self, fx: FxConverter):
        self.fx = fx

    @staticmethod
    def trading_day(now: datetime, limits: RiskLimits) -> date:
        local = now.astimezone(ZoneInfo(limits.reset_tz))
        return local.date() + timedelta(days=1) if local.time() >= limits.reset_time else local.date()

    @staticmethod
    def next_reset(now: datetime, limits: RiskLimits) -> datetime:
        tz = ZoneInfo(limits.reset_tz)
        local = now.astimezone(tz)
        candidate = datetime.combine(local.date(), limits.reset_time, tzinfo=tz)
        if local >= candidate:
            candidate = datetime.combine(local.date() + timedelta(days=1), limits.reset_time, tzinfo=tz)
        return candidate

    def roll_day_if_needed(self, acc: Account, now: datetime) -> None:
        key = self.trading_day(now, acc.limits)
        if key != acc.day_key:
            acc.day_key = key
            acc.day_anchor = max(acc.balance, acc.equity)  # regra "maior entre saldo e equity"
            if acc.status is AccountStatus.DAILY_LOCKED:
                acc.status, acc.breach_reason = AccountStatus.ACTIVE, None

    @staticmethod
    def daily_floor(acc: Account) -> Decimal:
        return acc.day_anchor - acc.limits.daily_loss_limit

    @staticmethod
    def total_floor(acc: Account) -> Decimal:
        if acc.limits.trailing_drawdown:  # sobe com o topo de equity e para no saldo inicial
            return min(acc.high_water_mark - acc.limits.max_drawdown, acc.initial_balance)
        return acc.initial_balance - acc.limits.max_drawdown

    @staticmethod
    def daily_loss_used(acc: Account) -> Decimal:
        """Fração do limite diário já consumida (0..1), para a barra de risco da UI."""
        limit = acc.limits.daily_loss_limit
        if limit <= 0:
            return ZERO
        return min(Decimal("1"), max(ZERO, acc.day_anchor - acc.equity) / limit)

    def pre_trade(self, acc: Account, order: Order, inst: Instrument) -> None:
        if acc.status is not AccountStatus.ACTIVE and not order.reduce_only:
            raise OrderRejected("ACCOUNT_LOCKED",
                                f"Conta {acc.status.value}: novas operações bloqueadas.")
        if order.reduce_only:
            return  # reduzir risco é sempre permitido
        required = self.fx.convert(inst.margin_per_unit * order.qty, inst.quote_ccy, acc.base_ccy)
        if acc.equity - acc.used_margin < required:
            raise OrderRejected("INSUFFICIENT_MARGIN",
                                f"Margem livre insuficiente (requer {required:.2f} {acc.base_ccy}).")
        b = order.bracket
        if acc.limits.block_orders_risking_breach and b and b.stop_loss_ticks is not None:
            loss_at_stop = self.fx.convert(
                (b.stop_loss_ticks * inst.tick_value + 2 * inst.commission_per_unit) * order.qty,
                inst.quote_ccy, acc.base_ccy)
            if acc.equity - loss_at_stop <= self.daily_floor(acc):
                raise OrderRejected(
                    "DAILY_LOSS_AT_RISK",
                    f"Se o stop for atingido (−{loss_at_stop:.2f}) a perda diária ultrapassa o "
                    f"limite (piso {self.daily_floor(acc):.2f}). Reduza o tamanho ou o stop.")

    def evaluate(self, acc: Account) -> Optional[str]:
        if acc.status is not AccountStatus.ACTIVE:
            return None
        if acc.equity <= self.total_floor(acc):
            return "MAX_DRAWDOWN"
        if acc.equity <= self.daily_floor(acc):
            return "DAILY_LOSS"
        if acc.used_margin > 0 and acc.equity / acc.used_margin < acc.limits.stop_out_level:
            return "STOP_OUT"
        lim = acc.limits
        if (lim.profit_target is not None and acc.used_margin == 0
                and acc.balance >= acc.initial_balance + lim.profit_target
                and len(acc.trading_days) >= lim.min_trading_days):
            return "PROFIT_TARGET"
        return None


# ───────────────────────────── Motor ─────────────────────────────
class ExecutionEngine:
    def __init__(self, instruments: dict[str, Instrument], fx: FxConverter,
                 fill_model: FillModel, emit: Optional[Callable[[str, dict], None]] = None):
        self.instruments, self.fx, self.fill = instruments, fx, fill_model
        self.emit: Callable[[str, dict], None] = emit or (lambda topic, payload: None)
        self.risk = RiskGuard(fx)
        self.accounts: dict[str, Account] = {}
        self.orders: dict[str, Order] = {}  # só ordens WORKING (produção: índice por símbolo/preço)
        self.order_history: deque[Order] = deque(maxlen=ORDER_HISTORY_LIMIT)
        self.positions: dict[tuple[str, str], Position] = {}
        self.trades: dict[str, list[ClosedTrade]] = {}
        self.equity_history: dict[str, deque[tuple[datetime, Decimal]]] = {}
        self.quotes: dict[str, Quote] = {}
        self.regimes: dict[str, MarketRegime] = {}
        self.blackouts: list[tuple[datetime, datetime, frozenset[str]]] = []

    # ── contas ──
    def add_account(self, acc: Account, now: datetime) -> Account:
        self.accounts[acc.id] = acc
        self.trades.setdefault(acc.id, [])
        self.risk.roll_day_if_needed(acc, now)
        self._sample_equity(acc, now, force=True)
        return acc

    def remove_account(self, account_id: str) -> None:
        """Exclusão definitiva (LGPD): some do motor sem deixar ordens ou posições."""
        for o in list(self.orders.values()):
            if o.account_id == account_id:
                del self.orders[o.id]
        for key in [k for k in self.positions if k[0] == account_id]:
            del self.positions[key]
        self.accounts.pop(account_id, None)
        self.trades.pop(account_id, None)
        self.equity_history.pop(account_id, None)
        self.order_history = deque((o for o in self.order_history if o.account_id != account_id),
                                   maxlen=ORDER_HISTORY_LIMIT)

    def reset_account(self, account_id: str, now: datetime) -> Account:
        """Reset: zera posições/ordens sem executar e restaura o saldo inicial."""
        acc = self.accounts[account_id]
        for o in list(self.orders.values()):
            if o.account_id == account_id:
                self._cancel(o, "RESET")
        for key in [k for k in self.positions if k[0] == account_id]:
            del self.positions[key]
        acc.balance = acc.equity = acc.high_water_mark = acc.initial_balance
        acc.used_margin, acc.status, acc.breach_reason = ZERO, AccountStatus.ACTIVE, None
        acc.day_key = None
        acc.trading_days.clear()
        acc.reset_count += 1
        self.trades[account_id] = []
        self.equity_history.pop(account_id, None)
        self.risk.roll_day_if_needed(acc, now)
        self._sample_equity(acc, now, force=True)
        self.emit("account.reset", {"account_id": acc.id, "reset_count": acc.reset_count})
        return acc

    # ── comandos ──
    def submit(self, order: Order, now: datetime) -> Order:
        acc, inst = self.accounts[order.account_id], self.instruments[order.symbol]
        self.risk.roll_day_if_needed(acc, now)
        try:
            self._validate(order, inst)
            if order.symbol not in self.quotes:
                raise OrderRejected("NO_QUOTE", f"Sem cotação para {order.symbol}.")
            if acc.limits.news_blackout and not order.reduce_only and self.in_blackout(order.symbol, now):
                raise OrderRejected("NEWS_BLACKOUT",
                                    "Regra da mesa: novas posições bloqueadas na janela de notícia de alto impacto.")
            self.risk.pre_trade(acc, order, inst)
        except OrderRejected as e:
            order.status, order.reject_code, order.reject_reason = OrderStatus.REJECTED, e.code, str(e)
            self.order_history.append(order)
            self.emit("order.rejected", {"account_id": acc.id, "order_id": order.id,
                                         "symbol": order.symbol, "code": e.code, "reason": str(e)})
            return order
        self._register(order)
        if order.kind is OrderType.MARKET:
            price = self.fill.market_price(inst, order.side, order.qty, self.quotes[order.symbol])
        else:
            price = self._check_trigger(order, inst, aggressive=True)
        if price is not None:
            self._execute(order, price, now)
        return order

    def cancel(self, account_id: str, order_id: str) -> bool:
        o = self.orders.get(order_id)
        if o is None or o.account_id != account_id:
            return False
        self._cancel(o, "USUARIO")
        return True

    def modify(self, account_id: str, order_id: str, *, limit_price: Optional[Decimal] = None,
               stop_price: Optional[Decimal] = None) -> Order:
        o = self.orders.get(order_id)
        if o is None or o.account_id != account_id:
            raise OrderRejected("ORDER_NOT_FOUND", "Ordem não encontrada ou já finalizada.")
        inst = self.instruments[o.symbol]
        for px in (limit_price, stop_price):
            if px is not None and inst.to_tick(px) != px:
                raise OrderRejected("INVALID_TICK", f"Preço {px} fora do tick {inst.tick_size}.")
        if limit_price is not None and o.kind is OrderType.LIMIT:
            o.limit_price = limit_price
        if stop_price is not None and o.kind is OrderType.STOP:
            o.stop_price = stop_price
        self.emit("order.modified", {"account_id": o.account_id, "order_id": o.id,
                                     "limit_price": str(o.limit_price), "stop_price": str(o.stop_price)})
        return o

    def close_position(self, account_id: str, symbol: str, now: datetime) -> bool:
        pos = self.positions.get((account_id, symbol))
        if pos is None:
            return False
        self._close_at_market(pos, now, role="MANUAL", source="MANUAL")
        return True

    # ── consultas ──
    def positions_of(self, account_id: str) -> list[Position]:
        return [p for p in self.positions.values() if p.account_id == account_id]

    def working_orders(self, account_id: str) -> list[Order]:
        return [o for o in self.orders.values() if o.account_id == account_id]

    def in_blackout(self, symbol: str, now: datetime) -> bool:
        return any(start <= now <= end and symbol in symbols for start, end, symbols in self.blackouts)

    def upnl_base(self, p: Position) -> Decimal:
        acc, inst, q = self.accounts[p.account_id], self.instruments[p.symbol], self.quotes[p.symbol]
        exit_px = q.bid if p.side is Side.BUY else q.ask      # valor de liquidação, não o mid
        return self.fx.convert(inst.pnl(p.side, p.avg_price, exit_px, p.qty), inst.quote_ccy, acc.base_ccy)

    # ── fluxo de mercado ──
    def on_quote(self, raw: Quote, now: datetime) -> None:
        inst = self.instruments[raw.symbol]
        q = self.regimes.get(raw.symbol, MarketRegime()).apply(raw, inst)
        self.quotes[q.symbol] = q

        # 1) Gatilhos: entradas pendentes e pernas OCO (SL/TP)
        working = [o for o in self.orders.values() if o.symbol == q.symbol]
        for o in working:
            if o.status is not OrderStatus.WORKING:  # cancelada por OCO/breach neste mesmo tick
                continue
            price = self._check_trigger(o, inst)
            if price is None:
                continue
            if not o.reduce_only:
                try:
                    self.risk.pre_trade(self.accounts[o.account_id], o, inst)
                except OrderRejected as e:
                    self._cancel(o, e.code)
                    continue
            self._execute(o, price, now)

        # 2) Breakeven e trailing (ajustados depois dos gatilhos → valem a partir do próximo tick)
        for pos in [p for p in self.positions.values() if p.symbol == q.symbol]:
            self._manage_stops(pos, q, inst)

        # 3) Mark-to-market + regras de risco de todas as contas expostas ao símbolo
        for acc_id in {p.account_id for p in self.positions.values() if p.symbol == q.symbol}:
            self._mark_to_market(self.accounts[acc_id], now)

    # ── internos ──
    @staticmethod
    def _validate(o: Order, inst: Instrument) -> None:
        if o.qty <= 0:
            raise OrderRejected("INVALID_QTY", "Quantidade deve ser positiva.")
        if (o.qty / inst.lot_step) % 1 != 0:
            raise OrderRejected("INVALID_LOT", f"Quantidade deve ser múltipla de {inst.lot_step}.")
        if (o.kind is OrderType.LIMIT and o.limit_price is None) or \
           (o.kind is OrderType.STOP and o.stop_price is None):
            raise OrderRejected("MISSING_PRICE", f"Ordem {o.kind.value} exige preço.")
        for px in (o.limit_price, o.stop_price):
            if px is not None and inst.to_tick(px) != px:
                raise OrderRejected("INVALID_TICK", f"Preço {px} fora do tick {inst.tick_size}.")

    def _check_trigger(self, o: Order, inst: Instrument, aggressive: bool = False) -> Optional[Decimal]:
        q = self.quotes[o.symbol]
        touch = q.ask if o.side is Side.BUY else q.bid
        if o.kind is OrderType.LIMIT:
            crossed = touch <= o.limit_price if o.side is Side.BUY else touch >= o.limit_price
            if not crossed:
                return None
            if not aggressive:          # passiva no book: executa no próprio limite (conservador)
                return o.limit_price
            mkt = self.fill.market_price(inst, o.side, o.qty, q)  # limite "a mercado" na entrada
            return min(mkt, o.limit_price) if o.side is Side.BUY else max(mkt, o.limit_price)
        if o.kind is OrderType.STOP:
            # Dispara por bid/ask (a B3 dispara pelo último negócio: configurável por venue).
            hit = touch >= o.stop_price if o.side is Side.BUY else touch <= o.stop_price
            # Vira ordem a mercado: em gap executa PIOR que o stop, como no mundo real.
            return self.fill.market_price(inst, o.side, o.qty, q) if hit else None
        return None

    def _execute(self, o: Order, price: Decimal, now: datetime) -> None:
        acc, inst = self.accounts[o.account_id], self.instruments[o.symbol]
        key = (o.account_id, o.symbol)
        if o.reduce_only:
            pos = self.positions.get(key)
            if pos is None or pos.side is o.side:
                self._cancel(o, "REDUCE_ONLY_SEM_POSICAO")
                return
            o.qty = min(o.qty, pos.qty)
        self.risk.roll_day_if_needed(acc, now)
        o.status, o.fill_price = OrderStatus.FILLED, price
        self._finish(o)
        commission = self.fx.convert(inst.commission_per_unit * o.qty, inst.quote_ccy, acc.base_ccy)
        acc.balance -= commission
        acc.trading_days.add(acc.day_key)
        closed_any, closed_or_flipped = self._apply_to_position(acc, inst, o, price, commission, now)
        self.emit("order.filled", {"account_id": acc.id, "order_id": o.id, "symbol": o.symbol,
                                   "side": o.side.name, "role": o.role,
                                   "price": str(price), "qty": str(o.qty)})
        if o.oco_group:                       # OCO: uma perna executou → cancela as irmãs
            self._cancel_group(o.oco_group, except_id=o.id)
        if closed_or_flipped:                 # posição zerada/invertida → SL/TP órfãos saem
            self._cancel_children(key)
        pos = self.positions.get(key)
        if pos and o.bracket and not o.reduce_only:
            self._attach_bracket(pos, o.bracket)
        self._mark_to_market(acc, now, force_sample=closed_any)

    def _apply_to_position(self, acc: Account, inst: Instrument, o: Order, price: Decimal,
                           commission: Decimal, now: datetime) -> tuple[bool, bool]:
        """Netting. Retorna (fechou algum trade, posição anterior zerada ou invertida)."""
        key = (acc.id, o.symbol)
        pos = self.positions.get(key)
        if pos is None:
            self.positions[key] = Position(acc.id, o.symbol, o.side, o.qty, price,
                                           opened_at=now, fees_base=commission)
            return False, False
        if pos.side is o.side:                # aumento: preço médio ponderado
            pos.avg_price = (pos.avg_price * pos.qty + price * o.qty) / (pos.qty + o.qty)
            pos.qty += o.qty
            pos.fees_base += commission
            return False, False
        closed = min(pos.qty, o.qty)
        entry_fees = pos.fees_base * closed / pos.qty
        exit_fees = commission * closed / o.qty
        gross = self.fx.convert(inst.pnl(pos.side, pos.avg_price, price, closed),
                                inst.quote_ccy, acc.base_ccy)
        acc.balance += gross
        reason = "MANUAL" if o.role == "ENTRY" else o.role
        trade = ClosedTrade(acc.id, o.symbol, pos.side, closed, pos.avg_price, price,
                            pos.opened_at or now, now, gross, entry_fees + exit_fees, reason)
        self.trades.setdefault(acc.id, []).append(trade)
        self.emit("trade.closed", {"account_id": acc.id, "symbol": o.symbol, "qty": str(closed),
                                   "entry": str(pos.avg_price), "exit": str(price),
                                   "net_pnl_base": str(trade.net_pnl_base), "reason": reason})
        pos.qty -= closed
        pos.fees_base -= entry_fees
        if pos.qty > 0:
            return True, False
        del self.positions[key]
        if o.qty > closed:                    # virada de mão
            self.positions[key] = Position(acc.id, o.symbol, o.side, o.qty - closed, price,
                                           opened_at=now, fees_base=commission - exit_fees)
        return True, True

    def _attach_bracket(self, pos: Position, b: BracketSpec) -> None:
        inst = self.instruments[pos.symbol]
        self._cancel_children((pos.account_id, pos.symbol))
        group, exit_side, entry = uuid.uuid4().hex, pos.side.opposite, inst.to_tick(pos.avg_price)
        pos.bracket, pos.breakeven_done, pos.sl_order_id, pos.tp_order_id = b, False, None, None
        sl_ticks = b.stop_loss_ticks if b.stop_loss_ticks is not None else b.trailing_ticks
        if sl_ticks is not None:
            sl = Order(pos.account_id, pos.symbol, exit_side, OrderType.STOP, pos.qty,
                       stop_price=entry - pos.side.value * sl_ticks * inst.tick_size,
                       reduce_only=True, oco_group=group, role="STOP_LOSS")
            self._register(sl)
            pos.sl_order_id = sl.id
        if b.take_profit_ticks is not None:
            tp = Order(pos.account_id, pos.symbol, exit_side, OrderType.LIMIT, pos.qty,
                       limit_price=entry + pos.side.value * b.take_profit_ticks * inst.tick_size,
                       reduce_only=True, oco_group=group, role="TAKE_PROFIT")
            self._register(tp)
            pos.tp_order_id = tp.id

    def _manage_stops(self, pos: Position, q: Quote, inst: Instrument) -> None:
        b = pos.bracket
        sl = self.orders.get(pos.sl_order_id or "")
        if b is None or sl is None:
            return
        mark = q.bid if pos.side is Side.BUY else q.ask       # preço pelo qual sairia
        profit_ticks = (mark - pos.avg_price) * pos.side.value / inst.tick_size
        new_stop, role = sl.stop_price, sl.role
        if (b.breakeven_trigger_ticks is not None and not pos.breakeven_done
                and profit_ticks >= b.breakeven_trigger_ticks):
            be = inst.to_tick(pos.avg_price + pos.side.value * b.breakeven_offset_ticks * inst.tick_size)
            if self._tighter(pos.side, new_stop, be) != new_stop:
                new_stop, role = be, "BREAKEVEN"
            pos.breakeven_done = True
        if b.trailing_ticks is not None and profit_ticks >= b.trailing_activation_ticks:
            trail = mark - pos.side.value * b.trailing_ticks * inst.tick_size
            if self._tighter(pos.side, new_stop, trail) != new_stop:  # só anda a favor
                new_stop, role = trail, "TRAILING"
        if new_stop != sl.stop_price:
            sl.stop_price, sl.role = new_stop, role
            self.emit("order.modified", {"account_id": sl.account_id, "order_id": sl.id,
                                         "stop_price": str(new_stop), "role": role})

    @staticmethod
    def _tighter(side: Side, current: Decimal, candidate: Decimal) -> Decimal:
        return max(current, candidate) if side is Side.BUY else min(current, candidate)

    def _mark_to_market(self, acc: Account, now: datetime, force_sample: bool = False) -> None:
        upnl = margin = ZERO
        for p in self.positions_of(acc.id):
            inst = self.instruments[p.symbol]
            upnl += self.upnl_base(p)
            margin += self.fx.convert(inst.margin_per_unit * p.qty, inst.quote_ccy, acc.base_ccy)
        acc.equity, acc.used_margin = acc.balance + upnl, margin
        acc.high_water_mark = max(acc.high_water_mark, acc.equity)  # trailing EOD: só no reset
        self.risk.roll_day_if_needed(acc, now)
        self._sample_equity(acc, now, force=force_sample)
        breach = self.risk.evaluate(acc)
        if breach:
            self._handle_breach(acc, breach, now)

    def _sample_equity(self, acc: Account, now: datetime, force: bool = False) -> None:
        history = self.equity_history.setdefault(acc.id, deque(maxlen=EQUITY_HISTORY_LIMIT))
        if force or not history or now - history[-1][0] >= EQUITY_SAMPLE_EVERY:
            history.append((now, acc.equity))

    def _handle_breach(self, acc: Account, reason: str, now: datetime) -> None:
        if reason == "STOP_OUT":
            # Fecha a posição mais perdedora; o mark-to-market disparado pela execução
            # reavalia a margem e fecha a próxima se ainda necessário (comportamento MT5).
            worst = min(self.positions_of(acc.id), key=self.upnl_base)
            self.emit("risk.stop_out", {"account_id": acc.id, "symbol": worst.symbol,
                                        "equity": str(acc.equity), "used_margin": str(acc.used_margin)})
            self._close_at_market(worst, now)
            return
        if reason == "PROFIT_TARGET":
            acc.status = AccountStatus.PASSED
            for o in self.working_orders(acc.id):
                self._cancel(o, reason)
            self.emit("challenge.passed", {"account_id": acc.id, "balance": str(acc.balance)})
            return
        # Status ANTES de liquidar: impede reentrada na avaliação durante o flatten.
        acc.status = AccountStatus.BREACHED if reason == "MAX_DRAWDOWN" else AccountStatus.DAILY_LOCKED
        acc.breach_reason = reason
        for o in self.working_orders(acc.id):
            self._cancel(o, reason)
        for p in self.positions_of(acc.id):
            self._close_at_market(p, now)
        self.emit("risk.breach", {"account_id": acc.id, "reason": reason,
                                  "equity": str(acc.equity), "balance": str(acc.balance)})

    def _close_at_market(self, p: Position, now: datetime, role: str = "LIQUIDATION",
                         source: str = "RISK_ENGINE") -> None:
        inst, q = self.instruments[p.symbol], self.quotes[p.symbol]
        o = Order(p.account_id, p.symbol, p.side.opposite, OrderType.MARKET, p.qty,
                  reduce_only=True, role=role, source=source)
        self._register(o)
        self._execute(o, self.fill.market_price(inst, o.side, o.qty, q), now)

    def _register(self, o: Order) -> None:
        self.orders[o.id] = o
        self.emit("order.accepted", {"account_id": o.account_id, "order_id": o.id, "symbol": o.symbol,
                                     "kind": o.kind.value, "side": o.side.name, "role": o.role,
                                     "qty": str(o.qty), "oco_group": o.oco_group})

    def _finish(self, o: Order) -> None:
        self.orders.pop(o.id, None)
        self.order_history.append(o)

    def _cancel(self, o: Order, reason: str) -> None:
        o.status = OrderStatus.CANCELED
        self._finish(o)
        self.emit("order.canceled", {"account_id": o.account_id, "order_id": o.id,
                                     "symbol": o.symbol, "role": o.role, "reason": reason})

    def _cancel_group(self, group: str, except_id: str) -> None:
        for o in list(self.orders.values()):
            if o.oco_group == group and o.id != except_id:
                self._cancel(o, "OCO")

    def _cancel_children(self, key: tuple[str, str]) -> None:
        for o in list(self.orders.values()):
            if o.reduce_only and (o.account_id, o.symbol) == key:
                self._cancel(o, "POSICAO_ENCERRADA")
