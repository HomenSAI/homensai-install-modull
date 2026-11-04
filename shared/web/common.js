// HomenS.AI · © 2026 Serhii Khomenko — общее для страниц установщика и сборщика:
// токен сеанса, запросы к /api/, переводы lang.json, тема, мелкие построители элементов.
// Подключается в <head> без defer: тема применяется до отрисовки.
"use strict";

const HSI = (() => {
  const THEME_KEY = "homensai.theme";
  const TOKEN_KEY = "homensai.token";
  const LANG_KEY = "homensai.lang";

  function storage(kind, key, value) {
    try {
      const store = kind === "session" ? window.sessionStorage : window.localStorage;
      if (value === undefined) return store.getItem(key);
      if (value === null) store.removeItem(key); else store.setItem(key, value);
    } catch (e) { /* хранилище недоступно — работаем без него */ }
    return null;
  }

  // Тема: выбор пользователя, иначе системная.
  const savedTheme = storage("local", THEME_KEY);
  if (savedTheme === "light" || savedTheme === "dark") document.documentElement.setAttribute("data-theme", savedTheme);

  // Токен приходит во фрагменте адреса (#t=…): фрагмент не уходит на сервер и в Referer;
  // после чтения он убирается из адресной строки и живёт только в sessionStorage вкладки.
  let token = null;
  const match = /^#t=([A-Za-z0-9_-]{24,128})$/.exec(window.location.hash);
  if (match) {
    token = match[1];
    storage("session", TOKEN_KEY, token);
    history.replaceState(null, "", window.location.pathname);
  } else {
    token = storage("session", TOKEN_KEY);
  }

  let catalog = {};
  // Язык: выбранный пользователем на странице, иначе язык установщика, иначе браузера.
  let language = storage("local", LANG_KEY);

  function t(key, vars) {
    const table = catalog[language || "ru"] || catalog.ru || {};
    let text = table[key] || (catalog.ru && catalog.ru[key]) || key;
    if (vars) for (const name of Object.keys(vars)) text = text.split("{" + name + "}").join(String(vars[name]));
    return text;
  }

  function translate(root) {
    (root || document).querySelectorAll("[data-i18n]").forEach(node => { node.textContent = t(node.dataset.i18n); });
    (root || document).querySelectorAll("[data-i18n-label]").forEach(node => {
      node.setAttribute("aria-label", t(node.dataset.i18nLabel));
      node.setAttribute("title", t(node.dataset.i18nLabel));
    });
    document.documentElement.lang = language || "ru";
  }

  async function loadLanguages(preferred) {
    const response = await fetch("lang.json", { cache: "no-store", credentials: "omit" });
    catalog = await response.json();
    if (!language || !catalog[language]) language = preferred || (navigator.language || "ru").slice(0, 2);
    if (!catalog[language]) language = "ru";
    translate();
  }

  function setLanguage(value) {
    if (!catalog[value]) return;
    language = value;
    storage("local", LANG_KEY, value);
    translate();
  }

  class ApiError extends Error {
    constructor(status, code) { super(code); this.status = status; this.code = code; }
  }

  async function api(method, path, body) {
    const headers = { "X-HomenSAI-Token": token || "" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(path, { method, headers, cache: "no-store", credentials: "omit",
        body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (e) {
      throw new ApiError(0, "network");
    }
    if (!response.ok) {
      let code = "http_" + response.status;
      try { code = (await response.json()).error || code; } catch (e) { /* не JSON */ }
      throw new ApiError(response.status, code);
    }
    return response;
  }

  async function json(method, path, body) {
    return (await api(method, path, body)).json();
  }

  function errorText(error) {
    const code = (error && error.code) || "other";
    if (code.startsWith("field:")) return t("error.field", { field: code.slice(6) });
    if (code.startsWith("unknown:")) return t("error.field", { field: code.slice(8) });
    const known = { busy: 1, token: 1, host: 1, network: 1, exists: 1, not_configured: 1 };
    return known[code] ? t("error." + code) : t("error.other", { code });
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (name === "text") node.textContent = value;
      else if (name === "class") node.className = value;
      else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
      else node.setAttribute(name, value === true ? "" : value);
    }
    for (const child of [].concat(children || [])) {
      if (child !== null && child !== undefined) node.append(child);
    }
    return node;
  }

  const SIGNS = { ok: "✓", warn: "!", bad: "✕", info: "●" };

  function badge(kind, label) {
    return el("span", { class: "badge " + kind }, [el("span", { class: "sign", "aria-hidden": "true", text: SIGNS[kind] }), label]);
  }

  function setBadge(node, kind, label) {
    node.className = "badge " + kind;
    node.replaceChildren(el("span", { class: "sign", "aria-hidden": "true", text: SIGNS[kind] }), label);
    node.hidden = false;
  }

  function bindChrome() {
    const theme = document.getElementById("theme");
    if (theme) theme.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme")
        || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = current === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      storage("local", THEME_KEY, next);
    });
  }

  function showError(error) {
    const box = document.getElementById("toast");
    if (!box) return;
    box.textContent = errorText(error);
    box.hidden = false;
    box.scrollIntoView({ block: "nearest" });
  }

  function clearError() {
    const box = document.getElementById("toast");
    if (box) box.hidden = true;
  }

  function size(bytes) {
    if (bytes === null || bytes === undefined) return "—";
    const units = ["B", "KB", "MB", "GB"];
    let value = bytes, unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return value.toFixed(unit ? 1 : 0) + " " + units[unit];
  }

  return {
    t, translate, loadLanguages, setLanguage, api, json, el, badge, setBadge, bindChrome, showError, clearError, size, errorText,
    get language() { return language; },
    get token() { return token; },
    setToken(value) { token = value; storage("session", TOKEN_KEY, value); },
  };
})();
