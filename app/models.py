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
    DELIVERY_UNCONFIRMED = "delivery_unconfirmed"  # a API aceitou, sem provar
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
    Stage.DELIVERY_UNCONFIRMED: "Entrega sem confirmação",
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
    UNCONFIRMED = "unconfirmed"  # 2xx sem id: nao reenvia sozinho (duplicaria)
    RETRYING = "retrying"        # falha transitoria; o laco de reenvio tenta
    FAILED = "failed"            # falha permanente ou tentativas esgotadas

    #: Os que o laco de reenvio pode pegar.
    REENVIAVEIS = ("", RETRYING)


class QuoteStatus:
    """O que aconteceu com a citacao da mensagem original."""

    NONE = "none"                # nao havia o que citar
    OK = "ok"                    # a API devolveu a mensagem com stanzaId certo
    UNVERIFIED = "unverified"    # enviada com quoted, resposta sem como conferir
    NOT_APPLIED = "not_applied"  # a API devolveu a mensagem SEM a citacao
    FALLBACK = "fallback"        # citacao recusada; saiu sem ela, com o nome


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
    #: Falhou por algo que passa sozinho (timeout, 429, 5xx)?
    transitorio: bool = False
    #: A API respondeu 2xx mas sem id: nao ha' como afirmar que saiu, e
    #: reenviar pode duplicar. Nao e' sucesso nem falha comum.
    sem_prova: bool = False
    media_id: str = ""

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

    def __bool__(self) -> bool:
        return self.status == Delivery.DELIVERED
