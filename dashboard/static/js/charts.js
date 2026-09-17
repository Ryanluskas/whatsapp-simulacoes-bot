/**
 * Gráficos em SVG escrito à mão.
 *
 * Não usamos uma biblioteca de gráficos por três motivos concretos:
 *  1. As especificações de marca deste sistema (ponta arredondada de 4px quadrada
 *     na linha de base, folga de 2px na cor da superfície entre segmentos
 *     empilhados, anel de 2px em marcadores sobrepostos) não são expressáveis
 *     nas bibliotecas usuais sem gambiarra;
 *  2. a versão anterior carregava a biblioteca de um CDN — o painel quebrava
 *     sem internet, o que é inaceitável numa ferramenta de operação local;
 *  3. todo gráfico aqui tem um gêmeo em tabela, para que nenhum valor dependa
 *     de passar o mouse ou de enxergar cor.
 *
 * As cores são tokens de `tokens.css` (var(--...)); `svg()` os aplica como
 * propriedade CSS. Nenhum HEX aqui.
 */

import { h, mount, svg } from "./core/dom.js";

const SURFACE = "var(--surface)";

export const COLOR = {
  // Rampa ordinal (lilás), do escuro ao claro.
  ramp: ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)"],
  brand: "var(--chart-series)",
  done: "var(--success)",
  error: "var(--error)",
  queued: "var(--muted)",
  processing: "var(--info)",
  consulting: "var(--lilac)",
  warning: "var(--warning)",
  idle: "var(--border-strong)",
};

export const STATE_COLOR = {
  completed: COLOR.done, done: COLOR.done,
  error: COLOR.error,
  queued: COLOR.queued, received: COLOR.queued, validated: COLOR.queued, identified: COLOR.queued,
  processing: COLOR.processing,
  consulting: COLOR.consulting, extracting: COLOR.consulting, replying: COLOR.consulting,
  interrupted: COLOR.warning, cancelled: COLOR.idle,
};

const PAD = { top: 14, right: 16, bottom: 26, left: 40 };
const BAR_MAX = 24;      // marca fina: nunca preencher a faixa inteira
const GAP = 2;           // folga na cor da superfície entre marcas que se tocam
const RADIUS = 4;        // ponta de dados arredondada

/* ====================================================== moldura comum ==== */

/**
 * Envolve um SVG com camada de dica, legenda e o gêmeo em tabela.
 * @param {object} o
 * @param {SVGElement} o.svgNode
 * @param {Array} [o.legend]  [{label, color, kind:"rect"|"line"}]
 * @param {object} [o.table]  {columns:[...], rows:[[...]]}
 */
export function chartFrame({ svgNode, legend = [], table = null, note = "" }) {
  const tip = h("div.chart-tip", { role: "status", "aria-live": "off" });
  const root = h("div.chart", svgNode, tip);
  root._tip = tip;

  const parts = [root];

  if (legend.length >= 2) {
    parts.push(h("div.chart-legend",
      legend.map((item) => h("span.key",
        h("span", {
          class: `swatch${item.kind === "line" ? " line" : ""}`,
          style: { background: item.color },
        }),
        item.label,
      )),
    ));
  }

  if (table) {
    const twin = h("div.chart-table.hidden", buildTable(table));
    const toggle = h("button.table-toggle", {
      type: "button",
      "aria-expanded": "false",
      onclick: () => {
        const open = twin.classList.toggle("hidden");
        toggle.setAttribute("aria-expanded", String(!open));
        toggle.textContent = open ? "Ver como tabela" : "Ocultar tabela";
      },
    }, "Ver como tabela");
    parts.push(h("div.chart-foot", note && h("span.note", note), toggle));
    parts.push(twin);
  } else if (note) {
    parts.push(h("div.chart-foot", h("span.note", note)));
  }

  return h("div", ...parts);
}

