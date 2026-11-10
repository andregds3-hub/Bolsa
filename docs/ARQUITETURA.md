# Arquitetura — Simulador de Mercado Global

## 0. Premissas de engenharia

1. **Três modos de dados, mesmo motor:** Live (feed licenciado), Replay (ticks históricos) e Sintético
   (gerador estocástico). O motor não sabe de onde vem o tick, então dá para lançar com Replay/Sintético
   enquanto o licenciamento de dados em tempo real (B3 e CME cobram por usuário) é negociado.
2. **Event sourcing no núcleo:** ordem, execução, lançamento de saldo e quebra de regra são eventos
   imutáveis; saldo, posições e ranking são projeções. Dá auditoria, recuperação após falha e paridade
   entre backtest e ao vivo.
3. **Horário do evento, não do relógio:** toda função do motor recebe `now`.
4. **Um escritor por conta:** cada conta pertence a um shard (ator) que processa comandos em série.
5. **Dinheiro em `Decimal`/`NUMERIC`,** nunca `float`.

## 1. Arquitetura técnica

```
┌──────────────────────────── CLIENTES ─────────────────────────────┐
│ Next.js · Landing (SSG/SEO) + App de Trading (SPA) · PWA           │
└──────────────┬───────────────────────────────────┬─────────────────┘
        HTTPS  │ REST/JSON                    WSS  │ Protobuf (deltas + seq)
     ┌─────────▼─────────┐                ┌────────▼─────────┐
     │  BFF / API         │◄──── OIDC ────►│  Keycloak (IdP)  │
     │  FastAPI           │                └──────────────────┘
     └─────────┬─────────┘                ┌──────────────────┐
               │ comandos                  │ Centrifugo (WS)   │ fan-out p/ 100k+ conexões
               ▼                           └────────▲─────────┘
┌──────────────────────────── NATS JetStream (barramento de eventos) ───────────────┐
└───▲──────────────▲────────────────▲──────────────▲──────────────▲────────────────┘
    │              │                │              │              │
 Market Data   Execution &      Macro/News      Quant          Strategy Runtime
 (live/replay/ Risk Engine      Simulator       (gregas, IV,   (EAs em sandbox
  sintético)   (shards/atores)  (choques)       backtest)      gVisor/Firecracker)
    │              │                                │
    ▼              ▼                                ▼
 TimescaleDB   PostgreSQL 17 (OLTP)   Redis/Valkey (cache, ZSET ranking)   S3 (ticks Parquet)
```

| Camada | Escolha | Por quê |
|---|---|---|
| Frontend | Next.js 15 + TypeScript, Tailwind + Radix/shadcn | Landing em SSG para SEO; app logado como SPA |
| Gráficos | TradingView Advanced Charts (Lightweight Charts como plano B; o MVP usa este) | Ferramentas de desenho e indicadores prontos |
| DOM / T&T / VAP | Canvas próprio (ou PixiJS) a 60 fps, agrupando updates a cada 100 ms | O DOM do React não aguenta 50 updates/s |
| Estado | Zustand (stream) + TanStack Query (REST) | Separa dados quentes de frios |
| Layout | Dockview (painéis arrastáveis, layouts salvos) | Workspace profissional |
| API/BFF | FastAPI (Python 3.13+) + Pydantic v2 | Mesma linguagem do motor e da parte quantitativa |
| Motor | Python asyncio com atores por shard; caminho crítico portável a Rust (PyO3) | Começar simples, medir, otimizar onde dói |
| Quant | NumPy/SciPy/Numba: BSM, Bjerksund-Stensland/binomial, superfície de vol | Gregas e payoff |
| EAs | Construtor visual + DSL; código livre em sandbox (gVisor, sem rede, cotas) | Isolamento e paridade backtest/ao vivo |
| Streaming | NATS JetStream (interno) + Centrifugo (WebSocket, JWT por canal) | Baixa latência, persistência, fan-out |
| Dados | PostgreSQL 17, TimescaleDB, Redis/Valkey, S3/MinIO | Cada dado no armazenamento certo |
| Autenticação | Keycloak (OIDC, OAuth Google/Apple/Microsoft, MFA, Argon2id, proteção contra força bruta) | Não escrever criptografia de autenticação na mão |
| Sessão web | BFF: tokens no servidor, cookie `HttpOnly; Secure; SameSite=Lax`; access 10 min, refresh rotativo; WS com token de 60 s | Protege contra XSS e roubo de token |
| Infra | Docker + Kubernetes (sa-east-1), Terraform, GitHub Actions, OpenTelemetry + Prometheus + Grafana + Loki, Sentry | Observabilidade desde o primeiro dia |

