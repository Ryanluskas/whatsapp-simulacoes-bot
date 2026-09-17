/**
 * Conexão em tempo real (Server-Sent Events).
 *
 * `EventSource` é nativo do navegador: nenhuma biblioteca externa, nenhum CDN
 * — o painel funciona sem internet — e a reconexão é automática. O cookie de
 * sessão viaja na requisição, então o canal é autenticado como qualquer rota.
 */

import * as store from "./store.js";

const EVENTS = [
  "hello",
  "message_received", "message_sent", "message_failed",
  "consultant_identified", "request_created", "request_rejected",
  "job_queued", "job_progress", "job_done", "job_error", "job_retry",
  "job_interrupted", "queue_recovered",
  "request_completed", "delivery_retry", "delivery_unconfirmed", "delivery_manual", "delivery_failed",
  "whatsapp_status", "whatsapp_connected", "whatsapp_disconnected",
  "metrics", "queue_update", "log",
];

let source = null;
const handlers = new Map();

export function on(type, fn) {
  if (!handlers.has(type)) handlers.set(type, new Set());
  handlers.get(type).add(fn);
  return () => handlers.get(type)?.delete(fn);
}

export function connect() {
  disconnect();
  source = new EventSource("/api/stream");

  source.onopen = () => store.patch("stream", { connected: true });
  source.onerror = () => {
    // O EventSource reconecta sozinho; aqui só refletimos o estado na interface.
    store.patch("stream", { connected: false });
  };

  for (const type of EVENTS) {
    source.addEventListener(type, (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      store.patch("stream", { connected: true, lastEvent: payload.created_at || null });
      dispatch(type, payload);
    });
  }
}

export function disconnect() {
  if (source) {
    source.close();
    source = null;
  }
  store.patch("stream", { connected: false });
}

function dispatch(type, payload) {
  for (const fn of handlers.get(type) || []) {
    try {
      fn(payload);
    } catch (error) {
      console.error(`handler de ${type} falhou:`, error);
    }
  }
  for (const fn of handlers.get("*") || []) {
    try {
      fn(payload, type);
    } catch (error) {
      console.error("handler curinga falhou:", error);
    }
  }
}
