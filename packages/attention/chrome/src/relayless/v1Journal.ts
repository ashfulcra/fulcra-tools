// Opt-in IndexedDB authority/creation journal. No auth, HTTP or default sender
// wiring here. All mutations compare the persisted gate in the same transaction.
import {runV1Read,openExistingDatabase,readTransactionBounded,type V1ReadJob,type V1ReadLimits,type ReadResult} from "./v1ReadBudget";
import {snapshotV1JSON,parseV1JSON} from "./v1Http";
import {consumeV1ActionObservation,type V1ActionObservation,type V1ActionSettlement} from "./v1ActionActor";
import {sha256Hex} from "./sha256";
import {effectPlans,phasedRequestLimit,referenceFor,referencedBody,PHASE_REQUEST_LIMIT,INLINE_BODY_LIMIT,type EffectPlan} from "./v1EffectBodies";
export interface V1Gate {
  version: 1;
  accountId: string | null;
  generation: string;
  enabled: boolean;
}
export interface V1CreationIntent {
  version: 1;
  accountId: string;
  specKey: string;
  fingerprint: string;
  status: "pending" | "submitted" | "resolved";
  createdUnderGeneration: string;
  destination: string | null;
}
export interface V1TagIntent {
  version: 1;
  accountId: string;
  label: string;
  name: string;
  status: "pending" | "submitted" | "resolved";
  createdUnderGeneration: string;
  destination: string | null;
}

/** Opaque encoded immutable envelope; semantic proof is rechecked by V1Outbox. */
export interface V1OutboxRow {
  version: 1; accountId: string; generation: string; dataType: string; recordId: string;
  primary: string; specFingerprint: string; tagIds: string[]; text: string; hash: string; bytes: number;
}
const OUTBOX_KEYS = ["version", "accountId", "generation", "dataType", "recordId", "primary", "specFingerprint", "tagIds", "text", "hash", "bytes"];
function eventType(value: unknown): value is string {
  return typeof value === "string" && value.startsWith("Event/") && uuid(value.slice(6));
}
function queueTags(value: unknown): value is string[] {
  return Array.isArray(value) && value.length <= 128 && Object.getOwnPropertySymbols(value).length === 0
    && Object.getOwnPropertyNames(value).length === value.length + 1
    && Array.from({ length: value.length }, (_, i) => Object.getOwnPropertyDescriptor(value, String(i)))
      .every(d => d?.enumerable && Object.hasOwn(d, "value") && uuid(d.value))
    && new Set(value).size === value.length;
}
function outboxSnapshot(value: unknown): V1OutboxRow {
  if (!keys(value, OUTBOX_KEYS) || value.version !== 1 || !uuid(value.accountId) || !uuid(value.generation)
      || !eventType(value.dataType) || !uuid(value.recordId) || typeof value.primary !== "string"
      || !value.primary || value.primary.length > 1024 || typeof value.specFingerprint !== "string" || !HASH.test(value.specFingerprint)
      || !queueTags(value.tagIds) || typeof value.hash !== "string" || !HASH.test(value.hash)
      || typeof value.text !== "string" || value.text.length > 4 * 1024 * 1024
      || !Number.isSafeInteger(value.bytes) || value.bytes !== new TextEncoder().encode(value.text).length
      || (value.bytes as number) > 4 * 1024 * 1024) throw new Error("Attention v1 queued envelope is unreadable");
  return { version: 1, accountId: value.accountId, generation: value.generation, dataType: value.dataType,
    recordId: value.recordId, primary: value.primary, specFingerprint: value.specFingerprint, tagIds: [...value.tagIds],
    text: value.text, hash: value.hash, bytes: value.bytes as number };
}
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const HASH = /^[a-f0-9]{64}$/;
const SPEC = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const FORMAT = { version: 1, initialized: false };
const GATE_KEYS = ["version", "accountId", "generation", "enabled"];
const INTENT_KEYS = ["version", "accountId", "specKey", "fingerprint", "status", "createdUnderGeneration", "destination"];
const TAG_KEYS = ["version", "accountId", "label", "name", "status", "createdUnderGeneration", "destination"];
function tagText(value: unknown, max: number): value is string {
  if (typeof value !== "string" || value.length > max * 2 || !value.trim() || [...value].length > max || /[\x00-\x1f\x7f]/.test(value)) return false;
  for (const point of value) {
    const code = point.codePointAt(0)!;
    if (code >= 0xd800 && code <= 0xdfff) return false;
  }
  return true;
}
function tagInput(scope: V1Gate, label: string, name: string): V1Gate {
  const captured = gateSnapshot(scope);
  if (!captured.accountId || !tagText(label, 1024) || !tagText(name, 30) || name.includes("/")) {
    throw new Error("Attention v1 tag scope is invalid");
  }
  return captured;
}
function tagSnapshot(value: unknown, account: string, label: string, name: string): V1TagIntent {
  if (!keys(value, TAG_KEYS) || value.version !== 1 || value.accountId !== account || value.label !== label
      || value.name !== name || !uuid(value.createdUnderGeneration)
      || !["pending", "submitted", "resolved"].includes(value.status as string)
      || (value.status === "pending" ? value.destination !== null : !uuid(value.destination))) {
    throw new Error("Attention v1 tag intent differs or is unreadable");
  }
  return { ...value } as unknown as V1TagIntent;
}
function keys(value: unknown, expected: string[]): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && [Object.prototype, null].includes(Object.getPrototypeOf(value))
    && Object.keys(value).length === expected.length && expected.every((key) => {
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      return descriptor?.enumerable === true && Object.hasOwn(descriptor, "value");
    })
    && Object.getOwnPropertySymbols(value).length === 0;
}
function uuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}
function gateSnapshot(value: unknown): V1Gate {
  if (!keys(value, GATE_KEYS) || value.version !== 1 || !uuid(value.generation)
      || !(value.accountId === null || uuid(value.accountId)) || typeof value.enabled !== "boolean"
      || (value.accountId === null && value.enabled)) throw new Error("Attention v1 account gate is unreadable");
  return { version: 1, accountId: value.accountId, generation: value.generation, enabled: value.enabled };
}
function sameGate(left: V1Gate | null, right: V1Gate | null): boolean {
  return left === null || right === null ? left === right
    : left.accountId === right.accountId && left.generation === right.generation && left.enabled === right.enabled;
}
function specInput(scope: V1Gate, key: string, hash: string): [V1Gate, string, string] {
  const captured = gateSnapshot(scope);
  if (!captured.accountId || typeof key !== "string" || !SPEC.test(key)
      || typeof hash !== "string" || !HASH.test(hash)) throw new Error("Attention v1 creation scope is invalid");
  return [captured, key, hash];
}
function checkCreationDeadline(deadline?:number,signal?:AbortSignal):void {
  if(signal?.aborted)throw new Error("Attention v1 creation proof was cancelled");
  if(deadline===undefined)return;
  const now=performance.now();
  if(!Number.isFinite(deadline)||!Number.isFinite(now)||now>=deadline)throw new Error("Attention v1 creation proof budget expired");
}
function intentSnapshot(value: unknown, account: string, key: string, hash: string): V1CreationIntent {
  if (!keys(value, INTENT_KEYS) || value.version !== 1 || value.accountId !== account || value.specKey !== key
      || value.fingerprint !== hash || !uuid(value.createdUnderGeneration)
      || !["pending", "submitted", "resolved"].includes(value.status as string)
      || (value.status === "pending" ? value.destination !== null
        : typeof value.destination !== "string" || !value.destination.startsWith("Event/")
          || !uuid(value.destination.slice(6)))) throw new Error("Attention v1 creation intent differs or is unreadable");
  return { ...value } as unknown as V1CreationIntent;
}


/** Network-free logical ownership. Caller evidence is not remote proof or a
 * dispatch capability. Physical-effect actors must provide their own phases. */