function buildTable({ columns, rows }) {
  return h("table",
    h("thead", h("tr", columns.map((c, i) =>
      h("th", { class: i > 0 ? "num" : "" }, c)))),
    h("tbody", rows.map((row) =>
      h("tr", row.map((cell, i) => h("td", { class: i > 0 ? "num" : "" }, String(cell)))))),
  );
}

/* ============================================================== escalas === */

const niceMax = (value) => {
  if (value <= 0) return 4;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const scaled = value / magnitude;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return step * magnitude;
};

function ticks(max, count = 4) {
  const step = max / count;
  return Array.from({ length: count + 1 }, (_, i) => Math.round(step * i));
}

function grid(g, { x0, x1, y, value, width }) {
  g.appendChild(svg("line", { class: "grid-line", x1: x0, x2: x1, y1: y, y2: y }));
  g.appendChild(svg("text", {
    class: "axis-text", x: x0 - 7, y: y + 3, "text-anchor": "end",
    text: formatTick(value, width),
  }));
}

const formatTick = (value) =>
  value >= 1000 ? `${(value / 1000).toFixed(value % 1000 === 0 ? 0 : 1)}k`
    : String(Math.round(value));

/* ========================================== série temporal (linha/área) === */

/**
 * Tendência ao longo do tempo. Uma série = uma cor, sem legenda (o título
 * já diz o que está plotado). Fio-cruz encontra o X; a dica lista o valor.
 */
export function lineChart(data, {
  width = 720, height = 210, xKey = "dia", yKey = "total",
  color = COLOR.brand, xLabel = (d) => d[xKey], valueLabel = (v) => String(v),
  tableColumns = ["Dia", "Total"],
} = {}) {
  if (!data.length) return null;

  const w = width, hgt = height;
  const x0 = PAD.left, x1 = w - PAD.right, y0 = PAD.top, y1 = hgt - PAD.bottom;
  const max = niceMax(Math.max(...data.map((d) => Number(d[yKey]) || 0)));
  const stepX = data.length > 1 ? (x1 - x0) / (data.length - 1) : 0;
  const px = (i) => (data.length > 1 ? x0 + stepX * i : (x0 + x1) / 2);
  const py = (v) => y1 - ((Number(v) || 0) / max) * (y1 - y0);

  const root = svg("svg", {
    viewBox: `0 0 ${w} ${hgt}`, role: "img",
    "aria-label": `Série temporal, ${data.length} pontos`,
  });

  const gridGroup = svg("g");
  ticks(max).forEach((value) =>
    grid(gridGroup, { x0, x1, y: py(value), value, width: w }));
  root.appendChild(gridGroup);

  const points = data.map((d, i) => [px(i), py(d[yKey])]);
  const path = points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");

  // Lavagem de ~10% — nunca um bloco saturado
  root.appendChild(svg("path", {
    d: `${path} L${points.at(-1)[0]},${y1} L${points[0][0]},${y1} Z`,
    fill: color, "fill-opacity": 0.1, stroke: "none",
  }));
  root.appendChild(svg("path", {
    d: path, fill: "none", stroke: color,
    "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
  }));

  // Marcador final ≥8px com anel de 2px na cor da superfície
  const [lastX, lastY] = points.at(-1);
  root.appendChild(svg("circle", {
    cx: lastX, cy: lastY, r: 4.5, fill: color, stroke: SURFACE, "stroke-width": 2,
  }));

  // Rótulo direto no ponto final — seletivo, não em todo ponto
  root.appendChild(svg("text", {
    class: "value-label", x: lastX - 8, y: lastY - 10, "text-anchor": "end",
    text: valueLabel(data.at(-1)[yKey]),
  }));

  root.appendChild(svg("line", { class: "axis-line", x1: x0, x2: x1, y1, y2: y1 }));

  // Rótulos do eixo X: no máximo ~7, para não colidir
  const every = Math.max(1, Math.ceil(data.length / 7));
  data.forEach((d, i) => {
    if (i % every && i !== data.length - 1) return;
    root.appendChild(svg("text", {
      class: "axis-text", x: px(i), y: y1 + 15, "text-anchor": "middle", text: xLabel(d),
    }));
  });

  const crosshair = svg("line", { class: "crosshair", y1: y0, y2: y1, opacity: 0 });
  const focus = svg("circle", { r: 5, fill: color, stroke: SURFACE, "stroke-width": 2, opacity: 0 });
  root.appendChild(crosshair);
  root.appendChild(focus);

  const frame = chartFrame({
    svgNode: root,
    table: { columns: tableColumns, rows: data.map((d) => [xLabel(d), d[yKey]]) },
  });
  const tip = frame.querySelector(".chart-tip");

  // O leitor mira numa data, nunca numa linha de 2px: o alvo é a faixa inteira
  const overlay = svg("rect", {
    x: x0 - stepX / 2, y: y0, width: x1 - x0 + stepX, height: y1 - y0,
    fill: "transparent", tabindex: "0", role: "application",
    "aria-label": "Passe o mouse ou use as setas para ler os valores",
  });
  root.appendChild(overlay);

  let index = data.length - 1;
  const show = (i) => {
    index = Math.max(0, Math.min(data.length - 1, i));
    const d = data[index];
    const [cx, cy] = [px(index), py(d[yKey])];
    crosshair.setAttribute("x1", cx);
    crosshair.setAttribute("x2", cx);
    crosshair.setAttribute("opacity", 1);
    focus.setAttribute("cx", cx);
    focus.setAttribute("cy", cy);
    focus.setAttribute("opacity", 1);
    mount(tip,
      h("div.tip-x", xLabel(d)),
      h("div.tip-row",
        h("span.tip-key", { style: { background: color } }),
        h("span.tip-val", valueLabel(d[yKey])),
      ),
    );
    tip.style.left = `${(cx / w) * 100}%`;
    tip.style.top = `${(cy / hgt) * 100}%`;
    tip.dataset.open = "true";
  };
  const hide = () => {
    tip.dataset.open = "false";
    crosshair.setAttribute("opacity", 0);
    focus.setAttribute("opacity", 0);
  };

  overlay.addEventListener("pointermove", (event) => {
    const box = root.getBoundingClientRect();
    const rel = ((event.clientX - box.left) / box.width) * w;
    show(Math.round((rel - x0) / (stepX || 1)));
  });
  overlay.addEventListener("pointerleave", hide);
  overlay.addEventListener("focus", () => show(index));
  overlay.addEventListener("blur", hide);
  overlay.addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight") { event.preventDefault(); show(index + 1); }
    if (event.key === "ArrowLeft") { event.preventDefault(); show(index - 1); }
  });

  return frame;
}

