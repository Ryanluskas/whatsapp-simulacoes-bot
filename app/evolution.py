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
from .models import IncomingMessage, QuoteStatus, ResultadoEnvio
from .renderer import PngInvalido, PngRenderer, validar_png
from .whatsapp import CONNECTED, DISCONNECTED, STARTING, WhatsAppStatus

#: Pausa antes de cada envio, em milissegundos. Ver a nota sobre banimento.
DELAY_HUMANO_MS = 1200

#: A Evolution manda este codigo quando a instancia nao foi ativada no Manager.
#: Reenviar nao resolve -- so' um humano abrindo ``/manager`` resolve.
LICENCA_PENDENTE = "LICENSE_REQUIRED"

#: Respostas em que a CITACAO pode ser a culpada, e vale repetir sem ela.
#:
#: A Evolution transforma quase toda excecao interna em 400 (inclusive
#: "mensagem citada nao encontrada"), e algumas em 500. Nesses casos a
#: mensagem nao saiu, e mandar de novo sem ``quoted`` e' seguro. Ja' 401/403
#: (chave), 404 (instancia) e 429/502/503/504/timeout nao tem nada a ver com
#: a citacao -- repetir sem ela so' duplicaria a falha, ou pior, a mensagem.
_CITACAO_PODE_SER_A_CAUSA = {400, 422, 500}


class ErroDeEnvio(Exception):
    """Falha na entrega, ja' classificada em transitoria ou permanente."""

    def __init__(self, mensagem: str, *, transitorio: bool, corpo: str = "",
                 status_http: int = 0) -> None:
        super().__init__(mensagem)
        self.transitorio = transitorio
        self.corpo = corpo
        self.status_http = status_http


def _procurar_stanza(no, profundidade: int = 0) -> str | None:
    """O ``contextInfo.stanzaId`` em qualquer lugar da mensagem devolvida.

    A citacao fica dentro do tipo da mensagem (``extendedTextMessage``,
    ``imageMessage``...), e o tipo muda conforme o conteudo. Procurar pela
    chave evita manter uma lista de tipos que o WhatsApp amplia sem avisar.
    """
    if profundidade > 6:
        return None
    if isinstance(no, dict):
        contexto = no.get("contextInfo")
        if isinstance(contexto, dict) and contexto.get("stanzaId"):
            return str(contexto["stanzaId"])
        for valor in no.values():
            achado = _procurar_stanza(valor, profundidade + 1)
            if achado:
                return achado
    return None


def conferir_citacao(corpo: dict, quote_message_id: str) -> str:
    """Le' a resposta da API e diz o que aconteceu com a citacao.

    * ``ok``          -- a mensagem criada aponta para o id pedido.
    * ``not_applied`` -- a mensagem veio, mas sem citacao (a Evolution nao
      achou o original e mandou solta) ou citando OUTRA mensagem.
    * ``unverified``  -- a resposta nao traz a mensagem; nao da' para afirmar
      nem negar. Nao e' inventado como sucesso: fica registrado assim.
    """
    if not quote_message_id:
        return QuoteStatus.NONE
    mensagem = corpo.get("message") if isinstance(corpo, dict) else None
    if not isinstance(mensagem, dict) or not mensagem:
        return QuoteStatus.UNVERIFIED
    stanza = _procurar_stanza(mensagem)
    if stanza == quote_message_id:
        return QuoteStatus.OK
    return QuoteStatus.NOT_APPLIED


