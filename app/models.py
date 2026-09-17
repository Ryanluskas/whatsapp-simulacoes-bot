"""Tipos de dominio e o vocabulario de estados usado em todo o sistema."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .clock import utc_now


class Stage:
    """Etapas do ciclo de vida de uma solicitacao.

    Sao gravadas em ``simulations.stage`` e emitidas nos eventos, de forma que
    a timeline do painel e a fila leem exatamente a mesma verdade.
    """

    RECEIVED = "received"
    IDENTIFIED = "identified"
    VALIDATED = "validated"
    QUEUED = "queued"
    PROCESSING = "processing"
    CONSULTING = "consulting"
    EXTRACTING = "extracting"
    RENDERING = "rendering"
    REPLYING = "replying"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    # --- entrega ---
    #
    # A simulacao terminou, mas a resposta ainda nao chegou ao consultor.
    # Antes estes casos eram gravados como `completed`, e o painel dizia
    # "concluido" para um resultado que ninguem tinha recebido.
    DELIVERY_RETRY = "delivery_retry"              # vai tentar de novo sozinho
    DELIVERY_UNCONFIRMED = "delivery_unconfirmed"  # pode ter saido; nao da' para provar
    DELIVERY_FAILED = "delivery_failed"            # desistiu; precisa de gente


class Status:
    """Estado agregado, usado para filtros e KPIs."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    OPEN = (QUEUED, PROCESSING, INTERRUPTED)
    TERMINAL = (COMPLETED, ERROR, CANCELLED)
    ALL = (QUEUED, PROCESSING, COMPLETED, ERROR, CANCELLED, INTERRUPTED)


STAGE_LABELS = {
    Stage.RECEIVED: "Mensagem recebida",
    Stage.IDENTIFIED: "Consultor identificado",
    Stage.VALIDATED: "Dados validados",
    Stage.QUEUED: "Na fila",
    Stage.PROCESSING: "Simulação iniciada",
    Stage.CONSULTING: "Consultando sistema",
    Stage.EXTRACTING: "Extraindo resultado",
    Stage.RENDERING: "Gerando imagem",
    Stage.REPLYING: "Enviando resposta",
    Stage.COMPLETED: "Concluído",
    Stage.ERROR: "Erro",
    Stage.CANCELLED: "Cancelado",
    Stage.INTERRUPTED: "Interrompido",
    Stage.DELIVERY_RETRY: "Reenvio pendente",
    Stage.DELIVERY_UNCONFIRMED: "Entrega incerta — verificar WhatsApp",
    Stage.DELIVERY_FAILED: "Entrega falhou",
}

STATUS_LABELS = {
    Status.QUEUED: "Aguardando",
    Status.PROCESSING: "Processando",
    Status.COMPLETED: "Concluído",
    Status.ERROR: "Erro",
    Status.CANCELLED: "Cancelado",
    Status.INTERRUPTED: "Interrompido",
}

STAGE_TO_STATUS = {
    Stage.RECEIVED: Status.QUEUED,
    Stage.IDENTIFIED: Status.QUEUED,
    Stage.VALIDATED: Status.QUEUED,
    Stage.QUEUED: Status.QUEUED,
    Stage.PROCESSING: Status.PROCESSING,
    Stage.CONSULTING: Status.PROCESSING,
    Stage.EXTRACTING: Status.PROCESSING,
    Stage.RENDERING: Status.PROCESSING,
    Stage.REPLYING: Status.PROCESSING,
    Stage.COMPLETED: Status.COMPLETED,
    Stage.ERROR: Status.ERROR,
    Stage.CANCELLED: Status.CANCELLED,
    Stage.INTERRUPTED: Status.INTERRUPTED,
}


class Delivery:
    """Estado da ENTREGA, separado do resultado da simulacao.

    Sao perguntas diferentes: "o portal respondeu?" e "o consultor recebeu?".
    Misturar as duas num `status` so' foi o que fez resultado nao entregue
    aparecer como concluido.

    Vazio (``""``) e' o legado: linhas gravadas antes desta coluna existir.
    """

    PENDING = "pending"          # em curso agora; ninguem mais pode tocar
    DELIVERED = "delivered"      # a camada devolveu prova (ou o modo dom confirmou)
    UNCONFIRMED = "unconfirmed"  # pode ter saido (500, timeout, 2xx sem id): nao reenvia sozinho
    RETRYING = "retrying"        # falha transitoria; o laco de reenvio tenta
    FAILED = "failed"            # falha permanente ou tentativas esgotadas

    #: Os que o laco de reenvio pode pegar.
    REENVIAVEIS = ("", RETRYING)


