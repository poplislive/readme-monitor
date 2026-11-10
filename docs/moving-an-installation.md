# Moving an installation

How to move ReadmeMonitor to another computer, or to another folder on the same computer, without
losing history or spending records and without ever running two copies by accident.

The commands below assume `$rm = ".\.venv\Scripts\readme-monitor.exe"` in PowerShell, run from the
repository folder.

## Why a copy does not run by itself

Enabling the worker (the interface's **Enable the worker on this computer**, or `activate`)
writes an activation record into the runtime folder. The record names the computer, identified
by its Windows machine identity, and the exact folder. A runtime folder that is copied to another
computer, or moved or copied to another path, therefore starts **inactive**: it can still read
GitHub, make dry runs and show its history, but it cannot spend money, write to GitHub or create
a schedule until you enable it again on purpose. This protects you from the most expensive
mistake: two workers spending against two copies of the same ledger.

## Move to another computer

**On the old computer**

1. In the interface press **Pause** (or run `readme-monitor pause`), and wait for any running check to finish.
2. Make a consistent copy of the database:

   ```powershell
   & $rm backup-db
   ```

   The file is written to `<runtime folder>\backups\state-<time>.db`.
3. Turn the old installation off:

   ```powershell
   & $rm deactivate
   ```

   This removes the scheduled task and revokes the activation. Your data and credentials stay
   where they are. If the old computer will be reused for other things, you can also run
   `.\scripts\uninstall.ps1`.

**On the new computer**

1. Install the program and start the interface ([Getting started](getting-started.md)).
2. Copy `config.toml` from the old runtime folder into the new one, and the `state-<time>.db`
   backup somewhere you can reach.
3. Restore the history:

   ```powershell
   & $rm restore-db "C:\path\to\state-<time>.db"
   ```

   `restore-db` checks the file for corruption, saves whatever database was there as
   `pre-restore-<time>.db`, and then replaces it. Its rule for money: **recorded spending never
   goes down.** If the database it replaces holds spending records that the backup does not
   contain, those records are carried forward into the restored database (and a reservation that
   was still open is kept as spent). Restoring an older copy can therefore never re-open an
   allowance that was already used.
4. Store the credentials again (step 2 and 4 of the setup, or `set-credential`). Credentials live
   in Windows Credential Manager for one Windows account and are never part of the copy.
5. Enable the worker on the new computer, deliberately: step 1 of the setup, or `activate`. Then
   check **Schedule** in the interface and save it, so the task is registered on this computer.
6. Press **Resume**, and run a dry run before the first real check.

## Move to another folder on the same computer

1. Pause, then `deactivate` the old folder (a Windows account can have one active folder at a time).
2. Move the folder, or copy it with its `config.toml` and a `backup-db` copy of the database.
3. Start the interface with the new folder (`.\scripts\start.ps1 -RuntimeHome <new folder>`), restore the
   database if you copied only a backup, and enable the worker there.

## One ledger, one worker

- Two computers must not run the same ledger at the same time. Each computer's database only
  knows what *it* spent. Two enabled copies would each believe the full budget is still available,
  and could both open a pull request or commit for the same change.
- The program cannot detect another installation. **Nothing technical prevents two unrelated
  installations (different folders or different computers) from watching the same
  repositories.** Each has its own ledger and its own limits; they would not share a budget, and
  they would not coordinate their pull requests or commits. Keep exactly one enabled
  installation per set of repositories.
- If you cannot reach the old computer, it may keep running its own enabled copy on its schedule,
  and nothing on the new computer can stop it. The reliable way to stop it is to revoke the
  GitHub token and the Anthropic key it uses (create new ones for the new computer), because the
  old copy then fails at its first request. Also remove its scheduled task when you can.

## Interrupted or partial moves

- If `restore-db` reports that the file failed its integrity check, nothing was changed.
- If you only copied `config.toml`, you start with an empty history and an empty ledger on the
  new computer. That is safe but forgets earlier spending in the current month: set the budget
  limits with that in mind, or restore a backup.
- Upgrading from an earlier version carries the earlier activation forward only if the computer
  and the folder still match exactly; otherwise the folder stays inactive until you enable it.
  See the [changelog](../CHANGELOG.md).
