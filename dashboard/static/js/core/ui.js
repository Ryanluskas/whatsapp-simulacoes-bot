/** Peças de interface reutilizáveis: estados, esqueletos, cards, toasts e modal. */

import { $, clear, h, mount } from "./dom.js";
import { icon } from "./icons.js";
import { allanaAvatar } from "./logo.js";
import { toneOf } from "./status.js";

/* ------------------------------------------------------------- estados --- */

/**
 * Estado vazio. `allana: true` troca o ícone pela Allana (discreta) — use
 * onde "vazio" é uma boa notícia ou a primeira vez na tela.
 */
export function empty({ mark = "empty", title = "Nada por aqui", desc = "", action = null,
  allana = false, compact = false } = {}) {
  return h(`div.empty${compact ? ".compact" : ""}`,
    allana ? allanaAvatar(44) : h("div.mark", icon(mark, 20)),
    h("div.title", title),
    desc && h("div.desc", desc),
    action,
  );
}

export function errorState(message, onRetry) {
  return h("div.error-state", { role: "alert" },
    allanaAvatar(44),
    h("div.title", "Não consegui carregar isto"),
    h("div.desc", message || "Tente de novo em instantes."),
    onRetry && h("button.btn.sm", { onclick: onRetry, type: "button" }, icon("refresh", 14), "Tentar de novo"),
  );
}

export function skeleton(height = 16, width = "100%") {
  return h("div.skeleton", { style: { height: `${height}px`, width }, "aria-hidden": "true" });
}

export function skeletonMetrics(count = 4) {
  return h("div.metrics", { "aria-busy": "true" },
    Array.from({ length: count }, () =>
      h("div.metric", skeleton(12, "50%"), skeleton(28, "40%"), skeleton(10, "65%"))),
  );
}

export function skeletonLines(count = 4, height = 14) {
  return h("div.skeleton-lines", { "aria-busy": "true" },
    Array.from({ length: count }, (_, i) => skeleton(height, `${92 - (i % 3) * 14}%`)));
}

/* --------------------------------------------------------------- cards --- */

/** Card com cabeçalho. Devolve o nó, o corpo e atalhos para trocá-los. */
export function card(title, { hint = "", actions = null, flush = false } = {}) {
  const body = h("div");
  const hintEl = h("span.hint", hint);
  const node = h(`section.card${flush ? ".flush" : ""}`,
    h("header", h("h3", title), hintEl, actions && h("div.actions", actions)),
    body);
  return {
    node,
    body,
    set: (...content) => mount(body, ...content),
    setHint: (text) => { hintEl.textContent = text; },
  };
}

export function metricCard({ label, value, foot = "", tone = null, iconName = null }) {
  return h("div.metric", { dataset: tone ? { tone } : {} },
    h("div.label", iconName && icon(iconName, 14), label),
    h("div.value", value),
    foot && h("div.foot", foot),
  );
}

export function callout(tone, title, body = null, actions = null) {
  const ICON = { error: "ban", warning: "alert", info: "info", success: "check" };
  return h("div.callout", { dataset: { tone }, role: tone === "error" ? "alert" : null },
    h("span.mark", icon(ICON[tone] || "info", 16)),
    h("div",
      title && h("div.title", title),
      body && h("div.body", body),
      actions && h("div.actions", actions),
    ),
  );
}

/* --------------------------------------------------------------- toast --- */

const TOAST_ICON = { success: "check", error: "ban", warning: "alert", info: "info" };

export function toast(level, title, body = "", timeout = 6000) {
  const root = $("#toasts");
  if (!root) return () => {};
  const node = h("div.toast", { dataset: { level }, role: level === "error" ? "alert" : "status" },
    h("span.mark", icon(TOAST_ICON[level] || "info", 16)),
    h("div", h("div.t-title", title), body && h("div.t-body", body)),
    h("button.t-close", { type: "button", "aria-label": "Fechar notificação", onclick: () => remove() },
      icon("close", 14)),
  );
  root.appendChild(node);

  let timer = timeout ? setTimeout(remove, timeout) : null;
  node.addEventListener("mouseenter", () => timer && clearTimeout(timer));
  node.addEventListener("mouseleave", () => {
    if (timeout) timer = setTimeout(remove, 2000);
  });

  function remove() {
    if (!node.isConnected) return;
    node.classList.add("out");
    setTimeout(() => node.remove(), 200);
  }
  return remove;
}

/* --------------------------------------------------------------- modal --- */

let closeActiveModal = null;

export const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

/** Prende o Tab dentro de `container` enquanto ele estiver aberto. */
export function trapFocus(container, event) {
  if (event.key !== "Tab") return;
  const items = Array.from(container.querySelectorAll(FOCUSABLE)).filter((el) => el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

export function modal(title, ...content) {
  closeModal();
  const root = $("#modal-root");
  const titleId = `modal-t-${Date.now()}`;
  const dialog = h("div.modal", { role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
    h("header",
      h("h3", { id: titleId }, title),
      h("button.btn.ghost.icon.sm.close", { type: "button", "aria-label": "Fechar", onclick: closeModal },
        icon("close", 16)),
    ),
    ...content,
  );

  mount(root, dialog);
  root.classList.remove("hidden");
  root.onclick = (event) => { if (event.target === root) closeModal(); };

  const previous = document.activeElement;
  const onKey = (event) => {
    if (event.key === "Escape") { event.stopPropagation(); closeModal(); }
    else trapFocus(dialog, event);
  };
  document.addEventListener("keydown", onKey, true);
  // Foca a primeira ação do conteúdo, não o "fechar"
  (dialog.querySelector(".modal-actions .btn, form input, form button") || dialog.querySelector(FOCUSABLE))?.focus();

  closeActiveModal = () => {
    document.removeEventListener("keydown", onKey, true);
    root.classList.add("hidden");
    clear(root);
    closeActiveModal = null;
    previous?.focus?.();
  };
  return dialog;
}

export function closeModal() {
  closeActiveModal?.();
}

/**
 * Confirmação antes de uma ação com consequência. `variant` escolhe o
 * botão de confirmar: "primary" (vermelho), "confirm" ou "caution".
 */
export function confirmAction(title, message, onConfirm, confirmLabel = "Confirmar",
  { variant = "primary", iconName = null } = {}) {
  modal(title,
    h("p.dim", message),
    h("div.modal-actions",
      h("button.btn", { type: "button", onclick: closeModal }, "Cancelar"),
      h(`button.btn.${variant}`, {
        type: "button",
        onclick: () => { closeModal(); onConfirm(); },
      }, iconName && icon(iconName, 15), confirmLabel),
    ),
  );
}

/* ------------------------------------------------------------- pontos --- */

/** Ponto de estado. Aceita o nome do estado (connected, error…) ou um tom. */
export function statusDot(state, { pulse = false, large = false, tone = null } = {}) {
  return h("span", {
    class: `dot${large ? " lg" : ""}${pulse ? " pulse" : ""}`,
    dataset: { tone: tone || toneOf(state) },
    "aria-hidden": "true",
  });
}

export function chip(label, value) {
  return h("span.chip", label, value !== undefined && h("b", String(value)));
}

/* -------------------------------------------------------------- utils --- */

export function debounce(fn, wait = 280) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}
