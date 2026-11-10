"use strict";
// Workspace de trading: REST para comandos, WebSocket para o fluxo (4 ticks/s).

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const S = {
  me: null, accounts: [], accountId: null, account: null, instruments: {}, quotes: {}, symbol: null,
  fav: [], cat: "FAV", query: "", visible: [],
  chart: null, candles: null, bars: [], tf: Number(localStorage.getItem("timeframe")) || 60, volBase: null,
  ind: null, indSeries: {}, subs: [], hoverIndex: null, drawing: null, loadingCandles: false, lines: [], linesKey: "",
  nextEvent: null, ws: null, view: "trade", lastMid: {}, clockSkew: 0, bannerHtml: "",
};
const TZ_SHIFT = -new Date().getTimezoneOffset() * 60; // Lightweight Charts exibe o tempo em UTC
const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
// Cores dos gráficos sempre lidas do tema ativo (claro/escuro), nunca fixas no código.
function chartTheme(timeScale = {}) {
  return {
    layout: {background: {color: "transparent"}, textColor: cssVar("--muted")},
    grid: {vertLines: {color: cssVar("--grid")}, horzLines: {color: cssVar("--grid")}},
    rightPriceScale: {borderColor: cssVar("--border")},
    timeScale: {borderColor: cssVar("--border"), ...timeScale},
  };
}
const ROLE = {ENTRY: "entrada", STOP_LOSS: "stop loss", BREAKEVEN: "breakeven", TRAILING: "trailing stop",
  TAKE_PROFIT: "take profit", MANUAL: "manual", LIQUIDATION: "liquidação de risco"};
const STATUS = {ACTIVE: "Ativa", DAILY_LOCKED: "Travada (diário)", BREACHED: "Reprovada", PASSED: "Aprovada"};
const CATEGORIES = [["FAV", "★ Favoritos"], ["ACAO", "Ações"], ["FII", "FIIs"], ["FIAGRO", "Fiagro"],
  ["FIINFRA", "FI-Infra"], ["ETF", "ETFs"], ["BDR", "BDRs"], ["FUTURO", "Futuros"], ["OPCAO", "Opções"],
  ["INTERNACIONAL", "Internacional"]];
const CATEGORY_NAME = Object.fromEntries(CATEGORIES);
const LIST_LIMIT = 200;
const roleName = r => ROLE[r] || r;

// ── utilitários ──
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const nowS = () => Date.now() / 1000 + S.clockSkew;
const fmtCache = {};
function money(v, ccy) {
  ccy = ccy || (S.account && S.account.base_ccy) || "BRL";
  fmtCache[ccy] = fmtCache[ccy] || new Intl.NumberFormat("pt-BR", {style: "currency", currency: ccy});
  return v == null ? "—" : fmtCache[ccy].format(v);
}
function price(v, sym) {
  if (v == null) return "—";
  const d = S.instruments[sym] ? S.instruments[sym].decimals : 2;
  return v.toLocaleString("pt-BR", {minimumFractionDigits: d, maximumFractionDigits: d});
}
const num = (v, d = 2) => v == null ? "—" : Number(v).toLocaleString("pt-BR", {minimumFractionDigits: d, maximumFractionDigits: d});
const pct = (v, d = 1) => v == null ? "—" : `${(v * 100).toFixed(d).replace(".", ",")}%`;
const signCls = v => v > 0 ? "pos" : v < 0 ? "neg" : "";
// Normaliza para busca: minúsculas e sem acentos (remove as marcas combinantes U+0300–U+036F do NFD).
const norm = s => [...String(s || "").normalize("NFD")]
  .filter(c => c.charCodeAt(0) < 0x300 || c.charCodeAt(0) > 0x36f).join("").toLowerCase();
const fmtDate = iso => iso ? new Date(iso + "T12:00:00").toLocaleDateString("pt-BR") : "";
function change(q) {
  if (!q || !q.ref) return null;
  return (q.bid + q.ask) / 2 / q.ref - 1;
}
function changeText(v) {
  return v == null ? "" : `${v > 0 ? "▲" : v < 0 ? "▼" : "•"} ${pct(Math.abs(v), 2)}`;
}
function clock(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return (h ? String(h).padStart(2, "0") + ":" : "") + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}
const kv = (k, v) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`;
const kpi = (k, v, cls = "") => `<div class="card kpi"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;

async function api(path, opts = {}) {
  const r = await fetch(path, {method: opts.method || "GET", headers: {"Content-Type": "application/json"},
    body: opts.body ? JSON.stringify(opts.body) : undefined});
  if (r.status === 401) { location.href = "/auth?mode=login"; throw new Error("Sessão expirada."); }
  if (r.status === 204) return null;
  const data = await r.json().catch(() => null);
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(!d ? r.statusText : typeof d === "string" ? d : d.map(x => x.msg).join("; "));
  }
  return data;
}
function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = text;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 6500);
}
function log(text) {
  const box = $("#log"), el = document.createElement("div");
  el.textContent = `${new Date().toLocaleTimeString("pt-BR")}  ${text}`;
  box.prepend(el);
  while (box.children.length > 100) box.lastChild.remove();
}

// ── inicialização ──
async function init() {
  S.me = await api("/api/me");
  (await api("/api/instruments")).forEach(i => { S.instruments[i.symbol] = i; });
  const win = Object.values(S.instruments).find(i => i.front && i.root === "WIN");
  S.symbol = win ? win.symbol : Object.keys(S.instruments)[0];
  await loadAccounts();
  setupAssets(); setupNav(); setupTicket(); setupChart(); setupOptions(); setupAutomation(); setupAccountView();
  S.quotes = await api(`/api/quotes?symbols=${encodeURIComponent([S.symbol, ...S.visible].join(","))}`);
  await selectSymbol(S.symbol);
  connect();
  $("#theme-btn").onclick = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  $("#lvl").onclick = () => showView("analytics");
  applyTheme();
  setupTips();
  loadProgress();
  setInterval(loadProgress, 15000);
  setInterval(renderTape, 3000);
}

async function loadAccounts(selectId) {
  S.accounts = await api("/api/accounts");
  const keep = S.accounts.find(a => a.id === (selectId || S.accountId));
  S.accountId = (keep || S.accounts[0] || {}).id || null;
  S.account = S.accounts.find(a => a.id === S.accountId) || null;
  const sel = $("#acc-select");
  sel.innerHTML = S.accounts.map(a => `<option value="${a.id}">${esc(a.name)} · ${a.base_ccy}</option>`).join("");
  sel.value = S.accountId || "";
  sel.onchange = () => switchAccount(sel.value);
  subscribe();
  renderAccount();
}

function switchAccount(id) {
  S.accountId = id;
  S.account = S.accounts.find(a => a.id === id) || null;
  S.linesKey = "";
  subscribe();
  renderAccount();
  loadProgress();
  if (S.view === "analytics") loadAnalytics();
}

// ── streaming ──
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  S.ws = ws;
  ws.onopen = () => { $("#conn").textContent = "ao vivo"; $("#conn").classList.add("on"); subscribe(); };
  ws.onclose = e => {
    $("#conn").textContent = "reconectando…";
    $("#conn").classList.remove("on");
    if (e.code === 4401) { location.href = "/auth?mode=login"; return; }
    // Antes de reconectar, confere a sessão: se expirou, api() leva ao login em vez de tentar para sempre.
    setTimeout(() => api("/api/me").then(connect).catch(() => setTimeout(connect, 5000)), 2000);
  };
  ws.onmessage = e => onTick(JSON.parse(e.data));
}
// Assina só o que está na tela: ativo do gráfico + lista visível + favoritos (o servidor soma as posições).
function subscribe() {
  if (!S.ws || S.ws.readyState !== WebSocket.OPEN) return;
  const symbols = [...new Set([S.symbol, ...S.visible, ...S.fav])].filter(Boolean).slice(0, 400);
  S.ws.send(JSON.stringify({account_id: S.accountId, symbols}));
}

function onTick(msg) {
  Object.assign(S.quotes, msg.quotes);
  S.nextEvent = msg.next_event;
  S.clockSkew = msg.ts - Date.now() / 1000;
  if (msg.account && msg.account.id === S.accountId) {
    S.account = msg.account;
    const i = S.accounts.findIndex(a => a.id === msg.account.id);
    if (i >= 0) S.accounts[i] = msg.account;
  }
  msg.notices.forEach(handleNotice);
  updateCandle(msg.ts);
  renderWatch(); renderBook(); renderAccount(); renderTicketRisk();
}

function handleNotice(n) {
  const sym = n.symbol || "";
  switch (n.topic) {
    case "order.rejected": log(`REJEITADA ${sym} · ${n.code}`); break; // o toast vem da resposta REST
    case "order.filled":
      log(`EXECUTADA ${n.side === "BUY" ? "compra" : "venda"} ${num(+n.qty)} ${sym} @ ${price(+n.price, sym)} (${roleName(n.role)})`);
      break;
    case "order.canceled":
      if (!["OCO", "POSICAO_ENCERRADA"].includes(n.reason)) log(`CANCELADA ${sym} · ${n.reason}`);
      break;
    case "trade.closed": {
      const v = +n.net_pnl_base;
      toast(`Trade encerrado em ${sym}: ${money(v)} (${roleName(n.reason)})`, v >= 0 ? "good" : "warning");
      log(`FECHADO ${sym} ${money(v)} · ${roleName(n.reason)}`);
      setTimeout(loadProgress, 300);  // trade fechado pode render XP e conquistas
      break;
    }
    case "risk.breach":
      toast(n.reason === "DAILY_LOSS"
        ? "⛔ Limite de perda diária atingido: posições zeradas e conta travada até o reset."
        : "⛔ Max drawdown violado: desafio reprovado.", "critical");
      break;
    case "risk.stop_out": toast(`⛔ Stop out: margem abaixo do mínimo, ${sym} liquidado a mercado.`, "critical"); break;
    case "challenge.passed": toast("✅ Meta atingida: desafio aprovado!", "good"); break;
    case "account.reset": toast("Conta resetada.", "good"); break;
    case "news.released": {
      const ev = n.event;
      toast(`📰 ${ev.title}: ${ev.actual} vs consenso ${ev.forecast} (surpresa ${ev.surprise_z}σ). Spread e slippage elevados.`, "warning");
      log(`NOTÍCIA ${ev.title} · atual ${ev.actual} · consenso ${ev.forecast}`);
      break;
    }
  }
}

