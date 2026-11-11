// Typed client for the local API (docs/api.md). Same origin only; secrets travel only in request bodies.
import type { CredentialAnswer, Diagnostics, Discovery, Draft, RepoName, RepoUpdate, RmEvent, Run, Schedule, ScheduleState, Session, State, Usage } from "./types";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

let csrf = "";

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") { headers["X-RM-CSRF"] = csrf; headers["Content-Type"] = "application/json"; }
  let res: Response;
  try {
    res = await fetch(path, { method, headers, credentials: "same-origin", cache: "no-store", body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "network", "ReadmeMonitor is not answering. Check that the window that started it is still open.");
  }
  const text = await res.text();
  let json: unknown = null;
  try { json = text ? JSON.parse(text) : null; } catch { /* non-JSON answer handled below */ }
  if (!res.ok) {
    const err = (json as { error?: { code?: string; message?: string } } | null)?.error;
    throw new ApiError(res.status, err?.code ?? `http_${res.status}`, err?.message ?? `The request failed (HTTP ${res.status}).`);
  }
  return json as T;
}

const repoPath = (name: RepoName) => `/api/repos/${name.split("/").map(encodeURIComponent).join("/")}`;
type Ok<T = object> = { ok: true } & T;

export const api = {
  async session(): Promise<Session> { const s = await call<Session>("GET", "/api/session"); csrf = s.csrf; return s; },
  state: () => call<State>("GET", "/api/state"),
  events: (q: { after?: number; limit?: number; run_id?: number; repo?: RepoName }) => {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(q)) if (v !== undefined) p.set(k, String(v));
    return call<{ events: RmEvent[]; cursor: number }>("GET", `/api/events?${p}`);
  },
  runs: (limit = 100) => call<{ runs: Run[] }>("GET", `/api/runs?limit=${limit}`),
  run: (id: number) => call<{ run: Run; events: RmEvent[] }>("GET", `/api/runs/${id}`),
  draft: (id: number) => call<Draft>("GET", `/api/drafts/${id}`),
  usage: () => call<Usage>("GET", "/api/usage"),

  activate: () => call<Ok>("POST", "/api/installation/activate", { confirm: true }),
  deactivate: () => call<Ok>("POST", "/api/installation/deactivate", { confirm: true }),
  setHome: (path: string) => call<Ok>("POST", "/api/installation/home", { path }),
  setCredential: (kind: "github" | "anthropic", value: string) =>
    call<Ok<CredentialAnswer>>("POST", `/api/credentials/${kind}`, { value }),
  diagnostics: () => call<Diagnostics>("GET", "/api/diagnostics"),
  adopt: (name: RepoName) => call<Ok<{ started: boolean; reason?: string }>>("POST", `${repoPath(name)}/adopt`, { confirm: true }),
  deleteCredential: (kind: "github" | "anthropic") => call<Ok>("DELETE", `/api/credentials/${kind}`),
  discover: () => call<Ok<Discovery>>("POST", "/api/github/discover"),
  putRepo: (name: RepoName, body: RepoUpdate) => call<Ok>("PUT", repoPath(name), body),
  deleteRepo: (name: RepoName) => call<Ok>("DELETE", repoPath(name)),
  putSettings: (body: Record<string, unknown>) => call<Ok>("PUT", "/api/settings", body),
  putSchedule: (s: Schedule) => call<Ok<{ applied: boolean; schedule?: ScheduleState; reason?: string }>>("PUT", "/api/schedule", s),
  pause: (repo?: RepoName) => call<Ok>("POST", "/api/pause", repo ? { repo } : {}),
  resume: (repo?: RepoName) => call<Ok>("POST", "/api/resume", repo ? { repo } : {}),
  startRun: (kind: "live" | "dry_run", repos?: RepoName[]) => call<Ok<{ started: boolean; reason?: string }>>("POST", "/api/run", repos ? { kind, repos } : { kind }),
  cancel: () => call<Ok<{ requested: boolean }>>("POST", "/api/cancel"),
};

export const RUN_REFUSALS: Record<string, string> = {
  already_running: "A check is already running.",
  inactive: "The worker is not enabled on this computer yet.",
  paused: "Monitoring is paused. Resume it first.",
  credentials_missing: "A required credential is missing. Add it in Setup.",
  updating: "An application update is being installed. Checks resume when it finishes.",
};

export const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));