**Metas de latência (p99):** tick → motor → tela < 150 ms; confirmação de ordem < 30 ms.

### Motor multimoeda
O P&L nasce na moeda de cotação do instrumento e é convertido para a moeda base da conta pela cotação
corrente; pares sem cotação direta são triangulados via USD. No MVP, USD/BRL sai do mini dólar
(pontos por US$ 1.000) e EUR/USD do próprio par.

### Fidelidade às regras de mercado
- **B3:** WIN tick 5 pts (R$ 1/tick), WDO tick 0,5 pt (R$ 10/ponto), vencimentos (WIN bimestral, WDO
  mensal) e rolagem, lote padrão 100 e fracionário, leilões, circuit breaker, túnel de preços,
  emolumentos/ISS, IR parametrizável.
- **Forex/CFD:** lote de 100 mil, alavancagem por notional, swap com rollover às 17h NY (triplo na
  quarta), gaps de fim de semana, stop out por nível de margem.
- **Execução:** stop vira mercado e executa pior em gap; limite passivo executa no próprio preço;
  slippage proporcional à profundidade consumida.

### Simulador macro (choque de notícia)
- `surpresa_z = (atual − consenso) / desvio histórico da surpresa`
- Pré-release (−60 s): liquidez cai pela metade, spread ×1,5.
- Release: `mult(t) = 1 + (M − 1)·e^(−t·ln2/meia_vida)` para spread e volatilidade; a liquidez volta a
  1 na mesma curva; no modo Sintético o preço salta `k·z·β` por ativo.
- Regra opcional de mesa: bloqueia novas posições ±2 min de notícias de alto impacto.

## 2. Fluxo de telas

```
LANDING (/) ── Hero + proposta de valor + recursos + como funciona + disclaimer
   │  [Criar Conta Gratuita]  [Acessar Plataforma]
   ▼
CADASTRO / LOGIN (/auth) ── nome, e-mail, senha medida ao vivo, 3 aceites obrigatórios + marketing opcional,
   │                          tipo de conta e moeda base (onboarding embutido); OAuth na fase 2
   ▼
WORKSPACE (/app)
   ├─ Trading: watchlist · gráfico com linhas de SL/TP/médio · topo do book · boleta OCO com risco em R$
   │           e % da folga diária · posições · ordens · log
   │   Barra de risco fixa: saldo · equity · margem · perda diária usada · folga até o DD · meta · reset
   │   Estados: banner âmbar T−2 min de notícia; banner vermelho DAILY_LOCKED/BREACHED; verde PASSED
   ├─ Opções: templates (travas, straddle, strangle, borboleta, condor, venda coberta) → payoff + gregas
   ├─ Automação: EA de tendência → backtest determinístico no mesmo motor
   ├─ Journal: KPIs, curva de capital, histórico de trades com motivo de saída e custos
   ├─ Ranking: temporada, score de consistência, elegibilidade, resets públicos
   ├─ Calendário: eventos, consenso/atual/surpresa, disparo manual de notícia
   └─ Conta: contas simuladas (usar/resetar/criar), preferência de cores, exportar dados, excluir conta
```

## 3. Schema de banco

Ver [db/migrations/001_init.sql](../db/migrations/001_init.sql).

## 4. Métricas e ranking

- **Profit Factor** = Σ ganhos / |Σ perdas| · **Payoff** = ganho médio / |perda média| · **Win Rate**
- **Expectativa** = resultado médio por trade · **Sortino por trade** = média / desvio das perdas
- **Consistência** = 1 − (lucro do melhor dia / lucro total)
- **Score** = 30% Sortino + 25% (1 − MaxDD/limite) + 20% consistência + 15% PF + 10% retorno (0–100).
  Elegível: sem quebra de regra e mínimos de trades/dias (produção: 20 trades, 10 dias).
- **Anti-gaming:** reset arquiva a conta; 1 conta ranqueada por usuário por temporada; resets públicos.

## 5. Riscos de negócio

- **Licenciamento de dados:** B3 e CME cobram redistribuição por usuário, inclusive dado atrasado.
- **Prêmios em dinheiro:** se ranking ou desafios pagarem prêmios reais, o enquadramento jurídico muda;
  exige parecer antes do lançamento.