/* ================================================== colunas (uma série) === */

export function barChart(data, {
  width = 720, height = 200, xKey = "hora", yKey = "total",
  color = COLOR.brand, xLabel = (d) => d[xKey], tipLabel = (d) => String(d[xKey]),
  tableColumns = ["Faixa", "Total"], everyNth = 3,
} = {}) {
  if (!data.length) return null;

  const w = width, hgt = height;
  const x0 = PAD.left, x1 = w - PAD.right, y0 = PAD.top, y1 = hgt - PAD.bottom;
  const max = niceMax(Math.max(...data.map((d) => Number(d[yKey]) || 0)));
  const band = (x1 - x0) / data.length;
  const barW = Math.min(BAR_MAX, band - GAP * 2);

  const root = svg("svg", {
    viewBox: `0 0 ${w} ${hgt}`, role: "img",
    "aria-label": `Distribuição em ${data.length} faixas`,
  });

  const gridGroup = svg("g");
  ticks(max).forEach((value) => grid(gridGroup, { x0, x1, y: y1 - (value / max) * (y1 - y0), value }));
  root.appendChild(gridGroup);

  const frame = chartFrame({
    svgNode: root,
    table: { columns: tableColumns, rows: data.map((d) => [xLabel(d), d[yKey]]) },
  });
  const tip = frame.querySelector(".chart-tip");

  data.forEach((d, i) => {
    const value = Number(d[yKey]) || 0;
    const bx = x0 + band * i + (band - barW) / 2;
    const bh = (value / max) * (y1 - y0);
    const by = y1 - bh;

    const group = svg("g");
    if (value > 0) {
      group.appendChild(roundedTopBar(bx, by, barW, bh, color));
    }
    // Alvo de toque cobre a faixa inteira, não só os pixels pintados
    const hit = svg("rect", {
      class: "hit", x: x0 + band * i, y: y0, width: band, height: y1 - y0,
      tabindex: "0", role: "img", "aria-label": `${tipLabel(d)}: ${value}`,
    });
    const enter = () => {
      mount(tip,
        h("div.tip-x", tipLabel(d)),
        h("div.tip-row",
          h("span.tip-key", { style: { background: color } }),
          h("span.tip-val", String(value)),
        ),
      );
      tip.style.left = `${((bx + barW / 2) / w) * 100}%`;
      tip.style.top = `${(Math.min(by, y1 - 8) / hgt) * 100}%`;
      tip.dataset.open = "true";
      group.style.opacity = "0.82";
    };
    const leave = () => { tip.dataset.open = "false"; group.style.opacity = "1"; };
    hit.addEventListener("pointerenter", enter);
    hit.addEventListener("pointerleave", leave);
    hit.addEventListener("focus", enter);
    hit.addEventListener("blur", leave);

    root.appendChild(group);
    root.appendChild(hit);
  });

  root.appendChild(svg("line", { class: "axis-line", x1: x0, x2: x1, y1, y2: y1 }));
  data.forEach((d, i) => {
    if (i % everyNth) return;
    root.appendChild(svg("text", {
      class: "axis-text", x: x0 + band * i + band / 2, y: y1 + 15,
      "text-anchor": "middle", text: xLabel(d),
    }));
  });

  return frame;
}

