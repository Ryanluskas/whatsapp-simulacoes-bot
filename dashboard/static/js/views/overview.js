/** Visão geral: os números do dia, a cadência recente e o funil operacional. */

import * as charts from "../charts.js";
import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import * as store from "../core/store.js";
import { empty, skeletonTiles } from "../core/ui.js";
import { feedRow } from "../feed.js";

export function render(root, { navigate }) {
  const tiles = h("div.grid.kpi-grid");
  const volume = card("Volume por dia", "últimos 30 dias");
  const outcome = card("Resultado por dia", "concluídas acima · erros abaixo");
  const hours = card("Horários de maior uso", "últimos 30 dias");
  const consultants = card("Consultores hoje", "por volume");
  const funnel = card("Funil de hoje", "da mensagem à resposta");
  const activity = card("Atividade recente", "ao vivo");

  mount(root,
    h("div.section-head",
      h("h3", "Visão geral"),
      h("span.hint", "Atualiza sozinho — não precisa recarregar"),
      h("div.spacer"),
      h("button.btn.sm", { type: "button", onclick: () => navigate("monitor") }, "Abrir monitor"),
    ),
    tiles,
    h("div.grid.cols-2", { style: { marginTop: "16px" } }, volume.node, outcome.node),
    h("div.grid.cols-2", { style: { marginTop: "16px" } }, hours.node, consultants.node),
    h("div.grid.cols-2", { style: { marginTop: "16px" } }, funnel.node, activity.node),
  );

  const draw = (metrics) => {
    if (!metrics) {
      mount(tiles, skeletonTiles(8));
      return;
    }
    const k = metrics.kpis;

    mount(tiles,
      tile("Hoje", fmt.int(k.today), `${fmt.int(k.today_completed)} concluídas`, "brand",
        charts.sparkline((metrics.by_day || []).slice(-12).map((d) => d.total))),
      tile("Na fila", fmt.int(k.queued), k.processing ? `${k.processing} em execução` : "nenhuma em execução"),
      tile("Concluídas", fmt.int(k.completed), "no período", "done"),
      tile("Com erro", fmt.int(k.errors), k.interrupted ? `${k.interrupted} interrompidas` : "no período",
        k.errors > 0 ? "error" : null),
      tile("Taxa de sucesso", fmt.pct(k.success_rate), "concluídas ÷ finalizadas"),
      tile("Tempo médio", fmt.duration(k.avg_seconds), "por simulação concluída"),
      tile("Com refin", fmt.int(k.with_refin), "ofertas encontradas"),
      tile("Valor liberado", fmt.brl(k.total_released), "somado no período", "brand"),
    );

    volume.set(charts.lineChart(metrics.by_day, {
      xLabel: (d) => fmt.dayLabel(d.dia),
      tableColumns: ["Dia", "Solicitações"],
    }) || empty({ mark: "reports", title: "Sem simulações ainda", desc: "O gráfico aparece na primeira solicitação." }));

    outcome.set(charts.divergingChart(metrics.by_day, {
      xLabel: (d) => fmt.dayLabel(d.dia),
    }) || empty({ mark: "reports", title: "Sem resultados ainda" }));

    hours.set(charts.barChart(metrics.by_hour, {
      xKey: "hora", xLabel: (d) => `${d.hora}h`, tipLabel: (d) => `${d.hora}:00 – ${d.hora}:59`,
      tableColumns: ["Hora", "Solicitações"],
    }) || empty({ mark: "reports", title: "Sem histórico de horários" }));

    consultants.set(charts.rankChart(metrics.by_consultant, {
      tableColumns: ["Consultor", "Solicitações hoje"],
    }) || empty({ mark: "consultants", title: "Nenhum consultor hoje", desc: "Ninguém solicitou simulações ainda." }));

    funnel.set(charts.funnelChart(metrics.funnel)
      || empty({ mark: "reports", title: "Sem movimento hoje" }));
  };

  const drawFeed = (items) => {
    activity.set(items.length
      ? h("div.feed", items.slice(0, 9).map(feedRow))
      : empty({ mark: "monitor", title: "Aguardando atividade", desc: "Nada aconteceu desde que o painel abriu." }));
  };

  draw(store.get("metrics"));
  drawFeed(store.get("feed"));

  const off = [
    store.subscribe("metrics", draw),
    store.subscribe("feed", drawFeed),
  ];
  return () => off.forEach((fn) => fn());
}

function card(title, hint) {
  const body = h("div");
  const node = h("div.card", h("header", h("h3", title), hint && h("span.hint", hint)), body);
  return { node, set: (content) => mount(body, content) };
}

function tile(label, value, foot, accent = null, spark = null) {
  return h("div.tile", { dataset: accent ? { accent } : {} },
    h("div.label", label),
    h("div.value", value),
    foot && h("div.foot", foot),
    spark,
  );
}
