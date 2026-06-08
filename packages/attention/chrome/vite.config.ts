import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { crx } from "@crxjs/vite-plugin";
// Named manifest.config.json (not manifest.json) on purpose: if this dir
// held a manifest.json, Chrome would happily "Load unpacked" the SOURCE
// directory — a silently-broken extension whose HTML points at raw .tsx.
// With no manifest.json here, picking the wrong folder fails loudly.
// The build emits the real loadable manifest at dist/manifest.json.
import manifest from "./manifest.config.json";

export default defineConfig({
  plugins: [
    react(),
    crx({ manifest }),
  ],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      // wizard.html is opened in a tab via chrome.tabs.create — it isn't
      // referenced by the manifest, so we tell rollup explicitly.
      input: {
        wizard: "wizard.html",
      },
    },
  },
});