/** Barra com topo arredondado e base quadrada — a ponta de dados é o topo. */
function roundedTopBar(x, y, w, hgt, fill) {
  const r = Math.min(RADIUS, w / 2, Math.max(0, hgt));
  const d = hgt <= r
    ? `M${x},${y + hgt} L${x},${y} L${x + w},${y} L${x + w},${y + hgt} Z`
    : `M${x},${y + hgt} L${x},${y + r} Q${x},${y} ${x + r},${y} `
      + `L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + hgt} Z`;
  return svg("path", { class: "mark", d, fill });
}

/* ============================================ barras divergentes (bem/mal) === */

/**
 * Concluídas acima da linha, erros abaixo. A POSIÇÃO carrega o sinal, então
 * a leitura não depende de distinguir verde de vermelho — o par verde/vermelho
 * reprova a checagem de daltonismo isoladamente, e é por isso que a forma
 * escolhida encoda a polaridade na geometria.
 */
export function divergingChart(data, {
  width = 720, height = 220, xKey = "dia",
  upKey = "completed", downKey = "errors",
  upLabel = "Concluídas", downLabel = "Erros",
  xLabel = (d) => d[xKey],
} = {}) {
  if (!data.length) return null;

  const w = width, hgt = height;
  const x0 = PAD.left, x1 = w - PAD.right;
  const top = PAD.top, bottom = hgt - PAD.bottom;
  const upMax = niceMax(Math.max(1, ...data.map((d) => Number(d[upKey]) || 0)));
  const downMax = niceMax(Math.max(1, ...data.map((d) => Number(d[downKey]) || 0)));
  // A base fica proporcional aos dois braços, sem esmagar o menor
  const zone = bottom - top;
  const upZone = zone * (upMax / (upMax + downMax));
  const zeroY = top + upZone;

  const band = (x1 - x0) / data.length;
  const barW = Math.min(BAR_MAX, band - GAP * 2);

  const root = svg("svg", {
    viewBox: `0 0 ${w} ${hgt}`, role: "img",
    "aria-label": `${upLabel} acima e ${downLabel} abaixo da linha de base, por dia`,
  });

  root.appendChild(svg("line", {
    class: "grid-line", x1: x0, x2: x1, y1: top, y2: top,
  }));
  root.appendChild(svg("text", {
    class: "axis-text", x: x0 - 7, y: top + 3, "text-anchor": "end", text: String(upMax),
  }));
  root.appendChild(svg("text", {
    class: "axis-text", x: x0 - 7, y: bottom + 3, "text-anchor": "end", text: String(downMax),
  }));

  const frame = chartFrame({
    svgNode: root,
    legend: [
      { label: upLabel, color: COLOR.done, kind: "rect" },
      { label: downLabel, color: COLOR.error, kind: "rect" },
    ],
    table: {
      columns: ["Dia", upLabel, downLabel],
      rows: data.map((d) => [xLabel(d), d[upKey] || 0, d[downKey] || 0]),
    },
  });
  const tip = frame.querySelector(".chart-tip");

  data.forEach((d, i) => {
    const up = Number(d[upKey]) || 0;
    const down = Number(d[downKey]) || 0;
    const bx = x0 + band * i + (band - barW) / 2;
    const group = svg("g");

    if (up > 0) {
      const uh = (up / upMax) * (upZone - 6);
      group.appendChild(roundedTopBar(bx, zeroY - GAP - uh, barW, uh, COLOR.done));
    }
    if (down > 0) {
      const dh = (down / downMax) * (zone - upZone - 6);
      group.appendChild(roundedBottomBar(bx, zeroY + GAP, barW, dh, COLOR.error));
    }

    const hit = svg("rect", {
      class: "hit", x: x0 + band * i, y: top, width: band, height: zone,
      tabindex: "0", role: "img",
      "aria-label": `${xLabel(d)}: ${up} ${upLabel}, ${down} ${downLabel}`,
    });
    const enter = () => {
      mount(tip,
        h("div.tip-x", xLabel(d)),
        h("div.tip-row",
          h("span.tip-key", { style: { background: COLOR.done } }),
          h("span.tip-val", String(up)), h("span.tip-name", upLabel)),
        h("div.tip-row",
          h("span.tip-key", { style: { background: COLOR.error } }),
          h("span.tip-val", String(down)), h("span.tip-name", downLabel)),
      );
      tip.style.left = `${((bx + barW / 2) / w) * 100}%`;
      tip.style.top = `${(zeroY / hgt) * 100}%`;
      tip.dataset.open = "true";
      group.style.opacity = "0.82";
    };
    const leave = () => { tip.dataset.open = "false"; group.style.opacity = "1"; };
    hit.addEventListener("pointerenter", enter);
    hit.addEventListener("pointerleave", leave);
    hit.addEventListener("focus", enter);
    hit.addEventListener("blur", leave);

    root.appendChild(group);
    root.appendChild(hit);
  });

  // Linha zero neutra — o "nada" do gráfico divergente
  root.appendChild(svg("line", { class: "axis-line", x1: x0, x2: x1, y1: zeroY, y2: zeroY }));

  const every = Math.max(1, Math.ceil(data.length / 7));
  data.forEach((d, i) => {
    if (i % every && i !== data.length - 1) return;
    root.appendChild(svg("text", {
      class: "axis-text", x: x0 + band * i + band / 2, y: hgt - 6,
      "text-anchor": "middle", text: xLabel(d),
    }));
  });

  return frame;
}

