/* ComfyUI System Monitor - floating panel.
 *
 * Loaded by ComfyUI as an extension from /extensions/comfyui-sysmon/panel.js.
 * The host API is resolved defensively (see resolveHostApi) so the panel still
 * works if the frontend reorganises its internal module paths.
 */

import { MetricChart } from "./chart.js";

const PREFIX = "/sysmon";
const COLLAPSE_KEY = "sysmon.collapsed";
const POS_KEY = "sysmon.position";
const TAB_KEY = "sysmon.tab";
const HIDDEN_KEY = "sysmon.hidden";

const TABS = [
  { id: "live", label: "实时" },
  { id: "peaks", label: "峰值" },
  { id: "runs", label: "运行" },
  { id: "errors", label: "报错" },
  { id: "ai", label: "AI" },
  { id: "settings", label: "设置" },
];

const METRIC_ROWS = [
  { key: "gpu_util", label: "GPU 利用率", path: ["gpu", "util_percent"], unit: "%", max: 100 },
  { key: "vram_used", label: "显存", path: ["gpu", "mem_used_mb"], unit: "MB", totalPath: ["gpu", "mem_total_mb"] },
  { key: "cpu", label: "CPU 利用率", path: ["cpu_percent"], unit: "%", max: 100 },
  { key: "ram_used", label: "内存", path: ["ram_used_mb"], unit: "MB", totalPath: ["ram_total_mb"] },
  { key: "disk_read", label: "磁盘读取", path: ["disk_read_mb_s"], unit: "MB/s" },
  { key: "disk_write", label: "磁盘写入", path: ["disk_write_mb_s"], unit: "MB/s" },
];

// ---------------------------------------------------------------------------
// Host API resolution
// ---------------------------------------------------------------------------
let cachedApi = null;

async function resolveHostApi() {
  if (cachedApi) return cachedApi;
  const candidates = [
    "../../scripts/api.js",
    "/scripts/api.js",
    "../../../scripts/api.js",
  ];
  for (const path of candidates) {
    try {
      const mod = await import(/* @vite-ignore */ path);
      if (mod && (mod.api || mod.default?.api)) {
        cachedApi = mod.api || mod.default.api;
        return cachedApi;
      }
    } catch (err) {
      /* try the next candidate */
    }
  }
  // Fallback: some builds expose the API on window.comfyAPI.
  const global = globalThis.comfyAPI;
  if (global?.api?.api) {
    cachedApi = global.api.api;
    return cachedApi;
  }
  if (globalThis.comfyAPI?.api) {
    cachedApi = globalThis.comfyAPI.api;
    return cachedApi;
  }
  cachedApi = null;
  return null;
}

async function request(path, options = {}) {
  const init = { ...options };
  if (init.body && typeof init.body !== "string") {
    init.body = JSON.stringify(init.body);
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
  }
  const api = await resolveHostApi();
  if (api?.fetchApi) {
    const response = await api.fetchApi(PREFIX + path, init);
    const text = await response.text();
    return { ok: response.ok, status: response.status, data: safeJson(text) };
  }
  const response = await fetch(PREFIX + path, init);
  const text = await response.text();
  return { ok: response.ok, status: response.status, data: safeJson(text) };
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch (err) {
    return { ok: false, error: "非 JSON 响应: " + String(text).slice(0, 200) };
  }
}

function toast(message, severity = "info") {
  try {
    const manager = globalThis.comfyAPI?.app?.app?.extensionManager
      || globalThis.app?.extensionManager;
    if (manager?.toast?.add) {
      manager.toast.add({ severity, summary: "System Monitor", detail: message, life: 5000 });
      return;
    }
  } catch (err) {
    /* fall through to console */
  }
  if (severity === "error") console.error("[sysmon]", message);
  else console.log("[sysmon]", message);
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function dig(object, path) {
  let node = object;
  for (const key of path) {
    if (node === null || node === undefined || typeof node !== "object") return null;
    node = node[key];
  }
  return node === undefined ? null : node;
}

function fmt(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  const num = Number(value);
  if (Math.abs(num) >= 1000) return num.toLocaleString(undefined, { maximumFractionDigits: digits });
  return num.toFixed(digits);
}

function fmtBytesMB(mb) {
  if (mb === null || mb === undefined) return "-";
  return mb >= 1024 ? (mb / 1024).toFixed(2) + " GB" : Math.round(mb) + " MB";
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds < 60) return seconds.toFixed(seconds < 10 ? 1 : 0) + "s";
  const mins = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return mins + "m" + String(rest).padStart(2, "0") + "s";
}

function fmtTime(iso) {
  if (!iso) return "";
  return String(iso).replace(/^\d{4}-/, "");
}

/** Unix seconds -> local wall clock, e.g. "19:42:07". */
function fmtClock(unixSeconds) {
  if (!unixSeconds) return "";
  try {
    return new Date(unixSeconds * 1000).toLocaleTimeString();
  } catch (err) {
    return "";
  }
}

function pctClass(percent, warn, crit) {
  if (percent === null || percent === undefined) return "";
  if (crit && percent >= crit) return "is-crit";
  if (warn && percent >= warn) return "is-warn";
  return "";
}

/**
 * Build a labelled checkbox row.
 *
 * Returns `{ row, input }` so callers can append `row` and read `input.checked`.
 */
function checkRow(label, checked) {
  const row = el("div", "sysmon-check");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = Boolean(checked);
  const id = "sysmon-chk-" + Math.random().toString(36).slice(2, 9);
  input.id = id;
  const text = el("label", null, label);
  text.setAttribute("for", id);
  row.appendChild(input);
  row.appendChild(text);
  return { row, input };
}

/** Minimal, XSS-safe Markdown subset renderer for AI answers. */
function renderMarkdown(text) {
  const container = el("div");
  const lines = String(text || "").split(/\r?\n/);
  let list = null;
  let code = null;

  const inline = (raw) => {
    let out = escapeHtml(raw);
    out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    return out;
  };

  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      if (code) {
        container.appendChild(code);
        code = null;
      } else {
        code = el("pre");
      }
      continue;
    }
    if (code) {
      code.textContent += (code.textContent ? "\n" : "") + line;
      continue;
    }
    const heading = line.match(/^\s*(#{1,4})\s+(.*)$/);
    if (heading) {
      list = null;
      container.appendChild(el("h2", null, heading[2].replace(/\*\*/g, "")));
      continue;
    }
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      const wantOrdered = Boolean(numbered);
      if (!list || list.wantOrdered !== wantOrdered) {
        list = el(wantOrdered ? "ol" : "ul");
        list.wantOrdered = wantOrdered;
        container.appendChild(list);
      }
      const li = el("li");
      li.innerHTML = inline((bullet || numbered)[1]);
      list.appendChild(li);
      continue;
    }
    if (!line.trim()) {
      list = null;
      continue;
    }
    list = null;
    const paragraph = el("p");
    paragraph.innerHTML = inline(line);
    paragraph.style.margin = "4px 0";
    container.appendChild(paragraph);
  }
  if (code) container.appendChild(code);
  return container;
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------
export class SysmonPanel {
  constructor() {
    this.state = null;
    this.series = [];
    this.runs = [];
    this.errors = [];
    this.config = null;
    this.tab = localStorage.getItem(TAB_KEY) || "live";
    this.openRuns = new Set();
    this.analysis = null;
    // AI-tab content lives in state and is re-rendered on every poll. Appending
    // it once would not survive, because renderAi() rebuilds the body.
    this.lastAnalysisError = null;
    // Analysis results live in state and are re-rendered, never appended:
    // every tab rebuilds on each poll, so an appended node disappears within a
    // second. Reusing this.analysis for both scopes made an error analysis
    // overwrite the run one.
    this.analyses = {};
    this.aiPanel = null;
    this.aiChat = [];
    this.busy = false;
    this.liveChart = null;
    this.timer = null;
    this.build();
  }