class QuoteStatus:
    """O que aconteceu com a CITACAO -- independente de a mensagem ter chegado.

    Citacao e entrega sao perguntas diferentes, e cada uma tem o seu campo
    (``quote_status`` e ``delivery_status``). Uma citacao recusada nao e'
    entrega falha; uma entrega incerta nao diz nada sobre a citacao.

    Vazio (``""``) = a citacao nao chegou a ser avaliada (a requisicao nao
    foi processada: 429, 503, conexao recusada...).
    """

    NONE = "none"                # nao havia o que citar
    #: A resposta da API traz ``contextInfo.stanzaId`` IGUAL ao id pedido.
    OK = "ok"
    #: A mensagem pode ter saido com a citacao, mas a resposta nao permitiu
    #: provar (sem ``stanzaId``, sem ``key.id``, 500, timeout). Nunca vira OK.
    UNVERIFIED = "unverified"
    #: A API provou que a mensagem saiu citando OUTRA coisa (``stanzaId``
    #: diferente). A mensagem chegou; NAO se manda uma segunda.
    NOT_APPLIED = "not_applied"
    #: A API RECUSOU explicitamente a citacao (400/422 que aponta o quoted);
    #: a resposta foi mandada de novo SEM citacao, com o nome do consultor.
    FALLBACK = "fallback"


class EnvioSemProva(RuntimeError):
    """A camada falhou DEPOIS de disparar o envio: a mensagem pode ter saido.

    Quem recebe trata como entrega incerta -- nem texto por cima, nem reenvio
    automatico. Qualquer excecao com ``sem_prova = True`` vale o mesmo (ex.:
    ``ActorTimeout`` de um comando que chegou a comecar).
    """

    sem_prova = True


class EnvioNaoSaiu(RuntimeError):
    """Prova de que nada saiu (ex.: a pre-visualizacao continua aberta)."""

    sem_prova = False


class Desfecho:
    """Como terminou UMA requisicao a API do WhatsApp.

    A pergunta que decide tudo e': **a mensagem pode ter saido?**

    * so' ``RECUSADA``, ``CITACAO_RECUSADA``, ``PERMANENTE`` e ``TRANSITORIA``
      garantem que NAO saiu -- e so' nelas e' seguro mandar de novo (sem
      citacao, em texto, ou mais tarde);
    * ``INCERTA`` = a API nao deixou provar nem uma coisa nem outra (500,
      timeout depois de enviar, conexao caida no meio, 2xx sem ``key.id``).
      Mandar de novo pode duplicar a resposta no grupo; nao se manda.
    """

    ENTREGUE = "entregue"                  # 2xx com key.id
    CITACAO_RECUSADA = "citacao_recusada"  # 400/422 apontando o quoted
    RECUSADA = "recusada"                  # 4xx de validacao: nada saiu
    PERMANENTE = "permanente"              # 401/403/404, licenca: nada saiu
    TRANSITORIA = "transitoria"            # 429/502/503/504, nao conectou: nada saiu
    INCERTA = "incerta"                    # pode ter saido


@dataclass(frozen=True)
class IncomingMessage:
    """Mensagem lida do WhatsApp Web, ja' normalizada."""

    message_id: str
    chat_id: str
    chat_name: str
    sender_id: str          # JID real do remetente (ex.: 5567999999999@c.us)
    sender_name: str        # nome exibido no WhatsApp
    text: str
    timestamp: str = ""
    #: O ``key.participant`` exatamente como chegou (pode ser ``@lid``).
    #: ``sender_id`` e' a IDENTIDADE (telefone resolvido); este e' o que a
    #: citacao precisa para apontar o autor certo dentro do grupo.
    participant: str = ""

    @property
    def sender_phone(self) -> str:
        raw = self.sender_id.split("@", 1)[0]
        return "".join(ch for ch in raw if ch.isdigit())


@dataclass(frozen=True)
class ParsedRequest:
    consultant_name: str
    cpf: str
    bank: str
    contract: str
    simulation_type: str = "consignado"
    customer_name: str = "Lead"
    origin: str = ""          # órgão/estado, a 2ª linha da mensagem do grupo
    phone: str = ""


@dataclass(frozen=True)
class SimulationJob:
    request: ParsedRequest
    message: IncomingMessage
    request_id: str
    simulation_id: int
    consultant_id: int | None = None
    attempt: int = 1
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class SimulationResult:
    job: SimulationJob
    ok: bool
    status: str = ""
    error: str = ""
    retryable: bool = False
    reduction_value: float = 0.0
    margin: str = ""
    contracts: tuple = ()
    #: O que o portal disse, LITERAL, na ordem em que apareceu.
    #: [{"texto": ..., "categoria": ...}] -- ver `app/motivos.py`.
    #: Vazio de verdade significa "o portal nao disse nada", e so' nesse caso
    #: a resposta pode falar em "nenhum contrato encontrado".
    motivos: tuple = ()
    installment_sum: float = 0.0
    installment_count: int = 0
    debt_sum: float = 0.0
    #: PNG da tela do Santander, recortado na area do resultado.
    #: Vazio quando o recorte nao pode ser feito com seguranca -- e ai'
    #: a resposta sai com o card montado por nos. Ver `tela_do_portal`.
    portal_png: str = ""

    @property
    def has_refin(self) -> bool:
        return self.status == "Sim"


