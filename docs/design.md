# Interface design

The browser interface served by `readme-monitor ui`. Source lives in `ui/`; the compiled page is
committed in `readme_monitor/web/`. The contract with the backend is [api.md](api.md).

## Product and scene

ReadmeMonitor keeps the README sections of a person's own GitHub repositories accurate by drafting
evidence-based updates. The interface is an operating surface: a developer sets it up once, then
glances at it between other work to see what was checked, what changed and what was published.
It reads as a calm engineering workbench about one transformation: **commits become
documentation**. Nothing decorative competes with the diff or the status.

## Structure

- **Setup** (opens first while `onboarding.complete` is false, always reachable from the top bar):
  six steps matching `state.onboarding.steps`: this computer, GitHub, repositories, model and
  spending, publication and schedule, dry run and start. Each step shows the backend's done state;
  nothing claims success the backend did not confirm, and typed input survives a failed request.
  Setup never closes itself: the person leaves it with Start (enabled once a dry run has finished)
  or "Go to the workbench", at any time. An **Advanced** disclosure below the steps holds
  diagnostics, account-wide discovery, the generation policy and disabling the worker (confirmed).
- **Repository status** comes only from `RepoView.state` (`ui/src/lib/status.ts`): never checked is
  neutral, unchanged/published is OK, waiting states are attention, failures are failure, unknown
  states show their raw name. A repository with no events in the viewed run says so and shows its
  stored state; it is never reported as checked. The repository header carries Pause/Resume and,
  for `needs_init`/`adoption_deferred`, a confirmed adoption pull request.
- **Simulation**: every GitHub address is invented, so links render as text marked "(simulated,
  not a real address)", never as clickable links.
- A rendering error shows "Something on this screen failed to display" with Reload, not an empty page.
- **Workbench**: compact top bar; collapsible repository rail (drawer below 900 px); the workflow
  spine for the selected repository's live or latest run with its detail tabs (Commits, Evidence,
  README, Diff, Validation, Links); the schedule bar; run history and spending.
- The workflow spine is the one signature element: stages are nodes on a vertical wire, commit cards
  enter at the inspection stage, and the outcome sits above the spine so the answer comes first.
  Three publication states stay distinct: *Push confirmed* (GitHub accepted the write), *Pull
  request awaiting merge*, *README confirmed on the target branch*.

## Visual tokens

| Role | Dark (default) | Light | Use |
| --- | --- | --- | --- |
| Background | `#0e1116` | `#eef1f5` | page |
| Surface / raised | `#151a21` / `#1b212a` | `#ffffff` / `#f5f7fa` | panels, inputs, hover |
| Line | `#2a323e` | `#d3dae3` | 1 px borders |
| Text / secondary | `#e5e9ef` / `#a3adbb` | `#141a22` / `#4a5566` | |
| Cyan | `#56c8e6` | `#0a6f8c` | active work, primary action, focus of the live stage |
| Green | `#5fcf92` | `#1c7243` | confirmed completion |
| Amber | `#e7b04e` | `#8a5800` | attention, waiting, simulation label |
| Red | `#f07a70` | `#b3261e` | failure only |
| Brand | navy `#0a163c`, purple `#7650a8` | same | product mark and text selection only |

Every color meaning is paired with an icon and a word (chips, step states, rail indicators). Text
colors meet WCAG AA on their surfaces in both themes.

Type: **Instrument Sans** (variable) for the interface, **IBM Plex Mono** for hashes, paths, diffs and
repository names; both bundled through `@fontsource`, no remote fonts. Scale: 12 / 13 / 15 / 18 / 22 px
at the default root. Spacing: 4 px steps (4, 8, 12, 16, 20, 24, 32, 40). Radii: 4 / 6 / 10 px by
hierarchy. The root font size steps to 19 px from 2200 px and 22 px from 3200 px wide, so 4K screens
get larger, wider panes rather than a small centered column. Icons are inline SVG at 1.5 px stroke.

Theme: dark by default, light when the system prefers it, and a manual toggle (dark, light,
system) stored in `localStorage` as a non-secret preference.

## Motion system

Motion exists only to explain cause and effect, state change or spatial continuity, and it is driven
only by real events and state. There is no artificial wait, no typed-out model output, no progress
percentage and no looping activity other than one restrained indicator on the stage that is truly in
progress. Only `transform` and `opacity` are animated.

| Token | Duration | Easing | Distance | Enter / exit | Interruption rule | Reduced / Off |
| --- | --- | --- | --- | --- | --- | --- |
| `control` | 150 ms | `cubic-bezier(0.22, 1, 0.36, 1)` | 0 to 1 px press; scale 0.8 to 1 for step icons | fade + scale in; fade out | retargets from the current value | Reduced: fade only. Off: instant |
| `panel` | 260 ms | same | 8 px horizontal on repository change; drawer full width | fade + slide in; instant out on narrow | a new selection restarts from the current position | Reduced: fade only. Off: instant |
| `stage` | 450 ms | same | wire fill scaleY 0 to 1; outcome and commit cards 6 to 8 px | fade + short slide in | skipped when the next real event arrives within 300 ms, when events arrive in a burst (reconnect, fast run), and for anything loaded as history | Reduced: fade only. Off: instant |
| `layout` | spring, stiffness 520, damping 46 (critically damped, about 300 ms) | spring | rail selection highlight and tab indicator travel | shared element moves between positions | retargets mid-flight; no overshoot, no oscillation | Reduced and Off: jumps |
| `busy` | 1.6 s loop | same | a short bar travels down the active wire | only while a stage is `started` and not settled | stops the moment the stage settles | Reduced and Off: static tinted wire; the "In progress" label carries the state |

Rules:

- Shared-element continuity: the rail's selection highlight glides to the chosen repository, the
  workflow and detail pane slide in from the rail's side, and the tab indicator travels between tabs.
- History is never replayed: a past run opened from run history renders its final state at once and
  is labelled "History, not live".
- Decorative loops pause while the document is hidden.
- Motion preference: Full, Reduced or Off from the top bar, stored in `localStorage`. Without a stored
  choice, `prefers-reduced-motion` selects Reduced. Every state change stays visible in every mode.

## Accessibility and responsiveness

- Landmarks (`header`, `nav`, `main`), one `h1`, panel headings; a skip link.
- Full keyboard operation: tabs use arrow keys, Home and End; the drawer closes with Escape and
  returns focus to its trigger; visible focus rings from the palette.
- A polite live region announces live stage changes and outcomes, coalesced to at most one message
  per second; history is never announced.
- Forms have real labels, inline described errors and keep typed input on failure. Secrets use
  password fields, are sent once in a request body and are cleared after the backend confirms them;
  they never reach `localStorage`, `sessionStorage` or a URL.
- Repository text is shown as text or through Markdown with raw HTML disabled. Images are never
  loaded (their description and address are shown as text); only http and https links are opened, in
  a new tab with `rel="noopener noreferrer"`.
- Layouts tested at 3840x2160, 2560x1440, 1920x1080, 1024x768, 768x1024 and 375x812 with no horizontal
  page scroll. Below 900 px the rail becomes a drawer, panes stack, and touch targets are 44 px.

## Development

```powershell
cd ui
npm install
npm run dev        # sample data only, labelled "Sample data"; choose a scenario at the bottom right
npm run typecheck
npm run test       # pure logic: pipeline folding, schedule planning, link safety
npm run build      # writes readme_monitor/web/
```

The sample data in `ui/src/mock/` follows `api.md` exactly and is loaded only by `npm run dev`; the
production build does not contain it. The built page loads nothing from outside its own origin.