  // -- construction -----------------------------------------------------
  build() {
    const panel = el("div");
    panel.id = "sysmon-panel";

    panel.appendChild(this.buildHeader());

    const tabs = el("div", "sysmon-tabs");
    this.tabButtons = {};
    for (const tab of TABS) {
      const button = el("button", "sysmon-tab");
      button.appendChild(el("span", null, tab.label));
      const badge = el("span", "sysmon-badge");
      button.appendChild(badge);
      button.addEventListener("click", () => this.setTab(tab.id));
      tabs.appendChild(button);
      this.tabButtons[tab.id] = { button, badge };
    }
    panel.appendChild(tabs);

    this.body = el("div", "sysmon-body");
    panel.appendChild(this.body);

    this.element = panel;
    document.body.appendChild(panel);

    this.launcher = el("button");
    this.launcher.id = "sysmon-launcher";
    this.dot = el("span", "sysmon-dot");
    this.launcherText = el("span", null, "System Monitor");
    this.launcher.appendChild(this.dot);
    this.launcher.appendChild(this.launcherText);
    // Must call show(), not setCollapsed(): the launcher only appears while the
    // panel is hidden, so merely uncollapsing would leave it unreachable.
    this.launcher.addEventListener("click", () => this.show());
    document.body.appendChild(this.launcher);

    if (localStorage.getItem(COLLAPSE_KEY) === "1") this.setCollapsed(true);
    this.restorePosition();
    this.setTab(this.tab);
    this.makeDraggable();
  }

  buildHeader() {
    const header = el("div", "sysmon-header");
    header.appendChild(el("span", null, "Monitor"));
    header.appendChild(el("span", "sysmon-title", "System Monitor"));
    header.appendChild(el("span", "sysmon-header-spacer"));

    this.chipGpu = el("span", "sysmon-chip", "-");
    this.chipVram = el("span", "sysmon-chip", "-");
    header.appendChild(this.chipGpu);
    header.appendChild(this.chipVram);

    this.collapseBtn = el("button", "sysmon-icon-btn", "-");
    this.collapseBtn.title = "折叠 / 展开";
    this.collapseBtn.addEventListener("click", () => this.toggleCollapsed());
    header.appendChild(this.collapseBtn);

    this.closeBtn = el("button", "sysmon-icon-btn", "x");
    this.closeBtn.title = "隐藏面板（可从右下角悬浮按钮重新打开）";
    this.closeBtn.addEventListener("click", () => this.hide());
    header.appendChild(this.closeBtn);
    return header;
  }

  makeDraggable() {
    const header = this.element.querySelector(".sysmon-header");
    let startX = 0;
    let startY = 0;
    let originLeft = 0;
    let originTop = 0;
    let dragging = false;

    const onMove = (event) => {
      if (!dragging) return;
      const rect = this.element.getBoundingClientRect();
      const left = Math.min(
        Math.max(0, originLeft + event.clientX - startX),
        window.innerWidth - rect.width - 4,
      );
      const top = Math.min(
        Math.max(0, originTop + event.clientY - startY),
        window.innerHeight - 40,
      );
      this.element.style.left = left + "px";
      this.element.style.top = top + "px";
      this.element.style.right = "auto";
      this.element.style.bottom = "auto";
    };

    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      const rect = this.element.getBoundingClientRect();
      localStorage.setItem(POS_KEY, JSON.stringify({ left: rect.left, top: rect.top }));
    };

