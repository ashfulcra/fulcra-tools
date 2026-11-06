// @vitest-environment node
import {IDBFactory,IDBObjectStore} from "fake-indexeddb";
import {test,expect,vi} from "vitest";
import {V1Journal,type V1Gate,type V1DispatchAuthority,type V1MutationIntent,type V1PhasedMutation} from "../../src/relayless/v1Journal";

const ACCOUNT="11111111-2222-4333-8444-555555555555",TYPE="Event/22222222-3333-4444-8555-666666666666",CONTROL="Event/99999999-3333-4444-8555-666666666666";
const GEN="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",OLD="bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee",ACTION="33333333-4444-4555-8666-777777777777",TAG="44444444-5555-4666-8777-888888888888",SECOND="55555555-5555-4666-8777-888888888888",FP="a".repeat(64),KEY="collect.attention.v1";
const gate:V1Gate={version:1,accountId:ACCOUNT,generation:GEN,enabled:true};
const plans=()=>[{name:"deliver",destination:TYPE,records:[{id:ACTION,synthetic:true}]},{name:"repair",destination:"DeletedRecord",records:[{id:TAG,record_id:ACTION,data_type:"Event"}]}];
const proof=(outcome="present",context="original")=>({version:1,outcome,evidence:{context}});
async function setup(options:{guardless?:boolean;older?:boolean;multiple?:boolean;exclude?:boolean;wrongDestination?:boolean}={}){
 const indexedDB=new IDBFactory(),journal=new V1Journal({indexedDB}),reservationGate=options.older?{...gate,generation:OLD}:gate;
 await journal.transition(null,reservationGate);await journal.reserveCreation(reservationGate,KEY,FP);
 const creation=await journal.resolveCreation(reservationGate,KEY,FP,TYPE);
 await journal.reserveTag(reservationGate,"original","Original");const tag=await journal.resolveTag(reservationGate,"original","Original",TAG);
 const authority:V1DispatchAuthority={creations:[creation],tags:[tag]};
 if(options.multiple||options.exclude){await journal.reserveCreation(reservationGate,"collect.control.v1","b".repeat(64));authority.creations.push(await journal.resolveCreation(reservationGate,"collect.control.v1","b".repeat(64),CONTROL));}
 if(options.multiple){await journal.reserveTag(reservationGate,"second","Second");authority.tags.push(await journal.resolveTag(reservationGate,"second","Second",SECOND));}
 if(options.older)await journal.transition(reservationGate,gate);
 const input=plans();if(options.wrongDestination)input[0].destination=CONTROL;
 if(options.exclude){input[0].name="policy";input[0].destination=CONTROL;input[1].name="delete";}
 const {intent}=await journal.reservePhasedMutation(gate,ACTION,options.exclude?"exclude":"deliver",options.guardless?{}:{dispatchAuthority:authority},input);
 return {indexedDB,journal,intent,authority};
}
type State=Awaited<ReturnType<typeof setup>>;
function verify(s:State,options:{journal?:V1Journal;intent?:V1MutationIntent;phase?:string;authority?:unknown;proof?:unknown;deadline?:number;signal?:AbortSignal}={}):Promise<V1PhasedMutation>{
 return (options.journal??s.journal).verifyGuardedEffect(gate,options.intent??s.intent,options.phase??"deliver",(Object.hasOwn(options,"authority")?options.authority:s.authority) as V1DispatchAuthority,options.proof??proof(),options.deadline,options.signal);
}
async function raw(s:State,store:string,key:IDBValidKey,change:(value:any)=>any){
 const request=s.indexedDB.open("fulcra-attention-v1");return new Promise<void>((resolve,reject)=>{request.onerror=()=>reject(request.error);request.onsuccess=()=>{
  const db=request.result,tx=db.transaction(store,"readwrite"),st=tx.objectStore(store),read=st.get(key);
  read.onsuccess=()=>{const value=change(read.result);if(value===undefined)st.delete(key);else st.put(value,key);};
  tx.oncomplete=()=>{db.close();resolve();};tx.onabort=()=>{db.close();reject(tx.error);};
 }});
}
async function unchanged(s:State,expected:V1MutationIntent=s.intent){expect(await s.journal.readMutation(gate,ACTION)).toEqual(expected);}

