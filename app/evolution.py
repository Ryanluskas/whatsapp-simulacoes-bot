"""Camada de WhatsApp por API (Evolution), no lugar da raspagem de tela.

O que muda em relacao a ``whatsapp.py``
---------------------------------------
Os tres defeitos conhecidos deixam de existir por construcao:

* **Nao citava.** Era uma cascata de cliques no menu de contexto, que
  dependia do HTML da Meta. Vira o campo ``quoted`` do JSON.
* **Mandava documento.** Era o ``input[type=file]`` errado, ou o item errado
  do menu do clipe. Vira ``mediatype: "image"``, explicito.
* **Falhava calado.** Era acao de interface sem verificacao. Vira resposta
  HTTP: um codigo de status e um ``key.id`` que prova a entrega.

Este arquivo e' **HTTP puro**. Sem Playwright, sem thread dona, sem
navegador. A unica excecao aparente e' ``render_png``, e ela nao e' excecao:
o trabalho e' delegado ao ``PngRenderer``, um servico separado. O
``ThreadActor`` continua existindo so' para o Santander.

Riscos que ficaram registrados aqui de proposito
------------------------------------------------
A Evolution usa Baileys, que implementa o protocolo do WhatsApp por
engenharia reversa. **Existe risco de banimento do numero.** Por isso o
``delay`` entre envios nao e' enfeite nem cortesia: e' o que mantem o
comportamento parecido com o de uma pessoa. Nao remova.
"""

from __future__ import annotations

import base64
import queue
import threading
from pathlib import Path

import httpx

from .clock import now_iso
from .models import IncomingMessage, ResultadoEnvio
from .renderer import PngRenderer
from .whatsapp import CONNECTED, DISCONNECTED, STARTING, WhatsAppStatus

#: Pausa antes de cada envio, em milissegundos. Ver a nota sobre banimento.
DELAY_HUMANO_MS = 1200

#: A Evolution manda este codigo quando a instancia nao foi ativada no Manager.
#: Reenviar nao resolve -- so' um humano abrindo ``/manager`` resolve.
LICENCA_PENDENTE = "LICENSE_REQUIRED"


class ErroDeEnvio(Exception):
    """Falha na entrega, ja' classificada em transitoria ou permanente."""

    def __init__(self, mensagem: str, *, transitorio: bool, corpo: str = "") -> None:
        super().__init__(mensagem)
        self.transitorio = transitorio
        self.corpo = corpo


def classificar_resposta(status_http: int, corpo: str) -> tuple[bool, str]:
    """Devolve ``(transitorio, motivo)`` para uma resposta que nao deu certo.

    Separar isto do envio nao e' preciosismo: e' o que permite testar a
    classificacao com uma tabela, sem servidor nenhum. Reenviar um erro
    permanente enche o grupo de repeticao inutil; nao reenviar um transitorio
    deixa o consultor sem resposta. Os dois erros ja' aconteceram.
    """
    trecho = (corpo or "")[:400]

    if status_http == 503 and LICENCA_PENDENTE in (corpo or ""):
        return False, ("a instancia da Evolution nao esta ativada -- abra "
                       "/manager e faca a ativacao da licenca")
    if status_http >= 500:
        return True, f"a Evolution respondeu {status_http}: {trecho}"
    if status_http == 429:
        # Excesso de requisicoes e' transitorio por definicao.
        return True, f"a Evolution pediu calma (429): {trecho}"
    if status_http >= 400:
        return False, f"a Evolution recusou ({status_http}): {trecho}"
    return True, f"resposta inesperada ({status_http}): {trecho}"


