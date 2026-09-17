/** Logs técnicos com filtros por nível, serviço, solicitação e busca livre. */

import { api } from "../core/api.js";
import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import { stateBadge } from "../core/status.js";
import * as stream from "../core/stream.js";
import { dataTable } from "../core/table.js";
import { debounce, empty, errorState } from "../core/ui.js";

const PAGE = 100;
const COLUMNS = [
  { label: "Hora", class: "num" },
  { label: "Nível" },
  { label: "Serviço" },
  { label: "Mensagem" },
  { label: "Solicitação" },
  { label: "Consultor" },
];

export function render(root) {
  const filtros = { level: "", service: "", q: "", request_id: "", offset: 0 };
  let aoVivo = true;

  const tabela = dataTable({ columns: COLUMNS, caption: "Registro técnico do sistema" });
  const pager = h("div.pager");
  const servicoSel = h("select.input", {
    "aria-label": "Filtrar por serviço",
    onchange: (e) => { filtros.service = e.target.value; filtros.offset = 0; load(); },
  }, h("option", { value: "" }, "Serviço: todos"));

  const liveBtn = h("button.btn.sm.primary", {
    type: "button", "aria-pressed": "true",
    onclick: () => {
      aoVivo = !aoVivo;
      liveBtn.setAttribute("aria-pressed", String(aoVivo));
      liveBtn.classList.toggle("primary", aoVivo);
      liveBtn.textContent = aoVivo ? "Ao vivo" : "Pausado";
    },
  }, "Ao vivo");

  mount(root,
    h("div.stack",
      h("div.toolbar",
        h("div.grow",
          h("input.input", {
            type: "search", placeholder: "Buscar na mensagem…", "aria-label": "Buscar nos logs",
            oninput: debounce((e) => { filtros.q = e.target.value.trim(); filtros.offset = 0; load(); }),
          }),
        ),
        h("select.input", {
          "aria-label": "Filtrar por nível",
          onchange: (e) => { filtros.level = e.target.value; filtros.offset = 0; load(); },
        },
          h("option", { value: "" }, "Nível: todos"),
          ["INFO", "WARNING", "ERROR", "DEBUG"].map((l) => h("option", { value: l }, l)),
        ),
        servicoSel,
        h("input.input", {
          type: "text", placeholder: "REQ000123", style: { width: "132px", flex: "none" },
          "aria-label": "Filtrar por solicitação",
          oninput: debounce((e) => { filtros.request_id = e.target.value.trim(); filtros.offset = 0; load(); }),
        }),
        liveBtn,
      ),
      tabela.wrap,
      pager,
    ),
  );

  async function load() {
    if (!tabela.tbody.childElementCount) tabela.loading(8);
    let dados;
    try {
      dados = await api.logs({ ...filtros, limit: PAGE });
    } catch (error) {
      tabela.message(errorState(error.message, load));
      return;
    }

    if (servicoSel.options.length === 1 && dados.services?.length) {
      dados.services.forEach((servico) =>
        servicoSel.appendChild(h("option", { value: servico }, servico)));
    }

    if (!dados.items.length) {
      tabela.message(empty({
        mark: "logs", title: "Nenhum log para este filtro",
        desc: "Ajuste os filtros ou aguarde a próxima atividade do sistema.",
      }));
      mount(pager);
      return;
    }

    tabela.rows(dados.items, (log) => ({
      cells: [
        h("span.mono.muted", fmt.dateTime(log.created_at)),
        stateBadge(log.level, log.level),
        log.service || "—",
        h("span", { style: { display: "block", maxWidth: "62ch" } }, log.message || ""),
        log.request_id ? h("span.mono", log.request_id) : h("span.faint", "—"),
        log.consultant || h("span.faint", "—"),
      ],
    }));

    const ate = Math.min(filtros.offset + dados.items.length, dados.total);
    mount(pager,
      h("span.info", `${filtros.offset + 1}–${ate} de ${fmt.int(dados.total)}`),
      h("button.btn.sm", {
        type: "button", disabled: filtros.offset === 0,
        onclick: () => { filtros.offset = Math.max(0, filtros.offset - PAGE); load(); },
      }, "Anterior"),
      h("button.btn.sm", {
        type: "button", disabled: ate >= dados.total,
        onclick: () => { filtros.offset += PAGE; load(); },
      }, "Próxima"),
    );
  }

  load();
  // Um log novo entra na tela sem F5, desde que o operador não tenha pausado
  return stream.on("log", debounce(() => {
    if (aoVivo && filtros.offset === 0) load();
  }, 900));
}
