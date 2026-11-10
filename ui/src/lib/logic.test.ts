import { describe, expect, it } from "vitest";
import type { RepoView, RmEvent, Schedule } from "../api/types";
import { foldRun, mergeEvents, outcomeTitle } from "./pipeline";
import { describe as describeSchedule, plannedChecks, scheduleError, shownFrequency, withFrequency } from "./schedule";
import { coverageText, fillDays, humanizeTimes, safeHref, usd, withoutUrls } from "./format";
import { idleText, needsAttention, repoStatus } from "./status";

const ev = (id: number, stage: RmEvent["stage"], status: RmEvent["status"], data: RmEvent["data"] = {}, repo: string | null = "octo-demo/atlas-api"): RmEvent =>
  ({ id, at: "2026-01-01T00:00:00Z", run_id: 1, trigger: "manual", repo, repo_id: 1, stage, status, revision: null, message: `${stage} ${status}`, data });

describe("foldRun", () => {
  it("marks earlier steps done when a later one starts and keeps the live step active", () => {
    const v = foldRun([ev(1, "run", "started", {}, null), ev(2, "inspect", "started"), ev(3, "evidence", "started")], null, true);
    expect(v.steps.map((s) => s.status)).toEqual(["done", "done", "active", "not_reached", "not_reached", "not_reached", "not_reached"]);
    expect(v.outcome?.kind).toBe("running");
  });
  it("never treats a push as confirmation", () => {
    const v = foldRun([ev(1, "publish", "done", { commit_url: "https://github.com/x/y/commit/1" }), ev(2, "result", "info", { outcome: "pushed" })], null, false);
    expect(v.outcome?.kind).toBe("pushed");
    expect(v.outcome?.commitUrl).toBe("https://github.com/x/y/commit/1");
  });
  it("reports a quiet cycle positively", () => {
    const v = foldRun([ev(1, "run", "started", {}, null), ev(2, "run", "done", {}, null)], null, false);
    expect(v.outcome?.kind).toBe("no_change");
    expect(v.steps[1].status).toBe("not_reached");
    expect(v.steps[1].reason).toMatch(/nothing changed/);
  });
  it("unknown outcomes fail closed", () => {
    expect(foldRun([ev(1, "result", "info", { outcome: "weird" })], null, false).outcome?.kind).toBe("failed");
  });
});

describe("mergeEvents", () => {
  it("never duplicates by id", () => {
    const m = new Map<number, RmEvent>();
    expect(mergeEvents(m, [ev(1, "run", "started"), ev(2, "run", "done")])).toBe(true);
    expect(mergeEvents(m, [ev(2, "run", "done")])).toBe(false);
    expect(m.size).toBe(2);
  });
});

describe("schedule", () => {
  const base: Schedule = { mode: "interval", interval_minutes: 15, time: "09:00", weekdays: ["mon"], timezone: "utc" };
  it("validates the custom interval range", () => {
    expect(scheduleError({ ...base, weekdays: [], interval_minutes: 4 })).toMatch(/5 to 1440/);
    expect(scheduleError({ ...base, weekdays: [], interval_minutes: 1441 })).not.toBeNull();
    expect(scheduleError({ ...base, weekdays: [], interval_minutes: 5 })).toBeNull();
  });
  it("plans daily checks in UTC", () => {
    const from = new Date("2026-11-10T12:00:00Z");
    const c = plannedChecks({ ...base, mode: "daily" }, from);
    expect(c.map((d) => d.toISOString())).toEqual(["2026-11-11T09:00:00.000Z", "2026-11-12T09:00:00.000Z", "2026-11-13T09:00:00.000Z"]);
    expect(describeSchedule({ ...base, mode: "weekly", timezone: "local", weekdays: ["mon", "tue", "wed", "thu", "fri"] }, "X")).toBe("Weekdays at 09:00 (this computer's time, X)");
  });
  it("forces weekly schedules to local time and rejects weekly UTC", () => {
    expect(withFrequency(base, "weekly").timezone).toBe("local");
    expect(scheduleError({ ...base, mode: "weekly" })).toMatch(/computer's time/);
    expect(plannedChecks({ ...base, mode: "weekly" }, new Date())).toEqual([]);
  });
  it("keeps an explicit Custom choice even when the minutes equal a preset", () => {
    const s = withFrequency(base, "custom");
    expect(shownFrequency(s, false)).toBe("15");
    expect(shownFrequency(s, true)).toBe("custom");
    expect(shownFrequency({ ...s, interval_minutes: 45 }, false)).toBe("custom");
    expect(scheduleError({ ...s, interval_minutes: 45, weekdays: [] })).toBeNull();
    expect(shownFrequency(withFrequency(s, "daily"), true)).toBe("daily");
  });
});

describe("safeHref", () => {
  it("allows only http and https", () => {
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("https://github.com/a")).toBe("https://github.com/a");
  });
});

const rv = (over: Partial<RepoView> = {}): RepoView => ({
  name: "octo-demo/atlas-api", repo_id: 1, visibility: "public", source: "config", enrolled: true, mode: "review_pr", api_export: "none", restricted: false,
  branch: null, readme_paths: ["README.md"], managed_sections: ["features"], disabled_sections: [], source_include: [], source_exclude: [], paused: false,
  disposition: "enrolled", detail: "", state: "unchanged", state_detail: null, head: null, last_checked: "2026-11-10T10:00:00Z", pending_since: null, next_eligible: null,
  last_generated: null, last_published: null, proposal: null, latest_draft_id: null, spent_month_usd: 0, ...over,
});

