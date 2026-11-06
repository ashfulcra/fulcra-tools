// Pure opt-in v1 encoder. Account/schema/queue/delivery authority is separate.
import contract from "../../../fulcra_attention/v1_contract.json";
import { scrubUrl } from "../scrub";
import type { AttentionEvent } from "../types";
import { sourceId } from "./wire";
import {parseV1Instant} from "./v1Time";

export interface V1WireContext {
  dataType: string;
  tagIds: string[];
  identitySlug: string;
  identityLabel?: string | null;
}

const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const EVENT_KEYS = ["url", "title", "og_description", "favicon_url", "category", "chrome_identity",
  "og_type", "lang", "start_time", "end_time", "client"];
const CONTEXT_KEYS = new Set(["dataType", "tagIds", "identitySlug", "identityLabel"]);

function text(value: unknown): value is string {
  if (typeof value !== "string") return false;
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(++i);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
    } else if (code >= 0xdc00 && code <= 0xdfff) return false;
  }
  return true;
}

function destination(value: unknown): asserts value is string {
  if (!text(value) || !value.startsWith("Event/") || !UUID.test(value.slice(6))) {
    throw new Error("Attention v1 requires a full canonical Event destination");
  }
}

/** Already-scrubbed capture key; never run a second platform URL normalizer.
 * Initial shared boundary covers ASCII DNS/IPv4 hosts and RFC3986 paths.
 * Unsupported keys must remain held, not be rewritten or dropped by a caller.
 */
