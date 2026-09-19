/**
 * Diagnose why the history chart renders empty.
 *
 * Checks every link in the chain independently: the canvas element and its
 * measured size, whether the chart instance exists, whether the panel actually
 * received samples, and whether the chart's own draw path produces pixels.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const URL_TO_OPEN = process.argv[2];
const CDP_PORT = Number(process.argv[3] || 9410);
const WAIT_S = Number(process.argv[4] || 40);

const EDGE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].find((p) => existsSync(p));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class Cdp {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.events = [];
    ws.addEventListener("message", (e) => {
      let m; try { m = JSON.parse(e.data); } catch { return; }
      if (m.id !== undefined && this.pending.has(m.id)) { this.pending.get(m.id)(m); this.pending.delete(m.id); }
      else if (m.method) this.events.push(m);
    });
  }
  send(method, params = {}, timeoutMs = 40000) {
    const id = ++this.id;
    return new Promise((res, rej) => {
      const t = setTimeout(() => rej(new Error("timeout " + method)), timeoutMs);
      this.pending.set(id, (m) => { clearTimeout(t); res(m); });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async ev(expression) {
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.result && r.result.exceptionDetails) {
      return { __error: r.result.exceptionDetails.text || JSON.stringify(r.result.exceptionDetails) };
    }
    return r.result && r.result.result ? r.result.result.value : undefined;
  }
}

const profile = mkdtempSync(join(tmpdir(), "sysmon-chart-"));
const child = spawn(EDGE, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--window-size=1600,1000",
  "--remote-debugging-port=" + CDP_PORT, "--user-data-dir=" + profile, "about:blank",
], { stdio: "ignore" });

async function main() {
  let targets = null;
  for (let i = 0; i < 40 && !targets; i++) {
    await sleep(500);
    try { const r = await fetch("http://127.0.0.1:" + CDP_PORT + "/json/list"); if (r.ok) targets = await r.json(); } catch { /* retry */ }
  }
  const page = targets.find((t) => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", rej, { once: true });
  });
  const cdp = new Cdp(ws);
  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
  await cdp.send("Page.navigate", { url: URL_TO_OPEN });
  await sleep(WAIT_S * 1000);

  console.log("=== 1. 面板与画布状态 ===");
  console.log(JSON.stringify(await cdp.ev(`(function () {
    const panel = document.getElementById("sysmon-panel");
    if (!panel) return { panel: false };
    const wrap = panel.querySelector(".sysmon-chart-wrap");
    const canvas = panel.querySelector(".sysmon-chart-wrap canvas");
    const out = { panel: true, wrap: !!wrap, canvas: !!canvas };
    if (wrap) {
      const r = wrap.getBoundingClientRect();
      const cs = getComputedStyle(wrap);
      out.wrapRect = { w: Math.round(r.width), h: Math.round(r.height) };
      out.wrapDisplay = cs.display;
      out.wrapOverflow = cs.overflow;
    }
    if (canvas) {
      const r = canvas.getBoundingClientRect();
      const cs = getComputedStyle(canvas);
      out.canvasRect = { w: Math.round(r.width), h: Math.round(r.height) };
      out.canvasAttr = { w: canvas.width, h: canvas.height };
      out.canvasDisplay = cs.display;
      out.canvasCss = { w: cs.width, h: cs.height };
      out.devicePixelRatio = window.devicePixelRatio;
      out.clientSize = { w: canvas.clientWidth, h: canvas.clientHeight };
      out.offsetSize = { w: canvas.offsetWidth, h: canvas.offsetHeight };
    }
    return out;
  })()`, null, 1)));

  console.log("");
  console.log("=== 2. 面板是否拿到了采样 ===");
  console.log(JSON.stringify(await cdp.ev(`(function () {
    // The panel instance is module-scoped; reach it through the rendered DOM instead.
    const canvas = document.querySelector("#sysmon-panel .sysmon-chart-wrap canvas");
    if (!canvas) return { canvas: false };
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height;
    let data = null, nonBlank = 0;
    try {
      const img = ctx.getImageData(0, 0, w, h).data;
      let sum = 0;
      for (let i = 3; i < img.length; i += 4) if (img[i] > 0) nonBlank++;
      data = { w: w, h: h, nonTransparentPixels: nonBlank, totalPixels: (w * h) };
    } catch (e) { data = { error: String(e) }; }
    return { canvas: true, pixels: data };
  })()`, null, 1)));

  console.log("");
  console.log("=== 3. 直接调后端确认数据仍在 ===");
  console.log(JSON.stringify(await cdp.ev(`(async function () {
    const r = await fetch("/sysmon/series?limit=180");
    const d = await r.json();
    const s = d.samples || [];
    const usable = s.filter(function (x) {
      const g = x.gpu || {};
      return g.mem_total_mb && x.ram_total_mb;
    }).length;
    return { ok: d.ok, samples: s.length, usable: usable,
             first: s.length ? { t: s[0].t, cpu: s[0].cpu_percent } : null,
             last: s.length ? { t: s[s.length-1].t, cpu: s[s.length-1].cpu_percent } : null };
  })()`, null, 1)));

  console.log("");
  console.log("=== 4. 手动在画布上画一条对角线（验证画布本身可用） ===");
  console.log(JSON.stringify(await cdp.ev(`(function () {
    const canvas = document.querySelector("#sysmon-panel .sysmon-chart-wrap canvas");
    if (!canvas) return { canvas: false };
    const ctx = canvas.getContext("2d");
    ctx.strokeStyle = "#ff0000";
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(canvas.width, canvas.height);
    ctx.stroke();
    const img = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    let red = 0;
    for (let i = 0; i < img.length; i += 4) if (img[i] > 200 && img[i+1] < 80) red++;
    return { canvas: true, redPixels: red, canvasW: canvas.width, canvasH: canvas.height };
  })()`, null, 1)));

  console.log("");
  console.log("=== 5. 页面异常 ===");
  const errs = cdp.events.filter((e) => e.method === "Runtime.exceptionThrown");
  if (!errs.length) console.log("  (无)");
  for (const e of errs.slice(0, 8)) {
    console.log("  " + ((e.params.exceptionDetails.exception || {}).description || e.params.exceptionDetails.text || "").split("\n").slice(0, 3).join(" | "));
  }
  const logs = cdp.events.filter((e) => e.method === "Log.entryAdded" && e.params.entry.level === "error");
  console.log("  控制台 error 数: " + logs.length);
  for (const l of logs.slice(0, 6)) console.log("    " + l.params.entry.text.slice(0, 160));

  const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(process.cwd(), "chart-diag.png"), Buffer.from(shot.result.data, "base64"));
  console.log("\n  screenshot: chart-diag.png");

  ws.close(); child.kill(); process.exit(0);
}

main().catch((e) => { console.log("FATAL " + e.message); child.kill(); process.exit(2); })
  .finally(async () => { await sleep(500); try { rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ } });
