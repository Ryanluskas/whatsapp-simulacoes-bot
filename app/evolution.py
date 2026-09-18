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
import re
import queue
import threading
from pathlib import Path
from typing import NamedTuple
from enum import Enum

import httpx

from .clock import now_iso
from .models import Desfecho, IncomingMessage, QuoteStatus, ResultadoEnvio
from .renderer import PngInvalido, PngRenderer, validar_png
from .whatsapp import CONNECTED, DISCONNECTED, STARTING, WhatsAppStatus

#: Pausa antes de cada envio, em milissegundos. Ver a nota sobre banimento.
DELAY_HUMANO_MS = 1200

#: A Evolution manda este codigo quando a instancia nao foi ativada no Manager.
#: Reenviar nao resolve -- so' um humano abrindo ``/manager`` resolve.
LICENCA_PENDENTE = "LICENSE_REQUIRED"

# --------------------------------------------------------------- classificacao
#
# A regra que manda aqui: **so' se manda de novo quando ha' prova de que a
# primeira NAO saiu.** Mandar duas vezes o resultado de um cliente no grupo e'
# pior que uma entrega que precisa ser conferida por uma pessoa.
#
# Por que o corpo importa num 400: a Evolution transforma em 400 tanto a
# validacao do payload (antes de enviar) quanto excecoes DENTRO do envio --
# inclusive as que acontecem DEPOIS de a mensagem sair (gravar no banco dela,
# disparar webhook). Um 400 sozinho, sem explicacao, nao prova nada.
#
# As marcas abaixo sao heuristicas sobre o TEXTO que a Evolution devolve. Elas
# so' servem para abrir caminho a um reenvio; na ausencia delas, o desfecho
# e' o que nao reenvia (INCERTA).

#: O corpo fala da citacao: recusa por causa do ``quoted``. "quote" sozinho
#: ficou de fora (casava com qualquer texto e abria caminho para um reenvio),
#: e "quoted" so' vale como PALAVRA -- "unquoted" nao e' a citacao.
_MARCAS_DE_CITACAO = ("stanzaid", "contextinfo")
_CITACAO_COMO_PALAVRA = re.compile(r"(?<![a-z])quoted(?:message)?(?![a-z])")

#: O corpo sugere falha DEPOIS de processar (a mensagem pode ter saido).
#: Inclui as integracoes que a Evolution dispara depois de enviar: guardar a
#: midia no S3/MinIO, avisar Chatwoot, RabbitMQ, SQS, websocket.
_MARCAS_POS_ENVIO = ("prisma", "database", "unique constraint", "chatwoot",
                     "timed out", "timeout", "etimedout", "econnreset",
                     "socket hang up", "connection closed",
                     "minio", "bucket", "accesskeyid", "s3client", "putobject",
                     "rabbitmq", "amqp", "sqs", "websocket")

#: O corpo sugere validacao ANTES de enviar (nada saiu).
_MARCAS_DE_VALIDACAO = ('"exists":false', "requires property", "is not of a type",
                        "is not one of enum", "does not match pattern",
                        "additionalproperty", "is not allowed", "must be",
                        "is required", "invalid", "inválid", "malformed", "malformad")

#: Nada foi processado; a MESMA requisicao pode ser repetida depois.
_HTTP_TRANSITORIO = {408, 429, 503}
#: Nada foi processado; repetir nao resolve.
_HTTP_PERMANENTE = {401, 403, 404}