export interface V1MutationIntent {
 version:1|2;accountId:string;generation:string;actionId:string;operation:"deliver"|"exclude";
 requestText:string;requestHash:string;requestBytes:number;status:"prepared"|"issued"|"verified";
 proofText:string|null;proofHash:string|null;proofBytes:number;ordinal:number;cumulativeBytes:number;
 effects?:V1EffectState[];
}
export interface V1EffectState {name:string;status:"prepared"|"issued"|"verified";proofText:string|null;proofHash:string|null;proofBytes:number;}
export interface V1PhasedMutation extends V1MutationIntent {version:2;effects:V1EffectState[];}
export interface V1EffectInput {name:string;destination:string;records:unknown[];}
export interface V1DispatchAuthority {creations:V1CreationIntent[];tags:V1TagIntent[];}
function dispatchAuthority(scope:V1Gate,value:unknown):V1DispatchAuthority {
 const captured=snapshotV1JSON(value,65536);
 if(!keys(captured,["creations","tags"])||!Array.isArray(captured.creations)||captured.creations.length<1||captured.creations.length>2
  ||!Array.isArray(captured.tags)||captured.tags.length>128||!scope.accountId)mutationFail();
 const creations=captured.creations.map(raw=>{
  if(!keys(raw,INTENT_KEYS)||raw.accountId!==scope.accountId)mutationFail();
  specInput(scope,raw.specKey as string,raw.fingerprint as string);
  const row=intentSnapshot(raw,scope.accountId!,raw.specKey as string,raw.fingerprint as string);if(row.status!=="resolved")mutationFail();
  return {version:row.version,accountId:row.accountId,specKey:row.specKey,fingerprint:row.fingerprint,status:row.status,createdUnderGeneration:row.createdUnderGeneration,destination:row.destination};
 }).sort((a,b)=>a.specKey<b.specKey?-1:a.specKey>b.specKey?1:0);
 const tags=captured.tags.map(raw=>{
  if(!keys(raw,TAG_KEYS)||raw.accountId!==scope.accountId)mutationFail();tagInput(scope,raw.label as string,raw.name as string);
  const row=tagSnapshot(raw,scope.accountId!,raw.label as string,raw.name as string);if(row.status!=="resolved")mutationFail();
  return {version:row.version,accountId:row.accountId,label:row.label,name:row.name,status:row.status,createdUnderGeneration:row.createdUnderGeneration,destination:row.destination};
 }).sort((a,b)=>a.label<b.label?-1:a.label>b.label?1:0);
 for(const [rows,key]of [[creations,"specKey"],[tags,"label"],[tags,"name"]] as const){if(new Set(rows.map(row=>(row as unknown as Record<string,unknown>)[key])).size!==rows.length)mutationFail();}
 return {creations,tags};
}
function originalDispatchAuthority(scope:V1Gate,original:V1MutationIntent,name:string,value:unknown):V1DispatchAuthority {
 const guards=dispatchAuthority(scope,value),request=parseV1JSON(original.requestText) as {details?:{dispatchAuthority?:unknown}};
 if(JSON.stringify(dispatchAuthority(scope,request.details?.dispatchAuthority))!==JSON.stringify(guards))mutationFail();
 const plan=effectPlans(original).find(plan=>plan.name===name);
 // Native phases retain the same original target/control predicates. Their
 // semantic target/body and current provider truth remain the typed actor's job.
 if(!plan||plan.destination!=="DeletedRecord"&&!guards.creations.some(row=>row.destination===plan.destination))mutationFail();
 return guards;
}
function readDispatchPredicates(tx:IDBTransaction,read:<V>(request:IDBRequest<V>,next:(value:V)=>void)=>void,guards:V1DispatchAuthority,finish:()=>void):void {
 const predicates=[...guards.creations.map(row=>({store:"creations",key:[row.accountId,row.specKey],row})),...guards.tags.map(row=>({store:"tags",key:[row.accountId,row.label],row}))];
 const check=(index:number)=>{
  if(index===predicates.length){finish();return;}
  const predicate=predicates[index];
  read(tx.objectStore(predicate.store).get(predicate.key),value=>{
   const expected=predicate.row;
   const found=predicate.store==="creations"?intentSnapshot(value,expected.accountId,(expected as V1CreationIntent).specKey,(expected as V1CreationIntent).fingerprint)
    :tagSnapshot(value,expected.accountId,(expected as V1TagIntent).label,(expected as V1TagIntent).name);
   if(Object.keys(expected).some(key=>(found as unknown as Record<string,unknown>)[key]!==expected[key as keyof typeof expected]))mutationFail();
   check(index+1);
  });
 };check(0);
}
export interface MutationPointer {
 version:1|2;accountId:string;generation:string;actionId:string;requestHash:string;
 status:V1MutationIntent["status"];proofHash:string|null;totalActions:number;totalBytes:number;
 effects?:{name:string;status:V1EffectState["status"];proofHash:string|null}[];
}
export interface OwnerEvidence {
 status:'known'|'unknown';observedGate:Readonly<V1Gate>|null;
 owner:Readonly<{scope:Readonly<{accountId:string;generation:string}>;intent:Readonly<V1MutationIntent>}>|null;
 terminalPointer:Readonly<MutationPointer>|null;reasons:readonly string[];
}
interface MutationState {pointer:MutationPointer|null;current:V1MutationIntent|null;entry:V1MutationIntent|null;count:number;bodies:Record<string,V1OutboxRow>;}
const MUTATION_LIMIT=65536,MUTATION_BYTES=32*1024*1024,MUTATION_COUNT=100000;
const MUTATION_KEYS=["version","accountId","generation","actionId","operation","requestText","requestHash","requestBytes","status","proofText","proofHash","proofBytes","ordinal","cumulativeBytes"];
const POINTER_KEYS=["version","accountId","generation","actionId","requestHash","status","proofHash","totalActions","totalBytes"];
function mutationFail():never {throw new Error("Attention v1 mutation ownership is unavailable; retain original work");}
function mutationDeadline(deadline?:number,signal?:AbortSignal):void {if(signal?.aborted)mutationFail();if(deadline!==undefined){const now=performance.now();if(!Number.isFinite(deadline)||!Number.isFinite(now)||now>=deadline)mutationFail();}}
function mutationJSON(value:unknown,limit=MUTATION_LIMIT):string {
 const copy=snapshotV1JSON(value,limit);
 if(!copy||typeof copy!=="object"||Array.isArray(copy))mutationFail();
 return JSON.stringify(copy);
}
function mutationProof(value:unknown,operation:V1MutationIntent["operation"]):string {
 const text=mutationJSON(value),proof=JSON.parse(text);
 if(!keys(proof,["version","outcome","evidence"])||proof.version!==1||!proof.evidence||typeof proof.evidence!=="object"||Array.isArray(proof.evidence)
  ||!(operation==="deliver"?["delivered","excluded"]:["deleted","already-excluded"]).includes(proof.outcome as string))mutationFail();
 return text;
}
const SETTLEMENT="collect-v1-atomic-settlement";
function reservedSettlement(proof:unknown):boolean {
 return !!proof&&typeof proof==='object'&&'evidence' in proof&&!!proof.evidence&&typeof proof.evidence==='object'&&Object.hasOwn(proof.evidence,'settlement');
}
function stableSettlement(value:unknown):string {
 if(Array.isArray(value))return '['+value.map(stableSettlement).join(',')+']';
 if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+stableSettlement((value as Record<string,unknown>)[key])).join(',')+'}';
 return JSON.stringify(value);
}
/** Closed persisted receipt validator; its digests are re-proved by withMutation. */
function settlementEvidence(row:V1MutationIntent):Record<string,any>|null {
 if(row.status!=="verified"||!row.proofText)return null;
 const proof=parseV1JSON(row.proofText) as any;if(!reservedSettlement(proof))return null;
 const e=proof.evidence;
 if(row.version!==2||!keys(e,['settlement','version','accountId','generation','actionId','operation','requestHash','bodyHash','targetDataType','targetRecordId','controlDataType','dispatchAuthority','phaseProofs','currentEvidence'])
  ||e.settlement!==SETTLEMENT||e.version!==1||['accountId','generation','actionId','operation','requestHash'].some(key=>e[key]!==row[key as keyof V1MutationIntent]))mutationFail();
 const plans=effectPlans(row),first=plans[0],policy=row.operation==='exclude'?parseV1JSON(first.bodyText! ) as any:null;
 if(row.operation==='deliver'&&!first.bodyRef||e.bodyHash!==first.bodyHash||e.targetDataType!==(first.bodyRef?.dataType??policy?.target_data_type)
  ||e.targetRecordId!==(first.bodyRef?.recordId??policy?.target_record_id)||!uuid(e.targetRecordId))mutationFail();
 const guards=originalDispatchAuthority({version:1,enabled:true,accountId:row.accountId,generation:row.generation},row,first.name,e.dispatchAuthority);
 if(!guards.creations.some(c=>c.specKey==='collect.record-exclusions.v1'&&c.destination===e.controlDataType))mutationFail();
 const current=e.currentEvidence;
 const schema=(v:unknown)=>keys(v,['schemaHash','inputSchemaHash'])&&typeof v.schemaHash==='string'&&HASH.test(v.schemaHash)&&typeof v.inputSchemaHash==='string'&&HASH.test(v.inputSchemaHash);
 if(!keys(current,['target','control','nativeSchemaHash','policyHash'])||!schema(current.target)||!schema(current.control)||typeof current.nativeSchemaHash!=='string'||!HASH.test(current.nativeSchemaHash)
  ||!(current.policyHash===null||typeof current.policyHash==='string'&&HASH.test(current.policyHash))||!Array.isArray(e.phaseProofs)||e.phaseProofs.length!==2)mutationFail();
 if(proof.outcome!==(row.operation==='exclude'?'deleted':current.policyHash?'excluded':'delivered')||row.operation==='exclude'&&!current.policyHash)mutationFail();
 for(let i=0;i<2;i++){
  const p=e.phaseProofs[i],context=p?.evidence?.context,pe=p?.evidence;
  if(JSON.stringify(p)!==row.effects?.[i].proofText||!keys(p,['version','outcome','evidence'])||p.version!==1
   ||!keys(pe,['actor','version','context','target','control','policyHash','nativeSchemaHash'])||pe.actor!=='collect-v1-action-actor'||pe.version!==1
   ||!keys(context,['accountId','generation','actionId','operation','requestHash','phase','targetDataType','targetRecordId','controlDataType','authorityHash','payloadHash'])
   ||['accountId','generation','actionId','operation','requestHash','targetDataType','targetRecordId','controlDataType'].some(key=>context[key]!==e[key])
   ||context.phase!==plans[i].name||context.payloadHash!==plans[i].bodyHash||typeof context.authorityHash!=='string'||!HASH.test(context.authorityHash)
   ||stableSettlement(pe.target)!==stableSettlement(current.target)||stableSettlement(pe.control)!==stableSettlement(current.control)||pe.nativeSchemaHash!==current.nativeSchemaHash)mutationFail();
  if(i===1){if(p.outcome!==(current.policyHash?'absent':'not-needed')||pe.policyHash!==current.policyHash)mutationFail();}
  else if(row.operation==='deliver'){
   if(p.outcome==='present'){if(pe.policyHash!==null)mutationFail();}
   else if(p.outcome!=='already-excluded'||!current.policyHash||pe.policyHash!==current.policyHash)mutationFail();
  }else if(!['present','already-excluded'].includes(p.outcome as string)||pe.policyHash!==current.policyHash)mutationFail();
 }
 return e;
}
function freezeMutation<T>(value:T):T {
 if(value&&typeof value==="object"){for(const child of Object.values(value))freezeMutation(child);Object.freeze(value);}return value;
}
function effectProof(value:unknown):string {
 const text=mutationJSON(value),proof=JSON.parse(text);
 if(!keys(proof,["version","outcome","evidence"])||proof.version!==1||!proof.evidence||typeof proof.evidence!=="object"||Array.isArray(proof.evidence)
  ||typeof proof.outcome!=="string"||!["present","absent","already-excluded","not-needed"].includes(proof.outcome))mutationFail();
 return text;
}
function effectDigest(row:V1MutationIntent,index:number,plan:EffectPlan,text:string):Promise<string> {
 return sha256Hex(JSON.stringify([row.requestHash,index,plan.name,plan.destination,plan.bodyHash,text]));
}
function effectStates(row:V1MutationIntent):V1EffectState[] {
 if(row.version===1)return [];
 const plans=effectPlans(row),effects=row.effects;
 for(const plan of plans)if(plan.bodyRef&&(plan.bodyRef.accountId!==row.accountId||plan.bodyRef.generation!==row.generation))mutationFail();
 if(!Array.isArray(effects)||effects.length!==plans.length)mutationFail();
 let unfinished=false,issued=0;
 for(let i=0;i<effects.length;i++){
  const state=effects[i];
  if(!keys(state,["name","status","proofText","proofHash","proofBytes"])||state.name!==plans[i].name||!["prepared","issued","verified"].includes(state.status))mutationFail();
  if(state.status==="verified"){
   if(unfinished||typeof state.proofText!=="string"||typeof state.proofHash!=="string"||!HASH.test(state.proofHash)
    ||!Number.isSafeInteger(state.proofBytes)||state.proofBytes!==new TextEncoder().encode(state.proofText).length||state.proofBytes>MUTATION_LIMIT
    ||effectProof(parseV1JSON(state.proofText))!==state.proofText)mutationFail();
  }else{
   if(state.proofText!==null||state.proofHash!==null||state.proofBytes!==0||state.status==="issued"&&(unfinished||++issued>1))mutationFail();
   unfinished=true;
  }
 }
 if(row.status==="prepared"&&issued||row.status==="verified"&&unfinished)mutationFail();
 return effects;
}
function mutationSnapshot(value:unknown):V1MutationIntent {
 const version=value&&typeof value==="object"?Object.getOwnPropertyDescriptor(value,"version")?.value:null;
 const expected=version===2?[...MUTATION_KEYS,"effects"]:MUTATION_KEYS;
 if(!keys(value,expected)||Object.getOwnPropertyNames(value).length!==expected.length)mutationFail();
 // Legacy fields are all primitives. Detach only the new nested state instead
 // of rescanning every large immutable request/proof string a second time.
 value={...value,...version===2?{effects:snapshotV1JSON(value.effects,512*1024)}:{}};
 if(!keys(value,expected)||![1,2].includes(value.version as number)||!uuid(value.accountId)||!uuid(value.generation)||!uuid(value.actionId)
  ||!["deliver","exclude"].includes(value.operation as string)||!["prepared","issued","verified"].includes(value.status as string)
  ||typeof value.requestText!=="string"||value.requestText.length>(value.version===2?PHASE_REQUEST_LIMIT:MUTATION_LIMIT)||typeof value.requestHash!=="string"||!HASH.test(value.requestHash)||!Number.isSafeInteger(value.requestBytes)
  ||value.requestBytes!==new TextEncoder().encode(value.requestText).length||(value.requestBytes as number)>(value.version===2?PHASE_REQUEST_LIMIT:MUTATION_LIMIT)
  ||!Number.isSafeInteger(value.ordinal)||(value.ordinal as number)<1||(value.ordinal as number)>MUTATION_COUNT
  ||!Number.isSafeInteger(value.cumulativeBytes)||(value.cumulativeBytes as number)>MUTATION_BYTES)mutationFail();
 if(mutationJSON(parseV1JSON(value.requestText),value.version===2?phasedRequestLimit(value.requestText):MUTATION_LIMIT)!==value.requestText)mutationFail();
 if(value.status==="verified") {
  if(typeof value.proofText!=="string"||value.proofText.length>MUTATION_LIMIT||typeof value.proofHash!=="string"||!HASH.test(value.proofHash)||!Number.isSafeInteger(value.proofBytes)
   ||value.proofBytes!==new TextEncoder().encode(value.proofText).length||(value.proofBytes as number)>MUTATION_LIMIT
   ||mutationProof(parseV1JSON(value.proofText),value.operation as V1MutationIntent["operation"])!==value.proofText)mutationFail();
 }else if(value.proofText!==null||value.proofHash!==null||value.proofBytes!==0)mutationFail();
 const row=value as unknown as V1MutationIntent,phaseBytes=effectStates(row).reduce((sum,e)=>sum+e.proofBytes,0);
 if(row.cumulativeBytes<row.requestBytes+row.proofBytes+phaseBytes)mutationFail();
 settlementEvidence(row);
 return row;
}
function pointerSnapshot(value:unknown,account:string):MutationPointer {
 if(!keys(value,typeof value==="object"&&value!==null&&"version" in value&&value.version===2?[...POINTER_KEYS,"effects"]:POINTER_KEYS)||![1,2].includes(value.version as number)||value.accountId!==account||!uuid(value.generation)||!uuid(value.actionId)
  ||typeof value.requestHash!=="string"||!HASH.test(value.requestHash)||!["prepared","issued","verified"].includes(value.status as string)
  ||(value.status==="verified"?(typeof value.proofHash!=="string"||!HASH.test(value.proofHash)):value.proofHash!==null)
  ||!Number.isSafeInteger(value.totalActions)||(value.totalActions as number)<1||(value.totalActions as number)>MUTATION_COUNT
  ||!Number.isSafeInteger(value.totalBytes)||(value.totalBytes as number)<1||(value.totalBytes as number)>MUTATION_BYTES)mutationFail();
 if(value.version===2){
  const effects=snapshotV1JSON(value.effects,2048);
  if(!Array.isArray(effects)||effects.length!==2)mutationFail();
  for(let i=0;i<effects.length;i++){
   const effect=effects[i];
   if(!keys(effect,["name","status","proofHash"])||!(i===0?["deliver","policy"]:["repair","delete"]).includes(effect.name as string)
    ||!["prepared","issued","verified"].includes(effect.status as string)
    ||(effect.status==="verified"?(typeof effect.proofHash!=="string"||!HASH.test(effect.proofHash)):effect.proofHash!==null))mutationFail();
  }
  return {...value,effects} as unknown as MutationPointer;
 }
 return {...value} as unknown as MutationPointer;
}
function mutationPointer(row:V1MutationIntent):MutationPointer {return {version:row.version,accountId:row.accountId,generation:row.generation,actionId:row.actionId,
 requestHash:row.requestHash,status:row.status,proofHash:row.proofHash,totalActions:row.ordinal,totalBytes:row.cumulativeBytes,...row.version===2?{effects:effectStates(row).map(e=>({name:e.name,status:e.status,proofHash:e.proofHash}))}:{}};}
