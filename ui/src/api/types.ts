// Types from docs/api.md (contract v1). Keep in step with that document; it is the source of truth.
export type Iso = string;
export type RepoName = string;

export interface Session { csrf: string; version: string; simulation: boolean }

export type RuntimeState = "inactive" | "unavailable" | "paused" | "running" | "scheduled_idle" | "manual_idle";
export type OnboardingStep = "runtime" | "github" | "repositories" | "model" | "publication" | "dry_run";
export type PublishMode = "preview" | "review_pr" | "auto_pr" | "direct";
export type Weekday = "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun";

export interface Run {
  id: number; trigger: "scheduled" | "manual" | "dry_run" | "init" | "preview" | "preview-api" | "test";
  started_at: Iso; finished_at: Iso | null;
  outcome: string | null;
  summary: Record<string, number | string> | null;
}

export interface Schedule {
  mode: "manual" | "interval" | "daily" | "weekly";
  interval_minutes: number;
  time: string;
  weekdays: Weekday[];
  timezone: "local" | "utc";
}

export interface ScheduleState {
  configured: Schedule;
  effective: Schedule | null;
  in_sync: boolean;
  task: { installed: boolean; enabled: boolean; status: string; next_run: Iso | null; last_run: Iso | null; last_result: string | null } | null;
  summary: string;
  next_checks: Iso[];
  timezone_name: string;
  notes: string[];
}

export interface Proposal { id: number; state: string; mode: string; pr_number: number | null; pr_url: string | null; published_sha: string | null; detail: string | null; created: Iso }

export interface RepoView {
  name: RepoName; repo_id: number | null; visibility: string | null;
  source: "config" | "discovered"; enrolled: boolean;
  mode: PublishMode | null;
  api_export: "none" | "allowlist" | null;
  restricted: boolean;
  branch: string | null; readme_paths: string[];
  managed_sections: string[]; disabled_sections: string[];
  source_include: string[]; source_exclude: string[];
  paused: boolean;
  disposition: string; detail: string;
  state: string; state_detail: string | null;
  head: string | null; last_checked: Iso | null;
  pending_since: Iso | null; next_eligible: Iso | null;
  last_generated: Iso | null; last_published: Iso | null;
  proposal: Proposal | null;
  latest_draft_id: number | null;
  spent_month_usd: number;
}

export interface State {
  version: string;
  simulation: boolean;
  now: Iso;
  cursor: number;
  installation: {
    home: string; host: string; account: string; platform: string;
    /** The system scheduler and credential store of this computer, named for people (e.g. "Windows Task Scheduler"). */
    scheduler: string; credential_store: string;
    /** Why this copy cannot register scheduled checks (an AppImage), or null. */
    scheduling_unavailable: string | null;
    desktop: boolean;
    supported: boolean; active: boolean; problem: string | null;
    activation: { host: string; activated_at: Iso; installation_id: string; migrated_from?: string } | null;
    host_lock: string | null; can_change_home: boolean;
  };
  runtime: { state: RuntimeState; detail: string; current_run: Run | null; last_run: Run | null; next_check: Iso | null };
  credentials: { github: { present: boolean; source: string; login: string | null }; anthropic: { present: boolean; source: string } };
  budget: {
    configured: boolean;
    per_run_usd: number | null; daily_usd: number | null; monthly_usd: number | null;
    pacing: "even" | "none";
    spent_today_usd: number; spent_month_usd: number;
    pacing_limit_today_usd: number | null;
    open_reservations: number; open_reserved_usd: number;
  };
  model: { id: string; effort: "low" | "medium" | "high"; priced: boolean; provider: "anthropic" };
  policy: { quiet_minutes: number; min_generation_interval_hours: number; max_postpone_hours: number };
  schedule: ScheduleState;
  // publish_mode, adoptions_per_day and exclude are not in contract v1 yet; read them only when present.
  discovery: { enabled: boolean; owner: string; last: Record<string, unknown> | null; publish_mode?: PublishMode; adoptions_per_day?: number; exclude?: string[] };
  repos: RepoView[];
  onboarding: { complete: boolean; steps: { id: OnboardingStep; done: boolean; detail: string }[] };
}

