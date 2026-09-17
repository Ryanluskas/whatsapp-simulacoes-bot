/**
 * Painel lateral de detalhe. Um por vez; Esc, clique fora e o botão fecham,
 * e o foco volta para quem abriu.
 */

import { $, h, mount } from "./dom.js";
import { icon } from "./icons.js";
import { trapFocus } from "./ui.js";

let active = null;

/**
 * @returns {{ head: HTMLElement, body: HTMLElement, close: () => void,
 *             isOpen: () => boolean }}
 */
export function openDrawer({ label }) {
  closeDrawer({ instant: true });
  const root = $("#drawer-root");
  const head = h("div.titles");
  const body = h("div.drawer-body");
  const closeBtn = h("button.btn.ghost.icon.close", { type: "button", "aria-label": "Fechar detalhes" },
    icon("close", 18));

  const panel = h("div.drawer", { role: "dialog", "aria-modal": "true", "aria-label": label },
    h("div.drawer-head", head, closeBtn),
    body,
  );
  const overlay = h("div.drawer-root", panel);
  mount(root, overlay);

  const previous = document.activeElement;
  let open = true;

  const onKey = (event) => {
    // Um modal aberto por cima (confirmação) cuida do próprio Esc
    if (!$("#modal-root")?.classList.contains("hidden")) return;
    if (event.key === "Escape") close();
    else trapFocus(panel, event);
  };

  function close({ instant = false } = {}) {
    if (!open) return;
    open = false;
    document.removeEventListener("keydown", onKey);
    const finish = () => { overlay.remove(); previous?.focus?.(); };
    if (instant) finish();
    else { overlay.classList.add("out"); setTimeout(finish, 180); }
    if (active?.close === close) active = null;
  }

  closeBtn.addEventListener("click", () => close());
  overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
  document.addEventListener("keydown", onKey);
  closeBtn.focus();

  active = { head, body, close, isOpen: () => open };
  return active;
}

export function closeDrawer(options) {
  active?.close(options);
}
