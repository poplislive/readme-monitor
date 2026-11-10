// Run history (opens past runs as history) and spending against the person's own limits.
import { useEffect, useMemo, useState } from "react";
import { api, errorText } from "../api/client";
import type { Run, State, Usage } from "../api/types";
import { clock, fillDays, usd } from "../lib/format";
import { Chip, Empty, Icon, Loading, Notice, VirtualList, type Tone } from "./ui";

const OUT: Record<string, { tone: Tone; icon: string; label: string }> = {
  ok: { tone: "ok", icon: "check", label: "Finished" }, error: { tone: "fail", icon: "x", label: "Error" }, crashed: { tone: "fail", icon: "x", label: "Crashed" },
  cancelled: { tone: "neutral", icon: "stop", label: "Cancelled" }, credentials_missing: { tone: "attention", icon: "key", label: "Credential missing" },
};
const TRIGGER: Record<string, string> = { scheduled: "Scheduled", manual: "Run now", dry_run: "Dry run", init: "Setup", preview: "Preview", "preview-api": "Preview", test: "Test" };

export function History({ lastRunId, onOpen, openId }: { lastRunId: number | null; onOpen: (r: Run) => void; openId: number | null }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState("all");
  useEffect(() => { api.runs(200).then((r) => { setRuns(r.runs); setErr(null); }, (e) => setErr(errorText(e))); }, [lastRunId]);
  const shown = useMemo(() => (runs ?? []).filter((r) => filter === "all" || (filter === "problems" ? !["ok", null].includes(r.outcome) : r.trigger === filter)), [runs, filter]);
  return (
    <section className="panel" aria-labelledby="hist-h">
      <div className="panel-head">
        <h2 id="hist-h" className="panel-title">Run history</h2>
        <label className="field inline"><span>Show</span>
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="all">All runs</option><option value="scheduled">Scheduled</option><option value="manual">Run now</option><option value="dry_run">Dry runs</option><option value="problems">Problems only</option>
          </select>
        </label>
      </div>
      {err ? <Notice tone="fail">{err}</Notice> : !runs ? <Loading label="Loading run history" /> : !shown.length ? <Empty title="No runs match">Change the filter or start a run.</Empty> : (
        <VirtualList label="Past runs" items={shown} rowHeight={48} height={288} getKey={(r) => r.id} render={(r) => {
          const o = r.outcome ? OUT[r.outcome] ?? { tone: "attention" as Tone, icon: "alert", label: r.outcome } : r.finished_at ? { tone: "neutral" as Tone, icon: "dot", label: "No outcome recorded" } : { tone: "active" as Tone, icon: "wave", label: "In progress" };
          return (
            <button className={`hist-row${openId === r.id ? " on" : ""}`} onClick={() => onOpen(r)} aria-pressed={openId === r.id}>
              <span className="grow">{r.finished_at ? `Past ${r.trigger === "dry_run" ? "dry run" : "run"}` : r.trigger === "dry_run" ? "Dry run" : "Run"} #{r.id}<span className="muted small"> {TRIGGER[r.trigger] ?? r.trigger}, {clock(r.started_at)}</span></span>
              <Chip tone={o.tone} icon={o.icon}>{o.label}</Chip>
            </button>
          );
        }} />
      )}
    </section>
  );
}

export function Budget({ state }: { state: State }) {
  const b = state.budget;
  const [usage, setUsage] = useState<Usage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api.usage().then(setUsage, (e) => setErr(errorText(e))); }, [b.spent_today_usd]);
  const days = usage ? fillDays(usage.by_day, state.now.slice(0, 10)) : [];
  const max = Math.max(0, ...days.map((d) => d.usd));
  return (
    <section className="panel" aria-labelledby="bud-h">
      <h2 id="bud-h" className="panel-title">Spending</h2>
      {!b.configured && <Notice tone="attention">Paid generation is off: set an Anthropic key and all three limits in Setup.</Notice>}
      <dl className="meters">
        <Meter label="Today" spent={b.spent_today_usd} limit={b.daily_usd} extra={b.pacing === "even" && b.pacing_limit_today_usd !== null ? `Pacing allows ${usd(b.pacing_limit_today_usd)} so far today` : undefined} />
        <Meter label="This month" spent={b.spent_month_usd} limit={b.monthly_usd} />
        <div className="meter"><dt>Per run</dt><dd>{usd(b.per_run_usd)} limit</dd></div>
        <div className="meter"><dt>Open reservations</dt><dd>{b.open_reservations} ({usd(b.open_reserved_usd)})</dd></div>
      </dl>
      {err ? <Notice tone="fail">{err}</Notice> : !usage ? <Loading label="Loading spending" /> : max === 0 ? <p className="small muted">No spending recorded in the last 30 days.</p> : (
        <figure className="spark">
          <svg viewBox={`0 0 ${days.length * 10} 60`} preserveAspectRatio="none" role="img" aria-label={`Daily spending for the last ${days.length} days, highest ${usd(max)}`}>
            {days.map((d, i) => <rect key={d.day} x={i * 10 + 1} width="8" y={60 - (d.usd / max) * 56} height={Math.max(d.usd > 0 ? 1 : 0, (d.usd / max) * 56)} rx="1"><title>{d.day}: {usd(d.usd)}</title></rect>)}
          </svg>
          <figcaption className="small muted">Last 30 days, highest day {usd(max)}. Only this installation's own model calls are counted; other use of the same key is not visible here.</figcaption>
        </figure>
      )}
    </section>
  );
}

function Meter({ label, spent, limit, extra }: { label: string; spent: number; limit: number | null; extra?: string }) {
  const pct = limit ? Math.min(1, spent / limit) : 0;
  const tone = pct >= 1 ? "fail" : pct >= 0.8 ? "attention" : "ok";
  return (
    <div className="meter">
      <dt>{label}</dt>
      <dd>{usd(spent)} of {usd(limit)}{pct >= 0.8 && <span className={`tone-${tone} small`}> <Icon name="alert" size={12} />{pct >= 1 ? " limit reached" : " near the limit"}</span>}</dd>
      {limit !== null && <div className="bar" aria-hidden="true"><span className={`tone-bg-${tone}`} style={{ transform: `scaleX(${pct})` }} /></div>}
      {extra && <dd className="small muted">{extra}</dd>}
    </div>
  );
}