function preparedURL(value: string): { url: string; host: string } {
  const bad = () => { throw new Error("Attention v1 requires a prepared capture URL"); };
  if (!/^[\x21-\x7e]+$/.test(value)) return bad();
  const match = /^(https?):\/\/([a-z0-9.-]+)(?::([0-9]+))?(\/[^?#]*)(?:\?([^#]+))?$/.exec(value);
  if (!match) return bad();
  const [, scheme, host, port, path, query] = match;
  const labels = host.split(".");
  if (host.length > 253 || labels.some((label) => label.length > 63
      || !/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label))) return bad();
  if (/^[0-9]+$/.test(labels[labels.length - 1]) || /^0x[0-9a-f]*$/.test(labels[labels.length - 1])) {
    if (labels.length !== 4 || labels.some((label) => !/^(0|[1-9][0-9]{0,2})$/.test(label)
        || Number(label) > 255)) return bad();
  }
  if (port !== undefined && (!/^(0|[1-9][0-9]{0,4})$/.test(port) || Number(port) > 65535
      || port === (scheme === "https" ? "443" : "80"))) return bad();
  if (!/^(?:[a-zA-Z0-9._~!$&'()*+,;=:@/-]|%[0-9a-fA-F]{2})*$/.test(path)
      || path.split("/").some((part) => [".", ".."].includes(part.replace(/%2e/gi, ".")))) return bad();
  if (query !== undefined) {
    for (const item of query.split("&")) {
      if (!/^(?:[a-zA-Z0-9*_.+%-])*=(?:[a-zA-Z0-9*_.+%-])*$/.test(item)) return bad();
      try { decodeURIComponent(item.replace(/\+/g, " ")); } catch { return bad(); }
    }
  }
  if (scrubUrl(value) !== value) return bad();
  return { url: value, host };
}

/** Strict, lossless browser millisecond bounds, including pre-epoch values. */
export function attentionTime(value: string): number {
  const micros=parseV1Instant(value);
  if(micros%1000n!==0n)throw new Error("Attention v1 timestamp loses millisecond precision");
  return Number(micros/1000n);
}

function iso(value: number): string {
  return new Date(value).toISOString().replace(".000Z", "Z");
}

/** UUID5 URL namespace, byte-for-byte with fulcra_common.v1.record_id_for. */
export async function recordIdFor(dataType: string, primary: string): Promise<string> {
  destination(dataType);
  if (!text(primary) || !primary.trim()) throw new Error("Attention v1 primary source is missing");
  const namespace = new Uint8Array([0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1,
    0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8]);
  const name = new TextEncoder().encode(`fulcra-tools:v1:${dataType}:${primary}`);
  const bytes = new Uint8Array(namespace.length + name.length);
  bytes.set(namespace);
  bytes.set(name, namespace.length);
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-1", bytes.buffer)).slice(0, 16);
  hash[6] = (hash[6] & 0x0f) | 0x50;
  hash[8] = (hash[8] & 0x3f) | 0x80;
  const hex = Array.from(hash, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** Capture every input synchronously before the first asynchronous digest. */
export async function buildV1Record(event: AttentionEvent, context: V1WireContext, checkEvidence?:()=>void) {
  checkEvidence?.();
  if (!event || typeof event !== "object" || Object.keys(event).length !== EVENT_KEYS.length
      || Object.keys(event).some((key) => !EVENT_KEYS.includes(key))
      || Object.getOwnPropertySymbols(event).length) throw new Error("Attention v1 event shape is unsupported");
  const captured = { ...event };
  for (const key of EVENT_KEYS) {
    const value = captured[key as keyof AttentionEvent];
    if (!text(value) && !(value === null && !["client", "start_time", "end_time"].includes(key))) {
      throw new Error("Attention v1 event contains invalid text");
    }
  }
  if (!context || typeof context !== "object" || Object.keys(context).some((key) => !CONTEXT_KEYS.has(key))
      || Object.getOwnPropertySymbols(context).length) throw new Error("Attention v1 context shape is unsupported");
  const dataType = context.dataType;
  destination(dataType);
  const identitySlug = context.identitySlug;
  const identityLabel = context.identityLabel ?? null;
  if (!text(identitySlug) || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(identitySlug)
      || (identityLabel !== null && !text(identityLabel))) throw new Error("Attention v1 installation identity is invalid");
  if (!Array.isArray(context.tagIds)) throw new Error("Attention v1 tag identities are invalid");
  const inputTags = Array.from(context.tagIds);
  if (inputTags.some((tag) => !text(tag) || !UUID.test(tag))) throw new Error("Attention v1 tag identities are invalid");
  const tags = [...new Set(inputTags)];
  const start = attentionTime(captured.start_time), end = attentionTime(captured.end_time);
  if (end < start || !captured.client.trim() || (captured.url === null) === (captured.category === null)) {
    throw new Error("Attention v1 event bounds/variant are invalid");
  }
  let url: string | null = null, host: string | null = null;
  if (captured.url !== null) {
    try {
      ({ url, host } = preparedURL(captured.url));
    } catch { throw new Error("Attention v1 URL is invalid"); }
  } else if (!captured.category?.trim()) throw new Error("Attention v1 category is empty");
  checkEvidence?.();const primary = await sourceId(url ?? captured.category!, iso(start), identitySlug);checkEvidence?.();
  checkEvidence?.();const identifier = await recordIdFor(dataType, primary);checkEvidence?.();
  const external = Object.freeze({ client: captured.client, host, chrome_identity: captured.chrome_identity,
    og_type: captured.og_type, lang: captured.lang, device: identitySlug, device_label: identityLabel });
  const record = Object.freeze({ id: identifier, start_time: iso(start), end_time: iso(end),
    sources: Object.freeze([primary]), tags: Object.freeze(tags),
    note: url !== null ? (captured.title ? `${captured.title} — ${url}` : url) : `Attention: ${captured.category}`,
    title: captured.title, service: "web", category: captured.category, url,
    og_description: captured.og_description, favicon_url: captured.favicon_url, parent_source_id: null,
    duration_seconds: (end - start) / 1000, external_ids: external });
  return Object.freeze({ record, sourceId: primary });
}

const contractJSON = JSON.stringify(contract);
export function attentionV1Contract(): typeof contract {
  return JSON.parse(contractJSON) as typeof contract;
}
