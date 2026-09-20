"""Cliente do WhatsApp Web.

Todo o Playwright vive dentro de ``WhatsAppService._thread``. Quem esta' de
fora (servidor HTTP, workers, manager) so' conversa por ``call``/``post``, que
enfileiram o trabalho para a thread dona - ver ``actor.py`` para o porque.

Alem de consertar a arquitetura, esta versao corrige tres defeitos concretos
da anterior:

* ``sender_id`` saia como a string ``"false"``. O ``data-id`` do WhatsApp e'
  ``false_<chat>@g.us_<msgid>_<remetente>@c.us``; o codigo antigo fazia
  ``split("_")[0]``, que devolve o prefixo booleano. Como ``consultants.phone``
  e' UNIQUE, todos os consultores colapsavam numa unica linha.
* No primeiro boot (sem ``state.json``) o bot lia as ultimas 30 mensagens do
  chat e tratava todas como pedidos novos. Agora a primeira leitura de cada
  chat so' cria a linha de base.
* A resposta era digitada no chat que estivesse aberto na tela. Agora todo
  envio declara o chat de destino e, quando possivel, cita a mensagem original.
"""

from __future__ import annotations

import base64
import queue
import re
import threading
import unicodedata
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .actor import ThreadActor
from .clock import now_iso
from .models import (EnvioNaoSaiu, EnvioSemProva, IncomingMessage, QuoteStatus,
                     ResultadoEnvio)
from .navegador_zumbi import encerrar_orfaos
from .state_store import StateStore

# --------------------------------------------------------------------- estados
DISCONNECTED = "disconnected"
STARTING = "starting"
QR = "qr"
CONNECTED = "connected"

CHAT_LIST = "#pane-side"
# Campo de digitacao. Lista de seletores separada por virgula: o CSS aceita
# e o Playwright casa com o primeiro que existir. Um seletor so' derrubou um
# envio de verdade com "Timeout 15000ms waiting for footer div[contenteditable]".
COMPOSER = (
    'footer div[contenteditable="true"], '
    'div[contenteditable="true"][data-tab="10"], '
    'div[contenteditable="true"][role="textbox"][aria-label*="ensagem"], '
    'div[contenteditable="true"][role="textbox"][aria-label*="essage"]'
)
# O campo de busca ja' trocou de data-tab varias vezes entre versoes do
# WhatsApp Web. Um seletor so' significa que o bot para de achar a conversa
# quando eles mexem no HTML -- foi o que aconteceu. Ordem: do mais especifico
# ao mais generico.
SEARCH_BOX_CANDIDATOS = (
    # O WhatsApp Web trocou o campo de busca de <div contenteditable> para um
    # <input> de verdade. Manter as duas familias: a versao instalada na
    # maquina do operador nao e' a mesma em todo lugar.
    '#side input[type="text"]',
    '#side input[role="textbox"]',
    'input[aria-label*="esquis"]',                 # "Pesquisar"
    'input[aria-label*="earch"]',
    'div[contenteditable="true"][data-tab="3"]',
    '#side div[contenteditable="true"][role="textbox"]',
    'div[role="textbox"][aria-label*="esquis"]',
    'div[role="textbox"][aria-label*="earch"]',
    '#side div[contenteditable="true"]',
)
QR_CANVAS = "canvas[aria-label], div[data-ref] canvas"

def _explicar_falha_de_perfil(mensagem: str, perfil: Path) -> str:
    """Traduz o erro mudo do Chromium quando o perfil ja' esta' em uso.

    "Target page, context or browser has been closed" e' o que o Chromium
    devolve quando outro processo ja' segura o mesmo user_data_dir: ele
    entrega o comando a' instancia existente e encerra a nova. A mensagem
    crua nao ajuda ninguem, e foi ela que apareceu a noite toda enquanto
    navegadores orfaos de execucoes anteriores continuavam vivos.
    """
    pistas = (
        "target page, context or browser has been closed",
        "browser has been closed",
        "processsingleton",
        "failed to create a processsingleton",
    )
    if not any(p in mensagem.lower() for p in pistas):
        return mensagem
    return (
        f"{mensagem} — provavelmente o perfil '{perfil.name}' já está aberto em "
        "outro navegador. Feche as janelas do Brave que o bot abriu (inclusive "
        "as que ficaram de execuções anteriores) e inicie de novo. No Gerenciador "
        "de Tarefas elas aparecem como brave.exe."
    )


# Rotulos que NUNCA devem ser clicados ao tentar responder: encaminhar manda
# os dados do cliente para outra conversa; apagar destroi a mensagem.
_PROIBIDO_CLICAR = re.compile(r"encaminh|forward|apagar|delete|excluir", re.I)


def _curto_titulo(texto: str, limite: int = 60) -> str:
    """Encurta titulos longos: a lista de participantes de um grupo grande
    ocupa a linha de log inteira e esconde o que importa."""
    texto = (texto or "").strip()
    return texto if len(texto) <= limite else texto[:limite - 1] + "…"


def _normalizar(texto: str) -> str:
    """Forma canonica para comparar nomes vindos do DOM com os do .env.

    O WhatsApp pode servir o acento decomposto (c + cedilha combinante) e o
    .env guardar a forma composta: strings diferentes byte a byte, com o
    mesmo significado. Normalizar aqui e' o que evita um "nao achei" mudo.
    """
    return " ".join(unicodedata.normalize("NFC", texto or "").split()).casefold()


_PRE_PLAIN = re.compile(r"^\[(?P<time>[^\]]+)\]\s*(?P<name>.*?):\s*$")

# JS de leitura. Roda uma vez por ciclo e devolve tudo o que precisamos,
# evitando dezenas de idas e voltas entre Python e o navegador.
# Procura a conversa na lista lateral comparando o texto NORMALIZADO, e marca
# o elemento para o Playwright clicar de verdade.
#
# Por que nao um seletor [title="..."]: o WhatsApp pode servir o titulo em
# forma decomposta (c + cedilha combinante) enquanto o .env tem a forma
# composta (c). Sao strings diferentes byte a byte e o seletor nunca casaria,
# sem nenhum sintoma alem de "nao achei". Normalizar resolve isso e de quebra
# tolera espaco duplo e diferenca de maiuscula.
MARCA_ALVO = "data-allana-alvo"

ACHAR_CONVERSA_JS = """
(nome) => {
  const norm = (s) => (s || '').normalize('NFC').replace(/\s+/g, ' ').trim().toLowerCase();
  const alvo = norm(nome);
  if (!alvo) return false;

  document.querySelectorAll('[data-allana-alvo]').forEach(
    (el) => el.removeAttribute('data-allana-alvo'));

  const lista = document.querySelector('#pane-side')
             || document.querySelector('#side')
             || document;

  for (const el of Array.from(lista.querySelectorAll('span[title]'))) {
    if (norm(el.getAttribute('title')) !== alvo) continue;
    const clicavel = el.closest('[role="listitem"], [role="row"], div[tabindex]') || el;
    clicavel.setAttribute('data-allana-alvo', '1');
    return true;
  }
  return false;
}
"""

# Ids das mensagens visiveis cujo texto comeca com um trecho dado.
#
# Serve para o bot RECONHECER o que ele mesmo acabou de enviar. Deduzir isso
# pelo HTML nao funcionou: esta versao do WhatsApp nao usa as classes
# message-in/message-out, serve o data-id sem o prefixo false_/true_, e os
# icones de recibo nao casam com nenhum seletor previsivel. Lembrar do que
# enviamos nao depende de convencao nenhuma do DOM.
IDS_POR_TEXTO_JS = r"""
([trecho, limite]) => {
  const main = document.querySelector('#main') || document.body;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const alvo = norm(trecho);
  if (!alvo) return [];

  const achados = [];
  const linhas = Array.from(main.querySelectorAll('[data-id]')).slice(-limite);
  for (const el of linhas) {
    const id = el.getAttribute('data-id') || '';
    if (!id) continue;
    const corpo = el.querySelector('span.selectable-text') || el;
    if (norm(corpo.innerText).startsWith(alvo)) achados.push(id);
  }
  return achados;
}
"""

# Trecho JS reaproveitado: "esta linha foi enviada por NOS?"
#
# Observado no DOM real desta instalacao (ferramentas/dump_dom.py):
#
# * ``.message-in`` / ``.message-out`` **nao existem**. Todo codigo que
#   dependia delas estava morto -- e a ferramenta chegou a apontar uma
#   resposta do proprio bot como "ultima mensagem recebida".
# * ``data-pre-plain-text`` existe em toda mensagem real, no formato
#   ``"[20:17, 30/08/2026] Operacional Capital: "``. O nome fica entre "] "
#   e ": ". E' o sinal mais confiavel que ha' aqui.
# * O ``data-id`` vem pelado, e as enviadas comecam com ``3EB0``.
# * O recibo de entrega so' aparece no que nos enviamos.
#
# Cascata, nesta ordem. Nenhum dos tres depende de classe CSS.
EH_NOSSA_JS = r"""
  const autorDaLinha = (el) => {
    const marca = el.querySelector('[data-pre-plain-text]')
               || (el.matches && el.matches('[data-pre-plain-text]') ? el : null);
    const bruto = marca ? (marca.getAttribute('data-pre-plain-text') || '') : '';
    const corte = bruto.indexOf('] ');
    if (corte < 0) return '';
    const resto = bruto.slice(corte + 2);
    const fim = resto.lastIndexOf(': ');
    return (fim < 0 ? resto : resto.slice(0, fim)).trim();
  };

  const norm = (s) => (s || '').normalize('NFC').replace(/\s+/g, ' ')
                        .trim().toLowerCase();

  const ehNossa = (el, id, nomeProprio) => {
    // Tres provas INDEPENDENTES, e basta uma. A ordem e' da mais forte para a
    // mais fraca, mas nenhuma delas pode responder "nao e' nossa" sozinha.
    //
    // Era o que acontecia: a comparacao de nome tinha `return` direto, entao
    // um BOT_SELF_NAME diferente do nome real da conta DESLIGAVA as outras
    // duas. Em 20/09/2026 o grupo mostrava "Operacional", o .env dizia
    // "Operacional Capital", e o bot passou a tratar mensagens da propria
    // conta como pedido de consultor -- tres solicitacoes criadas a partir do
    // que ele mesmo tinha enviado, todas com id "3EB0", que a prova (2)
    // reconheceria na hora.
    //
    // 1) Quem assinou a mensagem.
    const autor = autorDaLinha(el);
    if (autor && nomeProprio && norm(autor) === norm(nomeProprio)) return true;
    // 2) Prefixo do id: o WhatsApp Web gera "3EB0..." no que ele mesmo envia.
    //
    // `album-` na frente: quando o WhatsApp agrupa varias imagens nossas, o
    // id vira "album-3EB0...-3EB0...". Sem tirar esse prefixo, um album de
    // cards NOSSOS nao era reconhecido como nosso e voltava para o laco de
    // leitura como se fosse pedido de consultor.
    const nu = id.startsWith('album-') ? id.slice(6) : id;
    if (nu.startsWith('true_') || nu.startsWith('3EB0')) return true;
    // 3) Recibo de entrega: so' existe em mensagem propria.
    if (el.querySelector('[data-icon^="msg-"], [data-icon^="status-"]')) return true;
    return false;
  };
"""

# Quem assinou as mensagens visiveis, sem repetir.
#
# Serve para conferir o BOT_SELF_NAME contra a realidade logo no boot. Se o
# nome configurado nao aparecer entre os autores, TODA a deteccao de autoria
# cai para os sinais fracos -- e o bot pode voltar a ler as proprias
# mensagens. Melhor descobrir no boot do que em producao.
AUTORES_VISIVEIS_JS = r"""
() => {
  const vistos = new Map();   // nome normalizado -> como aparece na tela
  for (const el of document.querySelectorAll('[data-pre-plain-text]')) {
    const bruto = el.getAttribute('data-pre-plain-text') || '';
    const corte = bruto.indexOf('] ');
    if (corte < 0) continue;
    const resto = bruto.slice(corte + 2);
    const fim = resto.lastIndexOf(': ');
    const nome = (fim < 0 ? resto : resto.slice(0, fim)).trim();
    if (!nome) continue;
    const chave = nome.normalize('NFC').replace(/\s+/g, ' ').trim().toLowerCase();
    if (!vistos.has(chave)) vistos.set(chave, nome);
  }
  return Array.from(vistos.values());
}
"""

READ_MESSAGES_JS = """
([limit, nomeProprio]) => {
  const main = document.querySelector('#main') || document.querySelector('main') || document.body;
""" + EH_NOSSA_JS + """
  // Candidatos: o data-id existe em toda mensagem, com ou sem prefixo.
  // As classes .message-in/.message-out NAO existem nesta instalacao, entao
  // elas so' entram como ultimo recurso, para instalacoes antigas.
  let achados = Array.from(main.querySelectorAll('[data-id]'));
  if (!achados.length) achados = Array.from(main.querySelectorAll('div.message-in'));
  if (!achados.length) {
    achados = Array.from(main.querySelectorAll('div[role="row"]')).filter(
      (r) => r.querySelector('[data-pre-plain-text], span.selectable-text'));
  }

  // Desduplica: o mesmo data-id costuma aparecer na linha externa E num filho.
  const porId = new Map();
  for (const el of achados) {
    const holder = el.matches('[data-id]')
      ? el
      : (el.closest('[data-id]') || el.querySelector('[data-id]'));
    if (!holder) continue;
    const id = holder.getAttribute('data-id') || '';
    if (!id) continue;
    const linha = holder.closest('div[role="row"]') || holder;
    if (ehNossa(linha, id, nomeProprio)) continue;
    if (!porId.has(id)) porId.set(id, holder);
  }

  return Array.from(porId.keys()).slice(-limit).map((id) => {
    const el = porId.get(id);
    const copyable = el.querySelector('[data-pre-plain-text]')
                  || (el.matches('[data-pre-plain-text]') ? el : null);
    const meta = copyable ? copyable.getAttribute('data-pre-plain-text') || '' : '';

    // O corpo, nao o rotulo do autor: 'span.selectable-text' e' exclusivo do
    // texto da mensagem, e um querySelector com lista devolveria o primeiro
    // em ordem de documento -- que e' o nome de quem escreveu.
    let text = '';
    const corpo = (copyable || el).querySelector('span.selectable-text');
    if (corpo) text = corpo.innerText || '';
    if (!text) {
      const alternativo = el.querySelector('span.selectable-text');
      if (alternativo) text = alternativo.innerText || '';
    }
    if (!text && copyable) text = copyable.innerText || '';
    if (!text) text = el.innerText || '';
    return { id, meta, text };
  });
}
"""

# Descreve o que existe para citar: icones da bolha e itens de menu abertos.
# A citacao falhou nas duas vias sem dizer POR QUE; isto troca adivinhacao de
# seletor por evidencia da tela real do operador.
MARCA_MENSAGEM = "data-allana-msg"

# Acha a mensagem comparando o data-id como STRING e marca o elemento.
#
# Por que nao um seletor [data-id="..."]: o id do WhatsApp carrega '@', '.',
# '-' e '=' e depende de escape correto para virar CSS valido. Quando o
# seletor falha, o resultado e' um count()==0 indistinguivel de "a mensagem
# saiu da tela" -- e foi isso que impediu a citacao de funcionar, sem
# sintoma nenhum. Comparar string em JS nao tem esse problema.
ACHAR_MENSAGEM_JS = r"""
(dataId) => {
  document.querySelectorAll('[data-allana-msg]').forEach(
    (el) => el.removeAttribute('data-allana-msg'));

  for (const el of document.querySelectorAll('[data-id]')) {
    if (el.getAttribute('data-id') !== dataId) continue;

    // Marcar a LINHA da mensagem, nao o elemento que carrega o data-id.
    //
    // Nesta versao do WhatsApp o data-id vem "pelado" (ex.: 2A729AF702...)
    // e costuma estar num elemento interno. O menu de contexto responde ao
    // hover e ao botao direito na LINHA -- mirar no elemento interno fazia
    // as tres vias de citacao falharem sem explicacao.
    const linha = el.closest('div[role="row"]')
               || el.closest('div.message-in')
               || el.closest('div.message-out')
               || el;
    linha.setAttribute('data-allana-msg', '1');
    return true;
  }
  return false;
}
"""

# Cola a imagem no compositor, como se o operador tivesse dado Ctrl+V.
#
# Por que colar em vez de usar o menu de anexo: o menu e' o passo mais fragil
# do fluxo -- depende do clipe abrir, do rotulo "Fotos e videos" existir e de
# escolher o <input> certo entre varios. O diagnostico do operador mostrou o
# estrago: menu_abriu=False e o anexo caindo no input de DOCUMENTO da tela
# inicial. Colar um File de tipo image/png o WhatsApp SEMPRE trata como
# imagem, e preserva a citacao que ja' estiver ativa.
# O campo de legenda do preview da imagem.
#
# Identificado por CARACTERISTICA, nunca por posicao. O codigo anterior fazia
# `.last` sobre a lista de contenteditable e pegava sempre o compositor da
# conversa, que fica atras do preview: digitava no campo errado, o texto nao
# chegava, e o card saia mudo -- 40 vezes em 52 imagens, desde 30/08.
#
# O que o laboratorio observou com o preview ABERTO (dois campos na tela):
#
#   [0] aria="Digite uma mensagem"                    <- A LEGENDA
#       sem data-tab, FORA do footer, FORA do #main, com foco
#   [1] aria="Digite uma mensagem para o grupo <nome>"  <- o compositor
#       data-tab="10", dentro do footer, dentro do #main
#
# A diferenca entre os dois aria-label e' o SUFIXO. Por isso a comparacao e'
# de igualdade com o texto base, e nao `includes` -- que casaria com os dois.
CAMPO_DA_LEGENDA_JS = r"""
(rotulosBase) => {
  const limpar = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const visivel = (el) => {
    const r = el.getBoundingClientRect();
    return !!(r.width && r.height) && el.offsetParent !== null;
  };
  const base = rotulosBase.map(limpar);

  const todos = [...document.querySelectorAll('[contenteditable="true"]')];
  const candidatos = [];

  todos.forEach((el, i) => {
    const aria = limpar(el.getAttribute('aria-label'));
    // `data-tab` NAO e' ausente na legenda: o WhatsApp poe a string
    // "undefined" nela. O compositor da conversa tem um NUMERO ("10"). A
    // primeira versao desta regra exigia `=== null` e recusava os dois --
    // o laboratorio pegou isso antes de ir para producao, onde teria
    // abortado todo envio de imagem.
    const tab = el.getAttribute('data-tab');
    const tabDeCompositor = tab !== null && /^\d+$/.test(tab.trim());

    const ehLegenda =
         base.includes(aria)                       // igualdade, sem sufixo
      && !tabDeCompositor                          // o compositor tem data-tab numerico
      && !el.closest('footer')                     // o compositor esta no footer
      && !el.closest('#main')                      // e dentro de #main
      && visivel(el);
    if (ehLegenda) candidatos.push({ indice: i, aria: el.getAttribute('aria-label') });
  });

  // O inventario completo vai junto SEMPRE: quando a escolha falha, e' ele
  // que diz por que, em vez de deixar mais um "nao consegui" sem explicacao.
  const inventario = todos.map((el, i) => ({
    indice: i,
    aria: el.getAttribute('aria-label'),
    dataTab: el.getAttribute('data-tab'),
    noFooter: !!el.closest('footer'),
    noMain: !!el.closest('#main'),
    visivel: visivel(el),
    temFoco: el === document.activeElement,
  }));

  if (candidatos.length !== 1) {
    return { achou: false, quantos: candidatos.length, inventario };
  }
  return { achou: true, indice: candidatos[0].indice,
           aria: candidatos[0].aria, inventario };
}
"""

# Leitura do que ficou no campo, para PROVAR que a legenda entrou.
TEXTO_DO_CAMPO_JS = r"""
(indice) => {
  const el = document.querySelectorAll('[contenteditable="true"]')[indice];
  return el ? (el.innerText || '') : '';
}
"""

