"use strict";
// Execute the shipped JavaScript against a small DOM and controlled HTTP.
// These tests do not replace browser rendering or installed OS acceptance.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {createHash} = require("node:crypto");

const assetDir = process.argv[2] || path.resolve(__dirname, "../../homensai_install/wizard");
const assetBytes = Object.fromEntries(["app.js", "index.html", "locales.json"].map(name => [name, fs.readFileSync(path.join(assetDir, name))]));
const script = assetBytes["app.js"].toString("utf8");
const html = assetBytes["index.html"].toString("utf8");
const translations = JSON.parse(assetBytes["locales.json"].toString("utf8"));
const copy = value => JSON.parse(JSON.stringify(value));
const canonical = value => JSON.stringify(value, (_, item) => item && !Array.isArray(item) && typeof item === "object" ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
const digest = value => createHash("sha256").update(canonical(value)).digest("hex");
const deferred = () => {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject};};
const decode = value => value.replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">");

class Element {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase(); this.attributes = {...attrs}; this.children = [];
    this.parentNode = null; this.events = {}; this.dataset = {}; this._text = "";
    this.checked = "checked" in attrs; this.disabled = "disabled" in attrs; this.hidden = "hidden" in attrs;
    this.selected = "selected" in attrs; this.scrollTop = 0; this.clientHeight = 100; this.scrollHeight = 100;
    this.id = attrs.id || ""; this.type = attrs.type || ""; this.className = attrs.class || "";
    this._value = attrs.value; this.max = attrs.max ? Number(attrs.max) : 100;
    for (const [key, value] of Object.entries(attrs)) if (key.startsWith("data-"))
      this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
    this.classList = {toggle: (name, enabled) => {
      const classes = new Set(this.className.split(/\s+/).filter(Boolean));
      if (enabled === undefined ? !classes.has(name) : enabled) classes.add(name); else classes.delete(name);
      this.className = [...classes].join(" ");
    }, add: name => {this.className += " " + name;}, remove: name => {
      this.className = this.className.split(/\s+/).filter(value => value !== name).join(" ");
    }, contains: name => this.className.split(/\s+/).includes(name)};
  }
  get value() {
    if (this.tagName === "SELECT") {
      if (this._value !== undefined) return this._value;
      return (this.options.find(option => option.selected) || this.options[0])?.value || "";
    }
    return this._value === undefined ? (this.tagName === "OPTION" ? this.textContent : "") : this._value;
  }
  set value(value) {this._value = value; if (this.tagName === "PROGRESS") this.attributes.value = String(value);}
  get options() {return this.children.filter(child => child.tagName === "OPTION");}
  get textContent() {return this._text + this.children.map(child => child.textContent).join("");}
  set textContent(value) {this._text = String(value); this.children = [];}
  set innerHTML(_) {throw new Error("The wizard must not insert response data through innerHTML");}
  get innerHTML() {return this.textContent;}
  get firstChild() {return this.children[0] || null;}
  append(...values) {for (let value of values) {if (typeof value === "string") {const text = new Element("#text"); text.textContent = value; value = text;} value.remove(); value.parentNode = this; this.children.push(value);}}
  appendChild(value) {this.append(value); return value;}
  replaceChildren(...values) {for (const child of this.children) child.parentNode = null; this.children = []; this._text = ""; this.append(...values);}
  remove() {if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null;}
  setAttribute(name, value) {this.attributes[name] = String(value); if (name === "value") this.value = value; else if (name === "id") this.id = String(value); else if (name === "class") this.className = String(value); else if (name === "disabled" || name === "hidden") this[name] = true;}
  getAttribute(name) {return this.attributes[name] ?? null;}
  hasAttribute(name) {return name in this.attributes;}
  removeAttribute(name) {delete this.attributes[name]; if (name === "value") this._value = undefined; if (name === "id") this.id = ""; if (name === "disabled" || name === "hidden") this[name] = false;}
  addEventListener(name, fn) {(this.events[name] ||= []).push(fn);}
  async dispatch(name, extra = {}) {for (const fn of this.events[name] || []) await fn({type: name, target: this, currentTarget: this, preventDefault() {}, ...extra});}
  click() {return this.disabled ? Promise.resolve() : this.dispatch("click");}
  scrollIntoView() {}
  focus() {}
  querySelectorAll(selector) {return descendants(this).filter(node => matchesSelector(node, selector));}
  querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
  cloneNode(deep) {const node = new Element(this.tagName, this.attributes); node.textContent = this._text; node._value = this._value; if (deep) node.append(...this.children.map(child => child.cloneNode(true))); return node;}
}
function descendants(root) {return root.children.flatMap(child => [child, ...descendants(child)]);}
function matchesSimple(node, selector) {
  if (selector.includes(":checked") && !node.checked) return false;
  if (selector.includes(":disabled") && !node.disabled) return false;
  selector = selector.replace(/:(checked|disabled)/g, "");
  const attrs = [...selector.matchAll(/\[([^=\]]+)(?:=["']?([^"'\]]*)["']?)?\]/g)];
  for (const [, name, value] of attrs) {
    const actual = name.startsWith("data-") ? node.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] : node[name] ?? node.getAttribute(name);
    if (actual === undefined || actual === null || (value !== undefined && actual !== value)) return false;
  }
  selector = selector.replace(/\[[^\]]*\]/g, "");
  const id = selector.match(/#([\w-]+)/); if (id && node.id !== id[1]) return false;
  const classes = [...selector.matchAll(/\.([\w-]+)/g)]; if (classes.some(([, name]) => !node.classList.contains(name))) return false;
  const tag = selector.match(/^[\w-]+/); return !tag || node.tagName === tag[0].toUpperCase();
}
function matchesSelector(node, selector) {
  if (selector.includes(",")) return selector.split(",").some(part => matchesSelector(node, part.trim()));
  const parts = selector.trim().split(/\s+/); if (!matchesSimple(node, parts.pop())) return false;
  let ancestor = node.parentNode;
  while (parts.length) {const part = parts.pop(); while (ancestor && !matchesSimple(ancestor, part)) ancestor = ancestor.parentNode; if (!ancestor) return false; ancestor = ancestor.parentNode;}
  return true;
}
function parseDocument(source) {
  const root = new Element("document"), stack = [root];
  for (const token of source.match(/<!--[^]*?-->|<![^>]*>|<[^>]+>|[^<]+/g) || []) {
    if (token.startsWith("<!")) continue;
    if (token.startsWith("</")) {if (stack.length > 1) stack.pop(); continue;}
    if (token.startsWith("<")) {
      const match = token.match(/^<([\w-]+)([^]*?)\/?\s*>$/); if (!match) continue;
      const attrs = {}; for (const [, key, quoted, single, plain] of match[2].matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) attrs[key] = decode(quoted ?? single ?? plain ?? "");
      const node = new Element(match[1], attrs); stack.at(-1).append(node);
      if (!/^(input|meta|link|br|hr|img)$/i.test(match[1]) && !token.endsWith("/>")) stack.push(node);
    } else stack.at(-1)._text += decode(token);
  }
  return {root, documentElement: root.querySelector("html"), getElementById: id => descendants(root).find(node => node.id === id) || null,
    querySelectorAll: selector => root.querySelectorAll(selector), querySelector: selector => root.querySelector(selector),
    createElement: tag => new Element(tag), createTextNode: text => {const node = new Element("#text"); node.textContent = text; return node;}};
}

