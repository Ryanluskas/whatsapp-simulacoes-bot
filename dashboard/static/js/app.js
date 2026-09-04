/** Casca da aplicação: sessão, rotas, conexão ao vivo e estado global. */

import { api, onUnauthorized } from "./core/api.js";
import { $, h, mount } from "./core/dom.js";
import { icon } from "./core/icons.js";
import { logoLockup } from "./core/logo.js";
import * as store from "./core/store.js";
import * as stream from "./core/stream.js";
import { closeModal, toast } from "./core/ui.js";
import { shouldNotify } from "./feed.js";

import * as consultants from "./views/consultants.js";
import * as history from "./views/history.js";
import * as logs from "./views/logs.js";
import * as monitor from "./views/monitor.js";
import * as overview from "./views/overview.js";
import * as queue from "./views/queue.js";
import * as reports from "./views/reports.js";
import * as status from "./views/status.js";

const ROUTES = {
  overview:    { title: "Visão geral", icon: "overview",    view: overview },
  monitor:     { title: "Monitor",     icon: "monitor",     view: monitor, live: true },
  queue:       { title: "Fila",        icon: "queue",       view: queue, badge: "queue" },
  history:     { title: "Histórico",   icon: "history",     view: history },
  reports:     { title: "Relatórios",  icon: "reports",     view: reports },
  consultants: { title: "Consultores", icon: "consultants", view: consultants },
  logs:        { title: "Logs",        icon: "logs",        view: logs },
  status:      { title: "Status",      icon: "status",      view: status },
};

const DEFAULT_ROUTE = "overview";
let disposeView = null;
let current = null;

/* =============================================================== rotas === */

function navigate(name, { push = true } = {}) {
  const route = ROUTES[name] ? name : DEFAULT_ROUTE;
  if (push && location.hash.slice(1) !== route) {
    location.hash = route;
    return; // o hashchange chama de volta
  }
  if (current === route) return;

  disposeView?.();
  disposeView = null;
  current = route;
  closeModal();

  const meta = ROUTES[route];
  document.title = `${meta.title} · Allana`;
  $("#view-title").textContent = meta.title;
  $("#crumb").textContent = `Painel · ${meta.title}`;

  document.querySelectorAll(".nav-item").forEach((item) => {
    const active = item.dataset.route === route;
    item.setAttribute("aria-current", active ? "page" : "false");
    if (!active) item.removeAttribute("aria-current");
  });

  $("#app").dataset.nav = "closed";
  const root = $("#view");
  mount(root);
  try {
    disposeView = meta.view.render(root, { navigate }) || null;
  } catch (error) {
    console.error(error);
    mount(root, h("div.error-state",
      h("div.mark", icon("alert", 22)),
      h("div", "Esta tela falhou ao carregar."),
      h("code.mono", { style: { fontSize: "12px", color: "var(--ink-3)" } }, String(error.message || error)),
    ));
  }
  root.scrollTop = 0;
}

window.addEventListener("hashchange", () => navigate(location.hash.slice(1), { push: false }));

/* ================================================================ login == */

function showLogin(message = "") {
  stream.disconnect();
  current = null;
  disposeView?.();
  disposeView = null;
  $("#login").classList.remove("hidden");
  $("#app").classList.add("hidden");
  $("#login-error").textContent = message;
  $("#login-password").value = "";
  setTimeout(() => $("#login-password").focus(), 60);
}

async function enterApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");

  try {
    const data = await api.bootstrap();
    store.set("labels", data.labels || { stages: {}, statuses: {} });
    store.set("metrics", data.metrics);
    store.set("queue", data.queue);
    store.set("whatsapp", data.whatsapp);
    store.set("system", data.system);
    store.seedFeed(data.monitor || []);
  } catch (error) {
    if (error.status === 401) return;
    toast("error", "Falha ao carregar", error.message);
  }

  stream.connect();
  navigate(location.hash.slice(1) || DEFAULT_ROUTE, { push: false });
}

onUnauthorized(() => showLogin("Sessão expirada. Entre novamente."));

/* ================================================= eventos ao vivo ======= */

