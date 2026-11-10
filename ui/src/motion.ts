// Motion tokens (docs/design.md, "Motion system"). CSS mirrors these as --dur-* / --ease-*.
import { useEffect, useState } from "react";
import type { Transition } from "motion/react";

export const EASE: [number, number, number, number] = [0.22, 1, 0.36, 1];
export const T = {
  control: { duration: 0.15, ease: EASE } as Transition,
  panel: { duration: 0.26, ease: EASE } as Transition,
  stage: { duration: 0.45, ease: EASE } as Transition,
  // Critically damped: settles in ~300 ms with no visible overshoot and no oscillation.
  layout: { type: "spring", stiffness: 520, damping: 46, mass: 1 } as Transition,
  none: { duration: 0 } as Transition,
};

export type MotionPref = "full" | "reduced" | "off";
const KEY = "rm.motion";

function initial(): MotionPref {
  try { const v = localStorage.getItem(KEY); if (v === "full" || v === "reduced" || v === "off") return v; } catch { /* storage blocked */ }
  return matchMedia("(prefers-reduced-motion: reduce)").matches ? "reduced" : "full";
}

/** Non-secret display preference, stored locally. Reflected on <html data-motion> for CSS. */
export function useMotionPref(): [MotionPref, (m: MotionPref) => void] {
  const [pref, setPref] = useState<MotionPref>(initial);
  useEffect(() => { document.documentElement.dataset.motion = pref; }, [pref]);
  return [pref, (m) => { setPref(m); try { localStorage.setItem(KEY, m); } catch { /* storage blocked */ } }];
}

export type Theme = "dark" | "light" | "system";
export function useTheme(): [Theme, (t: Theme) => void] {
  const [t, setT] = useState<Theme>(() => { try { return (localStorage.getItem("rm.theme") as Theme) || "system"; } catch { return "system"; } });
  useEffect(() => { if (t === "system") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t; }, [t]);
  return [t, (v) => { setT(v); try { localStorage.setItem("rm.theme", v); } catch { /* storage blocked */ } }];
}

/** Decorative loops pause while the tab is hidden. */
export function useDocumentHidden() {
  useEffect(() => {
    const f = () => { document.documentElement.dataset.hidden = String(document.hidden); };
    f(); document.addEventListener("visibilitychange", f);
    return () => document.removeEventListener("visibilitychange", f);
  }, []);
}
