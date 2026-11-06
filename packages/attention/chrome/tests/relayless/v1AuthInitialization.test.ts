// @vitest-environment node
import {expect,test,vi} from 'vitest';
import {IDBFactory} from 'fake-indexeddb';
import {memStorage} from './memStorage';
vi.stubGlobal('fetch',()=>{throw Error('External network forbidden in initialization tests');});
const modules=async()=>({...await import('../../src/relayless/v1AuthStorage'),...await import('../../src/types')});
const deleteAuthority=(factory:IDBFactory)=>new Promise<void>((resolve,reject)=>{const r=factory.deleteDatabase('fulcra-attention-auth-v1');r.onsuccess=()=>resolve();r.onerror=()=>reject(r.error);r.onblocked=()=>reject(Error('Synthetic still open'));});
test('I4 default and read-only opens cannot mint an authority or initialization witness',async()=>{
 const {V1AuthStorage}=await modules(),indexedDB=new IDBFactory(),local=memStorage();
 for(const readOnly of [false,true]){const store=new V1AuthStorage({indexedDB,credentials:local,readOnly});await expect(store.readState()).rejects.toThrow();await store.close().catch(()=>{});}
 expect(await indexedDB.databases()).toEqual([]);expect(await local.get(null)).toEqual({});
});
test.each(['CLAIMED','CREATING','COMMITTED','AUTHORITY'])('I4 initialization lost %s acknowledgment recovers only original witness and snapshot',async boundary=>{
 const {V1AuthStorage,DEFAULT_SETTINGS,initializationKey}=await modules(),indexedDB=new IDBFactory(),local=memStorage(),original={...DEFAULT_SETTINGS,identityLabel:'Original strict snapshot'};await local.set({settings:original});
 const store=new V1AuthStorage({indexedDB,credentials:local}),set=local.set;let hit=false;
 local.set=async items=>{await set(items);if(!hit&&(items[initializationKey] as any)?.stage===boundary){hit=true;throw Error('Synthetic witness acknowledgment lost');}};
 const cas=store.compareAndSwap.bind(store);store.compareAndSwap=async(...args)=>{await cas(...args);if(!hit&&boundary==='AUTHORITY'){hit=true;throw Error('Synthetic authority acknowledgment lost');}};
 await expect(store.initializeAuthority(null)).rejects.toThrow();expect(hit).toBe(true);const w=(await local.get(initializationKey))[initializationKey] as any;expect(w.initialSettings).toEqual(original);await store.close().catch(()=>{});local.set=set;
 await local.set({settings:{...DEFAULT_SETTINGS,identityLabel:'Forbidden reread'}});const restarted=new V1AuthStorage({indexedDB,credentials:local});
 if(boundary==='CREATING'){await expect(restarted.initializeAuthority(null)).rejects.toThrow('AUTHORITY_MISSING');expect(await indexedDB.databases()).toEqual([]);}
 else{await restarted.initializeAuthority(null);const s=await restarted.readState();expect(s.initializationId).toBe(w.initializationId);expect(s.settings).toEqual(original);expect((await local.get(initializationKey))[initializationKey]).toEqual({...w,stage:'COMMITTED'});}
 await restarted.close().catch(()=>{});
});
test.each(['CREATING','COMMITTED'])('I4 entire database deletion with %s witness never recreates or rereads legacy',async stage=>{
 const {V1AuthStorage,DEFAULT_SETTINGS,initializationKey}=await modules(),indexedDB=new IDBFactory(),local=memStorage(),store=new V1AuthStorage({indexedDB,credentials:local});await store.initializeAuthority(null);const w=(await local.get(initializationKey))[initializationKey] as any;await store.close();await local.set({[initializationKey]:{...w,stage},settings:{...DEFAULT_SETTINGS,enabled:false}});await deleteAuthority(indexedDB);
 const restarted=new V1AuthStorage({indexedDB,credentials:local});await expect(restarted.initializeAuthority(null)).rejects.toThrow('AUTHORITY_MISSING');expect(await indexedDB.databases()).toEqual([]);expect((await local.get(initializationKey))[initializationKey]).toEqual({...w,stage});await restarted.close().catch(()=>{});
});
test.each(['malformed','mismatched','claim-over-existing','credential-evidence','gate-evidence'])('I4 %s initialization evidence is held without overwriting witness',async kind=>{
 const {V1AuthStorage,DEFAULT_SETTINGS,initializationKey}=await modules(),indexedDB=new IDBFactory(),local=memStorage();const w={version:1,initializationId:crypto.randomUUID(),stage:'COMMITTED',initialSettings:DEFAULT_SETTINGS};
 if(['mismatched','claim-over-existing'].includes(kind)){const initial=new V1AuthStorage({indexedDB,credentials:local});await initial.initializeAuthority(null);await initial.close();}
 if(kind==='malformed')await local.set({[initializationKey]:{version:1}});
 if(kind==='mismatched'||kind==='claim-over-existing')await local.set({[initializationKey]:{...w,stage:kind==='claim-over-existing'?'CLAIMED':'COMMITTED'}});
 if(kind==='credential-evidence')await local.set({'relaylessV1Credential:synthetic-evidence':{version:1}});
 const before=await local.get(null),store=new V1AuthStorage({indexedDB,credentials:local});await expect(store.initializeAuthority(kind==='gate-evidence'?{version:1,accountId:null,generation:crypto.randomUUID(),enabled:false}:null)).rejects.toThrow();expect(await local.get(null)).toEqual(before);await store.close().catch(()=>{});
});
test('I4 competing COMMITTED witness discovered before stage write is never overwritten',async()=>{
 const {V1AuthStorage,DEFAULT_SETTINGS,initializationKey}=await modules(),indexedDB=new IDBFactory(),local=memStorage(),claimed={version:1,initializationId:crypto.randomUUID(),stage:'CLAIMED',initialSettings:DEFAULT_SETTINGS},winner={...claimed,initializationId:crypto.randomUUID(),stage:'COMMITTED'};await local.set({[initializationKey]:claimed});
 const get=local.get;let reads=0;local.get=async keys=>{if(keys===initializationKey&&++reads===2)await local.set({[initializationKey]:winner});return get(keys);};
 const store=new V1AuthStorage({indexedDB,credentials:local});await expect(store.initializeAuthority(null)).rejects.toThrow('INITIALIZATION_CONFLICT');expect((await local.get(initializationKey))[initializationKey]).toEqual(winner);expect(await indexedDB.databases()).toEqual([]);await store.close().catch(()=>{});
});
