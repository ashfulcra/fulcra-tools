// Chrome MV3 worker owner. No callback API exposes reusable HTTP/journal roots.
import {V1AuthStorage,V1AuthError,authHold,exactAuth,makeV1Credential,strictV1Settings,type V1AuthState,type V1Publication,type V1RefreshAttempt,type V1ScopeTransition,type V1Credential} from './v1AuthStorage';
import {V1Journal,type V1Gate,type V1MutationIntent} from './v1Journal';
import {V1Http,snapshotV1JSON,parseV1JSON} from './v1Http';
import {settleV1Action,reconcileV1Action} from './v1ActionActor';
import {resolveAttentionV1} from './v1Destination';
import {resolveExclusionV1} from './v1ExclusionDestination';
import {resolveV1Tag} from './v1Tags';
import type {V1ReadProtection} from './v1ReadProtection';
import {readV1OwnedTypes} from './v1OwnedTypes';
import {FulcraOidc,type FetchFn,type DeviceCodeResponse} from './oidc';
import {sha256Hex} from './sha256';
import type {Settings} from '../types';
import {API_BASE,OIDC_CLIENT_ID,OIDC_DOMAIN,OIDC_AUDIENCE,OIDC_SCOPE} from './config';
import {registerAuth0OriginStrip} from './authOriginFix';
const config=Object.freeze({clientId:OIDC_CLIENT_ID,issuer:OIDC_DOMAIN,audience:OIDC_AUDIENCE,scope:OIDC_SCOPE});
const EMPTY_SCOPE='0'.repeat(64);
type ScopeClaim={signal:AbortSignal;transition:V1ScopeTransition|null;};
type Lease={controller:AbortController;deadline:number;ended:boolean;done:Promise<void>;release:()=>void;};
interface Options {storage:V1AuthStorage;journal:V1Journal;fetch?:FetchFn;now?:()=>number;refresh?:(token:string,signal:AbortSignal)=>Promise<unknown>;registerOriginStrip?:()=>Promise<void>;readProtection?:V1ReadProtection;}
export type V1AuthDisplay=Readonly<{protocol:'attention-auth-v1';mode:'v1-held';status:'committed'|'paused'|'disconnected'|'held';code:string;accountId:string|null;publicationId:string|null;label:string|null;drains:'disabled';}>;
function changed(s:V1AuthState,patch:Partial<V1AuthState>):V1AuthState{return {...s,...patch,revision:crypto.randomUUID()};}
function gateFor(p:V1Publication):V1Gate{return {version:1,accountId:p.accountId,generation:p.generation,enabled:true};}
function left(deadline:number,cap=30000):number {const remaining=deadline-performance.now();if(!Number.isFinite(deadline)||remaining<=0||remaining>cap)authHold('DEADLINE');return remaining;}
export class V1AuthBroker {
 private readonly storage:V1AuthStorage;private readonly journal:V1Journal;private readonly fetchFn:FetchFn;private readonly now:()=>number;private readonly refreshGrant:(token:string,signal:AbortSignal)=>Promise<unknown>;private readonly registerOriginStrip:()=>Promise<void>;
 private readonly readProtection:V1ReadProtection|undefined;
 private scopeClaim:ScopeClaim|null=null;private publisher=new AbortController();private scopeLifetimes=new Map<string,AbortSignal>();private refreshLifetimes=new Map<string,AbortSignal>();
 private checkPublisher(signal:AbortSignal):void{if(signal.aborted)authHold('PUBLICATION_REVOKED');}
 fencePublishers():void{this.beginScopeClaim();}
 // The replacement lifetime belongs exclusively to the superseding scope,
 // including the interval before its durable intent can be read or written.
 private beginScopeClaim():AbortSignal{const signal=this.revokeNow();this.scopeClaim={signal,transition:null};return signal;}
 private checkRefreshAdmission():void{if(this.scopeClaim)authHold('SCOPE_HELD');}
 private matchesScopeCommit(s:V1AuthState,t:V1ScopeTransition):boolean{
  if(s.pending||!exactAuth(s.settings,t.settings)||s.gate?.generation!==t.nextGeneration)return false;
  if(t.desired==='disconnected'||t.desired==='scope'&&!t.expectedPublication)return s.committed===null&&exactAuth(s.gate,t.nextGate);
  const p=s.committed;return !!p&&p.publicationId===t.publicationId&&p.credentialId===t.credentialId&&p.generation===t.nextGeneration&&p.scopeFingerprint===t.scopeFingerprint&&p.consentRevision===t.consentRevision
   &&(t.accountId===null||p.accountId===t.accountId)&&exactAuth(s.gate,{version:1,accountId:p.accountId,generation:t.nextGeneration,enabled:t.settings.enabled&&t.settings.pausedUntil===null});
 }
 private releaseScopeClaim(id:string,signal:AbortSignal,s:V1AuthState):void{
  const claim=this.scopeClaim;if(!claim)return;
  this.checkPublisher(signal);if(claim.signal!==signal||claim.transition?.transitionId!==id||!this.matchesScopeCommit(s,claim.transition))authHold('SCOPE_HELD');
  this.scopeClaim=null;
 }
 private initialized=false;private admitted=false;private heldCode='STARTUP_HELD';private leases=new Set<Lease>();private publishers=0;private renewals=new Map<string,Promise<V1AuthDisplay>>();private publicationTail:Promise<unknown>=Promise.resolve();
 private signin:{transitionId:string;device:DeviceCodeResponse;status:'pending'|'done'|'held'}|null=null;
 constructor(options:Options){this.readProtection=options.readProtection;this.storage=options.storage;this.journal=options.journal;this.fetchFn=options.fetch??((...args)=>fetch(...args));this.now=options.now??Date.now;this.refreshGrant=options.refresh??((token,signal)=>new FulcraOidc({fetch:this.providerFetch(signal)}).refresh(token));this.registerOriginStrip=options.registerOriginStrip??registerAuth0OriginStrip;}
 private providerFetch(signal:AbortSignal):FetchFn{return async(input,init={})=>{
  this.checkPublisher(signal);
  const response=await this.fetchFn(input,{...init,signal,redirect:'error',credentials:'omit',cache:'no-store'});
  if(response.redirected||response.url&&response.url!==String(input))authHold('PROVIDER_UNKNOWN');
  const reader=response.body?.getReader();if(!reader)authHold('PROVIDER_UNKNOWN');let bytes=0;const chunks:Uint8Array[]=[];
  try{for(;;){const {done,value}=await reader.read();if(done)break;bytes+=value.length;if(bytes>128*1024)authHold('PROVIDER_UNKNOWN');chunks.push(value);}}finally{void reader.cancel().catch(()=>{});reader.releaseLock();}
  const body=new Uint8Array(bytes);let offset=0;for(const chunk of chunks){body.set(chunk,offset);offset+=chunk.length;}
  const text=new TextDecoder('utf-8',{fatal:true}).decode(body);parseV1JSON(text);
  // A late COMPLETE successful grant must still reach its original private
  // receipt. Its caller's expired deadline prevents proof/publication, not
  // retention. No retry or active credential selection happens here.
  return new Response(text,{status:response.status,headers:response.headers});
 };}
 private boundedFetch(deadline:number,external?:AbortSignal,retain?:(input:RequestInfo|URL,response:Response)=>Promise<void>):FetchFn{return async(input,init={})=>{
  left(deadline,600000);const c=new AbortController(),cancel=()=>c.abort();external?.addEventListener('abort',cancel,{once:true});if(external?.aborted)c.abort();
  let timer:ReturnType<typeof setTimeout>|undefined;
  const timeout=new Promise<never>((_,reject)=>{timer=setTimeout(()=>{c.abort();reject(new V1AuthError('PROVIDER_UNKNOWN'));},Math.min(15000,deadline-performance.now()));});
  const operation=async()=>{if(c.signal.aborted)authHold('DEADLINE');const response=await this.providerFetch(c.signal)(input,init);if(retain)await retain(input,response.clone());if(c.signal.aborted)authHold('DEADLINE');return response;};
  try{return await Promise.race([operation(),timeout]);}finally{clearTimeout(timer);c.abort();external?.removeEventListener('abort',cancel);}
 };}
 private async publicationFailure(expected:string|null,error:unknown,signal:AbortSignal):Promise<V1AuthDisplay>{
  try{const s=await this.storage.readState();if(signal.aborted||s.committed?.publicationId!==expected)return this.display();}
  catch{/* Unknown state cannot authorize more work. */}
  if(signal.aborted)return this.display();this.revokeNow(error instanceof V1AuthError?error.code:'UNKNOWN');return this.display();
 }
 private revokeNow(code='SCOPE_HELD'):AbortSignal{this.publisher.abort();this.publisher=new AbortController();this.admitted=false;this.heldCode=code;for(const lease of this.leases)lease.controller.abort();return this.publisher.signal;}
 private async validateCommitted(s:V1AuthState):Promise<void>{
  if(s.pending)authHold('PENDING_PUBLICATION');
  if(!exactAuth(await this.journal.readGate(),s.gate))authHold('GATE_UNKNOWN');
  if(s.committed){const c=await this.storage.readCredential(s.committed.credentialId);if(c.predecessorPublicationId===s.committed.publicationId)authHold();
   if(c.origin==='refresh'){
    const a=await this.storage.readAttempt(c.ownerId);
    if(a.state!=='COMMITTED_SUCCESSOR'||!exactAuth(a.successor,s.committed)||c.predecessorPublicationId!==a.predecessor.publicationId||c.credentialId!==a.successorCredentialId)authHold('LINEAGE_UNKNOWN');
    const receipt=await this.storage.readCredential(a.receiptId);if(!exactAuth({...receipt,credentialId:a.successorCredentialId},c))authHold('LINEAGE_UNKNOWN');
   }
  }
 }
 async initializeSettings():Promise<void>{await this.storage.initializeAuthority(await this.journal.readGate());this.initialized=true;}
 async initialize():Promise<V1AuthDisplay>{
  const signal=this.publisher.signal,claim=this.scopeClaim;
  try{await this.initializeSettings();this.checkPublisher(signal);const s=await this.storage.readState();this.checkPublisher(signal);
   if(claim){
    if(this.scopeClaim!==claim||!claim.transition)authHold('SCOPE_HELD');const t=claim.transition;
    if(s.pending?.kind==='scope'&&s.pending.transitionId===t.transitionId){claim.signal=signal;this.scopeLifetimes.set(t.transitionId,signal);await this.finishScope(t.transitionId,performance.now()+30000);}
    else {if(!this.matchesScopeCommit(s,t))authHold('SCOPE_HELD');await this.validateCommitted(s);this.checkPublisher(signal);if(this.scopeClaim!==claim)authHold('SCOPE_HELD');claim.signal=signal;this.releaseScopeClaim(t.transitionId,signal,s);this.admitted=!!s.committed&&!!s.gate?.enabled;this.heldCode=s.committed?(s.gate?.enabled?'COMMITTED':'CONSENT_HELD'):'DISCONNECTED';}
   }else if(s.pending?.kind==='refresh'){this.refreshLifetimes.set(s.pending.attemptId,signal);await this.recoverRefresh(s.pending.attemptId,performance.now()+30000);}
   else if(s.pending?.kind==='scope'){this.scopeLifetimes.set(s.pending.transitionId,signal);await this.finishScope(s.pending.transitionId,performance.now()+30000);}
   else {await this.validateCommitted(s);this.checkPublisher(signal);this.admitted=!!s.committed&&!!s.gate?.enabled;this.heldCode=s.committed?(s.gate?.enabled?'COMMITTED':'CONSENT_HELD'):'DISCONNECTED';}
  }catch(e){if(!signal.aborted)this.heldCode=e instanceof V1AuthError?e.code:'UNKNOWN';}
  return this.display();
 }
 async display():Promise<V1AuthDisplay>{
  try{const s=await this.storage.readState(),p=s.committed;return Object.freeze({protocol:'attention-auth-v1',mode:'v1-held',status:p&&!s.pending&&!s.gate?.enabled&&this.heldCode==='CONSENT_HELD'?'paused':this.admitted&&p&&!s.pending?'committed':!p&&!s.pending&&this.heldCode==='DISCONNECTED'?'disconnected':'held',code:this.heldCode,accountId:p?.accountId??null,publicationId:p?.publicationId??null,label:null,drains:'disabled'});}
  catch{return Object.freeze({protocol:'attention-auth-v1',mode:'v1-held',status:'held',code:'STATE_UNKNOWN',accountId:null,publicationId:null,label:null,drains:'disabled'});}
 }
 private async claimScope(desired:V1ScopeTransition['desired'],scopeFingerprint=EMPTY_SCOPE,enabled=false,desiredSettings?:Settings,signal=this.beginScopeClaim()):Promise<V1ScopeTransition>{
  // Revocation occurs before storage/network awaits and does not wait for a
  // stuck grant job. Its late result can only fill that job's private receipt.
  this.checkPublisher(signal);if(this.scopeClaim?.signal!==signal)authHold('SCOPE_HELD');if(!this.initialized)authHold('STARTUP_HELD');
  const s=await this.storage.readState(),expectedGate=await this.journal.readGate();
  if(!exactAuth(s.gate,expectedGate))authHold('GATE_UNKNOWN');
  if(!s.initialized||!s.settings)authHold('SETTINGS_UNKNOWN');const settings=desiredSettings?strictV1Settings(desiredSettings):s.settings;if(scopeFingerprint===EMPTY_SCOPE)scopeFingerprint=await sha256Hex(JSON.stringify(settings));
  const generation=crypto.randomUUID(),account=desired==='scope'?s.committed?.accountId??null:null;
  const t:V1ScopeTransition={kind:'scope',transitionId:crypto.randomUUID(),expectedPublication:s.committed,expectedGate,nextGeneration:generation,scopeFingerprint,consentRevision:crypto.randomUUID(),desired,
   settings,nextGate:desired==='signin'?null:{version:1,accountId:account,generation,enabled:enabled&&account!==null},receiptId:crypto.randomUUID(),credentialId:crypto.randomUUID(),publicationId:crypto.randomUUID(),stage:'INTENT',accountId:account};
  let change:undefined|{before:V1RefreshAttempt;after:V1RefreshAttempt};
  if(s.pending?.kind==='refresh'){const a=await this.storage.readAttempt(s.pending.attemptId);change={before:a,after:{...a,state:'REVOKED'}};}
  this.checkPublisher(signal);if(this.scopeClaim?.signal!==signal)authHold('SCOPE_HELD');this.scopeClaim.transition=t;this.scopeLifetimes.set(t.transitionId,signal);await this.storage.compareAndSwap(s,changed(s,{pending:t}),change,undefined,signal);return t;
 }
 private async scopeFailure(id:string|undefined,error:unknown,signal:AbortSignal):Promise<V1AuthDisplay>{
  const code=error instanceof V1AuthError?error.code:'UNKNOWN';
  if(signal.aborted)return Object.freeze({...await this.display(),status:'held',code});
  try{const s=await this.storage.readState();if(!signal.aborted&&s.pending?.kind==='scope'&&s.pending.transitionId===id)this.revokeNow(code);}catch{if(!signal.aborted)this.revokeNow(code);}
  return Object.freeze({...await this.display(),status:'held',code});
 }
 async signOut():Promise<V1AuthDisplay>{const signal=this.beginScopeClaim();let id:string|undefined;try{const t=await this.claimScope('disconnected',EMPTY_SCOPE,false,undefined,signal);id=t.transitionId;await this.finishScope(id,performance.now()+30000);return this.display();}catch(e){return this.scopeFailure(id,e,signal);}}
 async readSettings():Promise<Settings>{const s=await this.storage.readState();if(!s.initialized||!s.settings)authHold('SETTINGS_HELD');return strictV1Settings(s.settings);}
 async changeSettings(value:unknown):Promise<V1AuthDisplay>{
  const signal=this.beginScopeClaim();let id:string|undefined;try{const settings=strictV1Settings(value),fingerprint=await sha256Hex(JSON.stringify(settings));
   const t=await this.claimScope('scope',fingerprint,settings.enabled&&settings.pausedUntil===null,settings,signal);id=t.transitionId;await this.finishScope(id,performance.now()+30000);return this.display();
  }catch(e){return this.scopeFailure(id,e,signal);}
 }
 private async scopeCurrent(id:string):Promise<{s:V1AuthState;t:V1ScopeTransition}>{const signal=this.scopeLifetimes.get(id);if(!signal)authHold('PUBLICATION_REVOKED');this.checkPublisher(signal);const s=await this.storage.readState();this.checkPublisher(signal);if(s.pending?.kind!=='scope'||s.pending.transitionId!==id)authHold('SCOPE_CHANGED');return {s,t:s.pending};}
 private async scopeStage(id:string,patch:Partial<V1ScopeTransition>):Promise<V1ScopeTransition>{const {s,t}=await this.scopeCurrent(id);const next={...t,...patch};const stages=['INTENT','CANDIDATE_RETAINED','PRINCIPAL_PROVED','SUCCESSOR_PREPARED','GATE_OBSERVED'];if(stages.indexOf(next.stage)<=stages.indexOf(t.stage)){for(const key of ['accountId','nextGate'] as const)if(patch[key]!==undefined&&!exactAuth(t[key],patch[key]))authHold('SCOPE_CHANGED');return t;}await this.storage.compareAndSwap(s,changed(s,{pending:next}),undefined,undefined,this.scopeLifetimes.get(id));return next;}
 // Only internal worker completion calls this seam. UI commands NEVER carry
 // token sets. Captured transitionId fences late completion after sign-out/B.
 private async completeSignIn(response:unknown,t:V1ScopeTransition,deadline=performance.now()+30000):Promise<V1AuthDisplay>{
  try{
   let credential:V1Credential;try{credential=await this.storage.readCredential(t.receiptId);if(!exactAuth(credential.response,response)||credential.ownerId!==t.transitionId||credential.predecessorPublicationId!==(t.expectedPublication?.publicationId??null))authHold('CREDENTIAL_LINEAGE');}catch(e){if(!(e instanceof V1AuthError)||e.code!=='CREDENTIAL_UNKNOWN')throw e;credential=makeV1Credential(t.receiptId,t.transitionId,t.expectedPublication?.publicationId??null,response,this.now());}
   await this.storage.retainCredential(credential); // before slow proof/deadline checks
   await this.scopeStage(t.transitionId,{stage:'CANDIDATE_RETAINED'});
   await this.finishScope(t.transitionId,Math.min(deadline,performance.now()+30000));
  }catch(e){try{await this.scopeCurrent(t.transitionId);this.checkPublisher(this.scopeLifetimes.get(t.transitionId)!);this.heldCode=e instanceof V1AuthError?e.code:'UNKNOWN';}catch{/* A later scope owns status. */}}
  return this.display();
 }
 private async prove(record:V1Credential,deadline:number,signal:AbortSignal):Promise<string>{
  left(deadline);const response=await this.boundedFetch(Math.min(deadline,performance.now()+15000),signal)(API_BASE+'/user/v1alpha1/info',{method:'GET',headers:{Authorization:'Bearer '+record.tokens.accessToken,Accept:'application/json'}});
  if(!response.ok||response.status!==200||!/^application\/json(?:;|$)/i.test(response.headers.get('content-type')??''))authHold('PRINCIPAL_UNKNOWN');
  const parsed=parseV1JSON(await response.text());
  if(!parsed||typeof parsed!=='object'||Array.isArray(parsed)||typeof (parsed as any).userid!=='string'||!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test((parsed as any).userid))authHold('PRINCIPAL_UNKNOWN');
  this.checkPublisher(signal);left(deadline);return (parsed as {userid:string}).userid;
 }
 private async finishScope(id:string,deadline:number):Promise<void>{
  let {t}=await this.scopeCurrent(id);let credential:V1Credential|undefined;
  if(t.desired==='signin'){
   // Predeclared receipt discovery handles a lost CANDIDATE_RETAINED ack.
   credential=await this.storage.readCredential(t.receiptId);
   if(credential.ownerId!==id||credential.predecessorPublicationId!==(t.expectedPublication?.publicationId??null))authHold('CREDENTIAL_LINEAGE');
   if(!exactAuth(credential,makeV1Credential(t.receiptId,id,t.expectedPublication?.publicationId??null,credential.response,credential.tokens.expiresAt-credential.response.expires_in*1000)))authHold('CREDENTIAL_LINEAGE');
   const account=await this.prove(credential,deadline,this.scopeLifetimes.get(id)!);await this.scopeCurrent(id);
   if(t.accountId!==null&&t.accountId!==account)authHold('PRINCIPAL_CHANGED');
   t=await this.scopeStage(id,{stage:'PRINCIPAL_PROVED',accountId:account,nextGate:{version:1,accountId:account,generation:t.nextGeneration,enabled:t.settings.enabled&&t.settings.pausedUntil===null}});
  }else if(t.desired==='scope'&&t.expectedPublication){
   credential=await this.storage.readCredential(t.expectedPublication.credentialId);
  }
  left(deadline);if(!t.nextGate)authHold('GATE_UNKNOWN');
  let publication:V1Publication|null=null;
  if(credential&&t.accountId){
   const successor={...credential,origin:t.desired==='signin'?'signin' as const:'scope' as const,credentialId:t.credentialId,ownerId:id,predecessorPublicationId:t.expectedPublication?.publicationId??null};
   await this.storage.retainCredential(successor);await this.scopeCurrent(id);
   publication={publicationId:t.publicationId,credentialId:t.credentialId,accountId:t.accountId,generation:t.nextGeneration,scopeFingerprint:t.scopeFingerprint,consentRevision:t.consentRevision};
  }
  t=await this.scopeStage(id,{stage:'SUCCESSOR_PREPARED'});left(deadline);
  const current=await this.journal.readGate();await this.scopeCurrent(id);
  if(!exactAuth(current,t.nextGate)){
   if(!exactAuth(current,t.expectedGate))authHold('GATE_UNKNOWN');
   const owned=this.journal.withLifetime(this.scopeLifetimes.get(id)!,deadline);try{await owned.transition(t.expectedGate,t.nextGate!);}finally{await owned.close();}
  }
  if(!exactAuth(await this.journal.readGate(),t.nextGate))authHold('GATE_UNKNOWN');
  await this.scopeStage(id,{stage:'GATE_OBSERVED'});left(deadline);
  const final=await this.scopeCurrent(id);
  await this.storage.compareAndSwap(final.s,changed(final.s,{committed:publication,gate:t.nextGate,settings:t.settings,pending:null}),undefined,deadline,this.scopeLifetimes.get(id));
  const committed=await this.storage.readState();await this.validateCommitted(committed);this.checkPublisher(this.scopeLifetimes.get(id)!);this.releaseScopeClaim(id,this.scopeLifetimes.get(id)!,committed);this.admitted=!!publication&&!!t.nextGate?.enabled;this.heldCode=publication?(t.nextGate?.enabled?'COMMITTED':'CONSENT_HELD'):'DISCONNECTED';
 }
 refresh(expectedPublicationId:string,deadline=performance.now()+30000):Promise<V1AuthDisplay>{
  if(this.scopeClaim)return this.display();
  if(this.renewals.has(expectedPublicationId))return this.renewals.get(expectedPublicationId)!;
  const signal=this.publisher.signal;this.publishers++; // synchronously blocks new leases, even while queued
  const run=this.publicationTail.then(()=>this.renew(expectedPublicationId,deadline,signal)).catch(e=>signal.aborted?this.display():this.publicationFailure(expectedPublicationId,e,signal)).finally(()=>{this.publishers--;this.renewals.delete(expectedPublicationId);});
  this.renewals.set(expectedPublicationId,run);this.publicationTail=run;return run;
 }
 private async renew(expected:string,deadline:number,signal:AbortSignal):Promise<V1AuthDisplay>{
  this.checkPublisher(signal);this.checkRefreshAdmission();left(deadline);if(!this.initialized)authHold('STARTUP_HELD');
  // Explicit admission-held, no budget restart or unbounded publisher wait.
  if(this.leases.size){let timer:ReturnType<typeof setTimeout>|undefined;try{await Promise.race([Promise.all([...this.leases].map(lease=>lease.done)),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new V1AuthError('ADMISSION_HELD')),left(deadline));})]);}finally{clearTimeout(timer);}left(deadline);}
  let s=await this.storage.readState();this.checkPublisher(signal);
  if(s.committed?.publicationId!==expected){
   const prior=(await this.storage.readAttempts()).find(a=>a.predecessor.publicationId===expected&&a.state==='COMMITTED_SUCCESSOR'&&a.successor?.publicationId===s.committed?.publicationId);
   if(prior&&!s.pending){await this.validateCommitted(s);return this.display();}authHold('PUBLICATION_CHANGED');
  }
  if(s.pending){if(s.pending.kind==='refresh'){this.refreshLifetimes.set(s.pending.attemptId,signal);await this.recoverRefresh(s.pending.attemptId,deadline);}else authHold('SCOPE_HELD');return this.display();}
  if(!this.admitted)authHold('ADMISSION_HELD');
  this.admitted=false;this.heldCode='RENEWAL_HELD';
  const predecessor=s.committed!,credential=await this.storage.readCredential(predecessor.credentialId);left(deadline);
  if(!credential.tokens.refreshToken)authHold('REFRESH_UNAVAILABLE');
  const fingerprint=await sha256Hex(credential.tokens.refreshToken);left(deadline);
  const attempts=await this.storage.readAttempts();
  if(attempts.some(a=>a.refreshFingerprint===fingerprint&&a.state!=='COMMITTED_SUCCESSOR'))authHold('REFRESH_EXCLUDED');
  const a:V1RefreshAttempt={version:1,attemptId:crypto.randomUUID(),predecessor,refreshFingerprint:fingerprint,config:{...config},receiptId:crypto.randomUUID(),successorPublicationId:crypto.randomUUID(),successorCredentialId:crypto.randomUUID(),state:'GRANT_CLAIMED',provedAccountId:null,successor:null};
  s=await this.storage.readState();if(!exactAuth(s.committed,predecessor)||s.pending)authHold('PUBLICATION_CHANGED');
  this.checkPublisher(signal);await this.storage.compareAndSwap(s,changed(s,{pending:{kind:'refresh',attemptId:a.attemptId}}),{before:null,after:a},deadline,signal);this.refreshLifetimes.set(a.attemptId,signal);
  left(deadline); // claim remains ambiguous if this expires BEFORE send
  const grantController=new AbortController(),cancel=()=>grantController.abort();signal.addEventListener('abort',cancel,{once:true});this.checkPublisher(signal);let timer:ReturnType<typeof setTimeout>|undefined;
  const operation=async()=>{
   this.checkPublisher(signal);const response=await this.refreshGrant(credential.tokens.refreshToken!,grantController.signal);
   const candidate=makeV1Credential(a.receiptId,a.attemptId,predecessor.publicationId,response,this.now(),credential.tokens);
   await this.storage.retainCredential(candidate); // complete successful receipt before any late checks
   await this.refreshStage(a.attemptId,{state:'CANDIDATE_RETAINED'});
   await this.recoverRefresh(a.attemptId,deadline);return this.display();
  };
  const timeout=new Promise<never>((_,reject)=>{timer=setTimeout(()=>{grantController.abort();reject(new V1AuthError('GRANT_UNKNOWN'));},left(deadline));});
  try{return await Promise.race([operation(),timeout]);}finally{clearTimeout(timer);grantController.abort();signal.removeEventListener('abort',cancel);}
 }

 private async refreshCurrent(id:string):Promise<{s:V1AuthState;a:V1RefreshAttempt}>{
  this.checkRefreshAdmission();const signal=this.refreshLifetimes.get(id);if(!signal)authHold('PUBLICATION_REVOKED');this.checkPublisher(signal);const s=await this.storage.readState(),a=await this.storage.readAttempt(id);this.checkPublisher(signal);
  if(s.pending?.kind!=='refresh'||s.pending.attemptId!==id||!exactAuth(s.committed,a.predecessor)||['REVOKED','QUARANTINED','COMMITTED_SUCCESSOR'].includes(a.state))authHold('PUBLICATION_CHANGED');
  return {s,a};
 }
 private async refreshStage(id:string,patch:Partial<V1RefreshAttempt>):Promise<V1RefreshAttempt>{
  const {s,a}=await this.refreshCurrent(id),next={...a,...patch};
  const stages=['GRANT_CLAIMED','CANDIDATE_RETAINED','PRINCIPAL_PROVED','SUCCESSOR_PREPARED'];
  if(stages.indexOf(next.state)<=stages.indexOf(a.state)){
   for(const key of ['provedAccountId','successor'] as const)if(patch[key]!==undefined&&!exactAuth(a[key],patch[key]))authHold('ATTEMPT_CHANGED');return a;
  }
  await this.storage.compareAndSwap(s,changed(s,{}),{before:a,after:next},undefined,this.refreshLifetimes.get(id));return next;
 }
 private async quarantineRefresh(id:string,code:string):Promise<never>{
  const {s,a}=await this.refreshCurrent(id);await this.storage.compareAndSwap(s,changed(s,{}),{before:a,after:{...a,state:'QUARANTINED'}},undefined,this.refreshLifetimes.get(id));authHold(code);
 }
 private async recoverRefresh(id:string,deadline:number):Promise<void>{
  let {a}=await this.refreshCurrent(id);if(!exactAuth(a.config,config))return this.quarantineRefresh(id,'CONFIG_CHANGED');
  const candidate=await this.storage.readCredential(a.receiptId);
  if(candidate.ownerId!==id||candidate.predecessorPublicationId!==a.predecessor.publicationId)return this.quarantineRefresh(id,'CREDENTIAL_LINEAGE');
  const original=await this.storage.readCredential(a.predecessor.credentialId);
  if(!original.tokens.refreshToken||await sha256Hex(original.tokens.refreshToken)!==a.refreshFingerprint)authHold('CREDENTIAL_LINEAGE');
  const reproduced=makeV1Credential(a.receiptId,id,a.predecessor.publicationId,candidate.response,candidate.tokens.expiresAt-candidate.response.expires_in*1000,original.tokens);
  if(!exactAuth(candidate,reproduced))authHold('CREDENTIAL_LINEAGE');
  a=await this.refreshStage(id,{state:'CANDIDATE_RETAINED'});
  left(deadline);const principal=await this.prove(candidate,deadline,this.refreshLifetimes.get(id)!);await this.refreshCurrent(id);
  if(principal!==a.predecessor.accountId)return this.quarantineRefresh(id,'PRINCIPAL_CHANGED');
  a=await this.refreshStage(id,{state:'PRINCIPAL_PROVED',provedAccountId:principal});
  const prepared={...candidate,credentialId:a.successorCredentialId};await this.storage.retainCredential(prepared);await this.refreshCurrent(id);
  const successor:V1Publication={...a.predecessor,publicationId:a.successorPublicationId,credentialId:a.successorCredentialId};
  a=await this.refreshStage(id,{state:'SUCCESSOR_PREPARED',successor});left(deadline);
  if(!exactAuth(await this.journal.readGate(),gateFor(a.predecessor)))authHold('GATE_UNKNOWN');
  const final=await this.refreshCurrent(id);
  await this.storage.compareAndSwap(final.s,changed(final.s,{committed:successor,pending:null}),{before:final.a,after:{...final.a,state:'COMMITTED_SUCCESSOR'}},deadline,this.refreshLifetimes.get(id));
  const committed=await this.storage.readState();await this.validateCommitted(committed);this.checkPublisher(this.refreshLifetimes.get(id)!);this.admitted=true;this.heldCode='COMMITTED';
 }
 private async lease(deadline:number):Promise<{lease:Lease;http:V1Http;journal:V1Journal;finish:()=>void}>{
  left(deadline);if(!this.initialized||!this.admitted||this.publishers||this.scopeClaim)authHold('ADMISSION_HELD');
  // Registration precedes awaits: a pending publisher cannot pass this lease.
  let release!:()=>void;const done=new Promise<void>(resolve=>{release=resolve;});
  const lease:Lease={controller:new AbortController(),deadline,ended:false,done,release};this.leases.add(lease);
  let ownedJournal:V1Journal|undefined;
  const finish=()=>{lease.ended=true;lease.controller.abort();clearTimeout(timer);if(ownedJournal)void ownedJournal.close();this.leases.delete(lease);lease.release();};
  const timer=setTimeout(()=>lease.controller.abort(),Math.ceil(left(deadline)));
  try{
   const s=await this.storage.readState();if(!this.admitted||s.pending||!s.committed||lease.controller.signal.aborted)authHold('ADMISSION_HELD');
   await this.validateCommitted(s);const publication=s.committed,c=await this.storage.readCredential(publication.credentialId);
   const check=()=>{left(deadline);if(lease.ended||lease.controller.signal.aborted)authHold('LEASE_ENDED');};
   const owned=this.journal.withLifetime(lease.controller.signal,deadline);ownedJournal=owned;
   const http=new V1Http({journal:owned,scope:gateFor(publication),readToken:async()=>{check();return c.tokens.accessToken;},fetch:this.fetchFn,now:this.now,timeoutMs:Math.min(30000,left(deadline)),readProtection:this.readProtection,lifetime:{signal:lease.controller.signal,deadline}});
   check();return {lease,http,journal:owned,finish};
  }catch(e){finish();throw e;}
 }
 async runAction(intent:V1MutationIntent,deadline:number,reconcile=false){
  const owned=await this.lease(deadline);
  try{const options={http:owned.http,journal:owned.journal,intent:snapshotV1JSON(intent,2*1024*1024) as unknown as V1MutationIntent,timeoutMs:left(deadline),signal:owned.lease.controller.signal};return reconcile?await reconcileV1Action(options):await settleV1Action(options);}finally{owned.finish();}
 }
 async runSetup(request:{kind:'attention'|'control'|'tag';pinnedId?:string;allowCreate?:boolean;label?:string},deadline:number):Promise<unknown>{
  const captured=snapshotV1JSON(request,8192) as typeof request,owned=await this.lease(deadline);
  try{if(captured.kind==='tag'){if(typeof captured.label!=='string')authHold('SETUP_INVALID');return await resolveV1Tag({http:owned.http,journal:owned.journal,deadline,signal:owned.lease.controller.signal},captured.label);}
   const options={http:owned.http,journal:owned.journal,pinnedId:captured.pinnedId,allowCreate:captured.allowCreate,timeoutMs:Math.min(10000,left(deadline)),signal:owned.lease.controller.signal};
   if(captured.kind==='attention')return await resolveAttentionV1(options);if(captured.kind==='control')return await resolveExclusionV1(options);authHold('SETUP_INVALID');
  }finally{owned.finish();}
 }
 async runRead(kind:'types',deadline:number):Promise<unknown>{const owned=await this.lease(deadline);try{if(kind!=='types')authHold('READ_INVALID');return await readV1OwnedTypes(owned.http,Math.min(8000,left(deadline)));}finally{owned.finish();}}
 async startSignIn():Promise<{transitionId:string;verificationUriComplete:string;userCode:string}>{
  const t=await this.claimScope('signin');
  try{await this.registerOriginStrip();await this.scopeCurrent(t.transitionId);const deadline=performance.now()+600000,oidc=new FulcraOidc({fetch:this.boundedFetch(deadline,this.scopeLifetimes.get(t.transitionId),async(input,response)=>{if(String(input).endsWith('/oauth/token')&&response.status===200){const credential=makeV1Credential(t.receiptId,t.transitionId,t.expectedPublication?.publicationId??null,parseV1JSON(await response.text()),this.now());await this.storage.retainCredential(credential);}})}),device=await oidc.requestDeviceCode();await this.scopeCurrent(t.transitionId);
   this.signin={transitionId:t.transitionId,device,status:'pending'};
   // Polling survives a page closing, but never gains publication authority
   // from a late result. No secrets cross the worker message boundary.
   void oidc.pollForToken(device.device_code,device.interval,{sleep:async ms=>{await new Promise(r=>setTimeout(r,ms));await this.scopeCurrent(t.transitionId);},maxAttempts:120}).then(async response=>{await this.completeSignIn(response,t,deadline);if(this.signin?.transitionId===t.transitionId)this.signin.status=['committed','paused'].includes((await this.display()).status)?'done':'held';}).catch(()=>{if(this.signin?.transitionId===t.transitionId)this.signin.status='held';});
   return {transitionId:t.transitionId,verificationUriComplete:device.verification_uri_complete,userCode:device.user_code};
  }catch{authHold('SIGNIN_HELD');}
 }
 async signInStatus(id:string):Promise<{status:'pending'|'done'|'held';display:V1AuthDisplay}>{return {status:this.signin?.transitionId===id?this.signin.status:'held',display:await this.display()};}
}
