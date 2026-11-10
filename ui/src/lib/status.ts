// Honest repository status from RepoView.state (worker states, docs/architecture.md). Pure, so it is tested.
import type { RepoView } from "../api/types";
import { relative } from "./format";

export type Tone = "ok" | "active" | "attention" | "fail" | "neutral";
export interface RepoStatus { tone: Tone; icon: string; label: string; reason: string | null }

const MAP: Record<string, [Tone, string, string]> = {
  unchanged: ["ok", "check", "Up to date"],
  no_changes: ["ok", "check", "Up to date"],
  published: ["ok", "check", "Published"],
  awaiting_merge: ["attention", "pr", "Pull request awaiting merge"],
  merged_unconfirmed: ["active", "pr", "Merged, awaiting confirmation"],
  pushed: ["active", "commit", "Pushed, awaiting confirmation"],
  proposal_created: ["active", "pr", "Proposal created"],
  waiting_for_checks: ["attention", "clock", "Waiting for checks"],
  generation_deferred: ["attention", "clock", "Update deferred"],
  budget_paused: ["attention", "clock", "Waiting for budget"],
  needs_init: ["attention", "alert", "Needs adoption"],
  adoption_deferred: ["attention", "clock", "Adoption deferred"],
  conflict: ["attention", "alert", "Conflict"],
  stale: ["attention", "alert", "Stale"],
  rejected: ["attention", "x", "Proposal rejected"],
  renamed: ["attention", "alert", "Renamed on GitHub"],
  transferred: ["attention", "alert", "Transferred on GitHub"],
  unsupported_readme: ["attention", "alert", "README not supported"],
  error: ["fail", "x", "Failed"],
  rate_limited: ["fail", "x", "Rate limited"],
  publish_blocked: ["fail", "x", "Publishing blocked"],
  permission_missing: ["fail", "key", "Permission missing"],
  credentials_missing: ["fail", "key", "Credential missing"],
  inaccessible: ["fail", "x", "Not accessible"],
  paused: ["neutral", "pause", "Paused"],
  archived: ["neutral", "minus", "Archived"],
  empty: ["neutral", "minus", "Empty repository"],
};

export function repoStatus(r: RepoView): RepoStatus {
  const reason = r.state_detail || r.detail || null;
  if (r.paused) return { tone: "neutral", icon: "pause", label: "Paused", reason };
  if (r.state === "new" || !r.last_checked) return { tone: "neutral", icon: "dot", label: "Not checked yet", reason };
  if (r.state === "proposal_created" && r.proposal?.state === "preview") return { tone: "active", icon: "doc", label: "Draft ready (preview)", reason };
  const m = MAP[r.state];
  // A state this page does not know yet is shown as it is, never as success.
  return m ? { tone: m[0], icon: m[1], label: m[2], reason } : { tone: "neutral", icon: "dot", label: `State: ${r.state}`, reason };
}

export const needsAttention = (r: RepoView) => { const t = repoStatus(r).tone; return t === "attention" || t === "fail"; };

/** What to say about a repository that has no events of its own in the viewed run. */
export function idleText(r: RepoView | null, now: Date = new Date()): string {
  if (!r) return "This run recorded no repository work.";
  if (r.state === "new" || !r.last_checked) return "This repository has not been checked yet.";
  if (["unchanged", "no_changes", "published"].includes(r.state)) return `Unchanged at the last check, ${relative(r.last_checked, now)}.`;
  return `Stored state: ${repoStatus(r).label.toLowerCase()}, last checked ${relative(r.last_checked, now)}.`;
}
