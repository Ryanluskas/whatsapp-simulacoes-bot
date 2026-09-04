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

import threading
from pathlib import Path

from .actor import ThreadActor


class PngRenderer(ThreadActor):
    """Chromium headless dedicado a virar HTML em PNG."""

    def __init__(self, browser_path: str = "", name: str = "png-renderer") -> None:
        super().__init__(name)
        self.browser_path = browser_path
        self._playwright = None
        self._browser = None
        self._lock = threading.Lock()

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

    # ------------------------------------------------------------------ publico
    def render_png(self, html: str, path: str | Path, width: int = 900,
                   timeout: float = 60.0) -> str:
        return self.call(self._do_render, html, str(path), width, timeout=timeout)

    def _do_render(self, html: str, path: str, width: int) -> str:
        destino = Path(path)
        destino.parent.mkdir(parents=True, exist_ok=True)

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

        if not destino.exists() or destino.stat().st_size == 0:
            # Mesma regra do envio: etapa sem prova e' etapa que falhou. Um PNG
            # de zero byte viraria "documento vazio" no grupo.
            raise RuntimeError(f"o PNG nao foi gravado: {destino}")
        return str(destino)
