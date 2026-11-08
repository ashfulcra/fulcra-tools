// Read-only provider proof at one scoped read. No mutation lease, permanent
// permission, deletion, queue acknowledgement or default sender is enabled.
import {V1Http,snapshotV1JSON} from "./v1Http";
import {V1Journal} from "./v1Journal";
import {readV1ExactRecords,v1RecordMatches} from "./v1Records";
import {verifyExclusionV1} from "./v1ExclusionDestination";
import {exclusionV1Contract,exclusionV1Fingerprint} from "./v1ExclusionSpec";
import {buildV1ExclusionRecord,exclusionIdFor} from "./v1Exclusions";
import {withV1DecisionFailure,requireV1DecisionCapability,type V1DecisionCapability} from "./v1DecisionAuthority";
const KEY=exclusionV1Contract().key;
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const TYPE=/^(Event|Metric)\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const MESSAGE="Collect v1 exclusion policy could not be verified. Keep queued data and reconcile the original control binding.";
function refuse():never{throw new Error(MESSAGE);}
interface Options {http:V1Http;journal:V1Journal;targetDataType:string;recordIds:readonly string[];timeoutMs?:number;decision?:{proof:V1DecisionCapability;deadline:number;signal:AbortSignal};}
export interface V1ExclusionRead {
 readonly accountId:string;readonly generation:string;readonly controlDataType:string;readonly targetDataType:string;
 readonly inputSchemaHash:string;readonly schemaHash:string;
 readonly exclusions:Readonly<Record<string,Readonly<Record<string,unknown>>>>;
}
export async function readV1Exclusions(options:Options):Promise<Readonly<V1ExclusionRead>> {
 return withV1DecisionFailure(options.decision?.proof,async()=>{
 const cancellation=new AbortController();
 const started=performance.now(),http=options.http.withReadCancellation(cancellation.signal),journal=options.journal,scope=http.scope,timeoutMs=options.timeoutMs??30000;
 http.assertJournal(journal);
 const captured=snapshotV1JSON({target:options.targetDataType,ids:options.recordIds},8192) as {target:string;ids:string[]};
 if(!Number.isFinite(started)||!Number.isFinite(timeoutMs)||timeoutMs<=0||timeoutMs>30000
  ||typeof captured.target!=="string"||!TYPE.test(captured.target)||!Array.isArray(captured.ids)||!captured.ids.length
  ||captured.ids.length>32||captured.ids.some(id=>typeof id!=="string"||!UUID.test(id)))refuse();
 const target=captured.target,ids=[...new Set(captured.ids)],decision=options.decision,deadline=Math.min(started+timeoutMs,decision?.deadline??Infinity);
 let ended=false;
 const remaining=()=>{const now=performance.now();if(ended||!Number.isFinite(now)||now>=deadline)refuse();return deadline-now;};
 const operation=async():Promise<Readonly<V1ExclusionRead>>=>{
  const fresh=await http.captureLocalGuard(deadline,decision?.signal??cancellation.signal);remaining();
  const proved=decision?await requireV1DecisionCapability(decision.proof,http,journal,deadline,decision.signal):null;remaining();
  if(proved&&(proved.target!==target||ids.length!==1||ids[0]!==proved.recordId))refuse();
  const checked=async()=>{remaining();await fresh();if(decision)await requireV1DecisionCapability(decision.proof,http,journal,deadline,decision.signal);remaining();};
  const hash=await exclusionV1Fingerprint();await checked();
  // A read may neither discover/adopt a new policy nor turn a submitted
  // creation receipt into authority. Explicit setup owns that transition.
  const prior=await journal.readCreation(scope,KEY,hash);await checked();
  if(!prior||prior.status!=="resolved"||!prior.destination||prior.destination===target)refuse();
  const control=prior.destination;if(proved&&proved.control!==control)refuse();
  const verify=async()=>{if(proved){await checked();return {dataType:control,reservationGeneration:prior.createdUnderGeneration,...proved.evidence.control};}return verifyExclusionV1({http,journal,pinnedId:control,timeoutMs:Math.min(10000,remaining())});};
  const before=await verify();await checked();
  if(before.reservationGeneration!==prior.createdUnderGeneration)refuse();
  const lookup=new Map(await Promise.all(ids.map(async id=>[await exclusionIdFor(control,target,id),id] as const)));await checked();
  if(lookup.size!==ids.length)refuse();
  const rows=await readV1ExactRecords(http,control,[...lookup.keys()],Math.min(8000,remaining()));await checked();
  const found:Record<string,Readonly<Record<string,unknown>>>=Object.create(null);
  for(const [identity,row]of Object.entries(rows)) {
   const identifier=lookup.get(identity);if(!identifier)refuse();
   if(["fulcra_userid","userid"].some(key=>Object.hasOwn(row,key)&&row[key]!==scope.accountId))refuse();
   const expected=await buildV1ExclusionRecord({controlType:control,targetDataType:target,targetRecordId:identifier,
    actionId:row.action_id as string,requestedAt:row.start_time as string,reason:row.reason as string,evidence:row.evidence as string[]});await checked();
   if(!v1RecordMatches(row,expected))refuse();found[identifier]=row;
  }
  const after=await verify();await checked();
  if(before.dataType!==control||after.dataType!==control||before.reservationGeneration!==after.reservationGeneration
   ||before.inputSchemaHash!==after.inputSchemaHash||before.schemaHash!==after.schemaHash)refuse();
  await journal.requireCreation(scope,KEY,hash,control,prior.createdUnderGeneration);await checked();
  return Object.freeze({accountId:scope.accountId!,generation:scope.generation,controlDataType:control,targetDataType:target,
   inputSchemaHash:after.inputSchemaHash,schemaHash:after.schemaHash,exclusions:Object.freeze(found)});
 };
 let timer:ReturnType<typeof setTimeout>|undefined;
 const timeout=new Promise<never>((_,reject)=>{timer=setTimeout(()=>{ended=true;cancellation.abort();reject(new Error(MESSAGE));},Math.ceil(remaining()));});
 try{return await Promise.race([operation(),timeout]);}catch{return refuse();}finally{ended=true;cancellation.abort();clearTimeout(timer);}
 });
}