# Foca o campo SEM ponteiro, e diz se conseguiu.
#
# O clique no campo da legenda estourou 8s em producao (REQ000008) com o campo
# visivel na tela: no preview da imagem ha' camada por cima, e o Playwright
# recusa clicar onde o evento nao chega. Focar por JS nao depende de ponto
# nenhum. O retorno e' a prova -- sem ele, `insert_text` escreveria no que
# estivesse com foco, que pode ser o compositor da conversa.
FOCAR_CAMPO_JS = r"""
(indice) => {
  const el = document.querySelectorAll('[contenteditable="true"]')[indice];
  if (!el) return false;
  el.focus();
  try {
    // Cursor no fim: com o cursor no inicio, a legenda entraria antes do que
    // ja' estivesse escrito.
    const selecao = window.getSelection();
    const faixa = document.createRange();
    faixa.selectNodeContents(el);
    faixa.collapse(false);
    selecao.removeAllRanges();
    selecao.addRange(faixa);
  } catch (e) { /* sem selecao: o foco sozinho ja' serve */ }
  return document.activeElement === el;
}
"""


COLAR_IMAGEM_JS = r"""
([b64, nome]) => {
  try {
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    const file = new File([arr], nome, { type: 'image/png' });
    const dt = new DataTransfer();
    dt.items.add(file);

    // O compositor da conversa e' o contenteditable dentro do <footer>.
    const alvos = [...document.querySelectorAll('[contenteditable="true"]')]
      .filter((e) => e.closest('footer'));
    const el = alvos[alvos.length - 1]
            || document.querySelector('[contenteditable="true"]');
    if (!el) return { ok: false, motivo: 'compositor não encontrado' };

    el.focus();
    el.dispatchEvent(new ClipboardEvent('paste', {
      clipboardData: dt, bubbles: true, cancelable: true,
    }));
    return { ok: true };
  } catch (e) {
    return { ok: false, motivo: String(e).slice(0, 120) };
  }
}
"""

