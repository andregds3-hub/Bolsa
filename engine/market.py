"""
Market data sintético + Simulador Macro (choques de notícia).

O motor não sabe de onde vem o tick: em modo Live/Replay o preço vem do feed e só o
regime (spread/liquidez) é sobreposto; em modo Sintético o preço também é gerado aqui.

Realismo do gerador:
- fator de mercado comum ("BR", "US"): ações, índice e ETFs andam juntos conforme o beta;
- liquidez: ativos ilíquidos têm preço contínuo mas cotam só de vez em quando (`activity`);
- derivados: fracionário e vencimentos seguintes espelham o ativo-base (com carrego),
  opções são precificadas por Black-Scholes com smile sobre o preço do ativo-objeto.
"""
from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

from engine.core import Instrument, MarketRegime, Quote
from engine.options import black_scholes

_NEUTRAL = MarketRegime()


@dataclass
class SymbolModel:
    instrument: Instrument
    price: float
    annual_vol: float                   # 0.25 = 25% a.a.
    spread_ticks: int = 1
    depth: float = 50.0                 # quantidade média no topo do book
    activity: float = 1.0               # probabilidade de sair cotação a cada passo (liquidez)
    factor: Optional[str] = None        # fator de mercado comum ("BR", "US")
    beta: float = 0.0                   # exposição ao fator (−1..1)
    underlying: Optional[str] = None    # derivado: espelho (fracionário, vencimento seguinte) ou opção
    basis: float = 1.0                  # espelho: preço = base × basis (carrego do vencimento seguinte)
    option_type: Optional[str] = None   # "C" | "P"
    strike: float = 0.0
    expiry: Optional[datetime] = None
    rate: float = 0.14
    ref_price: float = 0.0              # fechamento de referência para a variação do dia

    def __post_init__(self) -> None:
        self.ref_price = self.ref_price or self.price


def option_iv(base_vol: float, spot: float, strike: float) -> float:
    """Smile simples: vol sobe com a distância do strike, com leve skew de puts."""
    moneyness = math.log(strike / spot)
    return max(0.05, base_vol * (1 + 0.8 * abs(moneyness)) - 0.05 * moneyness)


@dataclass(frozen=True)
class ShockProfile:
    spread_mult: float = 4.0      # spread no instante do release
    depth_mult: float = 0.2       # liquidez remanescente no topo do book
    vol_mult: float = 4.0         # volatilidade no instante do release
    jump_pct_per_z: float = 0.002  # salto de 0,2% por desvio-padrão de surpresa
    half_life_s: float = 90.0     # meia-vida do choque
    pre_release_s: float = 60.0   # market makers recolhem liquidez antes do dado


@dataclass
class EconomicEvent:
    id: str
    title: str
    country: str
    impact: str                   # LOW | MEDIUM | HIGH
    scheduled_at: datetime
    symbols: dict[str, float]     # símbolo → sensibilidade (sinal e intensidade do salto)
    forecast: float
    surprise_std: float
    previous: Optional[float] = None
    actual: Optional[float] = None
    profile: ShockProfile = field(default_factory=ShockProfile)
    released: bool = False

    @property
    def surprise_z(self) -> float:
        if self.actual is None or self.surprise_std <= 0:
            return 0.0
        return (self.actual - self.forecast) / self.surprise_std


@dataclass
class StepResult:
    quotes: list[Quote]
    regimes: dict[str, MarketRegime]
    released: list[EconomicEvent]


