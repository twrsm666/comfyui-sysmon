/**
 * UI test for the AI analysis path using a stubbed network response.
 *
 * The success path can never be exercised without a valid API key, so the page's
 * fetch is wrapped to answer POST /sysmon/analyze with a realistic payload. That
 * verifies the whole client side: busy state, Markdown rendering, metadata,
 * follow-up UI, and the error branch.
 *
 * The analysis text is passed in as base64 and decoded in the page, which keeps
 * the injected script free of nested backticks and of any non-ASCII source.
 *
 *   node ai_ui_verify.mjs <url> [cdpPort] [waitSeconds]
 */

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const URL_TO_OPEN = process.argv[2];
const CDP_PORT = Number(process.argv[3] || 9400);
const WAIT_S = Number(process.argv[4] || 45);

const EDGE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].find((p) => existsSync(p));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let pass = 0;
let fail = 0;
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
    if (r.result && r.result.exceptionDetails) {
      throw new Error(r.result.exceptionDetails.text || "evaluate failed");
    }
    return r.result && r.result.result ? r.result.result.value : undefined;
  }
}

const ANALYSIS = [
  "## 结论",
  "本次运行正常完成，显存峰值 9.4 GB，未触及 16 GB 上限。",
  "",
  "## 瓶颈分析",
  "- **高斯模糊** 耗时最长（0.50 s），显存峰值也出现在该节点。",
  "- GPU 利用率偏低（0%），说明该节点主要是 CPU 侧调度。",
  "",
  "## 优化建议",
  "1. 把 batch_size 从 2 提到 3，显存仍有约 6 GB 余量。",
  "2. 用 `ImageScale` 先降分辨率再做模糊，可省约 40% 时间。",
  "",
  "## 风险提示",
  "继续提高 batch_size 最终会触发 OOM。",
].join("\n");

const ANALYSIS_B64 = Buffer.from(ANALYSIS, "utf8").toString("base64");

const STUB_SCRIPT = `
(function () {
  window.__sysmonCalls = [];
  const ANALYSIS = new TextDecoder().decode(
    Uint8Array.from(atob("${ANALYSIS_B64}"), function (c) { return c.charCodeAt(0); })
  );

  function jsonResponse(body, status) {
    return new Response(JSON.stringify(body), {
      status: status || 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  function intercept(url, init) {
    const u = String(url);
    if (u.indexOf("/sysmon/analyze") === -1) return null;
    const payload = init && init.body ? JSON.parse(init.body) : {};
    window.__sysmonCalls.push(payload);
    if (window.__sysmonForceError) {
      return Promise.resolve(jsonResponse({ ok: false, error: "API Key 无效或已过期（401）。" }, 400));
    }
    return new Promise(function (resolve) {
      setTimeout(function () {
        resolve(jsonResponse({
          ok: true,
          analysis: {
            text: ANALYSIS,
            model: "deepseek-chat",
            provider: "deepseek",
            usage: { total_tokens: 512 },
            at: Math.floor(Date.now() / 1000),
            had_error: false,
            context: { comfyui_run: { status: "success" } },
          },
        }, 200));
      }, 900);
    });
  }

  let wrapped = false;
  const appApi = globalThis.comfyAPI && comfyAPI.app && comfyAPI.app.api;
  if (appApi && appApi.fetchApi) {
    const orig = appApi.fetchApi.bind(appApi);
    appApi.fetchApi = function (url, init) { return intercept(url, init) || orig(url, init); };
    wrapped = true;
  }
  const origFetch = window.fetch.bind(window);
  window.fetch = function (url, init) { return intercept(url, init) || origFetch(url, init); };
  return { wrapped: wrapped, hasApi: !!appApi };
})()
`;