// ── navegador de ativos (busca, categorias, favoritos) ──
function defaultFavorites() {
  const fronts = ["WIN", "WDO", "DI1"].map(r => Object.values(S.instruments).find(i => i.front && i.root === r))
    .filter(Boolean).map(i => i.symbol);
  return [...fronts, "PETR4", "VALE3", "ITUB4", "BBAS3", "BOVA11", "IVVB11", "MXRF11", "HGLG11"].filter(s => S.instruments[s]);
}
function loadFavorites() {
  try {
    const saved = JSON.parse(localStorage.getItem("favorites"));
    S.fav = Array.isArray(saved) ? saved.filter(s => S.instruments[s]) : defaultFavorites();
  } catch (e) { S.fav = defaultFavorites(); }
}
function toggleFavorite(sym) {
  S.fav = S.fav.includes(sym) ? S.fav.filter(s => s !== sym) : [...S.fav, sym];
  localStorage.setItem("favorites", JSON.stringify(S.fav));
  renderAssetList();
}
function setupAssets() {
  loadFavorites();
  $("#as-cats").innerHTML = CATEGORIES.map(([k, label]) => `<button class="chip" data-cat="${k}" role="tab">${label}</button>`).join("");
  $$("#as-cats .chip").forEach(b => { b.onclick = () => { S.cat = b.dataset.cat; S.query = ""; $("#as-search").value = ""; renderAssetList(); }; });
  $("#as-search").oninput = e => { S.query = e.target.value; renderAssetList(); };
  const options = Object.values(S.instruments).filter(i => i.category === "OPCAO");
  $("#as-underlying").innerHTML = [...new Set(options.map(i => i.underlying))].map(u => `<option>${u}</option>`).join("");
  $("#as-expiry").innerHTML = [...new Set(options.map(i => i.expiry))].sort().map(e => `<option value="${e}">${fmtDate(e)}</option>`).join("");
  ["#as-underlying", "#as-expiry", "#as-otype"].forEach(id => { $(id).onchange = renderAssetList; });
  renderAssetList();
}
function filteredAssets() {
  const all = Object.values(S.instruments), q = norm(S.query.trim());
  if (q) {  // busca global: ticker, nome ou setor, inclusive fracionário e opções
    const rank = i => norm(i.symbol).startsWith(q) ? 0 : norm(i.symbol).includes(q) ? 1 : 2;
    return all.filter(i => norm(i.symbol).includes(q) || norm(i.description).includes(q) || norm(i.segment).includes(q))
      .sort((a, b) => rank(a) - rank(b) || (a.fractional ? 1 : 0) - (b.fractional ? 1 : 0) || a.symbol.localeCompare(b.symbol));
  }
  if (S.cat === "FAV") return S.fav.map(s => S.instruments[s]).filter(Boolean);
  if (S.cat === "OPCAO") {
    const u = $("#as-underlying").value, exp = $("#as-expiry").value, t = $("#as-otype").value;
    return all.filter(i => i.category === "OPCAO" && i.underlying === u && i.expiry === exp && i.option_type === t)
      .sort((a, b) => a.strike - b.strike);
  }
  const list = all.filter(i => i.category === S.cat && !i.fractional);
  return S.cat === "FUTURO"
    ? list.sort((a, b) => a.root.localeCompare(b.root) || a.expiry.localeCompare(b.expiry))
    : list.sort((a, b) => a.symbol.localeCompare(b.symbol));
}
function shortName(i) {
  if (i.category === "OPCAO") return `${i.type} ${num(i.strike)} · venc. ${fmtDate(i.expiry)}`;
  if (i.category === "FUTURO") return `${i.segment} · venc. ${fmtDate(i.expiry)}`;
  return i.fractional ? `${i.description}` : `${i.description}${i.segment ? " · " + i.segment : ""}`;
}
function renderAssetList() {
  $$("#as-cats .chip").forEach(b => b.classList.toggle("active", !S.query && b.dataset.cat === S.cat));
  $("#as-opt-filters").classList.toggle("hidden", Boolean(S.query) || S.cat !== "OPCAO");
  const items = filteredAssets(), shown = items.slice(0, LIST_LIMIT);
  $("#as-count").textContent = items.length > LIST_LIMIT
    ? `Mostrando ${LIST_LIMIT} de ${items.length}: refine a busca` : `${items.length} ativo${items.length === 1 ? "" : "s"}`;
  $("#as-list").innerHTML = shown.length ? shown.map(i => {
    const fav = S.fav.includes(i.symbol);
    return `<div class="sym" data-sym="${i.symbol}" role="button" tabindex="0" title="${esc(i.description)}">
      <button class="star ${fav ? "on" : ""}" data-star="${i.symbol}" aria-label="${fav ? "Remover dos" : "Adicionar aos"} favoritos">${fav ? "★" : "☆"}</button>
      <div class="s-main"><b>${i.symbol}</b><span class="muted small">${esc(shortName(i))}</span></div>
      <div class="s-px"><span class="mono" data-f="mid">—</span><span class="small" data-f="chg"></span></div></div>`;
  }).join("") : `<div class="empty">${S.cat === "FAV" && !S.query ? "Nenhum favorito: use ☆ para adicionar." : "Nenhum ativo encontrado."}</div>`;
  $$("#as-list .sym").forEach(el => {
    el.onclick = e => { if (!e.target.closest(".star")) selectSymbol(el.dataset.sym); };
    el.onkeydown = e => { if (e.key === "Enter") selectSymbol(el.dataset.sym); };
  });
  $$("#as-list .star").forEach(b => { b.onclick = e => { e.stopPropagation(); toggleFavorite(b.dataset.star); }; });
  S.visible = shown.map(i => i.symbol);
  subscribe();
  renderWatch();
}
function renderWatch() {
  for (const el of $$("#as-list .sym")) {
    const s = el.dataset.sym, q = S.quotes[s];
    el.classList.toggle("active", s === S.symbol);
    if (!q) continue;
    const v = change(q), chg = $('[data-f="chg"]', el);
    $('[data-f="mid"]', el).textContent = price((q.bid + q.ask) / 2, s);
    chg.textContent = changeText(v) + (q.spread_mult > 1.05 ? " ⚠" : "");
    chg.className = "small " + signCls(v);
    chg.title = q.spread_mult > 1.05 ? `Regime de notícia: spread ×${num(q.spread_mult, 1)}` : "";
  }
}

