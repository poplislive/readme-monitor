# Troubleshooting

Each row gives the symptom, the likely cause and a safe remedy. Nothing here deletes your data.
When in doubt, run the health check first:

```powershell
$rm = ".\.venv\Scripts\readme-monitor.exe"
& $rm doctor
```

`doctor` prints one `PASS`, `WARN` or `FAIL` line per item with the reason. The same checks are
shown in the interface (the diagnostics list). They never include a secret.

## Installing and starting

| Symptom | Likely cause | Safe remedy |
| --- | --- | --- |
| "Running scripts is disabled on this system" or "is not digitally signed" | Windows' script policy blocks `.ps1` files, often for files downloaded as a ZIP | Run it for this one time: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1`. Double-clicking `ReadmeMonitor.cmd` already does this for the launcher. Nothing in Windows is changed. |
| Installer: "Python was not found" or "needs 3.11 or newer" | Python is missing, too old, or only the Microsoft Store shortcut exists | Install Python 3.11 or newer from python.org (tick **Add python.exe to PATH**), then run the installer again. To use a specific one: `-PythonExe C:\path\to\python.exe`. |
| Installer: "Creating .venv failed" | The folder is read-only or synced in a way that blocks writes, or Python was installed without the `venv` module | Move the files to an ordinary writable folder (for example under your user folder), or repair Python with default options, then run the installer again. |
| Installer: "Installing pinned dependencies failed" | No internet connection, a proxy, or a security product blocking pip | Check the connection and proxy settings, then run the installer again; it is safe to repeat. |
| Launcher: "ReadmeMonitor is not installed on this computer yet" | `.venv` does not exist next to the scripts | Run `scripts\install.ps1` once. |
| The browser does not open | No default browser, or a policy blocks it | Run `.\scripts\start.ps1 -NoBrowser` and open the printed address yourself (once, within two minutes). |
| The page says to open ReadmeMonitor from its launcher | You opened the address from a bookmark or history, or the one-time sign-in address was used already or expired | Start it again with `ReadmeMonitor.cmd` or `.\scripts\start.ps1`; a running server is reused and a new address is printed. |
| "Port in use" or the address uses a different number than expected | Another program uses port 8765 | Nothing to fix: the next free port is used and its address is printed. To choose: `.\scripts\start.ps1 -Port 9000`. |
| A second window shows the same interface | The interface was started twice | Expected: the second start reuses the running server and only opens a new sign-in. |

## Enabling and the computer

