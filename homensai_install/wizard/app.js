"use strict";
const $=id=>document.getElementById(id);
let token=new URLSearchParams(location.hash.slice(1)).get("token")||sessionStorage.getItem("hinstall-token");
if(token){sessionStorage.setItem("hinstall-token",token);history.replaceState(null,"",location.pathname);}
let locale="en",messages={},session,step=0,plan=null,terms=null,opid=sessionStorage.getItem("hinstall-operation"),cursor=0,polling=false;
let capabilitiesLoaded=false,languageEpoch=0,languagePending=false;
const capabilitiesByMode={};
const phaseLabels={verify:"verify",prepare:"prepare",stage:"stage",migrate:"migrate",activate:"activate",start:"start",stop:"stop",health:"health",backup:"backup",restore:"restore",repair:"repair",uninstall:"uninstall",verify_archive:"verify_archive",before_restore:"before_restore",before_rollback:"before_rollback",migration_backups:"migration_backups",preserve_recovery:"preserve_recovery",remove_owned_code:"remove_owned_code"};
function phaseKey(phase){if(phase.startsWith("backup:"))return "migration_backups";if(phase.startsWith("migrate:"))return "migrate";return {archive:"backup","before-rollback":"before_rollback",switch:"activate",pointer_back:"activate",rollback_stop:"stop",rollback_start:"start",rollback_health:"health"}[phase]||phaseLabels[phase]||phase;}
const tr=k=>messages[k]||messages.processing||"…";
async function api(path,body){const response=await fetch("/api/v1"+path,{method:body?"POST":"GET",headers:{"X-HInstall-Token":token||"","Content-Type":"application/json"},body:body?JSON.stringify(body):undefined,credentials:"omit",cache:"no-store"});const data=await response.json();if(!response.ok||data.error)throw data.error||{code:"UNEXPECTED_ERROR"};return data.result;}
async function showError(error){let explanation={};try{explanation=await api("/errors/"+encodeURIComponent(error.code||"UNEXPECTED_ERROR")+"?locale="+locale);}catch{}$("error").hidden=false;$("error-code").textContent=error.code||"UNEXPECTED_ERROR";$("error-title").textContent=explanation.title||error.title||tr("error_title");$("error-description").textContent=explanation.description||error.description||error.message||tr("error_help");$("error-remediation").replaceChildren();for(const text of explanation.remediation||error.remediation||[]){const item=document.createElement("li");item.textContent=typeof text==="string"?text:JSON.stringify(text);$("error-remediation").append(item);}$("error").scrollIntoView({block:"nearest"});}
async function guarded(fn){try{$("error").hidden=true;await fn();}catch(error){await showError(error);}}
function panel(n){step=n;document.querySelectorAll("[data-panel]").forEach(e=>e.hidden=Number(e.dataset.panel)!==n);document.querySelectorAll("[data-step]").forEach(e=>e.classList.toggle("active",Number(e.dataset.step)===n));$("back").hidden=n===0||n>=3;$("next").hidden=n>=3;$("next").textContent=tr(n===2?"execute":"next");updateCapabilities();}
function updateCapabilities(){
  if(!capabilitiesLoaded)return;
  const preparing=prereq.initialized&&prerequisiteBlocksProduct();
  for(const option of $("mode").options){
    option.disabled=!Object.values(capabilitiesByMode[option.value]?.actions||{}).some(value=>value?.supported===true)&&
      !(prereq.initialized&&prereq.capabilitiesByMode[option.value]?.configured);
  }
  const actions=capabilitiesByMode[$("mode").value]?.actions||{};
  for(const option of $("action").options)option.disabled=actions[option.value]?.supported!==true;
  const supported=actions[$("action").value]?.supported===true;
  $("next").disabled=!supported||preparing||languagePending;
  $("discover").disabled=(!Object.values(actions).some(value=>value?.supported===true)&&!prereq.caps?.configured)||preparing||languagePending;
  $("capability-status").hidden=supported&&!preparing&&!languagePending;
  $("capability-status").textContent=tr(preparing?"prereq_product_wait":"action_unavailable");
  $("mode").disabled=prereq.initialized&&(prerequisiteRequestBusy()||prereq.discoveryUnknown||
    !!prereq.operationId&&!prerequisiteSettled()||prereq.operations.length>0||!!prereq.nextCursor);
}
function resetReview(){if(step>=3)return;plan=null;terms=null;$("accept-terms").checked=false;$("accept-plan").checked=false;panel(0);}
function setText(){document.documentElement.lang=locale;document.querySelectorAll("[data-i18n]").forEach(e=>e.textContent=tr(e.dataset.i18n));$("language").value=locale;panel(step);}
async function language(value){
  const requested=["ru","en","de"].includes(value)?value:"en",epoch=++languageEpoch;
  const previousTerms=terms,mode=$("mode").value;
  languagePending=true;
  if(step<3){plan=null;$("accept-terms").checked=false;$("accept-plan").checked=false;}
  if(prereq.initialized&&(prereq.review||prereq.reviewPending)){prereq.epoch++;clearPrerequisiteReview();}
  updateCapabilities();
  try{
    const translated=await api("/locales/"+requested);
    if(epoch!==languageEpoch)return;
    locale=requested;messages=translated;setText();localizePrerequisites();
    sessionStorage.setItem("hinstall-locale",locale);
    await api("/preferences",{locale:requested});
    if(epoch!==languageEpoch)return;
    if(step<3&&previousTerms&&terms===previousTerms&&$("mode").value===mode){
      const translatedTerms=await api("/terms/"+requested+"?mode="+encodeURIComponent(mode));
      if(epoch!==languageEpoch||terms!==previousTerms||$("mode").value!==mode)return;
      terms=translatedTerms;$("terms").textContent=terms.text;$("terms").scrollTop=0;
      $("terms-digest").textContent="SHA-256: "+terms.digest;$("accept-terms").disabled=true;
      if(step===2)panel(1);enableTerms();
    }
  }finally{if(epoch===languageEpoch){languagePending=false;updateCapabilities();updatePrerequisiteControls();}}
}
function enableTerms(){const e=$("terms");if(e.scrollTop+e.clientHeight>=e.scrollHeight-8)$("accept-terms").disabled=false;}
async function loadTerms(){
  const selectedLocale=locale,mode=$("mode").value;
  const result=await api("/terms/"+selectedLocale+"?mode="+encodeURIComponent(mode));
  if(locale!==selectedLocale||$("mode").value!==mode||languagePending)throw{code:"PLAN_STALE"};
  terms=result;$("terms").textContent=terms.text;$("terms-digest").textContent="SHA-256: "+terms.digest;
  $("accept-terms").checked=false;$("accept-terms").disabled=true;panel(1);requestAnimationFrame(enableTerms);
}
function inputs(){return{locale,mode:$("mode").value,install_root:$("install-root").value,action:$("action").value,archive_path:$("archive-path").value,acknowledge_data_loss:$("data-loss").checked,product_modes:[...document.querySelectorAll("#mode-choices input:checked")].map(x=>x.value)};}
async function discover(){const inventory=await api("/discover",inputs());$("inventory").textContent=JSON.stringify(inventory,null,2);$("environment-status").textContent=tr("checked");const env=inventory.environment_inventory;$("environment-summary").replaceChildren();if(env){const lines=[env.platform.os+" / "+env.platform.arch,env.identity.elevated?tr("elevated_context"):tr("ordinary_context"),inventory.installation?.installed?tr("current_installation"):tr("fresh_installation")];if(env.resources.physical_memory_bytes)lines.push(tr("available_memory")+": "+new Intl.NumberFormat(locale,{maximumFractionDigits:1}).format(env.resources.physical_memory_bytes/1073741824)+" GiB");for(const disk of env.disks||[])if(Number.isFinite(disk.free_bytes))lines.push(tr("available_disk")+": "+new Intl.NumberFormat(locale,{maximumFractionDigits:1}).format(disk.free_bytes/1073741824)+" GiB");for(const text of lines){const line=document.createElement("p");line.textContent=text;$("environment-summary").append(line);}}return inventory;}
function stepsOf(p){return p.engine_plan?.steps||p.steps||[];}
async function storeSecrets(){const body={};const password=$("archive-password").value;if(password)body.archive_password=password;const values={};for(const input of document.querySelectorAll("#product-secrets input")){if(input.value)values[input.dataset.secretName]=input.value;}if(Object.keys(values).length)body.product_secrets=values;if(Object.keys(body).length){await api("/secrets",body);$("archive-password").value="";document.querySelectorAll("#product-secrets input").forEach(input=>input.value="");}}
async function makePlan(){if(prerequisiteBlocksProduct()||languagePending)throw{code:"OPERATION_CONFLICT"};if(!$("accept-terms").checked)throw{code:"CONSENT_REQUIRED",description:tr("accept_terms")};await storeSecrets();plan=await api("/plans",inputs());$("plan-details").textContent=JSON.stringify(plan,null,2);const effective=plan.engine_plan?.effective_active?.release;$("effective-release").hidden=!effective;$("effective-release").textContent=effective?tr("active_release")+": "+effective:"";$("plan-steps").replaceChildren();for(const item of stepsOf(plan)){const li=document.createElement("li");li.textContent=tr(phaseKey(item.id));$("plan-steps").append(li);}$("accept-plan").checked=false;panel(2);}
async function execute(){if(prerequisiteBlocksProduct()||languagePending)throw{code:"OPERATION_CONFLICT"};if(!$("accept-plan").checked||!$("accept-terms").checked)throw{code:"CONSENT_REQUIRED"};const password=$("archive-password").value;if(password){await api("/secrets",{archive_password:password});$("archive-password").value="";}const key=crypto.randomUUID();const refs=[...new Set([plan.review_consent_ref,...(plan.required_consents||[]),...(plan.engine_plan?.required_consents||[])].filter(Boolean))];const op=await api("/operations",{plan_id:plan.plan_id,plan_digest:plan.plan_digest||plan.digest,consent_refs:refs,idempotency_key:key,accepted_terms:true,accepted_plan:true,terms_digest:terms.digest,locale});opid=op.operation_id||op.id;sessionStorage.setItem("hinstall-operation",opid);cursor=0;completed.clear();$("components").replaceChildren();$("events").textContent="";panel(3);poll();}
const completed=new Set();
function eventView(e){const phase=e.phase||e.step_id||"operation";const status=e.status||"RUNNING";const key=(e.component_id||"product")+":"+phase;let row=[...$("components").children].find(x=>x.dataset.key===key);if(!row){row=document.createElement("div");row.className="component";row.dataset.key=key;const label=document.createElement("strong");label.textContent=tr(phaseKey(phase));const bar=document.createElement("progress");bar.max=100;const info=document.createElement("span");row.append(label,bar,info);$("components").append(row);}const bar=row.querySelector("progress");if(status==="SUCCEEDED"){bar.value=100;completed.add(phaseKey(phase));}else if(Number.isFinite(e.total)&&e.total>0&&Number.isFinite(e.completed)){bar.value=Math.min(99,100*e.completed/e.total);}else bar.removeAttribute("value");row.querySelector("span").textContent=tr("status_"+status.toLowerCase());$("progress-status").textContent=tr(phaseKey(phase));const steps=plan?stepsOf(plan):[];if(steps.length){const totalWeight=steps.reduce((n,s)=>n+(s.weight||1),0);const doneWeight=steps.reduce((n,s)=>n+(completed.has(s.id)?(s.weight||1):0),0);const percent=Math.min(99,Math.floor(100*doneWeight/totalWeight));$("overall").value=percent;$("overall-label").textContent=percent+"%";}else{$("overall").removeAttribute("value");$("overall-label").textContent=tr("processing");}$("events").textContent=($("events").textContent+JSON.stringify(e)+"\n").slice(-16000);}
async function poll(){if(polling||!opid)return;polling=true;try{while(step===3){const stream=await api("/operations/"+encodeURIComponent(opid)+"/events?after_sequence="+cursor);for(const e of stream.events||[]){eventView(e);cursor=Math.max(cursor,e.sequence||0);}const status=stream.snapshot||await api("/operations/"+encodeURIComponent(opid));const state=status.status||status.state;if(["SUCCEEDED","FAILED","FAILED_ROLLED_BACK","CANCELED","RECOVERY_REQUIRED","BLOCKED","AWAITING_USER","REBOOT_REQUIRED","RELOGIN_REQUIRED"].includes(state)){finish(status,state);break;}await new Promise(r=>setTimeout(r,700));}}catch(error){await showError(error);$("progress-status").textContent=tr("connection_lost");}finally{polling=false;}}
function finish(status,state){panel(4);const success=state==="SUCCEEDED";$("result-title").textContent=tr(success?"success":"attention");$("result-description").textContent=tr("status_"+state.toLowerCase());$("result-details").textContent=JSON.stringify(status,null,2);$("overall").value=success?100:Number($("overall").value)||0;$("overall-label").textContent=success?"100%":$("overall-label").textContent;$("resume").hidden=!["RECOVERY_REQUIRED","AWAITING_USER","REBOOT_REQUIRED","RELOGIN_REQUIRED","CANCELED","FAILED"].includes(state);$("result-progress").replaceChildren();const finalLabel=document.createElement("p");finalLabel.textContent=tr("progress_title")+": "+(success?"100%":tr("attention"));const finalBar=$("overall").cloneNode(true);finalBar.removeAttribute("id");const components=$("components").cloneNode(true);components.removeAttribute("id");$("result-progress").append(finalLabel,finalBar,components);$("resume-password-row").hidden=$("resume").hidden;$("cancel-paused").hidden=!["RECOVERY_REQUIRED","AWAITING_USER","REBOOT_REQUIRED","RELOGIN_REQUIRED"].includes(state);$("cancel-paused-help").hidden=$("cancel-paused").hidden;const result=status.result||{};if(result.archive?.path)$("archive-path").value=result.archive.path;const raw=result.health?.url||result.url;$("open-app").hidden=true;if(success&&raw){try{const u=new URL(raw);if(u.protocol==="http:"&&["127.0.0.1","localhost","[::1]"].includes(u.hostname)){u.pathname="/";u.search="";u.hash="";$("open-app").href=u.href;$("open-app").hidden=false;}}catch{}}}
$("mode").addEventListener("change",()=>{changePrerequisiteMode();resetReview();});
$("action").addEventListener("change",()=>{$("data-loss-row").hidden=!["restore","rollback"].includes($("action").value);$("data-loss").checked=false;resetReview();});
$("language").addEventListener("change",()=>guarded(()=>language($("language").value)));
$("terms").addEventListener("scroll",enableTerms);
$("discover").addEventListener("click",()=>guarded(discover));
$("dismiss").addEventListener("click",()=>{$("error").hidden=true;});
$("back").addEventListener("click",()=>panel(Math.max(0,step-1)));
$("next").addEventListener("click",()=>guarded(async()=>{if(prerequisiteBlocksProduct()||languagePending)throw{code:"OPERATION_CONFLICT"};if(step===0){await discover();await loadTerms();}else if(step===1)await makePlan();else if(step===2)await execute();}));
$("cancel").addEventListener("click",()=>guarded(()=>api("/operations/"+encodeURIComponent(opid)+"/cancel",{locale})));
$("resume").addEventListener("click",()=>guarded(async()=>{if(prerequisiteBlocksProduct())throw{code:"OPERATION_CONFLICT"};const password=$("resume-password").value;if(password){await api("/secrets",{archive_password:password});$("resume-password").value="";}await api("/operations/"+encodeURIComponent(opid)+"/resume",{locale,decisions:{}});panel(3);poll();}));
$("maintenance").addEventListener("click",()=>{sessionStorage.removeItem("hinstall-operation");opid=null;completed.clear();plan=null;panel(0);});
guarded(async()=>{session=await api("/session");$("product").textContent=session.product_id+" · "+session.target.os+" / "+session.target.arch;$("install-root").value=session.options.install_root||"";$("development").hidden=!session.development;for(const mode of session.product_modes||[]){const label=document.createElement("label");label.className="check";const input=document.createElement("input");input.type="checkbox";input.value=mode.name;input.checked=session.selected_modes.includes(mode.name)||session.product_modes.length===1;input.disabled=session.product_modes.length===1;const text=document.createElement("span");text.textContent=mode.name+" ("+mode.components.join(", ")+")";label.append(input,text);$("mode-choices").append(label);}for(const secret of session.secrets||[]){const label=document.createElement("label");label.className="fields";const name=document.createElement("span");name.textContent=secret.name+(secret.required?" *":"");const input=document.createElement("input");input.type="password";input.autocomplete="new-password";input.dataset.secretName=secret.name;label.append(name,input);$("product-secrets").append(label);}let preferred=sessionStorage.getItem("hinstall-locale")||session.locale||navigator.language.split("-")[0];await language(preferred);for(const mode of ["native","docker"]){capabilitiesByMode[mode]=await api("/capabilities?mode="+mode);}capabilitiesLoaded=true;updateCapabilities();await initializePrerequisites();const unfinished=await api("/operations?unfinished_only=true&limit=100");$("pending-list").replaceChildren();$("pending-operations").hidden=!unfinished.operations.length;for(const item of unfinished.operations){const button=document.createElement("button");button.className="secondary";button.textContent=tr("review_operation")+": "+tr(item.action)+" · "+item.updated_at;button.addEventListener("click",()=>{opid=item.operation_id;sessionStorage.setItem("hinstall-operation",opid);cursor=0;completed.clear();panel(3);poll();});$("pending-list").append(button);}if(opid&&!prerequisiteBlocksProduct()){panel(3);poll();}else panel(0);});