    header.addEventListener("pointerdown", (event) => {
      if (event.target.closest("button")) return;
      const rect = this.element.getBoundingClientRect();
      dragging = true;
      startX = event.clientX;
      startY = event.clientY;
      originLeft = rect.left;
      originTop = rect.top;
      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp);
    });
  }

  restorePosition() {
    try {
      const saved = JSON.parse(localStorage.getItem(POS_KEY) || "null");
      if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) {
        this.element.style.left = Math.max(0, Math.min(saved.left, window.innerWidth - 60)) + "px";
        this.element.style.top = Math.max(0, Math.min(saved.top, window.innerHeight - 40)) + "px";
        this.element.style.right = "auto";
        this.element.style.bottom = "auto";
      }
    } catch (err) {
      /* ignore malformed saved position */
    }
  }

  // -- visibility -------------------------------------------------------
  setCollapsed(collapsed) {
    this.element.classList.toggle("is-collapsed", collapsed);
    this.collapseBtn.textContent = collapsed ? "+" : "-";
    localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
    this.body.style.display = collapsed ? "none" : "";
  }

  toggleCollapsed() {
    this.setCollapsed(!this.element.classList.contains("is-collapsed"));
  }

  hide() {
    this.element.classList.add("is-hidden");
    this.launcher.classList.add("is-visible");
    localStorage.setItem(HIDDEN_KEY, "1");
  }

  show() {
    this.element.classList.remove("is-hidden");
    this.launcher.classList.remove("is-visible");
    localStorage.setItem(HIDDEN_KEY, "0");
  }

  setTab(id) {
    this.tab = id;
    localStorage.setItem(TAB_KEY, id);
    for (const [key, entry] of Object.entries(this.tabButtons)) {
      entry.button.classList.toggle("is-active", key === id);
    }
    this.render();
  }

  // -- polling ----------------------------------------------------------
  start() {
    if (this.timer) return;
    const tick = async () => {
      if (localStorage.getItem(HIDDEN_KEY) === "1") {
        this.element.classList.add("is-hidden");
        this.launcher.classList.add("is-visible");
      }
      if (document.hidden) return;
      await this.refresh();
    };
    this.timer = setInterval(tick, 1000);
    tick();
  }

  async refresh() {
    if (this.busy) return;
    this.busy = true;
    try {
      const [state, series] = await Promise.all([
        request("/state"),
        request("/series?limit=180"),
      ]);
      if (state.data?.ok) {
        this.state = state.data;
        this.config = state.data.config || this.config;
      }
      if (series.data?.ok) {
        this.series = series.data.samples || [];
        this.samplerStats = series.data.sampler || null;
      }
      // Runs and errors change less often; fetch them on their own tabs.
      if (this.tab === "runs" || this.tab === "peaks") {
        const runs = await request("/runs?limit=12");
        if (runs.data?.ok) {
          const finished = runs.data.runs || [];
          const current = this.state?.current;
          // A running prompt is not in /runs yet, so splice it in at the top.
          this.runs = current
            ? [current, ...finished.filter((r) => r.prompt_id !== current.prompt_id)]
            : finished;
        }
      }
      if (this.tab === "errors" || this.tab === "ai") {
        const errors = await request("/errors?limit=25");
        if (errors.data?.ok) this.errors = errors.data.errors || [];
      }
      if (this.tab === "ai" && !this.runs.length) {
        const runs = await request("/runs?limit=12");
        if (runs.data?.ok) this.runs = runs.data.runs || [];
      }
      this.updateHeader();
      this.render();
    } catch (err) {
      console.warn("[sysmon] refresh failed", err);
    } finally {
      this.busy = false;
    }
  }

  updateHeader() {
    const sample = this.state?.sample;
    const gpu = sample?.gpu;
    const running = this.state?.current?.status === "running";

    this.chipGpu.textContent = gpu ? "GPU " + fmt(gpu.util_percent) + "%" : "GPU -";
    const vramPercent = gpu?.mem_total_mb ? (gpu.mem_used_mb / gpu.mem_total_mb) * 100 : null;
    this.chipVram.textContent = vramPercent === null ? "VRAM -" : "VRAM " + fmt(vramPercent) + "%";

    const ramPercent = sample?.ram_percent ?? null;
    const warn = this.config?.warn_vram_percent ?? 85;
    const crit = this.config?.crit_vram_percent ?? 95;
    const ramWarn = this.config?.warn_ram_percent ?? 85;
    const ramCrit = this.config?.crit_ram_percent ?? 95;
    const worst = Math.max(
      vramPercent === null ? 0 : vramPercent >= crit ? 2 : vramPercent >= warn ? 1 : 0,
      ramPercent === null ? 0 : ramPercent >= ramCrit ? 2 : ramPercent >= ramWarn ? 1 : 0,
    );
    this.dot.className = "sysmon-dot" + (worst === 2 ? " is-crit" : worst === 1 ? " is-warn" : "");

    const errorCount = this.errors?.length || 0;
    this.tabButtons.errors.badge.textContent = errorCount ? String(errorCount) : "";
    this.tabButtons.runs.badge.textContent = running ? ">" : "";

    this.launcherText.textContent = gpu
      ? "GPU " + fmt(gpu.util_percent) + "% · VRAM " + fmt(vramPercent) + "%"
      : "System Monitor";
  }

  // -- rendering --------------------------------------------------------
  render() {
    if (this.tab === "live") this.renderLive();
    else if (this.tab === "peaks") this.renderPeaks();
    else if (this.tab === "runs") this.renderRuns();
    else if (this.tab === "errors") this.renderErrors();
    else if (this.tab === "ai") this.renderAi();
    else if (this.tab === "settings") this.renderSettings();
  }

  renderLive() {
    const body = this.body;
    const sample = this.state?.sample;
    body.replaceChildren();

    const current = this.state?.current;
    if (current && current.status === "running") {
      const section = el("div", "sysmon-section");
      section.appendChild(el("div", "sysmon-section-title", "正在运行"));
      const line = el("div", "sysmon-kv");
      line.appendChild(el("span", null, current.workflow || "(未命名工作流)"));
      line.appendChild(el("span", null, current.executed_count + "/" + current.node_count + " 节点"));
      section.appendChild(line);
      body.appendChild(section);
    }

    const chartSection = el("div", "sysmon-section");
    chartSection.appendChild(el("div", "sysmon-section-title", "历史曲线"));
    const wrapper = el("div", "sysmon-chart-wrap");
    // The canvas element is created once and reused. This tab rebuilds its DOM
    // on every poll, and MetricChart holds a reference to one canvas: creating a
    // fresh element each time left the chart drawing into a detached node, so
    // the visible canvas stayed blank.
    if (!this.chartCanvas) {
      this.chartCanvas = document.createElement("canvas");
    }
    wrapper.appendChild(this.chartCanvas);
    chartSection.appendChild(wrapper);
    body.appendChild(chartSection);

    if (!this.liveChart) {
      this.liveChart = new MetricChart(
        this.chartCanvas,
        [
          { key: "gpu_util", label: "GPU", path: ["gpu", "util_percent"], unit: "%", color: "#3b82f6", max: 100, fill: true },
          { key: "cpu", label: "CPU", path: ["cpu_percent"], unit: "%", color: "#f59e0b", max: 100 },
          { key: "vram_pct", label: "VRAM", color: "#a855f7", max: 100 },
          { key: "ram_pct", label: "RAM", color: "#10b981", max: 100 },
        ],
        { percentMode: true },
      );
    }
    // Normalise VRAM/RAM to percentages for a fair overlay.
    const normalised = this.series.map((s) => {
      const gpu = s.gpu || {};
      return {
        ...s,
        vram_pct: gpu.mem_total_mb ? (gpu.mem_used_mb / gpu.mem_total_mb) * 100 : null,
        ram_pct: s.ram_total_mb ? (s.ram_used_mb / s.ram_total_mb) * 100 : null,
      };
    });
    this.liveChart.draw(normalised);

    const metrics = el("div", "sysmon-section");
    metrics.appendChild(el("div", "sysmon-section-title", "当前读数"));
    if (!sample) {
      metrics.appendChild(el("div", "sysmon-empty", "正在采集数据…"));
    } else {
      for (const row of METRIC_ROWS) {
        const value = dig(sample, row.path);
        const total = row.totalPath ? dig(sample, row.totalPath) : null;
        const percent = total ? (value / total) * 100 : row.max ? value : null;
        metrics.appendChild(this.metricRow(row, value, total, percent));
      }
    }
    body.appendChild(metrics);

    const gpu = sample?.gpu;
    if (gpu) {
      const details = el("div", "sysmon-section");
      details.appendChild(el("div", "sysmon-section-title", "硬件"));
      const grid = el("div", "sysmon-dual");
      const add = (label, value) => {
        if (value === null || value === undefined || value === "") return;
        const kv = el("div", "sysmon-kv");
        kv.appendChild(el("span", null, label));
        kv.appendChild(el("span", null, value));
        grid.appendChild(kv);
      };
      add("显卡", gpu.name || "-");
      add("温度", gpu.temperature_c !== null && gpu.temperature_c !== undefined ? fmt(gpu.temperature_c) + " °C" : null);
      add("功耗", gpu.power_w !== null && gpu.power_w !== undefined ? fmt(gpu.power_w, 1) + " W" : null);
      add("风扇", gpu.fan_percent !== null && gpu.fan_percent !== undefined ? fmt(gpu.fan_percent) + " %" : null);
      add("显存空闲", gpu.mem_free_mb !== null && gpu.mem_free_mb !== undefined ? fmtBytesMB(gpu.mem_free_mb) : null);
      add("进程显存", gpu.self_used_mb ? fmtBytesMB(gpu.self_used_mb) : null);
      add("内存可用", sample.ram_available_mb ? fmtBytesMB(sample.ram_available_mb) : null);
      add("交换分区", sample.swap_percent !== null && sample.swap_percent !== undefined ? fmt(sample.swap_percent) + " %" : null);
      add("CPU 频率", sample.cpu_freq_mhz ? fmt(sample.cpu_freq_mhz) + " MHz" : null);
      add("采集后端", this.samplerStats?.gpu_backend || gpu.backend || "-");
      details.appendChild(grid);
      body.appendChild(details);
    }

    const stats = this.samplerStats;
    if (stats && !stats.has_psutil) {
      body.appendChild(el("div", "sysmon-toast is-err", "未检测到 psutil，CPU/内存/磁盘数据不可用。请安装：pip install psutil"));
    }
  }

  metricRow(row, value, total, percent) {
    const wrap = el("div", "sysmon-metric");
    const head = el("div", "sysmon-metric-head");
    head.appendChild(el("span", "sysmon-metric-label", row.label));
    const valueText = row.unit === "MB" && value !== null
      ? fmtBytesMB(value) + (total ? " / " + fmtBytesMB(total) : "")
      : value === null || value === undefined
        ? "-"
        : fmt(value, row.unit === "%" ? 0 : 1) + " " + (row.unit || "");
    head.appendChild(el("span", "sysmon-metric-value", valueText));
    wrap.appendChild(head);
    if (percent !== null && percent !== undefined) {
      const isVram = row.key === "vram_used";
      const isRam = row.key === "ram_used";
      const warn = isVram ? this.config?.warn_vram_percent ?? 85 : isRam ? this.config?.warn_ram_percent ?? 85 : 0;
      const crit = isVram ? this.config?.crit_vram_percent ?? 95 : isRam ? this.config?.crit_ram_percent ?? 95 : 0;
      head.appendChild(el("span", "sysmon-metric-sub", fmt(percent) + "%"));
      const bar = el("div", "sysmon-bar");
      const fill = el("div", "sysmon-bar-fill " + pctClass(percent, warn, crit));
      fill.style.width = Math.max(0, Math.min(100, percent)) + "%";
      bar.appendChild(fill);
      wrap.appendChild(bar);
    }
    return wrap;
  }

  renderPeaks() {
    const body = this.body;
    body.replaceChildren();
    const current = this.state?.current;
    const latest = this.runs[0];

    const section = el("div", "sysmon-section");
    section.appendChild(el("div", "sysmon-section-title", "本次运行峰值归属"));

    const target = current && current.nodes?.length ? current : latest;
    if (!target || !target.nodes?.length) {
      section.appendChild(el("div", "sysmon-empty", "还没有可统计的运行数据。跑一次工作流即可。"));
      body.appendChild(section);
      return;
    }

    const meta = el("div", "sysmon-kv");
    meta.appendChild(el("span", null, (target.workflow || "(未命名)") + (target.status === "running" ? " · 进行中" : "")));
    meta.appendChild(el("span", null, target.status === "running"
      ? target.executed_count + "/" + target.node_count + " 节点"
      : "用时 " + fmtDuration(target.duration_s)));
    section.appendChild(meta);

    section.appendChild(el("div", "sysmon-hint",
      "峰值 = 该节点执行期间采集到的最大值（采样间隔 " + (target.sample_interval_ms || "?") + " ms）。点节点名可在画布中定位。"));

    const ownerTable = el("table", "sysmon-table");
    const thead = el("thead");
    const headRow = el("tr");
    for (const label of ["指标", "峰值", "发生在哪个节点"]) {
      headRow.appendChild(el("th", null, label));
    }
    thead.appendChild(headRow);
    ownerTable.appendChild(thead);
    const tbody = el("tbody");
    for (const row of METRIC_ROWS) {
      const owner = findPeakOwner(target.nodes, row.key);
      if (!owner) continue;
      const tr = el("tr");
      tr.appendChild(el("td", null, row.label));
      const isMem = row.key === "vram_used" || row.key === "ram_used";
      tr.appendChild(el("td", null, isMem && owner.peak.percent !== null && owner.peak.percent !== undefined
        ? fmt(owner.peak.value) + " " + owner.peak.unit + " (" + fmt(owner.peak.percent) + "%)"
        : fmt(owner.peak.value, 1) + " " + (owner.peak.unit || "")));
      const nameCell = el("td", "sysmon-node-name");
      nameCell.appendChild(el("span", null, owner.node.title || "#" + owner.node.node_id));
      nameCell.appendChild(el("span", "sysmon-node-type", owner.node.class_type || ""));
      nameCell.title = "点击定位到画布中的该节点";
      nameCell.style.cursor = "pointer";
      nameCell.addEventListener("click", () => locateNode(owner.node.node_id));
      tr.appendChild(nameCell);
      tbody.appendChild(tr);
    }
    ownerTable.appendChild(tbody);
    section.appendChild(ownerTable);
    body.appendChild(section);

    const nodesSection = el("div", "sysmon-section");
    nodesSection.appendChild(el("div", "sysmon-section-title", "各节点耗时与峰值"));
    const table = el("table", "sysmon-table");
    const nhead = el("thead");
    const nrow = el("tr");
    for (const label of ["节点", "耗时", "GPU", "显存", "内存"]) {
      nrow.appendChild(el("th", null, label));
    }
    nhead.appendChild(nrow);
    table.appendChild(nhead);
    const nbody = el("tbody");
    const sorted = [...target.nodes].sort((a, b) => (b.duration_s || 0) - (a.duration_s || 0));
    for (const node of sorted) {
      const tr = el("tr");
      if ((node.duration_s || 0) === (sorted[0].duration_s || 0)) tr.className = "is-peak";
      const nameCell = el("td", "sysmon-node-name");
      nameCell.appendChild(el("span", null, node.title || "#" + node.node_id));
      nameCell.appendChild(el("span", "sysmon-node-type", node.class_type || ""));
      nameCell.style.cursor = "pointer";
      nameCell.addEventListener("click", () => locateNode(node.node_id));
      tr.appendChild(nameCell);
      tr.appendChild(el("td", null, fmtDuration(node.duration_s)));
      tr.appendChild(el("td", null, peakText(node, "gpu_util", "%")));
      tr.appendChild(el("td", null, peakText(node, "vram_used", "MB", true)));
      tr.appendChild(el("td", null, peakText(node, "ram_used", "MB", true)));
      nbody.appendChild(tr);
    }
    table.appendChild(nbody);
    nodesSection.appendChild(table);
    body.appendChild(nodesSection);
  }

  renderRuns() {
    const body = this.body;
    body.replaceChildren();
    const section = el("div", "sysmon-section");
    section.appendChild(el("div", "sysmon-section-title", "最近运行"));

    const current = this.state?.current;
    const all = current
      ? [current, ...this.runs.filter((r) => r.prompt_id !== current.prompt_id)]
      : this.runs;
    if (!all.length) {
      section.appendChild(el("div", "sysmon-empty", "还没有运行记录。"));
      body.appendChild(section);
      return;
    }

    for (const run of all) section.appendChild(this.runCard(run));

    const actions = el("div", "sysmon-actions");
    const clear = el("button", "sysmon-btn", "清空历史");
    clear.addEventListener("click", async () => {
      await request("/runs", { method: "DELETE" });
      this.runs = [];
      toast("已清空内存中的运行历史（日志文件保留）", "info");
      await this.refresh();
    });
    actions.appendChild(clear);
    section.appendChild(actions);
    body.appendChild(section);
  }

  runCard(run) {
    const card = el("div", "sysmon-run");
    if (this.openRuns.has(run.prompt_id)) card.classList.add("is-open");

    const head = el("div", "sysmon-run-head");
    head.appendChild(el("span", "sysmon-status is-" + run.status));
    const title = el("span", "sysmon-run-title", run.workflow || "(未命名 #" + run.index + ")");
    title.title = String(run.prompt_id);
    head.appendChild(title);
    head.appendChild(el("span", "sysmon-run-meta",
      run.status === "running"
        ? "运行中 " + run.executed_count + "/" + run.node_count
        : fmtDuration(run.duration_s) + " · " + run.executed_count + "节点" + (run.cached_count ? " (" + run.cached_count + " 缓存)" : "")));
    head.addEventListener("click", () => {
      if (this.openRuns.has(run.prompt_id)) this.openRuns.delete(run.prompt_id);
      else this.openRuns.add(run.prompt_id);
      card.classList.toggle("is-open");
    });
    card.appendChild(head);

    const bodyEl = el("div", "sysmon-run-body");
    bodyEl.appendChild(el("div", "sysmon-when",
      (run.started_iso || "") + (run.status === "running" ? "" : " → 用时 " + fmtDuration(run.duration_s))));

    const peaks = run.peaks || {};
    if (Object.keys(peaks).length) {
      const grid = el("div", "sysmon-dual");
      for (const row of METRIC_ROWS) {
        const peak = peaks[row.key];
        if (!peak) continue;
        const kv = el("div", "sysmon-kv");
        kv.appendChild(el("span", null, row.label));
        const isMem = row.key === "vram_used" || row.key === "ram_used";
        const suffix = isMem && peak.percent !== null && peak.percent !== undefined
          ? " (" + fmt(peak.percent) + "%)"
          : "";
        kv.appendChild(el("span", null, fmt(peak.value, 1) + " " + (peak.unit || "") + suffix));
        grid.appendChild(kv);
      }
      bodyEl.appendChild(grid);
    }

    for (const error of run.errors || []) bodyEl.appendChild(this.errorBlock(error, run));

    if (run.nodes?.length) {
      const table = el("table", "sysmon-table");
      const thead = el("thead");
      const headRow = el("tr");
      for (const label of ["节点", "耗时", "GPU", "显存"]) {
        headRow.appendChild(el("th", null, label));
      }
      thead.appendChild(headRow);
      table.appendChild(thead);
      const tbody = el("tbody");
      const sorted = [...run.nodes].sort((a, b) => (b.duration_s || 0) - (a.duration_s || 0));
      for (const node of sorted.slice(0, 30)) {
        const tr = el("tr");
        const nameCell = el("td", "sysmon-node-name");
        nameCell.appendChild(el("span", null, node.title || "#" + node.node_id));
        nameCell.appendChild(el("span", "sysmon-node-type", node.class_type || ""));
        nameCell.style.cursor = "pointer";
        nameCell.addEventListener("click", () => locateNode(node.node_id));
        tr.appendChild(nameCell);
        tr.appendChild(el("td", null, fmtDuration(node.duration_s)));
        tr.appendChild(el("td", null, peakText(node, "gpu_util", "%")));
        tr.appendChild(el("td", null, peakText(node, "vram_used", "MB", true)));
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
      bodyEl.appendChild(table);
      if (run.nodes.length > 30) {
        bodyEl.appendChild(el("div", "sysmon-hint", "仅显示耗时最长的 30 个节点，共 " + run.nodes.length + " 个。"));
      }
    }

    if (run.saved_path) {
      bodyEl.appendChild(el("div", "sysmon-hint", "已保存：" + run.saved_path));
    }

    const actions = el("div", "sysmon-actions");
    if (run.status !== "running") {
      const analyzeBtn = el("button", "sysmon-btn is-primary", "AI 分析这次运行");
      analyzeBtn.addEventListener("click", () => this.analyze(run, null, analyzeBtn));
      actions.appendChild(analyzeBtn);
    }
    bodyEl.appendChild(actions);

    if (run.analysis) {
      const box = el("div", "sysmon-ai");
      box.appendChild(renderMarkdown(run.analysis.text));
      box.appendChild(el("div", "sysmon-ai-meta", (run.analysis.model || "") + " · " + fmtClock(run.analysis.at)));
      bodyEl.appendChild(box);
    }

    card.appendChild(bodyEl);
    return card;
  }

  renderErrors() {
    const body = this.body;
    body.replaceChildren();
    const section = el("div", "sysmon-section");
    section.appendChild(el("div", "sysmon-section-title", "报错记录 (" + this.errors.length + ")"));

    if (!this.errors.length) {
      section.appendChild(el("div", "sysmon-empty", "暂无报错"));
      body.appendChild(section);
      return;
    }

    for (const error of this.errors) {
      const run = this.runs.find((r) => r.prompt_id === error.prompt_id) || {
        prompt_id: error.prompt_id,
        workflow: error.workflow,
        index: error.run_index,
      };
      section.appendChild(this.errorBlock(error, run, true));
    }
    body.appendChild(section);
  }

  /** Stable identity for an error, so its analysis can be looked up later. */
  errorKey(error, run) {
    const list = (run && run.errors) || [];
    const index = list.indexOf(error);
    return index >= 0 ? index : (error && error.t) || 0;
  }

  errorBlock(error, run, expanded = false) {
    const box = el("div", "sysmon-error");
    box.appendChild(el("div", "sysmon-error-head",
      (error.kind === "interrupted" ? "[已中断] " : "[错误] ") + (error.exception_type || "错误")));

    const where = el("div", "sysmon-when",
      "节点：" + (error.node_title || error.node_type || "-") + (error.node_id ? " (#" + error.node_id + ")" : "") + " · " + fmtClock(error.t));
    where.style.cursor = "pointer";
    if (error.node_id) where.addEventListener("click", () => locateNode(error.node_id));
    box.appendChild(where);

    const message = el("div", "sysmon-error-msg", error.exception_message || "(无消息)");
    if (!expanded) message.style.maxHeight = "72px";
    box.appendChild(message);

    if (error.traceback?.length) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "堆栈 (" + error.traceback.length + " 行)";
      summary.style.cursor = "pointer";
      summary.style.opacity = "0.7";
      details.appendChild(summary);
      const pre = el("div", "sysmon-error-msg", error.traceback.join("\n"));
      pre.style.maxHeight = "200px";
      details.appendChild(pre);
      box.appendChild(details);
    }

    const actions = el("div", "sysmon-actions");
    const analyzeBtn = el("button", "sysmon-btn is-primary", "AI 分析这个报错");
    analyzeBtn.addEventListener("click", () => this.analyze(run, error, analyzeBtn, box));
    actions.appendChild(analyzeBtn);

    const copyBtn = el("button", "sysmon-btn", "复制报错");
    copyBtn.addEventListener("click", async () => {
      const text = (error.exception_type || "") + ": " + (error.exception_message || "") + "\n\n" + (error.traceback || []).join("\n");
      try {
        await navigator.clipboard.writeText(text);
        copyBtn.textContent = "已复制";
        setTimeout(() => { copyBtn.textContent = "复制报错"; }, 1500);
      } catch (err) {
        toast("复制失败，请手动选择文本", "error");
      }
    });
    actions.appendChild(copyBtn);
    box.appendChild(actions);

    // Rendered from state: this block is rebuilt on every poll, so a one-shot
    // append would vanish before the user could read it.
    const runId = run && run.prompt_id ? run.prompt_id : "unknown";
    const key = runId + ":" + this.errorKey(error, run);
    const entry = this.analyses[key];
    if (entry) {
      const wrap = el("div", "sysmon-ai");
      wrap.appendChild(renderMarkdown(entry.text));
      if (entry.meta) wrap.appendChild(el("div", "sysmon-ai-meta", entry.meta));
      box.appendChild(wrap);
    }
    return box;
  }

  renderAi() {
    const body = this.body;
    body.replaceChildren();
    const section = el("div", "sysmon-section");
    section.appendChild(el("div", "sysmon-section-title", "AI 运行诊断"));

    // Rendered from state, not appended once: this tab rebuilds on every poll,
    // so a one-shot append would vanish before the user could read it.
    if (this.lastAnalysisError) {
      section.appendChild(el("div", "sysmon-toast is-err", this.lastAnalysisError));
    }

    const configured = this.config?.llm_enabled && this.config?.llm_api_key_set;
    if (!configured) {
      section.appendChild(el("div", "sysmon-toast is-err",
        "尚未配置可用的在线大模型。请到「设置」标签页填写 API Key，DeepSeek 或任意 OpenAI 兼容接口都可以。"));
    }

    const latest = this.runs[0];
    if (!latest) {
      section.appendChild(el("div", "sysmon-empty", "还没有已完成的运行可供分析。"));
      body.appendChild(section);
      return;
    }

    const info = el("div", "sysmon-kv");
    info.appendChild(el("span", null, "最近一次运行：" + (latest.workflow || "(未命名)")));
    info.appendChild(el("span", null, latest.status + " · " + fmtDuration(latest.duration_s)));
    section.appendChild(info);

    const actions = el("div", "sysmon-actions");
    const btn = el("button", "sysmon-btn is-primary", "分析最近一次运行");
    btn.addEventListener("click", () => this.analyze(latest, latest.errors?.[0] || null, btn));
    actions.appendChild(btn);

    const previewBtn = el("button", "sysmon-btn", "查看将要发送的数据");
    previewBtn.addEventListener("click", () => this.showContextPreview(latest));
    actions.appendChild(previewBtn);
    section.appendChild(actions);

    const box = el("div");
    box.id = "sysmon-ai-output";
    section.appendChild(box);
    body.appendChild(section);

    const latestKey = latest.prompt_id + ":run";
    const storedRunAnalysis = this.analyses[latestKey];
    if (storedRunAnalysis && !this.aiChat.some((turn) => turn.fromRun)) {
      const wrap = el("div", "sysmon-ai");
      wrap.appendChild(renderMarkdown(storedRunAnalysis.text));
      if (storedRunAnalysis.meta) wrap.appendChild(el("div", "sysmon-ai-meta", storedRunAnalysis.meta));
      box.appendChild(wrap);
      this.aiChat.push({ fromRun: true, text: storedRunAnalysis.text, meta: storedRunAnalysis.meta });
    } else if (latest.analysis?.text && !this.aiChat.some((turn) => turn.fromRun)) {
      const wrap = el("div", "sysmon-ai");
      wrap.appendChild(renderMarkdown(latest.analysis.text));
      wrap.appendChild(el("div", "sysmon-ai-meta", "模型：" + (latest.analysis.model || "-")));
      box.appendChild(wrap);
      this.aiChat.push({ fromRun: true, text: latest.analysis.text, meta: latest.analysis.model || "-" });
    } else if (!this.aiChat.length && !this.lastAnalysisError) {
      box.appendChild(el("div", "sysmon-hint", "点击上面的按钮，把这次的运行指标和报错发给在线大模型，得到瓶颈分析和优化建议。"));
    }

    for (const turn of this.aiChat) {
      if (turn.fromRun) continue;
      const wrap = el("div", "sysmon-ai");
      wrap.appendChild(renderMarkdown(turn.text));
      if (turn.meta) wrap.appendChild(el("div", "sysmon-ai-meta", turn.meta));
      box.appendChild(wrap);
    }

    if (this.aiPanel) {
      const panelSection = el("div", "sysmon-section");
      panelSection.appendChild(el("div", "sysmon-section-title", this.aiPanel.title));
      if (this.aiPanel.hint) panelSection.appendChild(el("div", "sysmon-hint", this.aiPanel.hint));
      const pre = el("div", "sysmon-error-msg", this.aiPanel.content);
      pre.style.maxHeight = "340px";
      panelSection.appendChild(pre);
      box.appendChild(panelSection);
    }

    if (this.aiChat.length) {
      const followUp = el("div", "sysmon-field");
      const input = el("textarea");
      input.rows = 2;
      input.placeholder = "继续追问，例如：如果我想把分辨率翻倍，显存还够吗？";
      followUp.appendChild(input);
      const ask = el("button", "sysmon-btn", "继续追问");
      ask.addEventListener("click", async () => {
        const question = input.value.trim();
        if (!question) return;
        ask.disabled = true;
        ask.textContent = "思考中…";
        await this.analyze(latest, null, ask, null, question, true);
      });
      followUp.appendChild(ask);
      box.appendChild(followUp);
    }
  }

  renderSettings() {
    const body = this.body;

    // The form is built once and then left alone.
    //
    // Rebuilding it on every poll cannot work: the focused element is destroyed
    // and recreated once a second, so keystrokes that land between the teardown
    // and the restore are lost and the caret escapes to <body>. Preserving
    // values and re-focusing mitigates but does not fix that - it still
    // corrupted typed text. A settings form is not live data, so the correct
    // fix is to stop re-rendering it.
    if (this.settingsRendered && body.querySelector(".sysmon-settings-form")) {
      return;
    }

    const config = this.config;
    if (!config) {
      body.replaceChildren();
      body.appendChild(el("div", "sysmon-empty", "配置加载中…"));
      return;
    }
    body.replaceChildren();
    const formRoot = el("div", "sysmon-settings-form");

    // This tab is rebuilt on every poll, but the form holds text the user is
    // actively typing. Field values therefore live in this.settingsValues, are
    // written on input events, and are re-applied after each re-render -
    // otherwise the rebuilt nodes come back blank and the caret is lost once a
    // second, which reads as "the input boxes do not accept typing".
    // Two separate objects on purpose: `form` maps field -> element for the
    // current DOM (rebuilt every poll), while settingsValues is the durable
    // value store. Conflating them loses every keystroke on re-render.
    const savedValues = this.settingsValues || {};
    const savedFocus = this.settingsFocus || null;
    const isFirstBuild = !this.settingsValues;
    const form = {};

    const bindInput = (input, key, initial) => {
      input.dataset.field = key;
      input.value = isFirstBuild ? (initial ?? "") : (savedValues[key] ?? "");
      input.addEventListener("input", () => { this.settingsValues[key] = input.value; });
      input.addEventListener("focus", () => { this.settingsFocus = key; });
      input.addEventListener("blur", () => {
        if (this.settingsFocus === key) this.settingsFocus = null;
      });
      form[key] = input;
      return input;
    };

    const bindSelect = (select, key, initial) => {
      select.dataset.field = key;
      select.value = isFirstBuild ? initial : (savedValues[key] ?? initial);
      select.addEventListener("change", () => { this.settingsValues[key] = select.value; });
      form[key] = select;
      return select;
    };

    const bindCheckbox = (input, key, initial) => {
      input.dataset.field = key;
      input.checked = isFirstBuild ? Boolean(initial) : Boolean(savedValues[key]);
      input.addEventListener("change", () => { this.settingsValues[key] = input.checked; });
      form[key] = input;
      return input;
    };

    /** Current values as shown in the form, for saving or testing. */
    const readLlm = () => {
      const patch = {
        llm_provider: form.llm_provider.value,
        llm_base_url: form.llm_base_url.value.trim(),
        llm_model: form.llm_model.value.trim(),
        llm_language: form.llm_language.value,
        llm_enabled: form.llm_enabled.checked,
      };
      const typed = form.llm_api_key.value.trim();
      if (typed) patch.llm_api_key = typed;
      return patch;
    };

    const section = el("div", "sysmon-section");
    section.appendChild(el("div", "sysmon-section-title", "在线大模型"));

    const providerField = el("div", "sysmon-field");
    providerField.appendChild(el("label", null, "服务商"));
    const providerSelect = document.createElement("select");
    for (const [key, preset] of Object.entries(config.provider_presets || {})) {
      const option = document.createElement("option");
      option.value = key;
      option.textContent = preset.label || key;
      providerSelect.appendChild(option);
    }
    bindSelect(providerSelect, "llm_provider", config.llm_provider || "deepseek");
    providerField.appendChild(providerSelect);
    section.appendChild(providerField);

    const keyField = el("div", "sysmon-field");
    keyField.appendChild(el("label", null, "API Key"));
    const keyInput = document.createElement("input");
    keyInput.type = "password";
    keyInput.placeholder = config.llm_api_key_set ? "已保存（留空则不修改）" : "sk-...";
    // Never seed this from config: the server only ever sends a masked value.
    bindInput(keyInput, "llm_api_key", "");
    keyField.appendChild(keyInput);
    keyField.appendChild(el("div", "sysmon-hint",
      "Key 只保存在本地 sysmon_config.json，不会发送到除你配置的服务商以外的任何地方。"));
    section.appendChild(keyField);

    const baseField = el("div", "sysmon-field");
    baseField.appendChild(el("label", null, "Base URL（留空使用默认）"));
    const baseInput = document.createElement("input");
    baseInput.type = "text";
    baseInput.placeholder = "https://api.deepseek.com";
    bindInput(baseInput, "llm_base_url", config.llm_base_url || "");
    baseField.appendChild(baseInput);
    section.appendChild(baseField);

    const modelField = el("div", "sysmon-field");
    modelField.appendChild(el("label", null, "模型名（留空使用默认）"));
    const modelInput = document.createElement("input");
    modelInput.type = "text";
    modelInput.placeholder = "deepseek-chat";
    bindInput(modelInput, "llm_model", config.llm_model || "");
    modelField.appendChild(modelInput);
    section.appendChild(modelField);

    const langField = el("div", "sysmon-field");
    langField.appendChild(el("label", null, "回答语言"));
    const langSelect = document.createElement("select");
    for (const [value, label] of [["zh", "简体中文"], ["en", "English"]]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      langSelect.appendChild(option);
    }
    bindSelect(langSelect, "llm_language", config.llm_language || "zh");
    langField.appendChild(langSelect);
    section.appendChild(langField);

    const llmToggle = checkRow("启用 AI 分析", config.llm_enabled);
    bindCheckbox(llmToggle.input, "llm_enabled", config.llm_enabled);
    section.appendChild(llmToggle.row);

    this.settingsToast = el("div");
    section.appendChild(this.settingsToast);

    const llmActions = el("div", "sysmon-actions");
    const llmSaveBtn = el("button", "sysmon-btn is-primary", "保存");
    llmSaveBtn.addEventListener("click", async () => {
      await this.saveConfig(readLlm(), llmSaveBtn);
      // Drop the key from both the field and the persisted form state so a later
      // re-render cannot bring it back and it is not kept in memory.
      form.llm_api_key.value = "";
      this.settingsValues.llm_api_key = "";
    });
    llmActions.appendChild(llmSaveBtn);

    const testBtn = el("button", "sysmon-btn", "测试连接");
    testBtn.addEventListener("click", async () => {
      // Honour unsaved edits: push the visible form state before testing.
      const saved = await request("/config", { method: "POST", body: readLlm() });
      if (saved.data?.ok) this.config = saved.data.config;

      testBtn.disabled = true;
      testBtn.textContent = "测试中…";
      const result = await request("/test_llm", { method: "POST" });
      testBtn.disabled = false;
      testBtn.textContent = "测试连接";
      if (result.data?.ok) {
        this.showSettingsToast("连接成功：" + (result.data.model || "") + " 回复「" + (result.data.reply || "") + "」", true);
        toast("大模型连接成功", "success");
      } else {
        this.showSettingsToast(result.data?.error || "连接失败", false);
      }
    });
    llmActions.appendChild(testBtn);
    section.appendChild(llmActions);
    formRoot.appendChild(section);

    const sampling = el("div", "sysmon-section");
    sampling.appendChild(el("div", "sysmon-section-title", "采集与存储"));
    const numericFields = [
      ["采样间隔 (ms)", "sample_interval_ms", config.sample_interval_ms, 200, 60000],
      ["运行时采样间隔 (ms)", "sample_interval_active_ms", config.sample_interval_active_ms, 100, 60000],
      ["历史样本数", "history_size", config.history_size, 60, 20000],
      ["保留运行数（内存）", "history_runs", config.history_runs, 1, 200],
      ["最多保存日志文件", "max_saved_runs", config.max_saved_runs, 1, 5000],
    ];
    const numericInputs = [];
    for (const [label, key, value, min, max] of numericFields) {
      const field = el("div", "sysmon-field");
      field.appendChild(el("label", null, label));
      const input = document.createElement("input");
      input.type = "number";
      input.min = String(min);
      input.max = String(max);
      bindInput(input, key, value);
      field.appendChild(input);
      sampling.appendChild(field);
      numericInputs.push(input);
    }
    const saveRuns = checkRow("保存每次运行到 logs/ 目录", config.save_runs);
    bindCheckbox(saveRuns.input, "save_runs", config.save_runs);
    sampling.appendChild(saveRuns.row);
    const saveArtifacts = checkRow("报错时额外保存详细快照", config.save_error_artifacts);
    bindCheckbox(saveArtifacts.input, "save_error_artifacts", config.save_error_artifacts);
    sampling.appendChild(saveArtifacts.row);
    const samplingActions = el("div", "sysmon-actions");
    const saveSampling = el("button", "sysmon-btn is-primary", "保存采集设置");
    saveSampling.addEventListener("click", async () => {
      const patch = {};
      for (const input of numericInputs) {
        const value = Number(input.value);
        if (Number.isFinite(value)) patch[input.dataset.field] = value;
      }
      patch.save_runs = form.save_runs.checked;
      patch.save_error_artifacts = form.save_error_artifacts.checked;
      await this.saveConfig(patch, saveSampling);
    });
    samplingActions.appendChild(saveSampling);
    sampling.appendChild(samplingActions);
    formRoot.appendChild(sampling);

    const thresholds = el("div", "sysmon-section");
    thresholds.appendChild(el("div", "sysmon-section-title", "告警阈值 (%)"));
    const thresholdInputs = [];
    const thresholdFields = [
      ["显存警告", "warn_vram_percent", config.warn_vram_percent],
      ["显存严重", "crit_vram_percent", config.crit_vram_percent],
      ["内存警告", "warn_ram_percent", config.warn_ram_percent],
      ["内存严重", "crit_ram_percent", config.crit_ram_percent],
    ];
    for (const [label, key, value] of thresholdFields) {
      const field = el("div", "sysmon-field");
      field.appendChild(el("label", null, label));
      const input = document.createElement("input");
      input.type = "number";
      input.min = "1";
      input.max = "100";
      bindInput(input, key, value);
      field.appendChild(input);
      thresholds.appendChild(field);
      thresholdInputs.push(input);
    }
    const thresholdActions = el("div", "sysmon-actions");
    const saveThresholds = el("button", "sysmon-btn", "保存阈值");
    saveThresholds.addEventListener("click", async () => {
      const patch = {};
      for (const input of thresholdInputs) {
        const value = Number(input.value);
        if (Number.isFinite(value)) patch[input.dataset.field] = value;
      }
      await this.saveConfig(patch, saveThresholds);
    });
    thresholdActions.appendChild(saveThresholds);
    thresholds.appendChild(thresholdActions);
    formRoot.appendChild(thresholds);

    const info = el("div", "sysmon-section");
    info.appendChild(el("div", "sysmon-section-title", "状态"));
    const grid = el("div");
    const add = (label, value) => {
      if (!value) return;
      const kv = el("div", "sysmon-kv");
      kv.appendChild(el("span", null, label));
      kv.appendChild(el("span", null, String(value)));
      grid.appendChild(kv);
    };
    add("插件版本", this.state?.version);
    add("GPU 采集后端", this.samplerStats?.gpu_backend);
    add("采集线程", this.samplerStats?.running ? "运行中" : "已停止");
    add("已采集样本", this.samplerStats?.samples);
    add("Python", this.samplerStats?.python);
    info.appendChild(grid);
    formRoot.appendChild(info);

    body.appendChild(formRoot);
    this.settingsRendered = true;
    this.settingsForm = form;
    if (!this.settingsValues) {
      this.settingsValues = {};
      for (const [key, node] of Object.entries(form)) {
        this.settingsValues[key] = node.type === "checkbox" ? node.checked : node.value;
      }
    }
    // Put the caret back where it was, so typing is not interrupted every poll.
    if (savedFocus && form[savedFocus]) {
      try {
        form[savedFocus].focus();
      } catch (err) {
        /* focus is best-effort */
      }
    }
  }

  numberField(label, value, key, min, max) {
    const field = el("div", "sysmon-field");
    field.appendChild(el("label", null, label));
    const input = document.createElement("input");
    input.type = "number";
    input.value = value ?? "";
    input.min = String(min);
    input.max = String(max);
    input.dataset.key = key;
    field.appendChild(input);
    return field;
  }

  showSettingsToast(message, ok) {
    if (!this.settingsToast) return;
    this.settingsToast.replaceChildren();
    this.settingsToast.appendChild(el("div", "sysmon-toast " + (ok ? "is-ok" : "is-err"), message));
  }

  async saveConfig(patch, button) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "保存中…";
    const result = await request("/config", { method: "POST", body: patch });
    button.disabled = false;
    button.textContent = original;
    if (result.data?.ok) {
      this.config = result.data.config;
      toast("设置已保存", "success");
      this.showSettingsToast("设置已保存", true);
    } else {
      toast(result.data?.error || "保存失败", "error");
      this.showSettingsToast(result.data?.error || "保存失败", false);
    }
  }

  // -- actions ----------------------------------------------------------
  async analyze(run, error, button, container = null, question = "", followUp = false) {
    if (!run?.prompt_id) {
      toast("没有可分析的对象", "error");
      return;
    }
    const original = button.textContent;
    button.disabled = true;
    button.innerHTML = '<span class="sysmon-spinner"></span>分析中…';

    const payload = { prompt_id: run.prompt_id, include_graph: true, question, followup: followUp };
    if (error) payload.error = error;

    const result = await request("/analyze", { method: "POST", body: payload });
    button.disabled = false;
    button.textContent = original;

    if (!result.data?.ok) {
      const message = result.data?.error || "分析失败";
      toast(message, "error");
      if (this.tab === "ai") {
        // Persistent for this tab; a one-shot append is wiped by the next poll.
        this.lastAnalysisError = message;
      } else if (container || this.body) {
        (container || this.body).appendChild(el("div", "sysmon-toast is-err", message));
      }
      return;
    }

    const analysis = result.data.analysis;
    this.analysis = analysis;
    this.lastAnalysisError = null;
    const metaParts = [analysis.model, analysis.provider];
    if (analysis.usage?.total_tokens) metaParts.push(analysis.usage.total_tokens + " tokens");
    const meta = metaParts.filter(Boolean).join(" · ");

    // Store, then re-render. Appending here would be wiped by the next poll.
    const storeKey = error
      ? run.prompt_id + ":" + this.errorKey(error, run)
      : run.prompt_id + ":run";
    this.analyses[storeKey] = { text: analysis.text, meta };

    if (followUp || this.tab === "ai") {
      if (!followUp) this.aiChat.push({ text: analysis.text, meta, fromRun: false });
      if (this.tab === "ai") this.renderAi();
    } else {
      this.render();
    }
    toast("AI 分析完成", "success");
  }

  async showContextPreview(run) {
    const result = await request("/context?prompt_id=" + encodeURIComponent(run.prompt_id));
    if (!result.data?.ok) {
      toast("无法获取预览", "error");
      return;
    }
    this.aiPanel = {
      title: "将发送给大模型的数据",
      hint: "以下是完整请求体。不包含图片、提示词正文或模型输出。",
      content: JSON.stringify(result.data.context, null, 2),
    };
    if (this.tab === "ai") this.renderAi();
  }
}

