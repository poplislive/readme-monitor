import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The page is served by `readme-monitor ui` from "/" under a strict CSP (script-src 'self'),
// so the build emits only external module scripts, no source maps and no inline code.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../readme_monitor/web",
    emptyOutDir: true,
    sourcemap: false,
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    target: "es2022",
    // One local bundle served from 127.0.0.1; splitting would add requests without a network benefit.
    chunkSizeWarningLimit: 700,
  },
  server: { port: 5173, strictPort: false },
});
