"""Por que a setinha aparece no laboratório e não no serviço.

A evidência que trouxe até aqui
--------------------------------
Serviço real, três tentativas, sempre igual::

    PASSO 4: o menu nao abriu
    rótulos=['Reagir']          <- só o botão de reagir
    botões=['Ryan', 'Reagir']

Laboratório solto, mesmo JS, mesmo perfil, minutos depois::

    ACHOU  aria-label='Menu de contexto para a mensagem de Ryan'

A única diferença no jeito de abrir o navegador:

===================  ==========================================
serviço              ``viewport={"width":1280,"height":860}``
laboratório          ``args=["--start-maximized"]``, sem viewport
===================  ==========================================

Passar ``viewport`` faz o Playwright chamar
``Emulation.setDeviceMetricsOverride``. A suspeita é que isso mude o que o
navegador responde para ``@media (hover: hover)`` / ``(pointer: fine)`` — e o
WhatsApp só monta a setinha de contexto quando o ponteiro é "fino" e capaz de
hover. O botão de reagir aparecer e a setinha não é exatamente o sintoma de
duas regras de CSS diferentes.

Este script mede, em vez de supor. Para cada configuração de abertura ele
imprime o que o navegador diz sobre hover e ponteiro, e se a setinha aparece
de fato ao passar o mouse numa mensagem.

**Nada é enviado.** Só hover e leitura.

Uso::

    .venv/Scripts/python.exe ferramentas/testar_hover.py

Bot PARADO e Brave FECHADO. Cada configuração abre e fecha o navegador — o
perfil aceita um processo por vez, então elas rodam em sequência.
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
from app.whatsapp import BOTAO_DE_OPCOES_JS, GEOMETRIA_DA_LINHA_JS  # noqa: E402

SAIDA = RAIZ / "diagnostico"
SAIDA.mkdir(exist_ok=True)


def diga(t: str = "") -> None:
    print(f"  {t}", flush=True)


def titulo(t: str) -> None:
    print("", flush=True)
    print("=" * 74, flush=True)
    print(t, flush=True)
    print("=" * 74, flush=True)


# O que o navegador responde sobre o ponteiro. É isto que o CSS do WhatsApp
# consulta para decidir se monta a setinha.
CAPACIDADES_JS = r"""
() => ({
  hoverHover:      matchMedia('(hover: hover)').matches,
  hoverNone:       matchMedia('(hover: none)').matches,
  pointerFine:     matchMedia('(pointer: fine)').matches,
  pointerCoarse:   matchMedia('(pointer: coarse)').matches,
  anyHoverHover:   matchMedia('(any-hover: hover)').matches,
  anyPointerFine:  matchMedia('(any-pointer: fine)').matches,
  maxTouchPoints:  navigator.maxTouchPoints,
  larguraJanela:   window.innerWidth,
  alturaJanela:    window.innerHeight,
  devicePixelRatio: window.devicePixelRatio,
})
"""

ULTIMA_RECEBIDA_JS = r"""
() => {
  for (const linha of [...document.querySelectorAll('#main div[role="row"]')].reverse()) {
    const el = linha.querySelector('[data-id]');
    const id = el ? el.getAttribute('data-id') : '';
    if (!id) continue;
    const nossa = id.startsWith('true_') || id.startsWith('3EB0')
      || !!linha.querySelector('[data-icon^="msg-"], [data-icon^="status-"]');
    if (nossa) continue;
    return { id, texto: (linha.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 45) };
  }
  return null;
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

#: As configuracoes a comparar. A primeira e' a de producao HOJE.
CONFIGURACOES = [
    ("producao (viewport fixo)",
     {"viewport": {"width": 1280, "height": 860}}),
    ("sem viewport (janela real)",
     {"no_viewport": True, "args_extra": ["--start-maximized"]}),
    ("viewport fixo + window-size igual",
     {"viewport": {"width": 1280, "height": 860},
      "args_extra": ["--window-size=1280,940"]}),
]


def medir(pw, config, rotulo: str, extras: dict) -> dict:
    opcoes: dict = dict(
        user_data_dir=str(config.whatsapp_profile_dir),
        headless=config.whatsapp_headless,
        locale="pt-BR",
        args=["--no-first-run", "--lang=pt-BR",
              "--disable-blink-features=AutomationControlled",
              *extras.get("args_extra", [])],
    )
    if extras.get("no_viewport"):
        opcoes["no_viewport"] = True
    else:
        opcoes["viewport"] = extras["viewport"]
    if config.browser_path and Path(config.browser_path).exists():
        opcoes["executable_path"] = config.browser_path

    ctx = pw.chromium.launch_persistent_context(**opcoes)
    try:
        pagina = ctx.pages[0] if ctx.pages else ctx.new_page()
        if "web.whatsapp.com" not in (pagina.url or ""):
            pagina.goto("https://web.whatsapp.com", wait_until="domcontentloaded",
                        timeout=90_000)
        pagina.wait_for_selector('[role="row"], [aria-label]', timeout=120_000)
        time.sleep(3)

        caps = pagina.evaluate(CAPACIDADES_JS) or {}
        diga("capacidades que o navegador declara:")
        diga(f"    hover:hover={caps.get('hoverHover')}  "
             f"hover:none={caps.get('hoverNone')}")
        diga(f"    pointer:fine={caps.get('pointerFine')}  "
             f"pointer:coarse={caps.get('pointerCoarse')}")
        diga(f"    any-hover={caps.get('anyHoverHover')}  "
             f"any-pointer:fine={caps.get('anyPointerFine')}")
        diga(f"    maxTouchPoints={caps.get('maxTouchPoints')}  "
             f"janela={caps.get('larguraJanela')}x{caps.get('alturaJanela')}")

        achado = pagina.evaluate(ABRIR_GRUPO_JS, config.whatsapp_group_name) or {}
        if not achado.get("ok"):
            diga("  [FALHA] não achei o grupo")
            return {"rotulo": rotulo, "setinha": None, **caps}
        if not achado.get("ja"):
            pagina.mouse.click(achado["x"], achado["y"])
            time.sleep(2.5)

        alvo = pagina.evaluate(ULTIMA_RECEBIDA_JS)
        if not alvo:
            diga("  [FALHA] nenhuma mensagem recebida na tela")
            return {"rotulo": rotulo, "setinha": None, **caps}

        linha = pagina.evaluate(GEOMETRIA_DA_LINHA_JS, [alvo["id"], alvo["texto"]])
        time.sleep(0.4)
        linha = pagina.evaluate(GEOMETRIA_DA_LINHA_JS, [alvo["id"], alvo["texto"]])
        if not linha.get("achou"):
            diga("  [FALHA] não achei a linha")
            return {"rotulo": rotulo, "setinha": None, **caps}

        pagina.mouse.move(linha["x"], linha["y"])
        limite = time.monotonic() + 3.0
        botao = {}
        while True:
            botao = pagina.evaluate(BOTAO_DE_OPCOES_JS, alvo["id"]) or {}
            if botao.get("achou") or time.monotonic() >= limite:
                break
            pagina.wait_for_timeout(150)

        diga("")
        if botao.get("achou"):
            diga(f"  >>> SETINHA APARECEU: {botao.get('rotulo')!r}")
        else:
            diga(f"  >>> SETINHA NÃO APARECEU ({botao.get('motivo')})")
            diga(f"      rótulos na linha: {botao.get('visto')}")

        arquivo = SAIDA / f"hover_{rotulo.split()[0]}.png"
        try:
            pagina.screenshot(path=str(arquivo))
            diga(f"      [foto] {arquivo.name}")
        except Exception:
            pass
        return {"rotulo": rotulo, "setinha": bool(botao.get("achou")), **caps}
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def main() -> int:
    config = load_config()
    titulo("Hover: por que a setinha some no serviço")
    diga(f"grupo {config.whatsapp_group_name!r}")

    trava = TravaDeInstancia(config.whatsapp_profile_dir, rotulo="teste de hover")
    try:
        trava.adquirir()
    except InstanciaEmUso as conflito:
        print(explicar(conflito, config.whatsapp_profile_dir), flush=True)
        return 1

    resultados = []
    with sync_playwright() as pw:
        for rotulo, extras in CONFIGURACOES:
            titulo(f"CONFIGURAÇÃO: {rotulo}")
            try:
                resultados.append(medir(pw, config, rotulo, extras))
            except Exception:
                traceback.print_exc()
                resultados.append({"rotulo": rotulo, "setinha": None})
            time.sleep(2)

    titulo("RESUMO")
    for r in resultados:
        marca = {True: "APARECE ", False: "some    ", None: "erro    "}[r.get("setinha")]
        diga(f"  {marca} {r['rotulo']:34} "
             f"hover={r.get('hoverHover')} pointer_fine={r.get('pointerFine')} "
             f"touch={r.get('maxTouchPoints')}")

    aparece = [r for r in resultados if r.get("setinha")]
    some = [r for r in resultados if r.get("setinha") is False]
    diga("")
    if aparece and some:
        diga("  >>> A configuração de abertura DECIDE se a setinha existe.")
        diga(f"  >>> Funciona em: {[r['rotulo'] for r in aparece]}")
        diga(f"  >>> Falha em:    {[r['rotulo'] for r in some]}")
    elif not aparece:
        diga("  >>> A setinha não apareceu em NENHUMA. O viewport não é a causa.")
    else:
        diga("  >>> Apareceu em todas. A causa está em outro lugar do serviço.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