export type Stage = "run" | "discover" | "queue" | "inspect" | "evidence" | "draft" | "validate" | "publish" | "await" | "result";
export type EventStatus = "started" | "done" | "skipped" | "failed" | "info";

export interface CommitInfo { sha: string; message: string; author: string; date: Iso }
export interface FileChange { path: string; status: string; additions: number; deletions: number }

// All keys optional (docs/api.md "Event data by stage").
export interface EventData {
  summary?: unknown; seconds?: number; repos?: number; dry_run?: boolean;
  listed?: number; new?: number; renamed?: number; missing?: number; transferred?: number; complete?: boolean;
  position?: number; reason?: string;
  baseline?: string | null; head?: string; first_scan?: boolean; compare_status?: "ahead" | "diverged" | "behind" | "identical" | "unavailable";
  commits?: CommitInfo[]; commits_total?: number; files_changed?: FileChange[]; files_total?: number; truncated?: boolean; checks_pending?: boolean;
  export?: "allowlist" | "none"; files_read?: string[]; skipped?: string[]; facts?: number; readmes?: string[]; payload_chars?: number;
  mode?: string; sections_requested?: string[]; sections_returned?: string[]; model?: string; estimate_usd?: number;
  usage?: { input_tokens: number; output_tokens: number; cost_usd: number }; draft_id?: number;
  changed?: Record<string, string[]>; conflicts?: Record<string, string[]>; flags?: string[]; notes?: string[]; error?: string;
  branch?: string; proposal_id?: number; pr_number?: number; pr_url?: string; commit_sha?: string; commit_url?: string; auto_merge?: boolean;
  waiting_for?: "checks" | "merge" | "merge_confirmation"; since?: Iso;
  outcome?: string; state?: string; detail?: string;
}

export interface RmEvent {
  id: number; at: Iso; run_id: number | null; trigger: Run["trigger"] | null;
  repo: RepoName | null; repo_id: number | null;
  stage: Stage; status: EventStatus; revision: string | null; message: string; data: EventData;
}

export interface DraftFile { old: string; new: string; diff: string; changed: string[]; conflicts: string[] }
export interface Draft {
  id: number; at: Iso; run_id: number | null; repo: RepoName;
  kind: "dry_run" | "proposal" | "adoption"; proposal_id: number | null;
  files: Record<string, DraftFile>; flags: string[]; notes: string[]; estimate_usd: number | null;
}

export interface UsageRow { id: number; repo: string; run_id: number | null; model: string; state: string; reserved_usd: number; actual_usd: number; input_tokens: number; output_tokens: number; created_at: Iso }
export interface Usage { rows: UsageRow[]; by_day: { day: string; usd: number }[] }

export interface DiscoveredRepo { id: number; name: RepoName; visibility: string; archived: boolean; fork: boolean; default_branch: string; enrolled: boolean; disposition: string; detail: string }
export interface Coverage { owner: string; listed: number; public: number; private: number; skipped_not_owned: number; complete: boolean; limitations: string[]; token_kind: string }
export interface Discovery { login: string; coverage: Coverage; repositories: DiscoveredRepo[] }
/** GitHub identity check. `scopes` is the raw header ("repo, workflow") or null; token_kind is "classic" or "fine-grained or app (no scope header)". */
export interface CredentialAnswer { login?: string; scopes?: string | null; token_kind?: string }
export interface Diagnostics { checks: { status: "PASS" | "WARN" | "FAIL"; name: string; detail: string }[]; generated_at: Iso }

export interface RepoUpdate {
  branch?: string; readme_paths?: string[]; managed_sections?: string[]; disabled_sections?: string[];
  publish_mode?: PublishMode; api_export?: "none" | "allowlist"; source_include?: string[]; source_exclude?: string[];
  paused?: boolean; confirm_export?: boolean; confirm_direct?: boolean;
}

export interface ApiErrorBody { error: { code: string; message: string } }
