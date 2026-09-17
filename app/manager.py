"""Orquestracao: liga WhatsApp, fila, simulador, banco e painel.

Fluxo de uma solicitacao, ponta a ponta:

    mensagem recebida -> consultor identificado -> dados validados
    -> solicitacao criada -> fila -> processando -> consultando
    -> resultado -> resposta enviada -> historico

Cada uma dessas etapas grava em ``simulations.stage`` e publica um evento, que
alimenta ao mesmo tempo o monitor ao vivo e a timeline do historico. Como tudo
sai da mesma fonte, painel e banco nunca discordam.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, NamedTuple

from . import analytics, mensagens
from .cards import build_result_html
from .clock import iso_atras, now_iso, parse_iso, utc_now
from .config import ROOT, Config
from .consultants import ConsultantRepository
from .db import (ENTRADA_EXPIRADA, ENTRADA_IGNORADA, ENTRADA_RECEBIDA,
                 ENTRADA_RECUSADA, ENTRADA_SOLICITACAO, ENVIO_EM_CURSO, Database)
from .events import EventHub
from .jobs import QueueService, depois_de, estado_final
from .models import (
    ParsedRequest,
    STAGE_LABELS,
    Delivery,
    Desfecho,
    Entrega,
    IncomingMessage,
    QuoteStatus,
    SimulationJob,
    SimulationResult,
    Stage,
    Status,
)
from .parser import parse_request
from .remote import RemoteSimulator
from .renderer import PngInvalido, validar_png
from .security import redact
from .simulator import SimulatorService
from .state_store import StateStore
from .evolution import EvolutionClient
from .whatsapp import WhatsAppService, WhatsAppStatus
from .whatsapp_port import MODO_EVOLUTION

METRICS_MIN_INTERVAL = 2.0

#: Mensagens gravadas e nao tratadas antes de uma queda: ate' quando retomar.
#: Mais velhas que isto ja' nao sao pedido esperando -- responder agora
#: confundiria o grupo -- e viram `expired` com aviso no log.
JANELA_DE_RETOMADA_SEGUNDOS = 6 * 3600

# As imagens enviadas ficam guardadas: servem de comprovante do que o consultor
# recebeu e alimentam a tela de detalhe da solicitacao no painel.
COMPROVANTES_DIR = ROOT / "comprovantes"
COMPROVANTES_DIR.mkdir(exist_ok=True)


def _curto(exc: BaseException) -> str:
    texto = str(exc).strip()
    return texto.splitlines()[0][:160] if texto else exc.__class__.__name__


class Consultor(NamedTuple):
    """Quem pediu a simulacao, ja' resolvido contra o cadastro.

    Existe para as funcoes do fluxo nao precisarem receber cadastro, id e
    nome como tres parametros soltos -- e para nao haver duvida sobre qual
    nome usar (o do cadastro vence o do WhatsApp).
    """

    cadastro: dict
    id: int | None
    nome: str

    @property
    def conhecido(self) -> bool:
        return bool(self.cadastro.get("known"))

    @property
    def chave(self) -> str:
        """Identidade estável, para contar coisas por consultor.

        O JID quando existe; o nome como reserva. O nome sozinho não serve
        como identidade (duas pessoas podem se chamar igual), mas para
        agrupar avisos ele basta, e é melhor que tratar todo mundo sem
        cadastro como uma pessoa só.
        """
        return (self.cadastro.get("wa_id") or "").strip() or self.nome.lower()


class BotManager:
    def __init__(self, config: Config, db: Database, hub: EventHub) -> None:
        self.config = config
        self.db = db
        self.hub = hub
        self.consultants = ConsultantRepository(db)
        self.state = StateStore(config.state_path)
        # Onde ficam os PNGs enviados. Atributo, e nao so' a constante, para
        # que cada ambiente (e cada teste) aponte para a propria pasta.
        self.comprovantes_dir = Path(config.comprovantes_dir or COMPROVANTES_DIR)

        self._conferir_perfis_distintos()
        self.whatsapp = self._montar_whatsapp()

        # No modo remoto a automação do Santander roda fora daqui (ver
        # app/remote.py); o resto do sistema não percebe a diferença.
        registrar = lambda level, message: self.log(level, "simulador", message)  # noqa: E731
        if config.simulator_mode == "remote":
            self.simulators = [
                RemoteSimulator(name=f"simulador-remoto-{i + 1}", on_log=registrar)
                for i in range(config.worker_count)
            ]
        else:
            self.simulators = [
                SimulatorService(config, index=i, on_log=registrar)
                for i in range(config.worker_count)
            ]

        self.queue = QueueService(
            db=db,
            hub=hub,
            simulators=self.simulators,
            max_attempts=config.max_attempts,
            job_timeout=config.job_timeout_seconds,
            on_result=self._deliver_result,
            on_log=self._queue_log,
            alerta_fila=config.alerta_fila,
            alerta_espera_s=config.alerta_espera_minutos * 60,
        )

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._metrics_dirty = threading.Event()
        self._started_at = now_iso()
        # None = ainda não sabemos; evita anunciar queda antes da 1ª conexão.
        self._wa_conectado: bool | None = None

    # ------------------------------------------------------------ ciclo de vida
    def _conferir_perfis_distintos(self) -> None:
        """WhatsApp e Santander NUNCA no mesmo diretorio de perfil.

        O Chromium tranca o perfil: o segundo navegador a subir nele morre
        com "Target page, context or browser has been closed", e o sintoma
        parece defeito de codigo. So' importa quando os dois navegadores
        rodam aqui (WhatsApp pela tela e Santander local).
        """
        config = self.config
        if config.whatsapp_mode == MODO_EVOLUTION or config.simulator_mode != "local":
            return
        try:
            mesmo = (Path(config.whatsapp_profile_dir).resolve()
                     == Path(config.simulator_profile_dir).resolve())
        except OSError:
            return
        if mesmo:
            raise RuntimeError(
                "WHATSAPP_PROFILE_DIR e SIMULATOR_PROFILE_DIR apontam para o MESMO "
                f"perfil ({config.whatsapp_profile_dir}). Cada navegador precisa do seu.")

    @staticmethod
    def _prova_de_entrega(resultado) -> str:
        """O id que a camada devolveu como prova de que a mensagem saiu.

        No modo evolution e' o ``key.id`` da resposta HTTP -- prova de que a
        Evolution aceitou, e a chave para achar a mensagem depois. No modo dom
        nao ha' equivalente: a camada antiga so' devolve um booleano, e nunca
        soube dizer QUAL mensagem escreveu. Guardar quando existe e aceitar
        vazio quando nao existe mantem os dois modos no mesmo caminho.
        """
        enviado = getattr(resultado, "enviado_id", "") or ""
        if enviado:
            return str(enviado)
        evidencia = getattr(resultado, "evidencia", None) or {}
        return str(evidencia.get("key_id") or "")

    def _montar_whatsapp(self):
        """Escolhe a camada de WhatsApp. O UNICO lugar que sabe qual esta em uso.

        Depois daqui, tudo fala com a mesma superficie (``whatsapp_port``): a
        fila, o banco, o painel e o parser nao sabem -- nem precisam saber --
        se a mensagem veio de uma tela raspada ou de um webhook.

        Modo desconhecido nao existe: ``config`` ja' reduz qualquer coisa
        estranha a ``dom``, que e' o comportamento provado.
        """
        config = self.config
        registrar = lambda level, message: self.log(level, "whatsapp", message)  # noqa: E731

        if config.whatsapp_mode == MODO_EVOLUTION:
            # Falhar aqui, alto, e' muito melhor do que subir um bot que nunca
            # vai responder e ninguem entende por que.
            faltando = [nome for nome, valor in (
                ("EVOLUTION_API_KEY", config.evolution_api_key),
                ("EVOLUTION_GROUP_JID", config.evolution_group_jid),
                ("EVOLUTION_WEBHOOK_TOKEN", config.evolution_webhook_token),
            ) if not valor]
            if faltando:
                raise RuntimeError(
                    "WHATSAPP_MODE=evolution exige " + ", ".join(faltando)
                    + " no .env. Rode: python -m app.evolution_check")

            self.log("INFO", "whatsapp",
                     f"Camada de WhatsApp: Evolution API ({config.evolution_url}).")
            return EvolutionClient(
                base_url=config.evolution_url,
                api_key=config.evolution_api_key,
                instance=config.evolution_instance,
                group_jid=config.evolution_group_jid,
                group_name=config.whatsapp_group_name,
                browser_path=config.browser_path,
                on_status=self._on_whatsapp_status,
                on_log=registrar,
            )

        return WhatsAppService(
            profile_dir=config.whatsapp_profile_dir,
            group_name=config.whatsapp_group_name,
            state=self.state,
            poll_seconds=config.poll_seconds,
            headless=config.whatsapp_headless,
            reply_quote=config.reply_quote,
            browser_executable=config.browser_path,
            bot_self_name=config.bot_self_name,
            on_status=self._on_whatsapp_status,
            on_log=registrar,
        )

    def start(self) -> None:
        self.log("INFO", "sistema", "Iniciando serviços...")
        # No banco tambem: e' o log que o painel mostra, e a duvida
        # "esta versao tem o conserto?" precisa ser respondida por ele.
        try:
            from main import versao_do_codigo
            self.log("INFO", "sistema", f"Código carregado: {versao_do_codigo()}")
        except Exception:
            pass
        for simulator in self.simulators:
            simulator.start()
        self.queue.start()

        recovered = self.queue.recover()
        if recovered:
            self.log("WARNING", "sistema", f"{recovered} solicitação(ões) retomadas da fila.")
        # Depois do recover: uma mensagem que ja' virou solicitacao e' ligada
        # a ela aqui, em vez de gerar outra.
        self._retomar_entradas()

        if self.config.simulator_enabled and self.config.simulator_mode == "local":
            self._spawn(self._warm_simulators, "warmup")
        self.whatsapp.start()

        self._spawn(self._inbox_loop, "inbox")
        self._spawn(self._metrics_loop, "metrics")
        self._spawn(self._reenvio_loop, "reenvio")
        self._metrics_dirty.set()

    def stop(self) -> None:
        self._stop.set()
        self.queue.stop()
        # Cada servico se encerra na propria thread dona: nenhum objeto do
        # Playwright e' tocado a partir daqui.
        self.whatsapp.stop()
        for simulator in self.simulators:
            simulator.stop()
        for thread in self._threads:
            thread.join(timeout=3)
        self.log("INFO", "sistema", "Serviços encerrados.")

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    # Quanto esperar entre varreduras de pendencia. Baixo demais martela o
    # WhatsApp logo depois de uma queda; alto demais deixa o consultor esperando.
    _INTERVALO_REENVIO = 30.0
    _MAX_REENVIOS = 5
    # Folga antes de considerar uma entrega perdida. Precisa ser maior que
    # o envio mais lento (a imagem passa de um minuto), senao o reenvio
    # atropela a entrega original.
    _CARENCIA_REENVIO = 180.0

    def _reenvio_loop(self) -> None:
        """Reenvia resultados prontos que nao chegaram ao consultor.

        A entrega pode falhar por motivos passageiros -- o navegador caiu, a
        conversa fechou, o WhatsApp reconectou no meio. Antes, esse resultado
        se perdia em silencio.
        """
        while not self._stop.wait(self._INTERVALO_REENVIO):
            if not self.whatsapp.status.connected:
                continue
            try:
                self._reenviar_pendentes()
            except Exception as exc:
                self.log("ERROR", "sistema", f"Falha no reenvio: {_curto(exc)}")

    def _reenviar_pendentes(self) -> None:
        # Dois tipos de linha entram aqui:
        #
        # * `retrying` -- a primeira entrega falhou por motivo passageiro. O
        #   estado e' explicito, entao nao ha' corrida com entrega em curso
        #   (essa esta' `pending`); respeita so' o `next_delivery_at`.
        # * legado (`delivery_status` vazio) -- linhas de antes desta coluna,
        #   e as interrompidas. Seguem a regra antiga: so' o que terminou ha'
        #   mais de _CARENCIA_REENVIO segundos.
        #
        # `unconfirmed` e `failed` NAO entram: o primeiro duplicaria uma
        # mensagem que pode ter chegado; o segundo ja' foi desistido.
        #
        # INTERROMPIDA entra junto de propósito: ela terminou (travou), o
        # consultor está esperando, e sem isto ficaria invisível — o painel
        # mostrava "Interrompido" e o grupo não recebia nada.
        agora = now_iso()
        limite = iso_atras(self._CARENCIA_REENVIO)
        pendentes = self.db.fetchall(
            "SELECT * FROM simulations "
            " WHERE replied_at IS NULL "
            "   AND status IN (?, ?, ?) "
            "   AND (reply_attempts IS NULL OR reply_attempts < ?) "
            "   AND ( (COALESCE(delivery_status,'') = ? "
            "          AND COALESCE(next_delivery_at,'') <= ?) "
            "      OR (COALESCE(delivery_status,'') = '' "
            "          AND COALESCE(finished_at, updated_at, created_at) < ?) ) "
            " ORDER BY id LIMIT 5",
            (Status.COMPLETED, Status.ERROR, Status.INTERRUPTED,
             self._MAX_REENVIOS, Delivery.RETRYING, agora, limite),
        )
        for linha in pendentes:
            self._reenviar_um(dict(linha))

    def _reivindicar_reenvio(self, linha: dict) -> bool:
        """Marca a linha como `pending` SE ninguem pegou antes. Atomico."""
        with self.db.write() as conn:
            cur = conn.execute(
                "UPDATE simulations SET delivery_status=?, updated_at=? "
                " WHERE id=? AND replied_at IS NULL "
                "   AND COALESCE(delivery_status,'') IN (?, ?)",
                (Delivery.PENDING, now_iso(), linha.get("id"), *Delivery.REENVIAVEIS),
            )
            return cur.rowcount == 1

    def _reenviar_um(self, linha: dict) -> None:
        tentativas = int(linha.get("reply_attempts") or 0) + 1
        request_id = linha.get("request_id") or "?"
        estado_antes = linha.get("delivery_status") or ""

        if not self._reivindicar_reenvio(linha):
            return   # outra varredura (ou a entrega original) ja' esta' nela

        # O banco primeiro: uma saida ja' entregue (a gravacao do desfecho
        # falhou) ou uma chamada que comecou e nao terminou (``sending``) proibem
        # o reenvio. Mandar de novo nesses casos e' duplicar no grupo.
        situacao, _saida = self.queue.situacao_do_envio(int(linha.get("id") or 0))
        if situacao != "nada":
            self.queue._resolver_entrega_interrompida(
                linha, "o reenvio encontrou um envio anterior",
                manter_status=linha.get("stage") not in (Stage.DELIVERY_RETRY, Stage.REPLYING))
            return

        # Trava contra reenvio duplicado.
        #
        # Se a verificação de entrega falhar por qualquer motivo — seletor
        # errado, DOM novo — o reenvio mandaria a mesma resposta até cinco
        # vezes. Cinco cópias no grupo do cliente é pior que uma entrega não
        # confirmada. Perguntar ao chat é barato e não depende de classe CSS.
        try:
            if request_id != "?" and self.whatsapp.ja_enviado(request_id):
                self.db.update(
                    "simulations",
                    {"replied_at": now_iso(), "updated_at": now_iso(),
                     "delivery_status": Delivery.DELIVERED,
                     **self._etapa_depois_da_entrega(linha, Delivery.DELIVERED)},
                    {"id": linha.get("id")},
                )
                self.log(
                    "WARNING", "whatsapp",
                    f"Verificação deu falso negativo em {request_id}: a resposta "
                    "já estava no grupo. Não reenviei.",
                    request_id=request_id,
                )
                return
        except Exception as exc:
            # Não conseguir perguntar não pode impedir o reenvio: ficar sem
            # resposta é o defeito que este laço existe para corrigir.
            self.log("WARNING", "whatsapp",
                     f"Não consegui conferir se {request_id} já saiu ({_curto(exc)}). "
                     "Vou reenviar mesmo assim.",
                     request_id=request_id)

        consultor = linha.get("consultant_name") or ""
        texto, texto_sem_citacao = mensagens.duas_versoes(
            mensagens.resultado_reenviado, linha, self.config.mask_cpf_in_ui,
            consultor=consultor)
        registro: dict = {}
        try:
            enviado = self._send_reply(
                self._origem_da_linha(linha),
                texto,
                texto_sem_citacao=texto_sem_citacao,
                status=linha.get("status") or Status.COMPLETED,
                simulation_id=int(linha.get("id") or 0),
                request_id=request_id,
                consultant_name=consultor,
                consultant_id=linha.get("consultant_id"),
                registro=registro,
                tentativa=tentativas + 1,
            )
        except Exception as exc:   # nunca deixar a linha presa em `pending`
            enviado = False
            registro = {"motivo": _curto(exc), "transitorio": True}

        if enviado:
            entrega = Delivery.DELIVERED
        elif registro.get("sem_prova"):
            entrega = Delivery.UNCONFIRMED
        elif registro.get("transitorio", True) and tentativas < self._MAX_REENVIOS:
            # Legado continua legado: a regra antiga (carencia + contador)
            # segue valendo para ele, sem ganhar estado que nunca teve.
            entrega = Delivery.RETRYING if estado_antes == Delivery.RETRYING else ""
        else:
            entrega = Delivery.FAILED

        campos = {"reply_attempts": tentativas, "updated_at": now_iso(),
                  "delivery_status": entrega,
                  "delivery_error": "" if enviado else (registro.get("motivo") or "")[:240],
                  **self._etapa_depois_da_entrega(linha, entrega)}
        if registro.get("quote_status"):
            campos["quote_status"] = registro["quote_status"]
        if enviado:
            campos["replied_at"] = now_iso()
            campos["sent_message_id"] = registro.get("enviado_id") or ""
            self.log("INFO", "whatsapp",
                     f"{request_id}: resultado reenviado com sucesso "
                     f"(tentativa {tentativas}, citação {registro.get('quote_status') or '?'}, "
                     f"id {registro.get('enviado_id') or 'sem id'}).",
                     request_id=request_id)
        elif entrega == Delivery.UNCONFIRMED:
            self.log("WARNING", "whatsapp",
                     f"{request_id}: a API aceitou o reenvio mas não devolveu o id da "
                     "mensagem. Não vou reenviar de novo sozinho (duplicaria).",
                     request_id=request_id)
        elif entrega == Delivery.FAILED:
            self.log("ERROR", "whatsapp",
                     f"{request_id}: desisti de entregar depois de {tentativas} "
                     f"tentativas ({registro.get('motivo') or 'sem motivo'}). "
                     "O resultado continua no painel.",
                     request_id=request_id)
        else:
            campos["next_delivery_at"] = depois_de(self._espera_do_reenvio(tentativas))
        self.db.update("simulations", campos, {"id": linha.get("id")})

    # ----------------------------------------- entrega incerta: decisao manual
    #: O que o operador pode dizer depois de olhar o grupo.
    ACOES_ENTREGA_INCERTA = ("chegou", "nao_chegou")

    def resolver_entrega_incerta(self, simulation_id: int, acao: str,
                                 quem: str = "painel") -> dict:
        """O operador conferiu o grupo e decide o que a API nao deixou provar.

        Entrega incerta (``unconfirmed``) nunca e' reenviada sozinha: a primeira
        pode ter chegado. So' uma pessoa olhando o WhatsApp desempata:

        * ``chegou``     -- fecha como entregue. NADA e' enviado;
        * ``nao_chegou`` -- libera UM reenvio pelo laco de sempre: mesma
          solicitacao, mesma citacao, as mesmas travas (``situacao_do_envio``,
          ``ja_enviado``).

        A troca so' acontece se a linha AINDA estiver ``unconfirmed``, numa
        UPDATE condicional: dois cliques, duas abas ou duas pessoas nao viram
        dois reenvios. ``nao_chegou`` vale UMA vez por solicitacao: se o
        reenvio liberado tambem ficar incerto, sobra so' ``chegou`` -- o bot
        nao entra num ciclo de reenvios manuais sobre uma entrega que ninguem
        consegue provar. ``ValueError`` = acao invalida; ``LookupError`` = id
        inexistente; ``{"ok": False}`` = a troca nao pode ser feita (motivo).
        """
        if acao not in self.ACOES_ENTREGA_INCERTA:
            raise ValueError(f"ação desconhecida: {acao or '(vazia)'}; "
                             f"use {' ou '.join(self.ACOES_ENTREGA_INCERTA)}")
        linha = self.db.fetchone("SELECT * FROM simulations WHERE id=?", (simulation_id,))
        if not linha:
            raise LookupError("simulação não encontrada")
        linha = dict(linha)
        campos = (self._campos_de_entregue_manual(linha) if acao == "chegou"
                  else self._campos_de_reenvio_manual(linha))
        campos.update(delivery_resolved_by=(quem or "painel")[:60],
                      delivery_resolved_at=campos["updated_at"])
        if not self._trocar_se_incerta(simulation_id, campos):
            return {"ok": False, "motivo": self._por_que_nao_troca(simulation_id)}
        self._anunciar_decisao_manual(linha, acao, quem)
        return {"ok": True, "delivery_status": campos["delivery_status"]}

    def _por_que_nao_troca(self, simulation_id: int) -> str:
        agora = self.db.fetchone(
            "SELECT delivery_status, delivery_resolution FROM simulations WHERE id=?",
            (simulation_id,)) or {}
        if (agora.get("delivery_status") == Delivery.UNCONFIRMED
                and agora.get("delivery_resolution") == "manual:nao_chegou"):
            return ("já houve um reenvio manual desta solicitação e ele também ficou "
                    "incerto; confira o grupo e use 'Chegou' se ela estiver lá")
        return f"a entrega não está mais incerta (agora: {agora.get('delivery_status') or 'sem estado'})"

    @staticmethod
    def _campos_de_entregue_manual(linha: dict) -> dict:
        agora = now_iso()
        _status, final = estado_final(bool(linha.get("result_ok")))
        campos = {"delivery_status": Delivery.DELIVERED, "replied_at": agora,
                  "delivery_error": "", "next_delivery_at": None,
                  "delivery_resolution": "manual:chegou", "updated_at": agora}
        if linha.get("stage") == Stage.DELIVERY_UNCONFIRMED:
            campos["stage"] = final
        return campos

    def _campos_de_reenvio_manual(self, linha: dict) -> dict:
        agora = now_iso()
        campos = {"delivery_status": Delivery.RETRYING, "next_delivery_at": agora,
                  "delivery_error": "conferido no painel: não chegou; reenvio liberado",
                  # UM reenvio, mesmo com as tentativas esgotadas: foi uma
                  # pessoa que pediu, depois de olhar o grupo.
                  "reply_attempts": min(int(linha.get("reply_attempts") or 0),
                                        self._MAX_REENVIOS - 1),
                  "delivery_resolution": "manual:nao_chegou", "updated_at": agora}
        if linha.get("stage") == Stage.DELIVERY_UNCONFIRMED:
            campos["stage"] = Stage.DELIVERY_RETRY
        return campos

    def _trocar_se_incerta(self, simulation_id: int, campos: dict) -> bool:
        colunas = ", ".join(f"{coluna}=?" for coluna in campos)
        # ``nao_chegou`` so' uma vez: a condicao mora na MESMA UPDATE, entao
        # nem duas abas ao mesmo tempo passam dela.
        so_uma_vez = ("AND COALESCE(delivery_resolution,'') != 'manual:nao_chegou' "
                      if campos["delivery_status"] == Delivery.RETRYING else "")
        with self.db.write() as conn:
            cur = conn.execute(
                f"UPDATE simulations SET {colunas} "
                f" WHERE id=? AND delivery_status=? AND replied_at IS NULL {so_uma_vez}",
                (*campos.values(), simulation_id, Delivery.UNCONFIRMED))
            if cur.rowcount != 1:
                return False
            if campos["delivery_status"] == Delivery.RETRYING:
                # As saidas ``sending``/``unconfirmed`` fazem o reenvio desistir
                # (situacao_do_envio -> incerta), de proposito. Quem olhou o
                # grupo disse que elas nao chegaram: viram ``failed``.
                conn.execute(
                    "UPDATE messages SET status='failed', "
                    "       error=TRIM(COALESCE(NULLIF(error,''), '') || ' [conferido no painel: não chegou]') "
                    " WHERE simulation_id=? AND direction='out' AND status IN (?, ?)",
                    (simulation_id, ENVIO_EM_CURSO, Delivery.UNCONFIRMED))
            return True

    def _anunciar_decisao_manual(self, linha: dict, acao: str, quem: str) -> None:
        request_id = linha.get("request_id") or ""
        if acao == "chegou":
            nivel, titulo = "success", "Entrega conferida no WhatsApp: chegou"
            detalhe = "Marcada como entregue no painel. Nada foi reenviado."
        else:
            nivel, titulo = "warning", "Entrega conferida no WhatsApp: não chegou"
            detalhe = ("Reenvio liberado no painel: sai na próxima volta do laço de "
                       "reenvio, com o WhatsApp conectado.")
        self.log("INFO" if acao == "chegou" else "WARNING", "whatsapp",
                 f"{request_id}: {titulo.lower()} (por {quem}). {detalhe}",
                 request_id=request_id, consultant=linha.get("consultant_name") or "")
        self.hub.publish(
            "delivery_manual",
            {"request_id": request_id, "simulation_id": linha.get("id"),
             "acao": acao, "por": quem},
            stage=Stage.DELIVERY_RETRY if acao == "nao_chegou" else "",
            level=nivel, title=titulo, detail=detalhe, request_id=request_id,
            simulation_id=linha.get("id"),
            consultant_name=linha.get("consultant_name") or "",
            chat_id=linha.get("chat_id") or "")

    @staticmethod
    def _espera_do_reenvio(tentativas: int) -> float:
        """30 s, 60 s, 120 s... ate' 10 min. Nao martela a API caida."""
        return min(600.0, 30.0 * (2 ** max(0, tentativas - 1)))

    @staticmethod
    def _etapa_depois_da_entrega(linha: dict, entrega: str) -> dict:
        """A etapa so' muda para linhas que estavam esperando entrega.

        Uma interrompida continua `interrupted` depois de avisada; uma
        linha legada ja' estava na etapa final.
        """
        if linha.get("stage") not in (Stage.DELIVERY_RETRY, Stage.REPLYING):
            return {}
        _status, final = estado_final(bool(linha.get("result_ok")))
        return {"stage": {
            Delivery.DELIVERED: final,
            Delivery.UNCONFIRMED: Stage.DELIVERY_UNCONFIRMED,
            Delivery.FAILED: Stage.DELIVERY_FAILED,
        }.get(entrega, Stage.DELIVERY_RETRY)}

    def _origem_da_linha(self, linha: dict) -> IncomingMessage:
        """A mensagem original, lida do banco. Nunca da tela."""
        return IncomingMessage(
            message_id=linha.get("source_message_id") or "",
            chat_id=linha.get("chat_id") or "",
            chat_name=linha.get("chat_name") or self.config.whatsapp_group_name,
            sender_id=linha.get("sender_id") or "",
            sender_name=linha.get("sender_name") or linha.get("consultant_name") or "Consultor",
            text=linha.get("raw_message") or "",
            timestamp=linha.get("source_timestamp") or "",
            participant=linha.get("participant") or "",
        )

    def _warm_simulators(self) -> None:
        for simulator in self.simulators:
            if self._stop.is_set():
                return
            try:
                simulator.warm_up()
                self.log("INFO", "simulador", f"{simulator.name} pronto.")
            except Exception as exc:
                self.log("WARNING", "simulador", f"{simulator.name} não abriu ainda: {exc}")

    # ------------------------------------------------------------------- logs
    def log(
        self,
        level: str,
        service: str,
        message: str,
        request_id: str = "",
        consultant: str = "",
    ) -> None:
        safe_message = redact(message)
        stamp = now_iso()
        try:
            self.db.insert(
                "logs",
                {
                    "level": level,
                    "service": service,
                    "message": safe_message,
                    "request_id": request_id,
                    "consultant": consultant,
                    "created_at": stamp,
                },
            )
        except Exception:
            pass
        self.hub.publish(
            "log",
            {
                "level": level,
                "service": service,
                "message": safe_message,
                "request_id": request_id,
                "consultant": consultant,
                "created_at": stamp,
            },
            persist=False,
        )

    def _queue_log(self, level: str, service: str, message: str, **extra) -> None:
        self.log(
            level,
            service,
            message,
            request_id=extra.get("request_id", ""),
            consultant=extra.get("consultant", ""),
        )

    # -------------------------------------------------------- entrada WhatsApp
    def receber_mensagem(self, message: IncomingMessage) -> dict:
        """Porta de entrada DURAVEL: grava antes de aceitar.

        Usada pelo webhook. A mensagem so' vai para a fila de leitura depois
        de gravada -- se o processo cair antes de trata-la, o boot a retoma
        (``_retomar_entradas``). E a gravacao e' a trava de idempotencia: uma
        reentrega do mesmo ``message_id`` nao entra de novo, nem depois de
        reiniciar.

        Levanta excecao se nao conseguir gravar; a rota devolve erro e a
        Evolution reentrega, que e' exatamente o que se quer.
        """
        self.registrar_webhook()
        registro = self._registrar_entrada(message)
        if not registro["novo"]:
            self.log(
                "INFO", "whatsapp",
                f"Mensagem {message.message_id} já recebida antes "
                f"({registro.get('status')}"
                + (f", {registro['request_id']}" if registro.get("request_id") else "")
                + "). Reentrega ignorada, sem nova solicitação.",
                request_id=registro.get("request_id") or "",
            )
            return {"aceita": False, "motivo": "duplicada",
                    "request_id": registro.get("request_id") or ""}
        self.whatsapp.inbox.put(message)
        return {"aceita": True, "entrada_id": registro["id"]}

    def _registrar_entrada(self, message: IncomingMessage) -> dict:
        """Grava a mensagem recebida (uma vez so'). Ver ``Database.registrar_entrada``."""
        return self.db.registrar_entrada({
            "kind": "text",
            "chat_id": message.chat_id,
            "chat_name": message.chat_name,
            "wa_message_id": message.message_id,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "participant": message.participant,
            "wa_timestamp": str(message.timestamp or ""),
            "text": message.text,
            "created_at": now_iso(),
        })

    def _abrir_entrada(self, message: IncomingMessage) -> int | None:
        """Grava a mensagem e devolve o id da linha -- ou None se ja' foi tratada.

        A gravacao e' a trava: se esta mensagem ja' foi tratada (reentrega,
        leitura repetida, retomada duplicada), nada mais acontece.
        `received` ainda nao tratada e' o caso normal de quem chegou pelo
        webhook -- segue.
        """
        entrada = self._registrar_entrada(message)
        if not entrada["novo"] and entrada.get("status") != ENTRADA_RECEBIDA:
            self.log(
                "INFO", "whatsapp",
                f"Mensagem {message.message_id} já tratada ({entrada.get('status')}"
                + (f", {entrada['request_id']}" if entrada.get("request_id") else "")
                + "). Não crio outra solicitação.",
                request_id=entrada.get("request_id") or "",
            )
            return None

        existente = self._solicitacao_existente(message)
        if existente:
            # A solicitacao nasceu e a queda impediu de ligar a mensagem a ela.
            self._marcar_entrada(entrada["id"], ENTRADA_SOLICITACAO,
                                 simulation_id=existente["id"],
                                 request_id=existente["request_id"])
            self.log("WARNING", "whatsapp",
                     f"Mensagem {message.message_id} já tinha gerado "
                     f"{existente['request_id']}; não crio outra.",
                     request_id=existente["request_id"])
            return None
        return entrada["id"]

    def _marcar_entrada(self, entrada_id: int | None, status: str, **extra) -> None:
        if entrada_id:
            self.db.update("messages", {"status": status, **extra}, {"id": entrada_id})

    def _retomar_entradas(self) -> int:
        """Devolve a fila de leitura o que foi gravado e nao chegou a ser tratado.

        Cobre a queda entre "o webhook respondeu 200" e "a mensagem virou
        solicitacao": a Evolution nao reentrega (recebeu 200), e sem isto o
        pedido sumia.
        """
        linhas = self.db.fetchall(
            "SELECT * FROM messages WHERE direction='in' AND status=? ORDER BY id",
            (ENTRADA_RECEBIDA,),
        )
        limite = utc_now() - timedelta(seconds=JANELA_DE_RETOMADA_SEGUNDOS)
        retomadas = 0
        for linha in linhas:
            criada = parse_iso(linha.get("created_at"))
            if criada is not None and criada < limite:
                self._marcar_entrada(linha["id"], ENTRADA_EXPIRADA)
                self.log("WARNING", "whatsapp",
                         f"Mensagem {linha.get('wa_message_id')} ficou sem tratar desde "
                         f"{linha.get('created_at')}; velha demais para responder agora.")
                continue
            self.whatsapp.inbox.put(IncomingMessage(
                message_id=linha.get("wa_message_id") or "",
                chat_id=linha.get("chat_id") or "",
                chat_name=linha.get("chat_name") or "",
                sender_id=linha.get("sender_id") or "",
                sender_name=linha.get("sender_name") or "",
                text=linha.get("text") or "",
                timestamp=linha.get("wa_timestamp") or "",
                participant=linha.get("participant") or "",
            ))
            retomadas += 1
        if retomadas:
            self.log("WARNING", "whatsapp",
                     f"{retomadas} mensagem(ns) recebida(s) antes do reinício voltaram "
                     "para a leitura.")
        return retomadas

    def _inbox_loop(self) -> None:
        while not self._stop.is_set():
            try:
                message = self.whatsapp.inbox.get(timeout=0.5)
            except Exception:
                continue
            if message is None:
                continue
            try:
                self._handle_message(message)
            except Exception as exc:
                # A mensagem continua gravada como `received`: o proximo boot
                # a retoma. O id vai no log para achar o caso.
                self.log("ERROR", "bot",
                         f"Falha ao tratar a mensagem {message.message_id}: {exc}")

    # Marcas que so' aparecem no que ESTE bot escreve. Se uma delas estiver no
    # texto, a mensagem e' nossa e nao pode ser tratada como pedido.
    # Vem da camada de mensagens: se um texto mudar la', o filtro acompanha.
    _ASSINATURAS_DO_BOT = mensagens.ASSINATURAS

    def _e_mensagem_nossa(self, texto: str) -> bool:
        """Reconhece o proprio formato de saida.

        Segunda camada contra o laco que encheu o grupo de 53 cobrancas: a
        resposta "para simular preciso de: CPF." contem a palavra CPF sem
        valor, entao o parser a lia como pedido incompleto e o bot cobrava de
        novo -- alimentando a si mesmo. A primeira camada e' nao reler o que
        enviamos; esta garante que, mesmo se uma escapar, o ciclo nao recomeca.
        """
        # Comparacao SEM diferenciar maiuscula: "Não consegui" e "não
        # consegui" sao a mesma fala, e depender do caso deixaria uma delas
        # escapar -- exatamente o tipo de brecha que reabre o laco.
        alvo = (texto or "").casefold()
        return any(marca.casefold() in alvo for marca in self._ASSINATURAS_DO_BOT)

    def _identificar_e_registrar(self, message: IncomingMessage,
                                 entrada_id: int | None = None) -> Consultor:
        """Quem mandou, e o registro de que a mensagem chegou.

        Junta duas coisas que sempre andam juntas: descobrir o consultor pelo
        telefone e gravar/publicar o recebimento. Separa-las renderia duas
        funcoes que nunca sao chamadas em separado.

        A identidade vem do REMETENTE (``sender_id``), nunca do corpo: o nome
        escrito na mensagem e' o do cliente.
        """
        received = self.db.bump_meta("wa_received", 1)
        cadastro = self.consultants.resolve(message.sender_id, message.sender_name)
        consultor_id = cadastro.get("id")
        consultor_nome = cadastro.get("name") or message.sender_name or "Consultor"

        if not entrada_id:
            entrada_id = self._registrar_entrada(message)["id"]
        self.db.update("messages", {"consultant_id": consultor_id,
                                    "consultant_name": consultor_nome},
                       {"id": entrada_id})
        self.hub.publish(
            "message_received",
            {
                "message_id": message.message_id,
                "chat_id": message.chat_id,
                "chat_name": message.chat_name,
                "sender_id": message.sender_id,
                "sender_name": message.sender_name,
                "consultant_id": consultor_id,
                "consultant": consultor_nome,
                "known_consultant": bool(cadastro.get("known")),
                # O evento e' log operacional: CPF sai mascarado. O texto
                # integral fica so' na tabela de mensagens.
                "text": redact(message.text),
                "received_total": received,
            },
            stage=Stage.RECEIVED,
            title="Mensagem recebida",
            detail=f"{consultor_nome} · {message.chat_name or ''}".strip(" ·"),
            consultant_name=consultor_nome,
            chat_id=message.chat_id,
        )
        self.hub.publish(
            "consultant_identified",
            {
                "consultant_id": consultor_id,
                "consultant": consultor_nome,
                "wa_id": message.sender_id,
                "phone": message.sender_phone,
                "new": not cadastro.get("known"),
            },
            stage=Stage.IDENTIFIED,
            title="Consultor identificado",
            detail=consultor_nome,
            consultant_name=consultor_nome,
            chat_id=message.chat_id,
        )
        self._metrics_dirty.set()
        return Consultor(cadastro, consultor_id, consultor_nome)

    def _recusar(self, message: IncomingMessage, consultor: "Consultor", texto: str,
                 *, etiqueta: str, motivo: str, titulo: str, detalhe: str,
                 extra: dict | None = None) -> None:
        """Avisa o consultor e registra que o pedido nao seguiu.

        Os dois motivos de recusa -- falta dado, banco nao atendido -- faziam
        exatamente as mesmas tres coisas com textos diferentes. Uma funcao so'
        evita que um caminho ganhe tratamento e o outro fique para tras.
        """
        self._send_reply(message, texto, status=etiqueta,
                         consultant_name=consultor.nome, consultant_id=consultor.id)
        self.hub.publish(
            "request_rejected",
            {"consultant": consultor.nome, "reason": motivo, **(extra or {})},
            stage=Stage.VALIDATED,
            level="warning",
            title=titulo,
            detail=detalhe,
            consultant_name=consultor.nome,
            chat_id=message.chat_id,
        )

    def _solicitacao_existente(self, message: IncomingMessage) -> dict | None:
        """A solicitacao que esta mensagem JA' gerou, se gerou."""
        if not message.message_id:
            return None
        return self.db.fetchone(
            "SELECT id, request_id, status FROM simulations "
            " WHERE source_message_id=? AND COALESCE(chat_id,'')=? ORDER BY id LIMIT 1",
            (message.message_id, message.chat_id or ""),
        )

    def _gravar_solicitacao(self, message: IncomingMessage, consultor: "Consultor",
                            parsed: ParsedRequest, effective_name: str,
                            entrada_id: int | None = None) -> tuple[str, int] | None:
        """Grava a solicitacao e devolve (request_id, simulation_id) -- ou None.

        O ``request_id`` nasce aqui e acompanha a solicitacao ate' a resposta:
        e' ele que liga a mensagem do WhatsApp, a linha do banco, os eventos
        do painel e a imagem enviada. Nao mudar essa amarracao.

        A solicitacao e o vinculo com a mensagem sao gravados na MESMA
        transacao: uma queda entre os dois deixava a mensagem "sem tratar" e
        a solicitacao na fila, e a retomada criaria uma segunda.

        None = esta mensagem ja' tem solicitacao. A checagem roda DENTRO da
        transacao de escrita: duas leituras da mesma mensagem ao mesmo tempo
        (retomada + reentrega) nao criam duas simulacoes. O REQ reservado
        antes fica sem uso -- um numero pulado, nunca um pedido duplicado.
        """
        request_id = self._next_request_id()
        dados = self._linha_da_solicitacao(request_id, message, consultor,
                                           parsed, effective_name)
        with self.db.write() as conn:
            existente, simulation_id = self._inserir_solicitacao(
                conn, dados, message, entrada_id)
        if existente is not None:
            # Fora da transacao: o log grava no banco e abriria outra.
            self.log("WARNING", "whatsapp",
                     f"Mensagem {message.message_id} já tinha gerado "
                     f"{existente['request_id']}; não crio outra.",
                     request_id=existente["request_id"])
            return None
        self.hub.publish(
            "request_created",
            {
                "request_id": request_id,
                "simulation_id": simulation_id,
                "consultant": effective_name,
                "bank": parsed.bank,
                "contract": parsed.contract,
                "origin_message_id": message.message_id,
            },
            stage=Stage.VALIDATED,
            title="Dados validados",
            detail=f"{effective_name} · {parsed.bank} · contrato {parsed.contract}",
            request_id=request_id,
            simulation_id=simulation_id,
            consultant_name=effective_name,
            chat_id=message.chat_id,
        )
        return request_id, simulation_id

    @staticmethod
    def _inserir_solicitacao(conn, dados: dict, message: IncomingMessage,
                             entrada_id: int | None) -> tuple[dict | None, int | None]:
        """A checagem de duplicidade e a insercao, na MESMA transacao.

        Devolve ``(existente, simulation_id)``: com ``existente`` preenchido,
        nada foi inserido -- esta mensagem ja' tem solicitacao.
        """
        existente = conn.execute(
            "SELECT id, request_id FROM simulations "
            " WHERE source_message_id=? AND COALESCE(chat_id,'')=? LIMIT 1",
            (message.message_id, message.chat_id or ""),
        ).fetchone() if message.message_id else None
        if existente is not None:
            if entrada_id:
                conn.execute(
                    "UPDATE messages SET simulation_id=?, request_id=?, status=? WHERE id=?",
                    (existente["id"], existente["request_id"], ENTRADA_SOLICITACAO, entrada_id))
            return dict(existente), None

        colunas = list(dados)
        simulation_id = conn.execute(
            f"INSERT INTO simulations ({','.join(colunas)}) "
            f"VALUES ({','.join('?' for _ in colunas)})",
            tuple(dados.values()),
        ).lastrowid
        if entrada_id:
            conn.execute(
                "UPDATE messages SET simulation_id=?, request_id=?, status=? WHERE id=?",
                (simulation_id, dados["request_id"], ENTRADA_SOLICITACAO, entrada_id))
        elif message.message_id:
            conn.execute(
                "UPDATE messages SET simulation_id=?, request_id=? "
                " WHERE direction='in' AND wa_message_id=? AND simulation_id IS NULL",
                (simulation_id, dados["request_id"], message.message_id))
        return None, simulation_id

    def _linha_da_solicitacao(self, request_id: str, message: IncomingMessage,
                              consultor: "Consultor", parsed: ParsedRequest,
                              effective_name: str) -> dict:
        """A linha de `simulations`, com a ORIGEM inteira da mensagem.

        Chat, autor, id, texto e horario ficam gravados aqui: e' daqui que a
        resposta, o reenvio e a retomada depois de reiniciar tiram para onde e
        a quem responder. Nada disso e' redescoberto depois.
        """
        stamp = now_iso()
        return {
            "request_id": request_id,
            "consultant_id": consultor.id,
            "consultant_name": effective_name,
            "chat_id": message.chat_id,
            "chat_name": message.chat_name,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "participant": message.participant,
            "source_message_id": message.message_id,
            "source_timestamp": str(message.timestamp or ""),
            "cpf": parsed.cpf,
            "bank": parsed.bank,
            "contract": parsed.contract,
            "phone": parsed.phone,
            "customer_name": parsed.customer_name,
            "origin": parsed.origin,
            "simulation_type": parsed.simulation_type,
            "status": Status.QUEUED,
            "stage": Stage.VALIDATED,
            "attempts": 0,
            "max_attempts": self.config.max_attempts,
            "raw_message": message.text,
            "created_at": stamp,
            "updated_at": stamp,
        }

    def _enfileirar(self, message: IncomingMessage, consultor: "Consultor",
                    parsed: ParsedRequest, effective_name: str,
                    request_id: str, simulation_id: int) -> None:
        """Poe na fila e avisa o consultor SO' se ele for esperar."""
        job = SimulationJob(
            request=parsed,
            message=message,
            request_id=request_id,
            simulation_id=simulation_id,
            consultant_id=consultor.id,
            attempt=1,
        )
        position = self.queue.submit(job)
        self.log(
            "INFO", "bot",
            f"Solicitação {request_id} enfileirada (posição {position}).",
            request_id=request_id, consultant=effective_name,
        )
        # O aviso "peguei, tem X na frente" NAO e' mais enviado.
        #
        # Ele dobrava o numero de mensagens por pedido sem trazer nada que o
        # consultor precise: quem mandou o CPF sabe que mandou, e o que ele
        # espera e' o RESULTADO. Numa leva de pedidos o grupo recebia uma
        # fileira de "Ryan, peguei. Tem 14 na frente" -- catorze mensagens
        # antes da primeira resposta util.
        #
        # A posicao continua registrada no log e visivel no painel, que e'
        # onde essa informacao serve para alguma coisa.
        self._metrics_dirty.set()

    def _handle_message(self, message: IncomingMessage) -> None:
        if self._e_mensagem_nossa(message.text):
            self.log(
                "WARNING", "whatsapp",
                "Ignorando uma mensagem com o formato das nossas respostas — "
                "o bot leu a si mesmo. Verifique o filtro de mensagens próprias.",
            )
            return

        entrada_id = self._abrir_entrada(message)
        if entrada_id is None:
            return  # ja' tratada: UMA mensagem, UMA solicitacao

        consultor = self._identificar_e_registrar(message, entrada_id)

        parsed, missing = parse_request(
            message.text,
            fallback_consultant=consultor.nome,
            require_trigger=self.config.require_trigger,
            default_bank=self.config.default_bank,
        )

        if parsed is None and not missing:
            self._marcar_entrada(entrada_id, ENTRADA_IGNORADA)
            return  # conversa normal do grupo, nao e' um pedido

        if missing:
            self._marcar_entrada(entrada_id, ENTRADA_RECUSADA)
            self._recusar(message, consultor, mensagens.faltando(missing, consultor.nome),
                          etiqueta="missing", motivo="dados incompletos",
                          titulo="Dados incompletos",
                          detalhe=f"{consultor.nome} · faltou: {', '.join(missing)}",
                          extra={"missing": missing})
            return

        # O nome do consultor e' o do cadastro/WhatsApp. A linha solta da
        # mensagem so' vale se ainda nao houver um consultor conhecido.
        effective_name = consultor.nome if consultor.conhecido else parsed.consultant_name
        # `replace` em vez de reconstruir campo a campo: um campo novo em
        # ParsedRequest passaria batido e seria silenciosamente perdido aqui
        # (foi o que quase aconteceu com `origin`).
        parsed = replace(parsed, consultant_name=effective_name)

        if not self.config.bank_supported(parsed.bank):
            self._marcar_entrada(entrada_id, ENTRADA_RECUSADA)
            self._recusar(
                message, consultor._replace(nome=effective_name),
                mensagens.banco_nao_atendido(
                    effective_name, parsed.bank, self.config.supported_banks),
                etiqueta="unsupported", motivo="banco não suportado",
                titulo="Banco não suportado",
                detalhe=f"{effective_name} · {parsed.bank}",
                extra={"bank": parsed.bank})
            return

        criada = self._gravar_solicitacao(
            message, consultor, parsed, effective_name, entrada_id)
        if criada:
            self._enfileirar(message, consultor, parsed, effective_name, *criada)

    def _next_request_id(self) -> str:
        return f"REQ{self.db.bump_meta('request_seq', 1):06d}"

    # -------------------------------------------------------------- saida
    def _anotar_citacao(self, resultado, request_id: str, consultor: str) -> None:
        """Registra quando a resposta saiu SEM a citacao pedida.

        Nao e' so' log: e' o unico sinal de que a citacao parou de funcionar.
        Ela falhar nao segura a resposta -- o consultor recebe do mesmo jeito,
        com o nome dele no fim -- entao sem esta linha o defeito voltaria a
        ser invisivel, que e' exatamente como ele durou tanto.

        E' sobre a CITACAO. Se a entrega falhou ou ficou incerta, quem diz e'
        outra linha de log -- as duas coisas nao se misturam.
        """
        if not bool(resultado):
            return
        situacao = getattr(resultado, "quote_status", "") or ""
        if situacao == QuoteStatus.NOT_APPLIED:
            self.log(
                "WARNING", "whatsapp",
                "Citação NÃO aplicada: a API provou que a resposta saiu citando "
                "outra mensagem. A resposta chegou; não mando uma segunda.",
                request_id=request_id, consultant=consultor,
            )
        elif situacao == QuoteStatus.UNVERIFIED:
            # Saiu COM o pedido de citacao; so' nao ha' prova de que pegou.
            # Nao e' recusa (``quoted_ok`` falso aqui nao quer dizer isso).
            self.log(
                "INFO", "whatsapp",
                "Citação enviada, mas NÃO confirmada: a resposta da API não trouxe "
                "o stanzaId. Confira no celular se a resposta aparece citando o pedido.",
                request_id=request_id, consultant=consultor,
            )
        elif situacao == QuoteStatus.FALLBACK or (
                situacao != QuoteStatus.NONE and getattr(resultado, "quoted_ok", None) is False):
            self.log(
                "WARNING", "whatsapp",
                "Citação RECUSADA: a resposta foi entregue sem citação, com o nome "
                "do consultor no fim. Falha de citação, não de entrega.",
                request_id=request_id, consultant=consultor,
            )

    def _etapa(self, job: SimulationJob, stage: str) -> None:
        try:
            self.queue.atualizar_etapa(job, stage)
        except Exception as exc:  # etapa e' informativa; nao derruba a entrega
            self.log("WARNING", "sistema",
                     f"{job.request_id}: não consegui registrar a etapa {stage}: {_curto(exc)}",
                     request_id=job.request_id)

    def _evidencia(self, resultado, erro: str, message: IncomingMessage) -> dict:
        """O que a camada devolveu, num formato so' -- para o banco e o log.

        A camada Evolution classifica cada envio (``desfecho``). Excecao e
        retorno booleano (camada dom, dublês) nao dizem nada: contam como
        transitorios, que e' o comportamento antigo dessas camadas.
        """
        tipado = hasattr(resultado, "evidencia")
        evidencia = (getattr(resultado, "evidencia", None) or {}) if tipado else {}
        ok = bool(resultado)
        desfecho = (getattr(resultado, "desfecho", "") or evidencia.get("desfecho", "")) if tipado else ""
        if ok:
            transitorio, sem_prova = False, False
            desfecho = desfecho or Desfecho.ENTREGUE
        elif tipado:
            transitorio = bool(getattr(resultado, "transitorio", False)
                               or evidencia.get("transitorio", False))
            sem_prova = bool(getattr(resultado, "sem_prova", False)
                             or evidencia.get("sem_prova", False))
        else:
            transitorio, sem_prova = True, False
        quote_status = getattr(resultado, "quote_status", "") or ""
        if desfecho and tipado:
            # A camada classificou: ela sabe qual id foi na requisicao final.
            citado = getattr(resultado, "quoted_message_id", "") or ""
        else:
            citado = (message.message_id if quote_status in
                      (QuoteStatus.OK, QuoteStatus.UNVERIFIED) else "")
        return {
            "provider": (getattr(resultado, "provider", "") or self.config.whatsapp_mode),
            "desfecho": desfecho,
            "quote_status": quote_status,
            "quoted_message_id": citado,
            "quote_error": (getattr(resultado, "quote_error", "") or "") if tipado else "",
            "enviado_id": self._prova_de_entrega(resultado) if ok else "",
            "http_status": int(getattr(resultado, "http_status", 0)
                               or evidencia.get("http_status", 0) or 0),
            "transitorio": transitorio,
            "sem_prova": sem_prova,
            "media_id": getattr(resultado, "media_id", "") or "",
            "resposta": str(evidencia.get("corpo") or "")[:400],
            "motivo": erro,
        }

    @staticmethod
    def _status_da_mensagem(ok: bool, evid: dict, status_ok: str) -> str:
        if ok:
            return status_ok
        return Delivery.UNCONFIRMED if evid["sem_prova"] else "failed"

    def _log_de_envio(self, tipo: str, request_id: str, message: IncomingMessage,
                      evid: dict, tentativa: int, ok: bool, consultor: str) -> None:
        """Uma linha por envio com tudo que investiga um incidente sem abrir o banco."""
        if ok:
            nivel, resultado = "INFO", "entregue"
        elif evid["sem_prova"]:
            nivel, resultado = "WARNING", "incerta"
        elif evid["transitorio"]:
            nivel, resultado = "WARNING", evid["desfecho"] or "transitoria"
        else:
            nivel, resultado = "ERROR", evid["desfecho"] or "falhou"
        motivo = f" motivo={evid['motivo']}" if evid["motivo"] else ""
        citacao = f" quote_error={evid['quote_error']}" if evid["quote_error"] else ""
        self.log(
            nivel, "whatsapp",
            f"{request_id or 'sem REQ'} envio={tipo} tentativa={tentativa} "
            f"provider={evid['provider']} http={evid['http_status'] or '-'} "
            f"desfecho={resultado} origin_message_id={message.message_id or '-'} "
            f"quote_message_id={evid['quoted_message_id'] or '-'} "
            f"quote_participant={message.participant or '-'} "
            f"quote_status={evid['quote_status'] or '-'} "
            f"sent_message_id={evid['enviado_id'] or '-'}{motivo}{citacao}",
            request_id=request_id, consultant=consultor,
        )

    @staticmethod
    def _versao_que_saiu(resultado, com_citacao: str, sem_citacao: str) -> str:
        """Qual das duas versoes a camada mandou -- o banco tem de bater com o grupo."""
        if not sem_citacao:
            return com_citacao
        situacao = getattr(resultado, "quote_status", "") or ""
        if situacao in (QuoteStatus.FALLBACK, QuoteStatus.NONE):
            return sem_citacao
        if not situacao and getattr(resultado, "quoted_ok", True) is False:
            return sem_citacao
        return com_citacao

    def _send_reply(
        self,
        message: IncomingMessage,
        text: str,
        *,
        status: str,
        simulation_id: int | None = None,
        request_id: str = "",
        consultant_name: str = "",
        consultant_id: int | None = None,
        texto_sem_citacao: str = "",
        registro: dict | None = None,
        tentativa: int = 1,
    ) -> bool:
        """Envia texto respondendo a ``message``. O destino e a citacao vem DELA.

        ``message`` e' a mensagem original da solicitacao (gravada no banco):
        o chat, o id a citar, o texto e o autor citados. Nada aqui procura a
        mensagem de novo, olha conversa aberta ou usa "a ultima mensagem".

        ``registro``, quando passado, recebe a evidencia do envio -- quem
        decide o desfecho da entrega (reenviar, desistir) precisa dela.

        A linha do envio e' gravada ANTES de chamar a API, como ``sending``.
        Se o processo cair durante a chamada, e' essa linha que diz ao proximo
        boot que a mensagem PODE ter saido -- e que reenviar duplicaria.
        """
        registro = registro if registro is not None else {}
        linha_id = self.db.insert("messages", {
            "simulation_id": simulation_id,
            "request_id": request_id,
            "direction": "out",
            "kind": "text",
            "chat_id": message.chat_id,
            "chat_name": message.chat_name,
            "consultant_id": consultant_id,
            "consultant_name": consultant_name,
            "sender_id": message.sender_id,
            "text": text,
            "status": ENVIO_EM_CURSO,
            "provider": self.config.whatsapp_mode,
            "attempt": tentativa,
            "origin_message_id": message.message_id,
            "created_at": now_iso(),
        })
        ok = False
        error = ""
        resultado = None
        try:
            resultado = self.whatsapp.send(
                chat_id=message.chat_id,
                chat_name=message.chat_name or self.config.whatsapp_group_name,
                text=text,
                texto_sem_citacao=texto_sem_citacao,
                quote_message_id=message.message_id,
                quote_text=message.text,
                quote_participant=message.participant,
            )
            ok = bool(resultado)
            if not ok:
                error = (getattr(resultado, "motivo", "") or
                         "a camada de WhatsApp não confirmou o envio")[:240]
            self._anotar_citacao(resultado, request_id, consultant_name)
        except Exception as exc:
            error = str(exc)[:240] or exc.__class__.__name__

        evid = self._evidencia(resultado, error, message)
        registro.update(evid, ok=ok)
        saiu = self._versao_que_saiu(resultado, text, texto_sem_citacao)
        self._log_de_envio("texto", request_id, message, evid, tentativa, ok, consultant_name)

        self.db.update("messages", {
            "text": saiu,
            "wa_message_id": evid["enviado_id"],
            "status": self._status_da_mensagem(ok, evid, status),
            "provider": evid["provider"],
            "desfecho": evid["desfecho"],
            "quoted_message_id": evid["quoted_message_id"],
            "quote_status": evid["quote_status"],
            "quote_error": evid["quote_error"][:300],
            "http_status": evid["http_status"] or None,
            "error": error,
            "response_excerpt": evid["resposta"],
        }, {"id": linha_id})
        if ok:
            sent = self.db.bump_meta("wa_sent", 1)
            self.hub.publish(
                "message_sent",
                {
                    "request_id": request_id,
                    "simulation_id": simulation_id,
                    "consultant": consultant_name,
                    "chat_id": message.chat_id,
                    "text": redact(saiu),
                    "status": status,
                    "kind": "text",
                    "origin_message_id": message.message_id,
                    "quote_status": evid["quote_status"],
                    "sent_message_id": evid["enviado_id"],
                    "attempt": tentativa,
                    "sent_total": sent,
                },
                stage=Stage.REPLYING,
                title="Resposta enviada",
                detail=f"{consultant_name}" if consultant_name else "",
                request_id=request_id,
                simulation_id=simulation_id,
                consultant_name=consultant_name,
                chat_id=message.chat_id,
            )
        else:
            incerta = evid["sem_prova"]
            self.hub.publish(
                "message_failed",
                {
                    "request_id": request_id,
                    "simulation_id": simulation_id,
                    "consultant": consultant_name,
                    "error": error or "WhatsApp indisponível",
                    "desfecho": evid["desfecho"],
                    "retryable": evid["transitorio"],
                    "uncertain": incerta,
                    "http_status": evid["http_status"],
                    "attempt": tentativa,
                },
                stage=Stage.ERROR,
                level="warning" if incerta else "error",
                title=("Entrega incerta — verificar WhatsApp" if incerta
                       else "Falha ao responder"),
                detail=error or "WhatsApp indisponível",
                request_id=request_id,
                simulation_id=simulation_id,
                consultant_name=consultant_name,
                chat_id=message.chat_id,
            )
        return ok

    def _deliver_result(self, result: SimulationResult) -> Entrega:
        """Entrega o resultado: imagem quando der, texto quando nao der.

        As regras, na ordem em que valem:

        * FALHA DA IMAGEM NAO E' FALHA DO RESULTADO: render, PNG invalido ou
          envio que PROVADAMENTE nao saiu caem para o texto.
        * FALHA DE QUOTE NAO E' FALHA DE RESPOSTA: resolvida na camada, que
          manda sem citacao e com o nome do consultor -- so' quando a API
          recusou a citacao de forma explicita.
        * ENVIO INCERTO NAO GANHA SEGUNDA MENSAGEM: 500, timeout depois de
          enviar, 2xx sem id. A imagem pode ter chegado; texto por cima
          duplicaria. Fica ``unconfirmed`` -- verificar no WhatsApp.
        * Falha que provadamente nao enviou nada fica para o reenvio, no MESMO
          request_id.
        """
        job = result.job
        registro_img: dict = {}
        media_status = "disabled"
        enviado_imagem = False
        if self.config.send_result_image:
            enviado_imagem = self._send_result_image(result, registro=registro_img)
            media_status = registro_img.get("media_status") or (
                "ok" if enviado_imagem else "send_failed")

        if enviado_imagem or registro_img.get("sem_prova"):
            situacao = Delivery.DELIVERED if enviado_imagem else Delivery.UNCONFIRMED
            entrega = self._entrega_de(situacao, registro_img, media_status, "image")
        else:
            if media_status in ("render_failed", "invalid"):
                # A imagem nem chegou a subir: a etapa ainda diz "gerando imagem".
                self._etapa(job, Stage.REPLYING)
            com_citacao, sem_citacao = mensagens.duas_versoes(
                mensagens.texto, result, job.request_id,
                self.config.mask_cpf_in_ui,
                consultor=job.request.consultant_name)
            registro_txt: dict = {}
            enviado = self._send_reply(
                job.message,
                com_citacao,
                texto_sem_citacao=sem_citacao,
                status=Status.COMPLETED if result.ok else Status.ERROR,
                simulation_id=job.simulation_id,
                request_id=job.request_id,
                consultant_name=job.request.consultant_name,
                consultant_id=job.consultant_id,
                registro=registro_txt,
            )
            if enviado:
                situacao = Delivery.DELIVERED
            elif registro_txt.get("sem_prova"):
                situacao = Delivery.UNCONFIRMED
            elif registro_txt.get("transitorio", True):
                situacao = Delivery.RETRYING
            else:
                situacao = Delivery.FAILED
            entrega = self._entrega_de(situacao, registro_txt, media_status, "text")

        self._registrar_entrega(job, result, entrega)
        self._metrics_dirty.set()
        return entrega

    @staticmethod
    def _entrega_de(situacao: str, registro: dict, media_status: str, kind: str,
                    tentativa: int = 1) -> Entrega:
        return Entrega(situacao, motivo=registro.get("motivo", "") or "",
                       enviado_id=registro.get("enviado_id", "") or "",
                       quote_status=registro.get("quote_status", "") or "",
                       media_status=media_status, kind=kind, tentativa=tentativa,
                       desfecho=registro.get("desfecho", "") or "",
                       http_status=int(registro.get("http_status") or 0),
                       quoted_message_id=registro.get("quoted_message_id", "") or "",
                       quote_error=registro.get("quote_error", "") or "")

    def _registrar_entrega(self, job: SimulationJob, result: SimulationResult,
                           entrega: Entrega) -> None:
        """Grava o desfecho da entrega e fecha (ou nao) a solicitacao.

        So' aqui a solicitacao vira `completed`/`error`: depois que a
        resposta saiu com prova. Os outros desfechos tem etapa propria e
        dizem ao painel, sem eufemismo, o que falta.
        """
        status, stage = estado_final(result.ok)
        agora = now_iso()
        consultor = job.request.consultant_name
        campos: dict[str, Any] = {
            "status": status,
            "delivery_status": entrega.status,
            "quote_status": entrega.quote_status,
            "quote_error": entrega.quote_error[:300],
            "media_status": entrega.media_status,
            "updated_at": agora,
        }
        resumo = (f"{entrega.kind or '?'} · citação {entrega.quote_status or '?'} · "
                  f"imagem {entrega.media_status or '?'}")
        evidencia = (f"attempt={entrega.tentativa} origin_message_id="
                     f"{job.message.message_id or '-'} quoted_message_id="
                     f"{entrega.quoted_message_id or '-'} http={entrega.http_status or '-'} "
                     f"delivery_status={entrega.status} quote_status="
                     f"{entrega.quote_status or '-'} erro={entrega.motivo or '-'}")
        comum = dict(request_id=job.request_id, simulation_id=job.simulation_id,
                     consultant_name=consultor, chat_id=job.message.chat_id)
        payload = {"request_id": job.request_id, "simulation_id": job.simulation_id,
                   "origin_message_id": job.message.message_id,
                   "quoted_message_id": entrega.quoted_message_id,
                   "chat_id": job.message.chat_id, "attempt": entrega.tentativa,
                   "delivery_status": entrega.status, "kind": entrega.kind,
                   "desfecho": entrega.desfecho, "http_status": entrega.http_status,
                   "quote_status": entrega.quote_status, "quote_error": entrega.quote_error,
                   "media_status": entrega.media_status,
                   "sent_message_id": entrega.enviado_id, "reason": entrega.motivo}

        if entrega.status == Delivery.DELIVERED:
            campos.update(stage=stage, replied_at=agora, sent_message_id=entrega.enviado_id,
                          delivery_error="", next_delivery_at=None)
            self.log("INFO", "whatsapp",
                     f"{job.request_id}: entregue ({resumo}, id "
                     f"{entrega.enviado_id or 'sem id'}) em resposta a "
                     f"{job.message.message_id or 'mensagem sem id'}.",
                     request_id=job.request_id, consultant=consultor)
            self.hub.publish("request_completed", payload, stage=stage,
                             level="success" if result.ok else "warning",
                             title="Resposta entregue", detail=resumo, **comum)
        elif entrega.status == Delivery.UNCONFIRMED:
            campos.update(stage=Stage.DELIVERY_UNCONFIRMED, sent_message_id="",
                          delivery_error=entrega.motivo[:240])
            self.log("WARNING", "whatsapp",
                     f"{job.request_id}: Entrega incerta — verificar WhatsApp. {evidencia}. "
                     "A primeira pode ter chegado: não reenvio sozinho para não duplicar.",
                     request_id=job.request_id, consultant=consultor)
            self.hub.publish("delivery_unconfirmed", payload, stage=Stage.DELIVERY_UNCONFIRMED,
                             level="warning", title="Entrega incerta — verificar WhatsApp",
                             detail=entrega.motivo, **comum)
        elif entrega.status == Delivery.RETRYING:
            campos.update(stage=Stage.DELIVERY_RETRY, delivery_error=entrega.motivo[:240],
                          next_delivery_at=depois_de(self._espera_do_reenvio(1)))
            # Sem isto o resultado morria aqui: a simulacao aparecia concluida
            # no painel e o consultor nunca era avisado. Um resultado que nao
            # chega vale o mesmo que nao ter simulado.
            self.log("WARNING", "whatsapp",
                     f"{job.request_id}: resultado pronto mas não entregue; nada saiu "
                     f"({evidencia}). Vou tentar reenviar quando o WhatsApp voltar, na "
                     "mesma solicitação.",
                     request_id=job.request_id, consultant=consultor)
            self.hub.publish("delivery_retry", payload, stage=Stage.DELIVERY_RETRY,
                             level="warning", title="Reenvio pendente",
                             detail=entrega.motivo, **comum)
        else:
            campos.update(stage=Stage.DELIVERY_FAILED, delivery_error=entrega.motivo[:240])
            self.log("ERROR", "whatsapp",
                     f"{job.request_id}: a entrega falhou de um jeito que repetir não "
                     f"resolve ({evidencia}). O resultado está no painel.",
                     request_id=job.request_id, consultant=consultor)
            self.hub.publish("delivery_failed", payload, stage=Stage.DELIVERY_FAILED,
                             level="error", title="Entrega falhou",
                             detail=entrega.motivo, **comum)
        self.db.update("simulations", campos, {"id": job.simulation_id})

    def _print_do_portal(self, result: SimulationResult, provisorio: Path) -> bool:
        """Copia o print do portal para ``provisorio``. False = usar o card.

        O PRINT DO PORTAL vem na frente do card quando ele existe.

        O consultor confia no que ele mesmo veria na tela do banco. O card e'
        a nossa transcricao dela: se a leitura errar um campo, o erro chega
        bonito e indistinguivel de um acerto. O recorte se descarta sozinho
        quando pega o topo da pagina (nome do operador, empresa), e ai' cai
        para o card -- ver `app/tela_do_portal.py`.

        O print passa pela MESMA conferencia do card: um PNG truncado cai
        para o card, nunca vai quebrado para o grupo.
        """
        job = result.job
        if not self.config.image_show_client_data:
            # O print e' a TELA do banco: nome e CPF do cliente vao como
            # pixels, e nao ha' como mascara-los. ``IMAGE_SHOW_CLIENT_DATA=false``
            # vale para a imagem inteira -- entao sai o card, que mascara.
            return False
        do_portal = Path(result.portal_png) if result.portal_png else None
        if not (self.config.imagem_da_resposta == "portal"
                and do_portal and do_portal.is_file()):
            return False
        try:
            shutil.copyfile(do_portal, provisorio)
            validar_png(provisorio)
            return True
        except (OSError, PngInvalido) as exc:
            self.log(
                "WARNING", "whatsapp",
                f"O print do portal não serve ({_curto(exc)}). Usando o card.",
                request_id=job.request_id, consultant=job.request.consultant_name,
            )
            return False

    def _gerar_imagem(self, result: SimulationResult, registro: dict) -> Path | None:
        """Print do portal ou HTML -> PNG provisorio -> validado -> ``<request_id>.png``.

        O nome e' deterministico (o painel acha o comprovante pelo REQ), mas a
        renderizacao escreve num arquivo provisorio unico e so' ele, validado,
        substitui o final. Um render que falha no meio nunca deixa um PNG
        velho -- de uma tentativa anterior -- passando por imagem desta.
        """
        job = result.job
        pasta = Path(self.comprovantes_dir)
        destino = pasta / f"{job.request_id}.png"
        provisorio = pasta / f"{job.request_id}.{uuid.uuid4().hex[:10]}.tmp.png"
        try:
            pasta.mkdir(parents=True, exist_ok=True)
            if not self._print_do_portal(result, provisorio):
                html = build_result_html(
                    result,
                    job.request_id,
                    show_client_data=self.config.image_show_client_data,
                    tz=self.config.tz,
                )
                self.whatsapp.render_png(html, provisorio)
            validar_png(provisorio)
            os.replace(provisorio, destino)
            validar_png(destino, request_id=job.request_id, pasta=pasta)
            return destino
        except PngInvalido as exc:
            registro["media_status"] = "invalid"
            self.log(
                "WARNING", "whatsapp",
                f"A imagem gerada é inválida ({exc}). Respondendo em texto.",
                request_id=job.request_id, consultant=job.request.consultant_name,
            )
        except Exception as exc:
            registro["media_status"] = "render_failed"
            self.log(
                "WARNING", "whatsapp",
                f"Não consegui gerar a imagem do resultado ({_curto(exc)}). Respondendo em texto.",
                request_id=job.request_id, consultant=job.request.consultant_name,
            )
        finally:
            try:
                provisorio.unlink(missing_ok=True)
            except OSError:
                pass
        return None

    def _send_result_image(self, result: SimulationResult,
                           registro: dict | None = None, tentativa: int = 1) -> bool:
        """Responde com a imagem dos cards + quanto libera.

        False quando a imagem nao saiu COM PROVA. Quem chama olha ``registro``:
        so' cai para texto se ``sem_prova`` for falso -- ou seja, se a imagem
        provadamente nao saiu. O consultor nunca fica sem resposta por causa
        da imagem, e nunca recebe duas por causa da incerteza.
        """
        registro = registro if registro is not None else {}
        job = result.job
        consultant = job.request.consultant_name

        self._etapa(job, Stage.RENDERING)
        destino = self._gerar_imagem(result, registro)
        if destino is None:
            return False
        self._etapa(job, Stage.REPLYING)

        com_citacao, sem_citacao = mensagens.duas_versoes(
            mensagens.legenda, result, job.request_id, consultor=consultant)
        # ANTES da chamada: se o processo cair durante o envio, esta linha e' a
        # prova de que a imagem PODE ter saido.
        linha_id = self.db.insert("messages", {
            "simulation_id": job.simulation_id,
            "request_id": job.request_id,
            "direction": "out",
            "kind": "image",
            "chat_id": job.message.chat_id,
            "chat_name": job.message.chat_name,
            "consultant_id": job.consultant_id,
            "consultant_name": consultant,
            "sender_id": job.message.sender_id,
            "text": com_citacao,
            "media_path": str(destino),
            "status": ENVIO_EM_CURSO,
            "provider": self.config.whatsapp_mode,
            "attempt": tentativa,
            "origin_message_id": job.message.message_id,
            "created_at": now_iso(),
        })

        # As duas camadas avisam de falha de jeitos diferentes: a do navegador
        # LEVANTA excecao, a da Evolution DEVOLVE ok=False (ela tem a resposta
        # HTTP para explicar o motivo, e nao ha' por que transformar isso em
        # excecao). Tratar so' um dos dois faria a queda para texto -- a rede
        # de seguranca que garante resposta ao consultor -- parar de funcionar
        # justamente na camada nova.
        envio = None
        try:
            envio = self.whatsapp.send_image(
                chat_id=job.message.chat_id,
                chat_name=job.message.chat_name or self.config.whatsapp_group_name,
                image_path=destino,
                caption=com_citacao,
                caption_sem_citacao=sem_citacao,
                quote_message_id=job.message.message_id,
                quote_text=job.message.text,
                quote_participant=job.message.participant,
            )
        except Exception as exc:
            motivo = _curto(exc)
        else:
            motivo = "" if envio else (getattr(envio, "motivo", "") or "a camada não explicou")

        evid = self._evidencia(envio, motivo, job.message)
        registro.update(evid, ok=bool(envio))
        # A legenda que REALMENTE saiu: com citação sai a curta, sem citação
        # sai a que leva o nome do consultor. Gravar a versão errada faria o
        # painel discordar do grupo.
        saiu = self._versao_que_saiu(envio, com_citacao, sem_citacao)
        self._log_de_envio("imagem", job.request_id, job.message, evid, tentativa,
                           bool(envio), consultant)
        final = {
            "text": saiu,
            "wa_message_id": evid["enviado_id"],
            "provider": evid["provider"],
            "desfecho": evid["desfecho"],
            "quoted_message_id": evid["quoted_message_id"],
            "quote_status": evid["quote_status"],
            "quote_error": evid["quote_error"][:300],
            "http_status": evid["http_status"] or None,
            "media_id": evid["media_id"],
            "error": motivo[:240],
            "response_excerpt": evid["resposta"],
        }

        if not envio:
            registro["media_status"] = "unconfirmed" if evid["sem_prova"] else "send_failed"
            self.db.update("messages", {**final, "status": self._status_da_mensagem(
                False, evid, "")}, {"id": linha_id})
            if evid["sem_prova"]:
                self.log(
                    "WARNING", "whatsapp",
                    f"{job.request_id}: a imagem pode ter saído e não há prova ({motivo}). "
                    "Não mando o texto por cima: duplicaria a resposta.",
                    request_id=job.request_id, consultant=consultant,
                )
            else:
                self.log(
                    "WARNING", "whatsapp",
                    f"A imagem não saiu ({motivo}). Respondendo em texto.",
                    request_id=job.request_id, consultant=consultant,
                )
            return False

        registro["media_status"] = "ok"
        self._anotar_citacao(envio, job.request_id, consultant)
        if getattr(envio, "parcial", False):
            # Nao deveria acontecer -- a camada aborta o envio mudo e cai para
            # texto. Se chegar aqui, o painel precisa mostrar, senao volta a
            # ser um defeito invisivel.
            self.log(
                "WARNING", "whatsapp",
                f"{job.request_id}: card entregue SEM legenda (entrega parcial). "
                "O consultor recebeu a imagem sem o valor escrito nem o REQ.",
                request_id=job.request_id, consultant=consultant,
            )
        sent_total = self.db.bump_meta("wa_sent", 1)
        self.db.update(
            "messages",
            {
                **final,
                # "partial" quando o card foi sem a legenda: o consultor
                # recebeu a imagem, mas sem o valor escrito e sem o REQ.
                "status": ("partial" if getattr(envio, "parcial", False)
                           else (Status.COMPLETED if result.ok else Status.ERROR)),
            },
            {"id": linha_id},
        )
        self.hub.publish(
            "message_sent",
            {
                "request_id": job.request_id,
                "simulation_id": job.simulation_id,
                "consultant": consultant,
                "chat_id": job.message.chat_id,
                "text": redact(saiu),
                "kind": "image",
                "media": f"/api/comprovantes/{job.request_id}.png",
                "origin_message_id": job.message.message_id,
                "quote_status": evid["quote_status"],
                "sent_message_id": evid["enviado_id"],
                "media_id": evid["media_id"],
                "attempt": tentativa,
                "sent_total": sent_total,
            },
            stage=Stage.REPLYING,
            title="Resultado enviado (imagem)",
            detail=consultant,
            request_id=job.request_id,
            simulation_id=job.simulation_id,
            consultant_name=consultant,
            chat_id=job.message.chat_id,
        )
        return True

    # ---------------------------------------------------------------- status
    def _on_whatsapp_status(self, status: WhatsAppStatus) -> None:
        """Reflete o estado no painel e registra apenas as TRANSIÇÕES.

        O evento gravado ("conectou", "caiu") é uma linha do histórico
        operacional, não um batimento. Sem esta guarda, qualquer alteração de
        status com a conexão de pé virava um "WhatsApp conectado" novo no
        monitor — e como o polling atualiza o status a cada ciclo, a tela
        enchia de linhas repetidas a cada poucos segundos.
        """
        payload = self.whatsapp_status_payload(status)
        # O estado corrente vai sempre: é o que pinta o indicador do topo.
        self.hub.publish("whatsapp_status", payload, persist=False)

        conectado = status.connected
        anterior = self._wa_conectado
        if conectado == anterior:
            return
        self._wa_conectado = conectado

        if conectado:
            self.db.set_meta("wa_online_since", status.since or now_iso())
            self.hub.publish(
                "whatsapp_connected",
                payload,
                level="success",
                title="WhatsApp conectado",
                detail=status.chat_name or status.phone or "",
            )
        elif anterior:
            # Só anuncia queda para quem estava de pé — evita anunciar
            # "desconectado" durante a primeira tentativa de conexão.
            self.hub.publish(
                "whatsapp_disconnected",
                payload,
                level="warning",
                title="WhatsApp desconectado",
                detail=status.last_error or "conexão perdida",
            )

    def whatsapp_status_payload(self, status: WhatsAppStatus | None = None) -> dict:
        status = status or self.whatsapp.status
        return {
            **status.as_dict(),
            "group_name": self.config.whatsapp_group_name,
            "received": self.db.get_meta_int("wa_received"),
            "sent": self.db.get_meta_int("wa_sent"),
            "online_since": status.since or self.db.get_meta("wa_online_since", ""),
            # Qual camada esta no ar. Sem isto, quem olha o painel nao tem como
            # saber se o bot esta raspando a tela ou falando com a API -- e as
            # duas falham de jeitos bem diferentes.
            "mode": self.config.whatsapp_mode,
            "instance": (self.config.evolution_instance
                         if self.config.whatsapp_mode == MODO_EVOLUTION else ""),
            **self.diagnostico_do_whatsapp(),
        }

    def diagnostico_do_whatsapp(self) -> dict:
        """Por que o bot esta' ligado e nao responde?

        Responde as perguntas na ordem em que elas travam o fluxo: a Evolution
        responde? a chave vale? a instancia existe? esta' conectada? o webhook
        aponta para ca'? o grupo esta' configurado? e quando chegou a ultima
        mensagem por ele? Nenhum segredo entra aqui -- so' booleanos, estados
        e horarios.
        """
        if self.config.whatsapp_mode != MODO_EVOLUTION:
            return {"evolution": None, "last_webhook_at": ""}
        diagnostico = {}
        ler = getattr(self.whatsapp, "diagnostico", None)
        if callable(ler):
            try:
                diagnostico = dict(ler() or {})
            except Exception as exc:
                diagnostico = {"erro": _curto(exc)}
        diagnostico.setdefault("group_configured", bool(self.config.evolution_group_jid))
        diagnostico["webhook_token_configured"] = bool(self.config.evolution_webhook_token)
        ultimo = self.db.get_meta("wa_last_webhook_at", "")
        diagnostico["last_webhook_at"] = ultimo
        return {"evolution": diagnostico, "last_webhook_at": ultimo}

    def registrar_webhook(self) -> None:
        """Marca que a Evolution ALCANCOU o bot agora.

        E' o unico sinal que prova o caminho de volta inteiro (Evolution ->
        rede -> nossa rota). "Instancia conectada" nao prova isso.
        """
        try:
            self.db.set_meta("wa_last_webhook_at", now_iso())
        except Exception:
            pass

    def system_status_payload(self) -> dict:
        return {
            "started_at": self._started_at,
            "whatsapp": self.whatsapp_status_payload(),
            "simulators": [s.status() for s in self.simulators],
            "queue_depth": self.queue.depth(),
            "active_jobs": self.queue.active,
            "worker_count": self.config.worker_count,
            "group_name": self.config.whatsapp_group_name,
            "supported_banks": sorted(b.title() for b in self.config.supported_banks),
            "simulator_path": str(self.config.sim_bot_path),
            "simulator_mode": self.config.simulator_mode,
            "timezone": self.config.timezone,
            "mask_cpf": self.config.mask_cpf_in_ui,
        }

    def reconnect_whatsapp(self) -> None:
        self.log("INFO", "whatsapp", "Reconexão solicitada pelo painel.")
        self.whatsapp.request_reconnect()

    def qr_data_url(self) -> str:
        return self.whatsapp.qr_data_url()

    # --------------------------------------------------------------- metricas
    def _metrics_loop(self) -> None:
        last_push = 0.0
        while not self._stop.is_set():
            triggered = self._metrics_dirty.wait(timeout=5.0)
            if self._stop.is_set():
                return
            elapsed = time.monotonic() - last_push
            if triggered and elapsed < METRICS_MIN_INTERVAL:
                time.sleep(METRICS_MIN_INTERVAL - elapsed)
            self._metrics_dirty.clear()
            last_push = time.monotonic()
            try:
                self.hub.publish("metrics", self.metrics_snapshot(), persist=False)
                self.hub.publish(
                    "queue_update",
                    {"items": self.queue.snapshot(), "depth": self.queue.depth()},
                    persist=False,
                )
            except Exception as exc:
                self.log("ERROR", "sistema", f"Falha ao calcular métricas: {exc}")

    def touch_metrics(self) -> None:
        self._metrics_dirty.set()

    def metrics_snapshot(self) -> dict[str, Any]:
        return analytics.dashboard_snapshot(self.db, self.config.tz)

    def queue_snapshot(self) -> dict[str, Any]:
        # `pendencias` entra aqui porque o painel e' o unico lugar onde
        # alguem percebe que algo travou sem ir olhar o grupo.
        return {"items": self.queue.snapshot(), "depth": self.queue.depth(),
                **self.queue.pendencias()}

    def stage_labels(self) -> dict[str, str]:
        return dict(STAGE_LABELS)
