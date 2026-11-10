// Folds stored events (docs/api.md) into the pipeline the workflow surface draws. Pure, so it is tested.
import type { EventData, RepoName, RmEvent, Run, Stage } from "../api/types";

export type StepId = "queue" | "inspect" | "evidence" | "draft" | "validate" | "publish" | "await";
export type StepStatus = "not_reached" | "active" | "waiting" | "done" | "skipped" | "failed";

export const STEPS: { id: StepId; label: string; from: Stage[] }[] = [
  { id: "queue", label: "Queued and discovering", from: ["run", "discover", "queue"] },
  { id: "inspect", label: "Inspecting commits and source", from: ["inspect"] },
  { id: "evidence", label: "Gathering approved evidence", from: ["evidence"] },
  { id: "draft", label: "Drafting README changes", from: ["draft"] },
  { id: "validate", label: "Validating the diff", from: ["validate"] },
  { id: "publish", label: "Publishing to GitHub", from: ["publish"] },
  { id: "await", label: "Awaiting checks or merge", from: ["await"] },
];

export interface StepView { id: StepId; label: string; status: StepStatus; message: string | null; reason: string | null; at: string | null }

export type OutcomeKind =
  | "confirmed" | "pushed" | "awaiting_merge" | "awaiting_checks" | "preview" | "no_change" | "unchanged"
  | "deferred" | "paused" | "blocked" | "conflict" | "failed" | "cancelled" | "running" | "no_activity";

export interface Outcome { kind: OutcomeKind; detail: string; prUrl: string | null; commitSha: string | null; commitUrl: string | null }

export interface RunView {
  runId: number | null;
  live: boolean;
  /** The run is a dry run (from the run record or the events' trigger). */
  dry: boolean;
  steps: StepView[];
  outcome: Outcome | null;
  inspect: EventData | null;
  evidence: EventData | null;
  draft: EventData | null;
  validate: EventData | null;
  publish: EventData | null;
  await: EventData | null;
  lastEventId: number;
}

const stepOf = (s: Stage): StepId => STEPS.find((x) => x.from.includes(s))!.id;
const KNOWN: OutcomeKind[] = ["confirmed", "pushed", "awaiting_merge", "awaiting_checks", "preview", "no_change", "unchanged", "deferred", "paused", "blocked", "conflict", "failed", "cancelled"];

/** Events of one run that concern `repo` (plus run-wide events without a repo). */
export function eventsFor(events: Iterable<RmEvent>, runId: number | null, repo: RepoName | null): RmEvent[] {
  const out: RmEvent[] = [];
  for (const e of events) if ((runId === null || e.run_id === runId) && (e.repo === null || repo === null || e.repo === repo)) out.push(e);
  return out.sort((a, b) => a.id - b.id);
}

/** Newest run that touched `repo`, by event ids. */
export function latestRunFor(events: Iterable<RmEvent>, repo: RepoName): number | null {
  let best: RmEvent | null = null;
  for (const e of events) if (e.repo === repo && e.run_id !== null && (!best || e.id > best.id)) best = e;
  return best?.run_id ?? null;
}

