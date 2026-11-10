/*
 * Indicadores técnicos: funções puras, sem dependência de tela.
 * Entradas: arrays de números ou candles {time, open, high, low, close, volume}.
 * Saídas alinhadas aos candles, com null enquanto não há dados suficientes (aquecimento).
 * Funciona no navegador (window.Indicators) e no Node (require) para os testes.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Indicators = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const blank = n => new Array(n).fill(null);

  function sma(values, period) {
    const out = blank(values.length);
    let sum = 0;
    for (let i = 0; i < values.length; i++) {
      sum += values[i];
      if (i >= period) sum -= values[i - period];
      if (i >= period - 1) out[i] = sum / period;
    }
    return out;
  }

  // Média exponencial semeada pela média simples dos primeiros `period` valores.
  function ema(values, period) {
    const out = blank(values.length);
    if (values.length < period) return out;
    const k = 2 / (period + 1);
    let prev = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
    out[period - 1] = prev;
    for (let i = period; i < values.length; i++) {
      prev = values[i] * k + prev * (1 - k);
      out[i] = prev;
    }
    return out;
  }

  // Aplica uma função de média a partir do primeiro valor não nulo (ex.: sinal do MACD).
  function fromFirstValue(values, period, fn) {
    const out = blank(values.length), first = values.findIndex(v => v !== null);
    if (first < 0) return out;
    fn(values.slice(first), period).forEach((v, i) => { out[first + i] = v; });
    return out;
  }

  function bollinger(values, period = 20, mult = 2) {
    const mid = sma(values, period), upper = blank(values.length), lower = blank(values.length);
    for (let i = period - 1; i < values.length; i++) {
      let sq = 0;
      for (let j = i - period + 1; j <= i; j++) sq += (values[j] - mid[i]) ** 2;
      const sd = Math.sqrt(sq / period);  // desvio populacional, como na definição de Bollinger
      upper[i] = mid[i] + mult * sd;
      lower[i] = mid[i] - mult * sd;
    }
    return {mid, upper, lower};
  }

  // IFR de Wilder.
  function rsi(values, period = 14) {
    const out = blank(values.length);
    if (values.length <= period) return out;
    const value = (gain, loss) => gain === 0 && loss === 0 ? 50 : loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
    let gain = 0, loss = 0;
    for (let i = 1; i <= period; i++) {
      const d = values[i] - values[i - 1];
      if (d >= 0) gain += d; else loss -= d;
    }
    gain /= period;
    loss /= period;
    out[period] = value(gain, loss);
    for (let i = period + 1; i < values.length; i++) {
      const d = values[i] - values[i - 1];
      gain = (gain * (period - 1) + Math.max(d, 0)) / period;
      loss = (loss * (period - 1) + Math.max(-d, 0)) / period;
      out[i] = value(gain, loss);
    }
    return out;
  }

  function macd(values, fast = 12, slow = 26, signal = 9) {
    const f = ema(values, fast), s = ema(values, slow);
    const line = values.map((_, i) => f[i] !== null && s[i] !== null ? f[i] - s[i] : null);
    const sig = fromFirstValue(line, signal, ema);
    const hist = line.map((v, i) => v !== null && sig[i] !== null ? v - sig[i] : null);
    return {macd: line, signal: sig, hist};
  }

  function stochastic(bars, period = 14, smooth = 3) {
    const k = bars.map((b, i) => {
      if (i < period - 1) return null;
      let hi = -Infinity, lo = Infinity;
      for (let j = i - period + 1; j <= i; j++) {
        hi = Math.max(hi, bars[j].high);
        lo = Math.min(lo, bars[j].low);
      }
      return hi === lo ? 50 : (b.close - lo) / (hi - lo) * 100;
    });
    return {k, d: fromFirstValue(k, smooth, sma)};
  }

  // VWAP reiniciado a cada sessão (dia do candle, no fuso em que os tempos foram informados).
  function vwap(bars) {
    let pv = 0, vol = 0, day = null;
    return bars.map(b => {
      const d = new Date(b.time * 1000).toISOString().slice(0, 10);
      if (d !== day) { day = d; pv = 0; vol = 0; }
      const v = b.volume || 0;
      pv += (b.high + b.low + b.close) / 3 * v;
      vol += v;
      return vol > 0 ? pv / vol : null;
    });
  }

  // Média ponderada linear: pesos 1..n, o candle mais recente pesa n.
  function wma(values, period) {
    const out = blank(values.length), denom = period * (period + 1) / 2;
    for (let i = period - 1; i < values.length; i++) {
      let sum = 0;
      for (let j = 0; j < period; j++) sum += values[i - j] * (period - j);
      out[i] = sum / denom;
    }
    return out;
  }

  // Média suavizada de Wilder (SMMA/RMA): semente simples, depois (anterior × (n − 1) + valor) / n.
  function smma(values, period) {
    const out = blank(values.length);
    if (values.length < period) return out;
    let prev = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
    out[period - 1] = prev;
    for (let i = period; i < values.length; i++) {
      prev = (prev * (period - 1) + values[i]) / period;
      out[i] = prev;
    }
    return out;
  }

  // Hull: WMA(2 × WMA(n/2) − WMA(n), √n). Reage rápido com pouco ruído.
  function hma(values, period) {
    const half = wma(values, Math.max(1, Math.round(period / 2))), full = wma(values, period);
    const diff = values.map((_, i) => half[i] !== null && full[i] !== null ? 2 * half[i] - full[i] : null);
    return fromFirstValue(diff, Math.max(1, Math.round(Math.sqrt(period))), wma);
  }

  // Dupla e tripla exponencial: reduzem o atraso da MME.
  function dema(values, period) {
    const e1 = ema(values, period), e2 = fromFirstValue(e1, period, ema);
    return values.map((_, i) => e1[i] !== null && e2[i] !== null ? 2 * e1[i] - e2[i] : null);
  }
  function tema(values, period) {
    const e1 = ema(values, period), e2 = fromFirstValue(e1, period, ema), e3 = fromFirstValue(e2, period, ema);
    return values.map((_, i) => e3[i] !== null ? 3 * e1[i] - 3 * e2[i] + e3[i] : null);
  }

  const MOVING_AVERAGES = {SMA: sma, EMA: ema, WMA: wma, SMMA: smma, HMA: hma, DEMA: dema, TEMA: tema};
  function movingAverage(type, values, period) {
    const fn = MOVING_AVERAGES[type];
    if (!fn) throw new Error(`Tipo de média desconhecido: ${type}`);
    return fn(values, Math.max(1, Math.round(period)));
  }

  // Preço de entrada de um indicador: fechamento, abertura, máxima, mínima ou combinações.
  const SOURCES = {
    close: b => b.close, open: b => b.open, high: b => b.high, low: b => b.low,
    hl2: b => (b.high + b.low) / 2, hlc3: b => (b.high + b.low + b.close) / 3,
    ohlc4: b => (b.open + b.high + b.low + b.close) / 4,
  };
  function source(bars, kind = "close") {
    return bars.map(SOURCES[kind] || SOURCES.close);
  }

  return {sma, ema, wma, smma, hma, dema, tema, movingAverage, source, bollinger, rsi, macd, stochastic, vwap};
});
