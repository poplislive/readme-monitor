// The central surface: one run of one repository as connected stages, plus its detail tabs.
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api, errorText } from "../api/client";
import type { Draft, RepoView, Run } from "../api/types";
import { useStore } from "../api/store";
import { latestRunFor, OUTCOME_TEXT, outcomeTitle, type RunView, type StepId, type StepStatus } from "../lib/pipeline";
import { firstLine, humanizeTimes, relative, short, usd, withoutUrls } from "../lib/format";
import { idleText } from "../lib/status";
import { T } from "../motion";
import { Chip, Empty, ExtLink, Icon, Loading, Notice, SafeMarkdown, type Tone } from "./ui";

const STATUS: Record<StepStatus, { icon: string; label: string; tone: Tone }> = {
  not_reached: { icon: "minus", label: "Not reached", tone: "neutral" },
  active: { icon: "wave", label: "In progress", tone: "active" },
  waiting: { icon: "clock", label: "Waiting", tone: "attention" },
  done: { icon: "check", label: "Done", tone: "ok" },
  skipped: { icon: "minus", label: "Skipped", tone: "neutral" },
  failed: { icon: "x", label: "Failed", tone: "fail" },
};
const OUTCOME_ICON: Record<string, string> = { ok: "check", active: "wave", attention: "clock", fail: "alert", neutral: "dot" };
const MODE_LABEL: Record<string, string> = { preview: "Preview only", review_pr: "Pull request for review", auto_pr: "Self-merging pull request", direct: "Direct commit" };
export const modeLabel = (m: string | null | undefined) => (m ? MODE_LABEL[m] ?? m : "Discovery defaults");

export type Tab = "commits" | "evidence" | "readme" | "diff" | "validation" | "links";
const TABS: { id: Tab; label: string }[] = [
  { id: "commits", label: "Commits" }, { id: "evidence", label: "Evidence" }, { id: "readme", label: "README" },
  { id: "diff", label: "Diff" }, { id: "validation", label: "Validation" }, { id: "links", label: "Links" },
];
const TAB_FOR: Record<StepId, Tab> = { queue: "commits", inspect: "commits", evidence: "evidence", draft: "readme", validate: "validation", publish: "links", await: "links" };

/** Animate only for live, unhurried changes; bursts (reconnect, fast runs) and history settle instantly. */
function useHandoff(view: RunView, liveFrom: number) {
  const last = useRef({ id: 0, at: 0 });
  const now = performance.now();
  const burst = now - last.current.at < 300;
  const animate = view.live && view.lastEventId >= liveFrom && view.lastEventId !== last.current.id && !burst;
  useEffect(() => { if (view.lastEventId !== last.current.id) last.current = { id: view.lastEventId, at: performance.now() }; }, [view.lastEventId]);
  return animate;
}

