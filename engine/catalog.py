"""
Catálogo de instrumentos e modelos de preço do simulador.

B3: ações e units (lote padrão 100) com o mercado fracionário (sufixo F), FIIs, Fiagro, FI-Infra,
ETFs, BDRs, futuros (índice, dólar, DI, agro) com vencimentos calculados pela data e séries de
opções sobre as ações/ETF mais líquidos. Internacional: EURUSD, XAUUSD, US100.

Os preços são REFERÊNCIAS SINTÉTICAS aproximadas, não cotações reais: o mercado do MVP é
simulado. Os calendários usam dias úteis sem feriados (aproximação do calendário oficial da B3).
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable, Iterable, Optional

from engine.core import Account, FxConverter, Instrument, Quote, RiskLimits
from engine.market import ShockProfile, SymbolModel, option_iv
from engine.options import black_scholes

D = Decimal
MONTH_CODES = "FGHJKMNQUVXZ"
CALL_LETTERS, PUT_LETTERS = "ABCDEFGHIJKL", "MNOPQRSTUVWX"
RISK_FREE = 0.14
B3_CLOSE_UTC = time(20, 0)  # 17h em Brasília


# ───────────────────────────── Calendário ─────────────────────────────
def first_bday(y: int, m: int) -> date:
    d = date(y, m, 1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def last_bday(y: int, m: int) -> date:
    d = date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def wednesday_near_15(y: int, m: int) -> date:
    """Vencimento de WIN/IND: quarta-feira mais próxima do dia 15."""
    d = date(y, m, 15)
    offset = (2 - d.weekday()) % 7
    return d + timedelta(days=offset - 7 if offset > 3 else offset)


def third_friday(y: int, m: int) -> date:
    """Vencimento das opções mensais de ações: terceira sexta-feira."""
    d = date(y, m, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7 + 14)


def day_15(y: int, m: int) -> date:
    return date(y, m, 15)


def icf_expiry(y: int, m: int) -> date:
    return last_bday(y, m) - timedelta(days=8)  # ~6 dias úteis antes do último dia útil


def bdays_between(a: date, b: date) -> int:
    days, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def next_expiries(today: date, months: Iterable[int], count: int,
                  expiry: Callable[[int, int], date]) -> list[tuple[int, int, date]]:
    """Próximos `count` vencimentos (ano, mês, data) ainda não vencidos em `today`."""
    allowed, out, y, m = set(months), [], today.year, today.month
    while len(out) < count:
        if m in allowed:
            exp = expiry(y, m)
            if exp >= today:
                out.append((y, m, exp))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def contract_code(root: str, y: int, m: int) -> str:
    return f"{root}{MONTH_CODES[m - 1]}{y % 100:02d}"


# ───────────────────────────── Tabelas (ticker;nome;setor;preço de referência;liquidez 1-3) ─────────────────────────────
STOCKS = """
PETR4;Petrobras PN;Petróleo e gás;38.00;1
PETR3;Petrobras ON;Petróleo e gás;41.00;1
VALE3;Vale ON;Mineração;55.00;1
ITUB4;Itaú Unibanco PN;Bancos;36.00;1
ITUB3;Itaú Unibanco ON;Bancos;31.00;2
BBDC4;Bradesco PN;Bancos;14.00;1
BBDC3;Bradesco ON;Bancos;12.50;2
BBAS3;Banco do Brasil ON;Bancos;22.00;1
SANB11;Santander Brasil Unit;Bancos;28.00;2
BPAC11;BTG Pactual Unit;Bancos;40.00;1
ABCB4;ABC Brasil PN;Bancos;22.00;3
BPAN4;Banco Pan PN;Bancos;7.00;3
BRSR6;Banrisul PNB;Bancos;12.00;3
BMGB4;Banco BMG PN;Bancos;4.00;3
BRBI11;BR Partners Unit;Bancos de investimento;16.00;3
ITSA4;Itaúsa PN;Holdings;11.00;1
BRAP4;Bradespar PN;Holdings;17.00;2
CSAN3;Cosan ON;Holdings;6.00;2
B3SA3;B3 ON;Serviços financeiros;13.00;1
BBSE3;BB Seguridade ON;Seguros;36.00;1
PSSA3;Porto Seguro ON;Seguros;45.00;2
CXSE3;Caixa Seguridade ON;Seguros;15.00;2
IRBR3;IRB Re ON;Seguros;50.00;2
WIZC3;Wiz Co ON;Seguros;8.00;3
ABEV3;Ambev ON;Bebidas;13.00;1
WEGE3;WEG ON;Bens de capital;45.00;1
EMBR3;Embraer ON;Aeroespacial;75.00;1
POMO4;Marcopolo PN;Bens de capital;7.50;2
KEPL3;Kepler Weber ON;Bens de capital;9.00;3
ROMI3;Romi ON;Bens de capital;9.00;3
PRIO3;PRIO ON;Petróleo e gás;40.00;1
RECV3;PetroReconcavo ON;Petróleo e gás;13.00;2
BRAV3;Brava Energia ON;Petróleo e gás;16.00;2
UGPA3;Ultrapar ON;Distribuição de combustíveis;18.00;1
VBBR3;Vibra Energia ON;Distribuição de combustíveis;22.00;1
RAIZ4;Raízen PN;Açúcar e etanol;1.20;2
SMTO3;São Martinho ON;Açúcar e etanol;18.00;2
JALL3;Jalles Machado ON;Açúcar e etanol;4.00;3
SUZB3;Suzano ON;Papel e celulose;55.00;1
KLBN11;Klabin Unit;Papel e celulose;19.00;1
GGBR4;Gerdau PN;Siderurgia;17.00;1
GOAU4;Metalúrgica Gerdau PN;Siderurgia;9.50;2
CSNA3;CSN ON;Siderurgia;8.00;2
USIM5;Usiminas PNA;Siderurgia;5.00;2
CMIN3;CSN Mineração ON;Mineração;5.50;2
CBAV3;CBA ON;Metais;4.50;3
FESA4;Ferbasa PN;Metais;7.00;3
BRKM5;Braskem PNA;Petroquímica;8.00;2
UNIP6;Unipar PNB;Química;60.00;3
ELET3;Eletrobras ON;Energia elétrica;45.00;1
ELET6;Eletrobras PNB;Energia elétrica;48.00;2
EQTL3;Equatorial ON;Energia elétrica;33.00;1
CPLE6;Copel PNB;Energia elétrica;11.00;1
CMIG4;Cemig PN;Energia elétrica;11.00;1
TAEE11;Taesa Unit;Energia elétrica;35.00;2
EGIE3;Engie Brasil ON;Energia elétrica;40.00;2
ENEV3;Eneva ON;Energia elétrica;14.00;2
ALUP11;Alupar Unit;Energia elétrica;30.00;3
ISAE4;ISA Energia PN;Energia elétrica;26.00;2
AURE3;Auren ON;Energia elétrica;10.00;3
CPFE3;CPFL Energia ON;Energia elétrica;38.00;2
ENGI11;Energisa Unit;Energia elétrica;45.00;2
SBSP3;Sabesp ON;Saneamento;110.00;1
CSMG3;Copasa ON;Saneamento;25.00;3
SAPR11;Sanepar Unit;Saneamento;30.00;3
ORVR3;Orizon ON;Saneamento e resíduos;45.00;3
RAIL3;Rumo ON;Logística;18.00;1
MOTV3;Motiva ON;Concessões rodoviárias;13.00;1
ECOR3;Ecorodovias ON;Concessões rodoviárias;7.00;3
SIMH3;Simpar ON;Logística;4.00;3
TGMA3;Tegma ON;Logística;35.00;3
HBSA3;Hidrovias do Brasil ON;Logística;3.50;3
PORT3;Wilson Sons ON;Logística;18.00;3
RENT3;Localiza ON;Aluguel de veículos;45.00;1
MOVI3;Movida ON;Aluguel de veículos;6.00;3
VAMO3;Vamos ON;Aluguel de veículos;4.00;3
LREN3;Lojas Renner ON;Varejo;16.00;1
MGLU3;Magazine Luiza ON;Varejo;9.00;1
BHIA3;Casas Bahia ON;Varejo;4.00;3
AZZA3;Azzas 2154 ON;Varejo;30.00;2
CEAB3;C&A ON;Varejo;12.00;3
GUAR3;Guararapes ON;Varejo;9.00;3
VIVA3;Vivara ON;Varejo;25.00;3
SBFG3;Grupo SBF ON;Varejo;13.00;3
PETZ3;Petz ON;Varejo;4.00;3
LJQQ3;Quero-Quero ON;Varejo;3.00;3
ASAI3;Assaí ON;Varejo alimentar;9.00;1
PCAR3;GPA ON;Varejo alimentar;3.50;3
GMAT3;Grupo Mateus ON;Varejo alimentar;7.00;2
ALPA4;Alpargatas PN;Calçados;10.00;3
GRND3;Grendene ON;Calçados;5.00;3
VULC3;Vulcabras ON;Calçados;17.00;3
NATU3;Natura ON;Higiene pessoal;10.00;2
RADL3;Raia Drogasil ON;Farmácias;22.00;1
PGMN3;Pague Menos ON;Farmácias;4.00;3
PNVL3;Panvel ON;Farmácias;12.00;3
HYPE3;Hypera ON;Farmacêutica;22.00;2
BLAU3;Blau Farmacêutica ON;Farmacêutica;12.00;3
RDOR3;Rede D'Or ON;Saúde;38.00;1
HAPV3;Hapvida ON;Saúde;40.00;1
FLRY3;Fleury ON;Saúde;14.00;2
ODPV3;Odontoprev ON;Saúde;12.00;3
QUAL3;Qualicorp ON;Saúde;3.00;3
MATD3;Mater Dei ON;Saúde;3.50;3
ONCO3;Oncoclínicas ON;Saúde;3.00;3
VIVT3;Telefônica Brasil ON;Telecomunicações;30.00;1
TIMS3;TIM ON;Telecomunicações;20.00;1
TOTS3;Totvs ON;Tecnologia;40.00;1
LWSA3;Locaweb ON;Tecnologia;4.50;3
INTB3;Intelbras ON;Tecnologia;13.00;3
POSI3;Positivo ON;Tecnologia;6.00;3
COGN3;Cogna ON;Educação;3.00;2
YDUQ3;Yduqs ON;Educação;12.00;2
ANIM3;Ânima ON;Educação;3.00;3
SEER3;Ser Educacional ON;Educação;4.00;3
CSED3;Cruzeiro do Sul ON;Educação;5.00;3
CYRE3;Cyrela ON;Construção civil;28.00;1
MRVE3;MRV ON;Construção civil;7.00;2
EZTC3;EZTec ON;Construção civil;14.00;2
DIRR3;Direcional ON;Construção civil;14.00;2
TEND3;Tenda ON;Construção civil;25.00;2
CURY3;Cury ON;Construção civil;30.00;2
EVEN3;Even ON;Construção civil;7.00;3
JHSF3;JHSF ON;Construção civil;4.50;3
LAVV3;Lavvi ON;Construção civil;11.00;3
PLPL3;Plano&Plano ON;Construção civil;13.00;3
MDNE3;Moura Dubeux ON;Construção civil;20.00;3
TRIS3;Trisul ON;Construção civil;6.00;3
DXCO3;Dexco ON;Materiais de construção;6.00;3
MULT3;Multiplan ON;Shoppings;27.00;1
ALOS3;Allos ON;Shoppings;23.00;1
IGTI11;Iguatemi Unit;Shoppings;22.00;2
MBRF3;MBRF ON;Alimentos;20.00;1
BEEF3;Minerva ON;Alimentos;6.00;2
CAML3;Camil ON;Alimentos;5.00;3
MDIA3;M.Dias Branco ON;Alimentos;25.00;3
SLCE3;SLC Agrícola ON;Agronegócio;18.00;2
TTEN3;3tentos ON;Agronegócio;14.00;3
AGRO3;BrasilAgro ON;Agronegócio;22.00;3
RAPT4;Randon PN;Autopeças;8.00;3
TUPY3;Tupy ON;Autopeças;16.00;3
LEVE3;Mahle Metal Leve ON;Autopeças;30.00;3
FRAS3;Fras-le ON;Autopeças;20.00;3
SMFT3;Smart Fit ON;Academias;22.00;2
CVCB3;CVC ON;Turismo;2.00;3
OPCT3;OceanPact ON;Serviços offshore;6.00;3
GGPS3;GPS ON;Serviços;16.00;3
VLID3;Valid ON;Serviços;22.00;3
"""

# (ticker;nome;segmento;preço;liquidez;categoria)
FUNDS = """
MXRF11;Maxi Renda;Papel (CRI);9.50;1;FII
HGLG11;CSHG Logística;Logística;155.00;1;FII
KNRI11;Kinea Renda Imobiliária;Híbrido;150.00;1;FII
XPML11;XP Malls;Shoppings;105.00;1;FII
VISC11;Vinci Shopping Centers;Shoppings;105.00;1;FII
HGRU11;CSHG Renda Urbana;Renda urbana;125.00;1;FII
KNCR11;Kinea Rendimentos Imobiliários;Papel (CRI);103.00;1;FII
KNIP11;Kinea Índices de Preços;Papel (CRI);90.00;1;FII
KNSC11;Kinea Securities;Papel (CRI);8.50;2;FII
KNHF11;Kinea Hedge Fund;Híbrido;90.00;2;FII
CPTS11;Capitânia Securities II;Papel (CRI);7.80;1;FII
IRDM11;Iridium Recebíveis;Papel (CRI);70.00;2;FII
RECR11;REC Recebíveis;Papel (CRI);80.00;2;FII
HGCR11;CSHG Recebíveis;Papel (CRI);95.00;2;FII
VGHF11;Valora Hedge Fund;Papel (CRI);7.50;1;FII
XPCI11;XP Crédito Imobiliário;Papel (CRI);85.00;2;FII
MCCI11;Mauá Capital Recebíveis;Papel (CRI);85.00;2;FII
RBRR11;RBR Rendimento High Grade;Papel (CRI);85.00;2;FII
RBRY11;RBR Crédito Estruturado;Papel (CRI);95.00;2;FII
VRTA11;Fator Verità;Papel (CRI);85.00;2;FII
DEVA11;Devant Recebíveis;Papel (CRI);30.00;3;FII
URPR11;Urca Prime Renda;Papel (CRI);50.00;3;FII
BCRI11;Banestes Recebíveis;Papel (CRI);70.00;3;FII
SNCI11;Suno Recebíveis;Papel (CRI);90.00;3;FII
CVBI11;VBI CRI;Papel (CRI);85.00;2;FII
BTCI11;BTG Crédito Imobiliário;Papel (CRI);9.00;2;FII
RZAK11;Riza Akin;Papel (CRI);80.00;3;FII
HABT11;Habitat Recebíveis;Papel (CRI);75.00;3;FII
XPLG11;XP Log;Logística;100.00;1;FII
BTLG11;BTG Logística;Logística;100.00;1;FII
VILG11;Vinci Logística;Logística;90.00;2;FII
LVBI11;VBI Logístico;Logística;110.00;2;FII
BRCO11;Bresco Logística;Logística;115.00;2;FII
GGRC11;GGR Covepi;Logística;10.00;2;FII
XPIN11;XP Industrial;Logística;70.00;3;FII
SDIL11;SDI Logística;Logística;85.00;3;FII
PATL11;Pátria Logística;Logística;55.00;3;FII
HSML11;HSI Malls;Shoppings;80.00;2;FII
MALL11;Malls Brasil Plural;Shoppings;105.00;2;FII
HGBS11;Hedge Brasil Shopping;Shoppings;19.00;2;FII
PMLL11;Pátria Malls;Shoppings;100.00;3;FII
BPML11;BTG Shoppings;Shoppings;85.00;3;FII
CPSH11;Capitânia Shoppings;Shoppings;9.50;3;FII
PVBI11;VBI Prime Properties;Lajes corporativas;80.00;2;FII
HGRE11;CSHG Real Estate;Lajes corporativas;115.00;2;FII
JSRE11;JS Real Estate;Lajes corporativas;65.00;2;FII
RBRP11;RBR Properties;Lajes corporativas;50.00;3;FII
BRCR11;BTG Corporate Office;Lajes corporativas;45.00;2;FII
RCRB11;Rio Bravo Renda Corporativa;Lajes corporativas;140.00;3;FII
GTWR11;Green Towers;Lajes corporativas;80.00;3;FII
VINO11;Vinci Offices;Lajes corporativas;5.00;3;FII
RECT11;REC Renda Imobiliária;Lajes corporativas;30.00;3;FII
TRXF11;TRX Real Estate;Renda urbana;100.00;2;FII
RBVA11;Rio Bravo Varejo;Renda urbana;9.00;3;FII
GARE11;Guardian Real Estate;Renda urbana;8.50;2;FII
ALZR11;Alianza Trust;Híbrido;105.00;2;FII
TGAR11;TG Ativo Real;Desenvolvimento;110.00;2;FII
HFOF11;Hedge Top FoFII;Fundo de fundos;70.00;2;FII
RBRF11;RBR Alpha;Fundo de fundos;7.00;2;FII
KISU11;Kilima Suno 30;Fundo de fundos;7.00;3;FII
RZTR11;Riza Terrax;Agro (terras);95.00;2;FII
KNCA11;Kinea Crédito Agro;Fiagro (CRA);95.00;2;FIAGRO
RZAG11;Riza Agro;Fiagro (CRA);9.00;2;FIAGRO
XPCA11;XP Crédito Agro;Fiagro (CRA);8.00;2;FIAGRO
SNAG11;Suno Agro;Fiagro (CRA);10.00;2;FIAGRO
VGIA11;Valora CRA;Fiagro (CRA);9.00;2;FIAGRO
KDIF11;Kinea Infra;Debêntures incentivadas;125.00;2;FIINFRA
JURO11;Sparta Infra;Debêntures incentivadas;100.00;2;FIINFRA
CPTI11;Capitânia Infra;Debêntures incentivadas;90.00;2;FIINFRA
IFRA11;Itaú Infra;Debêntures incentivadas;100.00;3;FIINFRA
BDIF11;BTG Infra;Debêntures incentivadas;80.00;3;FIINFRA
"""

# (ticker;nome;segmento;preço;liquidez;fator;beta;vol)
ETFS = """
BOVA11;iShares Ibovespa;Índice Brasil;130.00;1;BR;0.95;0.20
BOVV11;It Now Ibovespa;Índice Brasil;130.00;2;BR;0.95;0.20
SMAL11;iShares Small Cap;Índice Brasil;100.00;1;BR;0.85;0.25
DIVO11;It Now IDIV;Índice Brasil (dividendos);100.00;2;BR;0.80;0.18
IVVB11;iShares S&P 500;Índice EUA (em reais);380.00;1;US;0.90;0.18
SPXI11;It Now S&P 500;Índice EUA (em reais);350.00;2;US;0.90;0.18
NASD11;Trend Nasdaq 100;Índice EUA (em reais);15.00;2;US;0.90;0.24
ACWI11;Trend MSCI ACWI;Índice global (em reais);12.00;3;US;0.80;0.16
XINA11;Trend China;Índice China (em reais);9.00;3;-;0;0.28
GOLD11;Trend Ouro;Ouro (em reais);18.00;2;-;0;0.16
HASH11;Hashdex Nasdaq Crypto;Criptoativos;60.00;2;-;0;0.55
IMAB11;It Now IMA-B;Renda fixa (IPCA+);95.00;3;BR;0.15;0.06
B5P211;It Now IMA-B5 P2;Renda fixa (IPCA+);100.00;3;BR;0.10;0.04
FIXA11;Mirae Renda Fixa Pré;Renda fixa (pré);10.00;3;BR;0.15;0.07
LFTS11;Investo Tesouro Selic;Renda fixa (Selic);115.00;3;-;0;0.01
"""

# (ticker;empresa;setor;preço) — BDRs nível I/III, lote 1, correlacionados ao fator EUA
BDRS = """
AAPL34;Apple;Tecnologia;60.00
MSFT34;Microsoft;Tecnologia;80.00
AMZO34;Amazon;Varejo e nuvem;55.00
GOGL34;Alphabet (Google);Tecnologia;65.00
M1TA34;Meta Platforms;Tecnologia;70.00
NVDC34;Nvidia;Semicondutores;20.00
TSMC34;TSMC;Semicondutores;80.00
ITLC34;Intel;Semicondutores;10.00
TSLA34;Tesla;Veículos elétricos;60.00
NFLX34;Netflix;Mídia;20.00
DISB34;Walt Disney;Mídia;45.00
JPMC34;JPMorgan Chase;Bancos;90.00
BOAC34;Bank of America;Bancos;50.00
BERK34;Berkshire Hathaway;Holdings;120.00
VISA34;Visa;Meios de pagamento;80.00
MSCD34;Mastercard;Meios de pagamento;70.00
COCA34;Coca-Cola;Bebidas;70.00
MCDC34;McDonald's;Restaurantes;60.00
WALM34;Walmart;Varejo;50.00
NIKE34;Nike;Vestuário;25.00
PFIZ34;Pfizer;Farmacêutica;25.00
JNJB34;Johnson & Johnson;Farmacêutica;60.00
EXXO34;Exxon Mobil;Petróleo e gás;60.00
CHVX34;Chevron;Petróleo e gás;55.00
MELI34;Mercado Livre;Varejo online;100.00
BABA34;Alibaba;Varejo online;30.00
ROXO34;Nu Holdings (Nubank);Bancos digitais;9.00
INBR32;Inter&Co;Bancos digitais;40.00
XPBR31;XP Inc.;Serviços financeiros;50.00
"""

EVEN_MONTHS, ALL_MONTHS = (2, 4, 6, 8, 10, 12), tuple(range(1, 13))
# raiz, nome, meses, regra de vencimento, nº de vencimentos, moeda, tick, valor do tick, margem, corretagem,
# preço, vol, spread, profundidade, fator, beta, atividade, carrego por vencimento, segue a raiz
FUTURES = [
    ("WIN", "Mini Índice Bovespa (R$ 0,20/pt)", EVEN_MONTHS, wednesday_near_15, 2, "BRL", "5", "1.00", "150", "0.25",
     158_000.0, 0.22, 1, 60, "BR", 0.95, 1.0, 0.012, None),
    ("IND", "Índice Bovespa cheio (R$ 1/pt)", EVEN_MONTHS, wednesday_near_15, 1, "BRL", "5", "5.00", "1500", "1.00",
     158_000.0, 0.22, 1, 30, "BR", 0.95, 0.6, 0.0, "WIN"),
    ("WDO", "Mini Dólar (R$ 10/pt)", ALL_MONTHS, first_bday, 2, "BRL", "0.5", "5.00", "150", "1.00",
     5_400.0, 0.15, 1, 40, "BR", -0.35, 1.0, 0.004, None),
    ("DOL", "Dólar cheio (R$ 50/pt)", ALL_MONTHS, first_bday, 1, "BRL", "0.5", "25.00", "750", "2.50",
     5_400.0, 0.15, 1, 20, "BR", -0.35, 0.6, 0.0, "WDO"),
    ("BGI", "Boi gordo (330 arrobas, R$/@)", ALL_MONTHS, last_bday, 2, "BRL", "0.05", "16.50", "2500", "1.50",
     320.0, 0.15, 2, 15, None, 0.0, 0.3, 0.005, None),
    ("CCM", "Milho (450 sacas, R$/saca)", (1, 3, 5, 7, 9, 11), day_15, 2, "BRL", "0.01", "4.50", "1500", "1.50",
     70.0, 0.25, 3, 15, None, 0.0, 0.3, 0.01, None),
    ("ICF", "Café arábica (100 sacas, US$/saca)", (3, 5, 7, 9, 12), icf_expiry, 2, "USD", "0.05", "5.00", "2500", "1.00",
     450.0, 0.30, 2, 10, None, 0.0, 0.3, 0.01, None),
]
DI_YEARS = (1, 2, 3, 4, 5, 7, 9)
OPTION_UNDERLYINGS = ("PETR4", "VALE3", "ITUB4", "BBDC4", "BBAS3", "ABEV3", "B3SA3", "PRIO3", "MGLU3", "BOVA11")

# atividade, spread (ticks), profundidade, vol a.a., beta ao fator BR
EQUITY_TIERS = {"1": (1.0, 1, 20_000, 0.28, 0.65), "2": (0.6, 1, 8_000, 0.35, 0.55), "3": (0.3, 2, 2_500, 0.45, 0.45)}
FUND_TIERS = {"1": (0.6, 1, 2_000, 0.12, 0.25), "2": (0.3, 2, 500, 0.15, 0.20), "3": (0.15, 3, 200, 0.18, 0.15)}
STOCK_TYPES = {"3": "ON", "4": "PN", "5": "PNA", "6": "PNB"}


def _rows(table: str) -> list[list[str]]:
    return [line.split(";") for line in table.strip().splitlines()]


def _stock_type(ticker: str) -> str:
    return "Unit" if ticker.endswith("11") else STOCK_TYPES.get(ticker[-1], "")


def _strike_step(spot: float) -> float:
    return 0.25 if spot < 10 else 0.5 if spot < 30 else 1.0 if spot < 80 else 2.0 if spot < 200 else 5.0


def _strike_code(strike: float) -> str:
    return str(round(strike * 100 if strike < 10 else strike * 10 if strike < 100 else strike))


# ───────────────────────────── Construção ─────────────────────────────
@dataclass
class Catalog:
    today: date
    instruments: dict[str, Instrument] = field(default_factory=dict)
    meta: dict[str, dict] = field(default_factory=dict)
    models: dict[str, SymbolModel] = field(default_factory=dict)
    front: dict[str, str] = field(default_factory=dict)

    def add(self, inst: Instrument, model: dict, **meta) -> None:
        self.instruments[inst.symbol] = inst
        self.meta[inst.symbol] = meta
        self.models[inst.symbol] = SymbolModel(inst, **model)


def _add_stocks(cat: Catalog) -> None:
    for ticker, name, sector, price, tier in _rows(STOCKS):
        act, spread, depth, vol, beta = EQUITY_TIERS[tier]
        p, kind = float(price), _stock_type(ticker)
        fee = D(f"{p * 0.0003:.6f}")  # emolumentos + liquidação (~0,03%), corretagem zero
        cat.add(Instrument(ticker, "BRL", D("0.01"), D("0.01"), fee, D(price), D("100"), "B3", name),
                dict(price=p, annual_vol=vol, spread_ticks=spread, depth=depth, activity=act, factor="BR", beta=beta),
                category="ACAO", type=kind, segment=sector)
        cat.add(Instrument(ticker + "F", "BRL", D("0.01"), D("0.01"), fee, D(price), D("1"), "B3",
                           f"{name} · fracionário"),
                dict(price=p, annual_vol=vol, spread_ticks=spread + 1, depth=300, activity=act * 0.5, underlying=ticker),
                category="ACAO", type=kind, segment=sector, fractional=True, standard=ticker)


def _add_funds(cat: Catalog) -> None:
    for ticker, name, segment, price, tier, category in _rows(FUNDS):
        act, spread, depth, vol, beta = FUND_TIERS[tier]
        p = float(price)
        cat.add(Instrument(ticker, "BRL", D("0.01"), D("0.01"), D(f"{p * 0.0003:.6f}"), D(price), D("1"), "B3", name),
                dict(price=p, annual_vol=vol, spread_ticks=spread, depth=depth, activity=act, factor="BR", beta=beta),
                category=category, type="Cota", segment=segment)


def _add_etfs(cat: Catalog) -> None:
    for ticker, name, segment, price, tier, factor, beta, vol in _rows(ETFS):
        act, spread, depth, _, _ = FUND_TIERS[tier]
        p = float(price)
        cat.add(Instrument(ticker, "BRL", D("0.01"), D("0.01"), D(f"{p * 0.0003:.6f}"), D(price), D("1"), "B3", name),
                dict(price=p, annual_vol=float(vol), spread_ticks=spread, depth=depth * 2, activity=min(1.0, act * 1.5),
                     factor=None if factor == "-" else factor, beta=float(beta)),
                category="ETF", type="ETF", segment=segment)


def _add_bdrs(cat: Catalog) -> None:
    for ticker, name, sector, price in _rows(BDRS):
        p = float(price)
        cat.add(Instrument(ticker, "BRL", D("0.01"), D("0.01"), D(f"{p * 0.0003:.6f}"), D(price), D("1"), "B3",
                           f"{name} · BDR"),
                dict(price=p, annual_vol=0.30, spread_ticks=3, depth=500, activity=0.3, factor="US", beta=0.6),
                category="BDR", type="BDR", segment=sector)


def _add_futures(cat: Catalog) -> None:
    for (root, name, months, expiry_rule, count, ccy, tick, tick_value, margin, fee, price, vol, spread, depth,
         factor, beta, activity, carry, follows) in FUTURES:
        for i, (y, m, exp) in enumerate(next_expiries(cat.today, months, count, expiry_rule)):
            code = contract_code(root, y, m)
            inst = Instrument(code, ccy, D(tick), D(tick_value), D(fee), D(margin), D("1"), "B3",
                              f"{name} · venc. {exp:%d/%m/%Y}")
            if i == 0 and follows is None:
                model = dict(price=price, annual_vol=vol, spread_ticks=spread, depth=depth, activity=activity,
                             factor=factor, beta=beta)
            else:  # vencimento seguinte ou contrato cheio: espelha o contrato de referência
                base = cat.front[follows] if follows else cat.front[root]
                model = dict(price=price * (1 + carry * i), annual_vol=vol, spread_ticks=spread + (1 if i else 0),
                             depth=depth / (2 if i else 1), activity=activity * (0.5 if i else 1), underlying=base,
                             basis=1 + carry * i)
            if i == 0:
                cat.front[root] = code
            cat.add(inst, model, category="FUTURO", type="Futuro", segment=name.split(" (")[0],
                    root=root, expiry=exp.isoformat(), front=i == 0)


def _add_di(cat: Catalog) -> None:
    """DI1: negociado em taxa (% a.a.). P&L linearizado pelo DV01 do vencimento (aproximação)."""
    for k in DI_YEARS:
        y = cat.today.year + k
        exp = first_bday(y, 1)
        n = bdays_between(cat.today, exp) / 252
        rate = 14.40 - 0.30 * min(n, 4) + 0.06 * max(0.0, n - 4)
        tick = 0.001 if n <= 1.2 else 0.005
        pu = 100_000 / (1 + rate / 100) ** n
        tick_value = n * pu / (1 + rate / 100) * tick / 100  # comprado em taxa ganha quando a taxa sobe
        code = contract_code("DI1", y, 1)
        cat.add(Instrument(code, "BRL", D(str(tick)), D(f"{max(tick_value, 0.01):.2f}"), D("0.50"),
                           D(f"{pu * 0.02:.0f}"), D("1"), "B3", f"DI1 · taxa % a.a. · venc. {exp:%d/%m/%Y}"),
                dict(price=round(rate / tick) * tick, annual_vol=0.10, spread_ticks=1, depth=500,
                     activity=0.6, factor="BR", beta=-0.4),
                category="FUTURO", type="Juros", segment="DI1 (taxa de juros)", root="DI1",
                expiry=exp.isoformat(), front=k == DI_YEARS[0])
        if k == DI_YEARS[0]:
            cat.front["DI1"] = code


def _add_options(cat: Catalog) -> None:
    build_now = datetime.combine(cat.today, time(12, 0), tzinfo=timezone.utc)
    expiries = next_expiries(cat.today + timedelta(days=1), ALL_MONTHS, 2, third_friday)
    for underlying in OPTION_UNDERLYINGS:
        base = cat.models[underlying]
        spot, step = base.price, _strike_step(base.price)
        first = math.floor(spot * 0.85 / step) * step
        strikes = [round(first + i * step, 2) for i in range(int(spot * 0.30 / step) + 2)]
        root = underlying[:4]
        for y, m, exp in expiries:
            exp_dt = datetime.combine(exp, B3_CLOSE_UTC, tzinfo=timezone.utc)
            t0 = max(0.0, (exp_dt - build_now).total_seconds()) / (365 * 86400)
            for kind, letters, label in (("C", CALL_LETTERS, "Call"), ("P", PUT_LETTERS, "Put")):
                for strike in strikes:
                    code = f"{root}{letters[m - 1]}{_strike_code(strike)}"
                    if code in cat.instruments:
                        continue
                    theo = black_scholes(spot, strike, t0, RISK_FREE, option_iv(base.annual_vol, spot, strike), kind)
                    atm = abs(math.log(strike / spot)) < 0.05
                    cat.add(Instrument(code, "BRL", D("0.01"), D("0.01"), D("0.0005"), D(f"{spot * 0.15:.2f}"),
                                       D("100"), "B3", f"{underlying} {label} {strike:.2f} · venc. {exp:%d/%m/%Y}"),
                            dict(price=max(theo.price, 0.01), annual_vol=base.annual_vol, depth=5_000,
                                 activity=0.5 if atm else 0.2, underlying=underlying, option_type=kind,
                                 strike=strike, expiry=exp_dt, rate=RISK_FREE),
                            category="OPCAO", type=label, segment=f"Opções de {underlying}",
                            underlying=underlying, strike=strike, option_type=kind, expiry=exp.isoformat())


def _add_international(cat: Catalog) -> None:
    for inst, model, segment in [
        (Instrument("EURUSD", "USD", D("0.00001"), D("1.00"), D("3.50"), D("1170"), D("0.01"), "FX",
                    "Euro/Dólar (1 lote = 100k, 1:100)"),
         dict(price=1.17, annual_vol=0.08, spread_ticks=8, depth=30), "Forex"),
        (Instrument("XAUUSD", "USD", D("0.01"), D("1.00"), D("5.00"), D("4000"), D("0.01"), "CFD",
                    "Ouro spot (1 lote = 100 oz, 1:100)"),
         dict(price=4_000.0, annual_vol=0.18, spread_ticks=25, depth=10), "Commodities"),
        (Instrument("US100", "USD", D("0.1"), D("0.10"), D("0"), D("250"), D("0.1"), "CFD",
                    "Nasdaq 100 CFD (US$ 1/ponto, 1:100)"),
         dict(price=25_000.0, annual_vol=0.22, spread_ticks=10, depth=50, factor="US", beta=0.9), "Índices"),
    ]:
        cat.add(inst, model, category="INTERNACIONAL", type=inst.venue, segment=segment)


def build_catalog(today: date) -> Catalog:
    cat = Catalog(today)
    _add_stocks(cat)
    _add_funds(cat)
    _add_etfs(cat)
    _add_bdrs(cat)
    _add_futures(cat)
    _add_di(cat)
    _add_options(cat)
    _add_international(cat)
    return cat


CATALOG = build_catalog(date.today())
INSTRUMENTS: dict[str, Instrument] = CATALOG.instruments
META: dict[str, dict] = CATALOG.meta
FRONT: dict[str, str] = CATALOG.front
FEATURED = [FRONT["WIN"], FRONT["WDO"], "PETR4", "VALE3", "ITUB4", "BOVA11", "MXRF11", "HGLG11", "EURUSD"]


def default_models() -> list[SymbolModel]:
    """Cópias novas dos modelos (cada simulador evolui os seus preços)."""
    return [replace(m) for m in CATALOG.models.values()]


def models_for(symbols: Iterable[str]) -> list[SymbolModel]:
    """Só os modelos necessários (+ cadeia de ativos-objeto e as fontes de câmbio), para backtests rápidos."""
    wanted = set(symbols) | {FRONT["WDO"], "EURUSD"}
    pending = list(wanted)
    while pending:
        underlying = CATALOG.models[pending.pop()].underlying
        if underlying and underlying not in wanted:
            wanted.add(underlying)
            pending.append(underlying)
    return [replace(m) for s, m in CATALOG.models.items() if s in wanted]


def update_fx(fx: FxConverter, quotes: list[Quote]) -> None:
    """Motor multimoeda: USD/BRL sai do mini dólar (pontos por US$ 1.000), EUR/USD do spot."""
    for q in quotes:
        if q.symbol == FRONT["WDO"]:
            fx.update("USD", "BRL", (q.bid + q.ask) / 2 / 1000)
        elif q.symbol == "EURUSD":
            fx.update("EUR", "USD", (q.bid + q.ask) / 2)


# Título, saldo inicial e regras (valores na moeda base da conta)
PLANS: dict[str, tuple[str, Decimal, dict]] = {
    "PRACTICE": ("Conta Prática 100k", D("100000"),
                 dict(daily_loss_limit=D("100000"), max_drawdown=D("100000"),
                      block_orders_risking_breach=False)),
    "CHALLENGE_50K": ("Desafio 50k · DD estático", D("50000"),
                      dict(daily_loss_limit=D("2500"), max_drawdown=D("5000"),
                           profit_target=D("4000"), min_trading_days=4)),
    "CHALLENGE_50K_TRAILING": ("Desafio 50k · DD trailing", D("50000"),
                               dict(daily_loss_limit=D("1000"), max_drawdown=D("2000"),
                                    trailing_drawdown=True, profit_target=D("3000"),
                                    min_trading_days=2, news_blackout=True)),
}


def make_account(plan: str, base_ccy: str, name: Optional[str] = None) -> Account:
    title, balance, rules = PLANS[plan]
    tz, reset = ("America/Sao_Paulo", time(18, 0)) if base_ccy == "BRL" else ("America/New_York", time(17, 0))
    limits = RiskLimits(reset_tz=tz, reset_time=reset, **rules)
    return Account(uuid.uuid4().hex[:12], base_ccy, balance, balance, limits, name=name or title, plan=plan)


# Notícias recorrentes do calendário simulado: símbolo → sensibilidade do salto à surpresa
WIN, WDO, DI = FRONT["WIN"], FRONT["WDO"], FRONT["DI1"]
NEWS_TEMPLATES = [
    dict(title="Payroll (NFP, mil vagas)", country="US", impact="HIGH", forecast=180.0, surprise_std=60.0,
         symbols={"EURUSD": -1.0, "XAUUSD": -1.0, "US100": -0.6, WDO: 0.8, WIN: -0.5, "IVVB11": -0.4}),
    dict(title="IPCA m/m (%)", country="BR", impact="HIGH", forecast=0.35, surprise_std=0.08,
         symbols={WIN: -1.0, DI: 1.0, "BOVA11": -1.0, "PETR4": -0.7, "ITUB4": -0.6, "MGLU3": -1.5,
                  "CYRE3": -1.2, WDO: 0.5, "MXRF11": 0.3}),
    dict(title="CPI EUA a/a (%)", country="US", impact="HIGH", forecast=2.8, surprise_std=0.2,
         symbols={"US100": -1.0, "XAUUSD": -0.6, "EURUSD": -0.7, WDO: 0.5, "IVVB11": -0.8}),
    dict(title="Decisão de juros FOMC (%)", country="US", impact="HIGH", forecast=3.75, surprise_std=0.15,
         symbols={"US100": -1.0, "XAUUSD": -0.8, "EURUSD": -0.8, WDO: 0.6, WIN: -0.4, "IVVB11": -0.8},
         profile=ShockProfile(spread_mult=5.0, depth_mult=0.15, vol_mult=5.0, half_life_s=120)),
    dict(title="Copom – Selic (%)", country="BR", impact="HIGH", forecast=13.75, surprise_std=0.2,
         symbols={WIN: -1.0, DI: 1.2, "BOVA11": -1.0, "ITUB4": -0.8, "BBAS3": -0.8, "CYRE3": -1.5,
                  "MGLU3": -1.8, WDO: -0.5}),
]
