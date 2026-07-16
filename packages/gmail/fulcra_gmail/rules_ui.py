RULES_UI_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Gmail rule builder</title>
<style>
 body{font:14px system-ui;margin:0;padding:24px;max-width:900px}
 h1{font-size:20px} .row{display:flex;gap:8px;align-items:center}
 .msg{border:1px solid #ddd;border-radius:6px;padding:8px;margin:4px 0}
 .msg .sub{font-weight:600} .msg .frm{color:#555;font-size:12px}
 .chip{display:inline-block;border:1px solid #999;border-radius:14px;padding:4px 10px;margin:3px;cursor:pointer}
 .chip.on{background:#5b2be0;color:#fff;border-color:#5b2be0}
 .pill{padding:2px 6px;border-radius:4px;font-size:12px}
 .good{background:#e6ffed} .bad{background:#ffe6e6}
 button{padding:6px 12px;border-radius:6px;border:1px solid #5b2be0;background:#5b2be0;color:#fff;cursor:pointer}
 button.sec{background:#fff;color:#5b2be0}
 input,select,textarea{padding:6px;border:1px solid #ccc;border-radius:6px}
 .m{margin:12px 0}
</style></head>
<body id="gmail-rule-builder">
<h1>Gmail rule builder</h1>
<div class="row m"><label>Account</label><select id="acct"></select></div>
<div class="row m"><input id="q" placeholder="Gmail search e.g. from:amazon receipt" style="flex:1">
 <button onclick="search()">Search</button></div>
<div id="results" class="m"></div>
<div class="row m"><button class="sec" onclick="derive()">Derive rule from ✓/✗</button>
 <button class="sec" onclick="aiSuggest()">Suggest with AI (opt-in)</button></div>
<div id="chips" class="m"></div>
<div id="preview" class="m"></div>
<div class="m"><label>Actions</label>
 <label><input type="checkbox" id="a_file" checked> file</label>
 <label><input type="checkbox" id="a_relay"> relay</label>
 <input id="relay_to" placeholder="relay to (agent)">
 <input id="relay_priority" placeholder="P2" size="3"></div>
<div class="row m"><input id="name" placeholder="rule name"><input id="rid" placeholder="rule id">
 <button onclick="save()">Save rule</button></div>
<div id="status" class="m" style="display:none;font-weight:600"></div>
<hr><h1>Rules</h1><div id="rules"></div>
<script>
function getToken(){return (document.cookie.match(/(?:^|;\s*)fulcra_token=([^;]+)/) || [])[1]
  || localStorage.getItem('fulcra-web-token') || '';}
async function ensureToken(){
  // The daemon sets the fulcra_token cookie on GET / (the dashboard root).
  // If this page is opened directly in a browser that never loaded the
  // dashboard, bootstrap the cookie with one same-origin fetch of /.
  if(getToken())return;
  try{await fetch('/', {credentials:'same-origin', cache:'no-store'});}catch(e){}
}
let RESULTS=[], LABEL={}, CHIPS=[], EDITING=null, EDIT_RULE=null;
async function api(path, body, method){const m=method||(body?'POST':'GET');
  const H={'Content-Type':'application/json','Authorization':'Bearer '+getToken()};
  const r=await fetch(path,{method:m,headers:H,
  body:body?JSON.stringify(body):undefined});if(!r.ok)throw new Error((await r.json()).detail||r.status);return r.json();}
function acct(){return document.getElementById('acct').value;}
async function loadAccounts(){try{const d=await api('/api/gmail/rules/accounts');
  document.getElementById('acct').innerHTML=(d.accounts||[]).map(a=>
   `<option value="${esc(a.account_id)}">${esc(a.email)} (${esc(a.status)})</option>`).join('');
  }catch(e){document.getElementById('acct').innerHTML='';}}
async function search(){const q=document.getElementById('q').value;
  const box=document.getElementById('results');
  box.innerHTML='<em>Searching… (fetching matching messages)</em>';
  try{
    const d=await api('/api/gmail/rules/search',{account_id:acct(),q});
    RESULTS=d.messages;render();
    if(!RESULTS.length)box.innerHTML='<em>No messages matched that search.</em>';
  }catch(e){box.innerHTML='<em>Search failed: '+esc(e.message)+'</em>';}}
function render(){document.getElementById('results').innerHTML=RESULTS.map(m=>`
  <div class="msg"><div class="sub">${esc(m.subject)}</div><div class="frm">${esc(m.from)} · ${esc(m.date)}</div>
  <div class="row"><button class="sec" onclick="mark('${m.message_id}','pos')">✓ match</button>
  <button class="sec" onclick="mark('${m.message_id}','neg')">✗ not</button>
  <span id="lbl-${m.message_id}"></span></div></div>`).join('');}
function mark(id,v){LABEL[id]=LABEL[id]===v?undefined:v;
  document.getElementById('lbl-'+id).textContent=LABEL[id]==='pos'?'✓':LABEL[id]==='neg'?'✗':'';}
function ids(v){return Object.keys(LABEL).filter(k=>LABEL[k]===v);}
async function derive(){EDITING=null;EDIT_RULE=null;
  const d=await api('/api/gmail/rules/derive',
  {account_id:acct(),positives:ids('pos'),negatives:ids('neg')});CHIPS=d.chips;drawChips();preview();}
function drawChips(){document.getElementById('chips').innerHTML=CHIPS.map((c,i)=>
  `<span class="chip ${c.on?'on':''}" onclick="toggle(${i})">${esc(c.label)}</span>`).join('');}
function toggle(i){CHIPS[i].on=!CHIPS[i].on;drawChips();preview();}
function draft(){const m=CHIPS.filter(c=>c.on&&c.field==='match').map(c=>c.value).join(' ');
  const r={id:val('rid')||'rule',version:1,name:val('name')||'rule',match:m,actions:actions()};
  CHIPS.filter(c=>c.on&&c.field==='subject_regex').forEach(c=>r.subject_regex=c.value);
  CHIPS.filter(c=>c.on&&c.field==='from_regex').forEach(c=>r.from_regex=c.value);
  CHIPS.filter(c=>c.on&&c.field==='has_attachment').forEach(()=>r.has_attachment=true);
  if(val('relay_to'))r.relay_to=val('relay_to'); if(val('relay_priority'))r.relay_priority=val('relay_priority');
  return r;}
// The fields the builder FORM actually represents. On edit-save these are
// overwritten from the form (or removed if the form omits them); every OTHER
// field on the existing rule (accounts, backfill, enabled, …) round-trips
// untouched so editing a rule's name can't silently re-enable or broaden it.
const FORM_FIELDS=['match','actions','name','relay_to','relay_priority',
                   'subject_regex','has_attachment','from_regex'];
function editBody(){const d=draft();
  const base=Object.assign({}, EDIT_RULE||{});
  FORM_FIELDS.forEach(f=>{ if(d[f]!==undefined){base[f]=d[f];} else {delete base[f];} });
  base.id=EDITING;
  if(EDIT_RULE&&EDIT_RULE.version)base.version=EDIT_RULE.version;
  return base;}
function actions(){const a=[];if(document.getElementById('a_file').checked)a.push('file');
  if(document.getElementById('a_relay').checked)a.push('relay');return a;}
async function preview(){try{const d=await api('/api/gmail/rules/preview',
  {account_id:acct(),rule:draft(),positives:ids('pos'),negatives:ids('neg')});
  document.getElementById('preview').innerHTML=`Matches: <b>${d.match_count}</b> ·
   <span class="pill good">✓ caught ${d.positives_caught.length}/${ids('pos').length}</span>
   <span class="pill ${d.negatives_caught.length?'bad':'good'}">✗ leaked ${d.negatives_caught.length}</span>`;
  }catch(e){document.getElementById('preview').textContent='Preview: '+e.message;}}
async function aiSuggest(){if(!confirm('This sends the from/subject/snippet of your labeled examples to Claude. Continue?'))return;
  EDITING=null;EDIT_RULE=null;
  const d=await api('/api/gmail/rules/ai-suggest',{account_id:acct(),positives:ids('pos'),negatives:ids('neg'),consent:true});
  const r=d.draft_rule;document.getElementById('chips').innerHTML=
   `<div>AI: ${esc(d.explanation)}</div><pre>${esc(JSON.stringify(r,null,1))}</pre>`;
  if(r.match){CHIPS=[{kind:'ai',field:'match',value:r.match,label:'q='+r.match,on:true}];
   if(r.subject_regex)CHIPS.push({kind:'ai',field:'subject_regex',value:r.subject_regex,label:'subject~'+r.subject_regex,on:true});
   drawChips();preview();}}
async function save(){
  try{
    if(EDITING){await api('/api/gmail/rules/'+encodeURIComponent(EDITING),editBody(),'PUT');}
    else{await api('/api/gmail/rules',draft());}
    EDITING=null;EDIT_RULE=null;await loadRules();setStatus('Rule saved.');}
  catch(e){setStatus('Save failed: '+e.message);}}
function setStatus(msg){const el=document.getElementById('status');
  el.textContent=msg;el.style.display='block';
  clearTimeout(setStatus._t);setStatus._t=setTimeout(()=>{el.style.display='none';},6000);}
async function editRule(id){const r=await api('/api/gmail/rules/'+encodeURIComponent(id));
  EDITING=id;EDIT_RULE=r;
  document.getElementById('rid').value=r.id; document.getElementById('name').value=r.name;
  document.getElementById('q').value=r.match||'';
  document.getElementById('a_file').checked=(r.actions||[]).includes('file');
  document.getElementById('a_relay').checked=(r.actions||[]).includes('relay');
  document.getElementById('relay_to').value=r.relay_to||'';
  document.getElementById('relay_priority').value=r.relay_priority||'';
  CHIPS=[{kind:'edit',field:'match',value:r.match||'',label:'q='+(r.match||''),on:true}];
  if(r.subject_regex)CHIPS.push({kind:'edit',field:'subject_regex',value:r.subject_regex,label:'subject~'+r.subject_regex,on:true});
  if(r.from_regex)CHIPS.push({kind:'edit',field:'from_regex',value:r.from_regex,label:'from~'+r.from_regex,on:true});
  if(r.has_attachment)CHIPS.push({kind:'edit',field:'has_attachment',value:'has:attachment',label:'has attachment',on:true});
  drawChips();preview();}
async function loadRules(){const d=await api('/api/gmail/rules');
  document.getElementById('rules').innerHTML=d.rules.map(r=>`<div class="msg">
   <b>${esc(r.name)}</b> <span class="frm">${esc(r.summary)}</span>
   <button class="sec" onclick="editRule('${r.id}')">edit</button>
   <button class="sec" onclick="toggleRule('${r.id}',${!r.enabled})">${r.enabled?'disable':'enable'}</button>
   <button class="sec" onclick="delRule('${r.id}')">delete</button></div>`).join('');}
async function toggleRule(id,en){await api('/api/gmail/rules/'+id+'/enabled',{enabled:en});loadRules();}
async function delRule(id){if(confirm('Delete '+id+'?')){await api('/api/gmail/rules/'+encodeURIComponent(id),null,'DELETE');loadRules();}}
function val(id){return document.getElementById(id).value.trim();}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
(async()=>{await ensureToken();loadAccounts();loadRules();})();
</script></body></html>"""
