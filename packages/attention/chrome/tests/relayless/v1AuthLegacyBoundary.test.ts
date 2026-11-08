// @vitest-environment node
import {afterEach, expect, test, vi} from 'vitest';
import {memStorage} from './memStorage';
// The guard is installed before dynamic OLD publisher imports. Injected transports
// accept only synthetic endpoint responses; native/browser helpers are not invoked.
vi.stubGlobal('fetch',()=>{throw Error('External network forbidden in Slice A');});
afterEach(()=>vi.restoreAllMocks());
const deferred=<T>()=>{let resolve!:(value:T)=>void;const promise=new Promise<T>(r=>{resolve=r;});return {promise,resolve};};
test('legacy stale publication is the unsafe boundary isolated from v1',async()=>{
 const {TokenStore}=await import('../../src/relayless/tokenStore');const storage=memStorage();
 const store=new TokenStore({storage,now:()=>1000});await store.set({accessToken:'synthetic-old',refreshToken:'synthetic-refresh',expiresAt:0});
 const response=deferred<Response>(),started=deferred<void>();const refreshing=store.getValidAccessToken({force:true,fetch:async()=>{started.resolve();return response.promise;}});
 await started.promise;await store.clear();response.resolve(Response.json({access_token:'synthetic-late',expires_in:3600}));await refreshing;
 expect((await store.get())?.accessToken).toBe('synthetic-late');
});
test('legacy fresh sign-in fallback is unsafe and must never bootstrap v1',async()=>{
 const {TokenStore}=await import('../../src/relayless/tokenStore');const storage=memStorage();const store=new TokenStore({storage});
 await store.set({accessToken:'synthetic-account-A',refreshToken:'synthetic-A-refresh',idToken:'synthetic-A-id',expiresAt:0});
 const next=await store.setFromTokenSet({access_token:'synthetic-account-B',expires_in:3600});
 expect(next.refreshToken).toBe('synthetic-A-refresh');expect(next.idToken).toBe('synthetic-A-id');
});
test('generation-first hold retains original issued bytes and owner, no reissue',async()=>{
 const {actorFixture,gate,TAG,TARGET,ACTION}=await import('./v1ActionActorFixture');
 const {settleV1Action}=await import('../../src/relayless/v1ActionActor');
 const s=await actorFixture();s.state.persist=false;expect((await settleV1Action({http:s.http,journal:s.journal,intent:s.intent})).status).toBe('unknown');
 const original=await s.journal.readMutation(gate,ACTION),queue=await s.journal.readOutbox(gate,TARGET,s.entry.record.id);
 await s.journal.transition(gate,{...gate,generation:TAG});const calls=s.calls.length;
 expect((await settleV1Action({http:s.http,journal:s.journal,intent:s.intent})).status).toBe('unknown');
 expect(s.calls).toHaveLength(calls);expect(original?.requestText).toBe(s.intent.requestText);expect(queue).not.toBeNull();expect(original?.status).toBe('issued');await expect(s.journal.reserveMutation({...gate,generation:TAG},'77777777-7777-4777-8777-777777777777','deliver',{})).rejects.toThrow();await s.journal.close();
});
