// @vitest-environment node
import {describe,expect,test} from "vitest";
import {V1ReadProtection,v1ReadProtectionRules} from "../../src/relayless/v1ReadProtection";
import {auth0OriginStripRule} from "../../src/relayless/authOriginFix";
import {EXTENSION_ID,readProtectionFixture} from "./v1ReadProtectionFixture";
describe("browser v1 JSONL network encoding protection",()=>{
 test("fixed rules affect only own production exact record GETs",()=>{
  const rules=v1ReadProtectionRules(EXTENSION_ID);expect(rules.map(rule=>rule.id)).toEqual([1002,1003,1004,1005]);
  for(const rule of rules.slice(0,2)){expect(rule.condition.initiatorDomains).toEqual([EXTENSION_ID]);expect(rule.condition.requestMethods).toEqual(["get"]);
   const expression=new RegExp(rule.condition.regexFilter!);expect(expression.test("https://api.fulcradynamics.com/data/v1/records?q=synthetic")).toBe(true);
   for(const url of ["http://api.fulcradynamics.com/data/v1/records?q=x","https://api.fulcradynamics.com/data/v1/records-other?q=x","https://api.fulcradynamics.com/data/v1/records","https://example.com/data/v1/records?q=x","https://api.fulcradynamics.com/user/v1alpha1/info"])expect(expression.test(url)).toBe(false);
  }
  expect(rules[0].action.requestHeaders).toEqual([{header:"accept-encoding",operation:"set",value:"identity"}]);expect(rules[1].action.type).toBe("block");
  expect(rules[1].condition.responseHeaders).toEqual([{header:"content-encoding",excludedValues:["identity"]}]);
 });
 test("explicit install preserves the exact Auth0 rule and verifies actual readback",async()=>{
  const s=readProtectionFixture(false);s.state.rules=[auth0OriginStripRule(EXTENSION_ID)];await s.protection.install();expect(s.state.writes).toBe(1);expect(s.state.rules).toHaveLength(5);await s.protection.assert();expect(s.state.writes).toBe(1);
 });
 test("read assertion never installs missing rules",async()=>{
  const s=readProtectionFixture(false);await expect(s.protection.assert()).rejects.toThrow();expect(s.state.writes).toBe(0);
 });
 test("a claimed registration without actual rules refuses",async()=>{
  const s=readProtectionFixture(false);s.state.dropWrites=true;await expect(s.protection.install()).rejects.toThrow();expect(s.state.rules).toEqual([]);
 });
 test.each(["missing","altered","allow","duplicate","unknown","session","static","auth0"])("unverifiable or overriding %s state refuses",async kind=>{
  const s=readProtectionFixture();if(kind==="missing")s.state.rules.pop();
  if(kind==="altered")s.state.rules[1].condition.responseHeaders![0].excludedValues=["*"];
  if(kind==="allow")s.state.rules[1].action.type="allow" as chrome.declarativeNetRequest.RuleActionType;
  if(kind==="duplicate")s.state.rules.push(structuredClone(s.state.rules[0]));
  if(kind==="unknown")s.state.rules.push({id:9999,action:{type:"allow"},condition:{}} as chrome.declarativeNetRequest.Rule);
  if(kind==="session")s.state.session=[{id:9999,action:{type:"allow"}}];if(kind==="static")s.state.enabled=["unknown"];
  if(kind==="auth0"){const rule=auth0OriginStripRule(EXTENSION_ID);rule.condition.requestDomains=["api.fulcradynamics.com"];s.state.rules.push(rule);}
  await expect(s.protection.assert()).rejects.toThrow();expect(s.state.writes).toBe(0);
 });
 test("unknown existing rules are retained rather than silently replaced during setup",async()=>{
  const s=readProtectionFixture(false);s.state.rules=[{id:9999,action:{type:"allow"},condition:{}} as chrome.declarativeNetRequest.Rule];
  await expect(s.protection.install()).rejects.toThrow();expect(s.state.writes).toBe(0);expect(s.state.rules[0].id).toBe(9999);
 });
 test("rule property order and set order do not manufacture a mismatch",async()=>{
  const s=readProtectionFixture();s.state.rules.reverse();for(const rule of s.state.rules)rule.condition.resourceTypes!.reverse();await s.protection.assert();
 });
 test("extension identity and API handle are captured before an asynchronous read",async()=>{
  const s=readProtectionFixture(),options={dnr:s.dnr,extensionId:EXTENSION_ID};const protection=new V1ReadProtection(options);options.extensionId="b".repeat(32);await protection.assert();
 });
 test("invalid extension identities cannot create protection",()=>{for(const id of ["", "wrong", "z".repeat(32)])expect(()=>v1ReadProtectionRules(id)).toThrow();});
});

test("typed POST encoding rules cover a protective suffix superset, never authority",()=>{
 const rules=v1ReadProtectionRules(EXTENSION_ID).slice(2);expect(rules).toHaveLength(2);
 for(const rule of rules){expect(rule.condition.initiatorDomains).toEqual([EXTENSION_ID]);expect(rule.condition.requestMethods).toEqual(["post"]);const re=new RegExp(rule.condition.regexFilter!);
 for(const path of ["Event/11111111-2222-4333-8444-555555555555","DeletedRecord","Event/abc","Event/---"])expect(re.test("https://api.fulcradynamics.com/ingest/v1/record/"+path+"?api_version=v1")).toBe(true);
 for(const path of ["Event","Event/UPPER","Metric/11111111-2222-4333-8444-555555555555","DeletedRecord/","DeletedRecord?api_version=v1&other=1","DeletedRecord?api_version=v1"])expect(re.test("https://api.fulcradynamics.com/ingest/v1/record/"+path+"?api_version=v1")).toBe(false);
 }
 expect(rules[0].action.requestHeaders).toEqual([{header:"accept-encoding",operation:"set",value:"identity"}]);expect(rules[1].condition.responseHeaders).toEqual([{header:"content-encoding",excludedValues:["identity"]}]);
});
