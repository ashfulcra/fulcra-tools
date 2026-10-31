// Source opt-in only. No command switches a live worker from an active legacy
// drain. Installed stale-page/source-host fencing is a separate activation gate.
import {defaultLocalStorageArea,type StorageArea} from './storageArea';
import {V1AuthError} from './v1AuthStorage';
export type AttentionAuthMode='legacy'|'v1-held';
export async function readAuthMode(storage:StorageArea=defaultLocalStorageArea()):Promise<AttentionAuthMode>{
 const value=(await storage.get('attentionAuthMode')).attentionAuthMode;
 if(value===undefined||value==='legacy')return 'legacy';if(value==='v1-held')return 'v1-held';throw new V1AuthError('MODE_UNKNOWN');
}
export async function assertLegacyAuth(storage?:StorageArea):Promise<void>{if(await readAuthMode(storage)==='v1-held')throw new V1AuthError('WORKER_OWNER_REQUIRED');}
