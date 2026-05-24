"use strict";

const TOKEN = document.cookie
  .split("; ")
  .find(r => r.startsWith("fulcra_token="))
  ?.split("=")[1];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      "Content-Type": "application/json",
      ...(opts.headers ?? {}),
    },
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return res.json();
}

function app() {
  return {
    route: "loading",
    plugins: [],
    fulcraAuth: null,
    errorMessage: "",

    async boot() {
      try {
        const status = await api("/api/status");
        this.plugins = status.plugins ?? [];
        // Phase B5 doesn't have /api/fulcra/auth/status yet; default unauthenticated.
        this.fulcraAuth = { authenticated: false };
        const anyEnabled = this.plugins.some(p => p.enabled);
        this.route = this.fulcraAuth.authenticated && anyEnabled ? "dashboard" : "onboarding";
      } catch (e) {
        this.route = "error";
        this.errorMessage = e.message;
      }
    },
  };
}
