# Configuration reference

The configuration file is TOML, at `<home>\config.toml`, where `<home>` is your runtime folder
(by default `%USERPROFILE%\ReadmeMonitor`). `readme-monitor init` and the setup interface create
it from the starter file
[`readme_monitor/templates/config.example.toml`](../readme_monitor/templates/config.example.toml).
`readme-monitor doctor` validates it. Unknown keys are rejected so typos do not silently change
behavior.

**The interface edits this file.** Every change you make in the setup interface (repositories,
budget, model, schedule, discovery) is validated first and then written atomically to
`config.toml`; comments you typed by hand are not preserved when the file is rewritten. The first
time the interface rewrites a file you wrote by hand, it keeps a one-time copy next to it named
`config.toml.pre-ui-<time>.bak`. You can still edit the file by hand while the interface is
closed; the interface reads whatever is there. Credentials never go in this file.

## `[installation]`

| Key | Default | Meaning |
| --- | --- | --- |
| `host_lock` | `""` (off) | Optional. Only a computer with this name may enable the worker for this folder. Letters, digits and hyphens. |

Enabling the worker is recorded in `activation.json` inside the runtime folder, not here (see
[Architecture](architecture.md#activation)). `host_lock` adds a computer-name check on top of the
computer identity check; leave it empty unless you want that extra pin.

## `[schedule]`

When checks run. Windows Task Scheduler is the only scheduler. Saved in the interface or here,
then registered with `install-task` (the interface does this when the worker is enabled).

| Key | Default | Meaning |
| --- | --- | --- |
| `mode` | `interval` | `manual` (nothing runs until you start a check), `interval`, `daily` or `weekly` |
| `interval_minutes` | 15 | Interval mode; the interface accepts 5 to 1440, the file 1 to 1440 |
| `time` | `"09:00"` | Daily and weekly mode, 24-hour clock |
| `weekdays` | `["mon", "tue", "wed", "thu", "fri"]` | Weekly mode: any of `mon` to `sun` |
| `timezone` | `local` | `local` follows this computer's clock, including daylight saving; `utc` is fixed |

Without a `[schedule]` table the schedule is `interval` every `[monitor] poll_interval_minutes`,
which is what earlier versions did. The starter file includes a `[schedule]` table of 15 minutes.

## `[monitor]`

| Key | Default | Meaning |
| --- | --- | --- |
| `poll_interval_minutes` | 5 | Interval used when there is no `[schedule]` table |
| `model` | `claude-sonnet-5-5` | Anthropic model id |
| `effort` | `medium` | Claude effort level: `low`, `medium` or `high` |
| `max_output_tokens` | 8000 | Output cap per request; reserved in full against the budget |
| `request_timeout_seconds` | 120 | One Claude or GitHub request |
| `max_attempts` | 2 | Claude attempts per README; each reserves budget |
| `run_timeout_seconds` | 600 | Whole check; repositories not reached are deferred |
| `max_repos_per_run` | 10 | Least recently checked first |
| `check_wait_minutes` | 30 | How long to wait for pending CI checks |
| `bot_branch` | `readme-monitor/update` | The monitor-owned branch in each repository |
| `max_file_bytes` | 60000 | Larger files are never sent |
| `max_payload_chars` | 180000 | Request size cap; largest files are dropped first |
| `quiet_minutes` | 30 | No paid generation until the branch has not moved for this long, so a burst of commits costs one request |
| `min_generation_interval_hours` | 12 | At most one paid generation per repository in this window |
| `max_postpone_hours` | 24 | After this long pending, constant commits no longer delay generation (the minimum interval still applies) |

With these defaults a README reflects a change within about 30 minutes when the repository is
quiet and funds are available, and at worst about a day later for a repository that changes all
the time. Not every commit gets a paid update: many changes are coalesced into one.

## `[budget]` (US dollars)

`per_run_usd`, `daily_usd`, `monthly_usd`. All three must be set before any paid request is
made. Days and months are UTC calendar periods. There are no defaults: enter only limits you
have decided on. They are shared by every repository, README file, retry, `preview --api` and
scheduled check that uses this runtime folder's database; they are not per-repository allowances.

`pacing` (default `even`): month-to-date spend may reach the share of `monthly_usd` for the days
started so far, plus one `per_run_usd`. For example, in a 30-day month with a monthly limit of 30 and a
per-run limit of 1, pacing allows 2 on the 1st, 16 on the 15th and 30 on the last day. `none` turns
pacing off (the other limits still apply). When money is short, the repository that has been
waiting longest is funded first; the others keep their pending work and show `budget_paused`
with the reason.

The ledger counts only requests this monitor makes through this database. Claude usage by other
tools on the same Anthropic account is not visible to it.

## `[pricing."<model>"]` (optional)

`input`, `output`, `cache_write`, `cache_read` in US dollars per million tokens. A built-in price
exists for `claude-sonnet-5-5` (checked 2026-11-09); a model with no known price cannot be used
for paid generation until you add its price here.

## `[[repo]]` (one per enrolled repository)

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | required | `owner/repository` |
| `branch` | `""` | Target branch; empty follows the current default branch |
| `readme_paths` | `["README.md"]` | The only files the monitor may write |
| `source_include` | `[]` | Glob patterns (`**` = any folders) of files that may be read |
| `source_exclude` | `[]` | Patterns removed from the include list |
| `template_version` | 1 | PopL Repository Standard v1 |
| `managed_sections` | all except overview, roadmap, contributing, license | Section ids the monitor writes |
| `disabled_sections` | `[]` | Sections left out entirely |
| `publish_mode` | `preview` | `preview`, `review_pr`, `auto_pr` or `direct` (see below) |
| `api_export` | `none` | `none` never sends source to Claude; `allowlist` sends `source_include` files |
| `daily_usd`, `monthly_usd` | none | Optional per-repository limits |
| `paused` | false | Pause this repository |

Repository access alone never enrolls a repository or authorizes sending its code. Each
repository is listed here deliberately, and `api_export` must be set to `allowlist` explicitly
(in the interface this needs a confirmation). A `[[repo]]` entry overrides discovery defaults.

**Publication modes.**

| Mode | Result |
| --- | --- |
| `preview` | Drafts are written under `<home>\previews`. Nothing is written to GitHub. |
| `review_pr` | One monitor-owned branch and one pull request per repository, for a person to review. |
| `auto_pr` | As above, and asks GitHub to merge it automatically once your branch rules and checks pass. Withheld when the content has review flags. |
| `direct` | A normal (never forced) commit straight to the target branch, pinned to the exact README version that was analyzed. Opt-in per repository, needs *Contents* write access, and does not work on a protected branch (the state becomes `publish_blocked` with the reason). Content with review flags and the one-time adoption of a README still go through a pull request. |

`direct` is not available for discovery defaults, only for an enrolled repository.

## `[discovery]` (optional: every repository an account owns)

| Key | Default | Meaning |
| --- | --- | --- |
| `owner` | `""` (off) | GitHub account whose own repositories are listed; must be the account the GitHub credential belongs to |
| `refresh_hours` | 6 | How often the scheduled check re-lists the account (`discover` does it on demand) |
| `publish_mode` | `review_pr` | Publishing mode for discovered repositories without a `[[repo]]` entry (`preview`, `review_pr` or `auto_pr`) |
| `managed_sections` | `["project-structure", "recent-changes"]` | Only sections built from GitHub metadata are allowed: discovery never sends source to Claude |
| `adoptions_per_day` | 3 | Adoption pull requests opened per UTC day across all repositories (0 = only by `init`) |
| `exclude` | `[]` | `owner/name` entries listed in the inventory but never processed |

Discovered repositories are tracked by GitHub's repository id, so a rename keeps their history,
spending and pending work. Repositories the account only collaborates on are ignored. Their
README is the one GitHub shows (`.github/`, then the root, then `docs/`), with its actual file
name; a non-Markdown README is reported and left alone. Adoption only adds the missing managed
sections: a hand-written section with a standard name (for example "Changelog" or "Project
structure") stays with people and is never wrapped or replaced. A repository with no README gets a
minimal proposal (title, GitHub description, structure, releases); an empty repository waits.

## `[policy]`

`export_restricted`: `owner/name` entries whose content may never be sent to Claude, whatever
`[[repo]]` or discovery says. A `[[repo]]` entry with `api_export = "allowlist"` for one of them is
a configuration error (the interface answers with a conflict). The restriction is stored with the
repository id, so it survives renames. Lifting it needs a deliberate edit of this file.
