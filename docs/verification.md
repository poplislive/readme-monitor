# Verification record

What has actually been checked, for which revision, and what has not. Simulated tests are not
verification against the real services. Dates are UTC.

Every check below carries one of these labels:

| Label | Meaning |
| --- | --- |
| **simulation** | Real program code against in-memory stand-ins for GitHub, Claude and/or Task Scheduler |
| **local integration** | Real program code and real local software (Git, the file system, HTTP on this computer, pip, PowerShell); no GitHub, provider or scheduled run |
| **real GitHub** | Requests made to GitHub's real service |
| **real provider** | Requests made to the real model provider |
| **real scheduled host** | A run started by the real Windows Task Scheduler |
| **pending** | Owed by someone, not done yet |

## Version 0.3.0 (setup interface, activation, schedules, direct publication)

Environment: Windows 11 (build 26200), PowerShell 5.1 and 7.6, Python 3.12.10, Node 24.19 and npm 11.
Source: the working tree on top of commit `bb4f1ab` (branch `feature/public-app`), not yet
committed when the checks ran. Rows 1 to 11 were first run by the verification and release
specialist and rerun by the lead on the final tree (2026-11-10); rows 12 and 13 were run by the
lead; rows marked pending are owed. Rows 1, 5, 14 and 15 were rerun later on 2026-11-10 against the
public repository `poplislive/readme-monitor` (commit `78061ed` with the fixes listed below).