class MarketSimulator:
    """GBM com fator comum e volatilidade dependente do regime + saltos nas notícias."""
    SECONDS_PER_YEAR = 252 * 8.5 * 3600  # ano de pregão

    def __init__(self, models: list[SymbolModel], seed: Optional[int] = None):
        self.models = {m.instrument.symbol: m for m in models}
        self.rng = random.Random(seed)
        self.events: list[EconomicEvent] = []

    def schedule(self, event: EconomicEvent) -> None:
        self.events.append(event)
        self.events.sort(key=lambda e: e.scheduled_at)

    def prune(self, now: datetime, keep_s: float = 900) -> None:
        self.events = [e for e in self.events
                       if not e.released or (now - e.scheduled_at).total_seconds() < keep_s]

    def regime_at(self, symbol: str, now: datetime) -> tuple[MarketRegime, float]:
        spread = depth = vol = 1.0
        touched = False
        for ev in self.events:
            if symbol not in ev.symbols:
                continue
            p = ev.profile
            dt = (now - ev.scheduled_at).total_seconds()
            if -p.pre_release_s <= dt < 0:
                depth, spread, touched = min(depth, 0.5), max(spread, 1.5), True
            elif dt >= 0 and ev.released:
                decay = 0.5 ** (dt / p.half_life_s)
                if decay < 0.01:
                    continue
                spread = max(spread, 1 + (p.spread_mult - 1) * decay)
                depth = min(depth, 1 - (1 - p.depth_mult) * decay)
                vol = max(vol, 1 + (p.vol_mult - 1) * decay)
                touched = True
        if not touched:
            return _NEUTRAL, 1.0
        return MarketRegime(Decimal(str(round(spread, 2))), Decimal(str(round(depth, 2)))), vol

    def step(self, now: datetime, dt_s: float) -> StepResult:
        released = self._release(now)
        factor_shocks: dict[str, float] = {}
        quotes: list[Quote] = []
        regimes: dict[str, MarketRegime] = {}
        for symbol, m in self.models.items():  # 1) ativos-base: o preço evolui sempre
            if m.underlying:
                continue
            regime, vol_mult = self.regime_at(symbol, now)
            z = self.rng.gauss(0, 1)
            if m.factor:
                f = factor_shocks.get(m.factor)
                if f is None:
                    f = factor_shocks[m.factor] = self.rng.gauss(0, 1)
                z = m.beta * f + math.sqrt(max(0.0, 1 - m.beta * m.beta)) * z
            sigma = m.annual_vol * vol_mult * math.sqrt(dt_s / self.SECONDS_PER_YEAR)
            m.price *= math.exp(sigma * z - 0.5 * sigma * sigma)
            regimes[symbol] = regime
            if m.activity >= 1 or self.rng.random() < m.activity:  # ... mas só cota se houver negócio
                quotes.append(self._quote(m, now))
        for symbol, m in self.models.items():  # 2) derivados, a partir do preço já atualizado
            base = self.models.get(m.underlying) if m.underlying else None
            if base is None:
                continue
            m.price = self._derived_price(m, base, now)
            regimes[symbol] = regimes.get(m.underlying, _NEUTRAL) if m.option_type is None else _NEUTRAL
            if self.rng.random() < m.activity:
                quotes.append(self._quote(m, now))
        return StepResult(quotes, regimes, released)

    def _release(self, now: datetime) -> list[EconomicEvent]:
        released = []
        for ev in self.events:
            if ev.released or ev.scheduled_at > now:
                continue
            if ev.actual is None:
                ev.actual = round(ev.forecast + self.rng.gauss(0, ev.surprise_std), 4)
            ev.released = True
            released.append(ev)
            z = max(-4.0, min(4.0, ev.surprise_z))
            for symbol, beta in ev.symbols.items():
                if symbol in self.models:
                    self.models[symbol].price *= 1 + ev.profile.jump_pct_per_z * z * beta
        return released

    @staticmethod
    def _derived_price(m: SymbolModel, base: SymbolModel, now: datetime) -> float:
        if m.option_type is None:
            return base.price * m.basis
        t = max(0.0, (m.expiry - now).total_seconds()) / (365 * 86400) if m.expiry else 0.0
        vol = option_iv(base.annual_vol, base.price, m.strike)
        return black_scholes(base.price, m.strike, t, m.rate, vol, m.option_type).price

    def _quote(self, m: SymbolModel, now: datetime) -> Quote:
        inst = m.instrument
        tick = float(inst.tick_size)
        # opções: spread proporcional ao prêmio (~4%), mínimo de 1 tick
        spread = m.spread_ticks if m.option_type is None else max(1, int(m.price * 0.04 / tick))
        bid = Decimal(max(1, round(m.price / tick - spread / 2))) * inst.tick_size
        ask = bid + spread * inst.tick_size
        size = lambda: Decimal(max(1, round(m.depth * self.rng.uniform(0.5, 1.5))))
        return Quote(inst.symbol, bid, ask, size(), size(), now)


@dataclass(slots=True)
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class CandleSeries:
    def __init__(self, interval_s: int = 5, maxlen: int = 720):
        self.interval_s = interval_s
        self.candles: deque[Candle] = deque(maxlen=maxlen)

    def add(self, ts: datetime, price: float, volume: float = 0.0) -> Candle:
        bucket = int(ts.timestamp()) // self.interval_s * self.interval_s
        last = self.candles[-1] if self.candles else None
        if last and last.time == bucket:
            last.high, last.low, last.close = max(last.high, price), min(last.low, price), price
            last.volume += volume
            return last
        candle = Candle(bucket, price, price, price, price, volume)
        self.candles.append(candle)
        return candle

    def extend(self, candles: list[Candle]) -> None:
        self.candles.extend(candles)


def aggregate(candles: list[Candle], interval_s: int) -> list[Candle]:
    """Agrupa candles menores em um timeframe maior (ex.: 5 s → 15 s, 1 min → 5 min)."""
    out: list[Candle] = []
    for c in candles:
        bucket = c.time // interval_s * interval_s
        last = out[-1] if out else None
        if last and last.time == bucket:
            last.high, last.low, last.close = max(last.high, c.high), min(last.low, c.low), c.close
            last.volume += c.volume
        else:
            out.append(Candle(bucket, c.open, c.high, c.low, c.close, c.volume))
    return out


def traded_volume(depth: float, ticks_moved: float) -> float:
    """Volume simulado de um negócio: fração da liquidez do topo, maior quando o preço anda."""
    return round(depth * 0.08 * (1 + min(5.0, ticks_moved)), 2)


def synthetic_history(m: SymbolModel, end: datetime, minutes: int, rng: random.Random) -> list[Candle]:
    """Histórico de candles de 1 min que TERMINA no preço atual do modelo (continuidade com o ao vivo).
    GBM com 4 sub-passos por candle para gerar máxima e mínima realistas."""
    sub, tick = 4, float(m.instrument.tick_size)
    sigma = m.annual_vol * math.sqrt(60 / sub / MarketSimulator.SECONDS_PER_YEAR)
    cum, path = 0.0, [0.0]
    for _ in range(minutes * sub):
        cum += sigma * rng.gauss(0, 1) - 0.5 * sigma * sigma
        path.append(cum)
    prices = [m.price * math.exp(x - cum) for x in path]
    last_bucket = int(end.timestamp()) // 60 * 60 - 60
    quotes_per_min = 240 * m.activity
    candles = []
    for i in range(minutes):
        seg = prices[i * sub:(i + 1) * sub + 1]
        o, c = seg[0], seg[-1]
        moved = abs(c - o) / tick if tick else 0.0
        volume = traded_volume(m.depth, moved / max(1.0, quotes_per_min ** 0.5)) * quotes_per_min
        candles.append(Candle(last_bucket - (minutes - 1 - i) * 60, o, max(seg), min(seg), c,
                              round(volume * rng.uniform(0.6, 1.4), 2)))
    return candles
