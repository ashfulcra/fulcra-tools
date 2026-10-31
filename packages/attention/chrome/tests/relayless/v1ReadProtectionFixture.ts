import {V1ReadProtection,v1ReadProtectionRules} from "../../src/relayless/v1ReadProtection";
export const EXTENSION_ID="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
export function readProtectionFixture(installed=true){
 const state={rules:installed?v1ReadProtectionRules(EXTENSION_ID):[],session:[] as unknown[],enabled:[] as string[],writes:0,dropWrites:false};
 const dnr={getDynamicRules:async()=>structuredClone(state.rules),getSessionRules:async()=>structuredClone(state.session),getEnabledRulesets:async()=>[...state.enabled],
  updateDynamicRules:async(options:{removeRuleIds?:number[];addRules?:unknown[]})=>{state.writes++;if(!state.dropWrites)state.rules=[...state.rules.filter(rule=>!options.removeRuleIds?.includes(rule.id)),...structuredClone(options.addRules??[]) as typeof state.rules];}};
 return {state,dnr,protection:new V1ReadProtection({dnr,extensionId:EXTENSION_ID})};
}
