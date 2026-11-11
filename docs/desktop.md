# Desktop application

The desktop application is ReadmeMonitor as an installable program for Windows, macOS and Linux.
Nobody needs to clone the repository, install Python or Node, or keep a terminal open. It is the
same engine, the same interface and the same rules as the command line: the same budgets,
publication modes, source-export consent and history. The app only adds the parts a desktop
program needs: an install, a window, a tray icon and updates.

> **Status: release candidate in development.** Unsigned development installers are built by the
> Desktop workflow. No signed release has been published yet. [Platform support](#platform-support)
> lists which platforms have been verified so far.

## How it is put together

| Part | What it is |
| --- | --- |
| Shell | A small native program (Tauri 2) that owns the window, the tray icon, the menus and updates. |
| Engine | The Python engine frozen into a folder (`engine/rm-engine`, PyInstaller), shipped inside the app. |
| Interface | The existing interface, served by the engine on `127.0.0.1` with the same pairing, CSRF, Host/Origin checks and CSP as `readme-monitor ui`. |

When the app starts, the shell starts `rm-engine desktop-serve` and reads one line from the
engine's standard output: the port it chose and a one-time sign-in code. The shell then signs its
own window in: the loopback page posts the code in a request header. The code never appears in a
URL, on a command line, in a log or on disk, and it works once. The shell also checks that the port
answers as its own installation before showing it. The same private pipe carries the tray's
commands (Run now, Pause/Resume). When the shell exits, the pipe closes and the engine stops
serving. Checks it has started keep running in their own processes.

The loopback page has no access to the shell's native functions: no capability lists it. A link
to anywhere else opens in your browser, never in the app's window.

## Install

| System | File | Notes |
| --- | --- | --- |
| Windows 10/11, x64 | `ReadmeMonitor_<version>_x64-setup.exe` | Per-user install into `%LOCALAPPDATA%\Programs\ReadmeMonitor`; no administrator rights. Installs Microsoft Edge WebView2 if it is missing. |
| macOS 11 or later, Apple silicon or Intel | `ReadmeMonitor_<version>_aarch64.dmg` / `_x64.dmg` | Drag ReadmeMonitor to Applications. Development builds are not notarized: open them with Control-click, Open. |
| Ubuntu 22.04+ / Debian 12+, x64 | `ReadmeMonitor_<version>_amd64.deb` | `sudo apt install ./ReadmeMonitor_<version>_amd64.deb`. Needs WebKitGTK 4.1 (installed as a dependency). |
| Other Linux, x64 | `ReadmeMonitor_<version>_amd64.AppImage` | Portable: runs without installing. **It cannot schedule checks**, because the system scheduler cannot start a program from the AppImage's temporary mount. Use Run now, or the .deb. |

## Where things are kept

Program files and your data are kept apart. Updating or uninstalling the app never touches the
runtime folder.

| | Windows | macOS | Linux |
| --- | --- | --- | --- |
| Program | `%LOCALAPPDATA%\Programs\ReadmeMonitor` | `/Applications/ReadmeMonitor.app` | `/usr/bin` and `/usr/lib/ReadmeMonitor` (.deb; Tauri's layout, not yet checked on an installed system) |
| Runtime folder (settings, history, spending ledger, previews, logs, backups) | `%USERPROFILE%\ReadmeMonitor` | `~/Library/Application Support/ReadmeMonitor` | `$XDG_DATA_HOME/ReadmeMonitor` (default `~/.local/share/ReadmeMonitor`) |
| Credentials | Windows Credential Manager | macOS Keychain | Secret Service (GNOME Keyring, KWallet) |
| Shell's own state (window size, helper log) | `%APPDATA%` / `%LOCALAPPDATA%\io.github.poplislive.readmemonitor` | `~/Library/Application Support/io.github.poplislive.readmemonitor` | `~/.config`, `~/.local/share/io.github.poplislive.readmemonitor` |

An existing installation keeps its folder. If this account already activated a runtime folder
(`~/.readme-monitor.json` names it), the app, the command line and the scheduler all use that
folder. Nothing is relocated.

Credentials are only stored in the operating system's own store. If none is available or it is
locked, saving a credential fails with a message. ReadmeMonitor never falls back to a file. The
`ANTHROPIC_API_KEY` and `READMEMONITOR_GITHUB_TOKEN` environment variables still work as before.
They are never copied into logs, diagnostics or release files.

## Window, tray and quitting

- **Closing the window** hides it. ReadmeMonitor stays in the tray (Windows) or the menu bar (macOS),
  and the first close says so. On Linux, where a tray icon may not be visible, closing quits.
- **Opening it again** (the tray icon, or launching the app a second time) brings back the same
  window. A second launch never starts a second engine or a second ledger.
- **Quit** (tray menu, File menu, Ctrl/Cmd+Q) stops the app and its interface server.
- **Scheduled checks belong to the operating system's scheduler.** They run whether or not the app is
  open, and quitting does not cancel them. To stop them, use **Pause background checks** in the tray
  or **Pause** in the interface. Both use the same pause the command line uses (`readme-monitor pause`).
- The tray shows the engine's real state. **Run a check now** is available only when the worker is
  enabled, not paused, not already checking and no update is being installed.

## Scheduled checks on each system

ReadmeMonitor registers one scheduled check with the system scheduler of your account. It points at
the installed engine (`engine/rm-engine-bg.exe` on Windows, `engine/rm-engine` elsewhere), a path
that updates keep. The interface reads the registration back from the scheduler and shows it as **In
effect** only when the scheduler confirms it **and** it starts this copy of the engine.

| | Windows | macOS | Linux |
| --- | --- | --- | --- |
| Scheduler | Task Scheduler `\ReadmeMonitor\Sync` | launchd user agent `io.github.poplislive.readme-monitor.sync` | systemd user units `readme-monitor-sync.service` / `.timer` |
| Overlap | A start during a check is skipped (`IgnoreNew`), and `worker.lock` also guards against overlap | launchd never starts a running job twice, plus `worker.lock` | An active oneshot service is not started again, plus `worker.lock` |
| Missed while asleep or off | One catch-up start (`StartWhenAvailable`) | Coalesced into one start on wake | Calendar: one catch-up start (`Persistent=true`). Interval: monotonic time, which pauses during sleep |
| Time limit | `ExecutionTimeLimit` | The worker's own deadline only (launchd has none) | `TimeoutStartSec` |
| Daily at a UTC time | Yes | **No**: launchd uses local time, so the schedule is refused, not shifted | Yes |
| Weekly | Local time (all systems) | Local time | Local time |
| Signed out | No checks | No checks | No checks (lingering is never enabled) |
| Network down | The start waits for a network | The check runs and records the network error | The check runs and records the network error |

Changing the schedule only replaces the registration. It never starts or stops a check. On macOS,
a change made while a check is running is refused until the check ends, because reloading the agent
would interrupt it.

## Updates

**Help, Check for updates** (or the tray) asks GitHub Releases for a newer version. The app also checks
30 seconds after it starts and then once a day. You can choose **Later** or **Install and restart**:

1. The update is downloaded and its signature is verified against the public key built into the
   app. HTTPS and signature checks cannot be switched off.
2. The engine waits until no check is running. A running check always finishes; it is never
   interrupted. Then, while holding the worker lock, the engine writes a consistent database backup
   (`backups/pre-update-<from>-to-<to>-<time>.db`) and sets the **maintenance flag**
   (`maintenance.json`).
3. Every check started meanwhile, from the scheduler, the command line or the interface, sees the
   flag while holding the lock and exits without doing anything.
4. The app stops its own engine, installs the update and restarts.
5. At start-up the new engine opens the database (its versioned migrations run here), checks its
   integrity and only then clears the flag.

Your schedule, your pause, your credentials, your activation and your spending records are not
touched by any of this.

If something fails:

- **Download or signature fails:** nothing changes.
- **A check is still running after two hours:** the update is not installed.
- **The installer fails:** the current version keeps running.
- **The new version fails its start-up check** (for example an incompatible database): the flag
  stays, so scheduled checks keep skipping and the data stays exactly as it was. The app says so.
  Keep that version installed and report the problem.
- **An abandoned flag** stops blocking checks after 6 hours. It is never deleted; the next start-up
  check of the app clears it.

There is no automatic rollback. A newer database schema cannot be opened by an older version, and
restoring an older backup would lose the spending recorded since. See [operations](operations.md).

## Moving from the command-line installation

1. Install the desktop app and start it. It finds the activated runtime folder by itself: same
   settings, same history, same ledger, and the activation still applies.
2. **Schedule** shows a notice if the registered check still starts the old copy (for example
   `...\.venv\Scripts\pythonw.exe` in the checkout). Choose **Save schedule** once. The same task or
   unit is replaced, so there is never a second one. `readme-monitor schedule` shows the same
   read-back from a terminal.
3. Optional: remove the old checkout once the schedule shows **In effect**.

This changes no budget, no repository policy and no credential, and it activates nothing. A
computer that was never activated stays inactive.

## Uninstall and your data

| System | Uninstall | Removed | Kept |
| --- | --- | --- | --- |
| Windows | Settings, Apps, ReadmeMonitor | Program files, Start menu entry, **the scheduled task** | Runtime folder, credentials |
| macOS | First, in the app, **Setup, Turn the worker off**. This removes the launchd agent and keeps all data. Then move the app to the Trash | The app (and the agent, by the first step) | Runtime folder, credentials |
| Linux (.deb) | First, in the app, **Setup, Turn the worker off**. The package manager cannot remove per-user units. Then `sudo apt remove readme-monitor-desktop` | Program files (and the units, by the first step) | Runtime folder, credentials |

The engine inside the app accepts the same commands as `readme-monitor` for anyone who prefers a
terminal. Examples: `"/Applications/ReadmeMonitor.app/Contents/Resources/engine/rm-engine" uninstall-task`,
or `& "$env:LOCALAPPDATA\Programs\ReadmeMonitor\engine\rm-engine.exe" status`.

On Windows, the uninstaller's "Delete the application data" box removes only the shell's own state
(window size, logs), never the runtime folder.

To remove your data deliberately: deactivate first (**Setup, Turn the worker off**, or
`readme-monitor deactivate`). Then delete the runtime folder and remove the two credentials
(`readme-monitor remove-credential github`, `readme-monitor remove-credential anthropic`). Deleting
the runtime folder deletes the spending ledger and the history.

## Building from source

Every installer is built on its own system and architecture: PyInstaller does not cross-compile.

Windows (PowerShell, from the repository root; Python 3.12, Node 24, Rust stable with the MSVC tools):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-desktop.ps1
```

macOS and Linux (bash, from the repository root; on Linux first install `libwebkit2gtk-4.1-dev
libayatana-appindicator3-dev librsvg2-dev patchelf`):

```bash
python3 -m pip install -r packaging/requirements-build.txt && python3 -m pip install --no-deps .
python3 packaging/python_notices.py build/notices/PYTHON_THIRD_PARTY.md
python3 -m PyInstaller packaging/engine.spec --distpath build/engine-dist --workpath build/pyi --noconfirm
python3 packaging/smoke_engine.py build/engine-dist/engine/rm-engine
python3 packaging/rust_notices.py build/notices/RUST_THIRD_PARTY.md
cd desktop && npm ci && npx tauri build --config '{"bundle":{"createUpdaterArtifacts":false}}'
```

`packaging/check_versions.py` confirms one version across the engine, the interface, the shell and
the tag. Without `TAURI_SIGNING_PRIVATE_KEY` you get a development build. Its installer works, but
it cannot be offered as an update.

## Releasing

1. Set the same version everywhere: `readme_monitor/__init__.py`, `ui/package.json`,
   `desktop/package.json`, `desktop/src-tauri/Cargo.toml`. Then run `packaging/check_versions.py`.
2. Push the tag `vX.Y.Z`. The Desktop workflow builds every target, signs the updater payloads and
   attaches the installers, `latest.json` and `SHA256SUMS.txt` to a **draft** release.
3. Review the draft (installers, checksums, notes, evidence) and publish it. Installed apps see an
   update only after publication.

Repository secrets used only by tag builds, never by pull requests:

| Secret | For |
| --- | --- |
| `TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | Updater signatures. The public half is `plugins.updater.pubkey` in `desktop/src-tauri/tauri.conf.json`. A tag build without the key fails rather than publishing unsigned updates. |
| `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID` | Developer ID signing of the engine's nested binaries (`packaging/macos/sign-engine.sh`) and of the app, then notarization. Optional: without them the macOS build is unsigned. |

Windows publisher signing is not configured yet. Tauri accepts a certificate thumbprint or a custom
`signCommand` (for example a cloud signing service) in `bundle.windows`. A signature alone does not
bring an immediate SmartScreen reputation.

The updater signature and the operating system's publisher signature are separate. The first proves
that an update comes from the holder of the updater key. The second is what Windows SmartScreen and
macOS Gatekeeper check before a first install.

## Platform support

A platform is listed as supported only after an installed build has been checked on it. CI
compilation and mocked tests do not count. The evidence for each build is in
[verification](verification.md).

| Target | Build | Install | Runtime | Scheduler | Upgrade | Signing |
| --- | --- | --- | --- | --- | --- | --- |
| Windows x64 (PopL, Windows 11) | verified | verified (silent per-user install, uninstall) | partial: engine, sign-in, single instance, crash recovery; the window's visuals and tray not tested | partial: Task Scheduler starts the packaged engine; a full activated run not tested | partial: installer upgrade keeps location, task and data; in-app update not tested | updater: candidate key, not used; publisher: none |
| macOS arm64 / x64 | CI workflow prepared | not tested | not tested | not tested | not tested | not configured |
| Linux x64 (.deb, AppImage) | CI workflow prepared | not tested | not tested | systemd backend verified with real `systemctl --user` (WSL Ubuntu 26.04), not the packaged app | not tested | updater only |
