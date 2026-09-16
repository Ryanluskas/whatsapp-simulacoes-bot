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
    REPLYING = "replying"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


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
    Stage.REPLYING: "Enviando resposta",
    Stage.COMPLETED: "Concluído",
    Stage.ERROR: "Erro",
    Stage.CANCELLED: "Cancelado",
    Stage.INTERRUPTED: "Interrompido",
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
    Stage.REPLYING: Status.PROCESSING,
    Stage.COMPLETED: Status.COMPLETED,
    Stage.ERROR: Status.ERROR,
    Stage.CANCELLED: Status.CANCELLED,
    Stage.INTERRUPTED: Status.INTERRUPTED,
}


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

    def __bool__(self) -> bool:
        """Compatibilidade: o codigo antigo tratava o retorno como booleano."""
        return self.ok

    @property
    def parcial(self) -> bool:
        """Chegou, mas incompleto. Aparece no log e no painel."""
        return bool(self.ok and self.legenda_ok is False)
