from collections import Counter
from datetime import date, datetime, timezone

import pytest

from engine.catalog import build_catalog, models_for, third_friday, wednesday_near_15
from engine.market import MarketSimulator

DAY = date(2026, 11, 9)  # segunda-feira


@pytest.fixture(scope="module")
def cat():
    return build_catalog(DAY)


def test_covers_every_b3_asset_class(cat):
    counts = Counter(m["category"] for m in cat.meta.values())
    assert counts["ACAO"] >= 250            # ações/units + fracionário
    assert counts["FII"] >= 50 and counts["FIAGRO"] >= 5 and counts["FIINFRA"] >= 5
    assert counts["ETF"] >= 15 and counts["BDR"] >= 25
    assert counts["FUTURO"] >= 15 and counts["OPCAO"] >= 300
    assert set(cat.models) == set(cat.instruments) == set(cat.meta)


def test_contract_calendar_and_codes(cat):
    assert wednesday_near_15(2026, 12) == date(2026, 12, 16)
    assert third_friday(2026, 11) == date(2026, 11, 20)
    # WDO de novembro venceu no 1º dia útil (02/11): o vigente é dezembro
    assert cat.front == {"WIN": "WINZ26", "IND": "INDZ26", "WDO": "WDOZ26", "DOL": "DOLZ26",
                         "BGI": "BGIX26", "CCM": "CCMX26", "ICF": "ICFZ26", "DI1": "DI1F27"}
    assert {"WING27", "WDOF27", "DI1F28", "DI1F35"} <= set(cat.instruments)


def test_lots_and_option_tickers(cat):
    i = cat.instruments
    assert (i["PETR4"].lot_step, i["PETR4F"].lot_step, i["MXRF11"].lot_step, i["AAPL34"].lot_step) == (100, 1, 1, 1)
    assert cat.meta["PETR4F"] == {**cat.meta["PETR4F"], "fractional": True, "standard": "PETR4"}
    assert i["PETRK380"].lot_step == 100  # call novembro, strike 38,00
    assert cat.meta["PETRK380"]["option_type"] == "C" and cat.meta["PETRW380"]["option_type"] == "P"
    assert cat.meta["PETRK380"]["expiry"] == "2026-11-20" and "PETRL380" in i  # dezembro


def test_derivatives_follow_their_underlying(cat):
    models = [m for s, m in cat.models.items() if s in {"PETR4", "PETR4F", "PETRK360", "PETRK400", "WINZ26", "INDZ26"}]
    sim = MarketSimulator(models, seed=1)
    now = datetime(2026, 11, 9, 15, tzinfo=timezone.utc)
    for _ in range(50):
        sim.step(now, 1.0)
    m = sim.models
    assert m["PETR4F"].price == m["PETR4"].price and m["INDZ26"].price == m["WINZ26"].price
    assert m["PETRK360"].price > m["PETRK400"].price > 0  # call: strike maior vale menos


def test_models_for_pulls_underlying_chain_and_fx():
    from engine.catalog import FRONT, INSTRUMENTS
    option = next(s for s in INSTRUMENTS if s.startswith("VALE") and len(s) > 6)
    symbols = {m.instrument.symbol for m in models_for([option])}
    assert {option, "VALE3", FRONT["WDO"], "EURUSD"} <= symbols and len(symbols) == 4
