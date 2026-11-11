// Regenerates the README pictures in docs/screenshots/ from the simulation.
//
//   node scripts/screenshots.mjs [--out DIR] [--python EXE] [--chrome EXE]
//
// It starts `readme-monitor ui --demo` (invented sample repositories; nothing is sent to GitHub or
// Anthropic), plays a dry run and two live runs through the interface's own API, and captures the
// page in the installed Chrome or Edge through the DevTools protocol. Node 22 or newer has the
// WebSocket and fetch this needs, so nothing is installed. Windows only: the setup picture shows the
// platform. No picture is saved unless every run succeeded and every page shows the simulation
// banner and fits its width.
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SHOTS = [ // file, width, height, view (the label of the header button that opens it)
  ["workbench.png", 1920, 1080, "Workbench"],
  ["workbench-4k.png", 3840, 2160, "Workbench"],
  ["workbench-mobile.png", 390, 844, "Workbench"],
  ["setup.png", 1920, 1080, "Setup"],
];
// Ctrl+C cannot be sent to a child process on Windows, so a closed stdin stands in for it: the
// simulation then stops as usual and removes its temporary folder.
const DEMO = `import sys, threading, _thread
threading.Thread(target=lambda: (sys.stdin.read(), _thread.interrupt_main()), daemon=True).start()
from readme_monitor import cli
sys.exit(cli.main(["ui", "--demo", "--no-browser", "--port", "0"]))`;
// The interface's own calls, run inside the signed-in page.
const API = `(async (path, body) => {
  const init = body && { method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json",
    "X-RM-CSRF": (await (await fetch("/api/session")).json()).csrf } };
  const r = await fetch(path, init);
  if (!r.ok) throw new Error(path + " answered HTTP " + r.status);
  return r.json();
})`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms, what) {
  for (const end = Date.now() + ms; Date.now() < end; await sleep(200)) {
    const v = await fn();
    if (v) return v;
  }
  throw new Error(what);
}