function roundedBottomBar(x, y, w, hgt, fill) {
  const r = Math.min(RADIUS, w / 2, Math.max(0, hgt));
  const d = hgt <= r
    ? `M${x},${y} L${x + w},${y} L${x + w},${y + hgt} L${x},${y + hgt} Z`
    : `M${x},${y} L${x + w},${y} L${x + w},${y + hgt - r} `
      + `Q${x + w},${y + hgt} ${x + w - r},${y + hgt} L${x + r},${y + hgt} `
      + `Q${x},${y + hgt} ${x},${y + hgt - r} Z`;
  return svg("path", { class: "mark", d, fill });
}

/* ================================================ ranking (barras horiz.) === */

export function rankChart(rows, {
  width = 560, labelKey = "nome", valueKey = "total",
  color = COLOR.brand, tableColumns = ["Consultor", "Total"], valueLabel = (v) => String(v),
} = {}) {
  if (!rows.length) return null;

  const rowH = 30;
  const labelW = 132;
  const w = width;
  const hgt = rows.length * rowH + 8;
  const x0 = labelW;
  const x1 = w - 52;
  const max = Math.max(1, ...rows.map((r) => Number(r[valueKey]) || 0));

  const root = svg("svg", {
    viewBox: `0 0 ${w} ${hgt}`, role: "img", "aria-label": `Ranking de ${rows.length} itens`,
  });

  const frame = chartFrame({
    svgNode: root,
    table: { columns: tableColumns, rows: rows.map((r) => [r[labelKey], r[valueKey]]) },
  });

  rows.forEach((row, i) => {
    const value = Number(row[valueKey]) || 0;
    const y = i * rowH + 4;
    const barH = Math.min(BAR_MAX - 8, rowH - 12);
    const barY = y + (rowH - barH) / 2 - 4;
    const barW = Math.max(value > 0 ? 3 : 0, (value / max) * (x1 - x0));

    root.appendChild(svg("text", {
      class: "axis-text", x: labelW - 10, y: barY + barH / 2 + 3.5, "text-anchor": "end",
      text: truncateLabel(row[labelKey], 17),
    }));
    if (value > 0) root.appendChild(roundedEndBar(x0, barY, barW, barH, color));
    // Valor na ponta da barra: rótulo direto, fora da marca (sempre cabe)
    root.appendChild(svg("text", {
      class: "value-label", x: x0 + barW + 7, y: barY + barH / 2 + 3.5, text: valueLabel(value),
    }));
  });

  return frame;
}

