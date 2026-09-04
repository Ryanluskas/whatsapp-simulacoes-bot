"""Por que a citação falha em PRODUÇÃO e passa no laboratório.

O mesmo código, no mesmo perfil, minutos de diferença:

    laboratório:  menu aberto: True, itens: ['responder', ...]
    produção:     nenhum menu abriu; linha_encontrada=False, tudo vazio

A diferença não está no código da citação — está no ESTADO DA PÁGINA quando
produção o chama. Duas candidatas, e o conserto muda conforme qual for:

**A. A linha saiu do DOM.** Entre ler o pedido e responder passam ~90 s. O
WhatsApp descarta linhas antigas conforme a conversa rola (virtualização), e
um `data-id` guardado há um minuto e meio pode simplesmente não existir mais.
Conserto: rolar até achar antes de desistir.

**B. A conversa acabou de ser reaberta.** `_garantir_conversa` roda
imediatamente antes de `_citar`. Reabrir remonta a lista, e o `data-id` pode
ainda não estar montado. Conserto: esperar a conversa assentar.

Este script mede as duas. Não escreve nada no grupo.

Uso::

    .venv/Scripts/python.exe ferramentas/testar_estado_da_conversa.py

Bot PARADO e Brave FECHADO.
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
from app.whatsapp import GEOMETRIA_DA_LINHA_JS  # noqa: E402


def diga(t: str = "") -> None:
    print(f"  {t}", flush=True)


def titulo(t: str) -> None:
    print("", flush=True)
    print("=" * 72, flush=True)
    print(t, flush=True)
    print("=" * 72, flush=True)


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

# Está no DOM? Está VISÍVEL? Quantas linhas existem? São perguntas diferentes,
# e o diagnóstico anterior misturava as três num "linha_encontrada=False".
ESTADO_DA_LINHA_JS = r"""
(dataId) => {
  let noDom = false, visivel = false, caixa = null;
  for (const el of document.querySelectorAll('[data-id]')) {
    if (el.getAttribute('data-id') !== dataId) continue;
    noDom = true;
    const linha = el.closest('div[role="row"]') || el;
    const r = linha.getBoundingClientRect();
    caixa = { y: Math.round(r.y), h: Math.round(r.height) };
    visivel = !!(r.width && r.height);
    break;
  }
  const linhas = document.querySelectorAll('#main div[role="row"]');
  const ids = [...linhas].map((l) => {
    const e = l.querySelector('[data-id]');
    return e ? e.getAttribute('data-id') : null;
  }).filter(Boolean);
  return { noDom, visivel, caixa, linhasNaTela: linhas.length,
           primeiro: ids[0] || null, ultimo: ids[ids.length - 1] || null };
}
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
    return { id, texto: (linha.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 50) };
  }
  return null;
}
"""

# Rola a lista de mensagens para o topo (mensagens antigas).
ROLAR_JS = r"""
(quanto) => {
  const painel = document.querySelector('#main [data-testid="conversation-panel-messages"]')
              || document.querySelector('#main div[role="application"]')
              || document.querySelector('#main');
  if (!painel) return { ok: false };
  painel.scrollTop += quanto;
  return { ok: true, scrollTop: painel.scrollTop, altura: painel.scrollHeight };
}
"""


def abrir(pagina, grupo: str) -> bool:
    achado = pagina.evaluate(ABRIR_GRUPO_JS, grupo) or {}
    if not achado.get("ok"):
        return False
    if not achado.get("ja"):
        pagina.mouse.click(achado["x"], achado["y"])
        time.sleep(2.5)
    return True


def main() -> int:
    config = load_config()
    grupo = config.whatsapp_group_name
    titulo("Estado da conversa — por que produção falha e o laboratório passa")
    diga(f"grupo {grupo!r}")

    trava = TravaDeInstancia(config.whatsapp_profile_dir, rotulo="estado da conversa")
    try:
        trava.adquirir()
    except InstanciaEmUso as conflito:
        print(explicar(conflito, config.whatsapp_profile_dir), flush=True)
        return 1

    with sync_playwright() as pw:
        opcoes = {"headless": False, "args": ["--start-maximized"]}
        if config.browser_path and Path(config.browser_path).exists():
            opcoes["executable_path"] = config.browser_path
        ctx = pw.chromium.launch_persistent_context(
            str(config.whatsapp_profile_dir), **opcoes)
        pagina = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if "web.whatsapp.com" not in (pagina.url or ""):
                pagina.goto("https://web.whatsapp.com")
            diga("aguardando carregar...")
            pagina.wait_for_selector('[role="row"], [aria-label]', timeout=120_000)
            time.sleep(3)

            if not abrir(pagina, grupo):
                diga("[FALHA] não achei o grupo")
                return 1
            diga("conversa aberta")

            alvo = pagina.evaluate(ULTIMA_RECEBIDA_JS)
            if not alvo:
                diga("[FALHA] nenhuma mensagem recebida na tela")
                return 1
            msg_id = alvo["id"]
            diga(f"alvo: {msg_id}  {alvo['texto']!r}")

            base = pagina.evaluate(ESTADO_DA_LINHA_JS, msg_id)
            diga(f"agora: noDom={base['noDom']} visivel={base['visivel']} "
                 f"linhas={base['linhasNaTela']}")

            # ---------------------------------------------- HIPÓTESE B
            titulo("HIPÓTESE B — a conversa acabou de ser reaberta")
            diga("fechando e reabrindo a conversa, como _garantir_conversa faz...")
            # Sair para outra conversa e voltar reproduz a remontagem.
            pagina.evaluate("""() => {
              const itens = document.querySelectorAll('[role="listitem"], [role="gridcell"]');
              if (itens.length > 1) itens[1].click();
            }""")
            time.sleep(1.5)
            if not abrir(pagina, grupo):
                diga("[FALHA] não consegui reabrir")
                return 1

            diga("")
            diga("quando o data-id volta a existir no DOM?")
            comeco = time.monotonic()
            achou_em = None
            for _ in range(50):                      # até 10 s
                e = pagina.evaluate(ESTADO_DA_LINHA_JS, msg_id)
                decorrido = time.monotonic() - comeco
                if e["noDom"]:
                    achou_em = decorrido
                    diga(f"  {decorrido:5.2f}s  noDom=True visivel={e['visivel']} "
                         f"linhas={e['linhasNaTela']}")
                    break
                if int(decorrido * 5) % 5 == 0:
                    diga(f"  {decorrido:5.2f}s  noDom=False linhas={e['linhasNaTela']}")
                pagina.wait_for_timeout(200)

            if achou_em is None:
                diga("")
                diga("  >>> A LINHA NUNCA VOLTOU em 10 s após reabrir.")
                diga("  >>> HIPÓTESE B CONFIRMADA — reabrir a conversa perde o alvo.")
            elif achou_em > 0.5:
                diga("")
                diga(f"  >>> Voltou em {achou_em:.2f}s. Citar imediatamente após")
                diga("  >>> reabrir pega o DOM ainda vazio. HIPÓTESE B vale.")
            else:
                diga("")
                diga("  >>> Voltou na hora. Reabrir NÃO é o problema.")

            # ---------------------------------------------- HIPÓTESE A
            titulo("HIPÓTESE A — a linha sai do DOM quando a conversa rola")
            antes = pagina.evaluate(ESTADO_DA_LINHA_JS, msg_id)
            diga(f"antes de rolar: noDom={antes['noDom']} linhas={antes['linhasNaTela']}")

            for volta in range(1, 7):
                r = pagina.evaluate(ROLAR_JS, -1500)   # sobe (mensagens antigas)
                time.sleep(0.9)
                e = pagina.evaluate(ESTADO_DA_LINHA_JS, msg_id)
                diga(f"  rolagem {volta}: scrollTop={r.get('scrollTop')} "
                     f"noDom={e['noDom']} visivel={e['visivel']} "
                     f"linhas={e['linhasNaTela']}")
                if not e["noDom"]:
                    diga("")
                    diga("  >>> A LINHA SAIU DO DOM ao rolar.")
                    diga("  >>> HIPÓTESE A CONFIRMADA — o WhatsApp descarta linhas")
                    diga("  >>> antigas, e um data-id de 90 s atrás pode não existir.")
                    break
            else:
                diga("")
                diga("  >>> A linha resistiu a 6 rolagens. Hipótese A é fraca.")

            titulo("O scrollIntoView RECUPERA?")
            g = pagina.evaluate(GEOMETRIA_DA_LINHA_JS, [msg_id, alvo["texto"]])
            diga(f"GEOMETRIA_DA_LINHA_JS: achou={g.get('achou')} via={g.get('via')!r}")
            if not g.get("achou"):
                diga(f"  motivo: {g.get('motivo')}")
                diga("  >>> É exatamente o que produção registra: linha_encontrada=False")
            return 0
        except Exception:
            titulo("ESTOUROU")
            traceback.print_exc()
            return 1
        finally:
            print("\n  >>> navegador aberto; Enter para fechar", flush=True)
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                time.sleep(45)


if __name__ == "__main__":
    sys.exit(main())