/** `repo`: the repository being viewed; when it has no events of its own the view says so instead of claiming a check. */
export function foldRun(events: RmEvent[], run: Run | null, live: boolean, repo: RepoName | null = null): RunView {
  const steps: StepView[] = STEPS.map((s) => ({ id: s.id, label: s.label, status: "not_reached", message: null, reason: null, at: null }));
  const byId = Object.fromEntries(steps.map((s) => [s.id, s])) as Record<StepId, StepView>;
  const dry = run ? run.trigger === "dry_run" : events.some((e) => e.trigger === "dry_run" || e.data.dry_run === true);
  const view: RunView = { runId: run?.id ?? events[0]?.run_id ?? null, live, dry, steps, outcome: null, inspect: null, evidence: null, draft: null, validate: null, publish: null, await: null, lastEventId: 0 };
  let runDone = false;
  let failedRun = false;
  for (const e of events) {
    view.lastEventId = Math.max(view.lastEventId, e.id);
    if (e.stage === "result") {
      const d = e.data;
      const kind = (KNOWN.includes(d.outcome as OutcomeKind) ? d.outcome : "failed") as OutcomeKind;
      view.outcome = { kind, detail: d.detail ?? e.message, prUrl: d.pr_url ?? view.outcome?.prUrl ?? null, commitSha: d.commit_sha ?? view.publish?.commit_sha ?? null, commitUrl: view.publish?.commit_url ?? null };
      continue;
    }
    const st = byId[stepOf(e.stage)];
    if (e.stage === "run" && e.status !== "started") { runDone = true; failedRun = e.status === "failed"; }
    if (e.stage !== "run" || e.status === "started") {
      if (e.status === "started") st.status = "active";
      else if (e.status === "done") st.status = "done";
      else if (e.status === "skipped") { st.status = "skipped"; st.reason = e.message; }
      else if (e.status === "failed") { st.status = "failed"; st.reason = e.message; }
      else if (st.status === "not_reached") st.status = "active";
    }
    st.message = e.message; st.at = e.at;
    if (e.stage !== "run" && e.stage !== "discover" && e.stage !== "queue") view[e.stage] = { ...(view[e.stage] ?? {}), ...e.data };
    // Earlier steps are finished once a later step has begun.
    const idx = STEPS.findIndex((s) => s.id === st.id);
    for (let i = 0; i < idx; i++) if (steps[i].status === "active") steps[i].status = "done";
  }
  if (view.outcome && view.publish?.commit_url) view.outcome.commitUrl = view.publish.commit_url;
  const finished = runDone || (run?.finished_at ?? null) !== null;
  if (!view.outcome && finished) {
    const anyRepoWork = events.some((e) => e.repo !== null);
    const cancelled = run?.outcome === "cancelled";
    view.outcome = failedRun || (run?.outcome && !["ok", "cancelled"].includes(run.outcome))
      ? { kind: "failed", detail: events.at(-1)?.message ?? "The run ended with an error.", prUrl: null, commitSha: null, commitUrl: null }
      : cancelled ? { kind: "cancelled", detail: "The run was cancelled at a safe boundary.", prUrl: null, commitSha: null, commitUrl: null }
      : !anyRepoWork && repo !== null ? { kind: "no_activity", detail: "", prUrl: null, commitSha: null, commitUrl: null }
      : !anyRepoWork ? { kind: "no_change", detail: "Checked: nothing needed updating.", prUrl: null, commitSha: null, commitUrl: null }
      : null;
  }
  if (!view.outcome && live) view.outcome = { kind: "running", detail: "", prUrl: null, commitSha: null, commitUrl: null };
  // Steps that never ran once the run is settled are skipped with the outcome as the reason.
  if (view.outcome && view.outcome.kind !== "running") {
    if (byId.queue.status === "active") byId.queue.status = "done";
    for (const s of steps) {
      if (s.status === "active") s.status = view.outcome.kind === "failed" ? "failed" : view.outcome.kind.startsWith("awaiting") && s.id === "await" ? "waiting" : "done";
      if (s.status === "not_reached") s.reason = OUTCOME_TEXT[view.outcome.kind].short;
    }
  }
  return view;
}

export const OUTCOME_TEXT: Record<OutcomeKind, { title: string; short: string; tone: "ok" | "active" | "attention" | "fail" | "neutral" }> = {
  confirmed: { title: "README confirmed on the target branch", short: "Not needed: already confirmed", tone: "ok" },
  pushed: { title: "Push confirmed: GitHub accepted the write", short: "Not reached: waiting for confirmation", tone: "active" },
  awaiting_merge: { title: "Pull request awaiting merge", short: "Not reached: the pull request is open", tone: "attention" },
  awaiting_checks: { title: "Pull request awaiting checks", short: "Not reached: checks are running", tone: "attention" },
  preview: { title: "Preview drafted locally", short: "Not reached: preview only", tone: "neutral" },
  no_change: { title: "Checked: nothing needed updating", short: "Not needed: nothing changed", tone: "ok" },
  unchanged: { title: "Checked: README already matches", short: "Not needed: README already matches", tone: "ok" },
  deferred: { title: "Update deferred", short: "Not reached: deferred", tone: "attention" },
  paused: { title: "Paused", short: "Not reached: paused", tone: "neutral" },
  blocked: { title: "Blocked", short: "Not reached: blocked", tone: "fail" },
  conflict: { title: "Conflict: a person edited a managed section", short: "Not reached: conflict", tone: "attention" },
  failed: { title: "Failed", short: "Not reached: the run failed", tone: "fail" },
  cancelled: { title: "Cancelled", short: "Not reached: cancelled", tone: "neutral" },
  running: { title: "Running", short: "Not reached yet", tone: "active" },
  no_activity: { title: "No activity for this repository in this run", short: "Not reached: no activity for this repository", tone: "neutral" },
};

/** Outcome title; a dry run never reads as a live result. */
export function outcomeTitle(v: RunView): string {
  const o = v.outcome;
  if (!o) return "";
  if (v.dry && o.kind === "preview") return /would (ask|request)|model request would/i.test(o.detail) ? "Dry run: a model draft would be requested" : "Dry run: draft ready for review";
  const t = OUTCOME_TEXT[o.kind].title;
  const d = t.replace(/^Checked: /, "");
  return v.dry && o.kind !== "no_activity" ? `Dry run: ${d[0].toLowerCase()}${d.slice(1)}` : t;
}

/** Insert events keyed by id; never duplicates, keeps ascending order, bounded. */
export function mergeEvents(into: Map<number, RmEvent>, incoming: RmEvent[], cap = 5000): boolean {
  let changed = false;
  for (const e of incoming) if (!into.has(e.id)) { into.set(e.id, e); changed = true; }
  if (into.size > cap) { const ids = [...into.keys()].sort((a, b) => a - b); for (const id of ids.slice(0, into.size - cap)) into.delete(id); }
  return changed;
}