function candleColors() {
  const css = getComputedStyle(document.body);
  const up = css.getPropertyValue("--up").trim(), down = css.getPropertyValue("--down").trim();
  return {upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down};
}
function setupChart() {
  S.chart = LightweightCharts.createChart($("#chart"), {
    ...chartTheme({timeVisible: true, secondsVisible: true}), autoSize: true,
    rightPriceScale: {borderColor: cssVar("--border"), minimumWidth: PRICE_SCALE_WIDTH},
    crosshair: {mode: LightweightCharts.CrosshairMode.Normal},
  });
  S.candles = S.chart.addCandlestickSeries(candleColors());
  // painéis de osciladores acompanham o zoom/rolagem do gráfico principal
  S.chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
    if (range) S.subs.forEach(s => s.chart.timeScale().setVisibleLogicalRange(range));
  });
  S.chart.subscribeCrosshairMove(param => {
    S.hoverIndex = param && param.logical !== undefined && param.logical !== null ? Math.round(param.logical) : null;
    renderLegends();
  });
  S.drawing = new DrawingLayer($("#chart-main"), {
    chart: S.chart, series: S.candles, getBars: () => S.bars, getTf: () => S.tf,
    fmtPrice: v => price(v, S.symbol),
    onChange: items => localStorage.setItem(`drawings:${S.symbol}`, JSON.stringify(items)),
    onToolEnd: renderDrawTools,
  });
  setupChartTools();
  buildIndicators();
}
async function selectSymbol(sym) {
  const inst = S.instruments[sym];
  S.symbol = sym;
  S.loadingCandles = true;
  $("#c-sym").textContent = sym;
  $("#c-desc").textContent = `${inst.description} · ${CATEGORY_NAME[inst.category] || inst.category}` +
    (inst.category === "OPCAO" ? ` · objeto ${inst.underlying}` : inst.segment ? ` · ${inst.segment}` : "");
  $("#t-symbol").value = sym;
  subscribe();
  S.candles.applyOptions({priceFormat: {type: "price", precision: inst.decimals, minMove: inst.tick_size}});
  try {
    const data = await api(`/api/candles/${sym}?tf=${S.tf}`);
    if (S.symbol !== sym) return; // usuário trocou de ativo durante o carregamento
    S.bars = data.map(c => ({time: c.time + TZ_SHIFT, open: c.open, high: c.high, low: c.low, close: c.close,
      volume: c.volume}));
    S.candles.setData(S.bars.map(ohlc));
    S.volBase = null;
    refreshIndicators(true);
    S.drawing.setItems(JSON.parse(localStorage.getItem(`drawings:${sym}`) || "[]"));
    S.chart.timeScale().scrollToRealTime();
  } finally {
    if (S.symbol === sym) S.loadingCandles = false;
  }
  S.linesKey = "";
  onTicketChange(true);
  renderWatch(); renderBook(); renderPriceLines();
}
function updateCandle(ts) {
  const q = S.quotes[S.symbol];
  if (!q || S.loadingCandles) return;
  const mid = (q.bid + q.ask) / 2, bucket = Math.floor(ts / S.tf) * S.tf + TZ_SHIFT, c = S.bars[S.bars.length - 1];
  // volume do tick = quanto o acumulado da sessão andou desde o último tick
  const traded = S.volBase === null || q.volume === undefined ? 0 : Math.max(0, q.volume - S.volBase);
  S.volBase = q.volume === undefined ? null : q.volume;
  if (c && c.time === bucket) {
    c.high = Math.max(c.high, mid); c.low = Math.min(c.low, mid); c.close = mid; c.volume += traded;
  } else if (!c || bucket > c.time) {
    S.bars.push({time: bucket, open: mid, high: mid, low: mid, close: mid, volume: traded});
  } else return;
  S.candles.update(ohlc(S.bars[S.bars.length - 1]));
  refreshIndicators(false);
  S.drawing.render();
  $("#c-px").textContent = price(mid, S.symbol);
  const v = change(q);
  $("#c-chg").textContent = v == null ? "" : `${changeText(v)} no dia`;
  $("#c-chg").className = "small " + signCls(v);
  $("#c-regime").innerHTML = q.spread_mult > 1.05
    ? `<span class="tag warning">⚠ Regime de notícia: spread ×${num(q.spread_mult, 1)} · liquidez ${pct(q.depth_mult, 0)}</span>` : "";
}
// ── análise gráfica: timeframes, indicadores e desenhos ──
const PRICE_SCALE_WIDTH = 86;  // mesma largura em todos os painéis: candles e osciladores ficam alinhados
const CHART_TFS = [[5, "5s"], [15, "15s"], [60, "1m"], [300, "5m"], [900, "15m"]];
const IND_DEFS = {
  ema: {label: "MME", name: "Média móvel exponencial", pane: "main", params: {period: 9}, color: "--ind-1",
    info: "Média que dá mais peso aos preços recentes e reage rápido. Preço acima dela sugere tendência de alta."},
  sma: {label: "MMS", name: "Média móvel simples", pane: "main", params: {period: 21}, color: "--ind-2",
    info: "Média dos últimos N fechamentos. A MME curta cruzando a MMS longa é um sinal clássico de virada de tendência."},
  bb: {label: "Bollinger", name: "Bandas de Bollinger", pane: "main", params: {period: 20, mult: 2}, color: "--ind-3",
    info: "Média ± 2 desvios-padrão. Bandas estreitas indicam baixa volatilidade, que costuma anteceder movimentos fortes."},
  vwap: {label: "VWAP", name: "Preço médio ponderado por volume", pane: "main", params: {}, color: "--ind-4",
    info: "Preço médio do dia ponderado pelo volume. Grandes investidores usam como referência de preço justo."},
  volume: {label: "Volume", name: "Volume negociado", pane: "volume", params: {},
    info: "Quantidade negociada em cada candle. Movimentos com volume alto costumam ser mais confiáveis."},
  rsi: {label: "IFR", name: "Índice de Força Relativa (RSI)", pane: "sub", params: {period: 14}, color: "--ind-1",
    info: "Oscila de 0 a 100. Acima de 70 o ativo está sobrecomprado; abaixo de 30, sobrevendido."},
  macd: {label: "MACD", name: "Convergência/divergência de médias", pane: "sub", params: {fast: 12, slow: 26, signal: 9},
    color: "--ind-1", info: "Diferença entre duas médias exponenciais. Quando cruza a linha de sinal, o momento está mudando."},
  stoch: {label: "Estocástico", name: "Estocástico lento", pane: "sub", params: {period: 14, smooth: 3}, color: "--ind-1",
    info: "Onde o fechamento está na faixa máxima–mínima do período. Acima de 80 sobrecomprado, abaixo de 20 sobrevendido."},
};
const PARAM_NAMES = {period: "Período", mult: "Desvios", fast: "Rápida", slow: "Lenta", signal: "Sinal", smooth: "Suavização"};
const DRAW_TOOLS = [[null, "↖", "Cursor: mover e dar zoom no gráfico"], ["trend", "╱", "Linha de tendência"],
  ["hline", "―", "Linha horizontal (suporte/resistência)"], ["rect", "▭", "Retângulo (zona de preço)"],
  ["fib", "Fib", "Fibonacci de retração"], ["ruler", "📏", "Régua: mede a variação em pontos, % e candles"]];
const ohlc = ({time, open, high, low, close}) => ({time, open, high, low, close});
const withAlpha = (color, alpha) => /^#[0-9a-f]{6}$/i.test(color) ? color + alpha : color;