| Symptom | Likely cause | Safe remedy |
| --- | --- | --- |
| `Refused: Live ReadmeMonitor work … is refused: The worker is not enabled for … yet` (exit code 3); the interface shows **inactive** | You have not enabled the worker for this runtime folder on this computer | In step 1 of the setup use **Enable the worker on this computer**, or run `& $rm activate`. Dry runs, `status`, `doctor` and previews without Claude need no enabling. |
| "This runtime folder was activated on another computer or copied from one" | The folder was copied from another computer (or the other computer's data was restored here) | Working as designed: a copy never spends or publishes. Enable it deliberately here, after reading [Moving an installation](moving-an-installation.md). The old computer must stop using it. |
| "…has been moved or copied" | The folder is at a different path than the one that was activated | Move it back, or enable it again at the new path (`activate --home <folder>`). Recorded spending is kept either way. |
| "…has an activation record from an earlier version that does not match this computer and folder" | An upgrade found a 0.2.0 record that belongs to another computer or folder | Enable it here deliberately if this is the installation you want to run. |
| "…already has an active runtime folder" | A Windows account can have one active folder | `& $rm deactivate --home "<the other folder>"` first, then activate this one. |
| "The worker can only be enabled on Windows" | You are on another operating system | The worker runs on Windows only; other systems can use the offline commands and dry runs. |
| The computer was renamed and the worker still runs | Expected | The identity check uses the Windows machine identity, not the name. Only `host_lock` (if you set one) compares the name. |

## Credentials

| Symptom | Likely cause | Safe remedy |
| --- | --- | --- |
| `doctor`: `FAIL github credential … missing`; state `credentials_missing` | No token stored for **this Windows account** | Store it in the interface (step 2/4) or `& $rm set-credential github`, as the account that runs the checks. |
| It worked, then the credential is "missing" after signing in as someone else, or in a scheduled check | Credential Manager entries belong to one Windows account | Store the credential again while signed in as the account that runs the task. |
| GitHub says the token is invalid or lacks scopes | Expired or too-narrow token | Create a new token (see [Operations](operations.md#credentials)) and store it again; the old one is replaced. |
| The environment variable is used instead of the stored credential | `doctor` shows `environment:…` as the source | Credential Manager wins when it has an entry; remove the variable if it is stale. |

## Schedule and background running

| Symptom | Likely cause | Safe remedy |
| --- | --- | --- |
| Nothing runs while I am signed out, or while the computer is asleep or shut down | The task runs only while the account is signed in and the computer is awake | Expected. One catch-up check runs when the computer is available again. There is no setting that makes it run signed out. |
| The schedule bar says **Out of step** or "No scheduled task" | `config.toml` and Windows disagree (the task was removed, or the file was edited by hand) | Save the schedule in the interface again, or `& $rm install-task`. Both need the worker to be enabled. |
| Saving a schedule says it was saved but not applied | The worker is not enabled yet | Expected. After you enable the worker, save the schedule once more to register it with Windows. |
| The next run time is blank or "unknown" | Windows reported it in a language the program does not read, or the task has not run yet | Harmless. The computed next checks are still shown. |
| The task shows a `Last Result` other than 0 | A repository failed in that check | `& $rm status` lists recent errors. |
| `Skipped: another check is running` | A scheduled and a manual check overlapped; the lock stopped the second | Nothing to do; no work was duplicated. |
| **Run now** is disabled | The worker is not enabled, a check is already running, or the monitor is paused | Enable, wait, or resume. |

## A repository's state

| State or symptom | Likely cause | Safe remedy |
| --- | --- | --- |
| `budget_paused`, "no approved budget" | The three limits are not all set | Set per-check, daily and monthly limits (step 4). |
| `budget_paused`, "limit … would be exceeded" | Today's or this month's allowance is used, or the pacing limit is reached | Nothing: work is kept and resumes when the allowance allows. Raise a limit only if you meant to. |
| `generation_deferred` | A change is waiting for the quiet period (`quiet_minutes`) or the interval between paid updates | Nothing; the next eligible time is shown. |
| `conflict` | A person edited a managed block, the monitor's branch, or the markers are damaged | The monitor will not overwrite it. Fix or accept the edit by hand; to hand a block back, restore its content or re-adopt (`init --repo owner/name`). |
| `permission_missing` | The token cannot push to the repository | Give the token *Contents* and *Pull requests* write access, or use preview mode. |
| `publish_blocked` (direct mode) | The branch is protected or the token cannot write to it | The monitor does not retry around protection. Switch the repository to a pull-request mode, or allow the commit in the repository's settings. Nothing was written. |
| `stale` | The branch moved while publishing | Nothing: it is analyzed again at the next check. |
| `needs_init` | The README has no managed blocks | `& $rm init --repo owner/name --dry-run` to see the change, then `init` (enabled worker) or adopt from the interface. |
| `unsupported_readme` | The primary README is not Markdown | Convert it, or leave this repository to people. |
| `empty` / `archived` | No commits yet / archived on GitHub | Nothing is written; checked again later. |
| `renamed` | An enrolled entry uses the old name | Update `name` in `config.toml` (history moves with the repository id). |
| `transferred` | The repository moved to another owner | It is not processed any more; remove the entry. |
| Draft text looks wrong | The model used the evidence it was given | The **Validation** tab lists review flags. In `review_pr` mode nothing changes until you merge. |

## Data

| Symptom | Likely cause | Safe remedy |
| --- | --- | --- |
| "was created by a newer ReadmeMonitor (schema 4)" | An older program opened a database from a newer one | Install the newer version again. An older version can neither use nor restore over a newer database; see [Roll back an upgrade](operations.md#roll-back-an-upgrade). |
| Spending looks lower after a restore | It cannot: `restore-db` carries forward newer usage records | Check `& $rm status`; the ledger only grows. |
| I want to start over | | `.\scripts\uninstall.ps1 -RemoveData`, after reading its output. |

Still stuck? Open an issue and include the output of `& $rm status` (it contains no secrets).
For a security problem use the process in [SECURITY.md](../SECURITY.md) instead of an issue.