/** Barra horizontal: ponta de dados arredondada, quadrada na linha de base. */
function roundedEndBar(x, y, w, hgt, fill) {
  const r = Math.min(RADIUS, hgt / 2, Math.max(0, w));
  const d = w <= r
    ? `M${x},${y} L${x + w},${y} L${x + w},${y + hgt} L${x},${y + hgt} Z`
    : `M${x},${y} L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} `
      + `L${x + w},${y + hgt - r} Q${x + w},${y + hgt} ${x + w - r},${y + hgt} L${x},${y + hgt} Z`;
  return svg("path", { class: "mark", d, fill });
}

const truncateLabel = (text, max) => {
  const value = String(text ?? "");
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
};

/* ============================================ composição (barra empilhada) === */

/**
 * Uma barra empilhada horizontal. Segmentos separados por folga de 2px na cor
 * da superfície — nunca por contorno desenhado.
 */
export function stackedBar(segments, { width = 560, height = 16 } = {}) {
  const total = segments.reduce((sum, s) => sum + (Number(s.value) || 0), 0);
  if (!total) return null;

  // Barra 1D: a altura é fixa em pixels e só a largura acompanha o container.
  // Com o `height:auto` padrão dos gráficos, um viewBox largo encolhia a barra
  // para uns 5px de altura em telas estreitas.
  const root = svg("svg", {
    viewBox: `0 0 ${width} ${height}`, role: "img",
    preserveAspectRatio: "none",
    style: `width:100%;height:${height + 6}px`,
    "aria-label": segments.map((s) => `${s.label}: ${s.value}`).join(", "),
  });

  const usable = width - GAP * Math.max(0, segments.filter((s) => s.value > 0).length - 1);
  let x = 0;
  segments.forEach((seg) => {
    const value = Number(seg.value) || 0;
    if (!value) return;
    const w = (value / total) * usable;
    root.appendChild(svg("rect", {
      x, y: 0, width: Math.max(2, w), height, rx: 3, fill: seg.color,
      class: "mark",
    }));
    x += w + GAP;
  });

  return chartFrame({
    svgNode: root,
    legend: segments.filter((s) => s.value > 0).map((s) => ({
      label: `${s.label} · ${s.value}`, color: s.color, kind: "rect",
    })),
    table: {
      columns: ["Estado", "Total", "%"],
      rows: segments.map((s) => [s.label, s.value, `${((s.value / total) * 100).toFixed(1)}%`]),
    },
  });
}

