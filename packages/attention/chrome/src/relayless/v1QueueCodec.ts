// Pure immutable queue encoding/reproduction. No current gate, provider proof,
// transport or settlement authority. Preserve original captured generations.
import type { V1Gate, V1OutboxRow } from "./v1Journal";
import { snapshotV1JSON } from "./v1Http";
import { buildV1Record, type V1WireContext } from "./v1Wire";
import { attentionV1Fingerprint, proveAttentionV1Schema, proveAttentionV1InputSchema } from "./v1AttentionSpec";
import type { ResolvedAttentionV1 } from "./v1Destination";
import type { AttentionEvent } from "../types";
import { sha256Hex } from "./sha256";
const LIMIT=4*1024*1024;
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
export interface Capture {scope:V1Gate;binding:ResolvedAttentionV1;event:AttentionEvent;context:V1WireContext;}
export interface V1QueuedAttention extends Capture {
 readonly version:1;readonly primary:string;
 readonly record:Awaited<ReturnType<typeof buildV1Record>>["record"];
 readonly payloadText:string;readonly payloadHash:string;
}
function refuse():never {throw new Error("Attention v1 queued capture cannot be verified; retain the original data");}
function exact(value:unknown,names:string[]):value is Record<string,unknown> {
 return !!value&&typeof value==="object"&&!Array.isArray(value)&&Object.keys(value).length===names.length&&names.every(key=>Object.hasOwn(value,key));
}
function frozen<T>(value:T):T {
 if(value&&typeof value==="object"){for(const child of Object.values(value))frozen(child);Object.freeze(value);}return value;
}
export function captureQueueScope(value:unknown):V1Gate {
 const scope=snapshotV1JSON(value,1024) as V1Gate;
 if(!exact(scope,["version","accountId","generation","enabled"])||scope.version!==1||typeof scope.accountId!=="string"
  ||!UUID.test(scope.accountId)||typeof scope.generation!=="string"||!UUID.test(scope.generation)||typeof scope.enabled!=="boolean")refuse();
 return scope;
}
/** All caller-owned JSON is detached before the first cryptographic await. */
export async function prepareQueuedAttention(input:Capture,checkEvidence?:()=>void):Promise<Readonly<V1QueuedAttention>> {
 checkEvidence?.();
 const captured=snapshotV1JSON(input,LIMIT) as Capture;
 if(!exact(captured,["scope","binding","event","context"]))refuse();
 const scope=captureQueueScope(captured.scope),binding=captured.binding,context=captured.context;
 if(!scope.enabled||!exact(binding,["accountId","generation","dataType","specFingerprint","schema","schemaText","schemaHash","inputSchemaText","inputSchemaHash"])
  ||binding.accountId!==scope.accountId||binding.generation!==scope.generation
  ||!exact(context,context.identityLabel===undefined?["dataType","tagIds","identitySlug"]:["dataType","tagIds","identitySlug","identityLabel"])
  ||context.dataType!==binding.dataType||!Array.isArray(context.tagIds)||context.tagIds.length>128||new Set(context.tagIds).size!==context.tagIds.length
  ||new TextEncoder().encode(JSON.stringify(captured.event)).length>65536)refuse();
 // The encoder snapshots its inputs synchronously before awaiting source IDs.
 checkEvidence?.();const built=await buildV1Record(captured.event,context,checkEvidence);checkEvidence?.();
 checkEvidence?.();const fingerprint=await attentionV1Fingerprint();checkEvidence?.();
 if(binding.specFingerprint!==fingerprint)refuse();
 checkEvidence?.();const proof=await proveAttentionV1Schema(binding.schemaText);checkEvidence?.();
 checkEvidence?.();const inputProof=await proveAttentionV1InputSchema(binding.inputSchemaText,checkEvidence);checkEvidence?.();
 if(proof.schemaHash!==binding.schemaHash||inputProof.inputSchemaHash!==binding.inputSchemaHash
  ||JSON.stringify(binding.schema)!==JSON.stringify(proof.schema))refuse();
 checkEvidence?.();const payloadText=JSON.stringify(built.record),payloadHash=await sha256Hex(payloadText);checkEvidence?.();
 return frozen({version:1,scope,binding,event:captured.event,context,primary:built.sourceId,record:built.record,payloadText,payloadHash});
}
export async function queuedRowFor(entry:Readonly<V1QueuedAttention>,checkEvidence?:()=>void):Promise<V1OutboxRow> {
 checkEvidence?.();
 const text=JSON.stringify(entry),bytes=new TextEncoder().encode(text).length;if(bytes>LIMIT)refuse();
 checkEvidence?.();const row:V1OutboxRow={version:1,accountId:entry.scope.accountId!,generation:entry.scope.generation,dataType:entry.binding.dataType,
  recordId:entry.record.id,primary:entry.primary,specFingerprint:entry.binding.specFingerprint,tagIds:[...entry.record.tags],
  text,hash:await sha256Hex(text),bytes};
 checkEvidence?.();return row;
}
/** Reproduce every originally captured field/hash without consulting or
 * changing current authority. Callers must separately recheck gate and rows. */
export async function decodeQueuedAttentionRow(row:V1OutboxRow,checkEvidence?:()=>void):Promise<Readonly<V1QueuedAttention>> {
  checkEvidence?.();const hash=await sha256Hex(row.text);checkEvidence?.();if(hash!==row.hash)refuse();
  let parsed:unknown;try{parsed=JSON.parse(row.text);}catch{return refuse();}
  const stored=snapshotV1JSON(parsed,LIMIT) as V1QueuedAttention;
  if(!exact(stored,["version","scope","binding","event","context","primary","record","payloadText","payloadHash"])||stored.version!==1)refuse();
  checkEvidence?.();const reproduced=await prepareQueuedAttention({scope:stored.scope,binding:stored.binding,event:stored.event,context:stored.context},checkEvidence);checkEvidence?.();
  checkEvidence?.();const reproducedRow=await queuedRowFor(reproduced,checkEvidence);checkEvidence?.();if(JSON.stringify(reproducedRow)!==JSON.stringify(row))refuse();
  return reproduced;
}