let exportPreview=null;
$("search-errors").addEventListener("click",()=>guarded(async()=>{const data=await api("/errors?locale="+locale+"&query="+encodeURIComponent($("error-query").value));$("catalog-results").replaceChildren();for(const item of data.errors){const card=document.createElement("article");const title=document.createElement("h3");title.textContent=item.code+" · "+item.title;const text=document.createElement("p");text.textContent=item.description;const list=document.createElement("ul");for(const remedy of item.remediation||[]){const line=document.createElement("li");line.textContent=remedy;list.append(line);}card.append(title,text,list);$("catalog-results").append(card);}if(!data.errors.length)$("catalog-results").textContent=tr("no_results");}));
$("statistics").addEventListener("click",()=>guarded(async()=>{const data=await api("/statistics");const rate=data.success_rate_all_operations;$("statistics-results").textContent=[tr("operation_count")+": "+data.total_operations,tr("attempt_count")+": "+data.total_attempts,tr("statistics_success")+": "+rate.numerator+" / "+rate.denominator,tr("period")+": "+(data.selection.since||tr("all_time"))+" — "+(data.selection.until||tr("now")),data.data_complete?tr("complete_history"):tr("incomplete_history")].join("\n");}));
$("preview-export").addEventListener("click",()=>guarded(async()=>{exportPreview=await api("/diagnostics/export/preview",{operation_id:opid||null,locale});$("export-preview").textContent=JSON.stringify(exportPreview.report,null,2);$("confirm-export").checked=false;$("export").disabled=true;}));
$("confirm-export").addEventListener("change",()=>{$("export").disabled=!exportPreview||!$("confirm-export").checked;});
$("export").addEventListener("click",()=>guarded(async()=>{if(!exportPreview||!$("confirm-export").checked)throw{code:"CONSENT_REQUIRED"};const result=await api("/diagnostics/export",{operation_id:opid||null,locale,confirmed:true,expected_digest:exportPreview.sha256});const url=URL.createObjectURL(new Blob([result.text],{type:"application/json"}));const link=document.createElement("a");link.href=url;link.download=result.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}));