function loadIndicatorSettings() {
  const settings = Object.fromEntries(Object.entries(IND_DEFS).map(([id, d]) =>
    [id, {on: ["ema", "sma", "volume"].includes(id), ...d.params}]));
  try {
    const saved = JSON.parse(localStorage.getItem("indicators") || "{}");
    for (const id of Object.keys(settings)) Object.assign(settings[id], saved[id] || {});
  } catch (e) { /* configuração corrompida: usa o padrão */ }
  return settings;
}
function setupChartTools() {
  if (!CHART_TFS.some(([tf]) => tf === S.tf)) S.tf = 60;
  S.ind = loadIndicatorSettings();
  $("#tf-group").innerHTML = CHART_TFS.map(([tf, label]) =>
    `<button data-tf="${tf}" title="Candles de ${label}">${label}</button>`).join("");
  $$("#tf-group button").forEach(b => {
    b.onclick = () => { S.tf = +b.dataset.tf; localStorage.setItem("timeframe", S.tf); renderTfButtons(); selectSymbol(S.symbol); };
  });
  $("#draw-group").innerHTML = DRAW_TOOLS.map(([tool, icon, title]) =>
    `<button data-tool="${tool || ""}" title="${title}" aria-label="${title}">${icon}</button>`).join("") +
    `<button data-act="undo" title="Desfazer o último desenho" aria-label="Desfazer o último desenho">↶</button>` +
    `<button data-act="clear" title="Apagar os desenhos deste ativo" aria-label="Apagar os desenhos deste ativo">🗑</button>`;
  $$("#draw-group [data-tool]").forEach(b => { b.onclick = () => { S.drawing.setTool(b.dataset.tool || null); renderDrawTools(); }; });
  $("#draw-group [data-act=undo]").onclick = () => S.drawing.undo();
  $("#draw-group [data-act=clear]").onclick = () => {
    if (S.drawing.items.length && confirm("Apagar todos os desenhos deste ativo?")) S.drawing.clear();
  };
  $("#ind-btn").onclick = e => {
    e.stopPropagation();
    const open = !$("#ind-panel").classList.toggle("hidden");
    $("#ind-btn").setAttribute("aria-expanded", String(open));
  };
  document.addEventListener("click", e => {
    if (e.target.closest(".ind-wrap")) return;
    $("#ind-panel").classList.add("hidden");
    $("#ind-btn").setAttribute("aria-expanded", "false");
  });
  renderTfButtons();
  renderIndicatorPanel();
  renderDrawTools();
}
function renderTfButtons() {
  $$("#tf-group button").forEach(b => b.classList.toggle("active", +b.dataset.tf === S.tf));
}
function renderDrawTools() {
  const active = S.drawing ? S.drawing.tool : null;
  $$("#draw-group [data-tool]").forEach(b => b.classList.toggle("active", (b.dataset.tool || null) === active));
}
function onIndicatorsChanged() {
  localStorage.setItem("indicators", JSON.stringify(S.ind));
  buildIndicators();
  refreshIndicators(true);
}
function renderIndicatorPanel() {
  $("#ind-panel").innerHTML = `<div class="ind-head"><b>Indicadores</b>
    <span class="muted small">calculados sobre os candles do gráfico</span></div>` +
    Object.entries(IND_DEFS).map(([id, d]) => `<div class="ind-row">
      <label class="check"><input type="checkbox" data-ind="${id}" ${S.ind[id].on ? "checked" : ""}>
        ${d.color ? `<i class="swatch" style="background:var(${d.color})"></i>` : `<i class="swatch vol"></i>`}
        <span><b>${d.label}</b> <span class="muted small">${d.name}</span></span></label>
      <span class="info" tabindex="0" title="${esc(d.info)}">?</span>
      <div class="ind-params">${Object.keys(d.params).map(p => `<label>${PARAM_NAMES[p]}
        <input type="number" min="1" step="${p === "mult" ? "0.5" : "1"}" data-ind="${id}" data-param="${p}"
        value="${S.ind[id][p]}"></label>`).join("")}</div></div>`).join("");
  $$("#ind-panel input[type=checkbox]").forEach(cb => {
    cb.onchange = () => {
      const id = cb.dataset.ind;
      S.ind[id].on = cb.checked;
      if (cb.checked && !localStorage.getItem(`seen:${id}`)) {  // estudo: explica o indicador na primeira vez
        toast(`📘 ${IND_DEFS[id].name}: ${IND_DEFS[id].info}`);
        localStorage.setItem(`seen:${id}`, "1");
      }
      onIndicatorsChanged();
    };
  });
  $$("#ind-panel input[data-param]").forEach(input => {
    input.onchange = () => {
      const {ind, param} = input.dataset, value = Number(input.value);
      if (!(value > 0)) { input.value = S.ind[ind][param]; return; }
      S.ind[ind][param] = param === "mult" ? value : Math.round(value);
      onIndicatorsChanged();
    };
  });
}
function buildIndicators() {
  if (!S.chart) return;
  Object.values(S.indSeries).flat().forEach(s => s.chart.removeSeries(s.series));
  S.subs.forEach(sub => { sub.chart.remove(); sub.el.remove(); });
  S.indSeries = {};
  S.subs = [];
  const on = id => S.ind[id].on, color = id => cssVar(IND_DEFS[id].color);
  const add = (id, key, chart, series) => (S.indSeries[id] = S.indSeries[id] || []).push({key, chart, series});
  const line = (chart, c, extra = {}) => chart.addLineSeries({color: c, lineWidth: 2, priceLineVisible: false,
    lastValueVisible: false, crosshairMarkerVisible: false, ...extra});
  const level = (series, value) => series.createPriceLine({price: value, color: cssVar("--muted"), lineWidth: 1,
    lineStyle: 2, axisLabelVisible: true, title: ""});
  if (on("ema")) add("ema", "line", S.chart, line(S.chart, color("ema")));
  if (on("sma")) add("sma", "line", S.chart, line(S.chart, color("sma")));
  if (on("bb")) ["upper", "mid", "lower"].forEach(k =>
    add("bb", k, S.chart, line(S.chart, color("bb"), {lineWidth: 1, lineStyle: k === "mid" ? 2 : 0})));
  if (on("vwap")) add("vwap", "line", S.chart, line(S.chart, color("vwap"), {lineStyle: 1}));
  if (on("volume")) {
    add("volume", "volume", S.chart, S.chart.addHistogramSeries({priceScaleId: "vol", priceFormat: {type: "volume"},
      priceLineVisible: false, lastValueVisible: false}));
    S.chart.priceScale("vol").applyOptions({scaleMargins: {top: 0.8, bottom: 0}});
  }
  for (const id of ["rsi", "macd", "stoch"]) {  // osciladores: um painel abaixo do gráfico para cada um
    if (!on(id)) continue;
    const el = document.createElement("div");
    el.className = "subchart";
    el.innerHTML = `<div class="sub-legend"></div>`;
    $("#subcharts").append(el);
    const chart = LightweightCharts.createChart(el, {...chartTheme({visible: false}), autoSize: true,
      handleScroll: false, handleScale: false, crosshair: {mode: LightweightCharts.CrosshairMode.Normal},
      rightPriceScale: {borderColor: cssVar("--border"), minimumWidth: PRICE_SCALE_WIDTH}});
    S.subs.push({id, chart, el});
    if (id === "rsi") {
      const s = line(chart, color("rsi"));
      [70, 30].forEach(v => level(s, v));
      add(id, "line", chart, s);
    } else if (id === "macd") {
      add(id, "hist", chart, chart.addHistogramSeries({priceLineVisible: false, lastValueVisible: false}));
      add(id, "macd", chart, line(chart, color("macd")));
      add(id, "signal", chart, line(chart, cssVar("--ind-2"), {lineWidth: 1}));
    } else {
      const k = line(chart, color("stoch"));
      [80, 20].forEach(v => level(k, v));
      add(id, "k", chart, k);
      add(id, "d", chart, line(chart, cssVar("--ind-2"), {lineWidth: 1}));
    }
  }
  $("#ind-btn").textContent = `📊 Indicadores (${Object.values(S.ind).filter(x => x.on).length})`;
  const range = S.chart.timeScale().getVisibleLogicalRange();
  if (range) S.subs.forEach(s => s.chart.timeScale().setVisibleLogicalRange(range));
}
function computeIndicators() {
  const I = window.Indicators, bars = S.bars, closes = bars.map(b => b.close), p = S.ind, out = {};
  if (p.ema.on) out.ema = {line: I.ema(closes, p.ema.period)};
  if (p.sma.on) out.sma = {line: I.sma(closes, p.sma.period)};
  if (p.bb.on) out.bb = I.bollinger(closes, p.bb.period, p.bb.mult);
  if (p.vwap.on) out.vwap = {line: I.vwap(bars)};
  if (p.rsi.on) out.rsi = {line: I.rsi(closes, p.rsi.period)};
  if (p.macd.on) out.macd = I.macd(closes, p.macd.fast, p.macd.slow, p.macd.signal);
  if (p.stoch.on) out.stoch = I.stochastic(bars, p.stoch.period, p.stoch.smooth);
  return out;
}
// full: recarrega a série inteira; senão, só o último candle (a cada tick).
function refreshIndicators(full) {
  if (!S.bars.length) return;
  const data = computeIndicators(), last = S.bars.length - 1;
  const upColor = withAlpha(cssVar("--up"), "88"), downColor = withAlpha(cssVar("--down"), "88");
  S.indData = data;
  for (const [id, list] of Object.entries(S.indSeries)) {
    for (const {key, series} of list) {
      const point = i => {
        const b = S.bars[i];
        if (id === "volume") return {time: b.time, value: b.volume || 0, color: b.close >= b.open ? upColor : downColor};
        const v = data[id][key][i];
        if (v === null || v === undefined) return {time: b.time};  // espaço vazio mantém os painéis alinhados
        return key === "hist" ? {time: b.time, value: v, color: v >= 0 ? upColor : downColor} : {time: b.time, value: v};
      };
      if (full) series.setData(S.bars.map((_, i) => point(i)));
      else series.update(point(last));
    }
  }
  renderLegends();
}
// Legenda com os valores do candle sob o cursor (ou do último): texto na cor do texto, cor só na amostra.
function renderLegends() {
  const d = S.indData, n = S.bars.length;
  if (!d || !n) return;
  const i = S.hoverIndex !== null && S.hoverIndex >= 0 && S.hoverIndex < n ? S.hoverIndex : n - 1, bar = S.bars[i];
  const p = v => v === null || v === undefined ? "—" : price(v, S.symbol);
  const f = (v, digits) => v === null || v === undefined ? "—" : num(v, digits);
  const item = (cssColor, label, value) => `<span class="lg"><i class="swatch" style="background:var(${cssColor})"></i>${label} <b>${value}</b></span>`;
  const on = id => S.ind[id].on, parts = [`<span class="lg">Ab <b>${p(bar.open)}</b> Máx <b>${p(bar.high)}</b>
    Mín <b>${p(bar.low)}</b> Fech <b>${p(bar.close)}</b></span>`];
  if (on("ema")) parts.push(item("--ind-1", `MME ${S.ind.ema.period}`, p(d.ema.line[i])));
  if (on("sma")) parts.push(item("--ind-2", `MMS ${S.ind.sma.period}`, p(d.sma.line[i])));
  if (on("bb")) parts.push(item("--ind-3", `BB ${S.ind.bb.period},${S.ind.bb.mult}`, `${p(d.bb.lower[i])} – ${p(d.bb.upper[i])}`));
  if (on("vwap")) parts.push(item("--ind-4", "VWAP", p(d.vwap.line[i])));
  if (on("volume")) parts.push(`<span class="lg">Vol <b>${f(bar.volume, 0)}</b></span>`);
  $("#chart-legend").innerHTML = parts.join("");
  for (const sub of S.subs) {
    const s = S.ind[sub.id];
    $(".sub-legend", sub.el).innerHTML =
      sub.id === "rsi" ? item("--ind-1", `IFR ${s.period}`, f(d.rsi.line[i], 1)) :
      sub.id === "macd" ? item("--ind-1", `MACD ${s.fast},${s.slow}`, f(d.macd.macd[i], 3)) +
        item("--ind-2", `Sinal ${s.signal}`, f(d.macd.signal[i], 3)) + `<span class="lg">Hist <b>${f(d.macd.hist[i], 3)}</b></span>` :
      item("--ind-1", `%K ${s.period}`, f(d.stoch.k[i], 1)) + item("--ind-2", `%D ${s.smooth}`, f(d.stoch.d[i], 1));
  }
}

function renderPriceLines() {
  if (!S.candles) return;
  const a = S.account, lines = [];
  const pos = a && a.positions.find(p => p.symbol === S.symbol);
  if (pos) {
    lines.push({price: pos.avg_price, color: cssVar("--text-2"), title: `${pos.side === "BUY" ? "C" : "V"} ${num(pos.qty)} médio`, lineStyle: 0});
    if (pos.sl != null) lines.push({price: pos.sl, color: cssVar("--critical"), lineStyle: 2,
      title: pos.sl_role === "TRAILING" ? "Trailing" : pos.sl_role === "BREAKEVEN" ? "Breakeven" : "Stop"});
    if (pos.tp != null) lines.push({price: pos.tp, color: cssVar("--good"), title: "Alvo", lineStyle: 2});
  }
  (a ? a.orders : []).filter(o => o.symbol === S.symbol && !o.reduce_only).forEach(o =>
    lines.push({price: o.limit_price != null ? o.limit_price : o.stop_price, color: cssVar("--muted"), lineStyle: 1,
      title: `${o.kind === "LIMIT" ? "Lim" : "Stop"} ${o.side === "BUY" ? "C" : "V"} ${num(o.qty)}`}));
  const key = JSON.stringify(lines);
  if (key === S.linesKey) return;
  S.linesKey = key;
  S.lines.forEach(l => S.candles.removePriceLine(l));
  S.lines = lines.map(l => S.candles.createPriceLine({...l, lineWidth: 1, axisLabelVisible: true}));
}
function renderBook() {
  const q = S.quotes[S.symbol], inst = S.instruments[S.symbol];
  if (!q || !inst) return;
  $("#b-bid").textContent = price(q.bid, S.symbol);
  $("#b-ask").textContent = price(q.ask, S.symbol);
  $("#b-bid-size").textContent = num(q.bid_size, 0);
  $("#b-ask-size").textContent = num(q.ask_size, 0);
  const ticks = Math.round((q.ask - q.bid) / inst.tick_size);
  $("#b-spread").textContent = `Spread ${ticks} tick${ticks === 1 ? "" : "s"} · tick ${inst.tick_size} = ${money(inst.tick_value, inst.quote_ccy)}/un.`;
}

