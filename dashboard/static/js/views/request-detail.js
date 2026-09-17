/**
 * Detalhe de uma solicitação, no painel lateral.
 *
 * Mostra exatamente o que o banco gravou: resultado da simulação, estado da
 * entrega, estado da citação, tentativas, erros e a linha do tempo. Nada é
 * inferido — quando o campo está vazio, aparece "—".
 */

import { api } from "../core/api.js";
import { h, mount } from "../core/dom.js";
import { openDrawer } from "../core/drawer.js";
import * as fmt from "../core/format.js";
import { icon } from "../core/icons.js";
import {
  RESOLUTION, badge, deliveryOf, iconOf, quoteOf, stateBadge, toneOf,
} from "../core/status.js";
import { timeline } from "../core/timeline.js";
import { callout, confirmAction, errorState, skeletonLines, toast } from "../core/ui.js";

/**
 * @param {number} id  id da simulação
 * @param {{ onChange?: () => void }} options  chamado quando a entrega muda
 */
export async function openRequest(id, { onChange = null } = {}) {
  const drawer = openDrawer({ label: `Detalhes da solicitação ${id}` });
  mount(drawer.head, h("h2", "Carregando…"));
  mount(drawer.body, skeletonLines(6, 16));
  await preencher(drawer, id, onChange);
}

async function preencher(drawer, id, onChange) {
  let data;
  try {
    data = await api.simulation(id);
  } catch (error) {
    if (!drawer.isOpen()) return;
    mount(drawer.body, errorState(error.message, () => preencher(drawer, id, onChange)));
    return;
  }
  if (!drawer.isOpen()) return;

  const s = data.simulation;
  const entrega = deliveryOf(s);
  const citacao = quoteOf(s.quote_status);
  const recarregar = () => {
    onChange?.();
    preencher(drawer, id, onChange);
  };

  mount(drawer.head,
    h("h2", h("span.mono", s.request_id || `#${id}`)),
    h("div.cell-badges",
      stateBadge(s.status, s.status_label),
      entrega ? badge(entrega.tone, entrega.label, entrega.icon, { attention: entrega.attention }) : null,
    ),
    h("p.muted", { style: { fontSize: "var(--t-meta-lg)" } },
      `${s.consultant_name || "Consultor não identificado"} · ${fmt.dateTime(s.created_at)}`),
  );

  mount(drawer.body,
    s.delivery_status === "unconfirmed" ? acoesDeEntregaIncerta(s, recarregar) : null,

    s.delivery_resolution
      ? callout("info", "Entrega resolvida no painel",
        (RESOLUTION[s.delivery_resolution] || s.delivery_resolution)
        + (s.delivery_resolved_by ? ` Por ${s.delivery_resolved_by}` : "")
        + (s.delivery_resolved_at ? ` em ${fmt.dateTime(s.delivery_resolved_at)}.` : ""))
      : null,

    s.error_message ? callout("error", "Erro na simulação", s.error_message) : null,
    s.delivery_error && s.delivery_status !== "unconfirmed"
      ? callout("warning", "Erro na entrega", s.delivery_error) : null,

    // ------------------------------------------------------------ pedido
    secao("Solicitação",
      h("div.kv",
        linha("Solicitação", h("span.mono", s.request_id || "—")),
        linha("Mensagem", h("span.mono", s.source_message_id || "—")),
        linha("Consultor", s.consultant_name || "—"),
        linha("Chat", s.chat_name || mascararJid(s.chat_id) || "—"),
        linha("Recebida em", fmt.dateTime(s.created_at)),
        linha("Etapa", s.stage_label || s.stage || "—"),
        linha("Tempo", fmt.duration(s.processing_seconds)),
        linha("Tentativas", tentativas(s)),
      ),
    ),

    // ----------------------------------------------------------- entrega
    secao("Entrega da resposta",
      h("div.kv",
        linha("Estado", entrega
          ? h("span", badge(entrega.tone, entrega.label, entrega.icon, { attention: entrega.attention }),
            h("span.muted", { style: { marginLeft: "8px" } }, entrega.long))
          : h("span.faint", "Sem resposta registrada")),
        linha("Citação", h("span", badge(citacao.tone, citacao.label, "quote"))),
        linha("Reenvios", String(s.reply_attempts || 0)),
        linha("Respondida em", s.replied_at ? fmt.dateTime(s.replied_at) : "—"),
        linha("ID enviado", h("span.mono", s.sent_message_id || "—")),
      ),
    ),

    // ---------------------------------------------------------- resultado
    secao("Resultado da simulação",
      h("div.kv.two",
        linha("Cliente", s.customer_name || "—"),
        linha("CPF", h("span.mono", s.cpf_display || "—")),
        linha("Banco", s.bank || "—"),
        linha("Contrato", h("span.mono", s.contract || "—")),
        linha("Refin", s.refin || "—"),
        linha("Margem", s.margin || "—"),
        linha("Valor disponível", fmt.brl(s.reduction_value)),
        linha("Soma das parcelas", fmt.brl(s.installment_sum)),
        linha("Saldo devedor", fmt.brl(s.debt_sum)),
      ),
    ),

    // ------------------------------------------------------------ tempo
    secao("Linha do tempo",
      data.timeline.length
        ? timeline(data.timeline.map((e) => ({
          time: fmt.time(e.created_at, false),
          tone: toneOf(e.level === "error" ? "ERROR" : e.level === "success" ? "success"
            : e.level === "warning" ? "warning" : e.stage),
          icon: iconOf(e.stage) === "empty" ? "clock" : iconOf(e.stage),
          title: e.stage_label || e.title,
          detail: e.detail || "",
        })))
        : h("p.muted", "Sem eventos registrados para esta solicitação."),
    ),

    secao("Mensagens",
      data.messages.length
        ? timeline(data.messages.map((m) => ({
          time: fmt.time(m.created_at, false),
          tone: tomDaMensagem(m),
          icon: m.direction === "in" ? "inbox" : iconeDaMensagem(m),
          title: tituloDaMensagem(m),
          message: fmt.truncate(m.text, 520),
          evidence: evidencia(m),
          image: imagem(m),
        })), { label: "Mensagens da solicitação" })
        : h("p.muted", "Nenhuma mensagem registrada."),
    ),

    data.contracts?.length
      ? secao("Contratos encontrados", data.contracts.map((contrato, i) =>
        h("div",
          h("p.muted", { style: { fontSize: "var(--t-meta)", marginBottom: "4px" } }, `Contrato ${i + 1}`),
          h("div.kv", Object.entries(contrato).map(([k, v]) => linha(k, String(v ?? "—")))),
        )))
      : null,
  );
}

