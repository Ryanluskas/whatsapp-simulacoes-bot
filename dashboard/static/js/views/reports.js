/** Relatórios: números reais do banco, por período e por consultor, com exportação. */

import { api } from "../core/api.js";
import * as charts from "../charts.js";
import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import { chip, empty, errorState, skeletonTiles, toast } from "../core/ui.js";

const PERIODS = [
  ["today", "Hoje"], ["yesterday", "Ontem"], ["7d", "7 dias"],
  ["30d", "30 dias"], ["month", "Mês"], ["custom", "Personalizado"],
];

export function render(root) {
  let period = "30d";
  let start = "";
  let end = "";

  const tiles = h("div.grid.kpi-grid");
  const volume = card("Volume por dia");
  const outcome = card("Concluídas × erros");
  const hours = card("Distribuição por hora");
  const durations = card("Tempo de processamento");
  const statusCard = card("Composição por estado");
  const consultantsBody = h("tbody");

  const buttons = PERIODS.map(([id, label]) =>
    h("button", {
      type: "button", "aria-pressed": String(id === period),
      onclick: () => {
        period = id;
        buttons.forEach((b, i) => b.setAttribute("aria-pressed", String(PERIODS[i][0] === period)));
        custom.classList.toggle("hidden", period !== "custom");
        if (period !== "custom" || (start && end)) load();
      },
    }, label));

  const startInput = h("input", { type: "date", class: "input", "aria-label": "Data inicial",
    onchange: (e) => { start = e.target.value; if (end) load(); } });
  const endInput = h("input", { type: "date", class: "input", "aria-label": "Data final",
    onchange: (e) => { end = e.target.value; if (start) load(); } });
  const custom = h("div.hidden", { style: { display: "flex", gap: "8px" } }, startInput, endInput);

  mount(root,
    h("div.section-head",
      h("h3", "Relatórios"),
      h("div.spacer"),
      h("div.seg", buttons),
      custom,
    ),
    h("div.toolbar",
      h("span.muted", { style: { fontSize: "12px" } }, "Exportar:"),
      exportButton("csv", "CSV"),
      exportButton("xlsx", "Excel"),
      exportButton("pdf", "PDF"),
      h("span.muted.push", { style: { fontSize: "11px" } },
        "Os arquivos respeitam o mascaramento de CPF configurado."),
    ),
    tiles,
    h("div.grid.cols-2", { style: { marginTop: "16px" } }, volume.node, outcome.node),
    h("div.grid.cols-2", { style: { marginTop: "16px" } }, hours.node, durations.node),
    h("div", { style: { marginTop: "16px" } }, statusCard.node),
    h("div.card", { style: { marginTop: "16px" } },
      h("header", h("h3", "Por consultor")),
      h("div.table-wrap",
        h("table.data",
          h("thead", h("tr",
            h("th", "Consultor"), h("th.num", "Solicitações"), h("th.num", "Concluídas"),
            h("th.num", "Erros"), h("th.num", "Com refin"), h("th.num", "Tempo médio"),
            h("th.num", "Taxa"), h("th.num", "Última atividade"),
          )),
          consultantsBody,
        ),
      ),
    ),
  );

  function exportButton(format, label) {
    return h("button.btn.sm", {
      type: "button",
      onclick: () => {
        window.location.href = api.exportUrl({ format, period, start, end });
        toast("info", `Exportando ${label}`, "O download começa em instantes.");
      },
    }, label);
  }

  async function load() {
    mount(tiles, skeletonTiles(8));
    let data;
    try {
      data = await api.reports({ period, start, end });
    } catch (error) {
      mount(tiles, errorState(error.message, load));
      return;
    }

    const g = data.general;
    mount(tiles,
      tile("Solicitações", fmt.int(g.total), "no período"),
      tile("Concluídas", fmt.int(g.completed), "", "done"),
      tile("Erros", fmt.int(g.errors), g.interrupted ? `${g.interrupted} interrompidas` : "",
        g.errors ? "error" : null),
      tile("Canceladas", fmt.int(g.cancelled), ""),
      tile("Taxa de sucesso", fmt.pct(g.success_rate), "concluídas ÷ finalizadas"),
      tile("Tempo médio", fmt.duration(g.avg_seconds), "por simulação"),
      tile("Com refinanciamento", fmt.int(g.with_refin), "ofertas encontradas", "brand"),
      tile("Valor liberado", fmt.brl(g.total_released), "somado no período", "brand"),
    );

    volume.set(charts.lineChart(data.by_day, {
      xLabel: (d) => fmt.dayLabel(d.dia), tableColumns: ["Dia", "Solicitações"],
    }) || empty({ mark: "reports", title: "Sem dados no período" }));

    outcome.set(charts.divergingChart(data.by_day, { xLabel: (d) => fmt.dayLabel(d.dia) })
      || empty({ mark: "reports", title: "Sem dados no período" }));

    hours.set(charts.barChart(data.by_hour, {
      xKey: "hora", xLabel: (d) => `${d.hora}h`, tipLabel: (d) => `${d.hora}:00 – ${d.hora}:59`,
      tableColumns: ["Hora", "Solicitações"],
    }) || empty({ mark: "reports", title: "Sem dados no período" }));

    durations.set(charts.rankChart(data.durations, {
      labelKey: "label", valueKey: "total", tableColumns: ["Faixa", "Simulações"],
    }) || empty({ mark: "reports", title: "Nenhuma simulação concluída no período" }));

    const breakdown = (data.status_breakdown || []).map((s) => ({
      label: s.label, value: s.total, color: charts.STATE_COLOR[s.status] || charts.COLOR.idle,
    }));
    statusCard.set(charts.stackedBar(breakdown, { width: 900 })
      || empty({ mark: "reports", title: "Sem dados no período" }));

    if (!data.by_consultant.length) {
      mount(consultantsBody, h("tr", h("td", { colspan: 8 },
        empty({ mark: "consultants", title: "Nenhum consultor no período" }))));
    } else {
      mount(consultantsBody, data.by_consultant.map((c) => h("tr",
        h("td.cell-strong", c.nome),
        h("td.num", fmt.int(c.total)),
        h("td.num", fmt.int(c.completed)),
        h("td.num", c.errors ? h("span", { style: { color: "var(--st-error)" } }, fmt.int(c.errors)) : "0"),
        h("td.num", fmt.int(c.with_refin)),
        h("td.num", fmt.duration(c.avg_seconds)),
        h("td.num", fmt.pct(c.success_rate)),
        h("td.num.muted", fmt.dateTime(c.last_activity)),
      )));
    }
  }

  load();
  return () => {};
}

function card(title) {
  const body = h("div");
  const node = h("div.card", h("header", h("h3", title)), body);
  return { node, set: (content) => mount(body, content) };
}

function tile(label, value, foot, accent = null) {
  return h("div.tile", { dataset: accent ? { accent } : {} },
    h("div.label", label), h("div.value", value), foot && h("div.foot", foot));
}
