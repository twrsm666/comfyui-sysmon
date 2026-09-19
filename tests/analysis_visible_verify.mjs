/**
 * Verify an analysis result actually stays visible on the errors tab.
 *
 * The result used to be appended into the error block, which the polls rebuild
 * once a second, so it flashed and disappeared. The AI call is stubbed so this
 * needs no API key and stays deterministic.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const URL_TO_OPEN = process.argv[2];
const CDP_PORT = Number(process.argv[3] || 9470);
const WAIT_S = Number(process.argv[4] || 40);

const EDGE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].find((p) => existsSync(p));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let pass = 0, fail = 0;
const check = (label, ok, detail = "") => {
  if (ok) { pass++; console.log("  PASS  " + label); }
  else { fail++; console.log("  FAIL  " + label + "  " + detail); }
};

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
    if (r.result && r.result.exceptionDetails) return { __error: r.result.exceptionDetails.text };
    return r.result && r.result.result ? r.result.result.value : undefined;
  }
}

const ANALYSIS = [
  "## 结论",
  "这个报错是 CLIP 编码器与主模型不匹配造成的：文本特征维度 1024 无法与 5376 相乘。",
  "",
  "## 瓶颈分析",
  "- 报错发生在 KSampler (#19)，但根因在上游的 CLIPLoader。",
  "- 显存峰值 8.4 GB（51.8%），内存 56.6%，都不是瓶颈。",
  "",
  "## 优化建议",
  "1. 把加载CLIP的类型从 stable_diffusion 改为对应 anima 的类型。",
  "2. 确认 qwen_3_06b_base 与 anima-base-v1.0 配套。",
  "",
  "## 风险提示",
  "维度不匹配若被绕过会导致输出全黑。",
].join("\n");
const ANALYSIS_B64 = Buffer.from(ANALYSIS, "utf8").toString("base64");

const STUB = `
(function () {
  const ANALYSIS = new TextDecoder().decode(
    Uint8Array.from(atob("${ANALYSIS_B64}"), function (c) { return c.charCodeAt(0); })
  );
  function jsonResponse(body, status) {
    return new Response(JSON.stringify(body), { status: status || 200, headers: { "Content-Type": "application/json" } });
  }
  function intercept(url, init) {
    if (String(url).indexOf("/sysmon/analyze") === -1) return null;
    return new Promise(function (resolve) {
      setTimeout(function () {
        resolve(jsonResponse({ ok: true, analysis: {
          text: ANALYSIS, model: "deepseek-chat", provider: "deepseek",
          usage: { total_tokens: 800 }, at: Math.floor(Date.now() / 1000), had_error: true,
          context: { comfyui_run: { status: "error" } },
        }}));
      }, 700);
    });
  }
  const orig = window.fetch.bind(window);
  window.fetch = function (u, i) { return intercept(u, i) || orig(u, i); };
  return 1;
})()
`;

const profile = mkdtempSync(join(tmpdir(), "sysmon-analysis-"));
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
  await cdp.ev(STUB);

  // Seed a failing run so the errors tab has something to analyse.
  await cdp.ev(`(async function () {
    await fetch("/prompt", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: "analysis-verify", prompt: {
        "1": { class_type: "SysmonFailNode", inputs: { seed: 3 } } },
        extra_data: { extra_pnginfo: { workflow: { name: "analysis-verify", nodes: [
          { id: 1, type: "SysmonFailNode", title: "故意失败节点" }] } } } }) });
    return 1;
  })()`);
  await sleep(8000);

  await cdp.ev('[...document.querySelectorAll("#sysmon-panel .sysmon-tab")].find(function(b){return b.textContent.trim()==="报错";}).click(),1');
  await sleep(3000);

  const before = await cdp.ev(`(function () {
    const body = document.querySelector("#sysmon-panel .sysmon-body");
    const btns = [...body.querySelectorAll("button")].map(function (b) { return b.textContent.trim(); });
    return { hasError: body.textContent.indexOf("RuntimeError") >= 0 && body.textContent.indexOf("故意失败节点") >= 0,
             hasAnalyzeBtn: btns.some(function (b) { return b.indexOf("AI 分析这个报错") >= 0; }),
             buttons: btns };
  })()`);
  console.log("  报错页状态: " + JSON.stringify(before));
  check("errors tab shows the captured error", before.hasError === true, JSON.stringify(before));
  check("errors tab offers the analyse button", before.hasAnalyzeBtn === true, JSON.stringify(before.buttons));

  // Click it.
  const clicked = await cdp.ev(`(function () {
    const b = [...document.querySelectorAll("#sysmon-panel .sysmon-body button")]
      .find(function (x) { return x.textContent.indexOf("AI 分析这个报错") >= 0; });
    if (!b) return { found: false };
    b.click();
    return { found: true };
  })()`);
  check("analyse button clicked", clicked.found === true, JSON.stringify(clicked));

  // Poll for the result, then confirm it PERSISTS across several re-renders.
  let seen = null;
  for (let i = 0; i < 20 && !seen; i++) {
    await sleep(400);
    seen = await cdp.ev(`(function () {
      const body = document.querySelector("#sysmon-panel .sysmon-body");
      const ai = body.querySelector(".sysmon-ai");
      return ai ? { headings: [...ai.querySelectorAll("h2")].map(function (h) { return h.textContent.trim(); }),
                   len: ai.textContent.length } : null;
    })()`);
  }
  check("analysis appears after the call", seen !== null, JSON.stringify(seen));
  if (seen) {
    check("analysis has markdown headings", seen.headings.indexOf("结论") >= 0, JSON.stringify(seen.headings));
  }

  const samples = [];
  for (let i = 0; i < 5; i++) {
    await sleep(1000);
    samples.push(await cdp.ev(`(function () {
      const ai = document.querySelector("#sysmon-panel .sysmon-body .sysmon-ai");
      return ai ? ai.textContent.length : 0;
    })()`));
  }
  console.log("  5 秒内每帧的分析文本长度: " + JSON.stringify(samples));
  check("analysis survives every re-render", samples.every((v) => v > 100), JSON.stringify(samples));

  const errs = cdp.events.filter((e) => e.method === "Runtime.exceptionThrown");
  check("no JS exceptions", errs.length === 0, String(errs.length));

  const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(process.cwd(), "analysis-visible.png"), Buffer.from(shot.result.data, "base64"));
  console.log("  screenshot: analysis-visible.png");

  console.log("\npassed: " + pass + "   failed: " + fail);
  ws.close(); child.kill();
  process.exit(fail ? 1 : 0);
}

main().catch((e) => { console.log("FATAL " + e.message); child.kill(); process.exit(2); })
  .finally(async () => { await sleep(500); try { rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ } });
