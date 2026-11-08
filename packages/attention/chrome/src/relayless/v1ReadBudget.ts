// Inactive evidence reads only. Never use this race/cancellation machinery for
// writes: confirmed transaction completion belongs to the existing writers.
import {snapshotV1JSON} from './v1Http';
export interface V1ReadLimits {deadline:number;signal:AbortSignal;remainingRecords:number;remainingBytes:number;}
export type ReadResult<T>={status:'known';value:Readonly<T>}|{status:'unknown';code:string};
class ReadUnknown extends Error {constructor(readonly code:string){super(code);}}
function immutable<T>(value:T):Readonly<T>{
 if(value&&typeof value==='object'){for(const child of Object.values(value))immutable(child);Object.freeze(value);}return value;
}
/** One job owns all waiting and read-only resources; nested calls never reset it. */
interface ReadLifecycle {signal?:AbortSignal;deadline?:number;check?:()=>void;}
export class V1ReadJob {
 private ended=false;
 private readonly cleanup=new Set<()=>void>();
 private readonly signal:AbortSignal;
 private readonly deadline:number;
 constructor(readonly limits:V1ReadLimits,private readonly lifecycle:ReadLifecycle={}){
  this.signal=lifecycle.signal?AbortSignal.any([limits.signal,lifecycle.signal]):limits.signal;
  this.deadline=Math.min(limits.deadline,lifecycle.deadline??Infinity);
 }
 check():void {
  if(this.ended)throw new ReadUnknown('READ_ENDED');
  this.lifecycle.check?.();const now=performance.now();
  if(this.signal.aborted)throw new ReadUnknown('READ_ABORTED');
  if(!Number.isFinite(now)||!Number.isFinite(this.deadline)||now>=this.deadline)throw new ReadUnknown('READ_DEADLINE');
  if(!Number.isSafeInteger(this.limits.remainingRecords)||this.limits.remainingRecords<0||!Number.isSafeInteger(this.limits.remainingBytes)||this.limits.remainingBytes<0)throw new ReadUnknown('READ_BUDGET');
 }
 refuse(code='EVIDENCE_UNKNOWN'):never {throw new ReadUnknown(code);}
 own(cleanup:()=>void):()=>void {this.check();this.cleanup.add(cleanup);return ()=>this.cleanup.delete(cleanup);}
 async wait<T>(pending:Promise<T>):Promise<T>{this.check();const value=await pending;this.check();return value;}
 /** Cap and charge detached JSON before retaining it. Synchronous processing is
  * bounded per value, not preemptible on the JavaScript event loop. */
 copy<T>(value:T,maxBytes:number):Readonly<T>{
  this.check();if(this.limits.remainingRecords<1||this.limits.remainingBytes<1)this.refuse('READ_BUDGET');
  const captured=snapshotV1JSON(value,Math.min(maxBytes,this.limits.remainingBytes)) as T;
  this.check();const size=new TextEncoder().encode(JSON.stringify(captured)).length;
  if(size>this.limits.remainingBytes)this.refuse('READ_BUDGET');
  this.limits.remainingRecords--;this.limits.remainingBytes-=size;
  const result=immutable(captured);this.check();return result;
 }
 async run<T>(read:(job:V1ReadJob)=>Promise<T>):Promise<ReadResult<T>>{
  return new Promise(resolve=>{
   let timer:ReturnType<typeof setTimeout>|undefined;
   const done=(result:ReadResult<T>)=>{
    if(this.ended)return;this.ended=true;clearTimeout(timer);this.signal.removeEventListener('abort',abort);
    for(const close of this.cleanup){try{close();}catch{/* Only this job's read handles. */}}this.cleanup.clear();resolve(result);
   };
   const unknown=(error:unknown)=>done({status:'unknown',code:error instanceof ReadUnknown?error.code:'EVIDENCE_UNKNOWN'});
   const abort=()=>unknown(new ReadUnknown('READ_ABORTED'));
   try{
    this.check();
    // Installed before read() can perform its first open, callback or await.
    timer=setTimeout(()=>unknown(new ReadUnknown('READ_DEADLINE')),Math.max(0,this.deadline-performance.now()));
    this.signal.addEventListener('abort',abort,{once:true});
    Promise.resolve(read(this)).then(value=>{try{this.check();const captured=immutable(value);this.check();done({status:'known',value:captured});}catch(error){unknown(error);}},unknown);
   }catch(error){unknown(error);}
  });
 }
}
export function runV1Read<T>(limits:V1ReadLimits,read:(job:V1ReadJob)=>Promise<T>,lifecycle?:ReadLifecycle):Promise<ReadResult<T>>{return new V1ReadJob(limits,lifecycle).run(read);}

/** Open with NO requested version; abort the implicit absent-DB upgrade.
 * Handles stay job-owned and never touch a cached writer or its lifetime. */
export function openExistingDatabase(job:V1ReadJob,factory:IDBFactory,name:string,version:number,stores:string[],validate:(db:IDBDatabase,tx:IDBTransaction)=>void):Promise<IDBDatabase>{
 job.check();return new Promise((resolve,reject)=>{
  let request:IDBOpenDBRequest;
  const fail=()=>reject(new ReadUnknown('DATABASE_UNKNOWN'));
  try{request=factory.open(name);}catch{fail();return;}
  request.onerror=fail;request.onblocked=fail;
  request.onupgradeneeded=()=>{try{request.transaction?.abort();}catch{}fail();};
  request.onsuccess=()=>{
   const db=request.result;
   try{
    job.check();job.own(()=>db.close());
    if(db.version!==version||JSON.stringify([...db.objectStoreNames].sort())!==JSON.stringify([...stores].sort()))job.refuse('DATABASE_SHAPE');
    db.onversionchange=()=>db.close();
    const tx=db.transaction(stores,'readonly'),release=job.own(()=>{try{tx.abort();}catch{}});
    tx.onabort=()=>{release();fail();};tx.onerror=()=>{};
    tx.oncomplete=()=>{release();try{job.check();resolve(db);}catch(error){reject(error);}};
    try{job.check();validate(db,tx);job.check();}catch(error){try{tx.abort();}catch{}reject(error);}
   }catch(error){db.close();reject(error);}
  };
 });
}
export type EvidenceRead=<V>(request:IDBRequest<V>,next:(value:V)=>void)=>void;
export function readTransactionBounded<T>(job:V1ReadJob,db:IDBDatabase,stores:string[],run:(tx:IDBTransaction,read:EvidenceRead,finish:(value:T)=>void)=>void):Promise<T>{
 job.check();return new Promise((resolve,reject)=>{
  let tx:IDBTransaction;try{tx=db.transaction(stores,'readonly');}catch(error){reject(error);return;}
  const release=job.own(()=>{try{tx.abort();}catch{}});let result:T,finished=false;
  const fail=(error:unknown)=>{try{tx.abort();}catch{}reject(error);};
  const safe=(fn:()=>void)=>{try{job.check();fn();job.check();}catch(error){fail(error);}};
  const read:EvidenceRead=(request,next)=>{request.onsuccess=()=>safe(()=>next(request.result));};
  tx.onabort=()=>{release();reject(new ReadUnknown('TRANSACTION_UNKNOWN'));};tx.onerror=()=>{};
  tx.oncomplete=()=>{release();safe(()=>{if(!finished)job.refuse('TRANSACTION_UNKNOWN');resolve(result);});};
  safe(()=>run(tx,read,value=>{job.check();result=value;finished=true;}));
 });
}
