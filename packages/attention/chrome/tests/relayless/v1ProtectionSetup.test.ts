// @vitest-environment node
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {IDBFactory} from 'fake-indexeddb';
import {readProtectionFixture,EXTENSION_ID} from './v1ReadProtectionFixture';
const ACCOUNT='11111111-2222-4333-8444-555555555555';
const close:(()=>Promise<void>)[]=[];
const deferred=()=>{let resolve!:()=>void;const promise=new Promise<void>(r=>{resolve=r;});return {promise,resolve};};
beforeEach(async()=>{
 vi.resetModules();vi.stubGlobal('indexedDB',new IDBFactory());
 await chrome.storage.local.clear();await chrome.storage.local.set({attentionAuthMode:'v1-held'});
});
afterEach(async()=>{for(const fn of close.splice(0))await fn();vi.restoreAllMocks();vi.unstubAllGlobals();});
async function fixture(){
 const {V1AuthBroker}=await import('../../src/relayless/v1AuthBroker'),{V1AuthStorage}=await import('../../src/relayless/v1AuthStorage'),{V1Journal}=await import('../../src/relayless/v1Journal');
 const native=readProtectionFixture(false),storage=new V1AuthStorage(),journal=new V1Journal();
 const network=vi.fn(async(input:RequestInfo|URL)=>{if(new URL(String(input)).pathname==='/user/v1alpha1/info')return Response.json({userid:ACCOUNT});throw Error('Synthetic route refused');});
 const broker=new V1AuthBroker({storage,journal,fetch:network,readProtection:native.protection});
 close.push(async()=>{await journal.close();await storage.close();});
 await broker.initialize();await (broker as any).completeSignIn({access_token:'synthetic-protection',expires_in:3600},await (broker as any).claimScope('signin'));network.mockClear();
 return {native,storage,journal,broker,network};
}
test('actual worker command uses concrete native readback, preserves authority and keeps capture held',async()=>{
 const s=await fixture(),before=await s.storage.readState(),native=s.native;
 vi.stubGlobal('chrome',{...chrome,runtime:{...chrome.runtime,id:EXTENSION_ID},declarativeNetRequest:native.dnr});
 const {initializeWorkerAuth,handleV1AuthCommand,readWorkerCaptureDescriptor}=await import('../../src/relayless/v1AuthWorker');
 await initializeWorkerAuth();expect(native.state.writes).toBe(0);
 const response=await handleV1AuthCommand({type:'attention-auth-v1',version:1,command:'protection-setup',payload:null});
 expect(response).toEqual({version:1,ok:true,result:{version:1,status:'installed-as-of-read',capture:'held',drains:'disabled'}});
 expect(native.state.writes).toBe(1);await native.protection.assert();
 expect(await s.storage.readState()).toEqual(before);expect(await readWorkerCaptureDescriptor()).toBeNull();expect(s.network).not.toHaveBeenCalled();
});
test('broker setup never turns a dropped native update into success',async()=>{
 const s=await fixture();s.native.state.dropWrites=true;
 await expect((s.broker as any).installReadProtection(performance.now()+1000)).rejects.toThrow();expect(s.native.state.writes).toBe(1);
});
test('unknown policy remains untouched',async()=>{
 const s=await fixture();s.native.state.rules=[{id:9999,action:{type:'allow'},condition:{}} as any];
 await expect((s.broker as any).installReadProtection(performance.now()+1000)).rejects.toThrow();expect(s.native.state.writes).toBe(0);expect(s.native.state.rules[0].id).toBe(9999);
});
test('concurrent setup is refused while the original nonabortable native operation is pending',async()=>{
 const s=await fixture(),wait=deferred(),read=s.native.dnr.getDynamicRules;
 s.native.dnr.getDynamicRules=async()=>{await wait.promise;return read();};
 const first=(s.broker as any).installReadProtection(performance.now()+1000);const checked=expect(first).resolves.toMatchObject({status:'installed-as-of-read'});
 await expect((s.broker as any).installReadProtection(performance.now()+1000)).rejects.toThrow('PROTECTION_BUSY');wait.resolve();await checked;expect(s.native.state.writes).toBe(1);
});
test.each(['rejection','synchronous-throw'])('one native read %s cannot release a different pending native read',async failure=>{
 const s=await fixture(),wait=deferred(),entered=deferred(),read=s.native.dnr.getDynamicRules;
 s.native.dnr.getDynamicRules=async()=>{entered.resolve();await wait.promise;return read();};
 s.native.dnr.getSessionRules=()=>{if(failure==='synchronous-throw')throw Error('Synthetic native synchronous failure');return Promise.reject(Error('Synthetic native rejection'));};
 const first=s.broker.installReadProtection(performance.now()+120);const checked=expect(first).rejects.toThrow();await entered.promise;await checked;
 expect((s.broker as any).leases.size).toBe(1);
 await expect(s.broker.installReadProtection(performance.now()+1000)).rejects.toThrow('PROTECTION_BUSY');
 const publication=(await s.storage.readState()).committed!;
 await s.broker.refresh(publication.publicationId,performance.now()+40);expect((await s.storage.readAttempts()).length).toBe(0);
 wait.resolve();await new Promise(r=>setTimeout(r,20));expect(s.native.state.writes).toBe(0);expect((s.broker as any).leases.size).toBe(0);
});
test.each(['deadline','revocation'])('%s returns promptly but retains installer reservation until native settlement',async kind=>{
 const s=await fixture(),wait=deferred(),entered=deferred(),update=s.native.dnr.updateDynamicRules;
 s.native.dnr.updateDynamicRules=async options=>{entered.resolve();await wait.promise;return update(options);};
 const first=(s.broker as any).installReadProtection(performance.now()+(kind==='deadline'?120:1000));const checked=expect(first).rejects.toThrow();await entered.promise;
 if(kind==='revocation')s.broker.fencePublishers();await checked;
 await expect((s.broker as any).installReadProtection(performance.now()+1000)).rejects.toThrow('PROTECTION_BUSY');
 wait.resolve();await vi.waitFor(()=>expect(s.native.state.writes).toBe(1));
 await new Promise(r=>setTimeout(r,20));
 if(kind==='deadline')await expect((s.broker as any).installReadProtection(performance.now()+1000)).resolves.toMatchObject({status:'installed-as-of-read'});
 else await expect((s.broker as any).installReadProtection(performance.now()+1000)).rejects.toThrow('ADMISSION_HELD');
 expect((await s.broker.display()).drains).toBe('disabled');
});
test('cancellation during initial native read prevents a later policy write',async()=>{
 const s=await fixture(),wait=deferred(),entered=deferred(),read=s.native.dnr.getDynamicRules;
 s.native.dnr.getDynamicRules=async()=>{entered.resolve();await wait.promise;return read();};
 const first=(s.broker as any).installReadProtection(performance.now()+1000);const checked=expect(first).rejects.toThrow();await entered.promise;s.broker.fencePublishers();await checked;wait.resolve();
 await new Promise(r=>setTimeout(r,20));expect(s.native.state.writes).toBe(0);
});
test('durable authority change during native update refuses a success receipt',async()=>{
 const s=await fixture(),before=await s.storage.readState(),update=s.native.dnr.updateDynamicRules;
 s.native.dnr.updateDynamicRules=async options=>{await s.storage.compareAndSwap(before,{...before,revision:crypto.randomUUID(),settings:{...before.settings!,enabled:false},gate:{...before.gate!,enabled:false}});return update(options);};
 await expect((s.broker as any).installReadProtection(performance.now()+1000)).rejects.toThrow();expect(s.native.state.writes).toBe(1);expect(s.network).not.toHaveBeenCalled();
});
test('setup must keep the lease original state instead of recapturing a changed authority',async()=>{
 const s=await fixture(),read=s.storage.readState.bind(s.storage);let reads=0;
 s.storage.readState=async()=>{const state=await read();if(++reads===2){await s.storage.compareAndSwap(state,{...state,revision:crypto.randomUUID()});return read();}return state;};
 await expect(s.broker.installReadProtection(performance.now()+1000)).rejects.toThrow();expect(s.native.state.writes).toBe(0);
});
test('concrete protection honors a cancelled lifetime before every native update',async()=>{
 const s=readProtectionFixture(false),controller=new AbortController(),read=s.dnr.getDynamicRules;
 s.dnr.getDynamicRules=async()=>{controller.abort();return read();};
 await expect((s.protection.install as any)({signal:controller.signal,deadline:performance.now()+1000})).rejects.toThrow();expect(s.state.writes).toBe(0);
});
test('concrete protection refuses an expired lifetime before native reads',async()=>{
 const s=readProtectionFixture(false),read=vi.spyOn(s.dnr,'getDynamicRules');
 await expect((s.protection.install as any)({signal:new AbortController().signal,deadline:performance.now()-1})).rejects.toThrow();expect(read).not.toHaveBeenCalled();
});
test('cancellation at the asynchronous owner-check boundary cannot reach the native write',async()=>{
 const s=readProtectionFixture(false),controller=new AbortController();let checks=0;
 await expect(s.protection.install({signal:controller.signal,deadline:performance.now()+1000,check:async()=>{
  if(++checks===3)queueMicrotask(()=>queueMicrotask(()=>controller.abort()));
 }})).rejects.toThrow();expect(s.state.writes).toBe(0);
});
test.each([{}, {passed:true}, {deadline:999999}, {readProtection:{}}])('page cannot provide protection authority %j',async payload=>{
 const s=await fixture();vi.stubGlobal('chrome',{...chrome,runtime:{...chrome.runtime,id:EXTENSION_ID},declarativeNetRequest:s.native.dnr});
 const {handleV1AuthCommand}=await import('../../src/relayless/v1AuthWorker');expect(await handleV1AuthCommand({type:'attention-auth-v1',version:1,command:'protection-setup',payload})).toMatchObject({ok:false,code:'COMMAND_INVALID'});expect(s.native.state.writes).toBe(0);
});
test('cold protection command cannot initialize online admission to install policy',async()=>{
 const s=await fixture();vi.stubGlobal('chrome',{...chrome,runtime:{...chrome.runtime,id:EXTENSION_ID},declarativeNetRequest:s.native.dnr});
 const {handleV1AuthCommand}=await import('../../src/relayless/v1AuthWorker');
 expect(await handleV1AuthCommand({type:'attention-auth-v1',version:1,command:'protection-setup',payload:null})).toMatchObject({ok:false,code:'ADMISSION_HELD'});expect(s.native.state.writes).toBe(0);
});
test('missing native API refuses explicit setup without breaking read-only display',async()=>{
 await fixture();const {initializeWorkerAuth,handleV1AuthCommand}=await import('../../src/relayless/v1AuthWorker');await initializeWorkerAuth();
 expect(await handleV1AuthCommand({type:'attention-auth-v1',version:1,command:'protection-setup',payload:null})).toMatchObject({ok:false});
 expect(await handleV1AuthCommand({type:'attention-auth-v1',version:1,command:'display',payload:null})).toMatchObject({ok:true,result:{status:'committed',drains:'disabled'}});
});
test('restart explicitly re-reads current policy instead of adopting an old success flag',async()=>{
 const s=await fixture();vi.stubGlobal('chrome',{...chrome,runtime:{...chrome.runtime,id:EXTENSION_ID},declarativeNetRequest:s.native.dnr});
 const command={type:'attention-auth-v1',version:1,command:'protection-setup',payload:null};
 const first=await import('../../src/relayless/v1AuthWorker');await first.initializeWorkerAuth();expect(await first.handleV1AuthCommand(command)).toMatchObject({ok:true});
 s.native.state.rules=[];vi.resetModules();const restarted=await import('../../src/relayless/v1AuthWorker');await restarted.initializeWorkerAuth();expect(s.native.state.writes).toBe(1);
 expect(await restarted.handleV1AuthCommand(command)).toMatchObject({ok:true});expect(s.native.state.writes).toBe(2);await s.native.protection.assert();
});