// ── boleta ──
function setupTicket() {
  $("#t-kind").onchange = () => onTicketChange(false);
  $$("#ticket input").forEach(i => { i.oninput = () => { renderTicketRisk(); renderTicketHint(); }; });
  $("#t-buy").onclick = () => placeOrder("BUY");
  $("#t-sell").onclick = () => placeOrder("SELL");
}
function onTicketChange(symbolChanged) {
  const inst = S.instruments[S.symbol], kind = $("#t-kind").value, q = S.quotes[S.symbol];
  $("#t-qty").step = inst.lot_step;
  $("#t-qty").min = inst.lot_step;
  if (symbolChanged || !$("#t-qty").value) $("#t-qty").value = inst.lot_step >= 1 ? inst.lot_step : +(inst.lot_step * 10).toFixed(2);
  $("#t-price-field").classList.toggle("hidden", kind === "MARKET");
  $("#t-price").step = inst.tick_size;
  if (kind !== "MARKET" && q) {
    const mid = (q.bid + q.ask) / 2;
    $("#t-price").value = (Math.round(mid / inst.tick_size) * inst.tick_size).toFixed(inst.decimals);
  }
  renderTicketRisk();
  renderTicketHint();
}
// Dica de lote: separada do bloco de risco (que muda a cada tick) para o link não perder o clique.
function renderTicketHint() {
  const inst = S.instruments[S.symbol], qty = +$("#t-qty").value || 0, frac = S.symbol + "F";
  let html = "";
  if (inst.lot_step === 100 && qty % 100 !== 0 && S.instruments[frac]) {
    html = `<span class="neg">Lote padrão de 100.</span> Para 1 a 99 ações use <a href="#" data-frac="${frac}">${frac}</a> (fracionário).`;
  } else if (inst.fractional) {
    html = `<span class="muted">Mercado fracionário: lotes de 100 têm mais liquidez em <a href="#" data-frac="${inst.standard}">${inst.standard}</a>.</span>`;
  } else if (inst.category === "OPCAO") {
    html = `<span class="muted">Opção de ${inst.underlying}: strike ${num(inst.strike)}, vencimento ${fmtDate(inst.expiry)}, lote 100.</span>`;
  }
  if (html === S.hintHtml) return;
  S.hintHtml = html;
  $("#t-hint").innerHTML = html;
  const link = $("[data-frac]", $("#t-hint"));
  if (link) link.onclick = e => { e.preventDefault(); selectSymbol(link.dataset.frac); };
}
function intOrNull(sel) {
  const v = $(sel).value.trim();
  return v ? parseInt(v, 10) : null;
}
function renderTicketRisk() {
  const inst = S.instruments[S.symbol], a = S.account;
  if (!inst || !a) return;
  const qty = +$("#t-qty").value || 0, sl = intOrNull("#t-sl");
  const margin = inst.margin_per_unit * qty;
  let html = `Margem estimada: ${money(margin, inst.quote_ccy)}`;
  if (sl) {
    const risk = (sl * inst.tick_value + 2 * inst.commission_per_unit) * qty;
    html += ` · Risco no stop: <b>${money(risk, inst.quote_ccy)}</b>`;
    if (a.plan !== "PRACTICE" && inst.quote_ccy === a.base_ccy) {
      const room = a.equity - a.daily_floor, share = room > 0 ? risk / room : 1;
      html += ` = <b class="${share >= 1 ? "neg" : ""}">${pct(share, 0)}</b> da folga diária${share >= 1 ? " — será rejeitada" : ""}`;
    }
  } else if (a.plan !== "PRACTICE") {
    html += ` · <span class="neg">sem stop: risco não limitado</span>`;
  }
  $("#t-risk").innerHTML = html;
  const locked = a.status !== "ACTIVE";
  for (const btn of [$("#t-buy"), $("#t-sell")]) {
    btn.disabled = locked;
    btn.title = locked ? `Conta ${STATUS[a.status] || a.status}: novas ordens bloqueadas` : "";
  }
}
async function placeOrder(side) {
  if (!S.accountId) { toast("Nenhuma conta selecionada.", "critical"); return; }
  const kind = $("#t-kind").value;
  const body = {symbol: S.symbol, side, kind, qty: $("#t-qty").value};
  if (kind === "LIMIT") body.limit_price = $("#t-price").value;
  if (kind === "STOP") body.stop_price = $("#t-price").value;
  const bracket = {
    stop_loss_ticks: intOrNull("#t-sl"), take_profit_ticks: intOrNull("#t-tp"), trailing_ticks: intOrNull("#t-trail"),
    trailing_activation_ticks: intOrNull("#t-trail-act") || 0, breakeven_trigger_ticks: intOrNull("#t-be"),
    breakeven_offset_ticks: intOrNull("#t-be-off") || 0,
  };
  if (bracket.stop_loss_ticks || bracket.take_profit_ticks || bracket.trailing_ticks) body.bracket = bracket;
  try {
    const res = await api(`/api/accounts/${S.accountId}/orders`, {method: "POST", body});
    if (res.order.status === "REJECTED") toast(`Ordem rejeitada: ${res.order.reject_reason}`, "critical");
    else if (res.order.status === "WORKING") toast(`Ordem ${kind === "LIMIT" ? "limite" : "stop"} registrada.`);
    S.account = res.account;
    renderAccount();
  } catch (e) { toast(e.message, "critical"); }
}
async function closePosition(symbol) {
  try { S.account = await api(`/api/accounts/${S.accountId}/positions/${symbol}/close`, {method: "POST"}); renderAccount(); }
  catch (e) { toast(e.message, "critical"); }
}
async function cancelOrder(id) {
  try { await api(`/api/accounts/${S.accountId}/orders/${id}`, {method: "DELETE"}); }
  catch (e) { toast(e.message, "critical"); }
}

