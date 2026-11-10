"""
Gamificação: níveis, XP e conquistas que recompensam disciplina (stop, consistência, diversificação),
não só lucro. Calculado a partir do histórico da conta, sem estado próprio.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Callable, Sequence

from engine.analytics import Metrics
from engine.core import Account, AccountStatus, ClosedTrade

LEVELS = [  # (XP mínimo, título)
    (0, "Aprendiz"), (100, "Observador do Pregão"), (250, "Operador"), (500, "Trader"),
    (900, "Estrategista"), (1400, "Gestor de Risco"), (2100, "Mestre do Mercado"),
]
XP_PER_TRADE = 5
XP_PER_GREEN_DAY = 20


@dataclass(frozen=True)
class Achievement:
    id: str
    icon: str
    title: str
    description: str
    xp: int


@dataclass(frozen=True)
class Context:
    trades: Sequence[ClosedTrade]
    metrics: Metrics
    account: Account
    categories: set[str]  # categorias dos ativos operados (ACAO, FII, FUTURO, OPCAO...)


RULES: list[tuple[Achievement, Callable[[Context], bool]]] = [
    (Achievement("primeiro_trade", "🚀", "Primeiro trade", "Encerre sua primeira operação.", 50),
     lambda c: len(c.trades) >= 1),
    (Achievement("respeitou_stop", "🛡️", "Respeitou o stop", "Saia de um trade pelo stop loss, sem mexer nele.", 60),
     lambda c: any(t.exit_reason in ("STOP_LOSS", "BREAKEVEN", "TRAILING") for t in c.trades)),
    (Achievement("alvo_na_mosca", "🎯", "Alvo na mosca", "Encerre um trade no take profit.", 60),
     lambda c: any(t.exit_reason == "TAKE_PROFIT" for t in c.trades)),
    (Achievement("surfista", "🏄", "Surfou a tendência", "Saia de um trade pelo trailing stop com lucro.", 80),
     lambda c: any(t.exit_reason == "TRAILING" and t.net_pnl_base > 0 for t in c.trades)),
    (Achievement("dez_trades", "🔁", "Ritmo de pregão", "Complete 10 trades.", 80),
     lambda c: len(c.trades) >= 10),
    (Achievement("dia_verde", "📗", "Dia no verde", "Termine um dia com resultado positivo.", 70),
     lambda c: c.metrics.best_day > 0),
    (Achievement("edge", "📐", "Vantagem estatística", "Profit Factor ≥ 1,5 com pelo menos 10 trades.", 150),
     lambda c: c.metrics.trades >= 10 and (c.metrics.profit_factor or 0) >= 1.5),
    (Achievement("diversificado", "🧭", "Explorador da B3", "Opere 3 classes de ativos diferentes.", 100),
     lambda c: len(c.categories) >= 3),
    (Achievement("fii", "🏢", "Investidor imobiliário", "Opere um FII.", 40),
     lambda c: "FII" in c.categories),
    (Achievement("derivativos", "⚡", "Derivativos na veia", "Opere um contrato futuro.", 40),
     lambda c: "FUTURO" in c.categories),
    (Achievement("opcoes", "🧮", "Mesa de opções", "Opere uma opção.", 60),
     lambda c: "OPCAO" in c.categories),
    (Achievement("aprovado", "🏆", "Desafio aprovado", "Bata a meta do desafio sem quebrar regras.", 300),
     lambda c: c.account.status is AccountStatus.PASSED),
]


def level_for(xp: int) -> tuple[int, str, int, int]:
    """(nível, título, XP do nível atual, XP do próximo nível ou o atual se for o último)."""
    index = max(i for i, (floor, _) in enumerate(LEVELS) if xp >= floor)
    floor, title = LEVELS[index]
    ceiling = LEVELS[index + 1][0] if index + 1 < len(LEVELS) else floor
    return index + 1, title, floor, ceiling


def progress(trades: Sequence[ClosedTrade], metrics: Metrics, account: Account,
             category_of: Callable[[str], str]) -> dict:
    ctx = Context(trades, metrics, account, {category_of(t.symbol) for t in trades})
    achievements = [{**asdict(a), "done": rule(ctx)} for a, rule in RULES]
    by_day: dict = defaultdict(float)
    for t in trades:
        by_day[t.closed_at.date()] += float(t.net_pnl_base)
    green_days = sum(1 for v in by_day.values() if v > 0)
    xp = (sum(a["xp"] for a in achievements if a["done"]) + XP_PER_TRADE * len(trades)
          + XP_PER_GREEN_DAY * green_days)
    level, title, floor, ceiling = level_for(xp)
    return {"xp": xp, "level": level, "title": title, "level_floor": floor, "next_level_xp": ceiling,
            "progress": 1.0 if ceiling == floor else (xp - floor) / (ceiling - floor),
            "unlocked": sum(1 for a in achievements if a["done"]), "achievements": achievements}