$("cancel-paused").addEventListener("click",()=>guarded(async()=>{const status=await api("/operations/"+encodeURIComponent(opid)+"/cancel",{locale});if(["QUEUED","RUNNING"].includes(status.status)){panel(3);poll();}else finish(status,status.status);}));

// Подготовка зависимостей использует отдельную операцию общего SDK.
// READY здесь не является SUCCEEDED установки приложения.
const prereq = {
  initialized:false, mode:null, caps:null, capabilitiesByMode:{}, choices:{},
  review:null, displayPlan:null, productTerms:null, licenses:{}, consentInputs:[],
  unsupportedConsent:false, operationId:null, snapshot:null, cursor:0, revision:-1,
  epoch:0, observationEpoch:0, polling:false, pollEpoch:0, startPending:false, startUncertain:false,
  changePending:null, cancelPending:false, reviewPending:false, discoveryUnknown:false,
  operations:[], nextCursor:null, listEpoch:0, listPending:false, replaceOperationId:null, afterReady:false,
  connectionLost:false, eventLines:[], refreshPending:false, proceedPending:false
};
const prereqStorageKey = "hinstall-prerequisite-operation";
const prereqVersionPattern = /^v?[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:\+[A-Za-z0-9_.-]+)?$(?![\s\S])/;
const prereqStates = new Set(["READY","CANCELED","AWAITING_USER","RUNNING","QUEUED","RECOVERY_REQUIRED","REBOOT_REQUIRED","RELOGIN_REQUIRED","FAILED","BLOCKED"]);
function prepText(key, values={}) {
  let text=tr(key);
  for(const [name,value] of Object.entries(values))text=text.split("{"+name+"}").join(String(value));
  return text;
}
function prepNode(tag, text, className) {
  const node=document.createElement(tag);
  if(text!==undefined)node.textContent=text;
  if(className)node.className=className;
  return node;
}
function samePrerequisiteTarget(a,b) {
  return !!a&&!!b&&Object.keys(a).length===4&&Object.keys(b).length===4&&
    ["os","arch","mode","scope"].every(key=>typeof a[key]==="string"&&a[key]===b[key]);
}
function samePrerequisiteBinding(a,b) {
  return !!a&&!!b&&["product_id","owner_id","catalog_digest"].every(key=>typeof a[key]==="string"&&a[key]===b[key])&&samePrerequisiteTarget(a.target,b.target);
}
function prerequisiteControllerFree() {
  return !!prereq.snapshot&&prereq.snapshot.execution?.active===false;
}
function prerequisiteSettled() {
  return prerequisiteControllerFree()&&(prereq.snapshot.status==="CANCELED"||prereq.snapshot.status==="READY"&&prereq.snapshot.ready===true);
}
function prerequisiteRequestBusy() {
  return prereq.startPending||prereq.startUncertain||!!prereq.changePending||prereq.reviewPending||prereq.proceedPending||prereq.cancelPending||prereq.listPending||languagePending;
}
function prerequisiteBlocksProduct() {
  if(!prereq.initialized)return false;
  return prereq.discoveryUnknown||prereq.connectionLost||prerequisiteRequestBusy()||!!prereq.review||prereq.afterReady||
    !!prereq.nextCursor||prereq.operations.some(item=>item.operation_id!==prereq.operationId)||
    (!!prereq.operationId&&!prerequisiteSettled());
}
function invalidateProductForPrerequisites() {
  plan=null;terms=null;
  $("accept-terms").checked=false;$("accept-plan").checked=false;
  if(step<3)panel(0);
  else updateCapabilities();
}
function savePrerequisiteReference() {
  const caps=prereq.capabilitiesByMode[prereq.snapshot?.target?.mode||prereq.mode];
  if(!prereq.operationId||!caps?.configured)return;
  sessionStorage.setItem(prereqStorageKey,JSON.stringify({schema_version:1,operation_id:prereq.operationId,
    product_id:caps.product_id,owner_id:caps.owner_id,target:caps.target,catalog_digest:caps.catalog_digest}));
}
function storedPrerequisiteReference() {
  try {
    const value=JSON.parse(sessionStorage.getItem(prereqStorageKey)||"null");
    if(value?.schema_version===1&&typeof value.operation_id==="string"&&/^[a-z][a-z0-9_-]{0,79}$/.test(value.operation_id)&&
       value.product_id===session.product_id&&samePrerequisiteBinding(value,prereq.capabilitiesByMode[value.target?.mode]))return value;
  }catch{}
  return null;
}
function clearPrerequisiteReview() {
  prereq.review=null;prereq.productTerms=null;prereq.licenses={};prereq.consentInputs=[];
  prereq.unsupportedConsent=false;prereq.replaceOperationId=null;
  $("prereq-accept-plan").checked=false;$("prereq-terms").replaceChildren();
  $("prereq-review-panel").hidden=true;
}
function knownPrerequisiteVersion(value) {
  return typeof value==="string"&&value.length<=128&&prereqVersionPattern.test(value)?value:null;
}
function prerequisiteVersionRange(requirement) {
  if(!requirement)return tr("prereq_version_unavailable");
  const minimum=requirement.minimum_version,maximum=requirement.maximum_version;
  if(minimum!==null&&!knownPrerequisiteVersion(minimum)||maximum!==null&&!knownPrerequisiteVersion(maximum))return tr("prereq_version_unavailable");
  if(minimum===null&&maximum===null)return tr("prereq_version_any");
  if(minimum===maximum)return prepText("prereq_version_exact",{version:minimum});
  if(minimum!==null&&maximum!==null)return prepText("prereq_version_range",{minimum,maximum});
  return prepText(minimum!==null?"prereq_version_min":"prereq_version_max",{version:minimum!==null?minimum:maximum});
}
function prerequisiteRequirement(item) {
  const p=prereq.displayPlan;
  if(!samePrerequisiteBinding(prereq.caps,p)||!Array.isArray(prereq.caps.requirements))return null;
  const matches=prereq.caps.requirements.filter(entry=>entry?.component_id===item.component_id);
  return matches.length===1?matches[0]:null;
}
function prerequisiteName(item) {
  return messages["prereq_kind_"+item.kind]||item.component_id;
}
function appendPrerequisiteManual(parent,manual) {
  const details=prepNode("details");details.append(prepNode("summary",tr("prereq_manual_help")));
  const list=prepNode("ol");
  const lines=manual?.locale===locale&&Array.isArray(manual.steps)?manual.steps:
    [tr("prereq_manual_step1"),tr("prereq_manual_step2"),tr("prereq_manual_step3")];
  for(const line of lines)if(typeof line==="string")list.append(prepNode("li",line));
  details.append(list);
  if(typeof manual?.official_url==="string")try {
    const url=new URL(manual.official_url);
    if(url.protocol==="https:"&&!url.username&&!url.password){const link=prepNode("a",tr("prereq_official_instructions"));link.href=url.href;link.target="_blank";link.rel="noopener noreferrer";details.append(link);}
  }catch{}
  parent.append(details);
}
function renderPrerequisiteComponents() {
  $("prereq-components").replaceChildren();
  for(const item of prereq.displayPlan?.components||[]) {
    const card=prepNode("article",undefined,"prereq-card");card.dataset.componentId=item.component_id;
    card.append(prepNode("h3",prerequisiteName(item)+" · "+tr(item.required?"prereq_required":"prereq_optional")));
    card.append(prepNode("p",tr("prereq_found_version")+": "+(knownPrerequisiteVersion(item.detected_version)||tr("prereq_version_unknown"))));
    const requirement=prerequisiteRequirement(item);
    card.append(prepNode("p",tr("prereq_required_version")+": "+prerequisiteVersionRange(requirement)));
    if(requirement&&messages["prereq_scope_"+requirement.scope])card.append(prepNode("p",tr("prereq_scope_"+requirement.scope)));
    card.append(prepNode("p",messages["prereq_detect_"+item.initial_status]||tr("prereq_unknown")));
    card.append(prepNode("p",messages["prereq_ownership_"+item.ownership]||tr("prereq_unknown")));
    if(item.depends_on?.length)card.append(prepNode("p",prepText("prereq_depends_on",{components:item.depends_on.join(", ")})));
    if(item.elevated)card.append(prepNode("p",tr("prereq_requires_admin")));
    if((prereq.choices[item.component_id]||item.choice)==="auto")for(const artifact of item.artifacts||[]){
      card.append(prepNode("p",prepText("prereq_artifact",{version:knownPrerequisiteVersion(artifact.version)||tr("prereq_version_unknown"),source:artifact.source_host||tr("prereq_unknown")})));
      const details=prepNode("details");details.append(prepNode("summary",tr("technical_details")),prepNode("p","SHA-256: "+artifact.sha256));card.append(details);
    }
    const label=prepNode("label",tr("prereq_choice"));const select=prepNode("select");
    select.id="prereq-choice-"+item.component_id;select.dataset.componentId=item.component_id;
    const requiredBySelected=(prereq.displayPlan.components||[]).some(other=>(prereq.choices[other.component_id]||other.choice)!=="skip"&&(other.depends_on||[]).includes(item.component_id));
    for(const value of ["use","auto","manual","skip"]) {
      const option=prepNode("option",tr("prereq_choice_"+value));option.value=value;
      option.disabled=value==="use"?item.initial_ready!==true:value==="auto"?!(prereq.caps?.automatic_component_ids||[]).includes(item.component_id):value==="skip"?(item.required||requiredBySelected):false;
      select.append(option);
    }
    select.value=prereq.choices[item.component_id]||item.choice;
    select.disabled=prerequisiteRequestBusy()||!!prereq.operationId&&!prerequisiteControllerFree();
    select.addEventListener("change",()=>{
      const allowed=Array.from(select.options).some(option=>option.value===select.value&&!option.disabled);
      if(!allowed||select.disabled){select.value=prereq.choices[item.component_id]||item.choice;return;}
      prereq.choices[item.component_id]=select.value;prereq.epoch++;clearPrerequisiteReview();
      invalidateProductForPrerequisites();renderPrerequisiteComponents();$("prereq-status").textContent=tr("prereq_review_dirty");updatePrerequisiteControls();
    });
    label.append(select);card.append(label);appendPrerequisiteManual(card,item.manual);$("prereq-components").append(card);
  }
}
function prerequisiteConsent(kind,refs,label,componentId) {
  const row=prepNode("label",undefined,"check");const input=prepNode("input");input.type="checkbox";
  input.dataset.consentKind=kind;input.dataset.consentRefs=JSON.stringify(refs);
  if(componentId)input.dataset.componentId=componentId;
  input.addEventListener("change",updatePrerequisiteControls);
  row.append(input,prepNode("span",label));prereq.consentInputs.push({input,refs});return {row,input};
}
function addPrerequisiteTerms(title,text,metadata,refs,kind,componentId) {
  const box=prepNode("section",undefined,"prereq-terms");box.append(prepNode("h3",title));
  box.append(prepNode("p",metadata,"muted"));
  const content=prepNode("div",text,"terms");content.tabIndex=0;content.setAttribute("role","region");content.setAttribute("aria-label",title);
  content.id=kind==="product"?"prereq-product-text":"prereq-license-text-"+componentId;
  box.append(content);
  if(refs.length) {
    const consent=prerequisiteConsent(kind,refs,tr(kind==="product"?"prereq_accept_product":"prereq_accept_license"),componentId);
    consent.input.id=kind==="product"?"prereq-accept-product":"prereq-license-accept-"+componentId;
    consent.input.disabled=true;
    const enable=()=>{if(content.scrollTop+content.clientHeight>=content.scrollHeight-8){consent.input.disabled=false;updatePrerequisiteControls();}};
    content.addEventListener("scroll",enable);box.append(consent.row);requestAnimationFrame(enable);
  }
  const save=prepNode("button",tr("prereq_save_terms"),"secondary");save.type="button";
  save.addEventListener("click",()=>{const url=URL.createObjectURL(new Blob([text],{type:"text/plain;charset=utf-8"}));const link=prepNode("a");link.href=url;link.download=kind==="product"?"product-terms.txt":"component-terms.txt";link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);});box.append(save);
  $("prereq-terms").append(box);
}
function renderPrerequisiteTerms() {
  $("prereq-terms").replaceChildren();prereq.consentInputs=[];prereq.unsupportedConsent=false;
  if(!prereq.review)return;
  const p=prereq.review.plan;const required=p.required_consents||[];const mapped=new Set();
  const productRefs=required.filter(ref=>(p.product_consent_refs||[]).includes(ref));
  addPrerequisiteTerms(tr("prereq_terms_product"),prereq.productTerms.text,
    prepText("prereq_terms_metadata",{locale:prereq.productTerms.locale||locale,version:session.options?.release||tr("prereq_version_unknown"),digest:prereq.productTerms.digest}),productRefs,"product");
  productRefs.forEach(ref=>mapped.add(ref));
  // The HTTP product-terms acknowledgement stays explicit even if a host's
  // product refs list is empty; no technical reference authorizes itself.
  if(!productRefs.length){const consent=prerequisiteConsent("product",[],tr("prereq_accept_product"));consent.input.id="prereq-accept-product";consent.input.disabled=true;const e=$("prereq-product-text");const enable=()=>{if(e.scrollTop+e.clientHeight>=e.scrollHeight-8){consent.input.disabled=false;updatePrerequisiteControls();}};e.addEventListener("scroll",enable);$("prereq-terms").append(consent.row);requestAnimationFrame(enable);}
  for(const item of p.components||[])if(item.choice!=="skip") {
    const license=prereq.licenses[item.component_id];const refs=required.filter(ref=>ref.startsWith("prerequisite-license:"+item.component_id+":"));
    addPrerequisiteTerms(prepText("prereq_terms_component",{component:prerequisiteName(item)}),license.text,
      prepText("prereq_terms_metadata",{locale:license.locale,version:license.version,digest:license.text_sha256}),refs,"license",item.component_id);
    refs.forEach(ref=>mapped.add(ref));
  }
  for(const ref of required) {
    if(mapped.has(ref))continue;
    if(ref.startsWith("prerequisites:review:")){mapped.add(ref);continue;}
    let kind=null,label=null,componentId=null;
    for(const item of p.components||[])if(item.choice!=="skip") {
      if(ref.startsWith("prerequisite-auto:"+item.component_id+":")){kind="auto";componentId=item.component_id;label=prepText("prereq_accept_auto",{component:prerequisiteName(item)});break;}
      if(ref.startsWith("prerequisite-elevation:"+item.component_id+":")){kind="elevation";componentId=item.component_id;label=prepText("prereq_accept_elevation",{component:prerequisiteName(item)});break;}
      for(const condition of item.license.conditions||[])if(ref.startsWith("prerequisite-condition:"+condition+":")){kind="condition";label=prepText("prereq_accept_condition",{condition});break;}
      if(kind)break;
    }
    if(kind){const consent=prerequisiteConsent(kind,[ref],label,componentId);const box=prepNode("div",undefined,"prereq-consent");box.append(consent.row);$("prereq-terms").append(box);mapped.add(ref);}
    else prereq.unsupportedConsent=true;
  }
  if(prereq.unsupportedConsent)$("prereq-terms").append(prepNode("p",tr("prereq_unsupported_consent"),"notice"));
  $("prereq-accept-plan").checked=false;
}
function prerequisiteReviewApproved() {
  return !!prereq.review&&!prereq.unsupportedConsent&&$("prereq-accept-plan").checked&&
    prereq.consentInputs.every(({input})=>input.checked&&!input.disabled);
}
function acceptedPrerequisiteRefs() {
  const refs=new Set();for(const item of prereq.consentInputs)if(item.input.checked&&!item.input.disabled)item.refs.forEach(ref=>refs.add(ref));
  if($("prereq-accept-plan").checked)for(const ref of prereq.review.plan.required_consents||[])if(ref.startsWith("prerequisites:review:"))refs.add(ref);
  if(!(prereq.review.plan.required_consents||[]).every(ref=>refs.has(ref)))throw{code:"CONSENT_REQUIRED"};
  return [...refs];
}
async function loadPrerequisiteCapabilities() {
  const result={};
  for(const mode of ["native","docker"])result[mode]=await api("/prerequisites/capabilities?mode="+mode);
  prereq.capabilitiesByMode=result;prereq.caps=result[prereq.mode];
}
function renderPrerequisiteOperations() {
  $("prereq-operations").replaceChildren();
  if(prereq.operations.length||prereq.nextCursor)$("prereq-operations").append(prepNode("h3",tr("prereq_operations")));
  for(const item of prereq.operations){const button=prepNode("button",prepText("prereq_review_operation",{operation:item.operation_id}),"secondary");button.type="button";button.addEventListener("click",()=>guarded(()=>selectPrerequisiteOperation(item.operation_id,item.target?.mode)));$("prereq-operations").append(button);}
  if(prereq.nextCursor){const button=prepNode("button",tr("prereq_more_operations"),"secondary");button.type="button";button.addEventListener("click",()=>guarded(async()=>{await loadPrerequisiteOperations(true);updatePrerequisiteControls();}));$("prereq-operations").append(button);}
}
async function loadPrerequisiteOperations(append=false) {
  const request=++prereq.listEpoch,previousCursor=prereq.nextCursor;
  prereq.listPending=true;updatePrerequisiteControls();
  try {
    if(!Object.values(prereq.capabilitiesByMode).some(value=>value?.configured)){
      prereq.operations=[];prereq.nextCursor=null;renderPrerequisiteOperations();return;
    }
    const suffix=append&&previousCursor?"&after_id="+encodeURIComponent(previousCursor):"";
    const result=await api("/prerequisites/operations?unfinished_only=true&limit=100"+suffix);
    if(request!==prereq.listEpoch)return;
    if(!Array.isArray(result.operations)||result.operations.some(item=>
      typeof item.operation_id!=="string"||!samePrerequisiteBinding(item,prereq.capabilitiesByMode[item.target?.mode])))throw{code:"INTEGRITY_FAILED"};
    prereq.operations=append?[...prereq.operations,...result.operations]:result.operations;
    prereq.nextCursor=result.next_cursor||null;renderPrerequisiteOperations();
  }finally{if(request===prereq.listEpoch){prereq.listPending=false;updatePrerequisiteControls();}}
}
async function initializePrerequisites() {
  prereq.initialized=true;prereq.discoveryUnknown=true;prereq.mode=$("mode").value;renderPrerequisiteStatus();
  try {
    await loadPrerequisiteCapabilities();await loadPrerequisiteOperations();prereq.discoveryUnknown=false;
    const stored=storedPrerequisiteReference();const selected=stored||((prereq.operations.length===1&&!prereq.nextCursor)?prereq.operations[0]:null);
    if(selected)await selectPrerequisiteOperation(selected.operation_id,selected.target?.mode);
    else renderPrerequisiteStatus();
  }catch(error){prereq.connectionLost=true;renderPrerequisiteStatus();throw error;}
}
async function refreshPrerequisites() {
  if(prereq.refreshPending)return;
  prereq.refreshPending=true;prereq.discoveryUnknown=true;renderPrerequisiteStatus();
  let observedId=null,observedEpoch=null;
  try {
    await loadPrerequisiteCapabilities();await loadPrerequisiteOperations();
    prereq.discoveryUnknown=false;
    const operationId=prereq.operationId,epoch=prereq.observationEpoch;
    observedId=operationId;observedEpoch=epoch;
    if(operationId){
      const value=await api("/prerequisites/operations/"+encodeURIComponent(operationId));
      if(operationId===prereq.operationId&&epoch===prereq.observationEpoch)acceptPrerequisiteSnapshot(value);
      pollPrerequisites();
    }else prereq.connectionLost=false;
  }catch(error){
    if(observedId&&(observedId!==prereq.operationId||observedEpoch!==prereq.observationEpoch))return;
    prereq.connectionLost=true;throw error;
  }
  finally{prereq.refreshPending=false;renderPrerequisiteStatus();}
}
async function selectPrerequisiteOperation(operationId,mode) {
  if(prerequisiteRequestBusy())throw{code:"OPERATION_CONFLICT"};
  const caps=prereq.capabilitiesByMode[mode];if(!caps?.configured)throw{code:"CAPABILITY_UNSUPPORTED"};
  prereq.epoch++;const epoch=++prereq.observationEpoch;prereq.pollEpoch++;prereq.polling=false;clearPrerequisiteReview();
  prereq.mode=mode;prereq.caps=caps;$("mode").value=mode;prereq.operationId=operationId;
  prereq.displayPlan=null;prereq.choices={};prereq.afterReady=false;
  prereq.snapshot=null;prereq.cursor=0;prereq.revision=-1;prereq.eventLines=[];prereq.startUncertain=false;
  invalidateProductForPrerequisites();savePrerequisiteReference();panel(0);renderPrerequisiteComponents();renderPrerequisiteStatus();
  try {
    const value=await api("/prerequisites/operations/"+encodeURIComponent(operationId));
    if(operationId!==prereq.operationId||epoch!==prereq.observationEpoch)return;
    acceptPrerequisiteSnapshot(value);pollPrerequisites();
  }catch(error){if(operationId===prereq.operationId&&epoch===prereq.observationEpoch){prereq.connectionLost=true;renderPrerequisiteStatus();throw error;}}
}
async function changePrerequisiteMode() {
  if(!prereq.initialized)return;
  if(prerequisiteRequestBusy()||prereq.operationId&&!prerequisiteSettled()||prereq.operations.length||prereq.nextCursor){$("mode").value=prereq.mode;return;}
  prereq.epoch++;prereq.observationEpoch++;prereq.pollEpoch++;prereq.polling=false;clearPrerequisiteReview();prereq.displayPlan=null;prereq.choices={};
  prereq.operationId=null;prereq.snapshot=null;prereq.afterReady=false;prereq.cursor=0;prereq.revision=-1;
  sessionStorage.removeItem(prereqStorageKey);prereq.mode=$("mode").value;prereq.caps=prereq.capabilitiesByMode[prereq.mode];
  renderPrerequisiteComponents();renderPrerequisiteStatus();
}
function localizePrerequisites() {
  if(!prereq.initialized)return;
  // An executing operation keeps its original plan/consents. A draft with new
  // language must be reviewed afresh; translation never causes a mutation.
  if(prereq.review||prereq.reviewPending){prereq.epoch++;clearPrerequisiteReview();}
  renderPrerequisiteComponents();renderPrerequisiteOperations();renderPrerequisiteStatus();
}
async function planPrerequisites() {
  if(!prereq.caps?.configured||prereq.discoveryUnknown||prereq.connectionLost||prerequisiteRequestBusy()||prereq.operationId&&!prerequisiteControllerFree())throw{code:"OPERATION_CONFLICT"};
  if(prereq.operationId&&!prerequisiteSettled()&&(Object.values(prereq.snapshot.steps||{}).some(item=>["UNKNOWN","INSTALLING"].includes(item.state))||["REBOOT_REQUIRED","RELOGIN_REQUIRED"].includes(prereq.snapshot.status)))throw{code:"OPERATION_CONFLICT"};
  const epoch=++prereq.epoch;prereq.reviewPending=true;clearPrerequisiteReview();invalidateProductForPrerequisites();updatePrerequisiteControls();
  try {
    const result=await api("/prerequisites/plans",{mode:prereq.mode,locale,choices:{...prereq.choices},offline:false});
    if(epoch!==prereq.epoch)return;
    if(!samePrerequisiteBinding(prereq.caps,result.plan)||result.locale!==locale||result.plan?.locale!==locale)throw{code:"PLAN_STALE"};
    prereq.displayPlan=result.plan;prereq.choices={...result.plan.choices};renderPrerequisiteComponents();
    const productTerms=await api("/terms/"+locale+"?mode="+encodeURIComponent(prereq.mode));
    if(epoch!==prereq.epoch)return;
    if(productTerms.digest!==result.terms_digest||typeof productTerms.text!=="string")throw{code:"INTEGRITY_FAILED"};
    const licenses={};
    for(const item of result.plan.components||[])if(item.choice!=="skip") {
      const license=await api("/prerequisites/licenses/"+encodeURIComponent(item.component_id));
      if(epoch!==prereq.epoch)return;
      if(typeof license.text!=="string"||!["license_id","version","locale","text_sha256"].every(key=>license[key]===item.license[key]))throw{code:"INTEGRITY_FAILED"};
      licenses[item.component_id]=license;
    }
    prereq.review=result;prereq.productTerms=productTerms;prereq.licenses=licenses;
    prereq.replaceOperationId=prereq.operationId&&!prerequisiteSettled()?prereq.operationId:null;
    prereq.connectionLost=false;renderPrerequisiteTerms();$("prereq-review-panel").hidden=false;
    const sizes=(result.plan.components||[]).filter(item=>item.choice==="auto"&&!item.initial_ready).flatMap(item=>item.artifacts||[]).map(item=>item.size);
    $("prereq-download-size").textContent=sizes.every(size=>Number.isFinite(size)&&size>=0)?prepText("prereq_download_size",{size:new Intl.NumberFormat(locale,{maximumFractionDigits:1}).format(sizes.reduce((a,b)=>a+b,0)/1048576)}):tr("prereq_download_unknown");
    $("prereq-status").textContent=tr("prereq_plan_ready");
  }finally{prereq.reviewPending=false;renderPrerequisiteComponents();updatePrerequisiteControls();}
}
async function startPrerequisites() {
  if(prerequisiteRequestBusy()||prereq.discoveryUnknown||prereq.connectionLost||!prerequisiteReviewApproved()||
     prereq.operationId&&!prerequisiteControllerFree())throw{code:"CONSENT_REQUIRED"};
  const review=prereq.review,replace=prereq.replaceOperationId,refs=acceptedPrerequisiteRefs();
  if(!replace&&(prereq.nextCursor||prereq.operations.some(item=>item.operation_id!==prereq.operationId)))throw{code:"OPERATION_CONFLICT"};
  prereq.startPending=true;prereq.observationEpoch++;prereq.connectionLost=false;invalidateProductForPrerequisites();
  if(!replace){prereq.operationId=review.operation_id;prereq.snapshot=null;prereq.revision=-1;prereq.cursor=0;prereq.eventLines=[];prereq.afterReady=false;}
  const operationId=prereq.operationId;savePrerequisiteReference();updatePrerequisiteControls();
  try {
    const path=replace?"/prerequisites/operations/"+encodeURIComponent(replace)+"/replace-plan":"/prerequisites/operations";
    const result=await api(path,{plan_id:review.plan_id,plan_digest:review.plan_digest,terms_digest:review.terms_digest,
      accepted_terms:true,accepted_plan:true,consent_refs:refs});
    acceptPrerequisiteSnapshot(result);clearPrerequisiteReview();
  }catch(error){
    // A failed response is not evidence that the durable operation was absent.
    // Reconcile the preallocated ID with GET; never submit a second start here.
    prereq.startUncertain=true;prereq.connectionLost=true;
    try {
      acceptPrerequisiteSnapshot(await api("/prerequisites/operations/"+encodeURIComponent(operationId)));
      clearPrerequisiteReview();
    }catch(observationError){
      const refused=["INVALID_REQUEST","CONSENT_REQUIRED","PLAN_NOT_FOUND","PLAN_STALE","CAPABILITY_UNSUPPORTED","OPERATION_CONFLICT"].includes(error?.code);
      if(!replace&&refused&&observationError?.code==="OPERATION_NOT_FOUND"){
        prereq.operationId=null;prereq.snapshot=null;prereq.startUncertain=false;prereq.connectionLost=false;
        sessionStorage.removeItem(prereqStorageKey);clearPrerequisiteReview();
        prereq.discoveryUnknown=true;
        try{await loadPrerequisiteOperations();prereq.discoveryUnknown=false;}catch{prereq.connectionLost=true;}
      }
    }
    throw error;
  }finally{prereq.startPending=false;prereq.observationEpoch++;renderPrerequisiteStatus();if(prereq.operationId)pollPrerequisites();}
}
function acceptPrerequisiteSnapshot(value) {
  if(!value||value.operation_id!==prereq.operationId||!samePrerequisiteBinding(value,prereq.caps)||!Number.isInteger(value.revision)||value.revision<0)throw{code:"INTEGRITY_FAILED"};
  if(value.revision<prereq.revision)return false;
  const changed=value.revision!==prereq.revision||value.status!==prereq.snapshot?.status||value.ready!==prereq.snapshot?.ready;
  prereq.snapshot=value;prereq.revision=value.revision;prereq.startUncertain=false;prereq.connectionLost=false;
  if(changed){invalidateProductForPrerequisites();if(value.status==="READY"&&value.ready===true)prereq.afterReady=true;}
  savePrerequisiteReference();renderPrerequisiteStatus();return true;
}
function renderPrerequisiteStatus() {
  if(!prereq.initialized)return;
  $("prereq-section").hidden=!(prereq.caps?.configured||prereq.discoveryUnknown||prereq.operationId||prereq.operations.length||prereq.nextCursor);
  const value=prereq.snapshot;
  $("prereq-operation").hidden=!prereq.operationId;
  if(prereq.connectionLost||prereq.startUncertain)$("prereq-status").textContent=tr("prereq_connection_lost");
  else if(prereq.discoveryUnknown)$("prereq-status").textContent=tr("prereq_checking");
  else if(prereq.reviewPending)$("prereq-status").textContent=tr("prereq_checking");
  else if(!prereq.caps?.configured)$("prereq-status").textContent=tr("prereq_not_configured");
  else if(prereq.review)$("prereq-status").textContent=tr("prereq_plan_ready");
  else $("prereq-status").textContent=prereq.displayPlan?tr("prereq_review_dirty"):tr("prereq_help");
  if(prereq.operationId) {
    let label=tr("prereq_unknown");
    if(value){if(value.execution?.active!==false)label=tr(value.execution?.active===true?"prereq_controller_running":"prereq_controller_unknown");
      else label=value.status==="READY"?(value.ready===true?tr("prereq_ready"):tr("prereq_unknown")):prereqStates.has(value.status)?tr("status_"+value.status.toLowerCase()):tr("prereq_unknown");}
    $("prereq-operation-status").textContent=prereq.operationId+" · "+label;
    const actionKey="prereq_action_"+value?.required_action;
    $("prereq-required-action").textContent=messages[actionKey]||"";
    $("prereq-required-action").hidden=!messages[actionKey];
    const ready=value?.status==="READY"&&value.ready===true&&prerequisiteControllerFree();
    if(ready)$("prereq-progress").value=100;else $("prereq-progress").removeAttribute("value");
    $("prereq-progress").setAttribute("aria-label",tr("prereq_operation"));
    $("prereq-progress-components").replaceChildren();
    let count=0,total=0;
    for(const [componentId,state] of Object.entries(value?.steps||{})) {
      const row=prepNode("div",undefined,"component");const check=(value.checks||[]).find(item=>item.component_id===componentId);
      row.append(prepNode("strong",componentId));
      if(state.state!=="SKIPPED"){total++;const bar=prepNode("progress");bar.max=100;if(state.state==="READY"){bar.value=100;count++;}else bar.removeAttribute("value");row.append(bar);}
      row.append(prepNode("span",messages["prereq_component_"+String(state.state).toLowerCase()]||tr("prereq_unknown")));
      if(check)row.append(prepNode("p",tr("prereq_found_version")+": "+(knownPrerequisiteVersion(check.detected_version)||tr("prereq_version_unknown"))));
      if(check&&!check.ready)appendPrerequisiteManual(row,check.manual);
      $("prereq-progress-components").append(row);
    }
    $("prereq-completed").textContent=prepText("prereq_completed",{count,total});
    $("prereq-events").textContent=JSON.stringify({operation_id:prereq.operationId,status:value?.status||null,ready:value?.ready===true,
      revision:value?.revision,execution:value?.execution||null,required_action:value?.required_action||null,error_code:value?.error?.code||null},null,2)+"\n"+prereq.eventLines.join("\n");
    if(value?.host_action?.performed===false)$("prereq-operation-status").textContent+=" · "+tr("prereq_boundary_recorded");
    if(value?.error?.code)$("prereq-operation-status").textContent+=" · "+value.error.code;
  }
  updatePrerequisiteControls();
}
function updatePrerequisiteControls() {
  if(!prereq.initialized)return;
  const value=prereq.snapshot,free=prerequisiteControllerFree(),busy=prerequisiteRequestBusy()||prereq.discoveryUnknown||prereq.connectionLost;
  const terminal=value&&["READY","CANCELED"].includes(value.status);
  const uncertain=Object.values(value?.steps||{}).some(item=>["UNKNOWN","INSTALLING"].includes(item.state));
  const boundary=value&&["REBOOT_REQUIRED","RELOGIN_REQUIRED"].includes(value.status);
  $("prereq-review").disabled=!prereq.caps?.configured||busy||!!prereq.operationId&&(!free||!terminal&&(uncertain||boundary));
  $("prereq-refresh").disabled=prereq.refreshPending;
  $("prereq-start").disabled=busy||!prerequisiteReviewApproved()||!!prereq.operationId&&!free||
    !prereq.replaceOperationId&&(!!prereq.nextCursor||prereq.operations.some(item=>item.operation_id!==prereq.operationId));
  $("prereq-start").textContent=tr(prereq.replaceOperationId?"prereq_replace":"prereq_prepare");
  $("prereq-decline").disabled=prereq.startPending;
  $("prereq-recheck").hidden=!value||terminal;
  $("prereq-recheck").disabled=!free||busy;
  $("prereq-resume").hidden=!value||terminal||!["resume","reconcile_installer","resolve_prerequisite_error"].includes(value.required_action);
  $("prereq-resume").disabled=!free||busy;
  $("prereq-cancel").hidden=!prereq.operationId||!!terminal&&free;
  $("prereq-cancel-help").hidden=$("prereq-cancel").hidden;
  $("prereq-cancel").disabled=prereq.cancelPending||prereq.startPending||!value;
  $("prereq-replan").hidden=!value||terminal;
  $("prereq-replan").disabled=!free||busy||uncertain||["REBOOT_REQUIRED","RELOGIN_REQUIRED"].includes(value?.status);
  $("prereq-now").hidden=$("prereq-later").hidden=$("prereq-boundary-help").hidden=!boundary;
  $("prereq-now").disabled=$("prereq-later").disabled=!free||busy;
  $("prereq-proceed").hidden=!(value?.status==="READY"&&value.ready===true);
  $("prereq-proceed").disabled=!free||busy||prereq.discoveryUnknown||prereq.operations.some(item=>item.operation_id!==prereq.operationId)||!!prereq.nextCursor;
  for(const select of document.querySelectorAll("#prereq-components select"))select.disabled=busy||!!prereq.operationId&&!free;
  updateCapabilities();
}
async function pollPrerequisites() {
  if(prereq.polling||!prereq.operationId)return;
  const operationId=prereq.operationId,epoch=++prereq.pollEpoch;prereq.polling=true;
  try {
    while(prereq.operationId===operationId&&epoch===prereq.pollEpoch) {
      const observation=prereq.observationEpoch;
      const result=await api("/prerequisites/operations/"+encodeURIComponent(operationId)+"/events?after_sequence="+prereq.cursor+"&limit=100");
      if(prereq.operationId!==operationId||epoch!==prereq.pollEpoch)return;
      if(observation===prereq.observationEpoch){
        for(const event of result.events||[])if(Number.isInteger(event.sequence)&&event.sequence>prereq.cursor){
          prereq.cursor=event.sequence;
          prereq.eventLines.push(JSON.stringify({sequence:event.sequence,component_id:event.component_id,phase:event.phase,status:event.status,message_code:event.message_code}));
        }
        prereq.eventLines=prereq.eventLines.slice(-100);acceptPrerequisiteSnapshot(result.snapshot);
        if(prerequisiteControllerFree()&&!prereq.changePending&&!prereq.startPending&&!prereq.cancelPending){
          if(prerequisiteSettled()){await loadPrerequisiteOperations();renderPrerequisiteStatus();}break;
        }
      }
      await new Promise(resolve=>setTimeout(resolve,700));
    }
  }catch(error){if(prereq.operationId===operationId&&epoch===prereq.pollEpoch){
    // Error details may need another HTTP request. Release this poll before
    // awaiting it, so an explicit Refresh can start observing immediately.
    prereq.polling=false;prereq.pollEpoch++;
    prereq.connectionLost=true;renderPrerequisiteStatus();await showError(error);
  }}
  finally{if(epoch===prereq.pollEpoch)prereq.polling=false;}
}
async function prerequisiteControl(action,body={}) {
  if(!prereq.operationId)throw{code:"OPERATION_NOT_FOUND"};
  if(action!=="cancel"&&(!prerequisiteControllerFree()||prereq.discoveryUnknown||prereq.connectionLost||prerequisiteRequestBusy()))throw{code:"OPERATION_CONFLICT"};
  if(action==="cancel"){if(prereq.cancelPending)return;prereq.cancelPending=true;}else prereq.changePending=action;
  const operationId=prereq.operationId;prereq.observationEpoch++;
  invalidateProductForPrerequisites();updatePrerequisiteControls();pollPrerequisites();
  try {
    const result=await api("/prerequisites/operations/"+encodeURIComponent(operationId)+"/"+action,body);
    if(operationId===prereq.operationId){acceptPrerequisiteSnapshot(result);clearPrerequisiteReview();}
  }catch(error){if(operationId===prereq.operationId)prereq.connectionLost=true;throw error;}
  finally{
    if(action==="cancel")prereq.cancelPending=false;else prereq.changePending=null;
    prereq.observationEpoch++;renderPrerequisiteStatus();pollPrerequisites();
  }
}
async function proceedAfterPrerequisites() {
  if(!prerequisiteControllerFree()||prereq.snapshot?.status!=="READY"||prereq.snapshot.ready!==true||
     prereq.discoveryUnknown||prereq.connectionLost||prerequisiteRequestBusy())throw{code:"OPERATION_CONFLICT"};
  const operationId=prereq.operationId,chosenLocale=locale,chosenMode=prereq.mode;
  prereq.proceedPending=true;updatePrerequisiteControls();
  const current=()=>operationId===prereq.operationId&&locale===chosenLocale&&prereq.mode===chosenMode&&!languagePending;
  try {
    acceptPrerequisiteSnapshot(await api("/prerequisites/operations/"+encodeURIComponent(operationId)));
    if(!current()||!prerequisiteControllerFree()||prereq.snapshot.status!=="READY"||prereq.snapshot.ready!==true)throw{code:"OPERATION_CONFLICT"};
    await loadPrerequisiteOperations();if(prereq.operations.length||prereq.nextCursor)throw{code:"OPERATION_CONFLICT"};
    invalidateProductForPrerequisites();
    for(const mode of ["native","docker"])capabilitiesByMode[mode]=await api("/capabilities?mode="+mode);
    if(!current())throw{code:"PLAN_STALE"};
    await discover();if(!current())throw{code:"PLAN_STALE"};
    clearPrerequisiteReview();await loadTerms();
    if(!current())throw{code:"PLAN_STALE"};
    prereq.afterReady=false;
  }catch(error){prereq.afterReady=true;invalidateProductForPrerequisites();throw error;}
  finally{prereq.proceedPending=false;updatePrerequisiteControls();}
}
$("prereq-review").addEventListener("click",()=>guarded(planPrerequisites));
$("prereq-refresh").addEventListener("click",()=>guarded(refreshPrerequisites));
$("prereq-accept-plan").addEventListener("change",updatePrerequisiteControls);
$("prereq-start").addEventListener("click",()=>guarded(startPrerequisites));
$("prereq-decline").addEventListener("click",()=>{if(prereq.startPending)return;prereq.epoch++;clearPrerequisiteReview();renderPrerequisiteStatus();});
$("prereq-recheck").addEventListener("click",()=>guarded(()=>prerequisiteControl("recheck")));
$("prereq-resume").addEventListener("click",()=>guarded(()=>prerequisiteControl("resume")));
$("prereq-cancel").addEventListener("click",()=>guarded(()=>prerequisiteControl("cancel")));
$("prereq-replan").addEventListener("click",()=>guarded(planPrerequisites));
$("prereq-now").addEventListener("click",()=>guarded(()=>prerequisiteControl("boundary",{when:"now"})));
$("prereq-later").addEventListener("click",()=>guarded(()=>prerequisiteControl("boundary",{when:"later"})));
$("prereq-proceed").addEventListener("click",()=>guarded(proceedAfterPrerequisites));
$("prereq-print").addEventListener("click",()=>window.print());