function sameMutationRequest(left:V1MutationIntent,right:V1MutationIntent):boolean {return ["version","accountId","generation","actionId","operation","requestText","requestHash","requestBytes","ordinal"]
 .every(key=>left[key as keyof V1MutationIntent]===right[key as keyof V1MutationIntent]);}
// Bind the entire immutable request identity, including its operation. Body-only
// hashes let a corrupted stored operation acquire different authority on restart.
function mutationRequestHash(row:Pick<V1MutationIntent,"version"|"accountId"|"generation"|"actionId"|"operation"|"requestText"|"requestBytes">):Promise<string> {
 return sha256Hex(JSON.stringify([row.version,row.accountId,row.generation,row.actionId,row.operation,row.requestText,row.requestBytes]));
}
async function mutationDigest(row:V1MutationIntent|null,bodies:Record<string,V1OutboxRow>={},requireBody=true,checkEvidence?:()=>void):Promise<void> {
 checkEvidence?.();if(!row)return;
 const settlement=settlementEvidence(row);checkEvidence?.();
 if(settlement){const authorityHash=await sha256Hex(stableSettlement(settlement.dispatchAuthority));checkEvidence?.();if(settlement.phaseProofs.some((p:any)=>p.evidence.context.authorityHash!==authorityHash))mutationFail();}
 checkEvidence?.();const requestHash=await mutationRequestHash(row);checkEvidence?.();
 if(requestHash!==row.requestHash)mutationFail();
 if(row.proofText!==null){checkEvidence?.();const proofHash=await sha256Hex(row.proofText);checkEvidence?.();if(proofHash!==row.proofHash)mutationFail();}
 if(row.version===2){const plans=effectPlans(row),states=effectStates(row);checkEvidence?.();
  for(let i=0;i<plans.length;i++){
   checkEvidence?.();const plan=plans[i];
   if(plan.bodyRef){if(requireBody&&row.status!=="verified"){
    const body=bodies[row.actionId];if(!body)mutationFail();await referencedBody(row,plan,body,checkEvidence);checkEvidence?.();
   }}else {const bodyHash=await sha256Hex(plan.bodyText!);checkEvidence?.();if(bodyHash!==plan.bodyHash)mutationFail();}
   if(states[i].proofText!==null){checkEvidence?.();const proofHash=await effectDigest(row,i,plan,states[i].proofText!);checkEvidence?.();if(proofHash!==states[i].proofHash)mutationFail();}
  }
 }
}

class MutationChanged extends Error {}
function storeShape(store:IDBObjectStore,indexes:string[]=[]):void {
 if(store.keyPath!==null||store.autoIncrement||JSON.stringify([...store.indexNames].sort())!==JSON.stringify([...indexes].sort()))mutationFail();
}
function oldShapes(db:IDBDatabase,tx:IDBTransaction,version:number):void {
 for(const name of ["gate","generations","creations"]){if(!db.objectStoreNames.contains(name))mutationFail();storeShape(tx.objectStore(name));}
 if(version>=2){const tags=tx.objectStore("tags");storeShape(tags,["accountName"]);const index=tags.index("accountName");
  if(!index.unique||index.multiEntry||JSON.stringify(index.keyPath)!=='["accountId","name"]')mutationFail();}
 if(version>=3)storeShape(tx.objectStore("outbox"));
}
function mutationShapes(tx:IDBTransaction):void {
 const store=tx.objectStore("mutations");storeShape(store,["accountId","accountState","accountAction"]);storeShape(tx.objectStore("currentMutation"));
 const action=store.index("accountAction");if(!action.unique||action.multiEntry||JSON.stringify(action.keyPath)!=='["accountId","actionId"]')mutationFail();
 for(const [name,path]of [["accountId","accountId"],["accountState",["accountId","status"]]] as const){const index=store.index(name);
  if(index.unique||index.multiEntry||JSON.stringify(index.keyPath)!==JSON.stringify(path))mutationFail();}
}

/** One transaction decides each compare-and-set, across independent contexts.
 * Strict durability is required for write-ahead creation intent publication.
 * An unresolved intent has no reset/retry-create verb: reconcile the catalog.
 */
export class V1Journal {
  private readonly factory: IDBFactory;
  private readonly name: string;
  private opening: Promise<IDBDatabase> | null = null;
  private closed = false;
  #lifetime:Readonly<{signal:AbortSignal;deadline:number}>|undefined;
  #receiptOwner:V1Journal|undefined;

  constructor(options: { indexedDB?: IDBFactory; name?: string } = {}) {
    this.factory = options.indexedDB ?? globalThis.indexedDB;
    this.name = options.name ?? "fulcra-attention-v1";
  }

