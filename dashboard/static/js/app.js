/** Casca da aplicação: sessão, rotas, conexão ao vivo e estado global. */

import { api, onUnauthorized } from "./core/api.js";
import { $, h, mount } from "./core/dom.js";
import { closeDrawer } from "./core/drawer.js";
import { icon } from "./core/icons.js";
import * as store from "./core/store.js";
import * as stream from "./core/stream.js";
import { PRESENCE_LABEL, WHATSAPP_LABEL, toneOf } from "./core/status.js";
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

/* Ordem da barra lateral = ordem da operação: primeiro o que está
   acontecendo agora, depois gestão, por último o sistema. */
const ROUTES = {
  overview: {
    title: "Visão geral", subtitle: "Como o bot está agora",
    icon: "overview", group: "Operação", view: overview,
  },
  history: {
    title: "Solicitações", subtitle: "Pedidos, resultado e entrega",
    icon: "history", group: "Operação", view: history,
  },
  queue: {
    title: "Fila", subtitle: "Aguardando e em execução",
    icon: "queue", group: "Operação", view: queue, badge: "queue",
  },
  monitor: {
    title: "Monitor", subtitle: "Eventos em tempo real",
    icon: "monitor", group: "Operação", view: monitor,
  },
  consultants: {
    title: "Consultores", subtitle: "Quem pede simulações no grupo",
    icon: "consultants", group: "Gestão", view: consultants,
  },
  reports: {
    title: "Relatórios", subtitle: "Números por período e exportação",
    icon: "reports", group: "Gestão", view: reports,
  },
  logs: {
    title: "Logs", subtitle: "Registro técnico do sistema",
    icon: "logs", group: "Sistema", view: logs,
  },
  status: {
    title: "Status", subtitle: "WhatsApp, simulador e configuração",
    icon: "status", group: "Sistema", view: status,
  },
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
  closeDrawer({ instant: true });

  const meta = ROUTES[route];
  document.title = `${meta.title} · Allana Bot`;
  $("#view-title").textContent = meta.title;
  $("#view-subtitle").textContent = meta.subtitle;

  document.querySelectorAll(".nav-item").forEach((item) => {
    const active = item.dataset.route === route;
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });

  fecharMenu();
  const root = $("#view");
  mount(root);
  try {
    disposeView = meta.view.render(root, { navigate }) || null;
  } catch (error) {
    console.error(error);
    mount(root, h("div.error-state", { role: "alert" },
      h("div.title", "Esta tela falhou ao carregar."),
      h("code.mono.desc", String(error.message || error)),
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
  closeDrawer({ instant: true });
  closeModal();
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
    if (type === "queue_paused" || type === "queue_resumed") { 
      api.get("/api/system").then(res => store.set("system", res));
      return; 
    }
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

function buildNav() {
  const grupos = new Map();
  for (const [name, meta] of Object.entries(ROUTES)) {
    if (!grupos.has(meta.group)) grupos.set(meta.group, []);
    grupos.get(meta.group).push([name, meta]);
  }

  mount($("#nav"), Array.from(grupos, ([grupo, itens]) =>
    h("div.nav-group",
      h("div.nav-group-label", grupo),
      itens.map(([name, meta]) => h("a.nav-item", {
        href: `#${name}`, dataset: { route: name },
        onclick: (event) => { event.preventDefault(); navigate(name); },
      },
        h("span.ic", icon(meta.icon, 17)),
        h("span", meta.title),
        meta.badge === "queue"
          ? h("span.count", { dataset: { route: name }, title: "Solicitações na fila" }, "0")
          : null,
      )),
    )));
}

function abrirMenu() {
  const app = $("#app");
  app.dataset.nav = "open";
  $("#menu-btn").setAttribute("aria-expanded", "true");
  $("#sidebar").querySelector(".nav-item")?.focus();
}

function fecharMenu() {
  const app = $("#app");
  if (app.dataset.nav !== "open") return;
  app.dataset.nav = "closed";
  $("#menu-btn").setAttribute("aria-expanded", "false");
}

function wireChrome() {
  mount($("#logout"), icon("logout", 17));
  mount($("#menu-btn"), icon("menu", 18));
  mount($("#sb-close"), icon("close", 16));
  const lupa = icon("search", 15);
  lupa.classList.add("ic");
  $(".search")?.prepend(lupa);

  $("#logout").addEventListener("click", async () => {
    try { await api.logout(); } catch { /* sai de qualquer forma */ }
    showLogin();
  });

  $("#menu-btn").addEventListener("click", () => {
    if ($("#app").dataset.nav === "open") fecharMenu();
    else abrirMenu();
  });
  $("#sb-close").addEventListener("click", () => { fecharMenu(); $("#menu-btn").focus(); });
  $("#nav-overlay").addEventListener("click", () => { fecharMenu(); $("#menu-btn").focus(); });

  $("#global-search").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    const term = event.target.value.trim();
    if (term.length < 2) return;
    history.setSearchTerm(term);
    event.target.value = "";
    current = null;          // força o redesenho mesmo já estando na lista
    navigate("history");
  });

  // Atalhos: "/" foca a busca; Esc fecha o menu lateral no celular
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) {
      event.preventDefault();
      $("#global-search").focus();
    }
    if (event.key === "Escape" && $("#app").dataset.nav === "open") {
      fecharMenu();
      $("#menu-btn").focus();
    }
  });

  // Conexão do WhatsApp: presença na barra lateral e estado no cabeçalho
  const paintWhatsApp = (wa) => {
    const state = wa.state || (wa.connected ? "connected" : "disconnected");
    const tom = toneOf(state);
    const onde = wa.chat_name || wa.group_name || "";

    $("#wa-dot").dataset.tone = tom;
    $("#wa-dot").classList.toggle("pulse", Boolean(wa.connected));
    $("#wa-label").textContent = WHATSAPP_LABEL[state] || "Desconhecido";
    $("#wa-meta").textContent = onde;

    $("#presence-dot").dataset.tone = tom;
    $("#presence-dot").classList.toggle("pulse", Boolean(wa.connected));
    $("#presence-label").textContent = PRESENCE_LABEL[state] || "Estado desconhecido";
  };
  paintWhatsApp(store.get("whatsapp"));
  store.subscribe("whatsapp", paintWhatsApp);

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
    $("#stream-dot").dataset.tone = s.connected ? "success" : "error";
    $("#stream-label").textContent = s.connected ? "Eventos ao vivo" : "Reconectando…";
  };
  paintStream(store.get("stream"));
  store.subscribe("stream", paintStream);
}

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