// ── conta, risco e tabelas ──
function renderAccount() {
  const a = S.account;
  if (!a) { $("#riskbar").innerHTML = ""; return; }
  const ccy = a.base_ccy, rules = a.plan !== "PRACTICE", used = a.daily_loss_used, lim = a.limits;
  $("#riskbar").innerHTML = [
    kv("Saldo", money(a.balance, ccy)),
    kv("Equity", `<span class="${signCls(a.equity - a.balance)}">${money(a.equity, ccy)}</span>`),
    kv("Margem livre", money(a.free_margin, ccy)),
    kv("Perda diária usada", rules
      ? `${pct(used, 0)} de ${money(lim.daily_loss_limit, ccy)}<div class="meter ${used >= .8 ? "crit" : used >= .5 ? "warn" : ""}"><i style="width:${Math.min(100, used * 100)}%"></i></div>`
      : "sem regra"),
    kv(lim.trailing_drawdown ? "Folga até o DD (trailing)" : "Folga até o max DD", rules ? money(a.equity - a.total_floor, ccy) : "sem regra"),
    kv("Meta do desafio", lim.profit_target != null
      ? `${money(a.balance - a.initial_balance, ccy)} / ${money(lim.profit_target, ccy)} · ${a.trading_days}/${lim.min_trading_days} dias` : "—"),
    kv("Reset diário em", clock(a.next_reset - nowS())),
  ].join("");
  renderBanners(); renderPositions(); renderOrders(); renderPriceLines();
}
function renderBanners() {
  const a = S.account, ev = S.nextEvent, out = [];
  if (a && a.status === "DAILY_LOCKED") out.push(`<div class="banner critical" role="alert">⛔ Limite de perda diária atingido. Operações bloqueadas até o reset (${clock(a.next_reset - nowS())}).</div>`);
  if (a && a.status === "BREACHED") out.push(`<div class="banner critical" role="alert">⛔ Max drawdown violado: desafio reprovado. Resete a conta na aba Conta.</div>`);
  if (a && a.status === "PASSED") out.push(`<div class="banner good" role="status">✅ Meta atingida: desafio aprovado!</div>`);
  if (ev) {
    const left = ev.scheduled_at - nowS();
    if (left <= 120 && left > -2) out.push(`<div class="banner warning" role="status">⚠ Notícia de alto impacto: ${esc(ev.title)} em ${clock(left)}. Spread e slippage vão subir em ${ev.symbols.join(", ")}.${a && a.limits.news_blackout ? " Sua mesa bloqueia novas posições nesta janela." : ""}</div>`);
  }
  const html = out.join("");
  if (html !== S.bannerHtml) { $("#banners").innerHTML = html; S.bannerHtml = html; }
}
function setCell(row, name, text, cls) {
  const el = $(`[data-c="${name}"]`, row);
  el.textContent = text;
  if (cls !== undefined) el.className = "num " + cls;
}
// A estrutura da tabela só é recriada quando as linhas mudam; nos ticks só o texto muda (cliques não se perdem).
function renderPositions() {
  const list = (S.account && S.account.positions) || [], box = $("#positions");
  const key = list.map(p => p.symbol + p.side).join("|");
  if (box.dataset.key !== key || !box.innerHTML) {
    box.dataset.key = key;
    box.innerHTML = list.length ? `<table><thead><tr><th>Ativo</th><th>Lado</th><th class="num">Qtd</th><th class="num">Preço médio</th>
      <th class="num">Stop</th><th class="num">Alvo</th><th class="num">Resultado</th><th></th></tr></thead><tbody>${list.map(p => `
      <tr data-sym="${p.symbol}"><td><b>${p.symbol}</b></td><td>${p.side === "BUY" ? "Compra" : "Venda"}</td>
      <td class="num" data-c="qty"></td><td class="num" data-c="avg"></td><td class="num" data-c="sl"></td>
      <td class="num" data-c="tp"></td><td class="num" data-c="upnl"></td>
      <td><button class="btn sm" data-close="${p.symbol}">Zerar</button></td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">Sem posições abertas.</div>`;
    $$("[data-close]", box).forEach(b => { b.onclick = () => closePosition(b.dataset.close); });
  }
  for (const p of list) {
    const row = $(`tr[data-sym="${p.symbol}"]`, box);
    if (!row) continue;
    setCell(row, "qty", num(p.qty));
    setCell(row, "avg", price(p.avg_price, p.symbol));
    setCell(row, "sl", p.sl == null ? "—" : `${price(p.sl, p.symbol)}${p.sl_role === "TRAILING" ? " (trailing)" : p.sl_role === "BREAKEVEN" ? " (BE)" : ""}`);
    setCell(row, "tp", price(p.tp, p.symbol));
    setCell(row, "upnl", money(p.upnl), signCls(p.upnl));
  }
}
function renderOrders() {
  const list = ((S.account && S.account.orders) || []).filter(o => !o.reduce_only), box = $("#orders");
  const key = list.map(o => o.id).join("|");
  if (box.dataset.key === key && box.innerHTML) return;
  box.dataset.key = key;
  box.innerHTML = list.length ? `<table><thead><tr><th>Ativo</th><th>Tipo</th><th>Lado</th><th class="num">Qtd</th>
    <th class="num">Preço</th><th>OCO</th><th></th></tr></thead><tbody>${list.map(o => `
    <tr><td><b>${o.symbol}</b></td><td>${o.kind === "LIMIT" ? "Limite" : "Stop"}</td><td>${o.side === "BUY" ? "Compra" : "Venda"}</td>
    <td class="num">${num(o.qty)}</td><td class="num">${price(o.limit_price != null ? o.limit_price : o.stop_price, o.symbol)}</td>
    <td class="muted small">SL/TP na execução</td><td><button class="btn sm" data-cancel="${o.id}">Cancelar</button></td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">Sem ordens pendentes. SL/TP das posições aparecem na tabela acima e no gráfico.</div>`;
  $$("[data-cancel]", box).forEach(b => { b.onclick = () => cancelOrder(b.dataset.cancel); });
}

// ── navegação ──
function setupNav() {
  $$("#nav .tab").forEach(b => { b.onclick = () => showView(b.dataset.view); });
  setInterval(() => {
    if (S.view === "ranking") loadRanking();
    if (S.view === "calendar") loadCalendar();
  }, 3000);
}
function showView(v) {
  S.view = v;
  $$("#nav .tab").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach(el => el.classList.toggle("hidden", el.id !== `view-${v}`));
  if (v === "analytics") loadAnalytics();
  if (v === "ranking") loadRanking();
  if (v === "calendar") loadCalendar();
  if (v === "account") renderAccountsTable();
  if (v === "options" && !S.optionsRan) { S.optionsRan = true; runOptions(); }
}

// ── curvas de capital (série única: sem legenda, o título do card a nomeia) ──
const lineCharts = {};
function lineChart(el, points, key) {
  let c = lineCharts[key];
  if (!c) {
    const chart = LightweightCharts.createChart(el, {...chartTheme({timeVisible: true, secondsVisible: false}),
      autoSize: true});
    const series = chart.addLineSeries({color: cssVar("--series-1"), lineWidth: 2, priceLineVisible: false});
    c = lineCharts[key] = {chart, series};
  }
  const data = [];
  for (const [t, v] of points) {
    const time = Math.floor(t) + TZ_SHIFT;
    if (data.length && time <= data[data.length - 1].time) data[data.length - 1].value = v;
    else data.push({time, value: v});
  }
  c.series.setData(data);
  c.chart.timeScale().fitContent();
}
function metricsKpis(m, ccy) {
  return [
    kpi("Resultado líquido", money(m.net_pnl, ccy), signCls(m.net_pnl)),
    kpi("Profit Factor", m.profit_factor == null ? (m.gross_profit > 0 ? "∞" : "—") : num(m.profit_factor)),
    kpi("Payoff", m.payoff == null ? "—" : num(m.payoff)),
    kpi("Taxa de acerto", m.trades ? pct(m.win_rate) : "—"),
    kpi("Expectativa / trade", money(m.expectancy, ccy), signCls(m.expectancy)),
    kpi("Max drawdown", `${money(m.max_drawdown, ccy)} <span class="muted small">${pct(m.max_drawdown_pct, 2)}</span>`),
    kpi("Sortino (por trade)", m.sortino == null ? "—" : num(m.sortino)),
    kpi("Consistência", m.consistency == null ? "—" : pct(m.consistency, 0)),
    kpi("Trades", m.trades),
    kpi("Dias operados", m.trading_days),
  ].join("");
}

// ── journal ──
async function loadAnalytics() {
  if (!S.accountId) return;
  try {
    const d = await api(`/api/accounts/${S.accountId}/analytics`);
    $("#an-kpis").innerHTML = metricsKpis(d.metrics, d.base_ccy);
    lineChart($("#an-equity"), d.equity_curve, "an");
    $("#an-trades").innerHTML = d.trades.length ? `<table><thead><tr><th>Fechamento</th><th>Ativo</th><th>Lado</th>
      <th class="num">Qtd</th><th class="num">Entrada</th><th class="num">Saída</th><th>Saída por</th><th class="num">Custos</th>
      <th class="num">Resultado</th></tr></thead><tbody>${d.trades.map(t => `<tr>
      <td>${new Date(t.closed_at * 1000).toLocaleTimeString("pt-BR")}</td><td><b>${t.symbol}</b></td>
      <td>${t.side === "BUY" ? "Compra" : "Venda"}</td><td class="num">${num(t.qty)}</td>
      <td class="num">${price(t.entry, t.symbol)}</td><td class="num">${price(t.exit, t.symbol)}</td><td>${roleName(t.reason)}</td>
      <td class="num">${money(t.fees, d.base_ccy)}</td><td class="num ${signCls(t.net_pnl)}">${money(t.net_pnl, d.base_ccy)}</td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">Nenhum trade encerrado ainda nesta conta.</div>`;
  } catch (e) { toast(e.message, "critical"); }
}

// ── ranking ──
async function loadRanking() {
  try {
    const d = await api("/api/leaderboard");
    $("#rk-title").textContent = `Ranking · ${d.season} (elegível: ≥ ${d.rules.min_trades} trades e ≥ ${d.rules.min_days} dia)`;
    $("#rk-table").innerHTML = d.rows.length ? `<table><thead><tr><th class="num">#</th><th>Trader</th><th>Status</th>
      <th class="num">Score</th><th class="num">Retorno</th><th class="num">Max DD</th><th class="num">PF</th>
      <th class="num">Acerto</th><th class="num">Sortino</th><th class="num">Consist.</th><th class="num">Trades</th>
      <th class="num">Resets</th><th>Elegível</th></tr></thead><tbody>${d.rows.map(r => `<tr>
      <td class="num">${r.rank}</td><td><b>${esc(r.alias)}</b> ${r.bot ? '<span class="tag">EA</span>' : ""}</td>
      <td>${STATUS[r.status] || r.status}</td><td class="num"><b>${num(r.score)}</b></td>
      <td class="num ${signCls(r.return_pct)}">${pct(r.return_pct, 2)}</td><td class="num">${pct(r.max_drawdown_pct, 2)}</td>
      <td class="num">${r.profit_factor == null ? "—" : num(r.profit_factor)}</td><td class="num">${r.trades ? pct(r.win_rate, 0) : "—"}</td>
      <td class="num">${r.sortino == null ? "—" : num(r.sortino)}</td><td class="num">${r.consistency == null ? "—" : pct(r.consistency, 0)}</td>
      <td class="num">${r.trades}</td><td class="num">${r.resets}</td><td>${r.eligible ? "✔ sim" : "— não"}</td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">Ainda sem contas de desafio.</div>`;
  } catch (e) { /* ranking é informativo: ignora falha transitória */ }
}

