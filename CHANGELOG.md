# Changelog

All notable changes to ReadmeMonitor. Dates are UTC. The project is pre-release: nothing has
been published as a release yet, and the version numbers mark the stages of development.

## Unreleased: desktop application (release candidate in development)

Not part of 0.3.0. The version number for the first desktop release is still to be decided; the
files below stay at 0.3.0 so that every part reports one version.

### Added

- **Desktop application** for Windows, macOS and Linux ([docs/desktop.md](docs/desktop.md)): a
  Tauri 2 shell around the frozen engine (PyInstaller) and the existing interface. It has its own
  window, a tray or menu-bar icon (Open, Run a check now, Pause/Resume background checks, Quit),
  one instance per installation, remembered window size, and safe external links. The window signs
  in with a one-time code passed over a private pipe, never in a URL.
- **Signed in-app updates** from GitHub Releases. A running check always finishes first. The
  update takes a database backup and sets a maintenance flag that every worker entry point honours
  under `worker.lock`. A start-up health check runs before checks resume.
- **macOS and Linux scheduling**: launchd user agents and systemd user timers next to Windows Task
  Scheduler, with the same configured-versus-in-effect read-back. A scheduled check that still starts
  another copy (for example an earlier source install) is reported, not shown as in effect.
- **Per-computer activation on macOS (IOPlatformUUID) and Linux (/etc/machine-id)**, plus
  platform runtime folders: `~/Library/Application Support/ReadmeMonitor` and
  `$XDG_DATA_HOME/ReadmeMonitor`. An existing activated folder is kept where it is.
- Release workflow `.github/workflows/desktop.yml`: unsigned validation builds for pull requests,
  and a draft release with `latest.json` and `SHA256SUMS.txt` for a version tag.

### Fixed

- A new runtime folder could answer its first concurrent interface requests with "database is
  locked". The database is now created once, before the server takes requests.
- Saving a credential refuses an insecure keyring backend (a plaintext file) instead of using it.

## 0.3.0 - unreleased

The first version meant for people other than its author: it installs on any Windows computer,
has a setup interface, and is documented for newcomers.

### Added

- **A local setup interface** (`readme-monitor ui`, or double-click `ReadmeMonitor.cmd`). It runs
  in your browser on your own computer only and covers a six-step setup (this computer, GitHub,
  repositories, model and spending, publication and schedule, dry run and start) and a workbench
  that shows every stage of every check, the commits that were examined, the evidence that was
  read, the proposed README and its diff, run history and spending. No Node.js is needed to use
  it; the compiled interface ships in the package.
- **Simulation mode** (`readme-monitor ui --demo`): the same interface over invented
  repositories, with an in-memory GitHub and a scripted model. Nothing real is contacted.
- **Activation.** Live work (spending, publishing, scheduling) works only after you enable the
  worker for a runtime folder on a computer: `readme-monitor activate`, or the interface. A copied
  or moved folder stays inactive until you enable it again. `deactivate` turns it off.
- **Schedules**: manual only, every N minutes, daily, or chosen weekdays, at the computer's time
  (following daylight saving) or UTC. The interface registers the schedule with Windows Task
  Scheduler and reads it back to show whether Windows holds what you configured.
  New commands `schedule` and `cancel`.
- **Direct publication** (`publish_mode = "direct"`, opt-in per repository): one ordinary,
  non-forced commit to the target branch, pinned to the analyzed README version. Reviewed
  content and the first adoption of a README still use a pull request.
- **Activity events and stored drafts** (database schema 3): every stage of a check is recorded
  with safe, bounded data; each proposed or dry-run README is stored with its diff.
- **Dry runs**: `run-once --dry-run` (optionally `--repo`) reads GitHub and stores drafts without
  any model request or GitHub write, and works before the worker is enabled.
- `scripts\start.ps1`, `ReadmeMonitor.cmd` (start the interface), `scripts\uninstall.ps1`
  (remove the task, the activation and optionally credentials or data), and
  `scripts\verify-clean-install.ps1` (checks that a clean copy installs and serves the interface).
- **README pictures that can be regenerated**: `scripts/screenshots.mjs` captures them from the
  simulation in the installed Chrome or Edge, without installing anything, and a manual
  **Screenshots** workflow does the same on GitHub ([Contributing](CONTRIBUTING.md)). The pictures
  use UTC, so they do not show the time zone of the computer that took them.
- Documentation for newcomers: [Getting started](docs/getting-started.md),
  [Troubleshooting](docs/troubleshooting.md), [Moving an installation](docs/moving-an-installation.md),
  [Privacy](docs/privacy.md), [Security policy](SECURITY.md), [Contributing](CONTRIBUTING.md) and
  the [local API contract](docs/api.md).
- Acceptance tests (`tests/test_acceptance_*.py`), one per acceptance criterion, including a
  publication test against a real local Git repository.