@dataclass(frozen=True)
class ResultadoEnvio:
    """O que aconteceu numa tentativa de entrega, com prova.

    Trocar o ``bool`` por isto e' a correcao estrutural do envio: antes o
    codigo executava uma acao na interface e assumia que deu certo. Quando o
    DOM do WhatsApp mudava, o passo falhava, ninguem percebia, e o fluxo
    seguia como se tivesse funcionado -- foi assim que a imagem virou
    documento e a citacao sumiu sem deixar rastro.

    Regra: nenhuma etapa e' concluida sem uma verificacao LIDA do DOM depois
    dela. Nao deu para verificar? A etapa falhou.
    """

    ok: bool
    via: str = ""                 # "colar" | "input_imagem" | "texto"
    quoted_ok: bool = False
    tipo_midia: str = "nenhum"    # "imagem" | "documento" | "nenhum"
    #: None = nao se aplica (envio de texto). True/False = a legenda entrou.
    #: Imagem enviada sem a legenda pedida e' entrega PARCIAL: o card chega,
    #: mas sem o valor escrito, sem o nome do cliente e sem o _REQ -- que e' a
    #: chave de busca no painel. Contar isso como sucesso escondeu o defeito
    #: por dois dias, com o log dizendo "imagem ok".
    legenda_ok: bool | None = None
    motivo: str = ""              # vazio quando ok=True
    evidencia: dict = field(default_factory=dict)

    # --- evidencia tipada (a camada Evolution preenche; a dom, o que puder) ---
    provider: str = ""            # "evolution" | "dom"
    #: Ver ``QuoteStatus``. Vazio = a camada nao informou.
    quote_status: str = ""
    #: O id que o WhatsApp deu a mensagem que SAIU. E' a prova de entrega.
    enviado_id: str = ""
    http_status: int = 0
    #: Ver ``Desfecho``. Vazio = camada que nao classifica (dom, dublês).
    desfecho: str = ""
    #: NADA saiu e repetir a MESMA requisicao depois pode dar certo
    #: (429, 502/503/504, conexao recusada). E' ``desfecho == TRANSITORIA``.
    transitorio: bool = False
    #: A mensagem PODE ter saido e nao ha' como provar (500, timeout depois
    #: de enviar, 2xx sem id). Reenviar pode duplicar. E' ``desfecho == INCERTA``.
    sem_prova: bool = False
    media_id: str = ""
    #: O id citado na requisicao que produziu este desfecho ("" se ela foi
    #: sem citacao, inclusive no fallback).
    quoted_message_id: str = ""
    #: Por que a citacao foi recusada, quando foi. Separado de ``motivo``,
    #: que e' sobre a ENTREGA.
    quote_error: str = ""

    def __post_init__(self) -> None:
        # ``quoted_ok`` AFIRMA que a citacao pegou. Com ``quote_status``
        # informado, so' ``ok`` sustenta essa afirmacao: unverified,
        # not_applied, fallback e none nunca -- venha de que camada vier.
        if self.quote_status and self.quote_status != QuoteStatus.OK:
            object.__setattr__(self, "quoted_ok", False)   # dataclass congelado

    def __bool__(self) -> bool:
        """Compatibilidade: o codigo antigo tratava o retorno como booleano."""
        return self.ok

    @property
    def parcial(self) -> bool:
        """Chegou, mas incompleto. Aparece no log e no painel."""
        return bool(self.ok and self.legenda_ok is False)


@dataclass(frozen=True)
class Entrega:
    """Desfecho de UMA tentativa de entregar o resultado ao consultor.

    ``status`` e' um ``Delivery``. Os demais campos sao a evidencia que vai
    para a linha da solicitacao e para o log.
    """

    status: str
    motivo: str = ""
    enviado_id: str = ""
    quote_status: str = ""
    media_status: str = ""        # "ok" | "disabled" | "render_failed" | "invalid" | "send_failed" | "unconfirmed"
    kind: str = ""                # "image" | "text"
    tentativa: int = 1
    desfecho: str = ""
    http_status: int = 0
    quoted_message_id: str = ""
    quote_error: str = ""

    def __bool__(self) -> bool:
        return self.status == Delivery.DELIVERED
