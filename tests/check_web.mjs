/**
 * Static checks for the System Monitor web bundle.
 *
 * Catches the two failure modes that are invisible until a user interacts with
 * the panel, because the browser only reports them as "Failed to load
 * extension" or a silent ReferenceError on a tab click:
 *
 *   1. syntax errors / duplicate declarations (the first version of panel.js
 *      declared `saveBtn` twice and the whole extension refused to load)
 *   2. calls to helpers that were never defined (`checkRow()` was called three
 *      times but never written, breaking the settings tab on click)
 *
 * Also verifies relative imports resolve.
 *
 *   node tests/check_web.mjs
 */

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB_DIR = resolve(HERE, "..", "web");

let failed = 0;

function check(label, ok, detail = "") {
  if (ok) {
    console.log(`  PASS  ${label}`);
  } else {
    failed++;
    console.log(`  FAIL  ${label}  ${detail}`);
  }
}

const files = readdirSync(WEB_DIR).filter((f) => f.endsWith(".js"));
console.log(`Checking ${files.length} web module(s) in ${WEB_DIR}\n`);

for (const file of files) {
  const path = join(WEB_DIR, file);
  const source = readFileSync(path, "utf8");
  console.log(`--- ${file} (${source.length} bytes)`);

  // 1. Parse as an ES module: catches duplicate declarations and syntax errors.
  let parseError = null;
  try {
    // A data: URL forces module parsing without resolving relative imports.
    new Function(`return import("data:text/javascript;base64,${Buffer.from(source).toString("base64")}")`);
  } catch (err) {
    parseError = err.message;
  }
  // new Function only wraps; use a real parse via dynamic import of a temp copy
  // when the wrapper reports an error, to get an accurate message.
  if (!parseError) {
    const { writeFileSync, mkdtempSync, rmSync } = await import("node:fs");
    const { tmpdir } = await import("node:os");
    const tmp = mkdtempSync(join(tmpdir(), "sysmon-parse-"));
    const tmpFile = join(tmp, file);
    writeFileSync(tmpFile, source);
    try {
      await import(`file://${tmpFile.replace(/\\/g, "/")}`);
    } catch (err) {
      // Missing host modules are expected outside ComfyUI; only syntax counts.
      if (err instanceof SyntaxError) parseError = err.message;
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  }
  check(`${file} parses as a module`, parseError === null, parseError || "");

  // 2. Balanced braces/parens as a cheap extra signal.
  const count = (re) => (source.match(re) || []).length;
  check(
    `${file} has balanced braces`,
    count(/\{/g) === count(/\}/g),
    `{ =${count(/\{/g)} } =${count(/\}/g)}`,
  );
  check(
    `${file} has balanced parens`,
    count(/\(/g) === count(/\)/g),
    `( =${count(/\(/g)} ) =${count(/\)/g)}`,
  );

  // 3. Relative imports must point at files that exist.
  const imports = [...source.matchAll(/from\s+["'](\.[^"']+)["']/g)].map((m) => m[1]);
  for (const spec of imports) {
    const target = resolve(dirname(path), spec);
    let exists = true;
    try {
      readFileSync(target);
    } catch {
      exists = false;
    }
    check(`${file} import ${spec} resolves`, exists, target);
  }

  // 4. Every called helper must exist somewhere in the module (or be a known
  //    global / method call). This catches calls to functions that were never
  //    defined - which only surface as a runtime ReferenceError when the user
  //    clicks the relevant tab, exactly how checkRow() slipped through once.
  //
  //    Strings, template literals and comments are stripped first: matching raw
  //    source produced false hits on `rgba(${r},...)` inside a template literal
  //    and on prose containing a word followed by a bracket.
  const code = stripNonCode(source);

  const declared = new Set();
  for (const m of code.matchAll(/(?:^|\s)(?:function|class)\s+([A-Za-z_$][\w$]*)/g)) {
    declared.add(m[1]);
  }
  for (const m of code.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=/g)) {
    declared.add(m[1]);
  }
  // Destructuring / multi-declarations: `const a = 1, b = 2;`
  for (const m of code.matchAll(/,\s*([A-Za-z_$][\w$]*)\s*=/g)) {
    declared.add(m[1]);
  }
  for (const m of code.matchAll(/^\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{/gm)) {
    declared.add(m[1]); // class methods
  }
  for (const m of code.matchAll(/(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)/g)) {
    declared.add(m[1]);
  }
  for (const m of source.matchAll(/import\s*\{([^}]+)\}/g)) {
    for (const name of m[1].split(",")) {
      const clean = name.trim().split(/\s+as\s+/).pop().trim();
      if (clean) declared.add(clean);
    }
  }
  // Anything assigned as an object key or property is treated as known too.
  for (const m of code.matchAll(/([A-Za-z_$][\w$]*)\s*:/g)) {
    declared.add(m[1]);
  }

  const KNOWN_GLOBALS = new Set([
    "console", "document", "window", "globalThis", "Math", "JSON", "Object", "Array",
    "String", "Number", "Boolean", "Promise", "Set", "Map", "Date", "Error", "URL",
    "fetch", "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "localStorage", "navigator", "requestAnimationFrame", "parseInt", "parseFloat",
    "isNaN", "isFinite", "encodeURIComponent", "decodeURIComponent", "structuredClone",
    "Blob", "Image", "WebSocket", "TextEncoder", "TextDecoder", "RegExp", "Symbol",
    "if", "for", "while", "switch", "catch", "return", "typeof", "function", "await",
    "new", "super", "this", "constructor", "get", "set", "of", "in", "do", "else",
    "try", "finally", "throw", "delete", "void", "yield", "case", "default", "break",
    "continue", "instanceof", "import", "export", "class", "extends", "static", "async",
    "for", "while", "with", "debugger",
  ]);

  const called = new Set();
  // Only a bare identifier immediately followed by "(" counts as a call.
  for (const m of code.matchAll(/(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(/g)) {
    called.add(m[1]);
  }
  const unresolved = [...called].filter(
    (name) => !declared.has(name) && !KNOWN_GLOBALS.has(name),
  );
  check(
    `${file} has no calls to undefined local helpers`,
    unresolved.length === 0,
    unresolved.join(", "),
  );

  // 5. Visibility contract.
  //    The launcher is only visible while the panel is hidden, so its click
  //    handler must call show(). It previously called setCollapsed(false),
  //    which uncollapses but never clears `is-hidden` - leaving the panel
  //    unreachable with no way to get it back. That is a *wrong* call rather
  //    than an undefined one, so rule 4 cannot see it.
  const launcherHandler = source.match(
    /\.launcher\.addEventListener\(\s*["']click["']\s*,\s*([^;]+?)\);/s,
  );
  if (launcherHandler) {
    const body = launcherHandler[1];
    check(
      `${file} launcher click calls show()`,
      /\bshow\s*\(/.test(body),
      `handler was: ${body.trim().slice(0, 80)}`,
    );
    check(
      `${file} launcher click does not merely uncollapse`,
      !/setCollapsed/.test(body),
      `handler was: ${body.trim().slice(0, 80)}`,
    );
  }
  // hide() must add is-hidden and reveal the launcher; show() must undo both.
  const hideFn = source.match(/\bhide\s*\(\s*\)\s*\{([\s\S]*?)\n  \}/);
  const showFn = source.match(/\bshow\s*\(\s*\)\s*\{([\s\S]*?)\n  \}/);
  if (hideFn) {
    check(`${file} hide() adds is-hidden`, /is-hidden/.test(hideFn[1]) && /add\(/.test(hideFn[1]),
      hideFn[1].trim().slice(0, 80));
    check(`${file} hide() reveals the launcher`, /launcher/.test(hideFn[1]) && /is-visible/.test(hideFn[1]),
      hideFn[1].trim().slice(0, 80));
  }
  if (showFn) {
    check(`${file} show() removes is-hidden`, /is-hidden/.test(showFn[1]) && /remove\(/.test(showFn[1]),
      showFn[1].trim().slice(0, 80));
    check(`${file} show() hides the launcher`, /launcher/.test(showFn[1]) && /remove\(/.test(showFn[1]),
      showFn[1].trim().slice(0, 80));
  }

  // 6. The chart canvas must outlive a re-render.
  //    This tab rebuilds its DOM on every poll via replaceChildren(). Creating
  //    the canvas fresh each time left MetricChart drawing into a detached node,
  //    so the visible canvas was never painted: the history curve was blank and
  //    no exception was ever raised. The canvas therefore has to live in
  //    instance state and be re-appended rather than re-created.
  if (/sysmon-chart-wrap/.test(source)) {
    check(
      `${file} chart canvas is cached on the instance`,
      /this\.chartCanvas/.test(source),
      "canvas looks like it is created per render; the chart would draw into a detached node",
    );
    // The chart DOM is built in the first part of renderLive(); take a window
    // from the method start rather than trying to balance nested method braces.
    const start = source.indexOf("renderLive(");
    if (start >= 0) {
      const window2100 = source.slice(start, start + 2100);
      const chartPart = window2100.slice(0, window2100.indexOf("MetricChart") + 120);
      check(
        `${file} renderLive does not create a fresh canvas`,
        !/createElement\(\s*["']canvas["']\s*\)/.test(chartPart),
        "renderLive creates a canvas, which detaches the one the chart holds",
      );
    }
  }
}

/**
 * Blank out string literals, template literals and comments so identifier
 * scanning only sees real code. Length is preserved so nothing shifts.
 */
function stripNonCode(src) {
  let out = "";
  let i = 0;
  const n = src.length;
  while (i < n) {
    const c = src[i];
    const next = src[i + 1];
    // Line comment
    if (c === "/" && next === "/") {
      while (i < n && src[i] !== "\n") out += " ", i++;
      continue;
    }
    // Block comment
    if (c === "/" && next === "*") {
      out += "  ";
      i += 2;
      while (i < n && !(src[i] === "*" && src[i + 1] === "/")) out += src[i] === "\n" ? "\n" : " ", i++;
      if (i < n) out += "  ", i += 2;
      continue;
    }
    // Template literal (handles ${...} by scanning to the matching backtick)
    if (c === "`") {
      out += " ";
      i++;
      let depth = 0;
      while (i < n) {
        if (src[i] === "\\") {
          out += "  ";
          i += 2;
          continue;
        }
        if (src[i] === "$" && src[i + 1] === "{") {
          depth++;
          out += "  ";
          i += 2;
          continue;
        }
        if (depth > 0 && src[i] === "}") {
          depth--;
          out += " ";
          i++;
          continue;
        }
        if (depth === 0 && src[i] === "`") {
          out += " ";
          i++;
          break;
        }
        out += src[i] === "\n" ? "\n" : " ";
        i++;
      }
      continue;
    }
    // Single / double quoted strings
    if (c === '"' || c === "'") {
      const quote = c;
      out += " ";
      i++;
      while (i < n) {
        if (src[i] === "\\") {
          out += "  ";
          i += 2;
          continue;
        }
        if (src[i] === quote) {
          out += " ";
          i++;
          break;
        }
        out += src[i] === "\n" ? "\n" : " ";
        i++;
      }
      continue;
    }
    // Regex literal - only when clearly in a value position.
    if (c === "/" && /[=(,:[!&|?{};+\-*%<>~\n]\s*$/.test(out)) {
      out += " ";
      i++;
      let inClass = false;
      while (i < n) {
        if (src[i] === "\\") {
          out += "  ";
          i += 2;
          continue;
        }
        if (src[i] === "[") inClass = true;
        else if (src[i] === "]") inClass = false;
        else if (src[i] === "/" && !inClass) {
          out += " ";
          i++;
          break;
        } else if (src[i] === "\n") break;
        out += " ";
        i++;
      }
      continue;
    }
    out += c;
    i++;
  }
  return out;
}

console.log(`\n${"=".repeat(46)}`);
console.log(failed === 0 ? "web checks: all passed" : `web checks: ${failed} FAILED`);
console.log("=".repeat(46));
process.exit(failed === 0 ? 0 : 1);