class Categoria:
    """O QUE a resposta disse, separado do que o envio faz com isso (``Desfecho``).

    Duas categorias levam ao mesmo desfecho INCERTA de proposito:
    ``POST_SEND_ERROR`` (o corpo mostra erro depois de enviar) e ``UNCERTAIN``
    (nao ha' como saber). Separa-las e' o que deixa o log e os testes dizerem
    POR QUE a mensagem pode ter saido.
    """

    QUOTE_REJECTED = "QUOTE_REJECTED"            # 400/422 apontando o quoted: nada saiu
    VALIDATION_REJECTED = "VALIDATION_REJECTED"  # 400/422 de validacao: nada saiu
    POST_SEND_ERROR = "POST_SEND_ERROR"          # erro depois de enviar: pode ter saido
    TRANSIENT = "TRANSIENT"                      # nada processado; repetir depois
    PERMANENT = "PERMANENT"                      # nada processado; repetir nao resolve
    UNCERTAIN = "UNCERTAIN"                      # sem como saber: pode ter saido

    #: Desfecho de cada categoria -- a unica traducao, num lugar so'.
    DESFECHO = {
        QUOTE_REJECTED: Desfecho.CITACAO_RECUSADA,
        VALIDATION_REJECTED: Desfecho.RECUSADA,
        POST_SEND_ERROR: Desfecho.INCERTA,
        TRANSIENT: Desfecho.TRANSITORIA,
        PERMANENT: Desfecho.PERMANENTE,
        UNCERTAIN: Desfecho.INCERTA,
    }


class Classificacao(NamedTuple):
    desfecho: str
    motivo: str
    categoria: str = Categoria.UNCERTAIN

    @property
    def transitorio(self) -> bool:
        return self.desfecho == Desfecho.TRANSITORIA


def classificar_resposta(status_http: int, corpo: str,
                         com_citacao: bool = False) -> Classificacao:
    """Classifica uma resposta HTTP >= 400 da Evolution.

    ==========================  ===================  ==========================
    resposta                    desfecho             o que o chamador faz
    ==========================  ===================  ==========================
    400/422 apontando o quoted  citacao_recusada     repete SEM citacao
    400/422 de validacao        recusada             nao repete (imagem -> texto)
    400/422 sem explicacao      incerta              nao repete, nao cai p/ texto
    401/403/404, licenca        permanente           nao repete
    408/429/503                 transitoria          repete igual, depois
    500, 502, 504, outros 5xx   incerta              nao repete, nao cai p/ texto
    ==========================  ===================  ==========================

    Separar isto do envio e' o que permite testar a politica com uma tabela,
    sem servidor nenhum.
    """
    texto = corpo or ""
    trecho = texto[:400]
    baixo = texto.lower()
    compacto = "".join(baixo.split())

    if status_http == 503 and LICENCA_PENDENTE in texto:
        return Classificacao(Desfecho.PERMANENTE,
                             "a instancia da Evolution nao esta ativada -- abra "
                             "/manager e faca a ativacao da licenca",
            Categoria.PERMANENT)
    if status_http in _HTTP_PERMANENTE:
        return Classificacao(Desfecho.PERMANENTE,
                             f"a Evolution recusou ({status_http}): {trecho}",
            Categoria.PERMANENT)
    if status_http in _HTTP_TRANSITORIO:
        return Classificacao(Desfecho.TRANSITORIA,
                             f"a Evolution nao processou agora ({status_http}): {trecho}",
            Categoria.TRANSIENT)
    if status_http >= 500:
        # 500 NAO prova que nada saiu: pode ser erro depois do envio. Mandar
        # de novo (com ou sem citacao) pode duplicar a resposta.
        return Classificacao(Desfecho.INCERTA,
                             f"a Evolution respondeu {status_http}; a mensagem pode ter "
                             f"saido: {trecho}",
            Categoria.UNCERTAIN)
    if status_http in (400, 422):
        if any(marca in baixo for marca in _MARCAS_POS_ENVIO):
            return Classificacao(Desfecho.INCERTA,
                                 f"a Evolution respondeu {status_http} com sinal de erro "
                                 f"depois do envio: {trecho}",
            Categoria.POST_SEND_ERROR)
        if com_citacao and (_CITACAO_COMO_PALAVRA.search(baixo)
                            or any(marca in compacto for marca in _MARCAS_DE_CITACAO)):
            return Classificacao(Desfecho.CITACAO_RECUSADA,
                                 f"a Evolution recusou a citacao ({status_http}): {trecho}",
            Categoria.QUOTE_REJECTED)
        if any(marca.replace(" ", "") in compacto for marca in _MARCAS_DE_VALIDACAO):
            return Classificacao(Desfecho.RECUSADA,
                                 f"a Evolution recusou o envio ({status_http}): {trecho}",
            Categoria.VALIDATION_REJECTED)
        return Classificacao(Desfecho.INCERTA,
                             f"a Evolution respondeu {status_http} sem dizer o que "
                             f"recusou; a mensagem pode ter saido: {trecho}",
            Categoria.UNCERTAIN)
    if status_http >= 400:
        # 405, 409, 413 (payload grande demais para o proxy)...: rejeitado
        # antes de chegar ao envio.
        return Classificacao(Desfecho.RECUSADA,
                             f"a Evolution recusou ({status_http}): {trecho}",
            Categoria.VALIDATION_REJECTED)
    return Classificacao(Desfecho.INCERTA, f"resposta inesperada ({status_http}): {trecho}",
            Categoria.UNCERTAIN)