// ---------------------------------------------------------------------------
// Peak helpers
// ---------------------------------------------------------------------------
function findPeakOwner(nodes, metric) {
  let best = null;
  for (const node of nodes) {
    const peak = node.peaks?.[metric];
    if (!peak || peak.value === null || peak.value === undefined) continue;
    if (!best || peak.value > best.peak.value) best = { node, peak };
  }
  return best;
}

function peakText(node, metric, unit, asBytes = false) {
  const peak = node.peaks?.[metric];
  if (!peak) return "-";
  if (asBytes) return fmtBytesMB(peak.value);
  return fmt(peak.value, peak.value < 10 ? 1 : 0) + (unit || "");
}

/** Highlight a node on the canvas, if the frontend exposes a way to do it. */
function locateNode(nodeId) {
  try {
    const app = globalThis.comfyAPI?.app?.app || globalThis.app;
    const graph = app?.graph;
    if (!graph || !nodeId) return;
    const node = graph.getNodeById?.(Number(nodeId)) || graph.getNodeById?.(nodeId);
    if (!node) return;
    graph.clearSelection?.();
    node.is_selected = true;
    node.setDirtyCanvas?.(true, true);
    if (app.canvas?.centerOnNode) app.canvas.centerOnNode(node);
    else if (app.canvas?.ds?.offset) {
      app.canvas.ds.offset[0] = -(node.pos[0] + (node.size?.[0] || 0) / 2) * (app.canvas.ds.scale || 1);
      app.canvas.ds.offset[1] = -(node.pos[1] + (node.size?.[1] || 0) / 2) * (app.canvas.ds.scale || 1);
      app.canvas.setDirty?.(true, true);
    }
  } catch (err) {
    console.debug("[sysmon] could not locate node", err);
  }
}

