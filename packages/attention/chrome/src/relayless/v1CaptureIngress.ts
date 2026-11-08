// Source-only held capture. This store owns retention, never HTTP authority.
// Codec/journal envelopes remain exact and separate from provenance evidence.
import {runV1Read,openExistingDatabase,type V1ReadLimits,type ReadResult} from './v1ReadBudget';
import type {Capture,V1QueuedAttention} from './v1QueueCodec';
import {prepareQueuedAttention,queuedRowFor} from './v1QueueCodec';
import type {V1Journal} from './v1Journal';
import {V1Outbox} from './v1Outbox';
import {snapshotV1JSON} from './v1Http';
import type {AttentionEvent} from '../types';

export interface CaptureDescriptor {
 version:1;scope:Capture['scope'];binding:Capture['binding'];context:Capture['context'];
 provenance:{publicationId:string;scopeFingerprint:string;consentRevision:string;installationId:string;setupReceiptId:string;dispatchReceiptId:string};
}
/** Local source enrichment only, independent of account/setup authority.
 * Absent/null on old rows is unknown and is never filled from retry settings. */
export interface CaptureSourceContext {version:1;client:string;identityLabel:string|null;}
export type CaptureRaw=Readonly<Record<string,unknown>>;
export interface CaptureCheckpoint {
 version:1;id:string;origin:'visit'|'backfill'|'legacy';linkage:Readonly<Record<string,unknown>>;
 descriptor:CaptureDescriptor|null;sourceContext?:CaptureSourceContext|null;raw:CaptureRaw;prepared:AttentionEvent|null;candidate:Readonly<V1QueuedAttention>|null;
 state:'active'|'held'|'prepared'|'enqueued';reason:string;revision:number;
}
export type PortionInput=Pick<CaptureCheckpoint,'id'|'origin'|'linkage'|'descriptor'|'sourceContext'|'raw'>;
const DATABASE='fulcra-attention-ingress-v1',STORE='checkpoints',ACCOUNTING='$accounting';
export const CAPTURE_BUDGET_BYTES=64*1024*1024;
const LIMIT=16*1024*1024;
function unknown():never{throw new Error('Attention capture retention UNKNOWN; backpressure; preserve active input');}
function detach<T>(v:T):T{return snapshotV1JSON(v,LIMIT) as T;}
function bytes(v:unknown):number{return new TextEncoder().encode(JSON.stringify(v)).length;}
function validSourceContext(v:unknown):v is CaptureSourceContext|null{
 if(v===null)return true;if(!v||typeof v!=='object'||Array.isArray(v))return false;const c=v as CaptureSourceContext;
 return Object.keys(c).sort().join(',')==='client,identityLabel,version'&&c.version===1&&typeof c.client==='string'&&c.client.length>0&&c.client.length<=1024
  &&(c.identityLabel===null||typeof c.identityLabel==='string'&&c.identityLabel.length<=1024&&!/[\x00-\x1f\x7f]/.test(c.identityLabel));
}
function valid(v:unknown):v is CaptureCheckpoint{
 if(!v||typeof v!=='object'||Array.isArray(v))return false;const r=v as CaptureCheckpoint;
 return Object.keys(r).sort().join(',')===(Object.hasOwn(r,'sourceContext')?'candidate,descriptor,id,linkage,origin,prepared,raw,reason,revision,sourceContext,state,version':'candidate,descriptor,id,linkage,origin,prepared,raw,reason,revision,state,version')
  &&(!Object.hasOwn(r,'sourceContext')||validSourceContext(r.sourceContext))
  &&r.version===1&&typeof r.id==='string'&&r.id!==ACCOUNTING&&['visit','backfill','legacy'].includes(r.origin)
  &&['active','held','prepared','enqueued'].includes(r.state)&&Number.isSafeInteger(r.revision)&&r.revision>0
  &&!!r.raw&&typeof r.raw==='object'&&!!r.linkage&&typeof r.linkage==='object'&&typeof r.reason==='string';
}
interface Accounting{id:string;version:1;bytes:number;count:number;}
function accounting(v:unknown):v is Accounting{const a=v as Accounting;return !!a&&a.id===ACCOUNTING&&a.version===1&&Number.isSafeInteger(a.bytes)&&a.bytes>=0&&a.bytes<=CAPTURE_BUDGET_BYTES&&Number.isSafeInteger(a.count)&&a.count>=0;}
export class V1CaptureIngress {
 private readonly factory:IDBFactory;
 constructor(factory:IDBFactory=globalThis.indexedDB){this.factory=factory;}
 /** Structural evidence only: no checkpoint/accounting/fresh-origin proof. */
 openExistingBounded(limits:V1ReadLimits):Promise<ReadResult<null>>{
  return runV1Read(limits,async job=>{
   await job.wait(openExistingDatabase(job,this.factory,DATABASE,1,[STORE],(_db,tx)=>{
    const store=tx.objectStore(STORE);if(store.keyPath!=='id'||store.autoIncrement||store.indexNames.length)job.refuse('INGRESS_SHAPE');
   }));return null;
  });
 }
 private open():Promise<IDBDatabase>{return new Promise((resolve,reject)=>{
  let r:IDBOpenDBRequest;try{r=this.factory.open(DATABASE,1);}catch{reject(new Error('Attention capture retention UNKNOWN; backpressure; preserve active input'));return;}
  let failed=false;r.onblocked=()=>{failed=true;reject(new Error('Attention capture retention UNKNOWN; database blocked'));};
  r.onupgradeneeded=()=>{const s=r.result.createObjectStore(STORE,{keyPath:'id'});s.add({id:ACCOUNTING,version:1,bytes:0,count:0});};
  r.onerror=()=>reject(new Error('Attention capture retention UNKNOWN; database unavailable'));
  r.onsuccess=()=>{if(failed){r.result.close();return;}r.result.onversionchange=()=>r.result.close();resolve(r.result);};
 });}
 private async change(id:string,edit:(row:CaptureCheckpoint|null)=>CaptureCheckpoint,beforePut?:(next:CaptureCheckpoint,old:CaptureCheckpoint|null)=>CaptureCheckpoint):Promise<CaptureCheckpoint>{
  if(!id||id===ACCOUNTING||id.length>1024)unknown();const db=await this.open();
  try{return await new Promise<CaptureCheckpoint>((resolve,reject)=>{
   let tx:IDBTransaction;try{tx=db.transaction(STORE,'readwrite',{durability:'strict'});if(tx.durability!=='strict')unknown();}catch{reject(new Error('Attention capture retention UNKNOWN; strict durability unavailable'));return;}
   let result:CaptureCheckpoint|undefined,failed=false;const s=tx.objectStore(STORE),r=s.get(id);
   const refuse=()=>{failed=true;try{tx.abort();}catch{/* transaction already stopped */}};
   r.onsuccess=()=>{try{if(r.result!==undefined&&!valid(r.result))unknown();const old=(r.result??null) as CaptureCheckpoint|null;result=detach(edit(old));if(!valid(result))unknown();
    const a=s.get(ACCOUNTING);a.onsuccess=()=>{try{if(!accounting(a.result))unknown();if(beforePut){result=detach(beforePut(result!,old));if(!valid(result))unknown();}const next=a.result.bytes+bytes(result)-(old?bytes(old):0);if(next>CAPTURE_BUDGET_BYTES)unknown();
     s.put(result!);s.put({...a.result,bytes:next,count:a.result.count+(old?0:1)});
    }catch{refuse();}};
   }catch{refuse();}};
   tx.onabort=()=>reject(new Error('Attention capture retention UNKNOWN; backpressure; preserve active input'));
   tx.onerror=()=>{};tx.oncomplete=()=>{if(failed||!result)reject(new Error('Attention capture retention UNKNOWN'));else resolve(detach(result));};
  });}finally{db.close();}
 }
 async read(id:string):Promise<CaptureCheckpoint|null>{const all=await this.rows();return all.find(r=>r.id===id)??null;}
 async rows():Promise<CaptureCheckpoint[]>{const db=await this.open();try{return await new Promise((resolve,reject)=>{
  const t=db.transaction(STORE),r=t.objectStore(STORE).getAll();t.onabort=()=>reject(new Error('Attention capture retention UNKNOWN; read failed'));
  t.oncomplete=()=>{try{const a=r.result.find(v=>v.id===ACCOUNTING),rows=r.result.filter(v=>v.id!==ACCOUNTING);if(!accounting(a)||!rows.every(valid)||rows.length!==a.count||rows.reduce((n,v)=>n+bytes(v),0)!==a.bytes)unknown();resolve(rows.map(row=>detach(row)));}catch{reject(new Error('Attention capture retention UNKNOWN; accounting unreadable'));}};
 });}finally{db.close();}}
 async checkpointPortion(input:PortionInput):Promise<CaptureCheckpoint>{const captured=detach(input);return this.change(captured.id,old=>{
  if(old){if(old.origin!==captured.origin||JSON.stringify(old.linkage)!==JSON.stringify(captured.linkage)||JSON.stringify(old.descriptor)!==JSON.stringify(captured.descriptor))unknown();
   if(old.state!=='active')return old;return {...old,raw:captured.raw,revision:old.revision+1};}
  return {version:1,...captured,sourceContext:captured.sourceContext??null,prepared:null,candidate:null,state:'active',reason:captured.descriptor?'capture-pending':'unattributed',revision:1};
 });}
 async sealPortion(id:string,event:AttentionEvent|null,reason='unattributed',current?:()=>boolean):Promise<CaptureCheckpoint>{const prepared=detach(event);return this.change(id,old=>{
  if(!old)unknown();if(old.state!=='active')return old;
  return {...old,prepared,state:'held',reason,revision:old.revision+1};
 },current?(next,old)=>{
  // Evaluate only synchronously at the final put, after the accounting read.
  // A failed lifetime cannot erase an already confirmed sealed history row.
  if(old?.state!=='active')return next;
  let allowed=false;try{allowed=current()===true;}catch{/* revoked/unknown */}
  return allowed?next:{...next,prepared:null,state:'held',reason:'capture-scope-changed'};
 }:undefined);}
 async noteLegacyLoss(id:string):Promise<CaptureCheckpoint>{return this.change(id,old=>{if(!old||old.origin!=='legacy'||old.raw.kind!=='legacy-link')unknown();if(old.reason==='legacy-retention-unknown')return old;return {...old,state:'held',reason:'legacy-retention-unknown',revision:old.revision+1};});}
 async hold(id:string,reason:string):Promise<CaptureCheckpoint>{return this.change(id,old=>{if(!old)unknown();if(old.reason==='legacy-retention-unknown')return old;return {...old,state:'held',reason,revision:old.revision+1};});}
 async enqueueCaptured(id:string,authority:{current:(d:CaptureDescriptor)=>Promise<boolean>;journal:V1Journal}):Promise<CaptureCheckpoint>{
  let row=await this.read(id);if(!row)unknown();if(!row.descriptor||!row.prepared)return row;
  if(!await authority.current(detach(row.descriptor)))return this.hold(id,'descriptor-not-current');
  const input:Capture={scope:row.descriptor.scope,binding:row.descriptor.binding,context:row.descriptor.context,event:row.prepared};
  const candidate=row.candidate??await prepareQueuedAttention(input);
  row=await this.change(id,old=>{if(!old||JSON.stringify(old.descriptor)!==JSON.stringify(row!.descriptor)||JSON.stringify(old.prepared)!==JSON.stringify(row!.prepared))unknown();
   if(old.candidate&&JSON.stringify(old.candidate)!==JSON.stringify(candidate))unknown();return {...old,candidate,state:'prepared',reason:'enqueue-ack-unknown',revision:old.revision+1};});
  if(!await authority.current(detach(row.descriptor!)))return this.hold(id,'descriptor-not-current');
  // Read exact retained candidate before retry. Gate checking stays in journal.
  const exact=await queuedRowFor(candidate),existing=await authority.journal.readOutbox(candidate.scope,candidate.binding.dataType,candidate.record.id);
  if(existing&&JSON.stringify(existing)!==JSON.stringify(exact))return this.hold(id,'source-collision');
  if(!existing){await new V1Outbox(authority.journal).enqueue(input);}
  const readback=await authority.journal.readOutbox(candidate.scope,candidate.binding.dataType,candidate.record.id);
  if(JSON.stringify(readback)!==JSON.stringify(exact))unknown();
  return this.change(id,old=>{if(!old||JSON.stringify(old.candidate)!==JSON.stringify(candidate))unknown();return {...old,state:'enqueued',reason:'journal-verified-pending',revision:old.revision+1};});
 }
 async status(){const rows=await this.rows();return {status:'held' as const,drains:'disabled' as const,retained:rows.length,active:rows.filter(r=>r.state==='active').length,held:rows.filter(r=>r.state==='held').length,prepared:rows.filter(r=>r.state==='prepared').length,enqueued:rows.filter(r=>r.state==='enqueued').length,legacyOriginals:rows.filter(r=>r.origin==='legacy').length,legacyRetentionUnknown:rows.filter(r=>r.reason==='legacy-retention-unknown').length,verifiedUploaded:0,serializedBytes:rows.reduce((n,r)=>n+bytes(r),0),budgetBytes:CAPTURE_BUDGET_BYTES};}
}
