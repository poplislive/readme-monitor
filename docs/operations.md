# Operations guide

Day-to-day use, from the setup interface and from the command line. Commands are PowerShell, run
from the repository folder (the folder that contains `pyproject.toml`). The runtime folder
defaults to `%USERPROFILE%\ReadmeMonitor`; the command line uses the folder you activated, or
the one named in the `READMEMONITOR_HOME` environment variable, or the default, and any command
accepts `--home <folder>` to choose one.

```powershell
$rm = ".\.venv\Scripts\readme-monitor.exe"
```

First time here? Read [Getting started](getting-started.md).

## What "enabled on this computer" means

An installation can read GitHub and draft changes anywhere. Anything that can **spend money**
(Claude requests, including free token counting), **write to GitHub** or **create a schedule**
works only after you deliberately enable the worker for one runtime folder on one computer:

- in the interface: step 1, **Enable the worker on this computer**; or
- on the command line: `& $rm activate`.

Without it those commands stop with `Refused:` and exit code 3 before reading any credential.
Offline commands and dry runs keep working. A runtime folder that is copied to another
computer, or moved to another path, becomes inactive until you enable it there; see
[Moving an installation](moving-an-installation.md). To turn it off: `& $rm deactivate` (removes
the scheduled task and revokes the activation; your data stays).

A Windows account has one active runtime folder at a time. `bind-host` is the old name of
`activate` and still works.

## Install, upgrade, start

```powershell
.\scripts\install.ps1            # install or upgrade; never enables, schedules or spends
.\scripts\start.ps1              # the interface (or double-click ReadmeMonitor.cmd)
```

**Upgrade:** download the newer files (or `git pull`), then run `.\scripts\install.ps1` again. It
keeps `config.toml`, never adopts READMEs and never changes the activation. The database schema
is versioned and an older program refuses to open a newer database. Opening a database made by
0.1.0 or 0.2.0 with 0.3.0 first writes `backups\pre-schema3-<time>.db` and then only adds
tables and columns. See the [changelog](../CHANGELOG.md) for what each version changes.

## Daily use

The interface has two parts: **Setup** (until the six steps are done, reachable again from the
top bar) and the **workbench**.

- The repository rail on the left lists every repository with its state. Pick one to see its
  stages: inspect, evidence, draft, validate, publish, await, result. The tabs show the commits
  that were examined, the evidence that was read, the proposed README, the diff, the validation
  notes and links.
- The schedule bar shows what is configured, what Windows actually holds (read back from Task
  Scheduler), and the next checks. If they differ it says so.
- Run history and spending show past checks and the ledger.
- **Run now** starts one check immediately (as a separate process, so it continues if you close
  the page). **Cancel** asks the running check to stop at its next safe point; a request already
  sent to Claude or GitHub is completed and recorded, never abandoned.

Closing or reopening the page never changes the schedule and never repeats work: the page only
shows state held by the program.

## Schedule

Windows Task Scheduler is the only scheduler. Choose one of four modes in the interface (or in
`[schedule]` in `config.toml`, see [Configuration](configuration.md)): manual only, every N
minutes, every day at a time, or chosen weekdays at a time; at this computer's time (following
daylight saving) or UTC.

```powershell
& $rm schedule                 # configured schedule, next checks, and what Windows holds
& $rm install-task             # register the configured schedule (needs the worker enabled)
& $rm install-task --dry-run   # print the task definition instead
& $rm run-task-now             # ask Task Scheduler to start it now
& $rm uninstall-task           # remove the task (keeps data)
```

Saving a schedule in the interface registers it again and reads it back. Replacing the task
neither starts nor stops a running check. The task definition:

| Setting | Value | Why |
| --- | --- | --- |
| Run as | the Windows account that enabled the worker, "only when logged on" | it reads that account's stored credentials |
| Missed runs | start when available, once | one catch-up after sleep or shutdown, never a burst |
| Overlap | ignore the new start | Task Scheduler never stacks runs |
| Time limit | `run_timeout_seconds` + 5 minutes | a hung run is stopped |
| Program | `.venv\Scripts\pythonw.exe -m readme_monitor run-once --trigger scheduled --home … --config …` | explicit paths, no console window, no secrets |

A file lock in the runtime folder also stops a manual run and a scheduled run from overlapping;
the second one exits with "Skipped".

**Checks happen only while you are signed in to Windows and the computer is awake.** A check
missed while it was asleep or off runs once when it is available again. Running while signed out
would need your Windows password stored for the task, which this program does not do.

## Credentials

Credentials are kept in Windows Credential Manager under the target `ReadmeMonitor`, for the
Windows account that runs the monitor. Nothing is written to files, task arguments or logs. Use
the interface, or:

```powershell
& $rm set-credential github        # hidden input
& $rm set-credential anthropic
& $rm remove-credential github
```

- **GitHub:** a personal access token. Reading needs *Metadata*, *Contents*, *Pull requests*,
  *Commit statuses* and *Checks* read access. `review_pr` and `auto_pr` also need *Contents* and
  *Pull requests* write; `direct` needs *Contents* write. A fine-grained token limited to
  selected repositories can only list those, so account-wide discovery reports its coverage as
  not proven; complete coverage needs "All repositories" or a classic `repo` token.
