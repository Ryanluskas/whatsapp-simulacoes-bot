"""Inventário do DOM real do WhatsApp Web — incapaz de falhar calado.

A primeira versão desta ferramenta morreu sem produzir arquivo nenhum, e sem
o terminal não deu para saber onde. Esta grava ``diagnostico/execucao.log``
ANTES de qualquer outra coisa, com flush a cada linha, e tira screenshot em
cada etapa. Se ela morrer, o log diz na etapa exata — screenshot funciona
mesmo quando o ``evaluate`` falha.

Pré-requisitos
--------------
* bot **PARADO** — o perfil do Chromium aceita um processo por vez;
* Brave **FECHADO** — nenhum ``brave.exe`` no Gerenciador de Tarefas.

Uso
---
    .venv\\Scripts\\python.exe ferramentas\\dump_dom.py "Santander Capital Simulações"

Sem argumento, usa ``WHATSAPP_GROUP_NAME`` do ``.env``.

Gera em ``diagnostico/``
------------------------
==========================  ====================================================
``execucao.log``            o que aconteceu, etapa por etapa, com horário
``00_timeout.html``         só se o WhatsApp não carregar — em que tela parou
``01_conversa``             conversa aberta, estado normal
``02_hover``                mouse sobre a última mensagem recebida
``03_menu_mensagem``        menu aberto pelo botão da bolha
``03b_menu_direito``        menu aberto com o botão direito
``04_menu_anexo``           menu do clipe aberto
==========================  ====================================================

O que sai daí — ``accept`` dos inputs, ``data-icon``, ``aria-label``, rótulos
de menu — é a fonte dos seletores. Anote no PR: daqui a alguns meses o
WhatsApp muda tudo de novo.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SAIDA = Path("diagnostico")


class Diario:
    """Log que sobrevive a qualquer morte do script.

    Abre o arquivo no início e grava com flush a cada linha. Sem isso, um
    traceback no meio leva junto tudo o que ainda estava em buffer — foi por
    isso que a execução anterior não deixou pista nenhuma.
    """

    def __init__(self, caminho: Path):
        self._arquivo = caminho.open("w", encoding="utf-8", buffering=1)
        self.etapa = "início"

    def __call__(self, texto: str, etapa: str | None = None) -> None:
        if etapa:
            self.etapa = etapa
        linha = f"{datetime.now():%H:%M:%S}  [{self.etapa}]  {texto}"
        print(linha, flush=True)
        self._arquivo.write(linha + "\n")
        self._arquivo.flush()

    def erro(self, exc: BaseException) -> None:
        self("FALHOU AQUI: " + repr(exc))
        rastro = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        print(rastro, flush=True)
        self._arquivo.write(rastro + "\n")
        self._arquivo.flush()

    def fechar(self) -> None:
        try:
            self._arquivo.close()
        except OSError:
            pass


JS_INVENTARIO = r"""
() => {
  const norm = (s) => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
                        .toLowerCase().trim();
  const visivel = (e) => e.offsetParent !== null || e.getClientRects().length > 0;

  const inputs = [...document.querySelectorAll('input[type=file]')].map((i) => ({
    accept: i.getAttribute('accept'),
    multiple: i.multiple,
    visivel: visivel(i),
    ariaDoPai: i.closest('[aria-label]')?.getAttribute('aria-label') || null,
  }));

  const editaveis = [...document.querySelectorAll('[contenteditable="true"]')].map((e) => ({
    dataTab: e.getAttribute('data-tab'),
    aria: e.getAttribute('aria-label'),
    placeholder: e.getAttribute('data-placeholder') || null,
    dentroDoFooter: !!e.closest('footer'),
    visivel: visivel(e),
  }));

  // SEM limite, e olhando o FIM do body primeiro.
  //
  // A versao anterior cortava em 80 itens em ordem de documento, e o WhatsApp
  // renderiza os menus no fim do body -- entao o corte comia exatamente o que
  // a captura existia para registrar. Aqui os candidatos vem em ordem
  // inversa, e so' os que tem texto curto entram.
  const todos = [...document.querySelectorAll(
    '[role="menuitem"], [role="button"], li, div[tabindex], div, span, button')];
  const itensDeMenu = todos.reverse()
    .filter(visivel)
    .map((e) => ({ tag: e.tagName.toLowerCase(), role: e.getAttribute('role'),
                   aria: e.getAttribute('aria-label'),
                   texto: norm(e.innerText).slice(0, 40) }))
    .filter((x) => x.texto && x.texto.length < 40);

  // E, separado, o que esta' de fato num container de menu aberto: e' o que
  // decide os seletores de citacao e de anexo.
  const menusAbertos = [...document.querySelectorAll(
      '[role="menu"], [role="application"], [role="dialog"], ul')]
    .filter(visivel)
    .map((m) => ({
      role: m.getAttribute('role'),
      aria: m.getAttribute('aria-label'),
      itens: [...m.querySelectorAll('div, li, span, button')]
        .filter(visivel)
        .map((e) => norm(e.innerText))
        .filter((s) => s && s.length < 40),
    }))
    .filter((m) => m.itens.length);

  const linhas = [...document.querySelectorAll('[role="row"]')].slice(-6).map((r) => ({
    dataId: r.querySelector('[data-id]')?.getAttribute('data-id') || null,
    classeEntrada: !!r.querySelector('.message-in'),
    classeSaida: !!r.querySelector('.message-out'),
    prePlain: r.querySelector('[data-pre-plain-text]')?.getAttribute('data-pre-plain-text'),
    texto: norm(r.innerText).slice(0, 60),
    temImagemBlob: !!r.querySelector('img[src^="blob:"]'),
    icones: [...r.querySelectorAll('[data-icon]')].map((i) => i.getAttribute('data-icon')),
    botoes: [...r.querySelectorAll('button, [role="button"]')]
              .map((b) => b.getAttribute('aria-label')).filter(Boolean),
  }));

  return {
    url: location.href,
    cabecalho: document.querySelector('header')?.innerText?.slice(0, 100) || null,
    temMain: !!document.querySelector('#main'),
    inputs, editaveis, itensDeMenu, menusAbertos, linhas,
    icones: [...new Set([...document.querySelectorAll('[data-icon]')]
              .map((i) => i.getAttribute('data-icon')))],
  };
}
"""

JS_ABRIR_GRUPO = r"""
(nome) => {
  const norm = (s) => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
                        .toLowerCase().trim();
  const alvo = norm(nome);
  const itens = [...document.querySelectorAll('[role="listitem"], [role="row"]')];
  const achado = itens.find((e) => norm(e.innerText).includes(alvo));
  if (!achado) {
    return { ok: false,
             vistos: itens.map((e) => norm(e.innerText).slice(0, 40)).slice(0, 30) };
  }
  achado.click();
  return { ok: true };
}
"""

JS_ULTIMA_ENTRADA = r"""
() => {
  const linhas = [...document.querySelectorAll('[role="row"]')];
  for (let i = linhas.length - 1; i >= 0; i--) {
    const idEl = linhas[i].querySelector('[data-id]');
    const id = idEl ? (idEl.getAttribute('data-id') || '') : '';
    if (!id) continue;

    // Quem assinou. O data-pre-plain-text traz "[hora, data] Nome: ".
    // Esta ferramenta chegou a apontar a resposta do PROPRIO BOT como
    // "ultima mensagem recebida" -- exatamente o defeito que ela existe
    // para expor. O nome vem de fora, para nao ficar fixo aqui.
    const marca = linhas[i].querySelector('[data-pre-plain-text]');
    const bruto = marca ? (marca.getAttribute('data-pre-plain-text') || '') : '';
    const corte = bruto.indexOf('] ');
    const resto = corte < 0 ? '' : bruto.slice(corte + 2);
    const fim = resto.lastIndexOf(': ');
    const autor = (fim < 0 ? resto : resto.slice(0, fim)).trim();

    const nomeProprio = (window.__allanaNomeProprio || '').trim().toLowerCase();
    const nossa = (autor && nomeProprio && autor.toLowerCase() === nomeProprio)
      || id.startsWith('true_') || id.startsWith('3EB0')
      || !!linhas[i].querySelector('[data-icon^="msg-"], [data-icon^="status-"]')
      || !!linhas[i].querySelector('.message-out');
    if (nossa) continue;
    linhas[i].scrollIntoView({ block: 'center' });
    const b = linhas[i].getBoundingClientRect();
    return { x: b.x + b.width * 0.5, y: b.y + b.height * 0.5,
             dataId: id, autor: autor,
             texto: (linhas[i].innerText || '').slice(0, 50) };
  }
  return null;
}
"""

JS_BOTAO_MENU_NA_LINHA = r"""
(dataId) => {
  const linhas = [...document.querySelectorAll('[role="row"]')];
  const linha = linhas.find((r) => {
    const el = r.querySelector('[data-id]');
    return el && el.getAttribute('data-id') === dataId;
  });
  if (!linha) return null;
  const proibido = /encaminh|forward|reagir|react|apagar|delete|baixar|download/i;
  const cands = [...linha.querySelectorAll('[data-icon], button, [role="button"]')]
    .filter((e) => !proibido.test(
      (e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('data-icon') || '')));
  if (!cands.length) return null;
  const alvo = cands[0];
  const b = alvo.getBoundingClientRect();
  return { x: b.x + b.width / 2, y: b.y + b.height / 2,
           icone: alvo.getAttribute('data-icon'),
           aria: alvo.getAttribute('aria-label') };
}
"""

JS_BOTAO_CLIPE = r"""
() => {
  const cands = [...document.querySelectorAll(
    'footer [data-icon], footer button, footer [role="button"]')];
  const alvo = cands.find((e) => {
    const i = (e.getAttribute('data-icon') || '').toLowerCase();
    const a = (e.getAttribute('aria-label') || '').toLowerCase();
    return i.includes('attach') || i.includes('clip') || i.includes('plus')
        || a.includes('anexar') || a.includes('attach');
  });
  if (!alvo) return null;
  const b = alvo.getBoundingClientRect();
  return { x: b.x + b.width / 2, y: b.y + b.height / 2,
           icone: alvo.getAttribute('data-icon'),
           aria: alvo.getAttribute('aria-label') };
}
"""


def foto(pagina, log: Diario, nome: str) -> None:
    """Screenshot. Funciona mesmo quando o ``evaluate`` falha."""
    try:
        pagina.screenshot(path=str(SAIDA / f"{nome}.png"), full_page=False)
        log(f"screenshot {nome}.png")
    except Exception as exc:
        log(f"screenshot {nome}.png falhou: {exc!r}")


def salvar(pagina, log: Diario, nome: str) -> dict | None:
    """Grava JSON + HTML + screenshot. Cada parte falha por si, sem levar as outras."""
    foto(pagina, log, nome)
    dados = None
    try:
        dados = pagina.evaluate(JS_INVENTARIO)
        (SAIDA / f"{nome}.json").write_text(
            json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"gravado {nome}.json")
    except Exception as exc:
        log(f"inventário de {nome} falhou: {exc!r}")
    try:
        (SAIDA / f"{nome}.html").write_text(pagina.content(), encoding="utf-8")
        log(f"gravado {nome}.html")
    except Exception as exc:
        log(f"HTML de {nome} falhou: {exc!r}")
    return dados


def conferir_ambiente(log: Diario, config) -> bool:
    """Valida ANTES de abrir o navegador, com mensagem própria para cada caso."""
    ok = True

    perfil = Path(config.whatsapp_profile_dir)
    if perfil.exists():
        log(f"perfil: {perfil} (existe)")
    else:
        log(f"[X] perfil NÃO existe: {perfil}")
        log("    O bot nunca rodou nesta máquina? Suba-o uma vez e leia o QR.")
        ok = False

    if config.browser_path:
        if Path(config.browser_path).exists():
            log(f"navegador: {config.browser_path}")
        else:
            log(f"[X] BROWSER_EXECUTABLE aponta para algo que não existe: "
                f"{config.browser_path}")
            ok = False
    else:
        log("navegador: Chromium do Playwright (sem Brave configurado)")

    try:
        teste = SAIDA / ".escrita"
        teste.write_text("x", encoding="utf-8")
        teste.unlink()
        log(f"pasta de saída gravável: {SAIDA.resolve()}")
    except OSError as exc:
        log(f"[X] não consigo escrever em {SAIDA}: {exc}")
        ok = False

    vivos = navegadores_vivos()
    if vivos:
        log(f"[X] há {vivos} processo(s) do navegador em execução.")
        log("    Feche o Brave e pare o bot: o perfil aceita um processo por vez.")
        ok = False
    else:
        log("nenhum navegador do bot em execução")

    return ok


def navegadores_vivos() -> int:
    """Quantos brave/chrome estão rodando. Best-effort, só para avisar."""
    if os.name != "nt":
        return 0
    try:
        import subprocess
        saida = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq brave.exe", "/NH"],
            capture_output=True, text=True, timeout=10).stdout
        return sum(1 for linha in saida.splitlines() if "brave.exe" in linha.lower())
    except Exception:
        return 0


def main() -> int:
    SAIDA.mkdir(exist_ok=True)
    log = Diario(SAIDA / "execucao.log")
    contexto = None
    try:
        log("iniciando", etapa="preparo")
        from app.config import load_config
        config = load_config()
        grupo = sys.argv[1] if len(sys.argv) > 1 else config.whatsapp_group_name
        log(f"grupo alvo: {grupo!r}")

        if not conferir_ambiente(log, config):
            log("[X] ambiente não está pronto — nada foi capturado.")
            return 1

        from playwright.sync_api import sync_playwright
        log("abrindo o navegador", etapa="navegador")
        with sync_playwright() as pw:
            opcoes: dict = {
                "user_data_dir": str(config.whatsapp_profile_dir),
                "headless": False,
                "args": ["--start-maximized", "--no-sandbox"],
            }
            if config.browser_path:
                opcoes["executable_path"] = config.browser_path
            contexto = pw.chromium.launch_persistent_context(**opcoes)
            log(f"navegador aberto com {len(contexto.pages)} aba(s)")

            pagina = next((p for p in contexto.pages
                           if "web.whatsapp.com" in (p.url or "")), None)
            if pagina is None:
                pagina = contexto.pages[0] if contexto.pages else contexto.new_page()
                log("nenhuma aba no WhatsApp; navegando")
                pagina.goto("https://web.whatsapp.com", wait_until="domcontentloaded")

            log("esperando o WhatsApp carregar (até 60s)", etapa="carregar")
            carregou = False
            for tentativa in range(6):
                try:
                    pagina.wait_for_selector('[role="row"], [aria-label]', timeout=10_000)
                    carregou = True
                    break
                except Exception:
                    log(f"  ainda carregando... {(tentativa + 1) * 10}s")
            if not carregou:
                log("[X] não carregou em 60s. Gravando a tela em que parou.")
                foto(pagina, log, "00_timeout")
                try:
                    (SAIDA / "00_timeout.html").write_text(pagina.content(),
                                                           encoding="utf-8")
                    log("gravado 00_timeout.html — QR? conflito de sessão?")
                except Exception as exc:
                    log(f"não consegui gravar o HTML: {exc!r}")
                return 1
            log("WhatsApp carregado")

            # A PRIMEIRA captura vem antes de qualquer clique: se algo abaixo
            # falhar, pelo menos o estado normal da conversa está salvo.
            log("capturando o estado atual", etapa="01_conversa")
            salvar(pagina, log, "01_conversa")

            if grupo:
                log(f"procurando {grupo!r} na lista", etapa="abrir_grupo")
                try:
                    r = pagina.evaluate(JS_ABRIR_GRUPO, grupo) or {}
                    if r.get("ok"):
                        log("grupo aberto")
                        pagina.wait_for_timeout(2000)
                        salvar(pagina, log, "01b_grupo_aberto")
                    else:
                        log("[!] grupo NÃO encontrado. Nomes vistos:")
                        for n in r.get("vistos", []):
                            log(f"     - {n}")
                except Exception as exc:
                    log(f"busca do grupo falhou: {exc!r}")

            log("procurando a última mensagem recebida", etapa="hover")
            pos = None
            try:
                # O nome próprio vem do .env, não fixo no JS.
                pagina.evaluate("(n) => { window.__allanaNomeProprio = n; }",
                                config.bot_self_name)
                pos = pagina.evaluate(JS_ULTIMA_ENTRADA)
            except Exception as exc:
                log(f"busca da mensagem falhou: {exc!r}")

            if not pos:
                log("[!] nenhuma mensagem RECEBIDA na tela.")
                log("    Mande um CPF no grupo e rode de novo.")
            else:
                log(f"mensagem alvo: {pos.get('dataId')} de {pos.get('autor')!r} "
                    f"— {pos.get('texto')!r}")
                try:
                    pagina.mouse.move(pos["x"], pos["y"])
                    pagina.wait_for_timeout(1000)
                    salvar(pagina, log, "02_hover")
                except Exception as exc:
                    log(f"hover falhou: {exc!r}")

                log("procurando o botão de menu da bolha", etapa="03_menu")
                botao = None
                try:
                    botao = pagina.evaluate(JS_BOTAO_MENU_NA_LINHA, pos["dataId"])
                    log(f"botão encontrado: {botao}")
                except Exception as exc:
                    log(f"busca do botão falhou: {exc!r}")

                if botao:
                    try:
                        pagina.mouse.move(pos["x"], pos["y"])
                        pagina.mouse.click(botao["x"], botao["y"])
                        pagina.wait_for_timeout(1200)
                        salvar(pagina, log, "03_menu_mensagem")
                        pagina.keyboard.press("Escape")
                        pagina.wait_for_timeout(500)
                    except Exception as exc:
                        log(f"abrir o menu pelo botão falhou: {exc!r}")

                log("tentando o menu pelo botão direito", etapa="03b_direito")
                try:
                    pagina.mouse.click(pos["x"], pos["y"], button="right")
                    pagina.wait_for_timeout(1200)
                    salvar(pagina, log, "03b_menu_direito")
                    pagina.keyboard.press("Escape")
                    pagina.wait_for_timeout(500)
                except Exception as exc:
                    log(f"menu pelo botão direito falhou: {exc!r}")

            log("procurando o clipe", etapa="04_anexo")
            clipe = None
            try:
                clipe = pagina.evaluate(JS_BOTAO_CLIPE)
                log(f"clipe encontrado: {clipe}")
            except Exception as exc:
                log(f"busca do clipe falhou: {exc!r}")

            if clipe:
                try:
                    pagina.mouse.click(clipe["x"], clipe["y"])
                    pagina.wait_for_timeout(1200)
                    dados = salvar(pagina, log, "04_menu_anexo")
                    if dados:
                        log("INPUTS COM O MENU ABERTO: "
                            + str([i["accept"] for i in dados["inputs"]]))
                    pagina.keyboard.press("Escape")
                except Exception as exc:
                    log(f"abrir o menu do clipe falhou: {exc!r}")
            else:
                log("[!] clipe não encontrado no rodapé — conversa aberta?")

            log("terminado", etapa="fim")
            log(f"arquivos em {SAIDA.resolve()}")
            log("FECHE a janela antes de subir o bot.")
        return 0

    except Exception as exc:
        log.erro(exc)
        return 1
    finally:
        try:
            if contexto is not None:
                contexto.close()
        except Exception:
            pass
        log("log encerrado")
        log.fechar()


if __name__ == "__main__":
    raise SystemExit(main())