const identity = {product_id: "fixture-product", owner_id: "fixture-owner", target: {os: "linux", arch: "x64", mode: "native", scope: "user"}, catalog_digest: "a".repeat(64)};
function capabilities(changes = {}) {return {...copy(identity), configured: true, production_ready: false, automatic_component_ids: ["base"],
  requirements: [
    {component_id: "base", kind: "runtime", required: true, minimum_version: "1.0.0", maximum_version: "2.0.0", scope: "host"},
    {component_id: "client", kind: "runtime", required: true, minimum_version: "1.0.0", maximum_version: null, scope: "host"},
    {component_id: "metrics", kind: "runtime", required: false, minimum_version: null, maximum_version: null, scope: "host"},
  ], ...changes};}
function reviewed(changes = {}) {const components = ["base", "client", "metrics"].map((name, index) => ({component_id: name, kind: "runtime", required: index < 2,
  choice: index < 2 ? "manual" : "skip", initial_status: "missing", initial_ready: false, detected_version: null, scope: "host",
  depends_on: index === 1 ? ["base"] : [], ownership: "shared", elevated: false,
  artifacts: [{artifact_id: name + "-installer", version: "1.1.0", sha256: "c".repeat(64), size: 1024, license_ref: name + "-license", source_host: "example.invalid"}],
  license: {license_id: name + "-license", version: "1", text_sha256: "d".repeat(64), locale: "en", acceptance_required: index < 2, conditions: [], decision: "allowed"},
  manual: {locale: "en", steps: ["Manual steps for " + name], official_url: "https://example.invalid/" + name, contains_executable_commands: false}}));
  return {plan_id: "prereqplan_fixture", operation_id: "prep_fixture", plan_digest: "plan-digest", terms_digest: "terms-digest", locale: "en",
    plan: {...copy(identity), locale: "en", plan_digest: "plan-digest", components, choices: {base: "manual", client: "manual", metrics: "skip"},
      product_consent_refs: ["terms:terms-digest"], required_consents: ["prerequisites:review:fixture", ...components.filter(row => row.required).map(row => "prerequisite-license:" + row.component_id + ":" + digest(row.license)), "terms:terms-digest"], offline: false}, ...changes};}
function snapshot(changes = {}) {return {...copy(identity), operation_id: "prep_fixture", plan_digest: "plan-digest", revision: 1, status: "AWAITING_USER", ready: false,
  required_action: "manual_install", cancel_requested: false, checks: [], steps: {}, events: [], execution: {mode: "background", state: "finished", active: false}, ...changes};}
function download(changes = {}) {return {schema_version: 1, plan_digest: "plan-digest", component_id: "base",
  artifact_id: "base-installer", artifact_sha256: "c".repeat(64), transfer_id: "e".repeat(32), attempt: 0,
  state: "transferring", completed: 512, total: 1024, unit: "bytes", source: "network", resumed: false,
  verified: false, percent: 50, sequence: 8, updated_at: "2026-10-25T17:00:00Z", ...changes};}
function downloading(changes = {}) {return snapshot({status: "RUNNING", revision: 8,
  required_action: null, steps: {base: {state: "DOWNLOADING", attempt: 0}},
  execution: {mode: "background", state: "running", active: true}, download_progress: download(), ...changes});}

