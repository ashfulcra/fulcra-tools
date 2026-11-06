// @vitest-environment node
import { IDBFactory, IDBObjectStore, IDBDatabase } from "fake-indexeddb";
import { describe, expect, test, vi } from "vitest";
import { V1Journal, type V1Gate } from "../../src/relayless/v1Journal";

const ACCOUNT = "11111111-2222-4333-8444-555555555555";
const OTHER = "22222222-3333-4444-8555-666666666666";
const GENERATION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const NEXT = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
const TARGET = `Event/${OTHER}`;
const SPEC = "collect.attention.v1";
const HASH = "a".repeat(64);
const gate: V1Gate = { version: 1, accountId: ACCOUNT, generation: GENERATION, enabled: false };
function pair() {
  const indexedDB = new IDBFactory();
  return [new V1Journal({ indexedDB }), new V1Journal({ indexedDB })];
}

describe("durable browser v1 account gate and creation intents", () => {
  test("read-only original reservation predicate preserves generation across current-account transitions",async()=>{
    const [journal]=pair();await journal.transition(null,gate);await journal.reserveCreation(gate,SPEC,HASH);
    await journal.resolveCreation(gate,SPEC,HASH,TARGET);
    expect((await journal.requireCreation(gate,SPEC,HASH,TARGET,GENERATION)).status).toBe("resolved");
    await expect(journal.requireCreation(gate,SPEC,HASH,TARGET,NEXT)).rejects.toThrow();
    await expect(journal.requireCreation(gate,SPEC,HASH,`Event/${ACCOUNT}`,GENERATION)).rejects.toThrow();
    const current={...gate,generation:NEXT};await journal.transition(gate,current);
    await expect(journal.requireCreation(gate,SPEC,HASH,TARGET,GENERATION)).rejects.toThrow();
    expect((await journal.requireCreation(current,SPEC,HASH,TARGET,GENERATION)).createdUnderGeneration).toBe(GENERATION);await journal.close();
  });
  test("verified creation receipt survives an account switch without resolving authority",async()=>{
    const [journal]=pair();await journal.transition(null,gate);await journal.reserveCreation(gate,SPEC,HASH);
    const other={...gate,accountId:OTHER,generation:NEXT};await journal.transition(gate,other);
    await journal.retainCreationReceipt(gate,SPEC,HASH,TARGET);
    const restored={...gate,generation:"cccccccc-dddd-4eee-8fff-000000000000"};await journal.transition(other,restored);
    expect(await journal.readCreation(restored,SPEC,HASH)).toMatchObject({status:"submitted",destination:TARGET});await journal.close();
  });
  test.each(["missing","fingerprint","generation"])("receipt retention requires the original reservation: %s",async kind=>{
    const [journal]=pair();await journal.transition(null,gate);if(kind!=="missing")await journal.reserveCreation(gate,SPEC,HASH);
    await expect(journal.retainCreationReceipt(kind==="generation"?{...gate,generation:NEXT}:gate,SPEC,
      kind==="fingerprint"?"b".repeat(64):HASH,TARGET)).rejects.toThrow();
    expect((await journal.readCreation(gate,SPEC,HASH))?.status??null).toBe(kind==="missing"?null:"pending");await journal.close();
  });
  test("conflicting known receipt permanently holds both immutable UUIDs before returning error",async()=>{
    const indexedDB=new IDBFactory(),journal=new V1Journal({indexedDB}),reopened=new V1Journal({indexedDB});
    await journal.transition(null,gate);await journal.reserveCreation(gate,SPEC,HASH);
    const alternate=`Event/${ACCOUNT}`;await journal.resolveCreation(gate,SPEC,HASH,alternate);
    await expect(journal.retainCreationReceipt(gate,SPEC,HASH,TARGET)).rejects.toThrow();
    for(const destination of [TARGET,alternate])await expect(journal.retainCreationReceipt(gate,SPEC,HASH,destination)).rejects.toThrow();
    await expect(journal.readCreation(gate,SPEC,HASH)).rejects.toThrow();
    await expect(journal.resolveCreation(gate,SPEC,HASH,alternate)).rejects.toThrow();await journal.close();
    await expect(reopened.readCreation(gate,SPEC,HASH)).rejects.toThrow();await reopened.close();
    const stored=await new Promise<any>((resolve,reject)=>{const request=indexedDB.open("fulcra-attention-v1");
      request.onerror=()=>reject(new Error("synthetic open failure"));request.onsuccess=()=>{
        const db=request.result,tx=db.transaction("creations","readonly"),read=tx.objectStore("creations").get([ACCOUNT,SPEC]);
        let value:unknown;read.onsuccess=()=>{value=read.result;};tx.oncomplete=()=>{db.close();resolve(value);};
      };});
    expect(stored.status).toBe("conflict");expect(stored.destination).toBe(alternate);
    expect(stored.conflictingDestinations).toEqual([alternate,TARGET]);expect(stored.createdUnderGeneration).toBe(GENERATION);
  });
  test("exact replay of retained receipt never downgrades an already resolved binding",async()=>{
    const [journal]=pair();await journal.transition(null,gate);await journal.reserveCreation(gate,SPEC,HASH);
    await journal.resolveCreation(gate,SPEC,HASH,TARGET);await journal.retainCreationReceipt(gate,SPEC,HASH,TARGET);
    expect((await journal.readCreation(gate,SPEC,HASH))?.status).toBe("resolved");await journal.close();
  });
  test.each(["reserve","submit","resolve"])("expired local %s cannot publish after an asynchronous journal read",async action=>{
    const [journal]=pair();await journal.transition(null,gate);
    if(action!=="reserve")await journal.reserveCreation(gate,SPEC,HASH);
    let now=100;const clock=vi.spyOn(performance,"now").mockImplementation(()=>now);
    try{
      const pending=action==="reserve"?journal.reserveCreation(gate,SPEC,HASH,105):action==="submit"
        ?journal.submitCreation(gate,SPEC,HASH,TARGET,105):journal.resolveCreation(gate,SPEC,HASH,TARGET,105);
      now=110;await expect(pending).rejects.toThrow();
      expect((await journal.readCreation(gate,SPEC,HASH))?.status??null).toBe(action==="reserve"?null:"pending");
    }finally{clock.mockRestore();await journal.close();}
  });
  test("a known submitted datatype UUID survives restart and cannot downgrade or retarget",async()=>{
    const [a,b]=pair();await a.transition(null,gate);await a.reserveCreation(gate,SPEC,HASH);
    await a.submitCreation(gate,SPEC,HASH,TARGET);await a.close();
    expect((await b.readCreation(gate,SPEC,HASH))?.status).toBe("submitted");
    await expect(b.submitCreation(gate,SPEC,HASH,`Event/${ACCOUNT}`)).rejects.toThrow();
    await b.resolveCreation(gate,SPEC,HASH,TARGET);await b.submitCreation(gate,SPEC,HASH,TARGET);
    expect((await b.readCreation(gate,SPEC,HASH))?.status).toBe("resolved");await b.close();
  });
  test("a fresh journal has no invented account and a restart preserves the exact gate", async () => {
    const [first, reopened] = pair();
    expect(await first.readGate()).toBeNull();
    await first.transition(null, gate);
    await first.close();
    expect(await reopened.readGate()).toEqual(gate);
    await reopened.close();
  });
  test("two contexts cannot overwrite the same gate revision", async () => {
    const [a, b] = pair();
    await a.transition(null, gate);
    const nextA = { ...gate, generation: NEXT, enabled: true };
    const nextB = { ...gate, generation: "cccccccc-dddd-4eee-8fff-000000000000", accountId: OTHER };
    const outcomes = await Promise.allSettled([a.transition(gate, nextA), b.transition(gate, nextB)]);
    expect(outcomes.filter((row) => row.status === "fulfilled")).toHaveLength(1);
    const actual = await a.readGate();
    expect([nextA, nextB]).toContainEqual(actual);
    await a.close(); await b.close();
  });
  test("no account or setting change can reuse its prior generation", async () => {
    const [a] = pair(); await a.transition(null, gate);
    await expect(a.transition(gate, { ...gate, enabled: true })).rejects.toThrow();
    await expect(a.transition(gate, { ...gate, accountId: OTHER })).rejects.toThrow();
    expect(await a.readGate()).toEqual(gate);
    await a.close();
  });
  test("the input transition is captured before opening the database", async () => {
    const [a] = pair(); const input = { ...gate };
    const pending = a.transition(null, input);
    input.accountId = OTHER;
    expect(await pending).toEqual(gate);
    expect(await a.readGate()).toEqual(gate);
    await a.close();
  });
  test("creation reservation is atomic across independent contexts", async () => {
    const [a, b] = pair(); await a.transition(null, gate);
    const outcomes = await Promise.all([a.reserveCreation(gate, SPEC, HASH), b.reserveCreation(gate, SPEC, HASH)]);
    expect(outcomes.map((row) => row.created).sort()).toEqual([false, true]);
    expect(outcomes[0].intent).toEqual(outcomes[1].intent);
    expect(outcomes[0].intent.status).toBe("pending");
    await a.close(); await b.close();
  });
  test("a possibly dispatched creation remains reserved through a restart and empty catalog", async () => {
    const [a, b] = pair(); await a.transition(null, gate);
    await a.reserveCreation(gate, SPEC, HASH); await a.close();
    const prior = await b.reserveCreation(gate, SPEC, HASH);
    expect(prior.created).toBe(false);
    expect(prior.intent.status).toBe("pending");
    await b.close();
  });
  test("another schema cannot silently replace a pending or resolved intent", async () => {
    const [a] = pair(); await a.transition(null, gate);
    await a.reserveCreation(gate, SPEC, HASH);
    await expect(a.reserveCreation(gate, SPEC, "b".repeat(64))).rejects.toThrow();
    await a.resolveCreation(gate, SPEC, HASH, TARGET);
    await expect(a.reserveCreation(gate, SPEC, "b".repeat(64))).rejects.toThrow();
    await a.close();
  });
  test("a stale callback cannot reserve or resolve against a new account generation", async () => {
    const [a, b] = pair(); await a.transition(null, gate);
    await a.reserveCreation(gate, SPEC, HASH);
    const current = { ...gate, accountId: OTHER, generation: NEXT };
    await b.transition(gate, current);
    await expect(a.reserveCreation(gate, SPEC, HASH)).rejects.toThrow();
    await expect(a.resolveCreation(gate, SPEC, HASH, TARGET)).rejects.toThrow();
    expect(await b.readCreation(current, SPEC, HASH)).toBeNull();
    await a.close(); await b.close();
  });
  test("same-account recovery can reconcile the old intent without authorizing another create", async () => {
    const [a] = pair(); await a.transition(null, gate);
    await a.reserveCreation(gate, SPEC, HASH);
    const current = { ...gate, generation: NEXT };
    await a.transition(gate, current);
    expect((await a.reserveCreation(current, SPEC, HASH)).created).toBe(false);
    const resolved = await a.resolveCreation(current, SPEC, HASH, TARGET);
    expect(resolved.status).toBe("resolved");
    expect(resolved.createdUnderGeneration).toBe(GENERATION);
    expect(await a.readCreation(current, SPEC, HASH)).toEqual(resolved);
    await a.close();
  });
  test("resolution is idempotent but cannot substitute another destination", async () => {
    const [a] = pair(); await a.transition(null, gate);
    await expect(a.resolveCreation(gate, SPEC, HASH, TARGET)).rejects.toThrow();
    await a.reserveCreation(gate, SPEC, HASH);
    const resolved = await a.resolveCreation(gate, SPEC, HASH, TARGET);
    expect(await a.resolveCreation(gate, SPEC, HASH, TARGET)).toEqual(resolved);
    await expect(a.resolveCreation(gate, SPEC, HASH, `Event/${ACCOUNT}`)).rejects.toThrow();
    expect(await a.readCreation(gate, SPEC, HASH)).toEqual(resolved);
    await a.close();
  });
  test("sign-out persists an explicit disabled gate and grants no creation authority", async () => {
    const [a] = pair(); await a.transition(null, gate);
    const signedOut = { ...gate, accountId: null, generation: NEXT };
    await a.transition(gate, signedOut);
    await expect(a.reserveCreation(signedOut, SPEC, HASH)).rejects.toThrow();
    await expect(a.transition(signedOut, { ...gate, generation: GENERATION })).rejects.toThrow();
    expect(await a.readGate()).toEqual(signedOut);
    await a.close();
  });
  test("a browser that ignores strict durability cannot publish a gate or creation reservation", async () => {
    const [a] = pair(); await a.transition(null, gate);
    const next = { ...gate, generation: NEXT, enabled: true };
    const original = IDBDatabase.prototype.transaction;
    const downgrade = vi.spyOn(IDBDatabase.prototype, "transaction").mockImplementation(function (this: IDBDatabase, names, mode) {
      return original.call(this, names, mode);
    });
    try {
      await expect(a.transition(gate, next)).rejects.toThrow();
      await expect(a.reserveCreation(gate, SPEC, HASH)).rejects.toThrow();
    } finally { downgrade.mockRestore(); }
    expect(await a.readGate()).toEqual(gate);
    expect(await a.readCreation(gate, SPEC, HASH)).toBeNull();
    expect((await a.reserveCreation(gate, SPEC, HASH)).created).toBe(true);
    expect(await a.transition(gate, next)).toEqual(next);
    await a.close();
  });
  test("quota failure rolls back both the gate and its generation reservation", async () => {
    const [a] = pair(); await a.transition(null, gate);
    const next = { ...gate, generation: NEXT, enabled: true };
    const original = IDBObjectStore.prototype.put;
    const fault = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementation(function (this: IDBObjectStore, value, key) {
      if (this.name === "gate" && key === "current") throw new DOMException("synthetic quota failure", "QuotaExceededError");
      return original.call(this, value, key);
    });
    try { await expect(a.transition(gate, next)).rejects.toThrow(); } finally { fault.mockRestore(); }
    expect(await a.readGate()).toEqual(gate);
    expect(await a.transition(gate, next)).toEqual(next);
    await a.close();
  });
  test("a committed transition replay is idempotent without restoring an old generation", async () => {
    const [a] = pair(); await a.transition(null, gate);
    const next = { ...gate, generation: NEXT };
    await a.transition(gate, next);
    expect(await a.transition(gate, next)).toEqual(next);
    await expect(a.transition(next, gate)).rejects.toThrow();
    await a.close();
  });
  test("accessors, unknown fields, malformed UUIDs and signed-out enabled state fail before persistence", async () => {
    const [a] = pair();
    let reads = 0;
    const accessor = { ...gate, get accountId() { reads += 1; return ACCOUNT; } };
    for (const input of [accessor, { ...gate, unknown: true }, { ...gate, generation: "broken" },
      { ...gate, accountId: null, enabled: true }]) {
      await expect(a.transition(null, input)).rejects.toThrow();
    }
    expect(reads).toBe(0);
    expect(await a.readGate()).toBeNull();
    await a.close();
  });
  test("a caller cannot mutate an already returned intent or gate in durable storage", async () => {
    const [a] = pair(); await a.transition(null, gate);
    const reserved = await a.reserveCreation(gate, SPEC, HASH);
    reserved.intent.destination = TARGET; reserved.intent.status = "resolved";
    const current = (await a.readGate())!; current.accountId = OTHER;
    expect((await a.readCreation(gate, SPEC, HASH))!.status).toBe("pending");
    expect(await a.readGate()).toEqual(gate);
    await a.close();
  });
  test("malformed durable state fails loudly instead of looking like a fresh journal", async () => {
    const indexedDB = new IDBFactory(); const a = new V1Journal({ indexedDB });
    await a.transition(null, gate); await a.close();
    const opened = indexedDB.open("fulcra-attention-v1");
    await new Promise<void>((resolve, reject) => {
      opened.onerror = () => reject(opened.error);
      opened.onsuccess = () => {
        const tx = opened.result.transaction("gate", "readwrite");
        tx.objectStore("gate").put({ ...gate, generation: "broken" }, "current");
        tx.oncomplete = () => { opened.result.close(); resolve(); };
      };
    });
    const b = new V1Journal({ indexedDB });
    await expect(b.readGate()).rejects.toThrow();
    await expect(b.transition(null, gate)).rejects.toThrow();
    await b.close();
  });
});
