/** Histórico com filtros reais e timeline por solicitação. */

import { api } from "../core/api.js";
import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import * as store from "../core/store.js";
import { badge, debounce, empty, errorState, modal, skeletonRows } from "../core/ui.js";

const PAGE = 25;
const COLUMNS = ["Solicitação", "Consultor", "Cliente", "CPF", "Banco", "Contrato",
  "Status", "Refin", "Valor", "Tempo", "Data"];

const state = {
  q: "", status: "", refin: "", bank: "", consultant: "", cpf: "", contract: "",
  period: "30d", start: "", end: "", offset: 0,
};

export function render(root) {
  const tbody = h("tbody");
  const pager = h("div.pager");
  const table = h("div.table-wrap",
    h("table.data",
      h("thead", h("tr", COLUMNS.map((c, i) =>
        h("th", { class: [8, 9].includes(i) ? "num" : "" }, c)))),
      tbody,
    ),
  );

  const search = h("input", {
    type: "search", class: "input", placeholder: "Consultor, contrato, banco ou ID…",
    value: state.q, "aria-label": "Buscar no histórico",
    oninput: debounce((e) => { state.q = e.target.value.trim(); state.offset = 0; load(); }),
  });

  const statusSel = select("Status: todos", [
    ["completed", "Concluída"], ["error", "Erro"], ["queued", "Aguardando"],
    ["processing", "Processando"], ["interrupted", "Interrompida"], ["cancelled", "Cancelada"],
  ], (value) => { state.status = value; state.offset = 0; load(); });

  const refinSel = select("Refin: todos", [["Sim", "Com refin"], ["Não", "Sem refin"]],
    (value) => { state.refin = value; state.offset = 0; load(); });

  const periodSel = select("Período: 30 dias", [
    ["today", "Hoje"], ["yesterday", "Ontem"], ["7d", "7 dias"],
    ["30d", "30 dias"], ["month", "Mês atual"], ["all", "Tudo"], ["custom", "Personalizado"],
  ], (value) => {
    state.period = value || "30d";
    custom.classList.toggle("hidden", state.period !== "custom");
    state.offset = 0;
    if (state.period !== "custom") load();
  });
  periodSel.value = "30d";

  const startInput = h("input", { type: "date", class: "input", "aria-label": "Data inicial",
    onchange: (e) => { state.start = e.target.value; load(); } });
  const endInput = h("input", { type: "date", class: "input", "aria-label": "Data final",
    onchange: (e) => { state.end = e.target.value; load(); } });
  const custom = h("div.hidden", { style: { display: "flex", gap: "8px" } }, startInput, endInput);

  const cpfInput = h("input", {
    type: "text", class: "input", placeholder: "CPF", style: { width: "128px" },
    "aria-label": "Filtrar por CPF",
    oninput: debounce((e) => { state.cpf = e.target.value; state.offset = 0; load(); }),
  });
  const bankInput = h("input", {
    type: "text", class: "input", placeholder: "Banco", style: { width: "110px" },
    "aria-label": "Filtrar por banco",
    oninput: debounce((e) => { state.bank = e.target.value; state.offset = 0; load(); }),
  });

  mount(root,
    h("div.section-head", h("h3", "Histórico de simulações")),
    // Uma linha de filtros acima de tudo que ela recorta
    h("div.toolbar",
      h("div.grow", search),
      statusSel, refinSel, cpfInput, bankInput, periodSel, custom,
      h("button.btn.sm.ghost.push", { type: "button", onclick: reset }, "Limpar"),
    ),
    table,
    pager,
  );

  let request = 0;

  async function load() {
    const token = ++request;
    table.classList.add("is-refetching");
    if (!tbody.childElementCount) mount(tbody, skeletonRows(8, COLUMNS.length));

    try {
      const data = await api.simulations({
        q: state.q, status: state.status, refin: state.refin, cpf: state.cpf,
        bank: state.bank, period: state.period, start: state.start, end: state.end,
        limit: PAGE, offset: state.offset,
      });
      if (token !== request) return;
      draw(data);
    } catch (error) {
      if (token !== request) return;
      mount(tbody, h("tr", h("td", { colspan: COLUMNS.length },
        errorState(error.message, load))));
      mount(pager);
    } finally {
      if (token === request) table.classList.remove("is-refetching");
    }
  }

  function draw({ items, total, offset }) {
    if (!items.length) {
      mount(tbody, h("tr", h("td", { colspan: COLUMNS.length }, empty({
        mark: "history",
        title: "Nenhuma simulação encontrada",
        desc: hasFilters() ? "Tente afrouxar os filtros." : "As simulações aparecem aqui assim que forem processadas.",
      }))));
      mount(pager);
      return;
    }

    mount(tbody, items.map((row) => h("tr", {
      dataset: { clickable: "true" }, tabindex: "0",
      onclick: () => openSimulation(row.id),
      onkeydown: (e) => { if (e.key === "Enter") openSimulation(row.id); },
    },
      h("td", h("span.mono", { style: { fontSize: "12px" } }, row.request_id)),
      h("td.cell-strong", row.consultant_name || "—"),
      h("td", row.customer_name || "—"),
      h("td", h("span.mono", row.cpf_display || "—")),
      h("td", row.bank || "—"),
      h("td", h("span.mono", row.contract || "—")),
      h("td", badge(row.status, row.status_label)),
      h("td", refinCell(row)),
      h("td.num", row.status === "completed" ? fmt.brl(row.reduction_value) : "—"),
      h("td.num.muted", fmt.duration(row.processing_seconds)),
      h("td.num.muted", fmt.dateTime(row.created_at)),
    )));

    const from = offset + 1;
    const to = Math.min(offset + items.length, total);
    mount(pager,
      h("span", `${from}–${to} de ${fmt.int(total)}`),
      h("button.btn.sm", {
        type: "button", disabled: offset === 0,
        onclick: () => { state.offset = Math.max(0, offset - PAGE); load(); },
      }, "Anterior"),
      h("button.btn.sm", {
        type: "button", disabled: to >= total,
        onclick: () => { state.offset = offset + PAGE; load(); },
      }, "Próxima"),
    );
  }

  function reset() {
    Object.assign(state, { q: "", status: "", refin: "", bank: "", cpf: "", period: "30d", start: "", end: "", offset: 0 });
    search.value = ""; statusSel.value = ""; refinSel.value = ""; cpfInput.value = "";
    bankInput.value = ""; periodSel.value = "30d"; custom.classList.add("hidden");
    load();
  }

  const hasFilters = () => Boolean(state.q || state.status || state.refin || state.bank || state.cpf);

  load();
  // Uma simulação que termina enquanto a tela está aberta entra na lista sozinha
  const off = store.subscribe("metrics", debounce(() => { if (state.offset === 0) load(); }, 1200));
  return off;
}

