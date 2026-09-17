/**
 * Uma fonte só para "que texto, que tom e que ícone tem este estado".
 *
 * Os estados vêm do backend (simulations.status/stage/delivery_status/
 * quote_status). Aqui só se traduz — nunca se deduz um estado que o banco não
 * gravou. Tom é papel de cor (tokens.css); o texto e o ícone vão junto, para
 * nada depender só da cor.
 */

import { h } from "./dom.js";
import { icon } from "./icons.js";

/* ---------------------------------------------------------------- tons --- */

const STATE = {
  // conexão do WhatsApp
  connected:    { tone: "success", icon: "check" },
  disconnected: { tone: "error",   icon: "plugOff" },
  qr:           { tone: "warning", icon: "alert" },
  starting:     { tone: "info",    icon: "spinner" },

  // status agregado da solicitação
  queued:      { tone: "neutral", icon: "clock" },
  processing:  { tone: "info",    icon: "spinner" },
  completed:   { tone: "success", icon: "check" },
  error:       { tone: "error",   icon: "ban" },
  cancelled:   { tone: "neutral", icon: "ban" },
  interrupted: { tone: "warning", icon: "pause" },

  // etapas
  received:   { tone: "neutral", icon: "inbox" },
  identified: { tone: "neutral", icon: "userCheck" },
  validated:  { tone: "neutral", icon: "clipboard" },
  consulting: { tone: "lilac",   icon: "spinner" },
  extracting: { tone: "lilac",   icon: "spinner" },
  rendering:  { tone: "lilac",   icon: "spinner" },
  replying:   { tone: "lilac",   icon: "send" },
  delivery_retry:       { tone: "warning", icon: "refresh" },
  delivery_unconfirmed: { tone: "warning", icon: "alert" },
  delivery_failed:      { tone: "error",   icon: "ban" },

  // nível de log / evento
  INFO:    { tone: "info",    icon: "info" },
  WARNING: { tone: "warning", icon: "alert" },
  ERROR:   { tone: "error",   icon: "ban" },
  DEBUG:   { tone: "neutral", icon: "logs" },
  success: { tone: "success", icon: "check" },
  warning: { tone: "warning", icon: "alert" },
  info:    { tone: "info",    icon: "info" },
};

export const toneOf = (state) => STATE[state]?.tone || "neutral";
export const iconOf = (state) => STATE[state]?.icon || "empty";

/* -------------------------------------------------------------- badges --- */

/** Badge genérico: tom + texto + ícone. */
export function badge(tone, label, iconName = null, { attention = false, title = null } = {}) {
  return h("span.badge", {
    dataset: { tone, ...(attention ? { attention: "true" } : {}) },
    title,
  }, iconName ? icon(iconName, 12) : null, label);
}

/** Badge a partir do nome do estado vindo do backend. */
export function stateBadge(state, label) {
  return badge(toneOf(state), label || state || "—", iconOf(state));
}

/* ------------------------------------------------------------- entrega --- */

// "Entregue" só com prova. `unconfirmed` pode ter chegado: ninguém reenvia
// sozinho, porque uma segunda mensagem duplicaria a resposta no grupo.
export const DELIVERY = {
  delivered:   { label: "Entregue",         long: "Entregue",                               tone: "success", icon: "check" },
  pending:     { label: "Enviando",         long: "Enviando agora",                         tone: "info",    icon: "send" },
  retrying:    { label: "Reenvio pendente", long: "Reenvio pendente — o bot tenta de novo", tone: "warning", icon: "refresh" },
  unconfirmed: { label: "Não confirmado",   long: "Não confirmada — conferir no WhatsApp",  tone: "warning", icon: "alert", attention: true },
  failed:      { label: "Falhou",           long: "Falhou — o bot desistiu",                tone: "error",   icon: "ban" },
};

export function deliveryOf(row) {
  // Linha antiga (antes da coluna existir) com resposta registrada = entregue,
  // o mesmo critério da migração do banco.
  const key = row?.delivery_status || (row?.replied_at ? "delivered" : "");
  return DELIVERY[key] ? { key, ...DELIVERY[key] } : null;
}

export function deliveryBadge(row) {
  const d = deliveryOf(row);
  if (!d) return h("span.faint", { title: "Ainda sem resposta para entregar" }, "—");
  return badge(d.tone, d.label, d.icon, { attention: d.attention, title: d.long });
}

/* ------------------------------------------------------------- citação --- */

export const QUOTE = {
  ok:          { label: "Citada — confirmada",               tone: "success" },
  unverified:  { label: "Citada — sem como conferir",        tone: "neutral" },
  not_applied: { label: "Saiu citando outra mensagem",       tone: "warning" },
  fallback:    { label: "Recusada — reenviada sem citação",  tone: "warning" },
  none:        { label: "Sem citação",                       tone: "neutral" },
};

export function quoteOf(value) {
  return QUOTE[value] || { label: value ? String(value) : "Não avaliada", tone: "neutral" };
}

/* ------------------------------------------------------- resolução manual */

export const RESOLUTION = {
  "manual:chegou": "Conferido no WhatsApp: a resposta chegou.",
  "manual:nao_chegou": "Conferido no WhatsApp: não chegou — um reenvio foi liberado.",
};

/* ----------------------------------------------------------- conexão --- */

export const WHATSAPP_LABEL = {
  connected: "Conectado",
  disconnected: "Desconectado",
  qr: "Aguardando QR Code",
  starting: "Conectando…",
};

export const PRESENCE_LABEL = {
  connected: "Online",
  disconnected: "Offline",
  qr: "Aguardando QR Code",
  starting: "Conectando…",
};
