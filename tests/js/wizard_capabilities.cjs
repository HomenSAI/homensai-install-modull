"use strict";
// Actual wizard script, injected DOM/fetch. This is a response contract test,
// not a browser render or installed product acceptance.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {id, value:"", checked:false, disabled:false,
    hidden:false, textContent:"", options:[], events:{},
    addEventListener(name, fn) { this.events[name] = fn; }});
  return elements.get(id);
}
element("mode").options = ["native","docker"].map(value => ({value, disabled:false}));
element("action").options = ["install","start","stop","backup","restore"].map(value => ({value, disabled:false}));
element("mode").value = "native";
element("action").value = "install";
const context = vm.createContext({
  document:{getElementById:element,querySelectorAll:()=>[],documentElement:{}},
  URLSearchParams, URL, location:{hash:"",pathname:"/"},
  sessionStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}},
  history:{replaceState:()=>{}}, fetch:()=>new Promise(()=>{}),
});
const assetDir=process.argv[2]||path.resolve(__dirname,"../../homensai_install/wizard");
vm.runInContext(fs.readFileSync(path.join(assetDir,"app.js"),"utf8"),context);
function run(source) { return vm.runInContext(source,context); }
function option(id, value) { return element(id).options.find(item=>item.value===value); }
run(`messages={next:"Next",action_unavailable:"Choose a supported action"};
     capabilitiesByMode.native={actions:{install:{supported:true},start:{supported:true}}};
     capabilitiesByMode.docker={actions:{install:{supported:false},start:{supported:true},stop:{supported:true},backup:{supported:true}}};
     capabilitiesLoaded=true; updateCapabilities();`);
assert.equal(option("mode","docker").disabled,false,"Docker maintenance must not depend on install support");
assert.equal(element("next").disabled,false);
assert.equal(option("action","backup").disabled,true);

run('plan={plan_id:"reviewed-native"};terms={digest:"native"};step=2;');
element("accept-terms").checked=element("accept-plan").checked=true;
element("mode").value="docker";
element("mode").events.change();
assert.equal(run("plan"),null);
assert.equal(run("terms"),null);
assert.equal(run("step"),0);
assert.equal(element("accept-terms").checked,false);
assert.equal(element("accept-plan").checked,false);
assert.equal(element("next").disabled,true,"Unavailable selected action requires a new user choice");
assert.equal(element("capability-status").hidden,false);
assert.equal(element("discover").disabled,false);

element("action").value="backup";
element("action").events.change();
assert.equal(element("next").disabled,false);
assert.equal(element("capability-status").hidden,true);
assert.equal(option("action","install").disabled,true);
assert.equal(option("action","backup").disabled,false);

run('capabilitiesByMode.docker={actions:{}};updateCapabilities();');
assert.equal(option("mode","docker").disabled,true);
assert.equal(element("discover").disabled,true);
assert.equal(element("next").disabled,true);

run('messages.action_unavailable="Wählen Sie eine andere Aktion";updateCapabilities();');
assert.equal(element("capability-status").textContent,"Wählen Sie eine andere Aktion");

run('step=3;plan={plan_id:"active"};terms={digest:"active"};');
element("mode").events.change();
assert.equal(run("step"),3);
assert.equal(run("plan.plan_id"),"active","Hidden controls cannot discard running operation progress");
console.log(JSON.stringify({status:"PASS",cases:6,scope:"wizard JavaScript with injected DOM/HTTP"}));
