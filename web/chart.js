/* Dependency-free multi-series line chart for the System Monitor panel.
   Draws on a canvas at devicePixelRatio so it stays crisp on HiDPI screens. */

const SERIES_COLORS = {
  gpu_util: "#3b82f6",
  vram: "#a855f7",
  cpu: "#f59e0b",
  ram: "#10b981",
  disk_read: "#06b6d4",
  disk_write: "#ef4444",
};

export class MetricChart {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {{keys: string[], label: string, color: string, unit?: string}[]} series
   * @param {{yMax?: number, percentMode?: boolean}} options
   */
  constructor(canvas, series, options = {}) {
    this.canvas = canvas;
    this.series = series;
    this.options = options;
    this.ctx = canvas.getContext("2d");
    this._onResize = () => this.resize();
    window.addEventListener("resize", this._onResize);
    this.resize();
  }

  resize() {
    const canvas = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(1, Math.floor(rect.width * dpr));
    const height = Math.max(1, Math.floor(rect.height * dpr));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    this.dpr = dpr;
  }

  destroy() {
    window.removeEventListener("resize", this._onResize);
  }

  /**
   * @param {object[]} samples raw samples from /sysmon/series
   * @param {number} dpr
   */
  draw(samples) {
    const ctx = this.ctx;
    if (!ctx) return;
    this.resize();
    const dpr = this.dpr || 1;
    const W = this.canvas.width;
    const H = this.canvas.height;
    ctx.clearRect(0, 0, W, H);
    if (!samples || samples.length < 2) {
      ctx.fillStyle = "rgba(255,255,255,0.28)";
      ctx.font = `${11 * dpr}px system-ui, sans-serif`;
      ctx.textAlign = "center";
      ctx.fillText("waiting for samples…", W / 2, H / 2);
      return;
    }

    const padTop = 16 * dpr;
    const padBottom = 4 * dpr;
    const plotH = Math.max(1, H - padTop - padBottom);

    // Resolve each series into numeric values, scaled to a 0..1 range.
    const resolved = [];
    for (const def of this.series) {
      const values = samples.map((s) => extract(s, def));
      const max = def.max ?? this._autoMax(values);
      resolved.push({ def, values, max: max > 0 ? max : 1 });
    }

    // Horizontal gridlines at 25/50/75%.
    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.lineWidth = 1;
    for (let i = 1; i <= 3; i++) {
      const y = Math.round(padTop + (plotH * i) / 4) + 0.5;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(W, y);
      ctx.stroke();
    }

    const stepX = samples.length > 1 ? W / (samples.length - 1) : W;
    for (const { def, values, max } of resolved) {
      ctx.beginPath();
      ctx.strokeStyle = def.color || SERIES_COLORS[def.key] || "#888";
      ctx.lineWidth = 1.5 * dpr;
      ctx.lineJoin = "round";
      let started = false;
      values.forEach((value, index) => {
        if (value === null || value === undefined || Number.isNaN(value)) {
          started = false;
          return;
        }
        const x = index * stepX;
        const y = padTop + plotH - Math.min(1, value / max) * plotH;
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      ctx.stroke();

      // Filled area under the primary series only, to keep it readable.
      if (def.fill) {
        ctx.lineTo(W, padTop + plotH);
        ctx.lineTo(0, padTop + plotH);
        ctx.closePath();
        const grad = ctx.createLinearGradient(0, padTop, 0, padTop + plotH);
        grad.addColorStop(0, hexToRgba(def.color || "#3b82f6", 0.3));
        grad.addColorStop(1, hexToRgba(def.color || "#3b82f6", 0.02));
        ctx.fillStyle = grad;
        ctx.fill();
      }
    }

    // Current-value labels, right-aligned.
    ctx.font = `${10 * dpr}px ui-monospace, Consolas, monospace`;
    ctx.textAlign = "right";
    let labelY = 11 * dpr;
    for (const { def, values, max } of resolved) {
      const last = lastNumber(values);
      if (last === null) continue;
      const text = `${def.label} ${formatNumber(last)}${def.unit || (this.options.percentMode ? "%" : "")}`;
      ctx.fillStyle = def.color || SERIES_COLORS[def.key] || "#888";
      const x = W - 4 * dpr;
      ctx.strokeStyle = "rgba(0,0,0,0.75)";
      ctx.lineWidth = 2.5 * dpr;
      ctx.strokeText(text, x, labelY);
      ctx.fillText(text, x, labelY);
      labelY += 12 * dpr;
      if (labelY > padTop + plotH) break;
      void max;
    }
  }

  _autoMax(values) {
    if (this.options.percentMode) return 100;
    let max = 0;
    for (const value of values) {
      if (typeof value === "number" && value > max) max = value;
    }
    // Round up to a friendly number so the axis does not jitter every sample.
    if (max <= 0) return 1;
    const magnitude = Math.pow(10, Math.floor(Math.log10(max)));
    return Math.ceil(max / magnitude) * magnitude;
  }
}

function extract(sample, def) {
  let node = sample;
  const path = def.path || [def.key];
  for (const key of path) {
    if (node === null || node === undefined || typeof node !== "object") return null;
    node = node[key];
  }
  return typeof node === "number" && Number.isFinite(node) ? node : null;
}

function lastNumber(values) {
  for (let i = values.length - 1; i >= 0; i--) {
    const value = values[i];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

function formatNumber(value) {
  if (value === null) return "-";
  if (Math.abs(value) >= 1000) return Math.round(value).toLocaleString();
  if (Math.abs(value) >= 100) return value.toFixed(0);
  return value.toFixed(1);
}

function hexToRgba(hex, alpha) {
  const clean = String(hex).replace("#", "");
  const full = clean.length === 3 ? clean.split("").map((c) => c + c).join("") : clean;
  const num = parseInt(full, 16);
  const r = (num >> 16) & 255;
  const g = (num >> 8) & 255;
  const b = num & 255;
  return `rgba(${r},${g},${b},${alpha})`;
}
