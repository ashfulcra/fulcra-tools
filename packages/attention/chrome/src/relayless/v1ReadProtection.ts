// Opt-in Chrome network protection for exact JSONL reads and typed receipts. Browser Fetch can
// accept incomplete gzip without a trailer; decoded EOF alone is insufficient.
import {auth0OriginStripRule,AUTH0_ORIGIN_STRIP_RULE_ID} from "./authOriginFix";
// The installed Chrome declarations predate Chrome128 response conditions.
// Keep this narrow shape; unsupported runtimes must reject setup/readback,
// never silently omit the encoded-response block.
type Rule=chrome.declarativeNetRequest.Rule & {condition:chrome.declarativeNetRequest.RuleCondition & {
 responseHeaders?:{header:string;values?:string[];excludedValues?:string[]}[];
}};
interface DNR {
 getDynamicRules():Promise<Rule[]>;
 getSessionRules():Promise<unknown[]>;
 getEnabledRulesets():Promise<string[]>;
 updateDynamicRules(options:{removeRuleIds:number[];addRules:Rule[]}):Promise<unknown>;
}
const IDS=[1002,1003,1004,1005];
function refuse():never {throw new Error("Attention v1 read protection is unavailable. Keep queued data and complete setup again.");}
export function v1ReadProtectionRules(extensionId:string):Rule[] {
 if(typeof extensionId!=="string"||!/^[a-p]{32}$/.test(extensionId))refuse();
 const condition={regexFilter:"^https://api\\.fulcradynamics\\.com/data/v1/records\\?",isUrlFilterCaseSensitive:true,
  initiatorDomains:[extensionId],requestMethods:["get"] as chrome.declarativeNetRequest.RequestMethod[],
  resourceTypes:["xmlhttprequest","other"] as chrome.declarativeNetRequest.ResourceType[]};
 const pair=(offset:number,condition:Rule["condition"]):Rule[]=>[
 {id:IDS[offset],priority:1,action:{type:"modifyHeaders" as chrome.declarativeNetRequest.RuleActionType,
  requestHeaders:[{header:"accept-encoding",operation:"set" as chrome.declarativeNetRequest.HeaderOperation,value:"identity"}]},condition},
 {id:IDS[offset+1],priority:2,action:{type:"block" as chrome.declarativeNetRequest.RuleActionType},condition:{...condition,
  responseHeaders:[{header:"content-encoding",excludedValues:["identity"]}]}}];
 // Chrome's2KiB compiled-regex bound refuses fixed UUID repetition. This
 // protective superset also covers malformed hex/hyphen Event suffixes; it
 // grants no authority. Typed effect/journal validation enforces full UUIDs.
 return [...pair(0,condition),...pair(2,{...condition,
  regexFilter:"^https://api\\.fulcradynamics\\.com/ingest/v1/record/(Event/[a-f0-9-]+|DeletedRecord)\\?api_version=v1$",
  requestMethods:["post"] as chrome.declarativeNetRequest.RequestMethod[]})];
}
/** Canonicalize only a small finite native-rule snapshot. Array order is not
 * significant in these fixed rules (all header arrays have exactly one item).
 * Unknown properties, duplicate entries and altered rules still differ. */
function canonical(value:unknown):string {
 let nodes=0;
 const visit=(value:unknown,depth:number):unknown=>{
  if(++nodes>1024||depth>16)refuse();
  if(value===null||typeof value==="boolean")return value;
  if(typeof value==="string"){if(value.length>8192)refuse();return value;}
  if(typeof value==="number"){if(!Number.isFinite(value))refuse();return value;}
  if(!value||typeof value!=="object"||Object.getOwnPropertySymbols(value).length)refuse();
  if(Array.isArray(value)) {
   if(value.length>64||Object.getOwnPropertyNames(value).length!==value.length+1)refuse();
   const result=[];for(let i=0;i<value.length;i++){const d=Object.getOwnPropertyDescriptor(value,String(i));if(!d?.enumerable||!Object.hasOwn(d,"value"))refuse();result.push(visit(d.value,depth+1));}
   return result.sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)));
  }
  if(![Object.prototype,null].includes(Object.getPrototypeOf(value)))refuse();
  const result:Record<string,unknown>=Object.create(null);
  for(const key of Object.getOwnPropertyNames(value).sort()){
   const d=Object.getOwnPropertyDescriptor(value,key);if(!d?.enumerable||!Object.hasOwn(d,"value"))refuse();result[key]=visit(d.value,depth+1);
  }return result;
 };
 return JSON.stringify(visit(value,0));
}
export class V1ReadProtection {
 readonly #dnr:DNR;readonly #id:string;
 constructor(options:{dnr?:DNR;extensionId?:string}={}) {
  const id=options.extensionId??(typeof chrome==="undefined"?undefined:chrome.runtime?.id);
  const dnr=options.dnr??(typeof chrome==="undefined"?undefined:chrome.declarativeNetRequest);
  if(!id||!dnr)refuse();v1ReadProtectionRules(id);
  if(["getDynamicRules","getSessionRules","getEnabledRulesets","updateDynamicRules"].some(key=>typeof dnr[key as keyof DNR]!=="function"))refuse();
  this.#id=id;this.#dnr=dnr;
 }
 private async state(requireInstalled:boolean):Promise<void> {
  try {
   const [dynamic,session,enabled]=await Promise.all([this.#dnr.getDynamicRules(),this.#dnr.getSessionRules(),this.#dnr.getEnabledRulesets()]);
   if(!Array.isArray(dynamic)||dynamic.length>5||!Array.isArray(session)||session.length||!Array.isArray(enabled)||enabled.length)refuse();
   const wanted=v1ReadProtectionRules(this.#id),seen=new Set<number>();
   for(const rule of dynamic) {
    if(!rule||seen.has(rule.id))refuse();seen.add(rule.id);
    if(IDS.includes(rule.id)) {if(requireInstalled&&canonical(rule)!==canonical(wanted.find(row=>row.id===rule.id)))refuse();}
    else if(rule.id!==AUTH0_ORIGIN_STRIP_RULE_ID||canonical(rule)!==canonical(auth0OriginStripRule(this.#id)))refuse();
   }
   if(requireInstalled&&IDS.some(id=>!seen.has(id)))refuse();
  }catch{return refuse();}
 }
 /** Explicit setup only. Preserve the known Auth0 rule; never clear unknown
  * rules/session/static policy to make a read appear protected. */
 async install():Promise<void> {
  await this.state(false);
  try{await this.#dnr.updateDynamicRules({removeRuleIds:[...IDS],addRules:v1ReadProtectionRules(this.#id)});}catch{return refuse();}
  await this.assert();
 }
 /** Read-only assertion; V1Http includes this await in its request deadline. */
 async assert():Promise<void>{await this.state(true);}
}