/** Usada pela busca global do topo antes de navegar para esta tela. */
export function setSearchTerm(term) {
  state.q = term;
  state.offset = 0;
}

function refinCell(row) {
  if (row.status !== "completed") return h("span.muted", "—");
  const yes = row.refin === "Sim";
  return h("span", { style: { color: yes ? "var(--st-done)" : "var(--ink-3)" } }, yes ? "Sim" : "Não");
}

function select(placeholder, options, onchange) {
  return h("select.input", {
    style: { width: "auto", minWidth: "132px" }, "aria-label": placeholder,
    onchange: (e) => onchange(e.target.value),
  },
    h("option", { value: "" }, placeholder),
    options.map(([value, label]) => h("option", { value }, label)),
  );
}

/* ------------------------------------------------------------- detalhe --- */

export async function openSimulation(id) {
  const body = h("div", h("div.skeleton", { style: { height: "220px" } }));
  modal(`Solicitação #${id}`, body);

  let data;
  try {
    data = await api.simulation(id);
  } catch (error) {
    mount(body, errorState(error.message, () => openSimulation(id)));
    return;
  }

  const s = data.simulation;
  mount(body,
    h("div.grid.cols-2", { style: { gap: "20px" } },
      h("div.kv",
        row("Solicitação", h("span.mono", s.request_id)),
        row("Consultor", s.consultant_name || "—"),
        row("Cliente", s.customer_name || "—"),
        row("CPF", h("span.mono", s.cpf_display || "—")),
        row("Banco", s.bank || "—"),
        row("Contrato", h("span.mono", s.contract || "—")),
      ),
      h("div.kv",
        row("Status", badge(s.status, s.status_label)),
        row("Refinanciamento", s.refin || "—"),
        row("Valor disponível", fmt.brl(s.reduction_value)),
        row("Soma das parcelas", fmt.brl(s.installment_sum)),
        row("Saldo devedor", fmt.brl(s.debt_sum)),
        row("Margem", s.margin || "—"),
        row("Tempo", fmt.duration(s.processing_seconds)),
        row("Tentativas", `${s.attempts || 0} de ${s.max_attempts || 1}`),
      ),
    ),
    s.error_message ? h("div", {
      style: {
        marginTop: "18px", padding: "10px 12px", borderRadius: "6px",
        background: "var(--st-error-bg)", fontSize: "13px",
      },
    }, `Erro: ${s.error_message}`) : null,

    sectionTitle("Timeline"),
    data.timeline.length
      ? h("ul.timeline", data.timeline.map((event) => h("li",
        h("span.tl-time", fmt.time(event.created_at)),
        h("span.tl-node", { style: { background: nodeColor(event) } }),
        h("div.tl-body",
          h("div.t", event.stage_label || event.title),
          event.detail && h("div.d", event.detail),
        ),
      )))
      : h("p.muted", { style: { fontSize: "13px" } }, "Sem eventos registrados para esta solicitação."),

    sectionTitle("Mensagens"),
    data.messages.length
      ? h("ul.timeline", data.messages.map((message) => h("li",
        h("span.tl-time", fmt.time(message.created_at)),
        h("span.tl-node", {
          style: { background: message.direction === "in" ? "var(--st-processing)" : "var(--st-done)" },
        }),
        h("div.tl-body",
          h("div.t", message.direction === "in" ? "Recebida"
            : (message.kind === "image" ? "Enviada · imagem" : "Enviada")),
          h("div.d", { style: { whiteSpace: "pre-wrap" } }, fmt.truncate(message.text, 460)),
          // O comprovante exato que o consultor recebeu no grupo
          message.media_url
            ? h("a", {
              href: message.media_url, target: "_blank", rel: "noopener",
              title: "Abrir a imagem enviada",
            },
              h("img", {
                src: message.media_url,
                alt: "Imagem enviada ao grupo com o resultado da simulação",
                loading: "lazy",
                style: {
                  display: "block", marginTop: "8px", width: "100%", maxWidth: "440px",
                  borderRadius: "8px", border: "1px solid var(--line)",
                },
              }))
            : null,
        ),
      )))
      : h("p.muted", { style: { fontSize: "13px" } }, "Nenhuma mensagem registrada."),

    data.contracts?.length ? sectionTitle("Contratos encontrados") : null,
    data.contracts?.length
      ? h("div.table-wrap", h("table.data",
        h("thead", h("tr", Object.keys(data.contracts[0]).map((k) => h("th", k)))),
        h("tbody", data.contracts.map((contract) =>
          h("tr", Object.values(contract).map((v) => h("td", String(v ?? "—")))))),
      ))
      : null,
  );
}

const row = (label, value) => h("div", h("span.k", label), h("span.v", value));

const sectionTitle = (text) => h("h4", {
  style: {
    margin: "22px 0 10px", color: "var(--ink-3)", fontSize: "11px",
    letterSpacing: "0.12em", textTransform: "uppercase",
  },
}, text);

function nodeColor(event) {
  if (event.level === "error") return "var(--st-error)";
  if (event.level === "success") return "var(--st-done)";
  if (event.level === "warning") return "var(--st-queued)";
  return "var(--st-processing)";
}
