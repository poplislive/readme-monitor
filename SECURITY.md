# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.3.x | Yes |
| Earlier than 0.3.0 | No |

ReadmeMonitor is pre-1.0. Only the newest 0.3.x release receives fixes.

## Reporting a vulnerability

Please report vulnerabilities privately. Do not open a public issue or pull request for a
security problem.

Use GitHub's private vulnerability reporting: on this repository open the **Security** tab and
choose **Report a vulnerability**. Include the version, what you did, what you expected and what
happened. Never include a real token or API key in a report.

This project is maintained by one person, so replies may take several days. Reports are
acknowledged, assessed, and fixed or explained as time allows. There is no bug bounty.

## In scope

- The `readme-monitor` command line and the worker in the `readme_monitor` package.
- The local interface (`readme-monitor ui`) and its local API: pairing, session, host, origin and
  CSRF checks, and rendering of repository text.
- Credential handling: any path that stores, logs, returns or transmits a GitHub token or
  Anthropic API key other than to the service it belongs to.
- The spending limits and ledger: any way to make the worker spend, or reserve money, beyond the
  configured limits, or to spend or publish on a computer that has not been activated.
- Publication: any way to write to a repository the user did not enrol, overwrite a human edit,
  or send a file that is excluded from export.

## Out of scope

- Problems that need an attacker who already controls the Windows account or the computer.
- Vulnerabilities in GitHub, Anthropic, Windows or third-party packages themselves (report them
  to their owners; tell us if ReadmeMonitor uses them in a way that makes them worse).
- Spending that happens outside this installation. The ledger counts only requests this
  installation made.
- Unsupported platforms. The worker supports Windows 11 only in this release.

## Security model in brief

- **Local interface.** `readme-monitor ui` listens on `127.0.0.1` only. A single-use pairing code,
  valid for two minutes, signs the browser in and sets a session cookie
  (`HttpOnly; SameSite=Strict`). Every request must carry an expected `Host` header, and every
  change must carry the server's own `Origin` and a CSRF header. Responses set a strict
  Content-Security-Policy. The interface makes no requests to other sites.
- **Credentials.** The GitHub token and Anthropic API key are stored in the operating system's
  credential store (Windows Credential Manager). They are never returned by the local API,
  written to events or logs, or placed in a URL. As an alternative, the documented environment
  variables are read if no stored credential exists.
- **Activation per computer.** A fresh installation cannot spend money, publish or register a
  scheduled task. Live work needs an explicit activation on that computer and runtime folder.
  Offline commands and dry runs work without it.
- **Spending limits and ledger.** Per-run, daily and monthly limits are checked against a local
  ledger before every Anthropic request, using a reservation of the worst-case cost that is later
  reconciled to actual usage. Without a configured budget, paid generation stays paused. The
  ledger counts only this installation's own requests, and a copied database is a snapshot, not
  a shared ledger.
- **What goes to Anthropic.** Only for repositories where source export is explicitly enabled
  (`api_export = "allowlist"`): allow-listed, size-limited files with secret-looking values
  redacted, files with sensitive names skipped, and repositories listed as export-restricted
  never sent. See [docs/privacy.md](docs/privacy.md).
- **Repository text is untrusted.** Source files, README text, commit messages and model output
  are treated as data. The model is told to ignore instructions found in them, can only propose
  text for named README sections, and application code validates and places that text. Only
  text between the monitor's own markers is ever replaced, and human edits inside them are never
  overwritten.
- **Markdown.** The interface shows repository and model text as plain text or as Markdown with
  raw HTML disabled.

## Limits

Secret redaction is pattern-based and can miss secrets that do not look like the known formats.
Do not enable source export for a repository whose allow-listed files may contain credentials.
No independent security audit has been performed.
