// chrome/tests/relayless/ensureDefinition.test.ts
import { describe, test, expect, vi } from "vitest";
import {
  ensureAttentionDefinitionAndTags,
  listAttentionDestinations,
  chooseAttentionDestination,
  createAttentionDestination,
  updateIdentity,
  clearResolvedAttention,
  slugifyIdentity,
  machineTagName,
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
    // 401 on every attempt (incl. the forced retry) → still unauthorized.
    const fetchFn = mockFetch(async () => new Response("", { status: 401 }));
    const forcingToken = vi.fn(async (o?: { force?: boolean }) =>
      (o?.force ? "FRESH" : "STALE") as string | null,
    );
    await expect(
      ensureAttentionDefinitionAndTags({ getToken: forcingToken, fetch: fetchFn, storage }),
    ).rejects.toBeInstanceOf(UnauthorizedError);
  });

  test("force-refreshes once on a 401 and succeeds when the refreshed token works", async () => {
    // Repro of Bug A2: the stored access token is fresh by the local clock but
    // the server has REVOKED it, so the first Data API call 401s. A valid
    // refresh token recovers in one forced refresh — ensure must retry, not nag
    // the user to sign in again.
    const storage = memStorage();
    const getTok = vi.fn(async (o?: { force?: boolean }) =>
      (o?.force ? "FRESH" : "STALE") as string | null,
    );
    // Every request 401s until the FRESH (forced) token is presented.
    const seenAuth: string[] = [];
    const fetchFn = mockFetch(async (input, init) => {
      const auth = (init?.headers as Record<string, string> | undefined)?.[
        "Authorization"
      ];
      seenAuth.push(auth ?? "");
      if (auth !== "Bearer FRESH") return new Response("", { status: 401 });
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.includes("/user/v1alpha1/tag/name/")) {
        const name = decodeURIComponent(url.split("/tag/name/")[1]);
        return tagRow(`id-${name}`);
      }
      if (url.endsWith("/user/v1alpha1/annotation") && method === "GET") {
        return new Response(
          JSON.stringify([
            {
              id: "def-existing",
              name: "Attention",
              annotation_type: "duration",
              deleted_at: null,
              created_at: "2026-01-01T00:00:00Z",
            },
          ]),
          { status: 200 },
        );
      }
      throw new Error(`unexpected request: ${method} ${url}`);
    });

    const res = await ensureAttentionDefinitionAndTags({
      getToken: getTok,
      fetch: fetchFn,
      storage,
    });
    expect(res.definitionId).toBe("def-existing");
    expect(res.tagIds).toEqual(["id-attention", "id-web"]);
    // A forced refresh was attempted exactly because of the 401.
    expect(getTok).toHaveBeenCalledWith({ force: true });
    // The very first request used the stale token, then a forced retry.
    expect(seenAuth[0]).toBe("Bearer STALE");
    expect(seenAuth).toContain("Bearer FRESH");
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

  test("with an identity label resolves+caches [attention, web, machine:<slug>]", async () => {
    const storage = memStorage();
    const { fetchFn, calls } = makeApi({
      tags: { attention: "tag-attn", web: "tag-web", "machine:work-mbp-chrome": "tag-machine" },
      defs: [],
      defCreateId: "fresh-def",
    });
    const res = await createAttentionDestination(
      { getToken, fetch: fetchFn, storage },
      "My Attention",
      "Work MBP — Chrome",
    );
    expect(res.tagIds).toEqual(["tag-attn", "tag-web", "tag-machine"]);
    expect(calls.tagGets).toEqual(["attention", "web", "machine:work-mbp-chrome"]);
    const cached = await ensureAttentionDefinitionAndTags({ getToken, fetch: fetchFn, storage });
    expect(cached.tagIds).toEqual(["tag-attn", "tag-web", "tag-machine"]);
  });
});

describe("slugifyIdentity", () => {
  test("lowercases and replaces runs of non-[a-z0-9] with single -", () => {
    expect(slugifyIdentity("Work MBP — Chrome")).toBe("work-mbp-chrome");
    expect(slugifyIdentity("ash@fulcra's laptop!!")).toBe("ash-fulcra-s-laptop");
  });

  test("trims leading/trailing separators and collapses repeats", () => {
    expect(slugifyIdentity("  ---Hello___World---  ")).toBe("hello-world");
    expect(slugifyIdentity("a   b")).toBe("a-b");
  });

  test("empty / all-separator label falls back to 'browser'", () => {
    expect(slugifyIdentity("")).toBe("browser");
    expect(slugifyIdentity("   ")).toBe("browser");
    expect(slugifyIdentity("---")).toBe("browser");
  });

  test("machineTagName prefixes machine:", () => {
    expect(machineTagName("Work MBP — Chrome")).toBe("machine:work-mbp-chrome");
    expect(machineTagName("")).toBe("machine:browser");
  });
});

