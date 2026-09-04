/**
 * Conjunto de ícones do sistema.
 *
 * Antes a interface usava emoji (📜 ⚔ ⚙ ⛨ …). Emoji é fonte do sistema
 * operacional: renderiza colorido em uma máquina e monocromático em outra,
 * muda de tamanho sem controle, e vira quadrado vazio quando a fonte não tem
 * o glifo — foi o que aconteceu com o botão de sair.
 *
 * Aqui são traços SVG desenhados em grade de 24px, todos com a mesma largura
 * de linha e as mesmas pontas. Herdam `currentColor`, então acompanham o
 * estado do elemento (hover, ativo, desabilitado) sem regra extra.
 */

import { svg } from "./dom.js";

const TRACOS = {
  // --- navegação ---
  overview:    ["M3 3h7v7H3z", "M14 3h7v7h-7z", "M14 14h7v7h-7z", "M3 14h7v7H3z"],
  monitor:     ["M3 12h4l3 8 4-16 3 8h4"],
  queue:       ["M8 6h13", "M8 12h13", "M8 18h13", "M3 6h.01", "M3 12h.01", "M3 18h.01"],
  history:     ["M3 12a9 9 0 1 0 3-6.7", "M3 4v5h5", "M12 8v4l3 2"],
  reports:     ["M4 20V10", "M10 20V4", "M16 20v-7", "M22 20H2"],
  consultants: ["M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2",
                "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8", "M22 21v-2a4 4 0 0 0-3-3.87"],
  logs:        ["M4 17l6-6-6-6", "M12 19h8"],
  status:      ["M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"],

  // --- ações ---
  logout:      ["M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4", "M16 17l5-5-5-5", "M21 12H9"],
  menu:        ["M3 6h18", "M3 12h18", "M3 18h18"],
  search:      ["M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z", "M21 21l-4.35-4.35"],
  close:       ["M18 6L6 18", "M6 6l12 12"],
  refresh:     ["M3 12a9 9 0 0 1 15-6.7L21 8", "M21 3v5h-5",
                "M21 12a9 9 0 0 1-15 6.7L3 16", "M3 21v-5h5"],

  // --- fluxo operacional ---
  inbox:       ["M22 12h-6l-2 3h-4l-2-3H2", "M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"],
  send:        ["M22 2L11 13", "M22 2l-7 20-4-9-9-4 20-7z"],
  userCheck:   ["M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2",
                "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8", "M16 11l2 2 4-4"],
  clipboard:   ["M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2",
                "M9 2h6v4H9z"],
  clock:       ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M12 7v5l3 2"],
  spinner:     ["M12 3v3", "M12 18v3", "M5.6 5.6l2.1 2.1", "M16.3 16.3l2.1 2.1",
                "M3 12h3", "M18 12h3", "M5.6 18.4l2.1-2.1", "M16.3 7.7l2.1-2.1"],
  check:       ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M8.5 12.5l2.5 2.5 4.5-5"],
  alert:       ["M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
                "M12 9v4", "M12 17h.01"],
  ban:         ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M5.6 5.6l12.8 12.8"],
  pause:       ["M10 4H6v16h4z", "M18 4h-4v16h4z"],
  plug:        ["M12 22v-5", "M9 8V2", "M15 8V2", "M18 8H6v4a6 6 0 0 0 12 0V8z"],
  plugOff:     ["M12 22v-5", "M18 8H6v4a6 6 0 0 0 12 0V8z", "M3 3l18 18"],
  empty:       ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z"],
};

/**
 * @param {string} nome  chave de TRACOS
 * @param {number} size  lado em px
 */
export function icon(nome, size = 18) {
  const raiz = svg("svg", {
    viewBox: "0 0 24 24",
    width: size,
    height: size,
    fill: "none",
    stroke: "currentColor",
    "stroke-width": 1.8,
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    focusable: "false",
    style: `flex:none;width:${size}px;height:${size}px`,
  });
  for (const d of TRACOS[nome] || TRACOS.empty) {
    raiz.appendChild(svg("path", { d }));
  }
  return raiz;
}

export const temIcone = (nome) => Object.prototype.hasOwnProperty.call(TRACOS, nome);
