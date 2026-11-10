// DEVELOPMENT ONLY. Sample data shaped exactly like docs/api.md. Never imported by the production bundle.
import type { Draft, RepoView, RmEvent, Run, State, Usage } from "../api/types";

export type Scenario =
  | "fresh" | "onboarding" | "inactive" | "running" | "awaiting_merge" | "confirmed" | "no_change"
  | "conflict" | "deferred" | "failed" | "paused" | "disconnected" | "signed_out";

export const SCENARIOS: Scenario[] = ["fresh", "onboarding", "inactive", "running", "awaiting_merge", "confirmed", "no_change", "conflict", "deferred", "failed", "paused", "disconnected", "signed_out"];

const NOW = Date.now();
export const iso = (offsetMin: number) => new Date(NOW + offsetMin * 60_000).toISOString().replace(/\.\d{3}Z$/, "Z");
const ALL_SECTIONS = ["features", "quick-start", "configuration", "architecture", "project-structure", "development", "status", "recent-changes"];

function repo(name: string, over: Partial<RepoView> = {}): RepoView {
  return {
    name, repo_id: Math.abs([...name].reduce((h, c) => (h * 31 + c.charCodeAt(0)) | 0, 7)), visibility: "public",
    source: "config", enrolled: true, mode: "review_pr", api_export: "none", restricted: false,
    branch: null, readme_paths: ["README.md"], managed_sections: ALL_SECTIONS, disabled_sections: [],
    source_include: [], source_exclude: [], paused: false,
    disposition: "enrolled", detail: "Monitored from its own entry.", state: "unchanged", state_detail: null,
    head: "4be1f09c2d7a8e3b5f6a1c9d0e2f4a6b8c0d1e3f", last_checked: iso(-12), pending_since: null, next_eligible: null,
    last_generated: iso(-60 * 26), last_published: iso(-60 * 26), proposal: null, latest_draft_id: 41, spent_month_usd: 0.42, ...over,
  };
}

export function buildRepos(s: Scenario): RepoView[] {
  const base = [
    repo("octo-demo/atlas-api", { api_export: "allowlist", source_include: ["src/**/*.py", "pyproject.toml"], spent_month_usd: 1.18 }),
    repo("octo-demo/harbor-cli", { mode: "auto_pr", last_checked: iso(-30) }),
    repo("octo-demo/lumen-docs", { mode: "preview", visibility: "private", last_checked: iso(-90) }),
    repo("octo-demo/quarry-sdk", { mode: "direct", branch: "main", last_checked: iso(-5) }),
    ...Array.from({ length: 36 }, (_, i) => repo(`octo-demo/sample-service-${String(i + 1).padStart(2, "0")}`, {
      source: "discovered", enrolled: false, mode: null, api_export: null, disposition: i % 9 === 0 ? "excluded" : "discovered",
      detail: i % 9 === 0 ? "Archived on GitHub; not monitored." : "Found in the account; uses the discovery defaults.",
      visibility: i % 3 ? "public" : "private", last_checked: i % 4 ? iso(-15 - i) : null, state: i % 4 ? "unchanged" : "new", latest_draft_id: null, spent_month_usd: 0,
    })),
  ];
  const a = base[0];
  if (s === "fresh") return [];
  if (s === "onboarding" || s === "inactive") return base.map((r) => ({ ...r, state: "new", last_checked: null, last_generated: null, last_published: null, spent_month_usd: 0 }));
  base[1].state = "needs_init"; base[1].state_detail = "The README has no managed-block markers yet; adoption adds them by pull request.";
  if (s === "awaiting_merge") { a.state = "awaiting_merge"; a.proposal = { id: 12, state: "awaiting_merge", mode: "review_pr", pr_number: 87, pr_url: "https://github.com/octo-demo/atlas-api/pull/87", published_sha: null, detail: "Waiting for a reviewer to merge.", created: iso(-8) }; }
  if (s === "confirmed") { a.state = "published"; a.last_published = iso(-3); a.proposal = { id: 12, state: "published", mode: "review_pr", pr_number: 87, pr_url: "https://github.com/octo-demo/atlas-api/pull/87", published_sha: "9d3c1a7e5b2f4c6d8e0a1b3c5d7e9f1a2b4c6d8e", detail: "Merged and read back from main.", created: iso(-40) }; }
  if (s === "conflict") { a.state = "conflict"; a.state_detail = "Someone edited the Quick start block by hand. It is left alone until the marker hash is refreshed."; }
  if (s === "deferred") { a.state = "generation_deferred"; a.pending_since = iso(-6); a.next_eligible = iso(24); a.state_detail = `quiet period: waiting since ${iso(-6).replace("Z", "+00:00")}`; }
  if (s === "failed") { a.state = "error"; a.state_detail = "The model answer was not valid JSON. Nothing was published; the reservation was released."; }
  if (s === "paused") base.forEach((r) => (r.paused = r.enrolled));
  base[2].paused = true;
  return base;
}

