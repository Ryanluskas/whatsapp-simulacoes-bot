"""Renderiza HTML em PNG, sem depender do navegador do WhatsApp.

Por que este arquivo existe
---------------------------
A imagem do resultado sempre foi gerada dentro do navegador que o WhatsApp
Web ja' mantinha aberto -- economia legitima enquanto havia um navegador
garantido. No modo ``evolution`` nao ha' navegador nenhum: a conversa com o
WhatsApp e' HTTP puro. Sem este modulo, migrar a camada de envio derrubaria
junto a geracao da imagem, que hoje funciona.

Entao o renderizador vira um servico proprio: um Chromium headless, dono da
sua thread, que so' sabe transformar HTML em PNG. Ele nao conhece WhatsApp,
nao conhece Evolution, e nao encosta no Playwright do Santander.

Preguicoso de proposito
-----------------------
O navegador so' sobe na primeira imagem pedida. Um sistema que roda o dia
inteiro sem nenhuma simulacao nao tem por que segurar um Chromium na
memoria. Se ele morrer, a proxima chamada sobe outro.

Regra herdada, e ela vale aqui igual: **Playwright nao atravessa thread.**
Toda operacao passa pelo ``ThreadActor``, como no resto do sistema.
"""

from __future__ import annotations

import struct
import threading
import zlib
from pathlib import Path

from .actor import ActorStopped, ThreadActor

ASSINATURA_PNG = b"\x89PNG\r\n\x1a\n"

#: Canais por tipo de cor do PNG (especificacao, secao 11.2.2).
_CANAIS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class PngInvalido(ValueError):
    """O arquivo nao serve para ser enviado como resultado."""


