// One app-wide store: session, state, and the activity log kept by event id (never duplicated).
import { useSyncExternalStore } from "react";
import { api, ApiError, errorText } from "./client";
import type { RmEvent, Session, State } from "./types";
import { mergeEvents } from "../lib/pipeline";

export type Conn = "connecting" | "live" | "reconnecting" | "offline";
export interface Snapshot {
  phase: "loading" | "signed_out" | "error" | "ready";
  error: string | null;
  session: Session | null;
  state: State | null;
  stateError: string | null;
  events: RmEvent[];
  /** Ids that arrived over the stream after the page loaded; only these may animate as live. */
  liveFrom: number;
  conn: Conn;
}

let snap: Snapshot = { phase: "loading", error: null, session: null, state: null, stateError: null, events: [], liveFrom: Infinity, conn: "connecting" };
const subs = new Set<() => void>();
const log = new Map<number, RmEvent>();
const set = (p: Partial<Snapshot>) => { snap = { ...snap, ...p }; subs.forEach((f) => f()); };
const subscribe = (f: () => void) => { subs.add(f); return () => subs.delete(f); };
export const useStore = () => useSyncExternalStore(subscribe, () => snap);

let es: EventSource | null = null;
let refreshTimer = 0;
let pollTimer = 0;
let lastEventId = 0;

export async function refreshState() {
  try { const state = await api.state(); set({ state, stateError: null }); }
  catch (e) {
    if (e instanceof ApiError && e.status === 401) return set({ phase: "signed_out" });
    set({ stateError: errorText(e) });
  }
}

function addEvents(list: RmEvent[]) {
  if (!mergeEvents(log, list)) return;
  for (const e of list) lastEventId = Math.max(lastEventId, e.id);
  set({ events: [...log.values()].sort((a, b) => a.id - b.id) });
}

// Bursts of run/result events (fast runs, reconnects) cause one state read, not one per event.
function scheduleRefresh() { clearTimeout(refreshTimer); refreshTimer = window.setTimeout(refreshState, 400); }

/** Ticks every 2 s; reads state every 2 s while a run is going (a run can end without a final event), else every 30 s live / 5 s offline. */
let lastPoll = 0;
const pollDue = (running: boolean, live: boolean) => (running ? 2_000 : live ? 30_000 : 5_000);
function poll() {
  clearTimeout(pollTimer);
  pollTimer = window.setTimeout(async () => {
    if (!document.hidden && Date.now() - lastPoll >= pollDue(snap.state?.runtime.state === "running", snap.conn === "live")) {
      lastPoll = Date.now();
      await refreshState();
      if (snap.conn !== "live") await catchUp();
    }
    poll();
  }, 2_000);
}

async function catchUp() {
  try { addEvents((await api.events({ after: lastEventId, limit: 500 })).events); } catch { /* shown by conn state */ }
}

function connect() {
  es?.close();
  // Start after the newest event already held, so nothing stored between the first read and the stream is lost.
  es = new EventSource(`/api/events/stream?after=${lastEventId}`);
  let wasDown = false;
  es.onopen = () => {
    set({ conn: "live" });
    if (wasDown) refreshState();
    catchUp();
    wasDown = false;
  };
  es.addEventListener("activity", (m) => {
    let e: RmEvent;
    try { e = JSON.parse((m as MessageEvent).data); } catch { return; }
    addEvents([e]);
    if (e.stage === "run" || e.stage === "result") scheduleRefresh();
  });
  es.onerror = () => {
    wasDown = true;
    // The browser retries on its own while readyState is CONNECTING; a CLOSED source needs a new one.
    if (es?.readyState === 2) { set({ conn: "offline" }); setTimeout(connect, 5_000); }
    else set({ conn: "reconnecting" });
  };
}

export async function boot() {
  try {
    const session = await api.session();
    set({ session });
    const [state, ev] = await Promise.all([api.state(), api.events({ limit: 500 })]);
    addEvents(ev.events);
    set({ state, phase: "ready", liveFrom: lastEventId + 1 });
    lastPoll = Date.now();
    connect();
    poll();
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) set({ phase: "signed_out" });
    else set({ phase: "error", error: errorText(e) });
  }
}

/** Run history: fetch a past run's events into the same log (they stay non-live). */
export async function loadRun(runId: number) { const r = await api.run(runId); addEvents(r.events); return r.run; }