function wireStream() {
  // Um único assinante alimenta o feed e as notificações; as telas leem do store.
  stream.on("*", (event, type) => {
    if (type === "hello") return;

    // Eventos de estado atualizam o store e não entram no feed
    if (type === "metrics") { store.set("metrics", event.payload); return; }
    if (type === "queue_update") { store.set("queue", event.payload); return; }
    if (type === "whatsapp_status") { store.set("whatsapp", event.payload); return; }
    if (type === "log") return; // a tela de logs cuida disso sozinha

    // Conexão/desconexão contam as duas coisas: viram linha no feed e mudam o estado
    if (type === "whatsapp_connected" || type === "whatsapp_disconnected") {
      store.set("whatsapp", { ...store.get("whatsapp"), ...event.payload });
    }

    store.pushFeed(event);

    if (shouldNotify(event)) {
      toast(event.level === "error" ? "error" : "warning",
        event.title || "Atenção", event.detail || "");
    }
    if (type === "whatsapp_connected") {
      toast("success", "WhatsApp conectado", event.detail || "");
    }
  });
}

/* ================================================= cromo da interface ==== */

function buildLogos() {
  mount($("#login-logo"), logoLockup({ size: 44, sub: "" }));
  mount($("#sidebar-logo"), logoLockup({ size: 34, sub: "Operações" }));
}

function buildNav() {
  const nav = $("#nav");
  mount(nav, Object.entries(ROUTES).map(([name, meta]) =>
    h("a.nav-item", {
      href: `#${name}`, dataset: { route: name }, role: "link",
      onclick: (event) => { event.preventDefault(); navigate(name); },
    },
      h("span.ic", icon(meta.icon, 17)),
      h("span", meta.title),
      meta.badge === "queue" ? h("span.count", { dataset: { route: name } }, "0") : null,
    )));
}

function wireChrome() {
  mount($("#logout"), icon("logout", 17));
  const lupa = icon("search", 15);
  lupa.classList.add("ic");
  $(".search")?.prepend(lupa);
  mount($("#menu-btn"), icon("menu", 18));

  $("#logout").addEventListener("click", async () => {
    try { await api.logout(); } catch { /* sai de qualquer forma */ }
    showLogin();
  });

  $("#menu-btn").addEventListener("click", () => {
    const app = $("#app");
    app.dataset.nav = app.dataset.nav === "open" ? "closed" : "open";
  });

  $("#global-search").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    const term = event.target.value.trim();
    if (term.length < 2) return;
    history.setSearchTerm(term);
    event.target.value = "";
    current = null;          // força o redesenho mesmo já estando no histórico
    navigate("history");
  });

  // Atalho: "/" foca a busca, Esc fecha o modal
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) {
      event.preventDefault();
      $("#global-search").focus();
    }
  });

  // Indicador de conexão do WhatsApp no topo e na barra lateral
  const paint = (wa) => {
    const state = wa.state || (wa.connected ? "connected" : "disconnected");
    $("#wa-dot").dataset.state = state;
    $("#wa-dot").classList.toggle("pulse", Boolean(wa.connected));
    $("#wa-dot-mini").dataset.state = state;
    $("#wa-label").textContent = LABEL[state] || "Desconhecido";
    $("#wa-meta").textContent = wa.phone || wa.chat_name || wa.group_name || "";
    $("#wa-mini-label").textContent = wa.connected ? "WhatsApp online" : "WhatsApp offline";
  };
  paint(store.get("whatsapp"));
  store.subscribe("whatsapp", paint);

  const paintQueue = (queueState) => {
    const el = document.querySelector('.nav-item .count[data-route="queue"]');
    if (!el) return;
    const depth = queueState.depth || 0;
    el.textContent = String(depth);
    el.dataset.live = String(depth > 0);
  };
  paintQueue(store.get("queue"));
  store.subscribe("queue", paintQueue);

  const paintStream = (s) => {
    $("#stream-dot").dataset.state = s.connected ? "connected" : "error";
    $("#stream-dot").title = s.connected ? "Recebendo eventos ao vivo" : "Reconectando…";
  };
  paintStream(store.get("stream"));
  store.subscribe("stream", paintStream);
}

const LABEL = {
  connected: "Conectado",
  disconnected: "Desconectado",
  qr: "Ler QR Code",
  starting: "Conectando…",
};

/* ==================================================================== boot */

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#login-btn");
  const password = $("#login-password").value;
  button.disabled = true;
  $("#login-error").textContent = "";
  try {
    await api.login(password);
    await enterApp();
  } catch (error) {
    $("#login-error").textContent = error.status === 429
      ? error.message
      : "Senha inválida.";
    $("#login-password").select();
  } finally {
    button.disabled = false;
  }
});

async function boot() {
  buildLogos();
  buildNav();
  wireChrome();
  wireStream();
  try {
    const session = await api.session();
    if (session.authenticated) {
      store.patch("session", { authenticated: true, user: session.user, maskCpf: session.mask_cpf });
      await enterApp();
    } else {
      showLogin();
    }
  } catch {
    showLogin("Não foi possível falar com o servidor.");
  }
}

boot();