export function buildState(s: Scenario): State {
  const setupDone = !["fresh", "onboarding", "inactive"].includes(s);
  const active = setupDone;
  const steps = [
    { id: "runtime" as const, done: active, detail: active ? "Enabled on WORKSTATION-1." : "Not enabled on this computer yet." },
    { id: "github" as const, done: s !== "fresh", detail: s !== "fresh" ? "Signed in as octo-demo." : "No GitHub token stored." },
    { id: "repositories" as const, done: s !== "fresh", detail: s !== "fresh" ? "4 repositories enrolled." : "No repositories chosen." },
    { id: "model" as const, done: setupDone || s === "inactive", detail: setupDone || s === "inactive" ? "Anthropic key and three spending limits are set." : "Key or spending limits missing; paid generation stays off." },
    { id: "publication" as const, done: setupDone || s === "inactive", detail: setupDone || s === "inactive" ? "Publication modes chosen." : "No publication mode chosen yet." },
    { id: "dry_run" as const, done: setupDone, detail: setupDone ? "A dry run finished" : "No dry run yet." },
  ];
  const runtimeState = !active ? "inactive" : s === "running" ? "running" : s === "paused" ? "paused" : "scheduled_idle";
  const sched = { mode: "interval" as const, interval_minutes: 15, time: "09:00", weekdays: ["mon", "tue", "wed", "thu", "fri"] as State["schedule"]["configured"]["weekdays"], timezone: "local" as const };
  const lastRun: Run = { id: 214, trigger: "scheduled", started_at: iso(-12), finished_at: iso(-11), outcome: s === "failed" ? "error" : "ok", summary: { repos: 4, unchanged: 3 } };
  return {
    version: "0.3.0", simulation: true, now: iso(0), cursor: 0,
    installation: {
      home: "C:\\Users\\example\\AppData\\Local\\ReadmeMonitor", host: "WORKSTATION-1", account: "example", platform: "Windows 11",
      supported: true, active, problem: active ? null : "The worker is not enabled on this computer, so nothing can spend, publish or be scheduled.",
      activation: active ? { host: "WORKSTATION-1", activated_at: iso(-60 * 24 * 3), installation_id: "inst-7f3a" } : null,
      host_lock: null, can_change_home: !active,
    },
    runtime: {
      state: runtimeState,
      detail: runtimeState === "running" ? "Checking 4 repositories." : runtimeState === "paused" ? "Paused by you. Nothing is checked until you resume." : runtimeState === "inactive" ? "Not enabled on this computer." : "Waiting for the next scheduled check.",
      current_run: s === "running" ? { id: 215, trigger: "manual", started_at: iso(0), finished_at: null, outcome: null, summary: null } : null,
      last_run: setupDone ? lastRun : null,
      next_check: active && s !== "paused" ? iso(3) : null,
    },
    credentials: { github: { present: s !== "fresh", source: "credential store", login: s !== "fresh" ? "octo-demo" : null }, anthropic: { present: setupDone || s === "inactive", source: "credential store" } },
    budget: {
      configured: setupDone || s === "inactive", per_run_usd: setupDone ? 1 : null, daily_usd: setupDone ? 3 : null, monthly_usd: setupDone ? 20 : null, pacing: "even",
      spent_today_usd: setupDone ? 0.61 : 0, spent_month_usd: setupDone ? 4.37 : 0, pacing_limit_today_usd: setupDone ? 0.94 : null,
      open_reservations: s === "running" ? 1 : 0, open_reserved_usd: s === "running" ? 0.35 : 0,
    },
    model: { id: "claude-sonnet-5-5", effort: "medium", priced: true, provider: "anthropic" },
    policy: { quiet_minutes: 30, min_generation_interval_hours: 6, max_postpone_hours: 24 },
    schedule: {
      configured: sched, effective: active ? sched : null, in_sync: active, summary: "Every 15 minutes",
      task: active ? { installed: true, enabled: s !== "paused", status: s === "running" ? "Running" : "Ready", next_run: iso(3), last_run: iso(-12), last_result: "0" } : null,
      next_checks: active ? [iso(3), iso(18), iso(33)] : [], timezone_name: "Central European Standard Time",
      notes: ["Checks run only while you are signed in to Windows.", "A computer that is asleep skips checks; the next one catches up."],
    },
    discovery: { enabled: s !== "fresh", owner: "octo-demo", last: null },
    repos: buildRepos(s),
    onboarding: { complete: setupDone, steps },
  };
}