let server, chrome, profile;
try {
  const { values: opt } = parseArgs({ options: { out: { type: "string" }, python: { type: "string" }, chrome: { type: "string" } } });
  if (process.platform !== "win32") throw new Error("run this on Windows: the setup picture shows the platform");
  const out = resolve(opt.out ?? join(ROOT, "docs", "screenshots"));
  const venv = join(ROOT, ".venv", "Scripts", "python.exe");
  const python = opt.python ?? (existsSync(venv) ? venv : "python");
  const browser = opt.chrome ?? process.env.CHROME ?? [process.env.ProgramFiles, process.env["ProgramFiles(x86)"], process.env.LOCALAPPDATA]
    .filter(Boolean).flatMap((d) => [join(d, "Google", "Chrome", "Application", "chrome.exe"), join(d, "Microsoft", "Edge", "Application", "msedge.exe")])
    .find(existsSync);
  if (!browser) throw new Error("Chrome or Edge was not found; pass --chrome <path to chrome.exe or msedge.exe>");

  server = spawn(python, ["-c", DEMO], { cwd: ROOT, stdio: ["pipe", "pipe", "inherit"] });
  const auth = await new Promise((ok, no) => {
    let text = "";
    server.stdout.on("data", (d) => { text += d; const m = /http:\/\/127\.0\.0\.1:\d+\/auth\?code=\S+/.exec(text); if (m) ok(m[0]); });
    server.on("error", no);
    server.on("exit", (code) => no(new Error(`the simulation stopped (exit ${code}) before it printed its address`)));
    setTimeout(() => no(new Error("the simulation did not start within 60 seconds")), 60_000).unref();
  });

  profile = mkdtempSync(join(tmpdir(), "rm-shots-"));
  // No extensions (installed ones could slow the start or draw into the page) and no background
  // traffic: the browser only talks to the simulation on this computer.
  chrome = spawn(browser, ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--disable-default-apps", "--disable-sync", "--disable-background-networking", "--disable-component-update",
    "--remote-debugging-port=0", `--user-data-dir=${profile}`, "--window-size=3840,2160", "about:blank"], { stdio: "ignore" });
  // With port 0 the browser picks a free port and writes it to DevToolsActivePort in the profile.
  const port = await until(() => { try { return Number(readFileSync(join(profile, "DevToolsActivePort"), "utf8").split("\n")[0]); } catch { return 0; } },
    30_000, "the browser did not start");
  const target = await until(async () => {
    try { return (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === "page"); } catch { return undefined; }
  }, 30_000, "the browser opened no page");

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((ok, no) => { ws.onopen = ok; ws.onerror = () => no(new Error("could not connect to the browser's DevTools")); });
  let seq = 0;
  const pending = new Map();
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    const p = pending.get(d.id);
    if (!p) return;
    pending.delete(d.id);
    if (d.error) p.no(new Error(`${p.method}: ${d.error.message}`)); else p.ok(d.result);
  };
  const send = (method, params = {}) => new Promise((ok, no) => { pending.set(++seq, { ok, no, method }); ws.send(JSON.stringify({ id: seq, method, params })); });
  const js = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(`page script failed: ${r.exceptionDetails.exception?.description ?? r.exceptionDetails.text}`);
    return r.result.value;
  };

  // The README pictures use the dark theme, US formats and UTC, whatever this computer is set to: a
  // public picture must not reveal the time zone of the computer that took it.
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
  await send("Emulation.setLocaleOverride", { locale: "en-US" });
  await send("Emulation.setTimezoneOverride", { timezoneId: "UTC" });
  await send("Page.navigate", { url: auth });
  await until(() => js(`!!document.querySelector("main") && !document.body.innerText.includes("Opening ReadmeMonitor")`), 30_000, "the interface did not open");

  // The simulation adds commits after the first live run, so the second one shows a pull request
  // awaiting merge, a direct commit and a paused budget.
  for (const kind of ["dry_run", "live", "live"]) {
    const before = await js(`${API}("/api/runs?limit=1").then((r) => r.runs[0]?.id ?? 0)`);
    const start = await js(`${API}("/api/run", { kind: "${kind}" })`);
    if (!start.started) throw new Error(`the ${kind} run did not start: ${start.reason}`);
    const outcome = await until(() => js(`Promise.all([${API}("/api/runs?limit=1"), ${API}("/api/state")])
      .then(([r, s]) => r.runs[0]?.id > ${before} && s.runtime.state !== "running" && r.runs[0].outcome)`), 120_000, `the ${kind} run did not finish`);
    if (outcome !== "ok") throw new Error(`the ${kind} run ended "${outcome}"`);
  }

  const pictures = [];
  for (const [file, width, height, view] of SHOTS) {
    await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 768 });
    await js(`[...document.querySelectorAll("button")].find((b) => b.textContent.trim() === ${JSON.stringify(view)})?.click()`);
    // Fonts, then the entrance transitions, so the picture shows the resting state.
    await js(`(async () => {
      await document.fonts.ready;
      await new Promise((r) => setTimeout(r, 1500));
      for (let i = 0; i < 50 && document.getAnimations().some((a) => a.playState === "running" && a.effect?.getTiming().iterations !== Infinity); i++)
        await new Promise((r) => setTimeout(r, 100));
    })()`);
    const page = await js(`({ demo: document.body.innerText.includes("Simulation:"), sideways: document.documentElement.scrollWidth - innerWidth })`);
    if (!page.demo) throw new Error(`${file}: the simulation banner is missing`);
    if (page.sideways > 0) throw new Error(`${file}: the page scrolls sideways by ${page.sideways} px at ${width}x${height}`);
    const png = Buffer.from((await send("Page.captureScreenshot", { format: "png" })).data, "base64");
    if (png.readUInt32BE(16) !== width || png.readUInt32BE(20) !== height)
      throw new Error(`${file}: captured ${png.readUInt32BE(16)}x${png.readUInt32BE(20)} instead of ${width}x${height}`);
    pictures.push([join(out, file), png]);
  }
  ws.close();
  mkdirSync(out, { recursive: true });
  for (const [path, png] of pictures) {
    writeFileSync(path, png);
    console.log(`${path} (${png.length} bytes)`);
  }
} catch (e) {
  console.error(`screenshots: ${e.message}`);
  process.exitCode = 1;
} finally {
  chrome?.kill();
  if (server && server.exitCode === null) {
    const stopped = new Promise((r) => server.once("exit", r));
    const force = setTimeout(() => server.kill(), 30_000);
    server.stdin.end();
    await stopped;
    clearTimeout(force);
  }
  if (profile) {
    try { rmSync(profile, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 }); }
    catch { console.error(`screenshots: could not remove the browser profile ${profile}`); }
  }
}
