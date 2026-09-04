"""O SERVIÇO REAL citando — não o JS solto, o `WhatsAppService` inteiro.

Por que este laboratório existe
-------------------------------
Os outros exercitam o JS da citação chamando-o direto de um script. Passam
sempre. Produção usa o mesmo JS e falha sempre::

    laboratório:  menu aberto: True, itens: ['responder', ...]
    produção:     nenhum menu abriu depois do clique

Se o JS é o mesmo, a diferença está em **como produção chega até ele**: o
serviço sobe o navegador do seu jeito, mantém um laço de leitura rodando a
cada 3 s, garante a conversa aberta antes de cada envio e roda tudo dentro da
thread dona do ``ThreadActor``. Nenhuma dessas coisas existia nos outros
laboratórios.

Aqui o ``WhatsAppService`` é instanciado de verdade, com a configuração de
produção, e o ``_citar`` real é chamado pelo ator — exatamente como o
``_enviar_imagem`` faz.

**Nada é enviado.** ``_citar`` apenas arma a barra de citação; o envio fica
fora deste script de propósito: o grupo tem consultores de verdade.

Uso::

    .venv/Scripts/python.exe ferramentas/testar_envio_real.py [repeticoes]

Bot PARADO e Brave FECHADO.
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app.config import load_config  # noqa: E402
from app.instancia import (  # noqa: E402
    InstanciaEmUso, TravaDeInstancia, explicar)
from app.state_store import StateStore  # noqa: E402
from app.whatsapp import (  # noqa: E402
    CITACAO_ATIVA_JS, PREVIEW_ABERTO_JS, WhatsAppService)

SAIDA = RAIZ / "diagnostico"
SAIDA.mkdir(exist_ok=True)


def diga(t: str = "") -> None:
    print(f"  {t}", flush=True)


def titulo(t: str) -> None:
    print("", flush=True)
    print("=" * 74, flush=True)
    print(t, flush=True)
    print("=" * 74, flush=True)


# Uma mensagem recebida, contando de baixo para cima.
#
# `pular=0` e' a ultima; `pular=2` e' a terceira de tras para frente. Producao
# cita uma mensagem de ~90 s atras, com respostas do bot embaixo dela -- e ate'
# agora todos os laboratorios citavam a ULTIMA, que e' o caso mais facil: ela
# esta' sempre visivel, sem precisar rolar.
ULTIMA_RECEBIDA_JS = r"""
(pular) => {
  let vistas = 0;
  for (const linha of [...document.querySelectorAll('#main div[role="row"]')].reverse()) {
    const el = linha.querySelector('[data-id]');
    const id = el ? el.getAttribute('data-id') : '';
    if (!id) continue;
    // `album-3EB0...` também é nosso: o WhatsApp agrupa nossas imagens.
    const nu = id.startsWith('album-') ? id.slice(6) : id;
    const nossa = nu.startsWith('true_') || nu.startsWith('3EB0')
      || !!linha.querySelector('[data-icon^="msg-"], [data-icon^="status-"]');
    if (nossa) continue;
    if (vistas++ < (pular || 0)) continue;
    const r = linha.getBoundingClientRect();
    return { id, ordem: vistas - 1,
             visivel: r.top >= 0 && r.bottom <= window.innerHeight,
             topo: Math.round(r.top),
             texto: (linha.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 50) };
  }
  return null;
}
"""


def main() -> int:
    repeticoes = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    completo = "--completo" in sys.argv
    # `--pular N`: citar a N-ésima mensagem recebida de trás para frente.
    # Produção nunca cita a última — há respostas do bot embaixo dela.
    pular = 0
    if "--pular" in sys.argv:
        pular = int(sys.argv[sys.argv.index("--pular") + 1])
    config = load_config()

    titulo("O serviço REAL citando")
    diga(f"grupo      {config.whatsapp_group_name!r}")
    diga(f"headless   {config.whatsapp_headless}")
    diga(f"reply_quote{config.reply_quote!s:>5}")
    diga(f"repetições {repeticoes}")
    diga(f"pular      {pular} (0 = a última recebida)")
    diga("")
    diga("NADA será enviado ao grupo: só `_citar`, que arma a barra.")

    trava = TravaDeInstancia(config.whatsapp_profile_dir, rotulo="envio real")
    try:
        trava.adquirir()
    except InstanciaEmUso as conflito:
        print(explicar(conflito, config.whatsapp_profile_dir), flush=True)
        return 1

    registros: list[str] = []

    servico = WhatsAppService(
        profile_dir=config.whatsapp_profile_dir,
        group_name=config.whatsapp_group_name,
        state=StateStore(config.state_path),
        poll_seconds=config.poll_seconds,
        headless=config.whatsapp_headless,
        reply_quote=config.reply_quote,
        browser_executable=config.browser_path,
        bot_self_name=config.bot_self_name,
        on_log=lambda nivel, msg: (
            registros.append(f"{nivel}: {msg}"),
            print(f"      [{nivel}] {msg[:150]}", flush=True)),
    )

    servico.start()
    try:
        titulo("SUBINDO — igual ao boot do bot")
        limite = time.monotonic() + 150
        while time.monotonic() < limite:
            if servico.status.connected:
                break
            time.sleep(1)
        if not servico.status.connected:
            diga(f"[FALHA] não conectou: {servico.status.state} "
                 f"{servico.status.last_error!r}")
            return 1
        diga(f"conectado. conversa={servico.status.chat_name!r}")

        # Deixar o laço de leitura rodar alguns ciclos: produção SEMPRE tem
        # esse laço ativo quando envia, e é uma das diferenças em relação aos
        # outros laboratórios.
        diga("deixando o laço de leitura rodar 3 ciclos...")
        time.sleep(max(config.poll_seconds * 3, 6))

        alvo = servico.call(
            lambda: servico._page.evaluate(ULTIMA_RECEBIDA_JS, pular), timeout=30)
        if not alvo:
            diga("[FALHA] nenhuma mensagem recebida na tela.")
            diga("Mande um CPF no grupo pelo celular e rode de novo.")
            return 1
        diga(f"alvo: {alvo['id']}  {alvo['texto']!r}")
        diga(f"      {alvo['ordem']}ª de trás para frente, "
             f"visível={alvo['visivel']} topo={alvo['topo']}px")

        cartoes = sorted((RAIZ / "comprovantes").glob("*.png"),
                         key=lambda f: f.stat().st_mtime, reverse=True)
        cartao = next((c for c in cartoes if c.stat().st_size > 1000), None)
        if completo and cartao is None:
            diga("[FALHA] nenhum comprovante para anexar")
            return 1

        # ------------------------------------------- instrumentação fina
        #
        # O log do serviço diz "o menu não abriu", mas não diz o que houve
        # ENTRE achar a setinha e desistir. Envolvemos os dois métodos para
        # fotografar exatamente esse intervalo: onde o clique caiu, o que a
        # tela virou logo depois, e como ela estava quando a espera estourou.
        passos: list[str] = []
        setinha_original = servico._esperar_a_setinha
        menu_original = servico._esperar_o_menu

        def setinha_espiada(message_id: str = ""):
            r = setinha_original(message_id)
            passos.append(f"setinha: achou={r.get('achou')} "
                          f"rotulo={r.get('rotulo')!r} "
                          f"x={r.get('x')} y={r.get('y')} "
                          f"motivo={r.get('motivo')!r}")
            if r.get("achou"):
                # O QUE ESTÁ naquele ponto? Se não for a setinha, o clique
                # cai em outra coisa e o menu nunca abre — e o log diria
                # "cliquei" do mesmo jeito.
                try:
                    sob = servico._page.evaluate(r"""
                      ([x, y]) => {
                        const el = document.elementFromPoint(x, y);
                        if (!el) return { nada: true };
                        const cadeia = [];
                        let p = el;
                        for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
                          cadeia.push({
                            tag: p.tagName.toLowerCase(),
                            aria: p.getAttribute('aria-label'),
                            role: p.getAttribute('role'),
                            icone: p.getAttribute('data-icon'),
                          });
                        }
                        const alvo = document.querySelector('[data-allana-linha]');
                        const rl = alvo ? alvo.getBoundingClientRect() : null;
                        return { cadeia, linha: rl ? {
                          x: Math.round(rl.x), dir: Math.round(rl.right),
                          y: Math.round(rl.y), base: Math.round(rl.bottom) } : null };
                      }""", [r["x"], r["y"]])
                    passos.append(f"  sob o ponto do clique: {sob}")
                except Exception as exc:
                    passos.append(f"  sondagem falhou: {exc}")
            return r

        def menu_espiado():
            # ANTES de esperar: a tela logo após o clique na setinha.
            try:
                servico._page.screenshot(
                    path=str(SAIDA / f"apos_clique_{len(passos):02d}.png"))
                estado = servico._page.evaluate(r"""
                  () => {
                    const lateral = document.querySelector('#pane-side');
                    const menus = [...document.querySelectorAll(
                      '[role="menu"], [role="menubar"], [role="dialog"], [role="listbox"]')]
                      .map((m) => {
                        const r = m.getBoundingClientRect();
                        return { role: m.getAttribute('role'),
                                 naLateral: !!(lateral && lateral.contains(m)),
                                 w: Math.round(r.width), h: Math.round(r.height),
                                 texto: (m.innerText||'').replace(/\s+/g,' ').trim().slice(0,60) };
                      });
                    return { menus,
                      // Tudo que apareceu com aparência de menu, sem filtro de role.
                      flutuantes: [...document.querySelectorAll('div')]
                        .filter((d) => {
                          const e = getComputedStyle(d);
                          return (e.position === 'absolute' || e.position === 'fixed')
                              && d.getBoundingClientRect().width > 120
                              && d.getBoundingClientRect().height > 80
                              && /responder|reply/i.test(d.innerText || '');
                        })
                        .map((d) => ({ role: d.getAttribute('role'),
                                       classe: (d.className || '').toString().slice(0, 40),
                                       texto: (d.innerText||'').replace(/\s+/g,' ').trim().slice(0,70) }))
                        .slice(0, 4),
                    };
                  }""")
                passos.append(f"logo apos o clique: menus={estado.get('menus')}")
                passos.append(f"  flutuantes com 'responder': {estado.get('flutuantes')}")
            except Exception as exc:
                passos.append(f"instrumentacao falhou: {exc}")

            r = menu_original()
            passos.append(f"esperar_o_menu -> {r}")
            if r:
                # O menu existe. O QUE ele é? ITEM_RESPONDER_JS não acha item
                # nenhum nele, então os dois estão vendo coisas diferentes.
                try:
                    detalhe = servico._page.evaluate(r"""
                      () => {
                        const lateral = document.querySelector('#pane-side');
                        return [...document.querySelectorAll('[role="menu"], [role="menubar"]')]
                          .filter((m) => !(lateral && lateral.contains(m)))
                          .map((m) => {
                            const r = m.getBoundingClientRect();
                            return {
                              role: m.getAttribute('role'),
                              w: Math.round(r.width), h: Math.round(r.height),
                              x: Math.round(r.x), y: Math.round(r.y),
                              filhos: m.children.length,
                              innerText: (m.innerText || '').replace(/\s+/g,' ').trim().slice(0,120),
                              textContent: (m.textContent || '').replace(/\s+/g,' ').trim().slice(0,120),
                              qtdMenuitem: m.querySelectorAll('[role="menuitem"]').length,
                              qtdLi: m.querySelectorAll('li').length,
                              qtdDivTab: m.querySelectorAll('div[tabindex]').length,
                              qtdBotao: m.querySelectorAll('[role="button"]').length,
                              qtdDiv: m.querySelectorAll('div').length,
                              html: (m.innerHTML || '').slice(0, 200),
                            };
                          });
                      }""")
                    for m in detalhe:
                        passos.append(f"  MENU: role={m['role']} {m['w']}x{m['h']} "
                                      f"em ({m['x']},{m['y']}) filhos={m['filhos']}")
                        passos.append(f"    innerText={m['innerText']!r}")
                        passos.append(f"    textContent={m['textContent']!r}")
                        passos.append(f"    menuitem={m['qtdMenuitem']} li={m['qtdLi']} "
                                      f"divTab={m['qtdDivTab']} botao={m['qtdBotao']} "
                                      f"div={m['qtdDiv']}")
                        passos.append(f"    html={m['html']!r}")
                except Exception as exc:
                    passos.append(f"  detalhe falhou: {exc}")
            return r

        servico._esperar_a_setinha = setinha_espiada
        servico._esperar_o_menu = menu_espiado

        # ============================================================
        #  O CAMINHO INTEIRO, com o disparo NEUTRALIZADO
        # ============================================================
        #
        # `_enviar_imagem` faz: garantir conversa -> citar -> anexar ->
        # digitar legenda -> DISPARAR. Só o último passo escreve no grupo, e
        # é só ele que trocamos por um registro. Tudo antes roda igual a
        # produção, no serviço real, na thread dona.
        disparos: list[str] = []

        def nao_disparar():
            estado = servico._page.evaluate(CITACAO_ATIVA_JS, alvo["texto"]) or {}
            disparos.append(
                f"NÃO DISPAREI. citação ativa={estado.get('ativa')} "
                f"local={estado.get('local')!r}")
            try:
                servico._page.screenshot(
                    path=str(SAIDA / f"completo_{len(disparos):02d}.png"))
            except Exception:
                pass
            # Fechar o preview AQUI, enquanto ele com certeza está aberto.
            # A limpeza do caminho de erro não dava conta sozinha, e o preview
            # sobrevivente contaminava a volta seguinte.
            try:
                servico._limpar_preview()
                # PREVIEW_ABERTO_JS, não `img[src^="blob:"]`: imagens já
                # enviadas na conversa também são blob, e o detector antigo
                # dizia "preview aberto" com a tela limpa.
                sobrou = servico._page.evaluate(PREVIEW_ABERTO_JS)
                disparos.append(f"preview ainda aberto depois de limpar: {sobrou}")
                if sobrou:
                    # Botão de fechar do preview, se Escape não bastou.
                    fechar = servico._page.locator(
                        '[aria-label="Fechar"], [aria-label="Close"], '
                        '[data-icon="x"], [data-icon="x-viewer"]').last
                    if fechar.count():
                        fechar.click(timeout=4000)
                        servico._page.wait_for_timeout(400)
                    disparos.append("depois do botão fechar: "
                                    + str(servico._page.evaluate(PREVIEW_ABERTO_JS)))
            except Exception as exc:
                disparos.append(f"limpeza falhou: {str(exc)[:90]}")
            raise RuntimeError("disparo neutralizado pelo laboratório")

        servico._disparar_envio = nao_disparar

        # -------------------------------------------------- as repetições
        resultados = []
        for volta in range(1, repeticoes + 1):
            titulo(f"TENTATIVA {volta}/{repeticoes} — o caminho de produção")

            # Exatamente o que `_enviar_imagem` faz antes de citar.
            diga("_garantir_conversa(...)")
            ok = servico.call(servico._garantir_conversa,
                              config.whatsapp_group_name, timeout=60)
            diga(f"  -> {ok}")
            if not ok:
                diga("[FALHA] não garantiu a conversa")
                resultados.append((volta, None, "sem conversa"))
                continue

            if completo:
                diga("_enviar_imagem(...)  [caminho INTEIRO, sem disparar]")
                marca = len(registros)
                try:
                    # `_do_send_image` e' a entrada REAL de producao: ela
                    # envolve `_enviar_imagem` com o registro de tempo e,
                    # sobretudo, com a limpeza da tela no caminho de erro.
                    # Chamar `_enviar_imagem` direto pulava essa limpeza e
                    # deixava o preview aberto contaminando a volta seguinte
                    # -- um defeito do laboratorio, nao do sistema.
                    servico.call(servico._do_send_image,
                                 "", config.whatsapp_group_name, str(cartao),
                                 "✅ *R$ 34.509,85* · TESTE" + chr(10) + "_REQ999999_",
                                 alvo["id"], "", timeout=180)
                except Exception as exc:
                    if "neutralizado" not in str(exc):
                        diga(f"  estourou antes do disparo: {str(exc)[:160]}")
                for linha in registros[marca:]:
                    diga(f"     {linha[:150]}")
                for linha in disparos:
                    diga(f"  >>> {linha}")
                citou = any("ativa=True" in d for d in disparos)
                resultados.append((volta, citou, "caminho inteiro"))
                disparos.clear()
                servico.call(servico._limpar_ui, timeout=30)
                servico.call(servico._limpar_preview, timeout=30)
                time.sleep(2)
                continue

            diga("_citar(...)  [o método REAL, na thread dona]")
            marca = len(registros)
            comeco = time.monotonic()
            citou = servico.call(servico._citar, alvo["id"], "imagem", timeout=60)
            gasto = time.monotonic() - comeco
            diga(f"  -> citou={citou}  em {gasto:.1f}s")

            novos = registros[marca:]
            for linha in novos:
                diga(f"     {linha[:160]}")

            if passos:
                diga("")
                diga("  --- o que aconteceu entre a setinha e a desistência ---")
                for linha in passos:
                    diga(f"    {linha[:300]}")
                passos.clear()

            estado = servico.call(lambda: servico._estado_da_citacao, timeout=15)
            if estado:
                diga("")
                diga(f"  onde parou: {estado.get('onde')}")
                diga(f"  linha_no_dom={estado.get('achou_linha')} "
                     f"menus={estado.get('menus')}")
                diga(f"  rótulos={estado.get('rotulos')}")
                diga(f"  botões={estado.get('botoes')}")

            try:
                servico.call(lambda: servico._page.screenshot(
                    path=str(SAIDA / f"real_{volta:02d}.png")), timeout=30)
                diga(f"  [foto] real_{volta:02d}.png")
            except Exception:
                pass

            resultados.append((volta, citou, estado.get("onde") if estado else ""))

            # Limpar a citação armada antes da próxima volta, senão a segunda
            # herda o estado da primeira.
            if citou:
                servico.call(servico._limpar_ui, timeout=30)
            time.sleep(2)

        titulo("RESUMO")
        for volta, citou, onde in resultados:
            marca = "OK  " if citou else "FALHA"
            diga(f"  tentativa {volta}: {marca}  {onde or ''}")
        quantas = sum(1 for _, c, _ in resultados if c)
        diga("")
        diga(f"  {quantas}/{len(resultados)} citações funcionaram no SERVIÇO REAL")
        if quantas == len(resultados):
            diga("  >>> O serviço cita bem. A falha de produção vem de outra")
            diga("  >>> coisa no caminho — provavelmente do estado deixado por")
            diga("  >>> um envio anterior.")
        elif quantas == 0:
            diga("  >>> REPRODUZIDO. A diferença está no serviço, não no JS.")
        else:
            diga("  >>> INTERMITENTE. Compare as fotos das voltas.")
        return 0
    except Exception:
        titulo("ESTOUROU")
        traceback.print_exc()
        return 1
    finally:
        diga("")
        diga("encerrando o serviço...")
        try:
            servico.stop(timeout=20)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
