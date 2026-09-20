"""A regra da citação, num lugar só.

UMA SOLICITAÇÃO → UMA ORIGEM → UMA RESPOSTA → UMA CITAÇÃO DAQUELA ORIGEM.

Por que este módulo existe
--------------------------
A identidade da mensagem original sempre esteve gravada
(``simulations.source_message_id``), mas quem decidia "a citação está certa?"
era cada camada por conta própria, e ninguém registrava **qual** mensagem foi
citada. O banco de produção de 20/09/2026 mostra o tamanho do buraco:

* 25 mensagens de saída gravadas, **0** com ``quoted_message_id``;
* 10 solicitações com ``quote_status=ok`` — nenhuma delas com a prova de
  contra qual mensagem esse "ok" foi medido;
* e o grupo repete cliente o tempo todo: sete pares de solicitações com o
  mesmo nome, entre eles as duas que o operador viu saindo erradas.

No modo ``dom`` a conferência compara o TEXTO da barra de citação com o texto
da mensagem. Com duas mensagens iguais na tela, as duas passam nessa conta --
a prova não distingue, e mesmo assim o resultado virava ``ok``.

O que este módulo garante
-------------------------
Uma função decide, e é a mesma para as duas camadas (``dom`` e
``evolution``). Ela não envia, não acessa rede e não olha tela: recebe o que
está gravado e o que a camada pretende fazer, e responde se pode.

Falha aqui **bloqueia o envio com citação**. Responder à mensagem errada é
pior do que responder sem citação nenhuma: o consultor leria o resultado de
outro cliente como se fosse o dele.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Motivos de recusa. São o que aparece no log e no teste -- texto livre aqui
#: viraria cinco grafias diferentes do mesmo problema.
QUOTE_SEM_REQUEST_ID = "QUOTE_SEM_REQUEST_ID"
QUOTE_SEM_ORIGIN_MESSAGE_ID = "QUOTE_SEM_ORIGIN_MESSAGE_ID"
QUOTE_SEM_CHAT_ID = "QUOTE_SEM_CHAT_ID"
QUOTE_ALVO_AUSENTE = "QUOTE_ALVO_AUSENTE"
QUOTE_ID_MISMATCH = "QUOTE_ID_MISMATCH"
QUOTE_CHAT_MISMATCH = "QUOTE_CHAT_MISMATCH"


@dataclass(frozen=True)
class Origem:
    """A mensagem que originou a solicitação, como está GRAVADA.

    Nunca é montada a partir da tela, da última mensagem, do texto ou de um
    dicionário em memória: nasce na ingestão, vai para o banco, e é de lá que
    volta -- inclusive depois de um reinício.
    """

    request_id: str
    message_id: str
    chat_id: str
    participant: str = ""

    @classmethod
    def da_linha(cls, linha: dict) -> "Origem":
        """Constrói a origem a partir da linha de ``simulations``."""
        return cls(
            request_id=str(linha.get("request_id") or ""),
            message_id=str(linha.get("source_message_id") or ""),
            chat_id=str(linha.get("chat_id") or ""),
            participant=str(linha.get("participant") or ""),
        )

    @property
    def completa(self) -> bool:
        return bool(self.request_id and self.message_id and self.chat_id)


@dataclass(frozen=True)
class Conferencia:
    """O veredito. ``ok=False`` significa: não envie citando."""

    ok: bool
    motivo: str = ""
    detalhe: str = ""

    def __bool__(self) -> bool:
        return self.ok


APROVADO = Conferencia(ok=True)


def conferir_integridade(origem: Origem, *, alvo: str, chat_id: str,
                         com_citacao: bool = True) -> Conferencia:
    """A resposta pode sair citando ``alvo``?

    ``alvo`` é o id que a camada vai colocar na requisição (Evolution) ou o id
    da linha que ela marcou na tela (dom). ``chat_id`` é a conversa para onde
    a resposta vai.

    Sem citação (``com_citacao=False``) a conferência continua valendo para a
    identidade da solicitação e para a conversa -- uma resposta sem citação
    ainda não pode ir para outro grupo.
    """
    if not origem.request_id:
        return Conferencia(False, QUOTE_SEM_REQUEST_ID,
                           "a solicitação chegou aqui sem request_id")
    if not origem.message_id:
        return Conferencia(
            False, QUOTE_SEM_ORIGIN_MESSAGE_ID,
            f"{origem.request_id} não tem origin_message_id gravado")
    if not origem.chat_id:
        return Conferencia(False, QUOTE_SEM_CHAT_ID,
                           f"{origem.request_id} não tem chat_id gravado")
    if chat_id and chat_id != origem.chat_id:
        return Conferencia(
            False, QUOTE_CHAT_MISMATCH,
            f"a resposta sairia em {chat_id!r} e o pedido veio de "
            f"{origem.chat_id!r}")
    if not com_citacao:
        return APROVADO
    if not alvo:
        return Conferencia(False, QUOTE_ALVO_AUSENTE,
                           f"{origem.request_id} pediu citação sem alvo nenhum")
    if alvo != origem.message_id:
        return Conferencia(
            False, QUOTE_ID_MISMATCH,
            f"a citação apontaria {alvo} e a origem é {origem.message_id}")
    return APROVADO


def linha_de_log(origem: Origem, *, alvo: str = "", chat_id: str = "",
                 estrategia: str = "", quote_status: str = "",
                 delivery_status: str = "", wa_message_id: str = "",
                 motivo: str = "") -> str:
    """Uma linha que explica a citação sem precisar abrir o código.

    Só identificadores e estados -- nada de CPF, nome de cliente, telefone ou
    token. O id de mensagem do WhatsApp não é segredo e é o que permite achar
    a conversa depois.
    """
    campos = [
        origem.request_id or "sem-REQ",
        f"origin_message_id={origem.message_id or '-'}",
        f"chat_id={chat_id or origem.chat_id or '-'}",
        f"quoted_message_id={alvo or '-'}",
        f"strategy={estrategia or '-'}",
        f"quote_status={quote_status or '-'}",
    ]
    if delivery_status:
        campos.append(f"delivery_status={delivery_status}")
    if wa_message_id:
        campos.append(f"wa_message_id={wa_message_id}")
    if motivo:
        campos.append(f"motivo={motivo}")
    return " ".join(campos)
