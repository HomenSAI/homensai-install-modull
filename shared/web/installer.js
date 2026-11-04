// HomenS.AI · © 2026 Serhii Khomenko — страница установщика: сведения о комплекте и установке,
// запуск команд, журнал и вопросы установщика (подтверждение, пароль архива, секреты).
"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const TABS = ["install", "update", "secrets", "uninstall", "status"];
  let info = null;
  let kitPath = null;
  let after = 0;
  let polling = false;
  let questionId = null;

  function selectTab(name) {
    for (const tab of TABS) {
      const selected = tab === name;
      $("tab-" + tab).setAttribute("aria-selected", String(selected));
      $("tab-" + tab).tabIndex = selected ? 0 : -1;
      $("panel-" + tab).hidden = !selected;
    }
  }

  function renderInfo() {
    const kit = info.kit;
    $("kit-error").hidden = !info.error;
    $("kit-error").textContent = info.error || "";
    $("kit-path").value = info.kit_path;
    $("kit-product").textContent = kit ? kit.product_name + " (" + kit.product_id + ")" : "—";
    $("kit-version").textContent = kit ? kit.version : "—";
    $("kit-targets").textContent = kit && kit.targets ? kit.targets.join(", ") : "—";
    $("kit-key").textContent = kit ? kit.public_key_sha256 : "—";

    const state = info.state;
    const badge = $("state-badge");
    if (!kit) badge.hidden = true;
    else if (!state || state.status === null) HSI.setBadge(badge, "info", HSI.t("state.none"));
    else if (state.interrupted) HSI.setBadge(badge, "warn", HSI.t("state.interrupted"));
    else if (state.status === "installed") HSI.setBadge(badge, "ok", HSI.t("state.installed"));
    else if (state.status === "uninstalled") HSI.setBadge(badge, "info", HSI.t("state.uninstalled"));
    else HSI.setBadge(badge, "warn", HSI.t("state.other") + ": " + state.status);
    $("state-version").textContent = state && state.version ? state.version : "—";
    $("state-root").textContent = state && state.install_root ? state.install_root : "—";
    $("state-modes").textContent = state && state.modes ? state.modes.join(", ") : "—";
    $("state-stored").textContent = info.stored_installer || "—";
    $("stored-note").hidden = !info.stored_installer;

    const modes = $("install-modes");
    modes.querySelectorAll("label").forEach(node => node.remove());
    if (kit && kit.all_modes) {
      for (const mode of kit.all_modes) {
        const box = HSI.el("input", { type: "checkbox", value: mode, checked: (kit.modes || []).includes(mode) });
        modes.append(HSI.el("label", { class: "check" }, [box, HSI.el("span", { text: mode })]));
      }
    }
    const secrets = (kit && kit.secrets) || [];
    $("secrets-none").hidden = secrets.length > 0;
    $("secrets-edit").hidden = secrets.length === 0;
    $("secrets-name").replaceChildren(...secrets.map(name => HSI.el("option", { value: name, text: name })));

    const installed = state && state.status === "installed";
    const busy = polling;
    $("run-install").disabled = busy || !kit || installed;
    $("run-update").disabled = busy || !kit || !(installed || (state && state.status === "updating") || (state && state.status === "consolidating"));
    for (const id of ["run-secrets-list", "run-secrets-set", "run-secrets-clear", "run-status", "run-uninstall"]) {
      $(id).disabled = busy || !kit || !state || state.status === null;
    }
    if (!state || state.status === "uninstalled") selectTab("install");
    else if (installed && $("tab-install").getAttribute("aria-selected") === "true") selectTab("update");
  }

  async function loadInfo() {
    const query = new URLSearchParams(HSI.language ? { language: HSI.language } : {});
    if (kitPath) query.set("kit", kitPath);
    try {
      info = await HSI.json("GET", "/api/info?" + query.toString());
      renderInfo();
      HSI.clearError();
    } catch (error) {
      HSI.showError(error);
    }
  }

  // --- выполнение команды ------------------------------------------------------------
  function command(name, options) {
    const body = Object.assign({ command: name, language: HSI.language }, options || {});
    if (kitPath) body.kit = kitPath;
    return body;
  }

  async function run(body) {
    HSI.clearError();
    try {
      await HSI.json("POST", "/api/run", body);
    } catch (error) {
      HSI.showError(error);
      return;
    }
    after = 0;
    $("log").replaceChildren();
    $("run-card").hidden = false;
    HSI.setBadge($("run-badge"), "info", HSI.t("run.running"));
    $("run-card").scrollIntoView({ block: "start" });
    polling = true;
    renderInfo();
    poll();
  }

  async function poll() {
    let snapshot;
    try {
      snapshot = await HSI.json("GET", "/api/progress?after=" + after);
    } catch (error) {
      HSI.showError(error);
      if (error.code === "network") { polling = false; return; }
      setTimeout(poll, 2000);
      return;
    }
    const log = $("log");
    for (const line of snapshot.lines) {
      log.append(HSI.el("li", { class: line.kind === "error" ? "error" : null, text: line.text }));
    }
    if (snapshot.lines.length) log.scrollTop = log.scrollHeight;
    after = snapshot.next;
    showQuestion(snapshot.prompt);
    if (snapshot.running) {
      if (!snapshot.prompt) HSI.setBadge($("run-badge"), "info", HSI.t("run.running"));
      setTimeout(poll, snapshot.prompt ? 1000 : 600);
      return;
    }
    polling = false;
    const code = snapshot.result ? snapshot.result.code : 1;
    if (code === 0) HSI.setBadge($("run-badge"), "ok", HSI.t("run.ok"));
    else if (code === 3) HSI.setBadge($("run-badge"), "warn", HSI.t("run.cancelled"));
    else HSI.setBadge($("run-badge"), "bad", HSI.t("run.failed", { code }));
    loadInfo();
  }

  function showQuestion(prompt) {
    const form = $("question");
    if (!prompt) {
      form.hidden = true;
      questionId = null;
      return;
    }
    if (prompt.id === questionId) return;
    questionId = prompt.id;
    HSI.setBadge($("run-badge"), "warn", HSI.t("run.waiting"));
    $("question-text").textContent = prompt.text;
    const confirm = prompt.kind === "confirm";
    const secret = prompt.kind === "secret";
    $("question-field").hidden = confirm;
    $("question-yes").hidden = !confirm;
    $("question-no").hidden = !confirm;
    $("question-submit").hidden = confirm;
    $("question-skip").hidden = !secret;
    $("question-cancel").hidden = confirm;
    $("question-show-wrap").hidden = !secret;
    $("question-show").checked = false;
    const input = $("question-input");
    input.type = secret ? "password" : "text";
    input.autocomplete = secret ? "new-password" : "off";
    input.value = "";
    form.hidden = false;
    (confirm ? $("question-yes") : input).focus();
  }

  async function answer(value, cancel) {
    const id = questionId;
    if (id === null) return;
    $("question").hidden = true;
    $("question-input").value = "";
    try {
      await HSI.json("POST", "/api/answer", cancel ? { id, cancel: true } : { id, value });
    } catch (error) {
      HSI.showError(error);
    }
  }

  // --- привязка ----------------------------------------------------------------------
  function bind() {
    HSI.bindChrome();
    for (const tab of TABS) $("tab-" + tab).addEventListener("click", () => selectTab(tab));
    $("tab-install").parentElement.addEventListener("keydown", event => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      const current = TABS.findIndex(tab => $("tab-" + tab).getAttribute("aria-selected") === "true");
      const next = TABS[(current + (event.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length];
      selectTab(next);
      $("tab-" + next).focus();
    });
    $("language").addEventListener("change", event => { HSI.setLanguage(event.target.value); loadInfo(); });
    $("kit-open").addEventListener("click", () => { kitPath = $("kit-path").value.trim() || null; loadInfo(); });
    $("kit-reset").addEventListener("click", () => { kitPath = null; loadInfo(); });

    $("run-install").addEventListener("click", () => {
      const modes = [...document.querySelectorAll("#install-modes input:checked")].map(box => box.value);
      run(command("install", {
        root: $("install-root").value.trim() || null, modes: modes.length ? modes : null,
        expect_key_sha256: $("install-expect").value.trim().toLowerCase() || null }));
    });
    $("run-update").addEventListener("click", () => run(command("update", {
      accept_key_sha256: $("update-accept").value.trim().toLowerCase() || null,
      allow_downgrade: $("update-downgrade").checked, reconcile_stop: $("update-reconcile").checked })));
    $("run-secrets-list").addEventListener("click", () => run(command("secrets")));
    $("run-secrets-set").addEventListener("click", () => run(command("secrets", { name: $("secrets-name").value })));
    $("run-secrets-clear").addEventListener("click", () => run(command("secrets", { name: $("secrets-name").value, clear: true })));
    $("run-status").addEventListener("click", () => run(command("status")));
    $("run-uninstall").addEventListener("click", () => {
      const purge = document.querySelector("input[name=purge]:checked").value === "purge";
      run(command("uninstall", { purge }));
    });

    $("question").addEventListener("submit", event => { event.preventDefault(); answer($("question-input").value, false); });
    $("question-yes").addEventListener("click", () => answer("yes", false));
    $("question-no").addEventListener("click", () => answer("no", false));
    $("question-skip").addEventListener("click", () => answer("", false));
    $("question-cancel").addEventListener("click", () => answer("", true));
    $("question-show").addEventListener("change", event => { $("question-input").type = event.target.checked ? "text" : "password"; });

    $("quit").addEventListener("click", async () => {
      try {
        await HSI.json("POST", "/api/quit", {});
        document.querySelector("main").replaceChildren(HSI.el("section", { class: "card" }, [HSI.el("p", { text: HSI.t("quit.done") })]));
        $("quit").disabled = true;
      } catch (error) {
        HSI.showError(error);
      }
    });
  }

  async function start() {
    bind();
    await loadInfo();
    try {
      await HSI.loadLanguages(info ? info.language : null);
    } catch (error) { /* без переводов остаются русские подписи разметки */ }
    if (info && info.language !== HSI.language) await loadInfo();
    else if (info) renderInfo();
    if (info) {
      const select = $("language");
      const names = { ru: "Русский", en: "English", de: "Deutsch" };
      select.replaceChildren(...info.languages.filter(code => names[code]).map(code => HSI.el("option", { value: code, text: names[code] })));
      select.value = HSI.language;
    }
    // Продолжить показ операции, начатой до перезагрузки вкладки.
    try {
      const snapshot = await HSI.json("GET", "/api/progress?after=0");
      if (snapshot.running || snapshot.result) {
        $("run-card").hidden = false;
        polling = snapshot.running;
        poll();
      }
    } catch (error) { /* ошибка уже показана */ }
  }

  document.addEventListener("DOMContentLoaded", start);
})();