// ---------------------------------------------------------------------------
// Extension registration
// ---------------------------------------------------------------------------
let panel = null;

function ensurePanel() {
  if (!panel) {
    panel = new SysmonPanel();
    panel.start();
  }
  return panel;
}

/**
 * Inject the stylesheet.
 *
 * The path is derived from this module's own URL rather than hardcoded, because
 * ComfyUI serves an extension either under its folder name or under the
 * pyproject project name. Deriving it keeps both working, and also handles
 * installs behind a URL sub-path.
 */
function ensureStylesheet() {
  if (document.getElementById("sysmon-styles")) return;
  let href;
  try {
    href = new URL("./sysmon.css", import.meta.url).href;
  } catch (err) {
    href = "/extensions/comfyui-sysmon/sysmon.css";
  }
  const link = document.createElement("link");
  link.id = "sysmon-styles";
  link.rel = "stylesheet";
  link.href = href;
  link.addEventListener("error", () => console.warn("[sysmon] stylesheet failed to load from", href));
  document.head.appendChild(link);
}

function registerSettings(app) {
  const settings = app?.ui?.settings;
  if (!settings?.addSetting) return;
  const definitions = [
    {
      id: "sysmon.showPanel",
      name: "[System Monitor] 显示悬浮面板",
      type: "boolean",
      defaultValue: true,
      onChange: (value) => {
        const instance = ensurePanel();
        if (value) instance.show();
        else instance.hide();
      },
    },
    {
      id: "sysmon.startCollapsed",
      name: "[System Monitor] 启动时折叠面板",
      type: "boolean",
      defaultValue: false,
      onChange: (value) => ensurePanel().setCollapsed(Boolean(value)),
    },
  ];
  for (const definition of definitions) {
    try {
      settings.addSetting(definition);
    } catch (err) {
      console.debug("[sysmon] could not register setting", definition.id, err);
    }
  }
}

