/**
 * Tabela de dados. No desktop é tabela; abaixo de 720px cada linha vira um
 * cartão (o rótulo da coluna vai em data-label), sem rolagem horizontal.
 */

import { h, mount } from "./dom.js";
import { skeleton } from "./ui.js";

/**
 * @param {object} o
 * @param {Array<{label: string, class?: string}>} o.columns  no máximo 6
 * @param {string} o.caption  descrição para leitor de tela
 */
export function dataTable({ columns, caption }) {
  const tbody = h("tbody");
  const table = h("table.data.as-cards",
    h("caption.sr-only", caption),
    h("thead", h("tr", columns.map((c) =>
      h("th", { class: c.class || "", scope: "col" }, c.label || h("span.sr-only", c.srLabel || "Ações"))))),
    tbody,
  );
  const wrap = h("div.table-wrap.as-cards", table);

  const cell = (index, content) => h("td", {
    class: columns[index]?.class || "",
    dataset: { label: columns[index]?.label || "" },
  }, content);

  return {
    wrap,
    tbody,
    /** `render(item)` devolve `{ cells: [...], onOpen? }`. */
    rows(items, render) {
      mount(tbody, items.map((item) => {
        const { cells, onOpen } = render(item);
        const props = onOpen ? {
          dataset: { clickable: "true" },
          tabindex: "0",
          onclick: (event) => {
            if (event.target.closest("button, a, input, select")) return;
            onOpen();
          },
          onkeydown: (event) => {
            if (event.target !== event.currentTarget) return;
            if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onOpen(); }
          },
        } : {};
        return h("tr", props, cells.map((content, i) => cell(i, content)));
      }));
    },
    loading(count = 6) {
      mount(tbody, Array.from({ length: count }, () =>
        h("tr", { "aria-hidden": "true" }, columns.map((_, i) => cell(i, skeleton(12, i === 0 ? "72%" : "58%"))))));
    },
    message(node) {
      mount(tbody, h("tr", h("td.full", { colspan: columns.length }, node)));
    },
  };
}

/** Célula de duas linhas: principal e apoio. */
export function twoLine(main, sub = null) {
  return h("div", h("div.cell-main", main), sub !== null && sub !== "" && h("div.cell-sub", sub));
}
