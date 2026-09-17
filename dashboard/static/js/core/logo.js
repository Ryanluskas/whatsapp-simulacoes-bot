/**
 * Marca da Allana.
 *
 * A personagem é parte da marca, não da interface: aparece na barra lateral,
 * no login e — pequena — em estados vazios e de erro. Em nenhum outro lugar.
 * Os arquivos em /img são recortes da arte original (ver README do painel).
 */

import { h } from "./dom.js";

/** Cabeça da Allana em círculo. `alt` vazio quando o nome já está ao lado. */
export function allanaAvatar(size = 40, { alt = "" } = {}) {
  return h("img.avatar", {
    src: "/img/allana-avatar.webp",
    width: size,
    height: size,
    alt,
    decoding: "async",
    loading: "lazy",
    style: { width: `${size}px`, height: `${size}px` },
  });
}

/** Arte completa (com o robô), em quadrado arredondado. */
export function allanaLogo(size = 88, { alt = "Allana Bot" } = {}) {
  return h("img.avatar", {
    src: "/img/allana-logo.webp",
    width: size,
    height: size,
    alt,
    decoding: "async",
    dataset: { shape: "app" },
    style: { width: `${size}px`, height: `${size}px` },
  });
}