| # | Check | Command (working directory: the repository) | Label | Result |
| --- | --- | --- | --- | --- |
| 1 | Whole automated suite | `.\.venv\Scripts\python.exe -m pytest -q` | simulation + local integration | 282 passed, 1 skipped, exit code 0 (also in a fresh installation made by `install.ps1 -Dev` from the public repository). The skipped test needs permission to create symbolic links, which this Windows account lacks |
| 2 | The eleven acceptance criteria | `.\.venv\Scripts\python.exe -m pytest -q tests/test_acceptance_product.py tests/test_acceptance_git_remote.py tests/test_acceptance_interface.py` | simulation + local integration | 25 passed |
| 3 | Direct publication against a real Git repository (fast-forward, expected-blob check, trailer, a concurrent commit by another person is never overwritten) | `tests/test_acceptance_git_remote.py` | local integration (a local bare Git repository, not GitHub) | 3 passed |
| 4 | Compiled interface scan: no secret pattern, local path, external address (other than XML namespaces and documentation links), inline script, event-handler attribute, `eval` or source map; reduced-motion rule present; every file the page names exists and is served | `tests/test_acceptance_interface.py` | local integration | 4 passed |
| 5 | A clean copy installs and serves the built interface: copy of the tree to a new temporary folder, its own `install.ps1` with `-RuntimeHome` inside that folder, `ui --no-browser`, pairing, `/` and `/api/state` over HTTP, a reused sign-in address refused, server stopped, folder removed | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify-clean-install.ps1` (Windows PowerShell 5.1) | local integration (pip downloads the pinned libraries from the package index; no credential, no task, nothing written outside the temporary folder) | 16 `PASS` lines, no `FAIL`, "All checks passed", exit code 0; afterwards no account pointer file, no runtime folder and no scheduled task existed |
| 6 | Installer errors are plain: a wrong `-PythonExe`, and no Python on `PATH` | `install.ps1 -PythonExe C:\nowhere\python.exe`; `install.ps1` with `PATH` reduced to the system folder | local integration | Both print one `ERROR:` sentence naming the fix and exit with code 1 (no stack trace) |
| 7 | Upgrade run and opt-in scheduling: a second `install.ps1` run keeps an edited `config.toml`; `-InstallTask` on an installation that was never enabled | `install.ps1 -RuntimeHome <temp> -InstallTask` in a temporary copy | local integration | `config.toml` byte-identical; message "The scheduled task was NOT registered"; exit code 2; no `activation.json`; no scheduled task exists |
| 8 | Launchers | `ReadmeMonitor.cmd -NoBrowser -Port <n> -RuntimeHome <temp>`; `scripts\start.ps1 -NoBrowser -Port <n> -RuntimeHome <temp>`; `start.ps1` in a tree without `.venv` | local integration | Both launchers started the interface on the requested port with the requested folder and printed a sign-in address; without `.venv` the message says to run the installer, exit code 1 |
| 9 | Uninstaller | `scripts\uninstall.ps1 -RuntimeHome <temp>` with and without `-RemoveData` / `-RemoveCredentials` (credential store replaced by a null store; a read-only query first confirmed no real scheduled task existed) | local integration | Default: nothing deleted, remaining folders listed. `-RemoveData` with a wrong answer: nothing deleted. With `DELETE`: only the temporary runtime folder removed. A folder without `config.toml`/`state.db` is refused |
| 10 | Interface build and unit tests | `npm run typecheck`, `npx vite build --outDir <temp>`, `npm test` in `ui/` | local integration | Typecheck clean. A rebuild reproduced the committed asset names. 22 unit tests passed |
| 11 | README managed blocks and links | `readme_monitor.readme.parse` over `README.md`; `tests/test_cli_config_scheduler.py::test_readme_relative_links_exist` and `::test_cli_commands_documented_in_readme_exist` | local integration | No block reports a person's edit; relative links resolve; every `readme-monitor <command>` in the docs exists |
| 12 | The real interface against the real server in simulation mode: setup, dry run, live runs, schedule, per-repository pause, layout at several sizes, frame timing | `readme-monitor ui --demo` in the built-in browser and in headless Chrome 157.0.8103.39 through its DevTools protocol | simulation (server, pipeline and interface real; GitHub and model simulated) | No console errors. No horizontal page scroll at 3840×2160, 2560×1440, 1920×1080, 1024×768, 768×1024, 390×844 and 375×812. On a phone width every control was at least 40 px. During a live simulated run at an *emulated* 3840×2160 viewport: 1,319 frames in 11.9 s, median 6.9 ms, 95th percentile 7.0 ms, 99th 13.9 ms, three frames over 33 ms (one, about 1 s, during a screenshot capture). This computer's display is 1920×1080 at 144 Hz with an RTX 3070: a physical 4K display was **not** measured. Keyboard-only and screen-reader passes were not repeated after the last repair. Screenshots: `docs/screenshots/` |
| 13 | Real Windows Task Scheduler with the product's own task definitions, but a harmless action (`cmd /c`) under a throwaway task name, never the worker | a lead-written script: register, read back with `scheduler.parse_task_xml`, start, start again while running, replace the definition while running, delete | real Task Scheduler (not a ReadmeMonitor run) | Every schedule form registered and read back as configured: every 15, 45, 60 and 360 minutes, daily (local and UTC), weekly (local), manual. `IgnoreNew` and `StartWhenAvailable` survive registration. A second start while one instance ran did not create a second instance. Replacing the definition during a run neither stopped nor doubled the running instance, and the new schedule was in effect. The task was removed afterwards. Two defects found and fixed: Windows returns a UTC start time as local time with an offset (read-back now understands it), and Windows counts weekdays in local time even for a UTC time, so weekly schedules are now local only. A scheduled **worker** run was not performed (live work is not enabled on the development computer) |
| 14 | A first check against real GitHub with 0.3.0, read-only: the public repository `poplislive/readme-monitor` in `preview` mode with `api_export = "none"`, in a scratch runtime folder that was never enabled | `readme-monitor run-once --dry-run --repo poplislive/readme-monitor --home <scratch>`, then **Dry run** in the real interface (`ui --home <scratch>`, not `--demo`) | real GitHub (read-only) | Every stage recorded against the real head commit: inspect, evidence, draft without a model request, validate, result ("2 sections would change"). The proposed project-structure section matched the repository (it adds the licence files). No branch, pull request or commit appeared on GitHub; the token was not written to any file of the runtime folder. Found the draft-storage and layout defects listed below. The real provider and GitHub writes remain under "Not tested yet" (**pending: owner**) |
| 15 | The CI workflow on GitHub's runners | push to `main` of `poplislive/readme-monitor`, then read the run | GitHub Actions | Run 42869539533 failed on Windows (the Cancel defect below). After the fix, run 42873853678 passed all four jobs: Windows installer and tests, Linux tests, interface build with the committed-output check, clean install |
| 16 | The README pictures are regenerated from the simulation (2026-11-11) | `node scripts\screenshots.mjs`, with and without `--out <temp>`, eight runs; the **Screenshots** workflow on GitHub with **push** off | simulation; local integration (Chrome 157 through its DevTools protocol) | Seven runs exited with code 0 in about 30 s and wrote four pictures (1920×1080, 3840×2160, 390×844, 1920×1080). One stopped with "the browser opened no page" and saved nothing; since then the browser starts without extensions or background traffic and gets 30 s to open. No run left a temporary folder, browser profile, sign-in file or process behind. Compared with the earlier pictures, only relative times differed. Between two runs the setup and phone pictures were identical; the desktop pictures differed by rendering noise (at most 2 of 255 in a colour channel) and by the clock times of the runs. Found that the pictures showed the capturing computer's time zone (they now use UTC), and the 4K defect listed below. On GitHub's Windows runner the workflow (push off, run 42936617857) succeeded and its pictures matched these, except that the header said "Motion: reduced": the runner asks for reduced motion. The script now also pins full motion and normal colours; the rerun is recorded with the release |

Output of check 5 (the account name and folder paths that the installer's health check prints
were removed here; every line produced by the verification script itself is shown):

```text
PASS  candidate tree copied
PASS  compiled interface is part of the copy
PASS  install.ps1 finished
PASS  virtual environment created inside the copy
PASS  starter configuration created
PASS  installing activated nothing
PASS  interface server started and printed its sign-in address
PASS  without pairing the API answers 401
PASS  pairing succeeded and / serves the built page
PASS  the page script is served from this server
PASS  /api/state answers with the installed version
PASS  state shows an inactive, non-simulated installation
PASS  no credential was read or stored
PASS  the sign-in address works only once
PASS  nothing written to the account activation pointer
PASS  temporary folder removed
All checks passed
```

### Acceptance criteria and their tests

| # | Criterion | Test | Kind |
| --- | --- | --- | --- |
| 1 | A fresh user on a supported machine with a neutral computer name completes setup through the HTTP API | `test_criterion_01_…` | simulation (computer identity, credential store, GitHub, Task Scheduler); real server, config writer, database |
| 2 | An unactivated installation cannot spend, publish or schedule | `test_criterion_02_…` | simulation; the real guard is in force |
| 3 | Legacy migration keeps the activation, history, reservations and ledger; a copy on another machine stays inactive | `test_criterion_03_…` | simulation (machine identity); real migration code |
| 4 | A deterministic run goes inspect, evidence, draft, validate, publish, through the real components; direct publication also against a real local Git repository | `test_criterion_04_…` (4 tests, 3 of them in `test_acceptance_git_remote.py`) | simulation; local integration |
| 5 | No-change runs make no model call and no Git write; malformed output, a person's edit, a moved branch, denied permission, provider failure and an exhausted budget each show the right event and state in the API | `test_criterion_05_…` (7 tests) | simulation |
| 6 | Overlapping Run-now and scheduled triggers over several repositories never duplicate work, pull requests, commits or spending | `test_criterion_06_…` | simulation; two real concurrent CLI runs and the real worker lock |
| 7 | Schedule edits change the backend; pause, restart, daylight saving and calendar edges, missed runs and interrupted migrations recover | `test_criterion_07_…` (5 tests) | simulation. Windows enforcing the task policies is not simulated |
| 8 | Closing and reopening the interface changes no schedule and replays no side effect | `test_criterion_08_…` | simulation; two server lifetimes, stream reconnects |
| 9 | Secrets and unsafe Markdown cannot leak or execute through previews, events, diagnostics or the compiled assets | `test_criterion_09_…` (2 tests) | simulation (hostile repository text); real assets |
| 10 | The compiled page exists, is same-origin, honours reduced motion, ships no source maps | `test_criterion_10_…` | local integration. Not a browser test |
| 11 | A clean install serves the built page | `scripts/verify-clean-install.ps1` (check 5 above); `test_criterion_11_…` only guards what the script may not do | local integration |

**Defects found during 0.3.0 verification, all fixed with a regression test.** By the acceptance
tests: switching the schedule mode reported "out of sync" because unused fields were compared. By
the lead's real Task Scheduler check: the UTC read-back and the weekly UTC rule (row 13). By three
independent reviewers: the setup screens could go blank on the real server's answers; repository
status showed "Up to date" for repositories never checked or waiting on a pull request; the
single-use sign-in code could be read or planted by another local user when the runtime folder was
on a shared drive (moved into the account's own profile); concurrent configuration saves could lose
one; a direct commit to several README files could be reported as "nothing was written" after one
had landed; oversized activity data could be stored as broken JSON and stop the activity feed;
restoring an older database could lower recorded spending when two identical reservations fell in
the same second; a Cancel pressed while idle could stop the next run; a repository stopped by its
own per-repository limit held the shared queue for every other repository; emoji in a saved setting
made the configuration unwritable; a disabled task trigger was still reported as the schedule. By
the first CI run on GitHub's Windows runner, then two independent reviews of the fix: a Cancel
pressed while a run the interface had just launched was still starting was ignored, and cancelling
an adoption launched from the interface never stopped it. Cancel now also names the launched run
with a per-launch token, and a finished run no longer clears a request another run still needed.
Two narrow cases remain: a Cancel pressed in the moment between a scheduled or terminal check
taking its lock and recording its run (while it reads its credentials) is not applied to it, and a
dry run started from a terminal while nothing else runs is not seen by the interface, so Cancel
there reports `requested: false`. By the read-only check against real GitHub (row 14): stored
drafts kept only the first 2,000 characters of the README, its proposal and the diff, so the
review screen showed a tenth of a real README (drafts now keep up to 200,000 characters and say
when they are cut); long folder paths, wide README code blocks and the dry-run grid widened the
page on a phone (now no horizontal scroll at 375, 768 and desktop widths on every setup step and
workbench tab, with real data); and a hand edit of the README's own `status` block had left its
hash stale, so the monitor would have treated that block as a person's edit. By the personal-data audit before the release: commits the monitor makes carried the account's primary
e-mail address, because GitHub fills it in when no author is sent; they now name the account's
noreply address. By the screenshot refresh (row 16): on screens 3200 px and wider the hidden "Skip
to content" link showed its bottom edge as a dark bar at the top left, because it was moved up a
fixed 40 px while the type grows with the screen. It is now hidden by its own height plus a small
gap, and still appears 8 px from the top when focused (measured at 1920 and 3840 px). By the review
of the upgrade instructions before the release: the documented way back from 0.3.0 to 0.2.0
(`restore-db` the pre-upgrade backup with the older version) could not work, because 0.2.0 refuses
to open the migrated database before it can restore anything, and 0.3.0 upgrades whatever it
restores. The documentation and the error message now say that there is no supported way back
after a schema change and that the safe response is to pause the newer version.

### What the suite does not prove

- Anything that needs the real services: the simulated GitHub is a model of GitHub's answers, and
  the local bare Git repository proves Git's own semantics (fast-forward, compare-and-swap of
  the branch), not GitHub's Contents API, rate limits, permissions or branch protection.
- Windows enforcing the task's missed-run, overlap and daylight-saving behaviour. The tests prove
  the task definition and the computation of next checks, not what Windows does with them.
- Anything visual or physical: keyboard, touch, responsive layout, 4K and animation quality.

## Not tested yet

| Item | Why | How to verify |
| --- | --- | --- |
| A check with 0.3.0 against real GitHub: opening, updating and merging a pull request, `published` confirmation after the merge, and the following no-op | Needs a real repository and an approved credential | Enroll a throwaway repository in `review_pr` mode, run a check, merge, run again |
| Direct publication against GitHub's real Contents API, including a protected branch (expected: `publish_blocked`) | Needs a real repository | Enroll a throwaway repository in `direct` mode; repeat with branch protection on |
| A paid Claude generation with 0.3.0 and the real spending ledger | Needs an approved budget | A small budget on a throwaway repository, then `status` |
| A real Task Scheduler registration, read-back, scheduled start, catch-up after sleep, signed-out behaviour, daylight-saving change | Registering a task on this computer was not part of this check | Enable the worker, save a schedule, `run-task-now`, then `status` shows a run with trigger `scheduled` |
| Credential Manager access from the scheduled task | Depends on the row above | The scheduled run does not end in `credentials_missing` |
| The interface in real browsers (keyboard, touch, physical 4K displays, reduced motion at run time) | Needs a browser and a person | Use `ReadmeMonitor.cmd -Demo` |
| Task time display on a non-English Windows (the next and last run times are read from English output only) | Not available | Run `schedule` and look at the "Task Scheduler" line |
| Windows 10, and Python 3.11 specifically for the worker | Only Windows 11 with Python 3.12 was used | Run the installer there |
| The installer on a computer that never had development tools | Only a developer computer was available | Follow [Getting started](getting-started.md) on a clean one |
| `uninstall.ps1 -RemoveCredentials` against the real Credential Manager | Would delete real credentials | Run it on a computer where that is intended |
| `auto_pr` with branch protection or a merge queue | Not configured anywhere | Enable auto-merge in a throwaway repository's settings first |

## Earlier versions

These records are kept as history. The production-host guard they mention was replaced by
activation in 0.3.0 and its tests were replaced by `tests/test_engine_activation.py` and the
acceptance tests above.

### Version 0.1.0

Environment: a development computer (Windows 11 build 26200, PowerShell 7.6, Python 3.12.10).

| Check | Command | Revision | Kind | Result |
| --- | --- | --- | --- | --- |
| Automated tests | `.\.venv\Scripts\python.exe -m pytest -q` | `658da6d` | simulation | 85 passed |
| Clean clone install | `git clone …`, then `.\scripts\install.ps1 -Dev` in an empty folder | `658da6d` | local integration | Exit 0; no archived virtual environment used; 85 passed in the clone |
| CI on GitHub | Windows: installer + tests + task definition render; Linux (Python 3.11): tests | `658da6d` | simulation, hosted | Both jobs succeeded |
| Health check against GitHub | `readme-monitor doctor --api` | `658da6d` | real GitHub (read-only) | All PASS except expected WARN for budget (not configured) and scheduled task (not installed) |
| Model availability | part of `doctor --api` (model lookup, no tokens) | `658da6d` | real provider (lookup only) | The configured model was available to the account |
| Adoption check | `readme-monitor init --dry-run` | `658da6d` | real GitHub (read-only) | "already adopted; nothing to migrate" |
| Offline preview | `readme-monitor preview --repo <owner/name>` | `658da6d` | real GitHub (read-only) | No Claude request; no section changes |
| One cycle, preview mode | `readme-monitor run-once` | `658da6d` | real GitHub (read-only) | `{"no_changes": 1, "seconds": 1.9}` |
| Repeat cycle | `readme-monitor run-once` | `658da6d` | real GitHub (read-only) | `{"unchanged": 1, "seconds": 1.1}` (conditional requests only) |

The automated tests covered these behaviors (test names in `tests/test_worker.py` unless noted):

| Requirement | Tests |
| --- | --- |
| Unchanged inputs: no Claude request, no Git write | `test_unchanged_inputs_make_no_claude_request_and_no_git_write` |
| Relevant changes update only the right sections | `test_relevant_change_updates_only_returned_sections` |
| Irrelevant changes and bot README merges cause no loop | `test_irrelevant_change_and_bot_merge_do_not_loop` |
| Downtime and many commits converge on one proposal | `test_downtime_with_many_commits_converges_on_one_proposal`, `test_new_commit_while_pr_open_updates_the_same_pr` |
| Overlapping runs do not duplicate work | `test_concurrent_run_is_skipped_by_lock` |
| Failed, timed-out, interrupted requests keep correct budget state | `test_timeout_keeps_reservation_and_survives_restart`, `test_rejected_request_releases_reservation`, `test_interrupted_reservation_is_counted_as_ambiguous`, `test_budget_exhaustion_pauses_generation_and_keeps_work` |
| Human edits and stale sources are never overwritten | `test_human_edit_in_managed_block_is_not_overwritten`, `test_stale_source_is_not_published`, `test_human_commit_on_bot_branch_is_not_overwritten` |
| Invalid output, false evidence, malformed markers, unauthorized paths, secrets rejected | `test_invalid_model_output_is_rejected_without_publishing`, `test_malformed_markers_are_reported`, `tests/test_llm_and_evidence.py`, `tests/test_readme.py`, `tests/test_cli_config_scheduler.py::test_config_validation` |
| Missing credentials, budgets, permissions give actionable states | `test_missing_budget_and_credentials_are_reported`, `test_missing_anthropic_key_state`, `test_missing_push_permission_blocks_before_paid_request`, `tests/test_cli_config_scheduler.py::test_run_once_without_github_credential_is_actionable` |
| Publication confirmed on the target branch | `test_publication_requires_content_on_target_branch` |
| README commands and links match the implementation | `tests/test_cli_config_scheduler.py::test_cli_commands_documented_in_readme_exist`, `test_readme_relative_links_exist` |

#### First live Claude generation (pilot of 0.1.0)

A small owner-approved budget; one repository enrolled with `api_export = "allowlist"`;
`max_output_tokens = 16000`; a development computer.

| Check | Kind | Result |
| --- | --- | --- |
| `readme-monitor preview --repo <owner/name> --api` | real provider, real GitHub (read-only) | Succeeded in 30 s. Proposed changes to 4 of 5 requested sections; one left unchanged. All 4 carried model uncertainty notes for review. |
| Usage accounting | real provider | The reservation was higher than the cost the provider reported (about 77,000 input and 2,900 output tokens); the ledger recorded the reported cost |
| Repeat cycle with unchanged inputs | real GitHub (read-only) | Reused the stored generation; spending did not change |

Defects found by this run and fixed in the following commit:

- The input-token estimate (characters / 2.5) was below the real count. Reservations now use the
  API's free token count, with characters / 2 as the fallback.
- Changing `publish_mode` or a spending limit changed the Claude-input fingerprint, which would
  have triggered a new paid generation. Those settings are no longer part of it.
- The secret redactor flagged the template placeholder `token = "{{section:"`.

A pull request opened by this pilot was never merged, so `published` confirmation after a merge
on real GitHub has not been observed.

### Version 0.2.0 (account-wide discovery, host guard, shared budget)

Environment: a development computer where live work was refused by design.

| Check | Command | Kind | Result |
| --- | --- | --- | --- |
| Automated tests | `.\.venv\Scripts\python.exe -m pytest -q` | simulation | 131 passed (90 earlier + 41 in `tests/test_portfolio.py`) |
| The guard on the development computer against a real pilot runtime folder | `run-once`, `run-once --trigger scheduled`, `preview --api`, `init --repo …`, `install-task`, `run-task-now` with `--home` of the pilot folder | local integration | All `Refused:` with exit code 3; the pilot database's SHA-256 unchanged and still schema 1; no scheduled task existed |
| Read-only discovery of a real account | `discover --dry-run` (token passed to the process only) | real GitHub (read-only) | Every owned repository listed across all pages; coverage complete (classic token with `repo` scope); nothing recorded |
| Read-only README survey (separate script, no writes) | GitHub tree and README blob reads | real GitHub (read-only) | No non-Markdown README and no truncated tree among the account's repositories |

New behavior covered by `tests/test_portfolio.py`:

| Requirement | Tests |
| --- | --- |
| Discovery reads every page (205 repositories, 3 pages) and ignores repositories not owned | `test_discovery_reads_every_page_beyond_100_and_ignores_repositories_not_owned` |
| New, renamed (history and spending kept), transferred (stopped) and archived repositories | `test_new_renamed_transferred_and_archived_repositories_follow_policy`, `test_worker_never_follows_a_transfer_redirect` |
| Interrupted or limited discovery never erases repositories or claims full coverage | `test_interrupted_discovery_changes_nothing_and_cycles_continue`, `test_limited_credential_never_claims_complete_coverage`, `test_credential_for_another_account_is_refused` |
| No unapproved payload; export restriction survives defaults, bulk enrollment and renames | `test_discovered_repositories_send_nothing_to_claude`, `test_export_restriction_survives_defaults_bulk_enrollment_and_rename` |
| Fair rotation over more than 10 repositories, across restarts, despite failures | `test_rotation_covers_every_repository_across_cycles_and_restarts`, `test_failing_repositories_cannot_monopolize_slots` |
| Generation policy: quiet period, no endless postponement, per-repository interval, oldest first | `test_quiet_period_coalesces_changes_then_generates_once`, `test_constant_commits_cannot_postpone_generation_forever`, `test_minimum_interval_between_paid_generations`, `test_oldest_pending_repository_is_funded_first` |
| Shared budget: atomic check-and-reserve, global limits, multiple READMEs, pacing | `test_reservation_check_is_atomic_across_processes`, `test_limits_are_shared_across_repositories`, `test_every_readme_and_attempt_counts_against_one_run`, `test_even_pacing_spreads_the_monthly_allowance` |
| Adoption: bounded batches, one PR per repository, unmanaged text kept, minimal README, filename case, empty/archived, closed PR, malformed markers | `test_adoption_opens_one_bounded_batch_of_review_prs`, `test_adoption_of_discovered_repository_leaves_hand_written_sections_alone`, `test_missing_readme_gets_a_minimal_evidence_based_proposal`, `test_actual_readme_filename_case_is_used`, `test_empty_and_archived_repositories_wait_without_writes`, `test_closed_adoption_pr_is_not_reopened_automatically`, `test_malformed_markers_in_a_discovered_readme_fail_safely` |
| Ledger migration: schema-1 backup and upgrade, restore never erases spend | `test_schema1_database_is_backed_up_and_migrated_with_spend_intact`, `test_restoring_an_older_copy_never_erases_newer_spend`, `test_restore_db_command_reports_carried_spend` |
| Status covers every repository, deferred work and budget scope | `test_status_reports_every_repository_and_deferred_work` |

Defect found by the real discovery and fixed before release: GitHub reports `size = 0` for a
repository that has a commit with one small file, so size is no longer used to call a repository
empty; only GitHub's "repository is empty" answer is.

## Desktop application (release candidate in development, 2026-11-10)

Branch `feature/desktop` of `poplislive/readme-monitor` (local commits, not pushed). PopL, Windows 11
Home 10.0.26200 x64, Python 3.12.10, Node 24.19.0, Rust 1.99.1, PyInstaller 6.22.3, Tauri 2.12.5
(CLI 2.12.4). The installer was not signed by a publisher, and its updater payloads were not signed.
No credential was used; nothing was activated on PopL; nothing was spent or written to GitHub.

| Check | Label | Result |
| --- | --- | --- |
| Engine tests (`pytest -q`) | simulation / local integration | 307 passed, 1 skipped (282 before this work plus 25 new in `tests/test_desktop_platforms.py`) |
| Interface typecheck and unit tests | local integration | `tsc` clean; 22 passed; `readme_monitor/web` rebuilt |
| Frozen engine (`packaging/engine.spec`, folder build) | local integration | Builds in about 30 s. `--version` cold 0.9 s, warm 0.3 s, outside any Python environment, from a stripped `PATH` |
| Frozen engine as the shell uses it (`packaging/smoke_engine.py`) | local integration | Ready in 0.6 to 0.9 s, header sign-in, page, state (scheduler and credential store named), simulated dry run `ok`, clean exit when stdin closes |
| Windows installer (NSIS, per user) | local integration | Built by `scripts/build-desktop.ps1`; silent install into `%LOCALAPPDATA%\Programs\ReadmeMonitor`. The pre-0.3.0 runtime folder at `%LOCALAPPDATA%\ReadmeMonitor` (9 files) stayed byte-identical through install, update and uninstall |
| Installed app launch | local integration | The shell starts its own `engine\rm-engine.exe desktop-serve` (ready in about 1.8 s), with a runtime folder whose path contains a space and `ü`. The window title is ReadmeMonitor and it responds |
| One instance | local integration | A second launch left one shell and one engine |
| Shell crash | local integration | Killing the shell stopped the engine within 0.5 s (stdin closed) and removed `ui-server.json` |
| Task Scheduler starts the packaged engine | local integration | The engine's own task definition, registered under a test name without triggers and started once: `rm-engine-bg.exe` ran with the non-ASCII path and returned 3 (refused: not activated, as intended on PopL). The task was deleted |
| Update install over the previous version (`/S /UPDATE`, as the updater runs it) | local integration | 0.3.0 to a 0.3.1 test build: same install folder; a placeholder `\ReadmeMonitor\Sync` task kept; runtime folders unchanged; all five licence files present |
| Uninstall (`/S`) | local integration | Program, folder, uninstall entry and Start menu shortcut removed; `\ReadmeMonitor\Sync` removed by the hook; runtime folders kept; no new folder created |
| systemd user timer backend | local integration (WSL Ubuntu 26.04, systemd 1 running) | Real `systemctl --user`: daily, weekly, interval and manual registered and read back from the active timer; `run_now` ran the (harmless) command; uninstall left nothing |

Defects found by these checks and fixed:

- Concurrent first requests on a new runtime folder answered "database is locked" (the installed
  app's window and tray polled at once). The database is now created before the server takes
  requests. The regression test does not reliably fail without the fix (the race depends on timing).
- `/api/state` dropped the new installation fields, so the page would have shown no scheduler name.
  Found by the smoke test; now covered by a test.
- The Windows per-user default install folder was the pre-0.3.0 runtime folder. Fresh installs now
  go to `%LOCALAPPDATA%\Programs\ReadmeMonitor`.
- The uninstaller's `uninstall-task` call created an empty runtime folder; it no longer does.
- `smoke_engine.py` could not start the engine from a relative path with forward slashes on Windows,
  which the CI job uses.

NOT TESTED:

- The window's rendering, keyboard and focus behaviour, the tray menu, close and reopen, the dialogs
  and the in-app update flow (Install and restart). Screen control was declined for this session.
- A signed update end to end (download, signature check, maintenance, install, health check).
  The engine side is covered by tests; the shell side has not been run.
- macOS (any architecture), the Linux packages (.deb, AppImage), WebKit rendering, the macOS
  Keychain and the Secret Service backend. They are built only by `.github/workflows/desktop.yml`,
  which has not run (the branch is not pushed).
- A real scheduled worker run of the packaged app, sleep/resume, sign-in at logon, network loss and a
  locked credential store. Each of these needs an activated installation.
- Publisher signing: none on Windows or macOS. The updater key is a candidate kept outside the repository.
