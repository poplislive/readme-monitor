// Plain-language schedule summary and planned checks for a schedule that is not saved yet.
// After saving, the page shows the backend's own summary and next_checks instead.
import type { Schedule, Weekday } from "../api/types";

export const WEEKDAYS: Weekday[] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
export const WEEKDAY_NAMES: Record<Weekday, string> = { mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday", fri: "Friday", sat: "Saturday", sun: "Sunday" };

export type Frequency = "manual" | "15" | "60" | "360" | "daily" | "weekly" | "custom";
export const FREQUENCIES: { id: Frequency; label: string }[] = [
  { id: "manual", label: "Manual" }, { id: "15", label: "Every 15 minutes" }, { id: "60", label: "Hourly" },
  { id: "360", label: "Every 6 hours" }, { id: "daily", label: "Daily" }, { id: "weekly", label: "Weekly" }, { id: "custom", label: "Custom" },
];

export function frequencyOf(s: Schedule): Frequency {
  if (s.mode !== "interval") return s.mode;
  return s.interval_minutes === 15 ? "15" : s.interval_minutes === 60 ? "60" : s.interval_minutes === 360 ? "360" : "custom";
}

/** The option shown as chosen: an explicit Custom choice sticks even while the minutes equal a preset. */
export const shownFrequency = (s: Schedule, customChosen: boolean): Frequency => (customChosen && s.mode === "interval" ? "custom" : frequencyOf(s));

export function withFrequency(s: Schedule, f: Frequency): Schedule {
  // Weekly is always this computer's time: Windows counts weekdays in local time.
  if (f === "weekly") return { ...s, mode: f, timezone: "local", weekdays: s.weekdays.length ? s.weekdays : ["mon"] };
  if (f === "manual" || f === "daily") return { ...s, mode: f };
  if (f === "custom") return { ...s, mode: "interval" };
  return { ...s, mode: "interval", interval_minutes: Number(f) };
}

export const validInterval = (n: number) => Number.isInteger(n) && n >= 5 && n <= 1440;
export const validTime = (t: string) => /^([01]\d|2[0-3]):[0-5]\d$/.test(t);

export function scheduleError(s: Schedule): string | null {
  if (s.mode === "interval" && !validInterval(s.interval_minutes)) return "Enter a whole number of minutes from 5 to 1440.";
  if ((s.mode === "daily" || s.mode === "weekly") && !validTime(s.time)) return "Enter a time as HH:MM, for example 09:00.";
  if (s.mode === "weekly" && !s.weekdays.length) return "Choose at least one weekday.";
  if (s.mode === "weekly" && s.timezone === "utc") return "Weekly schedules use this computer's time.";
  return null;
}

function every(n: number) {
  if (n % 60 === 0) { const h = n / 60; return h === 1 ? "Every hour" : `Every ${h} hours`; }
  return `Every ${n} minutes`;
}

export function describe(s: Schedule, zoneName: string): string {
  const tz = s.timezone === "utc" ? "UTC" : `this computer's time, ${zoneName}`;
  if (s.mode === "manual") return "Only when you choose Run now";
  if (s.mode === "interval") return every(s.interval_minutes);
  if (s.mode === "daily") return `Every day at ${s.time} (${tz})`;
  const days = WEEKDAYS.filter((d) => s.weekdays.includes(d));
  const names = days.length === 5 && !days.includes("sat") && !days.includes("sun") ? "Weekdays" : days.length === 7 ? "Every day" : days.map((d) => WEEKDAY_NAMES[d]).join(", ");
  return `${names} at ${s.time} (${tz})`;
}

/** Next `count` planned checks after `from`. Interval checks are counted from `from` (the task's own anchor is unknown until saved). */
export function plannedChecks(s: Schedule, from: Date, count = 3): Date[] {
  if (scheduleError(s) || s.mode === "manual") return [];
  if (s.mode === "interval") return Array.from({ length: count }, (_, i) => new Date(from.getTime() + (i + 1) * s.interval_minutes * 60_000));
  const [h, m] = s.time.split(":").map(Number);
  const utc = s.timezone === "utc";
  const out: Date[] = [];
  const d = new Date(from);
  for (let i = 0; i <= 7 * count && out.length < count; i++) {
    const c = new Date(d);
    if (utc) { c.setUTCDate(d.getUTCDate() + i); c.setUTCHours(h, m, 0, 0); } else { c.setDate(d.getDate() + i); c.setHours(h, m, 0, 0); }
    const wd = WEEKDAYS[((utc ? c.getUTCDay() : c.getDay()) + 6) % 7];
    if (c > from && (s.mode === "daily" || s.weekdays.includes(wd))) out.push(c);
  }
  return out;
}

export const sameSchedule = (a: Schedule | null, b: Schedule | null) =>
  !!a && !!b && a.mode === b.mode && a.timezone === b.timezone &&
  (a.mode !== "interval" || a.interval_minutes === b.interval_minutes) &&
  (a.mode === "manual" || a.mode === "interval" || a.time === b.time) &&
  (a.mode !== "weekly" || [...a.weekdays].sort().join() === [...b.weekdays].sort().join());
