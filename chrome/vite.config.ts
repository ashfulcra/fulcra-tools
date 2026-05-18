import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { crx } from "@crxjs/vite-plugin";
import manifest from "./manifest.json";

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
      // referenced by manifest.json, so we tell rollup explicitly.
      input: {
        wizard: "wizard.html",
      },
    },
  },
});
