/*
 * Ferramentas de desenho sobre o gráfico: tendência, horizontal, retângulo, Fibonacci e régua.
 * Camada SVG por cima do Lightweight Charts. Os desenhos guardam (tempo, preço), não pixels,
 * e são reprojetados a cada zoom, rolagem ou novo candle; por isso sobrevivem à troca de timeframe.
 *
 * Edição como no ProfitChart/TradingView: clicar seleciona e mostra as alças; arrastar o corpo move o
 * desenho inteiro; arrastar uma alça move só aquela ponta; Delete apaga; clicar no gráfico desmarca.
 * A camada só captura o mouse sobre os desenhos: no resto, o gráfico continua rolando e dando zoom.
 */
(function (root) {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  const TWO_POINTS = new Set(["trend", "rect", "fib", "ruler"]);
  const clone = item => JSON.parse(JSON.stringify(item));

  class DrawingLayer {
    /**
     * @param container elemento do gráfico principal
     * @param opts {chart, series, getBars, getTf, fmtPrice, onChange(items), onToolEnd(), onSelect(index|null)}
     */
    constructor(container, opts) {
      this.opts = opts;
      this.items = [];
      this.tool = null;
      this.pending = null;
      this.hover = null;
      this.selected = null;
      this.drag = null;
      this.svg = document.createElementNS(NS, "svg");
      this.svg.setAttribute("class", "draw-layer");
      container.appendChild(this.svg);
      this.svg.addEventListener("pointerdown", e => this.onDown(e));
      this.svg.addEventListener("pointermove", e => this.onMove(e));
      this.svg.addEventListener("pointerup", e => this.onUp(e));
      this.svg.addEventListener("pointercancel", e => this.onUp(e));
      this.svg.addEventListener("pointerleave", () => { if (this.tool && !this.drag) { this.hover = null; this.render(); } });
      document.addEventListener("keydown", e => this.onKey(e));
      // clique no gráfico (fora de qualquer desenho) desmarca a seleção
      opts.chart.subscribeClick(() => { if (!this.tool && !this.drag) this.select(null); });
      opts.chart.timeScale().subscribeVisibleLogicalRangeChange(() => this.render());
      new ResizeObserver(() => this.render()).observe(container);
    }

    // ── estado ──
    setTool(tool) {
      this.tool = tool;
      this.pending = null;
      this.selected = null;
      this.svg.classList.toggle("active", Boolean(tool));  // com ferramenta ativa a camada captura todos os cliques
      this.render();
      this.opts.onSelect(null);
      if (!tool) this.opts.onToolEnd();
    }
    setItems(items) {
      this.items = items || [];
      this.selected = null;
      this.opts.onSelect(null);
      this.render();
    }
    select(index) {
      if (this.selected === index) return;
      this.selected = index;
      this.render();
      this.opts.onSelect(index);
    }
    removeSelected() {
      if (this.selected === null) return;
      this.items.splice(this.selected, 1);
      this.selected = null;
      this.opts.onSelect(null);
      this.changed();
    }
    undo() { this.items.pop(); this.select(null); this.changed(); }
    clear() { this.items = []; this.select(null); this.changed(); }
    changed() { this.render(); this.opts.onChange(this.items); }

    // ── tempo ↔ posição lógica (índice de candle, fracionário; extrapola além dos dados) ──
    timeToLogical(t) {
      const bars = this.opts.getBars(), n = bars.length, tf = this.opts.getTf();
      if (!n) return null;
      if (t <= bars[0].time) return (t - bars[0].time) / tf;
      if (t >= bars[n - 1].time) return n - 1 + (t - bars[n - 1].time) / tf;
      let lo = 0, hi = n - 1;
      while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (bars[mid].time <= t) lo = mid; else hi = mid; }
      return lo + (t - bars[lo].time) / (bars[hi].time - bars[lo].time);
    }
    logicalToTime(l) {
      const bars = this.opts.getBars(), n = bars.length, tf = this.opts.getTf();
      if (!n) return null;
      if (l <= 0) return bars[0].time + l * tf;
      if (l >= n - 1) return bars[n - 1].time + (l - (n - 1)) * tf;
      const i = Math.floor(l);
      return bars[i].time + (l - i) * (bars[i + 1].time - bars[i].time);
    }
    x(t) {
      const l = this.timeToLogical(t);
      return l === null ? null : this.opts.chart.timeScale().logicalToCoordinate(l);
    }
    y(price) { return this.opts.series.priceToCoordinate(price); }
    // ponto do mouse em coordenadas do gráfico: posição lógica, tempo e preço
    pointFrom(e) {
      const r = this.svg.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
      const logical = this.opts.chart.timeScale().coordinateToLogical(px), price = this.opts.series.coordinateToPrice(py);
      return logical === null || price === null ? null : {logical, time: this.logicalToTime(logical), price};
    }

    // ── mouse ──
    onDown(e) {
      if (e.button !== 0) return;
      if (this.tool) return this.placePoint(e);
      const target = e.target.closest("[data-i]");
      const start = target && this.pointFrom(e);
      if (!start) return;
      const index = Number(target.dataset.i);
      e.preventDefault();
      e.stopPropagation();
      this.select(index);
      this.drag = {index, handle: target.dataset.h || null, start, original: clone(this.items[index]), moved: false};
      this.svg.setPointerCapture(e.pointerId);
      this.svg.classList.add("dragging");
    }
    onMove(e) {
      const p = this.pointFrom(e);
      if (this.drag) {
        if (!p) return;
        this.items[this.drag.index] = this.dragged(p);
        this.drag.moved = true;
        this.render();
      } else if (this.tool) {
        this.hover = p;
        this.render();
      }
    }
    onUp(e) {
      if (!this.drag) return;
      if (this.svg.hasPointerCapture(e.pointerId)) this.svg.releasePointerCapture(e.pointerId);
      this.svg.classList.remove("dragging");
      const moved = this.drag.moved;
      this.drag = null;
      if (moved) this.changed();  // só grava quando algo de fato mudou
    }
    onKey(e) {
      if (e.target.closest && e.target.closest("input, select, textarea")) return;
      if ((e.key === "Delete" || e.key === "Backspace") && this.selected !== null) {
        e.preventDefault();
        this.removeSelected();
      } else if (e.key === "Escape") {
        if (this.tool) this.setTool(null);
        else this.select(null);
      }
    }
    placePoint(e) {
      const p = this.pointFrom(e);
      if (!p) return;
      e.preventDefault();
      if (this.tool === "hline") {
        this.items.push({type: "hline", price: p.price});
      } else if (!this.pending) {
        this.pending = {time: p.time, price: p.price};
        return this.render();
      } else {
        this.items.push({type: this.tool, a: this.pending, b: {time: p.time, price: p.price}});
      }
      const created = this.items.length - 1;
      this.changed();
      this.setTool(null);  // como nas plataformas: um desenho por seleção da ferramenta
      this.select(created);  // e o desenho recém-criado já fica selecionado para ajuste
    }

    // Novo estado do item arrastado. O deslocamento no tempo é feito em candles (posição lógica), como na tela.
    dragged(p) {
      const {handle, start, original: o} = this.drag;
      const dl = p.logical - start.logical, dp = p.price - start.price;
      const shift = point => ({time: this.logicalToTime(this.timeToLogical(point.time) + dl), price: point.price + dp});
      const item = clone(o);
      if (o.type === "hline") {
        item.price = o.price + dp;
      } else if (!handle) {  // corpo: move o desenho inteiro
        item.a = shift(o.a);
        item.b = shift(o.b);
      } else if (handle === "a" || handle === "b") {
        item[handle] = shift(o[handle]);
      } else {  // cantos do retângulo/régua: "ab" = tempo de a + preço de b; "ba" = tempo de b + preço de a
        const timeOf = handle[0], priceOf = handle[1];
        item[timeOf].time = shift(o[timeOf]).time;
        item[priceOf].price = o[priceOf].price + dp;
      }
      return item;
    }

    // ── desenho ──
    render() {
      const w = this.svg.clientWidth, parts = [];
      this.items.forEach((item, i) => parts.push(this.group(item, i, w)));
      if (this.pending && this.hover && TWO_POINTS.has(this.tool)) {
        parts.push(`<g class="preview">${this.shape({type: this.tool, a: this.pending, b: this.hover}, w)}</g>`);
      }
      if (this.tool === "hline" && this.hover) {
        parts.push(`<g class="preview">${this.shape({type: "hline", price: this.hover.price}, w)}</g>`);
      }
      this.svg.innerHTML = parts.join("");
    }
    // Um desenho = forma visível + área de clique mais larga + alças (quando selecionado).
    group(item, i, w) {
      const selected = i === this.selected, body = this.shape(item, w);
      if (!body) return "";
      return `<g class="d-item${selected ? " selected" : ""}" data-i="${i}">${body}${this.hitArea(item, w)}` +
        (selected ? this.handles(item, i, w) : "") + "</g>";
    }
    hitArea(item, w) {
      const line = (x1, y1, x2, y2) => `<line class="d-hit" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`;
      if (item.type === "hline") { const y = this.y(item.price); return line(0, y, w, y); }
      const x1 = this.x(item.a.time), x2 = this.x(item.b.time), y1 = this.y(item.a.price), y2 = this.y(item.b.price);
      if (item.type === "trend") return line(x1, y1, x2, y2);
      if (item.type === "fib") {
        const left = Math.min(x1, x2), right = Math.max(x1, x2) + 60;
        return FIB_LEVELS.map(level => {
          const y = this.y(item.b.price + (item.a.price - item.b.price) * level);
          return y === null ? "" : line(left, y, right, y);
        }).join("") + line(x1, y1, x2, y2);
      }
      return "";  // retângulo e régua: o próprio preenchimento já recebe o clique
    }
    handles(item, i, w) {
      const dot = (h, cx, cy) => cx === null || cy === null ? "" :
        `<circle class="d-handle" data-i="${i}" data-h="${h}" cx="${cx}" cy="${cy}" r="5"/>`;
      if (item.type === "hline") return dot("", Math.min(48, w / 2), this.y(item.price));
      const xa = this.x(item.a.time), xb = this.x(item.b.time), ya = this.y(item.a.price), yb = this.y(item.b.price);
      const ends = dot("a", xa, ya) + dot("b", xb, yb);
      return item.type === "rect" || item.type === "ruler" ? ends + dot("ab", xa, yb) + dot("ba", xb, ya) : ends;
    }
    shape(item, w) {
      const fmt = this.opts.fmtPrice;
      if (item.type === "hline") {
        const y = this.y(item.price);
        return y === null ? "" : `<line class="d-line" x1="0" x2="${w}" y1="${y}" y2="${y}"/>
          <text class="d-label" x="60" y="${y - 5}">${fmt(item.price)}</text>`;
      }
      const x1 = this.x(item.a.time), x2 = this.x(item.b.time), y1 = this.y(item.a.price), y2 = this.y(item.b.price);
      if ([x1, x2, y1, y2].some(v => v === null)) return "";
      if (item.type === "trend") return `<line class="d-line" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`;
      const left = Math.min(x1, x2), top = Math.min(y1, y2), width = Math.abs(x2 - x1), height = Math.abs(y2 - y1);
      if (item.type === "rect") return `<rect class="d-rect" x="${left}" y="${top}" width="${width}" height="${height}"/>`;
      if (item.type === "fib") {
        const right = Math.max(x1, x2) + 60;
        return FIB_LEVELS.map(level => {
          const price = item.b.price + (item.a.price - item.b.price) * level, y = this.y(price);
          return y === null ? "" : `<line class="d-fib${level === 0.618 ? " golden" : ""}" x1="${left}" x2="${right}" y1="${y}" y2="${y}"/>
            <text class="d-label" x="${right + 4}" y="${y + 4}">${(level * 100).toFixed(1).replace(".", ",")}% · ${fmt(price)}</text>`;
        }).join("") + `<line class="d-guide" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`;
      }
      if (item.type === "ruler") {
        const delta = item.b.price - item.a.price, pct = delta / item.a.price * 100;
        const bars = Math.round(Math.abs(this.timeToLogical(item.b.time) - this.timeToLogical(item.a.time)));
        const up = delta >= 0, label = `${up ? "▲" : "▼"} ${fmt(Math.abs(delta))} (${pct.toFixed(2).replace(".", ",")}%) · ${bars} candles`;
        return `<rect class="d-ruler ${up ? "up" : "down"}" x="${left}" y="${top}" width="${width}" height="${height}"/>
          <line class="d-guide" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>
          <text class="d-label strong" x="${left + width / 2}" y="${top - 6}" text-anchor="middle">${label}</text>`;
      }
      return "";
    }
  }

  root.DrawingLayer = DrawingLayer;
})(window);