class EvolutionClient:
    """Implementa ``WhatsAppPort`` falando com a Evolution API."""

    #: Quantas mensagens nossas lembrar para o ``ja_enviado``. Ver o metodo.
    _MEMORIA_DE_ENVIOS = 400
    #: Quantos textos originais guardar para montar a citacao.
    _MEMORIA_DE_ORIGINAIS = 400
    _INTERVALO_DO_STATUS = 20.0

    def __init__(
        self,
        base_url: str,
        api_key: str,
        instance: str,
        group_jid: str,
        *,
        group_name: str = "",
        browser_path: str = "",
        on_log=None,
        on_status=None,
        renderer: PngRenderer | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self._api_key = api_key           # nunca vai para log nem para o painel
        self.instance = instance
        self.group_jid = group_jid
        self.group_name = group_name or group_jid

        self.inbox: "queue.Queue[IncomingMessage]" = queue.Queue()
        self._on_log = on_log
        self._on_status = on_status

        self._renderer = renderer or PngRenderer(browser_path=browser_path)
        self._client = client
        self._client_proprio = client is None

        self._status = WhatsAppStatus(chat_id=group_jid, chat_name=self.group_name)
        self._status_lock = threading.Lock()

        # Ver ``ja_enviado`` e ``lembrar_original``.
        self._marcas_enviadas: list[str] = []
        self._textos_originais: dict[str, str] = {}
        self._memoria_lock = threading.Lock()

        self._parar = threading.Event()
        self._vigia: threading.Thread | None = None

    # ------------------------------------------------------------------- infra
    def _log(self, level: str, message: str) -> None:
        if self._on_log:
            try:
                self._on_log(level, message)
            except Exception:
                pass

    @property
    def status(self) -> WhatsAppStatus:
        with self._status_lock:
            return self._status

    def _set_status(self, **changes) -> None:
        with self._status_lock:
            atual = self._status.as_dict()
            atual.pop("connected", None)
            atual.update(changes)
            anterior = self._status
            self._status = WhatsAppStatus(**atual)
            mudou = anterior.state != self._status.state
            agora = self._status
        if mudou and self._on_status:
            try:
                self._on_status(agora)
            except Exception:
                pass

    def _http(self) -> httpx.Client:
        if self._client is None:
            # Conectar rapido, mas dar tempo para o envio: um PNG em base64
            # sobe alguns megabytes, e cortar isso em 10 s reprovaria envio
            # que teria funcionado.
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(60.0, connect=10.0),
                headers={"apikey": self._api_key, "Content-Type": "application/json"},
            )
        return self._client

    def _post(self, rota: str, payload: dict) -> dict:
        """POST que ou devolve o corpo, ou levanta ``ErroDeEnvio`` classificado."""
        try:
            resposta = self._http().post(rota, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ErroDeEnvio(f"nao consegui falar com a Evolution: {exc}",
                              transitorio=True) from exc

        corpo = resposta.text or ""
        if resposta.status_code >= 400:
            transitorio, motivo = classificar_resposta(resposta.status_code, corpo)
            # O corpo inteiro no log: a Evolution costuma explicar bem o que
            # recusou, e essa explicacao e' a diferenca entre consertar em um
            # minuto ou passar a noite adivinhando payload.
            self._log("ERROR", f"Envio recusado pela Evolution: {motivo}")
            raise ErroDeEnvio(motivo, transitorio=transitorio, corpo=corpo)

        try:
            return resposta.json()
        except ValueError:
            return {}

    # ------------------------------------------------------------ ciclo de vida
    def start(self) -> None:
        self._parar.clear()
        self._renderer.start()
        self._set_status(state=STARTING, since=now_iso())
        if self._vigia is None or not self._vigia.is_alive():
            self._vigia = threading.Thread(
                target=self._vigia_loop, name="evolution-status", daemon=True)
            self._vigia.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._parar.set()
        if self._vigia is not None:
            self._vigia.join(timeout=timeout)
            self._vigia = None
        try:
            self._renderer.stop(timeout=timeout)
        except Exception:
            pass
        if self._client is not None and self._client_proprio:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._set_status(state=DISCONNECTED)

    def _vigia_loop(self) -> None:
        """Pergunta o estado da instancia de tempos em tempos.

        O webhook ``CONNECTION_UPDATE`` avisa quando a conexao cai, mas so' se
        a Evolution conseguir nos alcancar. Se ela mesma estiver fora do ar,
        nenhum webhook chega -- e o painel mostraria "conectado" para sempre.
        Esse foi exatamente o pior cenario da camada antiga: conectado na tela,
        mudo no grupo.
        """
        while not self._parar.wait(self._INTERVALO_DO_STATUS):
            try:
                self.atualizar_estado()
            except Exception as exc:
                self._set_status(state=DISCONNECTED, last_error=str(exc)[:200])

    def atualizar_estado(self) -> str:
        """Le ``/instance/connectionState`` e reflete no status."""
        try:
            resposta = self._http().get(f"/instance/connectionState/{self.instance}")
            dados = resposta.json() if resposta.status_code < 400 else {}
        except (httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
            self._set_status(state=DISCONNECTED,
                             last_error=f"Evolution inacessível: {exc}"[:200])
            return DISCONNECTED

        estado = ((dados.get("instance") or {}).get("state")
                  or dados.get("state") or "").lower()
        if estado == "open":
            self._set_status(state=CONNECTED, last_error="", last_poll=now_iso(),
                             chat_id=self.group_jid, chat_name=self.group_name)
        elif estado == "connecting":
            self._set_status(state=STARTING, last_poll=now_iso(), last_error="")
        else:
            self._set_status(state=DISCONNECTED, last_poll=now_iso(),
                             last_error="instância desconectada")
        return estado

    def request_reconnect(self) -> None:
        try:
            self._http().post(f"/instance/restart/{self.instance}")
            self._log("INFO", "Pedi reinício da instância da Evolution.")
        except Exception as exc:
            self._log("ERROR", f"Não consegui reiniciar a instância: {exc}")

    def qr_data_url(self, timeout: float = 20.0) -> str:
        """QR para vincular o numero, quando a instancia esta desconectada."""
        try:
            resposta = self._http().get(f"/instance/connect/{self.instance}")
            dados = resposta.json() if resposta.status_code < 400 else {}
        except Exception:
            return ""
        base64_qr = dados.get("base64") or (dados.get("qrcode") or {}).get("base64") or ""
        if base64_qr and not base64_qr.startswith("data:"):
            return f"data:image/png;base64,{base64_qr}"
        return base64_qr

    # ------------------------------------------------------------------ memoria
    def lembrar_original(self, message_id: str, texto: str) -> None:
        """Guarda o texto do pedido para poder cita-lo depois.

        A Evolution monta a citacao com ``key.id`` **e** o conteudo original.
        Como o ``manager`` so' repassa o id, o texto precisa vir daqui -- e
        quem o tem e' o webhook, no momento em que a mensagem chega.
        """
        if not message_id:
            return
        with self._memoria_lock:
            self._textos_originais[message_id] = texto or ""
            while len(self._textos_originais) > self._MEMORIA_DE_ORIGINAIS:
                self._textos_originais.pop(next(iter(self._textos_originais)))

    def _lembrar_envio(self, texto: str) -> None:
        with self._memoria_lock:
            self._marcas_enviadas.append(texto or "")
            del self._marcas_enviadas[:-self._MEMORIA_DE_ENVIOS]

    def ja_enviado(self, marca: str, timeout: float = 20.0) -> bool:
        """Ja' mandamos alguma mensagem com esta marca?

        Na camada antiga isto era uma busca no HTML da conversa. Aqui nao ha'
        HTML -- mas tambem nao ha' necessidade: **somos o unico remetente
        deste bot**, entao o que enviamos e' o que sabemos ter enviado. A
        memoria e' do processo: depois de reiniciar, ``ja_enviado`` volta a
        dizer ``False``, e o pior caso e' o consultor receber a resposta duas
        vezes. Ficar sem resposta seria pior, e e' o que a duvida evita.
        """
        if not marca:
            return False
        with self._memoria_lock:
            return any(marca in texto for texto in self._marcas_enviadas)

    def _citacao(self, quote_message_id: str) -> dict | None:
        if not quote_message_id:
            return None
        with self._memoria_lock:
            original = self._textos_originais.get(quote_message_id, "")
        return {"key": {"id": quote_message_id},
                "message": {"conversation": original}}

    # -------------------------------------------------------------------- envio
    def send(self, chat_id: str, chat_name: str, text: str,
             quote_message_id: str = "", timeout: float = 90.0,
             texto_sem_citacao: str = "") -> ResultadoEnvio:
        """Texto. O ``chat_id`` vindo do ``manager`` ja' e' o JID do grupo.

        ``texto_sem_citacao``: a versao que se sustenta sozinha, com o nome
        do consultor. Aqui a decisao e' mais simples que na camada do
        navegador -- sem id para citar, nao ha' citacao -- mas a assinatura
        e' a mesma nas duas, porque o ``manager`` nao pode saber qual esta'
        no ar.
        """
        citacao = self._citacao(quote_message_id)
        if not citacao and texto_sem_citacao:
            text = texto_sem_citacao

        payload = {
            "number": chat_id or self.group_jid,
            "text": text,
            "delay": DELAY_HUMANO_MS,
        }
        if citacao:
            payload["quoted"] = citacao

        try:
            corpo = self._post(f"/message/sendText/{self.instance}", payload)
        except ErroDeEnvio as exc:
            return ResultadoEnvio(
                ok=False, via="texto", motivo=str(exc),
                evidencia={"transitorio": exc.transitorio, "corpo": exc.corpo[:400]})

        enviado_id = ((corpo.get("key") or {}).get("id") or "")
        if not enviado_id:
            # 2xx sem id nao e' prova de entrega. Etapa sem prova falhou.
            return ResultadoEnvio(ok=False, via="texto",
                                  motivo="a Evolution respondeu sem key.id",
                                  evidencia={"transitorio": True,
                                             "corpo": str(corpo)[:400]})

        self._lembrar_envio(text)
        return ResultadoEnvio(ok=True, via="texto", tipo_midia="nenhum",
                              quoted_ok=bool(citacao),
                              evidencia={"key_id": enviado_id})

    def send_image(self, chat_id: str, chat_name: str, image_path: str | Path,
                   caption: str = "", quote_message_id: str = "",
                   timeout: float = 120.0,
                   caption_sem_citacao: str = "") -> ResultadoEnvio:
        """Imagem com legenda -- inline, nunca documento."""
        citacao = self._citacao(quote_message_id)
        if not citacao and caption_sem_citacao:
            caption = caption_sem_citacao

        caminho = Path(image_path)
        if not caminho.exists() or caminho.stat().st_size == 0:
            return ResultadoEnvio(ok=False, via="imagem", tipo_midia="nenhum",
                                  motivo=f"o PNG nao existe ou esta vazio: {caminho}")

        # base64 PURO. Com o prefixo ``data:image/png;base64,`` a Evolution
        # recusa -- e' o erro mais comum de quem integra.
        bruto = base64.b64encode(caminho.read_bytes()).decode("ascii")

        payload = {
            "number": chat_id or self.group_jid,
            # Literal, e testado: "document" faria a imagem chegar como
            # arquivo, que e' exatamente o defeito que esta migracao encerra.
            "mediatype": "image",
            "mimetype": "image/png",
            "media": bruto,
            "fileName": caminho.name,
            "caption": caption,
            "delay": DELAY_HUMANO_MS,
        }
        if citacao:
            payload["quoted"] = citacao

        try:
            corpo = self._post(f"/message/sendMedia/{self.instance}", payload)
        except ErroDeEnvio as exc:
            return ResultadoEnvio(
                ok=False, via="imagem", tipo_midia="nenhum", motivo=str(exc),
                evidencia={"transitorio": exc.transitorio, "corpo": exc.corpo[:400]})

        enviado_id = ((corpo.get("key") or {}).get("id") or "")
        if not enviado_id:
            return ResultadoEnvio(ok=False, via="imagem", tipo_midia="nenhum",
                                  motivo="a Evolution respondeu sem key.id",
                                  evidencia={"transitorio": True,
                                             "corpo": str(corpo)[:400]})

        self._lembrar_envio(caption)
        return ResultadoEnvio(ok=True, via="imagem", tipo_midia="imagem",
                              quoted_ok=bool(citacao),
                              evidencia={"key_id": enviado_id})

    # ------------------------------------------------------------------- imagem
    def render_png(self, html: str, path: str | Path, width: int = 900,
                   timeout: float = 60.0) -> str:
        return self._renderer.render_png(html, path, width=width, timeout=timeout)
