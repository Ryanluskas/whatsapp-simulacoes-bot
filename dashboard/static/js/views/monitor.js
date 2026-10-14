import { api } from "../core/api.js";
import { h, mount, replaceKeepingScroll } from "../core/dom.js";
import * as fmt from "../core/format.js";
import { icon } from "../core/icons.js";
import * as store from "../core/store.js";
import { WHATSAPP_LABEL, stateBadge } from "../core/status.js";
import { card, chip, empty, statusDot } from "../core/ui.js";
import { feedRow } from "../feed.js";

const FILTERS = [
  { id: "all", label: "Tudo" },
  { id: "messages", label: "Mensagens", types: ["message_received", "message_sent", "message_failed"] },
  { id: "jobs", label: "Simulações", types: ["job_queued", "job_progress", "job_done", "job_error", "job_retry", "request_created", "request_completed", "delivery_retry", "delivery_unconfirmed", "delivery_manual", "delivery_failed"] },
  { id: "problems", label: "Problemas", levels: ["error", "warning"] },
];

export function render(root) {
  let filter = "all";

  const body = h("div.console-body", { tabindex: "0", role: "log", "aria-label": "Fluxo de eventos ao vivo" });
  const livePill = h("span.live-pill");
  const pauseBtn = h("button.btn.sm.ghost", { type: "button", onclick: alternarPausa },
    icon("pause", 14), "Pausar Santander");

  const segButtons = FILTERS.map((f) =>
    h("button", {
      type: "button",
      "aria-pressed": String(f.id === filter),
      onclick: () => {
        filter = f.id;
        segButtons.forEach((b, i) => b.setAttribute("aria-pressed", String(FILTERS[i].id === filter)));
        desenharFeed(store.get("feed"));
      },
    }, f.label));

  const conexao = card("WhatsApp");
  const execucao = card("Em execução");
  const numeros = card("Hoje");

  mount(root,
    h("div.monitor",
      h("div.console",
        h("div.console-head",
          h("span.title", "Fluxo operacional"),
          h("div.seg", { role: "group", "aria-label": "Filtrar eventos" }, segButtons),
          pauseBtn,
          livePill,
        ),
        body,
      ),
      h("div.stack", conexao.node, execucao.node, numeros.node),
    ),
  );

  async function alternarPausa() {
    const s = store.get("system");
    const wasPaused = s && s.queue_paused;
    pauseBtn.disabled = true;
    try {
      const res = await api.post(wasPaused ? "/api/worker/resume" : "/api/worker/pause");
      store.set("system", res);
    } finally {
      pauseBtn.disabled = false;
    }
  }

  function desenharPausa(system) {
    if (!system) return;
    const pausado = system.queue_paused;
    mount(pauseBtn, icon(pausado ? "send" : "pause", 14), pausado ? "Retomar Santander" : "Pausar Santander");
    pauseBtn.classList.toggle("primary", pausado);
    pauseBtn.classList.toggle("ghost", !pausado);
    
    // Atualiza hint do "Em execução" para refletir estado de pausa real
    if (pausado) {
      if (system.pause_state === "pausando") {
        execucao.setHint(`PAUSANDO — terminando ${system.active_jobs} em andamento`);
      } else {
        execucao.setHint(`PAUSADO — Santander em uso manual`);
      }
    } else {
      const q = store.get("queue") || {};
      execucao.setHint(`${q.depth || 0} na fila`);
    }
  }

  function combina(event) {
    const regra = FILTERS.find((f) => f.id === filter);
    if (!regra || regra.id === "all") return true;
    if (regra.types) return regra.types.includes(event.type);
    if (regra.levels) return regra.levels.includes(event.level);
    return true;
  }

  function desenharFeed(items) {
    const visiveis = items.filter(combina);
    if (!visiveis.length) {
      mount(body, empty({
        allana: filter === "all",
        mark: "monitor",
        title: filter === "all" ? "Aguardando atividade" : "Nada neste filtro",
        desc: filter === "all"
          ? "Assim que um consultor mandar uma mensagem no grupo, ela aparece aqui."
          : "Troque o filtro para ver os outros eventos.",
      }));
      return;
    }
    replaceKeepingScroll(body, h("div.feed", visiveis.map(feedRow)));
  }

  function desenharConexao(wa) {
    const estado = wa.state || (wa.connected ? "connected" : "disconnected");
    conexao.set(
      h("div", { style: { display: "flex", alignItems: "center", gap: "12px", marginBottom: "14px" } },
        statusDot(estado, { large: true, pulse: Boolean(wa.connected) }),
        h("div",
          h("strong", { style: { fontSize: "var(--t-h3)" } }, WHATSAPP_LABEL[estado] || "Desconhecido"),
          h("div.muted", { style: { fontSize: "var(--t-meta)" } },
            wa.chat_name || wa.group_name || wa.phone || "—"),
        ),
      ),
      h("div.toolbar", { style: { marginBottom: 0 } },
        chip("Recebidas", fmt.int(wa.received)),
        chip("Enviadas", fmt.int(wa.sent)),
        wa.connected ? chip("Online há", fmt.uptime(wa.online_since)) : null,
      ),
      wa.last_error && !wa.connected
        ? h("p.muted", { style: { marginTop: "12px", fontSize: "var(--t-meta)" } }, wa.last_error)
        : null,
    );
  }

  function desenharExecucao(queue) {
    const rodando = (queue.items || []).filter((i) => i.status === "processing");
    const sys = store.get("system") || {};
    if (!sys.queue_paused) {
      execucao.setHint(`${queue.depth || 0} na fila`);
    }
    execucao.set(rodando.length
      ? h("div.stack", rodando.map((item) =>
        h("div",
          h("div", { style: { display: "flex", gap: "8px", alignItems: "center", marginBottom: "4px" } },
            stateBadge(item.stage, item.stage_label),
            h("span.mono.muted", { style: { fontSize: "var(--t-meta)" } }, item.request_id),
          ),
          h("div", item.consultant_name || "—"),
          h("div.cell-sub",
            `contrato ${item.contract || "—"}`,
            item.elapsed_seconds ? ` • ${fmt.duration(item.elapsed_seconds)}` : "",
            item.attempts > 1 ? ` • tentativa ${item.attempts}` : ""),
        )))
      : empty({ compact: true, mark: "clock", title: "Ocioso", desc: "Nenhuma simulação em execução." }));
  }

  function desenharNumeros(metrics) {
    const k = metrics?.kpis;
    numeros.set(k
      ? h("div.toolbar", { style: { marginBottom: 0 } },
        chip("Solicitações", fmt.int(k.today)),
        chip("Concluídas", fmt.int(k.today_completed)),
        chip("Erros", fmt.int(k.errors)),
        chip("Tempo médio", fmt.duration(k.avg_seconds)),
      )
      : h("p.muted", "Carregando..."));
  }

  function desenharAoVivo(s) {
    livePill.dataset.live = String(s.connected);
    mount(livePill,
      statusDot(null, { tone: s.connected ? "success" : "error", pulse: s.connected }),
      s.connected ? "AO VIVO" : "RECONECTANDO",
    );
  }

  desenharFeed(store.get("feed"));
  desenharConexao(store.get("whatsapp"));
  desenharExecucao(store.get("queue"));
  desenharNumeros(store.get("metrics"));
  desenharAoVivo(store.get("stream"));
  desenharPausa(store.get("system"));

  const off = [
    store.subscribe("feed", desenharFeed),
    store.subscribe("whatsapp", desenharConexao),
    store.subscribe("queue", desenharExecucao),
    store.subscribe("metrics", desenharNumeros),
    store.subscribe("stream", desenharAoVivo),
    store.subscribe("system", desenharPausa),
  ];
  return () => off.forEach((fn) => fn());
}
