// Worker-owned publication metadata. Secrets live ONLY in separately addressed
// Chrome local credential records, never in this IDB/journal/UI protocol.
import type {V1Gate} from './v1Journal';
import {runV1Read,openExistingDatabase,readTransactionBounded,type V1ReadJob,type V1ReadLimits,type ReadResult} from './v1ReadBudget';
import {snapshotV1JSON} from './v1Http';
import {defaultLocalStorageArea,type StorageArea} from './storageArea';
import type {TokenSet} from './oidc';
import {DEFAULT_SETTINGS,type Settings} from '../types';
import type {StoredTokens} from './tokenStore';
export class V1AuthError extends Error {constructor(readonly code:string){super('Attention v1 auth: '+code);this.name='V1AuthError';}}
export function authHold(code='UNKNOWN'):never {throw new V1AuthError(code);}
export const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
export const initializationKey='relaylessV1Initialization';
interface InitializationWitness {version:1;initializationId:string;stage:'CLAIMED'|'CREATING'|'COMMITTED';initialSettings:Settings;}
function witness(v:unknown):v is InitializationWitness{return keys(v,['version','initializationId','stage','initialSettings'])&&v.version===1&&id(v.initializationId)&&['CLAIMED','CREATING','COMMITTED'].includes(v.stage as string)&&validSettings(v.initialSettings);}
const HASH=/^[a-f0-9]{64}$/;
export const credentialKey=(id:string)=>{if(!UUID.test(id))authHold('CREDENTIAL_ID');return 'relaylessV1Credential:'+id;};
export function exactAuth(a:unknown,b:unknown):boolean {return stable(a)===stable(b);}
function stable(v:unknown):string {if(Array.isArray(v))return '['+v.map(stable).join(',')+']';if(v&&typeof v==='object')return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+stable((v as Record<string,unknown>)[k])).join(',')+'}';return JSON.stringify(v);}
function obj(v:unknown):v is Record<string,unknown>{return !!v&&typeof v==='object'&&!Array.isArray(v);}
function keys(v:unknown,names:string[]):v is Record<string,unknown>{return obj(v)&&Object.keys(v).length===names.length&&names.every(k=>Object.hasOwn(v,k));}
function text(v:unknown,max=32768):v is string{return typeof v==='string'&&v.trim().length>0&&v.length<=max&&!/[\x00-\x1f\x7f]/.test(v)&&!/[\ud800-\udfff]/u.test(v);}
function id(v:unknown):v is string{return typeof v==='string'&&UUID.test(v);}
function hash(v:unknown):v is string{return typeof v==='string'&&HASH.test(v);}
function gate(v:unknown):v is V1Gate{return keys(v,['version','accountId','generation','enabled'])&&v.version===1&&id(v.generation)&&(v.accountId===null||id(v.accountId))&&typeof v.enabled==='boolean'&&(!v.enabled||v.accountId!==null);}
export interface V1Publication {publicationId:string;credentialId:string;accountId:string;generation:string;scopeFingerprint:string;consentRevision:string;}
function publication(v:unknown):v is V1Publication{return keys(v,['publicationId','credentialId','accountId','generation','scopeFingerprint','consentRevision'])&&id(v.publicationId)&&id(v.credentialId)&&id(v.accountId)&&id(v.generation)&&hash(v.scopeFingerprint)&&id(v.consentRevision);}
export type RefreshState='GRANT_CLAIMED'|'CANDIDATE_RETAINED'|'PRINCIPAL_PROVED'|'SUCCESSOR_PREPARED'|'COMMITTED_SUCCESSOR'|'REVOKED'|'QUARANTINED';
export interface V1RefreshAttempt {
 version:1;attemptId:string;predecessor:V1Publication;refreshFingerprint:string;
 config:{clientId:string;issuer:string;audience:string;scope:string};
 receiptId:string;successorPublicationId:string;successorCredentialId:string;
 state:RefreshState;provedAccountId:string|null;successor:V1Publication|null;
}
export interface V1ScopeTransition {
 kind:'scope';transitionId:string;expectedPublication:V1Publication|null;expectedGate:V1Gate|null;
 nextGeneration:string;scopeFingerprint:string;consentRevision:string;desired:'disconnected'|'signin'|'scope';
 settings:Settings;nextGate:V1Gate|null;receiptId:string;credentialId:string;publicationId:string;
 stage:'INTENT'|'CANDIDATE_RETAINED'|'PRINCIPAL_PROVED'|'SUCCESSOR_PREPARED'|'GATE_OBSERVED';accountId:string|null;
}
export interface V1AuthState {version:1;initializationId:string;initialized:boolean;settings:Settings|null;revision:string;committed:V1Publication|null;gate:V1Gate|null;pending:V1ScopeTransition|{kind:'refresh';attemptId:string}|null;}
function attempt(v:unknown):v is V1RefreshAttempt {
 return keys(v,['version','attemptId','predecessor','refreshFingerprint','config','receiptId','successorPublicationId','successorCredentialId','state','provedAccountId','successor'])&&v.version===1&&id(v.attemptId)&&publication(v.predecessor)&&hash(v.refreshFingerprint)
 &&keys(v.config,['clientId','issuer','audience','scope'])&&Object.values(v.config).every(x=>text(x,2048))&&id(v.receiptId)&&id(v.successorPublicationId)&&id(v.successorCredentialId)
 &&['GRANT_CLAIMED','CANDIDATE_RETAINED','PRINCIPAL_PROVED','SUCCESSOR_PREPARED','COMMITTED_SUCCESSOR','REVOKED','QUARANTINED'].includes(v.state as string)
 &&(v.provedAccountId===null||v.provedAccountId===v.predecessor.accountId)&&(v.successor===null||publication(v.successor)&&v.successor.publicationId===v.successorPublicationId&&v.successor.credentialId===v.successorCredentialId&&v.successor.accountId===v.predecessor.accountId&&v.successor.generation===v.predecessor.generation&&v.successor.scopeFingerprint===v.predecessor.scopeFingerprint&&v.successor.consentRevision===v.predecessor.consentRevision)
 &&(!['PRINCIPAL_PROVED','SUCCESSOR_PREPARED','COMMITTED_SUCCESSOR'].includes(v.state as string)||v.provedAccountId===v.predecessor.accountId)
 &&(!['SUCCESSOR_PREPARED','COMMITTED_SUCCESSOR'].includes(v.state as string)||v.successor!==null);
}
function transition(v:unknown):v is V1ScopeTransition {
 return keys(v,['kind','transitionId','expectedPublication','expectedGate','nextGeneration','scopeFingerprint','consentRevision','desired','settings','nextGate','receiptId','credentialId','publicationId','stage','accountId'])&&v.kind==='scope'&&id(v.transitionId)&&(v.expectedPublication===null||publication(v.expectedPublication))&&(v.expectedGate===null||gate(v.expectedGate))&&id(v.nextGeneration)&&hash(v.scopeFingerprint)&&id(v.consentRevision)
 &&validSettings(v.settings)&&['disconnected','signin','scope'].includes(v.desired as string)&&(v.nextGate===null||gate(v.nextGate)&&v.nextGate.generation===v.nextGeneration)&&id(v.receiptId)&&id(v.credentialId)&&id(v.publicationId)
 &&['INTENT','CANDIDATE_RETAINED','PRINCIPAL_PROVED','SUCCESSOR_PREPARED','GATE_OBSERVED'].includes(v.stage as string)&&(v.accountId===null||id(v.accountId));
}
export function strictV1Settings(value:unknown):Settings {const v=snapshotV1JSON(value,8192);if(!validSettings(v))authHold('SETTINGS_UNKNOWN');return v;}
function validSettings(v:unknown):v is Settings {
 return keys(v,['enabled','identityLabel','onboarded','pausedUntil','heartbeatEnabled'])&&typeof v.enabled==='boolean'&&typeof v.onboarded==='boolean'&&typeof v.heartbeatEnabled==='boolean'&&(v.identityLabel===null||typeof v.identityLabel==='string'&&v.identityLabel.length<=1024&&!/[\x00-\x1f\x7f]/.test(v.identityLabel))&&(v.pausedUntil===null||typeof v.pausedUntil==='number'&&Number.isFinite(v.pausedUntil));
}
function state(v:unknown):v is V1AuthState {
 if(!keys(v,['version','initializationId','initialized','settings','revision','committed','gate','pending'])||v.version!==1||!id(v.initializationId)||typeof v.initialized!=='boolean'||!id(v.revision))return false;
 if(!v.initialized)return v.settings===null&&v.committed===null&&v.gate===null&&v.pending===null;
 if(!validSettings(v.settings)||(v.committed!==null&&!publication(v.committed))||(v.gate!==null&&!gate(v.gate)))return false;
 if(v.pending!==null&&!transition(v.pending)&&!(keys(v.pending,['kind','attemptId'])&&v.pending.kind==='refresh'&&id(v.pending.attemptId)))return false;
 if(v.pending&&v.pending.kind==='scope'&&(!exactAuth(v.pending.expectedPublication,v.committed)||!exactAuth(v.pending.expectedGate,v.gate)))return false;
 return v.committed===null?v.gate===null||!(v.gate as V1Gate).enabled:v.gate!==null&&(v.committed as V1Publication).accountId===(v.gate as V1Gate).accountId&&(v.committed as V1Publication).generation===(v.gate as V1Gate).generation&&(v.gate as V1Gate).enabled===((v.settings as Settings).enabled&&(v.settings as Settings).pausedUntil===null);
}
export interface V1Credential {version:1;origin:'signin'|'scope'|'refresh';credentialId:string;ownerId:string;predecessorPublicationId:string|null;response:TokenSet;tokens:StoredTokens;}
export function strictTokenResponse(value:unknown):TokenSet {
 const v=snapshotV1JSON(value,128*1024);
 if(!obj(v)||Object.keys(v).some(k=>!['access_token','refresh_token','id_token','expires_in','token_type','scope'].includes(k))||!text(v.access_token)||typeof v.expires_in!=='number'||!Number.isFinite(v.expires_in)||v.expires_in<=0||v.expires_in>31536000
 ||['refresh_token','id_token','token_type','scope'].some(k=>Object.hasOwn(v,k)&&!text(v[k])))authHold('INVALID_TOKEN_RESPONSE');
 return v as unknown as TokenSet;
}
export function makeV1Credential(credentialId:string,ownerId:string,predecessorPublicationId:string|null,response:unknown,now:number,previous?:StoredTokens):V1Credential {
 const token=strictTokenResponse(response);
 if(!Number.isFinite(now))authHold();
 return validateCredential({version:1,origin:previous?'refresh':'signin',credentialId,ownerId,predecessorPublicationId,response:token,tokens:{accessToken:token.access_token,refreshToken:Object.hasOwn(token,'refresh_token')?token.refresh_token!:previous?.refreshToken??null,idToken:Object.hasOwn(token,'id_token')?token.id_token!:previous?.idToken??null,expiresAt:now+token.expires_in*1000}});
}
function validateCredential(v:unknown):V1Credential {
 if(!keys(v,['version','origin','credentialId','ownerId','predecessorPublicationId','response','tokens'])||v.version!==1||!['signin','scope','refresh'].includes(v.origin as string)||!id(v.credentialId)||!id(v.ownerId)||(v.predecessorPublicationId!==null&&!id(v.predecessorPublicationId))||!keys(v.tokens,['accessToken','refreshToken','idToken','expiresAt']))authHold('CREDENTIAL_UNKNOWN');
 const response=strictTokenResponse(v.response),t=v.tokens;
 if(!text(t.accessToken)||t.accessToken!==response.access_token||(t.refreshToken!==null&&!text(t.refreshToken))||(t.idToken!==null&&!text(t.idToken))||typeof t.expiresAt!=='number'||!Number.isFinite(t.expiresAt)
 ||v.origin==='signin'&&(!Object.hasOwn(response,'refresh_token')&&t.refreshToken!==null||!Object.hasOwn(response,'id_token')&&t.idToken!==null)
 ||Object.hasOwn(response,'refresh_token')&&t.refreshToken!==response.refresh_token||Object.hasOwn(response,'id_token')&&t.idToken!==response.id_token)authHold('CREDENTIAL_UNKNOWN');
 return snapshotV1JSON(v,128*1024) as unknown as V1Credential;
}
/** Detached version-1 raw envelope only; C1 schema/linked authority is unvalidated. */
export type V1RawUnvalidatedCaptureRecord=Readonly<Record<string,unknown>&{version:1}>;
function captureRecordKey(key:string):boolean {
 return ['capture-origin','capture-source','capture-selection','capture-accounting'].includes(key)
  ||['capture-setup:','capture-receipt:','capture-dispatch:','capture-transition:','capture-group:'].some(prefix=>key.startsWith(prefix)&&UUID.test(key.slice(prefix.length)));
}
export class V1AuthStorage {
 private opening:Promise<IDBDatabase>|null=null;private closed=false;private createWitness:InitializationWitness|null=null;
 private readonly factory:IDBFactory;private readonly credentials:StorageArea;private readonly readOnly:boolean;
 constructor(options:{indexedDB?:IDBFactory;credentials?:StorageArea;readOnly?:boolean}={}){this.readOnly=options.readOnly??false;this.factory=options.indexedDB??globalThis.indexedDB;this.credentials=options.credentials??defaultLocalStorageArea();}
 private open():Promise<IDBDatabase>{
  if(this.closed)return Promise.reject(new V1AuthError('STORAGE_CLOSED'));
  if(this.opening)return this.opening;
  this.opening=new Promise((resolve,reject)=>{
   const request=this.factory.open('fulcra-attention-auth-v1',1);let ended=false;
   let missing=false;const fail=()=>{if(!ended){ended=true;clearTimeout(timer);try{request.transaction?.abort();}catch{}reject(new V1AuthError(missing?'AUTHORITY_MISSING':'STORAGE_UNKNOWN'));}};
   const timer=setTimeout(fail,5000);request.onerror=fail;request.onblocked=fail;
   request.onupgradeneeded=e=>{if(ended||this.readOnly||e.oldVersion!==0||!this.createWitness){missing=e.oldVersion===0;request.transaction?.abort();return;}const initial=this.createWitness;this.createWitness=null;const db=request.result;db.createObjectStore('state').put({version:1,initializationId:initial.initializationId,initialized:false,settings:null,revision:crypto.randomUUID(),committed:null,gate:null,pending:null},'authority');db.createObjectStore('attempts');};
   request.onsuccess=()=>{const db=request.result;if(ended||this.closed||[...db.objectStoreNames].sort().join(',')!=='attempts,state'){db.close();fail();return;}try{const tx=db.transaction(['state','attempts'],'readonly');for(const name of ['state','attempts']){const store=tx.objectStore(name);if(store.keyPath!==null||store.autoIncrement||store.indexNames.length)throw Error();}}catch{db.close();fail();return;}ended=true;clearTimeout(timer);db.onversionchange=()=>{this.closed=true;db.close();};resolve(db);};
  });return this.opening;
 }
 private async transaction<T>(write:boolean,run:(tx:IDBTransaction,read:<V>(r:IDBRequest<V>,next:(v:V)=>void)=>void,finish:(v:T)=>void)=>void,deadline?:number,signal?:AbortSignal):Promise<T>{
  const check=()=>{if(signal?.aborted)authHold('PUBLICATION_REVOKED');if(deadline!==undefined&&(!Number.isFinite(deadline)||performance.now()>=deadline))authHold('DEADLINE');};check();
  if(write&&this.readOnly)authHold('READ_ONLY');
  const db=await this.open();check();return new Promise((resolve,reject)=>{
   const tx=write?db.transaction(['state','attempts'],'readwrite',{durability:'strict'}):db.transaction(['state','attempts'],'readonly');
   let result:T,finished=false,failure:unknown;
   const fail=(e:unknown)=>{failure=e;try{tx.abort();}catch{/* A completed transaction owns its confirmed result; await its completion callback. */}};
   if(write&&tx.durability!=='strict'){fail(null);reject(new V1AuthError('DURABILITY_UNKNOWN'));return;}
   const cancel=()=>fail(new V1AuthError('PUBLICATION_REVOKED'));signal?.addEventListener('abort',cancel,{once:true});
   const timer=setTimeout(()=>fail(new V1AuthError('STORAGE_UNKNOWN')),5000);
   const safe=(fn:()=>void)=>{try{check();fn();}catch(e){fail(e);}};
   const read=<V>(r:IDBRequest<V>,next:(v:V)=>void)=>{r.onsuccess=()=>safe(()=>next(r.result));};
   tx.onabort=()=>{signal?.removeEventListener('abort',cancel);clearTimeout(timer);reject(failure instanceof V1AuthError?failure:new V1AuthError('STORAGE_UNKNOWN'));};tx.onerror=()=>{};
   tx.oncomplete=()=>{signal?.removeEventListener('abort',cancel);clearTimeout(timer);finished?resolve(result):reject(new V1AuthError('STORAGE_UNKNOWN'));};
   safe(()=>run(tx,read,v=>{result=v;finished=true;}));
  });
 }
 private evidenceRead<T>(limits:V1ReadLimits,read:(job:V1ReadJob)=>Promise<T>):Promise<ReadResult<T>>{
  return runV1Read(limits,read,{check:()=>{if(this.closed)authHold('STORAGE_CLOSED');}});
 }
 // Separate noncreating evidence handles. No production caller uses these seams.
 private openExistingBounded(job:V1ReadJob):Promise<IDBDatabase>{
  return openExistingDatabase(job,this.factory,'fulcra-attention-auth-v1',1,['state','attempts'],(_db,tx)=>{
   for(const name of ['state','attempts']){const store=tx.objectStore(name);if(store.keyPath!==null||store.autoIncrement||store.indexNames.length)job.refuse('AUTH_SHAPE');}
  });
 }
 private async readTransactionBounded<T>(job:V1ReadJob,store:string,key:string,max:number):Promise<Readonly<T>>{
  const db=await job.wait(this.openExistingBounded(job));
  return job.wait(readTransactionBounded<Readonly<T>>(job,db,[store],(tx,read,finish)=>read(tx.objectStore(store).get(key),value=>{
   if(value===undefined)job.refuse('RECORD_MISSING');finish(job.copy(value,max) as Readonly<T>);
  })));
 }
 readStateBounded(limits:V1ReadLimits):Promise<ReadResult<V1AuthState>>{
  return this.evidenceRead(limits,async job=>{
   const value=await job.wait(this.readTransactionBounded<V1AuthState>(job,'state','authority',32768));
   if(!state(value))job.refuse('STATE_UNKNOWN');job.check();
   const local=await job.wait(this.credentials.get(initializationKey));job.check();
   const w=job.copy(local[initializationKey],16384);
   if(!witness(w)||w.stage!=='COMMITTED'||w.initializationId!==value.initializationId||!value.initialized)job.refuse('INITIALIZATION_UNKNOWN');job.check();
   return value;
  });
 }
 readAttemptBounded(attemptId:string,limits:V1ReadLimits):Promise<ReadResult<V1RefreshAttempt>>{
  return this.evidenceRead(limits,async job=>{
   if(!id(attemptId))job.refuse('ATTEMPT_ID');
   const value=await job.wait(this.readTransactionBounded<V1RefreshAttempt>(job,'attempts',attemptId,65536));
   if(!attempt(value)||value.attemptId!==attemptId)job.refuse('ATTEMPT_UNKNOWN');job.check();return value;
  });
 }
 /** Broker-private credential evidence: never exposed through a page adapter. */
 readCredentialBounded(credentialId:string,limits:V1ReadLimits):Promise<ReadResult<V1Credential>>{
  return this.evidenceRead(limits,async job=>{
   if(this.readOnly)job.refuse('READ_ONLY');if(!id(credentialId))job.refuse('CREDENTIAL_ID');const key=credentialKey(credentialId);job.check();
   const local=await job.wait(this.credentials.get(key));job.check();
   const value=job.copy(local[key],128*1024),validated=validateCredential(value);job.check();
   if(validated.credentialId!==credentialId)job.refuse('CREDENTIAL_UNKNOWN');return validated;
  });
 }
 readCaptureRecordBounded(key:string,limits:V1ReadLimits):Promise<ReadResult<V1RawUnvalidatedCaptureRecord>>{
  return this.evidenceRead(limits,async job=>{
   if(typeof key!=='string'||!captureRecordKey(key))job.refuse('CAPTURE_KEY');
   const value=await job.wait(this.readTransactionBounded<V1RawUnvalidatedCaptureRecord>(job,'state',key,1024*1024));
   if(!obj(value)||value.version!==1)job.refuse('CAPTURE_RAW_UNKNOWN');job.check();return value;
  });
 }
 private async rawState():Promise<V1AuthState>{return this.transaction(false,(tx,read,finish)=>read(tx.objectStore('state').get('authority'),v=>{if(!state(v))authHold('STATE_UNKNOWN');finish(v);}));}
 async readState():Promise<V1AuthState>{const s=await this.rawState(),w=(await this.credentials.get(initializationKey))[initializationKey];if(!witness(w)||w.stage!=='COMMITTED'||w.initializationId!==s.initializationId||!s.initialized)authHold('INITIALIZATION_UNKNOWN');return s;}
 private async writeWitness(before:InitializationWitness|undefined,after:InitializationWitness):Promise<void>{
  if(!exactAuth((await this.credentials.get(initializationKey))[initializationKey],before))authHold('INITIALIZATION_CONFLICT');
  try{await this.credentials.set({[initializationKey]:after});}catch{authHold('INITIALIZATION_WRITE_UNKNOWN');}
  if(!exactAuth((await this.credentials.get(initializationKey))[initializationKey],after))authHold('INITIALIZATION_WRITE_UNKNOWN');
 }
 /** Called only by the serialized actual worker owner. Default/read-only opens cannot create. */
 async initializeAuthority(existingGate:V1Gate|null):Promise<void>{
  if(this.readOnly)authHold('READ_ONLY');
  let w=(await this.credentials.get(initializationKey))[initializationKey] as InitializationWitness|undefined;
  if(w!==undefined&&!witness(w))authHold('INITIALIZATION_UNKNOWN');
  if(!w){
   try{await this.rawState();authHold('INITIALIZATION_UNKNOWN');}catch(e){if(!(e instanceof V1AuthError)||e.code!=='AUTHORITY_MISSING')throw e;}
   const local=await this.credentials.get(null);if(existingGate!==null||Object.keys(local).some(k=>k.startsWith('relaylessV1Credential:')))authHold('INITIALIZATION_UNKNOWN');
   const initialSettings=await this.firstSettingsSnapshot();w={version:1,initializationId:crypto.randomUUID(),stage:'CLAIMED',initialSettings};await this.writeWitness(undefined,w);
  }
  if(w.stage==='CLAIMED'){
   try{await this.rawState();authHold('INITIALIZATION_CONFLICT');}catch(e){if(!(e instanceof V1AuthError)||e.code!=='AUTHORITY_MISSING')throw e;}
   const creating={...w,stage:'CREATING' as const};await this.writeWitness(w,creating);w=creating;
   this.createWitness=w;this.opening=null;
  }
  const s=await this.rawState();if(s.initializationId!==w.initializationId)authHold('INITIALIZATION_CONFLICT');
  if(w.stage==='COMMITTED'){if(!s.initialized)authHold('INITIALIZATION_UNKNOWN');return;}
  if(!s.initialized)await this.compareAndSwap(s,{...s,initialized:true,settings:w.initialSettings,revision:crypto.randomUUID()},undefined,undefined,undefined,true);
  const verified=await this.rawState();if(!verified.initialized||!exactAuth(verified.settings,w.initialSettings)||verified.initializationId!==w.initializationId)authHold('INITIALIZATION_CONFLICT');
  await this.writeWitness(w,{...w,stage:'COMMITTED'});
 }
 async readAttempts():Promise<V1RefreshAttempt[]>{return this.transaction(false,(tx,read,finish)=>{const rows:V1RefreshAttempt[]=[];read(tx.objectStore('attempts').openCursor(),function next(cursor){if(!cursor){finish(rows);return;}if(rows.length>=10000||!attempt(cursor.value)||cursor.key!==cursor.value.attemptId)authHold('ATTEMPTS_UNKNOWN');rows.push(cursor.value);cursor.continue();});});}
 async readAttempt(attemptId:string):Promise<V1RefreshAttempt>{if(!id(attemptId))authHold();return this.transaction(false,(tx,read,finish)=>read(tx.objectStore('attempts').get(attemptId),v=>{if(!attempt(v)||v.attemptId!==attemptId)authHold('ATTEMPT_UNKNOWN');finish(v);}));}
 // CAS of the ENTIRE captured state and exact attempt; no network/crypto await
 // can occur between predicates and writes in the strict final transaction.
 async compareAndSwap(expected:V1AuthState,next:V1AuthState,change?:{before:V1RefreshAttempt|null;after:V1RefreshAttempt},deadline?:number,signal?:AbortSignal,initializing=false):Promise<void>{
  const before=snapshotV1JSON(expected,32768),after=snapshotV1JSON(next,32768);
  change=change?snapshotV1JSON(change,65536) as typeof change:undefined;
  if(!state(before)||!state(after)||before.initializationId!==after.initializationId||before.initialized&&!after.initialized||exactAuth(before,after)||before.revision===after.revision||change&&(!attempt(change.after)||change.before!==null&&!attempt(change.before)||change.before!==null&&change.before.attemptId!==change.after.attemptId))authHold('CAS_INVALID');
  await this.transaction<void>(true,(tx,read,finish)=>{
   read(tx.objectStore('state').get('authority'),v=>{if(!state(v)||!exactAuth(v,before))authHold('PUBLICATION_CHANGED');
    const put=()=>{if(signal?.aborted)authHold('PUBLICATION_REVOKED');if(deadline!==undefined&&performance.now()>=deadline)authHold('DEADLINE');if(change)tx.objectStore('attempts').put(change.after,change.after.attemptId);tx.objectStore('state').put(after,'authority');finish();};
    if(!change){put();return;}
    read(tx.objectStore('attempts').get(change.after.attemptId),v=>{if(change.before===null?v!==undefined:!attempt(v)||!exactAuth(v,change.before))authHold('ATTEMPT_CHANGED');
     if(change.before===null)read(tx.objectStore('attempts').count(),count=>{if(count>=10000)authHold('STORAGE_FULL');put();});else put();});
   });
  },deadline,signal);
  if(!exactAuth(await (initializing?this.rawState():this.readState()),after))authHold('COMMIT_UNKNOWN');
  if(change&&!exactAuth(await this.readAttempt(change.after.attemptId),change.after))authHold('COMMIT_UNKNOWN');
 }
 async retainCredential(record:V1Credential):Promise<void>{
  if(this.readOnly)authHold('READ_ONLY');
  const next=validateCredential(record),key=credentialKey(next.credentialId),old=(await this.credentials.get(key))[key];
  if(old!==undefined){if(!exactAuth(validateCredential(old),next))authHold('CREDENTIAL_CONFLICT');return;}
  try{await this.credentials.set({[key]:next});}catch{authHold('CREDENTIAL_WRITE_UNKNOWN');}
  if(!exactAuth(await this.readCredential(next.credentialId),next))authHold('CREDENTIAL_WRITE_UNKNOWN');
 }
 async readCredential(credentialId:string):Promise<V1Credential>{if(this.readOnly)authHold('READ_ONLY');const key=credentialKey(credentialId);try{const record=validateCredential((await this.credentials.get(key))[key]);if(record.credentialId!==credentialId)authHold();return record;}catch{authHold('CREDENTIAL_UNKNOWN');}}
 async firstSettingsSnapshot():Promise<Settings>{if(this.readOnly)authHold('READ_ONLY');try{const value=(await this.credentials.get('settings')).settings;return strictV1Settings(value===undefined?DEFAULT_SETTINGS:value);}catch{authHold('SETTINGS_UNKNOWN');}}
 async close():Promise<void>{this.closed=true;(await this.opening)?.close();}
}

/** Page projection: never creates/bootstraps authority or falls back to legacy. */
export async function readV1Settings():Promise<Settings>{
 const reader=new V1AuthStorage({readOnly:true});try{const s=await reader.readState();if(!s.initialized||!s.settings)authHold('SETTINGS_HELD');return strictV1Settings(s.settings);}finally{await reader.close().catch(()=>{});}
}