describe("repoStatus", () => {
  it("maps real worker states honestly", () => {
    expect(repoStatus(rv()).tone).toBe("ok");
    expect(repoStatus(rv({ state: "published" })).tone).toBe("ok");
    expect(repoStatus(rv({ state: "new", last_checked: null })).label).toBe("Not checked yet");
    expect(repoStatus(rv({ state: "unchanged", last_checked: null })).label).toBe("Not checked yet");
    expect(repoStatus(rv({ state: "awaiting_merge" })).label).toBe("Pull request awaiting merge");
    expect(repoStatus(rv({ state: "waiting_for_checks" })).label).toBe("Waiting for checks");
    expect(repoStatus(rv({ state: "proposal_created", proposal: { id: 1, state: "preview", mode: "preview", pr_number: null, pr_url: null, published_sha: null, detail: null, created: "" } })).label).toBe("Draft ready (preview)");
    for (const s of ["error", "rate_limited", "publish_blocked", "permission_missing", "credentials_missing", "inaccessible"]) expect(repoStatus(rv({ state: s })).tone).toBe("fail");
    for (const s of ["budget_paused", "needs_init", "adoption_deferred", "conflict", "stale", "generation_deferred"]) expect(repoStatus(rv({ state: s })).tone).toBe("attention");
    for (const s of ["archived", "empty", "paused"]) expect(repoStatus(rv({ state: s })).tone).toBe("neutral");
    expect(repoStatus(rv({ paused: true, state: "error" })).label).toBe("Paused");
  });
  it("shows unknown states as they are, never as success", () => {
    const s = repoStatus(rv({ state: "something_new" }));
    expect(s.tone).toBe("neutral");
    expect(s.label).toContain("something_new");
  });
  it("uses the same mapping for the attention filter", () => {
    expect(needsAttention(rv())).toBe(false);
    expect(needsAttention(rv({ state: "conflict" }))).toBe(true);
    expect(needsAttention(rv({ state: "rate_limited" }))).toBe(true);
    expect(needsAttention(rv({ state: "archived" }))).toBe(false);
  });
});

describe("a repository without events in the viewed run", () => {
  const runOnly = [ev(1, "run", "started", {}, null), ev(2, "run", "done", {}, null)];
  it("is not claimed as checked", () => {
    const v = foldRun(runOnly, null, false, "octo-demo/atlas-api");
    expect(v.outcome?.kind).toBe("no_activity");
    expect(outcomeTitle(v)).toBe("No activity for this repository in this run");
  });
  it("describes the stored state instead", () => {
    const now = new Date("2026-11-10T12:00:00Z");
    expect(idleText(rv(), now)).toBe("Unchanged at the last check, 2 hours ago.");
    expect(idleText(rv({ state: "new", last_checked: null }), now)).toMatch(/not been checked/);
    expect(idleText(rv({ state: "conflict" }), now)).toMatch(/^Stored state: conflict/);
  });
  it("labels dry-run previews as dry runs", () => {
    const d = (detail: string) => foldRun([{ ...ev(1, "result", "info", { outcome: "preview", detail }), trigger: "dry_run" }], null, false);
    expect(outcomeTitle(d("a live run would ask the model to draft features; a model request would reserve at most $0.0852"))).toBe("Dry run: a model draft would be requested");
    expect(outcomeTitle(d("Nothing was sent to GitHub."))).toBe("Dry run: draft ready for review");
  });
  it("labels a waiting pull request after the run ended as waiting, not in progress", () => {
    const v = foldRun([ev(1, "await", "info", { waiting_for: "merge" }), ev(2, "result", "info", { outcome: "awaiting_merge" })], null, false);
    expect(v.steps.at(-1)?.status).toBe("waiting");
  });
});

describe("money and text formatting", () => {
  it("keeps four decimals under one cent", () => {
    expect(usd(0.004)).toBe("$0.0040");
    expect(usd(0.0852)).toBe("$0.09");
    expect(usd(0)).toBe("$0.00");
    expect(usd(1.5)).toBe("$1.50");
    expect(usd(null)).toBe("not set");
  });
  it("fills a real 30-day axis", () => {
    const d = fillDays([{ day: "2026-11-08", usd: 0.2 }, { day: "2026-09-01", usd: 9 }], "2026-11-10");
    expect(d).toHaveLength(30);
    expect(d[0].day).toBe("2026-10-12");
    expect(d.at(-1)).toEqual({ day: "2026-11-10", usd: 0 });
    expect(d.at(-3)).toEqual({ day: "2026-11-08", usd: 0.2 });
    expect(d.reduce((a, x) => a + x.usd, 0)).toBe(0.2);
  });
  it("turns ISO timestamps in detail text into readable times", () => {
    const t = humanizeTimes("waiting for budget since 2026-11-10T07:11:57+00:00", new Date("2026-11-10T09:11:57Z"));
    expect(t).not.toContain("2026-11-10T");
    expect(t).toMatch(/^waiting for budget since .+ \(2 hours ago\)$/);
  });
  it("describes discovery coverage as a sentence", () => {
    const c = { owner: "octo-demo", listed: 12, public: 3, private: 9, skipped_not_owned: 0, complete: true, limitations: [], token_kind: "classic" };
    expect(coverageText(c)).toBe("12 repositories owned by octo-demo: 3 public, 9 private. Coverage complete.");
    expect(coverageText({ ...c, complete: false, limitations: ["Private repositories were not visible."] })).toMatch(/incomplete: Private repositories/);
  });
});
describe("withoutUrls", () => {
  it("removes an address shown elsewhere and keeps the rest of the sentence", () => {
    const pr = "https://github.com/o/r/pull/1";
    expect(withoutUrls(pr, pr)).toBe("");
    expect(withoutUrls(`opened ${pr}; waiting for review`, pr)).toBe("opened; waiting for review");
    expect(withoutUrls("no address here", null)).toBe("no address here");
    expect(withoutUrls(null, pr)).toBe("");
  });
});
