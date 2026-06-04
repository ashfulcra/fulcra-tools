// chrome/src/relayless/ensureDefinition.ts
//
// Relayless port of the daemon's "ensure the Attention definition + tags
// exist" job. In relay mode the localhost daemon (fulcra_attention/fulcra.py
// FulcraClient.ensure_definitions) does this and hands the resolved
// {definition_id, tag_ids} to the wire transform. In relayless mode there is
// no daemon, so the extension must do it directly against the Fulcra Data API
// — and supply the same two things buildWireRecord(event, {definitionId,
// tagIds}) needs.
//
// IDEMPOTENT find-or-create, byte-compatible with the Python source of truth:
//
//   - definition shape:  packages/fulcra-common/fulcra_common/wire.py
//       duration_definition_payload(...) and
//     packages/attention/fulcra_attention/definition_spec.py
//       ATTENTION_CANONICAL (name "Attention", description, value_type
//       "duration", unit null) + ATTENTION_DEFINITION_TAG_NAMES
//       ("attention","web").
//   - tag find/create:   fulcra_common/client.py BaseFulcraClient._resolve_tag
//       GET /user/v1alpha1/tag/name/{name} (200 → {id}) else
//       POST /user/v1alpha1/tag {name} (→ {id}).
//   - definition adopt-by-name:  fulcra_attention/fulcra.py
//       _find_attention_definition — list GET /user/v1alpha1/annotation,
//       keep name=="Attention" && annotation_type=="duration" &&
//       !deleted_at, sort by created_at, take the oldest (so every machine
//       converges on one def). Else POST /user/v1alpha1/annotation with the
//       canonical create body.
//
// The resolved {definitionId, tagIds} is cached in extension local storage so
// it is resolved ONCE (a cloud round-trip), not on every flush.

import { API_BASE } from "./config";
import type { FetchFn } from "./oidc";
import {
  type StorageArea,
  defaultLocalStorageArea,
} from "./storageArea";

// --- Canonical Attention descriptor (mirrors definition_spec.py) ----------

/** name fulcra_attention/definition_spec.py ATTENTION_CANONICAL["name"]. */
export const ATTENTION_DEFINITION_NAME = "Attention";
/** ATTENTION_CANONICAL["description"]. */
export const ATTENTION_DEFINITION_DESCRIPTION =
  "What the user paid attention to (browsing).";
/** ATTENTION_DEFINITION_TAG_NAMES — the tags the def is created with, in
 * order. buildWireRecord's caller passes the resolved ids in this order
 * (attention, web) as the leading tagIds. */
export const ATTENTION_DEFINITION_TAG_NAMES = ["attention", "web"] as const;

/** The FULL create body for the Attention duration definition. Mirrors
 * wire.duration_definition_payload(name, description, tags, value_type,
 * unit) — annotation_type "duration", measurement_spec carrying
 * measurement_type "duration", value_type "duration", unit null. */
function attentionCreatePayload(tagIds: string[]): Record<string, unknown> {
  return {
    annotation_type: "duration",
    name: ATTENTION_DEFINITION_NAME,
    description: ATTENTION_DEFINITION_DESCRIPTION,
    tags: tagIds,
    measurement_spec: {
      measurement_type: "duration",
      value_type: "duration",
      unit: null,
    },
  };
}

// --- Resolution result + cache -------------------------------------------

export interface ResolvedAttention {
  /** The Attention annotation-definition id. */
  definitionId: string;
  /** Resolved tag ids for ATTENTION_DEFINITION_TAG_NAMES, in order:
   * [attention, web]. */
  tagIds: string[];
}

const RESOLVED_KEY = "relaylessResolvedAttention";

export interface EnsureOpts {
  /** Return a valid Bearer access token (null when not signed in). */
  getToken: (opts?: { force?: boolean }) => Promise<string | null>;
  fetch?: FetchFn;
  /** Injectable storage for the resolved-id cache. Defaults to the extension
   * local storage area. */
  storage?: StorageArea;
}

/** Thrown when the API rejected the token (401). The flush layer maps this to
 * the "needs sign-in" error state. */
export class UnauthorizedError extends Error {
  constructor(message = "unauthorized") {
    super(message);
    this.name = "UnauthorizedError";
  }
}

/** A minimal annotation-definition shape (only the fields we read). */
interface DefinitionRow {
  id?: string;
  name?: string;
  annotation_type?: string;
  deleted_at?: string | null;
  created_at?: string | null;
}

/**
 * Resolve {definitionId, tagIds} for the relayless Attention transport,
 * find-or-creating the definition + tags on the account. Idempotent: an
 * existing "Attention" def is adopted (never duplicated), existing tags are
 * found (never re-created). The result is cached so subsequent calls make no
 * network requests.
 *
 * Throws UnauthorizedError on a 401 (so the caller can surface needs-sign-in)
 * and a generic Error on other transport/API failures (so the caller keeps
 * the events queued rather than dropping them).
 */
export async function ensureAttentionDefinitionAndTags(
  opts: EnsureOpts,
): Promise<ResolvedAttention> {
  const storage = opts.storage ?? defaultLocalStorageArea();
  const fetchFn = opts.fetch ?? ((...a: Parameters<FetchFn>) => fetch(...a));

  // Cache hit — already resolved on this profile. No network.
  const cached = await readCache(storage);
  if (cached) return cached;

  const token = await opts.getToken();
  if (!token) throw new UnauthorizedError("not signed in");

  const ctx: ApiCtx = { fetchFn, token };

  // Resolve the def's tag ids first (so a fresh create can attach them, same
  // as the daemon's resolver-create path which passes the resolved ids as
  // create_extra).
  const tagIds: string[] = [];
  for (const name of ATTENTION_DEFINITION_TAG_NAMES) {
    tagIds.push(await resolveTag(ctx, name));
  }

  // Adopt-by-name first; create only if absent.
  let definitionId = await findAttentionDefinition(ctx);
  if (definitionId == null) {
    definitionId = await createAttentionDefinition(ctx, tagIds);
  }

  const resolved: ResolvedAttention = { definitionId, tagIds };
  await writeCache(storage, resolved);
  return resolved;
}

/** Clear the cached resolution (e.g. on sign-out or account switch) so the
 * next ensure re-resolves against the new account. */
export async function clearResolvedAttention(
  storage: StorageArea = defaultLocalStorageArea(),
): Promise<void> {
  await storage.remove(RESOLVED_KEY);
}

// --- internals ------------------------------------------------------------

interface ApiCtx {
  fetchFn: FetchFn;
  token: string;
}

async function readCache(
  storage: StorageArea,
): Promise<ResolvedAttention | null> {
  const r = await storage.get(RESOLVED_KEY);
  const v = r[RESOLVED_KEY] as ResolvedAttention | undefined;
  if (!v || typeof v.definitionId !== "string" || !Array.isArray(v.tagIds)) {
    return null;
  }
  return v;
}

async function writeCache(
  storage: StorageArea,
  resolved: ResolvedAttention,
): Promise<void> {
  await storage.set({ [RESOLVED_KEY]: resolved });
}

function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

/** Raise UnauthorizedError on 401, generic Error on any other non-2xx. */
function assertOk(resp: Response, what: string): void {
  if (resp.status === 401) throw new UnauthorizedError(`${what}: 401`);
  if (resp.status < 200 || resp.status >= 300) {
    throw new Error(`${what}: HTTP ${resp.status}`);
  }
}

/**
 * Tag find-or-create. Mirrors _resolve_tag:
 *   GET /user/v1alpha1/tag/name/{name} → 200 {id}; else
 *   POST /user/v1alpha1/tag {name} → {id}.
 * The name is percent-encoded in the GET path (safe="") because the lookup
 * goes in the URL; the POST body always carries the raw name.
 */
async function resolveTag(ctx: ApiCtx, name: string): Promise<string> {
  const path = encodeURIComponent(name);
  const getResp = await ctx.fetchFn(
    `${API_BASE}/user/v1alpha1/tag/name/${path}`,
    { method: "GET", headers: authHeaders(ctx.token) },
  );
  if (getResp.status === 200) {
    const body = (await getResp.json()) as { id?: string };
    if (body && typeof body.id === "string") return body.id;
    throw new Error(`tag lookup for ${name}: missing id in 200 body`);
  }
  if (getResp.status === 401) throw new UnauthorizedError("tag lookup: 401");
  // Any non-200, non-401 (typically 404 "not found") → create it.
  const postResp = await ctx.fetchFn(`${API_BASE}/user/v1alpha1/tag`, {
    method: "POST",
    headers: { ...authHeaders(ctx.token), "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  assertOk(postResp, `tag create for ${name}`);
  const body = (await postResp.json()) as { id?: string };
  if (!body || typeof body.id !== "string") {
    throw new Error(`tag create for ${name}: missing id in response`);
  }
  return body.id;
}

/**
 * Return the id of the live "Attention" duration definition, or null.
 * Mirrors fulcra.py _find_attention_definition: list all defs, keep
 * name=="Attention" && annotation_type=="duration" && !deleted_at, sort by
 * created_at, take the oldest so every machine converges on one def.
 */
async function findAttentionDefinition(ctx: ApiCtx): Promise<string | null> {
  const resp = await ctx.fetchFn(`${API_BASE}/user/v1alpha1/annotation`, {
    method: "GET",
    headers: authHeaders(ctx.token),
  });
  assertOk(resp, "list annotation definitions");
  const rows = (await resp.json()) as DefinitionRow[];
  const matches = (Array.isArray(rows) ? rows : []).filter(
    (d) =>
      d.name === ATTENTION_DEFINITION_NAME &&
      d.annotation_type === "duration" &&
      !d.deleted_at,
  );
  if (matches.length === 0) return null;
  matches.sort((a, b) =>
    (a.created_at || "").localeCompare(b.created_at || ""),
  );
  const id = matches[0].id;
  return typeof id === "string" ? id : null;
}

/** Create the canonical Attention definition. Mirrors fulcra.py
 * ensure_definitions' create branch: POST the full create payload (with the
 * resolved tag ids attached). */
async function createAttentionDefinition(
  ctx: ApiCtx,
  tagIds: string[],
): Promise<string> {
  const resp = await ctx.fetchFn(`${API_BASE}/user/v1alpha1/annotation`, {
    method: "POST",
    headers: { ...authHeaders(ctx.token), "Content-Type": "application/json" },
    body: JSON.stringify(attentionCreatePayload(tagIds)),
  });
  assertOk(resp, "create annotation definition");
  const body = (await resp.json()) as { id?: string };
  if (!body || typeof body.id !== "string") {
    throw new Error("create annotation definition: missing id in response");
  }
  return body.id;
}
