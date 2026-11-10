# Local API and event contract (v1)

The setup interface is a browser page served by `readme-monitor ui` from the same machine. The
Python backend owns all state, schedules, credentials and publication; the page only displays and
controls it. This document is the contract between the two. It applies to ReadmeMonitor 0.3.x
(database schema 3).

## Transport and security

- The server binds `127.0.0.1` only (default port 8765, next free port if taken). It is never
  meant to be reachable from another computer.
- **Pairing.** The launcher writes a random single-use code to
  `<profile>/.readme-monitor-ui/<instance>-pair.json`, in the account's own profile folder (the
  folder that holds `.readme-monitor.json`), never in the runtime folder, whose permissions may be
  shared; `<instance>` is the first 16 hex digits of the SHA-256 of the normalized runtime folder
  path (the `X-RM-Instance` header). On Linux and macOS the folder is mode 700 and the file 600. A
  pairing file that is a symbolic link is refused and removed. The launcher then opens `http://127.0.0.1:<port>/auth?code=<code>`. The server
  accepts a code once, within two minutes, deletes the file, sets the session cookie
  `rm_session` (`HttpOnly; SameSite=Strict; Path=/`) and redirects to `/`. The pairing code is not
  a credential and is useless afterwards. Without a session, `/` shows a short "open from the
  launcher" page and every `/api/*` request answers `401`.