def _id_da_midia(corpo: dict) -> str:
    """Identificador publico do arquivo enviado, quando a API devolve.

    ``fileSha256`` e' o hash do conteudo -- serve de prova e nao e' segredo.
    ``mediaKey`` NUNCA: e' a chave que decifra a midia.
    """
    mensagem = corpo.get("message") if isinstance(corpo, dict) else None
    if not isinstance(mensagem, dict):
        return ""
    for tipo in ("imageMessage", "documentMessage"):
        midia = mensagem.get(tipo)
        if isinstance(midia, dict):
            valor = midia.get("fileSha256")
            if isinstance(valor, str):
                return valor[:88]
            if isinstance(valor, dict):  # Buffer serializado
                return str(valor.get("data", ""))[:88]
    return ""


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

        # Ver ``ja_enviado``.
        self._marcas_enviadas: list[str] = []
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
            raise ErroDeEnvio(motivo, transitorio=transitorio, corpo=corpo,
                              status_http=resposta.status_code)

        try:
            dados = resposta.json()
        except ValueError:
            dados = {}
        if not isinstance(dados, dict):
            dados = {}
        dados.setdefault("_http_status", resposta.status_code)
        return dados

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

    @staticmethod
    def _citacao(quote_message_id: str, chat_id: str, quote_text: str = "",
                 quote_participant: str = "") -> dict | None:
        """Monta ``quoted`` SO' com o que o chamador entregou.

        Esta camada nao descobre nada: nao procura a mensagem, nao lembra
        texto, nao olha conversa aberta. O id, o texto e o autor vem da
        solicitacao gravada no banco -- e por isso a citacao sobrevive a
        reinicio, o que a memoria em RAM que existia aqui nao garantia.

        ``participant`` e' o autor dentro do grupo. Sem ele o Baileys usa o
        proprio grupo como autor, e a citacao aparece atribuida a ninguem.
        """
        if not quote_message_id:
            return None
        chave: dict = {"id": quote_message_id, "fromMe": False}
        if chat_id:
            chave["remoteJid"] = chat_id
        if quote_participant and quote_participant != chat_id:
            chave["participant"] = quote_participant
        return {"key": chave, "message": {"conversation": quote_text or ""}}

    def _enviar(self, rota: str, payload: dict, *, via: str, tipo_midia: str,
                citacao: dict | None, alternativa: tuple[str, str],
                quote_message_id: str) -> ResultadoEnvio:
        """Um envio com a regra da citacao: FALHA DE QUOTE NAO E' FALHA DE RESPOSTA.

        1. tenta com ``quoted``;
        2. se a API recusar de um jeito em que a citacao pode ser a causa,
           manda de novo SEM ela, com a versao que leva o nome do consultor
           (``alternativa`` = campo e texto);
        3. confere na resposta se a citacao realmente pegou.

        Timeout, 429 e 5xx de indisponibilidade NAO disparam o passo 2: a
        mensagem pode ter saido, e a mesma falha se repetiria sem citacao.
        """
        campo, texto_alternativo = alternativa
        if citacao:
            payload = {**payload, "quoted": citacao}

        quote_status = QuoteStatus.NONE
        motivo_citacao = ""
        try:
            corpo = self._post(rota, payload)
            if citacao:
                quote_status = conferir_citacao(corpo, quote_message_id)
        except ErroDeEnvio as exc:
            if not (citacao and exc.status_http in _CITACAO_PODE_SER_A_CAUSA):
                return self._falha(exc, via=via, citacao=bool(citacao))
            motivo_citacao = str(exc)
            self._log("WARNING",
                      f"A Evolution recusou a citação de {quote_message_id} "
                      f"(HTTP {exc.status_http}). Enviando sem citação, com o "
                      "nome do consultor.")
            sem = {k: v for k, v in payload.items() if k != "quoted"}
            if texto_alternativo:
                sem[campo] = texto_alternativo
            try:
                corpo = self._post(rota, sem)
            except ErroDeEnvio as exc2:
                return self._falha(exc2, via=via, citacao=False)
            payload = sem
            quote_status = QuoteStatus.FALLBACK

        http_status = int(corpo.pop("_http_status", 0) or 0)
        enviado_id = str(((corpo.get("key") or {}).get("id") or ""))
        texto_enviado = payload.get(campo, "")
        evidencia = {"http_status": http_status,
                     "corpo": _resumo(corpo),
                     "quote_status": quote_status}
        if motivo_citacao:
            evidencia["motivo_citacao"] = motivo_citacao[:300]

        if not enviado_id:
            # 2xx sem id nao e' prova de entrega -- e tambem nao e' prova de
            # que NAO saiu. Nao inventar confirmacao, e nao reenviar sozinho:
            # quem chama registra como entrega sem evidencia.
            return ResultadoEnvio(
                ok=False, via=via, tipo_midia="nenhum",
                motivo=f"a Evolution respondeu {http_status} sem key.id",
                provider="evolution", quote_status=quote_status,
                http_status=http_status, sem_prova=True,
                evidencia={**evidencia, "transitorio": False, "sem_prova": True})

        self._lembrar_envio(texto_enviado)
        return ResultadoEnvio(
            ok=True, via=via, tipo_midia=tipo_midia,
            quoted_ok=quote_status in (QuoteStatus.OK, QuoteStatus.UNVERIFIED),
            provider="evolution", quote_status=quote_status,
            enviado_id=enviado_id, http_status=http_status,
            media_id=_id_da_midia(corpo) if tipo_midia == "imagem" else "",
            evidencia={**evidencia, "key_id": enviado_id})

    @staticmethod
    def _falha(exc: ErroDeEnvio, *, via: str, citacao: bool) -> ResultadoEnvio:
        return ResultadoEnvio(
            ok=False, via=via, tipo_midia="nenhum", motivo=str(exc),
            provider="evolution", transitorio=exc.transitorio,
            http_status=exc.status_http,
            quote_status=QuoteStatus.NONE if not citacao else "",
            evidencia={"transitorio": exc.transitorio, "corpo": exc.corpo[:400],
                       "http_status": exc.status_http})

    # -------------------------------------------------------------------- envio
    def send(self, chat_id: str, chat_name: str, text: str,
             quote_message_id: str = "", timeout: float = 90.0,
             texto_sem_citacao: str = "", quote_text: str = "",
             quote_participant: str = "") -> ResultadoEnvio:
        """Texto para ``chat_id``, citando ``quote_message_id`` quando houver.

        ``texto_sem_citacao``: a versao que se sustenta sozinha, com o nome
        do consultor. Sai quando nao ha' o que citar ou quando a citacao e'
        recusada. ``quote_text``/``quote_participant``: o texto e o autor da
        mensagem citada, vindos da solicitacao -- esta camada nao os procura.
        """
        if not chat_id:
            # Sem destino nao ha' "responder no grupo configurado": a
            # correlacao se perdeu antes daqui, e adivinhar o chat seria
            # exatamente o defeito que esta camada existe para encerrar.
            return ResultadoEnvio(ok=False, via="texto", provider="evolution",
                                  motivo="envio sem chat_id: a origem da solicitação se perdeu",
                                  evidencia={"transitorio": False})

        citacao = self._citacao(quote_message_id, chat_id, quote_text, quote_participant)
        if not citacao and texto_sem_citacao:
            text = texto_sem_citacao

        payload = {"number": chat_id, "text": text, "delay": DELAY_HUMANO_MS}
        return self._enviar(
            f"/message/sendText/{self.instance}", payload, via="texto",
            tipo_midia="nenhum", citacao=citacao,
            alternativa=("text", texto_sem_citacao),
            quote_message_id=quote_message_id)

    def send_image(self, chat_id: str, chat_name: str, image_path: str | Path,
                   caption: str = "", quote_message_id: str = "",
                   timeout: float = 120.0,
                   caption_sem_citacao: str = "", quote_text: str = "",
                   quote_participant: str = "") -> ResultadoEnvio:
        """Imagem com legenda -- inline, nunca documento."""
        if not chat_id:
            return ResultadoEnvio(ok=False, via="imagem", provider="evolution",
                                  motivo="envio sem chat_id: a origem da solicitação se perdeu",
                                  evidencia={"transitorio": False})

        caminho = Path(image_path)
        try:
            validar_png(caminho)
        except PngInvalido as exc:
            return ResultadoEnvio(ok=False, via="imagem", tipo_midia="nenhum",
                                  provider="evolution", motivo=str(exc),
                                  evidencia={"transitorio": False})

        citacao = self._citacao(quote_message_id, chat_id, quote_text, quote_participant)
        if not citacao and caption_sem_citacao:
            caption = caption_sem_citacao

        # base64 PURO. Com o prefixo ``data:image/png;base64,`` a Evolution
        # recusa -- e' o erro mais comum de quem integra.
        bruto = base64.b64encode(caminho.read_bytes()).decode("ascii")

        payload = {
            "number": chat_id,
            # Literal, e testado: "document" faria a imagem chegar como
            # arquivo, que e' exatamente o defeito que esta migracao encerra.
            "mediatype": "image",
            "mimetype": "image/png",
            "media": bruto,
            "fileName": caminho.name,
            "caption": caption,
            "delay": DELAY_HUMANO_MS,
        }
        return self._enviar(
            f"/message/sendMedia/{self.instance}", payload, via="imagem",
            tipo_midia="imagem", citacao=citacao,
            alternativa=("caption", caption_sem_citacao),
            quote_message_id=quote_message_id)

    # ------------------------------------------------------------------- imagem
    def render_png(self, html: str, path: str | Path, width: int = 900,
                   timeout: float = 60.0) -> str:
        return self._renderer.render_png(html, path, width=width, timeout=timeout)


def _resumo(corpo: dict) -> str:
    """Resposta da API resumida para o registro: ids e tipo, nunca a midia.

    O corpo de ``sendMedia`` pode trazer a imagem inteira em base64 e a
    ``mediaKey`` (que decifra o arquivo). Nenhum dos dois vai para o banco.
    """
    if not isinstance(corpo, dict):
        return ""
    chave = corpo.get("key") or {}
    resumo = {
        "key": {k: chave.get(k) for k in ("id", "remoteJid", "fromMe") if k in chave},
        "status": corpo.get("status"),
        "messageType": corpo.get("messageType"),
        "stanzaId": _procurar_stanza(corpo.get("message")),
    }
    return str({k: v for k, v in resumo.items() if v})[:400]