# O preview aberto e' imagem ou documento?
#
# Esta e' a verificacao que impede o defeito de voltar: enviar sem conferir
# foi o que fez o resultado chegar como "REQ000029.png · 57 KB" para baixar.
PREVIEW_E_IMAGEM_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').toLowerCase();
  const miniatura = document.querySelector(
    'img[src^="blob:"], img[src^="data:"], canvas');
  const texto = norm(document.body.innerText);
  // "57 KB" / "1,2 MB" sem miniatura = card de documento.
  const pesoVisivel = /\d+([.,]\d+)?\s*(kb|mb)\b/.test(texto);
  return {
    temMiniatura: !!miniatura,
    pareceDocumento: pesoVisivel && !miniatura,
  };
}
"""

# A ultima mensagem QUE NOS ENVIAMOS: saiu mesmo? virou imagem? citou?
ULTIMA_SAIDA_JS = r"""
() => {
  const linhas = [...document.querySelectorAll('[role="row"]')];
  const norm = (s) => (s || '').replace(/\s+/g, ' ').toLowerCase();
  for (let i = linhas.length - 1; i >= 0; i--) {
    const r = linhas[i];
    // "E' nossa?" em cascata, do sinal mais confiavel ao mais fragil.
    // Nenhum dos dois primeiros depende de classe CSS -- e' o que impede
    // esta verificacao de falhar SEMPRE numa instalacao que nao use
    // message-in/message-out.
    const idEl = r.querySelector('[data-id]');
    const id = idEl ? (idEl.getAttribute('data-id') || '') : '';
    const nossa =
         id.startsWith('true_')                                        // 1
      || !!r.querySelector('[data-icon^="msg-"], [data-icon^="status-"]')  // 2
      || !!r.querySelector('div.message-out');                         // 3
    if (!nossa) continue;
    const texto = r.innerText || '';
    return {
      texto: texto.slice(0, 200),
      temImagem: !!r.querySelector('img[src^="blob:"], img[src^="data:"]'),
      pareceDocumento: /\d+([.,]\d+)?\s*(kb|mb)\b/.test(norm(texto)),
      icones: [...r.querySelectorAll('[data-icon]')].map(
        (e) => e.getAttribute('data-icon')),
      citado: r.querySelector('blockquote, [aria-label]')?.innerText?.slice(0, 100) || null,
    };
  }
  return null;
}
"""

# A barra de citacao apareceu acima do compositor?
#
# Clicar em "Responder" nao prova que a citacao pegou. Sem esta leitura, o
# bot digitava por cima de um estado que nao existia e a resposta saia solta
# -- o defeito A, que o consultor via como "nao marcou a mensagem".
# A barra de citacao esta' armada, e e' da mensagem certa?
#
# Olha em DOIS lugares, e a razao vem do defeito da legenda: com a
# pre-visualizacao da imagem aberta, o compositor ativo NAO esta' no
# `<footer>` -- ele fica num container proprio, fora dele. Uma verificacao
# que so' olhasse o rodape diria "a citacao caiu" toda vez que o preview
# estivesse na tela, e o codigo descartaria uma citacao boa.
#
# Devolve ONDE encontrou, para o diagnostico distinguir "caiu" de "mudou de
# lugar".
# A barra de citacao esta' armada, e e' a da mensagem CERTA?
#
# O DEFEITO QUE ESTE BLOCO CONSERTA
# ---------------------------------
# A versao anterior comparava os 18 primeiros caracteres do CORPO INTEIRO da
# mensagem com o texto da barra. A barra nao mostra o corpo inteiro: mostra o
# autor e a PRIMEIRA LINHA, e encerra em reticencias.
#
# `diagnostico/barra_citacao.png`, com a barra armada de verdade:
#
#     Ryan
#     LUCIANGELA TESTADO
#     ...
#
# O corpo era "LUCIANGELA TESTADO / 72845554753 / AMAPA". Normalizado sem
# espacos, "luciangelatestado" tem 17 caracteres -- um a menos que os 18
# comparados. O 18o caractere procurado era o primeiro digito do CPF, que a
# barra nunca mostra. A conferencia reprovava por UM caractere.
#
# O formato do pedido e' sempre NOME / CPF / ESTADO, entao isso acontecia
# em qualquer nome curto. Rodando a conta sobre os 116 pedidos multi-linha
# gravados no banco: **69 reprovariam**. E o log de 01/09 as 12:20 mostra o
# desfecho -- a barra ESTAVA la', com autor e previa, e foi descartada:
#
#     INFO     Rodape sem a citacao esperada: 'Allana testando 2.274'
#     WARNING  Cliquei em 'responder' mas a barra de citacao nao apareceu
#
# Ou seja: a citacao funcionava. Quem a jogava fora era esta conferencia.
#
# O teste que deixou passar tinha uma barra de mentira, com o corpo inteiro
# dentro dela ("Ryan LUCIANGELA TESTADO 72845554753"). A fixture inventada
# confirmava o codigo em vez de confrontar a tela.
#
# COMO ELA JULGA AGORA
# --------------------
# Pelo caminho inverso: pega o que a barra MOSTRA e confere se aquilo e' o
# COMECO da mensagem. Assim ninguem precisa adivinhar onde o WhatsApp corta.
# Continua recusando a barra de outra mensagem -- que e' o perigo real: o
# consultor leria o resultado de outro cliente como se fosse o dele.
CITACAO_ATIVA_JS = r"""
(esperado) => {
  // A barra de citacao DO COMPOSITOR -- nao uma citacao do historico.
  //
  // `[data-testid="quoted-message"]` tambem existe em MENSAGENS da conversa
  // que citam outras. Pegar a primeira do documento lia a citacao de uma
  // mensagem antiga do grupo e a tratava como se fosse a barra armada.
  //
  // O que separa as duas: a barra do compositor NAO fica dentro de uma linha
  // de mensagem (`div[role="row"]`).
  const barraDoCompositor = () => {
    for (const el of document.querySelectorAll('[data-testid="quoted-message"]')) {
      if (el.closest('div[role="row"]')) continue;   // citacao do historico
      const r = el.getBoundingClientRect();
      if (r.width && r.height) return el;
    }
    return null;
  };
  const norm = (s) => (s || '').normalize('NFC').replace(/\s+/g, '').toLowerCase();
  const limpar = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const marcas = esperado || {};
  const autor = norm(marcas.autor);
  const corpo = norm(marcas.corpo || marcas.primeiraLinha);

  const barra = barraDoCompositor();
  if (!barra) {
    const rodape = document.querySelector('footer');
    return { ativa: false, local: '', temBarra: false, previa: '',
             rodape: rodape ? limpar(rodape.innerText).slice(0, 120) : null };
  }

  const dentro = norm(barra.innerText);
  const visto = limpar(barra.innerText).slice(0, 120);

  // A COMPARACAO E' AO CONTRARIO DA ANTERIOR, e e' por isso que ela funciona.
  //
  // Nao se pergunta "quanto da mensagem a barra deveria mostrar?" -- essa
  // pergunta nao tem resposta estavel, e chuta-la em 18 caracteres foi o
  // defeito. Pergunta-se: "o que a barra mostra e' o COMECO desta mensagem?"
  //
  // A previa e' sempre um prefixo do corpo, entao a conta e exata para nome
  // curto e para nome longo, com uma linha ou com tres, sem depender de onde
  // o WhatsApp corta.
  let previa = dentro;
  if (autor && previa.startsWith(autor)) previa = previa.slice(autor.length);
  previa = previa.replace(/[.\u2026]+$/, '');       // as reticencias do fim

  let ativa = false;
  let porque = '';
  if (previa.length >= 4 && corpo) {
    // Com o autor no comeco (o normal) e sem ele (caso a barra mude de
    // ordem): as duas leituras valem, e nenhuma delas aceita outra mensagem.
    ativa = corpo.startsWith(previa) || (autor + corpo).startsWith(dentro.replace(/[.\u2026]+$/, ''));
    porque = ativa ? 'a previa e o comeco da mensagem'
                   : 'a previa nao e o comeco desta mensagem';
  } else if (autor.length >= 3) {
    // Previa curta demais para decidir ("ok", "sim"): o autor responde.
    ativa = dentro.includes(autor);
    porque = ativa ? 'autor' : 'a previa nao traz o autor';
  } else {
    porque = 'nao sei o que esperar desta mensagem';
  }

  return { ativa, local: 'barra', temBarra: true, rodape: visto,
           previa, autor, porque };
}
"""

# Ha' uma citacao pendurada no compositor? Devolve onde clicar para cancelar.
#
# Citacao orfa e' perigosa: a resposta SEGUINTE sairia grudada na mensagem
# errada. Aconteceu no laboratorio -- a barra da primeira volta sobreviveu a
# `_limpar_ui` e as voltas seguintes leram "citacao ativa" sem terem citado
# nada.
CITACAO_PENDENTE_JS = r"""
() => {
  // A barra de citacao DO COMPOSITOR -- nao uma citacao do historico.
  //
  // `[data-testid="quoted-message"]` tambem existe em MENSAGENS da conversa
  // que citam outras. Pegar a primeira do documento lia a citacao de uma
  // mensagem antiga do grupo e a tratava como se fosse a barra armada: o log
  // dizia "havia uma citacao pendurada" apontando uma mensagem de dias atras,
  // e nao havia botao de cancelar porque nao havia barra nenhuma.
  //
  // O que separa as duas: a barra do compositor NAO fica dentro de uma linha
  // de mensagem (`div[role="row"]`).
  const barraDoCompositor = () => {
    for (const el of document.querySelectorAll('[data-testid="quoted-message"]')) {
      if (el.closest('div[role="row"]')) continue;   // citacao do historico
      const r = el.getBoundingClientRect();
      if (r.width && r.height) return el;
    }
    return null;
  };
  const barra = barraDoCompositor();
  if (!barra) return { pendente: false };

  // Procurar o botao PERTO DA BARRA, e nao dentro do <footer>.
  //
  // Com a pre-visualizacao da imagem aberta, a barra de citacao sai do
  // footer e vai para o container do preview -- e a busca restrita ao footer
  // dizia "nao tem botao de cancelar" com o botao na tela. Mesma licao do
  // campo de legenda: com o preview aberto, quase nada esta' onde estava.
  const rotulos = ['cancelar', 'cancel', 'fechar', 'close'];
  const norm = (s) => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
                               .trim().toLowerCase();

  let cancelar = null;
  let raiz = barra;
  for (let i = 0; i < 5 && raiz && !cancelar; i++) {
    raiz = raiz.parentElement;
    if (!raiz) break;
    for (const b of raiz.querySelectorAll('button, [role="button"], [aria-label]')) {
      if (!rotulos.includes(norm(b.getAttribute('aria-label')))) continue;
      const rb = b.getBoundingClientRect();
      if (rb.width && rb.height) { cancelar = b; break; }
    }
  }

  if (!cancelar) return { pendente: true, temBotao: false,
                          rotulosPerto: [...(barra.parentElement
                            ? barra.parentElement.querySelectorAll('[aria-label]') : [])]
                            .map((e) => e.getAttribute('aria-label')).slice(0, 8),
                          texto: (barra.innerText || '').trim().slice(0, 60) };

  const r = cancelar.getBoundingClientRect();
  return { pendente: true, temBotao: true,
           x: r.x + r.width / 2, y: r.y + r.height / 2,
           texto: (barra.innerText || '').trim().slice(0, 60) };
}
"""

# Ja' existe no chat uma mensagem NOSSA com este identificador?
#
# Trava contra reenvio duplicado. Se a verificacao de entrega falhar por
# qualquer motivo -- seletor errado, DOM novo -- o reenvio mandaria a mesma
# resposta de novo, ate' cinco vezes. Cinco copias no grupo do cliente e' pior
# que uma entrega nao confirmada. Aqui a pergunta e' simples e nao depende de
# classe CSS: o REQ ja' esta' escrito em alguma mensagem nossa?
JA_ENVIADO_JS = r"""
(marca) => {
  if (!marca) return false;
  const alvo = marca.toLowerCase();
  const linhas = [...document.querySelectorAll('[role="row"]')];
  for (const r of linhas) {
    const idEl = r.querySelector('[data-id]');
    const id = idEl ? (idEl.getAttribute('data-id') || '') : '';
    const nossa =
         id.startsWith('true_')
      || !!r.querySelector('[data-icon^="msg-"], [data-icon^="status-"]')
      || !!r.querySelector('div.message-out');
    if (!nossa) continue;
    if ((r.innerText || '').toLowerCase().includes(alvo)) return true;
  }
  return false;
}
"""

# Ha' algo aberto que o Escape fecharia?
#
# No WhatsApp Web, Escape SEM nada aberto FECHA A CONVERSA e volta para a
# lista. Foi o que aconteceu em producao: a citacao falhava, a limpeza
# pressionava Escape tres vezes, a conversa fechava, e o anexo seguinte via
# so' a tela inicial -- por isso o unico input disponivel era o de documento
# (accept="*") e a imagem nunca saia como foto.
#
# Este trecho responde se ha' de fato o que fechar.
TEM_O_QUE_FECHAR_JS = r"""
() => {
  const visivel = (e) => e && (e.offsetParent !== null || e.getClientRects().length > 0);
  const alvos = document.querySelectorAll(
    '[role="menu"], [role="dialog"], [role="application"] ul, ' +
    '[data-animate-modal-body], .overlay, [data-testid="media-preview"]');
  for (const el of alvos) if (visivel(el)) return true;
  // A tela de preview de midia: miniatura grande fora da lista de mensagens.
  const preview = document.querySelector('img[src^="blob:"]');
  if (visivel(preview) && !preview.closest('[role="row"]')) return true;
  return false;
}
"""

MARCA_ITEM = "data-allana-item"

# Acha um item de menu pelo TEXTO EXATO e marca para o Playwright clicar.
#
# Substitui os seletores CSS chutados (`li:has-text(...)`, `[aria-label=...]`)
# que nunca casaram com o menu real. Comparar texto em JS nao depende de
# classe, de aria-label nem de estrutura -- e recusa explicitamente
# "Encaminhar", que fica ao lado de "Responder" no mesmo menu e ja' fez o bot
# mandar dados de cliente para a conversa errada.
ACHAR_ITEM_MENU_JS = r"""
(alvos) => {
  document.querySelectorAll('[data-allana-item]').forEach(
    (el) => el.removeAttribute('data-allana-item'));

  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const querido = alvos.map(norm);

  // Menu real desta instalacao, nesta ordem:
  //   Dados da mensagem | Responder | Copiar | Reagir | Encaminhar |
  //   Fixar | Pergunte à Meta AI | Favoritar | Apagar
  //
  // "Encaminhar" e "Apagar" estao no MESMO menu que "Responder". Match frouxo
  // aqui e' desastre: encaminhar manda os dados do cliente para outra
  // conversa. Por isso a comparacao e' de IGUALDADE, nunca `includes`.
  const proibido = /^(encaminhar|forward|apagar|delete|excluir|denunciar|report|reagir|react|fixar|favoritar|dados da mensagem|message info)$/;

  let alvo = null;
  for (const el of document.querySelectorAll('div, li, span, button, a')) {
    const texto = norm(el.innerText);
    if (!texto || texto.length > 40) continue;
    if (proibido.test(texto)) continue;
    if (!querido.includes(texto)) continue;   // igualdade, nao substring
    if (!alvo || alvo.contains(el)) alvo = el;  // o mais profundo
  }
  if (!alvo) return "";

  // Recusa final: mesmo tendo casado, nao clicar em nada perigoso.
  const escolhido = norm(alvo.innerText);
  if (proibido.test(escolhido)) return "";

  let clicavel = alvo;
  for (let i = 0; i < 3; i++) {
    const pai = clicavel.parentElement;
    if (!pai) break;
    if (norm(pai.innerText) !== escolhido) break;
    clicavel = pai;
  }
  clicavel.setAttribute('data-allana-item', '1');
  return escolhido;
}
"""

# Descreve o menu de anexo quando nao achamos "Fotos e videos".
# Mesma logica do diagnostico da citacao: em vez de eu chutar o rotulo, o bot
# informa o que esta' escrito na tela do operador.
DIAGNOSTICO_ANEXO_JS = r"""
() => {
  const limpar = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const inputs = Array.from(document.querySelectorAll('input[type="file"]'))
    .map((el) => el.getAttribute('accept') || '(sem accept)');
  // .map(limpar) passaria o ELEMENTO, nao o texto: map chama a funcao com
  // (item, indice, array). Precisa da arrow explicita.
  const itens = Array.from(document.querySelectorAll(
      'li, [role="menuitem"], [role="button"], button'))
    .map((el) => limpar(el.innerText))
    .filter((s) => s && s.length < 30)
    .slice(0, 20);
  const icones = Array.from(document.querySelectorAll('[data-icon]'))
    .map((el) => el.getAttribute('data-icon')).slice(0, 20);
  return { inputs, itens, icones };
}
"""

# ============================================================================
#  CITACAO — imitar o que uma pessoa faz na mao
# ============================================================================
#
# O caminho manual, que este codigo copia passo a passo:
#
#   1. passar o mouse na mensagem do consultor
#   2. aparece uma setinha no canto da bolha -> clicar nela
#   3. abre um menu -> clicar em "Responder" (o PRIMEIRO item)
#   4. aparece a barra de citacao acima do campo de digitar
#   5. digitar / colar a imagem
#   6. Enter
#
# Duas coisas quebraram todas as tentativas anteriores, e as duas estao
# tratadas aqui de proposito:
#
# * **O botao so' existe com o mouse em cima, e demora a aparecer.** Ele e'
#   montado no `mouseenter`. Procurar no mesmo instante do hover encontra a
#   bolha sem botao nenhum -- e o diagnostico registrava exatamente isso
#   (`icones=['tail-in'] rotulos=[]`), que eu li como "o menu nao existe".
#
# * **O mouse nao pode sair de cima da bolha entre o hover e o clique.** Por
#   isso tudo aqui e' `page.mouse.move` / `page.mouse.click` em coordenadas,
#   e nao `locator.hover()` seguido de `locator.click()`: o segundo faz o
#   Playwright reposicionar o ponteiro, e a setinha some no caminho.

MARCA_LINHA = "data-allana-linha"
#: Marca a setinha achada, para o clique mirar no ELEMENTO.
MARCA_SETINHA = "data-allana-setinha"
#: Marca o BALAO da mensagem, para o botao direito mirar no elemento.
MARCA_BALAO = "data-allana-balao"
#: Marca o item "Responder" do menu aberto, para o clique mirar NELE.
MARCA_RESPONDER = "data-allana-responder"

# ---------------------------------------------------------------- PASSO 1
# Acha a linha da mensagem, rola ela para o centro e devolve a geometria.
#
# Marcar a LINHA (`div[role="row"]`), nao o elemento que carrega o data-id: o
# menu de contexto responde ao hover na linha. Nesta instalacao o data-id vem
# pelado (2A729AF70...) num elemento interno, e mirar nele fazia todas as vias
# falharem sem explicacao.
GEOMETRIA_DA_LINHA_JS = r"""
([dataId, textoAlvo]) => {
  const norm = (s) => (s || '').normalize('NFC').replace(/\s+/g, '').toLowerCase();
  document.querySelectorAll('[data-allana-linha]').forEach(
    (el) => el.removeAttribute('data-allana-linha'));

  const daLinha = (el) => el.closest('div[role="row"]')
                       || el.closest('div.message-in')
                       || el.closest('div.message-out')
                       || el;

  let linha = null;
  let via = '';

  if (dataId) {
    for (const el of document.querySelectorAll('[data-id]')) {
      if (el.getAttribute('data-id') === dataId) { linha = daLinha(el); via = 'data-id'; break; }
    }
  }

  // NAO ha' reserva por texto -- e essa ausencia e' a correcao.
  //
  // Procurar a mensagem pelo texto encontra QUALQUER uma com aquele texto, e
  // neste grupo o mesmo cliente se repete o tempo todo (no banco de 20/09,
  // sete pares de solicitacoes com o mesmo nome). Citar por aproximacao e'
  // como a resposta de um pedido acaba pendurada na mensagem de outro. Sem o
  // id na tela, nao se cita: a resposta sai sem citacao, com o nome de quem
  // pediu, e o log registra o motivo.
  if (!linha) {
    return { achou: false, via: '', motivo: 'a mensagem original nao esta na tela',
             dataId: '' };
  }

  // Quantas linhas VISIVEIS tem exatamente este texto (contando esta).
  //
  // Com mais de uma, a conferencia da barra de citacao -- que compara texto
  // -- nao consegue distinguir qual delas o WhatsApp citou. Isso nao impede
  // de citar: impede de AFIRMAR que citou (vira `unverified`, nunca `ok`).
  const corpoDoAlvo = norm(linha.innerText);
  let iguais = 0;
  if (corpoDoAlvo.length >= 8) {
    for (const outra of document.querySelectorAll('div[role="row"]')) {
      const r = outra.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      if (norm(outra.innerText) === corpoDoAlvo) iguais += 1;
    }
  }

  linha.setAttribute('data-allana-linha', '1');
  // Marcar tambem o BALAO: e' nele que o botao direito precisa cair, e
  // clicar no elemento dispensa coordenada. Com o grupo despejando dezenas
  // de mensagens, qualquer coordenada envelhece em milissegundos.
  document.querySelectorAll('[data-allana-balao]').forEach(
    (el) => el.removeAttribute('data-allana-balao'));
  linha.scrollIntoView({ block: 'center' });

  // Mirar no BALAO, nunca no centro da linha.
  //
  // `div[role="row"]` ocupa a LARGURA TODA da conversa. Numa mensagem
  // recebida o balao fica a esquerda, entao o centro da linha cai no fundo
  // vazio -- e foi exatamente isso que quebrou tudo: o hover no vazio nunca
  // revelava a setinha, e o clique com botao direito no vazio abria o menu
  // do GRUPO ("Adicionar membro", "Dados do grupo", "Fechar conversa") em
  // vez do menu da mensagem. O log dizia "menu aberto: True" e o menu era
  // outro.
  const balao = linha.querySelector('[data-pre-plain-text]')
             || linha.querySelector('.copyable-text')
             || linha.querySelector('span.selectable-text')
             || linha.querySelector('[data-id]')
             || linha;

  balao.setAttribute('data-allana-balao', '1');

  const r = balao.getBoundingClientRect();
  if (!r.width || !r.height) return { achou: false, via, motivo: 'o balao esta invisivel' };

  const linhaR = linha.getBoundingClientRect();
  return {
    achou: true, via,
    x: r.x + r.width / 2,
    y: r.y + r.height / 2,
    // Guardado so' para diagnostico: e' a diferenca entre os dois que
    // explica o defeito acima.
    centroDaLinha: { x: linhaR.x + linhaR.width / 2, y: linhaR.y + linhaR.height / 2 },
    texto: (linha.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60),
    // O id que foi REALMENTE marcado, devolvido para quem pediu conferir.
    dataId: (linha.querySelector('[data-id]') || linha).getAttribute('data-id') || dataId,
    iguais: iguais,
  };
}
"""

# ---------------------------------------------------------------- PASSO 3
# A setinha que abre o menu, DENTRO da linha marcada.
#
# O rotulo real desta instalacao e' "Abrir opcoes de mensagem" -- veio do
# dump_dom.py, nao de palpite. Os outros da lista sao as variantes que o
# WhatsApp usa em ingles e em versoes vizinhas; nenhum deles e' chute sobre
# classe CSS, sao todos aria-label.
#
# O que NAO pode entrar aqui: `[aria-haspopup]` generico. O botao de
# ENCAMINHAR tambem casa com ele, e clicar nele abre "Selecionar conversas" --
# foi assim que um PNG com nome e CPF de cliente foi parar na conversa errada.
BOTAO_DE_OPCOES_JS = r"""
(idEsperado) => {
  const linha = document.querySelector('[data-allana-linha]');
  if (!linha) return { achou: false, motivo: 'a linha marcada sumiu' };

  // A conversa e' viva: mensagem nova chegando rola a lista, e a coordenada
  // colhida ha' meio segundo passa a apontar para outro balao. Ja' aconteceu
  // -- o menu abriu na mensagem certa e a barra de citacao apareceu citando
  // OUTRA. Conferir aqui e' mais barato que descobrir no PASSO 5.
  if (idEsperado) {
    const dono = linha.querySelector('[data-id]');
    const id = dono ? dono.getAttribute('data-id') : '';
    if (id && id !== idEsperado) {
      return { achou: false, motivo: 'a linha marcada virou outra mensagem',
               idNaTela: id };
    }
  }

  // O rotulo REAL desta instalacao, colhido pelo laboratorio:
  //     "Menu de contexto para a mensagem de Ryan"
  //
  // Ele carrega o NOME de quem mandou, entao muda a cada mensagem -- por
  // isso a comparacao e' por PREFIXO, e nao por igualdade. A lista anterior
  // tinha "abrir opcoes de mensagem", que era palpite meu e nunca casou:
  // o codigo caía sempre no botao direito.
  const prefixos = ['menu de contexto', 'context menu',
                    'abrir opcoes de mensagem', 'open message options'];
  const norm = (s) => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
                               .replace(/\s+/g, ' ').trim().toLowerCase();

  let alvo = null;
  for (const el of linha.querySelectorAll('[aria-label], [data-icon]')) {
    const rotulo = norm(el.getAttribute('aria-label'));
    const icone = (el.getAttribute('data-icon') || '').toLowerCase();
    if ((rotulo && prefixos.some((p) => rotulo.startsWith(p)))
        || icone === 'down-context' || icone === 'chevron') {
      alvo = el; break;
    }
  }

  // O que EXISTE na linha, para o log quando nao achamos. Sem isto a falha
  // vira "nao achei" e ninguem sabe o que tinha na tela.
  const visto = [...linha.querySelectorAll('[aria-label]')]
    .map((el) => el.getAttribute('aria-label')).filter(Boolean).slice(0, 12);
  const icones = [...linha.querySelectorAll('[data-icon]')]
    .map((el) => el.getAttribute('data-icon')).slice(0, 12);

  if (!alvo) return { achou: false, motivo: 'a setinha nao apareceu', visto, icones };

  const r = alvo.getBoundingClientRect();
  if (!r.width || !r.height) {
    return { achou: false, motivo: 'a setinha existe mas esta invisivel', visto, icones };
  }

  // MARCAR o elemento, para o clique mirar NELE e nao numa coordenada.
  //
  // Clicar em (x, y) falhava sempre em producao: o WhatsApp ANIMA a entrada
  // da setinha, e entre medir o retangulo e disparar o clique ela ja' se
  // moveu. A sondagem provou -- `elementFromPoint` no ponto medido devolvia
  // divs anonimos, sem aria-label nenhum. O clique caia no fundo do balao,
  // o menu nao abria, e o log dizia "cliquei na setinha".
  document.querySelectorAll('[data-allana-setinha]').forEach(
    (el) => el.removeAttribute('data-allana-setinha'));
  alvo.setAttribute('data-allana-setinha', '1');

  return {
    achou: true, x: r.x + r.width / 2, y: r.y + r.height / 2,
    rotulo: alvo.getAttribute('aria-label') || alvo.getAttribute('data-icon'),
  };
}
"""

# ---------------------------------------------------------------- PASSO 4
# "Responder" -- o PRIMEIRO item do menu.
#
# Menu real de uma mensagem RECEBIDA, nesta ordem:
#   Responder | Responder em particular | Conversar com <nome> | Copiar |
#   Reagir | Encaminhar | Fixar | Pergunte a Meta AI | Favoritar |
#   Denunciar | Apagar
#
# A comparacao e' de IGUALDADE do texto normalizado. `includes` seria um
# desastre em dois niveis:
#
#   * "responder em particular" COMECA com "responder" -- abriria conversa
#     privada com o consultor, fora do grupo;
#   * "encaminhar" e "apagar" estao no mesmo menu.
#
# Por isso ha' tambem uma lista de proibidos: mesmo que a igualdade case por
# algum motivo inesperado, nada perigoso e' clicado.
ITEM_RESPONDER_JS = r"""
(alvos) => {
  const norm = (s) => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
                               .replace(/\s+/g, ' ').trim().toLowerCase();
  const querido = alvos.map(norm);
  const proibido = /^(responder em particular|reply privately|conversar com .*|encaminhar|forward|apagar|delete|excluir|denunciar|report|reagir|react|fixar|pin|favoritar|star|copiar|copy|pergunte a meta ai|dados da mensagem|message info)$/;

  // Procurar DENTRO do menu aberto, nao no documento inteiro.
  //
  // Sem este escopo a varredura pegava a lista de conversas da lateral: o
  // log de itens vinha com "+55 62 9000-1003 ontem foto" e nomes de grupo,
  // e nenhum deles era item de menu nenhum.
  const lateral = document.querySelector('#pane-side');
  const menus = [...document.querySelectorAll('[role="menu"], [role="menubar"]')]
    .filter((m) => !(lateral && lateral.contains(m)))
    // Mesmo piso do MENU_ABERTO_JS: o seletor de reacoes tambem e'
    // role="menu" e tem 4x1 pixel.
    .filter((m) => { const r = m.getBoundingClientRect();
                     return r.width >= 120 && r.height >= 60; });

  const raizes = menus.length ? menus : [];
  if (!raizes.length) return { achou: false, motivo: 'nenhum menu aberto', rotulos: [] };

  const candidatos = raizes.flatMap((m) => [...m.querySelectorAll(
      '[role="menuitem"], li, div[tabindex], [role="button"], div')]);

  // Tudo que o menu mostra, para o log. E' esta lista que responde
  // "o menu abriu e o item mudou de nome?" sem ninguem precisar adivinhar.
  const rotulos = candidatos
    .map((el) => norm(el.innerText))
    .filter((s) => s && s.length < 40);

  let alvo = null;
  for (const el of candidatos) {
    const texto = norm(el.innerText);
    if (!texto || texto.length > 40) continue;
    if (proibido.test(texto)) continue;
    if (!querido.includes(texto)) continue;     // IGUALDADE, nunca includes
    // O mais profundo que ainda tenha exatamente este texto: clicar no pai
    // pode cair fora do item.
    if (!alvo || alvo.contains(el)) alvo = el;
  }

  if (!alvo) return { achou: false, rotulos: [...new Set(rotulos)].slice(0, 20) };

  const escolhido = norm(alvo.innerText);
  if (proibido.test(escolhido)) {
    return { achou: false, recusado: escolhido,
             rotulos: [...new Set(rotulos)].slice(0, 20) };
  }

  const r = alvo.getBoundingClientRect();
  if (!r.width || !r.height) {
    return { achou: false, motivo: 'o item existe mas esta invisivel',
             rotulos: [...new Set(rotulos)].slice(0, 20) };
  }

  // MARCAR o item, para o clique mirar NELE e nao numa coordenada.
  //
  // O menu entra animado (escala e opacidade). O retangulo medido aqui vale
  // para o instante desta medicao; o clique acontece um pouco depois, e ate'
  // la' o item ja' se moveu. Foi o mesmo defeito da setinha, e a setinha ja'
  // tinha sido consertada assim -- este clique ficou para tras.
  document.querySelectorAll('[data-allana-responder]').forEach(
    (el) => el.removeAttribute('data-allana-responder'));
  alvo.setAttribute('data-allana-responder', '1');

  return { achou: true, x: r.x + r.width / 2, y: r.y + r.height / 2,
           rotulo: escolhido, rotulos: [...new Set(rotulos)].slice(0, 20) };
}
"""

# O menu ja' esta' na tela?
# Ha' um menu de contexto aberto?
#
# `ul[role="listbox"]` saiu da lista: ele casava com a LISTA DE CONVERSAS da
# lateral, que esta' sempre na tela. O laboratorio dizia "menu aberto: True"
# com nenhum menu aberto, e o item "Responder" era procurado entre os nomes
# das conversas.
MENU_ABERTO_JS = r"""
() => {
  const lateral = document.querySelector('#pane-side');
  for (const m of document.querySelectorAll('[role="menu"], [role="menubar"]')) {
    if (lateral && lateral.contains(m)) continue;
    const r = m.getBoundingClientRect();
    // Tamanho de MENU DE VERDADE.
    //
    // `r.width && r.height` aceitava um elemento de 4x1 pixel: o seletor de
    // reacoes, que tem role="menu" e aparece no mesmo hover. O log dizia
    // "menu aberto: True" e em seguida "itens na tela: []", e eu li isso
    // como "o item mudou de nome" -- quando o menu da mensagem nem tinha
    // aberto. Um menu com onze itens nao cabe em 4x1.
    if (r.width >= 120 && r.height >= 60) return true;
  }
  return false;
}
"""


# O CORPO da mensagem, sem o nome do autor e sem o horario.
#
# `linha.innerText` traz "Ryan AMEN JESUS 00:47" -- autor na frente, horario
# atras. A barra de citacao mostra so' o corpo, entao comparar com o texto da
# linha inteira falha justamente nas mensagens CURTAS, onde o horario cabe
# dentro do trecho comparado. Isso fazia o bot descartar uma citacao que tinha
# funcionado, e cair para o envio sem citacao sem motivo nenhum.
TEXTO_DA_MENSAGEM_JS = r"""
(dataId) => {
  let linha = null;
  for (const el of document.querySelectorAll('[data-id]')) {
    if (el.getAttribute('data-id') === dataId) {
      linha = el.closest('div[role="row"]') || el;
      break;
    }
  }
  if (!linha) return '';

  const corpo = linha.querySelector('span.selectable-text');
  if (corpo && (corpo.innerText || '').trim()) return corpo.innerText.trim();

  // Sem o span (imagem com legenda, por exemplo): tira o horario do fim.
  return (linha.innerText || '').replace(/\s+/g, ' ').trim()
                                .replace(/\s*\d{1,2}:\d{2}\s*$/, '');
}
"""

# O que a BARRA DE CITACAO mostra da mensagem original: o autor e a PRIMEIRA
# LINHA. Nada mais.
#
# Isto nao e' teoria: `diagnostico/barra_citacao.png`, colhido com a barra
# armada de verdade, mostra tres linhas --
#
#     Ryan
#     LUCIANGELA TESTADO
#     ...
#
# O corpo da mensagem era "LUCIANGELA TESTADO / 72845554753 / AMAPA". O CPF
# e o estado NAO aparecem: o WhatsApp encerra a previa em reticencias.
#
# Por que isto tem um JS proprio: a conferencia da citacao precisa das duas
# marcas ANTES de o menu abrir. Depois do clique em "Responder" a conversa
# pode ter rolado e a linha saido do DOM -- e ai' nao ha' com o que comparar.
MARCAS_DA_MENSAGEM_JS = r"""
(dataId) => {
  let linha = null;
  for (const el of document.querySelectorAll('[data-id]')) {
    if (el.getAttribute('data-id') === dataId) {
      linha = el.closest('div[role="row"]') || el;
      break;
    }
  }
  if (!linha) return { achou: false, autor: '', primeiraLinha: '', corpo: '' };

  // O autor vem do `data-pre-plain-text`: "[12:20, 01/09/2026] Allana: "
  let autor = '';
  const dono = linha.querySelector('[data-pre-plain-text]')
            || (linha.hasAttribute('data-pre-plain-text') ? linha : null);
  if (dono) {
    const meta = dono.getAttribute('data-pre-plain-text') || '';
    const m = meta.match(/^\[[^\]]*\]\s*(.*?):\s*$/);
    if (m) autor = m[1].trim();
  }

  const corpoEl = linha.querySelector('span.selectable-text');
  let corpo = corpoEl && (corpoEl.innerText || '').trim()
    ? corpoEl.innerText.trim()
    : (linha.innerText || '').trim().replace(/\s*\d{1,2}:\d{2}\s*$/, '');

  // A primeira linha NAO VAZIA. Numa mensagem encaminhada ou com legenda a
  // quebra as vezes vem antes do texto.
  const primeira = corpo.split(/\r?\n/).map((t) => t.trim()).find((t) => t) || '';

  return { achou: true, autor, primeiraLinha: primeira, corpo };
}
"""


# A pre-visualizacao de midia esta' aberta?
#
# NAO usar `img[src^="blob:"]` nem `[data-testid="drawer-fullscreen"]`: os dois
# existem com o preview fechado -- as imagens JA ENVIADAS na conversa tambem
# sao blob, e o drawer e' um container permanente do app. Uma sonda anterior
# deu "preview ainda aberto: True" com a tela limpa por causa disso.
#
# Os sinais que so' existem com o preview na tela:
PREVIEW_ABERTO_JS = r"""
() => !!document.querySelector(
  '[data-testid="media-editor-canvas"], '
  + '[data-testid="media-caption-input-container"], '
  + '[aria-label="Enviar 1 item selecionado"]')
