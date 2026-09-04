"""Agente do simulador — roda no WINDOWS, ao lado do Brave.

Quando o painel está em container (``SIMULATOR_MODE=remote``), a automação do
Santander continua nesta máquina: ela precisa de um navegador real, de um
perfil já logado e de você por perto quando o portal pedir relogin ou OTP.

    [container]  painel + WhatsApp + fila + banco
         ^  |
   resultado |  job
         |  v
    [este script] -> bot.py do Arqueiro -> Brave -> Santander

O agente só faz três coisas: pedir trabalho, executar com o
``SimulatorService`` de sempre e devolver o resultado. Toda a inteligência de
fila, tentativas e eventos continua do outro lado.

Uso:

    python agente.py                      # lê o .env desta pasta
    python agente.py --url http://192.168.0.10:8000 --token SEGREDO
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from dotenv import load_dotenv

from app.config import ROOT, load_config
from app.models import (
    IncomingMessage, ParsedRequest, SimulationJob, SimulationResult,
)
from app.simulator import SimulatorService, SimulatorUnavailable

log = logging.getLogger("agente")

INTERVALO_OCIOSO = 3.0      # sem trabalho: espera antes de perguntar de novo
INTERVALO_ERRO = 10.0       # servidor fora do ar
BATIMENTO_SEGUNDOS = 25.0   # sinal de vida durante uma simulacao (TTL la' e' 90s)
PARAR = False


class Painel:
    """Cliente HTTP mínimo. Sem dependência nova: urllib da biblioteca padrão."""

    def __init__(self, base_url: str, token: str, nome: str) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.nome = nome

    def _post(self, caminho: str, dados: dict | None = None) -> tuple[int, Any]:
        import json

        corpo = json.dumps(dados or {}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}{caminho}", data=corpo, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Agent-Token": self.token,
                "X-Agent-Name": self.nome,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resposta:
                bruto = resposta.read()
                if resposta.status == 204 or not bruto:
                    return resposta.status, None
                return resposta.status, json.loads(bruto)
        except urllib.error.HTTPError as exc:
            detalhe = exc.read().decode("utf-8", "replace")[:200]
            return exc.code, detalhe

    def claim(self) -> dict | None:
        status, dados = self._post("/api/agent/claim")
        if status == 200 and isinstance(dados, dict):
            return dados
        if status == 204:
            return None
        if status == 401:
            raise SystemExit("Token do agente rejeitado. Confira AGENT_TOKEN.")
        if status == 409:
            raise SystemExit(
                "O painel não está em SIMULATOR_MODE=remote — este agente não é necessário."
            )
        if status == 503:
            raise SystemExit("O painel está sem AGENT_TOKEN configurado.")
        raise ConnectionError(f"claim devolveu {status}: {dados}")

    def stage(self, request_id: str, etapa: str) -> None:
        try:
            self._post("/api/agent/stage", {"request_id": request_id, "stage": etapa})
        except Exception:
            pass  # progresso é informativo; não pode derrubar a simulação

    def result(self, request_id: str, resultado: SimulationResult) -> bool:
        status, _ = self._post("/api/agent/result", {
            "request_id": request_id,
            "ok": resultado.ok,
            "status": resultado.status,
            "error": resultado.error,
            "retryable": resultado.retryable,
            "reduction_value": resultado.reduction_value,
            "margin": resultado.margin,
            "contracts": list(resultado.contracts),
            "installment_sum": resultado.installment_sum,
            "installment_count": resultado.installment_count,
            "debt_sum": resultado.debt_sum,
        })
        return status == 200


def _job_da_tarefa(tarefa: dict) -> SimulationJob:
    """Remonta o job. O agente não precisa do chat: quem responde é o painel."""
    pedido = ParsedRequest(
        consultant_name=tarefa.get("consultant_name") or "Consultor",
        cpf=tarefa.get("cpf") or "",
        bank=tarefa.get("bank") or "Santander",
        contract=tarefa.get("contract") or "",
        simulation_type=tarefa.get("simulation_type") or "consignado",
        customer_name=tarefa.get("customer_name") or "Lead",
        origin=tarefa.get("origin") or "",
        phone=tarefa.get("phone") or "",
    )
    vazia = IncomingMessage(
        message_id="", chat_id="", chat_name="", sender_id="", sender_name="", text="",
    )
    return SimulationJob(
        request=pedido,
        message=vazia,
        request_id=tarefa.get("request_id") or "",
        simulation_id=int(tarefa.get("simulation_id") or 0),
        attempt=int(tarefa.get("attempt") or 1),
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
    )
    load_dotenv(ROOT / ".env", override=False)

    parser = argparse.ArgumentParser(description="Agente do simulador (Windows)")
    parser.add_argument("--url", default=os.getenv("PANEL_URL", "http://127.0.0.1:8000"),
                        help="endereço do painel (ex.: http://192.168.0.10:8000)")
    parser.add_argument("--token", default=os.getenv("AGENT_TOKEN", ""),
                        help="mesmo AGENT_TOKEN configurado no painel")
    parser.add_argument("--name", default=os.getenv("AGENT_NAME", platform.node()),
                        help="nome deste agente, para aparecer nos logs")
    args = parser.parse_args(argv)

    if not args.token:
        log.error("AGENT_TOKEN não definido. Passe --token ou coloque no .env.")
        return 1

    config = load_config()
    if not config.bot_module_path.exists():
        log.error("bot.py não encontrado em %s. Ajuste SIM_BOT_PATH no .env.",
                  config.sim_bot_path)
        return 1

    painel = Painel(args.url, args.token, args.name)
    simulador = SimulatorService(config, index=0,
                                 on_log=lambda nivel, msg: log.info("simulador: %s", msg))
    simulador.start()

    def encerrar(*_a):
        global PARAR
        PARAR = True
        log.info("Encerrando o agente...")

    for sinal in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sinal, encerrar)
        except (ValueError, OSError):
            pass

    print()
    print("=" * 58)
    print("  AGENTE DO SIMULADOR - Allana")
    print("=" * 58)
    print(f"  Painel .....: {args.url}")
    print(f"  Agente .....: {args.name}")
    print(f"  bot.py .....: {config.sim_bot_path}")
    print("=" * 58)
    print("  Aguardando simulações. Deixe esta janela aberta.")
    print()

    ociosos = 0
    try:
        while not PARAR:
            try:
                tarefa = painel.claim()
            except SystemExit:
                raise
            except Exception as exc:
                log.warning("Painel indisponível (%s). Nova tentativa em %.0fs.",
                            exc, INTERVALO_ERRO)
                time.sleep(INTERVALO_ERRO)
                continue

            if not tarefa:
                ociosos += 1
                if ociosos % 20 == 1:
                    log.info("Sem simulações na fila.")
                time.sleep(INTERVALO_OCIOSO)
                continue

            ociosos = 0
            job = _job_da_tarefa(tarefa)
            log.info("→ %s | %s | tentativa %s",
                     job.request_id, job.request.customer_name, job.attempt)

            etapa_atual = {"nome": "consulting"}

            def relatar(etapa: str, _rid=job.request_id) -> None:
                etapa_atual["nome"] = etapa
                painel.stage(_rid, etapa)

            # Sinal de vida durante a simulação. O painel devolve à fila um job
            # cujo agente ficou 90s calado; sem este batimento, uma consulta
            # lenta (mas saudável) seria tomada de nós no meio do caminho.
            parar_batimento = threading.Event()

            def bater(_rid=job.request_id):
                while not parar_batimento.wait(BATIMENTO_SEGUNDOS):
                    painel.stage(_rid, etapa_atual["nome"])

            batimento = threading.Thread(target=bater, daemon=True)
            batimento.start()

            inicio = time.monotonic()
            try:
                resultado = simulador.execute(job, relatar,
                                              timeout=config.job_timeout_seconds)
            except SimulatorUnavailable as exc:
                resultado = SimulationResult(job=job, ok=False, status="Indisponível",
                                             error=str(exc), retryable=False)
            except Exception as exc:
                resultado = SimulationResult(job=job, ok=False, status="Erro",
                                             error=str(exc)[:300], retryable=True)
            finally:
                parar_batimento.set()
                batimento.join(timeout=2)

            decorrido = time.monotonic() - inicio
            marca = "OK " if resultado.ok else "ERRO"
            log.info("← %s | %s | %.1fs | %s", job.request_id, marca, decorrido,
                     resultado.status or resultado.error)

            if not painel.result(job.request_id, resultado):
                log.warning("O painel não aceitou o resultado de %s "
                            "(provavelmente já expirou).", job.request_id)
    except SystemExit as exc:
        log.error("%s", exc)
        return 1
    finally:
        simulador.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
