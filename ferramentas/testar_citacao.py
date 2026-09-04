"""Laboratório da citação — roda o caminho real e conta o que aconteceu.

Por que existe
--------------
A citação já falhou por quatro causas diferentes, e cada rodada custava dez
minutos porque só descobríamos no grupo do cliente. Aqui o mesmo código de
produção roda sozinho, contra o WhatsApp de verdade, e diz passo a passo onde
parou.

**O JS da citação é IMPORTADO de app/whatsapp.py, nunca copiado.** Se ele
passar aqui, é o que roda em produção que passou. Uma cópia adaptada provaria
apenas que a cópia funciona.

Antes de rodar
--------------
* o bot PARADO (o perfil aceita um processo por vez)
* o Brave FECHADO (nenhum brave.exe no Gerenciador de Tarefas)

Uso::

    .venv/Scripts/python.exe ferramentas/testar_citacao.py

Deixa o navegador ABERTO no fim, de propósito: a tela é a evidência.
Screenshots de cada passo em ``diagnostico/citacao_NN_*.png``.
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
from app.whatsapp import WhatsAppService  # noqa: E402
from app.whatsapp import (  # noqa: E402
    BOTAO_DE_OPCOES_JS,
    CITACAO_ATIVA_JS,
    GEOMETRIA_DA_LINHA_JS,
    ITEM_RESPONDER_JS,
    MENU_ABERTO_JS,
    TEXTO_DA_MENSAGEM_JS,
    CAMPO_DA_LEGENDA_JS,
    COLAR_IMAGEM_JS,
    PREVIEW_E_IMAGEM_JS,
)

SAIDA = RAIZ / "diagnostico"
SAIDA.mkdir(exist_ok=True)

_passo = 0


def foto(pagina, nome: str) -> None:
    global _passo
    _passo += 1
    alvo = SAIDA / f"citacao_{_passo:02d}_{nome}.png"
    try:
        pagina.screenshot(path=str(alvo))
        print(f"        [foto] {alvo.name}", flush=True)
    except Exception as exc:
        print(f"        [foto falhou] {exc}", flush=True)


def titulo(texto: str) -> None:
    print("", flush=True)
    print("=" * 72, flush=True)
    print(texto, flush=True)
    print("=" * 72, flush=True)


def diga(texto: str) -> None:
    print(f"  {texto}", flush=True)


def segurar_a_janela() -> None:
    """Trava aqui para o navegador continuar aberto.

    Precisa acontecer DENTRO do ``with sync_playwright()``: sair do bloco
    encerra o Playwright e mata a janela junto -- e a tela é a evidência que
    este laboratório existe para mostrar.
    """
    print("", flush=True)
    print("  >>> O navegador está ABERTO para você ver a tela.", flush=True)
    print("  >>> Pressione Enter (ou Ctrl+C) para fechá-lo.", flush=True)
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        # Sem terminal interativo: segura um tempo e solta sozinho.
        print("  (sem terminal interativo — segurando 90 s)", flush=True)
        try:
            time.sleep(90)
        except KeyboardInterrupt:
            pass


# Acha a última mensagem RECEBIDA (autor != nome do bot) e devolve o data-id.
#
# O sinal de autoria desta instalação é o `data-pre-plain-text`, no formato
# "[HH:MM, DD/MM/AAAA] Nome: ". As classes message-in/message-out simplesmente
# não existem aqui — foi o que fez a primeira versão do bot nunca ler nada.
ULTIMA_RECEBIDA_JS = r"""
(nomeProprio) => {
  const norm = (s) => (s || '').normalize('NFC').trim().toLowerCase();
  const meu = norm(nomeProprio);

  const autorDa = (linha) => {
    const el = linha.querySelector('[data-pre-plain-text]');
    if (!el) return '';
    const bruto = el.getAttribute('data-pre-plain-text') || '';
    const m = bruto.match(/\]\s*([^:]+):\s*$/);
    return m ? m[1].trim() : '';
  };

  const linhas = [...document.querySelectorAll('#main div[role="row"]')];
  const autores = new Set();
  for (let i = linhas.length - 1; i >= 0; i--) {
    const autor = autorDa(linhas[i]);
    if (autor) autores.add(autor);
    if (!autor || (meu && norm(autor) === meu)) continue;

    const idEl = linhas[i].querySelector('[data-id]');
    const id = idEl ? idEl.getAttribute('data-id') : '';
    if (!id) continue;
    return {
      achou: true, id, autor,
      texto: (linhas[i].innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80),
      autores: [...autores],
    };
  }
  return { achou: false, autores: [...autores], linhas: linhas.length };
}
"""

# Devolve a COORDENADA do item na lista -- nao clica.
#
# `el.click()` em JS nao abre a conversa: o WhatsApp e' React e responde a
# eventos de ponteiro reais, nao ao clique sintetico do DOM. A primeira versao
# deste laboratorio dizia "conversa aberta" e seguia com a tela inicial do
# WhatsApp na frente; as "25 linhas" que ele achava eram a LISTA DE CONVERSAS
# da lateral, nao mensagens. Mesma classe de erro da setinha: acao sintetica
# onde o app espera entrada de verdade.
ACHAR_GRUPO_JS = r"""
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
  const vistos = [...document.querySelectorAll('[role="listitem"], [role="gridcell"]')]
    .map((el) => (el.innerText || '').split(String.fromCharCode(10))[0]).filter(Boolean).slice(0, 15);
  return { ok: false, vistos };
}
"""

# A conversa esta' REALMENTE aberta? O cabecalho e' a prova.
CONVERSA_ABERTA_JS = r"""
(nome) => {
  const norm = (s) => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
                               .toLowerCase().trim();
  const cab = document.querySelector('#main header');
  if (!cab) return { aberta: false, motivo: 'nao ha #main header' };
  const titulo = (cab.innerText || '').split(String.fromCharCode(10))[0];
  return {
    aberta: norm(cab.innerText).includes(norm(nome)),
    titulo,
    linhas: document.querySelectorAll('#main div[role="row"]').length,
  };
}
"""


def rodar(pagina, config) -> int:
    """Os seis passos. Devolve 0 quando a citação sai de verdade."""
    grupo = config.whatsapp_group_name

    # ----------------------------------------------------- abrir a conversa
    titulo("PASSO 0 — abrir a conversa")
    achado = pagina.evaluate(ACHAR_GRUPO_JS, grupo) or {}
    if not achado.get("ok"):
        diga(f"[FALHA] não achei {grupo!r} na lista de conversas.")
        diga(f"        conversas visíveis: {achado.get('vistos')}")
        foto(pagina, "sem_grupo")
        return 1

    if achado.get("ja"):
        diga("já estava aberta")
    else:
        # Clique de MOUSE, na coordenada. Ver o comentário de ACHAR_GRUPO_JS.
        diga(f"clicando na lista em x={achado['x']:.0f} y={achado['y']:.0f}")
        pagina.mouse.click(achado["x"], achado["y"])
        time.sleep(2)

    # Não acreditar no clique: conferir o cabeçalho.
    estado = pagina.evaluate(CONVERSA_ABERTA_JS, grupo) or {}
    diga(f"cabeçalho: {estado.get('titulo')!r}")
    diga(f"linhas de mensagem em #main: {estado.get('linhas')}")
    foto(pagina, "conversa")
    if not estado.get("aberta"):
        diga("")
        diga("[FALHA] cliquei, mas a conversa não abriu.")
        diga(f"        {estado.get('motivo') or 'o cabeçalho não é do grupo'}")
        return 1
    diga("conversa aberta e confirmada")

    # ------------------------------------- a última mensagem do consultor
    titulo("PASSO 0b — última mensagem RECEBIDA")
    alvo = pagina.evaluate(ULTIMA_RECEBIDA_JS, config.bot_self_name) or {}
    diga(f"autores visíveis: {alvo.get('autores')}")
    if not alvo.get("achou"):
        diga("[FALHA] nenhuma mensagem recebida na tela.")
        diga(f"linhas na tela: {alvo.get('linhas')}")
        diga("Mande um CPF no grupo pelo celular e rode de novo.")
        foto(pagina, "sem_recebida")
        return 1
    diga(f"autor    {alvo['autor']!r}")
    diga(f"data-id  {alvo['id']}")
    diga(f"texto    {alvo['texto']!r}")

    conhecidos = {a.strip().lower() for a in alvo.get("autores", [])}
    if config.bot_self_name.strip().lower() not in conhecidos:
        diga("")
        diga(f"[aviso] BOT_SELF_NAME={config.bot_self_name!r} não está entre os "
             "autores. Não atrapalha este teste, mas em produção o bot pode "
             "tratar as próprias respostas como pedidos.")

    message_id = alvo["id"]
    # O CORPO, pelo mesmo JS que a produção usa. O texto da linha traz autor e
    # horário, e comparar com ele recusa citação boa em mensagem curta.
    texto_original = (pagina.evaluate(TEXTO_DA_MENSAGEM_JS, message_id) or "").strip()
    diga(f"corpo    {texto_original!r}")

    # ------------------------------------------------- PASSO 1: a linha
    titulo("PASSO 1 — achar a linha")
    linha = pagina.evaluate(GEOMETRIA_DA_LINHA_JS, [message_id, texto_original]) or {}
    diga(f"achou={linha.get('achou')}  via={linha.get('via')!r}")
    if not linha.get("achou"):
        diga(f"[FALHA] {linha.get('motivo')}")
        foto(pagina, "sem_linha")
        return 1
    # O scrollIntoView mexeu na página: reler a geometria para o mouse ir
    # aonde a linha ESTÁ, e não aonde ela estava.
    time.sleep(0.4)
    linha = pagina.evaluate(GEOMETRIA_DA_LINHA_JS, [message_id, texto_original]) or {}
    if not linha.get("achou"):
        diga("[FALHA] a linha sumiu ao rolar.")
        return 1
    diga(f"balão    x={linha['x']:.0f} y={linha['y']:.0f}")
    if linha.get("centroDaLinha"):
        c = linha["centroDaLinha"]
        diga(f"(centro da linha inteira: x={c['x']:.0f} y={c['y']:.0f} — "
             "mirar aqui cai no fundo vazio)")
    diga(f"texto    {linha.get('texto')!r}")
    foto(pagina, "linha")

    # ------------------------------------- PASSO 2: hover, e não sair dali
    titulo("PASSO 2 — hover (o mouse não sai daqui)")
    pagina.mouse.move(linha["x"], linha["y"])
    diga(f"mouse em x={linha['x']:.0f} y={linha['y']:.0f}")
    time.sleep(0.6)
    foto(pagina, "hover")

    # ----------------------------------------------- PASSO 3: a setinha
    titulo("PASSO 3 — a setinha de opções")
    limite = time.monotonic() + 3.0
    botao: dict = {}
    while True:
        botao = pagina.evaluate(BOTAO_DE_OPCOES_JS, message_id) or {}
        if botao.get("achou") or time.monotonic() >= limite:
            break
        pagina.wait_for_timeout(120)

    if botao.get("achou"):
        diga(f"ACHOU   aria-label={botao.get('rotulo')!r}")
        diga(f"        x={botao['x']:.0f} y={botao['y']:.0f}")
        foto(pagina, "setinha")
        pagina.mouse.click(botao["x"], botao["y"])
        diga("cliquei na setinha")
    else:
        diga(f"NÃO ACHOU: {botao.get('motivo')}")
        if botao.get("idNaTela"):
            diga(f"        a linha virou {botao['idNaTela']} (chegou mensagem nova)")
        diga(f"        aria-labels na linha: {botao.get('visto')}")
        diga(f"        data-icons na linha:  {botao.get('icones')}")
        foto(pagina, "sem_setinha")
        diga("caindo para o botão direito no centro da linha")
        pagina.mouse.click(linha["x"], linha["y"], button="right")

    # -------------------------------------- PASSO 4: o menu e "Responder"
    titulo("PASSO 4 — o menu e o item 'Responder'")
    limite = time.monotonic() + 3.0
    abriu = False
    while True:
        abriu = bool(pagina.evaluate(MENU_ABERTO_JS))
        if abriu or time.monotonic() >= limite:
            break
        pagina.wait_for_timeout(120)
    diga(f"menu aberto: {abriu}")
    foto(pagina, "menu")

    item = pagina.evaluate(ITEM_RESPONDER_JS, ["responder", "reply"]) or {}
    diga(f"itens na tela: {item.get('rotulos')}")
    if not item.get("achou"):
        diga("")
        diga("[FALHA] nenhum item com texto EXATAMENTE 'responder'.")
        if item.get("recusado"):
            diga(f"        recusei {item['recusado']!r} por ser perigoso")
        if item.get("motivo"):
            diga(f"        {item['motivo']}")
        diga("        Se 'Responder' está na lista acima com outro nome,")
        diga("        basta acrescentá-lo em _TEXTO_RESPONDER.")
        return 1
    diga(f"casou com  {item['rotulo']!r}")
    pagina.mouse.click(item["x"], item["y"])
    diga("cliquei em Responder")
    time.sleep(0.7)
    foto(pagina, "clicou_responder")

    # --------------------------------- PASSO 5: a barra apareceu mesmo?
    titulo("PASSO 5 — a barra de citação apareceu?")
    limite = time.monotonic() + 2.5
    estado: dict = {}
    while True:
        estado = pagina.evaluate(CITACAO_ATIVA_JS, texto_original) or {}
        if estado.get("ativa") or time.monotonic() >= limite:
            break
        pagina.wait_for_timeout(150)

    diga(f"ativa={estado.get('ativa')}")
    diga(f"rodapé: {estado.get('rodape')!r}")
    foto(pagina, "barra")

    if not estado.get("ativa"):
        diga("")
        diga("[FALHA] cliquei em Responder mas a barra não apareceu no rodapé.")
        return 1

    # ------------------------------------------- PASSO 6: enviar de verdade
    # ============================================================
    #  PASSO 6 — o fluxo COMPLETO de produção
    # ============================================================
    #
    # Até aqui o laboratório provava a citação enviando TEXTO. Produção faz
    # outra coisa: cita, COLA A IMAGEM, digita a legenda e só então envia.
    # Esse caminho nunca tinha sido exercitado, e é o único que roda de
    # verdade quando há resultado para mandar.
    #
    # A pergunta que ele responde: a citação SOBREVIVE ao paste da imagem?
    # Se não sobreviver, o código de hoje marca `citou=True`, escolhe a
    # legenda curta (sem "↩ consultor") e a mensagem sai sem citação E sem o
    # nome — o pior dos dois mundos, e hoje invisível.
    titulo("PASSO 6 — colar a imagem COM a citação armada")

    cartoes = sorted((RAIZ / "comprovantes").glob("*.png"),
                     key=lambda f: f.stat().st_mtime, reverse=True)
    cartao = next((c for c in cartoes if c.stat().st_size > 1000), None)
    if cartao is None:
        diga("[FALHA] nenhum comprovante em comprovantes/ para colar.")
        return 1
    diga(f"usando {cartao.name} ({cartao.stat().st_size} bytes)")

    import base64
    b64 = base64.b64encode(cartao.read_bytes()).decode("ascii")
    colou = pagina.evaluate(COLAR_IMAGEM_JS, [b64, cartao.name])
    diga(f"colagem: {colou}")
    if isinstance(colou, dict) and not colou.get("ok"):
        diga("[FALHA] a colagem não aconteceu.")
        return 1

    limite = time.monotonic() + 12
    while time.monotonic() < limite:
        if (pagina.evaluate(PREVIEW_E_IMAGEM_JS) or {}).get("preview"):
            break
        pagina.wait_for_timeout(200)
    pagina.wait_for_timeout(700)
    foto(pagina, "preview_com_citacao")

    titulo("A CITAÇÃO SOBREVIVEU AO PASTE?")
    depois = pagina.evaluate(CITACAO_ATIVA_JS, texto_original) or {}
    diga(f"ativa={depois.get('ativa')}   local={depois.get('local')!r}")
    if not depois.get("ativa"):
        diga(f"procurei em: {depois.get('procurouEm')}")
    diga(f"texto ao redor: {depois.get('rodape')!r}")

    if depois.get("ativa"):
        diga("")
        diga(">>> A citação SOBREVIVEU. Se ela estiver em 'preview' e não em")
        diga(">>> 'footer', a verificação antiga (só footer) a daria como")
        diga(">>> perdida — e o código descartaria uma citação boa.")
    else:
        diga("")
        diga(">>> A citação CAIU ao colar a imagem. Aí o defeito é outro:")
        diga(">>> citar antes de anexar não funciona, e a ordem tem de mudar.")

    titulo("PASSO 7 — digitar a legenda")
    escolha = pagina.evaluate(CAMPO_DA_LEGENDA_JS,
                              list(WhatsAppService._ROTULOS_DA_LEGENDA)) or {}
    if not escolha.get("achou"):
        diga(f"[FALHA] campo da legenda não identificado ({escolha.get('quantos')})")
        for c in escolha.get("inventario") or []:
            diga(f"   {c}")
        return 1
    diga(f"campo: índice={escolha['indice']} aria={escolha['aria']!r}")
    alvo = pagina.locator('[contenteditable="true"]').nth(escolha["indice"])
    alvo.click(timeout=8000)
    pagina.keyboard.insert_text("teste do fluxo completo")
    pagina.wait_for_timeout(400)
    foto(pagina, "legenda_digitada")

    titulo("A CITAÇÃO AINDA ESTÁ LÁ, ANTES DO ENTER?")
    antes_do_enter = pagina.evaluate(CITACAO_ATIVA_JS, texto_original) or {}
    diga(f"ativa={antes_do_enter.get('ativa')}   "
         f"local={antes_do_enter.get('local')!r}")
    diga(f"texto ao redor: {antes_do_enter.get('rodape')!r}")

    titulo("RESUMO DOS TRÊS MOMENTOS")
    diga("  depois de citar          ativa=True   local='footer'")
    diga(f"  depois de colar a imagem ativa={str(depois.get('ativa')):<5}  "
         f"local={depois.get('local')!r}")
    diga(f"  antes do Enter           ativa={str(antes_do_enter.get('ativa')):<5}  "
         f"local={antes_do_enter.get('local')!r}")

    diga("")
    diga("NÃO vou apertar Enter — nada é enviado ao grupo.")
    diga("Cancele o preview no navegador quando terminar de olhar.")
    return 0 if antes_do_enter.get("ativa") else 2


def main() -> int:
    config = load_config()
    perfil = config.whatsapp_profile_dir
    executavel = config.browser_path

    titulo("Laboratório da citação")
    diga(f"grupo     {config.whatsapp_group_name!r}")
    diga(f"perfil    {perfil}")
    diga(f"navegador {executavel or '(Chromium do Playwright)'}")
    diga(f"bot       {config.bot_self_name!r}   (BOT_SELF_NAME)")

    if not config.whatsapp_group_name:
        diga("")
        diga("[FALHA] WHATSAPP_GROUP_NAME vazio no .env.")
        return 1

    # A mesma trava do bot, e de proposito: rodar este laboratorio com o bot
    # no ar e' exatamente o conflito de dois processos no mesmo perfil.
    trava = TravaDeInstancia(perfil, rotulo="laboratório da citação")
    try:
        trava.adquirir()
    except InstanciaEmUso as conflito:
        print(explicar(conflito, perfil), flush=True)
        return 1

    with sync_playwright() as pw:
        opcoes = {"headless": False, "args": ["--start-maximized"]}
        if executavel and Path(executavel).exists():
            opcoes["executable_path"] = executavel

        try:
            ctx = pw.chromium.launch_persistent_context(str(perfil), **opcoes)
        except Exception as exc:
            titulo("NÃO CONSEGUI ABRIR O NAVEGADOR")
            diga(str(exc)[:300])
            diga("")
            diga("Quase sempre é o perfil em uso por outro processo.")
            diga("Pare o bot e feche o Brave, depois rode de novo.")
            return 1

        pagina = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if "web.whatsapp.com" not in (pagina.url or ""):
                pagina.goto("https://web.whatsapp.com")

            diga("")
            diga("aguardando o WhatsApp carregar (até 2 min)...")
            try:
                pagina.wait_for_selector('[role="row"], [aria-label]', timeout=120_000)
            except Exception:
                titulo("O WHATSAPP NÃO CARREGOU")
                diga("A tela está no navegador. Provavelmente pede QR.")
                foto(pagina, "sem_carregar")
                return 1
            time.sleep(3)

            try:
                return rodar(pagina, config)
            except Exception:
                # O traceback ANTES de segurar a janela. Sem isto ele só
                # apareceria depois da espera, e um erro de JS virava
                # "o script não disse nada" -- o mesmo defeito silencioso
                # que este laboratório existe para eliminar.
                titulo("O LABORATÓRIO ESTOUROU")
                traceback.print_exc()
                sys.stdout.flush()
                try:
                    foto(pagina, "estouro")
                except Exception:
                    pass
                return 1
        finally:
            # Sempre, inclusive quando falha: a tela é a evidência.
            segurar_a_janela()


if __name__ == "__main__":
    sys.exit(main())
