/**
 * Monitor operacional — o bot trabalhando em tempo real.
 *
 * Nada aqui é animação simulada: cada linha é um evento que o backend gravou.
 * Ao abrir a tela o histórico recente vem do banco (por isso sobrevive a um
 * F5) e a partir daí o fluxo chega pelo SSE.
 */

import { h, mount, replaceKeepingScroll } from "../core/dom.js";
import * as fmt from "../core/format.js";
import * as store from "../core/store.js";
import { badge, chip, empty, statusDot } from "../core/ui.js";
import { feedRow } from "../feed.js";

const FILTERS = [
  { id: "all", label: "Tudo" },
  { id: "messages", label: "Mensagens", types: ["message_received", "message_sent", "message_failed"] },
  { id: "jobs", label: "Simulações", types: ["job_queued", "job_progress", "job_done", "job_error", "job_retry", "request_created", "request_completed", "delivery_retry", "delivery_unconfirmed", "delivery_failed"] },
  { id: "problems", label: "Problemas", levels: ["error", "warning"] },
];

export function render(root) {
  let filter = "all";
  let paused = false;

  const body = h("div.console-body", { tabindex: "0", role: "log", "aria-label": "Fluxo de eventos ao vivo" });
  const livePill = h("span.live-pill");
  const pauseBtn = h("button.btn.sm.ghost", { type: "button", onclick: togglePause }, "Pausar");

  const segButtons = FILTERS.map((f) =>
    h("button", {
      type: "button",
      "aria-pressed": String(f.id === filter),
      onclick: () => {
        filter = f.id;
        segButtons.forEach((b, i) => b.setAttribute("aria-pressed", String(FILTERS[i].id === filter)));
        drawFeed(store.get("feed"));
      },
    }, f.label));

  const connCard = h("div.card");
  const activeCard = h("div.card");
  const statsCard = h("div.card");

  mount(root,
    h("div.section-head",
      h("h3", "Monitor"),
      h("span.hint", "mensagem → consultor → fila → simulação → resposta"),
    ),
    h("div.monitor",
      h("div.console",
        h("div.console-head",
          h("span.title", "Fluxo operacional"),
          h("div.seg", segButtons),
          pauseBtn,
          livePill,
        ),
        body,
      ),
      h("div.grid", { style: { gap: "16px" } }, connCard, activeCard, statsCard),
    ),
  );

  function togglePause() {
    paused = !paused;
    pauseBtn.textContent = paused ? "Retomar" : "Pausar";
    pauseBtn.classList.toggle("primary", paused);
    if (!paused) drawFeed(store.get("feed"));
  }

  function matches(event) {
    const rule = FILTERS.find((f) => f.id === filter);
    if (!rule || rule.id === "all") return true;
    if (rule.types) return rule.types.includes(event.type);
    if (rule.levels) return rule.levels.includes(event.level);
    return true;
  }

  function drawFeed(items) {
    if (paused) return;
    const visible = items.filter(matches);
    if (!visible.length) {
      mount(body, empty({
        mark: "monitor",
        title: filter === "all" ? "Aguardando atividade" : "Nada neste filtro",
        desc: filter === "all"
          ? "Assim que um consultor mandar uma mensagem no grupo, ela aparece aqui."
          : "Troque o filtro para ver os outros eventos.",
      }));
      return;
    }
    replaceKeepingScroll(body, h("div.feed", visible.map(feedRow)));
  }

  function drawConnection(wa) {
    const connected = wa.connected;
    mount(connCard,
      h("header", h("h3", "WhatsApp")),
      h("div", { style: { display: "flex", alignItems: "center", gap: "12px", marginBottom: "14px" } },
        statusDot(wa.state, { large: true, pulse: connected }),
        h("div",
          h("strong", { style: { fontSize: "16px" } }, stateLabel(wa.state)),
          h("div.muted", { style: { fontSize: "12px" } },
            wa.phone || wa.chat_name || wa.group_name || "—"),
        ),
      ),
      h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } },
        chip("Recebidas", fmt.int(wa.received)),
        chip("Enviadas", fmt.int(wa.sent)),
        connected && chip("Online há", fmt.uptime(wa.online_since)),
      ),
      wa.last_error && !connected
        ? h("div.muted", { style: { marginTop: "12px", fontSize: "12px" } }, wa.last_error)
        : null,
    );
  }

  function drawActive(queue) {
    const running = (queue.items || []).filter((i) => i.status === "processing");
    mount(activeCard,
      h("header", h("h3", "Em execução"), h("span.hint", `${queue.depth || 0} na fila`)),
      running.length
        ? h("div.grid", { style: { gap: "10px" } }, running.map((item) =>
          h("div", { style: { display: "grid", gap: "6px" } },
            h("div", { style: { display: "flex", gap: "8px", alignItems: "center" } },
              badge(item.stage, item.stage_label),
              h("span.mono.muted", { style: { fontSize: "11px" } }, item.request_id),
            ),
            h("div", { style: { fontSize: "13px" } }, item.consultant_name || "—"),
            h("div.muted", { style: { fontSize: "12px" } },
              `contrato ${item.contract || "—"}`,
              item.elapsed_seconds ? ` · ${fmt.duration(item.elapsed_seconds)}` : "",
              item.attempts > 1 ? ` · tentativa ${item.attempts}` : ""),
          )))
        : empty({ mark: "clock", title: "Ocioso", desc: "Nenhuma simulação em execução." }),
    );
  }

  function drawStats(metrics) {
    const k = metrics?.kpis;
    mount(statsCard,
      h("header", h("h3", "Hoje")),
      k
        ? h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } },
          chip("Solicitações", fmt.int(k.today)),
          chip("Concluídas", fmt.int(k.today_completed)),
          chip("Erros", fmt.int(k.errors)),
          chip("Tempo médio", fmt.duration(k.avg_seconds)),
        )
        : h("div.muted", "Carregando…"),
    );
  }

  function drawLive(stream) {
    livePill.dataset.live = String(stream.connected);
    mount(livePill,
      statusDot(stream.connected ? "connected" : "error", { pulse: stream.connected }),
      stream.connected ? "AO VIVO" : "RECONECTANDO",
    );
  }

  drawFeed(store.get("feed"));
  drawConnection(store.get("whatsapp"));
  drawActive(store.get("queue"));
  drawStats(store.get("metrics"));
  drawLive(store.get("stream"));

  const off = [
    store.subscribe("feed", drawFeed),
    store.subscribe("whatsapp", drawConnection),
    store.subscribe("queue", drawActive),
    store.subscribe("metrics", drawStats),
    store.subscribe("stream", drawLive),
  ];
  return () => off.forEach((fn) => fn());
}

export function stateLabel(state) {
  return {
    connected: "Conectado",
    disconnected: "Desconectado",
    qr: "Aguardando QR Code",
    starting: "Conectando…",
  }[state] || "Desconhecido";
}