const A = "octo-demo/atlas-api";
type Ev = Omit<RmEvent, "id" | "at" | "run_id" | "trigger" | "repo_id" | "revision">;
const e = (stage: RmEvent["stage"], status: RmEvent["status"], message: string, data: RmEvent["data"] = {}, repo: string | null = A): Ev => ({ stage, status, message, data, repo });

const COMMITS = [
  { sha: "4be1f09c2d7a8e3b5f6a1c9d0e2f4a6b8c0d1e3f", message: "Add --dry-run flag to the export command\n\nPrints the plan without writing.", author: "Mira Okafor", date: iso(-95) },
  { sha: "c81d22e0a7b94f3d6e5c1a2b8f0d9e7c6b5a4f3e", message: "Document the new retry settings", author: "Jonas Pike", date: iso(-140) },
  { sha: "7a0e5b3f9c1d2e4a6b8c0d1e2f3a4b5c6d7e8f90", message: "Rename config key timeout to request_timeout", author: "Mira Okafor", date: iso(-300) },
];

export function script(kind: Scenario, dry = false): Ev[] {
  const head = [
    e("run", "started", "Check started for 4 repositories.", { repos: 4 }, null),
    e("discover", "done", "Listed 40 repositories; nothing new.", { listed: 40, new: 0, complete: true }, null),
    e("queue", "info", "Queued first.", { position: 1 }),
    e("inspect", "started", "Comparing the last analyzed head with main."),
    e("inspect", "done", "3 commits since the last check; 7 files changed.", { baseline: "1f0a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c", head: COMMITS[0].sha, compare_status: "ahead", commits: COMMITS, commits_total: 3, files_changed: [
      { path: "src/atlas/export.py", status: "modified", additions: 48, deletions: 6 }, { path: "src/atlas/config.py", status: "modified", additions: 12, deletions: 9 },
      { path: "docs/retry.md", status: "added", additions: 64, deletions: 0 }, { path: "tests/test_export.py", status: "modified", additions: 30, deletions: 2 },
      { path: "pyproject.toml", status: "modified", additions: 1, deletions: 1 }, { path: "CHANGELOG.md", status: "modified", additions: 6, deletions: 0 }, { path: ".github/workflows/ci.yml", status: "modified", additions: 2, deletions: 2 }], files_total: 7 }),
    e("evidence", "started", "Reading approved source files."),
    e("evidence", "done", "Read 4 files; 1 skipped; 23 facts.", { export: "allowlist", files_read: ["src/atlas/export.py", "src/atlas/config.py", "pyproject.toml", "README.md"], skipped: [".env.example: looks like it holds secrets"], facts: 23, readmes: ["README.md"], payload_chars: 18234 }),
    e("draft", "started", "Request sent to the model.", { mode: "claude", sections_requested: ["features", "configuration"], model: "claude-sonnet-5-5", estimate_usd: 0.31 }),
  ];
  if (kind === "failed") return [...head, e("draft", "failed", "The model answer was not valid JSON. The reservation was released.", { error: "invalid_output" }), e("result", "info", "Failed. Nothing was published.", { outcome: "failed", detail: "The model answer was not valid JSON. Nothing was published; the reservation was released." }), e("run", "failed", "Check finished with 1 failure.", {}, null)];
  if (kind === "deferred") return [head[0], head[1], e("queue", "info", "Waiting for a quiet period.", { reason: "quiet_period" }), head[3], head[4], e("result", "info", "Deferred: quiet period.", { outcome: "deferred", detail: "The last commit was 6 minutes ago. The update waits until 30 quiet minutes have passed, at most 24 hours." }), e("run", "done", "Check finished.", {}, null)];
  const draftDone = e("draft", "done", "Answer received and stored as draft 41.", { mode: "claude", sections_returned: ["features", "configuration"], usage: { input_tokens: 9120, output_tokens: 1210, cost_usd: 0.29 }, draft_id: 41 });
  if (kind === "conflict") return [...head, draftDone, e("validate", "done", "Configuration changed; Quick start has a hand edit.", { changed: { "README.md": ["configuration"] }, conflicts: { "README.md": ["quick-start"] }, notes: ["The Quick start block was edited by hand and is left alone."], draft_id: 41 }), e("result", "info", "Conflict in Quick start.", { outcome: "conflict", detail: "Someone edited the Quick start block by hand. It is left alone; the rest waits for you to review." }), e("run", "done", "Check finished.", {}, null)];
  const validated = [...head, draftDone, e("validate", "done", "2 sections changed; clean pass.", { changed: { "README.md": ["features", "configuration"] }, conflicts: {}, flags: [], notes: ["Feature list now mentions --dry-run."], draft_id: 41 })];
  if (dry) return [...validated.map((x) => (x.stage === "draft" ? { ...x, message: x.status === "started" ? "Building the draft without a model request." : "Draft 41 stored; no model request was made.", data: { mode: "dry_run", draft_id: 41 } } : x)), e("result", "info", "Dry run: draft ready for review.", { outcome: "preview", detail: "a live run would ask the model to draft features, configuration; a model request would reserve at most $0.0852; no other section would change" }), e("run", "done", "Dry run finished.", { dry_run: true }, null)];
  const published = [...validated, e("publish", "started", "Opening a pull request."), e("publish", "done", "Pull request #87 opened.", { mode: "review_pr", branch: "readme-monitor/update", proposal_id: 12, pr_number: 87, pr_url: "https://github.com/octo-demo/atlas-api/pull/87", commit_sha: "9d3c1a7e5b2f4c6d8e0a1b3c5d7e9f1a2b4c6d8e", commit_url: "https://github.com/octo-demo/atlas-api/commit/9d3c1a7e5b2f4c6d8e0a1b3c5d7e9f1a2b4c6d8e" })];
  if (kind === "confirmed") return [...published, e("await", "info", "Waiting for merge.", { waiting_for: "merge", pr_url: "https://github.com/octo-demo/atlas-api/pull/87" }), e("await", "done", "Merged."), e("result", "info", "README confirmed on main.", { outcome: "confirmed", detail: "Read back from main: both sections match the draft.", pr_url: "https://github.com/octo-demo/atlas-api/pull/87", commit_sha: "9d3c1a7e5b2f4c6d8e0a1b3c5d7e9f1a2b4c6d8e" }), e("run", "done", "Check finished.", {}, null)];
  return [...published, e("await", "info", "Waiting for a reviewer to merge pull request #87.", { waiting_for: "merge", pr_url: "https://github.com/octo-demo/atlas-api/pull/87", since: iso(0) }), e("result", "info", "Pull request awaiting merge.", { outcome: "awaiting_merge", detail: "Pull request #87 is open and waiting for a reviewer.", pr_url: "https://github.com/octo-demo/atlas-api/pull/87" }), e("run", "done", "Check finished.", {}, null)];
}

