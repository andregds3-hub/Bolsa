# BolsaSim — Simulador de Mercado Global

MVP executável do simulador descrito em [docs/ARQUITETURA.md](docs/ARQUITETURA.md): motor de execução com
ordens OCO e regras de Prop Firm, mercado sintético com choques de notícia, mesa de opções, EAs com
backtest, Trading Journal, ranking, autenticação com LGPD e frontend web.

## Rodar

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py            # http://127.0.0.1:8000
.\.venv\Scripts\python.exe -m pytest -q        # testes do servidor
node --test tests/js                            # testes dos indicadores (JavaScript)
```

No PyCharm: abra `main.py` e rode (Shift+F10). O estado fica em memória e recomeça a cada execução.

## Estrutura

| Caminho | Conteúdo |
|---|---|
| `engine/core.py` | Motor de execução, OCO (SL/TP/trailing/breakeven), RiskGuard (Daily Loss, Max DD estático/trailing, stop out, meta), multimoeda |
| `engine/market.py` | Gerador sintético (GBM), regime de notícia (spread, liquidez, volatilidade, salto), candles |
| `engine/options.py` | Black-Scholes-Merton, gregas, payoff e templates de estruturas |
| `engine/analytics.py` | Métricas do Journal e score do ranking |
| `engine/strategies.py` | EAs e backtest no mesmo motor do ao vivo |
| `engine/catalog.py` | Catálogo B3 (~900 instrumentos): ~150 ações/units + fracionário, FIIs, Fiagro, FI-Infra, ETFs, BDRs, futuros (WIN, IND, WDO, DOL, DI1, BGI, CCM, ICF) com vencimentos calculados pela data, séries de opções de 10 ativos; EURUSD, XAUUSD, US100; planos e calendário de notícias. Preços são referências sintéticas |
| `api/` | FastAPI: REST, WebSocket, autenticação (scrypt + sessão HMAC), LGPD |
| `web/` | Landing, cadastro/login e workspace (Lightweight Charts via CDN) |
| `web/indicators.js` | Indicadores técnicos (MMS, MME, Bollinger, VWAP, IFR, MACD, Estocástico) em funções puras |
| `web/drawings.js` | Ferramentas de desenho: tendência, horizontal, retângulo, Fibonacci e régua |
| `db/migrations/001_init.sql` | Schema PostgreSQL/TimescaleDB da fase 2 |
| `docker-compose.yml` | Infra da fase 2: Postgres+Timescale, Valkey, NATS, Keycloak |

## O que é MVP e o que é alvo

| Tema | MVP (este repositório) | Alvo de produção |
|---|---|---|
| Dados de mercado | Sintético (GBM + choques) | Feeds licenciados (B3, FX, CME) + Replay |
| Persistência | Memória | Event store (NATS JetStream) + PostgreSQL/Timescale |
| Autenticação | scrypt + cookie HttpOnly assinado | Keycloak/OIDC: OAuth social, MFA, recuperação de senha |
| Book | Topo do book (bid/ask + tamanhos) | DOM completo, Times & Trades, Volume Profile |
| Frontend | HTML/JS sem build | Next.js + TradingView Advanced Charts + Dockview |
| Opções | BSM, estruturas por template | Superfície de vol, americanas (Bjerksund-Stensland), ordens multi-perna |
| EAs | Estratégia interna (cruzamento de médias) | DSL/construtor visual + sandbox gVisor |

> Ambiente de simulação: nenhuma ordem vai ao mercado e nada aqui é recomendação de investimento.
