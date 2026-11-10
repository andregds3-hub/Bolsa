import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from engine.catalog import FRONT

WIN = FRONT["WIN"]

SIGNUP = {"name": "Ana Trader", "email": "ana@example.com", "password": "treino-2026-forte",
          "accept_terms": True, "accept_privacy": True, "accept_disclaimer": True, "plan": "CHALLENGE_50K"}


@pytest.fixture()
def client():
    with TestClient(create_app(seed=3, prewarm_seconds=120, start_loop=False)) as c:
        yield c


def test_signup_validates_password_consents_and_duplicates(client):
    assert client.post("/api/auth/signup", json={**SIGNUP, "password": "curta1"}).status_code == 422
    assert client.post("/api/auth/signup", json={**SIGNUP, "accept_disclaimer": False}).status_code == 422
    assert client.post("/api/auth/signup", json=SIGNUP).status_code == 201
    assert client.post("/api/auth/signup", json=SIGNUP).status_code == 409


def test_protected_routes_require_session(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/accounts").status_code == 401


def test_full_trading_flow_journal_ranking_and_lgpd(client):
    me = client.post("/api/auth/signup", json=SIGNUP).json()
    assert client.get("/api/me").json()["email"] == "ana@example.com"
    acc = client.get("/api/accounts").json()[0]
    assert acc["plan"] == "CHALLENGE_50K" and acc["status"] == "ACTIVE"

    rejected = client.post(f"/api/accounts/{acc['id']}/orders", json={
        "symbol": WIN, "side": "BUY", "qty": "200", "bracket": {"stop_loss_ticks": 200}}).json()
    assert rejected["order"]["reject_code"] == "DAILY_LOSS_AT_RISK"

    res = client.post(f"/api/accounts/{acc['id']}/orders", json={
        "symbol": WIN, "side": "BUY", "qty": "2",
        "bracket": {"stop_loss_ticks": 60, "take_profit_ticks": 120}}).json()
    assert res["order"]["status"] == "FILLED"
    pos = res["account"]["positions"][0]
    assert pos["symbol"] == WIN and pos["sl"] is not None and pos["tp"] is not None

    snap = client.post(f"/api/accounts/{acc['id']}/positions/{WIN}/close").json()
    assert snap["positions"] == [] and snap["orders"] == []
    journal = client.get(f"/api/accounts/{acc['id']}/analytics").json()
    assert journal["metrics"]["trades"] == 1 and journal["trades"][0]["reason"] == "MANUAL"
    prog = client.get(f"/api/accounts/{acc['id']}/progress").json()
    unlocked = {a["id"] for a in prog["achievements"] if a["done"]}
    assert {"primeiro_trade", "derivativos"} <= unlocked and prog["xp"] > 0

    board = client.get("/api/leaderboard").json()["rows"]
    assert any(r["alias"] == me["alias"] for r in board) and any(r["bot"] for r in board)

    assert client.post("/api/options/analyze", json={"template": "long_straddle", "spot": 38}).status_code == 200
    bars = client.get("/api/candles/PETR4?tf=300").json()  # 5 min, sobre o histórico de 6 h
    assert len(bars) >= 70 and all(c["volume"] > 0 for c in bars)
    assert client.get("/api/candles/PETR4?tf=7").status_code == 422
    assert client.get("/api/me/export").json()["user"]["email"] == "ana@example.com"
    assert client.delete("/api/me").status_code == 204
    assert client.get("/api/me").status_code == 401
    assert not any(r["alias"] == me["alias"] for r in client.get("/api/leaderboard").json()["rows"])


def test_other_users_accounts_are_not_reachable(client):
    client.post("/api/auth/signup", json=SIGNUP)
    ana_acc = client.get("/api/accounts").json()[0]["id"]
    client.post("/api/auth/logout")
    client.cookies.clear()
    client.post("/api/auth/signup", json={**SIGNUP, "email": "bruno@example.com"})
    assert client.get(f"/api/accounts/{ana_acc}").status_code == 404