describe("ensureAttentionDefinitionAndTags — identity label", () => {
  test("appends the machine tag when identityLabel is set", async () => {
    const storage = memStorage();
    const { fetchFn, calls } = makeApi({
      tags: { attention: "tag-attn", web: "tag-web", "machine:home-imac": "tag-mach" },
      defs: [{ id: "def-existing", name: "Attention", annotation_type: "duration", deleted_at: null, created_at: "2026-01-01T00:00:00Z" }],
    });
    const res = await ensureAttentionDefinitionAndTags({
      getToken, fetch: fetchFn, storage, identityLabel: "Home iMac",
    });
    expect(res.tagIds).toEqual(["tag-attn", "tag-web", "tag-mach"]);
    expect(calls.tagGets).toEqual(["attention", "web", "machine:home-imac"]);
  });

  test("does NOT append a machine tag when identityLabel is null", async () => {
    const storage = memStorage();
    const { fetchFn, calls } = makeApi({
      tags: { attention: "tag-attn", web: "tag-web" },
      defs: [{ id: "def-existing", name: "Attention", annotation_type: "duration", deleted_at: null, created_at: "2026-01-01T00:00:00Z" }],
    });
    const res = await ensureAttentionDefinitionAndTags({
      getToken, fetch: fetchFn, storage, identityLabel: null,
    });
    expect(res.tagIds).toEqual(["tag-attn", "tag-web"]);
    expect(calls.tagGets).toEqual(["attention", "web"]);
  });
});

describe("chooseAttentionDestination — identity label", () => {
  test("with a label resolves+caches [attention, web, machine:<slug>]", async () => {
    const storage = memStorage();
    const { fetchFn, calls } = makeApi({
      tags: { attention: "tag-attn", web: "tag-web", "machine:work-laptop": "tag-mach" },
      defs: [],
    });
    const res = await chooseAttentionDestination(
      { getToken, fetch: fetchFn, storage },
      "chosen-def",
      "Work Laptop",
    );
    expect(res).toEqual({
      definitionId: "chosen-def",
      tagIds: ["tag-attn", "tag-web", "tag-mach"],
    });
    expect(calls.tagGets).toEqual(["attention", "web", "machine:work-laptop"]);
    const cached = await ensureAttentionDefinitionAndTags({ getToken, fetch: fetchFn, storage });
    expect(cached).toEqual(res);
  });
});

describe("updateIdentity", () => {
  test("rewrites cached tagIds keeping the definitionId", async () => {
    const storage = memStorage();
    // Seed a cache via choose with no identity.
    const { fetchFn } = makeApi({
      tags: { attention: "tag-attn", web: "tag-web", "machine:new-name": "tag-mach" },
      defs: [],
    });
    await chooseAttentionDestination({ getToken, fetch: fetchFn, storage }, "def-keep");

    const res = await updateIdentity({ getToken, fetch: fetchFn, storage }, "New Name");
    expect(res).toEqual({
      definitionId: "def-keep",
      tagIds: ["tag-attn", "tag-web", "tag-mach"],
    });
    // The cache now carries the re-tagged result with the SAME definition id.
    const cached = await ensureAttentionDefinitionAndTags({ getToken, fetch: fetchFn, storage });
    expect(cached).toEqual(res);
  });

  test("empty label re-resolves just [attention, web]", async () => {
    const storage = memStorage();
    const { fetchFn } = makeApi({
      tags: { attention: "tag-attn", web: "tag-web" },
      defs: [],
    });
    await chooseAttentionDestination({ getToken, fetch: fetchFn, storage }, "def-keep", "Old");
    const res = await updateIdentity({ getToken, fetch: fetchFn, storage }, "");
    expect(res?.tagIds).toEqual(["tag-attn", "tag-web"]);
    expect(res?.definitionId).toBe("def-keep");
  });

  test("no-ops (returns null) when there is no cached definition", async () => {
    const storage = memStorage();
    const { fetchFn } = makeApi({ tags: {}, defs: [] });
    const res = await updateIdentity({ getToken, fetch: fetchFn, storage }, "Whatever");
    expect(res).toBeNull();
    expect(fetchFn).not.toHaveBeenCalled();
  });
});