export function Workflow({ repo, actions, view, run, history, liveFrom, onStep, onLeaveHistory }: {
  repo: RepoView | null; actions?: ReactNode; view: RunView | null; run: Run | null; history: boolean; liveFrom: number; onStep: (t: Tab) => void; onLeaveHistory: () => void;
}) {
  const animate = useHandoff(view ?? ({ live: false, lastEventId: 0 } as RunView), liveFrom);
  const tr = animate ? T.stage : T.none;
  if (!view) return <section className="panel workflow" aria-labelledby="wf-h"><h2 id="wf-h" className="panel-title">Workflow</h2>{actions}<Empty title="No runs yet for this repository">{repo && idleText(repo)} Choose Run now, or wait for the next scheduled check.</Empty></section>;
  const o = view.outcome;
  const ot = o ? OUTCOME_TEXT[o.kind] : null;
  const ins = view.inspect;
  return (
    <section className="panel workflow" aria-labelledby="wf-h">
      <header className="wf-head">
        <div>
          <h2 id="wf-h" className="panel-title">{repo?.name ?? "All repositories"}</h2>
          <p className="muted small">
            {history ? "Past " : ""}{view.dry ? (history ? "dry run" : "Dry run") : (history ? "run" : "Run")} #{view.runId}
            {view.live || (run && !run.finished_at) ? ", in progress" : run ? `, ${relative(run.started_at)}` : ""}.{" "}
            {repo && <>{modeLabel(repo.mode)} on {repo.branch ?? "the default branch"}.</>}
          </p>
        </div>
        {history && <div className="history-tag"><Chip tone="neutral" icon="history">History, not live</Chip><button className="btn ghost" onClick={onLeaveHistory}>Back to latest</button></div>}
      </header>
      {actions}

      {o && ot && o.kind !== "running" && (
        <motion.div className={`outcome tone-${ot.tone}`} key={o.kind} initial={animate ? { opacity: 0, y: 6 } : false} animate={{ opacity: 1, y: 0 }} transition={tr}>
          <Icon name={OUTCOME_ICON[ot.tone]} size={20} />
          <div><p className="outcome-title">{outcomeTitle(view)}</p><p className="small">{o.kind === "no_activity" ? idleText(repo) : humanizeTimes(withoutUrls(o.detail, o.prUrl, o.commitUrl))}</p>
            {(o.prUrl || o.commitUrl) && <p className="small links-row">{o.prUrl && <ExtLink href={o.prUrl}>Pull request</ExtLink>}{o.commitUrl && <ExtLink href={o.commitUrl}>Commit {short(o.commitSha)}</ExtLink>}</p>}
          </div>
        </motion.div>
      )}

      <ol className="spine">
        {view.steps.map((s) => {
          const st = STATUS[s.status];
          return (
            <li key={s.id} className={`step st-${s.status}`}>
              <div className="node" aria-hidden="true">
                <AnimatePresence initial={false} mode="popLayout">
                  <motion.span key={s.status} className={`node-icon tone-${st.tone}`} initial={{ opacity: 0, scale: 0.8 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: 0.8 }} transition={animate ? T.control : T.none}>
                    <Icon name={st.icon} size={14} />
                  </motion.span>
                </AnimatePresence>
              </div>
              <span className="wire" aria-hidden="true"><motion.span className="wire-fill" initial={false} animate={{ scaleY: s.status === "done" ? 1 : 0 }} transition={tr} />{s.status === "active" && <span className="busy" />}</span>
              <button className="step-body" onClick={() => onStep(TAB_FOR[s.id])} aria-label={`${s.label}: ${st.label}. ${s.message ?? s.reason ?? ""} Show details.`}>
                <span className="step-head"><span className="step-label">{s.label}</span><span className={`step-state tone-${st.tone}`}>{st.label}</span></span>
                {(s.message || s.reason) && <span className="step-msg">{s.status === "not_reached" || s.status === "skipped" ? s.reason ?? s.message : s.message}</span>}
                {s.id === "inspect" && ins?.commits && (
                  <span className="commits">
                    {ins.commits.slice(0, 3).map((c) => (
                      <motion.span key={c.sha} className="commit-card" initial={animate ? { opacity: 0, x: -8 } : false} animate={{ opacity: 1, x: 0 }} transition={tr}>
                        <code>{short(c.sha)}</code><span className="commit-msg">{firstLine(c.message)}</span><span className="muted small">{c.author}, {relative(c.date)}</span>
                      </motion.span>
                    ))}
                    {(ins.commits_total ?? 0) > 3 && <span className="muted small">and {(ins.commits_total ?? 0) - 3} more</span>}
                    {ins.files_changed && <span className="muted small">{ins.files_total ?? ins.files_changed.length} files changed, +{ins.files_changed.reduce((a, f) => a + f.additions, 0)} / −{ins.files_changed.reduce((a, f) => a + f.deletions, 0)}</span>}
                  </span>
                )}
                {s.id === "evidence" && view.evidence?.files_read && <span className="muted small">{view.evidence.files_read.length} files read, {view.evidence.facts ?? 0} facts{view.evidence.export === "none" ? ", no source export" : ""}</span>}
                {s.id === "draft" && view.draft?.sections_returned && <span className="chips">{view.draft.sections_returned.map((x) => <span key={x} className="tag">{x}</span>)}{view.draft.usage && <span className="muted small">{usd(view.draft.usage.cost_usd)}</span>}</span>}
                {s.id === "validate" && view.validate && <ValidateSummary v={view.validate} />}
                {s.id === "publish" && s.status === "done" && <span className="small"><Chip tone="active" icon="check">Push confirmed</Chip> GitHub accepted the write.</span>}
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function ValidateSummary({ v }: { v: NonNullable<RunView["validate"]> }) {
  const changed = Object.values(v.changed ?? {}).flat();
  const conflicts = Object.values(v.conflicts ?? {}).flat();
  return (
    <span className="chips">
      {changed.map((x) => <span key={x} className="tag tone-ok">{x}</span>)}
      {conflicts.map((x) => <span key={x} className="tag tone-attention"><Icon name="alert" size={12} />{x}</span>)}
      {!!v.flags?.length && <span className="tag tone-attention">{v.flags.length} flags</span>}
      {!conflicts.length && !v.flags?.length && !v.error && <span className="muted small">Clean pass</span>}
    </span>
  );
}

export function Detail({ view, repo, tab, setTab }: { view: RunView | null; repo: RepoView | null; tab: Tab; setTab: (t: Tab) => void }) {
  const { events } = useStore();
  // The stored latest draft belongs to this view only when this is the repository's latest run and that run drafted something.
  const latest = repo && view?.draft && view.runId !== null && latestRunFor(events, repo.name) === view.runId ? repo.latest_draft_id : null;
  const draftId = view?.draft?.draft_id ?? view?.validate?.draft_id ?? latest ?? null;
  const [draft, setDraft] = useState<{ id: number; d: Draft | null; err: string | null } | null>(null);
  useEffect(() => {
    if (draftId === null || draft?.id === draftId) return;
    setDraft({ id: draftId, d: null, err: null });
    api.draft(draftId).then((d) => setDraft({ id: draftId, d, err: null }), (e) => setDraft({ id: draftId, d: null, err: errorText(e) }));
  }, [draftId, draft?.id]);
  const onKey = (e: KeyboardEvent) => {
    const i = TABS.findIndex((t) => t.id === tab);
    const n = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : null;
    if (n === null) return;
    e.preventDefault();
    const t = TABS[(n + TABS.length) % TABS.length].id;
    setTab(t); document.getElementById(`tab-${t}`)?.focus();
  };
  const d = draft?.id === draftId ? draft : null;
  return (
    <section className="panel detail" aria-label="Run details">
      <div className="tabs" role="tablist" aria-label="Run details" onKeyDown={onKey}>
        {TABS.map((t) => (
          <button key={t.id} id={`tab-${t.id}`} role="tab" aria-selected={tab === t.id} aria-controls="tabpanel" tabIndex={tab === t.id ? 0 : -1} className="tab" onClick={() => setTab(t.id)}>
            {t.label}{tab === t.id && <motion.span layoutId="tab-ind" className="tab-ind" transition={T.layout} />}
          </button>
        ))}
      </div>
      <div id="tabpanel" role="tabpanel" aria-labelledby={`tab-${tab}`} className="tabpanel" tabIndex={0}>
        {!view ? <Empty title="Nothing to show yet">Details appear after the first check of this repository.</Empty>
          : tab === "commits" ? <Commits v={view} />
          : tab === "evidence" ? <Evidence v={view} />
          : tab === "readme" ? <DraftView d={d} kind="readme" />
          : tab === "diff" ? <DraftView d={d} kind="diff" />
          : tab === "validation" ? <Validation v={view} d={d?.d ?? null} />
          : <Links v={view} repo={repo} />}
      </div>
    </section>
  );
}

function Commits({ v }: { v: RunView }) {
  const i = v.inspect;
  if (!i) return <Empty title="Commits were not inspected in this run">{v.steps[1].reason ?? "The run did not reach this stage."}</Empty>;
  return (
    <div className="stack">
      {i.first_scan && <Notice icon="dot">First scan: there is no earlier baseline, so only the head commit is listed.</Notice>}
      {i.compare_status === "diverged" && <Notice tone="attention">History was rewritten since the last check. The list shows what GitHub returned for the new head; changes are still detected by content.</Notice>}
      {i.truncated && <Notice>Only part of the list is shown; GitHub reported {i.commits_total} commits and {i.files_total} files.</Notice>}
      {!i.first_scan && i.compare_status && <p className="muted small">Compared <code>{short(i.baseline)}</code> with <code>{short(i.head)}</code>: {i.compare_status}.</p>}
      <ul className="rows">
        {(i.commits ?? []).map((c) => <li key={c.sha} className="row"><code>{short(c.sha)}</code><span className="grow">{firstLine(c.message)}</span><span className="muted small">{c.author}, {relative(c.date)}</span></li>)}
      </ul>
      {!!i.files_changed?.length && <><h3 className="h3">Changed files</h3><ul className="rows">{i.files_changed.map((f) => <li key={f.path} className="row"><code className="grow">{f.path}</code><span className="small muted">{f.status}</span><span className="small add">+{f.additions}</span><span className="small del">−{f.deletions}</span></li>)}</ul></>}
    </div>
  );
}

function Evidence({ v }: { v: RunView }) {
  const e = v.evidence;
  if (!e) return <Empty title="No evidence was gathered in this run">{v.steps[2].reason ?? "The run did not reach this stage."}</Empty>;
  return (
    <div className="stack">
      <p>{e.facts ?? 0} facts from {e.files_read?.length ?? 0} files. Source export: {e.export === "allowlist" ? "on (only files matching the repository's patterns)" : "off (README and repository metadata only)"}.</p>
      <h3 className="h3">Files actually read</h3>
      <ul className="rows">{(e.files_read ?? []).map((f) => <li key={f} className="row"><Icon name="file" size={14} /><code>{f}</code></li>)}</ul>
      {!!e.skipped?.length && <><h3 className="h3">Skipped</h3><ul className="rows">{e.skipped.map((s) => <li key={s} className="row muted">{s}</li>)}</ul></>}
    </div>
  );
}

function DraftView({ d, kind }: { d: { d: Draft | null; err: string | null } | null; kind: "readme" | "diff" }) {
  if (!d) return <Empty title="No draft for this run">A draft appears once README changes are written.</Empty>;
  if (d.err) return <Notice tone="fail">{d.err}</Notice>;
  if (!d.d) return <Loading label="Loading the draft" />;
  return (
    <div className="stack">
      {Object.entries(d.d.files).map(([path, f]) => (
        <div key={path} className="stack">
          <p className="file-head"><Icon name="doc" size={14} /><code>{path}</code>{f.changed.map((c) => <span key={c} className="tag tone-ok">{c}</span>)}{f.conflicts.map((c) => <span key={c} className="tag tone-attention">{c} conflict</span>)}</p>
          {kind === "diff" ? <Diff text={f.diff} /> : (
            <div className="compare">
              <div><h3 className="h3">Before</h3><SafeMarkdown text={f.old} /></div>
              <div><h3 className="h3">After</h3><SafeMarkdown text={f.new} /></div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function Diff({ text }: { text: string }) {
  return (
    <pre className="diff" aria-label="Unified diff">
      {text.split("\n").map((l, i) => {
        const c = l.startsWith("+++") || l.startsWith("---") ? "meta" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : "";
        return <span key={i} className={`dl ${c}`}><span className="sr-only">{c === "add" ? "added: " : c === "del" ? "removed: " : ""}</span>{l || " "}{"\n"}</span>;
      })}
    </pre>
  );
}

function Validation({ v, d }: { v: RunView; d: Draft | null }) {
  const x = v.validate;
  if (!x) return <Empty title="Not validated in this run">{v.steps[4].reason ?? "The run did not reach this stage."}</Empty>;
  const conflicts = Object.entries(x.conflicts ?? {});
  const flags = x.flags ?? d?.flags ?? [];
  return (
    <div className="stack">
      {x.error && <Notice tone="fail">{x.error}</Notice>}
      {!flags.length && !conflicts.length && !x.error && <Notice tone="ok" icon="check">Clean pass: no flags and no conflicts.</Notice>}
      {!!flags.length && <><h3 className="h3">Flags</h3><p className="small muted">Flagged content always goes to a pull request, even in direct mode.</p><ul className="rows">{flags.map((f) => <li key={f} className="row"><Icon name="alert" size={14} />{f}</li>)}</ul></>}
      {!!conflicts.length && <><h3 className="h3">Conflicts</h3><ul className="rows">{conflicts.map(([p, s]) => <li key={p} className="row"><code>{p}</code>{s.join(", ")}: edited by a person, left untouched.</li>)}</ul></>}
      {!!(x.notes ?? d?.notes)?.length && <><h3 className="h3">Notes</h3><ul className="rows">{(x.notes ?? d?.notes ?? []).map((n) => <li key={n} className="row">{n}</li>)}</ul></>}
    </div>
  );
}

function Links({ v, repo }: { v: RunView; repo: RepoView | null }) {
  const p = v.publish;
  return (
    <ul className="rows">
      <li className="row"><span className="grow">Commit</span>{p?.commit_url ? <ExtLink href={p.commit_url}>{short(p.commit_sha)}</ExtLink> : <span className="muted">No commit in this run</span>}</li>
      <li className="row"><span className="grow">Pull request</span>{p?.pr_url || v.outcome?.prUrl ? <ExtLink href={p?.pr_url ?? v.outcome?.prUrl}>#{p?.pr_number ?? repo?.proposal?.pr_number}</ExtLink> : <span className="muted">No pull request in this run</span>}</li>
      <li className="row"><span className="grow">Repository</span>{repo ? <ExtLink href={`https://github.com/${repo.name}`}>{repo.name}</ExtLink> : <span className="muted">Choose a repository</span>}</li>
    </ul>
  );
}

