// Opt-in pending capture. Current authority and final row recheck stay here;
// byte/schema/source reproduction has one pure canonical implementation.
import type {V1Journal,V1Gate} from "./v1Journal";
import {prepareQueuedAttention,queuedRowFor,captureQueueScope,decodeQueuedAttentionRow,type Capture,type V1QueuedAttention} from "./v1QueueCodec";
export type {V1QueuedAttention} from "./v1QueueCodec";
export class V1Outbox {
 readonly #journal:V1Journal;
 constructor(journal:V1Journal) {this.#journal=journal;}
 async enqueue(input:Capture):Promise<Readonly<V1QueuedAttention>> {
  const entry=await prepareQueuedAttention(input),row=await queuedRowFor(entry);
  await this.#journal.putOutbox(entry.scope,row);return entry;
 }
 async read(scope:V1Gate,dataType:string,recordId:string):Promise<Readonly<V1QueuedAttention>|null> {
  const captured=captureQueueScope(scope),row=await this.#journal.readOutbox(captured,dataType,recordId);
  if(!row)return null;
  const reproduced=await decodeQueuedAttentionRow(row);
  // Reproof contains asynchronous digests. A gate change during them must hold
  // the old entry, even though the initial IDB read was current.
  const final=await this.#journal.readOutbox(captured,dataType,recordId);
  if(JSON.stringify(final)!==JSON.stringify(row))throw new Error("Attention v1 queued capture changed; retain original data");return reproduced;
 }
}
