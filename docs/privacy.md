# Privacy and data flow

ReadmeMonitor runs on your computer, with your GitHub token and your Anthropic API key. There is
no ReadmeMonitor service: the project operates no server and receives nothing from you. This page
describes where data goes. Items marked "as specified" are described in [api.md](api.md) and may
still be under development in a given build.

## What stays on your computer

In the runtime folder (by default a `ReadmeMonitor` folder in your user profile):

- Configuration (`config.toml`): repositories, budget, schedule, model. It contains no credentials.
- The state database (`state.db`): per-repository state, a response cache for GitHub, proposals,
  the spending ledger, activity events (newest 20,000 kept, as specified) and drafts (newest 300,
  as specified).
- Logs, with secret-looking values redacted.
- Backups and drafts you create, including proposed README text.
- A single-use pairing file for the local interface, deleted after use (as specified).

Nothing here is uploaded anywhere.

## What goes to GitHub

All calls go to `api.github.com` with your token, from your computer.

Reads, in every mode:

- Your identity and the token's scopes; the list of repositories your account owns (when
  discovery is on); repository metadata and default branch.
- The file tree and the contents of files the tool needs: the README, and source files that match
  your allow-list when export is enabled.
- Releases, check runs and commit statuses, and comparisons between revisions.
- Existing branches and pull requests on the monitor's own branch.

Writes, only when the installation is activated and the repository's publishing mode allows it:

- `preview`: nothing is written to GitHub.
- `review_pr`: a branch owned by the monitor, a commit on it and a pull request. The pull request
  body lists the changed sections and any review flags.
- `auto_pr`: the same, plus a request to enable auto-merge.
- `direct` (as specified): a commit on the target branch. It needs explicit confirmation.

Dry runs read GitHub and write nothing.

## What goes to Anthropic

Requests go to the Anthropic API with your key. Nothing is sent unless:

1. the installation is activated,
2. a budget is configured, and
3. the repository has source export enabled (`api_export = "allowlist"`) and is not listed in
   `[policy] export_restricted`.

For such a repository a request contains:

- Files that match `source_include`, do not match `source_exclude`, are not larger than
  `max_file_bytes` (default 60,000), and are at most 60 in number. Files with sensitive names
  (for example `.env`, keys, certificates, `*secret*`, `*credential*`, database files) are skipped.
- The README text outside the monitor's own markers (first 20,000 characters), the current text
  of the sections to update, and facts derived by the tool (dependencies, commands, runtime,
  description, licence, recent releases, check results).
- The repository name, branch and revision.

Before sending, secret-looking values are replaced by `[REDACTED]` and the number of redactions
is shown as a review flag. Redaction is pattern-based and cannot catch every secret. The payload
is limited to 180,000 characters by default; the largest files are dropped first.

Before a request, the tool may call Anthropic's token-counting endpoint with the same payload to
estimate its cost. That endpoint is not billed. Requests are made by the official `anthropic`
client, subject to Anthropic's own terms and retention policies, which this project does not
control.

Repositories discovered but not enrolled never send source. Repositories with `api_export =
"none"` only receive deterministic updates, such as the project structure and recent changes.

## What the interface stores in your browser

The interface sets one session cookie (`HttpOnly`, same site, as specified) and keeps a motion and
theme preference in the browser's local storage. Nothing else is stored. It
loads nothing from other sites: fonts and scripts are served by the local server.

## What is never collected

- No telemetry, analytics, crash reporting or usage statistics.
- No third-party requests from the interface or from the worker, other than GitHub and Anthropic
  as described above.
- No account, e-mail address or other identity is requested by the tool.

## Where credentials live

The GitHub token and Anthropic API key are stored in Windows Credential Manager under the service
name `ReadmeMonitor`, or supplied through the environment variables `READMEMONITOR_GITHUB_TOKEN`
and `ANTHROPIC_API_KEY`. They are never written to configuration, the database, logs or events,
never returned by the local API and never placed in a URL.

## Removing everything

At a high level:

1. Remove the scheduled task (deactivate the installation in the interface, or
   `readme-monitor uninstall-task`).
2. Remove the stored credentials (the interface, or `readme-monitor remove-credential`; or delete
   the `ReadmeMonitor` entries in Credential Manager). Revoke the GitHub token and Anthropic key
   at their providers if you no longer need them.
3. Delete the runtime folder and the checkout.
4. Pull requests, branches and commits the monitor created remain on GitHub until you close or
   delete them.

A detailed uninstall guide is provided separately.

## Limits of the spending ledger

The ledger records the requests this installation made, and enforces the limits you set against
them. It does not see spending from other tools, computers or installations that use the same
Anthropic key, and it is not Anthropic's billing record. Check your Anthropic console for the
authoritative total.