/* --------------------------------------------------------------- peças --- */

const secao = (titulo, ...conteudo) => h("section.dsec", h("h3", titulo), ...conteudo);
const linha = (k, v) => h("div", h("span.k", k), h("span.v", v));

function tentativas(s) {
  const feitas = s.attempts || 0;
  const max = s.max_attempts || 1;
  return `${feitas} de ${max}`;
}

/** O JID carrega número de telefone em conversa privada; só as pontas aparecem. */
function mascararJid(jid) {
  if (!jid) return "";
  const [usuario, dominio] = String(jid).split("@");
  if (!dominio) return jid;
  const visivel = usuario.length > 8
    ? `${usuario.slice(0, 4)}${"*".repeat(usuario.length - 8)}${usuario.slice(-4)}`
    : usuario;
  return `${visivel}@${dominio}`;
}

/**
 * Entrega incerta: a camada não deixou provar se a resposta saiu, e o bot NÃO
 * reenvia sozinho (duplicaria). Só quem olha o grupo desempata.
 */
function acoesDeEntregaIncerta(s, recarregar) {
  const decidir = (acao, titulo, mensagem, rotulo, feito, variant) =>
    confirmAction(titulo, mensagem, async () => {
      try {
        await api.post(`/api/simulations/${s.id}/entrega`, { acao });
        toast("success", feito, s.request_id);
      } catch (error) {
        toast("error", "Não foi possível registrar", error.message);
      }
      recarregar();
    }, rotulo, { variant });

  const jaTentouReenvio = s.delivery_resolution === "manual:nao_chegou";

  return callout("warning", "Entrega não confirmada",
    h("span", "Abra o grupo no WhatsApp e procure a resposta de ",
      h("span.mono", s.request_id),
      ". O bot não reenvia sozinho: se a primeira chegou, uma segunda duplicaria."),
    [
      h("button.btn.confirm", {
        type: "button",
        onclick: () => decidir("chegou", "A resposta chegou no grupo?",
          "Marca a entrega como feita. Nada é enviado ao consultor.",
          "Sim, chegou", "Marcada como entregue", "confirm"),
      }, icon("check", 15), "Chegou no grupo"),
      // "Não chegou" vale uma vez: se o reenvio também ficar incerto, sobra
      // conferir e marcar como entregue (o servidor recusa um segundo).
      jaTentouReenvio ? null : h("button.btn.caution", {
        type: "button",
        onclick: () => decidir("nao_chegou", "A resposta NÃO está no grupo?",
          "Libera UM reenvio do resultado, em texto, citando o mesmo pedido. Se a primeira "
          + "tiver chegado e você não viu, o consultor recebe duas vezes.",
          "Não chegou — reenviar", "Reenvio liberado", "caution"),
      }, icon("refresh", 15), "Não chegou — reenviar"),
      jaTentouReenvio
        ? h("span.muted", { style: { fontSize: "var(--t-meta)" } },
          "O reenvio manual já foi usado nesta solicitação.")
        : null,
    ],
  );
}

