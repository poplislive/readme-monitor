import { Component, createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import Markdown from "react-markdown";
import { safeHref } from "../lib/format";

const P: Record<string, ReactNode> = {
  check: <path d="M3.5 8.5l3 3 6-7" />,
  x: <path d="M4 4l8 8M12 4l-8 8" />,
  alert: <><path d="M8 2.5l6 11H2z" /><path d="M8 6.5v3.5M8 12v.01" /></>,
  pause: <path d="M5.5 3.5v9M10.5 3.5v9" />,
  play: <path d="M5 3l8 5-8 5z" />,
  stop: <rect x="4" y="4" width="8" height="8" rx="1" />,
  clock: <><circle cx="8" cy="8" r="5.5" /><path d="M8 5v3.2l2 1.3" /></>,
  calendar: <><rect x="2.5" y="3.5" width="11" height="10" rx="1.5" /><path d="M2.5 6.5h11M5.5 2v3M10.5 2v3" /></>,
  commit: <><circle cx="8" cy="8" r="2.5" /><path d="M1.5 8h4M10.5 8h4" /></>,
  pr: <><circle cx="4.5" cy="3.5" r="1.5" /><circle cx="4.5" cy="12.5" r="1.5" /><circle cx="11.5" cy="12.5" r="1.5" /><path d="M4.5 5v6M11.5 11V6.5a2 2 0 0 0-2-2H7M8.5 3l-1.5 1.5L8.5 6" /></>,
  branch: <><circle cx="4.5" cy="3.5" r="1.5" /><circle cx="4.5" cy="12.5" r="1.5" /><circle cx="11.5" cy="5" r="1.5" /><path d="M4.5 5v6M11.5 6.5c0 3-7 2-7 4.5" /></>,
  file: <><path d="M4 1.5h5l3 3v10H4z" /><path d="M9 1.5v3h3" /></>,
  doc: <><path d="M3.5 2h9v12h-9z" /><path d="M5.5 5h5M5.5 7.5h5M5.5 10h3" /></>,
  search: <><circle cx="7" cy="7" r="4.5" /><path d="M10.5 10.5L14 14" /></>,
  menu: <path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" />,
  gear: <><circle cx="8" cy="8" r="2" /><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4" /></>,
  sun: <><circle cx="8" cy="8" r="3" /><path d="M8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M3 13l1-1M12 4l1-1" /></>,
  moon: <path d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5z" />,
  wave: <path d="M1.5 8c1.5-3 3-3 4.5 0s3 3 4.5 0 3-3 4 0" />,
  link: <><path d="M7 9a2.5 2.5 0 0 0 3.5 0l2.5-2.5a2.5 2.5 0 0 0-3.5-3.5L8.5 4" /><path d="M9 7a2.5 2.5 0 0 0-3.5 0L3 9.5A2.5 2.5 0 0 0 6.5 13l1-1" /></>,
  plug: <><path d="M6 2v3M10 2v3M4.5 5h7v3a3.5 3.5 0 0 1-7 0z" /><path d="M8 11.5V14" /></>,
  offline: <><path d="M2 2l12 12" /><path d="M5 11.5h6.5a2.5 2.5 0 0 0 .5-5A4 4 0 0 0 5.6 5" /></>,
  minus: <path d="M4 8h8" />,
  dot: <circle cx="8" cy="8" r="3" />,
  chevron: <path d="M6 4l4 4-4 4" />,
  key: <><circle cx="5.5" cy="10.5" r="3" /><path d="M7.7 8.3L13.5 2.5M11 5l2 2" /></>,
  computer: <><rect x="2" y="2.5" width="12" height="8.5" rx="1" /><path d="M5.5 14h5M8 11v3" /></>,
  history: <><path d="M2.5 8a5.5 5.5 0 1 0 1.6-3.9" /><path d="M2 2.5v2.5h2.5M8 5v3.2l2 1.3" /></>,
};

export type IconName = keyof typeof P;
export function Icon({ name, size = 16, label }: { name: string; size?: number; label?: string }) {
  return (
    <svg className="icon" width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"
      aria-hidden={label ? undefined : true} role={label ? "img" : undefined} aria-label={label}>
      {P[name]}
    </svg>
  );
}

export type Tone = "ok" | "active" | "attention" | "fail" | "neutral";
/** Status always carries an icon and words; color only reinforces them. */
export function Chip({ tone, icon, children, title }: { tone: Tone; icon: string; children: ReactNode; title?: string }) {
  return <span className={`chip tone-${tone}`} title={title}><Icon name={icon} size={14} />{children}</span>;
}

export function Notice({ tone = "neutral", icon = "alert", children }: { tone?: Tone; icon?: string; children: ReactNode }) {
  return <div className={`notice tone-${tone}`} role={tone === "fail" ? "alert" : undefined}><Icon name={icon} /><div>{children}</div></div>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><p className="empty-title">{title}</p>{children && <div className="empty-body">{children}</div>}</div>;
}

export function Loading({ label }: { label: string }) {
  return <div className="loading" role="status"><span className="busy-dot" aria-hidden="true" />{label}</div>;
}

/** Fixed-row virtual list: renders only the rows in view (plus overscan). */
export function VirtualList<T>({ items, rowHeight, height, render, label, getKey }: { items: T[]; rowHeight: number; height: number; render: (item: T, i: number) => ReactNode; label: string; getKey: (item: T) => string | number }) {
  const ref = useRef<HTMLDivElement>(null);
  const [top, setTop] = useState(0);
  const start = Math.max(0, Math.floor(top / rowHeight) - 6);
  const end = Math.min(items.length, Math.ceil((top + height) / rowHeight) + 6);
  return (
    <div ref={ref} className="vlist" style={{ maxHeight: height }} onScroll={(e) => setTop(e.currentTarget.scrollTop)} role="list" aria-label={label}>
      <div style={{ height: items.length * rowHeight, position: "relative" }}>
        {items.slice(start, end).map((it, i) => (
          <div role="listitem" key={getKey(it)} style={{ position: "absolute", top: (start + i) * rowHeight, left: 0, right: 0, height: rowHeight }}>{render(it, start + i)}</div>
        ))}
      </div>
    </div>
  );
}

/** Repository Markdown as text: no raw HTML, no image loads, only http(s) links in a new tab. */
export function SafeMarkdown({ text }: { text: string }) {
  const sim = useContext(Simulated);
  return (
    <div className="md">
      <Markdown skipHtml components={{
        img: ({ alt, src }) => <span className="md-img"><Icon name="file" size={14} />Image not loaded: {alt || "no description"} <code>{String(src ?? "")}</code></span>,
        a: ({ href, children }) => { const h = safeHref(href); return sim && h ? <SimAddress href={h}>{children}</SimAddress> : h ? <a href={h} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>; },
      }}>{text}</Markdown>
    </div>
  );
}

/** True in simulation: every GitHub address there is invented, so none is clickable. */
export const Simulated = createContext(false);
function SimAddress({ href, children }: { href: string; children: ReactNode }) {
  return <span className="sim-addr">{children} <code>{href}</code> <span className="muted">(simulated, not a real address)</span></span>;
}

export function ExtLink({ href, children }: { href: string | null | undefined; children: ReactNode }) {
  const sim = useContext(Simulated);
  const h = safeHref(href);
  if (!h) return <span className="muted">{children} (no link)</span>;
  if (sim) return <SimAddress href={h}>{children}</SimAddress>;
  return <a href={h} target="_blank" rel="noopener noreferrer" className="ext">{children}<Icon name="link" size={12} /><span className="sr-only"> (opens GitHub in a new tab)</span></a>;
}

/** Polite announcements, coalesced to at most one per second; the newest message wins. */
let pending: string | null = null;
let timer = 0;
let push: (s: string) => void = () => undefined;
export function announce(msg: string) {
  pending = msg;
  if (!timer) timer = window.setTimeout(function flush() { if (pending) { push(pending); pending = null; timer = window.setTimeout(flush, 1000); } else timer = 0; }, 150);
}
export function LiveRegion() {
  const [msg, setMsg] = useState("");
  useEffect(() => { push = setMsg; }, []);
  return <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{msg}</div>;
}

export function useEscape(open: boolean, close: () => void) {
  useEffect(() => {
    if (!open) return;
    const f = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", f);
    return () => window.removeEventListener("keydown", f);
  }, [open, close]);
}

/** A rendering error shows a plain, recoverable message instead of an empty page. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main id="main" className="center">
        <h1 className="quiet-h">Something on this screen failed to display</h1>
        <p className="muted">Nothing was changed. Reload the page to continue.</p>
        <div className="row-actions"><button className="btn primary" onClick={() => location.reload()}>Reload</button><button className="btn ghost" onClick={() => this.setState({ failed: false })}>Try again without reloading</button></div>
      </main>
    );
  }
}