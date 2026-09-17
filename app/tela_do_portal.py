"""O print da tela do Santander que vai para o consultor.

Por que existe
--------------
O consultor confia no que ele mesmo veria no portal. Um card montado por nos
e' uma transcricao: se a leitura errar um campo, o erro chega bonito e
indistinguivel de um acerto. O print e' a tela, e nao a nossa versao dela.

O RECORTE E' A PARTE PERIGOSA
-----------------------------
A tela inteira do portal carrega, no topo, a identificacao do operador:

    Parceiro Santander
    Fulaninha
    Xyz Promotora Ltda Me
    Home  Meu negocio  Produtos  Propostas  Servicos ...

Isso e' o acesso da empresa ao banco, e nao pode ir para um grupo de
WhatsApp. Por isso aqui NAO existe `full_page=True`, e o recorte e' julgado
pelo texto que ele contem: achou qualquer marca do topo, o print e'
descartado. Sem print o consultor ainda recebe a resposta escrita; com o
print errado, o acesso vaza.

Como o recorte e' escolhido
---------------------------
Sem nenhum seletor de classe. As classes do portal sao geradas e mudam a
cada build; um seletor assim quebra calado e o recorte passa a pegar outra
coisa -- que, nesta funcao, significa vazar. O que nao muda e' o texto que a
tela mostra, e ele foi colhido do portal de verdade (``debug_cards.txt``):

    Selecione os contratos que deseja refinanciar
    7******46
    Parcelas / Valor da parcela / Taxa / Parcelas pagas / Saldo devedor
    Voltar  Continuar

Entao: acha o titulo, sobe ate' o primeiro ancestral que ja' contenha os
cards, e para ali. O ancestral seguinte seria maior sem necessidade -- e
quanto maior o recorte, mais perto do topo da pagina ele chega.

O QUE ESTE MODULO AINDA NAO TEM
-------------------------------
Ele nunca rodou contra o portal de verdade -- exige uma sessao logada do
Santander, que nao havia quando isto foi escrito. O que existe e' o texto
real da tela, colhido em ``debug_cards.txt``, e os testes montam uma pagina
com esse texto (topo do operador incluido) para exercitar a recusa. Quando a
sessao existir, uma execucao confirma o recorte -- e ate' la' a falha e'
segura: sem recorte, nenhum print e' enviado.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: O que a tela de resultado diz. Colhido do portal, nao inventado.
ANCORAS_DO_RESULTADO = (
    "selecione os contratos que deseja refinanciar",
)

#: Uma marca que so' existe DENTRO dos cards. O recorte precisa conte-la:
#: e' ela que separa "achei o titulo" de "achei o titulo e os contratos".
MARCA_DOS_CARDS = "saldo devedor"

#: O topo da pagina. Se qualquer uma aparecer no recorte, ele e' descartado.
#:
#: Nao e' zelo: e' o nome do operador e da empresa que tem acesso ao portal
#: do banco. Num grupo com dezenas de consultores, isso e' um vazamento.
MARCAS_DO_TOPO = (
    "parceiro santander",
    "meu negocio",
    "central de ajuda",
    "expandir menu",
    "acompanhe o progresso de propostas",
    "sair da conta",
)

#: CPF completo. O recorte dos cards nao deve pegar o bloco do cliente, que
#: fica acima dele; se pegar, e' sinal de que subimos demais.
_CPF = re.compile(r"\d{3}\.?\d{3}\.?\d{3}-?\d{2}")

#: Teto de tamanho. Um recorte gigante e' quase sempre "subi ate' o body".
_ALTURA_MAXIMA = 2200
_LARGURA_MAXIMA = 2600
_MINIMO = 80
#: Quanto do texto do recorte volta para conferencia (ver RECORTE_JS).
_TEXTO_CONFERIDO = 4000


RECORTE_JS = r"""
(pistas) => {
  const norm = (s) => (s || '')
      .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
      .replace(/\s+/g, ' ').trim().toLowerCase();

  const visivel = (el) => {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  };

  // 1. O TITULO. O elemento mais PROFUNDO que o contenha: subir a partir do
  //    menor no' possivel e' o que mantem o recorte apertado.
  let titulo = null;
  for (const el of document.querySelectorAll('h1, h2, h3, h4, p, span, div, legend, label')) {
    if (el.children.length > 2) continue;         // no' de texto, nao container
    const t = norm(el.textContent);
    if (!t || t.length > 160) continue;
    if (!pistas.ancoras.some((a) => t.includes(a))) continue;
    if (!visivel(el)) continue;
    titulo = el;                                   // fica com o ULTIMO: o mais profundo
  }
  if (!titulo) return { achou: false, motivo: 'nao achei o titulo da tela de resultado' };

  // 2. SUBIR ate' o primeiro ancestral que ja' tenha os cards, e PARAR.
  //
  //    Cada nivel a mais aproxima o recorte do topo da pagina, que e' onde
  //    esta' a identificacao do operador.
  let caixa = titulo;
  let subidas = 0;
  while (caixa && subidas < 12) {
    if (norm(caixa.textContent).includes(pistas.cards)) break;
    caixa = caixa.parentElement;
    subidas += 1;
  }
  if (!caixa || !norm(caixa.textContent).includes(pistas.cards)) {
    return { achou: false, motivo: 'achei o titulo mas nenhum contrato junto dele' };
  }

  const r = caixa.getBoundingClientRect();

  // O print e' dos PIXELS, e a conferencia abaixo le' o TEXTO do elemento.
  // Duas situacoes fazem os dois divergirem, e nas duas o print e' descartado:
  //  * o elemento nao cabe inteiro na janela (rolagem, lista maior que a
  //    tela): o print sai cortado ou deslocado;
  //  * um elemento fixo/sticky de FORA (cabecalho do portal, com o operador)
  //    cruza a area: ele aparece na imagem sem estar no texto conferido.
  const dentro = r.left >= 0 && r.top >= 0
      && r.right <= window.innerWidth + 1 && r.bottom <= window.innerHeight + 1;
  let sobrepostos = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (caixa.contains(el) || el.contains(caixa)) continue;
    const pos = getComputedStyle(el).position;
    if (pos !== 'fixed' && pos !== 'sticky') continue;
    if (!visivel(el)) continue;
    const q = el.getBoundingClientRect();
    if (q.right <= r.left || q.left >= r.right || q.bottom <= r.top || q.top >= r.bottom) continue;
    sobrepostos += 1;
  }

  return {
    achou: true,
    dentro_da_tela: dentro,
    sobrepostos,
    texto_total: (caixa.innerText || '').length,
    x: Math.max(0, Math.floor(r.x)),
    y: Math.max(0, Math.floor(r.y)),
    largura: Math.ceil(r.width),
    altura: Math.ceil(r.height),
    subidas,
    // O TEXTO do recorte volta para o Python conferir. A decisao de
    // descartar nao fica aqui: ela e' testavel do lado de fora, e uma
    // decisao dessas precisa de teste.
    texto: (caixa.innerText || '').slice(0, 4000),
  };
}
"""


def _sem_acento(texto: str) -> str:
    import unicodedata

    sem = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in sem if unicodedata.category(c) != "Mn").lower()


def recusar(recorte: dict) -> str:
    """Por que este recorte NAO pode ser enviado. Vazio quando pode.

    Separada da captura de proposito: e' a regra que impede um vazamento, e
    regra assim tem de ser exercitavel sem navegador nenhum.
    """
    if not recorte or not recorte.get("achou"):
        return recorte.get("motivo", "não achei a área do resultado") if recorte else "sem recorte"

    largura = int(recorte.get("largura") or 0)
    altura = int(recorte.get("altura") or 0)
    if largura < _MINIMO or altura < _MINIMO:
        return f"a área do resultado veio pequena demais ({largura}x{altura})"
    if largura > _LARGURA_MAXIMA or altura > _ALTURA_MAXIMA:
        # Recorte gigante e' quase sempre "subi ate' o body" -- e ai' o topo
        # da pagina entra junto.
        return f"a área do resultado veio grande demais ({largura}x{altura})"

    # Sem a prova de que o print e' o que foi conferido, nao ha' print. A falta
    # das chaves tambem recusa: nunca enviar um print potencialmente contaminado.
    if recorte.get("dentro_da_tela") is not True:
        return "o recorte sai da área visível (rolagem ou lista maior que a janela)"
    if int(recorte.get("sobrepostos") if recorte.get("sobrepostos") is not None else 1) > 0:
        return "há elemento fixo do portal por cima do recorte (cabeçalho ou menu)"
    if int(recorte.get("texto_total") or 0) > _TEXTO_CONFERIDO:
        return "o texto do recorte é grande demais para conferir inteiro"

    texto = _sem_acento(recorte.get("texto") or "")
    for marca in MARCAS_DO_TOPO:
        if marca in texto:
            return f"o recorte pegou o topo da página ({marca!r})"
    if _CPF.search(recorte.get("texto") or ""):
        return "o recorte pegou o bloco do cliente (CPF à vista)"
    return ""


def capturar(page: Any, destino: Path | str,
             ancoras: tuple[str, ...] = ANCORAS_DO_RESULTADO,
             cards: str = MARCA_DOS_CARDS) -> tuple[str, str]:
    """(caminho do PNG, motivo da recusa). Um dos dois vem vazio.

    Nunca levanta: um print que falha nao pode derrubar a entrega da resposta
    escrita, que e' o que o consultor realmente precisa.
    """
    destino = Path(destino)
    try:
        recorte = page.evaluate(RECORTE_JS,
                                {"ancoras": list(ancoras), "cards": cards}) or {}
    except Exception as exc:            # noqa: BLE001 - print e' acessorio
        return "", f"o portal não deixou medir a área do resultado ({exc.__class__.__name__})"

    motivo = recusar(recorte)
    if motivo:
        return "", motivo

    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(destino), clip={
            "x": float(recorte["x"]), "y": float(recorte["y"]),
            "width": float(recorte["largura"]), "height": float(recorte["altura"]),
        })
    except Exception as exc:            # noqa: BLE001
        return "", f"não consegui fotografar a área do resultado ({exc.__class__.__name__})"
    return str(destino), ""