  /** A real identity over the same durable store, with mandatory narrowing.
   * Child APIs cannot discard parent cancellation or extend the deadline. */
  withLifetime(signal:AbortSignal,deadline:number):V1Journal {
    if(!Number.isFinite(deadline)||deadline<=performance.now()||deadline-performance.now()>30000)throw new Error("Attention v1 journal lifetime is invalid");
    this.checkLifetime();
    const child=new V1Journal({indexedDB:this.factory,name:this.name});
    child.#lifetime=Object.freeze({signal:this.#lifetime?AbortSignal.any([this.#lifetime.signal,signal]):signal,deadline:Math.min(deadline,this.#lifetime?.deadline??Infinity)});
    child.#receiptOwner=this.#receiptOwner??this;return child;
  }
  private checkLifetime():void {
    if(this.#lifetime&&(this.#lifetime.signal.aborted||performance.now()>=this.#lifetime.deadline))throw new Error("Attention v1 journal LEASE_ENDED");
  }

  private evidenceRead<T>(limits:V1ReadLimits,read:(job:V1ReadJob)=>Promise<T>):Promise<ReadResult<T>>{
    return runV1Read(limits,read,{...this.#lifetime,check:()=>{if(this.closed)throw new Error('READ_CLOSED');this.checkLifetime();}});
  }
  private openEvidenceJob(job:V1ReadJob):Promise<IDBDatabase>{
    return openExistingDatabase(job,this.factory,this.name,5,['gate','generations','creations','tags','outbox','mutations','currentMutation'],(db,tx)=>{
      oldShapes(db,tx,3);mutationShapes(tx);
    });
  }
  /** Structural probe only; no handle or action authority escapes. */
  openEvidenceBounded(limits:V1ReadLimits):Promise<ReadResult<null>>{
    return this.evidenceRead(limits,async job=>{await job.wait(this.openEvidenceJob(job));return null;});
  }
  readGateEvidenceBounded(limits:V1ReadLimits):Promise<ReadResult<V1Gate|null>>{
    return this.evidenceRead(limits,async job=>{
      const db=await job.wait(this.openEvidenceJob(job));
      return job.wait(readTransactionBounded<V1Gate|null>(job,db,['gate'],(tx,read,finish)=>{
        this.readCurrent(tx,(request,next)=>read(request,value=>{if(value!==undefined)job.copy(value,2048);job.check();next(value);}),finish);
      }));
    });
  }
  /** Original ownership evidence only. Strict lease-time reads remain mandatory
   * before action; current gate equality is deliberately NOT an evidence test. */
  async readAccountOwnerEvidence(accountId:string,limits:V1ReadLimits):Promise<OwnerEvidence>{
    let observedGate:Readonly<V1Gate>|null=null;
    const result=await this.evidenceRead(limits,async job=>{
      if(!uuid(accountId))job.refuse('ACCOUNT_ID');
      const db=await job.wait(this.openEvidenceJob(job));
      const evidence=await job.wait(readTransactionBounded<{pointer:MutationPointer|null;current:V1MutationIntent|null;bodies:Record<string,V1OutboxRow>}>(job,db,['gate','mutations','currentMutation','outbox'],(tx,read,finish)=>{
        this.readCurrent(tx,(request,next)=>read(request,value=>{if(value!==undefined)job.copy(value,2048);job.check();next(value);}),currentGate=>{
          observedGate=currentGate;
          const store=tx.objectStore('mutations');
          read(store.index('accountId').count(accountId),rawCount=>{
            const count=job.copy(rawCount,64);if(!Number.isSafeInteger(count)||count>MUTATION_COUNT)mutationFail();
            read(tx.objectStore('currentMutation').get(accountId),rawPointer=>{
              const pointer=rawPointer===undefined?null:pointerSnapshot(job.copy(rawPointer,4096),accountId);job.check();
              if((pointer===null)!==(count===0)||pointer&&pointer.totalActions!==count)mutationFail();
              const next=(row:V1MutationIntent|null)=>{
                if(pointer&&(!row||JSON.stringify(mutationPointer(row))!==JSON.stringify(pointer)))mutationFail();
                const outstanding:V1MutationIntent[]=[];
                const scan=(statuses:string[])=>{
                  job.check();const status=statuses[0];
                  if(status!==undefined){read(store.index('accountState').openCursor([accountId,status]),cursor=>{
                    if(cursor){const value=mutationSnapshot(job.copy(cursor.value,1024*1024));job.check();
                      if(value.accountId!==accountId||value.status!==status||JSON.stringify(cursor.primaryKey)!==JSON.stringify([accountId,value.actionId])||outstanding.length>=1)mutationFail();
                      outstanding.push(value);job.check();cursor.continue();return;
                    }scan(statuses.slice(1));
                  });return;}
                  if(pointer?.status==='verified'?outstanding.length!==0:pointer?outstanding.length!==1:outstanding.length!==0)mutationFail();
                  if(outstanding.length&&JSON.stringify(outstanding[0])!==JSON.stringify(row))mutationFail();
                  if(!pointer){finish({pointer:null,current:null,bodies:Object.create(null)});return;}
                  read(store.index('accountAction').openCursor([accountId,pointer.actionId]),cursor=>{
                    if(!cursor)mutationFail();const entry=mutationSnapshot(job.copy(cursor.value,1024*1024));job.check();
                    if(JSON.stringify(cursor.primaryKey)!==JSON.stringify([accountId,pointer.actionId])||entry.accountId!==accountId||entry.actionId!==pointer.actionId||entry.ordinal>count||JSON.stringify(entry)!==JSON.stringify(row))mutationFail();
                    const bodies:Record<string,V1OutboxRow>=Object.create(null),ref=row?.version===2&&row.status!=='verified'?effectPlans(row).find(plan=>plan.bodyRef)?.bodyRef:undefined;
                    if(!ref){finish({pointer,current:row,bodies});return;}
                    read(tx.objectStore('outbox').get([ref.accountId,ref.dataType,ref.recordId]),value=>{
                      const body=outboxSnapshot(job.copy(value,16*1024*1024));job.check();
                      if(JSON.stringify(referenceFor(body))!==JSON.stringify(ref))mutationFail();bodies[entry.actionId]=body;finish({pointer,current:row,bodies});
                    });
                  });
                };scan(['prepared','issued']);
              };
              if(!pointer){next(null);return;}
              read(store.get([accountId,pointer.actionId]),value=>{const row=value===undefined?null:mutationSnapshot(job.copy(value,1024*1024));job.check();next(row);});
            });
          });
        });
      }));
      await job.wait(mutationDigest(evidence.current,evidence.bodies,true,()=>job.check()));job.check();
      const terminal=evidence.pointer?.status==='verified';
      return {status:'known' as const,observedGate,owner:evidence.current?{scope:{accountId:evidence.current.accountId,generation:evidence.current.generation},intent:evidence.current}:null,
        terminalPointer:terminal?evidence.pointer:null,reasons:[]};
    });
    if(result.status==='known')return result.value;
    return freezeMutation({status:'unknown',observedGate,owner:null,terminalPointer:null,reasons:[result.code]});
  }

  private open(): Promise<IDBDatabase> {
    if (this.closed) return Promise.reject(new Error("Attention v1 journal is closed"));
    if (this.opening) return this.opening;
    this.opening = new Promise((resolve, reject) => {
      if (!this.factory) { reject(new Error("Attention v1 durable journal is unavailable")); return; }
      let finished = false;
      const request = this.factory.open(this.name, 5);
      const fail = () => {
        if (!finished) { finished = true; clearTimeout(timeout); try{request.transaction?.abort();}catch{} reject(new Error("Attention v1 journal could not open")); }
      };
      const timeout = setTimeout(fail, 5000);
      request.onerror = fail;
      request.onblocked = fail;
      request.onupgradeneeded = (event) => {
        if (finished || this.closed || ![0, 1, 2, 3, 4].includes(event.oldVersion)) { request.transaction?.abort(); return; }
        const db = request.result;
        if(event.oldVersion>0){try{oldShapes(db,request.transaction!,event.oldVersion);}catch{request.transaction?.abort();return;}}
        if (event.oldVersion === 0) {
          db.createObjectStore("gate").put(FORMAT, "format");
          db.createObjectStore("generations");
          db.createObjectStore("creations");
        } else if (["gate", "generations", "creations"].some((name) => !db.objectStoreNames.contains(name))) {
          request.transaction?.abort(); return;
        }
        if (event.oldVersion < 2) db.createObjectStore("tags").createIndex("accountName", ["accountId", "name"], { unique: true });
        if(event.oldVersion<3)db.createObjectStore("outbox");
        if(event.oldVersion>=4){try{mutationShapes(request.transaction!);}catch{request.transaction?.abort();}return;}
        const mutations=db.createObjectStore("mutations");
        mutations.createIndex("accountId","accountId");mutations.createIndex("accountState",["accountId","status"]);mutations.createIndex("accountAction",["accountId","actionId"],{unique:true});
        db.createObjectStore("currentMutation");
      };
      request.onsuccess = () => {
        const db = request.result;
        if (finished || this.closed) { db.close(); fail(); return; }
        if (["gate", "generations", "creations", "tags", "outbox", "mutations", "currentMutation"].some((name) => !db.objectStoreNames.contains(name))) {
          db.close(); fail(); return;
        }
        try {
          const validation=db.transaction(["gate","generations","creations","tags","outbox","mutations","currentMutation"],"readonly");
          oldShapes(db,validation,3);mutationShapes(validation);

        } catch { db.close(); fail(); return; }
        finished = true; clearTimeout(timeout);
        db.onversionchange = () => { db.close(); this.closed = true; };
        resolve(db);
      };
    });
    return this.opening;
  }

  private async transaction<T>(stores: string[], write: boolean,
    run: (tx: IDBTransaction, read: <V>(request: IDBRequest<V>, next: (value: V) => void) => void,
      finish: (result: T) => void) => void,signal?:AbortSignal): Promise<T> {
    this.checkLifetime();
    signal=this.#lifetime?AbortSignal.any([this.#lifetime.signal,...signal?[signal]:[]]):signal;
    const db = await this.open();
    this.checkLifetime();
    if (this.closed) throw new Error("Attention v1 journal is closed");
    return new Promise((resolve, reject) => {
      const names = [...new Set(["gate", ...stores])];
      const tx = write ? db.transaction(names, "readwrite", { durability: "strict" }) : db.transaction(names, "readonly");
      // Older engines can silently ignore an unknown transaction option.
      // Publication is not durable evidence unless the engine confirms it.
      if (write && tx.durability !== "strict") {
        tx.abort();
        reject(new Error("Attention v1 strict journal durability is unavailable"));
        return;
      }
      let result: T, hasResult = false;
      let failure: Error | null = null;
      const abort = (error: unknown) => {
        failure = error instanceof Error ? error : new Error("Attention v1 journal operation failed");
        try { tx.abort(); } catch { reject(failure); }
      };
      const cancelled=()=>abort(new Error("Attention v1 journal operation was cancelled"));
      const cleanup=()=>signal?.removeEventListener("abort",cancelled);
      const safe = (action: () => void) => { try { this.checkLifetime();action(); } catch (error) { abort(error); } };
      const read = <V>(request: IDBRequest<V>, next: (value: V) => void) => {
        request.onsuccess = () => safe(() => next(request.result));
      };
      tx.onabort = () => {cleanup();reject(failure ?? new Error("Attention v1 journal transaction failed"));};
      tx.onerror = () => { /* The transaction abort, not a request result, owns failure. */ };
      tx.oncomplete = () => {cleanup();hasResult ? resolve(result) : reject(new Error("Attention v1 journal result is missing"));};
      signal?.addEventListener("abort",cancelled,{once:true});
      if(signal?.aborted){cancelled();return;}
      // No external await is permitted in the transaction: every effect stays
      // in IndexedDB request callbacks, where the transaction remains active.
      safe(() => run(tx, read, (value) => { result = value; hasResult = true; }));
    });
  }

  private readCurrent(tx: IDBTransaction,
    read: <V>(request: IDBRequest<V>, next: (value: V) => void) => void,
    next: (gate: V1Gate | null) => void): void {
    const store = tx.objectStore("gate");
    read(store.get("format"), (format) => {
      if (!keys(format, ["version", "initialized"]) || format.version !== 1 || typeof format.initialized !== "boolean") {
        throw new Error("Attention v1 journal format is unreadable");
      }
      read(store.get("current"), (current) => {
        if (current === undefined) {
          if (format.initialized) throw new Error("Attention v1 initialized account gate is missing");
          next(null);
        } else {
          if (!format.initialized) throw new Error("Attention v1 account gate format differs");
          next(gateSnapshot(current));
        }
      });
    });
  }

  readGate(): Promise<V1Gate | null> {
    return this.transaction([], false, (tx, read, finish) => this.readCurrent(tx, read, finish));
  }

  async transition(expected: V1Gate | null, next: V1Gate): Promise<V1Gate> {
    const before = expected === null ? null : gateSnapshot(expected), after = gateSnapshot(next);
    if (before?.generation === after.generation) return Promise.reject(new Error("Attention v1 transition requires a new generation"));
    return this.transaction(["generations"], true, (tx, read, finish) => {
      this.readCurrent(tx, read, (current) => {
        // Exact replay of a committed transition is harmless, but a stale
        // expected gate can never restore a previously used generation.
        if (sameGate(current, after)) { finish(after); return; }
        if (!sameGate(current, before)) throw new Error("Attention v1 account gate changed");
        const used = tx.objectStore("generations");
        read(used.get(after.generation), (prior) => {
          if (prior !== undefined) throw new Error("Attention v1 generation was already used");
          read(used.count(), (count) => {
            if (count >= 100000) throw new Error("Attention v1 generation journal is full");
            used.add(true, after.generation);
            tx.objectStore("gate").put({ version: 1, initialized: true }, "format");
            tx.objectStore("gate").put(after, "current");
            finish(after);
          });
        });
      });
    });
  }

  private creation<T>(scope: V1Gate, specKey: string, fingerprint: string, write: boolean,
    action: (store: IDBObjectStore, prior: V1CreationIntent | null,
      read: <V>(request: IDBRequest<V>, next: (value: V) => void) => void, finish: (value: T) => void) => void,deadline?:number,signal?:AbortSignal): Promise<T> {
    checkCreationDeadline(deadline,signal);
    const [captured, key, hash] = specInput(scope, specKey, fingerprint);
    return this.transaction(["creations"], write, (tx, read, finish) => {
      const guarded=<V>(request:IDBRequest<V>,next:(value:V)=>void)=>read(request,value=>{checkCreationDeadline(deadline,signal);next(value);});
      checkCreationDeadline(deadline,signal);
      this.readCurrent(tx, guarded, (current) => {
        if (!sameGate(current, captured)) throw new Error("Attention v1 account gate changed");
        const store = tx.objectStore("creations");
        guarded(store.get([captured.accountId!, key]), (value) => {
          checkCreationDeadline(deadline,signal);
          action(store, value === undefined ? null : intentSnapshot(value, captured.accountId!, key, hash), guarded, finish);
        });
      });
    },signal);
  }

  async readCreation(scope: V1Gate, key: string, hash: string): Promise<V1CreationIntent | null> {
    return this.creation(scope, key, hash, false, (_store, prior, _read, finish) => finish(prior));
  }

  /** Read-only original-authority predicate, checked inside the same gate /
   * intent transaction. Missing, downgraded or replaced state is never healed. */
  async requireCreation(scope:V1Gate,key:string,hash:string,destination:string,reservationGeneration:string):Promise<V1CreationIntent> {
    if(!eventType(destination)||!uuid(reservationGeneration))throw new Error("Attention v1 original creation proof is invalid");
    return this.creation(scope,key,hash,false,(_store,prior,_read,finish)=>{
      if(!prior||prior.status!=="resolved"||prior.destination!==destination||prior.createdUnderGeneration!==reservationGeneration) {
        throw new Error("Attention v1 original resolved creation is unavailable");
      }
      finish(prior);
    });
  }

  async reserveCreation(scope: V1Gate, specKey: string, fingerprint: string,deadline?:number,signal?:AbortSignal): Promise<{ created: boolean; intent: V1CreationIntent }> {
    const [captured, key, hash] = specInput(scope, specKey, fingerprint);
    return this.creation(captured, key, hash, true, (store, prior, read, finish) => {
      if (prior) { finish({ created: false, intent: prior }); return; }
      read(store.count(), (count) => {
        checkCreationDeadline(deadline,signal);
        if (count >= 1000) throw new Error("Attention v1 creation journal is full");
        const intent: V1CreationIntent = { version: 1, accountId: captured.accountId!, specKey: key, fingerprint: hash,
          status: "pending", createdUnderGeneration: captured.generation, destination: null };
        store.add(intent, [captured.accountId!, key]);
        finish({ created: true, intent });
      });
    },deadline,signal);
  }

  private bindCreation(scope: V1Gate, specKey: string, fingerprint: string, destination: string,
    status: "submitted" | "resolved",deadline?:number,signal?:AbortSignal): Promise<V1CreationIntent> {
    if (typeof destination !== "string" || !destination.startsWith("Event/") || !uuid(destination.slice(6))) {
      return Promise.reject(new Error("Attention v1 creation destination is invalid"));
    }
    return this.creation(scope, specKey, fingerprint, true, (store, prior, _read, finish) => {
      if (!prior || (prior.destination !== null && prior.destination !== destination)) {
        throw new Error("Attention v1 original creation intent must be reconciled");
      }
      const resolved: V1CreationIntent = { ...prior, status: prior.status === "resolved" ? "resolved" : status, destination };
      store.put(resolved, [prior.accountId, prior.specKey]);
      finish(resolved);
    },deadline,signal);
  }

  submitCreation(scope: V1Gate, specKey: string, fingerprint: string, destination: string,deadline?:number,signal?:AbortSignal): Promise<V1CreationIntent> {
    return this.bindCreation(scope, specKey, fingerprint, destination, "submitted",deadline,signal);
  }

  resolveCreation(scope: V1Gate, specKey: string, fingerprint: string, destination: string,deadline?:number,signal?:AbortSignal): Promise<V1CreationIntent> {
    return this.bindCreation(scope, specKey, fingerprint, destination, "resolved",deadline,signal);
  }

  /** Retain an already verified response against its ORIGINAL reservation.
   * Account changes cannot discard historical receipts. This never creates an
   * intent or resolves authority, and intentionally does not read current gate.
   * A contradictory UUID commits a permanent hold before returning an error;
   * no ordinary read/resolution/replay can clear that conflict. */
  async retainCreationReceipt(scope:V1Gate,specKey:string,fingerprint:string,destination:string):Promise<void> {
    if(this.#receiptOwner)return this.#receiptOwner.retainCreationReceipt(scope,specKey,fingerprint,destination);
    const [captured,key,hash]=specInput(scope,specKey,fingerprint);
    if(!eventType(destination))throw new Error("Attention v1 creation receipt is invalid");
    const conflict=await this.transaction(["creations"],true,(tx,read,finish)=>{
      const store=tx.objectStore("creations");
      read(store.get([captured.accountId!,key]),value=>{
        const held=value?.status==="conflict";
        if(held&&!keys(value,[...INTENT_KEYS,"conflictingDestinations"]))throw new Error("Attention v1 creation receipt hold is unreadable");
        const parsed=held?{...value,status:"submitted"}:value;
        if(held)delete parsed.conflictingDestinations;
        const prior=intentSnapshot(parsed,captured.accountId!,key,hash);
        if(prior.createdUnderGeneration!==captured.generation)throw new Error("Attention v1 receipt requires its original reservation");
        if(held) {
          const ids=value.conflictingDestinations;
          if(!Array.isArray(ids)||ids.length<2||ids.length>32||ids.some(id=>!eventType(id))
            ||new Set(ids).size!==ids.length||!ids.includes(prior.destination))throw new Error("Attention v1 creation receipt hold is unreadable");
          if(!ids.includes(destination)) {
            if(ids.length>=32)throw new Error("Attention v1 creation receipt hold is full");
            store.put({...value,conflictingDestinations:[...ids,destination]},[captured.accountId!,key]);
          }
          finish(true);return;
        }
        if(prior.destination!==null&&prior.destination!==destination) {
          store.put({...prior,status:"conflict",conflictingDestinations:[prior.destination,destination]},[captured.accountId!,key]);
          finish(true);return;
        }
        store.put({...prior,status:prior.status==="resolved"?"resolved":"submitted",destination},[captured.accountId!,key]);
        finish(false);
      });
    });
    if(conflict)throw new Error("Attention v1 creation receipts conflict. Preserve all original bindings and reconcile setup.");
  }

  private tag<T>(scope: V1Gate, label: string, name: string, write: boolean,
    action: (store: IDBObjectStore, prior: V1TagIntent | null,
      read: <V>(request: IDBRequest<V>, next: (value: V) => void) => void, finish: (value: T) => void) => void,deadline?:number,signal?:AbortSignal): Promise<T> {
    checkCreationDeadline(deadline,signal);
    const captured = tagInput(scope, label, name);
    return this.transaction(["tags"], write, (tx, read, finish) => {
      const guarded=<V>(request:IDBRequest<V>,next:(value:V)=>void)=>read(request,value=>{checkCreationDeadline(deadline,signal);next(value);});
      this.readCurrent(tx, guarded, (current) => {
        checkCreationDeadline(deadline,signal);
        if (!sameGate(current, captured)) throw new Error("Attention v1 account gate changed");
        const store = tx.objectStore("tags");
        guarded(store.get([captured.accountId!, label]), (value) => {
          action(store, value === undefined ? null : tagSnapshot(value, captured.accountId!, label, name), guarded, finish);
        });
      });
    },signal);
  }

  async readTag(scope: V1Gate, label: string, name: string): Promise<V1TagIntent | null> {
    return this.tag(scope, label, name, false, (_store, prior, _read, finish) => finish(prior));
  }

  async reserveTag(scope: V1Gate, label: string, name: string,deadline?:number,signal?:AbortSignal): Promise<{ created: boolean; intent: V1TagIntent }> {
    const captured = tagInput(scope, label, name);
    return this.tag(captured, label, name, true, (store, prior, read, finish) => {
      checkCreationDeadline(deadline,signal);
      if (prior) { finish({ created: false, intent: prior }); return; }
      read(store.count(), (count) => {
        checkCreationDeadline(deadline,signal);
        if (count >= 1000) throw new Error("Attention v1 tag journal is full");
        const intent: V1TagIntent = { version: 1, accountId: captured.accountId!, label, name,
          status: "pending", createdUnderGeneration: captured.generation, destination: null };
        store.add(intent, [captured.accountId!, label]);
        finish({ created: true, intent });
      });
    },deadline,signal);
  }

  private bindTag(scope: V1Gate, label: string, name: string, destination: string,
    status: "submitted" | "resolved",deadline?:number,signal?:AbortSignal): Promise<V1TagIntent> {
    if (!uuid(destination)) return Promise.reject(new Error("Attention v1 tag destination is invalid"));
    return this.tag(scope, label, name, true, (store, prior, _read, finish) => {
      checkCreationDeadline(deadline,signal);
      if (!prior || (prior.destination !== null && prior.destination !== destination)) {
        throw new Error("Attention v1 original tag intent must be reconciled");
      }
      const resolved: V1TagIntent = { ...prior, status: prior.status === "resolved" ? "resolved" : status, destination };
      store.put(resolved, [prior.accountId, label]);
      finish(resolved);
    },deadline,signal);
  }

  submitTag(scope: V1Gate, label: string, name: string, destination: string,deadline?:number,signal?:AbortSignal): Promise<V1TagIntent> {
    return this.bindTag(scope, label, name, destination, "submitted",deadline,signal);
  }

  resolveTag(scope: V1Gate, label: string, name: string, destination: string,deadline?:number,signal?:AbortSignal): Promise<V1TagIntent> {
    return this.bindTag(scope, label, name, destination, "resolved",deadline,signal);
  }


  /** Local pending capture only. No method deletes or acknowledges entries.
   * Count/byte caps fail the whole strict transaction, never evict prior data. */
  async putOutbox(scope: V1Gate, value: V1OutboxRow): Promise<void> {
    const captured = gateSnapshot(scope), row = outboxSnapshot(value);
    if (!captured.enabled || row.accountId !== captured.accountId || row.generation !== captured.generation) {
      throw new Error("Attention v1 capture scope is disabled or differs");
    }
    return this.transaction(["outbox", "creations", "tags"], true, (tx, read, finish) => {
      this.readCurrent(tx, read, (current) => {
        if (!sameGate(current, captured)) throw new Error("Attention v1 account gate changed");
        read(tx.objectStore("creations").get([row.accountId, "collect.attention.v1"]), (value) => {
          const intent = intentSnapshot(value, row.accountId, "collect.attention.v1", row.specFingerprint);
          if (intent.status !== "resolved" || intent.destination !== row.dataType) throw new Error("Attention v1 capture has no resolved destination");
          // Resolve UUID membership from durable account-local tag intents.
          const ids = new Set<string>(); let tagsScanned = 0;
          read(tx.objectStore("tags").openCursor(), (cursor) => {
            if (cursor) {
              if (++tagsScanned > 1000) throw new Error("Attention v1 tag journal is oversized");
              const value = cursor.value;
              if (!value || typeof value !== "object" || !uuid(value.accountId)) throw new Error("Attention v1 tag journal is unreadable");
              tagInput({ ...captured, accountId: value.accountId }, value.label, value.name);
              const tag = tagSnapshot(value, value.accountId, value.label, value.name);
              if (JSON.stringify(cursor.key) !== JSON.stringify([tag.accountId, tag.label])) throw new Error("Attention v1 tag identity is unreadable");
              if (tag.accountId === row.accountId && tag.status === "resolved") ids.add(tag.destination!);
              cursor.continue(); return;
            }
            if (row.tagIds.some(id => !ids.has(id))) throw new Error("Attention v1 capture has unresolved tags");
            const store = tx.objectStore("outbox"), key = [row.accountId, row.dataType, row.recordId];
            read(store.get(key), (prior) => {
              if (prior !== undefined) {
                if (JSON.stringify(outboxSnapshot(prior)) !== JSON.stringify(row)) throw new Error("Attention v1 original queued payload differs; keep it held");
                finish(); return;
              }
              let count = 0, bytes = 0;
              // A single cursor retains only one bounded envelope at a time.
              const request = store.openCursor();
              read(request, function scan(cursor) {
                if (cursor) {
                  const existing = outboxSnapshot(cursor.value);
                  if (JSON.stringify(cursor.key) !== JSON.stringify([existing.accountId, existing.dataType, existing.recordId])) throw new Error("Attention v1 queue identity is unreadable");
                  count++; bytes += existing.bytes;
                  if (count >= 5000 || bytes + row.bytes > 32 * 1024 * 1024) throw new Error("Attention v1 queue is full; retain all queued data");
                  cursor.continue(); return;
                }
                store.add(row, key); finish();
              });
            });
          });
        });
      });
    });
  }

  async readOutbox(scope: V1Gate, dataType: string, recordId: string): Promise<V1OutboxRow | null> {
    const captured = gateSnapshot(scope);
    if (!captured.accountId || !eventType(dataType) || !uuid(recordId)) throw new Error("Attention v1 queue lookup is invalid");
    return this.transaction(["outbox"], false, (tx, read, finish) => {
      this.readCurrent(tx, read, (current) => {
        if (!sameGate(current, captured)) throw new Error("Attention v1 account gate changed");
        read(tx.objectStore("outbox").get([captured.accountId!, dataType, recordId]), (value) => {
          if (value === undefined) { finish(null); return; }
          const row = outboxSnapshot(value);
          if (row.accountId !== captured.accountId || row.dataType !== dataType || row.recordId !== recordId
              || row.generation !== captured.generation) throw new Error("Attention v1 original queued scope differs; keep it held");
          finish(row);
        });
      });
    });
  }


  private mutationState<T>(scope:V1Gate,actionId:string,write:boolean,deadline:number|undefined,signal:AbortSignal|undefined,
   action:(tx:IDBTransaction,read:<V>(r:IDBRequest<V>,next:(value:V)=>void)=>void,finish:(value:T)=>void,state:MutationState)=>void,extraStores:string[]=[]):Promise<T> {
   mutationDeadline(deadline,signal);
   if(!scope.accountId||!uuid(actionId))return Promise.reject(new Error("Attention v1 mutation scope is invalid"));
   return this.transaction(["mutations","currentMutation","outbox",...extraStores],write,(tx,read,finish)=>{
    const bounded=<V>(r:IDBRequest<V>,next:(value:V)=>void)=>read(r,value=>{mutationDeadline(deadline,signal);next(value);});
    this.readCurrent(tx,bounded,current=>{
     if(!sameGate(current,scope))mutationFail();
     const store=tx.objectStore("mutations"),owners=tx.objectStore("currentMutation"),account=scope.accountId!;
     bounded(store.index("accountId").count(account),count=>bounded(owners.get(account),rawPointer=>{
      if(!Number.isSafeInteger(count)||count>MUTATION_COUNT)mutationFail();
      const pointer=rawPointer===undefined?null:pointerSnapshot(rawPointer,account);
      if((pointer===null)!==(count===0)||pointer&&pointer.totalActions!==count)mutationFail();
      const next=(currentRow:V1MutationIntent|null)=>{
       if(pointer&&(!currentRow||JSON.stringify(mutationPointer(currentRow))!==JSON.stringify(pointer)))mutationFail();
       const outstanding:V1MutationIntent[]=[];
       const scan=(statuses:string[])=>{
        const status=statuses[0];
        if(status!==undefined){bounded(store.index("accountState").openCursor([account,status]),cursor=>{
         if(cursor){const row=mutationSnapshot(cursor.value);
          if(row.accountId!==account||row.status!==status||JSON.stringify(cursor.primaryKey)!==JSON.stringify([account,row.actionId])||outstanding.length>=1)mutationFail();
          outstanding.push(row);cursor.continue();return;
         }scan(statuses.slice(1));
        });return;}
        if(pointer?.status==="verified"?outstanding.length!==0:pointer?outstanding.length!==1:outstanding.length!==0)mutationFail();
        if(outstanding.length&&JSON.stringify(outstanding[0])!==JSON.stringify(currentRow))mutationFail();
        bounded(store.index("accountAction").openCursor([account,actionId]),cursor=>{
         const entry=cursor===null?null:mutationSnapshot(cursor.value);
         if(cursor&&JSON.stringify(cursor.primaryKey)!==JSON.stringify([account,actionId]))mutationFail();
         if(entry&&(entry.accountId!==account||entry.actionId!==actionId||entry.ordinal>count))mutationFail();
         const bodies:Record<string,V1OutboxRow>=Object.create(null),rows=[currentRow,entry].filter((row,index,all):row is V1MutationIntent=>!!row&&row.version===2&&row.status!=="verified"&&all.findIndex(other=>other?.actionId===row.actionId)===index);
         const load=(index:number)=>{
          if(index>=rows.length){mutationDeadline(deadline,signal);action(tx,bounded,finish,{pointer,current:currentRow,entry,count,bodies});return;}
          const row=rows[index],plan=effectPlans(row).find(plan=>plan.bodyRef);
          if(!plan?.bodyRef){load(index+1);return;}
          const ref=plan.bodyRef;
          bounded(tx.objectStore("outbox").get([ref.accountId,ref.dataType,ref.recordId]),value=>{
           const body=outboxSnapshot(value);if(JSON.stringify(referenceFor(body))!==JSON.stringify(ref))mutationFail();
           bodies[row.actionId]=body;load(index+1);
          });
         };load(0);
        });
       };scan(["prepared","issued"]);
      };
      if(!pointer){next(null);return;}
      bounded(store.get([account,pointer.actionId]),row=>next(row===undefined?null:mutationSnapshot(row)));
     }));
    });
   },signal);
  }

  private async withMutation<T>(scope:V1Gate,actionId:string,write:boolean,deadline:number|undefined,signal:AbortSignal|undefined,
   action:(tx:IDBTransaction,read:<V>(r:IDBRequest<V>,next:(value:V)=>void)=>void,finish:(value:T)=>void,state:MutationState)=>void,extraStores:string[]=[]):Promise<T> {
   for(let attempt=0;attempt<3;attempt++){
    const before=await this.mutationState<MutationState>(scope,actionId,false,deadline,signal,(_tx,_read,finish,state)=>finish(state),extraStores);
    await mutationDigest(before.current,before.bodies);if(before.entry?.actionId!==before.current?.actionId)await mutationDigest(before.entry,before.bodies);
    mutationDeadline(deadline,signal);
    try{return await this.mutationState(scope,actionId,write,deadline,signal,(tx,read,finish,state)=>{
     if(JSON.stringify(before)!==JSON.stringify(state))throw new MutationChanged();
     action(tx,read,finish,state);
    },extraStores);}catch(error){if(!(error instanceof MutationChanged)||attempt===2)throw error;}
   }
   return mutationFail();
  }

  async reserveMutation(scope:V1Gate,actionId:string,operation:"deliver"|"exclude",request:unknown,deadline?:number,signal?:AbortSignal):Promise<{created:boolean;intent:V1MutationIntent}> {
   return this.reserveMutationFormat(scope,actionId,operation,request,1,deadline,signal);
  }

  async reservePhasedMutation(scope:V1Gate,actionId:string,operation:"deliver"|"exclude",details:unknown,inputs:V1EffectInput[],deadline?:number,signal?:AbortSignal):Promise<{created:boolean;intent:V1PhasedMutation}> {
   const captured=gateSnapshot(scope),capturedDetails=snapshotV1JSON(details,MUTATION_LIMIT),capturedInputs=snapshotV1JSON(inputs,PHASE_REQUEST_LIMIT) as V1EffectInput[];
   mutationDeadline(deadline,signal);
   if(!Array.isArray(capturedInputs)||capturedInputs.length!==2)mutationFail();
   const plans:EffectPlan[]=capturedInputs.map(input=>{
    if(!keys(input,["name","destination","records"])||!Array.isArray(input.records)||!input.records.length||input.records.length>32)mutationFail();
    const bodyText=input.records.map(record=>mutationJSON(record,INLINE_BODY_LIMIT)).join("\n");
    return {name:input.name,destination:input.destination,bodyText,bodyHash:"0".repeat(64),bodyBytes:new TextEncoder().encode(bodyText).length};
   });
   const request={version:2,details:capturedDetails,effects:plans};
   const preflight={requestText:mutationJSON(request,PHASE_REQUEST_LIMIT),operation};effectPlans(preflight);
   for(const plan of plans){plan.bodyHash=await sha256Hex(plan.bodyText!);mutationDeadline(deadline,signal);}
   return this.reserveMutationFormat(captured,actionId,operation,request,2,deadline,signal) as Promise<{created:boolean;intent:V1PhasedMutation}>;
  }

  /** Exactly one original immutable queued body; no body duplication or send. */
  async reserveReferencedMutation(scope:V1Gate,actionId:string,details:unknown,key:{dataType:string;recordId:string},repairRecords:unknown[],deadline?:number,signal?:AbortSignal):Promise<{created:boolean;intent:V1PhasedMutation}> {
   const captured=gateSnapshot(scope),capturedDetails=snapshotV1JSON(details,MUTATION_LIMIT),capturedKey=snapshotV1JSON(key,1024),repair=snapshotV1JSON(repairRecords,INLINE_BODY_LIMIT);
   if(!keys(capturedKey,["dataType","recordId"])||!eventType(capturedKey.dataType)||!uuid(capturedKey.recordId)||!Array.isArray(repair)||!repair.length||repair.length>32||!captured.enabled)mutationFail();
   mutationDeadline(deadline,signal);
   const row=await this.readOutbox(captured,capturedKey.dataType,capturedKey.recordId);mutationDeadline(deadline,signal);if(!row)mutationFail();
   const envelope=snapshotV1JSON(parseV1JSON(row.text),4*1024*1024);
   if(!envelope||typeof envelope!=="object"||Array.isArray(envelope)||!("payloadText" in envelope)||typeof envelope.payloadText!=="string")mutationFail();
   const bodyHash=await sha256Hex(envelope.payloadText),bodyBytes=new TextEncoder().encode(envelope.payloadText).length;
   const first={name:"deliver",destination:row.dataType,bodyRef:referenceFor(row),bodyHash,bodyBytes};
   await referencedBody({accountId:row.accountId,generation:row.generation},{...first,bodyText:null},row);mutationDeadline(deadline,signal);
   const bodyText=repair.map(record=>mutationJSON(record,INLINE_BODY_LIMIT)).join("\n");
   const second={name:"repair",destination:"DeletedRecord",bodyText,bodyHash:await sha256Hex(bodyText),bodyBytes:new TextEncoder().encode(bodyText).length};
   mutationDeadline(deadline,signal);
   return this.reserveMutationFormat(captured,actionId,"deliver",{version:2,details:capturedDetails,effects:[first,second]},2,deadline,signal,row) as Promise<{created:boolean;intent:V1PhasedMutation}>;
  }

  /** Materialize original pending bytes only. This grants no dispatch authority. */
  async readEffect(scope:V1Gate,expected:V1MutationIntent,name:string,deadline?:number,signal?:AbortSignal):Promise<Readonly<{name:string;destination:string;bodyText:string;bodyHash:string;bodyBytes:number}>> {
   const captured=gateSnapshot(scope),original=mutationSnapshot(expected);mutationDeadline(deadline,signal);
   if(original.version!==2||captured.accountId!==original.accountId||typeof name!=="string")mutationFail();
   await mutationDigest(original,{},false);mutationDeadline(deadline,signal);
   const state=await this.withMutation<MutationState>(captured,original.actionId,false,deadline,signal,(_tx,_read,finish,state)=>finish(state));
   if(!state.entry||!sameMutationRequest(state.entry,original)||state.entry.status==="verified")mutationFail();
   const plan=effectPlans(state.entry).find(plan=>plan.name===name);if(!plan)mutationFail();
   const bodyText=plan.bodyRef?await referencedBody(state.entry,plan,state.bodies[state.entry.actionId]):plan.bodyText!;mutationDeadline(deadline,signal);
   // Recheck after materialization hashing, including current account and rows.
   await this.withMutation(captured,original.actionId,false,deadline,signal,(_tx,_read,finish,final)=>{
    if(JSON.stringify(final)!==JSON.stringify(state))mutationFail();finish(undefined);
   });
   return freezeMutation({name:plan.name,destination:plan.destination,bodyText,bodyHash:plan.bodyHash,bodyBytes:plan.bodyBytes});
  }

  private async reserveMutationFormat(scope:V1Gate,actionId:string,operation:"deliver"|"exclude",request:unknown,version:1|2,deadline?:number,signal?:AbortSignal,referenceRow?:V1OutboxRow):Promise<{created:boolean;intent:V1MutationIntent}> {
   const captured=gateSnapshot(scope);mutationDeadline(deadline,signal);
   if(!captured.enabled||!captured.accountId||!uuid(actionId)||!["deliver","exclude"].includes(operation))mutationFail();
   const text=mutationJSON(request,version===2?PHASE_REQUEST_LIMIT:MUTATION_LIMIT),bytes=new TextEncoder().encode(text).length;
   const hash=await mutationRequestHash({version,accountId:captured.accountId,generation:captured.generation,actionId,operation,requestText:text,requestBytes:bytes});
   const phases=version===2?effectPlans({requestText:text,operation}).map(plan=>({name:plan.name,status:"prepared" as const,proofText:null,proofHash:null,proofBytes:0})):[];
   mutationDeadline(deadline,signal);
   return this.withMutation(captured,actionId,true,deadline,signal,(tx,read,finish,state)=>{
    const prior=state.entry;
    if(prior){if(prior.version!==version||prior.generation!==captured.generation||prior.operation!==operation||prior.requestText!==text||prior.requestHash!==hash)mutationFail();
     finish({created:false,intent:freezeMutation(prior)});return;}
    if(state.pointer&&state.pointer.status!=="verified")mutationFail();
    const cumulativeBytes=(state.pointer?.totalBytes??0)+bytes;
    if(cumulativeBytes+(phases.length+1)*MUTATION_LIMIT>MUTATION_BYTES)throw new Error("Attention v1 mutation journal is full; retain all original work");
    read(tx.objectStore("mutations").count(),count=>{
     if(count>=MUTATION_COUNT)throw new Error("Attention v1 mutation journal is full; retain all original work");
     const row:V1MutationIntent={version,accountId:captured.accountId!,generation:captured.generation,actionId,operation,
      requestText:text,requestHash:hash,requestBytes:bytes,status:"prepared",proofText:null,proofHash:null,proofBytes:0,
      ordinal:state.count+1,cumulativeBytes,...version===2?{effects:phases}:{}};
     const publish=()=>{mutationDeadline(deadline,signal);tx.objectStore("mutations").add(row,[row.accountId,actionId]);
      tx.objectStore("currentMutation").put(mutationPointer(row),row.accountId);finish({created:true,intent:freezeMutation(row)});};
     if(referenceRow){read(tx.objectStore("outbox").get([referenceRow.accountId,referenceRow.dataType,referenceRow.recordId]),value=>{
      if(JSON.stringify(outboxSnapshot(value))!==JSON.stringify(referenceRow))mutationFail();publish();
     });}else publish();
    });
   });
  }

  async readMutation(scope:V1Gate,actionId:string,deadline?:number,signal?:AbortSignal):Promise<V1MutationIntent|null> {
   const captured=gateSnapshot(scope);
   return this.withMutation(captured,actionId,false,deadline,signal,(_tx,_read,finish,state)=>finish(state.entry?freezeMutation(state.entry):null));
  }

  async issueMutation(scope:V1Gate,expected:V1MutationIntent,deadline?:number,signal?:AbortSignal):Promise<boolean> {
   const captured=gateSnapshot(scope),original=mutationSnapshot(expected);mutationDeadline(deadline,signal);
   if(original.version!==1||!captured.enabled||captured.accountId!==original.accountId||captured.generation!==original.generation)mutationFail();
   await mutationDigest(original,{},false);mutationDeadline(deadline,signal);
   return this.withMutation(captured,original.actionId,true,deadline,signal,(tx,_read,finish,state)=>{
    const prior=state.entry;
    if(!prior||!sameMutationRequest(prior,original))mutationFail();
    if(prior.status!=="prepared"){finish(false);return;}
    if(original.status!=="prepared"||state.pointer?.actionId!==prior.actionId)mutationFail();
    const row:V1MutationIntent={...prior,status:"issued"};mutationDeadline(deadline,signal);
    tx.objectStore("mutations").put(row,[row.accountId,row.actionId]);tx.objectStore("currentMutation").put(mutationPointer(row),row.accountId);finish(true);
   });
  }

  /** Store immutable CALLER evidence, not independently verified remote truth.
   * Future typed actors alone may authorize transport and queue settlement. */
  async verifyMutation(scope:V1Gate,expected:V1MutationIntent,proof:unknown,deadline?:number,signal?:AbortSignal):Promise<V1MutationIntent> {
   const captured=gateSnapshot(scope),original=mutationSnapshot(expected);mutationDeadline(deadline,signal);
   if(!captured.enabled||captured.accountId!==original.accountId||captured.generation!==original.generation)mutationFail();
   if(reservedSettlement(proof))mutationFail();
   const text=mutationProof(proof,original.operation),hash=await sha256Hex(text),bytes=new TextEncoder().encode(text).length;
   await mutationDigest(original,{},false);mutationDeadline(deadline,signal);
   return this.withMutation(captured,original.actionId,true,deadline,signal,(tx,_read,finish,state)=>{
    const prior=state.entry;
    if(!prior||!sameMutationRequest(prior,original))mutationFail();
    if(prior.status==="verified"){
     if(prior.proofText!==text||prior.proofHash!==hash)mutationFail();finish(freezeMutation(prior));return;
    }
    if(prior.status!==original.status||state.pointer?.actionId!==prior.actionId||effectStates(prior).some(effect=>effect.status!=="verified")||prior.cumulativeBytes+bytes>MUTATION_BYTES)mutationFail();
    const row:V1MutationIntent={...prior,status:"verified",proofText:text,proofHash:hash,proofBytes:bytes,cumulativeBytes:prior.cumulativeBytes+bytes};
    mutationDeadline(deadline,signal);tx.objectStore("mutations").put(row,[row.accountId,row.actionId]);
    tx.objectStore("currentMutation").put(mutationPointer(row),row.accountId);finish(freezeMutation(row));
   });
  }

  /** No provider calls or queue materialization on the terminal branch. */
  async readSettlement(scope:V1Gate,expected:V1MutationIntent,deadline:number,signal?:AbortSignal):Promise<V1MutationIntent|null> {
   const captured=gateSnapshot(scope),original=mutationSnapshot(expected);mutationDeadline(deadline,signal);
   if(!captured.enabled||captured.accountId!==original.accountId||captured.generation!==original.generation)mutationFail();
   await mutationDigest(original,{},false);mutationDeadline(deadline,signal);
   return this.withMutation(captured,original.actionId,false,deadline,signal,(tx,read,finish,state)=>{
    if(!state.entry||!sameMutationRequest(state.entry,original))mutationFail();
    const entry=state.entry;
    read(tx.objectStore('generations').get(original.generation),used=>{
     if(used!==true)mutationFail();
     if(entry.status!=="verified"){finish(null);return;}
     if(!settlementEvidence(entry))mutationFail();finish(freezeMutation(entry));
    });
   },['generations']);
  }

  /** Fresh actor capability + original authority + strict local atomic commit. */
  async settleGuardedMutation(scope:V1Gate,expected:V1MutationIntent,observation:Readonly<V1ActionObservation>,deadline:number,signal?:AbortSignal):Promise<V1ActionSettlement> {
   mutationDeadline(deadline,signal);
   if(!consumeV1ActionObservation(observation,this,deadline))mutationFail();
   const captured=gateSnapshot(scope),original=mutationSnapshot(expected),o=observation.original;
   if(!o||observation.status!=="observed"||!sameMutationRequest(o.intent,original)||original.version!==2||!captured.enabled||captured.accountId!==original.accountId||captured.generation!==original.generation)mutationFail();
   const plans=effectPlans(original),first=plans[0];if(original.operation==='deliver'&&!first.bodyRef)mutationFail();
   const guards=originalDispatchAuthority(captured,original,first.name,o.dispatchAuthority);
   const evidence={settlement:SETTLEMENT,version:1,accountId:original.accountId,generation:original.generation,actionId:original.actionId,operation:original.operation,requestHash:original.requestHash,
    bodyHash:o.bodyHash,targetDataType:o.targetDataType,targetRecordId:o.targetRecordId,controlDataType:o.controlDataType,dispatchAuthority:o.dispatchAuthority,phaseProofs:observation.phaseProofs,currentEvidence:observation.currentEvidence};
   const proof={version:1,outcome:original.operation==='exclude'?'deleted':observation.outcome==='present'?'delivered':'excluded',evidence};
   const text=mutationProof(proof,original.operation),hash=await sha256Hex(text),bytes=new TextEncoder().encode(text).length;
   await mutationDigest(original,{},false);mutationDeadline(deadline,signal);
   return this.withMutation(captured,original.actionId,true,deadline,signal,(tx,read,finish,state)=>{
    const prior=state.entry;if(!prior||!sameMutationRequest(prior,original))mutationFail();
    if(prior.status==='verified'){
     if(!settlementEvidence(prior)||prior.proofText!==text)mutationFail();finish(Object.freeze({status:'already-settled',receipt:freezeMutation(prior)}));return;
    }
    if(state.pointer?.actionId!==original.actionId||effectStates(prior).some((effect,i)=>effect.status!=='verified'||effect.proofText!==JSON.stringify(observation.phaseProofs[i]))||prior.cumulativeBytes+bytes>MUTATION_BYTES)mutationFail();
    const row:V1MutationIntent={...prior,status:'verified',proofText:text,proofHash:hash,proofBytes:bytes,cumulativeBytes:prior.cumulativeBytes+bytes};settlementEvidence(row);
    // The original current and reservation generations must remain retained
    // facts in the SAME strict transaction, not just store names in its scope.
    const generations=[...new Set([original.generation,...guards.creations.map(row=>row.createdUnderGeneration),...guards.tags.map(row=>row.createdUnderGeneration)])];
    const checkGeneration=(index:number):void=>{
     if(index<generations.length){read(tx.objectStore('generations').get(generations[index]),used=>{if(used!==true)mutationFail();checkGeneration(index+1);});return;}
     readDispatchPredicates(tx,read,guards,()=>{
     mutationDeadline(deadline,signal);
     if(first.bodyRef){const ref=first.bodyRef,body=state.bodies[original.actionId];if(!body||JSON.stringify(referenceFor(body))!==JSON.stringify(ref))mutationFail();
      tx.objectStore('outbox').delete([ref.accountId,ref.dataType,ref.recordId]);}
     tx.objectStore('mutations').put(row,[row.accountId,row.actionId]);tx.objectStore('currentMutation').put(mutationPointer(row),row.accountId);
     finish(Object.freeze({status:'settled',receipt:freezeMutation(row)}));
     });
    };
    read(tx.objectStore('generations').count(),count=>{if(!Number.isSafeInteger(count)||count<1||count>100000)mutationFail();checkGeneration(0);});
   },['generations','creations','tags']);
  }

  /** Compatibility storage-only CAS. HTTP actors must use guarded issue. */
  async issueEffect(scope:V1Gate,expected:V1MutationIntent,name:string,deadline?:number,signal?:AbortSignal):Promise<boolean> {
   return this.issueEffectCore(scope,expected,name,undefined,deadline,signal);
  }

  /** Original retained local predicates, not fresh provider authority. */
  async issueGuardedEffect(scope:V1Gate,expected:V1MutationIntent,name:string,authority:V1DispatchAuthority,deadline?:number,signal?:AbortSignal):Promise<boolean> {
   // Undefined is never the compatibility bypass on the guarded entry point.
   mutationDeadline(deadline,signal);const captured=gateSnapshot(scope),guards=dispatchAuthority(captured,authority);
   return this.issueEffectCore(captured,expected,name,guards,deadline,signal);
  }

  private async issueEffectCore(scope:V1Gate,expected:V1MutationIntent,name:string,authority:V1DispatchAuthority|undefined,deadline?:number,signal?:AbortSignal):Promise<boolean> {
   const captured=gateSnapshot(scope),original=mutationSnapshot(expected);mutationDeadline(deadline,signal);
   if(original.version!==2||!captured.enabled||captured.accountId!==original.accountId||captured.generation!==original.generation||typeof name!=="string")mutationFail();
   const guards=authority===undefined?undefined:originalDispatchAuthority(captured,original,name,authority);
   await mutationDigest(original,{},false);mutationDeadline(deadline,signal);
   return this.withMutation(captured,original.actionId,true,deadline,signal,(tx,_read,finish,state)=>{
    const prior=state.entry;if(!prior||!sameMutationRequest(prior,original))mutationFail();
    const effects=effectStates(prior),index=effects.findIndex(effect=>effect.name===name);if(index<0)mutationFail();
    if(effects[index].status!=="prepared"){finish(false);return;}
    if(prior.status==="verified"||state.pointer?.actionId!==prior.actionId||effects.slice(0,index).some(e=>e.status!=="verified"))mutationFail();
    const publish=()=>{
     const row:V1MutationIntent={...prior,status:"issued",effects:effects.map((e,i)=>i===index?{...e,status:"issued"}:e)};
     mutationDeadline(deadline,signal);tx.objectStore("mutations").put(row,[row.accountId,row.actionId]);tx.objectStore("currentMutation").put(mutationPointer(row),row.accountId);finish(true);
    };
    if(!guards){publish();return;}
    readDispatchPredicates(tx,_read,guards,publish);
   },guards?["creations","tags"]:[]);
  }

  /** Compatibility storage-only CALLER evidence. Actors must use guarded proof. */
  async verifyEffect(scope:V1Gate,expected:V1MutationIntent,name:string,proof:unknown,deadline?:number,signal?:AbortSignal):Promise<V1PhasedMutation> {
   return this.verifyEffectCore(scope,expected,name,undefined,proof,deadline,signal);
  }

  /** Original retained local predicates at evidence publication and reuse.
   * Caller evidence is not remote proof, dispatch permission or settlement. */
  async verifyGuardedEffect(scope:V1Gate,expected:V1MutationIntent,name:string,authority:V1DispatchAuthority,proof:unknown,deadline?:number,signal?:AbortSignal):Promise<V1PhasedMutation> {
   // A missing authority can never select the storage-only compatibility path.
   mutationDeadline(deadline,signal);const captured=gateSnapshot(scope),guards=dispatchAuthority(captured,authority);
   return this.verifyEffectCore(captured,expected,name,guards,proof,deadline,signal);
  }

  private async verifyEffectCore(scope:V1Gate,expected:V1MutationIntent,name:string,authority:V1DispatchAuthority|undefined,proof:unknown,deadline?:number,signal?:AbortSignal):Promise<V1PhasedMutation> {
   const captured=gateSnapshot(scope),original=mutationSnapshot(expected),text=effectProof(proof);mutationDeadline(deadline,signal);
   if(original.version!==2||!captured.enabled||captured.accountId!==original.accountId||captured.generation!==original.generation||typeof name!=="string")mutationFail();
   const guards=authority===undefined?undefined:originalDispatchAuthority(captured,original,name,authority);
   const plans=effectPlans(original),index=plans.findIndex(plan=>plan.name===name);if(index<0)mutationFail();
   const hash=await effectDigest(original,index,plans[index],text),bytes=new TextEncoder().encode(text).length;
   await mutationDigest(original,{},false);mutationDeadline(deadline,signal);
   return this.withMutation(captured,original.actionId,true,deadline,signal,(tx,_read,finish,state)=>{
    const prior=state.entry;if(!prior||!sameMutationRequest(prior,original))mutationFail();
    const effects=effectStates(prior),effect=effects[index];
    // Guarded evidence belongs to the currently held original logical owner,
    // including idempotent reuse. Historical storage-only reuse stays below.
    if(guards&&(prior.status==="verified"||state.pointer?.actionId!==prior.actionId))mutationFail();
    const publish=()=>{
     if(effect.status==="verified"){
      if(effect.proofText!==text||effect.proofHash!==hash)mutationFail();finish(freezeMutation(prior as V1PhasedMutation));return;
     }
     if(prior.status==="verified"||state.pointer?.actionId!==prior.actionId||effects.slice(0,index).some(e=>e.status!=="verified")||prior.cumulativeBytes+bytes>MUTATION_BYTES)mutationFail();
     const row:V1PhasedMutation={...prior,version:2,cumulativeBytes:prior.cumulativeBytes+bytes,
      effects:effects.map((e,i)=>i===index?{name:e.name,status:"verified",proofText:text,proofHash:hash,proofBytes:bytes}:e)};
     mutationDeadline(deadline,signal);tx.objectStore("mutations").put(row,[row.accountId,row.actionId]);tx.objectStore("currentMutation").put(mutationPointer(row),row.accountId);finish(freezeMutation(row));
    };
    if(!guards){publish();return;}
    // Reuse is evidence too: even an immutable matching proof is held when its
    // original local authority changed. All reads share the mutation write TX.
    readDispatchPredicates(tx,_read,guards,publish);
   },guards?["creations","tags"]:[]);
  }

  async close(): Promise<void> {
    this.closed = true;
    if (this.opening) (await this.opening).close();
  }
}
