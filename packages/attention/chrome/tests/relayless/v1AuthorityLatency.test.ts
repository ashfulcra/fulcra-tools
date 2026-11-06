// @vitest-environment node
import {expect,test} from 'vitest';
import {actorFixture,TARGET,CONTROL} from './v1ActionActorFixture';
import {reconcileV1Action,settleV1Action} from '../../src/relayless/v1ActionActor';
type Fixture=Awaited<ReturnType<typeof actorFixture>>;
const run=(s:Fixture,extra={})=>reconcileV1Action({http:s.http,journal:s.journal,intent:s.intent,...extra});
function schedule(s:Fixture){const groups={principal:0,ownedInventory:0,targetSchema:0,controlSchema:0,tag:0,nativeSchema:0,policy:0,source:0,appPost:0};
 for(const c of s.calls){const u=new URL(c.url),p=u.pathname;if(c.init.method==='POST')groups.appPost++;else if(p.endsWith('/info'))groups.principal++;else if(p==='/input/v1/data_type')groups.ownedInventory++;else if(p.includes('/tag/'))groups.tag++;else if(p.includes('DeletedRecord'))groups.nativeSchema++;else if(p.endsWith('/schema'))groups[p.includes(CONTROL)?'controlSchema':'targetSchema']++;else if(p==='/data/v1/records')groups[u.searchParams.get('q')!.includes(CONTROL)?'policy':'source']++;}
 return groups;}
for(const path of ['fresh-delivery','already-present','exclude-new-policy','exclude-existing-policy','ambiguous-post','issued-retry','verified-phase-retry','terminal-history'])test('bounded actual fetch and streaming-body latency: '+path,async()=>{
 const s=await actorFixture(false,path.startsWith('exclude-'));
 if(path==='already-present')s.state.rows[TARGET]=[s.entry.record];
 if(path.startsWith('exclude-'))s.state.rows[TARGET]=[s.entry.record];
 if(path==='exclude-existing-policy')s.state.rows[CONTROL]=[s.policy];
 if(path==='ambiguous-post')s.state.mode='lost';
 if(path==='issued-retry'){s.state.persist=false;s.state.mode='lost';expect((await run(s)).status).toBe('unknown');}
 if(path==='verified-phase-retry')expect((await run(s)).status).toBe('observed');
 if(path==='terminal-history')expect((await settleV1Action({http:s.http,journal:s.journal,intent:s.intent})).status).toBe('settled');
 s.calls.length=0;s.state.fetchDelay=8;s.state.bodyDelay=4;
 const started=performance.now();
 const result=path==='terminal-history'?await settleV1Action({http:s.http,journal:s.journal,intent:s.intent,timeoutMs:2500}):await run(s,{timeoutMs:2500});
 const elapsedMs=Math.round(performance.now()-started),semantic=schedule(s);
 expect(result.status).toBe(path==='issued-retry'?'unknown':path==='terminal-history'?'already-settled':'observed');
 expect(elapsedMs).toBeLessThan(2500);
 if(path==='terminal-history'){expect(s.calls).toHaveLength(0);expect(semantic.appPost).toBe(0);}
 else{
  // Each coherent decision reads both current records, with two independently
  // closed metadata/principal snapshots. No whole-program count is pinned.
  expect(semantic.source).toBe(semantic.policy);expect(semantic.source).toBeGreaterThan(0);
  expect(semantic.ownedInventory).toBe(2*semantic.nativeSchema);
  expect(semantic.targetSchema).toBe(semantic.nativeSchema);expect(semantic.controlSchema).toBe(semantic.nativeSchema);
  expect(semantic.principal).toBeGreaterThanOrEqual(2*semantic.source);
  expect(semantic.appPost).toBe(path==='exclude-new-policy'?2:['fresh-delivery','exclude-existing-policy','ambiguous-post'].includes(path)?1:0);
 }
 console.log('AUTHORITY_LATENCY '+JSON.stringify({path,fetchDelayMs:8,bodyDelayMs:4,actorBudgetMs:2500,elapsedMs,applicationFetches:s.calls.length,semantic,wirePostCount:'not measured by fetch fixture'}));
 await s.journal.close();
},10000);
test('over-budget streamed authority returns UNKNOWN and cancels descendants without publication',async()=>{
 const s=await actorFixture();s.state.fetchDelay=15;s.state.bodyDelay=20;
 const result=await run(s,{timeoutMs:40});expect(result.status).toBe('unknown');expect(s.calls.some(c=>c.init.method==='POST')).toBe(false);
 const count=s.calls.length;expect(s.calls.at(-1)?.init.signal?.aborted).toBe(true);await new Promise(resolve=>setTimeout(resolve,80));expect(s.calls).toHaveLength(count);await s.journal.close();
});