"""

# O dialogo "Deseja descartar a selecao?" que o Escape abre sobre o preview.
DIALOGO_DESCARTAR_JS = r"""
() => {
  const norm = (s) => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
                               .replace(/\s+/g, ' ').trim().toLowerCase();
  for (const d of document.querySelectorAll('[role="dialog"], [role="alertdialog"]')) {
    const r = d.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    for (const b of d.querySelectorAll('button, [role="button"]')) {
      const texto = norm(b.innerText);
      // "descartar" e' o que CONFIRMA. "cancelar" devolve o preview -- foi
      // isso que o segundo Escape vinha fazendo.
      if (texto === 'descartar' || texto === 'discard') {
        const rb = b.getBoundingClientRect();
        if (rb.width && rb.height) {
          b.setAttribute('data-allana-descartar', '1');
          return { achou: true, texto };
        }
      }
    }
  }
  return { achou: false };
}
"""


MARCA_GATILHO = "data-allana-gatilho"

# Marca TODOS os candidatos a gatilho de menu dentro da bolha, em ordem.
#
# Em vez de adivinhar o nome do icone (que ja' mudou varias vezes), o Python
# testa um por um e confere se abriu um menu com "Responder". O que abre
# "Selecionar conversas" e' descartado pelo rotulo antes de ser clicado --
# encaminhar manda os dados do cliente para outra conversa.
MARCAR_GATILHOS_JS = r"""
(dataId) => {
  document.querySelectorAll('[data-allana-gatilho]').forEach(
    (el) => el.removeAttribute('data-allana-gatilho'));

  const subirParaALinha = (el) => el.closest('div[role="row"]')
                               || el.closest('div.message-in')
                               || el.closest('div.message-out')
                               || el;
  let linha = null;
  for (const el of document.querySelectorAll('[data-id]')) {
    if (el.getAttribute('data-id') === dataId) { linha = subirParaALinha(el); break; }
  }
  if (!linha) return 0;

  // Observado no DOM real: o botao NAO tem data-icon. Ele se identifica pelo
  // aria-label "Abrir opções de mensagem". A linha em hover tambem expoe
  // "Reagir", que abre o seletor de emoji -- nao serve.
  const preferidos = [
    '[aria-label="Abrir opções de mensagem"]',
    '[aria-label*="opções de mensagem"]',
    '[aria-label*="opcoes de mensagem"]',
    '[aria-label*="message options"]',
    '[data-icon="down-context"]',
    '[data-icon="ic-chevron-down-menu"]',
  ];
  // Desduplicar: o mesmo botao casa com varios seletores da lista, e
  // remarcar sobrescreve o indice -- o candidato 0 deixava de existir.
  const vistos = new Set();
  let n = 0;
  for (const seletor of preferidos) {
    for (const el of linha.querySelectorAll(seletor)) {
      if (vistos.has(el)) continue;
      vistos.add(el);
      el.setAttribute('data-allana-gatilho', String(n));
      n += 1;
      if (n >= 4) return n;
    }
  }
  return n;
}
"""

DIAGNOSTICO_CITACAO_JS = r"""
(dataId) => {
  // Achar por comparacao de STRING, nunca por seletor CSS.
  // CSS.escape() serve para identificadores, nao para valores entre aspas:
  // ele transforma 'a@b' em 'a\@b', que dentro de [data-id="..."] e' outra
  // string. Era por isso que este diagnostico dizia linha_encontrada=False
  // enquanto a citacao tinha achado a mensagem -- o diagnostico mentia.
  // A LINHA da mensagem, nao o elemento que carrega o data-id: nesta versao
  // do WhatsApp o id vem pelado e costuma estar num filho, e o menu de
  // contexto so' responde na linha.
  const subirParaALinha = (el) => el.closest('div[role="row"]')
                               || el.closest('div.message-in')
                               || el.closest('div.message-out')
                               || el;

  let linha = null;
  for (const el of document.querySelectorAll('[data-id]')) {
    if (el.getAttribute('data-id') === dataId) { linha = subirParaALinha(el); break; }
  }

  const texto = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();

  const icones = linha
    ? Array.from(linha.querySelectorAll('[data-icon]'))
        .map((el) => el.getAttribute('data-icon')).slice(0, 15)
    : [];
  const rotulos = linha
    ? Array.from(linha.querySelectorAll('[aria-label]'))
        .map((el) => el.getAttribute('aria-label')).slice(0, 15)
    : [];
  const botoes = linha
    ? Array.from(linha.querySelectorAll('[role="button"], button'))
        .map((el) => el.getAttribute('aria-label') || texto(el)).slice(0, 15)
    : [];
  const menus = Array.from(document.querySelectorAll(
      '[role="menu"], [role="menuitem"], ul[role="listbox"]'))
    .map(texto).filter(Boolean).slice(0, 10);
  const parecidos = Array.from(document.querySelectorAll(
      'li, div[role="menuitem"], div[role="button"], button'))
    .map(texto)
    .filter((s) => s && s.length < 40 && /respond|reply|encaminh|forward/i.test(s))
    .slice(0, 10);

  return { achou_linha: !!linha, icones, rotulos, botoes, menus, parecidos };
}
"""

# Roda so' quando a leitura devolve zero mensagens por varios ciclos seguidos.
# Sem isto o sintoma e' "conectado, fila zerada, nada acontece" -- que foi
# exatamente o que aconteceu: 0 mensagens lidas e nenhuma linha de log.
DIAGNOSTICO_LEITURA_JS = """
() => {
  const main = document.querySelector('#main') || document.querySelector('main');
  const conta = (sel) => {
    try { return (main || document).querySelectorAll(sel).length; } catch (e) { return -1; }
  };
  const cab = document.querySelector('header span[title]');
  return {
    main: !!main,
    conversa: cab ? (cab.getAttribute('title') || '') : '',
    linhas: conta('div[role="row"]'),
    campos_busca: (document.querySelectorAll('#side input').length
                   + document.querySelectorAll('#side div[contenteditable="true"]').length),
    conversas_na_lista: Array.from(
      (document.querySelector('#pane-side') || document).querySelectorAll('span[title]')
    ).slice(0, 8).map((el) => el.getAttribute('title')),
    recebidas_classe: conta('div.message-in'),
    enviadas_classe: conta('div.message-out'),
    recebidas_dataid: conta('[data-id^="false_"]'),
    enviadas_dataid: conta('[data-id^="true_"]'),
    pre_plain: conta('[data-pre-plain-text]')
  };
}
"""

# Devolve TODOS os titulos do cabecalho, nao um so'.
#
# Num grupo o cabecalho tem duas linhas com atributo title: o nome do grupo e
# a lista de participantes ("Allana, Glaucon, Joselia, Ryan, ..."). Pegar "o
# primeiro span[title]" trazia a lista de participantes, o bot concluia que a
# conversa aberta nao era o grupo configurado e recusava ler -- repetindo o
# aviso a cada 3 segundos. Quem sabe qual dos titulos importa e' o Python,
# que tem o nome configurado para comparar.
CHAT_INFO_JS = r"""
() => {
  // '#main header', nunca 'header' sozinho: o de fora e' o da LISTA LATERAL
  // ("3 Atualizações no status"), nao o da conversa.
  const main = document.querySelector('#main');
  const cabecalho = main ? main.querySelector('header') : null;

  // Duas fontes, porque nem todo nome vem como span[title]. Observado em
  // producao: neste header so' a LISTA DE PARTICIPANTES tem o atributo
  // `title`; o nome do grupo aparece apenas como texto. Ler so' os
  // span[title] fazia o bot achar que estava sempre na conversa errada, e
  // reabrir o grupo a cada 3 segundos sem nunca processar nada.
  const porAtributo = cabecalho
    ? Array.from(cabecalho.querySelectorAll('span[title]'))
        .map((s) => (s.getAttribute('title') || '').trim())
    : [];
  const porTexto = cabecalho
    ? (cabecalho.innerText || '').split(String.fromCharCode(10)).map((s) => s.trim())
    : [];

  const titulos = [];
  for (const valor of porAtributo.concat(porTexto)) {
    if (valor && !titulos.includes(valor)) titulos.push(valor);
  }

  const row = document.querySelector('#main [data-id]');
  const dataId = row ? row.getAttribute('data-id') || '' : '';
  const parts = dataId.split('_');
  const jid = parts.length > 1 ? parts[1] : '';
  return { titulos, jid, temMain: !!main };
}
"""

SELF_PHONE_JS = """
() => {
  const el = document.querySelector('span[title^="+"]');
  if (el) return el.getAttribute('title') || '';
  const alt = document.querySelector('[data-testid="default-user"] , header img[alt^="+"]');
  return alt ? (alt.getAttribute('alt') || '') : '';
}
"""


@dataclass(frozen=True)
class WhatsAppStatus:
    state: str = DISCONNECTED
    phone: str = ""
    chat_name: str = ""
    chat_id: str = ""
    since: str = ""
    last_error: str = ""
    last_poll: str = ""

    @property
    def connected(self) -> bool:
        return self.state == CONNECTED

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "connected": self.connected,
            "phone": self.phone,
            "chat_name": self.chat_name,
            "chat_id": self.chat_id,
            "since": self.since,
            "last_error": self.last_error,
            "last_poll": self.last_poll,
        }


def parse_data_id(data_id: str) -> tuple[str, str]:
    """``false_<chat>@g.us_<msgid>_<remetente>@c.us`` -> (chat_jid, sender_jid).

    Em conversa individual o remetente nao aparece no ``data-id``; nesse caso o
    proprio chat e' o remetente.
    """
    parts = (data_id or "").split("_")
    if len(parts) < 2:
        return "", ""
    chat_jid = parts[1]
    sender_jid = ""
    for chunk in reversed(parts[2:]):
        if "@" in chunk:
            sender_jid = chunk
            break
    if not sender_jid and "@g.us" not in chat_jid:
        sender_jid = chat_jid
    return chat_jid, sender_jid


def id_de_mensagem_nossa(data_id: str) -> bool:
    """A mensagem com este ``data-id`` foi enviada por NOS?

    Segunda camada, em Python: o mesmo julgamento que o JS faz na tela, feito
    de novo aqui, sem depender de nome configurado nem de seletor. Em
    20/09/2026 o JS deixou passar tres mensagens da propria conta -- o
    ``BOT_SELF_NAME`` nao batia com o nome real da conta no grupo -- e nao
    havia mais ninguem conferindo antes de virarem solicitacao.

    Duas familias de id, e a ordem importa:

    * formato classico -- ``true_`` saiu daqui, ``false_`` chegou de fora. O
      prefixo manda, e um ``3EB0`` no meio nao significa nada;
    * id pelado -- o WhatsApp Web gera ``3EB0...`` no que ele mesmo envia.
      Conferido no banco de producao: 15 mensagens de consultor chegaram com
      id em ``2A...``/``AC...``; as 3 da propria conta, em ``3EB0``.
    """
    nu = (data_id or "").strip()
    if nu.startswith("album-"):      # album de imagens NOSSAS
        nu = nu[len("album-"):]
    if nu.startswith("false_"):
        return False
    return nu.startswith("true_") or nu.startswith("3EB0")


def parse_pre_plain(meta: str) -> tuple[str, str]:
    """``[15:32, 27/08/2026] Ryan: `` -> ("15:32, 27/08/2026", "Ryan")."""
    match = _PRE_PLAIN.match((meta or "").strip())
    if not match:
        return "", ""
    return match.group("time").strip(), match.group("name").strip()


def clean_text(text: str) -> str:
    lines = []
    for raw in (text or "").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or line.lower() in {"encaminhada", "encaminhado", "editada", "editado"}:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


class WhatsAppService(ThreadActor):
    def __init__(
        self,
        profile_dir: Path,
        group_name: str,
        state: StateStore,
        *,
        poll_seconds: float = 3.0,
        headless: bool = False,
        reply_quote: bool = True,
        browser_executable: str = "",
        bot_self_name: str = "Operacional Capital",
        read_limit: int = 30,
        on_status: Callable[[WhatsAppStatus], None] | None = None,
        on_log: Callable[[str, str], None] | None = None,
        on_incoming: Callable[[IncomingMessage], None] | None = None,
    ) -> None:
        super().__init__("whatsapp")
        self.profile_dir = Path(profile_dir)
        self.group_name = (group_name or "").strip()
        self.state = state
        self.poll_seconds = max(1.0, poll_seconds)
        self.headless = headless
        self.reply_quote = reply_quote
        self.browser_executable = browser_executable
        # Como o bot aparece no grupo. Sem isso ele nao distingue as
        # proprias mensagens: as classes message-in/out nao existem aqui.
        self.bot_self_name = bot_self_name
        self.read_limit = max(10, read_limit)
        self._on_status = on_status
        self._on_log = on_log
        # Porta DURAVEL: grava a mensagem antes de a leitura marca-la como
        # vista (ver `_poll_messages`). Sem ela, fica o caminho antigo, so' RAM.
        self._on_incoming = on_incoming

        self.inbox: "queue.Queue[IncomingMessage]" = queue.Queue()

        # Ciclos seguidos sem ler nada. Serve para diagnosticar uma vez, e nao
        # a cada 3 segundos, quando a leitura para de casar com a pagina.
        self._sem_mensagens = 0
        # Como a ultima imagem foi anexada: colar, menu ou input.
        self._ultima_via_de_envio = ""
        # Se a ULTIMA resposta saiu ancorada. Alimenta o log de entrega.
        self._ultima_citacao_ok = False
        # Ultima conversa errada ja' avisada, para nao repetir o aviso.
        self._ultima_conversa_errada = ""
        # Diagnostico da citacao roda UMA vez, nao a cada resposta.
        self._citacao_diagnosticada = False
        # Estado da bolha colhido no hover, quando os icones existem.
        # Fotografia da tela no ponto exato onde a citacao falhou.
        self._estado_da_citacao: dict | None = None
        #: Autor e corpo da mensagem que estamos citando, colhidos no
        #: PASSO 1. No PASSO 5 a linha pode ter saido do DOM.
        self._marcas_da_citacao: dict = {}
        #: O alvo tinha irmao identico na tela? Decide entre `ok` e
        #: `unverified` -- ver `_conferir_citacao`.
        self._alvo_ambiguo = False
        #: O id que a citacao mirou. E' a prova gravada na saida.
        self._alvo_citado = ""
        # Qual via armou a citacao. Vai para o log de entrega.
        self._ultima_via_de_citacao = ""
        self._anexo_diagnosticado = False
        # A conferencia do nome proprio roda uma vez por execucao.
        self._nome_proprio_conferido = False

        self._status = WhatsAppStatus()
        self._status_lock = threading.Lock()
        self._playwright = None
        self._context = None
        self._page = None
        self._current_chat = ""
        self._reconnect_requested = threading.Event()
        self._qr_cache = ""

    # ------------------------------------------------------------------ estado
    @property
    def status(self) -> WhatsAppStatus:
        with self._status_lock:
            return self._status

    # ``last_poll`` e' um batimento: muda a cada ciclo de leitura e nao
    # representa mudanca de estado. Se ele contasse como alteracao, o painel
    # receberia um "WhatsApp conectado" a cada 3 segundos.
    # ~1 minuto com poll_seconds=3.0 antes de gritar.
    _CICLOS_ATE_DIAGNOSTICAR = 20
    # Reabrir de tempos em tempos, nao a cada ciclo: clicar sem parar na
    # interface atrapalharia o operador usando a mesma janela.
    _CICLOS_ATE_REABRIR = 10

    _CAMPOS_DE_ESTADO = ("state", "phone", "chat_name", "chat_id", "since", "last_error")

    def _set_status(self, **changes) -> None:
        with self._status_lock:
            previous = self._status
            merged = {**previous.as_dict(), **changes}
            merged.pop("connected", None)
            self._status = WhatsAppStatus(
                state=merged.get("state", previous.state),
                phone=merged.get("phone", previous.phone),
                chat_name=merged.get("chat_name", previous.chat_name),
                chat_id=merged.get("chat_id", previous.chat_id),
                since=merged.get("since", previous.since),
                last_error=merged.get("last_error", previous.last_error),
                last_poll=merged.get("last_poll", previous.last_poll),
            )
            changed = any(
                getattr(self._status, campo) != getattr(previous, campo)
                for campo in self._CAMPOS_DE_ESTADO
            )
            snapshot = self._status
        if changed and self._on_status:
            try:
                self._on_status(snapshot)
            except Exception:
                pass

    def _log(self, level: str, message: str) -> None:
        if self._on_log:
            try:
                self._on_log(level, message)
            except Exception:
                pass

    # ---------------------------------------------------------- API publica
    def ja_enviado(self, marca: str, timeout: float = 20.0) -> bool:
        """Alguma mensagem NOSSA no chat ja' contem esta marca?

        Chamado antes de reenviar. Roteado pelo ator, como todo o resto: o
        Playwright nunca sai da thread dona.
        """
        if not marca or not self.status.connected:
            return False
        try:
            return bool(self.call(self._do_ja_enviado, marca, timeout=timeout))
        except Exception:
            # Na duvida, NAO afirmar que ja' foi: o reenvio existe justamente
            # para o consultor nao ficar sem resposta.
            return False

    def _do_ja_enviado(self, marca: str) -> bool:
        if self._page is None:
            return False
        try:
            return bool(self._page.evaluate(JA_ENVIADO_JS, marca))
        except (PlaywrightTimeout, PlaywrightError):
            return False

    def send(self, chat_id: str, chat_name: str, text: str, quote_message_id: str = "",
             timeout: float = 90.0, texto_sem_citacao: str = "", quote_text: str = "",
             quote_participant: str = "") -> ResultadoEnvio:
        """Envia texto para um chat especifico. Executado na thread dona.

        ``texto_sem_citacao`` e' a MESMA resposta escrita para se sustentar
        sozinha -- com o nome do consultor no fim. Quem monta as duas versoes
        e' ``mensagens.py``; aqui so' se escolhe qual sai, e a escolha so'
        pode ser feita DEPOIS de tentar citar. Sem isso o manager teria de
        adivinhar antes do envio se a citacao ia funcionar.

        ``quote_text``/``quote_participant`` existem pelo contrato comum com a
        camada Evolution. Aqui (legado) a citacao e' feita na tela pelo id.
        """
        return self.call(self._do_send, chat_id, chat_name, text, quote_message_id,
                         texto_sem_citacao, timeout=timeout)

    def send_image(
        self,
        chat_id: str,
        chat_name: str,
        image_path: str | Path,
        caption: str = "",
        quote_message_id: str = "",
        timeout: float = 120.0,
        caption_sem_citacao: str = "",
        quote_text: str = "",
        quote_participant: str = "",
    ) -> ResultadoEnvio:
        """Envia uma imagem com legenda. Levanta excecao se nao conseguir.

        ``caption_sem_citacao``: ver a nota em ``send``.
        """
        return self.call(
            self._do_send_image, chat_id, chat_name, str(image_path), caption,
            quote_message_id, caption_sem_citacao, timeout=timeout,
        )

    def render_png(self, html: str, path: str | Path, width: int = 900,
                   timeout: float = 60.0) -> str:
        """Renderiza HTML e salva um PNG.

        Reaproveita o navegador que ja' esta' aberto para o WhatsApp: nenhuma
        dependencia de imagem e nenhum processo novo. Roda na thread dona,
        como todo o resto.
        """
        return self.call(self._do_render_png, html, str(path), width, timeout=timeout)

    def qr_data_url(self, timeout: float = 20.0) -> str:
        try:
            return self.call(self._do_qr, timeout=timeout) or ""
        except Exception:
            return self._qr_cache

    def request_reconnect(self) -> None:
        """Pede reconexao. O fechamento acontece na thread dona, nunca aqui."""
        self._reconnect_requested.set()

    # ------------------------------------------------------------- loop dono
    def run(self) -> None:
        backoff = 5.0
        while not self.stopping:
            try:
                self._set_status(state=STARTING, last_error="")
                self._launch()
                backoff = 5.0
            except Exception as exc:
                message = _short(exc)
                self._set_status(state=DISCONNECTED, last_error=message)
                self._log("ERROR", f"Falha ao iniciar o WhatsApp: {_explicar_falha_de_perfil(message, self.profile_dir)}")
                self._teardown()
                if self._sleep_with_commands(backoff):
                    break
                backoff = min(backoff * 1.6, 60.0)
                continue

            try:
                self._serve()
            except Exception as exc:
                self._log("ERROR", f"Sessão do WhatsApp interrompida: {_short(exc)}")
                self._set_status(state=DISCONNECTED, last_error=_short(exc))
            finally:
                self._teardown()

            if self.stopping:
                break
            if self._sleep_with_commands(3.0):
                break

        self._teardown()
        self._set_status(state=DISCONNECTED)

    def _sleep_with_commands(self, seconds: float) -> bool:
        """Espera atendendo comandos. Devolve True se pediram encerramento."""
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if not self._drain(min(remaining, 0.5)):
                return True

    # ------------------------------------------------------------- navegador
    def _launch(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        # Restos de uma execucao anterior seguram o perfil e impedem a
        # abertura. Em 01/09 o WhatsApp caiu, um brave.exe orfao continuou
        # vivo, e o bot nao subiu mais -- com 49 solicitacoes na fila
        # esperando alguem perceber.
        #
        # So' encerra navegador cujo `--user-data-dir` e' EXATAMENTE este
        # perfil: o navegador pessoal do operador nao e' tocado.
        try:
            encerrar_orfaos(self.profile_dir, on_log=self._log)
        except Exception as exc:
            # Limpeza nao pode impedir o boot: se falhar, seguimos e deixamos
            # o Playwright dar o erro dele, que ja' explica o perfil ocupado.
            self._log("WARNING", f"Não consegui conferir navegadores órfãos: {_short(exc)}")

        self._playwright = sync_playwright().start()

        opcoes: dict = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1280, "height": 860},
            locale="pt-BR",
            args=["--no-first-run", "--lang=pt-BR", "--disable-blink-features=AutomationControlled"],
        )
        # Abrir com o MESMO navegador do perfil. Um perfil copiado do Brave
        # aberto pelo Chromium do Playwright funciona na maior parte dos casos,
        # mas as senhas salvas dependem de detalhes da instalação — usar o
        # Brave evita essa classe inteira de surpresa.
        if self.browser_executable and Path(self.browser_executable).exists():
            opcoes["executable_path"] = self.browser_executable

        self._context = self._playwright.chromium.launch_persistent_context(**opcoes)
        self._page = self._escolher_pagina()
        self._page.set_default_timeout(20_000)
        if "web.whatsapp.com" not in (self._page.url or ""):
            self._page.goto("https://web.whatsapp.com",
                            wait_until="domcontentloaded", timeout=60_000)
        self._fechar_abas_extras()
        self._current_chat = ""

    def _escolher_pagina(self):
        """Devolve a aba do WhatsApp -- nunca uma about:blank por acaso.

        Um perfil persistente RESTAURA as abas da sessao anterior, e
        ``pages[0]`` pode perfeitamente ser uma pagina em branco que sobrou.
        Quando isso acontece o bot pilota uma aba vazia a sessao inteira: sem
        campo de digitacao, sem mensagem para citar, sem menu de anexo -- e
        cada sintoma parece um defeito diferente. Escolher pela URL elimina a
        classe inteira.
        """
        if self._context is None:
            raise RuntimeError("navegador indisponível")

        paginas = list(self._context.pages)
        for pagina in paginas:
            try:
                if "web.whatsapp.com" in (pagina.url or ""):
                    return pagina
            except Exception:
                continue
        # Nenhuma serve: reaproveita uma em branco ou cria.
        for pagina in paginas:
            try:
                if (pagina.url or "about:blank") in ("about:blank", ""):
                    return pagina
            except Exception:
                continue
        return self._context.new_page()

    def _fechar_abas_extras(self) -> None:
        """Fecha as abas que nao sao a do WhatsApp.

        Elas nao sao so' sujeira visual: enquanto existirem, um ``pages[0]``
        pode voltar a apontar para a errada no proximo boot.
        """
        if self._context is None or self._page is None:
            return
        fechadas = 0
        for pagina in list(self._context.pages):
            if pagina is self._page:
                continue
            try:
                if "web.whatsapp.com" in (pagina.url or ""):
                    continue
                pagina.close()
                fechadas += 1
            except Exception:
                continue
        if fechadas:
            self._log("INFO", f"{fechadas} aba(s) extra(s) fechada(s) no navegador.")

    def _teardown(self) -> None:
        """Sempre chamado na thread dona - e' o que a versao antiga violava."""
        for closer in (
            lambda: self._context.close() if self._context else None,
            lambda: self._playwright.stop() if self._playwright else None,
        ):
            try:
                closer()
            except Exception:
                pass
        self._context = None
        self._playwright = None
        self._page = None
        self._current_chat = ""

    # ------------------------------------------------------------------ sessao
    def _serve(self) -> None:
        if not self._await_login():
            return

        self._set_status(
            state=CONNECTED,
            since=now_iso(),
            phone=self._read_self_phone(),
            last_error="",
        )
        self._log("INFO", "WhatsApp conectado.")
        self._open_target_chat()

        next_poll = 0.0
        while not self.stopping:
            if not self._drain(0.4):
                return
            if self._reconnect_requested.is_set():
                self._reconnect_requested.clear()
                self._log("INFO", "Reconexão solicitada pelo painel.")
                return
            if time.monotonic() < next_poll:
                continue
            next_poll = time.monotonic() + self.poll_seconds

            if not self._is_logged_in():
                self._set_status(state=DISCONNECTED, last_error="sessão encerrada no celular")
                self._log("WARNING", "WhatsApp desconectou.")
                return
            self._poll_messages()

    def _await_login(self) -> bool:
        """Espera o login, publicando o estado QR enquanto o codigo estiver na tela."""
        deadline = time.monotonic() + 600
        announced_qr = False
        while time.monotonic() < deadline and not self.stopping:
            if not self._drain(0.3):
                return False
            if self._is_logged_in():
                return True
            if self._has_qr():
                self._qr_cache = self._read_qr() or self._qr_cache
                if not announced_qr:
                    self._log("WARNING", "Aguardando leitura do QR Code para conectar.")
                    announced_qr = True
                self._set_status(state=QR, last_error="aguardando leitura do QR Code")
            time.sleep(0.7)
        if not self.stopping:
            self._set_status(state=DISCONNECTED, last_error="tempo esgotado aguardando login")
        return False

    def _is_logged_in(self) -> bool:
        try:
            return self._page.locator(CHAT_LIST).count() > 0
        except Exception:
            return False

    def _has_qr(self) -> bool:
        try:
            return self._page.locator(QR_CANVAS).count() > 0
        except Exception:
            return False

    def _read_qr(self) -> str:
        try:
            return self._page.evaluate(
                """
                () => {
                  const c = document.querySelector('canvas[aria-label], div[data-ref] canvas');
                  return c ? c.toDataURL('image/png') : '';
                }
                """
            ) or ""
        except Exception:
            return ""

    def _read_self_phone(self) -> str:
        try:
            return (self._page.evaluate(SELF_PHONE_JS) or "").strip()
        except Exception:
            return ""

    # -------------------------------------------------------------------- chat
    def _open_target_chat(self) -> None:
        if self.group_name:
            self._open_chat(self.group_name)
        self._refresh_chat_info()

    def _nome_no_cabecalho(self) -> str:
        """Nome da conversa aberta agora, do cabecalho DO CHAT.

        Devolve vazio quando nao ha' conversa aberta -- e' assim que a guarda
        de envio sabe que nao pode digitar em lugar nenhum.
        """
        info = self._page.evaluate(CHAT_INFO_JS) or {}
        if not info.get("temMain"):
            return ""
        titulos = [str(x).strip() for x in (info.get("titulos") or []) if str(x).strip()]
        return self._escolher_titulo(titulos)

    def _abrir_pela_lista(self, chat_name: str) -> bool:
        """Abre a conversa clicando nela na lista lateral.

        Mais confiavel que a busca: nao depende do campo de pesquisa, que ja'
        mudou de <div contenteditable> para <input> entre versoes. O grupo de
        trabalho fica no topo da lista porque e' o mais ativo.
        """
        try:
            if not self._page.evaluate(ACHAR_CONVERSA_JS, chat_name):
                return False
            alvo = self._page.locator(f"[{MARCA_ALVO}]").first
            alvo.click(timeout=5_000)
            self._page.wait_for_selector("#main", timeout=6_000)
            self._page.wait_for_timeout(400)
            self._current_chat = chat_name
            return True
        except (PlaywrightTimeout, PlaywrightError):
            return False

    def _campo_de_busca(self):
        """Devolve o campo de busca ja' clicado, ou None se nenhum candidato servir."""
        for seletor in SEARCH_BOX_CANDIDATOS:
            alvo = self._page.locator(seletor).first
            try:
                if alvo.count() == 0:
                    continue
                alvo.click(timeout=3_000)
                return alvo
            except (PlaywrightTimeout, PlaywrightError):
                continue
        return None

    def _open_chat(self, chat_name: str) -> bool:
        if not chat_name:
            return False
        if self._current_chat == chat_name:
            return True

        # A conversa pode ja' estar aberta -- inclusive porque o operador a
        # abriu a mao depois do aviso anterior. Conferir o cabecalho primeiro
        # evita mexer na busca por nada, que e' justamente onde isto falhava.
        try:
            if _normalizar(self._nome_no_cabecalho()) == _normalizar(chat_name):
                self._current_chat = chat_name
                return True
        except (PlaywrightTimeout, PlaywrightError):
            pass

        # Clicar na conversa na lista lateral e' mais confiavel do que buscar:
        # nao depende do campo de busca, que ja' mudou de <div> para <input>.
        # O grupo de trabalho fica no topo da lista porque e' o mais ativo.
        if self._abrir_pela_lista(chat_name):
            return True

        try:
            search = self._campo_de_busca()
            if search is None:
                self._log(
                    "WARNING",
                    f"Não encontrei nem a conversa na lista nem o campo de busca "
                    f"para abrir '{chat_name}'. Abra a conversa manualmente.",
                )
                return False
            self._page.keyboard.press("Control+A")
            self._page.keyboard.press("Backspace")
            search.type(chat_name, delay=25)
            self._page.wait_for_timeout(1200)
            self._page.locator(f'span[title="{chat_name}"]').first.click(timeout=10_000)
            self._page.wait_for_selector("#main", timeout=10_000)
            self._page.wait_for_timeout(400)
            self._current_chat = chat_name
            return True
        except (PlaywrightTimeout, PlaywrightError) as exc:
            self._log(
                "WARNING",
                f"Não consegui abrir a conversa '{chat_name}' automaticamente ({_short(exc)}). "
                "Abra a conversa manualmente na janela do WhatsApp.",
            )
            self._escape()
            return False

    def _conferir_nome_proprio(self) -> None:
        """Confere o BOT_SELF_NAME contra os autores que estao na tela.

        Se o nome configurado nao aparecer em nenhuma mensagem, a deteccao de
        autoria perde o sinal forte e passa a depender do prefixo do id e do
        recibo -- que sao fracos aqui. O bot pode voltar a ler as proprias
        respostas, que foi o que encheu o grupo com 53 mensagens numa noite.
        Descobrir isso no boot custa uma linha de log; descobrir em producao
        custa o grupo do cliente.
        """
        if self._nome_proprio_conferido or self._page is None:
            return
        try:
            autores = self._page.evaluate(AUTORES_VISIVEIS_JS) or []
        except (PlaywrightTimeout, PlaywrightError):
            return
        if not autores:
            return   # nada na tela ainda; tenta no proximo ciclo

        self._nome_proprio_conferido = True
        alvo = _normalizar(self.bot_self_name)
        if any(_normalizar(a) == alvo for a in autores):
            self._log("INFO", f"Nome próprio confirmado: {self.bot_self_name!r} "
                              "aparece entre os autores do grupo.")
            return
        self._log(
            "ERROR",
            f"BOT_SELF_NAME={self.bot_self_name!r} NÃO aparece entre os autores "
            f"das mensagens visíveis: {autores}. Enquanto não bater, o bot pode "
            "tratar as próprias respostas como pedidos. Ajuste o .env com o nome "
            "exato que ele usa no grupo.",
        )

    def _diagnosticar_leitura_vazia(self) -> None:
        """Diz por que a leitura voltou vazia, em vez de ficar calado.

        Sao tres causas com correcoes diferentes: nenhuma conversa aberta,
        conversa aberta mas so' com mensagens nossas, ou os seletores nao
        casarem mais com o HTML do WhatsApp. O log tem de separar as tres.
        """
        try:
            d = self._page.evaluate(DIAGNOSTICO_LEITURA_JS) or {}
        except Exception as exc:
            self._log("WARNING", f"Nenhuma mensagem lida e o diagnóstico falhou ({_short(exc)}).")
            return

        if not d.get("main"):
            lista = d.get("conversas_na_lista") or []
            achou = any(
                (c or "").strip().casefold() == self.group_name.strip().casefold()
                for c in lista
            )
            self._log(
                "WARNING",
                "Nenhuma conversa aberta na janela do WhatsApp. "
                + (f"O grupo '{self.group_name}' ESTÁ na lista — vou continuar tentando abrir."
                   if achou else
                   f"E o grupo '{self.group_name}' NÃO aparece na lista lateral; "
                   f"confira WHATSAPP_GROUP_NAME no .env. Lista: {lista}")
                + f" (campos de busca encontrados: {d.get('campos_busca')})",
            )
            return

        recebidas = max(int(d.get("recebidas_dataid") or 0), int(d.get("recebidas_classe") or 0))
        if recebidas == 0 and int(d.get("enviadas_dataid") or 0) > 0:
            self._log(
                "INFO",
                f"Conversa '{d.get('conversa') or '?'}' aberta, mas só há mensagens "
                "enviadas por nós. Aguardando alguém escrever.",
            )
            return

        self._log(
            "ERROR",
            "Conversa aberta e com mensagens, mas a leitura devolveu zero: os "
            "seletores não casam mais com o HTML do WhatsApp Web. "
            f"conversa={d.get('conversa') or '?'} linhas={d.get('linhas')} "
            f"recebidas(data-id)={d.get('recebidas_dataid')} "
            f"recebidas(classe)={d.get('recebidas_classe')} "
            f"enviadas(data-id)={d.get('enviadas_dataid')} "
            f"pre-plain={d.get('pre_plain')}",
        )

    def _escolher_titulo(self, titulos: list[str]) -> str:
        """Qual dos titulos do cabecalho e' o nome da conversa.

        Se o grupo configurado esta' entre eles, e' ele -- comparando
        normalizado, para o acento decomposto nao derrubar a igualdade.
        Senao, o primeiro, que e' o nome nas conversas individuais.
        """
        alvo = _normalizar(self.group_name)
        if alvo:
            for titulo in titulos:
                if _normalizar(titulo) == alvo:
                    return self.group_name
        return titulos[0] if titulos else ""

    def _refresh_chat_info(self) -> None:
        try:
            info = self._page.evaluate(CHAT_INFO_JS) or {}
        except Exception:
            return
        titulos = [str(x).strip() for x in (info.get("titulos") or []) if str(x).strip()]
        name = self._escolher_titulo(titulos)
        jid = (info.get("jid") or "").strip()
        if name:
            self._current_chat = name
        self._set_status(chat_name=name, chat_id=jid)

    # ---------------------------------------------------------------- leitura
    def _poll_messages(self) -> None:
        try:
            rows = self._page.evaluate(
                READ_MESSAGES_JS, [self.read_limit, self.bot_self_name]) or []
        except Exception as exc:
            raise RuntimeError(f"falha ao ler mensagens: {_short(exc)}") from exc

        self._set_status(last_poll=now_iso())
        if not rows:
            self._sem_mensagens += 1
            # Conectado, fila zerada e nenhuma leitura: o sintoma mais dificil
            # de diagnosticar que este bot tem, porque parece saude. Depois de
            # ~1 minuto assim, despeja o que a pagina realmente contem.
            if self._sem_mensagens == self._CICLOS_ATE_DIAGNOSTICAR:
                self._diagnosticar_leitura_vazia()

            # E, sobretudo, tenta reabrir a conversa. Antes disto, uma falha ao
            # abrir na largada era DEFINITIVA: sem conversa aberta nao ha' linhas,
            # sem linhas o codigo saia aqui, e o unico ponto que reabria ficava
            # depois deste return. O bot passava a noite conectado sem ler nada.
            if self.group_name and self._sem_mensagens % self._CICLOS_ATE_REABRIR == 0:
                self._current_chat = ""
                if self._open_chat(self.group_name):
                    self._log("INFO", f"Conversa '{self.group_name}' reaberta automaticamente.")
            return

        if self._sem_mensagens >= self._CICLOS_ATE_DIAGNOSTICAR:
            self._log("INFO", f"Leitura normalizada: {len(rows)} mensagens visíveis.")
        self._sem_mensagens = 0

        self._refresh_chat_info()
        status = self.status
        chat_id = status.chat_id or self.group_name or "chat"
        chat_name = status.chat_name or self.group_name or "conversa aberta"

        if self.group_name and chat_name and _normalizar(chat_name) != _normalizar(self.group_name):
            # A conversa na tela nao e' a configurada: nao processar nada dela.
            # O aviso sai UMA vez por conversa errada, nao a cada ciclo: com
            # poll de 3s isto enchia o log com a mesma linha e escondia
            # qualquer outra coisa que estivesse acontecendo.
            if self._ultima_conversa_errada != chat_name:
                self._ultima_conversa_errada = chat_name
                self._log(
                    "WARNING",
                    f"Conversa aberta ('{_curto_titulo(chat_name)}') não é o grupo "
                    f"configurado ('{self.group_name}'). Tentando reabrir.",
                )
            self._current_chat = ""
            if self._open_chat(self.group_name):
                self._ultima_conversa_errada = ""
                self._log("INFO", f"Grupo '{self.group_name}' reaberto.")
            return

        self._ultima_conversa_errada = ""

        # Com a conversa certa aberta e mensagens na tela, este e' o
        # momento de conferir se o nome configurado bate com a realidade.
        self._conferir_nome_proprio()

        ids = [row.get("id", "") for row in rows if row.get("id")]
        # Primeira leitura deste chat: apenas marca a linha de base. Sem isto,
        # o bot responderia as ultimas 30 mensagens antigas a cada instalacao.
        if self.state.baseline_if_new(chat_id, ids):
            self._log(
                "INFO",
                f"Linha de base criada para '{chat_name}': "
                f"{len(ids)} mensagens antigas ignoradas.",
            )
            return

        for row in rows:
            message_id = row.get("id", "")
            if not message_id or self.state.has_seen(message_id):
                continue
            if id_de_mensagem_nossa(message_id):
                # Nao deveria chegar aqui: o JS ja' filtra pela autoria na
                # tela. Se chegou, aquele sinal falhou -- e responder a
                # propria resposta e' o defeito mais caro deste bot. Marca
                # como vista (nao volta no proximo ciclo) e diz por que.
                self.state.mark_seen(message_id)
                self._log("WARNING",
                          f"Mensagem {message_id[:28]} tem id de mensagem NOSSA e passou "
                          "pelo filtro de autoria da tela; ignorada aqui. Confira o "
                          f"BOT_SELF_NAME ({self.bot_self_name!r}) contra o nome que a "
                          "conta usa no grupo.")
                continue
            text = clean_text(row.get("text", ""))
            if not text:
                self.state.mark_seen(message_id)
                continue
            _chat_jid, sender_jid = parse_data_id(message_id)
            stamp, sender_name = parse_pre_plain(row.get("meta", ""))
            mensagem = IncomingMessage(
                message_id=message_id,
                chat_id=chat_id,
                chat_name=chat_name,
                sender_id=sender_jid or "desconhecido",
                sender_name=sender_name or "Consultor",
                text=text,
                timestamp=stamp,
            )
            if self._on_incoming is None:
                self.state.mark_seen(message_id)
                self.inbox.put(mensagem)
                continue
            # GRAVAR ANTES DE MARCAR COMO VISTA. Na ordem antiga (marca no
            # state.json, depois fila em RAM), uma queda com o pedido ainda
            # na fila -- comum com o ator ocupado enviando -- o perdia: o
            # proximo boot o via como "ja' visto" e ninguem respondia.
            try:
                self._on_incoming(mensagem)
            except Exception as exc:  # noqa: BLE001 - tenta de novo no proximo ciclo
                self._log("ERROR", f"Não consegui gravar a mensagem {message_id}: "
                                   f"{_short(exc)}. Tento de novo na próxima leitura.")
                continue
            self.state.mark_seen(message_id)

    # ------------------------------------------------------------------ envio
    def _ja_esta_no_chat(self, texto: str) -> bool:
        """A mensagem com este texto ja' aparece na conversa?

        Usado para nao duplicar: quando a confirmacao de envio falha mas a
        mensagem saiu, reenviar em texto entrega a mesma resposta duas vezes.
        """
        trecho = " ".join((texto or "").split())[:60]
        if not trecho or self._page is None:
            return False
        try:
            return bool(self._page.evaluate(IDS_POR_TEXTO_JS, [trecho, 8]))
        except (PlaywrightTimeout, PlaywrightError):
            return False

    def _lembrar_do_que_enviamos(self, texto: str) -> None:
        """Marca como vista a mensagem que ACABAMOS de enviar.

        Sem isto o bot lia a propria resposta no ciclo seguinte, nao achava
        CPF valido nela, cobrava CPF, lia a cobranca e cobrava de novo -- 53
        mensagens no grupo do cliente numa noite.

        Casar pelo TEXTO e' o que funciona aqui: esta versao do WhatsApp nao
        usa message-in/message-out, serve o data-id sem prefixo e nao expoe
        recibo de entrega em nenhum seletor estavel. O texto nos conhecemos
        porque fomos nos que escrevemos.
        """
        trecho = " ".join((texto or "").split())[:60]
        if not trecho or self._page is None:
            return
        try:
            ids = self._page.evaluate(IDS_POR_TEXTO_JS, [trecho, 8]) or []
        except (PlaywrightTimeout, PlaywrightError):
            return
        for mid in ids:
            self.state.mark_seen(str(mid), flush=False)
        if ids:
            self.state.flush()

    def _garantir_conversa(self, chat_name: str) -> bool:
        """Confirma que a conversa esta' MESMO aberta antes de enviar.

        Confiar em ``self._current_chat`` nao basta: ele guarda a ultima
        conversa que abrimos, e o WhatsApp pode ter voltado para a tela
        inicial no meio do caminho (reconexao, clique perdido, recarga). O
        bot seguia achando que estava na conversa e enviava dali.

        O estrago disso apareceu inteiro no diagnostico do operador: o anexo
        caiu no input de DOCUMENTO da tela inicial ("Enviar documento") --
        por isso o resultado chegava como arquivo -- e a citacao nao achava a
        mensagem, porque nao havia conversa nenhuma na tela.
        """
        if self._page is None:
            return False

        # `#main` so' existe com uma conversa aberta. E' o sinal, nao o
        # `_current_chat` que guardamos.
        # `#main` so' existe com uma conversa aberta. Sem ele nao ha' o que
        # verificar nem para onde enviar -- e enviar da tela errada manda os
        # dados do cliente para outra conversa.
        try:
            tem_conversa = self._page.locator("#main").count() > 0
        except (PlaywrightTimeout, PlaywrightError):
            tem_conversa = False

        if tem_conversa:
            aberta = ""
            try:
                aberta = self._nome_no_cabecalho()
            except (PlaywrightTimeout, PlaywrightError):
                aberta = ""
            if aberta and _normalizar(aberta) == _normalizar(chat_name):
                self._current_chat = chat_name
                return True

        # Nao esta' na conversa certa: esquecer o que achavamos e reabrir.
        self._current_chat = ""
        if self._open_chat(chat_name):
            return True
        self._log(
            "ERROR",
            f"Não consegui abrir '{chat_name}' para responder. A mensagem "
            "sairia da tela errada, então não vou enviar.",
        )
        return False

    def _garantir_composer(self, chat_name: str) -> bool:
        """Deixa o campo de digitacao alcancavel, ou diz que nao conseguiu.

        Este e' o erro que mais apareceu em producao: "Timeout 15000ms
        waiting for footer div[contenteditable]". A tentativa de citar abre
        menus, e quando alguma via falha sobra um painel por cima do campo --
        e a resposta, que ja' estava pronta, nao sai. Enviar importa mais do
        que citar, entao aqui a tela e' recuperada em tres degraus antes de
        desistir.
        """
        # Esta função roda em caminhos de recuperação: o navegador pode já ter
        # caído. Levantar aqui trocaria um erro claro por um obscuro.
        if self._page is None:
            return False

        # 0. A pagina certa? Se `self._page` virou uma about:blank, nenhum dos
        # degraus seguintes adianta -- nao ha' composer numa pagina em branco.
        try:
            if "web.whatsapp.com" not in (self._page.url or ""):
                correta = self._escolher_pagina()
                if correta is not self._page:
                    self._page = correta
                    self._page.set_default_timeout(20_000)
                    self._log("WARNING",
                              "O navegador estava numa aba em branco; voltei "
                              "para a aba do WhatsApp.")
                if "web.whatsapp.com" not in (self._page.url or ""):
                    self._page.goto("https://web.whatsapp.com",
                                    wait_until="domcontentloaded", timeout=30_000)
                self._fechar_abas_extras()
                self._current_chat = ""
        except (PlaywrightTimeout, PlaywrightError):
            pass

        alvo = self._page.locator(COMPOSER).last

        # 1. Ja' esta' bom?
        try:
            if alvo.count() and alvo.is_visible():
                return True
        except (PlaywrightTimeout, PlaywrightError):
            pass

        # 2. Fechar o que estiver por cima.
        self._limpar_ui()
        try:
            if alvo.count() and alvo.is_visible():
                self._log("INFO", "Campo de digitação recuperado fechando menus abertos.")
                return True
        except (PlaywrightTimeout, PlaywrightError):
            pass

        # 3. Reabrir a conversa: derruba qualquer estado preso.
        destino = chat_name or self.group_name
        if destino:
            self._current_chat = ""
            if self._open_chat(destino):
                try:
                    self._page.wait_for_selector(COMPOSER, timeout=8_000)
                    self._log("INFO", f"Campo de digitação recuperado reabrindo '{destino}'.")
                    return True
                except (PlaywrightTimeout, PlaywrightError):
                    pass

        self._log(
            "ERROR",
            "Não consegui alcançar o campo de digitação nem depois de fechar "
            "os menus e reabrir a conversa.",
        )
        return False

    def _do_send(self, chat_id: str, chat_name: str, text: str, quote_message_id: str,
                 texto_sem_citacao: str = "") -> ResultadoEnvio:
        if self._page is None or not self.status.connected:
            raise RuntimeError("WhatsApp não está conectado")

        target = chat_name or self.group_name
        if target and not self._garantir_conversa(target):
            raise RuntimeError(f"não consegui abrir a conversa '{target}' para responder")

        citou = self._citar(quote_message_id, "texto")
        # Sem citacao a resposta fica solta no grupo: e' a versao com o nome
        # do consultor que diz de quem ela e'.
        if not citou and texto_sem_citacao:
            text = texto_sem_citacao

        # Depois de mexer nos menus da citacao, a tela pode ter ficado com
        # algo por cima. Recuperar ANTES de tentar digitar.
        if not self._garantir_composer(chat_name):
            raise RuntimeError("campo de digitação indisponível")

        box = self._page.locator(COMPOSER).last
        box.click(timeout=15_000)
        # insert_text preserva as quebras de linha sem que o Enter envie a
        # mensagem no meio do texto.
        self._page.keyboard.insert_text(text)
        self._page.wait_for_timeout(120)
        entregue = ResultadoEnvio(
            ok=True, via="texto", quoted_ok=citou == QuoteStatus.OK,
            tipo_midia="nenhum", provider="dom",
            quote_status=self._situacao_da_citacao(quote_message_id, citou),
            # A PROVA de qual mensagem foi citada. Sem ela ninguem consegue
            # auditar depois: em producao, 25 saidas gravadas e zero com o id.
            quoted_message_id=self._alvo_citado)
        # A partir do Enter o texto pode ter saido: falha daqui em diante nao
        # e' "nao enviou" -- e' EnvioSemProva, salvo se o texto estiver no chat.
        try:
            self._page.keyboard.press("Enter")
            self._page.wait_for_timeout(450)
        except Exception as exc:  # noqa: BLE001
            if self._ja_esta_no_chat(text):
                self._lembrar_do_que_enviamos(text)
                return entregue
            raise EnvioSemProva(
                f"falha depois de enviar o texto ({_short(exc)}); ele pode ter saído") from exc
        self._lembrar_do_que_enviamos(text)
        return entregue

    def _situacao_da_citacao(self, quote_message_id: str, citou) -> str:
        """Traduz o que a tela provou para o vocabulario comum de evidencia.

        ``citou`` chega como o status de ``_citar`` (``ok``/``unverified``/
        vazio). Aceita booleano tambem, para nao quebrar quem ainda passa o
        valor antigo -- mas o caminho de producao usa o status.
        """
        if not (self.reply_quote and quote_message_id):
            return QuoteStatus.NONE
        if citou is True:
            return QuoteStatus.OK
        if not citou:
            return QuoteStatus.FALLBACK
        return str(citou)

    def _citar(self, message_id: str, tipo: str) -> str:
        """Cita a mensagem e REGISTRA quando nao consegue.

        A citacao e' o que liga a resposta ao pedido dentro de um grupo com
        varios consultores falando ao mesmo tempo. Ela falhar nao justifica
        segurar a resposta, mas falhar CALADA impede de descobrir que o menu
        do WhatsApp mudou de novo: o consultor recebe o resultado solto e
        ninguem fica sabendo.
        """
        self._ultima_citacao_ok = False
        self._alvo_citado = ""
        if not (self.reply_quote and message_id):
            return ""
        if self._try_quote(message_id):
            self._fechar_encaminhamento()
            # Clicar em "Responder" nao prova que a citacao pegou. Confirmar
            # a barra no rodape e' o que separa "achei o menu" de "a resposta
            # vai sair ancorada".
            situacao = self._conferir_citacao(message_id)
            if situacao:
                # O ALVO fica registrado mesmo quando a prova e' fraca: e' ele
                # que a conferencia de integridade compara com a origem
                # gravada, e sem ele ninguem audita nada depois.
                self._alvo_citado = message_id
                self._ultima_citacao_ok = situacao == QuoteStatus.OK
                return situacao
            self._log(
                "WARNING",
                f"Cliquei em Responder ({tipo}) mas a barra de citação não "
                "apareceu. Enviando sem a citação.",
            )
            self._limpar_preview()
            return ""
        self._log(
            "WARNING",
            f"Não consegui citar a mensagem original ao responder ({tipo}); "
            "enviando sem a citação. Se isto se repetir, o menu 'Responder' "
            "do WhatsApp Web provavelmente mudou.",
        )
        return ""

    # Itens "Responder"/"Reply" do menu de contexto da mensagem.
    _ITEM_RESPONDER = (
        'li:has-text("Responder")',
        'div[role="button"]:has-text("Responder")',
        'div[role="menuitem"]:has-text("Responder")',
        'li:has-text("Reply")',
        'div[role="button"]:has-text("Reply")',
        'div[role="menuitem"]:has-text("Reply")',
        '[aria-label="Responder"]', '[aria-label="Reply"]',
        'button:has-text("Responder")', 'button:has-text("Reply")',
    )

    # Textos aceitos para o item de resposta, em pt e en.
    _TEXTOS_RESPONDER = ("responder", "reply")

    def _fechar_encaminhamento(self) -> bool:
        """Fecha o dialogo de encaminhar, se ele tiver aberto. Devolve se estava.

        Rede de seguranca: encaminhar manda os dados do cliente para outra
        conversa. Se esse dialogo aparecer em qualquer ponto do fluxo de
        resposta, e' erro nosso -- fechar e registrar, nunca seguir em frente.
        """
        # Sem pagina nao ha' dialogo aberto. A rede de seguranca nao pode
        # levantar excecao propria: ela roda justamente nos caminhos de falha,
        # inclusive com o navegador ja' derrubado.
        if self._page is None:
            return False
        try:
            aberto = self._page.locator(
                'div[role="dialog"]:has-text("Selecionar conversas"), '
                'div[role="dialog"]:has-text("Forward to"), '
                'header:has-text("Selecionar conversas")'
            ).first
            if aberto.count() == 0:
                return False
            self._log(
                "ERROR",
                "A tela de ENCAMINHAR abriu durante a resposta. Fechando sem "
                "enviar: encaminhar mandaria os dados do cliente para outra "
                "conversa.",
            )
            for _ in range(3):
                self._page.keyboard.press("Escape")
                self._page.wait_for_timeout(200)
            return True
        except (PlaywrightTimeout, PlaywrightError):
            return False

    def _marcas_da_mensagem(self, message_id: str) -> dict:
        """Autor e corpo da mensagem original, para reconhecer a barra.

        Colhidas ANTES de o menu abrir e guardadas. No PASSO 5 a linha ja'
        pode ter saido do DOM -- numa enxurrada de cinquenta mensagens ela
        sai -- e ai' nao haveria com o que comparar. A versao anterior lia
        neste ponto: quando a leitura vinha vazia, a conferencia respondia
        "nao ativa" a uma citacao perfeita.
        """
        if self._page is None:
            return {}
        try:
            return self._page.evaluate(MARCAS_DA_MENSAGEM_JS, message_id) or {}
        except (PlaywrightTimeout, PlaywrightError):
            return {}

    def _citacao_confirmada(self, message_id: str, espera: float = 2.0) -> bool:
        """A barra de citacao esta' armada com a mensagem certa? (booleano)

        Mantida para quem so' precisa de sim/nao. Quem grava evidencia usa
        ``_conferir_citacao``, que separa "provei" de "nao consigo provar".
        """
        return self._conferir_citacao(message_id, espera) == QuoteStatus.OK

    def _conferir_citacao(self, message_id: str, espera: float = 2.0) -> str:
        """O que da' para PROVAR sobre a barra de citacao no rodape.

        * ``ok``         -- a barra esta' armada e o texto so' pode ser o
          desta mensagem (nenhuma outra igual na tela);
        * ``unverified`` -- a barra esta' armada e bate, mas ha' outra
          mensagem com o MESMO texto visivel: a prova textual nao distingue
          as duas. A resposta sai citada (o WhatsApp citou alguma), e o
          sistema nao afirma qual;
        * ``""``         -- nao ha' barra, ou ela e' de outra mensagem.

        O caso do meio nao e' teorico: no grupo deste bot o mesmo cliente se
        repete o tempo todo -- sete pares de solicitacoes num dia. Chamar
        aquilo de ``ok`` era afirmar sem prova.
        """
        if self._page is None:
            return ""
        marcas = self._marcas_da_citacao or self._marcas_da_mensagem(message_id)
        if marcas.get("achou"):
            self._marcas_da_citacao = marcas
        limite = time.monotonic() + espera
        ultimo: dict = {}
        while time.monotonic() < limite:
            try:
                ultimo = self._page.evaluate(CITACAO_ATIVA_JS, marcas) or {}
            except (PlaywrightTimeout, PlaywrightError):
                return ""
            if ultimo.get("ativa"):
                if self._alvo_ambiguo:
                    self._log("WARNING",
                              "A citação foi armada, mas há outra mensagem com o "
                              "MESMO texto na tela: não dá para provar qual delas o "
                              "WhatsApp citou. Registrando como não confirmada.")
                    return QuoteStatus.UNVERIFIED
                return QuoteStatus.OK
            self._page.wait_for_timeout(150)
        if ultimo:
            # O PORQUE, e nao so' o fato. "Rodape sem a citacao esperada"
            # sozinho custou rodadas de investigacao: ele nao dizia se a
            # barra existia, o que ela mostrava, nem contra o que foi
            # comparada.
            self._log(
                "INFO",
                f"Rodapé sem a citação esperada: barra={ultimo.get('temBarra')} "
                f"mostra={ultimo.get('rodape')!r} "
                f"prévia={ultimo.get('previa')!r} "
                f"esperado={(marcas.get('corpo') or '')[:40]!r} "
                f"({ultimo.get('porque')})")
        return ""

    def _texto_da_mensagem(self, message_id: str) -> str:
        """Corpo da mensagem original, para reconhecer a citacao certa.

        O CORPO, nao a linha: a barra de citacao mostra so' o texto, sem o
        nome do autor e sem o horario. Comparar com a linha inteira falhava
        nas mensagens curtas -- em "Ryan AMEN JESUS 00:47" o horario cabe
        dentro do trecho comparado, e o bot descartava uma citacao que tinha
        funcionado.
        """
        if self._page is None:
            return ""
        try:
            return (self._page.evaluate(TEXTO_DA_MENSAGEM_JS, message_id) or "").strip()
        except (PlaywrightTimeout, PlaywrightError):
            return ""

    # ==================================================================
    #  CITACAO — os seis passos do caminho manual
    # ==================================================================

    #: Quanto esperar a setinha aparecer depois do hover. Ela e' montada no
    #: `mouseenter`; procurar no mesmo instante encontra a bolha sem botao.
    _ESPERA_DA_SETINHA = 2.5
    #: Quanto esperar o menu abrir depois do clique.
    _ESPERA_DO_MENU = 2.5
    #: Quanto esperar a barra de citacao aparecer no rodape (PASSO 5).
    _ESPERA_DA_BARRA = 2.0

    #: Textos aceitos para o item de resposta. Igualdade exata, em pt e en.
    _TEXTO_RESPONDER = ("responder", "reply")

    def _try_quote(self, message_id: str) -> bool:
        """Cita a mensagem imitando o que uma pessoa faz na mao.

        Devolve True so' quando a barra de citacao foi CONFIRMADA no rodape.
        Clicar em "Responder" nao e' prova: ja' aconteceu de o clique cair no
        item errado e o envio sair solto sem ninguem perceber.
        """
        if self._page is None:
            return False

        # A citacao nao tem chance numa aba em branco.
        try:
            if "web.whatsapp.com" not in (self._page.url or ""):
                self._garantir_composer(self.group_name)
        except (PlaywrightTimeout, PlaywrightError):
            pass

        self._estado_da_citacao = None
        self._marcas_da_citacao = {}
        #: O alvo tinha irmao identico na tela? Decide entre `ok` e
        #: `unverified` -- ver `_conferir_citacao`.
        self._alvo_ambiguo = False
        #: O id que a citacao mirou nesta tentativa. Vai para a evidencia.
        self._alvo_citado = ""
        try:
            return self._passos_da_citacao(message_id)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            self._diagnosticar_citacao_uma_vez(
                message_id, f"o navegador recusou a acao: {_short(exc)}")
            return False

    def _passos_da_citacao(self, message_id: str) -> bool:
        pagina = self._page

        # PASSO 0 — comecar de uma tela limpa.
        #
        # Uma citacao pendurada de um envio anterior faria esta resposta sair
        # grudada na mensagem ERRADA. E enquanto ela estiver la', a
        # verificacao do PASSO 5 acha que citamos sem termos citado.
        #
        # MAS: a citacao pendurada pode ser JUSTAMENTE a que queremos. Foi o
        # que aconteceu no REQ000008 -- a tentativa de imagem armou a citacao
        # certa, a legenda falhou, e o texto entrou cancelando a barra boa
        # para refaze-la do zero. A segunda tentativa nao pegou, e a resposta
        # saiu sem citar uma mensagem que ja' estava citada.
        if self._citacao_confirmada(message_id, espera=0.5):
            self._log("INFO", "A citação da mensagem certa já estava armada no "
                              "compositor; aproveitei em vez de refazer.")
            self._ultima_via_de_citacao = "reaproveitada"
            return True
        self._cancelar_citacao_pendente()

        # -------------------------------------------------- PASSO 1: a linha
        linha = pagina.evaluate(
            GEOMETRIA_DA_LINHA_JS,
            [message_id, self._texto_da_mensagem(message_id)]) or {}
        if not linha.get("achou"):
            self._capturar_estado(message_id, "PASSO 1: achar a linha")
            self._diagnosticar_citacao_uma_vez(
                message_id, linha.get("motivo") or "nao localizei a linha")
            return False
        # O scrollIntoView do PASSO 1 mexe na pagina; reler a geometria para o
        # mouse ir ao lugar certo, e nao a onde a linha estava antes de rolar.
        pagina.wait_for_timeout(250)
        linha = pagina.evaluate(
            GEOMETRIA_DA_LINHA_JS,
            [message_id, self._texto_da_mensagem(message_id)]) or {}
        if not linha.get("achou"):
            self._diagnosticar_citacao_uma_vez(message_id, "a linha sumiu ao rolar")
            return False

        # A linha marcada tem de ser a que pedimos -- o JS devolve o id que
        # ele realmente marcou. Divergiu, nao cita: melhor sem citacao do que
        # pendurada na mensagem de outro consultor.
        marcado = str(linha.get("dataId") or "")
        if marcado and marcado != message_id:
            self._log("ERROR",
                      f"QUOTE_ID_MISMATCH na tela: pedi {message_id} e a linha "
                      f"marcada e' {marcado}. Nao vou citar.")
            self._limpar_ui()
            return False
        # Irmao identico na tela: da' para citar, nao da' para PROVAR qual.
        self._alvo_ambiguo = int(linha.get("iguais") or 1) > 1
        if self._alvo_ambiguo:
            self._log("INFO",
                      f"Ha' {linha.get('iguais')} mensagens com o mesmo texto na "
                      "tela; a citacao vai sair, mas sem prova de qual delas.")

        # AQUI, com a linha na mao: guardar autor e corpo para o PASSO 5.
        #
        # Depois do clique em "Responder" a conversa pode ter rolado e a linha
        # ter saido do DOM. Ler as marcas la' devolvia vazio, e a conferencia
        # reprovava uma citacao que tinha funcionado.
        marcas = self._marcas_da_mensagem(message_id)
        if marcas.get("achou"):
            self._marcas_da_citacao = marcas

        # ------------------------------------ PASSO 2: hover, e nao sair dali
        #
        # `mouse.move` em coordenada, e nao `locator.hover()`: dali em diante
        # o ponteiro NAO pode deixar a bolha, senao a setinha e' desmontada.
        pagina.mouse.move(linha["x"], linha["y"])

        # -------------------------------------- PASSOS 3 e 4: menu e item
        #
        # Duas vias, e a ordem importa: a setinha e' o caminho que uma pessoa
        # usa, mas em producao o clique nela abre o menu de forma
        # INTERMITENTE -- o elemento entra animado e o WhatsApp as vezes
        # ignora o clique. O botao direito na bolha e' o caminho estavel.
        #
        # Cada via e' julgada pelo mesmo criterio: o menu tem os ITENS?
        # "Abriu" nao basta -- ja' contamos como aberto um seletor de reacoes
        # de 4x1 pixel.
        tentativas = []
        item = self._abrir_menu_pela_setinha(message_id, linha)
        tentativas.append(f"setinha: {item.get('motivo') or 'achou'}")
        if not item.get("achou"):
            item = self._abrir_menu_pelo_botao_direito(message_id, linha)
            tentativas.append(f"botão direito: {item.get('motivo') or 'achou'}")

        if not item.get("achou"):
            self._capturar_estado(message_id, "PASSO 4: sem item 'Responder'")
            # QUAL via, e o que cada uma respondeu. Sem isso o log dizia
            # "nenhuma via funcionou" e a investigacao comecava do zero.
            self._log(
                "ERROR",
                f"Nenhuma via abriu o menu com 'Responder'. Vias: {tentativas}. "
                f"Itens vistos: {item.get('rotulos')}"
                + (f" (recusei '{item.get('recusado')}')" if item.get("recusado") else ""))
            self._diagnosticar_citacao_uma_vez(
                message_id, "nenhuma via abriu o menu da mensagem")
            self._limpar_ui()
            return False

        # Clicar no ELEMENTO marcado; a coordenada e' o ultimo recurso.
        try:
            loc = pagina.locator(f"[{MARCA_RESPONDER}]").first
            try:
                loc.focus()
                loc.press("Enter")
            except: pass
            
            loc.click(timeout=3_000, force=True)
            try:
                loc.evaluate('el => { let target = el.closest(\'li, [role="menuitem"], [role="button"]\') || el; target.click(); }')
            except:
                pass
        except (PlaywrightTimeout, PlaywrightError):
            pagina.mouse.click(item["x"], item["y"])

        # -------------------------------------- PASSO 5: CONFERIR, sempre
        #
        # Antes de digitar QUALQUER coisa. Uma citacao armada na mensagem
        # errada e' pior que nenhuma: o consultor leria o resultado de outro
        # cliente como se fosse o dele.
        if self._citacao_confirmada(message_id, espera=self._ESPERA_DA_BARRA):
            self._ultima_via_de_citacao = item.get("via") or "?"
            return True

        self._capturar_estado(message_id, "PASSO 5: a barra nao apareceu")
        self._log(
            "WARNING",
            f"Cliquei em '{item.get('rotulo')}' mas a barra de citação não "
            "apareceu no rodapé. Enviando sem a citação.")
        self._limpar_ui()
        return False

    def _cancelar_citacao_pendente(self) -> bool:
        """Tira do compositor uma citacao que sobrou de antes.

        Nao e' higiene, e' correcao: com a barra armada da mensagem A, uma
        resposta a mensagem B sairia grudada em A -- e o consultor leria o
        resultado de outro cliente como se fosse o dele.

        `_limpar_ui` nao resolvia: Escape nao fecha a barra de citacao, e
        pressiona-lo com nada aberto FECHA A CONVERSA. O botao de cancelar
        e' o unico caminho, e ele existe: aria-label="Cancelar", ao lado do
        `[data-testid="quoted-message"]`.
        """
        if self._page is None:
            return False
        try:
            estado = self._page.evaluate(CITACAO_PENDENTE_JS) or {}
        except (PlaywrightTimeout, PlaywrightError):
            return False
        if not estado.get("pendente"):
            return False

        self._log("INFO", "Havia uma citação pendurada no compositor "
                          f"({estado.get('texto')!r}); cancelando antes de citar.")
        if not estado.get("temBotao"):
            self._log("WARNING",
                      "A citação pendurada não tem botão de cancelar. "
                      f"Rótulos por perto: {estado.get('rotulosPerto')}")
            return False
        try:
            self._page.mouse.click(estado["x"], estado["y"])
            self._page.wait_for_timeout(300)
        except (PlaywrightTimeout, PlaywrightError):
            return False
        return not (self._page.evaluate(CITACAO_PENDENTE_JS) or {}).get("pendente")

    def _abrir_menu_pela_setinha(self, message_id: str, linha: dict) -> dict:
        """Via 1: a setinha de contexto, como uma pessoa faria.

        Devolve o resultado de ``ITEM_RESPONDER_JS``. ``achou=False`` quando
        esta via nao produziu um menu com "Responder" -- e aí o chamador tenta
        a proxima, sem alarde: a setinha falhar e' comum e nao e' defeito.
        """
        # Reancorar e passar o mouse DE NOVO: entre o hover do PASSO 2 e este
        # ponto a conversa pode ter rolado, e a setinha so' existe enquanto o
        # ponteiro esta' sobre a bolha certa.
        atual = self._reancorar(message_id)
        if not atual.get("achou"):
            return {"achou": False, "via": "setinha", "rotulos": [],
                    "motivo": f"a mensagem saiu da tela: {atual.get('motivo')}"}
        try:
            self._page.mouse.move(atual["x"], atual["y"])
        except (PlaywrightTimeout, PlaywrightError):
            pass

        botao = self._esperar_a_setinha(message_id)
        if not botao.get("achou"):
            return {"achou": False, "via": "setinha", "rotulos": [],
                    "motivo": f"setinha ausente: {botao.get('motivo')}"}

        # Clicar no ELEMENTO, nunca na coordenada: a setinha entra animada, e
        # o clique num ponto medido meio segundo antes cai onde ela nao esta'
        # mais. `locator.click()` espera o retangulo ficar estavel.
        try:
            self._page.locator(f"[{MARCA_SETINHA}]").first.click(timeout=5_000)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            return {"achou": False, "via": "setinha", "rotulos": [],
                    "motivo": f"a setinha não aceitou o clique: {_short(exc)}"}

        return {**self._esperar_os_itens(), "via": "setinha"}

    def _abrir_menu_pelo_botao_direito(self, message_id: str, linha: dict) -> dict:
        """Via 2: botao direito na bolha. E' a via ESTAVEL.

        Nao depende de icone, de aria-label nem de animacao. Em producao o
        clique na setinha abria o menu de forma intermitente -- as vezes o
        WhatsApp simplesmente ignorava -- e era esta via que sempre
        funcionava. Ela e' reserva so' porque a setinha e' o gesto que uma
        pessoa faz; na pratica e' ela que entrega.

        O alvo e' o BALAO, nunca o centro de ``div[role="row"]``: a linha
        ocupa a largura toda e o centro dela cai no fundo vazio, onde o botao
        direito abre o menu do GRUPO ("Adicionar membro", "Fechar conversa").
        """
        # REANCORAR antes de clicar.
        #
        # As coordenadas em `linha` foram medidas antes do hover. Com o grupo
        # despejando cinquenta mensagens em dois minutos -- aconteceu em
        # 01/09 -- a conversa rola e elas passam a apontar para o fundo
        # vazio: o botao direito ali abre o menu do GRUPO ("Adicionar
        # membro", "Sair do grupo"), que foi exatamente o que o log
        # registrou.
        atual = self._reancorar(message_id)
        if not atual.get("achou"):
            return {"achou": False, "via": "botão direito", "rotulos": [],
                    "motivo": f"a mensagem saiu da tela: {atual.get('motivo')}"}

        try:
            # No ELEMENTO, nao na coordenada: o balao esta' marcado e o
            # Playwright reconfere posicao e estabilidade no instante do
            # clique.
            self._page.locator(f"[{MARCA_BALAO}]").first.click(
                button="right", timeout=5_000)
        except (PlaywrightTimeout, PlaywrightError):
            # Ultimo recurso: coordenada -- REMEDIDA agora, nunca a antiga.
            try:
                self._page.mouse.click(atual["x"], atual["y"], button="right")
            except (PlaywrightTimeout, PlaywrightError) as exc:
                return {"achou": False, "via": "botão direito", "rotulos": [],
                        "motivo": f"o botão direito falhou: {_short(exc)}"}
        return {**self._esperar_os_itens(), "via": "botão direito"}

    def _reancorar(self, message_id: str) -> dict:
        """Reencontra a mensagem e devolve a geometria DE AGORA.

        Chamada imediatamente antes de cada interacao. Numa conversa parada
        nao muda nada; numa enxurrada, e' a diferenca entre clicar na
        mensagem e clicar no fundo da tela.
        """
        try:
            return self._page.evaluate(
                GEOMETRIA_DA_LINHA_JS,
                [message_id, self._texto_da_mensagem(message_id)]) or {}
        except (PlaywrightTimeout, PlaywrightError) as exc:
            return {"achou": False, "motivo": _short(exc)}

    def _esperar_a_setinha(self, message_id: str = "") -> dict:
        """PASSO 3: espera o botao de opcoes existir. Sai assim que aparece.

        Nao e' um `sleep` fixo: quando o botao ja' esta' la', volta na hora.
        Quando o teto estoura, o dicionario devolvido traz o que EXISTE na
        linha -- e' essa lista que diz se o botao mudou de rotulo ou se ele
        realmente nao e' renderizado.
        """
        limite = time.monotonic() + self._ESPERA_DA_SETINHA
        resultado: dict = {}
        while True:
            resultado = self._page.evaluate(BOTAO_DE_OPCOES_JS, message_id) or {}
            if resultado.get("achou") or time.monotonic() >= limite:
                return resultado
            self._page.wait_for_timeout(120)

    def _esperar_o_menu(self) -> bool:
        """O menu de contexto existe na tela?"""
        limite = time.monotonic() + self._ESPERA_DO_MENU
        while True:
            if self._page.evaluate(MENU_ABERTO_JS):
                return True
            if time.monotonic() >= limite:
                return False
            self._page.wait_for_timeout(120)

    def _esperar_os_itens(self) -> dict:
        """Espera o menu MONTAR os itens, nao so' existir.

        Sao duas coisas diferentes, e confundi-las custou uma rodada: o
        container do menu entra na tela antes do conteudo, entao havia um
        instante em que "o menu abriu" era verdade e a lista de itens vinha
        vazia. O log dizia "o menu abriu mas nao achei 'Responder'. Itens na
        tela: []" -- e a leitura obvia disso ("o item mudou de nome") estava
        errada: nao havia item nenhum ainda.

        Sai assim que houver itens; nao e' um `sleep`.
        """
        limite = time.monotonic() + self._ESPERA_DO_MENU
        ultimo: dict = {}
        while True:
            try:
                ultimo = self._page.evaluate(
                    ITEM_RESPONDER_JS, list(self._TEXTO_RESPONDER)) or {}
            except (PlaywrightTimeout, PlaywrightError):
                return {}
            # Achou, ou pelo menos ja' ha' itens para julgar.
            if ultimo.get("achou") or ultimo.get("rotulos"):
                return ultimo
            if time.monotonic() >= limite:
                return ultimo
            self._page.wait_for_timeout(120)

    def _diagnosticar_citacao_uma_vez(self, message_id: str, motivo: str) -> None:
        """Garante que TODO caminho de falha produza evidencia, uma vez so'.

        A versao anterior tinha o diagnostico so' no fim: quando a falha era
        "nao localizei a mensagem", a funcao voltava antes e nao registrava
        nada. O sintoma era citacao que nunca funcionava e log que nunca
        explicava.
        """
        if self._citacao_diagnosticada:
            return
        self._citacao_diagnosticada = True
        self._log("WARNING", f"Citação falhou porque {motivo}. Coletando diagnóstico...")
        self._diagnosticar_citacao(message_id)

    def _diagnosticar_citacao(self, message_id: str) -> None:
        """Descreve a tela NO MOMENTO da falha. Sem tocar em nada.

        A versao anterior fazia a pior coisa que um diagnostico pode fazer:
        antes de olhar, ela apertava **Escape**. Com nada aberto, Escape fecha
        a conversa no WhatsApp Web -- entao ela destruia a tela e descrevia os
        escombros. O log de producao dizia

            linha_encontrada=False icones=[] rotulos=[] botoes=[] menus=[]

        e eu passei rodadas lendo isso como "a mensagem sumiu da tela", quando
        era o proprio diagnostico que tinha acabado de fechar a conversa. Pior:
        em producao isso acontecia no meio do envio, obrigando o passo seguinte
        a reabrir a conversa.

        Agora a coleta e' feita pelo chamador, NO ponto exato da falha
        (``_capturar_estado``), e esta funcao so' formata. Ela nao clica, nao
        pressiona tecla e nao rola nada.
        """
        d = self._estado_da_citacao or {}
        if not d:
            # Ultima chance, ainda sem tocar na tela: ler o que houver.
            try:
                d = self._page.evaluate(DIAGNOSTICO_CITACAO_JS, message_id) or {}
            except Exception as exc:
                self._log("WARNING", f"Diagnóstico da citação falhou: {_short(exc)}")
                return

        self._log(
            "ERROR",
            f"Citação falhou em: {d.get('onde') or '?'}. A tela tinha: "
            f"linha_no_dom={d.get('achou_linha')} "
            f"ícones={d.get('icones')} "
            f"rótulos={d.get('rotulos')} "
            f"botões={d.get('botoes')} "
            f"itens_de_menu={d.get('parecidos')} "
            f"menus={d.get('menus')} "
            f"url={(self._page.url or '')[:60] if self._page else '(sem página)'}",
        )

    def _capturar_estado(self, message_id: str, onde: str) -> None:
        """Fotografa a tela no ponto da falha, antes de qualquer limpeza.

        Chamada de dentro dos passos da citacao. Guardar aqui e' o que separa
        "o menu nao abriu" de "a tela ja' estava vazia quando chegamos" -- e
        essa diferenca decide o conserto.
        """
        if self._estado_da_citacao is not None:
            return
        try:
            estado = self._page.evaluate(DIAGNOSTICO_CITACAO_JS, message_id) or {}
        except Exception:
            estado = {}
        estado["onde"] = onde
        self._estado_da_citacao = estado

    def _do_render_png(self, html: str, path: str, width: int) -> str:
        if self._context is None:
            raise RuntimeError("navegador indisponível para renderizar a imagem")
        destino = Path(path)
        destino.parent.mkdir(parents=True, exist_ok=True)

        pagina = self._context.new_page()
        try:
            # deviceScaleFactor 2 deixa o texto nitido quando o WhatsApp
            # reamostra a imagem no celular.
            pagina.set_viewport_size({"width": width, "height": 600})
            pagina.emulate_media(media="screen")
            pagina.set_content(html, wait_until="load")
            pagina.wait_for_timeout(120)
            alvo = pagina.locator("body > .folha").first
            if alvo.count():
                alvo.screenshot(path=str(destino), scale="css", type="png")
            else:
                pagina.screenshot(path=str(destino), full_page=True, type="png")
        finally:
            try:
                pagina.close()
            except Exception:
                # Se o fechamento falhar a aba fica viva e vira candidata a
                # `pages[0]` no proximo boot. A varredura resolve.
                pass
            try:
                self._fechar_abas_extras()
            except Exception:
                pass
        return str(destino)

    def _limpar_ui(self) -> None:
        """Fecha menus e pré-visualizações que tenham ficado abertos.

        Sem isto, uma falha no envio da imagem deixava o menu de anexo ou a
        tela de mídia por cima do campo de digitação — e a queda para texto,
        que existe justamente para o consultor nunca ficar sem resposta,
        falhava também. A rede de segurança não pode depender de o caminho
        que falhou ter deixado a tela limpa.
        """
        # Um diálogo de encaminhar aberto por engano fica por cima de tudo.
        self._fechar_encaminhamento()
        # E a citação pendurada, que Escape não fecha.
        self._cancelar_citacao_pendente()
        if self._page is None:
            return
        # Mesma regra: so' pressionar Escape se houver o que fechar.
        for _ in range(3):
            try:
                if not self._page.evaluate(TEM_O_QUE_FECHAR_JS):
                    break
                self._page.keyboard.press("Escape")
                self._page.wait_for_timeout(180)
            except (PlaywrightTimeout, PlaywrightError):
                break
        try:
            # Confirma que o campo de digitação voltou a ser alcançável.
            self._page.wait_for_selector(COMPOSER, timeout=5_000)
        except Exception:
            pass

    # Orcamento de tempo do envio inteiro. Estourar sem teto deixava a fila
    # parada e o consultor sem resposta -- e a tela num estado que contaminava
    # o proximo envio.
    _ORCAMENTO_DE_ENVIO = 45.0

    def _do_send_image(self, chat_id: str, chat_name: str, image_path: str,
                       caption: str, quote_message_id: str,
                       caption_sem_citacao: str = "") -> ResultadoEnvio:
        comeco = time.monotonic()
        try:
            resultado = self._enviar_imagem(chat_id, chat_name, image_path, caption,
                                            quote_message_id, caption_sem_citacao)
            decorrido = time.monotonic() - comeco
            # "imagem ok" com a legenda faltando era meia verdade, e foi ela
            # que escondeu o defeito por dois dias: o log dizia entregue, o
            # card chegava mudo, e ninguem ligou uma coisa na outra.
            self._log(
                "WARNING" if getattr(resultado, "parcial", False) else "INFO",
                f"{Path(image_path).stem} "
                f"{'ENTREGA PARCIAL' if getattr(resultado, 'parcial', False) else 'entregue'} "
                f"via {self._ultima_via_de_envio or '?'} "
                f"(citação {self._ultima_via_de_citacao if self._ultima_citacao_ok else 'não'}, "
                f"legenda {'ok' if getattr(resultado, 'legenda_ok', None) is not False else 'FALTANDO'}) "
                f"em {decorrido:.1f}s",
            )
            return resultado
        except Exception as exc:
            decorrido = time.monotonic() - comeco
            if decorrido >= self._ORCAMENTO_DE_ENVIO:
                self._log(
                    "ERROR",
                    f"{Path(image_path).stem} não entregue — estourou {decorrido:.0f}s "
                    f"no envio da imagem: {_short(exc)}",
                )
            # Qualquer falha: devolve a tela ao estado utilizável ANTES de
            # propagar, para que a resposta em texto ainda consiga sair.
            self._limpar_ui()
            raise

    def _enviar_imagem(self, chat_id: str, chat_name: str, image_path: str,
                       caption: str, quote_message_id: str,
                       caption_sem_citacao: str = "") -> ResultadoEnvio:
        if self._page is None or not self.status.connected:
            raise RuntimeError("WhatsApp não está conectado")
        if not Path(image_path).exists():
            raise FileNotFoundError(f"imagem não encontrada: {image_path}")

        target = chat_name or self.group_name
        if target and not self._garantir_conversa(target):
            raise RuntimeError(f"não consegui abrir a conversa '{target}' para responder")

        citou = self._citar(quote_message_id, "imagem")
        if not citou and caption_sem_citacao:
            caption = caption_sem_citacao

        # A citacao mexeu na tela (menus, Escape). Reconferir a conversa ANTES
        # de anexar: em producao ela chegou a fechar entre uma coisa e outra,
        # e o anexo passou a ver so' o input de documento da tela inicial.
        if target and not self._garantir_conversa(target):
            raise RuntimeError(
                f"a conversa '{target}' fechou durante a citação; não vou anexar")

        anexou, via, evidencia = self._anexar_imagem(image_path)
        if not anexou:
            # Nao mandar como documento "porque pelo menos chega": o consultor
            # precisa ver o valor na conversa para repassar ao cliente. Cair
            # para texto entrega isso; um card de download, nao.
            raise RuntimeError(
                "não consegui anexar a imagem como foto "
                f"(accept vistos: {evidencia.get('accept_vistos')})"
            )
        self._ultima_via_de_envio = via

        # O preview ja' foi confirmado como imagem em _anexar_imagem; este
        # wait cobre a montagem do restante da tela.
        # `media-caption-input-container` saiu: o laboratorio contou ZERO
        # ocorrencias dele nesta versao do WhatsApp. Esperar por um seletor
        # que nunca existe so' gasta o timeout do proximo da lista.
        self._page.wait_for_selector(
            'span[data-icon="send"], div[aria-label="Enviar"], '
            'span[data-icon="wds-ic-send-filled"], '
            'div[aria-label="Enviar 1 item selecionado"]',
            timeout=25_000,
        )
        self._page.wait_for_timeout(400)

        legenda_ok = None
        if caption:
            legenda_ok = self._digitar_legenda(caption)
            if not legenda_ok:
                # Card mudo e' pior que resposta em texto: sem a legenda vao
                # embora o valor escrito, o nome do cliente e o _REQ, que e' a
                # chave de busca no painel e uma das assinaturas anti-laco.
                # Cancelar aqui faz o chamador cair para o texto, que entrega
                # tudo isso.
                self._limpar_preview()
                raise RuntimeError(
                    "não consegui escrever a legenda; não vou enviar o card mudo")

        entregue = ResultadoEnvio(
            ok=True, via=self._ultima_via_de_envio or "imagem",
            quoted_ok=citou == QuoteStatus.OK, tipo_midia="imagem",
            legenda_ok=legenda_ok, provider="dom",
            quote_status=self._situacao_da_citacao(quote_message_id, citou),
            # Qual mensagem foi citada -- a prova que faltava na saida.
            quoted_message_id=self._alvo_citado)
        # DAQUI EM DIANTE a imagem pode ter saido. Qualquer falha que nao
        # PROVE o contrario (a pre-visualizacao aberta prova) vira
        # EnvioSemProva: o manager nao manda o texto por cima.
        try:
            return self._disparar_e_confirmar_imagem(image_path, caption, entregue)
        except EnvioNaoSaiu:
            raise
        except Exception as exc:  # noqa: BLE001 - qualquer falha depois do clique
            if caption and self._ja_esta_no_chat(caption):
                self._log("INFO", "Falha depois de enviar a imagem, mas a legenda está "
                                  "no chat. Não vou reenviar em texto.")
                self._lembrar_do_que_enviamos(caption)
                return entregue
            raise EnvioSemProva(
                f"falha depois de disparar o envio da imagem ({_short(exc)}); "
                "a imagem pode ter saído") from exc

    def _disparar_e_confirmar_imagem(self, image_path: str, caption: str,
                                     entregue: ResultadoEnvio) -> ResultadoEnvio:
        self._disparar_envio()

        # Confirma que a pre-visualizacao fechou - se ela continuar na tela, o
        # envio nao aconteceu e o chamador precisa cair para o texto.
        try:
            self._page.wait_for_selector('span[data-icon="send"]', state="detached", timeout=20_000)
        except PlaywrightTimeout:
            # O botao de enviar sumir e' um SINAL de que saiu, nao a prova.
            # Se o sinal falha mas a imagem foi mesmo enviada, levantar aqui
            # faz o chamador cair para o texto -- e o consultor recebe a
            # imagem E o texto, duplicado. Antes de desistir, procurar a
            # legenda no chat: se ela esta' la', o envio aconteceu.
            if caption and self._ja_esta_no_chat(caption):
                self._log(
                    "INFO",
                    "A confirmação do envio da imagem falhou, mas a mensagem "
                    "está no chat. Não vou reenviar em texto.",
                )
                self._lembrar_do_que_enviamos(caption)
                return entregue
            raise EnvioNaoSaiu("a pré-visualização da imagem não fechou; envio não confirmado")
        self._page.wait_for_timeout(400)
        # A legenda tambem e' mensagem nossa: registrar para nao rele-la no
        # ciclo seguinte e responder a si mesmo.
        self._lembrar_do_que_enviamos(caption)

        # PROVA depois de enviar: a ultima mensagem nossa e' mesmo imagem?
        #
        # Sem esta leitura, "enviei" significava apenas "cliquei em enviar" --
        # e foi assim que um card de download passou por entregue.
        saida = self._conferir_ultima_saida()
        if saida is not None and not saida.get("temImagem"):
            if saida.get("pareceDocumento"):
                self._log(
                    "WARNING",
                    f"{Path(image_path).stem} saiu como DOCUMENTO, não como "
                    "imagem. O consultor precisa baixar o arquivo para ver.",
                )
            else:
                self._log(
                    "WARNING",
                    "Não consegui confirmar que a última mensagem é imagem "
                    f"(ícones: {saida.get('icones')}).",
                )
        return entregue

    #: O rotulo do campo de legenda, sem o sufixo do compositor.
    _ROTULOS_DA_LEGENDA = ("Digite uma mensagem", "Type a message",
                           "Add a caption", "Adicionar legenda")

    def _digitar_legenda(self, caption: str) -> bool:
        """Escreve a legenda no campo do preview e CONFERE que ela entrou.

        Duas regras, e as duas nasceram do mesmo defeito:

        1. **Nunca escolher por posicao.** O codigo anterior fazia `.last`
           sobre os contenteditable e acertava o compositor da conversa, que
           fica atras do preview. Aqui o campo e' identificado por
           caracteristica; se nao houver exatamente um, aborta.

        2. **Ler de volta antes de enviar.** Digitar nao prova que entrou --
           foi digitando no campo errado que o card saiu mudo 40 vezes sem
           ninguem perceber.
        """
        escolha = self._page.evaluate(
            CAMPO_DA_LEGENDA_JS, list(self._ROTULOS_DA_LEGENDA)) or {}

        if not escolha.get("achou"):
            self._log(
                "ERROR",
                f"Não identifiquei o campo da legenda ({escolha.get('quantos')} "
                f"candidatos). Campos na tela: {escolha.get('inventario')}",
            )
            return False

        indice = escolha["indice"]
        try:
            # O clique vem primeiro porque e' o que o WhatsApp espera de uma
            # pessoa. Espera CURTA: com o foco por JS como reserva, insistir
            # oito segundos num clique que nao passa so' atrasa a resposta.
            try:
                self._page.locator('[contenteditable="true"]').nth(indice).click(
                    timeout=3_000)
            except (PlaywrightTimeout, PlaywrightError) as exc:
                self._log("INFO", f"O clique na legenda não passou ({_short(exc)}); "
                                  "vou focar o campo direto.")
            # Focar SEMPRE, e conferir. Sem esta prova, `insert_text` escreveria
            # no que estivesse com foco -- inclusive no compositor da conversa,
            # que mandaria a legenda solta para o grupo.
            if not self._page.evaluate(FOCAR_CAMPO_JS, indice):
                self._log("ERROR", "Não consegui pôr o foco no campo da legenda; "
                                   "não vou digitar às cegas.")
                return False
            self._page.keyboard.insert_text(caption)
            self._page.wait_for_timeout(250)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            self._log("ERROR", f"Falha ao escrever a legenda: {_short(exc)}")
            return False

        # A PROVA. Comparar um trecho, nao a string inteira: o campo
        # normaliza espacos e o WhatsApp converte *negrito* na exibicao.
        try:
            escrito = self._page.evaluate(TEXTO_DO_CAMPO_JS, indice) or ""
        except (PlaywrightTimeout, PlaywrightError):
            escrito = ""

        pedaco = "".join(caption.split())[:20]
        if pedaco and pedaco not in "".join(escrito.split()):
            self._log(
                "ERROR",
                f"Cliquei no campo {escolha.get('aria')!r} e digitei, mas a "
                f"legenda não está lá. Campo contém: {escrito[:60]!r}",
            )
            return False
        return True

    def _disparar_envio(self) -> None:
        """O UNICO ponto que faz a mensagem sair. De proposito.

        Ter isto num metodo com nome nao e' cerimonia: e' o que permite ao
        laboratorio rodar o caminho inteiro de producao -- citar, colar,
        digitar a legenda -- e parar exatamente aqui, sem escrever no grupo
        do cliente. Antes disso, testar o fluxo completo significava mandar
        mensagem de teste para consultores de verdade.
        """
        enviar = self._page.locator(
            'span[data-icon="send"], div[aria-label="Enviar"], button[aria-label="Enviar"], '
            'span[data-icon="wds-ic-send-filled"], div[aria-label="Enviar 1 item selecionado"]'
        ).last
        if enviar.count():
            enviar.click(timeout=10_000)
        else:
            self._page.keyboard.press("Enter")

    def _conferir_ultima_saida(self) -> dict | None:
        """Le a ultima mensagem QUE NOS enviamos e diz o que ela e'."""
        if self._page is None:
            return None
        try:
            return self._page.evaluate(ULTIMA_SAIDA_JS)
        except (PlaywrightTimeout, PlaywrightError):
            return None

    # O input de IMAGEM ("Fotos e vídeos") e o de DOCUMENTO sao dois <input
    # type=file> diferentes. Escolher errado e' o que fazia o resultado chegar
    # como um anexo REQ000001.png para baixar, em vez da imagem aberta na
    # conversa -- que e' o ponto de mandar imagem.
    _GATILHOS_ANEXO = (
        'div[title="Anexar"]', 'span[data-icon="attach-menu-plus"]',
        'span[data-icon="clip"]', 'span[data-icon="plus-rounded"]',
        'span[data-icon="plus"]', 'button[title="Anexar"]',
        'button[aria-label*="nexar"]', 'button[aria-label*="ttach"]',
    )

    # Textos do item "Fotos e videos" do menu de anexo, em pt e en. Sao TEXTOS,
    # nao seletores: a busca compara o conteudo do elemento.
    _TEXTOS_FOTOS = (
        "fotos e vídeos", "fotos e videos", "photos & videos",
        "fotos", "photos", "imagem", "image",
    )


    def _anexar_imagem(self, image_path: str) -> tuple[bool, str, dict]:
        """Anexa a imagem como FOTO. Devolve (ok, via, evidencia).

        Tres estrategias, da mais confiavel para a mais fragil:

        1. **Colar** o arquivo no compositor. Dispensa o menu de anexo, que e'
           o passo mais fragil do fluxo, e o WhatsApp sempre trata um File
           image/png colado como imagem.
        2. Menu "Fotos e vídeos" + ``expect_file_chooser``.
        3. O ``<input type=file>`` que aceita imagem -- **nunca** o generico.

        A escolha errada aqui foi o que fez o resultado chegar como
        "REQ000029.png · 57 KB" para baixar. O diagnostico mostrou a causa:
        sem conversa aberta, o unico input disponivel era o de DOCUMENTO da
        tela inicial.
        """
        evidencia: dict = {}

        # Sem conversa aberta nao ha' compositor para colar nem o input de
        # imagem do chat -- so' o de documento da tela inicial, que e'
        # justamente o que nao pode ser usado.
        try:
            if self._page.locator("#main").count() == 0:
                evidencia["sem_conversa"] = True
                self._log("WARNING",
                          "Tentei anexar sem conversa aberta. Não vou usar o "
                          "campo de documento da tela inicial.")
                return False, "", evidencia
        except (PlaywrightTimeout, PlaywrightError):
            pass

        # 1) Colar.
        try:
            b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
            resposta = self._page.evaluate(
                COLAR_IMAGEM_JS, [b64, Path(image_path).name]) or {}
            if resposta.get("ok") and self._esperar_preview():
                return True, "colar", evidencia
            evidencia["colar"] = resposta.get("motivo") or "preview não abriu"
        except (PlaywrightTimeout, PlaywrightError, OSError) as exc:
            evidencia["colar"] = _short(exc)
        self._limpar_preview()

        # 2) O <input> que aceita imagem.
        #
        # Observado no DOM real: com a conversa aberta existe UM UNICO
        # input[type=file], com accept="image/*", e ele ja' esta' no DOM SEM
        # precisar abrir o menu do clipe. Tentar antes do menu tira do caminho
        # feliz o passo mais fragil -- e o menu tem "Documento" como PRIMEIRO
        # item, que abre o seletor nativo do sistema e trava a automacao.
        entrada, aceitos = self._input_de_imagem()
        evidencia["accept_vistos"] = aceitos
        if entrada is not None:
            try:
                entrada.set_input_files(image_path)
                if self._esperar_preview():
                    return True, "input_imagem", evidencia
            except (PlaywrightTimeout, PlaywrightError) as exc:
                evidencia["input_imagem"] = _short(exc)
            self._limpar_preview()

        # 3) Menu "Fotos e vídeos" — ultimo recurso.
        abriu = self._abrir_menu_de_anexo()
        evidencia["menu_abriu"] = abriu
        try:
            achado = self._page.evaluate(ACHAR_ITEM_MENU_JS, list(self._TEXTOS_FOTOS))
        except (PlaywrightTimeout, PlaywrightError):
            achado = ""
        if achado:
            try:
                with self._page.expect_file_chooser(timeout=8_000) as espera:
                    self._page.locator(f"[{MARCA_ITEM}]").first.click(timeout=4_000)
                espera.value.set_files(image_path)
                if self._esperar_preview():
                    return True, "menu_fotos", evidencia
            except (PlaywrightTimeout, PlaywrightError) as exc:
                evidencia["menu_fotos"] = _short(exc)
        self._limpar_preview()

        # Nao ha' mais saida. NUNCA clicar em "Documento": ele abre o seletor
        # nativo do sistema, que trava a automacao, e entrega um card de
        # download ao consultor. Cair para texto e' melhor.
        self._diagnosticar_anexo(abriu)
        return False, "", evidencia

    def _input_de_imagem(self):
        """O ``input[type=file]`` que aceita imagem. Nunca o de documento.

        O WhatsApp mantem varios inputs na pagina ao mesmo tempo. Pegar "o
        primeiro do DOM" costuma pegar o de documento -- e' literalmente o
        defeito B. A escolha e' pelo ``accept``.
        """
        entradas = self._page.locator('input[type="file"]')
        vistos: list[str] = []
        for indice in range(entradas.count()):
            elemento = entradas.nth(indice)
            aceita = (elemento.get_attribute("accept") or "").lower()
            vistos.append(aceita or "(sem accept)")
            if "image" in aceita:
                return elemento, vistos
        return None, vistos

    def _esperar_preview(self, timeout: float = 6.0) -> bool:
        """Espera o preview abrir E prova que o que esta' nele e' imagem.

        Esta prova e' o que impede o defeito de voltar: enviar sem conferir
        foi o que entregou um card de download ao consultor.
        """
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            try:
                estado = self._page.evaluate(PREVIEW_E_IMAGEM_JS) or {}
            except (PlaywrightTimeout, PlaywrightError):
                return False
            if estado.get("temMiniatura"):
                return True
            if estado.get("pareceDocumento"):
                self._log(
                    "WARNING",
                    "O anexo abriu como DOCUMENTO, não como imagem. Cancelando "
                    "para não entregar um arquivo de download ao consultor.",
                )
                return False
            self._page.wait_for_timeout(200)
        return False

    def _escape(self, vezes: int = 1) -> None:
        """Escape de limpeza, melhor-esforco.

        Centraliza o que estava espalhado em varios ``try/except Exception:
        pass``. A diferenca que importa: aqui so' erro do Playwright e'
        engolido. Um ``AttributeError`` ou ``TypeError`` continua subindo --
        esses sao defeito nosso, e engoli-los foi o que ja' escondeu uma
        guarda quebrada em producao.
        """
        if self._page is None:
            return
        for _ in range(max(1, vezes)):
            # Conferir ANTES de cada tecla. Escape sem nada aberto fecha a
            # conversa, e o proximo passo passa a ver a tela inicial.
            try:
                if not self._page.evaluate(TEM_O_QUE_FECHAR_JS):
                    return
                self._page.keyboard.press("Escape")
                self._page.wait_for_timeout(120)
            except (PlaywrightTimeout, PlaywrightError):
                return

    def _limpar_preview(self) -> None:
        """Fecha preview/menu pendentes.

        Um preview aberto de uma tentativa anterior contamina a proxima -- e'
        forte candidato ao "as vezes nao manda nada".

        **Escape sozinho nao fecha a pre-visualizacao de midia.** Ele abre um
        dialogo "Deseja descartar a selecao?" com *Cancelar* e *Descartar*. A
        versao anterior apertava Escape duas vezes: o primeiro abria o
        dialogo, o segundo o CANCELAVA -- e o preview voltava intacto. O
        metodo parecia funcionar e nao fechava nada.
        """
        if self._page is None:
            return
        for _ in range(3):
            try:
                if not self._page.evaluate(TEM_O_QUE_FECHAR_JS):
                    return
                self._page.keyboard.press("Escape")
                self._page.wait_for_timeout(250)
                self._confirmar_descarte()
            except (PlaywrightTimeout, PlaywrightError):
                return

    def _confirmar_descarte(self) -> bool:
        """Clica em "Descartar" se o dialogo de descarte estiver na tela.

        No ELEMENTO, nunca na coordenada: o dialogo entra animado, e a mesma
        armadilha que quebrou a citacao vale aqui.
        """
        if self._page is None:
            return False
        try:
            achado = self._page.evaluate(DIALOGO_DESCARTAR_JS) or {}
            if not achado.get("achou"):
                return False
            self._page.locator("[data-allana-descartar]").first.click(timeout=4_000)
            self._page.wait_for_timeout(300)
            return True
        except (PlaywrightTimeout, PlaywrightError):
            return False

    def _abrir_menu_de_anexo(self) -> bool:
        """Abre o menu do clipe. Devolve se algum gatilho respondeu.

        Antes isto nao devolvia nada: quando nenhum gatilho casava, o codigo
        seguia procurando o item "Fotos e vídeos" num menu que nunca abriu, e
        a unica pista era "nao encontrei o campo de anexo".
        """
        for gatilho in self._GATILHOS_ANEXO:
            try:
                botao = self._page.locator(gatilho).first
                if botao.count():
                    botao.click(timeout=4_000)
                    self._page.wait_for_timeout(600)
                    return True
            except (PlaywrightTimeout, PlaywrightError):
                continue
        return False

    def _diagnosticar_anexo(self, abriu_menu: bool) -> None:
        """Uma vez por execucao, diz o que o menu de anexo realmente tem."""
        if self._anexo_diagnosticado:
            return
        self._anexo_diagnosticado = True
        try:
            d = self._page.evaluate(DIAGNOSTICO_ANEXO_JS) or {}
        except Exception as exc:
            self._log("WARNING", f"Diagnóstico do anexo falhou: {_short(exc)}")
            return
        self._log(
            "ERROR",
            "Não achei o item de foto no menu de anexo. "
            f"menu_abriu={abriu_menu} "
            f"inputs_de_arquivo={d.get('inputs')} "
            f"itens={d.get('itens')} "
            f"ícones={d.get('icones')}",
        )



    def _do_qr(self) -> str:
        if self._page is None:
            return self._qr_cache
        data = self._read_qr()
        if data:
            self._qr_cache = data
        return self._qr_cache


def _css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _short(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0][:240] if text else exc.__class__.__name__