- **Host check.** Requests whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>`
  are refused (`403`), which blocks DNS-rebinding pages.
- **Mutations** (`POST`, `PUT`, `DELETE`) also need an `Origin` header equal to the server's own
  origin and the header `X-RM-CSRF` with the value from `GET /api/session`.
- Responses carry `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'
  'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none';
  base-uri 'none'; form-action 'self'`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, and `Cache-Control: no-store` on `/api/*`.
- Credentials travel only in a request body to the backend, are stored in the operating system's
  credential store and are never returned, logged, written to events or placed in a URL.
- Every string that originates in a repository, GitHub or the model is data. The backend redacts
  secret-looking values before storing events; the page renders such text as text or through a
  Markdown renderer with raw HTML disabled.
- Errors: HTTP 4xx/5xx with `{"error": {"code": "<snake_case>", "message": "<for people>"}}`.

## Types

```ts
type Iso = string;            // UTC, ISO 8601, seconds precision
type RepoName = string;       // "owner/name"

interface Session { csrf: string; version: string; simulation: boolean }

interface State {
  version: string;
  simulation: boolean;         // true: fake GitHub and Claude; nothing real is contacted
  now: Iso;
  cursor: number;              // id of the newest stored event
  installation: {
    home: string; host: string; account: string; platform: string;
    supported: boolean;        // false where the worker cannot be activated (not Windows)
    active: boolean;
    problem: string | null;    // why live work is refused here, in plain language
    activation: { host: string; activated_at: Iso; installation_id: string; migrated_from?: string } | null;
    host_lock: string | null;  // optional [installation] host_lock from config
    can_change_home: boolean;  // only while inactive
  };
  runtime: {
    state: "inactive" | "unavailable" | "paused" | "running" | "scheduled_idle" | "manual_idle";
    detail: string;
    current_run: Run | null;
    last_run: Run | null;
    next_check: Iso | null;
  };
  credentials: {
    github: { present: boolean; source: string; login: string | null };
    anthropic: { present: boolean; source: string };
  };
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
  discovery: { enabled: boolean; owner: string; last: Record<string, unknown> | null };
  repos: RepoView[];
  onboarding: { complete: boolean; steps: { id: OnboardingStep; done: boolean; detail: string }[] };
}

type OnboardingStep = "runtime" | "github" | "repositories" | "model" | "publication" | "dry_run";

interface Run {
  id: number; trigger: "scheduled" | "manual" | "dry_run" | "init" | "preview" | "preview-api" | "test";
  started_at: Iso; finished_at: Iso | null;
  outcome: string | null;      // ok | error | crashed | cancelled | credentials_missing | null while running
  summary: Record<string, number | string | boolean> | null;
  // live run: {<repository state>: count, ..., seconds}; dry run: {previewed?, error?, dry_run: true};
  // crashed or credentials_missing: {error} (crashed: the exception type only)
}
// A run that ends crashed or credentials_missing also stores one `run` event with status "failed"
// and data {outcome, error?}; every other run ends with a `run` "done" event.
// Onboarding: dry_run is done only by a finished dry run with outcome ok that previewed at least
// one repository; repositories and publication are also satisfied by account-wide discovery
// ([discovery] owner) without [[repo]] entries.

interface Schedule {
  mode: "manual" | "interval" | "daily" | "weekly";
  interval_minutes: number;    // interval mode; 5..1440 (presets 15, 60, 360)
  time: string;                // "HH:MM", daily and weekly
  weekdays: ("mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun")[];  // weekly
  timezone: "local" | "utc";   // local follows this computer's clock, including daylight saving.
                               // Weekly schedules must be local: Windows counts weekdays in local time.
}

interface ScheduleState {
  configured: Schedule;        // what config.toml says
  effective: Schedule | null;  // read back from Windows Task Scheduler; null when no task exists
  in_sync: boolean;            // effective equals configured
  task: { installed: boolean; enabled: boolean; status: string; next_run: Iso | null;
          last_run: Iso | null; last_result: string | null } | null;
  // schtasks prints its list in the display language; times are read only from the English form.
  // Elsewhere the task is known from its XML definition: next_run is then the product's own next
  // check for daily and weekly schedules and null for intervals, last_run is null, status is "".
  summary: string;             // "Every 15 minutes", "Weekdays at 09:00 (this computer's time)"
  next_checks: Iso[];          // up to three; the first comes from Task Scheduler when it reports one
  timezone_name: string;       // this computer's zone, for display
  notes: string[];             // sign-in and sleep requirements, why a check may not produce an update
}

interface RepoView {
  name: RepoName; repo_id: number | null; visibility: string | null;
  source: "config" | "discovered"; enrolled: boolean;      // enrolled: has its own [[repo]] entry
  mode: "preview" | "review_pr" | "auto_pr" | "direct" | null;
  api_export: "none" | "allowlist" | null;
  restricted: boolean;
  branch: string | null; readme_paths: string[];
  managed_sections: string[]; disabled_sections: string[];
  source_include: string[]; source_exclude: string[];
  paused: boolean;
  disposition: string; detail: string;                      // discovery.disposition()
  state: RepoState; state_detail: string | null;            // worker state, see architecture.md
  head: string | null; last_checked: Iso | null;
  pending_since: Iso | null; next_eligible: Iso | null;
  last_generated: Iso | null; last_published: Iso | null;
  proposal: { id: number; state: ProposalState; mode: string; pr_number: number | null; pr_url: string | null;
              published_sha: string | null; detail: string | null; created: Iso } | null;
  latest_draft_id: number | null;
  spent_month_usd: number;
}

type RepoState =
  "new" | "unchanged" | "no_changes" | "previewed" | "proposal_created" | "awaiting_merge" | "waiting_for_checks"
  | "pushed" | "published" | "merged_unconfirmed" | "rejected" | "closed" | "conflict" | "stale" | "cancelled"
  | "generation_deferred" | "budget_paused" | "adoption_deferred" | "export_not_approved" | "credentials_missing"
  | "needs_init" | "unsupported_readme" | "publish_blocked" | "paused" | "backoff" | "rate_limited" | "error"
  | "inaccessible" | "permission_missing" | "empty" | "archived" | "renamed" | "transferred";

type ProposalState = "preview" | "publishing" | "awaiting_merge" | "published" | "rejected" | "superseded" | "failed";
// A direct publication refused after an earlier README path was committed is "failed" with
// published_sha set to that commit and a detail naming the committed and the refused paths.

interface Event {
  id: number;                  // cursor; strictly increasing
  at: Iso;
  run_id: number | null;
  trigger: Run["trigger"] | null;
  repo: RepoName | null; repo_id: number | null;
  stage: "run" | "discover" | "queue" | "inspect" | "evidence" | "draft" | "validate" | "publish" | "await" | "result";
  status: "started" | "done" | "skipped" | "failed" | "info";
  revision: string | null;     // source revision the event refers to
  message: string;             // short, safe, for people (at most 300 characters)
  data: EventData;
}

interface Draft {
  id: number; at: Iso; run_id: number | null; repo: RepoName;
  kind: "dry_run" | "proposal" | "adoption";
  proposal_id: number | null;
  files: Record<string, { old: string; new: string; diff: string; changed: string[]; conflicts: string[] }>;
  flags: string[]; notes: string[];
  estimate_usd: number | null;
}
```

### Event data by stage

All keys are optional. Lists are bounded as noted; `truncated: true` means more existed.

| Stage | Keys |
| --- | --- |
| `run` | `summary`, `seconds`, `repos` (count), `dry_run` |
| `discover` | `listed`, `new`, `renamed`, `missing`, `transferred`, `complete` |
| `queue` | `position`, `reason` (for example `backoff`, `queued_next_cycle`) |
| `inspect` | `baseline` (previous analyzed head or null), `head`, `first_scan`, `compare_status` (`ahead`, `diverged`, `behind`, `identical`, `unavailable`), `commits` (at most 20 of `{sha, message, author, date}`), `commits_total`, `files_changed` (at most 50 of `{path, status, additions, deletions}`), `files_total`, `truncated`, `checks_pending` |
| `evidence` | `export` (`allowlist` or `none`), `files_read` (paths actually loaded, at most 60), `skipped` (reasons), `facts` (count), `readmes` (paths), `payload_chars` |
| `draft` | `mode` (`claude`, `stored`, `deterministic`, `dry_run`), `sections_requested`, `sections_returned`, `model`, `estimate_usd`, `usage` (`{input_tokens, output_tokens, cost_usd}`). No `draft_id`: the draft is stored after this event; its id arrives on `validate` and, for dry runs, on `result` |
| `validate` | `changed` (`{path: [sections]}`), `conflicts` (`{path: [sections]}`), `flags`, `notes`, `draft_id`, `error` |
| `publish` | `mode`, `branch`, `proposal_id`, `pr_number`, `pr_url`, `commit_sha`, `commit_url`, `auto_merge` |
| `await` | `waiting_for` (`checks`, `merge`, `merge_confirmation`), `pr_url`, `since` |
| `result` | `outcome`, `state`, `detail`, `proposal_id`, `pr_url`, `commit_sha`, `draft_id` (dry runs) |

`result.outcome` is one of: `confirmed` (README verified on the target branch), `pushed` (a commit
or pull request exists on GitHub but the target branch is not yet confirmed), `awaiting_merge`,
`awaiting_checks`, `preview` (a local draft only), `no_change`, `unchanged`, `deferred`, `paused`,
`blocked`, `conflict`, `failed`, `cancelled`. A dry run's result is `preview` when a section would
change or a live run would ask the model to draft one, `no_change` otherwise, and `failed` or
`blocked` when the repository could not be read. A direct publication refused after an earlier
README path was committed ends with `outcome: "pushed"`, `state: "publish_blocked"`, that commit in
`commit_sha` and a `detail` naming what was committed and what was refused; it is not retried
until the inputs change.

Rules the backend follows when emitting events:

- An event describes something the worker actually did. `commits` come from GitHub's compare
  answer for `baseline...head`; `files_read` lists only files whose content was loaded.
- A first scan has no baseline: `first_scan` is true, `commits` holds only the head commit and
  `compare_status` is absent.
- Rewritten history gives `compare_status: "diverged"`; the commit list is then the commits
  reachable from the head that GitHub returned, and change detection still relies on content
  fingerprints, not on this list.
- There is no progress percentage and no token stream. `draft` is `started` when the request is
  sent and `done` when the answer has been received and stored.
- `publish` `done` means GitHub accepted the write. Only `result` with `outcome: "confirmed"`
  means the README was read back from the target branch.
- A repository that is unchanged since its last check produces no event of its own; it is counted
  in the `run` summary. A cycle in which nothing needed attention stores one `run` `done` event.
- Events are kept for the newest 20,000 rows; drafts for the newest 300.

## Read endpoints

| Method and path | Answer |
| --- | --- |
| `GET /api/session` | `Session` |
| `GET /api/state` | `State` |
| `GET /api/events?after=<id>&limit=<n>&run_id=<id>&repo=<owner/name>` | `{events: Event[], cursor: number}`; ascending; `limit` at most 500. Without `after`, the newest `limit` events. |
| `GET /api/events/stream` | Server-sent events. Each stored event is sent as `event: activity` with `id: <event id>` and the `Event` as JSON data. Honors `Last-Event-ID` (or `?after=`), so a reconnect resumes without duplicates. A comment line is sent every 15 seconds as a heartbeat. |
| `GET /api/runs?limit=<n>` | `{runs: Run[]}` newest first |
| `GET /api/runs/<id>` | `{run: Run, events: Event[]}` |
| `GET /api/drafts/<id>` | `Draft` |
| `GET /api/usage` | `{rows: UsageRow[], by_day: {day: string, usd: number}[]}`; newest 100 ledger rows and 30 days of totals |
| `GET /api/diagnostics` | `{checks: {status: "PASS" \| "WARN" \| "FAIL", name: string, detail: string}[], generated_at: Iso}`; contains no secrets |

`UsageRow`: `{id, repo, run_id, model, state, reserved_usd, actual_usd, input_tokens, output_tokens, created_at}`.

## Mutating endpoints

All answer `{ok: true, ...}` plus the fields named below, or an error.

| Method and path | Body | Effect |
| --- | --- | --- |
| `POST /api/installation/activate` | `{confirm: true}` | Enables the worker on this computer for this runtime folder. Refused when unsupported, when `host_lock` names another computer, or when this account already has another active folder. |
| `POST /api/installation/deactivate` | `{confirm: true}` | Removes the scheduled task and revokes the activation. Data stays. |
| `POST /api/installation/home` | `{path}` | Only while inactive: use another runtime folder. The path must be absolute, on a local drive and writable. |
| `POST /api/credentials/<kind>` | `{value}` | `kind` is `github` or `anthropic`. Stores the secret in the OS credential store. GitHub: also answers `{login, scopes, token_kind}` from a read-only identity check: `scopes` is the raw `X-OAuth-Scopes` header text (for example `"repo, read:org"`) or null when GitHub sends none; `token_kind` is `"classic"` (a scope header was sent) or `"fine-grained or app"`. |
| `DELETE /api/credentials/<kind>` | | Removes the stored secret. |
| `POST /api/github/discover` | | Read-only listing of repositories the credential's account owns: `{login, coverage, repositories: {id, name, visibility, archived, fork, default_branch, enrolled, disposition, detail}[]}`; `coverage` is `{owner, listed, public, private, skipped_not_owned, complete: boolean, limitations: string[], token_kind: "classic" \| "fine-grained or app (no scope header)" \| "simulation"}`. Nothing is recorded. |
| `PUT /api/repos/<owner>/<name>` | `{branch?, readme_paths?, managed_sections?, disabled_sections?, publish_mode?, api_export?, source_include?, source_exclude?, paused?, confirm_export?, confirm_direct?}` | Creates or updates the `[[repo]]` entry. `api_export: "allowlist"` needs `confirm_export: true`; `publish_mode: "direct"` needs `confirm_direct: true`. An empty `managed_sections` list is refused (`400 bad_request`); omitting the field keeps the default sections. |
| `DELETE /api/repos/<owner>/<name>` | | Removes the `[[repo]]` entry. History and spending records stay. |
| `POST /api/repos/<owner>/<name>/adopt` | `{confirm: true}` | Active installations only. Proposes the one-time adoption (managed-block markers and missing sections) as a pull request, in every publication mode including `direct`. `{started: true}` or `{started: false, reason}`. |
| `PUT /api/settings` | `{budget?: {per_run_usd, daily_usd, monthly_usd, pacing}, model?, effort?, policy?, discovery?: {owner, publish_mode, adoptions_per_day, exclude}}` | Writes `config.toml` (validated first, written atomically; concurrent saves are serialized). An empty or blank `model` is refused (`400 bad_request`). |
| `PUT /api/schedule` | `Schedule` | Saves the schedule. When the installation is active the scheduled task is re-registered and read back: `{schedule: ScheduleState, applied: true}`. Otherwise `{applied: false, reason}`. A weekly schedule must use `timezone: "local"` (Windows counts weekdays in local time); `"utc"` answers `400 invalid_config`. |
| `POST /api/pause` / `POST /api/resume` | `{repo?}` | Whole monitor, or one repository. |
| `POST /api/run` | `{kind: "live" \| "dry_run", repos?: RepoName[]}` | Starts one cycle in a separate process. `{started: true}` or `{started: false, reason: "already_running" \| "inactive" \| "paused" \| "credentials_missing"}`. A dry run reads GitHub, writes nothing to GitHub and makes no model request; it works on an inactive installation. |
| `POST /api/cancel` | | Asks the running cycle to stop at the next safe boundary: `{requested: boolean}`. This covers every run already recorded and the run this server launched, even one still starting; a run that starts afterwards is never stopped by it. A request already sent to the model or GitHub is completed and reconciled, never abandoned. |

## Configuration additions (config.toml)

```toml
[installation]
host_lock = ""            # optional: only a computer with this name may be activated

[schedule]
mode = "interval"         # manual | interval | daily | weekly
interval_minutes = 15
time = "09:00"
weekdays = ["mon", "tue", "wed", "thu", "fri"]
timezone = "local"        # local | utc
```

Without a `[schedule]` table the schedule is `interval` every `[monitor] poll_interval_minutes`,
which is what earlier versions did. `[[repo]] publish_mode` gains `"direct"`; `[discovery]
publish_mode` does not.

## Simulation

`readme-monitor ui --demo` serves the same interface from a temporary runtime folder with an
in-memory GitHub and a scripted model. The real worker, validation, ledger and publication code
run against those stand-ins. `Session.simulation` and `State.simulation` are true and the page
shows a permanent "Simulation" label. Nothing is sent to GitHub or Anthropic and Task Scheduler
is not touched. Answers show the computer as `DEMO-PC`, the account as `demo` and the temporary
folder as `DEMO-FOLDER`. Fake commits are dated when they are made (the seeded history a few days
earlier). Each start removes `readme-monitor-demo-*` folders in the system temp directory that are
older than a day and whose server process is gone.
