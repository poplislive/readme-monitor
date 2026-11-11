// Setup in six steps. Each step's done state comes from the backend; nothing here claims success on its own.
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { api, errorText, RUN_REFUSALS } from "../api/client";
import { refreshState, useStore } from "../api/store";
import type { Coverage, CredentialAnswer, Diagnostics, DiscoveredRepo, Discovery, OnboardingStep, PublishMode, RepoView, State } from "../api/types";
import { clock, coverageText } from "../lib/format";
import { eventsFor, foldRun } from "../lib/pipeline";
import { ScheduleBar } from "./ScheduleBar";
import { Chip, Empty, Icon, Loading, Notice } from "./ui";
import { Detail, Workflow, type Tab } from "./Workflow";

const TITLES: Record<OnboardingStep, string> = { runtime: "This computer", github: "GitHub", repositories: "Repositories", model: "Model and spending", publication: "Publication and schedule", dry_run: "Dry run and start" };

export const SECTIONS: { id: string; label: string; why: string }[] = [
  { id: "features", label: "Features", why: "What the project does, written from approved evidence." },
  { id: "quick-start", label: "Quick start", why: "First commands to install and run it." },
  { id: "configuration", label: "Configuration", why: "Settings and their defaults." },
  { id: "architecture", label: "Architecture", why: "How the main parts fit together." },
  { id: "project-structure", label: "Project structure", why: "Folder overview built from the file tree, no model needed." },
  { id: "development", label: "Development and testing", why: "How to run checks and contribute code." },
  { id: "status", label: "Current status and limitations", why: "What works today and what does not." },
  { id: "recent-changes", label: "Recent changes", why: "Built from GitHub releases, no model needed." },
  { id: "overview", label: "Overview", why: "Normally written by people." },
  { id: "roadmap", label: "Roadmap", why: "Normally written by people." },
  { id: "contributing", label: "Contributing and support", why: "Normally written by people." },
  { id: "license", label: "License", why: "States only the license GitHub detects; never chooses one." },
];

/** Runs an action, keeps typed input on failure and reports the backend's own message. */
function useAction() {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = async <T,>(f: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true); setErr(null);
    try { const r = await f(); await refreshState(); return r; } catch (e) { setErr(errorText(e)); return undefined; } finally { setBusy(false); }
  };
  return { busy, err, run, setErr };
}

export function Onboarding({ state, onFinish }: { state: State; onFinish: () => void }) {
  const steps = state.onboarding.steps;
  const [cur, setCur] = useState<OnboardingStep>(() => steps.find((s) => !s.done)?.id ?? "runtime");
  const i = steps.findIndex((s) => s.id === cur);
  const go = (d: number) => { const n = steps[i + d]; if (n) { setCur(n.id); requestAnimationFrame(() => document.getElementById("step-h")?.focus()); } };
  return (
    <div className="setup">
      <nav className="stepper" aria-label="Setup steps">
        <ol>
          {steps.map((s, k) => (
            <li key={s.id}>
              <button className={`stepper-btn${s.id === cur ? " on" : ""}`} aria-current={s.id === cur ? "step" : undefined} onClick={() => setCur(s.id)}>
                <span className={`stepper-mark ${s.done ? "tone-ok" : ""}`} aria-hidden="true">{s.done ? <Icon name="check" size={14} /> : k + 1}</span>
                <span><span className="stepper-title">{TITLES[s.id]}</span><span className="small muted">{s.done ? "Done" : "Not done"}: {s.detail}</span></span>
              </button>
            </li>
          ))}
        </ol>
      </nav>
      <section className="panel setup-body" aria-labelledby="step-h">
        <h2 id="step-h" className="setup-h" tabIndex={-1}>{TITLES[cur]}</h2>
        {cur === "runtime" && <RuntimeStep state={state} />}
        {cur === "github" && <GithubStep state={state} />}
        {cur === "repositories" && <ReposStep state={state} />}
        {cur === "model" && <ModelStep state={state} />}
        {cur === "publication" && <PublicationStep state={state} />}
        {cur === "dry_run" && <DryRunStep state={state} onFinish={onFinish} />}
        <div className="step-nav">
          <button className="btn ghost" disabled={i === 0} onClick={() => go(-1)}>Previous step</button>
          {i < steps.length - 1 && <button className="btn" onClick={() => go(1)}>Next step</button>}
          <button className="btn ghost" onClick={onFinish}>Go to the workbench</button>
        </div>
      </section>
      <Advanced state={state} />
    </div>
  );
}