class Harness {
  constructor(options = {}) {
    this.document = parseDocument(html); this.calls = []; this.timers = []; this.errors = []; this.overrides = [];
    this.storage = new Map(Object.entries(options.storage || {})); this.caps = options.caps ?? capabilities(); this.review = options.review ?? reviewed();
    this.current = options.current ?? snapshot(); this.operations = options.operations || []; this.eventQueue = [];
    const fetch = async (url, init = {}) => {
      const request = {url, path: url.replace(/^\/api\/v1/, ""), method: init.method || "GET", body: init.body ? JSON.parse(init.body) : null, init};
      this.calls.push(request);
      let result;
      const matched = this.overrides.find(row => row.matches(request));
      if (matched) result = await matched.handle(request); else result = this.response(request);
      if (result && result.__error) return {ok: false, json: async () => ({error: copy(result.__error)})};
      return {ok: true, json: async () => ({result: copy(result)})};
    };
    const context = {document: this.document, URLSearchParams, URL, Intl, console,
      location: {hash: "#token=fixture-token", pathname: "/"}, navigator: {language: "en"},
      sessionStorage: {getItem: key => this.storage.get(key) ?? null, setItem: (key, value) => this.storage.set(key, String(value)), removeItem: key => this.storage.delete(key)},
      history: {replaceState() {}}, fetch,
      crypto: {randomUUID: () => "fixture-uuid"},
      requestAnimationFrame: callback => callback(),
      setTimeout: (callback, delay) => {const timer = {callback, delay}; this.timers.push(timer); return timer;},
      clearTimeout: timer => {this.timers = this.timers.filter(value => value !== timer);},
    };
    context.window = context;
    this.context = vm.createContext(context);
    vm.runInContext(script, this.context, {filename: "actual-wizard-app.js"});
  }
  response(request) {
    const url = new URL(request.path, "http://fixture.invalid"), route = url.pathname;
    if (route === "/session") return {product_id: identity.product_id, locale: "en", target: copy(identity.target), options: {install_root: "/fixture/install"}, product_modes: [], selected_modes: [], secrets: [], development: true};
    if (route.startsWith("/locales/")) return translations[route.split("/").at(-1)];
    if (route === "/preferences") return {locale: request.body.locale};
    if (route === "/capabilities") return {actions: {install: {supported: true}, start: {supported: true}, backup: {supported: true}}};
    if (route.startsWith("/terms/")) return {text: "Product terms " + route.split("/").at(-1), digest: "terms-digest", locale: route.split("/").at(-1)};
    if (route === "/operations" && request.method === "GET") return {operations: [], has_more: false};
    if (route === "/discover") return {installation: {installed: false}};
    if (route === "/prerequisites/capabilities") return url.searchParams.get("mode") === "docker" ? {configured: false, target: {...identity.target, mode: "docker"}} : copy(this.caps);
    if (route === "/prerequisites/plans") return copy(this.review);
    if (route === "/prerequisites/operations") {
      if (request.method !== "GET") return copy(this.current);
      const operations = this.operations.map(row => row.operation_id === this.current.operation_id ? this.current : row)
        .filter(row => url.searchParams.get("unfinished_only") !== "true" || !["READY", "CANCELED"].includes(row.status));
      return {operations: operations.map(row => {const {steps, checks, events, ...summary} = row; return copy(summary);}), next_cursor: null};
    }
    if (route.startsWith("/prerequisites/licenses/")) {const component = route.split("/").at(-1); return {...copy(this.review.plan.components.find(row => row.component_id === component).license), component_id: component, text: "Separate terms " + component};}
    if (route.endsWith("/events") && route.startsWith("/prerequisites/")) {
      const after = Number(url.searchParams.get("after_sequence") || 0);
      return this.eventQueue.shift() || {events: Array.from({length: Math.max(0, Math.min(100, this.current.revision - after))},
        (_, index) => ({sequence: after + index + 1, status: this.current.status, message_code: "saved-state"})), snapshot: copy(this.current)};
    }
    if (route.startsWith("/prerequisites/operations/")) return copy(this.current);
    if (route.startsWith("/errors/")) return {title: route.split("/").at(-1), description: "Contract rejection", remediation: []};
    throw new Error("Unexpected HTTP request: " + request.method + " " + request.path);
  }
  el(id) {const node = this.document.getElementById(id); assert.ok(node, "Actual HTML must contain #" + id); return node;}
  run(source) {return vm.runInContext(source, this.context);}
  async flush() {for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));}
  async click(id, {force = false} = {}) {const node = this.el(id); if (force) await node.dispatch("click"); else await node.click(); await this.flush();}
  async change(id, value) {const node = this.el(id); node.value = value; await node.dispatch("change"); await this.flush();}
  async check(node, checked = true) {node.checked = checked; await node.dispatch("change"); await this.flush();}
  async acceptReview() {
    for (const checkbox of this.el("prereq-terms").querySelectorAll("input")) {
      assert.equal(checkbox.disabled, false, "Visible complete terms must enable their own explicit checkbox");
      await this.check(checkbox);
    }
    await this.check(this.el("prereq-accept-plan"));
  }
  async tick() {const timer = this.timers.shift(); assert.ok(timer, "Polling timer must exist"); timer.callback(); await this.flush();}
  matching(method, prefix) {return this.calls.filter(call => call.method === method && call.path.startsWith(prefix));}
  mutating() {return this.calls.filter(call => call.method === "POST" && /\/prerequisites\/operations|^\/operations/.test(call.path));}
}

let cases = 0;
async function test(name, body) {try {await body(); cases++;} catch (error) {console.error("CASE FAILED: " + name); throw error;}}

