/** Consultores: cadastro, edição, ativação e desempenho. */

import { api } from "../core/api.js";
import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import { icon } from "../core/icons.js";
import { badge } from "../core/status.js";
import { dataTable, twoLine } from "../core/table.js";
import { closeModal, confirmAction, empty, errorState, modal, toast } from "../core/ui.js";
import { setSearchTerm } from "./history.js";

const COLUMNS = [
  { label: "Consultor" },
  { label: "Situação" },
  { label: "Solicitações", class: "num" },
  { label: "Taxa de sucesso", class: "num" },
  { label: "Última atividade", class: "num" },
  { label: "", srLabel: "Ações", class: "act" },
];

export function render(root, { navigate }) {
  const tabela = dataTable({ columns: COLUMNS, caption: "Consultores e desempenho no período" });
  let period = "30d";

  const periodoSel = h("select.input", {
    "aria-label": "Período das estatísticas",
    onchange: (e) => { period = e.target.value; load(); },
  },
    h("option", { value: "7d" }, "Últimos 7 dias"),
    h("option", { value: "30d", selected: true }, "Últimos 30 dias"),
    h("option", { value: "month" }, "Mês atual"),
  );

  mount(root,
    h("div.stack",
      h("div.toolbar",
        h("span.muted", { style: { fontSize: "var(--t-meta-lg)" } },
          "Quem manda pedido no grupo é cadastrado automaticamente."),
        h("div.push", { style: { display: "flex", gap: "8px" } },
          periodoSel,
          h("button.btn.primary", { type: "button", onclick: () => abrirForm(null, load) },
            icon("plus", 15), "Cadastrar"),
        ),
      ),
      tabela.wrap,
    ),
  );

  async function load() {
    if (!tabela.tbody.childElementCount) tabela.loading(5);
    let dados;
    try {
      dados = await api.consultants({ period });
    } catch (error) {
      tabela.message(errorState(error.message, load));
      return;
    }

    if (!dados.items.length) {
      tabela.message(empty({
        allana: true,
        title: "Nenhum consultor ainda",
        desc: "Quem mandar uma solicitação no grupo entra nesta lista sozinho. "
            + "Você também pode cadastrar manualmente.",
      }));
      return;
    }

    tabela.rows(dados.items, (c) => ({
      cells: [
        twoLine(c.name, h("span.mono", c.phone_display || "—")),
        c.active ? badge("success", "Ativo", "check") : badge("neutral", "Inativo", "pause"),
        twoLine(fmt.int(c.total),
          `${fmt.int(c.completed)} concluídas · ${fmt.int(c.errors)} com erro`),
        twoLine(c.total ? fmt.pct(c.success_rate) : "—",
          c.avg_seconds ? `média ${fmt.duration(c.avg_seconds)}` : ""),
        c.last_activity ? fmt.relative(c.last_activity) : h("span.faint", "nunca"),
        h("div", { style: { display: "inline-flex", gap: "4px" } },
          h("button.btn.sm.ghost.icon", {
            type: "button", "aria-label": `Ver solicitações de ${c.name}`, "data-tip": "Solicitações",
            onclick: () => { setSearchTerm(c.name); navigate("history"); },
          }, icon("history", 15)),
          h("button.btn.sm.ghost.icon", {
            type: "button", "aria-label": `Editar ${c.name}`, "data-tip": "Editar",
            onclick: () => abrirForm(c, load),
          }, icon("edit", 15)),
          h("button.btn.sm.ghost.icon", {
            type: "button",
            "aria-label": `${c.active ? "Desativar" : "Ativar"} ${c.name}`,
            "data-tip": c.active ? "Desativar" : "Ativar",
            onclick: () => alternar(c, load),
          }, icon("power", 15)),
        ),
      ],
    }));
  }

  load();
  return () => {};
}

function alternar(consultor, recarregar) {
  if (consultor.active) {
    confirmAction(
      "Desativar consultor",
      `${consultor.name} deixa de aparecer como ativo. O histórico é preservado e, `
      + "se ele mandar uma nova solicitação, volta a ser reativado automaticamente.",
      async () => {
        try {
          await api.deactivateConsultant(consultor.id);
          toast("success", "Consultor desativado", consultor.name);
          recarregar();
        } catch (error) {
          toast("error", "Não foi possível desativar", error.message);
        }
      },
      "Desativar",
      { variant: "caution", iconName: "power" },
    );
    return;
  }
  api.updateConsultant(consultor.id, { active: true })
    .then(() => { toast("success", "Consultor ativado", consultor.name); recarregar(); })
    .catch((error) => toast("error", "Não foi possível ativar", error.message));
}

function abrirForm(consultor, recarregar) {
  const edicao = Boolean(consultor);
  const nome = h("input.input", { type: "text", value: consultor?.name || "", required: true });
  const telefone = h("input.input", {
    type: "text", value: consultor?.phone || consultor?.phone_display || "",
    placeholder: "5567999998888",
  });
  const notas = h("input.input", { type: "text", value: consultor?.notes || "" });
  const erro = h("div.form-error");
  const salvar = h("button.btn.primary", { type: "submit" }, edicao ? "Salvar" : "Cadastrar");

  const form = h("form", {
    style: { display: "grid", gap: "14px" },
    onsubmit: async (event) => {
      event.preventDefault();
      const valor = nome.value.trim();
      if (!valor) { erro.textContent = "Informe o nome."; return; }
      salvar.disabled = true;
      erro.textContent = "";
      const payload = { name: valor, phone: telefone.value.trim(), notes: notas.value.trim() };
      try {
        if (edicao) await api.updateConsultant(consultor.id, payload);
        else await api.createConsultant(payload);
        toast("success", edicao ? "Consultor atualizado" : "Consultor cadastrado", valor);
        closeModal();
        recarregar();
      } catch (err) {
        erro.textContent = err.message;
        salvar.disabled = false;
      }
    },
  },
    h("label.field", "Nome exibido", nome),
    h("label.field", "WhatsApp (só números, com DDI)", telefone),
    h("label.field", "Observações", notas),
    edicao
      ? h("p.muted", { style: { fontSize: "var(--t-meta)", margin: 0 } },
        "Ao salvar o nome aqui, ele deixa de ser sobrescrito pelo nome do WhatsApp.")
      : null,
    erro,
    h("div.modal-actions",
      h("button.btn", { type: "button", onclick: closeModal }, "Cancelar"),
      salvar,
    ),
  );

  modal(edicao ? `Editar ${consultor.name}` : "Novo consultor", form);
  nome.focus();
}