def classificar_falha_de_transporte(exc: Exception) -> Classificacao:
    """A requisicao nem teve resposta. Saiu ou nao saiu?

    * nao conectou (conexao recusada, timeout de conexao, pool cheio) ou nao
      terminou de subir o corpo: a Evolution nao tem a requisicao inteira, nada
      foi processado -> TRANSITORIA;
    * a requisicao subiu e a resposta nao veio (timeout de leitura, conexao
      derrubada depois, protocolo quebrado): a Evolution pode ter enviado ->
      INCERTA.
    """
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                        httpx.WriteError, httpx.WriteTimeout)):
        return Classificacao(Desfecho.TRANSITORIA,
                             f"a requisicao nao chegou a Evolution: {exc!r}"[:300],
            Categoria.TRANSIENT)
    return Classificacao(Desfecho.INCERTA,
                         f"a requisicao foi enviada e a resposta nao veio "
                         f"({exc.__class__.__name__}); a mensagem pode ter saido"[:300],
            Categoria.UNCERTAIN)


class CategoriaAck(str, Enum):
    CONFIRMACAO_ENTREGA = "confirmacao_entrega"
    NAO_CONFIRMADOR = "nao_confirmador"
    ERRO = "erro"


def classificar_ack_evolution(status: str) -> tuple[CategoriaAck, int]:
    """Classifica o status de entrega do webhook e devolve sua 'ordem' (peso).

    Um peso maior jamais é sobrescrito por um menor (ex: READ não volta pra PENDING).
    ERROR não derruba o que já foi confirmado.

    Pesos (baseados no Baileys):
    - 0: PENDING / 0 / Vazio
    - 0: SERVER_ACK / 1 (aceito pela Evolution, ainda sem prova de entrega)
    - 2: DELIVERY_ACK / 2 (Chegou no destinatário)
    - 3: READ / 3 (Lido)
    - 4: PLAYED / 4 (Áudio tocado)
    - -1: ERROR / 5 (Erro)
    """
    s = str(status).strip().upper()
    if s in ("1", "SERVER_ACK"):
        return CategoriaAck.NAO_CONFIRMADOR, 0
    if s in ("2", "DELIVERY_ACK"):
        return CategoriaAck.CONFIRMACAO_ENTREGA, 2
    if s in ("3", "READ"):
        return CategoriaAck.CONFIRMACAO_ENTREGA, 3
    if s in ("4", "PLAYED"):
        return CategoriaAck.CONFIRMACAO_ENTREGA, 4
    if s in ("5", "ERROR"):
        return CategoriaAck.ERRO, -1
    return CategoriaAck.NAO_CONFIRMADOR, 0


class ErroDeEnvio(Exception):
    """Falha na entrega, com o desfecho ja' classificado."""

    def __init__(self, mensagem: str, *, desfecho: str, corpo: str = "",
                 status_http: int = 0) -> None:
        super().__init__(mensagem)
        self.desfecho = desfecho
        self.corpo = corpo
        self.status_http = status_http

    @property
    def transitorio(self) -> bool:
        return self.desfecho == Desfecho.TRANSITORIA


