// Lossless RFC3339 instants shared by Python-compatible control records and
// submitted-field comparison. Attention capture adds a millisecond-only fence.
const MIN=-62135596800000000n,MAX=253402300799999999n;
function refuse():never{throw new Error("Collect v1 timestamp is invalid or loses precision");}
export function parseV1Instant(value:string):bigint {
 if(typeof value!=="string"||value.length>128)refuse();
 const match=/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
 if(!match||match[0]!==value)refuse();
 const [year,month,day,hour,minute,second]=match.slice(1,7).map(Number);
 const fraction=(match[7]??"").replace(/0+$/,"");
 if(fraction.length>6||year<1||month<1||month>12||day<1||hour>23||minute>59||second>59)refuse();
 const date=new Date(0);date.setUTCFullYear(year,month-1,day);date.setUTCHours(hour,minute,second,0);
 if(date.getUTCFullYear()!==year||date.getUTCMonth()!==month-1||date.getUTCDate()!==day)refuse();
 const zone=match[8],offsetHour=zone==="Z"?0:Number(zone.slice(1,3)),offsetMinute=zone==="Z"?0:Number(zone.slice(4,6));
 if(offsetHour>23||offsetMinute>59)refuse();
 const offset=(offsetHour*60+offsetMinute)*(zone.startsWith("-")?-1:1);
 const result=BigInt(date.getTime())*1000n+BigInt(fraction.padEnd(6,"0"))-BigInt(offset)*60000000n;
 if(result<MIN||result>MAX)refuse();return result;
}
/** Python datetime.isoformat UTC form: six fractional digits iff nonzero. */
export function formatV1Instant(value:bigint):string {
 if(typeof value!=="bigint"||value<MIN||value>MAX)refuse();
 let seconds=value/1000000n,fraction=value%1000000n;
 if(fraction<0){seconds--;fraction+=1000000n;}
 const base=new Date(Number(seconds)*1000).toISOString().slice(0,19);
 return base+(fraction?"."+String(fraction).padStart(6,"0"):"")+"Z";
}