function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return <dl className="facts">{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>;
}

function RuntimeStep({ state }: { state: State }) {
  const ins = state.installation;
  const a = useAction();
  const [confirming, setConfirming] = useState(false);
  const [home, setHome] = useState(ins.home);
  return (
    <div className="stack">
      <Facts rows={[["Runtime folder", <code>{ins.home}</code>], ["Computer", ins.host], ["Account", ins.account], ["Platform", <>{ins.platform} {ins.supported ? <Chip tone="ok" icon="check">Supported</Chip> : <Chip tone="fail" icon="x">Not supported</Chip>}</>],
        ["Worker", ins.active ? <Chip tone="ok" icon="check">Enabled{ins.activation ? ` since ${new Date(ins.activation.activated_at).toLocaleDateString()}` : ""}</Chip> : <Chip tone="neutral" icon="plug">Not enabled</Chip>]]} />
      {ins.problem && <Notice tone="attention">{ins.problem}</Notice>}
      {!ins.active && (
        <>
          <p>Until the worker is enabled on this computer, nothing can spend money, publish to GitHub or be scheduled. Enabling it does not start anything by itself: checks begin only after you press Start in the last step, or choose Run now.</p>
          {ins.can_change_home && (
            <form className="field-row" onSubmit={(e) => { e.preventDefault(); a.run(() => api.setHome(home)); }}>
              <label className="field grow"><span>Runtime folder</span><input value={home} onChange={(e) => setHome(e.target.value)} spellCheck={false} aria-describedby="home-help" /></label>
              <button className="btn" disabled={a.busy || home === ins.home}>Use this folder</button>
              <p id="home-help" className="small muted full">An absolute folder on a local drive that this account can write to. It holds the database, drafts and logs; credentials stay in {ins.credential_store}.</p>
            </form>
          )}
          {!confirming ? <button className="btn primary" disabled={!ins.supported || a.busy} onClick={() => setConfirming(true)}><Icon name="plug" />Enable the worker on this computer</button> : (
            <div className="confirm" role="group" aria-label="Confirm enabling the worker">
              <p>Enable the worker on <strong>{ins.host}</strong> for <code>{ins.home}</code>? It can then use your limits, publish in the modes you choose, and register a scheduled task when you press Start.</p>
              <button className="btn primary" disabled={a.busy} onClick={async () => { await a.run(() => api.activate()); setConfirming(false); }}>Enable now</button>
              <button className="btn ghost" onClick={() => setConfirming(false)}>Cancel</button>
            </div>
          )}
        </>
      )}
      {ins.active && <p className="small muted">To disable the worker on this computer, open Advanced below.</p>}
      {a.err && <Notice tone="fail">{a.err}</Notice>}
    </div>
  );
}

function SecretField({ label, help, onSave, present, onRemove, saveLabel }: { label: string; help: ReactNode; onSave: (v: string) => Promise<boolean>; present: boolean; onRemove: () => void; saveLabel: string }) {
  const [v, setV] = useState("");
  const [busy, setBusy] = useState(false);
  const id = label.replace(/\W+/g, "-").toLowerCase();
  async function submit(e: FormEvent) {
    e.preventDefault(); if (!v.trim()) return;
    setBusy(true);
    // The value lives in this field only until the backend confirms; it is never stored in the browser.
    if (await onSave(v.trim())) setV("");
    setBusy(false);
  }
  return (
    <form className="stack" onSubmit={submit} autoComplete="off">
      <label className="field"><span>{label}</span><input id={id} type="password" value={v} onChange={(e) => setV(e.target.value)} autoComplete="off" spellCheck={false} aria-describedby={`${id}-help`} placeholder={present ? "Stored. Paste a new one to replace it." : ""} /></label>
      <div id={`${id}-help`} className="small muted">{help}</div>
      <div className="row-actions"><button className="btn primary" disabled={busy || !v.trim()}>{saveLabel}</button>{present && <button type="button" className="btn ghost danger" onClick={onRemove}>Remove stored value</button>}</div>
    </form>
  );
}