def _stanzas(no, profundidade: int = 0, achados: list | None = None) -> list[tuple[int, str]]:
    """Todos os ``contextInfo.stanzaId`` da resposta, com a profundidade.

    A citacao fica dentro do tipo da mensagem (``extendedTextMessage``,
    ``imageMessage``...) e versoes da Evolution devolvem a mensagem em niveis
    diferentes. Procurar pela chave, em qualquer nivel, evita presumir a
    estrutura. O mais raso e' o da mensagem enviada; os mais fundos sao de
    mensagens que ela cita.
    """
    achados = [] if achados is None else achados
    if profundidade > 8:
        return achados
    if isinstance(no, dict):
        contexto = no.get("contextInfo")
        if isinstance(contexto, dict) and contexto.get("stanzaId"):
            achados.append((profundidade, str(contexto["stanzaId"])))
        for valor in no.values():
            _stanzas(valor, profundidade + 1, achados)
    elif isinstance(no, list):
        for valor in no:
            _stanzas(valor, profundidade + 1, achados)
    return achados


def _procurar_stanza(no) -> str | None:
    achados = _stanzas(no)
    return min(achados)[1] if achados else None


def extrair_key_id(corpo) -> str:
    """O ``key.id`` da mensagem criada -- na raiz ou dentro de ``data``.

    Nada alem disso: se o id nao estiver onde a Evolution o poe, nao ha' prova
    de entrega, e o desfecho fica incerto.
    """
    if not isinstance(corpo, dict):
        return ""
    for base in (corpo, corpo.get("data")):
        if isinstance(base, dict) and isinstance(base.get("key"), dict):
            valor = base["key"].get("id")
            if isinstance(valor, str) and valor.strip():
                return valor.strip()
    return ""


def conferir_citacao(corpo: dict, quote_message_id: str) -> str:
    """Le' a resposta REAL da API e diz o que aconteceu com a citacao.

    * ``ok``          -- o ``stanzaId`` mais raso e' o id pedido;
    * ``not_applied`` -- ha' ``stanzaId``, e ele aponta OUTRA mensagem;
    * ``unverified``  -- nenhum ``stanzaId`` na resposta. Nao da' para afirmar
      nem negar; nunca e' tratado como ``ok``.
    """
    if not quote_message_id:
        return QuoteStatus.NONE
    stanza = _procurar_stanza(corpo)
    if stanza is None:
        return QuoteStatus.UNVERIFIED
    return QuoteStatus.OK if stanza == quote_message_id else QuoteStatus.NOT_APPLIED


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


