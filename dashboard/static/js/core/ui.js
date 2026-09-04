/** Peças de interface reutilizáveis: estados vazios, esqueletos, toasts e modal. */

import { $, clear, h, mount } from "./dom.js";
import { icon } from "./icons.js";

/* ------------------------------------------------------------- estados --- */

/** `mark` é o NOME de um ícone (ver icons.js), não um caractere. */
export function empty({ mark = "empty", title = "Nada por aqui", desc = "", action = null } = {}) {
  return h("div.empty",
    h("div.mark", icon(mark, 20)),
    h("div.title", title),
    desc && h("div.desc", desc),
    action,
  );
}

export function errorState(message, onRetry) {
  return h("div.error-state",
    h("div.mark", icon("alert", 22)),
    h("div", message || "Não foi possível carregar."),
    onRetry && h("button.btn.sm", { onclick: onRetry, type: "button" }, "Tentar de novo"),
  );
}

export function skeleton(height = 16, width = "100%") {
  return h("div.skeleton", { style: { height: `${height}px`, width } });
}

export function skeletonTiles(count = 4) {
  return h("div.grid.kpi-grid",
    Array.from({ length: count }, () =>
      h("div.tile", skeleton(11, "45%"), skeleton(30, "62%"), skeleton(10, "70%"))),
  );
}

/** Devolve um ARRAY de <tr>, para ser passado direto a `mount(tbody, ...)`. */
export function skeletonRows(count = 6, columns = 5) {
  return Array.from({ length: count }, () =>
    h("tr", Array.from({ length: columns }, () => h("td", skeleton(12)))));
}

/* --------------------------------------------------------------- toast --- */

const TOAST_ICON = { success: "check", error: "ban", warning: "alert", info: "clock" };

export function toast(level, title, body = "", timeout = 6000) {
  const root = $("#toasts");
  if (!root) return;
  const node = h("div.toast", { dataset: { level } },
    h("span.mark", icon(TOAST_ICON[level] || "clock", 16)),
    h("div", h("div.t-title", title), body && h("div.t-body", body)),
    h("button.t-close", { type: "button", "aria-label": "Fechar", onclick: () => remove() },
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
    setTimeout(() => node.remove(), 220);
  }
  return remove;
}

/* --------------------------------------------------------------- modal --- */

let closeActiveModal = null;

export function modal(title, ...content) {
  closeModal();
  const root = $("#modal-root");
  const dialog = h("div.modal", { role: "dialog", "aria-modal": "true", "aria-label": title },
    h("header",
      h("h3", title),
      h("button.close", { type: "button", "aria-label": "Fechar", onclick: closeModal },
        icon("close", 15)),
    ),
    ...content,
  );

  mount(root, dialog);
  root.classList.remove("hidden");
  root.onclick = (event) => { if (event.target === root) closeModal(); };
  document.addEventListener("keydown", onKey);

  // Foco preso dentro do modal enquanto ele estiver aberto.
  const previous = document.activeElement;
  const focusables = () => dialog.querySelectorAll(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
  focusables()[0]?.focus();

  dialog.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const items = Array.from(focusables());
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  closeActiveModal = () => {
    document.removeEventListener("keydown", onKey);
    root.classList.add("hidden");
    clear(root);
    closeActiveModal = null;
    previous?.focus?.();
  };
  return dialog;

  function onKey(event) {
    if (event.key === "Escape") closeModal();
  }
}

export function closeModal() {
  closeActiveModal?.();
}

/* ------------------------------------------------------------- badges --- */

export function badge(state, label) {
  return h("span.badge", { dataset: { state } }, h("span.dot", { dataset: { state } }), label);
}

export function statusDot(state, { pulse = false, large = false } = {}) {
  return h("span", {
    class: `dot${large ? " lg" : ""}${pulse ? " pulse" : ""}`,
    dataset: { state },
  });
}

export function chip(label, value) {
  return h("span.chip", label, value !== undefined && h("b", String(value)));
}

/* -------------------------------------------------------------- utils --- */

export function confirmAction(title, message, onConfirm, confirmLabel = "Confirmar") {
  modal(title,
    h("p.dim", { style: { marginBottom: "20px" } }, message),
    h("div", { style: { display: "flex", gap: "8px", justifyContent: "flex-end" } },
      h("button.btn", { type: "button", onclick: closeModal }, "Cancelar"),
      h("button.btn.primary", {
        type: "button",
        onclick: () => { closeModal(); onConfirm(); },
      }, confirmLabel),
    ),
  );
}

export function debounce(fn, wait = 280) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}
