import { motion, MotionConfig } from "motion/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, errorText, RUN_REFUSALS } from "./api/client";
import { boot, loadRun, refreshState, useStore, type Conn } from "./api/store";
import type { RepoView, Run, RuntimeState, State } from "./api/types";
import { eventsFor, foldRun, latestRunFor, outcomeTitle } from "./lib/pipeline";
import { needsAttention, repoStatus } from "./lib/status";
import { humanizeTimes, relative, withoutUrls } from "./lib/format";
import { T, useDocumentHidden, useMotionPref, useTheme, type MotionPref } from "./motion";
import { Budget, History } from "./components/Insights";
import { Onboarding } from "./components/Onboarding";
import { ScheduleBar } from "./components/ScheduleBar";
import { Detail, modeLabel, Workflow, type Tab } from "./components/Workflow";
import { announce, Chip, ErrorBoundary, Icon, LiveRegion, Loading, Notice, Simulated, useEscape, VirtualList, type Tone } from "./components/ui";

const RUNTIME: Record<RuntimeState, { tone: Tone; icon: string; label: string }> = {
  inactive: { tone: "neutral", icon: "plug", label: "Not enabled" },
  unavailable: { tone: "fail", icon: "alert", label: "Unavailable" },
  paused: { tone: "attention", icon: "pause", label: "Paused" },
  running: { tone: "active", icon: "wave", label: "Running" },
  scheduled_idle: { tone: "ok", icon: "check", label: "Scheduled, idle" },
  manual_idle: { tone: "neutral", icon: "clock", label: "Manual, idle" },
};
const CONN: Record<Conn, { tone: Tone; icon: string; label: string } | null> = {
  live: null, connecting: { tone: "neutral", icon: "dot", label: "Connecting" },
  reconnecting: { tone: "attention", icon: "offline", label: "Reconnecting to live updates" }, offline: { tone: "fail", icon: "offline", label: "Disconnected; retrying every 5 seconds" },
};

export function App() {
  const s = useStore();
  const [motionPref, setMotionPref] = useMotionPref();
  const [theme, setTheme] = useTheme();
  // null until the first state arrives; then Setup stays open until the person leaves it (it never closes itself).
  const [setup, setSetup] = useState<boolean | null>(null);
  const complete = s.state?.onboarding.complete;
  useEffect(() => { if (setup === null && complete !== undefined) setSetup(!complete); }, [setup, complete]);
  useDocumentHidden();
  useEffect(() => { boot(); }, []);
  const sim = s.session?.simulation || s.state?.simulation;
  return (
    <MotionConfig reducedMotion={motionPref === "full" ? "never" : "always"} transition={motionPref === "off" ? T.none : undefined}>
      <a className="skip" href="#main">Skip to content</a>
      {sim && <div className="sim" role="note"><Icon name="alert" size={14} /><strong>Simulation:</strong> sample repositories, nothing is sent to GitHub or Anthropic</div>}
      <LiveRegion />
      {s.phase === "loading" ? <Center><Loading label="Opening ReadmeMonitor" /></Center>
        : s.phase === "signed_out" ? <Center><Mark /><h1 className="quiet-h">Open ReadmeMonitor from its launcher</h1><p className="muted">This page needs the one-time link the launcher opens. Start it from the ReadmeMonitor shortcut, or run <code>readme-monitor ui</code> in a terminal.</p></Center>
        : s.phase === "error" || !s.state ? <Center><Mark /><h1 className="quiet-h">ReadmeMonitor did not answer</h1><Notice tone="fail">{s.error ?? s.stateError ?? "Unknown error."}</Notice><button className="btn" onClick={() => location.reload()}>Try again</button></Center>
        : <Simulated.Provider value={!!sim}><ErrorBoundary><Shell state={s.state} conn={s.conn} stateError={s.stateError} setup={setup ?? !s.state.onboarding.complete} openSetup={setSetup} motionPref={motionPref} setMotionPref={setMotionPref} theme={theme} setTheme={setTheme} /></ErrorBoundary></Simulated.Provider>}
    </MotionConfig>
  );
}

function Center({ children }: { children: React.ReactNode }) { return <main id="main" className="center">{children}</main>; }
function Mark() {
  return <svg className="mark" width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><rect width="28" height="28" rx="6" fill="#0A163C" /><circle cx="8" cy="9" r="2.4" fill="none" stroke="#9DB3FF" strokeWidth="1.6" /><path d="M8 11.5v4.5c0 2 1 3 3 3h3" fill="none" stroke="#9DB3FF" strokeWidth="1.6" /><path d="M15 15.5h6M15 19.5h6M15 11.5h4" stroke="#B794E8" strokeWidth="1.6" strokeLinecap="round" /></svg>;
}

