"""Le' o que a Evolution entrega e transforma em ``IncomingMessage``.

Antes, descobrir estas cinco coisas custava JavaScript rodando dentro da
pagina do WhatsApp, e cada uma delas ja' quebrou em producao pelo menos uma
vez:

======================================  ==========================
o que era feito no DOM                  campo do webhook
======================================  ==========================
saber se a mensagem e' nossa            ``key.fromMe``
id da mensagem, para citar              ``key.id``
quem mandou                             ``key.participant``
o texto                                 ``message.conversation``
filtrar o grupo certo                   ``key.remoteJid``
======================================  ==========================

A guarda anti-laco -- que ja' rendeu 53 mensagens em cadeia no grupo do
cliente quando o bot leu as proprias respostas -- vira ``key.fromMe``. A
segunda camada (reconhecer o formato das nossas respostas, em
``mensagens.ASSINATURAS``) continua ativa no ``manager``: ela nao custa nada
e defesa em profundidade e' barata comparada a repetir aquele episodio.

Este modulo **nao decide se a mensagem vira solicitacao**. Isso continua
sendo do ``parser``, que nao muda. Aqui so' se decide o que e' ruido.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .models import IncomingMessage

#: Eventos que nos interessam. Qualquer outro e' descartado sem barulho --
#: a Evolution emite muita coisa (presenca, recibo, atualizacao de contato).
EVENTOS_ATENDIDOS = ("messages.upsert", "connection.update")


@dataclass(frozen=True)
class Leitura:
    """O que saiu de um payload de webhook.

    ``mensagem`` vem preenchida quando ha' algo para o sistema processar.
    ``motivo`` sempre explica a decisao -- inclusive quando ela foi ignorar,
    porque "o bot nao respondeu e ninguem sabe por que" foi o problema que
    custou mais tempo neste projeto.
    """

    mensagem: IncomingMessage | None = None
    motivo: str = ""
    aviso: str = ""          # preenchido quando algo merece WARNING no log

    def __bool__(self) -> bool:
        return self.mensagem is not None


def extrair_texto(message: dict) -> str:
    """O texto pode chegar em dois lugares.

    ``conversation`` e' a mensagem simples. Quando ela cita outra, tem link,
    ou formatacao, o WhatsApp usa ``extendedTextMessage``. Ler so' o primeiro
    faria o bot ignorar todo pedido que respondesse a alguem -- que e'
    justamente como um consultor costuma mandar correcao.
    """
    if not isinstance(message, dict):
        return ""
    direto = message.get("conversation")
    if isinstance(direto, str) and direto.strip():
        return direto.strip()
    estendida = message.get("extendedTextMessage") or {}
    texto = estendida.get("text") if isinstance(estendida, dict) else ""
    return (texto or "").strip() if isinstance(texto, str) else ""


def eh_lid(jid: str) -> bool:
    return (jid or "").endswith("@lid")


def identificar_remetente(key: dict, data: dict) -> tuple[str, str]:
    """Devolve ``(jid_para_identidade, aviso)``.

    O WhatsApp esta' trocando os identificadores: o remetente pode chegar
    como ``<numero>@s.whatsapp.net`` (o formato de sempre, e o numero antes
    do ``@`` E' o telefone) ou como ``<algo>@lid``, onde o valor **nao e'**
    telefone e nao casa com o cadastro.

    Quando for LID, tentamos o telefone que algumas versoes mandam junto
    (``senderPn``/``participantPn``). Nao vindo, devolvemos o LID mesmo: o
    cadastro passa a conhecer o consultor por ele, e o pedido segue. Perder a
    solicitacao seria muito pior do que nao saber a ficha de quem mandou.
    """
    participante = (key.get("participant") or "").strip()
    remoto = (key.get("remoteJid") or "").strip()
    # Em conversa individual nao ha' ``participant``: o proprio chat e' quem fala.
    bruto = participante or remoto

    if not eh_lid(bruto):
        return bruto, ""

    for campo in ("senderPn", "participantPn", "participantAlt"):
        alternativo = (key.get(campo) or data.get(campo) or "").strip()
        if alternativo and not eh_lid(alternativo):
            return alternativo, ""

    return bruto, (f"remetente veio como LID ({bruto}) e nao consegui resolver o "
                   "telefone; a solicitacao segue, mas o cadastro pode duplicar")


def interpretar(payload: dict, group_jid: str, group_name: str = "") -> Leitura:
    """Aplica os filtros, na ordem, e monta a mensagem quando sobra algo."""
    if not isinstance(payload, dict):
        return Leitura(motivo="payload não é um objeto")

    evento = (payload.get("event") or "").strip().lower()
    if evento and evento not in EVENTOS_ATENDIDOS:
        return Leitura(motivo=f"evento ignorado: {evento}")
    if evento == "connection.update":
        return Leitura(motivo="connection.update")

    data = payload.get("data")
    # Algumas versoes mandam um lote em ``data``. Tratar so' o primeiro item
    # e' suficiente porque o webhook e' chamado uma vez por mensagem; um lote
    # inesperado ainda rende a primeira em vez de render nada.
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return Leitura(motivo="payload sem data")

    key = data.get("key")
    if not isinstance(key, dict):
        return Leitura(motivo="payload sem key")

    # 1) outra conversa
    remoto = (key.get("remoteJid") or "").strip()
    if group_jid and remoto != group_jid:
        return Leitura(motivo=f"outra conversa ({remoto})")

    # 2) nossa propria mensagem -- a guarda anti-laco, agora numa linha
    if key.get("fromMe") is True:
        return Leitura(motivo="mensagem nossa (fromMe)")

    # 3) texto
    texto = extrair_texto(data.get("message") or {})
    if not texto:
        return Leitura(motivo="mensagem sem texto")

    message_id = (key.get("id") or "").strip()
    if not message_id:
        # Sem id nao da' para citar nem para deduplicar. Recusar e' melhor do
        # que processar algo que nao conseguimos nem responder direito.
        return Leitura(motivo="mensagem sem key.id")

    remetente, aviso = identificar_remetente(key, data)

    carimbo = data.get("messageTimestamp") or ""
    return Leitura(
        mensagem=IncomingMessage(
            message_id=message_id,
            chat_id=remoto or group_jid,
            chat_name=group_name or remoto or group_jid,
            sender_id=remetente,
            sender_name=(data.get("pushName") or "").strip(),
            text=texto,
            timestamp=str(carimbo),
        ),
        motivo="ok",
        aviso=aviso,
    )


class MemoriaDeIds:
    """Lembra quais ``key.id`` ja' foram processados.

    A Evolution reentrega o evento quando nao recebe 200 a tempo. Sem esta
    trava, um webhook reentregue vira uma segunda simulacao do mesmo CPF e
    uma segunda resposta no grupo -- o consultor pensa que simulamos duas
    vezes, e o portal do banco leva o dobro de carga a toa.

    Limitada de proposito: e' uma trava contra reentrega imediata, nao um
    historico. O banco ja' guarda o historico.
    """

    def __init__(self, limite: int = 2000) -> None:
        self.limite = limite
        self._vistos: dict[str, None] = {}
        self._lock = threading.Lock()

    def ja_visto(self, message_id: str) -> bool:
        """Marca como visto e diz se JA' estava. Uma operacao so', de proposito.

        Consultar e marcar em chamadas separadas abriria uma janela entre as
        duas -- e duas entregas simultaneas do mesmo evento passariam pelas
        duas antes de qualquer uma marcar.
        """
        if not message_id:
            return False
        with self._lock:
            if message_id in self._vistos:
                return True
            self._vistos[message_id] = None
            while len(self._vistos) > self.limite:
                self._vistos.pop(next(iter(self._vistos)))
            return False
