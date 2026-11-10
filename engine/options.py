"""
Mesa de Opções: Black-Scholes-Merton, gregas e payoff de estruturas.

BSM é exato para europeias (a maioria das puts na B3). Para americanas (calls de ações
na B3) o próximo passo é Bjerksund-Stensland ou árvore binomial; sem dividendos, a call
americana vale o mesmo que a europeia.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Optional


def _cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float  # por dia (convenção de `days_per_year`: 252 na B3, 365 nos EUA)
    vega: float   # por 1 ponto percentual de volatilidade
    rho: float    # por 1 ponto percentual de juros


def black_scholes(spot: float, strike: float, t_years: float, rate: float, vol: float, kind: str,
                  q: float = 0.0, days_per_year: int = 252) -> Greeks:
    if t_years <= 0 or vol <= 0:
        if kind == "C":
            return Greeks(max(spot - strike, 0.0), 1.0 if spot > strike else 0.0, 0, 0, 0, 0)
        return Greeks(max(strike - spot, 0.0), -1.0 if spot < strike else 0.0, 0, 0, 0, 0)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate - q + 0.5 * vol * vol) * t_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    df_r, df_q = math.exp(-rate * t_years), math.exp(-q * t_years)
    decay = -spot * df_q * _pdf(d1) * vol / (2 * sqrt_t)
    if kind == "C":
        price = spot * df_q * _cdf(d1) - strike * df_r * _cdf(d2)
        delta = df_q * _cdf(d1)
        theta = decay - rate * strike * df_r * _cdf(d2) + q * spot * df_q * _cdf(d1)
        rho = strike * t_years * df_r * _cdf(d2) / 100
    else:
        price = strike * df_r * _cdf(-d2) - spot * df_q * _cdf(-d1)
        delta = -df_q * _cdf(-d1)
        theta = decay + rate * strike * df_r * _cdf(-d2) - q * spot * df_q * _cdf(-d1)
        rho = -strike * t_years * df_r * _cdf(-d2) / 100
    gamma = df_q * _pdf(d1) / (spot * vol * sqrt_t)
    vega = spot * df_q * _pdf(d1) * sqrt_t / 100
    return Greeks(price, delta, gamma, theta / days_per_year, vega, rho)


@dataclass(frozen=True)
class Leg:
    kind: str                  # "C", "P" ou "S" (o próprio ativo-objeto)
    side: int                  # +1 compra, -1 venda
    qty: float = 1.0
    strike: float = 0.0
    premium: Optional[float] = None  # None = precificado por BSM no spot atual


TEMPLATES = {
    "bull_call_spread": "Trava de alta com calls",
    "bear_put_spread": "Trava de baixa com puts",
    "long_straddle": "Straddle comprado",
    "long_strangle": "Strangle comprado",
    "butterfly": "Borboleta com calls",
    "iron_condor": "Iron Condor",
    "covered_call": "Venda coberta",
}


def template(name: str, spot: float, width_pct: float = 0.05) -> list[Leg]:
    k = lambda pct: round(spot * (1 + pct), 2)
    w = width_pct
    legs = {
        "bull_call_spread": [Leg("C", +1, 1, k(0)), Leg("C", -1, 1, k(w))],
        "bear_put_spread": [Leg("P", +1, 1, k(0)), Leg("P", -1, 1, k(-w))],
        "long_straddle": [Leg("C", +1, 1, k(0)), Leg("P", +1, 1, k(0))],
        "long_strangle": [Leg("C", +1, 1, k(w)), Leg("P", +1, 1, k(-w))],
        "butterfly": [Leg("C", +1, 1, k(-w)), Leg("C", -1, 2, k(0)), Leg("C", +1, 1, k(w))],
        "iron_condor": [Leg("P", +1, 1, k(-2 * w)), Leg("P", -1, 1, k(-w)),
                        Leg("C", -1, 1, k(w)), Leg("C", +1, 1, k(2 * w))],
        "covered_call": [Leg("S", +1, 1), Leg("C", -1, 1, k(w))],
    }
    if name not in legs:
        raise KeyError(f"Estratégia desconhecida: {name}")
    return legs[name]


def analyze(legs: list[Leg], spot: float, rate: float, t_years: float, vol: float, *,
            points: int = 161, range_pct: float = 0.3, days_per_year: int = 252,
            multiplier: float = 1.0, q: float = 0.0) -> dict:
    """Payoff no vencimento e hoje (T+0), breakevens, máximos e gregas agregadas."""
    priced = []
    for leg in legs:
        if leg.kind == "S":
            premium = spot
        elif leg.premium is not None:
            premium = leg.premium
        else:
            premium = black_scholes(spot, leg.strike, t_years, rate, vol, leg.kind, q, days_per_year).price
        priced.append((leg, premium))

    def value_at(s: float, t: float) -> float:
        total = 0.0
        for leg, premium in priced:
            if leg.kind == "S":
                v = s
            elif t <= 0:
                v = max(s - leg.strike, 0.0) if leg.kind == "C" else max(leg.strike - s, 0.0)
            else:
                v = black_scholes(s, leg.strike, t, rate, vol, leg.kind, q, days_per_year).price
            total += leg.side * leg.qty * (v - premium)
        return total * multiplier

    lo, hi = spot * (1 - range_pct), spot * (1 + range_pct)
    grid = [lo + (hi - lo) * i / (points - 1) for i in range(points)]
    expiry = [value_at(s, 0) for s in grid]
    today = [value_at(s, t_years) for s in grid]

    breakevens: list[float] = []
    for i in range(1, points):
        a, b = expiry[i - 1], expiry[i]
        if a == 0 and (not breakevens or breakevens[-1] != grid[i - 1]):
            breakevens.append(grid[i - 1])
        elif a * b < 0:
            breakevens.append(grid[i - 1] + (grid[i] - grid[i - 1]) * (-a) / (b - a))

    eps = 1e-9
    right_slope = expiry[-1] - expiry[-2]
    extremes = expiry + [value_at(0.0, 0)]
    greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    for leg, _ in priced:
        if leg.kind == "S":
            greeks["delta"] += leg.side * leg.qty * multiplier
            continue
        g = black_scholes(spot, leg.strike, t_years, rate, vol, leg.kind, q, days_per_year)
        for name in greeks:
            greeks[name] += leg.side * leg.qty * multiplier * getattr(g, name)

    return {
        "grid": grid,
        "payoff_expiry": expiry,
        "payoff_today": today,
        "breakevens": breakevens,
        "max_profit": None if right_slope > eps else max(extremes),
        "max_loss": None if right_slope < -eps else min(extremes),
        "net_premium": sum(l.side * l.qty * p for l, p in priced if l.kind != "S") * multiplier,
        "greeks": greeks,
        "legs": [{**asdict(l), "premium": p} for l, p in priced],
    }