// ── calendário ──
async function loadCalendar() {
  try {
    const events = await api("/api/calendar");
    $("#cal-table").innerHTML = events.length ? `<table><thead><tr><th>Horário</th><th>País</th><th>Evento</th><th>Impacto</th>
      <th class="num">Anterior</th><th class="num">Consenso</th><th class="num">Atual</th><th class="num">Surpresa</th><th>Ativos</th>
      <th>Status</th></tr></thead><tbody>${events.map(e => `<tr>
      <td>${new Date(e.scheduled_at * 1000).toLocaleTimeString("pt-BR")}</td><td>${e.country}</td><td><b>${esc(e.title)}</b></td>
      <td><span class="tag ${e.impact === "HIGH" ? "warning" : ""}">${e.impact === "HIGH" ? "⚠ alto" : e.impact}</span></td>
      <td class="num">${num(e.previous)}</td><td class="num">${num(e.forecast)}</td><td class="num">${e.actual == null ? "—" : num(e.actual)}</td>
      <td class="num">${e.surprise_z == null ? "—" : num(e.surprise_z) + "σ"}</td><td class="small">${e.symbols.join(", ")}</td>
      <td>${e.released ? "divulgado" : "em " + clock(e.scheduled_at - nowS())}</td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">Sem eventos agendados.</div>`;
  } catch (e) { /* idem */ }
}
$("#cal-trigger").onclick = async () => {
  try { const ev = await api("/api/calendar/trigger", {method: "POST"}); toast(`📰 ${ev.title} agendado para daqui a 15 s.`, "warning"); loadCalendar(); }
  catch (e) { toast(e.message, "critical"); }
};

// ── mesa de opções ──
async function setupOptions() {
  const templates = await api("/api/options/templates");
  $("#op-template").innerHTML = Object.entries(templates).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#op-template").value = "iron_condor";
  $("#op-run").onclick = runOptions;
}
async function runOptions() {
  const q = S.quotes.PETR4;
  if (!$("#op-spot").value && q) $("#op-spot").value = ((q.bid + q.ask) / 2).toFixed(2);
  const body = {template: $("#op-template").value, spot: +$("#op-spot").value, vol: +$("#op-vol").value / 100,
    days: +$("#op-days").value, rate: +$("#op-rate").value / 100, width_pct: +$("#op-width").value / 100,
    multiplier: +$("#op-mult").value};
  try {
    const r = await api("/api/options/analyze", {method: "POST", body});
    const g = r.greeks, debit = r.net_premium >= 0;
    $("#op-kpis").innerHTML = [
      kpi(debit ? "Prêmio pago (débito)" : "Prêmio recebido (crédito)", money(Math.abs(r.net_premium), "BRL")),
      kpi("Lucro máximo", r.max_profit == null ? "ilimitado" : money(r.max_profit, "BRL")),
      kpi("Perda máxima", r.max_loss == null ? "ilimitada" : money(r.max_loss, "BRL"), "neg"),
      kpi("Breakeven(s)", r.breakevens.length ? r.breakevens.map(b => num(b)).join(" · ") : "—"),
      kpi("Delta", num(g.delta)), kpi("Gamma", num(g.gamma, 4)), kpi("Theta / dia", money(g.theta, "BRL")),
      kpi("Vega / 1 p.p.", money(g.vega, "BRL")),
    ].join("");
    $("#op-legs").innerHTML = `<table><thead><tr><th>Perna</th><th class="num">Strike</th><th class="num">Qtd</th>
      <th class="num">Prêmio</th></tr></thead><tbody>${r.legs.map(l => `<tr>
      <td>${l.side > 0 ? "Compra" : "Venda"} ${l.kind === "C" ? "call" : l.kind === "P" ? "put" : "ação"}</td>
      <td class="num">${l.kind === "S" ? "—" : num(l.strike)}</td><td class="num">${l.qty}</td><td class="num">${num(l.premium, 4)}</td></tr>`).join("")}</tbody></table>`;
    payoffPlot($("#op-chart"), r, body.spot);
  } catch (e) { toast(e.message, "critical"); }
}
function niceTicks(lo, hi, n) {
  const span = hi - lo, mag = Math.pow(10, Math.floor(Math.log10(span / n)));
  const step = [1, 2, 2.5, 5, 10].map(s => s * mag).find(s => span / s <= n) || 10 * mag, out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}
// Duas séries num só eixo: legenda acima + rótulos diretos; traço contínuo vs tracejado (não só cor).
function payoffPlot(el, r, spot) {
  const W = Math.max(360, el.clientWidth), H = 320, m = {l: 76, r: 104, t: 14, b: 34};
  const xs = r.grid, a = r.payoff_expiry, b = r.payoff_today;
  const lo = Math.min(...a, ...b, 0), hi = Math.max(...a, ...b, 0), pad = (hi - lo) * 0.08 || 1;
  const y0 = lo - pad, y1 = hi + pad, x0 = xs[0], x1 = xs[xs.length - 1];
  const X = v => m.l + (v - x0) / (x1 - x0) * (W - m.l - m.r);
  const Y = v => m.t + (y1 - v) / (y1 - y0) * (H - m.t - m.b);
  const path = ys => ys.map((y, i) => `${i ? "L" : "M"}${X(xs[i]).toFixed(1)},${Y(y).toFixed(1)}`).join("");
  const fmt = v => v.toLocaleString("pt-BR", {maximumFractionDigits: 2});
  let la = Y(a[a.length - 1]) + 4, lb = Y(b[b.length - 1]) + 4;
  if (Math.abs(la - lb) < 14) lb = la + (lb >= la ? 14 : -14);
  el.innerHTML = `<svg width="${W}" height="${H}" role="img" aria-label="Payoff da estrutura no vencimento e hoje">
    ${niceTicks(y0, y1, 5).map(v => `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}" style="stroke:var(--grid)"/>
      <text x="${m.l - 8}" y="${Y(v) + 4}" text-anchor="end" style="fill:var(--muted);font-size:11px">${fmt(v)}</text>`).join("")}
    ${niceTicks(x0, x1, 6).map(v => `<text x="${X(v)}" y="${H - 12}" text-anchor="middle" style="fill:var(--muted);font-size:11px">${fmt(v)}</text>`).join("")}
    <line x1="${m.l}" x2="${W - m.r}" y1="${Y(0)}" y2="${Y(0)}" style="stroke:var(--text-2)"/>
    <line x1="${X(spot)}" x2="${X(spot)}" y1="${m.t}" y2="${H - m.b}" style="stroke:var(--text-2);stroke-dasharray:3 3"/>
    <text x="${X(spot) + 4}" y="${m.t + 10}" style="fill:var(--muted);font-size:11px">spot</text>
    <path d="${path(b)}" style="fill:none;stroke:var(--series-2);stroke-width:2;stroke-dasharray:6 4"/>
    <path d="${path(a)}" style="fill:none;stroke:var(--series-1);stroke-width:2"/>
    <text x="${W - m.r + 8}" y="${la}" style="fill:var(--text-2);font-size:11px">Vencimento</text>
    <text x="${W - m.r + 8}" y="${lb}" style="fill:var(--text-2);font-size:11px">Hoje (T+0)</text>
    <g class="hover" visibility="hidden">
      <line class="hl" y1="${m.t}" y2="${H - m.b}" style="stroke:var(--muted)"/>
      <circle class="d1" r="5" style="fill:var(--series-1);stroke:var(--panel);stroke-width:2"/>
      <circle class="d2" r="5" style="fill:var(--series-2);stroke:var(--panel);stroke-width:2"/>
    </g>
    <rect class="hit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" style="fill:transparent"/>
  </svg><div class="tooltip hidden"></div>`;
  const svg = $("svg", el), hover = $(".hover", el), tip = $(".tooltip", el);
  $(".hit", el).onmousemove = e => {
    const px = e.clientX - svg.getBoundingClientRect().left;
    const i = Math.max(0, Math.min(xs.length - 1, Math.round((px - m.l) / (W - m.l - m.r) * (xs.length - 1))));
    const x = X(xs[i]);
    hover.setAttribute("visibility", "visible");
    $(".hl", el).setAttribute("x1", x); $(".hl", el).setAttribute("x2", x);
    $(".d1", el).setAttribute("cx", x); $(".d1", el).setAttribute("cy", Y(a[i]));
    $(".d2", el).setAttribute("cx", x); $(".d2", el).setAttribute("cy", Y(b[i]));
    tip.innerHTML = `Preço do ativo: <b>${fmt(xs[i])}</b><br>No vencimento: <b>${money(a[i], "BRL")}</b><br>Hoje (T+0): <b>${money(b[i], "BRL")}</b>`;
    tip.classList.remove("hidden");
    tip.style.left = `${Math.min(x + 12, W - 190)}px`;
    tip.style.top = `${m.t + 8}px`;
  };
  $(".hit", el).onmouseleave = () => { hover.setAttribute("visibility", "hidden"); tip.classList.add("hidden"); };
}

// ── automação / backtest ──
function setupAutomation() {
  $("#bt-symbol").innerHTML = Object.values(S.instruments).filter(i => i.category !== "OPCAO" && !i.fractional)
    .map(i => `<option value="${i.symbol}">${i.symbol} · ${esc(i.description)}</option>`).join("");
  $("#bt-symbol").value = S.symbol;
  $("#bt-symbol").onchange = e => { const i = S.instruments[e.target.value]; $("#bt-qty").value = i.lot_step >= 1 ? i.lot_step : +(i.lot_step * 10).toFixed(2); };
  $("#bt-run").onclick = async () => {
    const btn = $("#bt-run");
    btn.disabled = true; btn.textContent = "Rodando…";
    const body = {symbol: $("#bt-symbol").value, qty: $("#bt-qty").value, fast: +$("#bt-fast").value,
      slow: +$("#bt-slow").value, stop_loss_ticks: intOrNull("#bt-sl"), take_profit_ticks: intOrNull("#bt-tp"),
      trailing_ticks: intOrNull("#bt-trail"), steps: +$("#bt-steps").value, seed: +$("#bt-seed").value};
    try {
      const r = await api("/api/strategies/backtest", {method: "POST", body});
      $("#bt-kpis").innerHTML = kpi("Saldo final", money(r.final_balance, r.base_ccy)) + metricsKpis(r.metrics, r.base_ccy);
      lineChart($("#bt-equity"), r.equity_curve, "bt");
    } catch (e) { toast(e.message, "critical"); }
    btn.disabled = false; btn.textContent = "Rodar backtest";
  };
}