export const QUIET_RUN: Ev[] = [e("run", "started", "Check started for 4 repositories.", { repos: 4 }, null), e("run", "done", "Checked 4 repositories: nothing needed updating.", { summary: "4 unchanged", repos: 4 }, null)];

export function stamp(evs: Ev[], firstId: number, runId: number, trigger: Run["trigger"], startMin: number): RmEvent[] {
  return evs.map((x, i) => ({ ...x, id: firstId + i, at: iso(startMin + i * 0.2), run_id: runId, trigger, repo_id: x.repo ? 1 : null, revision: x.repo ? COMMITS[0].sha : null }));
}

export const DRAFT: Draft = {
  id: 41, at: iso(-1), run_id: 215, repo: A, kind: "proposal", proposal_id: 12, flags: [], notes: ["Feature list now mentions --dry-run."], estimate_usd: 0.31,
  files: {
    "README.md": {
      changed: ["features", "configuration"], conflicts: [],
      old: "## Features\n\n- Export projects to JSON or CSV\n- Retries failed uploads\n\n## Configuration\n\n| Key | Default |\n| --- | --- |\n| `timeout` | 30 |\n\n![Architecture diagram](https://example.com/arch.png)\n",
      new: "## Features\n\n- Export projects to JSON or CSV\n- Preview an export with `--dry-run` before anything is written\n- Retries failed uploads with configurable back-off ([details](https://github.com/octo-demo/atlas-api/blob/main/docs/retry.md))\n\n## Configuration\n\n| Key | Default |\n| --- | --- |\n| `request_timeout` | 30 |\n| `retry_attempts` | 3 |\n\n![Architecture diagram](https://example.com/arch.png)\n",
      diff: "--- a/README.md\n+++ b/README.md\n@@ -3,8 +3,10 @@\n - Export projects to JSON or CSV\n-- Retries failed uploads\n+- Preview an export with `--dry-run` before anything is written\n+- Retries failed uploads with configurable back-off ([details](https://github.com/octo-demo/atlas-api/blob/main/docs/retry.md))\n \n ## Configuration\n \n | Key | Default |\n | --- | --- |\n-| `timeout` | 30 |\n+| `request_timeout` | 30 |\n+| `retry_attempts` | 3 |\n",
    },
  },
};

export const USAGE: Usage = {
  rows: [{ id: 91, repo: A, run_id: 214, model: "claude-sonnet-5-5", state: "settled", reserved_usd: 0.35, actual_usd: 0.29, input_tokens: 9120, output_tokens: 1210, created_at: iso(-12) }],
  // Like the backend: only days that had spending are listed.
  by_day: [2, 5, 6, 11, 19, 27].map((i) => ({ day: iso(-(29 - i) * 1440).slice(0, 10), usd: i === 6 ? 0.004 : Math.round((Math.sin(i) + 1.2) * 18) / 100 })),
};
