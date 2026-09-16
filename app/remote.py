"""Simulador remoto: a fila fica no container, a automação roda no Windows.

Por que existe
--------------
O painel e o bot do WhatsApp rodam bem em container. O bot do Santander, não:
ele é construído contra detecção de robô (``STEALTH_JS``, user agent do Brave,
mouse simulado, pausas aleatórias) e depende de um navegador real, de um perfil
já logado e de intervenção humana no relogin e no OTP. Chromium headless dentro
de um container é justamente o cenário mais fácil de bloquear.

Então o sistema se divide em dois:

    [container]  painel + WhatsApp + fila + banco
         ^  |
   resultado |  job
         |  v
    [Windows]   agente.py -> bot.py do Arqueiro -> Brave -> Santander

Nada muda para o resto do sistema: ``QueueService`` continua chamando
``execute(job)`` e recebendo um ``SimulationResult``. A diferença é que aqui a
chamada bloqueia esperando um agente do outro lado da rede, em vez de um
navegador local. Tentativas, timeout, isolamento por ``request_id`` e os
eventos do monitor continuam valendo, sem exceção.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .clock import now_iso
from .models import SimulationJob, SimulationResult, Stage


# Quanto tempo uma reivindicacao vale sem sinal do agente. O agente reporta
# etapa a cada fase da simulacao, entao esse silencio significa que ele morreu.
CLAIM_TTL_SECONDS = 90.0


@dataclass
class _Pendente:
    job: SimulationJob
    on_stage: Callable[[str], None] | None
    pronto: threading.Event = field(default_factory=threading.Event)
    resultado: SimulationResult | None = None
    reivindicado_em: float = 0.0
    agente: str = ""

    @property
    def em_execucao(self) -> bool:
        return self.reivindicado_em > 0


class RemoteSimulator:
    """Fila de trabalho para agentes externos.

    Implementa a mesma interface que ``SimulatorService``, de modo que o
    ``QueueService`` não sabe (nem precisa saber) qual dos dois está usando.
    """

    def __init__(self, name: str = "simulador-remoto",
                 on_log: Callable[[str, str], None] | None = None) -> None:
        self.name = name
        self._on_log = on_log
        self._lock = threading.Lock()
        self._pendentes: dict[str, _Pendente] = {}
        self._ordem: list[str] = []
        self._ultimo_contato: str = ""
        self._agente_visto: str = ""
        self.running = True

    # ------------------------------------------------------------------ logs
    def _log(self, level: str, message: str) -> None:
        if self._on_log:
            try:
                self._on_log(level, message)
            except Exception:
                pass

    # ------------------------------------- interface esperada pela fila ----
    def start(self) -> None:  # nada a iniciar: quem trabalha é o agente
        self.running = True

    def stop(self, *_args, **_kwargs) -> None:
        self.running = False
        # Solta quem estiver esperando, para o desligamento não travar.
        with self._lock:
            pendentes = list(self._pendentes.values())
        for pendente in pendentes:
            if not pendente.pronto.is_set():
                pendente.resultado = SimulationResult(
                    job=pendente.job, ok=False, status="Erro",
                    error="sistema encerrando", retryable=True,
                )
                pendente.pronto.set()

    def status(self) -> dict:
        with self._lock:
            executando = sum(1 for p in self._pendentes.values() if p.em_execucao)
            aguardando = len(self._pendentes) - executando
        return {
            "name": self.name,
            "running": self.running,
            "ready": bool(self._ultimo_contato),
            "busy": executando > 0,
            "busy_seconds": 0.0,
            "mode": "remoto",
            "agent": self._agente_visto,
            "last_seen": self._ultimo_contato,
            "waiting": aguardando,
            "last_error": "" if self._ultimo_contato else "nenhum agente conectado ainda",
        }

    def execute(
        self,
        job: SimulationJob,
        on_stage: Callable[[str], None] | None = None,
        timeout: float | None = None,
    ) -> SimulationResult:
        """Publica o job para os agentes e espera o resultado."""
        pendente = _Pendente(job=job, on_stage=on_stage)
        with self._lock:
            self._pendentes[job.request_id] = pendente
            self._ordem.append(job.request_id)

        limite = timeout or 300.0
        try:
            if not pendente.pronto.wait(limite):
                return SimulationResult(
                    job=job, ok=False, status="Timeout",
                    error=("nenhum agente concluiu esta simulação a tempo"
                           if pendente.em_execucao
                           else "nenhum agente do simulador está conectado"),
                    retryable=True,
                )
            return pendente.resultado or SimulationResult(
                job=job, ok=False, status="Erro",
                error="o agente não devolveu resultado", retryable=True,
            )
        finally:
            with self._lock:
                self._pendentes.pop(job.request_id, None)
                if job.request_id in self._ordem:
                    self._ordem.remove(job.request_id)

    # ------------------------------------------------ API usada pelo agente
    def claim(self, agente: str = "") -> dict | None:
        """Entrega o próximo job ainda não reivindicado."""
        self._ultimo_contato = now_iso()
        if agente:
            self._agente_visto = agente

        agora = time.monotonic()
        with self._lock:
            for request_id in self._ordem:
                pendente = self._pendentes.get(request_id)
                if not pendente or pendente.pronto.is_set():
                    continue
                # Reivindicação vencida: o agente pegou o trabalho e sumiu
                # (caiu, foi reiniciado, perdeu a rede). Sem esta liberação o
                # job ficaria preso até o timeout do job inteiro, e o consultor
                # esperaria minutos por um erro — mesmo com o agente de volta
                # em segundos.
                if pendente.em_execucao:
                    if agora - pendente.reivindicado_em < CLAIM_TTL_SECONDS:
                        continue
                    self._log(
                        "WARNING",
                        f"{request_id}: agente '{pendente.agente or 'sem nome'}' não deu "
                        f"sinal em {int(CLAIM_TTL_SECONDS)}s. Devolvendo à fila.",
                    )
                pendente.reivindicado_em = agora
                pendente.agente = agente
                job = pendente.job
                break
            else:
                return None

        req = job.request
        self._log("INFO", f"{job.request_id} entregue ao agente {agente or 'sem nome'}.")
        return {
            "request_id": job.request_id,
            "simulation_id": job.simulation_id,
            "attempt": job.attempt,
            "consultant_name": req.consultant_name,
            "customer_name": req.customer_name,
            "origin": req.origin,
            "cpf": req.cpf,
            "bank": req.bank,
            "contract": req.contract,
            "phone": req.phone,
            "simulation_type": req.simulation_type,
        }

    def report_stage(self, request_id: str, stage: str) -> bool:
        """Progresso vindo do agente: alimenta o monitor em tempo real."""
        self._ultimo_contato = now_iso()
        with self._lock:
            pendente = self._pendentes.get(request_id)
            if pendente and pendente.em_execucao:
                # Sinal de vida: renova a reivindicação para uma simulação
                # demorada não ser tomada de um agente que está trabalhando.
                pendente.reivindicado_em = time.monotonic()
        if not pendente or stage not in _ETAPAS_ACEITAS:
            return False
        if pendente.on_stage:
            try:
                pendente.on_stage(stage)
            except Exception:
                pass
        return True

    def submit_result(self, request_id: str, payload: dict[str, Any]) -> bool:
        """Resultado final vindo do agente."""
        self._ultimo_contato = now_iso()
        with self._lock:
            pendente = self._pendentes.get(request_id)
        if not pendente or pendente.pronto.is_set():
            return False

        # O request_id sozinho nao basta como identidade: se o banco for
        # recriado, a numeracao recomeca, e o resultado atrasado de um
        # agente cairia num pedido NOVO com o mesmo REQ -- o resultado de um
        # cliente respondendo o pedido de outro. O simulation_id amarra.
        # (Agentes antigos nao mandam o campo; nesse caso vale o request_id.)
        informado = payload.get("simulation_id")
        if informado not in (None, "") and str(informado) != str(pendente.job.simulation_id):
            self._log("WARNING",
                      f"{request_id}: resultado recusado -- veio para a simulação "
                      f"{informado}, mas a pendente é {pendente.job.simulation_id}.")
            return False

        contratos = payload.get("contracts") or []
        if not isinstance(contratos, list):
            contratos = []
        # O que o portal disse, literal. Sem isto uma recusa pelo modo remoto
        # chegava ao consultor como "nenhum contrato encontrado" -- a frase
        # generica que so' vale quando o portal nao disse nada.
        motivos = payload.get("motivos") or []
        if not isinstance(motivos, list):
            motivos = []

        pendente.resultado = SimulationResult(
            job=pendente.job,
            ok=bool(payload.get("ok")),
            status=str(payload.get("status") or ""),
            error=str(payload.get("error") or "")[:400],
            retryable=bool(payload.get("retryable")),
            reduction_value=_float(payload.get("reduction_value")),
            margin=str(payload.get("margin") or ""),
            contracts=tuple(c for c in contratos if isinstance(c, dict)),
            installment_sum=_float(payload.get("installment_sum")),
            installment_count=int(_float(payload.get("installment_count"))),
            debt_sum=_float(payload.get("debt_sum")),
            motivos=tuple(m for m in motivos if isinstance(m, dict)),
        )
        pendente.pronto.set()
        return True

    def agent_info(self) -> dict:
        with self._lock:
            aguardando = [
                rid for rid in self._ordem
                if (p := self._pendentes.get(rid)) and not p.em_execucao
            ]
        return {
            "connected": bool(self._ultimo_contato),
            "agent": self._agente_visto,
            "last_seen": self._ultimo_contato,
            "waiting": len(aguardando),
        }


_ETAPAS_ACEITAS = {Stage.CONSULTING, Stage.EXTRACTING, Stage.PROCESSING}


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
