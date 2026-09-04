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

import threading
import time
from dataclasses import replace
from typing import Any, NamedTuple

from . import analytics, mensagens
from .cards import build_result_html
from .clock import iso_atras, now_iso
from .config import ROOT, Config
from .consultants import ConsultantRepository
from .db import Database
from .events import EventHub
from .jobs import QueueService
from .models import (
    ParsedRequest,
    STAGE_LABELS,
    IncomingMessage,
    SimulationJob,
    SimulationResult,
    Stage,
    Status,
)
from .parser import parse_request
from .remote import RemoteSimulator
from .security import redact
from .simulator import SimulatorService
from .state_store import StateStore
from .evolution import EvolutionClient
from .whatsapp import WhatsAppService, WhatsAppStatus
from .whatsapp_port import MODO_EVOLUTION

METRICS_MIN_INTERVAL = 2.0

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
    @staticmethod
    def _prova_de_entrega(resultado) -> str:
        """O id que a camada devolveu como prova de que a mensagem saiu.

        No modo evolution e' o ``key.id`` da resposta HTTP -- prova de que a
        Evolution aceitou, e a chave para achar a mensagem depois. No modo dom
        nao ha' equivalente: a camada antiga so' devolve um booleano, e nunca
        soube dizer QUAL mensagem escreveu. Guardar quando existe e aceitar
        vazio quando nao existe mantem os dois modos no mesmo caminho.
        """
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
        # INTERROMPIDA entra junto de propósito: ela terminou (travou), o
        # consultor está esperando, e sem isto ficaria invisível — o painel
        # mostrava "Interrompido" e o grupo não recebia nada.
        #
        # So' o que terminou ha' mais de _CARENCIA_REENVIO segundos.
        #
        # O status vira "completed" ANTES de a resposta sair, e o envio da
        # imagem leva quase um minuto. Sem esta carencia, a varredura pegava
        # entregas ainda em curso e mandava tudo duas vezes -- o consultor
        # recebia o resultado e logo em seguida o mesmo resultado marcado
        # como "(reenvio)".
        limite = iso_atras(self._CARENCIA_REENVIO)
        pendentes = self.db.fetchall(
            "SELECT * FROM simulations "
            " WHERE replied_at IS NULL "
            "   AND status IN (?, ?, ?) "
            "   AND (reply_attempts IS NULL OR reply_attempts < ?) "
            "   AND COALESCE(finished_at, updated_at, created_at) < ? "
            " ORDER BY id LIMIT 5",
            (Status.COMPLETED, Status.ERROR, Status.INTERRUPTED,
             self._MAX_REENVIOS, limite),
        )
        for linha in pendentes:
            self._reenviar_um(dict(linha))

    def _reenviar_um(self, linha: dict) -> None:
        tentativas = int(linha.get("reply_attempts") or 0) + 1
        request_id = linha.get("request_id") or "?"

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
                    {"replied_at": now_iso(), "updated_at": now_iso()},
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

        texto = mensagens.resultado_reenviado(linha, self.config.mask_cpf_in_ui)
        enviado = self._send_reply(
            IncomingMessage(
                message_id=linha.get("source_message_id") or "",
                chat_id=linha.get("chat_id") or "",
                chat_name=self.config.whatsapp_group_name,
                sender_id=linha.get("sender_id") or "",
                sender_name=linha.get("consultant_name") or "Consultor",
                text="",
            ),
            texto,
            status=linha.get("status") or Status.COMPLETED,
            simulation_id=int(linha.get("id") or 0),
            request_id=request_id,
            consultant_name=linha.get("consultant_name") or "",
            consultant_id=linha.get("consultant_id"),
        )
        campos = {"reply_attempts": tentativas, "updated_at": now_iso()}
        if enviado:
            campos["replied_at"] = now_iso()
            self.log("INFO", "whatsapp",
                     f"{request_id}: resultado reenviado com sucesso "
                     f"(tentativa {tentativas}).",
                     request_id=request_id)
        elif tentativas >= self._MAX_REENVIOS:
            self.log("ERROR", "whatsapp",
                     f"{request_id}: desisti de entregar depois de {tentativas} "
                     "tentativas. O resultado continua no painel.",
                     request_id=request_id)
        self.db.update("simulations", campos, {"id": linha.get("id")})

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
                self.log("ERROR", "bot", f"Falha ao tratar mensagem: {exc}")

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

    def _identificar_e_registrar(self, message: IncomingMessage) -> Consultor:
        """Quem mandou, e o registro de que a mensagem chegou.

        Junta duas coisas que sempre andam juntas: descobrir o consultor pelo
        telefone e gravar/publicar o recebimento. Separa-las renderia duas
        funcoes que nunca sao chamadas em separado.
        """
        received = self.db.bump_meta("wa_received", 1)
        cadastro = self.consultants.resolve(message.sender_id, message.sender_name)
        consultor_id = cadastro.get("id")
        consultor_nome = cadastro.get("name") or message.sender_name or "Consultor"

        self.db.insert(
            "messages",
            {
                "direction": "in",
                "kind": "text",
                "chat_id": message.chat_id,
                "wa_message_id": message.message_id,
                "consultant_id": consultor_id,
                "consultant_name": consultor_nome,
                "sender_id": message.sender_id,
                "text": message.text,
                "status": Stage.RECEIVED,
                "created_at": now_iso(),
            },
        )
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
                "text": message.text,
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

    def _gravar_solicitacao(self, message: IncomingMessage, consultor: "Consultor",
                            parsed: ParsedRequest, effective_name: str) -> tuple[str, int]:
        """Grava a solicitacao e devolve (request_id, simulation_id).

        O ``request_id`` nasce aqui e acompanha a solicitacao ate' a resposta:
        e' ele que liga a mensagem do WhatsApp, a linha do banco, os eventos
        do painel e a imagem enviada. Nao mudar essa amarracao.
        """
        request_id = self._next_request_id()
        stamp = now_iso()
        simulation_id = self.db.insert(
            "simulations",
            {
                "request_id": request_id,
                "consultant_id": consultor.id,
                "consultant_name": effective_name,
                "chat_id": message.chat_id,
                "sender_id": message.sender_id,
                "sender_name": message.sender_name,
                "source_message_id": message.message_id,
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
            },
        )
        self.db.update(
            "messages",
            {"simulation_id": simulation_id, "request_id": request_id},
            {"wa_message_id": message.message_id},
        )
        self.hub.publish(
            "request_created",
            {
                "request_id": request_id,
                "simulation_id": simulation_id,
                "consultant": effective_name,
                "bank": parsed.bank,
                "contract": parsed.contract,
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

        consultor = self._identificar_e_registrar(message)

        parsed, missing = parse_request(
            message.text,
            fallback_consultant=consultor.nome,
            require_trigger=self.config.require_trigger,
            default_bank=self.config.default_bank,
        )

        if parsed is None and not missing:
            return  # conversa normal do grupo, nao e' um pedido

        if missing:
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
            self._recusar(
                message, consultor._replace(nome=effective_name),
                mensagens.banco_nao_atendido(
                    effective_name, parsed.bank, self.config.supported_banks),
                etiqueta="unsupported", motivo="banco não suportado",
                titulo="Banco não suportado",
                detalhe=f"{effective_name} · {parsed.bank}",
                extra={"bank": parsed.bank})
            return


        request_id, simulation_id = self._gravar_solicitacao(
            message, consultor, parsed, effective_name)
        self._enfileirar(message, consultor, parsed, effective_name,
                         request_id, simulation_id)

    def _next_request_id(self) -> str:
        return f"REQ{self.db.bump_meta('request_seq', 1):06d}"

    # -------------------------------------------------------------- saida
    def _anotar_citacao(self, resultado, request_id: str, consultor: str) -> None:
        """Registra quando a resposta saiu SOLTA, sem citar o pedido.

        Nao e' so' log: e' o unico sinal de que a citacao parou de funcionar.
        Ela falhar nao segura a resposta -- o consultor recebe do mesmo jeito,
        com o nome dele no fim -- entao sem esta linha o defeito voltaria a
        ser invisivel, que e' exatamente como ele durou tanto.
        """
        if getattr(resultado, "quoted_ok", None) is False:
            self.log(
                "WARNING", "whatsapp",
                "Resposta enviada SEM citação (segue com o nome do consultor "
                "no fim). Se isto se repetir, o menu do WhatsApp mudou.",
                request_id=request_id, consultant=consultor,
            )

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
    ) -> bool:
        ok = False
        error = ""
        prova = ""
        try:
            resultado = self.whatsapp.send(
                chat_id=message.chat_id,
                chat_name=message.chat_name or self.config.whatsapp_group_name,
                text=text,
                texto_sem_citacao=texto_sem_citacao,
                quote_message_id=message.message_id,
            )
            ok = bool(resultado)
            prova = self._prova_de_entrega(resultado)
            self._anotar_citacao(resultado, request_id, consultant_name)
        except Exception as exc:
            error = str(exc)[:240]
            self.log(
                "ERROR", "whatsapp",
                f"Falha ao enviar resposta: {error}",
                request_id=request_id, consultant=consultant_name,
            )

        self.db.insert(
            "messages",
            {
                "simulation_id": simulation_id,
                "request_id": request_id,
                "direction": "out",
                "kind": "text",
                "chat_id": message.chat_id,
                "consultant_id": consultant_id,
                "consultant_name": consultant_name,
                "sender_id": message.sender_id,
                "text": text,
                "wa_message_id": prova,
                "status": status if ok else "failed",
                "created_at": now_iso(),
            },
        )
        if ok:
            sent = self.db.bump_meta("wa_sent", 1)
            self.hub.publish(
                "message_sent",
                {
                    "request_id": request_id,
                    "simulation_id": simulation_id,
                    "consultant": consultant_name,
                    "chat_id": message.chat_id,
                    "text": text,
                    "status": status,
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
            self.hub.publish(
                "message_failed",
                {
                    "request_id": request_id,
                    "simulation_id": simulation_id,
                    "consultant": consultant_name,
                    "error": error or "WhatsApp indisponível",
                },
                stage=Stage.ERROR,
                level="error",
                title="Falha ao responder",
                detail=error or "WhatsApp indisponível",
                request_id=request_id,
                simulation_id=simulation_id,
                consultant_name=consultant_name,
                chat_id=message.chat_id,
            )
        return ok

    def _deliver_result(self, result: SimulationResult) -> None:
        job = result.job
        sent = False

        if self.config.send_result_image:
            sent = self._send_result_image(result)

        if not sent:
            com_citacao, sem_citacao = mensagens.duas_versoes(
                mensagens.texto, result, job.request_id,
                self.config.mask_cpf_in_ui,
                consultor=job.request.consultant_name)
            sent = self._send_reply(
                job.message,
                com_citacao,
                texto_sem_citacao=sem_citacao,
                status=Status.COMPLETED if result.ok else Status.ERROR,
                simulation_id=job.simulation_id,
                request_id=job.request_id,
                consultant_name=job.request.consultant_name,
                consultant_id=job.consultant_id,
            )
        if sent:
            self.db.update(
                "simulations",
                {"replied_at": now_iso(), "updated_at": now_iso()},
                {"id": job.simulation_id},
            )
        else:
            # Sem isto o resultado morria aqui: a simulacao aparecia concluida
            # no painel e o consultor nunca era avisado. Um resultado que nao
            # chega vale o mesmo que nao ter simulado. Fica pendente e o laco
            # de reenvio tenta de novo quando o WhatsApp voltar.
            self.log(
                "WARNING", "whatsapp",
                f"{job.request_id}: resultado pronto mas não entregue. "
                "Vou tentar reenviar quando o WhatsApp voltar.",
                request_id=job.request_id,
                consultant=job.request.consultant_name,
            )
        self._metrics_dirty.set()

    def _send_result_image(self, result: SimulationResult) -> bool:
        """Responde com a imagem dos cards + quanto libera.

        Qualquer falha aqui volta False e o chamador cai para a resposta em
        texto. O consultor nunca fica sem resposta por causa da imagem.
        """
        job = result.job
        consultant = job.request.consultant_name
        destino = COMPROVANTES_DIR / f"{job.request_id}.png"

        try:
            html = build_result_html(
                result,
                job.request_id,
                show_client_data=self.config.image_show_client_data,
                tz=self.config.tz,
            )
            self.whatsapp.render_png(html, destino)
        except Exception as exc:
            self.log(
                "WARNING", "whatsapp",
                f"Não consegui gerar a imagem do resultado ({_curto(exc)}). Respondendo em texto.",
                request_id=job.request_id, consultant=consultant,
            )
            return False

        # As duas camadas avisam de falha de jeitos diferentes: a do navegador
        # LEVANTA excecao, a da Evolution DEVOLVE ok=False (ela tem a resposta
        # HTTP para explicar o motivo, e nao ha' por que transformar isso em
        # excecao). Tratar so' um dos dois faria a queda para texto -- a rede
        # de seguranca que garante resposta ao consultor -- parar de funcionar
        # justamente na camada nova.
        com_citacao, sem_citacao = mensagens.duas_versoes(
            mensagens.legenda, result, job.request_id, consultor=consultant)
        try:
            envio = self.whatsapp.send_image(
                chat_id=job.message.chat_id,
                chat_name=job.message.chat_name or self.config.whatsapp_group_name,
                image_path=destino,
                caption=com_citacao,
                caption_sem_citacao=sem_citacao,
                quote_message_id=job.message.message_id,
            )
        except Exception as exc:
            envio = None
            motivo = _curto(exc)
        else:
            motivo = getattr(envio, "motivo", "") or "a camada não explicou"

        if not envio:
            self.log(
                "WARNING", "whatsapp",
                f"Falha ao enviar a imagem ({motivo}). Respondendo em texto.",
                request_id=job.request_id, consultant=consultant,
            )
            return False

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
        self.db.insert(
            "messages",
            {
                "simulation_id": job.simulation_id,
                "request_id": job.request_id,
                "direction": "out",
                "kind": "image",
                "wa_message_id": self._prova_de_entrega(envio),
                "chat_id": job.message.chat_id,
                "consultant_id": job.consultant_id,
                "consultant_name": consultant,
                "sender_id": job.message.sender_id,
                # A legenda que REALMENTE saiu: com citação sai a curta, sem
                # citação sai a que leva o nome do consultor. Gravar a versão
                # errada faria o painel discordar do grupo.
                "text": sem_citacao if getattr(envio, "quoted_ok", True) is False
                        else com_citacao,
                "media_path": str(destino),
                # "partial" quando o card foi sem a legenda: o consultor
                # recebeu a imagem, mas sem o valor escrito e sem o REQ.
                "status": ("partial" if getattr(envio, "parcial", False)
                           else (Status.COMPLETED if result.ok else Status.ERROR)),
                "created_at": now_iso(),
            },
        )
        self.hub.publish(
            "message_sent",
            {
                "request_id": job.request_id,
                "simulation_id": job.simulation_id,
                "consultant": consultant,
                "chat_id": job.message.chat_id,
                "text": mensagens.legenda(result, job.request_id),
                "kind": "image",
                "media": f"/api/comprovantes/{job.request_id}.png",
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
        }

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
