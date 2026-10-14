/** Fila de processamento: posição, consultor, cliente, etapa e tempo. */

import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import { icon } from "../core/icons.js";
import * as store from "../core/store.js";
import { stateBadge } from "../core/status.js";
import { dataTable, twoLine } from "../core/table.js";
import { chip, empty } from "../core/ui.js";
import { openRequest } from "./request-detail.js";
import { api } from "../core/api.js";

const COLUMNS = [
  { label: "#", class: "num" },
  { label: "Consultor" },
  { label: "Cliente" },
  { label: "Etapa" },
  { label: "Tempo", class: "num" },
  { label: "", srLabel: "Detalhes", class: "act" },
];

export function render(root) {
  const resumo = h("div.toolbar");
  const tabela = dataTable({ columns: COLUMNS, caption: "Solicitações na fila e em execução" });

  const pauseBtn = h("button.btn.sm.ghost", { type: "button", onclick: alternarPausa },
    icon("pause", 14), "Pausar fila");
  pauseBtn.classList.add("push");

  mount(root, h("div.stack", h("div.stack-h", resumo, pauseBtn), tabela.wrap));

  async function alternarPausa() {
    const sys = store.get("system");
    const wasPaused = sys && sys.queue_paused;
    pauseBtn.disabled = true;
    try {
      const res = await api.post(wasPaused ? "/api/worker/resume" : "/api/worker/pause");
      store.set("system", res);
    } finally {
      pauseBtn.disabled = false;
    }
  }

  // Um relógio de 1s mantém o "tempo em execução" honesto entre eventos
  const ticker = setInterval(() => desenhar(store.get("queue")), 1000);

  function desenhar(queue) {
    const sys = store.get("system") || {};
    const pausado = sys.queue_paused;

    mount(pauseBtn, icon(pausado ? "play" : "pause", 14), pausado ? "Retomar execução" : "Pausar fila");
    pauseBtn.classList.toggle("primary", pausado);
    pauseBtn.classList.toggle("ghost", !pausado);

    const itens = queue.items || [];
    const rodando = itens.filter((i) => i.status === "processing").length;
    const esperando = itens.filter((i) => i.status === "queued").length;
    const presas = itens.filter((i) => i.status === "interrupted").length;

    mount(resumo,
      chip("Aguardando", fmt.int(esperando)),
      chip("Em execução", fmt.int(rodando)),
      presas ? chip("Interrompidas", fmt.int(presas)) : null,
      pausado ? h("span.faint", { style: { color: "var(--warning)" } }, "Fila pausada") : null,
    );

    if (!itens.length) {
      tabela.message(empty({
        allana: true,
        title: "Fila vazia",
        desc: "Nada aguardando nem em execução agora.",
      }));
      return;
    }

    tabela.rows(itens, (item) => ({
      onOpen: () => openRequest(item.id),
      cells: [
        twoLine(String(item.position), h("span.mono", item.request_id)),
        twoLine(item.consultant_name || "—", `recebida ${fmt.time(item.created_at, false)}`),
        twoLine(item.customer_name || "—", h("span.mono", mascararCpf(item.cpf))),
        h("div",
          stateBadge(item.stage, item.stage_label),
          item.attempts > 1
            ? h("div.cell-sub", { style: { color: "var(--warning)" } },
              `tentativa ${item.attempts} de ${item.max_attempts || "—"}`)
            : null,
        ),
        tempo(item),
        h("div.stack-h",
          item.status === "queued" ? h("button.btn.sm.ghost.icon.primary", {
            type: "button",
            "aria-label": "Começar desta",
            "data-tip": "Iniciar agora",
            onclick: (e) => { e.stopPropagation(); executarAgora(item.id); },
          }, icon("play", 16)) : null,
          item.status === "queued" ? h("button.btn.sm.ghost.icon", {
            type: "button",
            "aria-label": "Mover para o topo",
            "data-tip": "Topo",
            onclick: (e) => { e.stopPropagation(); reorder(item.id, "top"); },
          }, icon("arrowUpToLine", 16)) : null,
          item.status === "queued" ? h("button.btn.sm.ghost.icon", {
            type: "button",
            "aria-label": "Mover para cima",
            "data-tip": "Subir",
            onclick: (e) => { e.stopPropagation(); reorder(item.id, "up"); },
          }, icon("arrowUp", 16)) : null,
          item.status === "queued" ? h("button.btn.sm.ghost.icon", {
            type: "button",
            "aria-label": "Mover para baixo",
            "data-tip": "Descer",
            onclick: (e) => { e.stopPropagation(); reorder(item.id, "down"); },
          }, icon("arrowDown", 16)) : null,
          item.status === "queued" ? h("button.btn.sm.ghost.icon.danger", {
            type: "button",
            "aria-label": "Remover da fila",
            "data-tip": "Remover",
            onclick: (e) => { e.stopPropagation(); remover(item.id); },
          }, icon("trash", 16)) : null,
          h("button.btn.sm.ghost.icon", {
            type: "button",
            "aria-label": `Abrir detalhes de ${item.request_id}`,
            "data-tip": "Detalhes",
            onclick: (e) => { e.stopPropagation(); openRequest(item.id); },
          }, icon("chevronRight", 16))
        ),
      ],
    }));
  }

  desenhar(store.get("queue"));
  const offQ = store.subscribe("queue", desenhar);
  const offS = store.subscribe("system", () => desenhar(store.get("queue")));
  return () => { clearInterval(ticker); offQ(); offS(); };
}

function tempo(item) {
  if (item.status === "processing" && item.started_at) {
    const segundos = (Date.now() - new Date(item.started_at).getTime()) / 1000;
    return fmt.duration(Math.max(0, segundos));
  }
  if (item.processing_seconds) return fmt.duration(item.processing_seconds);
  return h("span.faint", "—");
}

/** O servidor já entrega mascarado quando MASK_CPF_IN_UI está ligado. */
function mascararCpf(cpf) {
  if (!cpf) return "—";
  const digitos = String(cpf).replace(/\D/g, "");
  if (digitos.length !== 11) return cpf;
  return `${digitos.slice(0, 3)}.***.***-${digitos.slice(9)}`;
}

async function reorder(id, action) {
  try {
    await api.post(`/api/queue/${id}/reorder`, { action });
  } catch (err) {
    alert(err.message || "Falha ao reordenar item.");
  }
}

async function remover(id) {
  if (!confirm("Tem certeza que deseja remover esta simulação da fila?")) return;
  try {
    await api.del(`/api/queue/${id}`);
  } catch (err) {
    alert(err.message || "Falha ao remover item.");
  }
}

async function executarAgora(id) {
  try {
    await api.post(`/api/queue/${id}/reorder`, { action: "top" });
    const sys = store.get("system");
    if (sys && sys.queue_paused) {
      await api.post("/api/worker/resume");
    }
  } catch (err) {
    alert(err.message || "Falha ao forçar início.");
  }
}
