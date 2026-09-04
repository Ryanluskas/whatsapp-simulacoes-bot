/**
 * Marca da Allana.
 *
 * Monograma geométrico: um "A" construído com dois traços e uma barra, dentro
 * de um quadrado arredondado. Foi desenhado em grade de 40px para continuar
 * legível a 16px (favicon) sem virar borrão — por isso os traços têm largura
 * fixa e as pontas são arredondadas.
 *
 * A cor vem dos tokens de marca, então trocar a paleta troca a logo junto.
 */

import { svg } from "./dom.js";

const NS = "http://www.w3.org/2000/svg";
let gradienteSeq = 0;

/** Só o símbolo. Use em favicon, avatar e onde o nome já aparece ao lado. */
export function logoMark(size = 36) {
  const id = `allana-g${++gradienteSeq}`;
  const root = svg("svg", {
    viewBox: "0 0 40 40",
    width: size,
    height: size,
    role: "img",
    "aria-label": "Allana",
    style: `flex:none;width:${size}px;height:${size}px`,
  });

  const defs = document.createElementNS(NS, "defs");
  const grad = svg("linearGradient", { id, x1: "0", y1: "0", x2: "1", y2: "1" });
  grad.appendChild(svg("stop", { offset: "0", "stop-color": "var(--brand-400)" }));
  grad.appendChild(svg("stop", { offset: "1", "stop-color": "var(--brand-700)" }));
  defs.appendChild(grad);
  root.appendChild(defs);

  root.appendChild(svg("rect", {
    x: 0, y: 0, width: 40, height: 40, rx: 11,
    fill: `url(#${id})`,
  }));

  // O "A": dois traços que se encontram no ápice, mais a barra transversal.
  const traco = {
    stroke: "#FFFFFF",
    "stroke-width": 2.7,
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    fill: "none",
  };
  root.appendChild(svg("path", { d: "M12.2 28.6 L20 11.6 L27.8 28.6", ...traco }));
  root.appendChild(svg("path", { d: "M16.1 22.4 H23.9", ...traco, opacity: 0.92 }));

  return root;
}

/**
 * Símbolo + nome. `sub` é a linha de apoio (some quando vazia).
 */
export function logoLockup({ size = 34, sub = "Simulações", compact = false } = {}) {
  const wrap = document.createElement("div");
  wrap.className = "logo";
  wrap.appendChild(logoMark(size));

  if (compact) return wrap;

  const texto = document.createElement("div");
  texto.className = "logo-text";

  const nome = document.createElement("div");
  nome.className = "logo-name";
  nome.textContent = "Allana";
  texto.appendChild(nome);

  if (sub) {
    const apoio = document.createElement("div");
    apoio.className = "logo-sub";
    apoio.textContent = sub;
    texto.appendChild(apoio);
  }

  wrap.appendChild(texto);
  return wrap;
}
