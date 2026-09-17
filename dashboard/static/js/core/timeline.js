/** Linha do tempo vertical: hora, marcador com ícone e o que aconteceu. */

import { h } from "./dom.js";
import { icon } from "./icons.js";

/**
 * @param {Array<{time: string, tone?: string, icon?: string, title: string,
 *   detail?: string, evidence?: string, message?: string, image?: object}>} items
 */
export function timeline(items, { label = "Linha do tempo" } = {}) {
  return h("ol.timeline", { "aria-label": label },
    items.map((item) => h("li.tl-item",
      h("span.tl-time", item.time),
      h("span.tl-node", { dataset: { tone: item.tone || "neutral" }, "aria-hidden": "true" },
        icon(item.icon || "clock", 13)),
      h("div.tl-body",
        h("div.t", item.title),
        item.detail && h("div.d", item.detail),
        item.message && h("div.msg", item.message),
        item.evidence && h("div.ev", item.evidence),
        item.image,
      ),
    )),
  );
}