- **Anthropic:** an API key for an account whose spending you have approved.
- The environment variables `READMEMONITOR_GITHUB_TOKEN` and `ANTHROPIC_API_KEY` are used only
  when Credential Manager has no entry. `doctor` shows which source was found, never the value.

## Pause, resume, cancel

```powershell
& $rm pause                       # whole monitor: scheduled checks exit without network calls
& $rm resume
& $rm pause --repo owner/name     # one repository
& $rm resume --repo owner/name
& $rm cancel                      # ask the running check to stop at its next safe point
```

`paused = true` in a repository's configuration also pauses it. Pausing changes no schedule: the
task keeps starting and exits at once.

## Run history, status and budgets

`& $rm status` (or `status --json`) shows whether the worker is enabled, the runtime folder, the
scheduled task and the last scheduled check, discovery time and coverage, the budget (spent
today and this month, remaining, pacing, open reservations), deferred work oldest first with its
age and reason, and per repository: last check, pending age, next eligible time, latest
proposal, last generation and last confirmed publication. The interface shows the same.

| You see | It means | What to do |
| --- | --- | --- |
| `unchanged` / `no_changes` | Nothing relevant changed, or the README is already correct | Nothing |
| `proposal_created` | Preview mode wrote a proposed README to `previews\` | Read the `.diff` file |
| `awaiting_merge` | A pull request is open | Review and merge it, or close it |
| `merged_unconfirmed` | Merged; waiting to see the README on the branch | Nothing |
| `published` | The proposed README is confirmed on the target branch | Nothing |
| `generation_deferred` | Waiting for the quiet period or the per-repository interval | Nothing; the next eligible time is shown |
| `budget_paused` | Waiting for money, or queued behind an older pending repository | Nothing; work is kept |

The full list is in [Architecture](architecture.md#states).

**Budgets.** Paid generation needs all three limits (per check, per day, per month; US dollars,
UTC calendar days and months). They are shared by every repository, retry and entry point that
uses the runtime folder's database. Each Claude attempt first reserves its worst-case cost, then
records the real cost. Restarting never resets the counters, and `restore-db` never lowers
recorded spending. The ledger counts only requests made through this installation's database;
other uses of the same Anthropic account are invisible to it.

## Discover every repository an account owns

```powershell
& $rm discover --dry-run          # read-only: list, dispositions, coverage; records nothing
& $rm discover                    # record the inventory (needs the worker enabled)
```

Turned on by `[discovery] owner` in `config.toml`. Every listed repository gets one disposition:
`enabled`, `awaiting_adoption`, `awaiting_export_approval` (structure and release sections are
maintained; Claude-written sections need an enrolled repository), `paused`, `archived`, `empty`,
`inaccessible` or `blocked` (with the reason). The scheduled check re-lists the account every
`refresh_hours`; a failed or partial listing changes nothing. Adoption pull requests for
discovered repositories open automatically, at most `adoptions_per_day` per day. A closed
adoption pull request is not reopened automatically; run `init --repo owner/name` to propose it
again.

## Backups, logs and data

```powershell
& $rm backup-db                   # consistent copy in <home>\backups (keeps the newest 10)
& $rm restore-db "<home>\backups\state-YYYYMMDDTHHMMSS.db"
```

`restore-db` checks the file's integrity, saves the current database as `pre-restore-*.db`, and
needs the worker lock (pause the monitor if a check is running). It never lowers recorded
spending: usage records missing from the restored file are carried forward. A database copied
to another computer is a snapshot, not a shared ledger.

Where things are, inside the runtime folder: `config.toml` (settings), `state.db` (history and
the spending ledger), `activation.json` (the enable record), `logs\readme-monitor.log` (rotated
at 1 MB, 5 old files, secret-looking values redacted), `backups\`, `previews\` (preview-mode
drafts), `inventory\` (discovery results), `task\` (the last task definition). Credentials are
not in this folder; they are in Windows Credential Manager.

## Roll back an upgrade

1. `& $rm pause`
2. `& $rm backup-db`
3. Put the previous version of the program files back (for example `git checkout <previous tag>`).
4. `.\scripts\install.ps1`
5. If the newer version changed the database schema, `& $rm restore-db <backup from before the upgrade>`.
6. `& $rm resume`

## Uninstall

```powershell
.\scripts\uninstall.ps1                       # removes the task and the activation; deletes nothing
.\scripts\uninstall.ps1 -RemoveCredentials    # also removes the stored GitHub and Anthropic credentials
.\scripts\uninstall.ps1 -RemoveData           # also deletes the runtime folder, after you type DELETE
```

By default the script uses the product's own commands (`deactivate`, and with
`-RemoveCredentials` `remove-credential`) and then lists exactly which folders still hold data.
It never deletes the program files; delete the repository folder by hand if you want it gone.

## Troubleshooting

See [Troubleshooting](troubleshooting.md).
