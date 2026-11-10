# Contributing to ReadmeMonitor

Thank you for your interest. Please read this page before opening a pull request.

## Licence

ReadmeMonitor is licensed under the [Apache License, Version 2.0](LICENSE). By submitting a
contribution you agree that it is licensed under the same terms (section 5 of the licence). You are
welcome to open an issue to discuss an idea or report a problem first. Security problems go through
[SECURITY.md](SECURITY.md), not an issue.

## Prerequisites

- Windows 11 for the worker. It is the only supported platform for running live work. The test
  suite also runs on Linux in CI.
- Python 3.11 or newer.
- Git.
- Node.js and npm, only if you change the interface under `ui/`.

## Set up

From a checkout, in PowerShell:

```powershell
.\scripts\install.ps1 -Dev
```

This creates `.venv`, installs the pinned dependencies and the test tools, and creates a runtime
folder with a starter configuration. It stores no credentials and registers no scheduled task.

## Run the tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use simulated GitHub and Claude. They must never call the real GitHub or Anthropic APIs,
read real credentials, register a scheduled task or write outside a temporary folder.

## Build the interface

```powershell
cd ui
npm ci
npm run build
```

The compiled output in `readme_monitor/web/` is committed, so users do not need Node. If you
change anything under `ui/`, rebuild and commit the output together with the source. If you add,
remove or upgrade an interface dependency, also run `python scripts/third_party_notices.py`.

## Try the simulation

```powershell
.\.venv\Scripts\readme-monitor.exe ui --demo
```

This serves the interface from a temporary folder with an in-memory GitHub and a scripted model.
Nothing is sent to GitHub or Anthropic and Task Scheduler is not touched.

## Coding expectations

- No live calls in tests. Use the existing fakes in `tests/`.
- No secrets, tokens, personal paths, computer names or account names in code, fixtures,
  documentation or comments. Use neutral examples such as `your-account/your-repository`.
- Keep every guard and budget check. Do not weaken validation, redaction, the spending limits,
  the activation requirement or an existing test to make a change pass.
- Keep changes small and in the existing style. Prefer the standard library; a new runtime
  dependency needs a clear reason.
- Add or update tests for new behaviour, especially anything that touches money, credentials,
  publication or stored data.
- Keep documentation accurate. Do not describe behaviour the code does not have.

## Proposing a change

1. Open an issue describing the problem or idea and wait for a response.
2. Fork the repository, work on a branch, and open a pull request
   that explains what changed and how you tested it.
3. Make sure the tests pass and, for interface changes, that the committed build is up to date.

Maintainers may decline changes that widen what the worker can spend, publish or send.
