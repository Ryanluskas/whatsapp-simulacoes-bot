/** Logs técnicos com filtros por nível, serviço, solicitação e busca livre. */

import { api } from "../core/api.js";
import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import * as stream from "../core/stream.js";
import { debounce, empty, errorState, skeletonRows } from "../core/ui.js";

const PAGE = 100;
const COLUMNS = ["Hora", "Nível", "Serviço", "Mensagem", "Solicitação", "Consultor"];
const LEVEL_STATE = { INFO: "processing", WARNING: "queued", ERROR: "error", DEBUG: "cancelled" };

export function render(root) {
  const filters = { level: "", service: "", q: "", request_id: "", offset: 0 };
  let live = true;

  const tbody = h("tbody");
  const pager = h("div.pager");
  const serviceSel = h("select.input", {
    style: { width: "auto", minWidth: "140px" }, "aria-label": "Filtrar por serviço",
    onchange: (e) => { filters.service = e.target.value; filters.offset = 0; load(); },
  }, h("option", { value: "" }, "Serviço: todos"));

  const liveToggle = h("button.btn.sm", {
    type: "button", "aria-pressed": "true",
    onclick: () => {
      live = !live;
      liveToggle.setAttribute("aria-pressed", String(live));
      liveToggle.classList.toggle("primary", live);
      liveToggle.textContent = live ? "Ao vivo" : "Pausado";
    },
  }, "Ao vivo");
  liveToggle.classList.add("primary");

  mount(root,
    h("div.section-head", h("h3", "Logs técnicos")),
    h("div.toolbar",
      h("div.grow",
        h("input.input", {
          type: "search", placeholder: "Buscar na mensagem…", "aria-label": "Buscar nos logs",
          oninput: debounce((e) => { filters.q = e.target.value.trim(); filters.offset = 0; load(); }),
        }),
      ),
      h("select.input", {
        style: { width: "auto" }, "aria-label": "Filtrar por nível",
        onchange: (e) => { filters.level = e.target.value; filters.offset = 0; load(); },
      },
        h("option", { value: "" }, "Nível: todos"),
        ["INFO", "WARNING", "ERROR", "DEBUG"].map((l) => h("option", { value: l }, l)),
      ),
      serviceSel,
      h("input.input", {
        type: "text", placeholder: "REQ000123", style: { width: "128px" },
        "aria-label": "Filtrar por solicitação",
        oninput: debounce((e) => { filters.request_id = e.target.value.trim(); filters.offset = 0; load(); }),
      }),
      liveToggle,
    ),
    h("div.table-wrap",
      h("table.data",
        h("thead", h("tr", COLUMNS.map((c) => h("th", c)))),
        tbody,
      ),
    ),
    pager,
  );

  async function load() {
    if (!tbody.childElementCount) mount(tbody, skeletonRows(8, COLUMNS.length));
    let data;
    try {
      data = await api.logs({ ...filters, limit: PAGE });
    } catch (error) {
      mount(tbody, h("tr", h("td", { colspan: COLUMNS.length }, errorState(error.message, load))));
      return;
    }

    if (serviceSel.options.length === 1 && data.services?.length) {
      data.services.forEach((service) =>
        serviceSel.appendChild(h("option", { value: service }, service)));
    }

    if (!data.items.length) {
      mount(tbody, h("tr", h("td", { colspan: COLUMNS.length }, empty({
        mark: "logs", title: "Nenhum log para este filtro",
        desc: "Ajuste os filtros ou aguarde a próxima atividade do sistema.",
      }))));
      mount(pager);
      return;
    }

    mount(tbody, data.items.map((log) => h("tr",
      h("td.muted", h("span.mono", { style: { fontSize: "12px" } }, fmt.dateTime(log.created_at))),
      h("td", h("span.badge", { dataset: { state: LEVEL_STATE[log.level] || "cancelled" } },
        h("span.dot", { dataset: { state: LEVEL_STATE[log.level] || "cancelled" } }), log.level)),
      h("td", log.service || "—"),
      h("td", { style: { maxWidth: "540px" } }, log.message || ""),
      h("td", log.request_id ? h("span.mono", { style: { fontSize: "12px" } }, log.request_id) : h("span.muted", "—")),
      h("td.muted", log.consultant || "—"),
    )));

    const to = Math.min(filters.offset + data.items.length, data.total);
    mount(pager,
      h("span", `${filters.offset + 1}–${to} de ${fmt.int(data.total)}`),
      h("button.btn.sm", {
        type: "button", disabled: filters.offset === 0,
        onclick: () => { filters.offset = Math.max(0, filters.offset - PAGE); load(); },
      }, "Anterior"),
      h("button.btn.sm", {
        type: "button", disabled: to >= data.total,
        onclick: () => { filters.offset += PAGE; load(); },
      }, "Próxima"),
    );
  }

  load();
  // Um log novo entra na tela sem F5, desde que o operador não tenha pausado
  const off = stream.on("log", debounce(() => {
    if (live && filters.offset === 0) load();
  }, 900));
  return off;
}
