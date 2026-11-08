// @vitest-environment node
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {IDBFactory} from 'fake-indexeddb';
// External-effect guards precede actual capture/auth imports.
vi.stubGlobal('fetch',()=>{throw Error('External transport forbidden in capture tests');});
const START=1700000000123;
beforeEach(async()=>{
 vi.resetModules();vi.stubGlobal('indexedDB',new IDBFactory());
 await chrome.storage.local.clear();await chrome.storage.sync.clear();await chrome.storage.session.clear();
 await chrome.storage.local.set({attentionAuthMode:'v1-held'});
 (chrome.identity.getProfileUserInfo as unknown as ReturnType<typeof vi.fn>).mockImplementation((_o:any,cb:any)=>cb({email:'',id:''}));
 vi.mocked(chrome.scripting.executeScript).mockResolvedValue([{result:{title:'Synthetic',og_description:null,og_type:null,favicon_url:null,lang:null}}] as never);
 const {saveWorkerSettings,handleV1AuthCommand}=await import('../../src/relayless/v1AuthWorker');vi.mocked(chrome.runtime.sendMessage).mockImplementation(async message=>handleV1AuthCommand(message));
 const {DEFAULT_SETTINGS}=await import('../../src/types');await saveWorkerSettings({...DEFAULT_SETTINGS});
});
afterEach(()=>vi.restoreAllMocks());
function tab(url='https://synthetic.invalid/path?token=secret#private'){
 vi.mocked(chrome.tabs.get).mockResolvedValue({id:1,windowId:1,active:true,url,title:'Synthetic'} as chrome.tabs.Tab);
 vi.mocked(chrome.windows.get).mockResolvedValue({id:1,focused:true} as chrome.windows.Window);
}
async function rows(){
 if(!(await indexedDB.databases()).some(d=>d.name==='fulcra-attention-ingress-v1'))return [];
 const db=await new Promise<IDBDatabase>((resolve,reject)=>{const r=indexedDB.open('fulcra-attention-ingress-v1',1);r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
 try{return await new Promise<any[]>((resolve,reject)=>{const t=db.transaction('checkpoints'),r=t.objectStore('checkpoints').getAll();t.oncomplete=()=>resolve(r.result.filter((v:any)=>v.id!=='$accounting'));t.onabort=()=>reject(t.error);});}finally{db.close();}
}
test('actual opted-in navigation checkpoints durably before cleanup without evicting a full legacy queue',async()=>{
 const originals=Array.from({length:5000},(_,i)=>({id:`old-${i}`,payload:{synthetic:i},queuedAt:i,attempts:0}));
 const priorCounters={counts:{date:'2023-11-14',logged:19,categorized:2,ignored:3},recentEmitted:[{legacy:'retained'}],relaylessSentIds:['old-sent'],relaylessResolvedAttention:{synthetic:'cache'}};
 await chrome.storage.local.set({outbox:structuredClone(originals),...structuredClone(priorCounters)});
 const {handleTabActivated,handleNavigation}=await import('../../src/background');tab();await handleTabActivated(1,START);
 tab('https://next.invalid/');await handleNavigation({tabId:1,url:'https://next.invalid/',frameId:0,timeStamp:START+12345});
 expect((await chrome.storage.local.get('outbox')).outbox).toEqual(originals);
 expect(await chrome.storage.local.get(Object.keys(priorCounters))).toEqual(priorCounters);
 const captured=await rows();expect(captured.some(r=>r.prepared?.start_time==='2023-11-14T22:13:20.123Z'&&r.prepared?.end_time==='2023-11-14T22:13:32.468Z')).toBe(true);
 expect(JSON.stringify(captured)).not.toContain('token=secret');expect(JSON.stringify(captured)).not.toContain('#private');
});
test('actual close preserves recoverable active input when durable checkpoint fails',async()=>{
 const {handleTabActivated,handleTabClose}=await import('../../src/background');tab();await handleTabActivated(1,START);
 const open=indexedDB.open.bind(indexedDB);vi.spyOn(indexedDB,'open').mockImplementation(((name:string,version?:number)=>{if(name==='fulcra-attention-ingress-v1')throw new DOMException('Synthetic quota','QuotaExceededError');return open(name,version);}) as any);
 await expect(handleTabClose(1,START+10000)).rejects.toThrow();expect((await chrome.storage.session.get('visits')).visits).toHaveProperty('1');
});
test('actual category checkpoint never persists the hidden site or title',async()=>{
 await chrome.storage.local.set({categoryMap:[{pattern:'synthetic.invalid',category:'webmail'}]});
 const {handleTabActivated,handleTabClose}=await import('../../src/background');tab();await handleTabActivated(1,START);await handleTabClose(1,START+10000);
 const captured=await rows();expect(captured.length).toBeGreaterThan(0);expect(JSON.stringify(captured)).not.toContain('synthetic.invalid');expect(JSON.stringify(captured)).not.toContain('Synthetic');expect(captured.some(r=>r.prepared?.category==='webmail'&&r.descriptor===null)).toBe(true);
});
test('actual history scan refuses disabled consent before reading browser history',async()=>{
 const {saveWorkerSettings}=await import('../../src/relayless/v1AuthWorker');const {DEFAULT_SETTINGS}=await import('../../src/types');await saveWorkerSettings({...DEFAULT_SETTINGS,enabled:false});
 const before=vi.mocked(chrome.history.search).mock.calls.length;const {fetchAndGroupHistory}=await import('../../src/wizard/history');
 await expect(fetchAndGroupHistory({daysBack:7,maxResults:100})).rejects.toThrow();expect(vi.mocked(chrome.history.search).mock.calls.length).toBe(before);
});
test('actual backfill refuses disabled consent and leaves selected rows and legacy evidence untouched',async()=>{
 const {saveWorkerSettings}=await import('../../src/relayless/v1AuthWorker');const {DEFAULT_SETTINGS}=await import('../../src/types');await saveWorkerSettings({...DEFAULT_SETTINGS,enabled:false});
 const {backfillHistory}=await import('../../src/wizard/backfill');const groups=[{host:'synthetic.invalid',count:1,urls:[{url:'https://synthetic.invalid/',title:'Original',lastVisitTime:START,visitCount:1}]}];
 await expect(backfillHistory(groups)).rejects.toThrow();expect(groups[0].urls[0].title).toBe('Original');expect((await chrome.storage.local.get('outbox')).outbox).toBeUndefined();
});
test('actual opted-in close retains uncapped clock observations and holds an uncertain orphan lapse',async()=>{
 const {handleTabActivated,handleTabClose}=await import('../../src/background');tab();await handleTabActivated(1,START);await handleTabClose(1,START+7200123);
 const captured=await rows();expect(captured.some(r=>r.raw?.observedFocusMs===7200123&&r.prepared===null&&r.reason==='continuity-unknown')).toBe(true);
});
test('actual navigation never attaches the new page metadata to the old captured URL',async()=>{
 const {handleTabActivated,handleNavigation}=await import('../../src/background');tab();await handleTabActivated(1,START);
 tab('https://next.invalid/');vi.mocked(chrome.scripting.executeScript).mockResolvedValue([{result:{title:'New private page title',og_description:'New private description',og_type:null,favicon_url:null,lang:null}}] as never);
 await handleNavigation({tabId:1,url:'https://next.invalid/',frameId:0,timeStamp:START+5000});
 const captured=await rows();expect(captured.some(r=>r.prepared?.url==='https://synthetic.invalid/path'&&r.prepared.title===null&&r.prepared.og_description===null)).toBe(true);
});
test('actual backfill lost acknowledgement retries the same stable row and freezes first label',async()=>{
 const {handleCaptureCommand}=await import('../../src/background');const {handleV1AuthCommand,saveWorkerSettings}=await import('../../src/relayless/v1AuthWorker');const {DEFAULT_SETTINGS}=await import('../../src/types');
 let lost=true;vi.mocked(chrome.runtime.sendMessage).mockImplementation(async (message:any)=>{if(message.type==='attention-capture-v1'){const result=await handleCaptureCommand(message);if(lost){lost=false;return undefined;}return result;}return handleV1AuthCommand(message);});
 const {backfillHistory}=await import('../../src/wizard/backfill');const selected=[{host:'synthetic.invalid',count:1,urls:[{url:'https://synthetic.invalid/',title:'Original title',lastVisitTime:START,visitCount:1}]}];
 await expect(backfillHistory(selected)).rejects.toThrow('UNKNOWN');const before=await rows();expect(before).toHaveLength(1);
 await saveWorkerSettings({...DEFAULT_SETTINGS,identityLabel:'Late identity'});expect(await backfillHistory(selected)).toBe(1);
 const after=await rows();expect(after).toHaveLength(1);expect(after[0].id).toBe(before[0].id);expect(after[0].prepared.chrome_identity).toBeNull();expect(after[0].prepared.end_time).toBe('2023-11-14T22:13:20.123Z');expect((await chrome.storage.local.get('outbox')).outbox).toBeUndefined();
});
test('actual freeze preserves uncapped observations and never emits a long orphan as capped focus',async()=>{
 const {handleTabActivated,handleWindowFocusChange,handleTabClose}=await import('../../src/background');tab();await handleTabActivated(1,START);await handleWindowFocusChange(-1,START+7200123);await handleTabClose(1,START+7200124);
 const captured=await rows();expect(captured.some(r=>r.raw.observedFocusMs===7200123&&r.prepared===null&&r.reason==='continuity-unknown')).toBe(true);
});
test('actual worker restart retains last durable checkpoint held with an explicit gap',async()=>{
 let b=await import('../../src/background');tab();await b.handleTabActivated(1,START);
 await chrome.storage.session.clear();vi.resetModules();b=await import('../../src/background');
 await vi.waitFor(async()=>{const captured=await rows();expect(captured.some(r=>r.state==='held'&&r.reason==='restart-gap-unknown'&&r.prepared===null)).toBe(true);});
});
test('actual observed late legacy arrivals are linked individually without changing originals, sent IDs or caches',async()=>{
 const {checkpointLegacyArrivals}=await import('../../src/background');
 const a={id:'old-a',payload:{url:'https://old.invalid/?token=must-stay-only-in-original'},queuedAt:1,attempts:0};const evidence={outbox:[a],relaylessSentIds:['old-sent'],relaylessResolvedAttention:{old:'destination'}};await chrome.storage.local.set(structuredClone(evidence));await checkpointLegacyArrivals();
 const late={id:'old-b',payload:{url:'https://late.invalid/'},queuedAt:2,attempts:0};await chrome.storage.local.set({outbox:[a,late]});await checkpointLegacyArrivals();
 const captured=await rows();expect(captured.filter(r=>r.origin==='legacy')).toHaveLength(2);expect(JSON.stringify(captured)).not.toContain('must-stay-only-in-original');expect((await chrome.storage.local.get('outbox')).outbox).toEqual([a,late]);expect((await chrome.storage.local.get('relaylessSentIds')).relaylessSentIds).toEqual(['old-sent']);
});
test('worker refuses token-bearing forged capture keys without persisting new private data',async()=>{
 const {handleCaptureCommand}=await import('../../src/background');const result=await handleCaptureCommand({type:'attention-capture-v1',version:1,command:'backfill',payload:{id:'backfill:'+'a'.repeat(64),matchingUrl:'https://synthetic.invalid/?token=secret',raw:{kind:'backfill',url:'https://synthetic.invalid/?token=secret',title:null,category:null,startTime:START-60000,endTime:START,syntheticDurationMs:60000}}});
 expect(result).toMatchObject({ok:false});expect(JSON.stringify(await rows())).not.toContain('token=secret');
});
test('surviving session observations after worker restart are held separately from the sealed original',async()=>{
 const b=await import('../../src/background');tab();await b.handleTabActivated(1,START);const prior=await rows();
 vi.resetModules();const restarted=await import('../../src/background');await restarted.handleTabClose(1,START+12345);
 const captured=await rows();expect(captured.some(r=>r.id===prior[0].id&&r.reason==='restart-gap-unknown')).toBe(true);expect(captured.some(r=>r.reason==='late-observation-held'&&r.raw.observedFocusMs===12345&&r.descriptor===null)).toBe(true);
});
test('scope revocation while tab lookup waits prevents a fresh visit from opening',async()=>{
 const b=await import('../../src/background');let entered!:()=>void,release!:(tab:chrome.tabs.Tab)=>void;const atTab=new Promise<void>(r=>entered=r),pending=new Promise<chrome.tabs.Tab>(r=>release=r);vi.mocked(chrome.tabs.get).mockImplementation(async()=>{entered();return pending;});
 const opening=b.setForegroundTab(1,START);await atTab;const {saveWorkerSettings}=await import('../../src/relayless/v1AuthWorker');const {DEFAULT_SETTINGS}=await import('../../src/types');await saveWorkerSettings({...DEFAULT_SETTINGS,enabled:false});release({id:1,windowId:1,active:true,url:'https://synthetic.invalid/'} as chrome.tabs.Tab);await opening;
 expect((await chrome.storage.session.get('visits')).visits??{}).not.toHaveProperty('1');expect((await rows()).filter(r=>r.origin==='visit')).toHaveLength(0);
});
test('disable then re-enable cannot turn the paused interval into original visit focus',async()=>{
 const b=await import('../../src/background');tab();await b.handleTabActivated(1,START);const {saveWorkerSettings}=await import('../../src/relayless/v1AuthWorker');const {DEFAULT_SETTINGS}=await import('../../src/types');await saveWorkerSettings({...DEFAULT_SETTINGS,enabled:false});await saveWorkerSettings(DEFAULT_SETTINGS);await b.handleTabClose(1,START+10000);
 expect((await rows()).some(r=>r.reason==='capture-scope-changed'&&r.prepared===null)).toBe(true);
});
test('actual storage listener retains supplied old and new legacy payloads after the current key has changed again',async()=>{
 await import('../../src/background');const payload={url:'https://old.invalid/',title:'Old',og_description:null,favicon_url:null,category:null,chrome_identity:null,og_type:null,lang:null,start_time:'2023-11-14T22:13:20Z',end_time:'2023-11-14T22:13:21Z',client:'synthetic'};
 const old={id:'old-observed',payload,queuedAt:1,attempts:0},next={id:'new-observed',payload:{...payload,url:'https://next.invalid/'},queuedAt:2,attempts:0};
 const listener=vi.mocked(chrome.storage.onChanged.addListener).mock.calls.at(-1)![0];listener({outbox:{oldValue:[old],newValue:[next]}},'local');
 await vi.waitFor(async()=>{const captured=await rows();expect(captured.filter(r=>r.origin==='legacy')).toHaveLength(2);expect(captured.some(r=>r.raw.payload?.title==='Old'&&r.raw.payload.url==='https://old.invalid/')).toBe(true);});
 expect((await chrome.storage.local.get('outbox')).outbox).toBeUndefined();
});
test('backfill metadata changes under the same stable source retain conflict evidence and the first prepared event',async()=>{
 const {handleCaptureCommand}=await import('../../src/background');const {handleV1AuthCommand}=await import('../../src/relayless/v1AuthWorker');vi.mocked(chrome.runtime.sendMessage).mockImplementation(async(message:any)=>message.type==='attention-capture-v1'?handleCaptureCommand(message):handleV1AuthCommand(message));
 const {backfillHistory}=await import('../../src/wizard/backfill');const selected=[{host:'synthetic.invalid',count:1,urls:[{url:'https://synthetic.invalid/',title:'First',lastVisitTime:START,visitCount:1}]}];await backfillHistory(selected);selected[0].urls[0].title='Changed';await backfillHistory(selected);
 const captured=await rows();expect(captured.some(r=>r.prepared?.title==='First')).toBe(true);expect(captured.some(r=>r.raw.title==='Changed'&&r.reason==='ingress-input-conflict'&&r.prepared===null)).toBe(true);
});
test('new positive focus signal after a scope transition starts a separate original portion',async()=>{
 const b=await import('../../src/background');tab();await b.handleTabActivated(1,START);const original=(await chrome.storage.session.get('visits')).visits[1].captureId;
 const {saveWorkerSettings}=await import('../../src/relayless/v1AuthWorker');const {DEFAULT_SETTINGS}=await import('../../src/types');await saveWorkerSettings({...DEFAULT_SETTINGS,identityLabel:'New scope'});await b.handleTabActivated(1,START+5000);
 const active=(await chrome.storage.session.get('visits')).visits[1];expect(active.captureId).not.toBe(original);expect(active.startTime).toBe(START+5000);expect((await rows()).some(r=>r.id===original&&r.reason==='capture-scope-changed'&&r.prepared===null)).toBe(true);
});

// Slice B round 1: reproduce independent review boundaries with real callers.
function round1Backfill(){return {type:'attention-capture-v1',version:1,command:'backfill',payload:{id:'backfill:'+'b'.repeat(64),matchingUrl:'https://synthetic.invalid/',raw:{kind:'backfill',url:'https://synthetic.invalid/',title:'Original title',category:null,startTime:START-60000,endTime:START,syntheticDurationMs:60000}}};}
test('R1 I2 checkpoint before interrupted seal freezes original nonsecret backfill label',async()=>{
 const worker=await import('../../src/relayless/v1AuthWorker'),{DEFAULT_SETTINGS}=await import('../../src/types'),{V1CaptureIngress}=await import('../../src/relayless/v1CaptureIngress');await worker.saveWorkerSettings({...DEFAULT_SETTINGS,identityLabel:'Original label'});
 const b=await import('../../src/background'),seal=vi.spyOn(V1CaptureIngress.prototype,'sealPortion').mockRejectedValueOnce(Error('synthetic interruption before seal'));
 expect(await b.handleCaptureCommand(round1Backfill())).toMatchObject({ok:false});seal.mockRestore();expect((await new V1CaptureIngress().read(round1Backfill().payload.id))?.state).toBe('active');
 await worker.saveWorkerSettings({...DEFAULT_SETTINGS,identityLabel:'Changed label'});expect(await b.handleCaptureCommand(round1Backfill())).toMatchObject({ok:true});expect((await new V1CaptureIngress().read(round1Backfill().payload.id))?.prepared?.chrome_identity).toBe('Original label');
});
test('R1 I3 completed disable while backfill read waits prevents ordinary prepared receipt',async()=>{
 const {V1CaptureIngress}=await import('../../src/relayless/v1CaptureIngress'),b=await import('../../src/background'),worker=await import('../../src/relayless/v1AuthWorker'),{DEFAULT_SETTINGS}=await import('../../src/types');await b.checkpointLegacyArrivals();
 let entered!:()=>void,release!:()=>void,once=true;const atRead=new Promise<void>(r=>entered=r),resume=new Promise<void>(r=>release=r),read=V1CaptureIngress.prototype.read;
 const spy=vi.spyOn(V1CaptureIngress.prototype,'read').mockImplementation(async function(this:InstanceType<typeof V1CaptureIngress>,id){if(once&&id===round1Backfill().payload.id){once=false;entered();await resume;}return read.call(this,id);});
 const pending=b.handleCaptureCommand(round1Backfill());await atRead;await worker.saveWorkerSettings({...DEFAULT_SETTINGS,enabled:false});release();const result=await pending;spy.mockRestore();const row=await new V1CaptureIngress().read(round1Backfill().payload.id);
 expect(result).toMatchObject({ok:false});expect(row?.prepared??null).toBeNull();expect(row?.reason).toBe('capture-scope-changed');expect(row?.raw).toEqual(round1Backfill().payload.raw);
});
test('R1 I4 completed disable during inner metadata tab lookup prevents a newly issued extraction',async()=>{
 const b=await import('../../src/background'),worker=await import('../../src/relayless/v1AuthWorker'),{DEFAULT_SETTINGS}=await import('../../src/types');tab('https://synthetic.invalid/');await b.setForegroundTab(1,START);const before=vi.mocked(chrome.scripting.executeScript).mock.calls.length;
 let entered!:()=>void,release!:()=>void,calls=0;const atLookup=new Promise<void>(r=>entered=r),resume=new Promise<void>(r=>release=r);vi.mocked(chrome.tabs.get).mockImplementation(async()=>{if(++calls===2){entered();await resume;}return {id:1,windowId:1,active:true,url:'https://synthetic.invalid/'} as chrome.tabs.Tab;});
 const pending=b.handleTabClose(1,START+5000);await atLookup;await worker.saveWorkerSettings({...DEFAULT_SETTINGS,enabled:false});release();await pending;expect(vi.mocked(chrome.scripting.executeScript).mock.calls.length).toBe(before);
});
test('R1 I5 observed unsafe original removal remains durably UNKNOWN after quiet sweep and restart',async()=>{
 let b=await import('../../src/background');await b.checkpointLegacyArrivals();const old={id:'unsafe-original',payload:{url:'https://synthetic.invalid/?token=synthetic-private'},queuedAt:1,attempts:0};await chrome.storage.local.set({outbox:[old]});await b.checkpointLegacyArrivals();await chrome.storage.local.set({outbox:[]});await b.checkpointLegacyArrivals([old]);await b.checkpointLegacyArrivals();
 const message={type:'attention-capture-v1',version:1,command:'status',payload:null};let result:any=await b.handleCaptureCommand(message);expect(result.result).toMatchObject({legacyObservation:'unknown',legacyRetentionUnknown:1});vi.resetModules();b=await import('../../src/background');await b.checkpointLegacyArrivals();result=await b.handleCaptureCommand(message);expect(result.result).toMatchObject({legacyObservation:'unknown',legacyRetentionUnknown:1});expect(JSON.stringify(await rows())).not.toContain('synthetic-private');expect((await chrome.storage.local.get('outbox')).outbox).toEqual([]);
});
test('R1 I2 unknown active backfill context stays held without a retrospective current label',async()=>{
 const b=await import('../../src/background');await b.checkpointLegacyArrivals();const {V1CaptureIngress}=await import('../../src/relayless/v1CaptureIngress'),c=round1Backfill();await new V1CaptureIngress().checkpointPortion({id:c.payload.id,origin:'backfill',linkage:{kind:'history-selection',rowId:c.payload.id},descriptor:null,sourceContext:null,raw:c.payload.raw});
 expect(await b.handleCaptureCommand(c)).toMatchObject({ok:false,code:'CAPTURE_CONTEXT_UNKNOWN'});const row=await new V1CaptureIngress().read(c.payload.id);expect(row?.prepared).toBeNull();expect(row?.reason).toBe('capture-context-unknown');expect(row?.sourceContext).toBeNull();
});
test('R1 I2 first live checkpoint retains source label after session loss and worker restart',async()=>{
 const worker=await import('../../src/relayless/v1AuthWorker'),{DEFAULT_SETTINGS}=await import('../../src/types');await worker.saveWorkerSettings({...DEFAULT_SETTINGS,identityLabel:'Original live label'});let b=await import('../../src/background');tab();await b.handleTabActivated(1,START);let stored=await rows();expect(stored[0].sourceContext).toEqual({version:1,client:'fulcra-attention-chrome/0.1.0',identityLabel:'Original live label'});
 await chrome.storage.session.clear();vi.resetModules();b=await import('../../src/background');await b.checkpointLegacyArrivals();stored=await rows();expect(stored[0].sourceContext.identityLabel).toBe('Original live label');expect(stored[0].prepared).toBeNull();expect(stored[0].reason).toBe('restart-gap-unknown');
});
test('R1 I5 quiet observation rechecks a durable unsafe link after an earlier loss write was interrupted',async()=>{
 const b=await import('../../src/background'),{V1CaptureIngress}=await import('../../src/relayless/v1CaptureIngress');await b.checkpointLegacyArrivals();const old={id:'unsafe-interrupted-loss',payload:{url:'https://synthetic.invalid/?token=synthetic-private'},queuedAt:1,attempts:0};await chrome.storage.local.set({outbox:[old]});await b.checkpointLegacyArrivals();await chrome.storage.local.set({outbox:[]});
 const loss=vi.spyOn(V1CaptureIngress.prototype,'noteLegacyLoss').mockRejectedValueOnce(Error('synthetic interrupted loss write'));await expect(b.checkpointLegacyArrivals([old])).rejects.toThrow();loss.mockRestore();await b.checkpointLegacyArrivals();
 const result:any=await b.handleCaptureCommand({type:'attention-capture-v1',version:1,command:'status',payload:null});expect(result.result).toMatchObject({legacyObservation:'unknown',legacyRetentionUnknown:1});
});

test.each([{day:'same-day',active:false},{day:'same-day',active:true},{day:'stale-day',active:false},{day:'stale-day',active:true}])('R2 N2 held ignored navigation preserves $day legacy Counts with active=$active',async({day,active})=>{
 const b=await import('../../src/background');tab('https://synthetic.invalid/');if(active)await b.handleTabActivated(1,START);
 const originalCounts={date:day==='same-day'?new Date().toISOString().slice(0,10):'2000-01-01',logged:19,categorized:7,ignored:4};await chrome.storage.local.set({counts:structuredClone(originalCounts)});await chrome.storage.sync.set({ignoreList:[{pattern:'ignored.invalid',addedAt:'2026-01-01T00:00:00Z'}]});tab('https://ignored.invalid/');
 await b.handleNavigation({tabId:1,url:'https://ignored.invalid/',frameId:0,timeStamp:START+5000});
 expect((await chrome.storage.local.get('counts')).counts).toEqual(originalCounts);expect((await chrome.storage.session.get('visits')).visits??{}).not.toHaveProperty('1');const captured=await rows();expect(captured.filter(r=>r.origin==='visit')).toHaveLength(active?1:0);if(active)expect(captured[0]).toMatchObject({state:'held',prepared:{url:'https://synthetic.invalid/',start_time:'2023-11-14T22:13:20.123Z',end_time:'2023-11-14T22:13:25.123Z'}});
});
