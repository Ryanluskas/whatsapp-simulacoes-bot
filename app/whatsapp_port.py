"""O contrato entre o sistema e o WhatsApp -- seja ele qual for.

Por que isto existe
-------------------
Ate' aqui o ``manager`` falava direto com ``whatsapp.py``, que dirige o
WhatsApp Web no navegador e le' o HTML da tela. Cada mudanca que a Meta faz
no HTML quebra alguma coisa nossa, e os tres defeitos conhecidos -- nao
citar a mensagem, mandar a imagem como documento, falhar sem avisar --
nascem todos dali: sao acoes de interface executadas no escuro.

A Evolution API fala o protocolo do WhatsApp direto. Citar vira um campo
``quoted`` no JSON; mandar imagem vira ``mediatype: "image"``; e a resposta
HTTP diz se entregou. Os tres defeitos deixam de ser possiveis por
construcao, em vez de serem consertados por tentativa.

Trocar de camada de uma vez seria imprudente: a camada nova ainda nao provou
que entrega no grupo real. Entao as duas coexistem, escolhidas por
``WHATSAPP_MODE=dom|evolution``, e **o resto do sistema nao sabe qual esta'
em uso**. Fila, banco, painel, parser e o simulador do Santander continuam
identicos.

O que este arquivo garante
--------------------------
Que as duas implementacoes tenham a MESMA superficie. Sem isto, trocar o
modo quebraria o ``manager`` em producao, num ponto que nenhum teste cobre.
``tests/test_evolution.py`` compara as assinaturas das duas contra este
protocolo -- e' o teste que torna a troca segura.

Sobre ``render_png``
--------------------
Ele esta' aqui por um motivo desconfortavel: hoje o PNG do resultado e'
gerado DENTRO do navegador que o WhatsApp Web ja' tem aberto. No modo
``evolution`` nao ha' navegador nenhum, entao a implementacao nova compoe um
renderizador proprio (``app/renderer.py``). A camada Evolution em si
continua sendo HTTP puro -- ela delega, nao dirige navegador.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import IncomingMessage, ResultadoEnvio

# Reexportados de proposito: quem implementa esta porta precisa dos dois tipos,
# e importa-los daqui deixa o contrato inteiro num lugar so'.
__all__ = ["WhatsAppPort", "IncomingMessage", "ResultadoEnvio",
           "METODOS_DO_CONTRATO", "MODO_DOM", "MODO_EVOLUTION", "MODOS"]


@runtime_checkable
class WhatsAppPort(Protocol):
    """O que o ``manager`` pode pedir a uma camada de WhatsApp.

    ``inbox`` e' uma ``queue.Queue[IncomingMessage]``: as duas implementacoes
    entregam mensagem pelo mesmo caminho, mesmo tendo origens diferentes (uma
    varre a tela, a outra recebe webhook). Foi o que permitiu nao tocar no
    laco de leitura do ``manager``.
    """

    # -------------------------------------------------------------- ciclo de vida
    def start(self) -> None: ...

    def stop(self, timeout: float = 10.0) -> None: ...

    # ------------------------------------------------------------------- estado
    @property
    def status(self): ...

    def request_reconnect(self) -> None: ...

    def qr_data_url(self, timeout: float = 20.0) -> str: ...

    # ------------------------------------------------------------------- envio
    def send(self, chat_id: str, chat_name: str, text: str,
             quote_message_id: str = "", timeout: float = 90.0) -> bool: ...

    def send_image(self, chat_id: str, chat_name: str, image_path: str | Path,
                   caption: str = "", quote_message_id: str = "",
                   timeout: float = 120.0) -> bool: ...

    def ja_enviado(self, marca: str, timeout: float = 20.0) -> bool: ...

    # ------------------------------------------------------------------ imagem
    def render_png(self, html: str, path: str | Path, width: int = 900,
                   timeout: float = 60.0) -> str: ...


#: Os metodos que o contrato exige. O teste de contrato compara as assinaturas
#: das duas implementacoes contra esta lista -- e' o que impede uma delas de
#: ganhar um parametro que a outra nao tem e so' quebrar em producao.
METODOS_DO_CONTRATO = (
    "start", "stop", "request_reconnect", "qr_data_url",
    "send", "send_image", "ja_enviado", "render_png",
)

MODO_DOM = "dom"
MODO_EVOLUTION = "evolution"
MODOS = (MODO_DOM, MODO_EVOLUTION)
