// How often checks run. Saved state is always the backend's read-back, never the local draft.
import { useEffect, useState } from "react";
import { api, errorText } from "../api/client";
import { refreshState } from "../api/store";
import type { Schedule, State } from "../api/types";
import { clock, relative } from "../lib/format";
import { FREQUENCIES, WEEKDAYS, WEEKDAY_NAMES, describe, plannedChecks, sameSchedule, scheduleError, shownFrequency, withFrequency } from "../lib/schedule";
import { Chip, Icon, Notice } from "./ui";

type Save = { s: "idle" } | { s: "saving" } | { s: "saved"; applied: boolean; reason?: string } | { s: "error"; msg: string };

export function ScheduleBar({ state, compact = false, onRunNow, onPauseToggle }: { state: State; compact?: boolean; onRunNow?: () => void; onPauseToggle?: () => void }) {
  const sch = state.schedule;
  const [draft, setDraft] = useState<Schedule>(sch.configured);
  const [save, setSave] = useState<Save>({ s: "idle" });
  const dirty = !sameSchedule(draft, sch.configured);
  // Follow the backend while the person has not changed anything.
  useEffect(() => { if (!dirty) setDraft(sch.configured); }, [sch.configured]); // eslint-disable-line react-hooks/exhaustive-deps
  const [customChosen, setCustomChosen] = useState(false);
  const freq = shownFrequency(draft, customChosen);
  const err = scheduleError(draft);
  const utc = draft.timezone === "utc";
  const planned = dirty ? plannedChecks(draft, new Date()) : sch.next_checks.map((x) => new Date(x));

  async function onSave() {
    if (err) return;
    setSave({ s: "saving" });
    try {
      const r = await api.putSchedule(draft);
      setSave({ s: "saved", applied: r.applied, reason: r.reason });
      await refreshState();
    } catch (e) { setSave({ s: "error", msg: errorText(e) }); }
  }

  const status = dirty ? (save.s === "saving" ? "Saving" : "Unsaved changes") : save.s === "saving" ? "Saving" : "Saved";
  return (
    <section className="panel schedule" aria-labelledby="sch-h">
      <div className="sch-top">
        <h2 id="sch-h" className="panel-title">Schedule</h2>
        <p className="small muted">This sets how often checks run. A paid update may still wait for the quiet period, the minimum interval or budget pacing.</p>
      </div>
      <div className="sch-grid">
        <fieldset className="seg" aria-describedby="sch-sum">
          <legend className="sr-only">How often to check</legend>
          {FREQUENCIES.map((f) => (
            <label key={f.id} className={`seg-opt${freq === f.id ? " on" : ""}`}>
              <input type="radio" name="freq" value={f.id} checked={freq === f.id} onChange={() => { setDraft(withFrequency(draft, f.id)); setCustomChosen(f.id === "custom"); setSave({ s: "idle" }); }} />{f.label}
            </label>
          ))}
        </fieldset>
        <div className="sch-fields">
          {freq === "custom" && (
            <div className="field-row">
              <label className="field"><span>Minutes between checks</span>
                <input type="number" min={5} max={1440} step={1} inputMode="numeric" value={Number.isNaN(draft.interval_minutes) ? "" : draft.interval_minutes} aria-invalid={!!err} aria-describedby="sch-err"
                  onChange={(e) => setDraft({ ...draft, interval_minutes: e.target.valueAsNumber })} />
              </label>
              <input type="range" min={5} max={1440} step={5} aria-label="Minutes between checks, slider" value={Number.isNaN(draft.interval_minutes) ? 5 : draft.interval_minutes} onChange={(e) => setDraft({ ...draft, interval_minutes: Number(e.target.value) })} />
            </div>
          )}
          {(draft.mode === "daily" || draft.mode === "weekly") && (
            <>
              <label className="field"><span>Time of day</span><input type="time" value={draft.time} aria-invalid={!!err} aria-describedby="sch-err" onChange={(e) => setDraft({ ...draft, time: e.target.value })} /></label>
              <label className="field"><span>Clock</span>
                <select value={draft.timezone} onChange={(e) => setDraft({ ...draft, timezone: e.target.value as Schedule["timezone"] })} aria-describedby={draft.mode === "weekly" ? "sch-tz-note" : undefined}>
                  <option value="local">This computer's time ({sch.timezone_name})</option>{draft.mode !== "weekly" && <option value="utc">UTC</option>}
                </select>
              </label>
              {draft.mode === "weekly" && <p id="sch-tz-note" className="small muted">Weekly checks use this computer's time, because {state.installation.scheduler} counts weekdays in local time.</p>}
            </>
          )}
          {draft.mode === "weekly" && (
            <fieldset className="days"><legend>Days</legend>
              {WEEKDAYS.map((d) => (
                <label key={d} className={`day${draft.weekdays.includes(d) ? " on" : ""}`}>
                  <input type="checkbox" checked={draft.weekdays.includes(d)} onChange={(e) => setDraft({ ...draft, weekdays: e.target.checked ? [...draft.weekdays, d] : draft.weekdays.filter((x) => x !== d) })} />
                  <span aria-hidden="true">{WEEKDAY_NAMES[d].slice(0, 2)}</span><span className="sr-only">{WEEKDAY_NAMES[d]}</span>
                </label>
              ))}
            </fieldset>
          )}
        </div>
        <div className="sch-summary">
          <p id="sch-sum"><strong>{describe(draft, sch.timezone_name)}</strong></p>
          {err && <p id="sch-err" className="err small" role="alert">{err}</p>}
          {planned.length > 0 && <p className="small muted">{dirty ? "Planned if saved: " : "Next checks: "}{planned.map((d) => clock(d.toISOString(), utc)).join("; ")}</p>}
          {!planned.length && !err && <p className="small muted">{draft.mode === "manual" ? "Checks happen only when you choose Run now." : "No planned checks reported yet."}</p>}
        </div>
        <div className="sch-actions">
          <span className={`save-state ${dirty ? "dirty" : ""}`} role="status"><Icon name={dirty ? "dot" : "check"} size={14} />{status}</span>
          <button className="btn primary" disabled={!dirty || !!err || save.s === "saving"} onClick={onSave}>Save schedule</button>
          {dirty && <button className="btn ghost" onClick={() => { setDraft(sch.configured); setCustomChosen(false); setSave({ s: "idle" }); }}>Discard</button>}
          {!compact && onPauseToggle && <button className="btn" onClick={onPauseToggle}><Icon name={state.runtime.state === "paused" ? "play" : "pause"} />{state.runtime.state === "paused" ? "Resume" : "Pause"}</button>}
          {!compact && onRunNow && <button className="btn" onClick={onRunNow} disabled={state.runtime.state === "running"}><Icon name="play" />Run now</button>}
        </div>
      </div>
      <div className="sch-readback small">
        {save.s === "error" && <Notice tone="fail">{save.msg} Your choices are kept; try saving again.</Notice>}
        {save.s === "saved" && !save.applied && <Notice tone="attention">Saved, but not applied to {state.installation.scheduler}: {save.reason ?? "the worker is not enabled on this computer."}</Notice>}
        {!dirty && sch.effective && (sch.in_sync
          ? <p><Chip tone="ok" icon="check">In effect</Chip> {state.installation.scheduler} confirms: {describe(sch.effective, sch.timezone_name).replace(/^E/, "e")}.{sch.task?.next_run && <> Next run {relative(sch.task.next_run)}.</>}</p>
          : <p><Chip tone="attention" icon="alert">Out of step</Chip> {state.installation.scheduler} reports {describe(sch.effective, sch.timezone_name).toLowerCase()}, not the saved schedule. Save again to re-apply it.</p>)}
        {!dirty && !sch.effective && state.installation.active && sch.configured.mode !== "manual" && <p><Chip tone="attention" icon="alert">No scheduled task</Chip> {state.installation.scheduler} has no scheduled check for this installation yet.</p>}
        {sch.notes.map((n) => <p key={n} className="muted">{n}</p>)}
      </div>
    </section>
  );
}
