/** Gestão de consultores: cadastro, edição, ativação e desempenho. */

import { api } from "../core/api.js";
import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import {
  badge, closeModal, confirmAction, empty, errorState, modal, skeletonRows, toast,
} from "../core/ui.js";
import { setSearchTerm } from "./history.js";

const COLUMNS = ["Consultor", "WhatsApp", "Status", "Solicitações", "Concluídas",
  "Erros", "Taxa", "Tempo médio", "Última atividade", ""];

export function render(root, { navigate }) {
  const tbody = h("tbody");
  let period = "30d";

  const periodSel = h("select.input", {
    style: { width: "auto" }, "aria-label": "Período das estatísticas",
    onchange: (e) => { period = e.target.value; load(); },
  },
    h("option", { value: "7d" }, "Últimos 7 dias"),
    h("option", { value: "30d", selected: true }, "Últimos 30 dias"),
    h("option", { value: "month" }, "Mês atual"),
  );

  mount(root,
    h("div.section-head",
      h("h3", "Consultores"),
      h("span.hint", "identificados automaticamente pelo número do WhatsApp"),
      h("div.spacer"),
      periodSel,
      h("button.btn.primary", { type: "button", onclick: () => openForm(null, load) }, "+ Cadastrar"),
    ),
    h("div.table-wrap",
      h("table.data",
        h("thead", h("tr", COLUMNS.map((c, i) =>
          h("th", { class: i >= 3 && i <= 7 ? "num" : "" }, c)))),
        tbody,
      ),
    ),
  );

  async function load() {
    mount(tbody, skeletonRows(5, COLUMNS.length));
    let data;
    try {
      data = await api.consultants({ period });
    } catch (error) {
      mount(tbody, h("tr", h("td", { colspan: COLUMNS.length }, errorState(error.message, load))));
      return;
    }

    if (!data.items.length) {
      mount(tbody, h("tr", h("td", { colspan: COLUMNS.length }, empty({
        mark: "consultants",
        title: "Nenhum consultor ainda",
        desc: "Quem mandar uma solicitação no grupo é cadastrado automaticamente. "
            + "Você também pode cadastrar manualmente.",
      }))));
      return;
    }

    mount(tbody, data.items.map((c) => h("tr",
      h("td.cell-strong", c.name),
      h("td", h("span.mono", { style: { fontSize: "12px" } }, c.phone_display || "—")),
      h("td", badge(c.active ? "completed" : "cancelled", c.active ? "Ativo" : "Inativo")),
      h("td.num", fmt.int(c.total)),
      h("td.num", fmt.int(c.completed)),
      h("td.num", c.errors
        ? h("span", { style: { color: "var(--st-error)" } }, fmt.int(c.errors)) : "0"),
      h("td.num", c.total ? fmt.pct(c.success_rate) : "—"),
      h("td.num", fmt.duration(c.avg_seconds)),
      h("td.num.muted", c.last_activity ? fmt.relative(c.last_activity) : "nunca"),
      h("td", { style: { whiteSpace: "nowrap" } },
        h("button.btn.sm.ghost", {
          type: "button", title: "Ver histórico deste consultor",
          onclick: () => { setSearchTerm(c.name); navigate("history"); },
        }, "Histórico"),
        h("button.btn.sm.ghost", {
          type: "button", onclick: () => openForm(c, load),
        }, "Editar"),
        h("button.btn.sm.ghost", {
          type: "button",
          onclick: () => toggleActive(c, load),
        }, c.active ? "Desativar" : "Ativar"),
      ),
    )));
  }

  load();
  return () => {};
}

function toggleActive(consultant, reload) {
  if (consultant.active) {
    confirmAction(
      "Desativar consultor",
      `${consultant.name} deixa de aparecer como ativo. O histórico é preservado e, `
      + "se ele mandar uma nova solicitação, volta a ser reativado automaticamente.",
      async () => {
        try {
          await api.deactivateConsultant(consultant.id);
          toast("success", "Consultor desativado", consultant.name);
          reload();
        } catch (error) {
          toast("error", "Não foi possível desativar", error.message);
        }
      },
      "Desativar",
    );
    return;
  }
  api.updateConsultant(consultant.id, { active: true })
    .then(() => { toast("success", "Consultor ativado", consultant.name); reload(); })
    .catch((error) => toast("error", "Não foi possível ativar", error.message));
}

function openForm(consultant, reload) {
  const isEdit = Boolean(consultant);
  const name = h("input.input", { type: "text", value: consultant?.name || "", required: true });
  const phone = h("input.input", {
    type: "text", value: consultant?.phone || consultant?.phone_display || "",
    placeholder: "5567999998888",
  });
  const notes = h("input.input", { type: "text", value: consultant?.notes || "" });
  const error = h("div.login-error");
  const save = h("button.btn.primary", { type: "submit" }, isEdit ? "Salvar" : "Cadastrar");

  const form = h("form", {
    style: { display: "grid", gap: "14px" },
    onsubmit: async (event) => {
      event.preventDefault();
      const value = name.value.trim();
      if (!value) { error.textContent = "Informe o nome."; return; }
      save.disabled = true;
      error.textContent = "";
      const payload = { name: value, phone: phone.value.trim(), notes: notes.value.trim() };
      try {
        if (isEdit) await api.updateConsultant(consultant.id, payload);
        else await api.createConsultant(payload);
        toast("success", isEdit ? "Consultor atualizado" : "Consultor cadastrado", value);
        closeModal();
        reload();
      } catch (err) {
        error.textContent = err.message;
        save.disabled = false;
      }
    },
  },
    h("label.field", "Nome exibido", name),
    h("label.field", "WhatsApp (só números, com DDI)", phone),
    h("label.field", "Observações", notes),
    isEdit
      ? h("p.muted", { style: { fontSize: "12px", margin: 0 } },
        "Ao salvar o nome aqui, ele deixa de ser sobrescrito pelo nome do WhatsApp.")
      : null,
    error,
    h("div", { style: { display: "flex", gap: "8px", justifyContent: "flex-end" } },
      h("button.btn", { type: "button", onclick: closeModal }, "Cancelar"),
      save,
    ),
  );

  modal(isEdit ? `Editar ${consultant.name}` : "Novo consultor", form);
  name.focus();
}