function Shell({ state, conn, stateError, setup, openSetup, motionPref, setMotionPref, theme, setTheme }: {
  state: State; conn: Conn; stateError: string | null; setup: boolean; openSetup: (b: boolean) => void;
  motionPref: MotionPref; setMotionPref: (m: MotionPref) => void; theme: string; setTheme: (t: "dark" | "light" | "system") => void;
}) {
  const enrolled = state.repos.filter((r) => r.enrolled || r.source === "config");
  const [sel, setSel] = useState<string | null>(null);
  const repo = state.repos.find((r) => r.name === sel) ?? enrolled[0] ?? state.repos[0] ?? null;
  const [drawer, setDrawer] = useState(false);
  const [msg, setMsg] = useState<{ tone: Tone; text: string } | null>(null);
  const rt = state.runtime;
  const R = RUNTIME[rt.state];
  const c = CONN[conn];

  async function act(f: () => Promise<object>, okText: string) {
    setMsg(null);
    try {
      const r = (await f()) as { started?: boolean; reason?: string; requested?: boolean };
      if (r.started === false) setMsg({ tone: "attention", text: RUN_REFUSALS[r.reason ?? ""] ?? r.reason ?? "Not started." });
      else if (r.requested === false) setMsg({ tone: "neutral", text: "Nothing is running." });
      else { announce(okText); await refreshState(); }
    } catch (e) { setMsg({ tone: "fail", text: errorText(e) }); }
  }
  const runNow = () => act(() => api.startRun("live"), "Run started.");
  const dryRun = (name: string) => act(() => api.startRun("dry_run", [name]), `Dry run started for ${name}.`);
  const pauseToggle = () => act(() => (rt.state === "paused" ? api.resume() : api.pause()), rt.state === "paused" ? "Resumed." : "Paused.");

  return (
    <>
      <header className="topbar">
        <div className="tb-left">
          {!setup && <button className="btn icon only-narrow" aria-label="Repositories" aria-expanded={drawer} onClick={() => setDrawer(true)}><Icon name="menu" /></button>}
          <Mark /><h1 className="product">ReadmeMonitor</h1>
          {!setup && enrolled.length > 0 && (
            <label className="field inline hide-narrow"><span className="sr-only">Active repository</span>
              <select value={repo?.name ?? ""} onChange={(e) => setSel(e.target.value)}>{enrolled.map((r) => <option key={r.name}>{r.name}</option>)}</select>
            </label>
          )}
        </div>
        <div className="tb-mid">
          <span className="small muted hide-narrow" title={state.installation.home}><Icon name="computer" size={14} /> {state.installation.host}</span>
          <Chip tone={R.tone} icon={R.icon} title={rt.detail}>{R.label}</Chip>
          {rt.next_check && rt.state !== "running" && <span className="small muted hide-narrow"><Icon name="clock" size={14} /> Next check {relative(rt.next_check)}</span>}
          {c && <Chip tone={c.tone} icon={c.icon}>{c.label}</Chip>}
        </div>
        <div className="tb-right">
          {!setup && <button className="btn primary" onClick={runNow} disabled={rt.state === "running" || !state.installation.active}><Icon name="play" />Run now</button>}
          {!setup && repo && <button className="btn" onClick={() => dryRun(repo.name)} disabled={rt.state === "running"} aria-label={`Dry run ${repo.name}: reads GitHub, writes nothing, no model request`} title="Reads GitHub, writes nothing to GitHub and makes no model request"><Icon name="play" />Dry run</button>}
          {!setup && rt.state === "running" && <button className="btn" onClick={() => act(() => api.cancel(), "Cancel requested. The current step finishes safely first.")}><Icon name="stop" />Cancel</button>}
          {!setup && state.installation.active && rt.state !== "running" && <button className="btn" onClick={pauseToggle}><Icon name={rt.state === "paused" ? "play" : "pause"} />{rt.state === "paused" ? "Resume" : "Pause"}</button>}
          <label className="field inline"><span className="sr-only">Motion</span>
            <select value={motionPref} onChange={(e) => setMotionPref(e.target.value as MotionPref)} aria-label="Motion"><option value="full">Motion: full</option><option value="reduced">Motion: reduced</option><option value="off">Motion: off</option></select>
          </label>
          <button className="btn icon" aria-label={`Theme: ${theme}. Switch theme`} onClick={() => setTheme(theme === "dark" ? "light" : theme === "light" ? "system" : "dark")}><Icon name={theme === "light" ? "sun" : "moon"} /></button>
          <button className="btn" onClick={() => openSetup(!setup)} aria-pressed={setup}><Icon name="gear" />{setup ? "Workbench" : "Setup"}</button>
        </div>
      </header>
      {(msg || stateError) && <div className="banner">{msg && <Notice tone={msg.tone}>{msg.text}</Notice>}{stateError && <Notice tone="attention">Showing the last known state: {stateError}</Notice>}</div>}
      {setup ? <main id="main" className="main-setup"><Onboarding state={state} onFinish={() => openSetup(false)} /></main>
        : <Workbench state={state} repo={repo} repos={state.repos} onSelect={(n) => { setSel(n); setDrawer(false); }} drawer={drawer} closeDrawer={() => setDrawer(false)} runNow={runNow} pauseToggle={pauseToggle} act={act} />}
    </>
  );
}

