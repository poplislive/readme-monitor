// DEVELOPMENT ONLY (npm run dev). Replaces fetch and EventSource for /api/* with an in-page fake that
// follows docs/api.md. main.tsx imports this behind import.meta.env.DEV, so the production build drops it.
import type { RmEvent, Run, Schedule, State } from "../api/types";
import { DRAFT, QUIET_RUN, SCENARIOS, USAGE, buildState, iso, script, stamp, type Scenario } from "./fixtures";

const params = new URLSearchParams(location.search);
const scenario = (SCENARIOS.includes(params.get("scenario") as Scenario) ? params.get("scenario") : "awaiting_merge") as Scenario;

const state: State = buildState(scenario);
const events: RmEvent[] = [];
const runs: Run[] = [];
const listeners = new Set<(e: RmEvent) => void>();
let nextId = 1000;
let stateCalls = 0;

// History: a few past cycles, then the scenario's own run.
for (let i = 6; i >= 1; i--) {
  const id = 214 - i;
  const evs = stamp(QUIET_RUN, nextId, id, "scheduled", -15 * i - 12); nextId += evs.length; events.push(...evs);
  runs.unshift({ id, trigger: "scheduled", started_at: evs[0].at, finished_at: evs[1].at, outcome: "ok", summary: { repos: 4, unchanged: 4 } });
}
if (state.onboarding.complete && scenario !== "running" && scenario !== "paused") {
  const evs = stamp(scenario === "no_change" ? QUIET_RUN : script(scenario), nextId, 214, "scheduled", -12); nextId += evs.length; events.push(...evs);
  runs.unshift({ id: 214, trigger: "scheduled", started_at: evs[0].at, finished_at: evs.at(-1)!.at, outcome: scenario === "failed" ? "error" : "ok", summary: { repos: 4 } });
}
state.cursor = nextId - 1;

function emit(e: RmEvent) { events.push(e); state.cursor = e.id; listeners.forEach((l) => l(e)); }

