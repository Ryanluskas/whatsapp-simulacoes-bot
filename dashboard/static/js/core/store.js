/**
 * Estado da aplicação com assinaturas por chave.
 *
 * As telas se inscrevem no que precisam e voltam a desenhar só quando aquilo
 * muda — em vez de repintar a tela inteira a cada evento, como fazia a versão
 * anterior (que reconstruía toda a lista do monitor a cada mensagem, perdendo
 * a posição de rolagem).
 */

const state = {
  session: { authenticated: false, user: null, maskCpf: true, groupName: "" },
  metrics: null,
  queue: { items: [], depth: 0 },
  whatsapp: { state: "disconnected", connected: false },
  system: null,
  feed: [],
  labels: { stages: {}, statuses: {} },
  stream: { connected: false, lastEvent: null },
};

const subscribers = new Map();
const FEED_LIMIT = 220;

export function get(key) {
  return key ? state[key] : state;
}

export function set(key, value) {
  state[key] = typeof value === "function" ? value(state[key]) : value;
  emit(key);
}

export function patch(key, partial) {
  state[key] = { ...state[key], ...partial };
  emit(key);
}

export function subscribe(keys, fn) {
  const list = Array.isArray(keys) ? keys : [keys];
  for (const key of list) {
    if (!subscribers.has(key)) subscribers.set(key, new Set());
    subscribers.get(key).add(fn);
  }
  return () => list.forEach((key) => subscribers.get(key)?.delete(fn));
}

function emit(key) {
  for (const fn of subscribers.get(key) || []) {
    try {
      fn(state[key], key);
    } catch (error) {
      console.error("assinante falhou:", key, error);
    }
  }
}

/** O feed é uma janela deslizante: o mais novo primeiro, tamanho limitado. */
export function pushFeed(entry) {
  const feed = state.feed;
  if (entry.id && feed.some((item) => item.id === entry.id)) return;
  feed.unshift(entry);
  if (feed.length > FEED_LIMIT) feed.length = FEED_LIMIT;
  emit("feed");
}

export function seedFeed(entries) {
  state.feed = entries.slice(-FEED_LIMIT).reverse();
  emit("feed");
}

export function stageLabel(stage) {
  return state.labels.stages?.[stage] || stage || "—";
}

export function statusLabel(status) {
  return state.labels.statuses?.[status] || status || "—";
}
