# Architecture

ReadmeMonitor has two halves that share one runtime folder:

- a **short-lived worker**: Windows Task Scheduler starts it on your schedule; it performs one
  synchronization cycle and exits;
- a **local interface server** (`readme-monitor ui`): a small web server on `127.0.0.1` that shows
  the worker's state and controls it. It does the same things the command line does and holds no
  state of its own.

All state lives in a SQLite database in the runtime folder (default `%USERPROFILE%\ReadmeMonitor`),
outside the source tree. Live work is allowed only for a runtime folder that was deliberately
enabled on the computer (see [Activation](#activation)).

## Components

| Module | Responsibility |
| --- | --- |
| `config.py` | Loads, validates and (for the interface) rewrites `config.toml`: enrolled repositories, discovery, export policy, limits, generation policy, schedule, model, prices |
| `runtime.py` | Activation: the computer's identity, the activation record in the runtime folder, the one active folder per Windows account, migration of earlier records |
| `discovery.py` | Paginated listing of the owner's repositories, reconciliation by repository id (renames, transfers, loss of access), dispositions |
| `github.py` | GitHub REST client: ETag conditional requests, pagination, rate-limit handling, bounded retries |
| `evidence.py` | Source allowlists, sensitive-file exclusion, fact extraction, payload limits, fingerprints |
| `credentials.py` | Windows Credential Manager lookup (via `keyring`) and secret redaction |
| `llm.py` | Claude request, budget reservation and reconciliation, structured-output validation |
| `readme.py` | Standard v1 sections, managed-block parsing, rendering and adoption |
| `publish.py` | Pull requests on a monitor-owned branch, or direct commits for repositories that opted in; ownership checks; publication confirmation |
| `db.py` | SQLite schema (version 3), short transactions, the event feed and stored drafts |
| `worker.py` | The cycle, process lock, fairness, backoff, and the events it emits |
| `scheduler.py` | Task Scheduler XML, registration, read-back and next-check calculation |
| `server.py` | The local interface server and its HTTP API |
| `web/` | The compiled interface (built from `ui/`, committed so users need no Node.js) |
| `simulation.py` | In-memory GitHub and scripted Claude, and the demo world used by `ui --demo` and the tests |
| `cli.py` | The `readme-monitor` command |

## Activation

Anything that can spend money, send repository content to Claude (including free token
counting), write to GitHub or create a schedule first asks `runtime.require_active`. It passes
only when all of these hold:

- the runtime folder contains an activation record written by `activate` (the interface's
  **Enable the worker on this computer**);
- the record names this computer, identified by the Windows MachineGuid, which changing needs
  administrator rights, so a folder copied to another computer is inactive there;
- the record names this exact folder, so a folder moved or copied elsewhere on the same computer
  is inactive too;
- the optional `[installation] host_lock` matches the computer name.

The computer name is informational: renaming the computer keeps the installation active. A
Windows account has one active runtime folder at a time (an account-level pointer names it). There
is deliberately no override switch. Records written by earlier versions (`bind-host`) are carried
forward only when machine, folder and host name all still match; otherwise the folder stays
inactive until you enable it again. An inactive installation can still read GitHub, run dry runs
and use every offline command. See [Moving an installation](moving-an-installation.md).

Nothing technical stops two *unrelated* installations (different folders, or different computers)
from watching the same repositories; each has its own ledger and would not know about the other's
work. Do not do that.

## One cycle

```text
activation ─► lock ─► recover interrupted reservations
  ─► discovery due? list every page, reconcile by repository id (a failed listing changes nothing)
  ─► [[repo]] entries + discovered repositories; those waiting for a retry time take no slot
  ─► up to max_repos_per_run, least recently checked first (stored, so it survives restarts):
  paused / transferred → skip
  empty (no commits) / archived / renamed → recorded, nothing written
  reconcile proposal → merged and README on target branch? → published
  head + releases    → conditional GET (304 = free); same as last time → done ("unchanged")
  inspect            → tree, README, checks, and the commits since the last analyzed revision
  checks pending     → wait (up to check_wait_minutes)
  not adopted        → adoption pull request (bounded daily batch) or needs_init / adoption_deferred
  document fingerprint unchanged → done
  evidence           → deterministic sections (structure, releases, license), allowlisted sources
  Claude-input fingerprint changed? → quiet period, per-repository interval, oldest-first queue
                     → reserve atomically against every limit → draft (Claude) → reconcile → validate
  render → no diff → no_changes
         → preview mode: write files under previews\   (proposal_created)
         → pull request modes: verify freshness + ownership → commit → PR (awaiting_merge)
         → direct mode: verify freshness → one non-force commit → read back (published)
```

## Events and drafts

Every stage a repository passes through stores an **event**: stage (`run`, `discover`, `queue`,
`inspect`, `evidence`, `draft`, `validate`, `publish`, `await`, `result`), status, the revision it
refers to, a short message and structured data (the commits that were examined, the files that
were read as evidence, the model's token usage, the pull request address). An event describes
something the worker actually did. Secret-looking text is redacted before an event is stored,
events are capped (the newest 20,000 are kept) and strings are bounded. Each proposed or dry-run
README is stored as a **draft** with the old text, the new text and the diff (the newest 300 are
kept). The interface reads both through the API and can replay them; a page reload or a stream
reconnect re-reads stored events and never re-runs anything. The schema is in
[api.md](api.md).

## Change detection without duplicate work

- **Content, not history.** Fingerprints use Git blob SHAs of the allowlisted files at the
  current head. Direct commits, squash merges, rebase merges, rewritten history and long
  downtime all reduce to "what is on the branch now". Many commits produce one proposal. (The
  commit list in an `inspect` event is for people to read; it never decides what changed.)
- **Two fingerprints.** The *Claude-input* fingerprint covers selected sources, documentation
  paths, the README outside managed blocks, completed check results, configuration, template
  and generator versions. The *document* fingerprint adds deterministic inputs (top-level
  layout, releases). Only a Claude-input change can cause a paid request.
- **No update loop.** Managed block contents are removed before fingerprinting, so merging the
  monitor's own pull request does not look like a new change. Only whether a block was edited by
  a person is included.
- **Incomplete answers fail closed.** A truncated Git tree or a list longer than the pagination
  bound raises an error instead of silently skipping files.
- **Stored output.** Claude's validated sections are stored with their input fingerprint. A
  retry after a stale-branch or publishing failure reuses them instead of paying again.

## Money and safety controls

- Every Claude attempt first reserves a conservative maximum cost: the input counted by the
  API's free token-counting endpoint plus a 5% and schema margin (or, if counting fails,
  characters / 2), plus the full `max_output_tokens`, since thinking is billed as output.
- Changing `publish_mode`, spending limits or pause state never triggers a new paid generation;
  only settings that change what Claude is asked are part of its input fingerprint.
- After the response, the reservation is replaced by the cost of the reported usage.
- Timeouts, connection failures and server errors keep the full reservation ("ambiguous"); a
  run that crashes mid-request leaves a reservation that the next run marks ambiguous. Restarts
  never reset counters.
- Requests rejected before processing (400-class, 429) release their reservation.
- Limits: per run, per UTC day, per UTC month, even monthly pacing, and optional per-repository
  day/month. They are global across repositories, README files, retries and entry points. The
  check and the reservation happen in one SQLite write transaction, so two processes cannot both
  spend the same remaining allowance. If any limit is missing, generation is paused and the
  pending work is kept.
- One ledger per runtime folder. A copy elsewhere is a snapshot and cannot run live work;
  `restore-db` carries forward newer usage records so a restore never re-opens a spent allowance.
- Token counting also sends the payload, so it passes the same activation check and export policy.
- The SDK's own retries are disabled so each attempt is accounted for. At most `max_attempts`
  (default 2) attempts per README.
- No refusal fallback to another model is configured: a refusal is reported for human review
  instead of silently switching models. Anthropic is the only supported provider.

## What Claude sees

Only for repositories with `api_export = "allowlist"` that are not export-restricted (discovered
repositories never are allowlisted; `[policy] export_restricted` wins over everything): files
matching `source_include`, minus `source_exclude`, minus sensitive names (`.env`, keys,
certificates, databases, anything named `*secret*` or `*credential*`), each at most
`max_file_bytes`, at most 60 files, with secret-looking values redacted, all inside
`max_payload_chars` (largest files dropped first and listed as omitted). Plus deterministic facts
(commands, entry points, dependencies, runtime versions, configuration names, documentation
paths, releases, check results for the revision) and the README text outside managed blocks.
[Privacy and data flow](privacy.md) lists exactly what leaves the computer.

The system prompt states that all of this is untrusted data. The response must match a JSON
schema. Application code then rejects: unrequested sections, citations of evidence that was not
supplied, marker text, HTML comments or active HTML, level-1/2 headings, oversized sections.
It flags for review (and withholds auto-merge and direct commits): uncited sections,
model-declared uncertainty, shell commands or configuration names not found in the evidence,
links to files that do not exist, and claim wording such as "production-ready" or coverage
percentages.

## Publishing

- **Pull request modes** (`review_pr`, `auto_pr`): one bot branch per repository
  (`readme-monitor/update`) and one open pull request. Before writing, the target branch must
  still be at the analyzed revision, every written path must be a configured README path, and the
  bot branch must either not exist, sit at the base, or have a head commit the monitor wrote (its
  recorded SHA or a commit carrying a `Readme-Monitor-Proposal` trailer for a known proposal).
  Otherwise it is a conflict. The bot branch is reset to the analyzed revision (a force update of
  the monitor's own branch only), then each README is committed through the Contents API.
  `auto_pr` asks GitHub to enable auto-merge, so branch protection, required reviews, checks and
  merge queues still apply.
- **Direct publication** (`direct`, opt-in per enrolled repository): for each README path one
  ordinary commit through the Contents API, pinned to the blob that was analyzed, made only while
  the target branch is still at the analyzed revision (or at the monitor's own previous commit).
  It never moves a ref and never forces. The commit carries the `Readme-Monitor-Proposal` trailer.
  If an answer is lost the program looks at the branch before deciding, so nothing is committed
  twice. A refusal by GitHub (branch protection, missing permission) is reported as
  `publish_blocked` and is not retried around. Content with review flags, and the one-time
  adoption of a README, always go through a pull request.
- A proposal becomes **published** only after the target branch's README contains the exact
  block hashes that were proposed.

## The interface server and its security model

`readme-monitor ui` serves the compiled page and a JSON API on `127.0.0.1` only. The full contract
is [api.md](api.md); the short version:

- **Pairing.** The launcher writes a random single-use code, opens an address containing it, and
  the server turns it into a session cookie (`HttpOnly`, `SameSite=Strict`) within two minutes.
  Without that cookie the API answers 401 and the page only says to start from the launcher.
- **Host and origin checks.** Requests must carry a loopback `Host` header (blocks DNS
  rebinding); every change (POST, PUT, DELETE) must also come from the server's own origin with a
  per-session token (blocks other web pages from driving it).
- **Content security.** The page is served with a strict Content-Security-Policy (same-origin
  scripts and styles only, no framing), and everything that originates in a repository, GitHub or
  the model is treated as data: it is returned as JSON text and rendered as text or through a
  Markdown renderer with raw HTML disabled.
- **Credentials** travel only in a request body to the server, go straight into Windows
  Credential Manager and are never returned, logged or stored in events.
- **A run is a separate process.** Starting a check from the page launches the same command the
  scheduler runs, detached, under the same lock. Closing the page does not stop it.

## Simulation

`readme-monitor ui --demo` serves the same interface from a temporary folder with an in-memory
GitHub and a scripted model (`simulation.py`). The real worker, validation, ledger and publication
code run against those stand-ins, so every stage, outcome and conflict can be seen without a
credential. Credentials typed into it are discarded, the schedule is kept in memory, and Task
Scheduler is not touched. The same stand-ins drive the automated tests.

## States

| State | Meaning |
| --- | --- |
| `unchanged` / `no_changes` | Nothing relevant changed, or the README already matches |
| `proposal_created` | Preview mode wrote a proposed README and diff locally |
| `awaiting_merge` | A documentation pull request is open |
| `merged_unconfirmed` | GitHub reports the merge; waiting to see the README on the branch |
| `published` | The proposed README is on the target branch |
| `rejected` | The PR was closed without merging; not re-proposed until inputs change |
| `waiting_for_checks` | CI for the new revision is still running |
| `needs_init` | The README has no managed blocks yet (run `init --dry-run`) |
| `adoption_deferred` | Today's adoption batch is used; retried the next UTC day |
| `generation_deferred` | Waiting for the quiet period or the per-repository interval; work is kept |
| `empty` / `archived` | No commits yet / archived or disabled on GitHub; nothing is written |
| `unsupported_readme` | The primary README is not Markdown |
| `renamed` / `transferred` | The repository's name or owner changed; a transfer stops processing |
| `conflict` | A person edited a managed block or the bot branch, or markers are malformed |
| `stale` | The branch moved while publishing; re-analyzed next run |
| `publish_blocked` | GitHub refused the write (branch protection or missing permission); nothing was written |
| `budget_paused` | No approved budget, or a limit would be exceeded; work is kept |
| `credentials_missing` / `permission_missing` | Fix the credential or its repository access |
| `inaccessible` / `error` | Repository missing or another failure; retried with backoff up to 6 hours |
| `paused` | Paused by `pause --repo` or `paused = true` |