function GithubStep({ state }: { state: State }) {
  const a = useAction();
  const [who, setWho] = useState<CredentialAnswer | null>(null);
  const [cov, setCov] = useState<Coverage | null>(null);
  const gh = state.credentials.github;
  useEffect(() => { if (gh.present) api.discover().then((d) => setCov(d.coverage), () => undefined); }, [gh.present, gh.login]);
  return (
    <div className="stack">
      <SecretField label="GitHub token" saveLabel="Verify and store token" present={gh.present} onRemove={() => a.run(() => api.deleteCredential("github"))}
        help={<>Use a <strong>fine-grained personal access token</strong> limited to the repositories you want monitored, with <em>Contents: read and write</em>, <em>Pull requests: read and write</em> and <em>Metadata: read</em>. A classic token with the <code>repo</code> scope also works but grants more than needed. The token goes to {state.installation.credential_store} and is never shown again.</>}
        onSave={async (v) => { const r = await a.run(() => api.setCredential("github", v)); if (r) setWho(r); return !!r; }} />
      {a.err && <Notice tone="fail">{a.err}</Notice>}
      {gh.present && <Facts rows={[["Signed in as", gh.login ?? who?.login ?? "unknown"], ["Token type", who?.token_kind === "classic" ? "Classic" : who?.token_kind ? `${who.token_kind[0].toUpperCase()}${who.token_kind.slice(1)}` : "Stored earlier"], ...(who?.scopes ? [["Scopes", who.scopes] as [string, ReactNode]] : []), ["What it can see", cov ? coverageText(cov) : "Checking…"]]} />}
    </div>
  );
}

function ReposStep({ state }: { state: State }) {
  const [disc, setDisc] = useState<Discovery | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [vis, setVis] = useState("all");
  const [enr, setEnr] = useState("all");
  const [arch, setArch] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const load = () => { setErr(null); api.discover().then(setDisc, (e) => setErr(errorText(e))); };
  useEffect(() => { if (state.credentials.github.present) load(); }, [state.credentials.github.present]); // eslint-disable-line react-hooks/exhaustive-deps
  const byName = useMemo(() => new Map(state.repos.map((r) => [r.name, r])), [state.repos]);
  if (!state.credentials.github.present) return <Empty title="Connect GitHub first">Repositories are listed from the account your token belongs to.</Empty>;
  if (err) return <Notice tone="fail">{err} <button className="btn ghost" onClick={load}>Try again</button></Notice>;
  if (!disc) return <Loading label="Listing your repositories (read-only)" />;
  const list = disc.repositories.filter((r) => r.name.toLowerCase().includes(q.toLowerCase()) && (vis === "all" || r.visibility === vis) && (arch || !r.archived)
    && (enr === "all" || (enr === "yes") === (byName.get(r.name)?.enrolled ?? r.enrolled)));
  return (
    <div className="stack">
      <p className="small muted">{coverageText(disc.coverage)} Listing is read-only and records nothing.</p>
      <div className="filters">
        <label className="field grow"><span>Search</span><input type="search" value={q} onChange={(e) => setQ(e.target.value)} /></label>
        <label className="field"><span>Visibility</span><select value={vis} onChange={(e) => setVis(e.target.value)}><option value="all">All</option><option value="public">Public</option><option value="private">Private</option></select></label>
        <label className="field"><span>Enrolled</span><select value={enr} onChange={(e) => setEnr(e.target.value)}><option value="all">All</option><option value="yes">Enrolled</option><option value="no">Not enrolled</option></select></label>
        <label className="check"><input type="checkbox" checked={arch} onChange={(e) => setArch(e.target.checked)} />Show archived</label>
      </div>
      {!list.length ? <Empty title="No repositories match these filters" /> : (
        <ul className="repo-list">
          {list.map((r) => <RepoRow key={r.id} d={r} repo={byName.get(r.name) ?? null} open={open === r.name} setOpen={(o) => setOpen(o ? r.name : null)} />)}
        </ul>
      )}
    </div>
  );
}

