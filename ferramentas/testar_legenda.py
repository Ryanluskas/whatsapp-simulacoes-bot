"""Laboratório da legenda da imagem — por que o card sai mudo.

O defeito
---------
A legenda da imagem falha desde 30/08: 40 falhas em 52 imagens enviadas. O
card chega ao grupo sem o valor em texto, sem o nome do cliente e **sem o
identificador** ``_REQ000065_`` — que é a chave de busca no painel e uma das
assinaturas anti-laço. Na tela do consultor parece uma imagem solta, não uma
resposta.

O texto é montado corretamente (o banco guarda), mas não chega ao campo.

O que este laboratório faz
--------------------------
Cola uma imagem no compositor do grupo e **descreve o que existe na tela do
preview**: todo ``[contenteditable]`` com seus atributos, quem está dentro do
``footer``, quem está visível, quem tem foco. Depois tenta digitar e confere
se o texto apareceu.

Os seletores atuais (``media-caption-input-container``, ``data-tab="10"``)
são exatamente do tipo que já se provou errado nesta instalação — por isso
aqui nada é chutado: a ferramenta mostra o que tem, e a correção vem depois.

Ele roda o teste DUAS vezes: com o compositor limpo e com uma citação
pendurada. Não porque a citação seja a causa provável (a legenda falhava dois
dias antes de qualquer citação ter funcionado), mas porque descartar com
evidência custa uma passada a mais e vale mais que argumento.

Uso::

    .venv/Scripts/python.exe ferramentas/testar_legenda.py

Bot PARADO e Brave FECHADO. Deixa o navegador aberto no fim.
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from playwright.sync_api import sync_playwright  # noqa: E402

from app.config import load_config  # noqa: E402
from app.instancia import (  # noqa: E402
    InstanciaEmUso, TravaDeInstancia, explicar)
from app.whatsapp import (  # noqa: E402
    BOTAO_DE_OPCOES_JS,
    COLAR_IMAGEM_JS,
    GEOMETRIA_DA_LINHA_JS,
    ITEM_RESPONDER_JS,
    MENU_ABERTO_JS,
    PREVIEW_E_IMAGEM_JS,
    TEM_O_QUE_FECHAR_JS,
)

SAIDA = RAIZ / "diagnostico"
SAIDA.mkdir(exist_ok=True)

_passo = 0


def foto(pagina, nome: str) -> None:
    global _passo
    _passo += 1
    alvo = SAIDA / f"legenda_{_passo:02d}_{nome}.png"
    try:
        pagina.screenshot(path=str(alvo))
        print(f"        [foto] {alvo.name}", flush=True)
    except Exception as exc:
        print(f"        [foto falhou] {exc}", flush=True)


def titulo(texto: str) -> None:
    print("", flush=True)
    print("=" * 74, flush=True)
    print(texto, flush=True)
    print("=" * 74, flush=True)


def diga(texto: str) -> None:
    print(f"  {texto}", flush=True)


# TUDO que pode receber texto na tela, com os atributos que os seletores
# atuais usam. É esta lista que responde "o campo de legenda existe e tem
# outro nome?" ou "ele não existe?" — sem ninguém precisar adivinhar.
INVENTARIO_JS = r"""
() => {
  const visivel = (el) => {
    const r = el.getBoundingClientRect();
    return !!(r.width && r.height) && el.offsetParent !== null;
  };
  const limpar = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const editaveis = [...document.querySelectorAll('[contenteditable="true"]')]
    .map((el, i) => {
      const r = el.getBoundingClientRect();
      return {
        indice: i,
        aria: el.getAttribute('aria-label'),
        dataTab: el.getAttribute('data-tab'),
        placeholder: el.getAttribute('data-placeholder')
                  || el.getAttribute('data-lexical-editor')
                  || null,
        role: el.getAttribute('role'),
        noFooter: !!el.closest('footer'),
        noMain: !!el.closest('#main'),
        visivel: visivel(el),
        temFoco: el === document.activeElement,
        texto: limpar(el.innerText).slice(0, 40),
        caixa: { x: Math.round(r.x), y: Math.round(r.y),
                 w: Math.round(r.width), h: Math.round(r.height) },
        // A cadeia de pais ajuda a achar um seletor estável quando nenhum
        // atributo do próprio elemento serve.
        pais: (() => {
          const nomes = [];
          let p = el.parentElement;
          for (let n = 0; n < 4 && p; n++, p = p.parentElement) {
            nomes.push(p.getAttribute('data-testid')
                    || p.getAttribute('aria-label')
                    || p.getAttribute('role')
                    || p.tagName.toLowerCase());
          }
          return nomes;
        })(),
      };
    });

  return {
    editaveis,
    // Os seletores que o código usa hoje, contados um a um.
    seletoresAtuais: {
      'media-caption-input-container':
        document.querySelectorAll(
          'div[data-testid="media-caption-input-container"] div[contenteditable="true"]').length,
      'contenteditable[data-tab="10"]':
        document.querySelectorAll('div[contenteditable="true"][data-tab="10"]').length,
      'footer contenteditable':
        document.querySelectorAll('footer div[contenteditable="true"]').length,
    },
    // Sem `canvas` na lista: ele existe em outros lugares da tela e dava
    // "tem preview: true" com nenhum preview aberto.
    temPreview: !!document.querySelector(
      'div[data-testid="media-preview"], img[src^="blob:"]'),
    botaoEnviar: [...document.querySelectorAll('[aria-label], [data-icon]')]
      .map((el) => el.getAttribute('aria-label') || el.getAttribute('data-icon'))
      .filter((s) => s && /enviar|send/i.test(s)).slice(0, 5),
  };
}
"""

ABRIR_GRUPO_JS = r"""
(nome) => {
  const norm = (s) => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
                               .toLowerCase().trim();
  const alvo = norm(nome);
  const cab = document.querySelector('#main header');
  if (cab && norm(cab.innerText).includes(alvo)) return { ok: true, ja: true };
  for (const el of document.querySelectorAll('[role="listitem"], [role="gridcell"], [role="row"]')) {
    if (!norm(el.innerText).includes(alvo)) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    return { ok: true, x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }
  return { ok: false };
}
"""

# O que sobrou pendurado no compositor: citação armada, texto meio digitado.
ESTADO_DO_COMPOSITOR_JS = r"""
() => {
  const limpar = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const rodape = document.querySelector('footer');
  const campo = rodape ? rodape.querySelector('[contenteditable="true"]') : null;
  return {
    rodape: rodape ? limpar(rodape.innerText).slice(0, 120) : null,
    textoNoCampo: campo ? limpar(campo.innerText) : null,
    // A barra de citação vive acima do campo, dentro do footer.
    temCitacao: !!(rodape && rodape.querySelector(
      '[data-testid="quoted-message"], [aria-label*="Cancelar"], [data-icon="x"]')),
  };
}
"""

def png_de_teste() -> str:
    """base64 de um card REAL de produção.

    A primeira versão usava um base64 que eu escrevi à mão, e ele estava
    corrompido: `atob` recusava, a colagem não acontecia, e o laboratório
    inventariava o compositor NORMAL achando que era a tela do preview. Um
    teste que não reproduz a condição não prova nada -- ele só dá uma
    resposta errada com confiança.

    Um comprovante de verdade também é o tamanho certo: um PNG de 1x1 pode
    nem abrir o preview.
    """
    import base64

    cartoes = sorted((RAIZ / "comprovantes").glob("*.png"),
                     key=lambda f: f.stat().st_mtime, reverse=True)
    for cartao in cartoes:
        if cartao.stat().st_size > 1000:
            print(f"  usando o card {cartao.name} ({cartao.stat().st_size} bytes)",
                  flush=True)
            return base64.b64encode(cartao.read_bytes()).decode("ascii")
    raise RuntimeError("nenhum comprovante em comprovantes/ para usar de teste")


def limpar_a_tela(pagina) -> None:
    """Compositor vazio, sem citação pendurada, sem preview aberto.

    Escape só quando há o que fechar — com nada aberto ele FECHA A CONVERSA
    no WhatsApp Web, e essa foi a causa de um defeito anterior.
    """
    titulo("PASSO 0b — limpar a tela antes de testar")
    antes = pagina.evaluate(ESTADO_DO_COMPOSITOR_JS) or {}
    diga(f"rodapé antes:   {antes.get('rodape')!r}")
    diga(f"citação presa:  {antes.get('temCitacao')}")
    diga(f"texto no campo: {antes.get('textoNoCampo')!r}")

    for tentativa in range(4):
        if not pagina.evaluate(TEM_O_QUE_FECHAR_JS):
            break
        pagina.keyboard.press("Escape")
        pagina.wait_for_timeout(250)
        diga(f"  Escape {tentativa + 1} (havia algo aberto)")

    # Apagar o que estiver digitado no campo.
    campo = pagina.locator('footer [contenteditable="true"]').first
    if campo.count():
        try:
            campo.click(timeout=4000)
            pagina.keyboard.press("Control+A")
            pagina.keyboard.press("Delete")
            pagina.wait_for_timeout(200)
        except Exception as exc:
            diga(f"  não consegui limpar o campo: {str(exc)[:80]}")

    depois = pagina.evaluate(ESTADO_DO_COMPOSITOR_JS) or {}
    diga(f"rodapé depois:  {depois.get('rodape')!r}")
    diga(f"citação presa:  {depois.get('temCitacao')}")
    foto(pagina, "limpo")


def armar_citacao(pagina, config) -> bool:
    """Deixa uma citação pendurada de propósito, para o teste comparativo."""
    from app.whatsapp import TEXTO_DA_MENSAGEM_JS

    alvo = pagina.evaluate(r"""
      () => {
        for (const linha of [...document.querySelectorAll('#main div[role="row"]')].reverse()) {
          const idEl = linha.querySelector('[data-id]');
          const id = idEl ? idEl.getAttribute('data-id') : '';
          if (!id) continue;
          const nossa = id.startsWith('true_') || id.startsWith('3EB0')
            || !!linha.querySelector('[data-icon^="msg-"], [data-icon^="status-"]');
          if (!nossa) return { id };
        }
        return null;
      }""")
    if not alvo:
        diga("não achei mensagem recebida para citar")
        return False

    corpo = (pagina.evaluate(TEXTO_DA_MENSAGEM_JS, alvo["id"]) or "").strip()
    linha = pagina.evaluate(GEOMETRIA_DA_LINHA_JS, [alvo["id"], corpo]) or {}
    if not linha.get("achou"):
        return False
    pagina.wait_for_timeout(300)
    linha = pagina.evaluate(GEOMETRIA_DA_LINHA_JS, [alvo["id"], corpo]) or {}
    pagina.mouse.move(linha["x"], linha["y"])

    limite = time.monotonic() + 2.5
    botao = {}
    while True:
        botao = pagina.evaluate(BOTAO_DE_OPCOES_JS, alvo["id"]) or {}
        if botao.get("achou") or time.monotonic() >= limite:
            break
        pagina.wait_for_timeout(120)
    if not botao.get("achou"):
        return False
    pagina.mouse.click(botao["x"], botao["y"])

    limite = time.monotonic() + 2.5
    while not pagina.evaluate(MENU_ABERTO_JS):
        if time.monotonic() >= limite:
            return False
        pagina.wait_for_timeout(120)

    item = pagina.evaluate(ITEM_RESPONDER_JS, ["responder", "reply"]) or {}
    if not item.get("achou"):
        return False
    pagina.mouse.click(item["x"], item["y"])
    pagina.wait_for_timeout(700)
    return True


def uma_rodada(pagina, rotulo: str) -> None:
    """Cola a imagem, descreve o preview, tenta a legenda."""
    titulo(f"COLANDO A IMAGEM — {rotulo}")

    campo = pagina.locator('footer [contenteditable="true"]').first
    if campo.count():
        campo.click(timeout=5000)
    # A função real recebe [base64, nome] — um ARRAY. Passar só a string faz
    # o JS desestruturar o primeiro CARACTERE como base64, e o `atob` recusa
    # com "not correctly encoded". Foi o que aconteceu nas duas primeiras
    # rodadas deste laboratório: a colagem nunca acontecia e o inventário
    # descrevia o compositor normal achando que era a tela do preview.
    ok = pagina.evaluate(COLAR_IMAGEM_JS, [png_de_teste(), "teste.png"])
    diga(f"colagem sintética: {ok}")
    if isinstance(ok, dict) and not ok.get("ok"):
        diga("[FALHA] a colagem não aconteceu — sem preview não há o que medir.")
        foto(pagina, f"colagem_falhou_{rotulo}")
        return

    # Esperar o preview montar.
    limite = time.monotonic() + 10
    while time.monotonic() < limite:
        estado = pagina.evaluate(PREVIEW_E_IMAGEM_JS) or {}
        if estado.get("preview"):
            break
        pagina.wait_for_timeout(200)
    pagina.wait_for_timeout(600)
    foto(pagina, f"preview_{rotulo}")

    titulo(f"O QUE EXISTE NA TELA DO PREVIEW — {rotulo}")
    inv = pagina.evaluate(INVENTARIO_JS) or {}
    diga(f"tem preview: {inv.get('temPreview')}")
    diga(f"botões de enviar: {inv.get('botaoEnviar')}")
    diga("")
    diga("seletores que o código usa HOJE:")
    for nome, quantos in (inv.get("seletoresAtuais") or {}).items():
        marca = "  <<< nenhum!" if quantos == 0 else ""
        diga(f"    {nome:38} {quantos}{marca}")
    diga("")
    diga(f"campos editáveis na tela: {len(inv.get('editaveis') or [])}")
    for e in inv.get("editaveis") or []:
        diga("")
        diga(f"  [{e['indice']}] aria={e['aria']!r}")
        diga(f"       data-tab={e['dataTab']!r}  role={e['role']!r}")
        diga(f"       placeholder={e['placeholder']!r}")
        diga(f"       footer={e['noFooter']}  #main={e['noMain']}  "
             f"visível={e['visivel']}  foco={e['temFoco']}")
        diga(f"       caixa={e['caixa']}")
        diga(f"       pais={e['pais']}")
        diga(f"       texto atual={e['texto']!r}")

    titulo(f"A FUNÇÃO REAL ESCOLHE O CAMPO — {rotulo}")
    from app.whatsapp import CAMPO_DA_LEGENDA_JS, WhatsAppService

    escolha = pagina.evaluate(CAMPO_DA_LEGENDA_JS,
                              list(WhatsAppService._ROTULOS_DA_LEGENDA)) or {}
    if escolha.get("achou"):
        diga(f"  ESCOLHEU  índice={escolha['indice']}  aria={escolha['aria']!r}")
    else:
        diga(f"  [FALHA] {escolha.get('quantos')} candidatos — abortaria o envio")
        for c in escolha.get("inventario") or []:
            diga(f"     {c}")

    titulo(f"TENTANDO DIGITAR A LEGENDA — {rotulo}")
    legenda = "✅ *R$ 34.509,85* · TESTE\n_REQ999999_"
    visiveis = [e for e in (inv.get("editaveis") or []) if e["visivel"]]
    if not visiveis:
        diga("[FALHA] nenhum campo editável visível — não há onde digitar.")
    for e in visiveis:
        alvo = pagina.locator('[contenteditable="true"]').nth(e["indice"])
        try:
            alvo.click(timeout=4000)
            pagina.keyboard.insert_text(legenda)
            pagina.wait_for_timeout(300)
            agora = (pagina.evaluate(
                "(i) => document.querySelectorAll('[contenteditable=\"true\"]')[i].innerText",
                e["indice"]) or "").strip()
            pegou = "34.509,85" in agora
            diga(f"  campo [{e['indice']}] aria={e['aria']!r} -> "
                 f"{'ACEITOU' if pegou else 'não pegou'}  {agora[:50]!r}")
            if pegou:
                foto(pagina, f"digitou_{rotulo}")
                diga("")
                diga(f"  >>> ESTE é o campo da legenda: aria={e['aria']!r} "
                     f"data-tab={e['dataTab']!r} footer={e['noFooter']}")
                diga(f"  >>> pais={e['pais']}")
                # Limpar para a próxima rodada não herdar.
                pagina.keyboard.press("Control+A")
                pagina.keyboard.press("Delete")
                return
        except Exception as exc:
            diga(f"  campo [{e['indice']}] erro: {str(exc)[:70]}")

    diga("")
    diga("[FALHA] nenhum campo aceitou a legenda.")


def main() -> int:
    config = load_config()
    titulo("Laboratório da legenda")
    diga(f"grupo   {config.whatsapp_group_name!r}")
    diga(f"perfil  {config.whatsapp_profile_dir}")

    trava = TravaDeInstancia(config.whatsapp_profile_dir, rotulo="laboratório da legenda")
    try:
        trava.adquirir()
    except InstanciaEmUso as conflito:
        print(explicar(conflito, config.whatsapp_profile_dir), flush=True)
        return 1

    with sync_playwright() as pw:
        opcoes = {"headless": False, "args": ["--start-maximized"]}
        if config.browser_path and Path(config.browser_path).exists():
            opcoes["executable_path"] = config.browser_path
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(config.whatsapp_profile_dir), **opcoes)
        except Exception as exc:
            titulo("NÃO CONSEGUI ABRIR O NAVEGADOR")
            diga(str(exc)[:250])
            diga("Pare o bot e feche o Brave.")
            return 1

        pagina = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if "web.whatsapp.com" not in (pagina.url or ""):
                pagina.goto("https://web.whatsapp.com")
            diga("")
            diga("aguardando o WhatsApp carregar...")
            pagina.wait_for_selector('[role="row"], [aria-label]', timeout=120_000)
            time.sleep(3)

            titulo("PASSO 0 — abrir a conversa")
            achado = pagina.evaluate(ABRIR_GRUPO_JS, config.whatsapp_group_name) or {}
            if not achado.get("ok"):
                diga(f"[FALHA] não achei {config.whatsapp_group_name!r}")
                return 1
            if not achado.get("ja"):
                pagina.mouse.click(achado["x"], achado["y"])
                time.sleep(2.5)
            diga("conversa aberta")

            limpar_a_tela(pagina)

            # RODADA 1: tela limpa. É a condição normal de produção.
            uma_rodada(pagina, "tela_limpa")

            limpar_a_tela(pagina)

            # RODADA 2: com citação pendurada. Serve para DESCARTAR a hipótese
            # de que a citação pendente atrapalha — ela não explica as falhas
            # de 30 e 31/08, mas medir custa uma passada.
            titulo("PASSO EXTRA — armar uma citação e repetir")
            if armar_citacao(pagina, config):
                diga("citação armada")
                foto(pagina, "citacao_armada")
                uma_rodada(pagina, "com_citacao")
            else:
                diga("não consegui armar a citação; comparação não feita")

            limpar_a_tela(pagina)
            titulo("FIM — nada foi enviado ao grupo")
            diga("Só colagens no compositor; nenhum Enter foi pressionado.")
            return 0
        except Exception:
            titulo("O LABORATÓRIO ESTOUROU")
            traceback.print_exc()
            sys.stdout.flush()
            try:
                foto(pagina, "estouro")
            except Exception:
                pass
            return 1
        finally:
            print("", flush=True)
            print("  >>> navegador ABERTO; Enter (ou Ctrl+C) para fechar", flush=True)
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                print("  (sem terminal — segurando 90 s)", flush=True)
                try:
                    time.sleep(90)
                except KeyboardInterrupt:
                    pass


if __name__ == "__main__":
    sys.exit(main())
