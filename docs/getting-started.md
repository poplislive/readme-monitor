# Getting started

This guide takes you from nothing to a first dry run on a Windows computer. It assumes no
programming knowledge. Every step is something you can undo, and nothing here spends money,
changes a repository or schedules anything until you say so in the last steps.

## What you need

| You need | Why | How to check |
| --- | --- | --- |
| Windows 11 | The checking program runs on Windows only. | Settings > System > About |
| Python 3.11 or newer | The program is written in Python. The installer sets up a private copy of everything else. | In PowerShell: `py -3 --version` (or `python --version`) |
| An internet connection | The installer downloads the program's libraries once; later the program talks to GitHub (and to Anthropic if you choose to). | |
| A GitHub account and a token | So the program can read your repositories. The interface explains which kind of token to create. | |
| An Anthropic API key (optional) | Only for the paid Claude-written sections. You can start without one. | |

You do not need Node.js, Git or any programming tools. If you do not have Python, install it
from [python.org/downloads](https://www.python.org/downloads/) and tick **Add python.exe to PATH**
in the first installer screen.

## 1. Install

1. Get the files: download this repository as a ZIP file and unzip it, or clone it with Git
   (`git clone https://github.com/poplislive/readme-monitor.git`).
2. Open PowerShell in that folder (in File Explorer, open the folder, click the address bar, type
   `powershell`, press Enter).
3. Run the installer:

   ```powershell
   .\scripts\install.ps1
   ```

If Windows says that running scripts is disabled, or that the file is not digitally signed, run
the same script for this one time without changing any Windows setting:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

The installer:

- creates a private Python environment in the `.venv` folder next to the scripts;
- installs the exact library versions listed in `requirements.txt`;
- creates your runtime folder, `%USERPROFILE%\ReadmeMonitor` by default, with a starter
  `config.toml` (it never overwrites one that exists);
- runs a health check and prints one `PASS`, `WARN` or `FAIL` line per item.

`FAIL` for the GitHub credential is normal at this point: you have not stored one yet.
Installing or upgrading never enables the worker, never stores a credential, never spends money
and never creates a schedule. To use another runtime folder, add `-RuntimeHome D:\Data\ReadmeMonitor`
(any absolute folder on a local drive that you can write to).

Running the installer again later is the upgrade path: it keeps your configuration and data.

## 2. Look around with the simulation (recommended)

Before connecting anything real, start the interface in simulation mode:

```powershell
.\scripts\start.ps1 -Demo
```

Your browser opens a page served from your own computer. It shows five invented repositories
(owned by an imaginary `octo-demo` account) and a scripted stand-in for GitHub and for Claude.
Press **Run now** to see a full check go through its stages: inspect, evidence, draft, validate,
publish. Each further run plays the next event of a short scenario: a source change, a person
merging a pull request, a person editing a README by hand. A permanent **Simulation** label is
shown, nothing is sent to GitHub or Anthropic, Windows Task Scheduler is not touched, and the
temporary folder it uses is deleted when you stop it (Ctrl+C in the PowerShell window).

## 3. Start the real interface

Double-click **`ReadmeMonitor.cmd`** in the repository folder, or run:

```powershell
.\scripts\start.ps1
```

A PowerShell window stays open while the interface runs (leave it open; Ctrl+C stops it) and
your browser opens a page at an address like `http://127.0.0.1:8765`. That address only works on
your own computer. Options (also work with the `.cmd` file):

| Option | Meaning |
| --- | --- |
| `-RuntimeHome <folder>` | Use another runtime folder for this start |
| `-Port <number>` | First port to try (default 8765; the next free one is used if it is taken) |
| `-NoBrowser` | Print the address instead of opening the browser |
| `-Demo` | Simulation, as above |

The first start creates a starter `config.toml` if your runtime folder has none. The page opens
through a one-time sign-in address printed in the window; it works once, for two minutes. If you
later see a page that says to open ReadmeMonitor from the launcher, start it again with the
launcher: the page is deliberately not usable from a bookmark.

## 4. The six setup steps

The interface opens on **Setup** until all six steps are done. Each step shows what the program
itself has confirmed; nothing is marked done on trust.

1. **This computer.** Shows where your data will be kept. You can choose another runtime folder
   here, but only before the worker is enabled. The button **Enable the worker on this computer**
   is the deliberate switch that allows spending, publishing and scheduling for this folder
   on this computer. Until you press it (and confirm), the program can only read and draft. You
   can do the remaining steps first and enable last.
2. **GitHub.** Paste a GitHub personal access token. The program checks it once with a read-only
   request, shows which account it belongs to, and stores it in Windows Credential Manager.
   The token is never shown again, never written to a file and never sent anywhere except GitHub.
3. **Repositories.** The program lists the repositories your account owns (read-only; nothing is
   recorded) and you enroll the ones you want. For each, choose which README sections the monitor
   may write and whether source files may be sent to Anthropic (off by default; turning it on needs
   a confirmation and a list of file patterns).
4. **Model and spending.** Optionally paste an Anthropic API key, then set the three spending
   limits (per check, per day, per month). No limit is pre-filled. Without all three, no paid
   request is ever made.
5. **Publication and schedule.** Choose how each repository is updated: preview only (drafts stay
   on your computer), a pull request for you to review (recommended first), a pull request that
   merges itself when checks pass, or a direct commit to the branch (needs an explicit
   confirmation). Then choose the schedule: manual only, every N minutes, every day, or chosen
   weekdays, at your computer's time or UTC. Saving a schedule before the worker is enabled only
   records it. After you enable the worker, save the schedule once more so that it is registered
   with Windows; the schedule bar then shows what Windows holds.
6. **Dry run and start.** A dry run reads GitHub and shows the drafts the program would propose.
   It makes no model request, writes nothing to GitHub and works before the worker is enabled.
   When you are satisfied, finish setup.

The program edits `config.toml` for you. If you had written one by hand, the first interface
edit keeps a one-time copy next to it (`config.toml.pre-ui-<time>.bak`).

## 5. First real check

After enabling the worker, press **Run now** in the workbench (or let the schedule do it). For a
repository in `review_pr` mode a successful check ends with a pull request on GitHub that you
review and merge. The workbench shows the outcome above the stages, in three distinct words:
*Push confirmed* (GitHub accepted the write), *Pull request awaiting merge*, and *README
confirmed on the target branch* (the program read the README back and it matches the proposal).

## The command line

Everything the interface does is also available as commands, which is useful for scripts and
for troubleshooting. From the repository folder:

```powershell
$rm = ".\.venv\Scripts\readme-monitor.exe"
& $rm doctor                 # health check
& $rm status                 # what is happening, per repository
& $rm run-once --dry-run     # a dry run, from the command line
& $rm --help                 # every command
```

More: [Operations guide](operations.md), [Configuration](configuration.md),
[Troubleshooting](troubleshooting.md), [Privacy and data flow](privacy.md).
