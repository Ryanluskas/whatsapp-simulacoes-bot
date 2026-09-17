/** Solicitações: lista filtrável, com estado do pedido e estado da entrega. */

import { api } from "../core/api.js";
import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import { icon } from "../core/icons.js";
import * as store from "../core/store.js";
import { deliveryBadge, stateBadge } from "../core/status.js";
import { dataTable, twoLine } from "../core/table.js";
import { debounce, empty, errorState } from "../core/ui.js";
import { openRequest } from "./request-detail.js";

const PAGE = 25;
const COLUMNS = [
  { label: "Data" },
  { label: "Consultor" },
  { label: "Cliente" },
  { label: "Status" },
  { label: "Entrega" },
  { label: "", srLabel: "Detalhes", class: "act" },
];

const state = {
  q: "", status: "", refin: "", bank: "", cpf: "",
  period: "30d", start: "", end: "", offset: 0,
};

export function render(root) {
  const tabela = dataTable({ columns: COLUMNS, caption: "Solicitações recebidas no grupo" });
  const pager = h("div.pager");

  const busca = h("input", {
    type: "search", class: "input", placeholder: "Consultor, cliente, contrato ou ID…",
    value: state.q, "aria-label": "Buscar nas solicitações",
    oninput: debounce((e) => { state.q = e.target.value.trim(); state.offset = 0; load(); }),
  });

  const statusSel = select("Status: todos", [
    ["completed", "Concluída"], ["error", "Erro"], ["queued", "Aguardando"],
    ["processing", "Processando"], ["interrupted", "Interrompida"], ["cancelled", "Cancelada"],
  ], (value) => { state.status = value; state.offset = 0; load(); });

  const periodoSel = select("Período: 30 dias", [
    ["today", "Hoje"], ["yesterday", "Ontem"], ["7d", "7 dias"],
    ["30d", "30 dias"], ["month", "Mês atual"], ["all", "Tudo"], ["custom", "Personalizado"],
  ], (value) => {
    state.period = value || "30d";
    datas.classList.toggle("hidden", state.period !== "custom");
    state.offset = 0;
    if (state.period !== "custom") load();
  });
  periodoSel.value = state.period;

  const inicio = h("input", { type: "date", class: "input", "aria-label": "Data inicial",
    onchange: (e) => { state.start = e.target.value; load(); } });
  const fim = h("input", { type: "date", class: "input", "aria-label": "Data final",
    onchange: (e) => { state.end = e.target.value; load(); } });
  const datas = h("div.hidden", { style: { display: "flex", gap: "8px" } }, inicio, fim);

  const cpf = h("input", {
    type: "text", class: "input", placeholder: "CPF", style: { width: "120px" },
    "aria-label": "Filtrar por CPF",
    oninput: debounce((e) => { state.cpf = e.target.value; state.offset = 0; load(); }),
  });
  const banco = h("input", {
    type: "text", class: "input", placeholder: "Banco", style: { width: "110px" },
    "aria-label": "Filtrar por banco",
    oninput: debounce((e) => { state.bank = e.target.value; state.offset = 0; load(); }),
  });
  const refin = select("Refin: todos", [["Sim", "Com refin"], ["Não", "Sem refin"]],
    (value) => { state.refin = value; state.offset = 0; load(); });

  // Filtros secundários ficam escondidos: a linha de cima resolve o dia a dia
  const extras = h("div.hidden", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } },
    cpf, banco, refin);
  const maisBtn = h("button.btn.sm", {
    type: "button", "aria-expanded": "false", "aria-controls": "filtros-extras",
    onclick: () => {
      const aberto = extras.classList.toggle("hidden");
      maisBtn.setAttribute("aria-expanded", String(!aberto));
    },
  }, icon("search", 14), "Mais filtros");
  extras.id = "filtros-extras";

  mount(root,
    h("div.toolbar",
      h("div.grow", busca),
      statusSel, periodoSel, datas, maisBtn,
      h("button.btn.sm.ghost.push", { type: "button", onclick: limpar }, "Limpar"),
    ),
    extras,
    tabela.wrap,
    pager,
  );

  let pedido = 0;

  async function load() {
    const token = ++pedido;
    tabela.wrap.classList.add("is-refetching");
    if (!tabela.tbody.childElementCount) tabela.loading(8);

    let dados;
    try {
      dados = await api.simulations({
        q: state.q, status: state.status, refin: state.refin, cpf: state.cpf,
        bank: state.bank, period: state.period, start: state.start, end: state.end,
        limit: PAGE, offset: state.offset,
      });
    } catch (error) {
      if (token !== pedido) return;
      tabela.message(errorState(error.message, load));
      mount(pager);
      tabela.wrap.classList.remove("is-refetching");
      return;
    }
    if (token !== pedido) return;
    tabela.wrap.classList.remove("is-refetching");
    desenhar(dados);
  }

  function desenhar({ items, total, offset }) {
    if (!items.length) {
      tabela.message(empty({
        allana: !temFiltro(),
        mark: "history",
        title: temFiltro() ? "Nenhuma solicitação com esses filtros" : "Nenhuma solicitação ainda",
        desc: temFiltro()
          ? "Tente afrouxar os filtros ou ampliar o período."
          : "Assim que um consultor mandar um pedido no grupo, ele aparece aqui.",
      }));
      mount(pager);
      return;
    }

    tabela.rows(items, (row) => ({
      onOpen: () => abrir(row.id),
      cells: [
        twoLine(fmt.dateTime(row.created_at), h("span.mono", row.request_id)),
        twoLine(row.consultant_name || "—", row.bank || ""),
        twoLine(row.customer_name || "—", h("span.mono", row.cpf_display || "")),
        stateBadge(row.status, row.status_label),
        deliveryBadge(row),
        h("button.btn.sm.ghost.icon", {
          type: "button",
          "aria-label": `Abrir detalhes de ${row.request_id}`,
          "data-tip": "Detalhes",
          onclick: () => abrir(row.id),
        }, icon("chevronRight", 16)),
      ],
    }));

    const de = offset + 1;
    const ate = Math.min(offset + items.length, total);
    mount(pager,
      h("span.info", `${de}–${ate} de ${fmt.int(total)}`),
      h("button.btn.sm", {
        type: "button", disabled: offset === 0,
        onclick: () => { state.offset = Math.max(0, offset - PAGE); load(); },
      }, "Anterior"),
      h("button.btn.sm", {
        type: "button", disabled: ate >= total,
        onclick: () => { state.offset = offset + PAGE; load(); },
      }, "Próxima"),
    );
  }

  const abrir = (id) => openRequest(id, { onChange: load });

  function limpar() {
    Object.assign(state, { q: "", status: "", refin: "", bank: "", cpf: "", period: "30d",
      start: "", end: "", offset: 0 });
    busca.value = ""; statusSel.value = ""; refin.value = ""; cpf.value = "";
    banco.value = ""; periodoSel.value = "30d"; datas.classList.add("hidden");
    load();
  }

  const temFiltro = () => Boolean(state.q || state.status || state.refin || state.bank || state.cpf);

  load();
  // Uma solicitação que termina enquanto a tela está aberta entra na lista sozinha
  return store.subscribe("metrics", debounce(() => { if (state.offset === 0) load(); }, 1200));
}

/** Usada pela busca do cabeçalho antes de navegar para esta tela. */
export function setSearchTerm(term) {
  state.q = term;
  state.offset = 0;
}

function select(placeholder, options, onchange) {
  return h("select.input", {
    "aria-label": placeholder,
    onchange: (e) => onchange(e.target.value),
  },
    h("option", { value: "" }, placeholder),
    options.map(([value, label]) => h("option", { value }, label)),
  );
}