/* ==================================================== funil (ordinal) ===== */

/**
 * Etapas ordenadas → rampa de UMA cor com passos de luminosidade monotônicos
 * (validada com `--ordinal`). A ordem se lê na própria cor.
 */
export function funnelChart(stages, { width = 560 } = {}) {
  const rows = stages.filter((s) => Number.isFinite(Number(s.total)));
  if (!rows.length) return null;

  const rowH = 42;
  const labelW = 168;
  const hgt = rows.length * rowH;
  const x0 = labelW;
  const x1 = width - 62;
  const max = Math.max(1, ...rows.map((r) => Number(r.total) || 0));
  // Passos da rampa validada, do mais claro (topo do funil) ao mais escuro
  const ramp = [COLOR.ramp[4], COLOR.ramp[3], COLOR.ramp[2], COLOR.ramp[1], COLOR.ramp[0]];

  const root = svg("svg", {
    viewBox: `0 0 ${width} ${hgt}`, role: "img", "aria-label": "Funil operacional do dia",
  });

  rows.forEach((row, i) => {
    const value = Number(row.total) || 0;
    const y = i * rowH;
    const barH = 20;
    const barY = y + 6;
    const barW = Math.max(value > 0 ? 3 : 0, (value / max) * (x1 - x0));
    const color = ramp[Math.min(i, ramp.length - 1)];

    root.appendChild(svg("text", {
      class: "axis-text", x: labelW - 12, y: barY + 14, "text-anchor": "end",
      text: truncateLabel(row.etapa, 22),
    }));
    if (value > 0) root.appendChild(roundedEndBar(x0, barY, barW, barH, color));
    root.appendChild(svg("text", {
      class: "value-label", x: x0 + barW + 8, y: barY + 14, text: String(value),
    }));

    // Taxa de conversão entre etapas — o número que o funil existe para mostrar
    if (i > 0) {
      const previous = Number(rows[i - 1].total) || 0;
      const rate = previous ? Math.round((value / previous) * 100) : 0;
      root.appendChild(svg("text", {
        class: "axis-text", x: x0 - 6, y: barY - 3, "text-anchor": "end",
        text: `${rate}%`,
      }));
    }
  });

  return chartFrame({
    svgNode: root,
    table: { columns: ["Etapa", "Total"], rows: rows.map((r) => [r.etapa, r.total]) },
    note: "Percentual = conversão em relação à etapa anterior.",
  });
}

/* ======================================================== sparkline ====== */

export function sparkline(values, { width = 120, height = 26, color = COLOR.brand } = {}) {
  const data = values.map((v) => Number(v) || 0);
  if (data.length < 2) return null;
  const max = Math.max(1, ...data);
  const stepX = width / (data.length - 1);
  const path = data
    .map((v, i) => `${i ? "L" : "M"}${(i * stepX).toFixed(1)},${(height - (v / max) * (height - 3) - 1.5).toFixed(1)}`)
    .join(" ");

  const root = svg("svg", {
    viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true", class: "spark",
    style: `width:${width}px;height:${height}px`,
  });
  root.appendChild(svg("path", {
    d: path, fill: "none", stroke: color, "stroke-width": 2,
    "stroke-linecap": "round", "stroke-linejoin": "round", opacity: 0.75,
  }));
  const lastY = height - (data.at(-1) / max) * (height - 3) - 1.5;
  root.appendChild(svg("circle", {
    cx: width, cy: lastY, r: 2.5, fill: color, stroke: SURFACE, "stroke-width": 1.5,
  }));
  return root;
}