/** Plays a run in real time; each event is a real state change in the fake, nothing is padded for animation. */
function playRun(kind: Scenario, trigger: Run["trigger"]) {
  const runId = (runs[0]?.id ?? 214) + 1;
  const run: Run = { id: runId, trigger, started_at: iso(0), finished_at: null, outcome: null, summary: null };
  runs.unshift(run); state.runtime.state = "running"; state.runtime.current_run = run;
  const evs = stamp(kind === "no_change" ? QUIET_RUN : script(kind, trigger === "dry_run"), nextId, runId, trigger, 0); nextId += evs.length;
  let t = 600;
  evs.forEach((e, i) => setTimeout(() => {
    emit({ ...e, at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z") });
    if (i === evs.length - 1) {
      run.finished_at = e.at; run.outcome = kind === "failed" ? "error" : "ok";
      state.runtime.state = state.onboarding.complete ? "scheduled_idle" : "inactive"; state.runtime.current_run = null; state.runtime.last_run = run;
      if (trigger === "dry_run") { const s = state.onboarding.steps.find((x) => x.id === "dry_run")!; s.done = true; s.detail = "A dry run finished"; }
    }
  }, (t += e.status === "started" ? 1800 : 700)));
}
if (scenario === "running") playRun("awaiting_merge", "manual");

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const fail = (status: number, code: string, message: string) => json(status, { error: { code, message } });
const ok = (extra: object = {}) => json(200, { ok: true, ...extra });
const step = (id: string, done: boolean, detail: string) => { const s = state.onboarding.steps.find((x) => x.id === id)!; s.done = done; s.detail = detail; state.onboarding.complete = state.onboarding.steps.every((x) => x.done); };
const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function route(method: string, path: string, body: Record<string, unknown>): Promise<Response> {
  await wait(250 + Math.random() * 250);
  if (scenario === "signed_out") return fail(401, "no_session", "Open ReadmeMonitor from its launcher.");
  const u = new URL(path, location.origin);
  const p = u.pathname;
  if (method === "GET") {
    if (p === "/api/session") return json(200, { csrf: "dev-csrf", version: "0.3.0", simulation: true });
    if (p === "/api/state") { if (scenario === "disconnected" && stateCalls++ > 0) return fail(503, "unavailable", "The worker database is busy. Retrying."); return json(200, { ...state, now: new Date().toISOString() }); }
    if (p === "/api/events") {
      const after = Number(u.searchParams.get("after") ?? -1), limit = Number(u.searchParams.get("limit") ?? 500);
      const run = u.searchParams.get("run_id"), repo = u.searchParams.get("repo");
      let list = events.filter((e) => e.id > after && (!run || e.run_id === Number(run)) && (!repo || e.repo === repo));
      list = after < 0 ? list.slice(-limit) : list.slice(0, limit);
      return json(200, { events: list, cursor: state.cursor });
    }
    if (p === "/api/runs") return json(200, { runs });
    const m = p.match(/^\/api\/runs\/(\d+)$/);
    if (m) { const run = runs.find((r) => r.id === Number(m[1])); return run ? json(200, { run, events: events.filter((e) => e.run_id === run.id) }) : fail(404, "not_found", "That run is no longer stored."); }
    if (/^\/api\/drafts\/\d+$/.test(p)) return json(200, DRAFT);
    if (p === "/api/diagnostics") return json(200, { checks: [{ status: "PASS", name: "simulation", detail: "sample data in the page" }, { status: "WARN", name: "schedule", detail: "no scheduled task registered yet" }], generated_at: iso(0) });
    if (p === "/api/usage") return json(200, USAGE);
  }
  if (p === "/api/installation/activate") { state.installation.active = true; state.installation.can_change_home = false; state.installation.problem = null; state.installation.activation = { host: state.installation.host, activated_at: iso(0), installation_id: "inst-dev" }; state.runtime.state = "manual_idle"; step("runtime", true, "Enabled on WORKSTATION-1."); return ok(); }
  if (p === "/api/installation/deactivate") { state.installation.active = false; state.installation.can_change_home = true; state.runtime.state = "inactive"; step("runtime", false, "Not enabled on this computer yet."); return ok(); }
  if (p === "/api/installation/home") { const v = String(body.path ?? ""); if (!/^[A-Za-z]:\\/.test(v)) return fail(400, "invalid_path", "Use an absolute folder on a local drive, for example D:\\ReadmeMonitor."); state.installation.home = v; return ok(); }
  const cred = p.match(/^\/api\/credentials\/(github|anthropic)$/);
  if (cred) {
    const kind = cred[1] as "github" | "anthropic";
    if (method === "DELETE") { state.credentials[kind].present = false; return ok(); }
    const v = String(body.value ?? "");
    if (v.length < 8 || v.startsWith("bad")) return fail(400, "credential_rejected", kind === "github" ? "GitHub did not accept this token. Check that it is copied completely and has not expired." : "This does not look like an Anthropic key.");
    state.credentials[kind].present = true;
    if (kind === "github") { state.credentials.github.login = "octo-demo"; step("github", true, "Signed in as octo-demo."); return ok(v.startsWith("ghp_") ? { login: "octo-demo", scopes: "repo, workflow", token_kind: "classic" } : { login: "octo-demo", scopes: null, token_kind: "fine-grained or app (no scope header)" }); }
    return ok();
  }
  if (p === "/api/github/discover") {
    const repos = buildState("inactive").repos;
    return ok({ login: "octo-demo", coverage: { owner: "octo-demo", listed: repos.length, public: repos.filter((r) => r.visibility === "public").length, private: repos.filter((r) => r.visibility !== "public").length, skipped_not_owned: 0, complete: true, limitations: [], token_kind: "fine-grained or app (no scope header)" }, repositories: repos.map((r) => ({ id: r.repo_id, name: r.name, visibility: r.visibility, archived: r.disposition === "excluded", fork: false, default_branch: "main", enrolled: state.repos.some((x) => x.name === r.name && x.enrolled), disposition: r.disposition, detail: r.detail })) });
  }
  const adoptM = p.match(/^\/api\/repos\/([^/]+)\/([^/]+)\/adopt$/);
  if (adoptM) {
    if (body.confirm !== true) return fail(400, "confirmation_required", "Adoption needs confirm: true.");
    if (state.runtime.state === "running") return ok({ started: false, reason: "already_running" });
    if (!state.installation.active) return ok({ started: false, reason: "inactive" });
    playRun("awaiting_merge", "init");
    return ok({ started: true });
  }
  const repoM = p.match(/^\/api\/repos\/([^/]+)\/([^/]+)$/);
  if (repoM) {
    const name = `${decodeURIComponent(repoM[1])}/${decodeURIComponent(repoM[2])}`;
    if (Array.isArray(body.managed_sections) && !body.managed_sections.length) return fail(400, "bad_request", "managed_sections must not be empty");
    if (body.api_export === "allowlist" && body.confirm_export !== true) return fail(400, "confirmation_required", "Turning on source export needs an explicit confirmation.");
    if (body.publish_mode === "direct" && body.confirm_direct !== true) return fail(400, "confirmation_required", "Direct commits need an explicit confirmation.");
    let r = state.repos.find((x) => x.name === name);
    if (method === "DELETE") { if (r) { r.enrolled = false; r.source = "discovered"; } return ok(); }
    if (!r) { r = { ...buildState("inactive").repos.find((x) => x.name === name)!, name }; state.repos.push(r); }
    Object.assign(r, { enrolled: true, source: "config" }, body.publish_mode ? { mode: body.publish_mode } : {}, ...(["branch", "readme_paths", "managed_sections", "api_export", "paused", "source_include"] as const).filter((k) => k in body).map((k) => ({ [k]: k === "branch" && body[k] === "" ? null : body[k] })));
    step("repositories", true, `${state.repos.filter((x) => x.enrolled).length} repositories enrolled.`);
    return ok();
  }
  if (p === "/api/settings") {
    const b = body.budget as State["budget"] | undefined;
    if (b) { if ([b.per_run_usd, b.daily_usd, b.monthly_usd].some((x) => !(typeof x === "number" && x > 0))) return fail(400, "invalid_budget", "Each limit must be an amount above zero."); Object.assign(state.budget, b, { configured: true }); }
    if (typeof body.model === "string") { if (!body.model.trim()) return fail(400, "bad_request", "model must not be empty"); state.model.id = body.model; }
    if (body.policy) Object.assign(state.policy, body.policy);
    if (body.discovery) { const d = body.discovery as { owner?: string }; if (typeof d.owner === "string") { state.discovery.owner = d.owner; state.discovery.enabled = !!d.owner; } }
    if (body.effort) state.model.effort = body.effort as State["model"]["effort"];
    step("model", state.budget.configured && state.credentials.anthropic.present, state.budget.configured ? "Spending limits set." : "Spending limits missing.");
    step("publication", true, "Publication modes chosen.");
    return ok();
  }
  if (p === "/api/schedule") {
    const s = body as unknown as Schedule;
    if (s.mode === "weekly" && s.timezone === "utc") return fail(400, "bad_request", "weekly schedules must use timezone local: Windows counts weekdays in local time");
    state.schedule.configured = s;
    if (!state.installation.active) return ok({ applied: false, reason: "Saved. It is applied to Windows Task Scheduler when the worker is enabled on this computer." });
    state.schedule.effective = s; state.schedule.in_sync = true;
    state.schedule.summary = s.mode === "manual" ? "Manual" : s.mode === "interval" ? `Every ${s.interval_minutes} minutes` : s.mode === "daily" ? `Daily at ${s.time}` : `Weekly at ${s.time}`;
    state.schedule.next_checks = s.mode === "manual" ? [] : [iso(5), iso(20), iso(35)];
    state.runtime.state = s.mode === "manual" ? "manual_idle" : "scheduled_idle"; state.runtime.next_check = state.schedule.next_checks[0] ?? null;
    return ok({ applied: true, schedule: state.schedule });
  }
  if (p === "/api/pause" || p === "/api/resume") {
    const pause = p === "/api/pause";
    if (body.repo) { const r = state.repos.find((x) => x.name === body.repo); if (r) r.paused = pause; }
    else state.runtime.state = pause ? "paused" : state.schedule.configured.mode === "manual" ? "manual_idle" : "scheduled_idle";
    return ok();
  }
  if (p === "/api/run") {
    if (state.runtime.state === "running") return ok({ started: false, reason: "already_running" });
    if (body.kind === "live" && !state.installation.active) return ok({ started: false, reason: "inactive" });
    if (body.kind === "live" && state.runtime.state === "paused") return ok({ started: false, reason: "paused" });
    playRun(body.kind === "dry_run" ? "awaiting_merge" : scenario === "running" ? "confirmed" : scenario, body.kind === "dry_run" ? "dry_run" : "manual");
    if (body.kind === "dry_run") step("dry_run", false, "Dry run in progress.");
    return ok({ started: true });
  }
  if (p === "/api/cancel") return ok({ requested: state.runtime.state === "running" });
  return fail(404, "not_found", `Sample data has no answer for ${method} ${p}.`);
}

const realFetch = window.fetch.bind(window);
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  if (!new URL(url, location.origin).pathname.startsWith("/api/")) return realFetch(input, init);
  const method = (init?.method ?? "GET").toUpperCase();
  if (method !== "GET" && new Headers(init?.headers).get("X-RM-CSRF") !== "dev-csrf") return fail(403, "csrf", "Missing X-RM-CSRF header.");
  return route(method, url, init?.body ? JSON.parse(String(init.body)) : {});
};

/** Minimal EventSource stand-in: resumes after the last delivered id like the real stream. */
class FakeEventSource extends EventTarget {
  readyState = 0; onopen: ((e: Event) => void) | null = null; onerror: ((e: Event) => void) | null = null;
  private last: number; private l = (e: RmEvent) => this.deliver(e);
  constructor(public url: string) {
    super();
    // Like the real stream: ?after=<id> resumes after that id; without it, only new events.
    const after = new URL(url, location.origin).searchParams.get("after");
    this.last = after === null ? state.cursor : Number(after);
    events.filter((e) => e.id > this.last).forEach((e) => setTimeout(() => this.deliver(e), 320));
    setTimeout(() => {
      if (scenario === "disconnected" || scenario === "signed_out") { this.readyState = 0; const ev = new Event("error"); this.onerror?.(ev); this.dispatchEvent(ev); if (scenario === "disconnected") setTimeout(() => this.close(), 0); return; }
      this.readyState = 1; const ev = new Event("open"); this.onopen?.(ev); this.dispatchEvent(ev);
      listeners.add(this.l);
    }, 300);
  }
  private deliver(e: RmEvent) { if (e.id <= this.last) return; this.last = e.id; this.dispatchEvent(new MessageEvent("activity", { data: JSON.stringify(e), lastEventId: String(e.id) })); }
  close() { this.readyState = 2; listeners.delete(this.l); }
}
(window as unknown as { EventSource: unknown }).EventSource = FakeEventSource;

// Visible label and scenario switcher. Plain DOM so it never mixes with the app's components.
const bar = document.createElement("div");
bar.className = "mock-bar";
bar.setAttribute("role", "region");
bar.setAttribute("aria-label", "Sample data controls");
bar.innerHTML = `<strong>Sample data</strong><label>Scenario <select>${SCENARIOS.map((s) => `<option${s === scenario ? " selected" : ""}>${s}</option>`).join("")}</select></label>`;
bar.querySelector("select")!.addEventListener("change", (ev) => { params.set("scenario", (ev.target as HTMLSelectElement).value); location.search = params.toString(); });
document.body.append(bar);
