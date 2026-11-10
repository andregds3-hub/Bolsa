// Testes dos indicadores: node --test tests/js
const test = require("node:test");
const assert = require("node:assert/strict");
const I = require("../../web/indicators.js");

const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);
const bar = (time, close, extra = {}) => ({time, open: close, high: close, low: close, close, volume: 1, ...extra});

test("médias simples e exponencial", () => {
  assert.deepEqual(I.sma([1, 2, 3, 4, 5], 3), [null, null, 2, 3, 4]);
  const e = I.ema([1, 2, 3, 4, 5], 3);  // k = 0,5; semente = 2
  assert.deepEqual(e.slice(0, 2), [null, null]);
  close(e[2], 2); close(e[3], 3); close(e[4], 4);
});

test("médias ponderada, suavizada, Hull, dupla e tripla", () => {
  close(I.wma([1, 2, 3], 3)[2], 14 / 6);
  const s = I.smma([1, 2, 3, 4], 2);  // semente 1,5; depois (1,5 + 3)/2 = 2,25; (2,25 + 4)/2 = 3,125
  close(s[1], 1.5); close(s[2], 2.25); close(s[3], 3.125);
  const flat = Array(80).fill(42);
  for (const type of ["SMA", "EMA", "WMA", "SMMA", "HMA", "DEMA", "TEMA"]) {
    close(I.movingAverage(type, flat, 20)[79], 42);  // média de série constante é a própria constante
  }
  assert.equal(I.tema(flat, 20)[55], null);  // 3 × (20 − 1) candles de aquecimento
  close(I.tema(flat, 20)[57], 42);
  assert.throws(() => I.movingAverage("XYZ", flat, 5));
});

test("fonte de preço", () => {
  const b = [{open: 1, high: 4, low: 2, close: 3}];
  assert.deepEqual(["close", "open", "hl2", "hlc3", "ohlc4"].map(k => I.source(b, k)[0]), [3, 1, 3, 3, 2.5]);
});

test("Bollinger: série constante tem bandas coladas na média", () => {
  const b = I.bollinger(Array(25).fill(10), 20, 2);
  assert.equal(b.mid[18], null);
  close(b.mid[24], 10); close(b.upper[24], 10); close(b.lower[24], 10);
  const w = I.bollinger([1, 3], 2, 2);  // média 2, desvio 1
  close(w.upper[1], 4); close(w.lower[1], 0);
});

test("IFR: só alta = 100, só baixa = 0, lateral = 50", () => {
  const up = Array.from({length: 20}, (_, i) => i);
  close(I.rsi(up, 14)[19], 100);
  close(I.rsi(up.slice().reverse(), 14)[19], 0);
  close(I.rsi(Array(20).fill(5), 14)[19], 50);
  assert.equal(I.rsi(up, 14)[13], null);
});

test("MACD de série constante é zero e o sinal começa depois da linha", () => {
  const m = I.macd(Array(60).fill(7), 12, 26, 9);
  assert.equal(m.macd[24], null);
  close(m.macd[25], 0);
  assert.equal(m.signal[32], null);
  close(m.signal[33], 0); close(m.hist[59], 0);
});

test("Estocástico: fechamento na máxima do período = 100", () => {
  const bars = Array.from({length: 20}, (_, i) => bar(i, i, {high: i, low: i - 1}));
  const s = I.stochastic(bars, 14, 3);
  close(s.k[19], 100); close(s.d[19], 100);
  assert.equal(s.k[12], null);
});

test("VWAP pondera pelo volume e reinicia a cada dia", () => {
  const day = 86400;
  const v = I.vwap([bar(0, 10, {volume: 1}), bar(60, 20, {volume: 3}), bar(day, 50, {volume: 2})]);
  close(v[0], 10); close(v[1], 17.5); close(v[2], 50);
});