class EvolutionClient:
    """Implementa ``WhatsAppPort`` falando com a Evolution API."""

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

        # Ver ``diagnostico``. Nada aqui e' segredo: so' booleanos e estados.
        self._diagnostico: dict = {
            "evolution_url_configured": bool(self.base_url),
            "evolution_instance": instance,
            "group_configured": bool(group_jid),
            "evolution_api_reachable": None,
            "api_key_valid": None,
            "instance_found": None,
            "evolution_state": "unknown",
            "webhook_configured": None,
            "webhook_points_to_bot": None,
            "checked_at": "",
        }

        # Ver ``ja_enviado``.

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
        """POST que ou devolve o corpo com ``key``, ou levanta ``ErroDeEnvio`` classificado."""
        try:
            resposta = self._http().post(rota, json=payload)
        except httpx.HTTPError as exc:
            # Qualquer erro do httpx, nao so' transporte: um ``DecodingError``
            # depois de um 2xx escapava como excecao generica e virava
            # "transitorio" no manager -- a imagem caia para texto e saiam duas.
            falha = classificar_falha_de_transporte(exc)
            self._log("ERROR", f"Envio para a Evolution sem resposta: {falha.motivo}")
            raise ErroDeEnvio(falha.motivo, desfecho=falha.desfecho) from exc

        corpo = resposta.text or ""
        if resposta.status_code >= 400:
            falha = classificar_resposta(resposta.status_code, corpo,
                                         com_citacao="quoted" in payload)
            # O corpo inteiro no log: a Evolution costuma explicar bem o que
            # recusou, e essa explicacao e' a diferenca entre consertar em um
            # minuto ou passar a noite adivinhando payload.
            self._log("ERROR", f"Envio recusado pela Evolution [{falha.desfecho}]: "
                               f"{falha.motivo}")
            raise ErroDeEnvio(falha.motivo, desfecho=falha.desfecho, corpo=corpo,
                              status_http=resposta.status_code)

        try:
            dados = resposta.json()
        except ValueError:
            dados = None
        if not isinstance(dados, dict):
            # 2xx com corpo ilegivel (proxy devolvendo HTML, resposta cortada):
            # a Evolution pode ter enviado. Incerto, nao "falhou".
            raise ErroDeEnvio(
                f"a Evolution respondeu {resposta.status_code} com um corpo ilegível; "
                "a mensagem pode ter saído",
                desfecho=Desfecho.INCERTA, corpo=corpo[:400],
                status_http=resposta.status_code)
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
        """Pergunta o estado da instancia de tempos em tempos -- a primeira ja' no boot.

        O webhook ``CONNECTION_UPDATE`` avisa quando a conexao cai, mas so' se
        a Evolution conseguir nos alcancar. Se ela mesma estiver fora do ar,
        nenhum webhook chega -- e o painel mostraria "conectado" para sempre.
        Esse foi exatamente o pior cenario da camada antiga: conectado na tela,
        mudo no grupo.
        """
        ciclo = 0
        while True:
            try:
                self.atualizar_estado()
                if ciclo % self._CICLOS_ENTRE_CONFERENCIAS_DO_WEBHOOK == 0:
                    self.conferir_webhook()
            except Exception as exc:
                self._set_status(state=DISCONNECTED, last_error=str(exc)[:200])
            ciclo += 1
            if self._parar.wait(self._INTERVALO_DO_STATUS):
                return

    #: O webhook muda pouco; conferir a cada ~5 min basta.
    _CICLOS_ENTRE_CONFERENCIAS_DO_WEBHOOK = 15

    def diagnostico(self) -> dict:
        """Por que o bot esta' ligado e nao responde? Sem chave, sem token.

        ``None`` = ainda nao deu para saber (nenhuma conferencia feita, ou a
        Evolution nem respondeu).
        """
        with self._status_lock:
            return dict(self._diagnostico)

    def _anotar_diagnostico(self, **campos) -> None:
        with self._status_lock:
            self._diagnostico.update(campos, checked_at=now_iso())

    def atualizar_estado(self) -> str:
        """Le ``/instance/connectionState`` e reflete no status e no diagnostico."""
        try:
            resposta = self._http().get(f"/instance/connectionState/{self.instance}")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self._anotar_diagnostico(evolution_api_reachable=False, api_key_valid=None,
                                     instance_found=None, evolution_state="unreachable")
            self._set_status(state=DISCONNECTED,
                             last_error=f"Evolution inacessível: {exc}"[:200])
            return DISCONNECTED

        codigo = resposta.status_code
        if codigo in (401, 403):
            self._anotar_diagnostico(evolution_api_reachable=True, api_key_valid=False,
                                     instance_found=None, evolution_state="unauthorized")
            self._set_status(state=DISCONNECTED, last_poll=now_iso(),
                             last_error=f"a Evolution recusou a chave ({codigo})")
            return DISCONNECTED
        if codigo == 404:
            self._anotar_diagnostico(evolution_api_reachable=True, api_key_valid=True,
                                     instance_found=False, evolution_state="not_found")
            self._set_status(state=DISCONNECTED, last_poll=now_iso(),
                             last_error=f"a instância '{self.instance}' não existe na Evolution")
            return DISCONNECTED
        if codigo >= 400:
            licenca = LICENCA_PENDENTE in (resposta.text or "")
            self._anotar_diagnostico(evolution_api_reachable=True, api_key_valid=None,
                                     instance_found=None,
                                     evolution_state="license_required" if licenca
                                     else f"http_{codigo}")
            self._set_status(state=DISCONNECTED, last_poll=now_iso(),
                             last_error=("licença da Evolution não ativada (/manager)" if licenca
                                         else f"a Evolution respondeu {codigo} ao estado"))
            return DISCONNECTED
        try:
            dados = resposta.json()
        except ValueError:
            dados = {}
        dados = dados if isinstance(dados, dict) else {}

        estado = str((dados.get("instance") or {}).get("state")
                     or dados.get("state") or "").lower()
        self._anotar_diagnostico(evolution_api_reachable=True, api_key_valid=True,
                                 instance_found=True, evolution_state=estado or "unknown")
        if estado == "open":
            self._set_status(state=CONNECTED, last_error="", last_poll=now_iso(),
                             chat_id=self.group_jid, chat_name=self.group_name)
        elif estado == "connecting":
            self._set_status(state=STARTING, last_poll=now_iso(), last_error="")
        else:
            self._set_status(state=DISCONNECTED, last_poll=now_iso(),
                             last_error="instância desconectada")
        return estado

    def injetar_estado_conexao(self, estado: str) -> None:
        """Chamado pelo webhook para refletir quedas e retornos imediatamente."""
        estado = (estado or "").strip().lower()
        self._anotar_diagnostico(evolution_state=estado)
        if estado == "open":
            self._set_status(state=CONNECTED, last_error="", chat_id=self.group_jid, chat_name=self.group_name)
        elif estado == "connecting":
            self._set_status(state=STARTING, last_error="")
        else:
            self._set_status(state=DISCONNECTED, last_error="instância desconectada (via webhook)")

    def conferir_webhook(self) -> bool | None:
        """A instancia tem webhook ligado apontando para ``/webhook/whatsapp``?

        Le' ``/webhook/find/<instancia>``. A resposta varia entre versoes; so'
        se afirma ``True``/``False`` quando os campos ``enabled`` e ``url``
        estao la'. Fora disso fica ``None`` ("nao deu para conferir"). A URL e
        os cabecalhos (que levam o token) nunca vao para o diagnostico.
        """
        configurado: bool | None = None
        aponta: bool | None = None
        try:
            resposta = self._http().get(f"/webhook/find/{self.instance}")
        except (httpx.TimeoutException, httpx.TransportError):
            resposta = None
        if resposta is not None and resposta.status_code < 400:
            bruto = (resposta.text or "").strip()
            if bruto in ("", "null", "{}"):
                configurado = False      # a instancia respondeu: nao ha' webhook
            else:
                try:
                    dados = resposta.json()
                except ValueError:
                    dados = None
                if isinstance(dados, dict) and isinstance(dados.get("webhook"), dict):
                    dados = dados["webhook"]
                if isinstance(dados, dict) and "url" in dados:
                    url = str(dados.get("url") or "")
                    configurado = bool(dados.get("enabled")) and bool(url)
                    aponta = ("/webhook/whatsapp" in url) if url else False
        self._anotar_diagnostico(webhook_configured=configurado,
                                 webhook_points_to_bot=aponta)
        return configurado

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
        pass  # Removido cache em RAM conforme revisão

    def ja_enviado(self, marca: str, timeout: float = 20.0) -> bool:
        """A Evolution não consulta tela. A fonte de verdade é o banco."""
        return False

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
        2. SO' se a Evolution RECUSOU a citacao (``citacao_recusada``: 400/422
           apontando o quoted -- prova de que nada saiu), manda de novo SEM
           ela, com a versao que leva o nome do consultor;
        3. confere na resposta se a citacao realmente pegou.

        500, timeout, conexao caida e 2xx sem id NAO disparam o passo 2: a
        primeira mensagem pode ter saido, e a segunda duplicaria a resposta.
        """
        campo, texto_alternativo = alternativa
        citado = bool(citacao)
        if citacao:
            payload = {**payload, "quoted": citacao}

        quote_status = QuoteStatus.NONE
        quote_error = ""
        try:
            corpo = self._post(rota, payload)
        except ErroDeEnvio as exc:
            if not (citado and exc.desfecho == Desfecho.CITACAO_RECUSADA):
                return self._falha(exc, via=via, citado=citado,
                                   quote_message_id=quote_message_id)
            quote_error = str(exc)[:300]
            self._log("WARNING",
                      f"A Evolution RECUSOU a citação de {quote_message_id} "
                      f"(HTTP {exc.status_http}); nada foi enviado. Enviando sem "
                      "citação, com o nome do consultor.")
            sem = {k: v for k, v in payload.items() if k != "quoted"}
            if texto_alternativo:
                sem[campo] = texto_alternativo
            try:
                corpo = self._post(rota, sem)
            except ErroDeEnvio as exc2:
                return self._falha(exc2, via=via, citado=False, quote_message_id="",
                                   quote_status=QuoteStatus.FALLBACK,
                                   quote_error=quote_error)
            payload = sem
            citado = False
            quote_status = QuoteStatus.FALLBACK

        # A partir daqui a Evolution JA' ACEITOU o POST. Qualquer excecao ao ler
        # a resposta (citacao, evidencia, memoria) nao pode virar "transitorio"
        # no manager -- isso reenviaria uma mensagem que saiu.
        try:
            return self._resultado_aceito(
                corpo, payload, campo=campo, via=via, tipo_midia=tipo_midia,
                citado=citado, quote_message_id=quote_message_id,
                quote_status=quote_status, quote_error=quote_error)
        except Exception as exc:  # noqa: BLE001 - qualquer falha aqui e' pos-envio
            return self._aceito_sem_conferir(
                corpo, exc, via=via, tipo_midia=tipo_midia, citado=citado,
                quote_message_id=quote_message_id, quote_status=quote_status,
                quote_error=quote_error)

    def _aceito_sem_conferir(self, corpo, exc: Exception, *, via: str, tipo_midia: str,
                             citado: bool, quote_message_id: str, quote_status: str,
                             quote_error: str) -> ResultadoEnvio:
        """O POST foi aceito e a leitura da resposta quebrou.

        Com ``key.id`` legivel a mensagem saiu (entregue, citacao sem prova);
        sem ele, incerta. Nunca "transitorio".
        """
        try:
            enviado_id = extrair_key_id(corpo) if isinstance(corpo, dict) else ""
        except Exception:  # noqa: BLE001
            enviado_id = ""
        motivo = (f"a Evolution aceitou o envio, mas a resposta nao pode ser lida "
                  f"({exc.__class__.__name__})")
        self._log("WARNING", motivo + ("; a mensagem saiu" if enviado_id
                                       else "; a mensagem pode ter saído"))
        situacao = QuoteStatus.UNVERIFIED if citado else quote_status
        comum = dict(via=via, provider="evolution", quote_status=situacao,
                     quote_error=quote_error,
                     quoted_message_id=quote_message_id if citado else "")
        if enviado_id:
            return ResultadoEnvio(ok=True, tipo_midia=tipo_midia, desfecho=Desfecho.ENTREGUE,
                                  enviado_id=enviado_id, motivo="",
                                  evidencia={"key_id": enviado_id, "desfecho": Desfecho.ENTREGUE,
                                             "leitura": motivo},
                                  **comum)
        return ResultadoEnvio(ok=False, tipo_midia="nenhum", desfecho=Desfecho.INCERTA,
                              sem_prova=True, motivo=motivo,
                              evidencia={"transitorio": False, "sem_prova": True,
                                         "desfecho": Desfecho.INCERTA},
                              **comum)

    def _resultado_aceito(self, corpo: dict, payload: dict, *, campo: str, via: str,
                          tipo_midia: str, citado: bool, quote_message_id: str,
                          quote_status: str, quote_error: str) -> ResultadoEnvio:
        """Le' a resposta de um POST aceito: prova de entrega e de citacao."""
        http_status = int(corpo.pop("_http_status", 0) or 0)
        enviado_id = extrair_key_id(corpo)
        if citado:
            # Sem key.id nao ha' mensagem provada -- muito menos citacao provada.
            quote_status = (conferir_citacao(corpo, quote_message_id) if enviado_id
                            else QuoteStatus.UNVERIFIED)
        comum = dict(via=via, provider="evolution", quote_status=quote_status,
                     http_status=http_status, quote_error=quote_error,
                     quoted_message_id=quote_message_id if citado else "")
        evidencia = {"http_status": http_status, "corpo": _resumo(corpo),
                     "quote_status": quote_status}
        if quote_error:
            evidencia["motivo_citacao"] = quote_error

        if not enviado_id:
            # 2xx sem id nao e' prova de entrega -- e tambem nao e' prova de
            # que NAO saiu. Nao inventar confirmacao, e nao reenviar sozinho.
            return ResultadoEnvio(
                ok=False, tipo_midia="nenhum", desfecho=Desfecho.INCERTA, sem_prova=True,
                motivo=f"a Evolution respondeu {http_status} sem key.id; a mensagem "
                       "pode ter saído",
                evidencia={**evidencia, "transitorio": False, "sem_prova": True,
                           "desfecho": Desfecho.INCERTA},
                **comum)

        if str(corpo.get("status") or "").strip().upper() == "ERROR":
            # key.id com status ERROR: a mensagem foi criada na instancia, mas
            # o WhatsApp nao a aceitou (WAMessageStatus.ERROR do Baileys). Nao
            # e' prova de entrega -- e tambem nao prova que nao chegou.
            return ResultadoEnvio(
                ok=False, tipo_midia="nenhum", desfecho=Desfecho.INCERTA, sem_prova=True,
                motivo=f"a Evolution devolveu o id {enviado_id} com status ERROR; a "
                       "mensagem pode não ter chegado",
                evidencia={**evidencia, "key_id": enviado_id, "transitorio": False,
                           "sem_prova": True, "desfecho": Desfecho.INCERTA},
                **comum)

        self._lembrar_envio(payload.get(campo, ""))
        return ResultadoEnvio(
            ok=True, tipo_midia=tipo_midia, desfecho=Desfecho.ENTREGUE,
            # ``quoted_ok`` afirma que a citacao PEGOU -- so' com prova. A
            # ``unverified`` saiu com o ``quoted`` na requisicao (a versao
            # curta, sem o nome do consultor), mas a API nao devolveu o
            # ``stanzaId``: nao da' para afirmar. Qual texto saiu e' outra
            # pergunta, respondida pelo ``quote_status``.
            quoted_ok=quote_status == QuoteStatus.OK,
            enviado_id=enviado_id,
            media_id=_id_da_midia(corpo) if tipo_midia == "imagem" else "",
            evidencia={**evidencia, "key_id": enviado_id, "desfecho": Desfecho.ENTREGUE},
            **comum)

    @staticmethod
    def _falha(exc: ErroDeEnvio, *, via: str, citado: bool, quote_message_id: str,
               quote_status: str | None = None, quote_error: str = "") -> ResultadoEnvio:
        """Envio sem prova de entrega. O desfecho diz se a mensagem PODE ter saido."""
        incerta = exc.desfecho == Desfecho.INCERTA
        if quote_status is None:
            # Requisicao que nao foi processada nao avaliou citacao nenhuma ("").
            # Uma incerta pode ter saido COM a citacao: nao da' para provar.
            quote_status = ((QuoteStatus.UNVERIFIED if incerta else "") if citado
                            else QuoteStatus.NONE)
        return ResultadoEnvio(
            ok=False, via=via, tipo_midia="nenhum", motivo=str(exc),
            provider="evolution", desfecho=exc.desfecho,
            transitorio=exc.transitorio, sem_prova=incerta,
            http_status=exc.status_http, quote_status=quote_status,
            quoted_message_id=quote_message_id if citado else "",
            quote_error=quote_error,
            evidencia={"transitorio": exc.transitorio, "sem_prova": incerta,
                       "desfecho": exc.desfecho, "corpo": exc.corpo[:400],
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
                                  desfecho=Desfecho.PERMANENTE,
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
                                  desfecho=Desfecho.PERMANENTE,
                                  motivo="envio sem chat_id: a origem da solicitação se perdeu",
                                  evidencia={"transitorio": False})

        caminho = Path(image_path)
        try:
            validar_png(caminho)
        except PngInvalido as exc:
            return ResultadoEnvio(ok=False, via="imagem", tipo_midia="nenhum",
                                  provider="evolution", motivo=str(exc),
                                  desfecho=Desfecho.RECUSADA,
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
