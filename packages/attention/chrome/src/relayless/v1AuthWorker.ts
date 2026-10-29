// This module is imported only by the Chrome worker. No default/installed
// activation; opted-in held mode keeps BOTH drains disabled in Slice A.
import {V1AuthBroker} from './v1AuthBroker';
import {V1AuthStorage,V1AuthError,readV1Settings} from './v1AuthStorage';
import {V1Journal} from './v1Journal';
import {V1CaptureIngress} from './v1CaptureIngress';
import {readAuthMode,type AttentionAuthMode} from './v1AuthMode';
import {DEFAULT_SETTINGS,type Settings} from '../types';
// Local cancellation marker only; never account/generation/setup evidence.
let captureEpoch=crypto.randomUUID();
export function workerCaptureEpoch():string{return captureEpoch;}
function fenceCapture():void{captureEpoch=crypto.randomUUID();}
let mode:Promise<AttentionAuthMode>|undefined,owner:V1AuthBroker|undefined,settingsReady:Promise<void>|undefined,authReady:Promise<unknown>|undefined;
export async function workerAuthMode():Promise<AttentionAuthMode>{
 mode??=readAuthMode();const chosen=await mode;if(await readAuthMode()!==chosen)throw new V1AuthError('MODE_CHANGED_RESTART_REQUIRED');return chosen;
}
async function startWorkerOwner(recover=true,scope=false):Promise<V1AuthBroker>{
 if(await workerAuthMode()!=='v1-held')throw new V1AuthError('MODE_MISMATCH');
 if(!owner){owner=new V1AuthBroker({storage:new V1AuthStorage(),journal:new V1Journal()});settingsReady=owner.initializeSettings();void settingsReady.catch(()=>{});if(scope)authReady=settingsReady;}
 if(recover&&!authReady){authReady=settingsReady!.then(()=>owner!.initialize());void authReady.catch(()=>{});}
 return owner;
}
async function broker():Promise<V1AuthBroker>{const b=await startWorkerOwner();await authReady;return b;}
export async function legacyDrainAllowed():Promise<boolean>{return await workerAuthMode()==='legacy';}
export async function handleV1AuthCommand(message:unknown):Promise<unknown>{
 try{
  if(!message||typeof message!=='object'||Array.isArray(message))throw new V1AuthError('PROTOCOL_MISMATCH');const m=message as Record<string,unknown>;
  if(Object.keys(m).sort().join(',')!=='command,payload,type,version'||m.type!=='attention-auth-v1'||m.version!==1||typeof m.command!=='string')throw new V1AuthError('PROTOCOL_MISMATCH');
  const localScope=['signout','settings','signin-start'].includes(m.command);if(localScope){fenceCapture();owner?.fencePublishers();}
  if(m.command==='settings-read'&&m.payload!==null)throw new V1AuthError('COMMAND_INVALID');
  const b=await startWorkerOwner(!localScope&&m.command!=='settings-read',localScope);if(m.command==='display')await settingsReady!.catch(()=>{});else await settingsReady;const p=m.payload as Record<string,unknown>|null;let result:unknown;
  switch(m.command){
   case 'settings-read':result=await b.readSettings();break;
   case 'display':if(p!==null)throw new V1AuthError('COMMAND_INVALID');result=await b.display();break;
   case 'signin-start':if(p!==null)throw new V1AuthError('COMMAND_INVALID');result=await b.startSignIn();break;
   case 'signin-status':if(!p||Object.keys(p).join(',')!=='transitionId'||typeof p.transitionId!=='string')throw new V1AuthError('COMMAND_INVALID');result=await b.signInStatus(p.transitionId);break;
   case 'refresh':if(!p||Object.keys(p).join(',')!=='expectedPublicationId'||typeof p.expectedPublicationId!=='string')throw new V1AuthError('COMMAND_INVALID');result=await b.refresh(p.expectedPublicationId);break;
   case 'signout':if(p!==null)throw new V1AuthError('COMMAND_INVALID');result=await b.signOut();break;
   case 'settings':result=await b.changeSettings(p);if((result as {status:string}).status==='held')throw new V1AuthError('SCOPE_UNKNOWN');break;
   case 'destination-list':result={status:'held',code:'SETUP_HELD',destinations:[]};break;
   case 'destination-choose':case 'destination-create':throw new V1AuthError('SETUP_HELD');
   default:throw new V1AuthError('PROTOCOL_MISMATCH');
  }
  return {version:1,ok:true,result};
 }catch(error){return {version:1,ok:false,code:error instanceof V1AuthError?error.code:'UNKNOWN'};}
}

/** Worker-local callers cannot send a command to their own message listener. */
export async function saveWorkerSettings(value:Settings):Promise<void>{
 fenceCapture();owner?.fencePublishers();
 if(await workerAuthMode()==='legacy'){await chrome.storage.local.set({settings:value});return;}
 const response=await handleV1AuthCommand({type:'attention-auth-v1',version:1,command:'settings',payload:value}) as {ok:boolean;code?:string};
 if(!response.ok)throw new V1AuthError(response.code??'SETTINGS_UNKNOWN');
}

export async function loadWorkerSettings():Promise<Settings>{
 if(await workerAuthMode()==='legacy')return {...DEFAULT_SETTINGS,...(await chrome.storage.local.get('settings')).settings};
 await startWorkerOwner(false);await settingsReady;return readV1Settings();
}
export async function initializeWorkerAuth():Promise<void>{if(await workerAuthMode()==='v1-held')await broker();}

/** Source-only B: completed setup provenance is not yet published. Missing
 * positive descriptor never borrows current display/settings/cache authority. */
export async function readWorkerCaptureDescriptor():Promise<import('./v1CaptureIngress').CaptureDescriptor|null>{
 await startWorkerOwner(false);await settingsReady;return null;
}
export async function workerCaptureDescriptorCurrent(_descriptor:import('./v1CaptureIngress').CaptureDescriptor):Promise<boolean>{
 await startWorkerOwner(false);await settingsReady;return false;
}

/** Worker retains the journal root; page/live callers hand over only ingress ID. */
export async function enqueueWorkerCapture(id:string):Promise<import('./v1CaptureIngress').CaptureCheckpoint>{
 const ingress=new V1CaptureIngress(),row=await ingress.read(id);
 if(!row)throw new V1AuthError('CAPTURE_UNKNOWN');
 if(!row.descriptor)return row;
 const journal=new V1Journal();try{return await ingress.enqueueCaptured(id,{current:workerCaptureDescriptorCurrent,journal});}finally{await journal.close();}
}