/* ----------------------------------------------------------- mensagens --- */

// "Enviada" só quando a camada devolveu prova. Tentativa que falhou ou que a
// API aceitou sem id não pode aparecer como resposta que chegou.
const MENSAGEM_SEM_PROVA = new Set(["failed", "unconfirmed", "sending"]);

function tituloDaMensagem(m) {
  if (m.direction === "in") return "Pedido recebido";
  const tipo = m.kind === "image" ? " (imagem)" : "";
  if (m.status === "failed") return `Envio falhou${tipo}`;
  if (m.status === "sending") return `Enviando${tipo}…`;
  if (m.status === "unconfirmed") return `Envio não confirmado${tipo}`;
  return `Resposta enviada${tipo}`;
}

function tomDaMensagem(m) {
  if (m.direction === "in") return "info";
  if (m.status === "failed") return "error";
  if (MENSAGEM_SEM_PROVA.has(m.status)) return "warning";
  return "success";
}

function iconeDaMensagem(m) {
  if (m.status === "failed") return "ban";
  if (MENSAGEM_SEM_PROVA.has(m.status)) return "alert";
  return "send";
}

function evidencia(m) {
  const partes = [];
  if (m.direction === "in") return m.wa_message_id ? `id ${m.wa_message_id}` : "";
  if (m.attempt) partes.push(`tentativa ${m.attempt}`);
  if (m.provider) partes.push(m.provider);
  if (m.quote_status) partes.push(`citação ${m.quote_status}`);
  if (m.wa_message_id) partes.push(`id ${m.wa_message_id}`);
  if (m.http_status) partes.push(`HTTP ${m.http_status}`);
  if (m.desfecho) partes.push(`desfecho ${m.desfecho}`);
  if (m.quote_error) partes.push(`citação recusada: ${m.quote_error}`);
  if (m.error) partes.push(m.error);
  return partes.join(" · ");
}

/** O comprovante que o consultor recebeu — só quando ele de fato recebeu. */
function imagem(m) {
  if (!m.media_url || MENSAGEM_SEM_PROVA.has(m.status)) return null;
  return h("a", { href: m.media_url, target: "_blank", rel: "noopener", title: "Abrir a imagem enviada" },
    h("img.proof", {
      src: m.media_url,
      alt: "Imagem enviada ao grupo com o resultado da simulação",
      loading: "lazy",
    }));
}
