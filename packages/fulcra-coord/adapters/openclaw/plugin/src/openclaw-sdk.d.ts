// Ambient declaration for the OpenClaw Plugin-SDK subpath this plugin imports.
//
// WHY THIS FILE EXISTS: the `openclaw` package is a *peerDependency* — the host
// gateway provides it at load time and it is deliberately NOT installed into
// node_modules (see .npmrc `omit=peer`; a live OpenClaw install finding was
// `openclaw plugins install .` choking on a
// bundled node_modules/openclaw). Without the real package present, `tsc` cannot
// resolve `import ... from "openclaw/plugin-sdk/plugin-entry"`. This shim makes
// the import type-check against a loose-but-faithful surface so the plugin builds
// offline; at runtime the host supplies the genuine implementation.
//
// Keep the surface minimal: declare exactly the subpath src/index.ts imports and
// only the members it touches. `skipLibCheck` (tsconfig) means we don't need to
// model the whole SDK — just enough that index.ts type-checks.

declare module "openclaw/plugin-sdk/plugin-entry" {
  /** Logger surface the plugin uses (index.ts only calls `.info`). */
  export interface OpenClawPluginLogger {
    info(message: string): void;
    warn?(message: string): void;
    error?(message: string): void;
    debug?(message: string): void;
  }

  /**
   * The plugin API handed to `register(api)`. `on` is intentionally loose: the
   * real SDK overloads the event/ctx shapes per hook name, but index.ts already
   * narrows the fields it reads (and is fully fail-safe), so an `any`-typed
   * handler is a faithful, low-risk stand-in for the build-only shim.
   */
  export interface OpenClawPluginApi {
    on(hookName: string, handler: (event: any, ctx: any) => void | Promise<void>): void;
    logger: OpenClawPluginLogger;
  }

  /** Plugin entry descriptor passed to `definePluginEntry`. */
  export interface PluginEntryDefinition {
    id: string;
    name: string;
    description: string;
    register(api: OpenClawPluginApi): void;
  }

  /** Real SDK entry-point factory; returns the descriptor unchanged. */
  export function definePluginEntry(definition: PluginEntryDefinition): PluginEntryDefinition;
}