type Act = (f: () => Promise<object>, okText: string) => Promise<void>;

function Workbench({ state, repo, repos, onSelect, drawer, closeDrawer, runNow, pauseToggle, act }: {
  state: State; repo: RepoView | null; repos: RepoView[]; onSelect: (n: string) => void; drawer: boolean; closeDrawer: () => void; runNow: () => void; pauseToggle: () => void; act: Act;
}) {
  const { events, liveFrom } = useStore();
  const [tab, setTab] = useState<Tab>("commits");
  const [hist, setHist] = useState<Run | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const cur = state.runtime.current_run;
  const name = repo?.name ?? null;
  const runId = hist?.id ?? (cur && events.some((e) => e.run_id === cur.id) ? cur.id : (name ? latestRunFor(events, name) : null) ?? events.at(-1)?.run_id ?? null);
  const live = !hist && cur?.id === runId;
  const view = useMemo(() => (runId === null ? null : foldRun(eventsFor(events, runId, name), hist ?? (live ? cur : state.runtime.last_run?.id === runId ? state.runtime.last_run : null), live, name)), [events, runId, name, hist, live, cur, state.runtime.last_run]);

  // Announce stage changes and outcomes of live runs only; history stays silent.
  const lastSaid = useRef("");
  useEffect(() => {
    if (!view?.live && !(view && view.lastEventId >= liveFrom)) return;
    const active = view.steps.find((s) => s.status === "active");
    const text = view.outcome && view.outcome.kind !== "running" ? `${name ?? "Run"}: ${outcomeTitle(view)}.` : active ? `${name ?? "Run"}: ${active.label}.` : "";
    if (text && text !== lastSaid.current) { lastSaid.current = text; announce(text); }
  }, [view, liveFrom, name]);

  const openHist = useCallback(async (r: Run) => { setHist(r); try { await loadRun(r.id); } catch { /* the workflow shows what is loaded */ } }, []);
  return (
    <div className={`workbench${collapsed ? " rail-collapsed" : ""}`}>
      <Rail repos={repos} selected={repo?.name ?? null} onSelect={(n) => { onSelect(n); setHist(null); }} open={drawer} close={closeDrawer} collapsed={collapsed} setCollapsed={setCollapsed} state={state} />
      <main id="main" className="center-col">
        <motion.div key={repo?.name ?? "none"} className="wf-grid" initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }} transition={T.panel}>
          <Workflow repo={repo} actions={repo && <RepoBar repo={repo} state={state} act={act} />} view={view} run={hist ?? (live ? cur : state.runtime.last_run?.id === runId ? state.runtime.last_run : null)} history={!!hist} liveFrom={hist ? Infinity : liveFrom} onStep={setTab} onLeaveHistory={() => setHist(null)} />
          <Detail view={view} repo={repo} tab={tab} setTab={setTab} />
        </motion.div>
        <ScheduleBar state={state} onRunNow={runNow} onPauseToggle={state.installation.active ? pauseToggle : undefined} />
        <div className="below">
          <History lastRunId={state.runtime.last_run?.id ?? null} onOpen={openHist} openId={hist?.id ?? null} />
          <Budget state={state} />
        </div>
      </main>
    </div>
  );
}

