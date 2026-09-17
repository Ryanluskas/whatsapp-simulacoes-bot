/**
 * Tradução de um evento do servidor para uma linha do console operacional.
 *
 * Os eventos são os mesmos que alimentam a timeline do histórico, então o que
 * o operador vê ao vivo e o que fica gravado nunca divergem.
 */

import { h } from "./core/dom.js";
import { icon } from "./core/icons.js";
import * as fmt from "./core/format.js";

const ICON = {
  message_received: "inbox",
  consultant_identified: "userCheck",
  request_created: "clipboard",
  request_rejected: "alert",
  job_queued: "clock",
  job_progress: "spinner",
  job_retry: "refresh",
  job_done: "check",
  job_error: "ban",
  job_interrupted: "pause",
  queue_recovered: "refresh",
  message_sent: "send",
  message_failed: "alert",
  request_completed: "check",
  delivery_retry: "refresh",
  delivery_unconfirmed: "alert",   // entrega incerta: alguém precisa conferir
  delivery_manual: "check",        // alguém conferiu no WhatsApp e decidiu no painel
  delivery_failed: "ban",
  whatsapp_connected: "plug",
  whatsapp_disconnected: "plugOff",
};

const DIRECTION = {
  message_received: "in",
  message_sent: "out",
  message_failed: "out",
};

/** Texto da mensagem, quando o evento carrega uma. */
function quoteOf(event) {
  const text = event.payload?.text;
  return typeof text === "string" && text.trim() ? text.trim() : "";
}

export function feedRow(event) {
  const stage = event.stage || "";
  const level = event.level || "info";
  const quote = quoteOf(event);
  const consultant = event.consultant_name || event.payload?.consultant || "";
  // O nome já aparece no cabeçalho da linha; repetir no detalhe é ruído.
  const detalheUtil = (event.detail || "").trim() === consultant.trim()
    ? "" : event.detail;

  return h("div.feed-row", {
    dataset: { dir: DIRECTION[event.type] || "sys", level, stage },
  },
    h("span.ts", fmt.time(event.created_at)),
    h("span.rail", h("span.dot", { dataset: { state: stateOf(event) } })),
    h("div.body",
      h("div.head",
        h("span.mark", icon(ICON[event.type] || "empty", 15)),
        h("span.t", event.title || event.type),
        consultant && h("span.who", `· ${consultant}`),
        event.request_id && h("span.rid", event.request_id),
      ),
      detalheUtil && h("div.detail", fmt.truncate(detalheUtil, 180)),
      quote && h("div.quote", fmt.truncate(quote, 700)),
    ),
  );
}

function stateOf(event) {
  if (event.type === "whatsapp_connected") return "connected";
  if (event.type === "whatsapp_disconnected") return "disconnected";
  if (event.level === "error") return "error";
  if (event.level === "success") return "completed";
  if (event.level === "warning") return "queued";
  return event.stage || "idle";
}

/** Um evento vale a pena virar toast? Só o que exige reação do operador. */
export function shouldNotify(event) {
  return ["job_error", "message_failed", "whatsapp_disconnected", "job_interrupted", "delivery_failed", "delivery_unconfirmed"]
    .includes(event.type);
}
