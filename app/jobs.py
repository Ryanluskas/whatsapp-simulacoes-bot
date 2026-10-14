"""Fila de simulacoes.

Diferencas para a fila anterior, que era um ``queue.Queue`` puro em memoria:

* **Sobrevive a reinicio.** A verdade fica no banco. Ao subir, tudo que ficou
  em ``queued``/``processing`` volta para a fila (ou vira ``interrupted``, se
  as tentativas acabaram). Antes, esses registros ficavam ``queued`` para
  sempre e poluiam a tela da fila indefinidamente.
* **Tentativas de verdade.** O campo ``attempts`` era gravado com o literal
  ``1``; agora conta as tentativas reais, e so' erros marcados como
  recuperaveis sao repetidos.
* **Sem vazamento.** A fila antiga empilhava todo resultado numa
  ``results`` que ninguem consumia.
* **Isolamento.** Cada job carrega o proprio ``request_id``, o chat e o id da
  mensagem de origem, do inicio ao fim. Duas solicitacoes nunca compartilham
  estado, e o despacho e' serializado por simulador.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable

from .actor import ActorTimeout
from .clock import iso_atras, now_iso, parse_iso, to_iso, utc_now
from .db import ENVIO_EM_CURSO, Database
from .events import EventHub
from .models import (
    STAGE_LABELS,
    Delivery,
    IncomingMessage,
    ParsedRequest,
    SimulationJob,
    SimulationResult,
    Stage,
    Status,
)
from .simulator import SimulatorService

RETRY_BACKOFF_SECONDS = 8.0

#: Falha de ambiente (o navegador do simulador nÃ£o abriu) nÃ£o gasta tentativa
#: do consultor, mas tambÃ©m nÃ£o pode virar laÃ§o eterno. Depois disto, a
#: solicitaÃ§Ã£o segue o caminho normal de falha e alguÃ©m Ã© avisado.
MAX_FALHAS_DE_AMBIENTE = 5
#: Espera maior que a das tentativas normais: o operador precisa de tempo para
#: fechar o navegador (ou o que mais esteja segurando o perfil).
ESPERA_DE_AMBIENTE_SEGUNDOS = 30.0

#: Primeira espera antes de reenviar uma resposta que nao saiu.
PRIMEIRO_REENVIO_SEGUNDOS = 30.0


def depois_de(segundos: float) -> str:
    from datetime import timedelta
    return to_iso(utc_now() + timedelta(seconds=segundos))


def estado_final(result_ok: bool) -> tuple[str, str]:
    """(status, stage) de uma solicitacao cuja entrega se resolveu."""
    return ((Status.COMPLETED, Stage.COMPLETED) if result_ok
            else (Status.ERROR, Stage.ERROR))


class QueueService:
    def __init__(
        self,
        db: Database,
        hub: EventHub,
        simulators: list[SimulatorService],
        *,
        max_attempts: int = 2,
        job_timeout: float = 300.0,
        on_result: Callable[[SimulationResult], None] | None = None,
        on_log: Callable[..., None] | None = None,
        alerta_fila: int = 10,
        alerta_espera_s: float = 300.0,
        start_paused: bool = False,
    ) -> None:
        self.db = db
        self.hub = hub
        self.simulators = simulators
        self.max_attempts = max(1, max_attempts)
        self.job_timeout = job_timeout
        # Quando gritar: fila grande e' enxurrada (so' demora); espera longa
        # e' travamento. Ver `_conferir_a_espera`.
        self.alerta_fila = max(1, alerta_fila)
        self.alerta_espera_s = max(30.0, alerta_espera_s)
        self._on_result = on_result
        self._on_log = on_log

        self._pending: "queue.PriorityQueue" = queue.PriorityQueue()
        self._queued_ids: set[int] = set()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._pause_event = threading.Event()
        if not start_paused:
            self._pause_event.set()
        self._lock = threading.RLock()
        self._active: dict[str, dict] = {}
        self._timers: set[threading.Timer] = set()
        # Um aviso por episodio, nao um por ciclo do vigia.
        self._ja_avisei_da_espera = False

    # ------------------------------------------------------------ ciclo de vida
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for index, simulator in enumerate(self.simulators):
            thread = threading.Thread(
                target=self._dispatch_loop,
                args=(simulator,),
                name=f"dispatcher-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

        vigia = threading.Thread(target=self._vigia_loop, name="vigia", daemon=True)
        vigia.start()
        self._threads.append(vigia)

    # De quanto em quanto procurar solicitacoes presas, e a partir de quando
    # considerar presa. A folga sobre o job_timeout evita brigar com uma
    # simulacao que ainda esta' viva.
    _INTERVALO_DO_VIGIA = 60.0
    _FOLGA_SOBRE_O_TIMEOUT = 120.0

    def _vigia_loop(self) -> None:
        """Resolve solicitacoes que ficaram presas em `processing`.

        O `recover()` cobre o reinicio; este cobre o resto. Uma solicitacao
        presa em `processing` com o sistema NO AR fica invisivel para todo
        mundo: nao esta' na fila, nao aparece como erro, e o laco de reenvio
        nao a pega porque ele so' olha `completed` e `error`. O consultor fica
        esperando uma resposta que nunca vem, sem explicacao -- foi o que
        aconteceu com a REQ000037.
        """
        while not self._stop.wait(self._INTERVALO_DO_VIGIA):
            try:
                self._resolver_presas()
                self._resolver_orfaos()
                self._conferir_a_espera()
            except Exception as exc:
                self._log("ERROR", f"Vigia da fila falhou: {exc}")

    
    def _resolver_orfaos(self) -> int:
        """Encontra jobs que estÃ£o QUEUED no banco mas nÃ£o estÃ£o na fila em memÃ³ria."""
        limite = iso_atras(self._INTERVALO_DO_VIGIA)
        presas = self.db.fetchall(
            "SELECT * FROM simulations "
            " WHERE status = ? AND queued_at < ? ORDER BY id LIMIT 10",
            (Status.QUEUED, limite),
        )
        resolvidas = 0
        for linha in presas:
            row = dict(linha)
            sim_id = row["id"]
            if sim_id in self._queued_ids:
                continue
            
            # NÃ£o estÃ¡ em execuÃ§Ã£o nem na fila. OrfÃ£o!
            job = self._job_from_row(row, attempt=int(row.get("attempts") or 0) + 1)
            self._log("WARNING", f"Resgatando {row.get('request_id')} que ficou Ã³rfÃ£o em QUEUED.")
            self.db.update(
                "simulations",
                {
                    "queued_at": now_iso(),
                    "updated_at": now_iso(),
                },
                {"id": sim_id},
            )
            self._queued_ids.add(job.simulation_id)
            self._pending.put((job.queue_position, job.simulation_id, job))
            resolvidas += 1
        return resolvidas

    def _resolver_presas(self) -> int:
        limite = iso_atras(self.job_timeout + self._FOLGA_SOBRE_O_TIMEOUT)
        # A foto do que esta' RODANDO vem ANTES da consulta. Na ordem inversa,
        # um job que terminava entre as duas leituras aparecia como "preso"
        # com uma linha ja' velha, e a entrega concluida era reavaliada em
        # cima de dados antigos. Nesta ordem, quem roda ja' estava ativo na
        # foto, e quem terminou ja' nao esta' `processing` na consulta.
        with self._lock:
            rodando = set(self._active)
        presas = self.db.fetchall(
            "SELECT * FROM simulations "
            " WHERE status = ? "
            "   AND COALESCE(started_at, queued_at, created_at) < ? "
            " ORDER BY id LIMIT 10",
            (Status.PROCESSING, limite),
        )
        resolvidas = 0
        for linha in presas:
            row = dict(linha)
            if row.get("request_id") in rodando:
                continue   # ainda em execucao de verdade
            if row.get("result_ok") is not None:
                # A simulacao TERMINOU; o que travou foi a entrega. Dizer ao
                # consultor "a simulacao travou" seria mentira, e o resultado
                # existe: vai para o reenvio.
                self._resolver_entrega_interrompida(row, "a entrega travou e nÃ£o retornou")
                resolvidas += 1
                continue
            self._log(
                "WARNING",
                f"{row.get('request_id')}: presa em processamento hÃ¡ mais de "
                f"{int(self.job_timeout + self._FOLGA_SOBRE_O_TIMEOUT)}s. "
                "Encerrando para o consultor nÃ£o ficar sem resposta.",
            )
            self._finalize_interrupted(
                row, "a simulaÃ§Ã£o travou e nÃ£o retornou")
            resolvidas += 1
        return resolvidas

    def pendencias(self) -> dict:
        """Quantas solicitacoes esperam, e ha' quanto tempo a mais antiga.

        Existe porque "o bot caiu e ninguem percebeu" e' o pior modo de falha
        deste sistema: em 15:20 do dia 01/09 entraram cinquenta pedidos, o bot
        respondeu UM e o WhatsApp caiu. Quarenta e nove consultores ficaram
        esperando, e nada na tela dizia isso -- a fila estava correta, o banco
        estava correto, e o painel nao mostrava numero nenhum.

        Conta `queued` E `processing`: uma presa em processamento espera
        igual, e foi assim que a REQ000037 ficou invisivel.
        """
        # Resultado pronto e nao entregue tambem e' consultor esperando.
        linha = self.db.fetchone(
            "SELECT COUNT(*) AS quantas, MIN(COALESCE(queued_at, created_at)) AS mais_antiga "
            "  FROM simulations WHERE status IN (?, ?) OR delivery_status = ?",
            (Status.QUEUED, Status.PROCESSING, Delivery.RETRYING),
        ) or {}
        quantas = int(linha.get("quantas") or 0)
        mais_antiga = linha.get("mais_antiga")

        espera = 0.0
        if mais_antiga:
            inicio = parse_iso(str(mais_antiga))
            if inicio:
                espera = max(0.0, (utc_now() - inicio).total_seconds())

        # Entrega incerta nao e' fila -- ninguem vai tentar de novo sozinho --
        # mas e' consultor que pode estar sem resposta. Conta as das ultimas
        # 24 h, que e' o que ainda vale conferir no grupo.
        incertas = int(self.db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE delivery_status=? AND updated_at >= ?",
            (Delivery.UNCONFIRMED, iso_atras(24 * 3600))) or 0)

        return {
            "pendentes": quantas,
            "entregas_incertas": incertas,
            "espera_maxima_s": round(espera),
            "desde": mais_antiga or "",
            "fila_cheia": quantas >= self.alerta_fila,
            "espera_longa": espera >= self.alerta_espera_s,
        }

    def _conferir_a_espera(self) -> None:
        """Grita quando a fila cresce demais ou alguem espera demais.

        Dois limiares, porque sao dois problemas: fila grande e' enxurrada
        (normal, so' demora), e espera longa e' travamento (nao e' normal).
        Um WARNING por ciclo, nao um por solicitacao -- cinquenta linhas
        iguais no log escondem em vez de avisar.
        """
        estado = self.pendencias()
        if not (estado["fila_cheia"] or estado["espera_longa"]):
            self._ja_avisei_da_espera = False
            return
        if self._ja_avisei_da_espera:
            return
        self._ja_avisei_da_espera = True

        minutos = estado["espera_maxima_s"] / 60
        self._log(
            "WARNING",
            f"{estado['pendentes']} solicitaÃ§Ã£o(Ãµes) esperando; a mais antiga "
            f"hÃ¡ {minutos:.0f} min. "
            + ("A fila estÃ¡ maior que o normal. "
               if estado["fila_cheia"] else "")
            + ("AlguÃ©m pode estar sem resposta hÃ¡ tempo demais."
               if estado["espera_longa"] else ""),
        )
        self.hub.publish("queue_backlog", estado, title="Fila acumulando")

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._pause_event.set()
        # Cancela as reenfileiragens agendadas: sem isso um timer pendente
        # acordaria depois do desligamento e empurraria trabalho numa fila morta.
        with self._lock:
            timers, self._timers = list(self._timers), set()
        for timer in timers:
            timer.cancel()
        for _ in self._threads:
            self._pending.put((float("inf"), -1, None))  # shutdown
        for thread in self._threads:
            thread.join(timeout=timeout / max(1, len(self._threads)))
        self._threads.clear()

    def _log(self, level: str, message: str, **extra) -> None:
        if self._on_log:
            try:
                self._on_log(level, "fila", message, **extra)
            except Exception:
                pass

    # ------------------------------------------------------------------ estado
    @property
    def active(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._active.items()}

    def depth(self) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM simulations WHERE status IN (?,?)",
                (Status.QUEUED, Status.PROCESSING),
            )
        )

    def position_of(self, request_id: str) -> int:
        row = self.db.fetchone(
            "SELECT created_at FROM simulations WHERE request_id=?", (request_id,)
        )
        if not row:
            return 0
        ahead = self.db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE status IN (?,?) AND created_at < ?",
            (Status.QUEUED, Status.PROCESSING, row["created_at"]),
        )
        return int(ahead) + 1

    def snapshot(self, limit: int = 100) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT id, request_id, consultant_name, cpf, bank, contract, customer_name, "
            "       status, stage, attempts, max_attempts, error_message, "
            "       delivery_status, reply_attempts, delivery_error, "
            "       created_at, queued_at, started_at, finished_at, processing_seconds "
            "FROM simulations WHERE status IN (?,?,?) OR delivery_status = ? "
            "ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END, queue_position ASC, created_at ASC LIMIT ?",
            (Status.QUEUED, Status.PROCESSING, Status.INTERRUPTED, Delivery.RETRYING,
             Status.PROCESSING, limit),
        )
        active = self.active
        items = []
        for position, row in enumerate(rows, start=1):
            live = active.get(row["request_id"], {})
            items.append(
                {
                    **row,
                    "position": position,
                    "stage_label": STAGE_LABELS.get(row["stage"], row["stage"]),
                    "elapsed_seconds": (
                        round(time.monotonic() - live["started_monotonic"], 1)
                        if live.get("started_monotonic")
                        else None
                    ),
                }
            )
        return items

    # -------------------------------------------------------------- enfileirar
    def submit(self, job: SimulationJob) -> int:
        position = self.position_of(job.request_id)
        self.db.update(
            "simulations",
            {
                "status": Status.QUEUED,
                "stage": Stage.QUEUED,
                "queued_at": now_iso(),
                "updated_at": now_iso(),
            },
            {"id": job.simulation_id},
        )
        self._queued_ids.add(job.simulation_id)
        self._pending.put((job.queue_position, job.simulation_id, job))
        self.hub.publish(
            "job_queued",
            {
                "request_id": job.request_id,
                "simulation_id": job.simulation_id,
                "consultant": job.request.consultant_name,
                "bank": job.request.bank,
                "contract": job.request.contract,
                "position": position,
                "attempt": job.attempt,
            },
            stage=Stage.QUEUED,
            title="Na fila",
            detail=f"{job.request.consultant_name} Â· posiÃ§Ã£o {position}",
            request_id=job.request_id,
            simulation_id=job.simulation_id,
            consultant_name=job.request.consultant_name,
            chat_id=job.message.chat_id,
        )
        return position

    # --------------------------------------------------------------- recuperar
    def recover(self) -> int:
        """Devolve a fila ao estado correto depois de um reinicio."""
        is_paused = self.db.scalar("SELECT value FROM meta WHERE key='bot_paused'")
        if is_paused == "1":
            self._pause_event.clear()
        else:
            self._pause_event.set()
        rows = self.db.fetchall(
            "SELECT * FROM simulations WHERE status IN (?,?) ORDER BY queue_position ASC, created_at ASC",
            (Status.QUEUED, Status.PROCESSING),
        )
        recovered = 0
        for row in rows:
            if row.get("result_ok") is not None:
                # Caiu no meio da ENTREGA: o portal ja' respondeu. Simular de
                # novo gastaria o Santander e poderia mandar dois resultados;
                # o que falta e' so' entregar este.
                self._resolver_entrega_interrompida(
                    row, "reinÃ­cio do sistema durante a entrega", imediato=True)
                continue
            attempts = int(row.get("attempts") or 0)
            limit = int(row.get("max_attempts") or self.max_attempts)
            if attempts >= limit:
                self._finalize_interrupted(row, "reinÃ­cio do sistema durante o processamento")
                continue
            job = self._job_from_row(row, attempt=attempts + 1)
            self.db.update(
                "simulations",
                {
                    "status": Status.QUEUED,
                    "stage": Stage.QUEUED,
                    "queued_at": now_iso(),
                    "updated_at": now_iso(),
                },
                {"id": row["id"]},
            )
            self._queued_ids.add(job.simulation_id)
            self._pending.put((job.queue_position, job.simulation_id, job))
            recovered += 1
        self._retomar_reenvios_interrompidos()
        if recovered:
            self._log("WARNING", f"{recovered} solicitaÃ§Ã£o(Ãµes) retomadas apÃ³s reinÃ­cio.")
            self.hub.publish(
                "queue_recovered",
                {"count": recovered},
                level="warning",
                title="Fila retomada",
                detail=f"{recovered} solicitaÃ§Ã£o(Ãµes) voltaram para a fila apÃ³s reinÃ­cio.",
            )
        return recovered

    def _finalize_interrupted(self, row: dict, reason: str) -> None:
        self.db.update(
            "simulations",
            {
                "status": Status.INTERRUPTED,
                "stage": Stage.INTERRUPTED,
                "error_message": reason,
                "finished_at": now_iso(),
                "updated_at": now_iso(),
            },
            {"id": row["id"]},
        )
        self.hub.publish(
            "job_interrupted",
            {"request_id": row["request_id"], "simulation_id": row["id"], "reason": reason},
            stage=Stage.INTERRUPTED,
            level="warning",
            title="Interrompido",
            detail=reason,
            request_id=row["request_id"] or "",
            simulation_id=row["id"],
            consultant_name=row.get("consultant_name") or "",
        )

    def _retomar_reenvios_interrompidos(self) -> int:
        """Reenvio que estava `pending` quando o processo caiu.

        O laco de reenvio marca a linha como `pending` antes de enviar (para
        duas varreduras nao mandarem a mesma resposta). Se o processo cai
        nesse meio, a linha ja' tem status final e ninguem mais a pegaria.
        Status e etapa ficam como estao -- uma interrompida continua
        interrompida; so' a ENTREGA e' decidida, pelo que ficou gravado.
        """
        linhas = self.db.fetchall(
            "SELECT * FROM simulations WHERE delivery_status=? AND status NOT IN (?, ?) "
            "   AND replied_at IS NULL",
            (Delivery.PENDING, Status.QUEUED, Status.PROCESSING),
        )
        for linha in linhas:
            self._resolver_entrega_interrompida(dict(linha), "reinÃ­cio durante o reenvio",
                                                imediato=True, manter_status=True)
        if linhas:
            self._log("WARNING", f"{len(linhas)} reenvio(s) interrompido(s) pelo reinÃ­cio "
                                 "foram reavaliados.")
        return len(linhas)

    def situacao_do_envio(self, simulation_id: int) -> tuple[str, dict | None]:
        """O que as linhas de SAIDA desta solicitacao provam.

        * ``("entregue", linha)`` -- alguma saida terminou como entregue;
        * ``("incerta", None)``   -- ha' saida ``sending`` (a chamada comecou e
          nao terminou) ou ``unconfirmed`` (a API respondeu sem provar: 500,
          timeout de leitura, 2xx sem id). Pode ter saido;
        * ``("nada", None)``      -- nenhuma tentativa que possa ter saido.

        ``unconfirmed`` conta como incerta mesmo quando a SIMULACAO ainda nao
        diz isso. A saida e a simulacao sao gravadas em momentos diferentes:
        uma queda (ou um "database is locked") entre as duas deixava a saida
        ``unconfirmed`` com a simulacao ``pending`` -- e a recuperacao lia
        "nada saiu" e reenviava. Era uma segunda mensagem no grupo.

        So' uma pessoa tira uma saida incerta dessa conta: "nao chegou", no
        painel, a marca como ``failed``.

        E' a unica fonte da decisao de reenviar depois de uma interrupcao.
        Adivinhar "provavelmente nao saiu" e' como se manda a resposta duas
        vezes.
        """
        entregue = self.db.fetchone(
            "SELECT id, wa_message_id, quote_status FROM messages "
            " WHERE simulation_id=? AND direction='out' "
            "   AND COALESCE(status,'') NOT IN ('failed', ?, ?) "
            " ORDER BY id DESC LIMIT 1",
            (simulation_id, Delivery.UNCONFIRMED, ENVIO_EM_CURSO),
        )
        if entregue:
            return "entregue", dict(entregue)
        em_curso = self.db.scalar(
            "SELECT COUNT(*) FROM messages WHERE simulation_id=? AND direction='out' "
            "   AND status IN (?, ?)", (simulation_id, ENVIO_EM_CURSO, Delivery.UNCONFIRMED))
        return ("incerta", None) if em_curso else ("nada", None)

    def _resolver_entrega_interrompida(self, row: dict, motivo: str, *,
                                       imediato: bool = False,
                                       manter_status: bool = False) -> str:
        """A entrega parou no meio. Decide pelo que esta' gravado, nunca por palpite.

        entregue -> fecha como entregue (sem reenviar); incerta -> ``unconfirmed``
        (sem reenviar: pode ter chegado); nada -> reenvio.
        """
        situacao, linha = self.situacao_do_envio(row["id"])
        agora = now_iso()
        status, stage = estado_final(bool(row.get("result_ok")))
        request_id = row.get("request_id") or ""
        if situacao == "entregue":
            campos = {"delivery_status": Delivery.DELIVERED, "replied_at": agora,
                      "sent_message_id": (linha or {}).get("wa_message_id") or "",
                      "delivery_error": "", "updated_at": agora}
            if not manter_status:
                campos.update(status=status, stage=stage)
            self.db.update("simulations", campos, {"id": row["id"]})
            self._log("WARNING",
                      f"{request_id}: {motivo}, mas a resposta JÃ tinha saÃ­do "
                      f"(id {campos['sent_message_id'] or 'sem id'}). Marquei como "
                      "entregue; nada foi reenviado.", request_id=request_id)
            return situacao
        if situacao == "incerta":
            erro = f"{motivo}; o envio tinha comeÃ§ado e a mensagem pode ter saÃ­do"
            campos = {"delivery_status": Delivery.UNCONFIRMED, "sent_message_id": "",
                      "delivery_error": erro, "updated_at": agora}
            if not manter_status:
                campos.update(status=status, stage=Stage.DELIVERY_UNCONFIRMED)
            # Saida e simulacao na MESMA transacao: gravadas separadas, uma
            # queda entre as duas e' justamente o estado que esta funcao
            # existe para resolver.
            with self.db.write() as conn:
                conn.execute(
                    "UPDATE messages SET status=?, desfecho='incerta', "
                    "       error=COALESCE(NULLIF(error,''), ?) "
                    " WHERE simulation_id=? AND direction='out' AND status=?",
                    (Delivery.UNCONFIRMED, f"interrompido durante o envio ({motivo})",
                     row["id"], ENVIO_EM_CURSO))
                conn.execute(
                    f"UPDATE simulations SET {', '.join(f'{c}=?' for c in campos)} WHERE id=?",
                    (*campos.values(), row["id"]))
            self._log("WARNING",
                      f"{request_id}: Entrega incerta â€” verificar WhatsApp. {erro}. "
                      "NÃ£o reenvio sozinho para nÃ£o duplicar.", request_id=request_id)
            self.hub.publish(
                "delivery_unconfirmed",
                {"request_id": request_id, "simulation_id": row["id"],
                 "origin_message_id": row.get("source_message_id") or "", "reason": erro},
                stage=Stage.DELIVERY_UNCONFIRMED, level="warning",
                title="Entrega incerta â€” verificar WhatsApp", detail=erro,
                request_id=request_id, simulation_id=row["id"],
                consultant_name=row.get("consultant_name") or "",
                chat_id=row.get("chat_id") or "")
            return situacao
        self._agendar_reenvio(row, motivo, imediato=imediato, manter_status=manter_status)
        return situacao

    def _agendar_reenvio(self, row: dict, motivo: str, imediato: bool = False,
                         manter_status: bool = False) -> None:
        """Resultado pronto e NADA saiu: entrega na mao do laco de reenvio."""
        status, _stage = estado_final(bool(row.get("result_ok")))
        agora = now_iso()
        campos = {
            "delivery_status": Delivery.RETRYING,
            "delivery_error": motivo,
            "next_delivery_at": agora if imediato else depois_de(PRIMEIRO_REENVIO_SEGUNDOS),
            "updated_at": agora,
        }
        if not manter_status:
            campos.update(status=status, stage=Stage.DELIVERY_RETRY)
        self.db.update("simulations", campos, {"id": row["id"]})
        self._log("WARNING",
                  f"{row.get('request_id')}: resultado pronto e nÃ£o entregue "
                  f"({motivo}); nenhum envio tinha comeÃ§ado. Vai para o reenvio, sem "
                  "simular de novo.",
                  request_id=row.get("request_id") or "")
        self.hub.publish(
            "delivery_retry",
            {"request_id": row.get("request_id"), "simulation_id": row["id"],
             "reason": motivo},
            stage=Stage.DELIVERY_RETRY, level="warning", title="Reenvio pendente",
            detail=motivo, request_id=row.get("request_id") or "",
            simulation_id=row["id"], consultant_name=row.get("consultant_name") or "",
            chat_id=row.get("chat_id") or "",
        )

    @staticmethod
    def _job_from_row(row: dict, attempt: int) -> SimulationJob:
        request = ParsedRequest(
            consultant_name=row.get("consultant_name") or "Consultor",
            cpf=row.get("cpf") or "",
            bank=row.get("bank") or "",
            contract=row.get("contract") or "",
            simulation_type=row.get("simulation_type") or "consignado",
            customer_name=row.get("customer_name") or "Lead",
            origin=row.get("origin") or "",
            phone=row.get("phone") or "",
        )
        # A origem vem INTEIRA do banco: id, chat, autor e o texto citado.
        # Nada aqui e' reconstruido a partir do que esta' na tela.
        message = IncomingMessage(
            message_id=row.get("source_message_id") or "",
            chat_id=row.get("chat_id") or "",
            chat_name=row.get("chat_name") or "",
            sender_id=row.get("sender_id") or "",
            sender_name=row.get("sender_name") or "",
            text=row.get("raw_message") or "",
            timestamp=row.get("source_timestamp") or "",
            participant=row.get("participant") or "",
        )
        return SimulationJob(
            request=request,
            message=message,
            request_id=row["request_id"],
            simulation_id=row["id"],
            consultant_id=row.get("consultant_id"),
            attempt=attempt,
            queue_position=float(row.get("queue_position") or 0.0),
        )

    # ----------------------------------------------------------------- despacho

    def is_paused(self) -> bool:
        return not self._pause_event.is_set()
        
    def pause(self) -> None:
        self._pause_event.clear()
        self.db.execute("INSERT INTO meta (key, value) VALUES ('bot_paused', '1') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        self.hub.publish("queue_paused", {"paused": True})

    def resume(self) -> None:
        self._pause_event.set()
        self.db.execute("INSERT INTO meta (key, value) VALUES ('bot_paused', '0') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        self.hub.publish("queue_resumed", {"paused": False})

    def _dispatch_loop(self, simulator: SimulatorService) -> None:
        while not self._stop.is_set():
            # Aguarda se estiver pausado (timeout curto para poder reagir ao stop)
            self._pause_event.wait(timeout=1.0)
            if self._stop.is_set():
                break
                
            if not self._pause_event.is_set():
                continue

            try:
                item = self._pending.get(timeout=1.0)
            except queue.Empty:
                continue
                
            q_pos, sim_id, job = item
            if job is None:
                return
                
            # VerificaÃ§ao de seguranÃ§a: o job ainda estÃ¡ na fila e a posiÃ§ao nÃ£o mudou?
            row = self.db.fetchone("SELECT status, queue_position FROM simulations WHERE id=?", (sim_id,))
            if not row or row["status"] != "queued" or abs(float(row["queue_position"] or 0.0) - q_pos) > 0.001:
                # Job foi excluido, processado, ou reordenado (o que injetou uma nova copia na fila)
                continue
                
            self._queued_ids.discard(job.simulation_id)
                
            # Se pausaram logo depois de pegarmos o job
            if not self._pause_event.is_set():
                self._queued_ids.add(job.simulation_id)
                self._pending.put((job.queue_position, job.simulation_id, job))
                continue
                
            try:
                self._process(job, simulator)
            except Exception as exc:  # nunca deixar a thread morrer
                self._log(
                    "ERROR",
                    f"Falha inesperada ao processar {job.request_id}: {exc}",
                    request_id=job.request_id,
                )

    def _process(self, job: SimulationJob, simulator: SimulatorService) -> None:
        started_iso = now_iso()
        started_monotonic = time.monotonic()

        with self._lock:
            self._active[job.request_id] = {
                "consultant": job.request.consultant_name,
                "simulation_id": job.simulation_id,
                "stage": Stage.PROCESSING,
                "attempt": job.attempt,
                "started_monotonic": started_monotonic,
                "started_at": started_iso,
                "simulator": simulator.name,
            }

        self.db.update(
            "simulations",
            {
                "status": Status.PROCESSING,
                "stage": Stage.PROCESSING,
                "started_at": started_iso,
                "attempts": job.attempt,
                "updated_at": started_iso,
            },
            {"id": job.simulation_id},
        )
        self._emit_stage(job, Stage.PROCESSING)

        def on_stage(stage: str) -> None:
            self.atualizar_etapa(job, stage)

        try:
            result = simulator.execute(job, on_stage, timeout=self.job_timeout)
        except ActorTimeout:
            result = SimulationResult(
                job=job,
                ok=False,
                status="Timeout",
                error=f"a simulaÃ§Ã£o passou de {int(self.job_timeout)}s sem responder",
                retryable=True,
            )
            self._log(
                "ERROR",
                f"{job.request_id}: tempo limite de {int(self.job_timeout)}s excedido.",
                request_id=job.request_id,
            )
        except Exception as exc:
            result = SimulationResult(
                job=job, ok=False, status="Erro", error=str(exc)[:240], retryable=False
            )

        elapsed = round(time.monotonic() - started_monotonic, 1)

        # AMBIENTE antes de tentativa: o navegador que nÃ£o abre nÃ£o Ã© defeito
        # do pedido. Gastar as duas tentativas do consultor nisso fazia a
        # solicitaÃ§Ã£o dele morrer por um problema que nÃ£o era dele -- e sem
        # ninguÃ©m ser avisado do que resolver.
        if (not result.ok and getattr(result, "ambiental", False)
                and job.ambientais < MAX_FALHAS_DE_AMBIENTE):
            with self._lock:
                self._active.pop(job.request_id, None)
            self._reenfileirar_por_ambiente(job, result)
            return

        if not result.ok and result.retryable and job.attempt < self.max_attempts:
            with self._lock:
                self._active.pop(job.request_id, None)
            self._retry(job, result, elapsed)
            return

        try:
            self._finish(job, result, elapsed)
        finally:
            # So' sai de "ativo" depois da ENTREGA: enquanto a resposta sobe,
            # o vigia nao pode tomar esta solicitacao por presa.
            with self._lock:
                self._active.pop(job.request_id, None)

    def atualizar_etapa(self, job: SimulationJob, stage: str) -> None:
        """Grava e publica a etapa atual. Usado pelo simulador e pela entrega."""
        with self._lock:
            if job.request_id in self._active:
                self._active[job.request_id]["stage"] = stage
        self.db.update(
            "simulations",
            {"stage": stage, "updated_at": now_iso()},
            {"id": job.simulation_id},
        )
        self._emit_stage(job, stage)

    def _reenfileirar_por_ambiente(self, job: SimulationJob,
                                   result: SimulationResult) -> None:
        """Devolve a solicitaÃ§Ã£o Ã  fila SEM gastar tentativa.

        A `attempt` continua a mesma: ela conta o que foi tentado no portal.
        O que falhou aqui foi a mÃ¡quina -- navegador que nÃ£o abre, perfil em
        uso, Playwright que nÃ£o sobe. O consultor nÃ£o tem nada a ver com isso,
        e o pedido dele nÃ£o pode morrer por causa disso.
        """
        vez = job.ambientais + 1
        self.db.update(
            "simulations",
            {"status": Status.QUEUED, "stage": Stage.QUEUED,
             "error_message": result.error, "updated_at": now_iso()},
            {"id": job.simulation_id},
        )
        self._log(
            "WARNING",
            f"{job.request_id}: {result.error}. A solicitaÃ§Ã£o volta para a fila SEM "
            f"gastar tentativa ({vez} de {MAX_FALHAS_DE_AMBIENTE}); a tentativa "
            f"{job.attempt} continua valendo. Se o navegador do simulador estiver "
            "aberto em outro lugar com o mesmo perfil, feche-o.",
            request_id=job.request_id, consultant=job.request.consultant_name,
        )
        self.hub.publish(
            "job_ambiente",
            {"request_id": job.request_id, "simulation_id": job.simulation_id,
             "attempt": job.attempt, "ambientais": vez, "error": result.error},
            stage=Stage.QUEUED, level="warning",
            title="Simulador indisponÃ­vel â€” na fila",
            detail=f"{result.error} ({vez}/{MAX_FALHAS_DE_AMBIENTE})",
            request_id=job.request_id, simulation_id=job.simulation_id,
            consultant_name=job.request.consultant_name,
        )
        de_novo = SimulationJob(
            request=job.request, message=job.message, request_id=job.request_id,
            simulation_id=job.simulation_id, consultant_id=job.consultant_id,
            attempt=job.attempt, ambientais=vez, queue_position=job.queue_position,
        )

        def reenfileirar() -> None:
            with self._lock:
                self._timers.discard(atraso)
            self._queued_ids.add(de_novo.simulation_id)
            self._pending.put((de_novo.queue_position, de_novo.simulation_id, de_novo))

        atraso = threading.Timer(ESPERA_DE_AMBIENTE_SEGUNDOS, reenfileirar)
        atraso.daemon = True
        with self._lock:
            self._timers.add(atraso)
        atraso.start()

    def _retry(self, job: SimulationJob, result: SimulationResult, elapsed: float) -> None:
        self.db.update(
            "simulations",
            {
                "status": Status.QUEUED,
                "stage": Stage.QUEUED,
                "error_message": result.error,
                "updated_at": now_iso(),
            },
            {"id": job.simulation_id},
        )
        self.hub.publish(
            "job_retry",
            {
                "request_id": job.request_id,
                "simulation_id": job.simulation_id,
                "attempt": job.attempt,
                "max_attempts": self.max_attempts,
                "error": result.error,
                "elapsed_seconds": elapsed,
            },
            stage=Stage.QUEUED,
            level="warning",
            title="Nova tentativa",
            detail=f"tentativa {job.attempt}/{self.max_attempts} falhou: {result.error}",
            request_id=job.request_id,
            simulation_id=job.simulation_id,
            consultant_name=job.request.consultant_name,
        )
        self._log(
            "WARNING",
            f"{job.request_id}: tentativa {job.attempt} falhou ({result.error}). Reenfileirando.",
            request_id=job.request_id,
            consultant=job.request.consultant_name,
        )
        retry_job = SimulationJob(
            request=job.request,
            message=job.message,
            request_id=job.request_id,
            simulation_id=job.simulation_id,
            consultant_id=job.consultant_id,
            attempt=job.attempt + 1,
            ambientais=job.ambientais,
            queue_position=job.queue_position,
        )
        # A espera acontece num timer, NÃƒO nesta thread. Antes era um
        # time.sleep() no despachante: com um worker, a fila inteira congelava
        # por 8s a cada falha recuperÃ¡vel e outros consultores esperavam Ã  toa.
        def reenfileirar() -> None:
            # Sai do conjunto ao disparar: antes cada nova tentativa deixava o
            # Timer (com o job dentro) guardado ate' o processo parar.
            with self._lock:
                self._timers.discard(atraso)
            self._queued_ids.add(retry_job.simulation_id)
            self._pending.put((retry_job.queue_position, retry_job.simulation_id, retry_job))

        atraso = threading.Timer(RETRY_BACKOFF_SECONDS, reenfileirar)
        atraso.daemon = True
        with self._lock:
            self._timers.add(atraso)
        atraso.start()

    def _finish(self, job: SimulationJob, result: SimulationResult, elapsed: float) -> None:
        import json

        finished = now_iso()
        entrega_pendente = self._on_result is not None
        if result.ok:
            payload = {
                "status": Status.COMPLETED,
                "stage": Stage.COMPLETED,
                "result_ok": 1,
                "error_message": "",
                "refin": result.status,
                "reduction_value": result.reduction_value,
                "margin": result.margin,
                "installment_sum": result.installment_sum,
                "installment_count": result.installment_count,
                "debt_sum": result.debt_sum,
                "contracts_count": len(result.contracts),
                "contracts_json": json.dumps(list(result.contracts), ensure_ascii=False, default=str),
                # O que o portal disse, literal. Guardado para o painel e
                # para o reenvio poderem repetir o MOTIVO em vez da frase
                # generica -- o consultor age diferente conforme ele.
                "motivos_portal": json.dumps(list(result.motivos or ()),
                                             ensure_ascii=False, default=str),
                "finished_at": finished,
                "processing_seconds": elapsed,
                "updated_at": finished,
            }
        else:
            payload = {
                "status": Status.ERROR,
                "stage": Stage.ERROR,
                "result_ok": 0,
                "error_message": result.error or result.status,
                "finished_at": finished,
                "processing_seconds": elapsed,
                "updated_at": finished,
            }
        if entrega_pendente:
            # NAO e' concluida ainda: o resultado existe, mas o consultor nao
            # o recebeu. Gravar `completed` aqui era o que fazia o reenvio
            # atropelar a entrega em curso e o painel mentir.
            payload.update({
                "status": Status.PROCESSING,
                "stage": Stage.REPLYING,
                "delivery_status": Delivery.PENDING,
            })
        self.db.update("simulations", payload, {"id": job.simulation_id})
        with self._lock:
            if job.request_id in self._active:
                self._active[job.request_id]["stage"] = Stage.REPLYING

        # A simulacao acabou; a solicitacao so' acaba com a entrega. O evento
        # de fim de simulacao nao leva a etapa `completed` quando ainda ha'
        # resposta por enviar -- a timeline mostraria "concluido" antes dela.
        etapa_ok, etapa_erro = ("", "") if entrega_pendente else (Stage.COMPLETED, Stage.ERROR)

        common = {
            "request_id": job.request_id,
            "simulation_id": job.simulation_id,
            "consultant": job.request.consultant_name,
            "elapsed_seconds": elapsed,
            "attempt": job.attempt,
        }
        if result.ok:
            self.hub.publish(
                "job_done",
                {
                    **common,
                    "refin": result.status,
                    "reduction_value": result.reduction_value,
                    "margin": result.margin,
                    "contracts": len(result.contracts),
                },
                stage=etapa_ok,
                level="success",
                title="SimulaÃ§Ã£o concluÃ­da",
                detail=f"{job.request.consultant_name} Â· refin: {result.status} Â· {elapsed}s",
                request_id=job.request_id,
                simulation_id=job.simulation_id,
                consultant_name=job.request.consultant_name,
                chat_id=job.message.chat_id,
            )
        else:
            self.hub.publish(
                "job_error",
                {**common, "error": result.error},
                stage=etapa_erro,
                level="error",
                title="Erro na simulaÃ§Ã£o",
                detail=f"{job.request.consultant_name} Â· {result.error}",
                request_id=job.request_id,
                simulation_id=job.simulation_id,
                consultant_name=job.request.consultant_name,
                chat_id=job.message.chat_id,
            )

        if not entrega_pendente:
            return

        falha = ""
        try:
            self._on_result(result)
        except Exception as exc:
            falha = str(exc)[:240] or exc.__class__.__name__
            self._log(
                "ERROR",
                f"{job.request_id}: falha ao entregar o resultado: {falha}",
                request_id=job.request_id,
            )
        self._garantir_desfecho_da_entrega(job, result, falha)

    def _garantir_desfecho_da_entrega(self, job: SimulationJob,
                                      result: SimulationResult, falha: str) -> None:
        """Nenhuma solicitacao pode ficar `pending` depois que a entrega voltou.

        Quem entrega (o manager) grava o desfecho com a evidencia. Se ele
        levantou excecao, ou se o callback nao grava nada (dubles, integracoes
        antigas), a linha ficaria em processamento para sempre -- invisivel
        para o reenvio. Aqui ela ganha um desfecho coerente.
        """
        linha = self.db.fetchone(
            "SELECT id, request_id, consultant_name, chat_id, result_ok, delivery_status "
            "  FROM simulations WHERE id=?", (job.simulation_id,))
        if not linha or linha.get("delivery_status") != Delivery.PENDING:
            return
        if falha:
            self._resolver_entrega_interrompida(dict(linha), f"falha ao entregar: {falha}")
            return
        status, stage = estado_final(result.ok)
        self.db.update(
            "simulations",
            {"status": status, "stage": stage, "delivery_status": "",
             "updated_at": now_iso()},
            {"id": job.simulation_id},
        )

    def _emit_stage(self, job: SimulationJob, stage: str) -> None:
        self.hub.publish(
            "job_progress",
            {
                "request_id": job.request_id,
                "simulation_id": job.simulation_id,
                "consultant": job.request.consultant_name,
                "stage": stage,
                "label": STAGE_LABELS.get(stage, stage),
                "attempt": job.attempt,
            },
            stage=stage,
            title=STAGE_LABELS.get(stage, stage),
            detail=job.request.consultant_name,
            request_id=job.request_id,
            simulation_id=job.simulation_id,
            consultant_name=job.request.consultant_name,
            chat_id=job.message.chat_id,
        )

    def reinject_job(self, sim_id: int) -> None:
        """Chamado pelo orquestrador apos reordenar a fila."""
        row = self.db.fetchone("SELECT * FROM simulations WHERE id=?", (sim_id,))
        if not row or row["status"] != "queued":
            return
            
        from .parser import parse_request
        parsed, missing = parse_request(
            row["raw_message"] or "",
            fallback_consultant=row["consultant_name"] or "",
            require_trigger=False,
            default_bank=row["bank"],
        )
        if not parsed:
            return
            
        from dataclasses import replace
        parsed = replace(parsed, cpf=row["cpf"])
            
        from .manager import IncomingMessage
        message = IncomingMessage(
            message_id=row["source_message_id"] or "",
            chat_id=row["chat_id"] or "",
            chat_name=row["chat_name"] or "",
            sender_id=row["sender_id"] or "",
            sender_name=row["sender_name"] or "",
            text=row["raw_message"] or "",
            timestamp=row.get("source_timestamp") or "",
            participant=row.get("participant") or "",
        )
        job = SimulationJob(
            request=parsed,
            message=message,
            request_id=row["request_id"],
            simulation_id=row["id"],
            consultant_id=row.get("consultant_id"),
            attempt=row["attempts"] + 1,
            queue_position=float(row.get("queue_position") or 0.0),
        )
        self._pending.put((job.queue_position, job.simulation_id, job))
        
        self.hub.publish(
            "queue_update",
            {"items": self.snapshot(), "depth": self.depth(), **self.pendencias()},
            persist=False,
        )


