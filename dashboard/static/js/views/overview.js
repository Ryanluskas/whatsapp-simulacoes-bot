/**
 * Visão geral — responde "como está o bot agora?".
 *
 * Tudo aqui é número gravado pelo backend. Onde o recorte é parcial (as
 * solicitações mais recentes), o texto diz o recorte em vez de sugerir que é
 * o total.
 */

import { api } from "../core/api.js";
import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import { icon } from "../core/icons.js";
import * as store from "../core/store.js";
import {
  WHATSAPP_LABEL, deliveryBadge, deliveryOf, stateBadge, toneOf,
} from "../core/status.js";
import {
  card, debounce, empty, errorState, metricCard, skeletonLines, skeletonMetrics, statusDot,
} from "../core/ui.js";
import { feedRow } from "../feed.js";
import { openRequest } from "./request-detail.js";

/** Quantas solicitações recentes o painel olha para achar pendências. */
const RECENTES = 50;
const NA_LISTA = 6;

export function render(root, { navigate }) {
  const painel = h("div.status-panel", { role: "group", "aria-label": "Estado do sistema" });
  const metricas = h("div");
  const recentes = card("Solicitações recentes", {
    actions: h("button.btn.sm.ghost", { type: "button", onclick: () => navigate("history") },
      "Ver todas"),
  });
  const atencao = card("Precisa de atenção", {
    hint: `entre as ${RECENTES} mais recentes`,
  });
  const hoje = card("Hoje, do pedido à resposta", { hint: "contagens do dia" });
  const atividade = card("Atividade", {
    actions: h("button.btn.sm.ghost", { type: "button", onclick: () => navigate("monitor") },
      "Abrir monitor"),
  });

  mount(root,
    h("div.stack",
      painel,
      metricas,
      h("div.grid.split", recentes.node, atencao.node),
      h("div.grid.split", hoje.node, atividade.node),
    ),
  );

  /* ------------------------------------------------------------- estado */
  function pintarPainel() {
    const wa = store.get("whatsapp") || {};
    const fila = store.get("queue") || {};
    const sistema = store.get("system");
    const ao_vivo = store.get("stream") || {};

    const estadoWa = wa.state || (wa.connected ? "connected" : "disconnected");
    const emExecucao = (fila.items || []).filter((i) => i.status === "processing").length;
    const sim = resumoDoSimulador(sistema);

    mount(painel,
      celula({
        rotulo: "WhatsApp", iconName: "message",
        tom: toneOf(estadoWa), pulse: Boolean(wa.connected),
        valor: WHATSAPP_LABEL[estadoWa] || "Desconhecido",
        detalhe: [wa.mode === "evolution" ? "Evolution API" : "WhatsApp Web",
          wa.chat_name || wa.group_name || ""].filter(Boolean).join(" · "),
      }),
      celula({
        rotulo: "Simulador", iconName: "server",
        tom: sim.tom, valor: sim.valor, detalhe: sim.detalhe,
      }),
      celula({
        rotulo: "Fila", iconName: "queue",
        tom: (fila.depth || 0) > 0 ? "info" : "neutral",
        valor: `${fmt.int(fila.depth || 0)} aguardando`,
        detalhe: emExecucao ? `${emExecucao} em execução` : "nenhuma em execução",
      }),
      celula({
        rotulo: "Tempo real", iconName: "activity",
        tom: ao_vivo.connected ? "success" : "error", pulse: Boolean(ao_vivo.connected),
        valor: ao_vivo.connected ? "Ao vivo" : "Reconectando…",
        detalhe: ao_vivo.lastEvent ? `último evento ${fmt.relative(ao_vivo.lastEvent)}` : "sem eventos ainda",
      }),
    );
  }

  /* ----------------------------------------------------------- métricas */
  function pintarMetricas(metrics) {
    if (!metrics) {
      mount(metricas, skeletonMetrics(4));
      return;
    }
    const k = metrics.kpis;
    const respondidas = (metrics.funnel || []).find((e) => e.etapa === "Respostas enviadas");

    mount(metricas, h("div.metrics",
      metricCard({
        label: "Solicitações hoje", iconName: "inbox",
        value: fmt.int(k.today),
        foot: `${fmt.int(k.today_completed)} concluídas`,
      }),
      metricCard({
        label: "Respostas enviadas hoje", iconName: "send",
        value: respondidas ? fmt.int(respondidas.total) : "—",
        foot: "com resposta registrada no banco",
      }),
      metricCard({
        label: "Erros · 30 dias", iconName: "ban",
        value: fmt.int(k.errors),
        foot: k.interrupted ? `${fmt.int(k.interrupted)} interrompidas` : "nenhuma interrompida",
        tone: k.errors > 0 ? "error" : null,
      }),
      metricCard({
        label: "Tempo médio · 30 dias", iconName: "clock",
        value: fmt.duration(k.avg_seconds),
        foot: "por simulação concluída",
      }),
    ));
  }

  /* ------------------------------------------------------------- listas */
  let emAndamento = 0;

  async function carregarSolicitacoes() {
    const token = ++emAndamento;
    if (!recentes.body.childElementCount) recentes.set(skeletonLines(4, 18));
    let dados;
    try {
      dados = await api.simulations({ period: "all", limit: RECENTES, offset: 0 });
    } catch (error) {
      if (token !== emAndamento) return;
      recentes.set(errorState(error.message, carregarSolicitacoes));
      atencao.set(h("p.muted", "Sem dados para conferir agora."));
      return;
    }
    if (token !== emAndamento) return;

    const itens = dados.items || [];
    if (!itens.length) {
      recentes.set(empty({
        allana: true, compact: true,
        title: "Nenhuma solicitação ainda",
        desc: "Estou de olho no grupo. O primeiro pedido aparece aqui.",
      }));
      atencao.set(empty({ compact: true, mark: "check", title: "Nada pendente" }));
      return;
    }

    recentes.set(h("div.rows", itens.slice(0, NA_LISTA).map(linhaDaSolicitacao)));

    const pendentes = itens.filter(precisaDeAtencao);
    atencao.set(pendentes.length
      ? h("div.stack",
        h("div.rows", pendentes.slice(0, 5).map(linhaDaSolicitacao)),
        pendentes.length > 5
          ? h("p.muted", { style: { fontSize: "var(--t-meta)" } },
            `e mais ${pendentes.length - 5} nas ${RECENTES} mais recentes.`)
          : null)
      : empty({
        compact: true, mark: "check",
        title: "Nada pendente",
        desc: `Nenhuma entrega incerta, reenvio ou erro entre as ${RECENTES} solicitações mais recentes.`,
      }));
  }

  function linhaDaSolicitacao(row) {
    return h("button.row-item", {
      type: "button",
      onclick: () => openRequest(row.id, { onChange: carregarSolicitacoes }),
    },
      h("div",
        h("div.cell-main", row.consultant_name || "Consultor não identificado"),
        h("div.cell-sub",
          h("span.mono", row.request_id), " · ",
          row.customer_name || "cliente não identificado", " · ",
          fmt.relative(row.created_at)),
      ),
      h("div.right",
        stateBadge(row.status, row.status_label),
        deliveryBadge(row),
      ),
    );
  }

  /* -------------------------------------------------------------- hoje */
  function pintarHoje(metrics) {
    if (!metrics) {
      hoje.set(skeletonLines(2, 40));
      return;
    }
    const etapas = metrics.funnel || [];
    if (!etapas.length || etapas.every((e) => !e.total)) {
      hoje.set(empty({
        compact: true, mark: "clock", title: "Sem movimento hoje",
        desc: "Nenhum pedido chegou no grupo desde a virada do dia.",
      }));
      return;
    }
    hoje.set(h("div.flow", etapas.map((etapa) =>
      h("div.flow-step",
        h("span.n", fmt.int(etapa.total)),
        h("span.l", etapa.etapa),
      ))));
  }

  function pintarAtividade(items) {
    atividade.set(items.length
      ? h("div.feed", items.slice(0, 7).map(feedRow))
      : empty({
        compact: true, mark: "monitor", title: "Aguardando atividade",
        desc: "Nada aconteceu desde que o painel abriu.",
      }));
  }

  pintarPainel();
  pintarMetricas(store.get("metrics"));
  pintarHoje(store.get("metrics"));
  pintarAtividade(store.get("feed"));
  carregarSolicitacoes();

  const relogio = setInterval(pintarPainel, 15000);
  const off = [
    store.subscribe(["whatsapp", "queue", "system", "stream"], pintarPainel),
    store.subscribe("metrics", (m) => { pintarMetricas(m); pintarHoje(m); }),
    store.subscribe("metrics", debounce(carregarSolicitacoes, 1500)),
    store.subscribe("feed", pintarAtividade),
  ];
  return () => { clearInterval(relogio); off.forEach((fn) => fn()); };
}