async function main() {
  await test("absent trusted coordinator preserves product flow", async () => {
    const ui = new Harness({caps: {configured: false}}); await ui.flush();
    assert.equal(ui.el("prereq-section").hidden, true);
    assert.equal(ui.el("next").disabled, false);
    assert.equal(ui.mutating().length, 0);
  });
  await test("read-only initialization and explicit reviewed choices", async () => {
    const ui = new Harness(); await ui.flush();
    assert.equal(ui.el("prereq-section").hidden, false);
    assert.equal(ui.matching("POST", "/prerequisites/plans").length, 0);
    assert.equal(ui.mutating().length, 0);
    await ui.click("prereq-review");
    assert.equal(ui.matching("POST", "/prerequisites/plans").length, 1);
    const base = ui.el("prereq-choice-base"), client = ui.el("prereq-choice-client"), metrics = ui.el("prereq-choice-metrics");
    assert.ok(!base.options.some(option => option.value === "skip" && !option.disabled));
    assert.ok(base.options.some(option => option.value === "auto" && !option.disabled));
    assert.ok(!client.options.some(option => option.value === "auto" && !option.disabled));
    assert.ok(metrics.options.some(option => option.value === "skip" && !option.disabled));
    const text = ui.el("prereq-components").textContent;
    assert.ok(text.includes("1.0.0") && text.includes("2.0.0"), "Signed version limits must be visible");
    assert.equal(ui.mutating().length, 0);
  });
  await test("every displayed consent and review precedes mutation", async () => {
    const ui = new Harness(); await ui.flush(); await ui.click("prereq-review");
    await ui.click("prereq-start", {force: true});
    assert.equal(ui.mutating().length, 0, "A forged click cannot bypass explicit consent");
    const boxes = ui.el("prereq-terms").querySelectorAll("input");
    assert.ok(boxes.length >= 3, "Product and separate component terms need distinct acknowledgements");
    for (const checkbox of boxes.slice(0, -1)) await ui.check(checkbox);
    await ui.check(ui.el("prereq-accept-plan"));
    await ui.click("prereq-start", {force: true});
    assert.equal(ui.mutating().length, 0);
    await ui.check(boxes.at(-1));
    await ui.click("prereq-start");
    const actions = ui.matching("POST", "/prerequisites/operations");
    assert.equal(actions.length, 1);
    assert.equal(actions[0].path, "/prerequisites/operations");
    assert.equal(actions[0].body.accepted_plan, true); assert.equal(actions[0].body.accepted_terms, true);
    assert.deepEqual(new Set(actions[0].body.consent_refs), new Set(ui.review.plan.required_consents));
    assert.equal(ui.matching("POST", "/operations").length, 0);
  });
  await test("unknown required consent cannot be silently acknowledged", async () => {
    const value = reviewed(); value.plan.required_consents.push("unrendered-required-permission:fixture");
    const ui = new Harness({review: value}); await ui.flush(); await ui.click("prereq-review");
    await ui.acceptReview(); await ui.click("prereq-start", {force: true});
    assert.equal(ui.mutating().length, 0);
  });
  await test("automatic install, elevation and licence conditions each require fresh consent", async () => {
    const ui = new Harness(); await ui.flush(); await ui.click("prereq-review"); await ui.acceptReview();
    await ui.change("prereq-choice-base", "auto");
    assert.equal(ui.run("prereq.review"), null);
    assert.equal(ui.el("prereq-accept-plan").checked, false);
    assert.equal(ui.matching("POST", "/prerequisites/plans").length, 1, "A changed choice does not silently obtain a new review");
    await ui.click("prereq-start", {force: true}); assert.equal(ui.mutating().length, 0);
    const value = reviewed(), base = value.plan.components[0];
    base.choice = "auto"; base.elevated = true; base.adapter_policy_digest = "e".repeat(64);
    base.license.conditions = ["host-only"]; value.plan.choices.base = "auto";
    value.plan.required_consents = value.plan.required_consents.filter(ref => !ref.startsWith("prerequisite-license:base:"));
    value.plan.required_consents.push("prerequisite-license:base:" + digest(base.license),
      "prerequisite-condition:host-only:" + digest(base.license),
      "prerequisite-auto:base:" + value.plan.catalog_digest,
      "prerequisite-elevation:base:" + base.adapter_policy_digest);
    ui.review = value; await ui.click("prereq-review"); await ui.acceptReview();
    assert.equal(ui.matching("POST", "/prerequisites/plans").at(-1).body.choices.base, "auto");
    for (const kind of ["auto", "elevation", "condition"]) {
      const checkbox = ui.el("prereq-terms").querySelector('input[data-consent-kind="' + kind + '"]');
      assert.ok(checkbox, "A separate explicit consent is required for " + kind);
      await ui.check(checkbox, false); await ui.click("prereq-start", {force: true});
      assert.equal(ui.mutating().length, 0, kind + " cannot be covered by another checkbox");
      await ui.check(checkbox);
    }
    await ui.click("prereq-start");
    assert.equal(ui.mutating().length, 1);
    assert.deepEqual(new Set(ui.mutating()[0].body.consent_refs), new Set(value.plan.required_consents));
  });
  await test("version projection joins exact binding and never infers readiness", async () => {
    for (const field of ["catalog_digest", "owner_id", "product_id", "target"]) {
      const caps = capabilities(); caps.requirements[0].minimum_version = "999.123.456";
      caps[field] = field === "target" ? {...caps.target, scope: "service"} : "different-binding";
      const ui = new Harness({caps}); await ui.flush(); await ui.click("prereq-review");
      assert.ok(!ui.el("prereq-components").textContent.includes("999.123.456"), "Misbound version data must not describe another reviewed plan: " + field);
      assert.equal(ui.mutating().length, 0);
    }
    const value = reviewed(); value.plan.components[0].detected_version = "9999.0.0";
    const ui = new Harness({review: value}); await ui.flush(); await ui.click("prereq-review");
    assert.ok(!ui.el("prereq-choice-base").options.some(option => option.value === "use" && !option.disabled), "UI must honor initial_ready=false instead of comparing displayed versions");
    assert.equal(ui.mutating().length, 0);
  });
  await test("malformed or redacted detected versions are unknown in review and recovery", async () => {
    const expected = translations.en.prereq_found_version + ": " + translations.en.prereq_version_unknown;
    for (const detected_version of ["1.2\n", "[REDACTED]"]) {
      const value = reviewed(); value.plan.components[0].detected_version = detected_version;
      const ui = new Harness({review: value}); await ui.flush(); await ui.click("prereq-review");
      const card = ui.el("prereq-components").querySelector('article[data-component-id="base"]');
      assert.equal(card.querySelector("p").textContent, expected, "Malformed detected version must not be displayed as a reviewed version");
      const current = snapshot({steps: {base: {state: "AWAITING_USER"}}, checks: [{component_id: "base", ready: false, detected_version, manual: value.plan.components[0].manual}]});
      const recovered = new Harness({current, operations: [current]}); await recovered.flush();
      assert.equal(recovered.el("prereq-progress-components").querySelector("p").textContent, expected,
        "Recovery checks must apply the same strict full-string validation");
      assert.equal(ui.mutating().length + recovered.mutating().length, 0);
    }
  });
  await test("double click creates one durable start request", async () => {
    const ui = new Harness(); await ui.flush(); await ui.click("prereq-review"); await ui.acceptReview();
    const gate = deferred();
    ui.overrides.push({matches: request => request.method === "POST" && request.path === "/prerequisites/operations", handle: () => gate.promise});
    const first = ui.el("prereq-start").dispatch("click"), second = ui.el("prereq-start").dispatch("click");
    await ui.flush(); assert.equal(ui.matching("POST", "/prerequisites/operations").length, 1);
    gate.resolve(snapshot()); await Promise.all([first, second]); await ui.flush();
    assert.equal(ui.matching("POST", "/prerequisites/operations").length, 1);
  });
  await test("lost ACK and reloaded UI never resume or start implicitly", async () => {
    for (const failure of ["network", "coded-recovery"]) {
      const ui = new Harness(); await ui.flush(); await ui.click("prereq-review"); await ui.acceptReview();
      ui.overrides.push({matches: request => request.method === "POST" && request.path === "/prerequisites/operations", handle: () => {
        ui.operations = [snapshot()];
        if (failure === "network") throw new Error("Synthetic lost ACK after durable server acceptance");
        return {__error: {code: "RECOVERY_REQUIRED"}};
      }});
      await ui.click("prereq-start");
      assert.equal(ui.mutating().length, 1);
      assert.ok([...ui.storage.values()].some(value => value.includes("prep_fixture")), "Operation identity must survive an uncertain start outcome: " + failure);
      const next = new Harness({storage: Object.fromEntries(ui.storage), operations: [snapshot()]}); await next.flush();
      assert.equal(next.mutating().length, 0);
      assert.equal(next.run("prereq.operationId"), "prep_fixture");
      assert.equal(next.matching("POST", "/prerequisites/plans").length, 0);
    }
  });
  await test("READY with active or unknown controller keeps polling and blocks effects", async () => {
    for (const active of [true, null]) {
      const current = snapshot({status: "READY", ready: true, revision: 4, required_action: null, execution: {mode: "background", state: active ? "finished" : "unknown", active}});
      const storage = {"hinstall-prerequisite-operation": JSON.stringify({schema_version: 1, operation_id: current.operation_id, ...copy(identity)})};
      const ui = new Harness({current, storage}); await ui.flush();
      assert.equal(ui.run("prereq.operationId"), current.operation_id);
      assert.equal(ui.el("prereq-proceed").disabled, true);
      assert.ok(ui.timers.length > 0, "Controller finalization must keep a poll scheduled");
      await ui.click("prereq-proceed", {force: true}); await ui.click("prereq-recheck", {force: true});
      await ui.click("prereq-resume", {force: true});
      assert.equal(ui.mutating().length, 0);
      assert.equal(ui.el("prereq-cancel").disabled, false, "Cancellation remains available while controller finalizes");
      assert.ok(!ui.el("prereq-section").textContent.includes("100%"));
    }
  });
  await test("manual recheck resets product consent and never submits product", async () => {
    const current = snapshot(), ui = new Harness({current, operations: [current]}); await ui.flush();
    // Simulate a review obtained before this prerequisite operation; the real
    // recheck handler must discard it, not reuse its accepted consent.
    ui.run('plan={plan_id:"old-product-review"};terms={digest:"old-product-terms"};');
    ui.el("accept-terms").checked = ui.el("accept-plan").checked = true;
    ui.current = snapshot({status: "READY", ready: true, revision: 2, required_action: null});
    await ui.click("prereq-recheck");
    assert.equal(ui.matching("POST", "/prerequisites/operations/prep_fixture/recheck").length, 1);
    assert.equal(ui.run("plan"), null); assert.equal(ui.run("terms"), null);
    assert.equal(ui.el("accept-terms").checked, false); assert.equal(ui.el("accept-plan").checked, false);
    assert.equal(ui.matching("POST", "/operations").length, 0);
    await ui.click("prereq-proceed");
    assert.equal(ui.matching("POST", "/operations").length, 0, "READY authorizes no product installation");
  });
  await test("cancellation stays available during recheck and rejects its older READY reply", async () => {
    const current = snapshot(), ui = new Harness({current, operations: [current]}); await ui.flush();
    const gate = deferred();
    ui.overrides.push({matches: request => request.method === "POST" && request.path.endsWith("/recheck"), handle: () => gate.promise});
    ui.overrides.push({matches: request => request.method === "POST" && request.path.endsWith("/cancel"), handle: () => {
      ui.current = snapshot({status: "CANCELED", revision: 3, cancel_requested: true, required_action: null});
      return ui.current;
    }});
    const recheck = ui.el("prereq-recheck").dispatch("click"); await ui.flush();
    assert.equal(ui.matching("POST", "/prerequisites/operations/prep_fixture/recheck").length, 1);
    assert.equal(ui.el("prereq-cancel").disabled, false, "Cancel must remain usable during a slow probe");
    await ui.click("prereq-cancel");
    assert.equal(ui.matching("POST", "/prerequisites/operations/prep_fixture/cancel").length, 1);
    assert.equal(ui.run("prereq.snapshot.status"), "CANCELED");
    gate.resolve(snapshot({status: "READY", ready: true, revision: 2, required_action: null}));
    await recheck; await ui.flush();
    assert.equal(ui.run("prereq.revision"), 3); assert.equal(ui.run("prereq.snapshot.status"), "CANCELED");
    assert.equal(ui.el("prereq-proceed").hidden, true);
    assert.equal(ui.matching("POST", "/operations").length, 0);
  });
  await test("reordered status does not roll revision back or fake completion", async () => {
    const current = snapshot({status: "RUNNING", revision: 4, execution: {mode: "background", state: "running", active: true}});
    const ui = new Harness({current, operations: [current]}); await ui.flush();
    assert.equal(ui.run("prereq.revision"), 4);
    ui.eventQueue.push({events: [{sequence: 2, status: "READY", message_code: "stale-event"}], snapshot: snapshot({status: "READY", ready: true, revision: 3})});
    await ui.tick();
    assert.equal(ui.run("prereq.revision"), 4); assert.equal(ui.run("prereq.snapshot.status"), "RUNNING");
    assert.equal(ui.el("prereq-proceed").disabled, true);
    assert.ok(!ui.el("prereq-section").textContent.includes("100%"));
    ui.eventQueue.push({events: [{sequence: 5, status: "REBOOT_REQUIRED", message_code: "needs-reboot"}], snapshot: snapshot({status: "REBOOT_REQUIRED", revision: 5, required_action: "choose_reboot"})});
    await ui.tick(); assert.equal(ui.run("prereq.revision"), 5);
  });
  await test("successful refresh restarts polling while an older error explanation is pending", async () => {
    const current = snapshot({status: "RUNNING", revision: 4, execution: {mode: "background", state: "running", active: true}});
    const ui = new Harness({current, operations: [current]}), errorExplanation = deferred(); let failed = false;
    ui.overrides.push({matches: request => request.path.includes("/events?") && !failed, handle: () => {
      failed = true; throw new Error("Synthetic event stream disconnect");
    }});
    ui.overrides.push({matches: request => request.path.startsWith("/errors/"), handle: () => errorExplanation.promise});
    await ui.flush(); assert.equal(ui.run("prereq.connectionLost"), true);
    const before = ui.calls.filter(call => call.path.includes("/events?")).length;
    await ui.click("prereq-refresh");
    assert.equal(ui.run("prereq.connectionLost"), false);
    errorExplanation.resolve({title: "Disconnected", description: "Synthetic old error", remediation: []});
    await ui.flush();
    const after = ui.calls.filter(call => call.path.includes("/events?")).length;
    assert.ok(after > before || ui.timers.length > 0, "Successful refresh must not lose its poll to an older error handler");
    ui.current = snapshot({status: "READY", ready: true, revision: 5, required_action: null});
    await ui.tick();
    assert.equal(ui.run("prereq.snapshot.status"), "READY");
    assert.equal(ui.mutating().length, 0);
  });
  await test("delayed plan cannot restore review after mode or language changes", async () => {
    for (const change of ["mode", "language"]) {
      const ui = new Harness(); await ui.flush(); const gate = deferred();
      ui.overrides.push({matches: request => request.method === "POST" && request.path === "/prerequisites/plans", handle: () => gate.promise});
      const reviewing = ui.el("prereq-review").dispatch("click"); await ui.flush();
      assert.equal(ui.matching("POST", "/prerequisites/plans").length, 1);
      await ui.change(change, change === "mode" ? "docker" : "de");
      gate.resolve(reviewed()); await reviewing; await ui.flush();
      const review = ui.run("prereq.review");
      if (change === "mode" && ui.el("mode").value === "native") {
        // Refusing a change while an outstanding request owns the UI is also
        // safe, provided the DOM target and accepted plan stay in agreement.
        if (review) assert.equal(review.plan.target.mode, "native");
      } else assert.equal(review, null, "An obsolete response must not restore a valid review");
      assert.equal(ui.mutating().length, 0);
      assert.equal(ui.el("prereq-accept-plan").checked, false);
    }
  });
  await test("out-of-order language responses preserve latest user language", async () => {
    const ui = new Harness(); await ui.flush(); const earlier = deferred();
    ui.overrides.push({matches: request => request.path === "/locales/ru", handle: () => earlier.promise});
    ui.el("language").value = "ru"; const russian = ui.el("language").dispatch("change"); await ui.flush();
    await ui.change("language", "de"); earlier.resolve(translations.ru); await russian; await ui.flush();
    assert.equal(ui.run("locale"), "de");
    assert.equal(ui.document.documentElement.lang, "de");
    assert.equal(ui.run("messages.title"), translations.de.title, "Older language response must not relabel current reviewed controls");
    assert.equal(ui.mutating().length, 0);
  });
  await test("replacement keeps original operation and requires separate resume", async () => {
    const current = snapshot({required_action: "replan"}), value = reviewed({operation_id: "prep_unused_review"});
    const ui = new Harness({current, review: value, operations: [current]}); await ui.flush();
    await ui.click("prereq-replan"); await ui.click("prereq-review"); await ui.acceptReview();
    await ui.click("prereq-start");
    const calls = ui.mutating(); assert.equal(calls.length, 1);
    assert.equal(calls[0].path, "/prerequisites/operations/prep_fixture/replace-plan");
    assert.equal(ui.run("prereq.operationId"), "prep_fixture");
    assert.equal(ui.matching("POST", "/prerequisites/operations/prep_fixture/resume").length, 0);
  });
  await test("reboot boundary records intent and never claims performed", async () => {
    const current = snapshot({status: "REBOOT_REQUIRED", required_action: "choose_reboot"});
    const ui = new Harness({current, operations: [current]}); await ui.flush();
    ui.overrides.push({matches: request => request.path.endsWith("/boundary"), handle: request => snapshot({status: "REBOOT_REQUIRED", revision: 2, host_action: {action: "reboot", when: request.body.when, performed: false}})});
    await ui.click("prereq-now"); await ui.click("prereq-later");
    const calls = ui.mutating(); assert.equal(calls.length, 2);
    assert.deepEqual(calls.map(call => call.body), [{when: "now"}, {when: "later"}]);
    assert.ok(calls.every(call => call.path === "/prerequisites/operations/prep_fixture/boundary"));
    assert.equal(ui.run("prereq.snapshot.host_action.performed"), false);
    assert.equal(ui.run("prereq.snapshot.status"), "REBOOT_REQUIRED");
  });
  await test("response text is inert and cannot invent 100 percent progress", async () => {
    const value = reviewed(), injection = '<img src=x onerror="globalThis.injected=true">';
    const badVersion = '<svg onload="globalThis.versionInjected=true">';
    value.plan.components[0].manual.steps = [injection]; value.plan.components[0].manual.official_url = "javascript:alert(1)";
    value.plan.components[0].detected_version = badVersion;
    const ui = new Harness({review: value}); await ui.flush(); await ui.click("prereq-review");
    assert.ok(ui.el("prereq-components").textContent.includes(injection));
    assert.ok(!ui.el("prereq-components").textContent.includes(badVersion));
    assert.equal(ui.el("prereq-section").querySelectorAll("img").length, 0);
    assert.equal(ui.el("prereq-section").querySelectorAll("svg").length, 0);
    assert.equal(ui.run("globalThis.injected"), undefined);
    assert.equal(ui.run("globalThis.versionInjected"), undefined);
    for (const link of ui.el("prereq-section").querySelectorAll("a")) assert.ok(!String(link.href || "").startsWith("javascript:"));
    assert.ok(!ui.el("prereq-section").textContent.includes("100%"));
    for (const progress of ui.el("prereq-section").querySelectorAll("progress")) assert.ok(!progress.hasAttribute("value") || Number(progress.value) < 100);
    assert.equal(ui.mutating().length, 0);
  });
  await test("settled controller drains saved event pages without skipping to snapshot revision", async () => {
    const current = snapshot({revision: 150});
    const ui = new Harness({current, operations: [current]});
    const events = Array.from({length: 150}, (_, index) => ({sequence: index + 1,
      status: "AWAITING_USER", message_code: "saved-progress-" + (index + 1)}));
    ui.eventQueue.push({events: events.slice(0, 100), snapshot: current},
      {events: events.slice(100), snapshot: current});
    await ui.flush();
    assert.equal(ui.run("prereq.cursor"), 100, "Snapshot revision is not an event cursor");
    assert.ok(ui.timers.length > 0, "A settled controller still needs to deliver the remaining event page");
    await ui.tick();
    assert.equal(ui.run("prereq.cursor"), 150);
    assert.ok(ui.el("prereq-events").textContent.includes("saved-progress-150"));
    assert.ok(ui.calls.some(call => call.path.includes("after_sequence=100&")));
    assert.equal(ui.mutating().length, 0);
  });
  await test("measured bytes restore without turning file progress into component readiness", async () => {
    const current = downloading(), storage = {"hinstall-prerequisite-operation": JSON.stringify({schema_version: 1,
      operation_id: current.operation_id, ...copy(identity)})};
    const ui = new Harness({current, storage}); await ui.flush();
    assert.equal(ui.el("prereq-download").hidden, false);
    assert.equal(Number(ui.el("prereq-download-progress").value), 50);
    assert.ok(ui.el("prereq-download").textContent.includes("512 / 1,024 bytes"));
    assert.ok(ui.el("prereq-download").textContent.includes("base-installer"));
    assert.equal(ui.el("prereq-progress").hasAttribute("value"), false);
    assert.equal(ui.el("prereq-proceed").hidden, true);
    assert.equal(ui.mutating().length, 0);
  });
  await test("full but unverified bytes remain indeterminate and verified file stays separate", async () => {
    for (const state of ["verifying", "verified"]) {
      const current = downloading({download_progress: download({state, completed: 1024,
        verified: state === "verified", percent: state === "verified" ? 100 : null})});
      const ui = new Harness({current, operations: [current]}); await ui.flush();
      assert.equal(ui.el("prereq-download-progress").hasAttribute("value"), state === "verified");
      assert.equal(ui.el("prereq-download").textContent.includes("100%"), state === "verified");
      assert.equal(ui.el("prereq-progress").hasAttribute("value"), false);
      assert.equal(ui.el("prereq-proceed").hidden, true);
      assert.ok(ui.el("prereq-download").textContent.includes(translations.en.prereq_transfer_help));
      assert.equal(ui.mutating().length, 0);
    }
  });
  await test("verified progress with unknown source cannot render 100 percent", async () => {
    const current = downloading({download_progress: download({state: "verified", completed: 1024,
      verified: true, percent: 100, source: "unknown"})});
    const ui = new Harness({current, operations: [current]}); await ui.flush();
    assert.equal(ui.el("prereq-download-progress").hasAttribute("value"), false,
      "Verified download with unknown source must remain indeterminate");
    assert.ok(ui.el("prereq-download").textContent.includes(translations.en.prereq_transfer_unknown));
    assert.ok(!ui.el("prereq-download").textContent.includes("100%"));
    assert.equal(ui.mutating().length, 0);
  });
  await test("retry and next artifact reset measured bytes and ignore older snapshots", async () => {
    const current = downloading(), ui = new Harness({current, operations: [current]}); await ui.flush();
    ui.current = downloading({revision: 9, download_progress: download({sequence: 9, state: "retrying", attempt: 1,
      completed: 128, percent: null})});
    await ui.click("prereq-refresh");
    assert.equal(ui.el("prereq-download-progress").hasAttribute("value"), false);
    assert.ok(ui.el("prereq-download").textContent.includes("128 / 1,024 bytes"));
    ui.eventQueue.push({events: [], snapshot: current}); await ui.tick();
    assert.equal(ui.run("prereq.revision"), 9);
    assert.ok(!ui.el("prereq-download").textContent.includes("512 / 1,024 bytes"));
    ui.current = downloading({revision: 10, steps: {base: {state: "READY"}, client: {state: "DOWNLOADING"}},
      download_progress: download({sequence: 10, transfer_id: "f".repeat(32), component_id: "client",
        artifact_id: "client-installer", completed: 0, percent: 0})});
    await ui.click("prereq-refresh");
    assert.equal(Number(ui.el("prereq-download-progress").value), 0);
    assert.ok(ui.el("prereq-download").textContent.includes("client-installer"));
    assert.ok(!ui.el("prereq-download").textContent.includes("base-installer"));
    assert.equal(ui.mutating().length, 0);
  });
  await test("invalid progress bindings or counters cannot render a measured or completed bar", async () => {
    const invalid = [{completed: true}, {completed: -1}, {completed: 1.5}, {completed: 1025}, {total: 0},
      {total: 64 * 1024**3 + 1}, {total: Number.MAX_SAFE_INTEGER + 1}, {percent: 100}, {verified: true},
      {percent: 49}, {plan_digest: "another-plan"}, {component_id: "unbound"}, {sequence: 9}, {sequence: false},
      {state: "unknown"}, {source: "secret-url"}, {resumed: "yes"}, {attempt: 8}, {attempt: false},
      {transfer_id: "bad"}, {artifact_sha256: "bad"}, {artifact_id: "<img src=x onerror=alert(1)>"},
      {state: "verified", completed: 512, percent: 100, verified: true},
      {state: "transferring", completed: 1024, percent: 99}];
    for (const changes of invalid) {
      const current = downloading({download_progress: download(changes)});
      const ui = new Harness({current, operations: [current]}); await ui.flush();
      assert.equal(ui.el("prereq-download-progress").hasAttribute("value"), false, JSON.stringify(changes));
      assert.ok(ui.el("prereq-download").textContent.includes(translations.en.prereq_transfer_unknown));
      assert.ok(!ui.el("prereq-download").textContent.includes("100%"));
      assert.equal(ui.el("prereq-download").querySelectorAll("img").length, 0);
      assert.equal(ui.mutating().length, 0);
    }
  });
  await test("legacy snapshots hide download data and cancellation keeps partial bytes honest", async () => {
    const legacy = new Harness({current: snapshot(), operations: [snapshot()]}); await legacy.flush();
    assert.equal(legacy.el("prereq-download").hidden, true);
    for (const state of ["failed", "canceled"]) {
      const current = downloading({status: state === "failed" ? "RECOVERY_REQUIRED" : "CANCELED",
        execution: {mode: "background", state: "finished", active: false},
        download_progress: download({state, percent: null})});
      const storage = {"hinstall-prerequisite-operation": JSON.stringify({schema_version: 1,
        operation_id: current.operation_id, ...copy(identity)})};
      const ui = new Harness({current, storage}); await ui.flush();
      assert.equal(ui.el("prereq-download-progress").hasAttribute("value"), false);
      assert.ok(ui.el("prereq-download").textContent.includes("512 / 1,024 bytes"));
      assert.ok(ui.el("prereq-download").textContent.includes(translations.en["prereq_transfer_" + state]));
      assert.equal(ui.mutating().length, 0);
    }
  });
  await test("disconnect labels saved measurements and cannot expose private download fields", async () => {
    const current = downloading({download_progress: download({url: "https://secret.invalid/?token=private",
      local_path: "/private/secret", etag: "secret-etag"})});
    const ui = new Harness({current, operations: [current]});
    ui.overrides.push({matches: request => request.path.includes("/events?"), handle: () => {throw new Error("Synthetic disconnect");}});
    await ui.flush();
    assert.ok(ui.el("prereq-download").textContent.includes(translations.en.prereq_transfer_saved));
    assert.equal(Number(ui.el("prereq-download-progress").value), 50);
    assert.ok(!ui.el("prereq-section").textContent.includes("secret"));
    assert.equal(ui.mutating().length, 0);
  });
  await test("restored download status follows each selected language without resuming", async () => {
    const current = downloading(), ui = new Harness({current, operations: [current]}); await ui.flush();
    for (const lang of ["ru", "de", "en"]) {
      await ui.change("language", lang);
      assert.ok(ui.el("prereq-download").textContent.includes(translations[lang].prereq_transfer_title));
      assert.ok(ui.el("prereq-download").textContent.includes(translations[lang].prereq_transfer_transferring));
      assert.equal(ui.el("prereq-download-progress").getAttribute("aria-label"), translations[lang].prereq_transfer_title);
      assert.equal(Number(ui.el("prereq-download-progress").value), 50);
    }
    assert.equal(ui.mutating().length, 0);
  });
  await test("duplicate event page keeps the received cursor while a newer snapshot is visible", async () => {
    const current = downloading({revision: 120, execution: {mode: "background", state: "finished", active: false},
      status: "AWAITING_USER", download_progress: download({sequence: 119})});
    const ui = new Harness({current, operations: [current]});
    const events = Array.from({length: 120}, (_, index) => ({sequence: index + 1, message_code: "saved-event"}));
    ui.eventQueue.push({events: events.slice(0, 100), snapshot: current},
      {events: events.slice(0, 100), snapshot: current}, {events: events.slice(100), snapshot: current});
    await ui.flush(); assert.equal(ui.run("prereq.cursor"), 100);
    assert.equal(Number(ui.el("prereq-download-progress").value), 50);
    await ui.tick(); assert.equal(ui.run("prereq.cursor"), 100);
    await ui.tick(); assert.equal(ui.run("prereq.cursor"), 120);
    assert.equal(ui.mutating().length, 0);
  });
  const assets = Object.fromEntries(["app.js", "index.html", "locales.json"].map(name => [name,
    createHash("sha256").update(assetBytes[name]).digest("hex")]));
  console.log(JSON.stringify({status: "PASS", cases, scope: "actual wizard JavaScript with injected DOM/HTTP; not browser acceptance", assets_sha256: assets}));
}
const watchdog = setTimeout(() => {console.error("Contract handler did not finish within 20 seconds"); process.exit(1);}, 20000);
main().catch(error => {console.error(error.stack || error); process.exitCode = 1;}).finally(() => clearTimeout(watchdog));
