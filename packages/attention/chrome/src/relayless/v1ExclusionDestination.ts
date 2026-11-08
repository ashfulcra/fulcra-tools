// Opt-in local control binding. Schema/ownership proof is not current policy,
// replay permission, capture enablement, native deletion or record delivery.
import {V1Http,V1HttpError,parseV1JSON,snapshotV1JSON} from "./v1Http";
import {V1Journal} from "./v1Journal";
import {readV1OwnedTypes,type V1OwnedType} from "./v1OwnedTypes";
import {exclusionV1Contract,exclusionV1CreateBody,exclusionV1Fingerprint,
 proveExclusionV1InputSchema,proveExclusionV1Schema} from "./v1ExclusionSpec";
import type {V1EventSchemaProof} from "./v1EventSchema";
const contract=exclusionV1Contract(),KEY=contract.key,NAME=contract.name;
const EVENT=/^Event\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
interface Options {http:V1Http;journal:V1Journal;pinnedId?:string;allowCreate?:boolean;timeoutMs?:number;signal?:AbortSignal;}
export interface ResolvedExclusionV1 extends V1EventSchemaProof {
 readonly accountId:string;readonly generation:string;readonly dataType:string;readonly specFingerprint:string;
 readonly inputSchemaText:string;readonly inputSchemaHash:string;
 readonly reservationGeneration:string;
}
const MESSAGE="Collect v1 exclusion control is unavailable or uncertain. Preserve the original policy binding and reconcile setup.";
function refuse():never {throw new Error(MESSAGE);}
function object(value:unknown):value is Record<string,unknown>{return value!==null&&typeof value==="object"&&!Array.isArray(value);}
function compatible(row:V1OwnedType):string {
 if(!EVENT.test(row.id)||row.deprecated||row.record_spec.type!=="event"
  ||typeof row.record_spec.schema!=="string"||!row.record_spec.schema)refuse();
 return row.record_spec.schema;
}
function pinned(rows:readonly V1OwnedType[],pin:string):V1OwnedType|null {
 const found=rows.find(row=>row.id===pin);if(!found)return null;compatible(found);return found;
}
function inputFields(row:V1OwnedType):Record<string,unknown> {
 const text=row.record_spec.schema;
 if(typeof text!=="string"||!text||text.length>1024*1024)refuse();
 let schema:unknown;try{schema=snapshotV1JSON(parseV1JSON(text),1024*1024);}catch{return refuse();}
 if(!object(schema)||schema.type!=="object"||!object(schema.properties))refuse();
 return schema.properties;
}
function receiptId(body:unknown,account:string):string {
 if(!object(body)||typeof body.id!=="string"||!EVENT.test(body.id)
  ||(body.fulcra_userid!==undefined&&body.fulcra_userid!==account)
  ||(body.userid!==undefined&&body.userid!==account))refuse();return body.id;
}
async function resolve(options:Options,verifyOnly:boolean):Promise<Readonly<ResolvedExclusionV1>> {
 const started=performance.now(),http=options.signal?options.http.withReadCancellation(options.signal):options.http,journal=options.journal,scope=http.scope;
 const explicit=options.pinnedId,allowCreate=options.allowCreate??false,timeoutMs=options.timeoutMs??10000;
 http.assertJournal(journal);
 if(typeof allowCreate!=="boolean"||(explicit!==undefined&&(typeof explicit!=="string"||!EVENT.test(explicit)))
  ||!Number.isFinite(started)||!Number.isFinite(timeoutMs)||timeoutMs<=0||timeoutMs>10000)refuse();
 const deadline=started+timeoutMs;
 const remaining=()=>{const now=performance.now();if(options.signal?.aborted||!Number.isFinite(now)||now>=deadline)refuse();return deadline-now;};
 const requestBudget=()=>Math.min(8000,remaining());
 const owned=async()=>{const rows=await readV1OwnedTypes(http,requestBudget());remaining();return rows;};
 // The input endpoint is the complete caller-owned authority. Inspect every
 // Event input schema, including retired ones: a name miss cannot authorize
 // replacement of a renamed permanent policy. Unreadable input is UNKNOWN.
 const discover=async(rows:readonly V1OwnedType[],pin:string|null):Promise<V1OwnedType|null>=>{
  if(pin)return pinned(rows,pin);
  if(rows.some(row=>row.name===NAME&&!EVENT.test(row.id)))refuse();
  const events=rows.filter(row=>EVENT.test(row.id));if(events.length>64)refuse();
  const found:V1OwnedType[]=[];
  for(const row of events) {
   remaining();const fields=inputFields(row);
   const plausible=row.name===NAME||["target_data_type","target_record_id","action_id"].some(key=>Object.hasOwn(fields,key))
    ||Object.keys(contract.fields).every(key=>Object.hasOwn(fields,key));
   if(plausible){await proveExclusionV1InputSchema(compatible(row));remaining();found.push(row);}
  }
  if(found.length>1)refuse();remaining();return found[0]??null;
 };
 const operation=async()=>{
 const hash=await exclusionV1Fingerprint();remaining();
 const prior=await journal.readCreation(scope,KEY,hash);remaining();
 if(verifyOnly&&prior?.status!=="resolved")refuse();
 if(explicit&&prior?.destination&&explicit!==prior.destination)refuse();
 let pin=explicit??prior?.destination??null;
 const establishedPin=explicit??(prior?.status==="resolved"?prior.destination:null);
 let selected=await discover(await owned(),pin);remaining();
 if(!selected) {
  if(pin||!allowCreate)refuse();
  const reserved=await journal.reserveCreation(scope,KEY,hash,deadline,options.signal);remaining();
  if(reserved.intent.destination)pin=reserved.intent.destination;
  else {
   if(!reserved.created)refuse();
   let receipt:unknown,failed:unknown;
   try{receipt=await http.json("/input/v1/data_type/Event",{method:"POST",body:exclusionV1CreateBody(),timeoutMs:requestBudget()});}
   catch(error){failed=error;}
   if(failed===undefined) {
    pin=receiptId(receipt,scope.accountId!);
    // Preserve a known receipt before any further proof. This is a submitted
    // intent, never a resolved binding; an expired setup cannot replace it.
    await journal.retainCreationReceipt(scope,KEY,hash,pin);
   }
   remaining();selected=await discover(await owned(),pin);
   if(!selected){if(failed instanceof V1HttpError)throw failed;refuse();}
  }
  if(!selected)selected=await discover(await owned(),pin);
  if(!selected)refuse();
 }
 remaining();const inputSchema=compatible(selected),dataType=selected.id;
 const inputProof=await proveExclusionV1InputSchema(inputSchema);remaining();
 const served=await http.json(`/data/v1/catalog/${dataType}/v1/schema`,{timeoutMs:requestBudget()});remaining();
 const proof=await proveExclusionV1Schema(JSON.stringify(served));remaining();
 const final=await discover(await owned(),establishedPin?dataType:null);
 if(!final||final.id!==dataType||compatible(final)!==inputSchema)refuse();remaining();
 if(!verifyOnly){await journal.reserveCreation(scope,KEY,hash,deadline,options.signal);remaining();}
 const bound=verifyOnly?await journal.requireCreation(scope,KEY,hash,dataType,prior!.createdUnderGeneration)
  :await journal.resolveCreation(scope,KEY,hash,dataType,deadline,options.signal);remaining();
 return Object.freeze({...proof,inputSchemaText:inputProof.inputSchemaText,inputSchemaHash:inputProof.inputSchemaHash,
  accountId:scope.accountId!,generation:scope.generation,dataType,specFingerprint:hash,reservationGeneration:bound.createdUnderGeneration});
 };
 // Bound blocked IndexedDB/crypto waits too. Every resumed effect still checks
 // the same absolute deadline; a late known receipt may only be retained.
 let timer:ReturnType<typeof setTimeout>|undefined;
 const timeout=new Promise<never>((_,reject)=>{timer=setTimeout(()=>reject(new Error(MESSAGE)),remaining());});
 try{return await Promise.race([operation(),timeout]);}finally{clearTimeout(timer);}
}
export function resolveExclusionV1(options:Options):Promise<Readonly<ResolvedExclusionV1>> {return resolve(options,false);}
/** No local writes, discovery/adoption, receipt promotion or creation. */
export function verifyExclusionV1(options:Pick<Options,"http"|"journal"|"pinnedId"|"timeoutMs"|"signal">):Promise<Readonly<ResolvedExclusionV1>> {
 return resolve({http:options.http,journal:options.journal,pinnedId:options.pinnedId,timeoutMs:options.timeoutMs,signal:options.signal,allowCreate:false},true);
}
