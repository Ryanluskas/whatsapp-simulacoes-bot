/** Fila de processamento: posição, consultor, cliente, etapa e tempo. */

import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import { icon } from "../core/icons.js";
import * as store from "../core/store.js";
import { stateBadge } from "../core/status.js";
import { dataTable, twoLine } from "../core/table.js";
import { chip, empty } from "../core/ui.js";
import { openRequest } from "./request-detail.js";

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

  mount(root, h("div.stack", resumo, tabela.wrap));

  // Um relógio de 1s mantém o "tempo em execução" honesto entre eventos
  const ticker = setInterval(() => desenhar(store.get("queue")), 1000);

  function desenhar(queue) {
    const itens = queue.items || [];
    const rodando = itens.filter((i) => i.status === "processing").length;
    const esperando = itens.filter((i) => i.status === "queued").length;
    const presas = itens.filter((i) => i.status === "interrupted").length;

    mount(resumo,
      chip("Aguardando", fmt.int(esperando)),
      chip("Em execução", fmt.int(rodando)),
      presas ? chip("Interrompidas", fmt.int(presas)) : null,
      h("span.muted.push", { style: { fontSize: "var(--t-meta)" } },
        "cada pedido é processado isolado dos outros"),
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
        h("button.btn.sm.ghost.icon", {
          type: "button",
          "aria-label": `Abrir detalhes de ${item.request_id}`,
          "data-tip": "Detalhes",
          onclick: () => openRequest(item.id),
        }, icon("chevronRight", 16)),
      ],
    }));
  }

  desenhar(store.get("queue"));
  const off = store.subscribe("queue", desenhar);
  return () => { clearInterval(ticker); off(); };
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