async function registerExtension() {
  // Styles must exist before the panel is built so it is never briefly unstyled.
  ensureStylesheet();

  let app = null;
  for (const path of ["../../scripts/app.js", "/scripts/app.js"]) {
    try {
      const mod = await import(/* @vite-ignore */ path);
      app = mod?.app || mod?.default?.app || mod?.default;
      if (app?.registerExtension) break;
      app = null;
    } catch (err) {
      /* try the next path */
    }
  }
  if (!app?.registerExtension) {
    // The frontend renamed its module paths: fall back to the global handle.
    app = globalThis.comfyAPI?.app?.app || globalThis.app || null;
  }
  if (!app?.registerExtension) {
    console.warn("[sysmon] ComfyUI app object not found; panel not registered.");
    return;
  }

  app.registerExtension({
    name: "comfyui.sysmon",
    async setup() {
      registerSettings(app);
      const instance = ensurePanel();
      // Respect the host setting when present.
      try {
        const wanted = app.ui?.settings?.getSettingValue?.("sysmon.showPanel");
        if (wanted === false) instance.hide();
      } catch (err) {
        /* keep the panel visible */
      }
    },
  });

  // ComfyUI may already be past the registration phase when a pack is loaded
  // late; make sure the panel still appears.
  if (app.canvas && !document.getElementById("sysmon-panel")) {
    registerSettings(app);
    ensurePanel();
  }
}

registerExtension();
