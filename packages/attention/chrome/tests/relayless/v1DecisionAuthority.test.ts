// @vitest-environment node
import {expect,test,vi} from 'vitest';
import {actorFixture,TARGET,CONTROL,TAG,gate} from './v1ActionActorFixture';
import {withV1DecisionAuthority,requireV1DecisionCapability,observeV1Decision,type V1DecisionCapability} from '../../src/relayless/v1DecisionAuthority';
import {prepareV1TypedEffect} from '../../src/relayless/v1TypedEffect';
import {readV1Exclusions} from '../../src/relayless/v1ExclusionPolicy';
import {reconcileV1Action} from '../../src/relayless/v1ActionActor';
import {V1Journal} from '../../src/relayless/v1Journal';
type Fixture=Awaited<ReturnType<typeof actorFixture>>;
function options(s:Fixture,extra={}) {return {http:s.http,journal:s.journal,intent:s.intent,authority:s.authority,target:TARGET,control:CONTROL,deadline:performance.now()+30000,signal:new AbortController().signal,...extra};}
const postCount=(s:Fixture)=>s.calls.filter(c=>c.init.method==='POST').length;

test('closed snapshot uses a complete opening and closing inventory around all shared metadata',async()=>{
 const s=await actorFixture(),o=options(s);
 await withV1DecisionAuthority(o,async(http,proof)=>{expect(await requireV1DecisionCapability(proof,http,s.journal,o.deadline,o.signal,s.intent,s.authority)).toMatchObject({target:TARGET,control:CONTROL});});
 const paths=s.calls.map(c=>new URL(c.url).pathname),catalogues=paths.map((p,i)=>p==='/input/v1/data_type'?i:-1).filter(i=>i>=0);
 expect(catalogues).toHaveLength(2);expect(paths[0]).toBe('/user/v1alpha1/info');
 for(const p of [`/data/v1/catalog/${TARGET}/v1/schema`,`/data/v1/catalog/${CONTROL}/v1/schema`,`/user/v1alpha1/tag/id/${TAG}`,'/data/v1/catalog/DeletedRecord/v1/schema'])expect(paths.indexOf(p)).toBeGreaterThan(catalogues[0]);
 expect(catalogues[1]).toBe(paths.length-1);expect(postCount(s)).toBe(0);await s.journal.close();
});
for(const target of [TARGET,CONTROL])test('input mutation within shared catalogue closure is unknown: '+target,async()=>{
 const s=await actorFixture();let inventories=0,called=false;
 s.state.beforeRead=async path=>{if(path==='/input/v1/data_type'&&++inventories===2)s.state.owned.find(r=>r.id===target)!.record_spec.schema=JSON.stringify({...JSON.parse(s.state.owned.find(r=>r.id===target)!.record_spec.schema),description:'changed'});};
 await expect(withV1DecisionAuthority(options(s),async()=>{called=true;})).rejects.toThrow();expect(called).toBe(false);expect(postCount(s)).toBe(0);await s.journal.close();
});
for(const mode of ['missing-control','retired-control','duplicate-inventory','foreign-inventory-owner','served-target','served-control','tag-owner','tag-deleted','native-schema'])test('no capability is minted from incomplete authority: '+mode,async()=>{
 const s=await actorFixture();let called=false;
 if(mode==='missing-control')s.state.owned.pop();if(mode==='retired-control')s.state.owned[1].deprecated=true;
 if(mode==='duplicate-inventory')s.state.owned.push(s.state.owned[0]);if(mode==='foreign-inventory-owner')(s.state.owned[1] as any).userid=TAG;
 if(mode.startsWith('served-'))s.state.servedOverrides[`/data/v1/catalog/${mode==='served-target'?TARGET:CONTROL}/v1/schema`]={};
 if(mode==='tag-owner')s.state.tag.userid=TAG;if(mode==='tag-deleted')(s.state.tag as any).deleted_at='2026-01-01T00:00:00Z';
 if(mode==='native-schema')s.state.nativeSchema.properties.record_id={type:'number'};
 await expect(withV1DecisionAuthority(options(s),async()=>{called=true;})).rejects.toThrow();expect(called).toBe(false);expect(postCount(s)).toBe(0);await s.journal.close();
});
for(const mode of ['serialized','foreign-http','foreign-journal','wider-deadline','different-signal','different-intent','different-authority'])test('opaque snapshot cannot transfer authority: '+mode,async()=>{
 const s=await actorFixture(),o=options(s),other=new V1Journal({indexedDB:s.indexedDB});
 let rejected=false;const callback=withV1DecisionAuthority(o,async(http,proof)=>{
  const badProof=mode==='serialized'?JSON.parse(JSON.stringify(proof)):proof,badHttp=mode==='foreign-http'?s.makeHttp():http;
  try{await requireV1DecisionCapability(badProof,badHttp,mode==='foreign-journal'?other:s.journal,mode==='wider-deadline'?o.deadline+1:o.deadline,mode==='different-signal'?new AbortController().signal:o.signal,mode==='different-intent'?{...s.intent,actionId:TAG}:s.intent,mode==='different-authority'?{...s.authority,tags:[]}:s.authority);}catch{rejected=true;}
 });
 if(mode==='serialized')await callback;else await expect(callback).rejects.toThrow();
 expect(rejected).toBe(true);expect(postCount(s)).toBe(0);await other.close();await s.journal.close();
});
test('proof and read clone expire at callback boundary and cannot be replayed in a later decision',async()=>{
 const s=await actorFixture(),o=options(s);let saved!:V1DecisionCapability,reads!:typeof s.http;
 await withV1DecisionAuthority(o,async(http,proof)=>{saved=proof;reads=http;const narrowed=http.withReadCancellation(new AbortController().signal);await requireV1DecisionCapability(proof,narrowed,s.journal,o.deadline,o.signal);});
 await expect(requireV1DecisionCapability(saved,reads,s.journal,o.deadline,o.signal)).rejects.toThrow();
 const count=s.calls.length;await expect(reads.json('/input/v1/data_type?include_deprecated=true')).rejects.toThrow();expect(s.calls).toHaveLength(count);
 await withV1DecisionAuthority(o,async(http)=>{await expect(requireV1DecisionCapability(saved,http,s.journal,o.deadline,o.signal)).rejects.toThrow();});await s.journal.close();
});
test('forged policy and typed seams cannot bypass independently safe defaults',async()=>{
 const s=await actorFixture(),o=options(s);
 await expect(readV1Exclusions({http:s.http,journal:s.journal,targetDataType:TARGET,recordIds:[s.entry.record.id],decision:{proof:{},deadline:o.deadline,signal:o.signal}})).rejects.toThrow();
 await expect(prepareV1TypedEffect(s.http,s.journal,s.intent,'deliver',s.authority,o.deadline,o.signal,{})).rejects.toThrow();expect(s.calls).toHaveLength(0);
 s.state.owned.pop();await expect(prepareV1TypedEffect(s.http,s.journal,s.intent,'deliver',s.authority,o.deadline,o.signal)).rejects.toThrow();expect(postCount(s)).toBe(0);await s.journal.close();
});
test('read principal session grants no typed or setup dispatch',async()=>{
 const s=await actorFixture(),o=options(s);
 await s.http.withPrincipalReads(o.deadline,o.signal,async http=>{
  await expect(http.submitEffect({journal:s.journal,intent:s.intent,name:'deliver',authority:s.authority})).rejects.toThrow();
  await expect(http.json('/input/v1/data_type/Event',{method:'POST',body:{}})).rejects.toThrow();
 });expect(postCount(s)).toBe(0);await s.journal.close();
});
for(const point of ['/info','/input/v1/data_type','/tag/id/',`${TARGET}/v1/schema`,`${CONTROL}/v1/schema`,'DeletedRecord/v1/schema','/data/v1/records'])test('same-account bearer replacement invalidates read decision at '+point,async()=>{
 const s=await actorFixture();let changed=false;s.state.beforeRead=async path=>{if(!changed&&path.includes(point)){changed=true;s.state.tokenRevision++;}};
 expect((await reconcileV1Action({http:s.http,journal:s.journal,intent:s.intent})).status).toBe('unknown');expect(changed).toBe(true);expect(postCount(s)).toBe(0);await s.journal.close();
});
for(const mode of ['token-body','principal-B','gate-token','crypto','cancel-crypto'])test('fresh guards close asynchronous authority seams: '+mode,async()=>{
 const s=await actorFixture(),c=new AbortController();let hit=false,infos=0;
 if(mode==='token-body'){s.state.onBody=async path=>{if(!hit&&path==='/input/v1/data_type'){hit=true;s.state.tokenRevision++;}};}
 if(mode==='principal-B')s.state.beforeRead=async path=>{if(path.endsWith('/info')&&++infos===2){hit=true;s.state.principal=TAG;}};
 if(mode==='gate-token')s.state.beforeToken=async()=>{if(!hit){hit=true;await s.journal.transition(gate,{...gate,enabled:false});}};
 const digest=crypto.subtle.digest.bind(crypto.subtle),spy=mode.includes('crypto')?vi.spyOn(crypto.subtle,'digest').mockImplementation(async(...args)=>{const result=await digest(...args);if(!hit){hit=true;if(mode==='cancel-crypto')c.abort();else s.state.tokenRevision++;}return result;}):null;
 try{expect((await reconcileV1Action({http:s.http,journal:s.journal,intent:s.intent,signal:c.signal})).status).toBe('unknown');expect(hit).toBe(true);expect(postCount(s)).toBe(0);}finally{spy?.mockRestore();await s.journal.close();}
});
test('fresh B observes a policy/source pair and no intermediate metadata result is published',async()=>{
 const s=await actorFixture();let infos=0;const o=options(s);
 s.state.beforeRead=async path=>{if(path.endsWith('/info')&&++infos===2)s.state.nativeSchema.description='changed during current records';};
 await expect(observeV1Decision({...o,recordId:s.entry.record.id,payload:s.entry.record})).rejects.toThrow();expect(infos).toBe(2);expect(postCount(s)).toBe(0);await s.journal.close();
});
test('bounded session cancels a hung principal and rejects late nested work',async()=>{
 const s=await actorFixture(),o=options(s,{deadline:performance.now()+40});let release!:()=>void,entered=false;
 s.state.beforeRead=async()=>{entered=true;await new Promise<void>(resolve=>{release=resolve;});};
 await expect(withV1DecisionAuthority(o,async http=>{await http.json('/input/v1/data_type?include_deprecated=true');})).rejects.toThrow();
 expect(entered).toBe(true);expect(s.calls).toHaveLength(1);expect(s.calls[0].init.signal?.aborted).toBe(true);release();await new Promise(resolve=>setTimeout(resolve,10));expect(s.calls).toHaveLength(1);await s.journal.close();
});
test('policy loss and native revival after effects hold original work with no second dispatch',async()=>{
 for(const mode of ['policy-loss','native-revival']){const s=await actorFixture(false,true);s.state.rows[TARGET]=[s.entry.record];s.state.afterPost=async()=>{if(mode==='policy-loss')s.state.rows[CONTROL]=[];else if(s.calls.filter(c=>c.init.method==='POST').at(-1)?.url.includes('DeletedRecord'))s.state.rows[TARGET]=[s.entry.record];};
 expect((await reconcileV1Action({http:s.http,journal:s.journal,intent:s.intent})).status).toBe('unknown');const count=postCount(s);expect(count).toBe(mode==='policy-loss'?1:2);expect((await reconcileV1Action({http:s.http,journal:s.journal,intent:s.intent})).status).toBe('unknown');expect(postCount(s)).toBe(count);await s.journal.close();}
});
test('dispatch on the same HTTP authority invalidates a still-live snapshot proof',async()=>{
 const s=await actorFixture(),o=options(s);let reached=false;
 await expect(withV1DecisionAuthority(o,async(http,proof)=>{
  await s.http.submitEffect({journal:s.journal,intent:s.intent,name:'deliver',authority:s.authority});
  reached=true;await requireV1DecisionCapability(proof,http,s.journal,o.deadline,o.signal);
 })).rejects.toThrow();expect(reached).toBe(true);expect(postCount(s)).toBe(1);await s.journal.close();
});
test('a failed protected read poisons the current capability even if caller catches it',async()=>{
 const s=await actorFixture(),o=options(s);
 await expect(withV1DecisionAuthority(o,async(http,proof)=>{
  s.state.recordStatus=206;await expect(http.records(TARGET,[s.entry.record.id])).rejects.toThrow();
  await requireV1DecisionCapability(proof,http,s.journal,o.deadline,o.signal);
 })).rejects.toThrow();expect(postCount(s)).toBe(0);await s.journal.close();
});
test('a swallowed contradictory fresh principal invalidates the entire decision',async()=>{
 const s=await actorFixture(),o=options(s);
 await expect(withV1DecisionAuthority(o,async(http,proof)=>{
  s.state.principal=TAG;await expect(http.proveReadPrincipal(o.deadline,o.signal)).rejects.toThrow();s.state.principal=gate.accountId!;
  await requireV1DecisionCapability(proof,http,s.journal,o.deadline,o.signal);
 })).rejects.toThrow();await s.journal.close();
});
test.each(['operation','version','requestBytes'])('proof is bound to original immutable intent field %s',async key=>{
 const s=await actorFixture(),o=options(s);
 let rejected=false;await expect(withV1DecisionAuthority(o,async(http,proof)=>{const intent={...s.intent,[key]:key==='operation'?'exclude':key==='version'?1:s.intent.requestBytes+1};try{await requireV1DecisionCapability(proof,http,s.journal,o.deadline,o.signal,intent as any,s.authority);}catch{rejected=true;}})).rejects.toThrow();expect(rejected).toBe(true);await s.journal.close();
});
test('winning issue CAS is the last authority await before the single application POST',async()=>{
 const s=await actorFixture();let won=false,posts=0;const issue=s.journal.issueGuardedEffect.bind(s.journal),assert=s.protection.protection.assert.bind(s.protection.protection),digest=crypto.subtle.digest.bind(crypto.subtle);
 s.journal.issueGuardedEffect=async(...args)=>{const result=await issue(...args);won=result;return result;};
 s.state.beforeToken=async()=>{if(won)throw Error('forbidden post-CAS token await');};
 s.protection.protection.assert=async()=>{if(won)throw Error('forbidden post-CAS DNR await');return assert();};
 s.state.afterPost=async()=>{posts++;won=false;};
 const spy=vi.spyOn(crypto.subtle,'digest').mockImplementation(async(...args)=>{if(won)throw Error('forbidden post-CAS digest await');return digest(...args);});
 try{await expect(s.http.submitEffect({journal:s.journal,intent:s.intent,name:'deliver',authority:s.authority})).resolves.toEqual({upload_id:TAG});expect(posts).toBe(1);expect(postCount(s)).toBe(1);}finally{spy.mockRestore();await s.journal.close();}
});
test('cancellation immediately after issue commit prevents dispatch and never restores once permission',async()=>{
 const s=await actorFixture(),c=new AbortController(),issue=s.journal.issueGuardedEffect.bind(s.journal);
 s.journal.issueGuardedEffect=async(...args)=>{const result=await issue(...args);if(result)c.abort();return result;};
 await expect(s.http.submitEffect({journal:s.journal,intent:s.intent,name:'deliver',authority:s.authority,signal:c.signal})).rejects.toThrow();
 expect(postCount(s)).toBe(0);expect((await s.journal.readMutation(gate,s.intent.actionId))?.effects?.[0].status).toBe('issued');
 await expect(s.http.submitEffect({journal:s.journal,intent:s.intent,name:'deliver',authority:s.authority})).rejects.toThrow();expect(postCount(s)).toBe(0);await s.journal.close();
});
test('same-account replacement at fresh pre-POST principal leaves original phase prepared',async()=>{
 const s=await actorFixture();let infos=0;s.state.beforeRead=async path=>{if(path.endsWith('/info')&&++infos===2)s.state.tokenRevision++;};
 await expect(s.http.submitEffect({journal:s.journal,intent:s.intent,name:'deliver',authority:s.authority})).rejects.toThrow();expect(infos).toBe(2);expect(postCount(s)).toBe(0);expect((await s.journal.readMutation(gate,s.intent.actionId))?.effects?.[0].status).toBe('prepared');await s.journal.close();
});
test('generation rotation during issue preparation holds original phase before commit',async()=>{
 const s=await actorFixture(),issue=s.journal.issueGuardedEffect.bind(s.journal),next={...gate,generation:TAG};let entered=false;
 s.journal.issueGuardedEffect=async(...args)=>{entered=true;await s.journal.transition(gate,next);return issue(...args);};
 await expect(s.http.submitEffect({journal:s.journal,intent:s.intent,name:'deliver',authority:s.authority})).rejects.toThrow();expect(entered).toBe(true);expect(postCount(s)).toBe(0);expect((await s.journal.readMutation(next,s.intent.actionId))?.effects?.[0].status).toBe('prepared');await s.journal.close();
});
test('internal policy capability cannot widen exact source record scope',async()=>{
 const s=await actorFixture(),o=options(s);
 let rejected=false;await expect(withV1DecisionAuthority(o,async(http,proof)=>{try{await readV1Exclusions({http,journal:s.journal,targetDataType:TARGET,recordIds:[TAG],decision:{proof,deadline:o.deadline,signal:o.signal}});}catch{rejected=true;}})).rejects.toThrow();expect(rejected).toBe(true);await s.journal.close();
});
test('setup POST on the same root also invalidates a live read decision',async()=>{
 const s=await actorFixture(),o=options(s);let posted=false;
 await expect(withV1DecisionAuthority(o,async(http,proof)=>{await s.http.json('/input/v1/data_type/Event',{method:'POST',body:{name:'Synthetic setup'}});posted=true;await requireV1DecisionCapability(proof,http,s.journal,o.deadline,o.signal);})).rejects.toThrow();expect(posted).toBe(true);expect(postCount(s)).toBe(1);await s.journal.close();
});
test('fresh principal respects the original HTTP per-request limit inside a longer decision',async()=>{
 const s=await actorFixture();s.http=s.makeHttp(s.journal,gate,{timeoutMs:25});s.state.beforeRead=async()=>new Promise<void>(()=>{});
 const started=performance.now(),result=await reconcileV1Action({http:s.http,journal:s.journal,intent:s.intent,timeoutMs:500});
 expect(result.status).toBe('unknown');expect(s.calls[0].init.signal?.aborted).toBe(true);expect(performance.now()-started).toBeLessThan(200);await s.journal.close();
});
for(const failure of ['malformed-policy','policy-journal','policy-input','typed-native','typed-policy','typed-journal','typed-authority'])test('caught helper failure revokes capability and callback publication: '+failure,async()=>{
 const s=await actorFixture(false,failure==='typed-policy',failure==='typed-native'?'native-action':failure==='typed-policy'?'policy-action':''),o=options(s);
 let finish!:()=>void,callbackEntered=false;const finished=new Promise<void>(resolve=>{finish=resolve;});
 let readRejected=false,proofSurvived=false,callbackPublished=false,readSessionSurvived=false,extraReadCalls=0;
 try{
  await withV1DecisionAuthority(o,async(http,proof)=>{
   callbackEntered=true;try{
   let restore:(()=>void)|undefined;
   if(failure==='malformed-policy')s.state.rows[CONTROL]=[{...s.policy,action_id:'malformed'}];
   if(failure==='policy-journal'){const spy=vi.spyOn(s.journal,'readCreation').mockRejectedValue(Error('Synthetic unreadable control binding'));restore=()=>spy.mockRestore();}
   if(failure==='typed-journal'){const spy=vi.spyOn(s.journal,'readEffect').mockRejectedValue(Error('Synthetic unreadable original body'));restore=()=>spy.mockRestore();}
   try{
    if(failure.startsWith('typed-'))await prepareV1TypedEffect(http,s.journal,s.intent,failure==='typed-policy'?'policy':'deliver',failure==='typed-authority'?{...s.authority,tags:[]}:s.authority,o.deadline,o.signal,proof);
    else await readV1Exclusions({http,journal:s.journal,targetDataType:TARGET,recordIds:failure==='policy-input'?[]:[s.entry.record.id],decision:{proof,deadline:o.deadline,signal:o.signal}});
   }catch{readRejected=true;}finally{restore?.();}
   try{await requireV1DecisionCapability(proof,http,s.journal,o.deadline,o.signal,s.intent,s.authority);proofSurvived=true;}catch{}
   const count=s.calls.length;
   try{await http.json('/input/v1/data_type?include_deprecated=true');readSessionSurvived=true;}catch{}
   extraReadCalls=s.calls.length-count;
   return {mustNotPublish:true};
   }finally{finish();}
  });callbackPublished=true;
 }catch{}
 if(callbackEntered)await finished;
 expect({readRejected,proofSurvived,callbackPublished,readSessionSurvived,extraReadCalls,applicationPosts:postCount(s)}).toEqual({readRejected:true,proofSurvived:false,callbackPublished:false,readSessionSurvived:false,extraReadCalls:0,applicationPosts:0});
 await s.journal.close();
});
test('forged helper proof cannot revoke a different real decision capability',async()=>{
 const s=await actorFixture(),o=options(s);let published=false;
 await withV1DecisionAuthority(o,async(http,proof)=>{
  s.state.rows[CONTROL]=[{...s.policy,action_id:'malformed'}];
  await expect(readV1Exclusions({http,journal:s.journal,targetDataType:TARGET,recordIds:[s.entry.record.id],decision:{proof:{},deadline:o.deadline,signal:o.signal}})).rejects.toThrow();
  await expect(prepareV1TypedEffect(http,s.journal,s.intent,'deliver',s.authority,o.deadline,o.signal,{})).rejects.toThrow();
  await requireV1DecisionCapability(proof,http,s.journal,o.deadline,o.signal,s.intent,s.authority);
 });published=true;expect(published).toBe(true);expect(postCount(s)).toBe(0);await s.journal.close();
});
test('semantic helper failure cancels an in-flight descendant read and prevents late continuation',async()=>{
 const s=await actorFixture(),o=options(s,{deadline:performance.now()+2000});let release:(()=>void)|undefined,entered!:()=>void;const seen=new Promise<void>(resolve=>{entered=resolve;});
 let finish!:()=>void,callbackEntered=false;const finished=new Promise<void>(resolve=>{finish=resolve;});
 let helperRejected=false,childAborted=false,childSucceeded:boolean|null=null,lateReadCalls=0,published=false;
 try{
  await withV1DecisionAuthority(o,async(http,proof)=>{
   callbackEntered=true;try{
   s.state.beforeRead=async path=>{if(path.endsWith(`${TARGET}/v1/schema`)){entered();await new Promise<void>(resolve=>{release=resolve;});}};
   const pending=http.json(`/data/v1/catalog/${TARGET}/v1/schema`).then(()=>true,()=>false);await seen;
   const child=s.calls.at(-1)!;s.state.rows[CONTROL]=[{...s.policy,action_id:'malformed'}];
   try{await readV1Exclusions({http,journal:s.journal,targetDataType:TARGET,recordIds:[s.entry.record.id],decision:{proof,deadline:o.deadline,signal:o.signal}});}catch{helperRejected=true;}
   childAborted=child.init.signal?.aborted??false;childSucceeded=await pending;
   const count=s.calls.length;release?.();await new Promise(resolve=>setTimeout(resolve,10));lateReadCalls=s.calls.length-count;
   }finally{finish();}
  });published=true;
 }catch{}finally{release?.();}
 if(callbackEntered)await finished;
 expect({helperRejected,childAborted,childSucceeded,lateReadCalls,published,applicationPosts:postCount(s)}).toEqual({helperRejected:true,childAborted:true,childSucceeded:false,lateReadCalls:0,published:false,applicationPosts:0});await s.journal.close();
});
test('semantic helper failure immediately aborts a fresh-principal descendant',async()=>{
 const s=await actorFixture();s.http=s.makeHttp(s.journal,gate,{timeoutMs:500});const o=options(s,{deadline:performance.now()+2000});
 let entered!:()=>void,release:(()=>void)|undefined;const seen=new Promise<void>(resolve=>{entered=resolve;});
 let finish!:()=>void,callbackEntered=false;const finished=new Promise<void>(resolve=>{finish=resolve;});
 let helperRejected=false,principalAborted=false,principalSucceeded:boolean|null=null,published=false;
 try{
  await withV1DecisionAuthority(o,async(http,proof)=>{
   callbackEntered=true;try{
   s.state.beforeRead=async path=>{if(path.endsWith('/info')){entered();await new Promise<void>(resolve=>{release=resolve;});}};
   const pending=http.proveReadPrincipal(o.deadline,o.signal).then(()=>true,()=>false);await seen;
   const child=s.calls.at(-1)!;s.state.rows[CONTROL]=[{...s.policy,action_id:'malformed'}];
   try{await readV1Exclusions({http,journal:s.journal,targetDataType:TARGET,recordIds:[s.entry.record.id],decision:{proof,deadline:o.deadline,signal:o.signal}});}catch{helperRejected=true;}
   principalAborted=child.init.signal?.aborted??false;principalSucceeded=await pending;
   }finally{finish();}
  });published=true;
 }catch{}finally{release?.();}
 if(callbackEntered)await finished;
 expect({helperRejected,principalAborted,principalSucceeded,published,applicationPosts:postCount(s)}).toEqual({helperRejected:true,principalAborted:true,principalSucceeded:false,published:false,applicationPosts:0});await s.journal.close();
});
test('already-aborted inherited principal session cannot start a request',async()=>{
 const s=await actorFixture(),c=new AbortController(),o=options(s);c.abort();
 await expect(s.http.withReadCancellation(c.signal).withPrincipalReads(o.deadline,o.signal,async()=>{})).rejects.toThrow();expect(s.calls).toHaveLength(0);await s.journal.close();
});
for(const outcome of ['success','error'])test('principal session removes inherited abort listeners after '+outcome,async()=>{
 const s=await actorFixture(),c=new AbortController(),o=options(s),active=new Set<unknown>();
 const add=c.signal.addEventListener.bind(c.signal),remove=c.signal.removeEventListener.bind(c.signal);
 const added=vi.spyOn(c.signal,'addEventListener').mockImplementation((type,listener,options)=>{if(type==='abort')active.add(listener);add(type,listener,options);});
 const removed=vi.spyOn(c.signal,'removeEventListener').mockImplementation((type,listener,options)=>{if(type==='abort')active.delete(listener);remove(type,listener,options);});
 try{
  const pending=s.http.withReadCancellation(c.signal).withPrincipalReads(o.deadline,o.signal,async http=>{await http.proveReadPrincipal(o.deadline,o.signal);if(outcome==='error')throw Error('Synthetic completed principal callback failure');return 'complete';});
  if(outcome==='error')await expect(pending).rejects.toThrow();else expect(await pending).toBe('complete');
  expect(active.size).toBe(0);c.abort();expect(await s.http.json('/input/v1/data_type?include_deprecated=true')).toHaveLength(2);
 }finally{added.mockRestore();removed.mockRestore();await s.journal.close();}
});
