/** Relatórios: números reais do banco, por período e por consultor, com exportação. */

import { api } from "../core/api.js";
import * as charts from "../charts.js";
import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import { icon } from "../core/icons.js";
import { dataTable, twoLine } from "../core/table.js";
import { card, empty, errorState, metricCard, skeletonMetrics, toast } from "../core/ui.js";

const PERIODS = [
  ["today", "Hoje"], ["yesterday", "Ontem"], ["7d", "7 dias"],
  ["30d", "30 dias"], ["month", "Mês"], ["custom", "Personalizado"],
];

const COLUMNS = [
  { label: "Consultor" },
  { label: "Solicitações", class: "num" },
  { label: "Concluídas", class: "num" },
  { label: "Erros", class: "num" },
  { label: "Taxa", class: "num" },
  { label: "Tempo médio", class: "num" },
];

export function render(root) {
  let period = "30d";
  let start = "";
  let end = "";

  const metricas = h("div");
  const volume = card("Volume por dia", { hint: "solicitações criadas" });
  const resultado = card("Concluídas × erros", { hint: "por dia" });
  const horas = card("Distribuição por hora", { hint: "quando chega pedido" });
  const duracoes = card("Tempo de processamento", { hint: "faixas" });
  const composicao = card("Composição por estado");
  const tabela = dataTable({ columns: COLUMNS, caption: "Desempenho por consultor no período" });
  const porConsultor = card("Por consultor", { hint: "no período" });
  porConsultor.set(tabela.wrap);

  const botoes = PERIODS.map(([id, label]) =>
    h("button", {
      type: "button", "aria-pressed": String(id === period),
      onclick: () => {
        period = id;
        botoes.forEach((b, i) => b.setAttribute("aria-pressed", String(PERIODS[i][0] === period)));
        datas.classList.toggle("hidden", period !== "custom");
        if (period !== "custom" || (start && end)) load();
      },
    }, label));

  const inicio = h("input", { type: "date", class: "input", "aria-label": "Data inicial",
    onchange: (e) => { start = e.target.value; if (end) load(); } });
  const fim = h("input", { type: "date", class: "input", "aria-label": "Data final",
    onchange: (e) => { end = e.target.value; if (start) load(); } });
  const datas = h("div.hidden", { style: { display: "flex", gap: "8px" } }, inicio, fim);

  mount(root,
    h("div.stack",
      h("div.toolbar",
        h("div.seg", { role: "group", "aria-label": "Período" }, botoes),
        datas,
        h("div.push", { style: { display: "flex", alignItems: "center", gap: "8px" } },
          h("span.muted", { style: { fontSize: "var(--t-meta)" } }, "Exportar:"),
          exportar("csv", "CSV"), exportar("xlsx", "Excel"), exportar("pdf", "PDF"),
        ),
      ),
      metricas,
      h("div.grid.cols-2", volume.node, resultado.node),
      h("div.grid.cols-2", horas.node, duracoes.node),
      composicao.node,
      porConsultor.node,
    ),
  );

  function exportar(formato, rotulo) {
    return h("button.btn.sm", {
      type: "button",
      onclick: () => {
        window.location.href = api.exportUrl({ format: formato, period, start, end });
        toast("info", `Exportando ${rotulo}`, "O download começa em instantes. O CPF sai mascarado "
          + "quando o mascaramento está ligado.");
      },
    }, icon("download", 14), rotulo);
  }

  async function load() {
    mount(metricas, skeletonMetrics(4));
    let dados;
    try {
      dados = await api.reports({ period, start, end });
    } catch (error) {
      mount(metricas, errorState(error.message, load));
      return;
    }

    const g = dados.general;
    mount(metricas, h("div.stack",
      h("div.metrics",
        metricCard({ label: "Solicitações", iconName: "inbox", value: fmt.int(g.total), foot: "no período" }),
        metricCard({ label: "Concluídas", iconName: "check", value: fmt.int(g.completed),
          foot: `taxa de ${fmt.pct(g.success_rate)}` }),
        metricCard({ label: "Erros", iconName: "ban", value: fmt.int(g.errors),
          foot: g.interrupted ? `${fmt.int(g.interrupted)} interrompidas` : "nenhuma interrompida",
          tone: g.errors ? "error" : null }),
        metricCard({ label: "Canceladas", iconName: "pause", value: fmt.int(g.cancelled), foot: "no período" }),
      ),
      h("div.metrics",
        metricCard({ label: "Tempo médio", iconName: "clock", value: fmt.duration(g.avg_seconds),
          foot: "por simulação concluída" }),
        metricCard({ label: "Com refinanciamento", iconName: "refresh", value: fmt.int(g.with_refin),
          foot: "ofertas encontradas" }),
        metricCard({ label: "Valor liberado", iconName: "reports", value: fmt.brl(g.total_released),
          foot: "somado no período" }),
        metricCard({ label: "Consultores ativos", iconName: "consultants",
          value: fmt.int(g.active_consultants), foot: "com pedido no período" }),
      ),
    ));

    volume.set(charts.lineChart(dados.by_day, {
      xLabel: (d) => fmt.dayLabel(d.dia), tableColumns: ["Dia", "Solicitações"],
    }) || empty({ compact: true, mark: "reports", title: "Sem dados no período" }));

    resultado.set(charts.divergingChart(dados.by_day, { xLabel: (d) => fmt.dayLabel(d.dia) })
      || empty({ compact: true, mark: "reports", title: "Sem dados no período" }));

    horas.set(charts.barChart(dados.by_hour, {
      xKey: "hora", xLabel: (d) => `${d.hora}h`, tipLabel: (d) => `${d.hora}:00 – ${d.hora}:59`,
      tableColumns: ["Hora", "Solicitações"],
    }) || empty({ compact: true, mark: "reports", title: "Sem dados no período" }));

    duracoes.set(charts.rankChart(dados.durations, {
      labelKey: "label", valueKey: "total", tableColumns: ["Faixa", "Simulações"],
    }) || empty({ compact: true, mark: "reports", title: "Nenhuma simulação concluída no período" }));

    const partes = (dados.status_breakdown || []).map((s) => ({
      label: s.label, value: s.total, color: charts.STATE_COLOR[s.status] || charts.COLOR.idle,
    }));
    composicao.set(charts.stackedBar(partes, { width: 900 })
      || empty({ compact: true, mark: "reports", title: "Sem dados no período" }));

    if (!dados.by_consultant.length) {
      tabela.message(empty({ compact: true, mark: "consultants", title: "Nenhum consultor no período" }));
      return;
    }
    tabela.rows(dados.by_consultant, (c) => ({
      cells: [
        twoLine(c.nome, c.last_activity ? `última ${fmt.relative(c.last_activity)}` : ""),
        fmt.int(c.total),
        fmt.int(c.completed),
        c.errors ? h("span", { style: { color: "var(--error)" } }, fmt.int(c.errors)) : "0",
        fmt.pct(c.success_rate),
        fmt.duration(c.avg_seconds),
      ],
    }));
  }

  load();
  return () => {};
}
