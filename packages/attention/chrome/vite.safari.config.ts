import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { crx } from "@crxjs/vite-plugin";
// The SAFARI bundle. Separate from vite.config.ts on purpose.
//
// The Xcode extension target copies a built dist verbatim, so whatever this
// emits is what ships to App Review. Building Safari from the Chrome manifest
// would request `history`, `identity`, all-URLs and Fulcra/Auth0 host access on
// behalf of an extension that makes no network calls at all — the native app
// does auth and ingest. See the packaging note in
// docs/proposals/2026-06-04-relayless-and-mobile-safari-attention.md.
import manifest from "./manifest.safari.config.json";

export default defineConfig({
  plugins: [react(), crx({ manifest })],
  build: {
    // NOT dist/: the Chrome build owns that, and one clobbering the other would
    // ship the wrong permission set to whichever built last.
    outDir: "dist-safari",
    emptyOutDir: true,
    rollupOptions: { input: { safari: "safari.html" } },
  },
});