const SEED_SCRIPT = `
(async function () {
  await fetch("/prompt", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      client_id: "ai-ui",
      prompt: {
        "1": { class_type: "EmptyImage", inputs: { width: 1200, height: 1200, batch_size: 1, color: 0 } },
        "2": { class_type: "ImageBlur", inputs: { image: ["1", 0], blur_radius: 12, sigma: 5.0 } },
        "3": { class_type: "PreviewImage", inputs: { images: ["2", 0] } },
      },
      extra_data: {
        extra_pnginfo: {
          workflow: {
            name: "AI-UI-test",
            nodes: [
              { id: 1, type: "EmptyImage", title: "gen" },
              { id: 2, type: "ImageBlur", title: "blur" },
              { id: 3, type: "PreviewImage", title: "preview" },
            ],
          },
        },
      },
    }),
  });
  return 1;
})()
`;

const profile = mkdtempSync(join(tmpdir(), "sysmon-ai-"));
const child = spawn(EDGE, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--window-size=1500,1000",
  "--remote-debugging-port=" + CDP_PORT, "--user-data-dir=" + profile, "about:blank",
], { stdio: "ignore" });

async function main() {
  let targets = null;
  for (let i = 0; i < 40 && !targets; i++) {
    await sleep(500);
    try {
      const r = await fetch("http://127.0.0.1:" + CDP_PORT + "/json/list");
      if (r.ok) targets = await r.json();
    } catch { /* retry */ }
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
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1500, height: 1000, deviceScaleFactor: 1, mobile: false });
  await cdp.send("Page.navigate", { url: URL_TO_OPEN });
  await sleep(WAIT_S * 1000);

  console.log("=== AI path with a stubbed successful response ===");
  console.log("  stub:", JSON.stringify(await cdp.ev(STUB_SCRIPT)));

  await cdp.ev(SEED_SCRIPT);
  await sleep(6000);

  await cdp.ev('[...document.querySelectorAll("#sysmon-panel .sysmon-tab")].find(function(b){return b.textContent.trim()==="AI";}).click(),1');
  await sleep(3500);

  const before = await cdp.ev(`(function () {
    const p = document.getElementById("sysmon-panel");
    return {
      buttons: [...p.querySelectorAll(".sysmon-body button")].map(function (b) { return b.textContent.trim(); }),
      hasOutput: !!document.getElementById("sysmon-ai-output"),
    };
  })()`);
  check("AI tab shows an analyse button", before.buttons.some((b) => b.indexOf("分析") >= 0), JSON.stringify(before.buttons));
  check("AI tab shows the preview button", before.buttons.some((b) => b.indexOf("查看") >= 0), JSON.stringify(before.buttons));
  check("AI output container exists", before.hasOutput === true);

  // context preview (real endpoint)
  await cdp.ev(`(function () {
    const b = [...document.querySelectorAll("#sysmon-panel .sysmon-body button")].find(function (x) { return x.textContent.indexOf("查看") >= 0; });
    if (b) b.click();
    return 1;
  })()`);
  await sleep(2500);
  const preview = await cdp.ev(`(function () {
    const t = document.querySelector("#sysmon-panel .sysmon-body").textContent;
    return { heading: t.indexOf("将发送给大模型的数据") >= 0, privacy: t.indexOf("不包含图片") >= 0, json: t.indexOf("comfyui_run") >= 0 };
  })()`);
  check("context preview renders", preview.heading === true, JSON.stringify(preview));
  check("context preview states the privacy boundary", preview.privacy === true);
  check("context preview shows the JSON payload", preview.json === true, JSON.stringify(preview));

  // analyse (stubbed). The panel re-renders on every 1 s poll, which wipes
  // transient DOM, so the checks below only assert things that survive a
  // re-render, or read state captured synchronously right after the click.
  const clicked = await cdp.ev(`(function () {
    const btns = [...document.querySelectorAll("#sysmon-panel .sysmon-body button")];
    const b = btns.find(function (x) { return x.textContent.indexOf("分析最近一次运行") >= 0; })
           || btns.find(function (x) { return x.textContent.indexOf("分析") >= 0; });
    if (!b) return { found: false, labels: btns.map(function (x) { return x.textContent.trim(); }) };
    b.click();
    // Captured in the same task, before any await, so this is the real busy state.
    const busy = [...document.querySelectorAll("#sysmon-panel .sysmon-body button")]
      .find(function (x) { return x.textContent.indexOf("分析中") >= 0; });
    return {
      found: true,
      busyLabel: busy ? busy.textContent.trim() : null,
      busyDisabled: busy ? busy.disabled : null,
      spinner: !!document.querySelector("#sysmon-panel .sysmon-spinner"),
    };
  })()`);
  check("analyse button clicked", clicked.found === true, JSON.stringify(clicked));
  check("button enters a busy state", clicked.busyLabel !== null, JSON.stringify(clicked));
  check("busy button is disabled", clicked.busyDisabled === true, JSON.stringify(clicked));
  check("spinner is shown while analysing", clicked.spinner === true, JSON.stringify(clicked));

  await sleep(4000);
  const after = await cdp.ev(`(function () {
    const body = document.querySelector("#sysmon-panel .sysmon-body");
    const ai = body.querySelector(".sysmon-ai");
    return {
      hasAiBox: !!ai,
      headings: ai ? [...ai.querySelectorAll("h2")].map(function (h) { return h.textContent.trim(); }) : [],
      listItems: ai ? ai.querySelectorAll("li").length : 0,
      hasCode: !!ai && !!ai.querySelector("code"),
      hasBold: !!ai && !!ai.querySelector("strong"),
      errToast: !!body.querySelector(".sysmon-toast.is-err"),
      bodyText: body.textContent.replace(/\s+/g, " ").slice(0, 200),
    };
  })()`);
  check("analysis box rendered", after.hasAiBox === true, JSON.stringify(after));
  check("markdown headings rendered", JSON.stringify(after.headings).indexOf("结论") >= 0,
    JSON.stringify(after.headings));
  check("markdown list items rendered", after.listItems >= 4, String(after.listItems));
  check("inline code rendered", after.hasCode === true);
  check("bold text rendered", after.hasBold === true);
  check("no error toast on success", after.errToast === false, JSON.stringify(after));

  const calls = await cdp.ev("window.__sysmonCalls");
  check("request sent to /analyze", Array.isArray(calls) && calls.length >= 1, JSON.stringify(calls));
  check("request carried the prompt id", !!(calls && calls[0] && calls[0].prompt_id), JSON.stringify(calls && calls[0]));
  check("request asked for the graph", !!(calls && calls[0] && calls[0].include_graph === true));

  // error branch: same flow, stubbed failure. The toast is transient too, so it
  // is polled for rather than sampled once.
  await cdp.ev("window.__sysmonForceError = true, 1");
  await cdp.ev(`(function () {
    const btns = [...document.querySelectorAll("#sysmon-panel .sysmon-body button")];
    const b = btns.find(function (x) { return x.textContent.indexOf("分析最近一次运行") >= 0; })
           || btns.find(function (x) { return x.textContent.indexOf("分析") >= 0; });
    if (b) b.click();
    return 1;
  })()`);
  let errText = null;
  for (let i = 0; i < 20 && !errText; i++) {
    await sleep(400);
    errText = await cdp.ev(`(function () {
      const t = document.querySelector("#sysmon-panel .sysmon-body .sysmon-toast.is-err");
      return t ? t.textContent.trim() : null;
    })()`);
  }
  check("error branch shows a toast", errText !== null, String(errText));
  check("error text comes from the server", (errText || "").indexOf("401") >= 0, String(errText));

  const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(process.cwd(), "ai-path.png"), Buffer.from(shot.result.data, "base64"));
  console.log("  screenshot: ai-path.png");

  const errs = cdp.events.filter((e) => e.method === "Runtime.exceptionThrown");
  check("no JS exceptions during the AI path", errs.length === 0,
    errs.map((e) => ((e.params.exceptionDetails.exception || {}).description || "").split("\n")[0]).join(" | "));

  console.log("\npassed: " + pass + "   failed: " + fail);
  ws.close();
  child.kill();
  process.exit(fail ? 1 : 0);
}

main().catch((e) => {
  console.log("FATAL " + e.message);
  child.kill();
  process.exit(2);
}).finally(async () => {
  await sleep(500);
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ }
});