function RepoRow({ d, repo, open, setOpen }: { d: DiscoveredRepo; repo: RepoView | null; open: boolean; setOpen: (o: boolean) => void }) {
  const a = useAction();
  const enrolled = repo?.enrolled ?? d.enrolled;
  const [branch, setBranch] = useState(repo?.branch ?? "");
  const [paths, setPaths] = useState((repo?.readme_paths ?? ["README.md"]).join(", "));
  const [sections, setSections] = useState<string[]>(repo?.managed_sections ?? SECTIONS.slice(0, 8).map((s) => s.id));
  const [exp, setExp] = useState(repo?.api_export === "allowlist");
  const [include, setInclude] = useState((repo?.source_include ?? []).join(", "));
  // Export that is already on was confirmed when it was turned on; only turning it on needs a new confirmation.
  const [confirmExp, setConfirmExp] = useState(repo?.api_export === "allowlist");
  const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
  const save = () => a.run(() => api.putRepo(d.name, { branch: branch.trim(), readme_paths: list(paths), managed_sections: sections, api_export: exp ? "allowlist" : "none", source_include: list(include), ...(exp ? { confirm_export: confirmExp } : {}) }));
  return (
    <li className={`repo-item${open ? " open" : ""}`}>
      <div className="repo-line">
        <span className="grow"><span className="mono">{d.name}</span> <span className="small muted">{d.visibility}{d.archived ? ", archived" : ""}{d.fork ? ", fork" : ""}</span></span>
        {enrolled ? <Chip tone="ok" icon="check">Enrolled</Chip> : <span className="small muted">{d.detail}</span>}
        {enrolled && <button className="btn ghost" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Close settings" : "Settings"}</button>}
        <button className="btn" disabled={a.busy || d.archived} onClick={() => a.run(() => (enrolled ? api.deleteRepo(d.name) : api.putRepo(d.name, {})))}>{enrolled ? "Unenroll" : "Enroll"}</button>
      </div>
      {a.err && <Notice tone="fail">{a.err}</Notice>}
      {open && enrolled && (
        <div className="repo-cfg stack">
          <div className="field-row">
            <label className="field"><span>Branch</span><input value={branch} onChange={(e) => setBranch(e.target.value)} placeholder={`Default branch (${d.default_branch})`} spellCheck={false} /></label>
            <label className="field grow"><span>README paths, comma separated</span><input value={paths} onChange={(e) => setPaths(e.target.value)} spellCheck={false} /></label>
          </div>
          <fieldset className="sections"><legend>Managed sections</legend>
            <p className="small muted full">Only text inside managed blocks is ever changed. Everything outside them is never touched.</p>
            {SECTIONS.map((s) => (
              <label key={s.id} className="check"><input type="checkbox" checked={sections.includes(s.id)} onChange={(e) => setSections(e.target.checked ? [...sections, s.id] : sections.filter((x) => x !== s.id))} />
                <span><strong>{s.label}</strong> <span className="small muted">{s.why}</span></span></label>
            ))}
          </fieldset>
          <fieldset className="export"><legend>Source export</legend>
            <label className="check"><input type="checkbox" checked={exp} onChange={(e) => { setExp(e.target.checked); setConfirmExp(false); }} />Send selected source files to Anthropic</label>
            {exp && (
              <div className="stack inset">
                <p className="small">Files matching these patterns are sent to Anthropic so Claude can write the Claude-written sections. Files and values that look like secrets are excluded. Without this, only the README and repository metadata are used and no paid request is needed for this repository.</p>
                <label className="field"><span>Include patterns, comma separated</span><input value={include} onChange={(e) => setInclude(e.target.value)} placeholder="src/**/*.py, pyproject.toml" spellCheck={false} /></label>
                <label className="check"><input type="checkbox" checked={confirmExp} onChange={(e) => setConfirmExp(e.target.checked)} />I understand that matching files from {d.name} will be sent to Anthropic.</label>
              </div>
            )}
          </fieldset>
          {!sections.length && <p className="err small" role="alert">Tick at least one managed section.</p>}
          <div className="row-actions"><button className="btn primary" disabled={a.busy || (exp && !confirmExp) || !sections.length} onClick={save}>Save repository settings</button></div>
        </div>
      )}
    </li>
  );
}

function ModelStep({ state }: { state: State }) {
  const a = useAction();
  const b = state.budget;
  const [model, setModel] = useState(state.model.id);
  const [effort, setEffort] = useState(state.model.effort);
  const [lim, setLim] = useState({ run: b.per_run_usd?.toString() ?? "", day: b.daily_usd?.toString() ?? "", month: b.monthly_usd?.toString() ?? "" });
  const [pacing, setPacing] = useState(b.pacing);
  const num = (s: string) => (s.trim() === "" ? NaN : Number(s));
  const bad = (s: string) => !(num(s) > 0);
  const anyBad = bad(lim.run) || bad(lim.day) || bad(lim.month) || !model.trim();
  const ready = state.credentials.anthropic.present && b.configured;
  return (
    <div className="stack">
      <Facts rows={[["Provider", "Anthropic"], ["Paid generation", ready ? <Chip tone="ok" icon="check">Can run within your limits</Chip> : <Chip tone="neutral" icon="minus">Off</Chip>]]} />
      <p className="small">Paid generation stays off until an Anthropic key and all three limits are set. Repositories without source export never need it.</p>
      <SecretField label="Anthropic API key" saveLabel="Store key" present={state.credentials.anthropic.present} onRemove={() => a.run(() => api.deleteCredential("anthropic"))}
        help={`Stored in ${state.installation.credential_store} and never shown again. Only this installation's own calls are counted against your limits.`} onSave={async (v) => !!(await a.run(() => api.setCredential("anthropic", v)))} />
      <form className="stack" onSubmit={(e) => { e.preventDefault(); if (!anyBad) a.run(() => api.putSettings({ model: model.trim(), effort, budget: { per_run_usd: num(lim.run), daily_usd: num(lim.day), monthly_usd: num(lim.month), pacing } })); }} noValidate>
        <div className="field-row">
          <label className="field grow"><span>Model id</span><input value={model} onChange={(e) => setModel(e.target.value)} spellCheck={false} aria-invalid={!model.trim()} aria-describedby="model-err" />
            {!model.trim() && <span id="model-err" className="err small">Enter a model id.</span>}</label>
          <label className="field"><span>Effort</span><select value={effort} onChange={(e) => setEffort(e.target.value as typeof effort)}><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option></select></label>
        </div>
        {!state.model.priced && <Notice tone="attention">This model has no known price, so its cost cannot be checked against your limits. Choose a priced model.</Notice>}
        <fieldset className="field-row"><legend>Spending limits in US dollars</legend>
          {([["run", "Per run"], ["day", "Per day"], ["month", "Per month"]] as const).map(([k, l]) => (
            <label key={k} className="field"><span>{l}</span>
              <input type="number" min="0" step="0.01" inputMode="decimal" value={lim[k]} onChange={(e) => setLim({ ...lim, [k]: e.target.value })} aria-invalid={lim[k] !== "" && bad(lim[k])} aria-describedby={`lim-${k}-err`} />
              {lim[k] !== "" && bad(lim[k]) && <span id={`lim-${k}-err`} className="err small">Enter an amount above zero.</span>}
            </label>
          ))}
          <label className="field"><span>Pacing</span><select value={pacing} onChange={(e) => setPacing(e.target.value as typeof pacing)}><option value="even">Spread evenly over the day</option><option value="none">No pacing</option></select></label>
        </fieldset>
        <div className="row-actions"><button className="btn primary" disabled={a.busy || anyBad}>Save model and limits</button></div>
      </form>
      {a.err && <Notice tone="fail">{a.err}</Notice>}
    </div>
  );
}

const MODES: { id: PublishMode; label: string; help: string }[] = [
  { id: "preview", label: "Preview only", help: "Drafts stay on this computer. Nothing is written to GitHub." },
  { id: "review_pr", label: "Open a pull request for review", help: "Recommended first. You review and merge each change." },
  { id: "auto_pr", label: "Pull request that merges itself when checks pass", help: "Opens a pull request with auto-merge; your branch rules still apply." },
  { id: "direct", label: "Commit directly to the branch", help: "Writes to the branch without a pull request." },
];

function PublicationStep({ state }: { state: State }) {
  const enrolled = state.repos.filter((r) => r.enrolled);
  return (
    <div className="stack">
      {!enrolled.length ? <Empty title="No repositories enrolled">Enroll at least one repository in the previous step.</Empty> : enrolled.map((r) => <ModeRow key={r.name} repo={r} />)}
      <h3 className="h3">How often to check</h3>
      <ScheduleBar state={state} compact />
    </div>
  );
}

function ModeRow({ repo }: { repo: RepoView }) {
  const a = useAction();
  const [direct, setDirect] = useState(false);
  const branch = repo.branch ?? "the default branch";
  const set = (m: PublishMode) => (m === "direct" ? setDirect(true) : a.run(() => api.putRepo(repo.name, { publish_mode: m })));
  return (
    <fieldset className="mode-set">
      <legend className="mono">{repo.name}</legend>
      {MODES.map((m) => (
        <label key={m.id} className={`mode${repo.mode === m.id ? " on" : ""}`}>
          <input type="radio" name={`mode-${repo.name}`} checked={repo.mode === m.id} disabled={a.busy} onChange={() => set(m.id)} />
          <span><strong>{m.label}</strong><span className="small muted"> {m.help}</span></span>
        </label>
      ))}
      {direct && (
        <div className="confirm" role="group" aria-label="Confirm direct commits">
          <p>Commit directly to <strong>{branch}</strong> of <strong className="mono">{repo.name}</strong>? Branch protection still applies, and flagged content still goes to a pull request.</p>
          <button className="btn primary" disabled={a.busy} onClick={async () => { await a.run(() => api.putRepo(repo.name, { publish_mode: "direct", confirm_direct: true })); setDirect(false); }}>Commit directly</button>
          <button className="btn ghost" onClick={() => setDirect(false)}>Cancel</button>
        </div>
      )}
      {a.err && <Notice tone="fail">{a.err}</Notice>}
    </fieldset>
  );
}

function DryRunStep({ state, onFinish }: { state: State; onFinish: () => void }) {
  const { events, liveFrom } = useStore();
  const a = useAction();
  const [msg, setMsg] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("readme");
  const dry = [...events].reverse().find((e) => e.trigger === "dry_run");
  const runId = dry?.run_id ?? null;
  const evs = runId === null ? [] : eventsFor(events, runId, null);
  const repoName = evs.find((e) => e.repo)?.repo ?? null;
  const live = state.runtime.current_run?.id === runId;
  const view = runId === null ? null : foldRun(eventsFor(evs, runId, repoName), null, live);
  const repo = state.repos.find((r) => r.name === repoName) ?? null;
  const manual = state.schedule.configured.mode === "manual";
  return (
    <div className="stack">
      <p>A dry run reads GitHub, writes nothing to GitHub and makes no model request. Review its drafts below before you start.</p>
      <div className="row-actions">
        <button className="btn" disabled={a.busy || state.runtime.state === "running"} onClick={async () => { const r = await a.run(() => api.startRun("dry_run")); setMsg(r && !r.started ? RUN_REFUSALS[r.reason ?? ""] ?? r.reason ?? null : null); }}><Icon name="play" />Run a dry run</button>
      </div>
      {msg && <Notice tone="attention">{msg}</Notice>}
      {a.err && <Notice tone="fail">{a.err}</Notice>}
      {view && <div className="dry-grid"><Workflow repo={repo} view={view} run={null} history={false} liveFrom={liveFrom} onStep={setTab} onLeaveHistory={() => undefined} /><Detail view={view} repo={repo} tab={tab} setTab={setTab} /></div>}
      <h3 className="h3">Start</h3>
      {!state.installation.active ? <Notice tone="attention">Enable the worker on this computer (step 1) before starting.</Notice> : (
        <>
          <p className="small">{manual ? "Your schedule is manual: checks happen only when you choose Run now." : `Start applies your schedule (${state.schedule.summary.toLowerCase()}) to ${state.installation.scheduler}.`}</p>
          {!manual && state.installation.scheduling_unavailable && <Notice tone="attention">Scheduled checks are not available in this copy: {state.installation.scheduling_unavailable}.</Notice>}
          <button className="btn primary" disabled={a.busy || !state.onboarding.steps.find((s) => s.id === "dry_run")?.done} onClick={async () => { const r = await a.run(() => api.putSchedule(state.schedule.configured)); if (r) onFinish(); }}>Start monitoring</button>
          {!state.onboarding.steps.find((s) => s.id === "dry_run")?.done && <p className="small muted">Start is available once the backend records a finished dry run.</p>}
        </>
      )}
    </div>
  );
}

/** Rarely needed settings, out of the way: diagnostics, account-wide discovery, generation policy, disabling the worker. */
function Advanced({ state }: { state: State }) {
  return (
    <details className="panel advanced">
      <summary className="panel-title">Advanced</summary>
      <div className="stack">
        <DiagnosticsPanel />
        <DiscoveryForm state={state} />
        <PolicyForm state={state} />
        <DisableWorker state={state} />
      </div>
    </details>
  );
}

function DiagnosticsPanel() {
  const [d, setD] = useState<Diagnostics | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => { setBusy(true); setErr(null); api.diagnostics().then(setD, (e) => setErr(errorText(e))).finally(() => setBusy(false)); };
  const TONE = { PASS: ["ok", "check"], WARN: ["attention", "alert"], FAIL: ["fail", "x"] } as const;
  return (
    <section className="stack" aria-labelledby="diag-h">
      <h3 id="diag-h" className="h3">Diagnostics</h3>
      <p className="small muted">Checks this installation's folders, configuration and database. Contains no secrets.</p>
      <div className="row-actions"><button className="btn" disabled={busy} onClick={load}>{d ? "Run the checks again" : "Run the checks"}</button></div>
      {err && <Notice tone="fail">{err}</Notice>}
      {busy && <Loading label="Running the checks" />}
      {d && (
        <>
          <ul className="rows">{d.checks.map((c, k) => <li key={k} className="row"><Chip tone={TONE[c.status]?.[0] ?? "neutral"} icon={TONE[c.status]?.[1] ?? "dot"}>{c.status}</Chip><strong>{c.name}</strong><span className="grow small muted">{c.detail}</span></li>)}</ul>
          <p className="small muted">Checked {clock(d.generated_at)}.</p>
        </>
      )}
    </section>
  );
}

const DISCOVERY_MODES = MODES.filter((m) => m.id !== "direct");

function DiscoveryForm({ state }: { state: State }) {
  const a = useAction();
  const d = state.discovery;
  const [owner, setOwner] = useState(d.owner);
  const [mode, setMode] = useState<PublishMode | "">(d.publish_mode ?? "");
  const [perDay, setPerDay] = useState(d.adoptions_per_day?.toString() ?? "");
  const initialExclude = (d.exclude ?? []).join(", ");
  const [exclude, setExclude] = useState(initialExclude);
  const list = exclude.split(",").map((x) => x.trim()).filter(Boolean);
  const perDayBad = perDay !== "" && !(Number.isInteger(Number(perDay)) && Number(perDay) >= 0 && Number(perDay) <= 50);
  const excludeBad = list.some((x) => !/^[^/\s]+\/[^/\s]+$/.test(x));
  const [saved, setSaved] = useState(false);
  async function save(e: FormEvent) {
    e.preventDefault(); if (perDayBad || excludeBad) return;
    // Only fields the person set are sent, so unseen values in config.toml are never overwritten.
    const body: Record<string, unknown> = { owner: owner.trim() };
    if (mode) body.publish_mode = mode;
    if (perDay !== "") body.adoptions_per_day = Number(perDay);
    if (exclude !== initialExclude) body.exclude = list;
    setSaved(!!(await a.run(() => api.putSettings({ discovery: body }))));
  }
  return (
    <form className="stack" onSubmit={save} noValidate aria-labelledby="disc-h">
      <h3 id="disc-h" className="h3">Account-wide discovery</h3>
      <p className="small muted">Monitors every repository the owner has, without a separate entry for each, using the defaults below. Status: {d.enabled ? `on for ${d.owner}` : "off"}. Leave the owner empty to turn it off.</p>
      <div className="field-row">
        <label className="field grow"><span>Owner (account name)</span><input value={owner} onChange={(e) => { setOwner(e.target.value); setSaved(false); }} spellCheck={false} /></label>
        <label className="field"><span>Publication mode</span>
          <select value={mode} onChange={(e) => { setMode(e.target.value as PublishMode | ""); setSaved(false); }}>
            {!d.publish_mode && <option value="">Keep the current setting</option>}
            {DISCOVERY_MODES.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
          </select>
        </label>
        <label className="field"><span>Adoptions per day</span>
          <input type="number" min={0} max={50} step={1} value={perDay} placeholder={d.adoptions_per_day === undefined ? "Keep current" : undefined} onChange={(e) => { setPerDay(e.target.value); setSaved(false); }} aria-invalid={perDayBad} aria-describedby="perday-help" />
        </label>
      </div>
      <p id="perday-help" className="small muted">Adoption adds managed-block markers to a README by pull request; this caps how many such pull requests open per day (0 to 50). Commit-directly mode is never used for discovered repositories.</p>
      {perDayBad && <p className="err small" role="alert">Enter a whole number from 0 to 50.</p>}
      <label className="field"><span>Never process (owner/name, comma separated)</span><input value={exclude} onChange={(e) => { setExclude(e.target.value); setSaved(false); }} spellCheck={false} aria-invalid={excludeBad} /></label>
      {excludeBad && <p className="err small" role="alert">Write each entry as owner/name.</p>}
      <div className="row-actions"><button className="btn primary" disabled={a.busy || perDayBad || excludeBad}>Save discovery settings</button>{saved && <span className="small"><Icon name="check" size={14} /> Saved</span>}</div>
      {a.err && <Notice tone="fail">{a.err}</Notice>}
    </form>
  );
}

function PolicyForm({ state }: { state: State }) {
  const a = useAction();
  const p = state.policy;
  const [v, setV] = useState({ quiet_minutes: String(p.quiet_minutes), min_generation_interval_hours: String(p.min_generation_interval_hours), max_postpone_hours: String(p.max_postpone_hours) });
  const [saved, setSaved] = useState(false);
  const bad = (s: string) => s.trim() === "" || !(Number(s) >= 0);
  const anyBad = Object.values(v).some(bad);
  const FIELDS = [
    ["quiet_minutes", "Quiet minutes", "Wait this long after the last commit, so a burst of pushes produces one update."],
    ["min_generation_interval_hours", "Minimum hours between paid updates", "A repository is not redrafted by the model more often than this."],
    ["max_postpone_hours", "Maximum postponement in hours", "After this long, a waiting update goes ahead even if commits keep arriving."],
  ] as const;
  return (
    <form className="stack" aria-labelledby="pol-h" noValidate onSubmit={async (e) => { e.preventDefault(); if (anyBad) return; setSaved(!!(await a.run(() => api.putSettings({ policy: Object.fromEntries(Object.entries(v).map(([k, x]) => [k, Number(x)])) })))); }}>
      <h3 id="pol-h" className="h3">When paid updates happen</h3>
      {FIELDS.map(([k, label, help]) => (
        <label key={k} className="field"><span>{label}</span>
          <input type="number" min={0} step="any" value={v[k]} onChange={(e) => { setV({ ...v, [k]: e.target.value }); setSaved(false); }} aria-invalid={bad(v[k])} aria-describedby={`${k}-help`} />
          <span id={`${k}-help`} className="small muted">{help}</span>
          {bad(v[k]) && <span className="err small">Enter a number of zero or more.</span>}
        </label>
      ))}
      <div className="row-actions"><button className="btn primary" disabled={a.busy || anyBad}>Save policy</button>{saved && <span className="small"><Icon name="check" size={14} /> Saved</span>}</div>
      {a.err && <Notice tone="fail">{a.err}</Notice>}
    </form>
  );
}

function DisableWorker({ state }: { state: State }) {
  const a = useAction();
  const [confirming, setConfirming] = useState(false);
  if (!state.installation.active) return null;
  return (
    <section className="stack" aria-labelledby="dis-h">
      <h3 id="dis-h" className="h3">Disable on this computer</h3>
      <p className="small muted">Disabling removes the scheduled task and revokes the activation. Your data stays.</p>
      {!confirming ? <div className="row-actions"><button className="btn ghost danger" onClick={() => setConfirming(true)}>Disable on this computer</button></div> : (
        <div className="confirm" role="group" aria-label="Confirm disabling the worker">
          <p>Disable the worker on <strong>{state.installation.host}</strong>? Scheduled checks stop until you enable it again.</p>
          <button className="btn primary danger" disabled={a.busy} onClick={async () => { await a.run(() => api.deactivate()); setConfirming(false); }}>Disable now</button>
          <button className="btn ghost" onClick={() => setConfirming(false)}>Cancel</button>
        </div>
      )}
      {a.err && <Notice tone="fail">{a.err}</Notice>}
    </section>
  );
}