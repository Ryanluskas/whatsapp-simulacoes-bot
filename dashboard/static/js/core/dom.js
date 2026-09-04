/**
 * Construção de DOM sem innerHTML.
 *
 * Nomes de consultor, textos de mensagem e mensagens de erro vêm do WhatsApp
 * e do portal do banco — são dados de terceiros. O painel anterior os
 * interpolava dentro de `innerHTML` (inclusive dentro de `value="..."`), o que
 * abria injeção a partir de um nome de contato. Aqui todo texto entra por
 * `textContent`, então não existe caminho de execução.
 */

/** Hiperscript mínimo: h("div.card", {onclick}, "texto", filho) */
export function h(spec, props = null, ...children) {
  const [tag, ...classes] = String(spec).split(".");
  const el = document.createElement(tag || "div");
  if (classes.length) el.className = classes.join(" ");

  if (props && typeof props === "object" && !isRenderable(props)) {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = [el.className, value].filter(Boolean).join(" ");
      else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key.startsWith("on") && typeof value === "function") {
        el.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === "html") el.innerHTML = value; // apenas para ícones internos
      else if (key in el && key !== "list" && typeof value !== "object") el[key] = value;
      else el.setAttribute(key, value === true ? "" : value);
    }
  } else if (props !== null && props !== undefined) {
    children.unshift(props);
  }

  append(el, children);
  return el;
}

export const svg = (tag, props = {}) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "text") el.textContent = value;
    else el.setAttribute(key, value);
  }
  return el;
};

function isRenderable(value) {
  return value instanceof Node || Array.isArray(value);
}

function append(parent, children) {
  for (const child of children.flat(4)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (child instanceof Node) {
      parent.appendChild(child);
    } else if (isNodeList(child)) {
      // HTMLCollection/NodeList são "vivas": copiar antes de mover os nós.
      Array.from(child).forEach((node) => parent.appendChild(node));
    } else {
      parent.appendChild(document.createTextNode(String(child)));
    }
  }
}

function isNodeList(value) {
  return value instanceof NodeList || value instanceof HTMLCollection;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function mount(node, ...children) {
  clear(node);
  append(node, children);
  return node;
}

/** Substitui o conteúdo mantendo a posição de rolagem (listas ao vivo). */
export function replaceKeepingScroll(node, ...children) {
  const top = node.scrollTop;
  mount(node, ...children);
  node.scrollTop = top;
  return node;
}
