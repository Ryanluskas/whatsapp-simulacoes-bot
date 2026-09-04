/** Fila de processamento: posição, consultor, cliente, etapa, tempo e tentativas. */

import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import * as store from "../core/store.js";
import { badge, chip, empty } from "../core/ui.js";
import { openSimulation } from "./history.js";

const COLUMNS = ["#", "Solicitação", "Consultor", "Cliente", "CPF", "Contrato",
  "Etapa", "Tentativas", "Recebida", "Tempo"];

export function render(root) {
  const summary = h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } });
  const tbody = h("tbody");

  mount(root,
    h("div.section-head",
      h("h3", "Fila de processamento"),
      h("span.hint", "vários consultores podem solicitar ao mesmo tempo · cada pedido é isolado"),
    ),
    h("div", { style: { marginBottom: "16px" } }, summary),
    h("div.table-wrap",
      h("table.data",
        h("thead", h("tr", COLUMNS.map((c, i) =>
          h("th", { class: i >= 7 ? "num" : "" }, c)))),
        tbody,
      ),
    ),
  );

  // Um relógio de 1s mantém o "tempo em execução" honesto entre eventos
  const ticker = setInterval(() => draw(store.get("queue")), 1000);

  function draw(queue) {
    const items = queue.items || [];
    const running = items.filter((i) => i.status === "processing").length;
    const waiting = items.filter((i) => i.status === "queued").length;
    const stuck = items.filter((i) => i.status === "interrupted").length;

    mount(summary,
      chip("Na fila", fmt.int(waiting)),
      chip("Em execução", fmt.int(running)),
      stuck ? chip("Interrompidas", fmt.int(stuck)) : null,
    );

    if (!items.length) {
      mount(tbody, h("tr", h("td", { colspan: COLUMNS.length },
        empty({
          mark: "queue",
          title: "Fila vazia",
          desc: "Nenhuma simulação aguardando ou em execução no momento.",
        }))));
      return;
    }

    mount(tbody, items.map((item) => h("tr", {
      dataset: { clickable: "true" },
      tabindex: "0",
      onclick: () => openSimulation(item.id),
      onkeydown: (e) => { if (e.key === "Enter") openSimulation(item.id); },
    },
      h("td.num.cell-strong", String(item.position)),
      h("td", h("span.mono", { style: { fontSize: "12px" } }, item.request_id)),
      h("td.cell-strong", item.consultant_name || "—"),
      h("td", item.customer_name || "—"),
      h("td", h("span.mono", maskedCpf(item.cpf))),
      h("td", h("span.mono", item.contract || "—")),
      h("td", badge(item.stage, item.stage_label)),
      h("td.num", attemptCell(item)),
      h("td.num.muted", fmt.time(item.created_at)),
      h("td.num", elapsedCell(item)),
    )));
  }

  draw(store.get("queue"));
  const off = store.subscribe("queue", draw);
  return () => { clearInterval(ticker); off(); };
}

function attemptCell(item) {
  const attempts = item.attempts || 0;
  if (attempts <= 1) return h("span.muted", String(attempts || "—"));
  return h("span", { style: { color: "var(--st-queued)" } }, `${attempts}/${item.max_attempts || "—"}`);
}

function elapsedCell(item) {
  if (item.status === "processing" && item.started_at) {
    const seconds = (Date.now() - new Date(item.started_at).getTime()) / 1000;
    return h("span", fmt.duration(Math.max(0, seconds)));
  }
  if (item.processing_seconds) return h("span", fmt.duration(item.processing_seconds));
  return h("span.muted", "—");
}

/** O servidor já entrega mascarado quando MASK_CPF_IN_UI está ligado. */
function maskedCpf(cpf) {
  if (!cpf) return "—";
  const digits = String(cpf).replace(/\D/g, "");
  if (digits.length !== 11) return cpf;
  return `${digits.slice(0, 3)}.***.***-${digits.slice(9)}`;
}
