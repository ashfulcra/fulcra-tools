// Opt-in account-scoped tag setup, through V1Http's API-principal boundary.
// Durable bindings preserve UUID identity after a cloud-side rename. They are
// historical ownership evidence, not a claim that the tag is currently live.
import { sha256Hex } from "./sha256";
import { V1HttpError, type V1Http } from "./v1Http";
import type { V1Journal } from "./v1Journal";
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
function validText(value:unknown):asserts value is string {
  if(typeof value!=="string"||value==="."||value===".."||!value.trim()||value.length>2048||[...value].length>1024||/[\x00-\x1f\x7f]/.test(value))throw new Error("Attention v1 tag label is invalid");
  for(const point of value){const code=point.codePointAt(0)!;if(code>=0xd800&&code<=0xdfff)throw new Error("Attention v1 tag label is invalid");}
}
/** Match Python tag_create_name for bounded, non-dot-segment labels. */
export async function tagCreateName(label:string):Promise<string> {
  validText(label);
  if([...label].length<=30&&!label.includes("/"))return label;
  const prefix=label.toLowerCase().replace(/[^a-z0-9]+/g,"-").slice(0,9).replace(/^-+|-+$/g,"")||"source";
  return `${prefix}-${(await sha256Hex(label)).slice(0,20)}`;
}
interface Options {
  deadline?:number;signal?:AbortSignal;
  http:Pick<V1Http,"scope"|"json"|"assertJournal">;
  journal:Pick<V1Journal,"readTag"|"reserveTag"|"submitTag"|"resolveTag">;
}
function identity(body:unknown,name:string,account:string):string {
  if(!body||typeof body!=="object"||Array.isArray(body))throw new Error("Attention v1 tag readback is unreadable");
  const row=body as Record<string,unknown>;
  if(typeof row.id!=="string"||!UUID.test(row.id)||(row.name!==undefined&&row.name!==name)
    ||(row.userid!==undefined&&row.userid!==account)||(row.fulcra_userid!==undefined&&row.fulcra_userid!==account)
    ||(row.deleted_at!==undefined&&row.deleted_at!==null))throw new Error("Attention v1 tag readback differs");
  return row.id;
}
export async function resolveV1Tag(options:Options,label:string):Promise<string> {
  // Strings and the HTTP object's immutable captured gate are copied before
  // asynchronous hashing. No legacy/global tag cache is consulted.
  const check=()=>{if(options.signal?.aborted||options.deadline!==undefined&&(!Number.isFinite(options.deadline)||performance.now()>=options.deadline))throw new Error("Attention v1 tag scope ended");};
  check();validText(label);
  const http=options.http,journal=options.journal,scope=Object.freeze({...http.scope});
  http.assertJournal(journal);
  const name=await tagCreateName(label);check();
  const path="/user/v1alpha1/tag/name/"+encodeURIComponent(name);
  const prior=await journal.readTag(scope,label,name);
  check();if(prior?.status==="resolved")return prior.destination!;
  let found:string|null=null;
  try{found=identity(await http.json(path),name,scope.accountId!);}
  catch(error){if(!(error instanceof V1HttpError&&error.code==="http_error"&&error.status===404))throw error;}
  check();const reservation=await journal.reserveTag(scope,label,name,options.deadline,options.signal);
  if(found!==null)return (await journal.resolveTag(scope,label,name,found,options.deadline,options.signal)).destination!;
  if(reservation.intent.status==="resolved")return reservation.intent.destination!;
  if(!reservation.created)throw new Error("Attention v1 prior tag creation must be reconciled");
  // A possibly dispatched creation can never be retried from an empty lookup.
  const created=identity(await http.json("/user/v1alpha1/tag",{method:"POST",body:{name}}),name,scope.accountId!);
  await journal.submitTag(scope,label,name,created,options.deadline,options.signal);
  const observed=identity(await http.json(path),name,scope.accountId!);
  if(observed!==created)throw new Error("Attention v1 created tag readback differs");
  return (await journal.resolveTag(scope,label,name,observed,options.deadline,options.signal)).destination!;
}
