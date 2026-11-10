import type { Coverage } from "../api/types";

/** Dollars; amounts under one cent keep four decimals so a $0.004 request never shows as $0.00. */
export const usd = (n: number | null | undefined) =>
  n === null || n === undefined ? "not set" : `$${n.toFixed(n > 0 && n < 0.01 ? 4 : 2)}`;
export const short = (sha: string | null | undefined) => (sha ? sha.slice(0, 7) : "unknown");

export function relative(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "never";
  const s = Math.round((new Date(iso).getTime() - now.getTime()) / 1000);
  const a = Math.abs(s);
  const [v, u] = a < 60 ? [a, "second"] : a < 3600 ? [Math.round(a / 60), "minute"] : a < 86400 ? [Math.round(a / 3600), "hour"] : [Math.round(a / 86400), "day"];
  if (a < 10) return "just now";
  const t = `${v} ${u}${v === 1 ? "" : "s"}`;
  return s < 0 ? `${t} ago` : `in ${t}`;
}

export function clock(iso: string | null | undefined, utc = false): string {
  if (!iso) return "none";
  return new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", month: "short", day: "numeric", timeZone: utc ? "UTC" : undefined });
}

const ISO_IN_TEXT = /\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?/g;
/** Backend detail text carries raw ISO timestamps; show them as local time with their age. */
export const humanizeTimes = (text: string, now: Date = new Date()) =>
  text.replace(ISO_IN_TEXT, (m) => (Number.isNaN(new Date(m).getTime()) ? m : `${clock(m)} (${relative(m, now)})`));

export const firstLine = (s: string) => s.split("\n", 1)[0];

/** Only http(s) links leave the page; everything else is shown as text. */
export function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  try { const u = new URL(url); return u.protocol === "https:" || u.protocol === "http:" ? u.href : null; } catch { return null; }
}

export function coverageText(c: Coverage): string {
  const base = `${c.listed} ${c.listed === 1 ? "repository" : "repositories"} owned by ${c.owner}: ${c.public} public, ${c.private} private.`;
  const skipped = c.skipped_not_owned ? ` ${c.skipped_not_owned} not owned by ${c.owner} ${c.skipped_not_owned === 1 ? "was" : "were"} left out.` : "";
  return base + skipped + (c.complete ? " Coverage complete." : ` Coverage incomplete: ${c.limitations.join(" ") || "the token could not list everything."}`);
}

/** Spending on a real 30-day axis ending at `endDay` (YYYY-MM-DD, UTC); days without spending are zero. */
export function fillDays(byDay: { day: string; usd: number }[], endDay: string, days = 30): { day: string; usd: number }[] {
  const have = new Map(byDay.map((d) => [d.day, d.usd]));
  const end = Date.parse(`${endDay}T00:00:00Z`);
  return Array.from({ length: days }, (_, i) => {
    const day = new Date(end - (days - 1 - i) * 86_400_000).toISOString().slice(0, 10);
    return { day, usd: have.get(day) ?? 0 };
  });
}

/** Detail text without addresses the page already shows through its (simulation-aware) link component. */
export function withoutUrls(text: string | null | undefined, ...urls: (string | null | undefined)[]): string {
  let out = text ?? "";
  for (const u of urls) if (u) out = out.split(u).join("");
  return out.replace(/\s+([;:,.])/g, "$1").replace(/\s{2,}/g, " ").replace(/^[\s;:,.-]+|[\s;:,-]+$/g, "").trim();
}
