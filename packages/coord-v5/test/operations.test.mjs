import { test } from "node:test";
import assert from "node:assert/strict";
import {
  chmodSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";

test("installed commands replay exact owned retained work and keep partial/unknown honest", async () => {
  const dir = mkdtempSync(join(tmpdir(), "coord-v5-operations-"));
  try {
    const packed = spawnSync(
      "npm",
      ["pack", "--json", "--pack-destination", dir],
      { cwd: resolve(import.meta.dirname, ".."), encoding: "utf8" },
    );
    assert.equal(packed.status, 0, packed.stderr);
    const filename = JSON.parse(packed.stdout)[0].filename;
    assert.equal(
      spawnSync(
        "npm",
        [
          "install",
          "--offline",
          "--ignore-scripts",
          "--no-audit",
          "--no-fund",
          join(dir, filename),
        ],
        { cwd: dir, encoding: "utf8" },
      ).status,
      0,
    );
    const root = join(dir, "node_modules", "@fulcra", "coord-v5");
    const bin = join(dir, "node_modules", ".bin", "coord-v5");
    const load = (path) => import(pathToFileURL(join(root, path)).href);
    const { readWorkWindow } = await load(
      "src/lib/server/gatekeeper/work-transport-read.js",
    );
    const { openWorkTransportStore } = await load(
      "src/lib/server/gatekeeper/work-transport-store.js",
    );
    const { serializeWorkEvent, workContentDigest } = await load(
      "src/lib/gatekeeper/work-contract.js",
    );
    const { replayWorkEvents } = await load(
      "src/lib/gatekeeper/work-projection.js",
    );
    const fixture = JSON.parse(
      readFileSync(
        resolve(
          import.meta.dirname,
          "../tests/fixtures/work-backlog-synthetic.json",
        ),
        "utf8",
      ),
    );
    const principal = "00000000-0000-4000-8000-000000000777";
    const stream = "00000000-0000-4000-8000-000000000778";
    const event = {
      ...fixture.events[0],
      stream_id: stream,
      actor: { ...fixture.events[0].actor, principal_id: principal },
    };
    const config = {
      baseUrl: "https://api.fulcradynamics.com/",
      principalId: principal,
      channel: `MomentAnnotation/${stream}`,
      workspaceId: event.workspace_id,
      workstreamId: event.workstream_id,
      actorBinding: event.actor,
    };
    const policy = {
      principal_id: principal,
      workspace_id: event.workspace_id,
      stream_id: stream,
      grants: [{ ...event.actor, capabilities: ["work.write"] }],
      work_jobs: [],
    };
    const source = `com.fulcradynamics.annotation.${stream}`;
    const metadata = {
      id: stream,
      fulcra_userid: principal,
      annotation_type: "moment",
      fulcra_source_id: source,
      deleted_at: null,
    };
    const fetch = async (url) =>
      new Response(
        JSON.stringify(
          String(url).includes("/info")
            ? { userid: principal }
            : String(url).includes("/catalog")
              ? [
                  {
                    id: config.channel,
                    api_version: "v1alpha1",
                    recordable: true,
                    queryable: true,
                    record_spec: { type: "event" },
                    fulcra_userid: principal,
                  },
                ]
              : String(url).includes("/annotation?")
                ? [metadata]
                : [
                    {
                      id: "00000000-0000-4000-8000-000000000779",
                      source_id: source,
                      metadata,
                      note: serializeWorkEvent(event),
                    },
                  ],
        ),
        { headers: { "content-type": "application/json" } },
      );
    const cache = join(dir, "cache");
    // Existing install dir contains npm files; separate private cache is required.
    const { mkdirSync } = await import("node:fs");
    mkdirSync(cache, { mode: 0o700 });
    chmodSync(cache, 0o700);
    const configPath = join(cache, "config.json");
    const policyPath = join(cache, "policy.json");
    writeFileSync(configPath, JSON.stringify(config), { mode: 0o600 });
    writeFileSync(policyPath, JSON.stringify(policy), { mode: 0o600 });
    const db = join(cache, "work.sqlite");
    const store = openWorkTransportStore({ config, dbPath: db });
    const read = await readWorkWindow({
      config,
      fetch,
      token: "fixture-only",
      start: "2026-09-26T00:00:00Z",
      end: "2026-09-27T00:00:00Z",
      now: () => Date.parse("2026-09-27T01:00:00Z"),
    });
    assert.equal(store.appendWindow(read).status, "stored");
    store.close();
    assert.equal(read.scope.stream_id, stream);
    for (const wrong of ["principal", "owner", "stream"]) {
      const wrongFetch = async (url) => {
        if (wrong === "principal" && String(url).includes("/info"))
          return new Response(JSON.stringify({ userid: "wrong-principal" }));
        if (wrong !== "principal" && String(url).includes("/annotation?"))
          return new Response(
            JSON.stringify([
              {
                ...metadata,
                ...(wrong === "owner"
                  ? { fulcra_userid: "wrong-owner" }
                  : { id: "00000000-0000-4000-8000-000000000780" }),
              },
            ]),
          );
        return fetch(url);
      };
      const denied = await readWorkWindow({
        config,
        fetch: wrongFetch,
        token: "fixture-only",
        start: "2026-09-26T00:00:00Z",
        end: "2026-09-27T00:00:00Z",
      });
      assert.equal(denied.observation.coverage, "unavailable", wrong);
      assert.equal(denied.candidates.length, 0, wrong);
    }
    assert.throws(
      () =>
        openWorkTransportStore({
          config: {
            ...config,
            channel: "MomentAnnotation/00000000-0000-4000-8000-000000000780",
          },
          dbPath: db,
        }),
      /STORE_SCOPE_MISMATCH/,
    );
    const run = (args, input) =>
      spawnSync(bin, args, {
        cwd: dir,
        input:
          input === undefined
            ? undefined
            : typeof input === "string"
              ? input
              : JSON.stringify(input),
        encoding: "utf8",
      });
    const flags = ["--config", configPath, "--db", db, "--policy", policyPath];
    const viewed = run(["work", "view", ...flags]);
    assert.equal(viewed.status, 0, viewed.stdout + viewed.stderr);
    const view = JSON.parse(viewed.stdout).projection;
    assert.equal(view.work[0].work_id, event.subject.id);
    assert.equal(view.observation.coverage, "partial");
    assert.equal(view.as_of, "2026-09-27T01:00:00.000Z");
    const digested = run([
      "work",
      "digest",
      ...flags,
      "--viewer",
      "agent-a",
      "--role",
      "coordinator",
      "--query",
      "everything_owed",
    ]);
    assert.equal(digested.status, 0, digested.stdout);
    const digest = JSON.parse(digested.stdout);
    assert.equal(digest.coverage, "partial");
    assert.equal(digest.clear, false);
    assert.equal(digest.as_of, view.as_of);
    writeFileSync(policyPath, JSON.stringify({ ...policy, grants: [] }));
    const withheld = run(["work", "view", ...flags]);
    assert.equal(withheld.status, 0);
    assert.equal(JSON.parse(withheld.stdout).projection.work.length, 0);
    assert.equal(
      JSON.parse(withheld.stdout).projection.observation.coverage,
      "partial",
    );
    const withheldDigest = run([
      "work",
      "digest",
      ...flags,
      "--viewer",
      "agent-a",
      "--role",
      "coordinator",
      "--query",
      "everything_owed",
    ]);
    assert.equal(JSON.parse(withheldDigest.stdout).clear, false);
    writeFileSync(
      policyPath,
      JSON.stringify({
        ...policy,
        stream_id: "00000000-0000-4000-8000-000000000780",
      }),
    );
    const wrongPolicy = run(["work", "view", ...flags]);
    assert.notEqual(wrongPolicy.status, 0);
    assert.equal(JSON.parse(wrongPolicy.stdout).status, "blocked");
    writeFileSync(policyPath, JSON.stringify(policy));
    const coldDb = join(cache, "cold.sqlite");
    openWorkTransportStore({ config, dbPath: coldDb }).close();
    const cold = run([
      "work",
      "view",
      "--config",
      configPath,
      "--db",
      coldDb,
      "--policy",
      policyPath,
    ]);
    assert.notEqual(cold.status, 0);
    assert.equal(JSON.parse(cold.stdout).status, "unavailable");
    assert.notEqual(JSON.parse(cold.stdout).clear, true);
    assert.equal(run(["event", "validate"], event).status, 0);
    const invalid = run(["event", "validate"], "{ broken");
    assert.notEqual(invalid.status, 0);
    assert.equal(JSON.parse(invalid.stdout).error.code, "INVALID_JSON");
    const large = run(["event", "validate"], "x".repeat(1024 * 1024 + 1));
    assert.notEqual(large.status, 0);
    assert.equal(JSON.parse(large.stdout).error.code, "INPUT_LIMIT");
    const normalizePrincipal = (value) => JSON.parse(JSON.stringify(value)
      .replaceAll('principal-a', principal)
      .replaceAll('00000000-0000-4000-8000-000000000200', stream));
    const body = normalizePrincipal(JSON.parse(
      readFileSync(
        resolve(
          import.meta.dirname,
          "../tests/fixtures/work-handoff-synthetic.json",
        ),
        "utf8",
      ),
    ));
    const checkpoint = run(["checkpoint", "validate"], body);
    assert.equal(checkpoint.status, 0, checkpoint.stdout);
    const again = run(
      ["checkpoint", "validate"],
      JSON.parse(checkpoint.stdout).checkpoint,
    );
    assert.equal(again.status, 0);
    assert.deepEqual(JSON.parse(again.stdout), JSON.parse(checkpoint.stdout));
    const history = normalizePrincipal([...fixture.events, ...fixture.replay_events]);
    const base = history[0];
    const time = "2026-09-27T12:00:00.000Z";
    const recipient = {
      ...base.actor,
      logical_agent_id: "receiver",
      instance_id: "receiver-instance",
    };
    const publication = {
      ...base,
      event_id: "30000000-0000-4000-8000-000000000001",
      operation_id: "30000000-0000-4000-8000-000000001001",
      kind: "checkpoint.published",
      subject: { type: "checkpoint", id: body.checkpoint_id },
      occurred_at: time,
      parents: [history[5].event_id],
      payload: {
        work_id: body.work_id,
        checkpoint_id: body.checkpoint_id,
        artifact: {
          id: "30000000-0000-4000-8000-000000000002",
          uri: "https://example.invalid/checkpoint",
          sha256: workContentDigest(body),
          version: null,
          media_type: "application/json",
          owner_principal_id: "artifact-owner",
          audience: "workspace",
          portable: true,
        },
        body_digest: workContentDigest(body),
        assignment_version: body.assignment_version,
        assignment_event_id: body.assignment_event_id,
      },
    };
    const events = [...history, publication];
    const context = {
      events,
      trust: {
        workspace_id: base.workspace_id,
        allowed_stream_ids: [base.stream_id],
        grants: [
          {
            ...base.actor,
            capabilities: [
              "work.write",
              "assignment.manage",
              "assignment.accept",
              "question.ask",
              "question.answer",
              "question.apply",
              "checkpoint.publish",
              "handoff.offer",
            ],
          },
        ],
        event_evidence: events.map((e, n) => ({
          event_id: e.event_id,
          event_digest: workContentDigest(e),
          record_id: `fixture-${n}`,
          source_principal_id: e.actor.principal_id,
          stream_id: e.stream_id,
          received_at: time,
        })),
      },
      observation: {
        coverage: "partial",
        as_of: time,
        last_successful_observation_at: time,
        sources: [
          { stream_id: base.stream_id, status: "partial", pending_pages: null },
        ],
        gaps: [],
        errors: [],
        completeness_evidence_id: null,
      },
      asOf: time,
    };
    const preparedResult = run(["checkpoint", "prepare"], body);
    assert.equal(preparedResult.status, 0, preparedResult.stdout);
    const prepared = JSON.parse(preparedResult.stdout);
    assert.deepEqual(JSON.parse(prepared.canonical_body), prepared.checkpoint);
    assert.equal(prepared.body_digest, workContentDigest(prepared.checkpoint));
    assert.equal(prepared.artifact_sha256,
      createHash("sha256").update(prepared.canonical_body, "utf8").digest("hex"));
    assert.equal(prepared.canonical_body.endsWith("\n"), false);
    assert.notEqual(run(["checkpoint", "prepare"], {}).status, 0);
    const packaged = run(["checkpoint", "package"], {
      ...context,
      checkpoint: body,
      publicationEvent: publication,
      recipient,
      accessRequirements: [],
    });
    assert.equal(packaged.status, 0, packaged.stdout);
    const pkg = JSON.parse(packaged.stdout).package;
    assert.equal(pkg.checkpoint.checkpoint_id, body.checkpoint_id);
    const readiness = run(["handoff", "readiness"], {
      package: pkg,
      projection: replayWorkEvents(context),
      receiver: recipient,
      checks: null,
      asOf: time,
    });
    assert.notEqual(readiness.status, 0);
    assert.ok(JSON.parse(readiness.stdout).errors.includes("CHECKS_MISSING"));
    assert.notEqual(run(["checkpoint", "package"], {}).status, 0);
    assert.notEqual(run(["handoff", "readiness"], {}).status, 0);
    const packageDigest = workContentDigest(pkg);
    const receiver = recipient;
    const offer = {
      ...base,
      event_id: "30000000-0000-4000-8000-000000000004",
      operation_id: "30000000-0000-4000-8000-000000001004",
      kind: "handoff.offered",
      subject: { type: "handoff", id: "30000000-0000-4000-8000-000000000010" },
      occurred_at: time,
      parents: [publication.event_id, history[5].event_id],
      payload: {
        work_id: body.work_id,
        expected_assignment_event_id: body.assignment_event_id,
        expected_version: body.assignment_version,
        checkpoint_event_id: publication.event_id,
        package_digest: packageDigest,
        target: receiver,
      },
    };
    const ready = {
      ...base,
      event_id: "30000000-0000-4000-8000-000000000005",
      operation_id: "30000000-0000-4000-8000-000000001005",
      kind: "handoff.ready",
      subject: offer.subject,
      actor: receiver,
      occurred_at: time,
      parents: [offer.event_id],
      payload: { offer_event_id: offer.event_id, package_digest: packageDigest,
        verification_receipt_id: "receipt-pub" },
    };
    const handoffDb = join(cache, "handoff.sqlite");
    const handoffStore = openWorkTransportStore({ config, dbPath: handoffDb });
    const rows = [...history, publication, offer, ready].map((e, n) => ({
      id: `50000000-0000-4000-8000-${String(n + 1).padStart(12, "0")}`,
      source_id: source, metadata, note: serializeWorkEvent(e),
    }));
    const handoffFetch = (url) => String(url).includes("/info") ||
      String(url).includes("/catalog") || String(url).includes("/annotation?")
      ? fetch(url)
      : new Response(JSON.stringify(rows), { headers: { "content-type": "application/json" } });
    const handoffRead = await readWorkWindow({ config, fetch: handoffFetch,
      token: "fixture-only", start: "2026-09-26T00:00:00Z", end: "2026-09-27T00:00:00Z",
      now: () => Date.parse(time) });
    assert.equal(handoffStore.appendWindow(handoffRead).status, "stored");
    handoffStore.close();
    const receipt = {
      schema: "handoff-verification/1", principal_id: principal,
      workspace_id: base.workspace_id, workstream_id: base.workstream_id,
      stream_id: stream, package: pkg,
      verification: {
        ready_event_id: ready.event_id, ready_event_digest: workContentDigest(ready),
        offer_event_id: offer.event_id, package_digest: packageDigest,
        checks: {
          checked_at: time, valid_until: "2099-01-01T00:00:00.000Z",
          receiver, package_digest: packageDigest,
          publication: { event_id: publication.event_id, artifact_id: publication.payload.artifact.id,
            body_digest: publication.payload.body_digest, status: "verified", receipt_id: "receipt-pub" },
          resources: [],
        },
      },
    };
    const receiptPath = join(cache, "receipt.json");
    writeFileSync(receiptPath, JSON.stringify(receipt), { mode: 0o600 });
    const importArgs = ["handoff", "verification", "import", "--config", configPath,
      "--db", handoffDb, "--receipt", receiptPath];
    const imported = run(importArgs);
    assert.equal(imported.status, 0, imported.stdout + imported.stderr);
    assert.deepEqual(JSON.parse(imported.stdout), { status: "stored" });
    assert.deepEqual(JSON.parse(run(importArgs).stdout), { status: "same" });
    const handoffPolicy = { ...policy, grants: [
      { ...event.actor, capabilities: ["work.write", "assignment.manage", "assignment.accept",
        "question.ask", "question.answer", "question.apply", "checkpoint.publish", "handoff.offer"] },
      { ...receiver, capabilities: ["handoff.ready", "assignment.accept"] },
    ] };
    writeFileSync(policyPath, JSON.stringify(handoffPolicy));
    const handoffView = run(["work", "view", "--config", configPath, "--db", handoffDb,
      "--policy", policyPath]);
    assert.equal(handoffView.status, 0, handoffView.stdout);
    const handoffProjection = JSON.parse(handoffView.stdout).projection;
    assert.equal(handoffProjection.observation.coverage, "partial");
    assert.equal(handoffProjection.handoffs[0].ready_event.event_id, ready.event_id);
    const badReceipt = { ...receipt, extra: "secret-sentinel" };
    writeFileSync(receiptPath, JSON.stringify(badReceipt));
    const badImport = run(importArgs);
    assert.notEqual(badImport.status, 0);
    assert.equal(badImport.stdout.includes("secret-sentinel"), false);
    chmodSync(receiptPath, 0o644);
    const publicInput = run(importArgs);
    assert.notEqual(publicInput.status, 0);
    assert.equal(publicInput.stdout.includes("secret-sentinel"), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