- Continuous integration builds the interface and runs the clean-install check on Windows.
- Licensed under the Apache License 2.0 (`LICENSE`, `NOTICE`), with the licences of the bundled
  interface packages and fonts in `THIRD_PARTY_NOTICES.md`
  (regenerated by `scripts/third_party_notices.py`).

### Changed

- **Any computer can be used.** The fixed production host name is gone. The installer and the
  tests no longer assume a particular computer.
- `install.ps1` gives plain-language errors for a missing or old Python, a failed environment
  or download, and tells you how to start the interface. `-InstallTask` now works only for an
  installation you have already enabled and otherwise explains what to do.
- `config.toml` is written by the interface when you use it (validated first, written
  atomically). The first rewrite of a hand-written file keeps a one-time `.bak` copy. New optional
  tables: `[schedule]` and `[installation]` (`host_lock`).
- The starter configuration now schedules a check every 15 minutes (installations without a
  `[schedule]` table keep using `poll_interval_minutes`, default 5).
- The runtime folder is chosen by `--home`, else `READMEMONITOR_HOME`, else the folder you
  activated, else `%USERPROFILE%\ReadmeMonitor`.
- `status --json` and `doctor` carry the information the interface shows.
- **Commits the monitor makes use the account's GitHub noreply address** as author and committer.
  Earlier versions let GitHub fill in the account's primary e-mail address, which a public
  repository shows to everyone.
- The documentation was rewritten for the public product and private details were removed.

### Migration notes for existing installations

- **`bind-host` is now `activate`.** `bind-host` still works as a deprecated alias. An activation
  record made by 0.2.0 (`production-host.json`) is carried forward automatically, once, only when
  the computer, the folder and the host name all still match. In every other case the folder stays
  inactive, and the interface or `readme-monitor activate` enables it deliberately. The old file
  is kept. Nothing is ever activated by installing or upgrading.
- **Database schema 3.** Opening a 0.1.0 or 0.2.0 database writes an automatic backup
  (`backups\pre-schema3-<time>.db`) and then only adds tables and columns; an interrupted upgrade
  simply continues on the next start. Spending records, reservations and history are unchanged.
  An older program refuses to open a newer database, so once 0.3.0 has opened it there is no way
  back to 0.2.0: if 0.3.0 misbehaves, pause it and report the problem (see
  [Roll back an upgrade](docs/operations.md#roll-back-an-upgrade)). The automatic backup is kept for
  diagnosis; it lacks everything recorded after the upgrade, including spending.
- **A runtime folder copied to another computer is inactive** there and keeps its ledger. See
  [Moving an installation](docs/moving-an-installation.md). Do not run the same ledger on two
  computers.
- The scheduled task can stay as it is. Saving a schedule in the interface registers it again
  with the same name and the same safe settings (one catch-up after sleep, no overlap).
- New files in the runtime folder: `activation.json` and `ui-server.json` (only while the
  interface runs). In the account's own user folder: a small pointer file,
  `.readme-monitor.json`, naming the active runtime folder, and `.readme-monitor-ui\`, which holds
  the interface's single-use sign-in code for at most two minutes. The code is kept out of the
  runtime folder because that folder's permissions can be inherited from a shared drive.

### Known limitations

- Windows is the only supported platform for the worker. The tests also run on Linux.
- Anthropic is the only model provider.
- Checks run only while you are signed in to Windows and the computer is awake.
- Version 0.3.0 has been verified with simulated GitHub, Claude and Task Scheduler. See
  [the verification record](docs/verification.md) for exactly what has and has not been run for
  real.

## 0.2.0 - 2026-11-10

### Added

- Account-wide discovery: lists every repository the account owns (all pages), follows renames
  and transfers by repository id, and gives each a status and reason.
- A per-computer guard for live work, one runtime folder and one ledger, with no override switch
  (replaced by activation in 0.3.0).
- One shared budget with per-run, daily, monthly and paced limits across all repositories, and
  oldest-waiting-first funding.
- Generation policy: a quiet period, a minimum interval between paid updates and a maximum
  postponement, so a burst of commits costs one request.
- Adoption pull requests in bounded daily batches; export restrictions that survive renames;
  fair rotation over more than ten repositories; `restore-db` that never lowers recorded spending.

### Changed

- Each review flag is listed once in a documentation pull request.

## 0.1.0 - 2026-11-10

### Added

- The first working version: detects README-relevant changes by file content, gathers
  evidence (commands, settings, dependencies, releases, check results), asks Claude for the
  sections it may maintain, validates the answer and opens a pull request for review.
- Managed blocks with hashes, so text you wrote is never overwritten; adoption of existing READMEs.
- Spending reservations and limits; stored credentials in Windows Credential Manager;
  Task Scheduler registration; the `readme-monitor` command (`doctor`, `init`, `preview`,
  `run-once`, `status`, `pause`, `resume`, `install-task`, `backup-db`, `restore-db` and more).

### Fixed (found by the first live pilot, before 0.2.0)

- The input-token estimate used for budget reservations was too low; it now uses the provider's
  free token count.
- Changing a publication mode or a spending limit no longer triggers a new paid generation.