/* --------------------------------------------------------------- peças --- */

function celula({ rotulo, valor, detalhe, tom, iconName, pulse = false }) {
  return h("div.status-cell",
    h("div.k", icon(iconName, 13), rotulo),
    h("div.v", statusDot(null, { tone: tom, pulse, large: true }), valor),
    h("div.d", { title: detalhe }, detalhe || "—"),
  );
}

function resumoDoSimulador(system) {
  const sims = system?.simulators || [];
  if (!system || !sims.length) return { tom: "neutral", valor: "—", detalhe: "sem dados do simulador" };
  const parados = sims.filter((s) => !s.running);
  const ocupados = sims.filter((s) => s.busy);
  const prontos = sims.filter((s) => s.running && s.ready);
  const detalhe = `${sims.length} worker(s) · ${prontos.length} pronto(s)`;
  if (parados.length === sims.length) return { tom: "error", valor: "Parado", detalhe };
  if (parados.length) return { tom: "warning", valor: "Parcial", detalhe };
  if (ocupados.length) return { tom: "info", valor: "Processando", detalhe };
  if (!prontos.length) return { tom: "warning", valor: "Carregando…", detalhe };
  return { tom: "success", valor: "Pronto", detalhe };
}

/**
 * O que um operador precisa olhar: entrega que pode não ter chegado, reenvio
 * em aberto, entrega que falhou, simulação com erro ou interrompida.
 */
function precisaDeAtencao(row) {
  const entrega = deliveryOf(row);
  if (entrega && ["unconfirmed", "retrying", "failed"].includes(entrega.key)) return true;
  return row.status === "error" || row.status === "interrupted";
}
