/**
 * Verify the settings form actually works end to end:
 *   1. typed text survives the 1 s re-render
 *   2. the caret is not stolen while typing
 *   3. values survive a tab switch
 *   4. clicking 保存 submits exactly what was typed
 */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const URL_TO_OPEN = process.argv[2];
const CDP_PORT = Number(process.argv[3] || 9430);
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

const profile = mkdtempSync(join(tmpdir(), "sysmon-form-"));
const child = spawn(EDGE, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--window-size=1600,1000",
  "--remote-debugging-port=" + CDP_PORT, "--user-data-dir=" + profile, "about:blank",
], { stdio: "ignore" });

const goto = (label) =>
  `[...document.querySelectorAll("#sysmon-panel .sysmon-tab")].find(function(b){return b.textContent.trim()===${JSON.stringify(label)};}).click(),1`;

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

  console.log("=== 设置表单可用性 ===");
  await cdp.ev(goto("设置"));
  await sleep(2500);

  // Type the way a user does: real focus first, then input events.
  const typed = await cdp.ev(`(function () {
    const p = document.getElementById("sysmon-panel");
    const key = p.querySelector('.sysmon-body input[type=password]');
    const base = [...p.querySelectorAll('.sysmon-body input[type=text]')][0];
    if (!key || !base) return { ok: false };
    key.focus();
    key.dispatchEvent(new FocusEvent("focus", { bubbles: false }));
    const text = "sk-typing-test-abcdefghijklmnop";
    for (const ch of text) {
      key.value += ch;
      key.dispatchEvent(new InputEvent("input", { bubbles: true, data: ch }));
    }
    base.value = "https://api.deepseek.com";
    base.dispatchEvent(new InputEvent("input", { bubbles: true }));
    return { ok: true, key: key.value, base: base.value, focused: document.activeElement === key };
  })()`);
  console.log("  输入后: " + JSON.stringify(typed));
  check("typing lands in the field", typed.ok === true && typed.key.endsWith("mnop"), JSON.stringify(typed));

  // Survive several re-renders.
  for (let i = 1; i <= 4; i++) {
    await sleep(1000);
    const snap = await cdp.ev(`(function () {
      const p = document.getElementById("sysmon-panel");
      const key = p.querySelector('.sysmon-body input[type=password]');
      const base = [...p.querySelectorAll('.sysmon-body input[type=text]')][0];
      return { key: key ? key.value : null, base: base ? base.value : null,
               focused: document.activeElement === key };
    })()`);
    if (i === 1) {
      check("key survives the first re-render", snap.key === typed.key, JSON.stringify(snap.key));
      check("base URL survives the first re-render", snap.base === "https://api.deepseek.com", JSON.stringify(snap.base));
    }
    if (i === 4) {
      check("key still intact after 4 renders", snap.key === typed.key, JSON.stringify(snap.key));
      check("caret is not stolen while typing", snap.focused === true, "focused=" + snap.focused);
    }
  }

  // Tab switch and back.
  await cdp.ev(goto("实时"));
  await sleep(1500);
  await cdp.ev(goto("设置"));
  await sleep(1500);
  const afterTabs = await cdp.ev(`(function () {
    const p = document.getElementById("sysmon-panel");
    const key = p.querySelector('.sysmon-body input[type=password]');
    return key ? key.value : null;
  })()`);
  check("values survive a tab switch", afterTabs === typed.key, JSON.stringify(afterTabs));

  // Save must submit exactly what was typed. Intercept to observe the payload.
  await cdp.ev(`(function () {
    window.__cfgPosts = [];
    const o = window.fetch.bind(window);
    window.fetch = function (u, i) {
      if (String(u).indexOf("/sysmon/config") >= 0 && i && i.method === "POST") {
        window.__cfgPosts.push(JSON.parse(i.body));
      }
      return o(u, i);
    };
    return 1;
  })()`);
  await cdp.ev(`(function () {
    const b = [...document.querySelectorAll("#sysmon-panel .sysmon-body button")].find(function (x) { return x.textContent.trim() === "保存"; });
    if (b) b.click();
    return 1;
  })()`);
  await sleep(2500);
  const posts = await cdp.ev("window.__cfgPosts");
  const last = Array.isArray(posts) && posts.length ? posts[posts.length - 1] : null;
  console.log("  保存提交的内容: " + JSON.stringify(last));
  check("save sent the typed key", !!last && last.llm_api_key === typed.key, JSON.stringify(last));
  check("save sent the base URL", !!last && last.llm_base_url === "https://api.deepseek.com", JSON.stringify(last));

  const cleared = await cdp.ev(`(function () {
    const p = document.getElementById("sysmon-panel");
    const key = p.querySelector('.sysmon-body input[type=password]');
    return key ? key.value : null;
  })()`);
  check("key field is cleared after saving", cleared === "", JSON.stringify(cleared));

  // Server should now report a saved key.
  const state = await cdp.ev(`(async function () {
    const d = await (await fetch("/sysmon/config")).json();
    return { keySet: d.config.llm_api_key_set, masked: d.config.llm_api_key, base: d.config.llm_base_url };
  })()`);
  console.log("  服务端配置: " + JSON.stringify(state));
  check("server reports the key as set", state.keySet === true, JSON.stringify(state));
  check("server never returns the raw key", state.masked !== typed.key, String(state.masked));

  const errs = cdp.events.filter((e) => e.method === "Runtime.exceptionThrown");
  check("no JS exceptions", errs.length === 0,
    errs.map((e) => ((e.params.exceptionDetails.exception || {}).description || "").split("\n")[0]).join(" | "));

  const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(process.cwd(), "settings-typing.png"), Buffer.from(shot.result.data, "base64"));

  console.log("\npassed: " + pass + "   failed: " + fail);
  ws.close(); child.kill();
  process.exit(fail ? 1 : 0);
}

main().catch((e) => { console.log("FATAL " + e.message); child.kill(); process.exit(2); })
  .finally(async () => { await sleep(500); try { rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ } });
