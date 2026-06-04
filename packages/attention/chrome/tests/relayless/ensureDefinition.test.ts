// chrome/tests/relayless/ensureDefinition.test.ts
import { describe, test, expect, vi } from "vitest";
import {
  ensureAttentionDefinitionAndTags,
  listAttentionDestinations,
  chooseAttentionDestination,
  createAttentionDestination,
  clearResolvedAttention,
  UnauthorizedError,
  ATTENTION_DEFINITION_NAME,
  ATTENTION_DEFINITION_DESCRIPTION,
} from "../../src/relayless/ensureDefinition";
import { memStorage, mockFetch } from "./memStorage";

const TOK = "ACCESS";
const getToken = vi.fn(async () => TOK as string | null);

function tagRow(id: string) {
  return new Response(JSON.stringify({ id }), { status: 200 });
}
function notFound() {
  return new Response('{"error":"not found"}', { status: 404 });
}
function created(id: string) {
  return new Response(JSON.stringify({ id }), { status: 200 });
}

/** A fetch handler routing the Data API endpoints. `defs` is the list the
 * annotation list-GET returns. Records the create-def POST body. */
function makeApi(opts: {
  tags: Record<string, string | null>; // name -> id, or null = 404 (create)
  defs: unknown[];
  defCreateId?: string;
}) {
  const calls = {
    tagGets: [] as string[],
    tagPosts: [] as string[],
    defGets: 0,
    defPosts: [] as unknown[],
  };
  let tagCreateSeq = 1000;
  const fetchFn = mockFetch(async (input, init) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (url.includes("/user/v1alpha1/tag/name/")) {
      const name = decodeURIComponent(url.split("/tag/name/")[1]);
      calls.tagGets.push(name);
      const id = opts.tags[name];
      return id ? tagRow(id) : notFound();
    }
    if (url.endsWith("/user/v1alpha1/tag") && method === "POST") {
      const body = JSON.parse(String(init?.body)) as { name: string };
      calls.tagPosts.push(body.name);
      return created(`created-${body.name}-${tagCreateSeq++}`);
    }
    if (url.endsWith("/user/v1alpha1/annotation") && method === "GET") {
      calls.defGets += 1;
      return new Response(JSON.stringify(opts.defs), { status: 200 });
    }
    if (url.endsWith("/user/v1alpha1/annotation") && method === "POST") {
      calls.defPosts.push(JSON.parse(String(init?.body)));
      return created(opts.defCreateId ?? "new-def");
    }
    throw new Error(`unexpected request: ${method} ${url}`);
  });
  return { fetchFn, calls };
}

describe("ensureAttentionDefinitionAndTags", () => {
  test("adopts an existing Attention def (no create POST), resolves tag ids", async () => {
    const storage = memStorage();
    const { fetchFn, calls } = makeApi({
      tags: { attention: "tag-attn", web: "tag-web" },
      defs: [
        {
          id: "def-existing",
          name: "Attention",
          annotation_type: "duration",
          deleted_at: null,
          created_at: "2026-01-01T00:00:00Z",
        },
      ],
    });
    const res = await ensureAttentionDefinitionAndTags({
      getToken,
      fetch: fetchFn,
      storage,
    });
    expect(res).toEqual({
      definitionId: "def-existing",
      tagIds: ["tag-attn", "tag-web"],
    });
    expect(calls.defPosts).toHaveLength(0); // adopted, not created
    expect(calls.tagGets).toEqual(["attention", "web"]);
    expect(calls.tagPosts).toHaveLength(0); // both tags existed
  });

  test("creates the def when none exists, with the canonical payload", async () => {
    const storage = memStorage();
    const { fetchFn, calls } = makeApi({
      tags: { attention: "tag-attn", web: "tag-web" },
      defs: [],
      defCreateId: "def-new",
    });
    const res = await ensureAttentionDefinitionAndTags({
      getToken,
      fetch: fetchFn,
      storage,
    });
    expect(res.definitionId).toBe("def-new");
    expect(res.tagIds).toEqual(["tag-attn", "tag-web"]);
    expect(calls.defPosts).toHaveLength(1);
    expect(calls.defPosts[0]).toEqual({
      annotation_type: "duration",
      name: ATTENTION_DEFINITION_NAME,
      description: ATTENTION_DEFINITION_DESCRIPTION,
      tags: ["tag-attn", "tag-web"],
      measurement_spec: {
        measurement_type: "duration",
        value_type: "duration",
        unit: null,
      },
    });
  });

  test("creates tags that are absent (find returns 404 → POST)", async () => {
    const storage = memStorage();
    const { fetchFn, calls } = makeApi({
      tags: { attention: null, web: "tag-web" }, // attention missing
      defs: [],
    });
    const res = await ensureAttentionDefinitionAndTags({
      getToken,
      fetch: fetchFn,
      storage,
    });
    expect(calls.tagGets).toEqual(["attention", "web"]);
    expect(calls.tagPosts).toEqual(["attention"]); // only the missing one
    expect(res.tagIds[0]).toMatch(/^created-attention-/);
    expect(res.tagIds[1]).toBe("tag-web");
  });

  test("ignores soft-deleted and non-duration defs; picks oldest by created_at", async () => {
    const storage = memStorage();
    const { fetchFn, calls } = makeApi({
      tags: { attention: "a", web: "w" },
      defs: [
        { id: "deleted", name: "Attention", annotation_type: "duration", deleted_at: "2026-02-02T00:00:00Z", created_at: "2026-01-01T00:00:00Z" },
        { id: "moment", name: "Attention", annotation_type: "moment", deleted_at: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "newer", name: "Attention", annotation_type: "duration", deleted_at: null, created_at: "2026-03-01T00:00:00Z" },
        { id: "oldest", name: "Attention", annotation_type: "duration", deleted_at: null, created_at: "2026-02-15T00:00:00Z" },
      ],
    });
    const res = await ensureAttentionDefinitionAndTags({ getToken, fetch: fetchFn, storage });
    expect(res.definitionId).toBe("oldest");
    expect(calls.defPosts).toHaveLength(0);
  });

  test("caches: second call makes no network requests", async () => {
    const storage = memStorage();
    const { fetchFn, calls } = makeApi({
      tags: { attention: "tag-attn", web: "tag-web" },
      defs: [{ id: "def-existing", name: "Attention", annotation_type: "duration", deleted_at: null, created_at: "2026-01-01T00:00:00Z" }],
    });
    const a = await ensureAttentionDefinitionAndTags({ getToken, fetch: fetchFn, storage });
    const callsAfterFirst = fetchFn.mock.calls.length;
    expect(callsAfterFirst).toBeGreaterThan(0);
    const b = await ensureAttentionDefinitionAndTags({ getToken, fetch: fetchFn, storage });
    expect(b).toEqual(a);
    expect(fetchFn.mock.calls.length).toBe(callsAfterFirst); // no new requests
    expect(calls.defGets).toBe(1);
  });

  test("clearResolvedAttention forces re-resolution", async () => {
    const storage = memStorage();
    const { fetchFn } = makeApi({
      tags: { attention: "a", web: "w" },
      defs: [{ id: "d1", name: "Attention", annotation_type: "duration", deleted_at: null, created_at: "2026-01-01T00:00:00Z" }],
    });
    await ensureAttentionDefinitionAndTags({ getToken, fetch: fetchFn, storage });
    const n = fetchFn.mock.calls.length;
    await clearResolvedAttention(storage);
    await ensureAttentionDefinitionAndTags({ getToken, fetch: fetchFn, storage });
    expect(fetchFn.mock.calls.length).toBeGreaterThan(n);
  });

  test("throws UnauthorizedError when not signed in (no token)", async () => {
    const storage = memStorage();
    const { fetchFn } = makeApi({ tags: {}, defs: [] });
    const noToken = vi.fn(async () => null);
    await expect(
      ensureAttentionDefinitionAndTags({ getToken: noToken, fetch: fetchFn, storage }),
    ).rejects.toBeInstanceOf(UnauthorizedError);
    expect(fetchFn).not.toHaveBeenCalled();
  });

  test("throws UnauthorizedError on a 401 from the API", async () => {
    const storage = memStorage();
    const fetchFn = mockFetch(async () => new Response("", { status: 401 }));
    await expect(
      ensureAttentionDefinitionAndTags({ getToken, fetch: fetchFn, storage }),
    ).rejects.toBeInstanceOf(UnauthorizedError);
  });
});

describe("listAttentionDestinations", () => {
  test("returns only live Attention duration defs, oldest-first, isAutoPick on first", async () => {
    const storage = memStorage();
    const { fetchFn, calls } = makeApi({
      tags: {},
      defs: [
        { id: "deleted", name: "Attention", annotation_type: "duration", deleted_at: "2026-02-02T00:00:00Z", created_at: "2026-01-01T00:00:00Z" },
        { id: "moment", name: "Attention", annotation_type: "moment", deleted_at: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "otherName", name: "Focus", annotation_type: "duration", deleted_at: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "newer", name: "Attention", annotation_type: "duration", deleted_at: null, created_at: "2026-03-01T00:00:00Z" },
        { id: "oldest", name: "Attention", annotation_type: "duration", deleted_at: null, created_at: "2026-02-15T00:00:00Z" },
      ],
    });
    const out = await listAttentionDestinations({ getToken, fetch: fetchFn, storage });
    expect(out.map((d) => d.id)).toEqual(["oldest", "newer"]);
    expect(out[0]).toEqual({
      id: "oldest",
      name: "Attention",
      createdAt: "2026-02-15T00:00:00Z",
      isAutoPick: true,
    });
    expect(out[1].isAutoPick).toBe(false);
    expect(calls.defGets).toBe(1);
  });

  test("returns an empty list when no Attention defs exist", async () => {
    const storage = memStorage();
    const { fetchFn } = makeApi({ tags: {}, defs: [] });
    const out = await listAttentionDestinations({ getToken, fetch: fetchFn, storage });
    expect(out).toEqual([]);
  });

  test("throws UnauthorizedError when not signed in", async () => {
    const storage = memStorage();
    const { fetchFn } = makeApi({ tags: {}, defs: [] });
    const noToken = vi.fn(async () => null);
    await expect(
      listAttentionDestinations({ getToken: noToken, fetch: fetchFn, storage }),
    ).rejects.toBeInstanceOf(UnauthorizedError);
    expect(fetchFn).not.toHaveBeenCalled();
  });
});

describe("chooseAttentionDestination", () => {
  test("resolves tag ids and caches {definitionId, tagIds} with the chosen id", async () => {
    const storage = memStorage();
    const { fetchFn, calls } = makeApi({
      tags: { attention: "tag-attn", web: "tag-web" },
      defs: [],
    });
    const res = await chooseAttentionDestination(
      { getToken, fetch: fetchFn, storage },
      "chosen-def",
    );
    expect(res).toEqual({
      definitionId: "chosen-def",
      tagIds: ["tag-attn", "tag-web"],
    });
    expect(calls.defPosts).toHaveLength(0); // never creates
    expect(calls.tagGets).toEqual(["attention", "web"]);
    // Cached so capture reads exactly this destination.
    const cached = await ensureAttentionDefinitionAndTags({ getToken, fetch: fetchFn, storage });
    expect(cached).toEqual(res);
  });

  test("throws UnauthorizedError when not signed in", async () => {
    const storage = memStorage();
    const { fetchFn } = makeApi({ tags: {}, defs: [] });
    const noToken = vi.fn(async () => null);
    await expect(
      chooseAttentionDestination({ getToken: noToken, fetch: fetchFn, storage }, "x"),
    ).rejects.toBeInstanceOf(UnauthorizedError);
  });
});

describe("createAttentionDestination", () => {
  test("POSTs a create with the given name and caches the new id", async () => {
    const storage = memStorage();
    const { fetchFn, calls } = makeApi({
      tags: { attention: "tag-attn", web: "tag-web" },
      defs: [],
      defCreateId: "fresh-def",
    });
    const res = await createAttentionDestination(
      { getToken, fetch: fetchFn, storage },
      "My Attention",
    );
    expect(res.definitionId).toBe("fresh-def");
    expect(res.tagIds).toEqual(["tag-attn", "tag-web"]);
    expect(calls.defPosts).toHaveLength(1);
    expect(calls.defPosts[0]).toEqual({
      annotation_type: "duration",
      name: "My Attention",
      description: ATTENTION_DEFINITION_DESCRIPTION,
      tags: ["tag-attn", "tag-web"],
      measurement_spec: {
        measurement_type: "duration",
        value_type: "duration",
        unit: null,
      },
    });
    const cached = await ensureAttentionDefinitionAndTags({ getToken, fetch: fetchFn, storage });
    expect(cached).toEqual(res);
  });

  test("defaults the name to ATTENTION_DEFINITION_NAME", async () => {
    const storage = memStorage();
    const { fetchFn, calls } = makeApi({
      tags: { attention: "a", web: "w" },
      defs: [],
      defCreateId: "fresh-def",
    });
    await createAttentionDestination({ getToken, fetch: fetchFn, storage });
    expect((calls.defPosts[0] as { name: string }).name).toBe(ATTENTION_DEFINITION_NAME);
  });
});
