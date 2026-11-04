// HomenS.AI · © 2026 Serhii Khomenko — страница сборщика: проекты, ключи, сборка, проверка и скачивание комплектов.
"use strict";

(() => {
  const $ = id => document.getElementById(id);
  let overview = null;
  let after = 0;
  let polling = false;
  let shownLast = false;

  function cell(label, children) {
    return HSI.el("td", { "data-label": label }, children);
  }

  function renderProjects() {
    const body = $("projects");
    const projects = overview.projects;
    $("projects-empty").hidden = projects.length > 0;
    $("projects-table").hidden = projects.length === 0;
    body.replaceChildren(...projects.map(project => {
      let state;
      if (!project.configured) state = HSI.badge("info", HSI.t("projects.not_configured"));
      else if (project.ready) state = HSI.el("span", {}, [HSI.badge("ok", HSI.t("projects.ready")), HSI.el("span", { class: "sha", text: HSI.t("projects.wheels", { count: project.wheels }) })]);
      else state = HSI.el("span", {}, [HSI.badge("warn", HSI.t("projects.not_ready")), HSI.el("span", { class: "sha", text: project.problem || "" })]);
      const actions = HSI.el("div", { class: "actions" }, project.configured ? [
        HSI.el("button", { class: "btn secondary sm", type: "button", text: HSI.t("projects.fetch"),
          onclick: () => start("POST", "/api/fetch", { project: project.id }) }),
      ] : []);
      return HSI.el("tr", {}, [
        cell(HSI.t("col.project"), HSI.el("span", { class: "mono", text: project.id })),
        cell(HSI.t("col.product"), project.product_name ? project.product_name + " (" + project.product_id + ")" : "—"),
        cell(HSI.t("col.targets"), (project.targets || []).join(", ") || "—"),
        cell(HSI.t("col.state"), state),
        cell(HSI.t("col.actions"), actions),
      ]);
    }));
    const configured = projects.filter(project => project.configured);
    const select = $("build-project");
    const previous = select.value;
    select.replaceChildren(...configured.map(project => HSI.el("option", { value: project.id,
      text: (project.product_name || project.id) + " — " + project.id })));
    if (configured.some(project => project.id === previous)) select.value = previous;
    const free = projects.filter(project => !project.configured);
    $("init-none").hidden = free.length > 0;
    $("init-form").hidden = free.length === 0;
    $("init-project").replaceChildren(...free.map(project => HSI.el("option", { value: project.id, text: project.id })));
  }

  function renderKeys() {
    const keys = overview.keys;
    $("keys-empty").hidden = keys.length > 0;
    $("keys").replaceChildren(...keys.map(key => HSI.el("li", {}, [
      HSI.el("b", { class: "mono", text: key.name }), " ",
      key.encrypted ? HSI.badge("ok", HSI.t("keys.encrypted")) : HSI.badge("warn", HSI.t("keys.plain")),
      HSI.el("span", { class: "sha", text: key.public_key_sha256 || "—" }),
    ])));
    const select = $("build-key");
    const previous = select.value;
    select.replaceChildren(...keys.map(key => HSI.el("option", { value: key.name, text: key.name })));
    if (keys.some(key => key.name === previous)) select.value = previous;
    $("build-no-key").hidden = keys.length > 0;
    updatePassphrase();
  }

  function updatePassphrase() {
    const key = (overview ? overview.keys : []).find(item => item.name === $("build-key").value);
    $("build-passphrase-wrap").hidden = !(key && key.encrypted);
  }

  function renderKits() {
    const kits = overview.kits.slice().sort((a, b) => b.time - a.time);
    $("kits-empty").hidden = kits.length > 0;
    $("kits-table").hidden = kits.length === 0;
    $("kits").replaceChildren(...kits.map(kit => {
      const sha = HSI.el("span", { class: "sha" });
      const actions = [
        HSI.el("button", { class: "btn secondary sm", type: "button", text: HSI.t("kits.verify"),
          onclick: () => start("POST", "/api/verify", { kit: kit.name, expect_key_sha256: kit.public_key_sha256 }) }),
      ];
      if (kit.archive) {
        actions.push(HSI.el("button", { class: "btn secondary sm", type: "button", text: HSI.t("kits.sha"),
          onclick: async () => {
            try {
              sha.textContent = (await HSI.json("GET", "/api/checksum?name=" + encodeURIComponent(kit.archive))).sha256;
            } catch (error) { HSI.showError(error); }
          } }));
        actions.push(HSI.el("button", { class: "btn sm", type: "button", text: HSI.t("kits.download"),
          onclick: () => download(kit.archive) }));
      }
      return HSI.el("tr", {}, [
        cell(HSI.t("col.kit"), [HSI.el("b", { class: "mono", text: kit.name }), HSI.el("span", { class: "sha", text: (kit.targets || []).join(", ") })]),
        cell(HSI.t("col.key"), HSI.el("span", { class: "sha", text: kit.public_key_sha256 || "—" })),
        cell(HSI.t("col.size"), HSI.size(kit.archive_bytes)),
        cell(HSI.t("col.actions"), [HSI.el("div", { class: "actions" }, actions), sha]),
      ]);
    }));
  }

  async function download(name) {
    try {
      const response = await HSI.api("GET", "/api/download?name=" + encodeURIComponent(name));
      const url = URL.createObjectURL(await response.blob());
      const link = HSI.el("a", { href: url, download: name });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (error) {
      HSI.showError(error);
    }
  }

  async function refresh() {
    try {
      overview = await HSI.json("GET", "/api/overview");
    } catch (error) {
      if (error.code === "token") { showLogin(); return; }
      HSI.showError(error);
      return;
    }
    $("login").hidden = true;
    $("workspace").hidden = false;
    renderProjects();
    renderKeys();
    renderKits();
    $("build-run").disabled = polling || overview.keys.length === 0;
    if (overview.job && !polling) follow();
    else if (!shownLast && !polling) { shownLast = true; poll(); }  // журнал последней команды
  }

  function showLogin() {
    $("workspace").hidden = true;
    $("login").hidden = false;
    $("login-token").focus();
  }

  async function start(method, path, body) {
    HSI.clearError();
    try {
      await HSI.json(method, path, body);
    } catch (error) {
      HSI.showError(error);
      return false;
    }
    after = 0;
    $("log").replaceChildren();
    follow();
    return true;
  }

  function follow() {
    polling = true;
    $("build-run").disabled = true;
    HSI.setBadge($("job-badge"), "info", HSI.t("run.running"));
    poll();
  }

  async function poll() {
    let job;
    try {
      job = await HSI.json("GET", "/api/progress?after=" + after);
    } catch (error) {
      HSI.showError(error);
      setTimeout(poll, 3000);
      return;
    }
    $("job-name").textContent = job.title || HSI.t("job.idle");
    const log = $("log");
    for (const line of job.lines) log.append(HSI.el("li", { text: line }));
    if (job.lines.length) log.scrollTop = log.scrollHeight;
    after = job.next;
    if (job.running) { setTimeout(poll, 1000); return; }
    polling = false;
    if (job.code === 0) HSI.setBadge($("job-badge"), "ok", HSI.t("run.ok"));
    else if (job.code !== null) HSI.setBadge($("job-badge"), "bad", HSI.t("run.failed", { code: job.code }));
    refresh();
  }

  function bind() {
    HSI.bindChrome();
    $("language").value = HSI.language;
    $("language").addEventListener("change", event => { HSI.setLanguage(event.target.value); if (overview) refresh(); });
    $("login-form").addEventListener("submit", event => {
      event.preventDefault();
      HSI.setToken($("login-token").value.trim());
      $("login-token").value = "";
      refresh();
    });
    $("build-key").addEventListener("change", updatePassphrase);
    $("build-form").addEventListener("submit", async event => {
      event.preventDefault();
      const body = { project: $("build-project").value, version: $("build-version").value.trim(),
        sequence: $("build-sequence").value.trim() || null, key: $("build-key").value };
      if (!$("build-passphrase-wrap").hidden) body.passphrase = $("build-passphrase").value;
      if (await start("POST", "/api/build", body)) $("build-passphrase").value = "";
    });
    $("keygen-form").addEventListener("submit", async event => {
      event.preventDefault();
      const body = { name: $("keygen-name").value.trim(), passphrase: $("keygen-passphrase").value || null };
      if (await start("POST", "/api/keygen", body)) $("keygen-passphrase").value = "";
    });
    $("init-form").addEventListener("submit", event => {
      event.preventDefault();
      start("POST", "/api/init", { project: $("init-project").value, product_id: $("init-id").value.trim(),
        product_name: $("init-name").value.trim(), port: Number($("init-port").value) });
    });
  }

  document.addEventListener("DOMContentLoaded", async () => {
    bind();
    try { await HSI.loadLanguages("ru"); } catch (error) { /* русские подписи разметки */ }
    $("language").value = HSI.language;
    if (HSI.token) refresh(); else showLogin();
  });
})();