def validar_png(caminho: str | Path, request_id: str = "",
                pasta: str | Path | None = None) -> tuple[int, int]:
    """Confere que o arquivo e' um PNG legivel e do pedido certo.

    "Existe e tem bytes" nao basta: um PNG truncado no meio da gravacao passa
    nessa conferencia e chega ao grupo como imagem quebrada. Aqui o arquivo e'
    lido de verdade -- CRC de cada bloco, descompressao dos dados e tamanho
    batendo com as dimensoes -- sem depender de biblioteca de imagem.

    ``request_id`` e ``pasta`` amarram o arquivo ao pedido: o nome tem de ser
    exatamente ``<request_id>.png`` e ele tem de estar na pasta esperada. E' o
    que impede ``REQ000122.png`` de sair como resposta da ``REQ000123``.

    Devolve ``(largura, altura)`` ou levanta ``PngInvalido`` dizendo o motivo.
    """
    alvo = Path(caminho)
    if request_id and alvo.name != f"{request_id}.png":
        raise PngInvalido(f"o arquivo {alvo.name} não pertence a {request_id}")
    if pasta is not None and alvo.resolve().parent != Path(pasta).resolve():
        raise PngInvalido(f"o arquivo {alvo.name} está fora da pasta de comprovantes")
    if not alvo.is_file():
        raise PngInvalido(f"o PNG não existe: {alvo.name}")
    dados = alvo.read_bytes()
    if not dados:
        raise PngInvalido(f"o PNG está vazio: {alvo.name}")
    if not dados.startswith(ASSINATURA_PNG):
        raise PngInvalido(f"{alvo.name} não é PNG (assinatura inválida)")

    posicao = len(ASSINATURA_PNG)
    largura = altura = 0
    profundidade = tipo_cor = entrelacado = 0
    idat = bytearray()
    viu_fim = False
    while posicao + 12 <= len(dados):
        (tamanho,) = struct.unpack(">I", dados[posicao:posicao + 4])
        tipo = dados[posicao + 4:posicao + 8]
        inicio, fim = posicao + 8, posicao + 8 + tamanho
        if fim + 4 > len(dados):
            raise PngInvalido(f"{alvo.name} está truncado no bloco {tipo!r}")
        conteudo = dados[inicio:fim]
        (crc,) = struct.unpack(">I", dados[fim:fim + 4])
        if zlib.crc32(tipo + conteudo) & 0xFFFFFFFF != crc:
            raise PngInvalido(f"{alvo.name} tem o bloco {tipo!r} corrompido")
        if tipo == b"IHDR":
            if tamanho != 13:
                raise PngInvalido(f"{alvo.name} tem cabeçalho inválido")
            largura, altura, profundidade, tipo_cor, _c, _f, entrelacado = \
                struct.unpack(">IIBBBBB", conteudo)
        elif tipo == b"IDAT":
            idat += conteudo
        elif tipo == b"IEND":
            viu_fim = True
            break
        posicao = fim + 4

    if largura <= 0 or altura <= 0:
        raise PngInvalido(f"{alvo.name} não tem dimensões válidas")
    if not viu_fim:
        raise PngInvalido(f"{alvo.name} está incompleto (sem IEND)")
    try:
        bruto = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise PngInvalido(f"{alvo.name} tem os dados da imagem corrompidos") from exc
    if entrelacado == 0 and tipo_cor in _CANAIS:
        bits = _CANAIS[tipo_cor] * profundidade
        esperado = altura * (1 + (largura * bits + 7) // 8)
        if len(bruto) != esperado:
            raise PngInvalido(
                f"{alvo.name} tem {len(bruto)} bytes de imagem, esperava {esperado}")
    return largura, altura


class PngRenderer(ThreadActor):
    """Chromium headless dedicado a virar HTML em PNG."""

    def __init__(self, browser_path: str = "", name: str = "png-renderer") -> None:
        super().__init__(name)
        self.browser_path = browser_path
        self._playwright = None
        self._browser = None
        self._lock = threading.Lock()
        # Distingue "pediram para parar" de "a thread morreu sozinha": so' no
        # segundo caso o proximo render sobe o ator de novo.
        self._parado_de_proposito = False

    # -------------------------------------------------------------- ciclo de vida
    def run(self) -> None:
        """Fica so' atendendo comandos.

        Diferente do ator do WhatsApp, aqui nao ha' laco de trabalho proprio:
        este servico e' reativo. O navegador sobe sob demanda, dentro do
        primeiro comando que precisar dele.
        """
        while not self.stopping:
            if not self._drain(timeout=0.5):
                break
        self._teardown()

    def _garantir_navegador(self):
        """Sobe o Chromium se ainda nao subiu. So' na thread dona."""
        if self._browser is not None and self._browser.is_connected():
            return self._browser

        from playwright.sync_api import sync_playwright

        if self._playwright is None:
            self._playwright = sync_playwright().start()

        opcoes = {"headless": True}
        # O mesmo executavel do resto do sistema quando ele existe; sem ele,
        # o Chromium que vem com o Playwright (o caso do container).
        if self.browser_path and Path(self.browser_path).exists():
            opcoes["executable_path"] = self.browser_path

        self._browser = self._playwright.chromium.launch(**opcoes)
        return self._browser

    def _teardown(self) -> None:
        for fechar in (
            lambda: self._browser and self._browser.close(),
            lambda: self._playwright and self._playwright.stop(),
        ):
            try:
                fechar()
            except Exception:
                # Encerramento nao pode levantar: quem chama esta' desligando.
                pass
        self._browser = None
        self._playwright = None

    def start(self) -> None:
        self._parado_de_proposito = False
        super().start()

    def stop(self, timeout: float = 15.0) -> None:
        self._parado_de_proposito = True
        super().stop(timeout=timeout)

    # ------------------------------------------------------------------ publico
    def render_png(self, html: str, path: str | Path, width: int = 900,
                   timeout: float = 60.0) -> str:
        # A thread dona pode ter morrido (excecao fora do comando, processo do
        # driver derrubado). Se ninguem pediu para parar, sobe de novo: o
        # renderizador e' preguicoso, e a proxima imagem nao pode depender de
        # alguem reiniciar o bot.
        if not self.running and not self._parado_de_proposito:
            self.start()
        try:
            return self.call(self._do_render, html, str(path), width, timeout=timeout)
        except ActorStopped:
            if self._parado_de_proposito:
                raise
            self.start()
            return self.call(self._do_render, html, str(path), width, timeout=timeout)

    def _do_render(self, html: str, path: str, width: int) -> str:
        destino = Path(path)
        destino.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._render_uma_vez(html, destino, width)
        except PngInvalido:
            raise
        except Exception:
            # O navegador morreu entre uma imagem e outra (processo encerrado,
            # driver caido). `is_connected()` nem sempre percebe a tempo:
            # derruba tudo e tenta UMA vez com um Chromium novo.
            self._teardown()
            return self._render_uma_vez(html, destino, width)

    def _render_uma_vez(self, html: str, destino: Path, width: int) -> str:
        navegador = self._garantir_navegador()
        # deviceScaleFactor 2 deixa o texto nitido quando o WhatsApp reamostra
        # a imagem no celular -- mesmo valor da implementacao antiga, que ja'
        # esta' provada em producao.
        contexto = navegador.new_context(
            viewport={"width": width, "height": 600}, device_scale_factor=2)
        pagina = contexto.new_page()
        try:
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
                contexto.close()
            except Exception:
                pass

        # Mesma regra do envio: etapa sem prova e' etapa que falhou. Um PNG
        # de zero byte viraria "documento vazio" no grupo.
        validar_png(destino)
        return str(destino)
