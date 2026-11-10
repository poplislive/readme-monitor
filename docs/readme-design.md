# README design: PopL Repository Standard v1

This page records how ReadmeMonitor's README layout was designed and what the reusable
template contains. The template lives at
[`readme_monitor/templates/popl-standard-v1.md`](../readme_monitor/templates/popl-standard-v1.md).

## References inspected

Four public READMEs were read through the GitHub API on 2026-11-09. Structure was adapted; no
wording, logos, screenshots, marketing claims or legal text were copied.

| Source | Revision inspected (branch head) | README last changed | Useful pattern | Adaptation in Standard v1 |
| --- | --- | --- | --- | --- |
| astral-sh/uv | `e2b7d2e13026` | 2026-07-08 | Short highlights list; commands paired with their output | Commands are shown with the output a reader should expect |
| fastapi/fastapi | `ba5d72bae328` | 2026-11-04 | Create → run → check progression | Quick start follows Configure → Run once → Verify → Schedule |
| shadcn-ui/ui | `8ab27715521c` | 2026-02-08 | Very focused front page that links to deeper docs | Operations, architecture and verification detail live in `docs/`, linked from the README |
| ollama/ollama | `f1ee5679fe6b` | 2026-11-06 | Platform-specific setup blocks; practical command grouping | Windows PowerShell is the primary path; a command reference table groups operations |

## Section contract

Sections always appear in this order with these headings. A repository can disable optional
sections in its configuration; the monitor never renames or reorders them.

| # | Heading | Section id | Default owner |
| --- | --- | --- | --- |
| 1 | Overview | `overview` | People |
| 2 | Features | `features` | Monitor (Claude) |
| 3 | Quick start | `quick-start` | Monitor (Claude) |
| 4 | Configuration | `configuration` | Monitor (Claude) |
| 5 | Architecture | `architecture` | Monitor (Claude) |
| 6 | Project structure | `project-structure` | Monitor (deterministic, from the file tree) |
| 7 | Development and testing | `development` | Monitor (Claude) |
| 8 | Current status and limitations | `status` | Monitor (Claude) |
| 9 | Recent changes | `recent-changes` | Monitor (deterministic, from GitHub releases) |
| 10 | Roadmap | `roadmap` | People |
| 11 | Contributing and support | `contributing` | People |
| 12 | License | `license` | People (deterministic text only if explicitly managed) |

Branding, screenshots, legal wording, the license decision, support contacts and roadmap
commitments stay human-owned unless a repository's configuration lists them in
`managed_sections`. The license section, when managed, only states facts: the license GitHub
detects, or that no license has been selected. It never chooses one.

## Managed blocks

```markdown
<!-- readme-monitor:start:features hash=0123456789ab -->
Validated generated content.
<!-- readme-monitor:end:features -->
```

- The `hash` attribute is the first 12 hex characters of the SHA-256 of the block content the
  monitor last wrote (trailing spaces and surrounding blank lines ignored). If the content no
  longer matches, a person edited it: the monitor reports a conflict and leaves the block alone.
- A block without a hash is treated as human-owned.
- Unknown ids, duplicates, nesting, unclosed blocks and stray marker text are rejected as
  malformed; nothing is written until a person fixes them.
- Text outside blocks is preserved byte for byte, including line endings.

## Adoption

`readme-monitor init --dry-run` shows the adoption diff for each enrolled README:

- Existing sections whose headings match the standard (for example "Installation" →
  Quick start, "How it works" → Architecture) are wrapped in markers with their current text,
  so the diff is only marker lines.
- Missing managed sections are added in standard order with a placeholder.
- Custom sections and all other text are untouched. A missing README is created from the template.

## Visual review checklist

Used before publishing ReadmeMonitor's own README (results in [verification](verification.md)):
relative links and anchors resolve, tables have header rows, code blocks are language-labelled,
the badge row stays short, and the page reads correctly in GitHub's light and dark themes and at
narrow widths.
