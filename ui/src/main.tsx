import "@fontsource-variable/instrument-sans/wght.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./styles.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

async function start() {
  // Development only: sample data replaces /api/*. Vite removes this branch from the production build.
  if (import.meta.env.DEV) await import("./mock/install");
  createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
}
start();
