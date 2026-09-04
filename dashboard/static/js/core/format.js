/** Formatação pt-BR. As datas chegam em UTC (sufixo Z) e são exibidas no fuso local. */

const BRL = new Intl.NumberFormat("pt-BR", {
  style: "currency", currency: "BRL", minimumFractionDigits: 2, maximumFractionDigits: 2,
});
const INT = new Intl.NumberFormat("pt-BR");
const COMPACT = new Intl.NumberFormat("pt-BR", { notation: "compact", maximumFractionDigits: 1 });

export const brl = (v) => (Number.isFinite(Number(v)) ? BRL.format(Number(v)) : "R$ 0,00");
export const int = (v) => INT.format(Number(v) || 0);
export const compact = (v) => {
  const n = Number(v) || 0;
  return Math.abs(n) >= 10000 ? COMPACT.format(n) : INT.format(n);
};
export const pct = (v) => `${(Number(v) || 0).toFixed(1).replace(".", ",")}%`;

export function date(iso) {
  const d = toDate(iso);
  return d ? d.toLocaleDateString("pt-BR") : "—";
}

export function time(iso, withSeconds = true) {
  const d = toDate(iso);
  if (!d) return "—";
  return d.toLocaleTimeString("pt-BR", {
    hour: "2-digit", minute: "2-digit", ...(withSeconds ? { second: "2-digit" } : {}),
  });
}

export function dateTime(iso) {
  const d = toDate(iso);
  return d ? `${d.toLocaleDateString("pt-BR")} ${time(iso, false)}` : "—";
}

export function relative(iso) {
  const d = toDate(iso);
  if (!d) return "—";
  const seconds = Math.floor((Date.now() - d.getTime()) / 1000);
  if (seconds < 5) return "agora";
  if (seconds < 60) return `há ${seconds}s`;
  if (seconds < 3600) return `há ${Math.floor(seconds / 60)}min`;
  if (seconds < 86400) return `há ${Math.floor(seconds / 3600)}h`;
  const days = Math.floor(seconds / 86400);
  return days === 1 ? "ontem" : `há ${days} dias`;
}

/** Duração legível: 24,3s · 2min 05s · 1h 12min */
export function duration(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total <= 0) return "—";
  if (total < 60) return `${total.toFixed(1).replace(".", ",")}s`;
  if (total < 3600) {
    const m = Math.floor(total / 60);
    return `${m}min ${String(Math.round(total % 60)).padStart(2, "0")}s`;
  }
  const hours = Math.floor(total / 3600);
  return `${hours}h ${Math.floor((total % 3600) / 60)}min`;
}

/** Tempo online a partir do instante em que a conexão subiu. */
export function uptime(iso) {
  const d = toDate(iso);
  if (!d) return "—";
  return duration(Math.max(0, (Date.now() - d.getTime()) / 1000));
}

export function dayLabel(isoDay) {
  if (!isoDay) return "";
  const [y, m, d] = isoDay.split("-").map(Number);
  return `${String(d).padStart(2, "0")}/${String(m).padStart(2, "0")}`;
}

export function initials(name) {
  const parts = String(name || "?").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  return (parts[0][0] + (parts[1]?.[0] || "")).toUpperCase();
}

export function phone(value) {
  const d = String(value || "").replace(/\D/g, "");
  if (d.length < 10) return value || "—";
  const local = d.length > 11 ? d.slice(2) : d;
  const ddd = local.slice(0, 2);
  const rest = local.slice(2);
  const head = rest.length > 8 ? rest.slice(0, 5) : rest.slice(0, 4);
  return `(${ddd}) ${head}-${rest.slice(head.length)}`;
}

export function truncate(text, max = 120) {
  const value = String(text ?? "");
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function toDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}
