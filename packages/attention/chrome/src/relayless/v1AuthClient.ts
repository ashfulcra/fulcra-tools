// Versioned page commands. No raw bearer, refresh credential or id_token is a
// page message/result. Display/prefill is read-only and never triggers renewal.
import {readAuthMode} from './v1AuthMode';
import {V1AuthError} from './v1AuthStorage';
import type {V1AuthDisplay} from './v1AuthBroker';
export async function v1Command(command:string,payload:unknown=null):Promise<any>{
 const response=await chrome.runtime.sendMessage({type:'attention-auth-v1',version:1,command,payload});
 if(!response||response.version!==1||response.ok!==true)throw new V1AuthError(typeof response?.code==='string'?response.code:'COMMAND_UNKNOWN');
 return response.result;
}
export const v1HeldMode=async()=>await readAuthMode()==='v1-held';
export const v1Display=():Promise<V1AuthDisplay>=>v1Command('display');
export async function startV1DeviceSignIn(onPrompt:(url:string,code:string)=>void):Promise<{ok:true}>{
 const start=await v1Command('signin-start');onPrompt(start.verificationUriComplete,start.userCode);
 for(let i=0;i<600;i++){
  await new Promise(resolve=>setTimeout(resolve,1000));const result=await v1Command('signin-status',{transitionId:start.transitionId});
  if(result.status==='done'&&['committed','paused'].includes(result.display.status))return {ok:true};
  if(result.status!=='pending')throw new V1AuthError('SIGNIN_HELD');
 }
 throw new V1AuthError('SIGNIN_UNKNOWN');
}
export async function requestV1Refresh(expectedPublicationId:string):Promise<V1AuthDisplay>{return v1Command('refresh',{expectedPublicationId});}
