/*
 * Ferramentas de desenho sobre o gráfico: tendência, horizontal, retângulo, Fibonacci e régua.
 * Camada SVG por cima do Lightweight Charts. Os desenhos guardam (tempo, preço), não pixels,
 * e são reprojetados a cada zoom, rolagem ou novo candle; por isso sobrevivem à troca de timeframe.
 */
(function (root) {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  const TWO_POINTS = new Set(["trend", "rect", "fib", "ruler"]);

  class DrawingLayer {
    /**
     * @param container elemento do gráfico principal
     * @param opts {chart, series, getBars, getTf, fmtPrice, onChange(items), onToolEnd()}
     */
    constructor(container, opts) {
      this.opts = opts;
      this.items = [];
      this.tool = null;
      this.pending = null;
      this.hover = null;
      this.svg = document.createElementNS(NS, "svg");
      this.svg.setAttribute("class", "draw-layer");
      container.appendChild(this.svg);
      this.svg.addEventListener("pointerdown", e => this.onDown(e));
      this.svg.addEventListener("pointermove", e => { this.hover = this.pointFrom(e); this.render(); });
      this.svg.addEventListener("pointerleave", () => { this.hover = null; this.render(); });
      document.addEventListener("keydown", e => { if (e.key === "Escape" && this.tool) this.setTool(null); });
      opts.chart.timeScale().subscribeVisibleLogicalRangeChange(() => this.render());
      new ResizeObserver(() => this.render()).observe(container);
    }

    setTool(tool) {
      this.tool = tool;
      this.pending = null;
      this.svg.classList.toggle("active", Boolean(tool));  // com ferramenta ativa a camada captura os cliques
      this.render();
      if (!tool) this.opts.onToolEnd();
    }
    setItems(items) { this.items = items || []; this.render(); }
    undo() { this.items.pop(); this.changed(); }
    clear() { this.items = []; this.changed(); }
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
    pointFrom(e) {
      const r = this.svg.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
      const l = this.opts.chart.timeScale().coordinateToLogical(px), price = this.opts.series.coordinateToPrice(py);
      return l === null || price === null ? null : {time: this.logicalToTime(l), price};
    }

    onDown(e) {
      if (!this.tool || e.button !== 0) return;
      const p = this.pointFrom(e);
      if (!p) return;
      e.preventDefault();
      if (this.tool === "hline") {
        this.items.push({type: "hline", price: p.price});
      } else if (!this.pending) {
        this.pending = p;
        return this.render();
      } else {
        this.items.push({type: this.tool, a: this.pending, b: p});
      }
      this.changed();
      this.setTool(null);  // como nas plataformas: um desenho por seleção da ferramenta
    }

    // ── desenho ──
    render() {
      const w = this.svg.clientWidth, parts = [];
      for (const item of this.items) parts.push(this.shape(item, w));
      if (this.pending && this.hover && TWO_POINTS.has(this.tool)) {
        parts.push(`<g class="preview">${this.shape({type: this.tool, a: this.pending, b: this.hover}, w)}</g>`);
      }
      if (this.tool === "hline" && this.hover) parts.push(`<g class="preview">${this.shape({type: "hline", price: this.hover.price}, w)}</g>`);
      this.svg.innerHTML = parts.join("");
    }
    shape(item, w) {
      const fmt = this.opts.fmtPrice;
      if (item.type === "hline") {
        const y = this.y(item.price);
        return y === null ? "" : `<line class="d-line" x1="0" x2="${w}" y1="${y}" y2="${y}"/>
          <text class="d-label" x="6" y="${y - 5}">${fmt(item.price)}</text>`;
      }
      const x1 = this.x(item.a.time), x2 = this.x(item.b.time), y1 = this.y(item.a.price), y2 = this.y(item.b.price);
      if ([x1, x2, y1, y2].some(v => v === null)) return "";
      if (item.type === "trend") return `<line class="d-line" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>
        <circle class="d-dot" cx="${x1}" cy="${y1}" r="3"/><circle class="d-dot" cx="${x2}" cy="${y2}" r="3"/>`;
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