/** Status, reason and the per-repository controls: pause or resume, and adoption when the README has no managed blocks yet. */
function RepoBar({ repo, state, act }: { repo: RepoView; state: State; act: Act }) {
  const st = repoStatus(repo);
  const [adopting, setAdopting] = useState(false);
  const canAdopt = repo.state === "needs_init" || repo.state === "adoption_deferred";
  return (
    <div className="stack repo-bar">
      <p className="small"><Chip tone={st.tone} icon={st.icon}>{st.label}</Chip>{withoutUrls(st.reason, repo.proposal?.pr_url) && <span className="muted"> {humanizeTimes(withoutUrls(st.reason, repo.proposal?.pr_url), new Date(state.now))}</span>}</p>
      <div className="row-actions">
        <button className="btn ghost" onClick={() => act(() => (repo.paused ? api.resume(repo.name) : api.pause(repo.name)), repo.paused ? `Resumed ${repo.name}.` : `Paused ${repo.name}.`)}>
          <Icon name={repo.paused ? "play" : "pause"} />{repo.paused ? "Resume this repository" : "Pause this repository"}
        </button>
        {canAdopt && !adopting && <button className="btn" onClick={() => setAdopting(true)}><Icon name="pr" />Adopt this README</button>}
      </div>
      {canAdopt && adopting && (
        <div className="confirm" role="group" aria-label="Confirm adoption">
          <p>Adoption adds the managed-block markers (and any missing managed sections) to the README of <strong className="mono">{repo.name}</strong>, once. It is always proposed as a pull request for you to review, whatever the publication mode; text outside the markers is not changed.{!state.installation.active && " The worker must be enabled on this computer first."}</p>
          <button className="btn primary" disabled={!state.installation.active || state.runtime.state === "running"} onClick={async () => { await act(() => api.adopt(repo.name), `Adoption proposal started for ${repo.name}.`); setAdopting(false); }}>Propose adoption pull request</button>
          <button className="btn ghost" onClick={() => setAdopting(false)}>Cancel</button>
        </div>
      )}
    </div>
  );
}
function Rail({ repos, selected, onSelect, open, close, collapsed, setCollapsed, state }: {
  repos: RepoView[]; selected: string | null; onSelect: (n: string) => void; open: boolean; close: () => void; collapsed: boolean; setCollapsed: (b: boolean) => void; state: State;
}) {
  const [q, setQ] = useState("");
  const [f, setF] = useState("all");
  const ref = useRef<HTMLElement>(null);
  const trigger = useRef<Element | null>(null);
  useEscape(open, close);
  useEffect(() => {
    if (open) { trigger.current = document.activeElement; ref.current?.querySelector<HTMLElement>("input")?.focus(); }
    else if (trigger.current instanceof HTMLElement) { trigger.current.focus(); trigger.current = null; }
  }, [open]);
  const list = repos.filter((r) => r.name.toLowerCase().includes(q.toLowerCase()) && (f === "all" || (f === "attention" ? needsAttention(r) : f === "paused" ? r.paused : true)));
  return (
    <>
      {open && <div className="scrim" onClick={close} aria-hidden="true" />}
      <nav ref={ref} className={`rail${open ? " open" : ""}`} aria-label="Repositories">
        <div className="rail-head">
          <h2 className="panel-title">{collapsed ? <span className="sr-only">Repositories</span> : `Repositories (${repos.length})`}</h2>
          <button className="btn icon hide-narrow" aria-label={collapsed ? "Expand repository list" : "Collapse repository list"} aria-expanded={!collapsed} onClick={() => setCollapsed(!collapsed)}><Icon name="chevron" /></button>
          <button className="btn icon only-narrow" aria-label="Close repository list" onClick={close}><Icon name="x" /></button>
        </div>
        {!collapsed && (
          <>
            <div className="rail-filters">
              <label className="field"><span className="sr-only">Search repositories</span><input type="search" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} /></label>
              <label className="field"><span className="sr-only">Filter</span><select value={f} onChange={(e) => setF(e.target.value)} aria-label="Filter repositories"><option value="all">All</option><option value="attention">Needs attention</option><option value="paused">Paused</option></select></label>
            </div>
            {!list.length ? <p className="small muted pad">No repositories match.</p> : (
              <VirtualList label="Repositories" items={list} rowHeight={60} height={Math.max(240, typeof window === "undefined" ? 600 : window.innerHeight - 220)} getKey={(r) => r.name} render={(r) => {
                const t = repoStatus(r);
                const on = r.name === selected;
                return (
                  <button className={`rail-row${on ? " on" : ""}`} aria-current={on ? "true" : undefined} onClick={() => onSelect(r.name)}>
                    {on && <motion.span layoutId="rail-sel" className="rail-sel" transition={T.layout} />}
                    <span className="rail-name mono">{r.name.split("/")[1]}</span>
                    <span className="rail-meta small"><span className={`tone-${t.tone} rail-st`} title={t.reason ?? undefined}><Icon name={t.icon} size={12} />{t.label}</span><span className="muted">{modeLabel(r.mode)}, {relative(r.last_checked, new Date(state.now))}</span></span>
                  </button>
                );
              }} />
            )}
          </>
        )}
      </nav>
    </>
  );
}

