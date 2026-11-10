"""
API REST + WebSocket do simulador (MVP com estado em memória).

Concorrência: todos os handlers são `async` e rodam no mesmo event loop do tick loop,
então o motor tem um único escritor (o loop) e nunca é tocado por duas threads.
O backtest roda num motor isolado em outra thread.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from api import auth
from engine.analytics import compute_metrics, ranking_score
from engine.gamification import progress
from engine.catalog import (FEATURED, FRONT, INSTRUMENTS, META, NEWS_TEMPLATES, PLANS, default_models, make_account,
                            models_for, update_fx)
from engine.core import (Account, AccountStatus, BracketSpec, ExecutionEngine, FillModel, FxConverter, Order,
                         OrderRejected, OrderType, Side)
from engine.market import CandleSeries, EconomicEvent, MarketSimulator
from engine.options import TEMPLATES, Leg, analyze, template
from engine.strategies import SmaCrossTrend, Strategy, backtest

log = logging.getLogger("simulador")
WEB = Path(__file__).resolve().parent.parent / "web"
TICK_S = 0.25
PREWARM_STEP_S = 3
MAX_SUBSCRIPTIONS = 400
NEWS_EVERY = timedelta(minutes=4)
BLACKOUT = timedelta(minutes=2)
SESSION_COOKIE = "sim_session"
SEASON_RULES = {"min_trades": 5, "min_days": 1}  # demo; produção: 20 trades e 10 dias
NOTICE_TOPICS = {"order.rejected", "order.filled", "order.canceled", "trade.closed", "risk.breach",
                 "risk.stop_out", "challenge.passed", "account.reset"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ───────────────────────────── Estado ─────────────────────────────
@dataclass
class User:
    id: str
    name: str
    email: str
    alias: str
    password_hash: str
    created_at: datetime
    consents: list[dict]
    account_ids: list[str] = field(default_factory=list)


class SimState:
    def __init__(self, seed: Optional[int] = None):
        self.fx = FxConverter()
        self.notices: list[dict] = []
        self.engine = ExecutionEngine(INSTRUMENTS, self.fx, FillModel(), emit=self._on_event)
        self.sim = MarketSimulator(default_models(), seed=seed)
        self.candles = {symbol: CandleSeries(5) for symbol in INSTRUMENTS}
        self.users: dict[str, User] = {}
        self.emails: dict[str, str] = {}
        self.owner: dict[str, str] = {}          # account_id → user_id ("bot" para EAs da casa)
        self.bots: list[Strategy] = []
        self.sockets: dict[WebSocket, dict] = {}
        self.news_cycle = 0

    def _on_event(self, topic: str, payload: dict) -> None:
        if topic in NOTICE_TOPICS:
            self.notices.append({"topic": topic, **payload})

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    # ── mercado ──
    def step(self, now: datetime, dt_s: float) -> None:
        result = self.sim.step(now, dt_s)
        update_fx(self.fx, result.quotes)
        self.engine.regimes.update(result.regimes)
        for q in result.quotes:
            self.engine.on_quote(q, now)
            self.candles[q.symbol].add(now, float((q.bid + q.ask) / 2))
            for bot in self.bots:
                bot.on_quote(q, now)
        for ev in result.released:
            self.notices.append({"topic": "news.released", "event": event_json(ev, now)})

    def bootstrap(self, prewarm_seconds: int) -> None:
        start = self.now() - timedelta(seconds=prewarm_seconds)
        self.spawn_bots(start)
        if prewarm_seconds >= 300:  # uma notícia no histórico para o gráfico já mostrar o choque
            self.schedule_news(start + timedelta(seconds=prewarm_seconds // 2))
        for i in range(0, prewarm_seconds, PREWARM_STEP_S):  # passos maiores: ~900 instrumentos no aquecimento
            self.step(start + timedelta(seconds=i), float(PREWARM_STEP_S))
        self.step(self.now(), TICK_S)
        self.notices.clear()

    def spawn_bots(self, now: datetime) -> None:
        D = Decimal
        specs = [
            ("Bot Tendência WIN", FRONT["WIN"], 8, 30, D("2"),
             BracketSpec(stop_loss_ticks=60, take_profit_ticks=150, trailing_ticks=50, trailing_activation_ticks=60)),
            ("Bot EURUSD", "EURUSD", 10, 40, D("0.5"),
             BracketSpec(stop_loss_ticks=150, take_profit_ticks=300, breakeven_trigger_ticks=100,
                         breakeven_offset_ticks=10)),
            ("Bot Ouro", "XAUUSD", 12, 48, D("0.1"),
             BracketSpec(stop_loss_ticks=800, take_profit_ticks=1600, trailing_ticks=600,
                         trailing_activation_ticks=600)),
        ]
        for name, symbol, fast, slow, qty, bracket in specs:
            acc = self.engine.add_account(make_account("CHALLENGE_50K", "BRL", name=name), now)
            self.owner[acc.id] = "bot"
            self.bots.append(SmaCrossTrend(self.engine, acc.id, symbol, fast, slow, qty, bracket))

    def schedule_news(self, at: datetime) -> EconomicEvent:
        tpl = dict(NEWS_TEMPLATES[self.news_cycle % len(NEWS_TEMPLATES)])
        self.news_cycle += 1
        ev = EconomicEvent(id=uuid.uuid4().hex[:10], scheduled_at=at, previous=tpl["forecast"], **tpl)
        self.sim.schedule(ev)
        if ev.impact == "HIGH":
            self.engine.blackouts.append((at - BLACKOUT, at + BLACKOUT, frozenset(ev.symbols)))
        return ev

    def maintain_calendar(self, now: datetime) -> None:
        if not any(not e.released for e in self.sim.events):
            self.schedule_news(now + NEWS_EVERY)
        self.sim.prune(now)
        self.engine.blackouts = [b for b in self.engine.blackouts if b[1] > now]

    # ── streaming ──
    async def broadcast(self, now: datetime) -> None:
        notices, self.notices = self.notices, []
        if not self.sockets:
            return
        cache: dict[str, dict] = {}  # cada cotação é serializada uma vez por tick, só se alguém a assina
        upcoming = next((event_json(e, now) for e in self.sim.events if not e.released), None)
        dead = []
        for ws, sub in list(self.sockets.items()):
            acc_id = sub.get("account_id")
            wanted = set(sub["symbols"]) | {p.symbol for p in self.engine.positions_of(acc_id or "")}
            quotes = {}
            for s in wanted:
                if s in self.engine.quotes:
                    quotes[s] = cache[s] if s in cache else cache.setdefault(s, quote_json(self, s))
            msg = {"type": "tick", "ts": now.timestamp(), "quotes": quotes, "next_event": upcoming,
                   "notices": [n for n in notices if n.get("account_id") in (None, acc_id)]}
            if acc_id in self.engine.accounts:
                msg["account"] = snapshot(self, acc_id, now)
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:  # cliente caiu no meio do envio
                dead.append(ws)
        for ws in dead:
            self.sockets.pop(ws, None)


async def tick_loop(state: SimState) -> None:
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        now = state.now()
        try:
            state.step(now, TICK_S)
            state.maintain_calendar(now)
            await state.broadcast(now)
        except Exception:
            log.exception("Falha no tick")
        await asyncio.sleep(max(0.0, TICK_S - (loop.time() - started)))


# ───────────────────────────── Serialização ─────────────────────────────
def money(x: Decimal) -> float:
    return float(x.quantize(Decimal("0.01")))


def opt(x: Optional[Decimal]) -> Optional[float]:
    return None if x is None else float(x)


def decimals(step: Decimal) -> int:
    return max(0, -step.normalize().as_tuple().exponent)


def instrument_json(i) -> dict:
    return {"symbol": i.symbol, "venue": i.venue, "description": i.description, "quote_ccy": i.quote_ccy,
            "tick_size": float(i.tick_size), "tick_value": float(i.tick_value), "lot_step": float(i.lot_step),
            "margin_per_unit": float(i.margin_per_unit), "commission_per_unit": float(i.commission_per_unit),
            "decimals": decimals(i.tick_size), **META.get(i.symbol, {})}


def quote_json(state: SimState, symbol: str) -> dict:
    q, regime = state.engine.quotes[symbol], state.engine.regimes.get(symbol)
    model = state.sim.models.get(symbol)
    return {"bid": float(q.bid), "ask": float(q.ask), "bid_size": float(q.bid_size), "ask_size": float(q.ask_size),
            "ref": model.ref_price if model else None,  # fechamento de referência → variação do dia
            "spread_mult": float(regime.spread_mult) if regime else 1.0,
            "depth_mult": float(regime.depth_mult) if regime else 1.0}


def order_json(o: Order) -> dict:
    return {"id": o.id, "symbol": o.symbol, "side": o.side.name, "kind": o.kind.value, "qty": float(o.qty),
            "limit_price": opt(o.limit_price), "stop_price": opt(o.stop_price), "role": o.role,
            "status": o.status.value, "fill_price": opt(o.fill_price), "reduce_only": o.reduce_only,
            "oco_group": o.oco_group, "source": o.source, "reject_code": o.reject_code,
            "reject_reason": o.reject_reason}


def event_json(ev: EconomicEvent, now: datetime) -> dict:
    return {"id": ev.id, "title": ev.title, "country": ev.country, "impact": ev.impact,
            "scheduled_at": ev.scheduled_at.timestamp(), "seconds_to": (ev.scheduled_at - now).total_seconds(),
            "forecast": ev.forecast, "previous": ev.previous, "actual": ev.actual if ev.released else None,
            "surprise_z": round(ev.surprise_z, 2) if ev.released else None, "released": ev.released,
            "symbols": sorted(ev.symbols), "profile": asdict(ev.profile)}


def snapshot(state: SimState, acc_id: str, now: datetime) -> dict:
    eng = state.engine
    acc = eng.accounts[acc_id]
    positions = []
    for p in eng.positions_of(acc_id):
        inst = INSTRUMENTS[p.symbol]
        sl, tp = eng.orders.get(p.sl_order_id or ""), eng.orders.get(p.tp_order_id or "")
        positions.append({"symbol": p.symbol, "side": p.side.name, "qty": float(p.qty),
                          "avg_price": round(float(p.avg_price), decimals(inst.tick_size) + 2),
                          "upnl": money(eng.upnl_base(p)), "sl": opt(sl.stop_price) if sl else None,
                          "sl_role": sl.role if sl else None, "tp": opt(tp.limit_price) if tp else None})
    lim = acc.limits
    return {
        "id": acc.id, "name": acc.name, "plan": acc.plan, "base_ccy": acc.base_ccy, "status": acc.status.value,
        "breach_reason": acc.breach_reason, "balance": money(acc.balance), "equity": money(acc.equity),
        "used_margin": money(acc.used_margin), "free_margin": money(acc.equity - acc.used_margin),
        "initial_balance": money(acc.initial_balance), "high_water_mark": money(acc.high_water_mark),
        "daily_floor": money(eng.risk.daily_floor(acc)), "total_floor": money(eng.risk.total_floor(acc)),
        "daily_loss_used": float(eng.risk.daily_loss_used(acc)),
        "next_reset": eng.risk.next_reset(now, lim).timestamp(),
        "limits": {"daily_loss_limit": money(lim.daily_loss_limit), "max_drawdown": money(lim.max_drawdown),
                   "trailing_drawdown": lim.trailing_drawdown, "profit_target": opt(lim.profit_target),
                   "min_trading_days": lim.min_trading_days, "news_blackout": lim.news_blackout,
                   "stop_out_level": float(lim.stop_out_level), "reset_tz": lim.reset_tz},
        "trading_days": len(acc.trading_days), "reset_count": acc.reset_count,
        "positions": positions, "orders": [order_json(o) for o in eng.working_orders(acc_id)],
    }


def downsample(points: list, n: int = 800) -> list:
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n)] + [points[-1]]


def account_metrics(state: SimState, acc: Account):
    eng = state.engine
    return compute_metrics(eng.trades.get(acc.id, []), eng.equity_history.get(acc.id, []), acc.initial_balance)


# ───────────────────────────── Modelos de entrada ─────────────────────────────
Plan = Literal["PRACTICE", "CHALLENGE_50K", "CHALLENGE_50K_TRAILING"]


class SignupIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)
    accept_terms: bool
    accept_privacy: bool
    accept_disclaimer: bool
    marketing: bool = False
    plan: Plan = "CHALLENGE_50K"
    base_ccy: Literal["BRL", "USD"] = "BRL"


class LoginIn(BaseModel):
    email: str
    password: str


class AccountIn(BaseModel):
    plan: Plan
    base_ccy: Literal["BRL", "USD"] = "BRL"


class BracketIn(BaseModel):
    stop_loss_ticks: Optional[int] = Field(None, ge=1)
    take_profit_ticks: Optional[int] = Field(None, ge=1)
    trailing_ticks: Optional[int] = Field(None, ge=1)
    trailing_activation_ticks: int = Field(0, ge=0)
    breakeven_trigger_ticks: Optional[int] = Field(None, ge=1)
    breakeven_offset_ticks: int = Field(0, ge=0)


class OrderIn(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    kind: Literal["MARKET", "LIMIT", "STOP"] = "MARKET"
    qty: Decimal = Field(gt=0)
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    bracket: Optional[BracketIn] = None


class ModifyIn(BaseModel):
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None


class LegIn(BaseModel):
    kind: Literal["C", "P", "S"]
    side: Literal[1, -1]
    qty: float = Field(1.0, gt=0)
    strike: float = Field(0.0, ge=0)
    premium: Optional[float] = Field(None, ge=0)


class OptionsIn(BaseModel):
    template: Optional[str] = "iron_condor"
    legs: Optional[list[LegIn]] = None
    spot: float = Field(gt=0)
    vol: float = Field(0.30, gt=0, le=3)
    days: int = Field(30, ge=0, le=1000)
    rate: float = Field(0.12, ge=-0.05, le=1)
    width_pct: float = Field(0.05, gt=0, le=0.5)
    multiplier: float = Field(100.0, gt=0)
    days_per_year: int = Field(252, ge=200, le=366)


class BacktestIn(BaseModel):
    symbol: str = FRONT["WIN"]
    fast: int = Field(10, ge=2, le=200)
    slow: int = Field(40, ge=3, le=500)
    qty: Decimal = Field(Decimal("1"), gt=0)
    stop_loss_ticks: Optional[int] = Field(60, ge=1)
    take_profit_ticks: Optional[int] = Field(150, ge=1)
    trailing_ticks: Optional[int] = Field(None, ge=1)
    steps: int = Field(4000, ge=200, le=20000)
    seed: int = 42


# ───────────────────────────── Dependências ─────────────────────────────
async def get_state(request: Request) -> SimState:
    return request.app.state.sim


async def current_user(request: Request) -> User:
    state: SimState = request.app.state.sim
    user = state.users.get(auth.read_token(request.cookies.get(SESSION_COOKIE)) or "")
    if user is None:
        raise HTTPException(401, "Sessão inválida ou expirada.")
    return user


def owned_account(state: SimState, user: User, account_id: str) -> Account:
    if state.owner.get(account_id) != user.id or account_id not in state.engine.accounts:
        raise HTTPException(404, "Conta não encontrada.")
    return state.engine.accounts[account_id]


def set_session(response: Response, user: User) -> None:
    # secure=True em produção (HTTPS); aqui o MVP roda em http://127.0.0.1
    response.set_cookie(SESSION_COOKIE, auth.issue_token(user.id), max_age=auth.SESSION_TTL_S,
                        httponly=True, samesite="lax", secure=False, path="/")


def me_json(user: User) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email, "alias": user.alias,
            "created_at": user.created_at.isoformat(), "account_ids": user.account_ids}


router = APIRouter()


# ───────────────────────────── Páginas ─────────────────────────────
@router.get("/", include_in_schema=False)
async def landing_page():
    return FileResponse(WEB / "index.html")


@router.get("/auth", include_in_schema=False)
async def auth_page():
    return FileResponse(WEB / "auth.html")


@router.get("/app", include_in_schema=False)
async def app_page():
    return FileResponse(WEB / "app.html")


# ───────────────────────────── Autenticação & LGPD ─────────────────────────────
@router.post("/api/auth/signup", status_code=201)
async def signup(body: SignupIn, request: Request, response: Response, state: SimState = Depends(get_state)):
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "E-mail inválido.")
    if not (body.accept_terms and body.accept_privacy and body.accept_disclaimer):
        raise HTTPException(422, "Aceite os Termos de Uso, a Política de Privacidade e o Disclaimer de Simulador.")
    problems = auth.password_problems(body.password, email)
    if problems:
        raise HTTPException(422, "Senha fraca: " + ", ".join(problems) + ".")
    if email in state.emails:
        raise HTTPException(409, "Já existe uma conta com este e-mail.")
    now = state.now()
    first = re.sub(r"[^a-z0-9]", "", body.name.split()[0].lower()) or "trader"
    consent = lambda doc, accepted: {"document": doc, "version": "2026-11", "accepted": accepted,
                                     "ip": request.client.host if request.client else None, "at": now.isoformat()}
    user = User(uuid.uuid4().hex, body.name.strip(), email, f"{first}_{secrets.token_hex(2)}",
                await asyncio.to_thread(auth.hash_password, body.password), now,
                [consent("TERMS", True), consent("PRIVACY", True), consent("SIM_DISCLAIMER", True),
                 consent("MARKETING", body.marketing)])
    state.users[user.id], state.emails[email] = user, user.id
    acc = state.engine.add_account(make_account(body.plan, body.base_ccy), now)
    state.owner[acc.id] = user.id
    user.account_ids.append(acc.id)
    set_session(response, user)
    return me_json(user)


@router.post("/api/auth/login")
async def login(body: LoginIn, response: Response, state: SimState = Depends(get_state)):
    user = state.users.get(state.emails.get(body.email.strip().lower(), ""))
    # mesma resposta para e-mail inexistente e senha errada (não revela quem tem conta)
    if user is None or not await asyncio.to_thread(auth.verify_password, body.password, user.password_hash):
        raise HTTPException(401, "E-mail ou senha incorretos.")
    set_session(response, user)
    return me_json(user)


@router.post("/api/auth/logout", status_code=204)
async def logout(request: Request, response: Response):
    auth.revoke_token(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/api/me")
async def me(user: User = Depends(current_user)):
    return me_json(user)


@router.get("/api/me/export")
async def export_my_data(user: User = Depends(current_user), state: SimState = Depends(get_state)):
    """LGPD art. 18 (portabilidade): tudo o que guardamos sobre o titular."""
    now = state.now()
    accounts = []
    for acc_id in user.account_ids:
        if acc_id in state.engine.accounts:
            trades = [{**asdict(t), "side": t.side.name, "net_pnl_base": str(t.net_pnl_base)}
                      for t in state.engine.trades.get(acc_id, [])]
            accounts.append({"snapshot": snapshot(state, acc_id, now), "trades": trades})
    return json.loads(json.dumps({"user": me_json(user), "consents": user.consents, "accounts": accounts},
                                 default=str))


@router.delete("/api/me", status_code=204)
async def delete_me(request: Request, response: Response, user: User = Depends(current_user),
                    state: SimState = Depends(get_state)):
    """LGPD art. 18 (eliminação): remove usuário, contas, trades e entradas no ranking."""
    for acc_id in user.account_ids:
        state.engine.remove_account(acc_id)
        state.owner.pop(acc_id, None)
    for ws, sub in list(state.sockets.items()):
        if sub["user_id"] == user.id:
            state.sockets.pop(ws, None)
            with suppress(Exception):
                await ws.close(code=4401)
    state.emails.pop(user.email, None)
    state.users.pop(user.id, None)
    auth.revoke_token(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")


# ───────────────────────────── Mercado ─────────────────────────────
@router.get("/api/instruments")
async def instruments():
    return [instrument_json(i) for i in INSTRUMENTS.values()]


@router.get("/api/plans")
async def plans():
    return [{"id": pid, "title": title, "balance": float(balance),
             "rules": {k: (float(v) if isinstance(v, Decimal) else v) for k, v in rules.items()}}
            for pid, (title, balance, rules) in PLANS.items()]


@router.get("/api/candles/{symbol}")
async def candles(symbol: str, state: SimState = Depends(get_state)):
    if symbol not in state.candles:
        raise HTTPException(404, "Instrumento desconhecido.")
    return [asdict(c) for c in state.candles[symbol].candles]


@router.get("/api/quotes")
async def quotes(symbols: Optional[str] = None, state: SimState = Depends(get_state)):
    """Cotações atuais; `?symbols=PETR4,VALE3` limita a lista (sem parâmetro: todas)."""
    wanted = symbols.split(",") if symbols else list(state.engine.quotes)
    return {s: quote_json(state, s) for s in wanted if s in state.engine.quotes}


@router.get("/api/featured")
async def featured():
    return [{"symbol": s, "description": INSTRUMENTS[s].description} for s in FEATURED]


@router.get("/api/calendar")
async def calendar(state: SimState = Depends(get_state)):
    now = state.now()
    return [event_json(e, now) for e in state.sim.events]


@router.post("/api/calendar/trigger", status_code=201)
async def trigger_news(user: User = Depends(current_user), state: SimState = Depends(get_state)):
    """Agenda uma notícia de alto impacto para daqui a 15 s (modo treino)."""
    now = state.now()
    return event_json(state.schedule_news(now + timedelta(seconds=15)), now)


# ───────────────────────────── Contas & ordens ─────────────────────────────
@router.get("/api/accounts")
async def my_accounts(user: User = Depends(current_user), state: SimState = Depends(get_state)):
    now = state.now()
    return [snapshot(state, a, now) for a in user.account_ids if a in state.engine.accounts]


@router.post("/api/accounts", status_code=201)
async def create_account(body: AccountIn, user: User = Depends(current_user), state: SimState = Depends(get_state)):
    now = state.now()
    acc = state.engine.add_account(make_account(body.plan, body.base_ccy), now)
    state.owner[acc.id] = user.id
    user.account_ids.append(acc.id)
    return snapshot(state, acc.id, now)


@router.get("/api/accounts/{account_id}")
async def get_account(account_id: str, user: User = Depends(current_user), state: SimState = Depends(get_state)):
    owned_account(state, user, account_id)
    return snapshot(state, account_id, state.now())


@router.post("/api/accounts/{account_id}/reset")
async def reset_account(account_id: str, user: User = Depends(current_user), state: SimState = Depends(get_state)):
    owned_account(state, user, account_id)
    now = state.now()
    state.engine.reset_account(account_id, now)
    return snapshot(state, account_id, now)


@router.post("/api/accounts/{account_id}/orders")
async def place_order(account_id: str, body: OrderIn, user: User = Depends(current_user),
                      state: SimState = Depends(get_state)):
    owned_account(state, user, account_id)
    if body.symbol not in INSTRUMENTS:
        raise HTTPException(404, "Instrumento desconhecido.")
    bracket = None
    if body.bracket and any(v is not None for v in (body.bracket.stop_loss_ticks, body.bracket.take_profit_ticks,
                                                    body.bracket.trailing_ticks)):
        bracket = BracketSpec(**body.bracket.model_dump())
    order = Order(account_id, body.symbol, Side[body.side], OrderType(body.kind), body.qty,
                  limit_price=body.limit_price, stop_price=body.stop_price, bracket=bracket)
    now = state.now()
    state.engine.submit(order, now)
    return {"order": order_json(order), "account": snapshot(state, account_id, now)}


@router.patch("/api/accounts/{account_id}/orders/{order_id}")
async def modify_order(account_id: str, order_id: str, body: ModifyIn, user: User = Depends(current_user),
                       state: SimState = Depends(get_state)):
    owned_account(state, user, account_id)
    try:
        order = state.engine.modify(account_id, order_id, limit_price=body.limit_price, stop_price=body.stop_price)
    except OrderRejected as e:
        raise HTTPException(422, str(e))
    return order_json(order)


@router.delete("/api/accounts/{account_id}/orders/{order_id}", status_code=204)
async def cancel_order(account_id: str, order_id: str, user: User = Depends(current_user),
                       state: SimState = Depends(get_state)):
    owned_account(state, user, account_id)
    if not state.engine.cancel(account_id, order_id):
        raise HTTPException(404, "Ordem não encontrada ou já finalizada.")


@router.post("/api/accounts/{account_id}/positions/{symbol}/close")
async def close_position(account_id: str, symbol: str, user: User = Depends(current_user),
                         state: SimState = Depends(get_state)):
    owned_account(state, user, account_id)
    now = state.now()
    if not state.engine.close_position(account_id, symbol, now):
        raise HTTPException(404, "Sem posição aberta neste instrumento.")
    return snapshot(state, account_id, now)


@router.get("/api/accounts/{account_id}/analytics")
async def analytics(account_id: str, user: User = Depends(current_user), state: SimState = Depends(get_state)):
    acc = owned_account(state, user, account_id)
    eng = state.engine
    curve = [(ts.timestamp(), float(eq)) for ts, eq in eng.equity_history.get(account_id, [])]
    trades = [{"symbol": t.symbol, "side": t.side.name, "qty": float(t.qty), "entry": float(t.entry_price),
               "exit": float(t.exit_price), "opened_at": t.opened_at.timestamp(), "closed_at": t.closed_at.timestamp(),
               "net_pnl": money(t.net_pnl_base), "fees": money(t.fees_base), "reason": t.exit_reason}
              for t in eng.trades.get(account_id, [])]
    return {"metrics": asdict(account_metrics(state, acc)), "equity_curve": downsample(curve),
            "trades": trades[-300:][::-1], "base_ccy": acc.base_ccy, "initial_balance": money(acc.initial_balance)}


@router.get("/api/accounts/{account_id}/progress")
async def account_progress(account_id: str, user: User = Depends(current_user), state: SimState = Depends(get_state)):
    """Nível, XP e conquistas da conta (gamificação)."""
    acc = owned_account(state, user, account_id)
    return progress(state.engine.trades.get(account_id, []), account_metrics(state, acc), acc,
                    lambda symbol: META.get(symbol, {}).get("category", "OUTRO"))


@router.get("/api/leaderboard")
async def leaderboard(state: SimState = Depends(get_state)):
    rows = []
    for acc_id, acc in state.engine.accounts.items():
        if acc.plan == "PRACTICE":
            continue
        owner = state.owner.get(acc_id)
        alias = acc.name if owner == "bot" else state.users[owner].alias
        m = account_metrics(state, acc)
        score, eligible = ranking_score(m, float(acc.limits.max_drawdown),
                                        acc.status is AccountStatus.BREACHED, **SEASON_RULES)
        rows.append({"alias": alias, "bot": owner == "bot", "plan": acc.plan, "status": acc.status.value,
                     "score": score, "eligible": eligible, "return_pct": m.return_pct,
                     "max_drawdown_pct": m.max_drawdown_pct, "profit_factor": m.profit_factor,
                     "win_rate": m.win_rate, "sortino": m.sortino, "consistency": m.consistency,
                     "trades": m.trades, "trading_days": m.trading_days, "resets": acc.reset_count})
    rows.sort(key=lambda r: (not r["eligible"], -r["score"]))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return {"season": "Temporada Nov/2026", "rules": SEASON_RULES, "rows": rows}


# ───────────────────────────── Opções & automação ─────────────────────────────
@router.get("/api/options/templates")
async def option_templates():
    return TEMPLATES


@router.post("/api/options/analyze")
async def options_analyze(body: OptionsIn):
    if body.legs:
        legs = [Leg(**leg.model_dump()) for leg in body.legs]
    else:
        try:
            legs = template(body.template or "", body.spot, body.width_pct)
        except KeyError as e:
            raise HTTPException(404, str(e))
    return analyze(legs, body.spot, body.rate, body.days / body.days_per_year, body.vol,
                   days_per_year=body.days_per_year, multiplier=body.multiplier)


@router.post("/api/strategies/backtest")
async def run_backtest(body: BacktestIn):
    if body.symbol not in INSTRUMENTS:
        raise HTTPException(404, "Instrumento desconhecido.")
    if body.fast >= body.slow:
        raise HTTPException(422, "A média rápida deve ser menor que a lenta.")
    bracket = BracketSpec(stop_loss_ticks=body.stop_loss_ticks, take_profit_ticks=body.take_profit_ticks,
                          trailing_ticks=body.trailing_ticks)
    factory = lambda eng, acc_id: SmaCrossTrend(eng, acc_id, body.symbol, body.fast, body.slow, body.qty,
                                                bracket, sample_every=2)
    acc, trades, curve = await asyncio.to_thread(backtest, factory, models_for([body.symbol]), body.steps, seed=body.seed)
    metrics = compute_metrics(trades, curve, acc.initial_balance)
    return {"metrics": asdict(metrics), "final_balance": money(acc.balance), "base_ccy": acc.base_ccy,
            "equity_curve": downsample([(ts.timestamp(), float(eq)) for ts, eq in curve])}


# ───────────────────────────── WebSocket ─────────────────────────────
@router.websocket("/ws")
async def stream(ws: WebSocket):
    state: SimState = ws.app.state.sim
    origin = ws.headers.get("origin")
    if origin and urlparse(origin).netloc != ws.headers.get("host"):  # bloqueia WebSocket cross-site
        await ws.close(code=4403)
        return
    user_id = auth.read_token(ws.cookies.get(SESSION_COOKIE))
    if user_id not in state.users:
        await ws.accept()  # aceitar antes de fechar: só assim o código 4401 chega ao navegador
        await ws.close(code=4401)
        return
    await ws.accept()
    state.sockets[ws] = {"user_id": user_id, "account_id": None, "symbols": set(FEATURED)}
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if not isinstance(msg, dict):
                continue
            acc_id = msg.get("account_id")
            if acc_id and state.owner.get(acc_id) == user_id:
                state.sockets[ws]["account_id"] = acc_id
            symbols = msg.get("symbols")
            if isinstance(symbols, list):  # assinatura: só o que está na tela (limite contra abuso)
                state.sockets[ws]["symbols"] = {s for s in symbols[:MAX_SUBSCRIPTIONS]
                                                if isinstance(s, str) and s in INSTRUMENTS}
    except (WebSocketDisconnect, ValueError):
        pass
    finally:
        state.sockets.pop(ws, None)


# ───────────────────────────── App ─────────────────────────────
def create_app(seed: Optional[int] = None, prewarm_seconds: int = 900, start_loop: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = SimState(seed)
        state.bootstrap(prewarm_seconds)
        app.state.sim = state
        task = asyncio.create_task(tick_loop(state)) if start_loop else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Simulador de Mercado Global", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def revalidate_frontend(request: Request, call_next):
        # O navegador sempre revalida páginas e estáticos (ETag → 304): nunca roda um app.js velho.
        response = await call_next(request)
        if request.url.path.startswith("/static/") or request.url.path in ("/", "/auth", "/app"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app


app = create_app()