test("matching original guards publish local evidence without issuing or releasing ownership",async()=>{
 const s=await setup(),row=await verify(s);expect(row.status).toBe("prepared");expect(row.effects.map(e=>e.status)).toEqual(["verified","prepared"]);
 expect(JSON.parse(row.effects[0].proofText!)).toEqual(proof());expect(Object.isFrozen(row.effects[0])).toBe(true);
 expect(await s.journal.issueEffect(gate,row,"deliver")).toBe(false);await expect(s.journal.reserveMutation(gate,SECOND,"deliver",{})).rejects.toThrow();await s.journal.close();
});
test.each([false,true])("same-transaction creation predicates apply before publication/reuse (reused=%s)",async reused=>{
 for(const change of ["missing","destination","fingerprint","generation","submitted","pending"]){
  const s=await setup(),before=reused?await verify(s):s.intent;
  await raw(s,"creations",[ACCOUNT,KEY],v=>change==="missing"?undefined:{...v,...change==="destination"?{destination:CONTROL}:change==="fingerprint"?{fingerprint:"b".repeat(64)}:change==="generation"?{createdUnderGeneration:OLD}:change==="pending"?{status:"pending",destination:null}:{status:"submitted"}});
  await expect(verify(s)).rejects.toThrow();await unchanged(s,before);await s.journal.close();
 }
});
test.each([false,true])("same-transaction tag predicates apply before publication/reuse (reused=%s)",async reused=>{
 for(const change of ["missing","destination","name","generation","submitted","pending"]){
  const s=await setup(),before=reused?await verify(s):s.intent;
  await raw(s,"tags",[ACCOUNT,"original"],v=>change==="missing"?undefined:{...v,...change==="destination"?{destination:SECOND}:change==="name"?{name:"Renamed"}:change==="generation"?{createdUnderGeneration:OLD}:change==="pending"?{status:"pending",destination:null}:{status:"submitted"}});
  await expect(verify(s)).rejects.toThrow();await unchanged(s,before);await s.journal.close();
 }
});
test.each(["creation","tag"])("newly substituted matching-destination %s tuple cannot replace original authority",async kind=>{
 const s=await setup(),authority=structuredClone(s.authority);
 if(kind==="creation"){authority.creations[0].createdUnderGeneration=OLD;await raw(s,"creations",[ACCOUNT,KEY],()=>authority.creations[0]);}
 else{authority.tags[0].name="Replacement";await raw(s,"tags",[ACCOUNT,"original"],()=>authority.tags[0]);}
 await expect(verify(s,{authority})).rejects.toThrow();await unchanged(s);await s.journal.close();
});
test.each(["undefined","null","empty","extra","duplicate","other-account","different-original"])("invalid caller authority %s cannot publish or reuse proof",async mode=>{
 const s=await setup(),before=await verify(s);let authority:any=structuredClone(s.authority);
 if(mode==="undefined")authority=undefined;if(mode==="null")authority=null;if(mode==="empty")authority.creations=[];
 if(mode==="extra")authority.trusted=true;if(mode==="duplicate")authority.creations.push(authority.creations[0]);
 if(mode==="other-account")authority.tags[0].accountId=TAG;if(mode==="different-original")authority.tags[0].createdUnderGeneration=OLD;
 await expect(verify(s,{authority})).rejects.toThrow();await unchanged(s,before);await s.journal.close();
});
test("guardless historical manifest remains unchanged while compatibility evidence still works",async()=>{
 const s=await setup({guardless:true});await expect(verify(s)).rejects.toThrow();await unchanged(s);
 const before=await s.journal.verifyEffect(gate,s.intent,"deliver",proof());await expect(verify(s)).rejects.toThrow();await unchanged(s,before);await s.journal.close();
});
test("a phase destination outside original creations is held",async()=>{const s=await setup({wrongDestination:true});await expect(verify(s)).rejects.toThrow();await unchanged(s);await s.journal.close();});
test.each([false,true])("native phase retains every original creation/tag guard (exclude=%s)",async exclude=>{
 const s=await setup({exclude,multiple:true}),first=exclude?"policy":"deliver",native=exclude?"delete":"repair";
 await expect(verify(s,{phase:native,proof:proof("not-needed")})).rejects.toThrow();
 const before=await verify(s,{phase:first});await raw(s,"creations",[ACCOUNT,"collect.control.v1"],v=>({...v,createdUnderGeneration:OLD}));
 await expect(verify(s,{phase:native,proof:proof("not-needed")})).rejects.toThrow();await unchanged(s,before);await s.journal.close();
});
test("canonical array and property order and older reservation generations preserve exact tuple meaning",async()=>{
 const s=await setup({multiple:true,older:true}),reverse=(value:object)=>Object.fromEntries(Object.entries(value).reverse());
 const authority={tags:s.authority.tags.slice().reverse().map(reverse),creations:s.authority.creations.slice().reverse().map(reverse)};
 await raw(s,"creations",[ACCOUNT,KEY],reverse);const one=await verify(s,{authority});
 const two=await verify(s,{authority,phase:"repair",proof:proof("not-needed")});expect(two.effects.map(e=>e.status)).toEqual(["verified","verified"]);
 expect(await verify(s,{authority})).toEqual(two);expect(one.status).toBe("prepared");await s.journal.close();
});
test("proof, intent and authority detach before asynchronous hashing",async()=>{
 const s=await setup(),authority=structuredClone(s.authority),intent=structuredClone(s.intent),evidence=proof();
 const pending=verify(s,{authority,intent,proof:evidence});authority.creations.length=0;authority.tags[0].destination=SECOND;evidence.evidence.context="replacement";intent.effects[0].status="issued";
 const result=await pending;expect(JSON.parse(result.effects[0].proofText!)).toEqual(proof());expect(result.effects[0].status).toBe("verified");await s.journal.close();
});
test("one competing proof context commits and the other cannot overwrite its immutable evidence",async()=>{
 const s=await setup(),peer=new V1Journal({indexedDB:s.indexedDB});const results=await Promise.allSettled([verify(s),verify(s,{journal:peer,proof:proof("present","competing")})]);
 expect(results.filter(r=>r.status==="fulfilled")).toHaveLength(1);expect(results.filter(r=>r.status==="rejected")).toHaveLength(1);
 const winner=(results.find(r=>r.status==="fulfilled") as PromiseFulfilledResult<V1PhasedMutation>).value;
 await unchanged(s,winner);expect(await verify(s,{proof:JSON.parse(winner.effects[0].proofText!)})).toEqual(winner);await peer.close();await s.journal.close();
});
test("identical retries across contexts and restart reuse one proof while original owner is held",async()=>{
 const s=await setup(),peer=new V1Journal({indexedDB:s.indexedDB});const [one,two]=await Promise.all([verify(s),verify(s,{journal:peer})]);expect(one).toEqual(two);
 await s.journal.close();expect(await verify(s,{journal:peer})).toEqual(one);
 await expect(peer.reserveMutation(gate,SECOND,"deliver",{})).rejects.toThrow();await peer.close();
});
test.each([false,true])("guarded proof reuse refuses terminal original ownership (newer owner=%s)",async newer=>{
 const s=await setup(),one=await verify(s),both=await verify(s,{phase:"repair",proof:proof("not-needed")});
 const final=await s.journal.verifyMutation(gate,both,{version:1,outcome:"delivered",evidence:{}});
 const next=newer?(await s.journal.reserveMutation(gate,SECOND,"deliver",{})).intent:null;
 await expect(verify(s,{intent:one})).rejects.toThrow();expect(await s.journal.verifyEffect(gate,s.intent,"deliver",proof())).toEqual(final);
 expect(await s.journal.readMutation(gate,ACTION)).toEqual(final);
 if(next){expect(await s.journal.readMutation(gate,SECOND)).toEqual(next);await expect(s.journal.reserveMutation(gate,OLD,"deliver",{})).rejects.toThrow();}
 await s.journal.close();
});
test("guard change between digest and proof transaction is held",async()=>{
 const s=await setup(),digest=crypto.subtle.digest.bind(crypto.subtle);let release!:()=>void,entered!:()=>void;
 const waiting=new Promise<void>(r=>{release=r;}),started=new Promise<void>(r=>{entered=r;});
 const spy=vi.spyOn(crypto.subtle,"digest").mockImplementation(async(...args)=>{entered();await waiting;return digest(...args);});
 try{const pending=verify(s);await started;await raw(s,"tags",[ACCOUNT,"original"],v=>({...v,name:"Changed"}));release();await expect(pending).rejects.toThrow();}
 finally{release();spy.mockRestore();}await unchanged(s);await s.journal.close();
});
test.each(["cancelled","expired"])("%s before guarded proof leaves original row untouched",async mode=>{
 const s=await setup(),c=new AbortController();if(mode==="cancelled")c.abort();await expect(verify(s,{signal:c.signal,deadline:mode==="expired"?0:undefined})).rejects.toThrow();await unchanged(s);await s.journal.close();
});
test.each(["cancelled","expired"])("%s while hashing cannot publish guarded proof",async mode=>{
 const s=await setup(),c=new AbortController(),digest=crypto.subtle.digest.bind(crypto.subtle);let release!:()=>void,now=0;
 const waiting=new Promise<void>(r=>{release=r;}),clock=vi.spyOn(performance,"now").mockImplementation(()=>now),spy=vi.spyOn(crypto.subtle,"digest").mockImplementation(async(...args)=>{await waiting;return digest(...args);});
 try{const pending=verify(s,{signal:c.signal,deadline:10});if(mode==="cancelled")c.abort();else now=10;release();await expect(pending).rejects.toThrow();}
 finally{release();spy.mockRestore();clock.mockRestore();}await unchanged(s);await s.journal.close();
});
test.each(["mutations","currentMutation"])("cancellation after %s put succeeds aborts the proof transaction",async store=>{
 const s=await setup(),c=new AbortController(),put=IDBObjectStore.prototype.put;
 const spy=vi.spyOn(IDBObjectStore.prototype,"put").mockImplementation(function(this:IDBObjectStore,...args){const request=put.apply(this,args);if(this.name===store)request.addEventListener("success",()=>c.abort());return request;});
 try{await expect(verify(s,{signal:c.signal})).rejects.toThrow();}finally{spy.mockRestore();}await unchanged(s);expect((await verify(s)).effects[0].status).toBe("verified");await s.journal.close();
});
test("quota failure aborts both phase proof and owner pointer",async()=>{
 const s=await setup(),put=IDBObjectStore.prototype.put;const spy=vi.spyOn(IDBObjectStore.prototype,"put").mockImplementation(function(this:IDBObjectStore,...args){if(this.name==="currentMutation")throw new DOMException("Synthetic quota","QuotaExceededError");return put.apply(this,args);});
 try{await expect(verify(s)).rejects.toThrow();}finally{spy.mockRestore();}await unchanged(s);expect((await verify(s)).effects[0].status).toBe("verified");await s.journal.close();
});
test("deadline reached on a guard read aborts publication",async()=>{
 const s=await setup(),get=IDBObjectStore.prototype.get;let now=0;const clock=vi.spyOn(performance,"now").mockImplementation(()=>now);
 const spy=vi.spyOn(IDBObjectStore.prototype,"get").mockImplementation(function(this:IDBObjectStore,key){const request=get.call(this,key);if(this.name==="tags")request.addEventListener("success",()=>{now=10;});return request;});
 try{await expect(verify(s,{deadline:10})).rejects.toThrow();}finally{spy.mockRestore();clock.mockRestore();}await unchanged(s);await s.journal.close();
});