// ── conta / LGPD ──
async function setupAccountView() {
  $("#me").innerHTML = `<p><b>${esc(S.me.name)}</b><br><span class="muted">${esc(S.me.email)}</span><br>
    <span class="small">Apelido público no ranking: <b>${esc(S.me.alias)}</b></span></p>`;
  const plans = await api("/api/plans");
  $("#new-plan").innerHTML = plans.map(p => `<option value="${p.id}">${esc(p.title)}</option>`).join("");
  $("#pref-cb").checked = document.documentElement.dataset.palette === "cb";
  $("#pref-cb").onchange = e => setPalette(e.target.checked);
  $("#pref-dark").checked = document.documentElement.dataset.theme === "dark";
  $("#pref-dark").onchange = e => setTheme(e.target.checked ? "dark" : "light");
  $("#logout").onclick = async () => { await api("/api/auth/logout", {method: "POST"}); location.href = "/"; };
  $("#new-acc").onclick = async () => {
    try {
      const acc = await api("/api/accounts", {method: "POST", body: {plan: $("#new-plan").value, base_ccy: $("#new-ccy").value}});
      await loadAccounts(acc.id);
      renderAccountsTable();
      toast(`Conta "${acc.name}" criada.`, "good");
    } catch (e) { toast(e.message, "critical"); }
  };
  $("#lgpd-export").onclick = async () => {
    const data = await api("/api/me/export");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json"}));
    link.download = "meus-dados-bolsasim.json";
    link.click();
  };
  $("#lgpd-delete").onclick = async () => {
    if (!confirm("Excluir definitivamente sua conta, contas simuladas, histórico e posição no ranking? Esta ação não pode ser desfeita.")) return;
    await api("/api/me", {method: "DELETE"});
    location.href = "/";
  };
}
function renderAccountsTable() {
  $("#acc-table").innerHTML = `<table><thead><tr><th>Conta</th><th>Moeda</th><th>Status</th><th class="num">Saldo</th>
    <th class="num">Resets</th><th></th></tr></thead><tbody>${S.accounts.map(a => `<tr>
    <td><b>${esc(a.name)}</b>${a.id === S.accountId ? ' <span class="tag">em uso</span>' : ""}</td><td>${a.base_ccy}</td>
    <td>${STATUS[a.status] || a.status}</td><td class="num">${money(a.balance, a.base_ccy)}</td><td class="num">${a.reset_count}</td>
    <td><button class="btn sm" data-use="${a.id}">Usar</button> <button class="btn sm danger" data-reset="${a.id}">Resetar</button></td>
    </tr>`).join("")}</tbody></table>`;
  $$("[data-use]").forEach(b => { b.onclick = () => { $("#acc-select").value = b.dataset.use; switchAccount(b.dataset.use); renderAccountsTable(); }; });
  $$("[data-reset]").forEach(b => {
    b.onclick = async () => {
      if (!confirm("Resetar esta conta? Posições, ordens e histórico serão apagados e o reset fica visível no ranking.")) return;
      try { await api(`/api/accounts/${b.dataset.reset}/reset`, {method: "POST"}); await loadAccounts(); renderAccountsTable(); }
      catch (e) { toast(e.message, "critical"); }
    };
  });
}

// ── tema ──
function applyTheme() {
  const dark = document.documentElement.dataset.theme === "dark";
  $("#theme-btn").textContent = dark ? "☀️" : "🌙";
  if ($("#pref-dark")) $("#pref-dark").checked = dark;
  if (S.chart) {
    S.chart.applyOptions(chartTheme());
    S.candles.applyOptions(candleColors());
    buildIndicators();  // recria séries e painéis com as cores do tema novo
    refreshIndicators(true);
    S.drawing.render();
  }
  Object.values(lineCharts).forEach(c => {
    c.chart.applyOptions(chartTheme());
    c.series.applyOptions({color: cssVar("--series-1")});
  });
  S.linesKey = "";
  renderPriceLines();
}
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("theme", theme);
  applyTheme();
}
function setPalette(colorblind) {
  if (colorblind) document.documentElement.dataset.palette = "cb";
  else delete document.documentElement.dataset.palette;
  localStorage.setItem("palette", colorblind ? "cb" : "classic");
  applyTheme();
}

// ── letreiro de cotações (favoritos) ──
function renderTape() {
  const items = S.fav.filter(s => S.quotes[s]).map(s => {
    const q = S.quotes[s], v = change(q);
    return `<span class="tape-item" data-sym="${s}" title="${esc(S.instruments[s].description)}"><b>${s}</b>
      <span class="mono">${price((q.bid + q.ask) / 2, s)}</span><span class="${signCls(v)}">${changeText(v)}</span></span>`;
  }).join("");
  if (!items) return;
  $("#tape").innerHTML = items + items;  // duplicado: rolagem contínua
  $$("#tape .tape-item").forEach(el => { el.onclick = () => { showView("trade"); selectSymbol(el.dataset.sym); }; });
}

// ── dicas de estudo ──
const TIPS = [
  "<b>Stop loss primeiro.</b> Defina quanto aceita perder antes de entrar; o tamanho da posição vem depois.",
  "<b>Payoff:</b> se o alvo é 2× o stop, você pode errar mais da metade das vezes e ainda lucrar.",
  "<b>Spread</b> é custo: em ativos ilíquidos ele come o lucro de operações curtas. Compare bid e ask antes.",
  "<b>Mini-índice (WIN):</b> cada ponto vale R$ 0,20 por contrato; 100 pontos = R$ 20.",
  "<b>Mini-dólar (WDO):</b> cada ponto vale R$ 10 por contrato; meio ponto é o menor tick.",
  "<b>Lote padrão:</b> ações na B3 são negociadas de 100 em 100. Para menos, use o fracionário (ticker com F).",
  "<b>FIIs</b> distribuem rendimentos mensais; o preço oscila menos que ações, mas também sofre com juros altos.",
  "<b>DI1</b> é negociado em taxa: comprar DI = apostar que os juros sobem.",
  "<b>Opções:</b> o comprador arrisca só o prêmio; o vendedor recebe o prêmio e assume o risco. Theta corre contra quem compra.",
  "<b>Notícias de alto impacto</b> (Payroll, Copom) alargam o spread e geram slippage. Muitos traders ficam de fora.",
  "<b>Trailing stop</b> deixa o lucro correr sem devolver tudo quando o mercado vira.",
  "<b>Breakeven</b> tira o risco do trade, mas se for movido cedo demais o ruído te tira da operação.",
  "<b>Perda diária máxima</b> existe para impedir o “dia de revanche”. Bateu o limite? O dia acabou.",
  "<b>Profit Factor</b> acima de 1,5 com dezenas de trades é sinal de vantagem real, não de sorte.",
  "<b>Diversificar</b> entre classes (ações, FIIs, juros) reduz o risco de um único evento derrubar tudo.",
];
function setupTips() {
  let i = Math.floor(Date.now() / 86_400_000) % TIPS.length;  // a dica do dia muda a cada 24h
  const show = () => { $("#tip-text").innerHTML = TIPS[i]; };
  $("#tip-next").onclick = () => { i = (i + 1) % TIPS.length; show(); };
  show();
}

// ── gamificação: nível, XP e conquistas ──
async function loadProgress() {
  if (!S.accountId) return;
  const accountId = S.accountId;
  try {
    const p = await api(`/api/accounts/${accountId}/progress`);
    if (accountId !== S.accountId) return;
    const unlocked = new Set(p.achievements.filter(a => a.done).map(a => a.id));
    if (S.progressAccount === accountId && S.unlocked) {  // só celebra o que mudou nesta conta
      p.achievements.filter(a => a.done && !S.unlocked.has(a.id))
        .forEach(a => toast(`🏆 Conquista desbloqueada: ${a.icon} ${a.title} (+${a.xp} XP)`, "trophy"));
      if (p.level > S.level) toast(`⭐ Você subiu para o nível ${p.level}: ${p.title}!`, "trophy");
    }
    S.unlocked = unlocked;
    S.level = p.level;
    S.progressAccount = accountId;
    renderProgress(p);
  } catch (e) { /* informativo: ignora falha transitória */ }
}
function renderProgress(p) {
  const width = `${Math.round(p.progress * 100)}%`, maxed = p.next_level_xp <= p.level_floor;
  $("#lvl-n").textContent = p.level;
  $("#lvl-title").textContent = p.title;
  $("#lvl-xp").textContent = maxed ? `${p.xp} XP · nível máximo` : `${p.xp} / ${p.next_level_xp} XP`;
  $("#lvl-bar").style.width = width;
  $("#jr-title").textContent = `Nível ${p.level} · ${p.title}`;
  $("#jr-xp").textContent = `${p.xp} XP · ${p.unlocked}/${p.achievements.length} conquistas` +
    (maxed ? "" : ` · faltam ${p.next_level_xp - p.xp} XP para o próximo nível`);
  $("#jr-bar").style.width = width;
  $("#jr-ach").innerHTML = p.achievements.map(a => `<div class="ach ${a.done ? "done" : ""}">
    <span class="ic" aria-hidden="true">${a.icon}</span>
    <div><b>${esc(a.title)}${a.done ? " ✓" : ""}</b><span>${esc(a.description)} · +${a.xp} XP</span></div></div>`).join("");
}

init().catch(e => { if (e.message !== "Sessão expirada.") toast(e.message, "critical"); });
