"""A Evolution v2.3.7 como ela é -- formatos tirados do código-fonte da tag.

Fonte: github.com/EvolutionAPI/evolution-api, tag ``2.3.7`` (Baileys
``7.0.0-rc.9``), a mesma fixada no ``docker-compose.yml``:

* resposta de ``sendText``/``sendMedia``: ``prepareMessage(messageSent)`` em
  ``whatsapp.baileys.service.ts`` -- ``contextInfo`` no TOPO; no texto o
  ``extendedTextMessage`` vira ``message.conversation``;
* ACK: o handler ``messages.update`` do mesmo arquivo manda ``data`` PLANO,
  ``{keyId, remoteJid, fromMe, participant, status, instanceId}``;
* envelope do webhook: ``webhook.controller.ts`` (``emit``) -- traz ``apikey``
  e ``server_url`` em TODO corpo;
* pedido recebido: ``prepareMessage(received)`` -- ``key`` como veio do
  Baileys, texto encaminhado em ``message.conversation``.

**Nenhum teste aqui fala com uma Evolution de verdade.** Eles provam que o bot
lê e escreve os formatos da v2.3.7. A validação real continua pendente até
existir uma Evolution rodando (ver ``ROTEIRO-TESTE-REAL.md``).
"""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.evolution import EvolutionClient
from app.evolution_webhook import interpretar, interpretar_todos
from app.events import EventHub
from app.manager import BotManager
from app.models import Desfecho, QuoteStatus
from app.web import create_app
from ferramentas.e2e_simulado import resposta_v237
from tests.test_concurrency import FakeWhatsApp, _config

GRUPO = "120363111222333@g.us"
OUTRO_GRUPO = "120363999888777@g.us"
CONSULTOR = "5567999990001@s.whatsapp.net"
CONSULTOR_LID = "112233445566778@lid"
TOKEN = "token-do-webhook-de-teste-v237"
#: O envelope do webhook da v2.3.7 carrega a chave da instância. Nunca pode
#: parar em log, evento ou resposta.
CHAVE_NO_ENVELOPE = "chave-da-instancia-que-nao-pode-ir-para-log-9f8e7d"
SERVIDOR_NO_ENVELOPE = "http://evolution-interna:8080"
CABECALHO = {"X-Webhook-Token": TOKEN}
PNG_1X1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PEDIDO = "Ivone Teste\n42888832453\nSantander"


# ----------------------------------------------------------------- formatos
def envelope(evento: str, data) -> dict:
    """``webhook.controller.ts``, ``emit``: o que chega no POST do webhook."""
    return {"event": evento, "instance": "allana", "data": data,
            "destination": "http://bot:8000/webhook/whatsapp",
            "date_time": "2026-09-21T10:00:00.000Z",
            "sender": "5562900000000@s.whatsapp.net",
            "server_url": SERVIDOR_NO_ENVELOPE, "apikey": CHAVE_NO_ENVELOPE}


def upsert(texto: str = PEDIDO, *, id_msg: str, participante: str = CONSULTOR,
           grupo: str = GRUPO, de_mim: bool = False, alternativo: str = "",
           encaminhada: bool = True, push: str = "Consultor") -> dict:
    """``messages.upsert`` como ``prepareMessage(received)`` monta.

    Os pedidos do grupo chegam ENCAMINHADOS: o WhatsApp manda
    ``extendedTextMessage`` com ``isForwarded``, e o ``prepareMessage`` o
    converte em ``message.conversation``, com o ``contextInfo`` no topo.
    """
    key = {"remoteJid": grupo, "fromMe": de_mim, "id": id_msg,
           "participant": participante}
    if alternativo:
        key["participantAlt"] = alternativo
    data = {"key": key, "pushName": push, "status": "DELIVERY_ACK",
            "message": {"conversation": texto}, "messageType": "conversation",
            "messageTimestamp": 1758450000,
            "instanceId": "00000000-0000-4000-8000-000000000237", "source": "android"}
    if encaminhada:
        data["contextInfo"] = {"forwardingScore": 1, "isForwarded": True}
    return envelope("messages.upsert", data)


def ack(key_id: str, status: str, *, chat: str = GRUPO, participante: str = "",
        de_mim: bool = True) -> dict:
    """``messages.update`` da v2.3.7: ``data`` PLANO, status já em nome."""
    data = {"keyId": key_id, "remoteJid": chat, "fromMe": de_mim, "status": status,
            "instanceId": "00000000-0000-4000-8000-000000000237"}
    if participante:
        data["participant"] = participante
    return envelope("messages.update", data)


def ack_legado(key_id: str, status: str, *, chat: str = GRUPO) -> dict:
    """O formato que os testes antigos usavam: o evento cru do Baileys."""
    return {"event": "messages.update",
            "data": [{"key": {"id": key_id, "remoteJid": chat},
                      "update": {"status": status}}]}


# ================================================ a Evolution falsa realista
class TestARespostaFalsaTemOFormatoReal:
    """Trava ``resposta_v237``: ela é a referência do E2E e destes testes.

    Se alguém a "simplificar" de volta para o ``stanzaId`` dentro de
    ``extendedTextMessage``, os testes de citação passariam contra um formato
    que a v2.3.7 não devolve. Estes falham antes.
    """

    PEDIDO_QUOTED = {"key": {"id": "3EB0ORIGEM", "fromMe": False, "remoteJid": GRUPO,
                             "participant": CONSULTOR},
                     "message": {"conversation": PEDIDO}}

    def test_texto_citado(self):
        r = resposta_v237({"number": GRUPO, "text": "resp", "quoted": self.PEDIDO_QUOTED},
                          "3EB0RESP", imagem=False)
        assert r["message"] == {"conversation": "resp"}, "texto é conversation na v2.3.7"
        assert "extendedTextMessage" not in json.dumps(r)
        assert r["contextInfo"]["stanzaId"] == "3EB0ORIGEM"
        assert r["contextInfo"]["participant"] == CONSULTOR
        assert r["messageType"] == "conversation" and r["key"]["id"] == "3EB0RESP"

    def test_imagem_citada(self):
        r = resposta_v237({"number": GRUPO, "caption": "c", "quoted": self.PEDIDO_QUOTED},
                          "3EB0RESP", imagem=True)
        assert r["contextInfo"]["stanzaId"] == "3EB0ORIGEM"
        assert r["message"]["imageMessage"]["contextInfo"]["stanzaId"] == "3EB0ORIGEM"
        assert r["messageType"] == "imageMessage"

    def test_sem_citacao_nao_ha_context_info(self):
        r = resposta_v237({"number": GRUPO, "text": "resp"}, "3EB0RESP", imagem=False)
        assert "contextInfo" not in r


# ============================================== citação contra a resposta real
class Servidor:
    """Evolution falsa: responde com o formato da v2.3.7, montado a partir do
    que o bot REALMENTE mandou. Assim o ``stanzaId`` da resposta nasce do
    ``quoted`` enviado -- o caminho inteiro, não um valor colado no teste."""

    def __init__(self, *, trocar_stanza: str = "", sem_stanza: bool = False,
                 status: int = 0, levanta: Exception | None = None):
        self.chamadas: list[tuple[str, dict]] = []
        self.trocar_stanza = trocar_stanza
        self.sem_stanza = sem_stanza
        self.status = status
        self.levanta = levanta

    def __call__(self, request: httpx.Request) -> httpx.Response:
        corpo = json.loads(request.content) if request.content else {}
        self.chamadas.append((request.url.path, corpo))
        if self.levanta is not None:
            raise self.levanta
        if self.status:
            return httpx.Response(self.status, json={"status": self.status,
                                                     "error": "Internal Server Error"})
        r = resposta_v237(corpo, f"3EB0RESP{len(self.chamadas):04d}",
                          imagem="sendMedia" in request.url.path)
        if self.sem_stanza:
            # O caso da armadilha da v2.3.7: `quoted` sem mensagem achável é
            # descartado EM SILÊNCIO -- a mensagem sai, sem contextInfo.
            r.pop("contextInfo", None)
            for conteudo in r["message"].values():
                if isinstance(conteudo, dict):
                    conteudo.pop("contextInfo", None)
        if self.trocar_stanza:
            r["contextInfo"]["stanzaId"] = self.trocar_stanza
        return httpx.Response(201, json=r)

    @property
    def envios(self) -> list[dict]:
        return [c for r, c in self.chamadas if r.startswith("/message/send")]


def _cliente(servidor: Servidor) -> EvolutionClient:
    return EvolutionClient(
        "http://evolution:8080", "k", "allana", GRUPO, renderer=object(),
        client=httpx.Client(transport=httpx.MockTransport(servidor),
                            base_url="http://evolution:8080"))


@pytest.fixture()
def png(tmp_path) -> Path:
    caminho = tmp_path / "card.png"
    caminho.write_bytes(base64.b64decode(PNG_1X1))
    return caminho


ORIGEM = "3EB0C5A277F7F9B6C599"


class TestCitacaoContraARespostaReal:
    def test_texto_citado_e_verificado(self):
        """quoted enviado -> resposta -> contextInfo.stanzaId == origem -> ok."""
        s = Servidor()
        r = _cliente(s).send(GRUPO, "Grupo", "resposta", quote_message_id=ORIGEM,
                             quote_text=PEDIDO, quote_participant=CONSULTOR)
        enviado = s.envios[0]["quoted"]
        assert enviado["key"] == {"id": ORIGEM, "fromMe": False, "remoteJid": GRUPO,
                                  "participant": CONSULTOR}
        assert r.ok and r.quote_status == QuoteStatus.OK and r.quoted_ok is True
        assert r.quoted_message_id == ORIGEM
        assert r.enviado_id == "3EB0RESP0001"

    def test_imagem_citada_e_verificada(self, png):
        s = Servidor()
        r = _cliente(s).send_image(GRUPO, "Grupo", png, caption="legenda",
                                   quote_message_id=ORIGEM, quote_text=PEDIDO,
                                   quote_participant=CONSULTOR)
        assert s.envios[0]["quoted"]["key"]["id"] == ORIGEM
        assert s.envios[0]["mediatype"] == "image"
        assert r.ok and r.quote_status == QuoteStatus.OK and r.quoted_ok is True
        assert r.quoted_message_id == ORIGEM

    def test_texto_e_imagem_mandam_o_mesmo_quoted(self, png):
        """Uma identidade só: a mesma função monta o quoted dos dois envios."""
        s = Servidor()
        c = _cliente(s)
        c.send(GRUPO, "Grupo", "t", quote_message_id=ORIGEM, quote_text=PEDIDO,
               quote_participant=CONSULTOR)
        c.send_image(GRUPO, "Grupo", png, caption="c", quote_message_id=ORIGEM,
                     quote_text=PEDIDO, quote_participant=CONSULTOR)
        assert s.envios[0]["quoted"] == s.envios[1]["quoted"]

    def test_stanza_errado_e_falha_explicita(self):
        s = Servidor(trocar_stanza="3EB0OUTRA")
        r = _cliente(s).send(GRUPO, "Grupo", "resposta", quote_message_id=ORIGEM,
                             quote_text=PEDIDO, quote_participant=CONSULTOR)
        assert r.quote_status == QuoteStatus.NOT_APPLIED and r.quoted_ok is False
        # O id que VOLTOU é o gravado, para o portão do manager comparar.
        assert r.quoted_message_id == "3EB0OUTRA"
        assert ORIGEM in r.quote_error and "3EB0OUTRA" in r.quote_error

    def test_sem_stanza_nao_e_sucesso(self):
        """A Evolution descartou a citação em silêncio: nunca vira ok."""
        s = Servidor(sem_stanza=True)
        r = _cliente(s).send(GRUPO, "Grupo", "resposta", quote_message_id=ORIGEM,
                             quote_text=PEDIDO, quote_participant=CONSULTOR)
        assert r.ok, "a mensagem saiu (há key.id)"
        assert r.quote_status == QuoteStatus.UNVERIFIED and r.quoted_ok is False

    def test_imagem_sem_stanza_nao_e_sucesso(self, png):
        s = Servidor(sem_stanza=True)
        r = _cliente(s).send_image(GRUPO, "Grupo", png, caption="c",
                                   quote_message_id=ORIGEM, quote_text=PEDIDO)
        assert r.quote_status == QuoteStatus.UNVERIFIED and r.quoted_ok is False

    def test_quoted_sempre_leva_a_mensagem(self):
        """Sem ``message`` no quoted, a v2.3.7 procura a origem no banco DELA e,
        não achando, manda sem citação e sem erro. Pedido antigo, reiniciado ou
        que ela nunca gravou: o quoted leva o texto, sempre."""
        s = Servidor()
        c = _cliente(s)
        c.send(GRUPO, "Grupo", "r", quote_message_id=ORIGEM, quote_text=PEDIDO)
        c.send(GRUPO, "Grupo", "r", quote_message_id=ORIGEM, quote_text="")
        for enviado in s.envios:
            assert isinstance(enviado["quoted"].get("message"), dict), (
                "quoted sem message depende do banco da Evolution")

    def test_http_500_nao_reenvia(self):
        s = Servidor(status=500)
        r = _cliente(s).send(GRUPO, "Grupo", "resposta", quote_message_id=ORIGEM,
                             quote_text=PEDIDO)
        assert len(s.envios) == 1, "500 pode ter saído: não se manda de novo"
        assert not r.ok and r.desfecho == Desfecho.INCERTA and r.transitorio is False

    def test_timeout_de_leitura_nao_reenvia(self):
        s = Servidor(levanta=httpx.ReadTimeout("sem resposta"))
        r = _cliente(s).send(GRUPO, "Grupo", "resposta", quote_message_id=ORIGEM,
                             quote_text=PEDIDO)
        assert len(s.envios) == 1
        assert not r.ok and r.desfecho == Desfecho.INCERTA and r.transitorio is False

    def test_timeout_de_conexao_nao_chegou_e_pode_repetir(self):
        """A fronteira: sem conexão a Evolution nem recebeu o pedido."""
        s = Servidor(levanta=httpx.ConnectTimeout("sem rota"))
        r = _cliente(s).send(GRUPO, "Grupo", "resposta", quote_message_id=ORIGEM)
        assert len(s.envios) == 1, "a camada não repete sozinha; quem decide é o manager"
        assert r.desfecho == Desfecho.TRANSITORIA and r.transitorio is True


# ================================================================ ACK: parser
class TestAckDaV237:
    def test_formato_plano_vira_atualizacao(self):
        leitura = interpretar(ack("3EB0RESP", "DELIVERY_ACK"), GRUPO)
        assert leitura.motivo == "messages.update"
        assert leitura.atualizacao == {"message_id": "3EB0RESP", "chat_id": GRUPO,
                                       "status": "DELIVERY_ACK"}

    def test_formato_legado_continua(self):
        leitura = interpretar(ack_legado("3EB0RESP", "READ"), GRUPO)
        assert leitura.atualizacao == {"message_id": "3EB0RESP", "chat_id": GRUPO,
                                       "status": "READ"}

    def test_lote_no_formato_plano(self):
        payload = ack("A", "SERVER_ACK")
        payload["data"] = [ack("A", "SERVER_ACK")["data"], ack("B", "READ")["data"]]
        ids = [l.atualizacao["message_id"] for l in interpretar_todos(payload, GRUPO)]
        assert ids == ["A", "B"]

    @pytest.mark.parametrize("data, motivo", [
        ({}, "payload sem key"),
        ({"remoteJid": GRUPO, "status": "READ"}, "payload sem key"),
        ({"keyId": "", "status": "READ"}, "mensagem sem key.id"),
        ({"keyId": None}, "mensagem sem key.id"),
        ({"key": {"id": "X"}}, "payload sem update"),
        ("lixo", "payload sem data"),
        (None, "payload sem data"),
    ])
    def test_payload_parcial_nao_quebra(self, data, motivo):
        leitura = interpretar({"event": "messages.update", "data": data}, GRUPO)
        assert not leitura and leitura.motivo == motivo

    def test_sem_chat_ou_status_nao_inventa(self):
        """Parcial mas com id: sai vazio no que falta, e o manager descarta."""
        leitura = interpretar({"event": "messages.update", "data": {"keyId": "X"}}, GRUPO)
        assert leitura.atualizacao == {"message_id": "X", "chat_id": "", "status": ""}


# ================================================== ACK: da rota até o banco
class FilaFalsa(FakeWhatsApp):
    def __init__(self):
        super().__init__()
        self.inbox = queue.Queue()


@pytest.fixture()
def sistema(tmp_path):
    config = _config(tmp_path, whatsapp_mode="evolution",
                     evolution_group_jid=GRUPO, evolution_webhook_token=TOKEN)
    db = Database(config.db_path)
    hub = EventHub(db)
    manager = BotManager(config, db, hub)
    manager.whatsapp = FilaFalsa()
    with TestClient(create_app(config, db, hub, manager)) as client:
        yield client, manager


def _entrega_incerta(manager, request_id: str, wa_id: str) -> int:
    """Uma entrega `unconfirmed` com o id da mensagem enviada gravado.

    ATENÇÃO: o código de produção desta base ainda não produz esta linha (o
    ``enviado_id`` é descartado quando o envio fica sem prova; ver o relatório,
    achado ALTO). O estado é montado à mão para provar o caminho
    parser -> banco -> promoção, que é o que este conjunto testa.
    """
    agora = "2026-09-21T10:00:00Z"
    sim_id = manager.db.insert("simulations", {
        "request_id": request_id, "status": "completed", "chat_id": GRUPO,
        "delivery_status": "unconfirmed", "stage": "delivery_unconfirmed",
        "created_at": agora, "updated_at": agora})
    manager.db.insert("messages", {
        "simulation_id": sim_id, "request_id": request_id, "direction": "out",
        "chat_id": GRUPO, "wa_message_id": wa_id, "status": "unconfirmed",
        "created_at": agora})
    return sim_id


class TestAckRealNaRota:
    def test_delivery_ack_promove_entrega_incerta(self, sistema):
        client, manager = sistema
        sim_id = _entrega_incerta(manager, "REQ_A", "3EB0RESP_A")
        r = client.post("/webhook/whatsapp", json=ack("3EB0RESP_A", "DELIVERY_ACK"),
                        headers=CABECALHO)
        assert r.status_code == 200 and r.json()["acao"] == "messages.update"
        sim = manager.db.fetchone("SELECT delivery_status, stage FROM simulations WHERE id=?",
                                  (sim_id,))
        assert sim["delivery_status"] == "delivered" and sim["stage"] == "completed"

    def test_server_ack_nao_promove(self, sistema):
        client, manager = sistema
        sim_id = _entrega_incerta(manager, "REQ_B", "3EB0RESP_B")
        client.post("/webhook/whatsapp", json=ack("3EB0RESP_B", "SERVER_ACK"),
                    headers=CABECALHO)
        sim = manager.db.fetchone("SELECT delivery_status FROM simulations WHERE id=?",
                                  (sim_id,))
        assert sim["delivery_status"] == "unconfirmed", "SERVER_ACK não prova entrega"

    def test_ack_de_outro_grupo_nao_promove(self, sistema):
        client, manager = sistema
        sim_id = _entrega_incerta(manager, "REQ_C", "3EB0RESP_C")
        client.post("/webhook/whatsapp",
                    json=ack("3EB0RESP_C", "DELIVERY_ACK", chat=OUTRO_GRUPO),
                    headers=CABECALHO)
        sim = manager.db.fetchone("SELECT delivery_status FROM simulations WHERE id=?",
                                  (sim_id,))
        assert sim["delivery_status"] == "unconfirmed"

    def test_ack_nao_regride(self, sistema):
        client, manager = sistema
        for status in ("READ", "SERVER_ACK", "ERROR"):
            client.post("/webhook/whatsapp", json=ack("3EB0MONO", status), headers=CABECALHO)
        linha = manager.db.fetchone(
            "SELECT status, rank FROM evolution_acks WHERE message_id='3EB0MONO'")
        assert (linha["status"], linha["rank"]) == ("READ", 3)

    def test_ack_real_e_legado_chegam_ao_mesmo_lugar(self, sistema):
        client, manager = sistema
        client.post("/webhook/whatsapp", json=ack("3EB0REAL", "READ"), headers=CABECALHO)
        client.post("/webhook/whatsapp", json=ack_legado("3EB0LEGADO", "READ"),
                    headers=CABECALHO)
        ids = {l["message_id"] for l in manager.db.fetchall(
            "SELECT message_id FROM evolution_acks WHERE chat_id=?", (GRUPO,))}
        assert ids == {"3EB0REAL", "3EB0LEGADO"}
        assert manager.db.get_meta_int("wa_webhook_acks") == 2

    def test_ack_nunca_dispara_envio(self, sistema):
        """ACK grava e, no máximo, promove. Nunca manda nada ao grupo."""
        client, manager = sistema
        _entrega_incerta(manager, "REQ_D", "3EB0RESP_D")
        for status in ("SERVER_ACK", "DELIVERY_ACK", "READ", "ERROR", "PENDING"):
            client.post("/webhook/whatsapp", json=ack("3EB0RESP_D", status),
                        headers=CABECALHO)
        assert manager.whatsapp.sent == [] and manager.whatsapp.images == []
        saidas = manager.db.fetchall("SELECT id FROM messages WHERE direction='out'")
        assert len(saidas) == 1, "o ACK criou uma saída nova"


# ======================================================= pedido recebido real
class TestUpsertDaV237:
    def test_pedido_encaminhado_vira_mensagem_com_a_identidade_inteira(self):
        leitura = interpretar(upsert(id_msg="3EB0PEDIDO01"), GRUPO)
        m = leitura.mensagem
        assert (m.message_id, m.chat_id, m.participant) == ("3EB0PEDIDO01", GRUPO, CONSULTOR)
        assert m.text == PEDIDO and m.sender_name == "Consultor"

    def test_participant_lid_e_preservado_cru(self):
        """O LID fica como veio para a citação; a identidade usa o telefone."""
        leitura = interpretar(upsert(id_msg="3EB0LID", participante=CONSULTOR_LID,
                                     alternativo=CONSULTOR), GRUPO)
        assert leitura.mensagem.participant == CONSULTOR_LID
        assert leitura.mensagem.sender_id == CONSULTOR

    def test_participant_lid_sem_alternativa(self):
        leitura = interpretar(upsert(id_msg="3EB0LID2", participante=CONSULTOR_LID), GRUPO)
        assert leitura.mensagem.participant == CONSULTOR_LID
        assert leitura.aviso, "LID sem telefone precisa aparecer no log"

    def test_participant_vazio_usa_o_proprio_chat(self):
        """Regra existente: sem participant, o autor é o próprio chat -- e o
        quoted deixa de mandar participant (ver ``_citacao``)."""
        leitura = interpretar(upsert(id_msg="3EB0SEMP", participante=""), GRUPO)
        assert leitura.mensagem.participant == GRUPO
        quoted = EvolutionClient._citacao("3EB0SEMP", GRUPO, PEDIDO, GRUPO)
        assert "participant" not in quoted["key"]


# ============================================ identidade: webhook -> envio
class RenderizadorFalso:
    def start(self): pass
    def stop(self, timeout=10.0): pass

    def render_png(self, html, path, width=900, timeout=60.0):
        destino = Path(path)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(base64.b64decode(PNG_1X1))
        return str(destino)


def _esperar(condicao, timeout=30.0):
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        resultado = condicao()
        if resultado:
            return resultado
        time.sleep(0.05)
    raise AssertionError("não aconteceu dentro do tempo")


def _sistema_inteiro(tmp_path, *, send_image: bool):
    """O sistema inteiro, com a Evolution falsa respondendo no formato real."""
    from app.jobs import QueueService
    from tests.test_concurrency import FakeSimulator

    servidor = Servidor()
    config = _config(tmp_path, whatsapp_mode="evolution", send_image=send_image,
                     evolution_group_jid=GRUPO, evolution_webhook_token=TOKEN)
    db = Database(config.db_path)
    hub = EventHub(db)
    manager = BotManager(config, db, hub)
    manager.whatsapp = EvolutionClient(
        base_url="http://evolution:8080", api_key="k", instance="allana",
        group_jid=GRUPO, group_name="Consultores",
        client=httpx.Client(transport=httpx.MockTransport(servidor),
                            base_url="http://evolution:8080"),
        renderer=RenderizadorFalso())
    simulador = FakeSimulator()
    manager.simulators = [simulador]
    manager.queue = QueueService(
        db=db, hub=hub, simulators=[simulador], max_attempts=2, job_timeout=30.0,
        on_result=manager._deliver_result, on_log=manager._queue_log)
    manager.queue.start()
    leitor = threading.Thread(target=manager._inbox_loop, daemon=True)
    leitor.start()
    try:
        with TestClient(create_app(config, db, hub, manager)) as client:
            yield client, manager, servidor
    finally:
        manager._stop.set()
        leitor.join(timeout=5)
        manager.queue.stop()


@pytest.fixture()
def ponta_a_ponta(tmp_path):
    """Resposta como IMAGEM citada (o padrão de produção)."""
    yield from _sistema_inteiro(tmp_path, send_image=True)


@pytest.fixture()
def ponta_a_ponta_texto(tmp_path):
    """Resposta como TEXTO citado: o formato que mais muda na v2.3.7
    (``extendedTextMessage`` vira ``conversation``, ``contextInfo`` no topo)."""
    yield from _sistema_inteiro(tmp_path, send_image=False)


def _respondido(manager, origem: str) -> dict:
    return _esperar(lambda: manager.db.fetchone(
        "SELECT * FROM simulations WHERE source_message_id=? AND replied_at IS NOT NULL",
        (origem,)))


class TestIdentidadeDePontaAPonta:
    def test_pedido_sai_citado_e_verificado(self, ponta_a_ponta):
        client, manager, servidor = ponta_a_ponta
        r = client.post("/webhook/whatsapp", json=upsert(id_msg="3EB0ORIGEM01"),
                        headers=CABECALHO)
        assert r.status_code == 200
        sim = _respondido(manager, "3EB0ORIGEM01")

        # banco: a identidade gravada é a que chegou
        assert (sim["source_message_id"], sim["chat_id"], sim["participant"]) == (
            "3EB0ORIGEM01", GRUPO, CONSULTOR)
        assert sim["delivery_status"] == "delivered" and sim["quote_status"] == "ok"

        # envio: o quoted leva exatamente essa identidade
        quoted = servidor.envios[0]["quoted"]
        assert quoted["key"] == {"id": "3EB0ORIGEM01", "fromMe": False,
                                 "remoteJid": GRUPO, "participant": CONSULTOR}
        assert quoted["message"]["conversation"] == PEDIDO
        assert servidor.envios[0]["number"] == GRUPO

        # evidência: o que a Evolution devolveu foi conferido e gravado
        saida = manager.db.fetchone(
            "SELECT * FROM messages WHERE simulation_id=? AND direction='out'", (sim["id"],))
        assert saida["provider"] == "evolution" and saida["quote_status"] == "ok"
        assert saida["origin_message_id"] == saida["quoted_message_id"] == "3EB0ORIGEM01"
        assert saida["wa_message_id"] == "3EB0RESP0001"

    def test_resposta_em_texto_sai_citada_e_verificada(self, ponta_a_ponta_texto):
        client, manager, servidor = ponta_a_ponta_texto
        client.post("/webhook/whatsapp", json=upsert(id_msg="3EB0TEXTO01"), headers=CABECALHO)
        sim = _respondido(manager, "3EB0TEXTO01")
        rotas = [r for r, _ in servidor.chamadas if r.startswith("/message/send")]
        assert rotas == ["/message/sendText/allana"], rotas
        assert servidor.envios[0]["quoted"]["key"]["id"] == "3EB0TEXTO01"
        saida = manager.db.fetchone(
            "SELECT * FROM messages WHERE simulation_id=? AND direction='out'", (sim["id"],))
        assert saida["quote_status"] == "ok" and saida["quoted_message_id"] == "3EB0TEXTO01"
        assert sim["quote_status"] == "ok" and sim["delivery_status"] == "delivered"

    def test_participant_lid_chega_ao_quoted(self, ponta_a_ponta):
        client, manager, servidor = ponta_a_ponta
        client.post("/webhook/whatsapp",
                    json=upsert(id_msg="3EB0LID01", participante=CONSULTOR_LID,
                                alternativo=CONSULTOR), headers=CABECALHO)
        sim = _respondido(manager, "3EB0LID01")
        assert sim["participant"] == CONSULTOR_LID
        assert servidor.envios[0]["quoted"]["key"]["participant"] == CONSULTOR_LID

    def test_dois_pedidos_de_texto_igual_cada_um_cita_o_seu(self, ponta_a_ponta):
        client, manager, servidor = ponta_a_ponta
        client.post("/webhook/whatsapp", json=upsert(id_msg="3EB0IGUAL_A"), headers=CABECALHO)
        client.post("/webhook/whatsapp",
                    json=upsert(id_msg="3EB0IGUAL_B", participante="5567999990002@s.whatsapp.net"),
                    headers=CABECALHO)
        for origem in ("3EB0IGUAL_A", "3EB0IGUAL_B"):
            sim = _respondido(manager, origem)
            saida = manager.db.fetchone(
                "SELECT quoted_message_id, quote_status FROM messages "
                "WHERE simulation_id=? AND direction='out'", (sim["id"],))
            assert saida["quoted_message_id"] == origem and saida["quote_status"] == "ok"
        citados = sorted(e["quoted"]["key"]["id"] for e in servidor.envios)
        assert citados == ["3EB0IGUAL_A", "3EB0IGUAL_B"]

    def test_reinicio_e_reenvio_remontam_a_mesma_citacao(self, ponta_a_ponta):
        """Depois de reiniciar (``_job_from_row``) ou num reenvio
        (``_origem_da_linha``), a origem vem da linha gravada -- e o quoted
        remontado é idêntico ao que saiu."""
        from app.jobs import QueueService

        client, manager, servidor = ponta_a_ponta
        client.post("/webhook/whatsapp",
                    json=upsert(id_msg="3EB0REINICIO", participante=CONSULTOR_LID),
                    headers=CABECALHO)
        linha = dict(_respondido(manager, "3EB0REINICIO"))
        saiu = servidor.envios[0]["quoted"]

        for origem in (QueueService._job_from_row(linha, 2).message,
                       manager._origem_da_linha(linha)):
            remontado = EvolutionClient._citacao(origem.message_id, origem.chat_id,
                                                 origem.text, origem.participant)
            assert remontado == saiu

    def test_ack_da_resposta_enviada_e_gravado(self, ponta_a_ponta):
        """O ciclo: a resposta sai, o key.id volta, e o ACK real desse id é
        registrado sem mexer na entrega já confirmada e sem novo envio."""
        client, manager, servidor = ponta_a_ponta
        client.post("/webhook/whatsapp", json=upsert(id_msg="3EB0CICLO"), headers=CABECALHO)
        sim = _respondido(manager, "3EB0CICLO")
        enviado = manager.db.fetchone(
            "SELECT wa_message_id FROM messages WHERE simulation_id=? AND direction='out'",
            (sim["id"],))["wa_message_id"]
        antes = len(servidor.envios)

        client.post("/webhook/whatsapp", json=ack(enviado, "DELIVERY_ACK"), headers=CABECALHO)
        linha = manager.db.fetchone(
            "SELECT status, rank, chat_id FROM evolution_acks WHERE message_id=?", (enviado,))
        assert (linha["status"], linha["rank"], linha["chat_id"]) == ("DELIVERY_ACK", 2, GRUPO)
        assert len(servidor.envios) == antes, "o ACK disparou um envio"
        depois = manager.db.fetchone("SELECT delivery_status FROM simulations WHERE id=?",
                                     (sim["id"],))
        assert depois["delivery_status"] == "delivered"


# ============================================================== segredos
class TestSegredoDoEnvelopeNaoVaza:
    def test_apikey_e_server_url_nao_vao_para_log_evento_nem_resposta(self, ponta_a_ponta):
        client, manager, servidor = ponta_a_ponta
        respostas = [client.post("/webhook/whatsapp", json=upsert(id_msg="3EB0SEGREDO"),
                                 headers=CABECALHO).text]
        sim = _respondido(manager, "3EB0SEGREDO")
        enviado = manager.db.fetchone(
            "SELECT wa_message_id FROM messages WHERE simulation_id=? AND direction='out'",
            (sim["id"],))["wa_message_id"]
        respostas.append(client.post("/webhook/whatsapp", json=ack(enviado, "READ"),
                                     headers=CABECALHO).text)

        for tabela in ("logs", "events", "messages", "simulations", "evolution_acks"):
            despejo = json.dumps([dict(l) for l in manager.db.fetchall(
                f"SELECT * FROM {tabela}")], ensure_ascii=False, default=str)
            assert CHAVE_NO_ENVELOPE not in despejo, f"a chave foi parar em {tabela}"
            assert SERVIDOR_NO_ENVELOPE not in despejo, f"server_url foi parar em {tabela}"
        for texto in respostas:
            assert CHAVE_NO_ENVELOPE not in texto


# ============================================================= diagnóstico
CHAVE = "chave-secreta-que-nao-pode-vazar-0123456789"
TOKEN_DIAG = "token-do-webhook-que-nao-pode-vazar-987"
WAVOIP = "wavoip-token-que-nao-pode-vazar-555"


def _config_diag(tmp_path):
    from dataclasses import replace

    base = _config(tmp_path, whatsapp_mode="evolution", evolution_group_jid=GRUPO,
                   evolution_webhook_token=TOKEN_DIAG)
    return replace(base, evolution_api_key=CHAVE, evolution_url="http://evolution-real:8080",
                   evolution_instance="allana")


def _evolution_real(*, versao="2.3.7", grupos_ignorados=False,
                    eventos=("MESSAGES_UPSERT", "MESSAGES_UPDATE", "CONNECTION_UPDATE"),
                    por_evento=False):
    """As respostas da v2.3.7 para as rotas que o diagnóstico lê."""
    def servidor(request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        if caminho == "/":
            # index.router.ts
            return httpx.Response(200, json={
                "status": 200, "message": "Welcome to the Evolution API, it is working!",
                "version": versao, "clientName": "evolution_exchange",
                "manager": "http://evolution-real:8080/manager",
                "documentation": "https://doc.evolution-api.com",
                "whatsappWebVersion": "2.3000.1026436087"})
        if caminho == "/instance/connectionState/allana":
            return httpx.Response(200, json={"instance": {"instanceName": "allana",
                                                          "state": "open"}})
        if caminho == "/webhook/find/allana":
            # event.controller.ts `get`: o registro do banco, como está
            return httpx.Response(200, json={
                "id": "cm0000000000000000000000", "enabled": True,
                "url": "http://192.168.0.10:8000/webhook/whatsapp",
                "headers": {"X-Webhook-Token": TOKEN_DIAG},
                "events": list(eventos), "webhookByEvents": por_evento,
                "webhookBase64": False, "createdAt": "2026-09-21T10:00:00.000Z",
                "updatedAt": "2026-09-21T10:00:00.000Z",
                "instanceId": "00000000-0000-4000-8000-000000000237"})
        if caminho == "/settings/find/allana":
            # channel.service.ts `findSettings`
            return httpx.Response(200, json={
                "rejectCall": False, "msgCall": "", "groupsIgnore": grupos_ignorados,
                "alwaysOnline": False, "readMessages": False, "readStatus": False,
                "syncFullHistory": False, "wavoipToken": WAVOIP})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(servidor),
                        base_url="http://evolution-real:8080", headers={"apikey": CHAVE})


def _sem_segredo(resultado: dict) -> None:
    saida = json.dumps(resultado, ensure_ascii=False)
    for segredo in (CHAVE, TOKEN_DIAG, WAVOIP, GRUPO, "120363111222333", "192.168.0.10",
                    "webhook/whatsapp"):
        assert segredo not in saida, f"o diagnóstico expôs {segredo!r}"


class TestDiagnosticoDaInfraestrutura:
    def test_tudo_certo_fica_pronto_sem_avisos(self, tmp_path):
        from app.evolution_diagnostico import diagnosticar, pronto

        r = diagnosticar(_config_diag(tmp_path), client=_evolution_real())
        assert r["version"] == "2.3.7" == r["version_expected"]
        assert r["groups_ignored"] is False
        assert r["webhook_events"] == {"missing": [], "by_events": False}
        assert r["avisos"] == [] and pronto(r) is True
        _sem_segredo(r)

    def test_groups_ignore_ligado_e_dito_com_todas_as_letras(self, tmp_path):
        from app.evolution_diagnostico import diagnosticar, pronto

        r = diagnosticar(_config_diag(tmp_path), client=_evolution_real(grupos_ignorados=True))
        assert r["groups_ignored"] is True and pronto(r) is False
        assert any("groupsIgnore" in a and "NÃO chegam" in a for a in r["avisos"]), r["avisos"]
        _sem_segredo(r)

    def test_o_comando_antigo_sem_messages_update_e_pego(self, tmp_path):
        """Era o que a documentação mandava configurar: nenhum ACK chegaria."""
        from app.evolution_diagnostico import diagnosticar, pronto

        r = diagnosticar(_config_diag(tmp_path), client=_evolution_real(
            eventos=("MESSAGES_UPSERT", "CONNECTION_UPDATE")))
        assert r["webhook_events"]["missing"] == ["MESSAGES_UPDATE"] and pronto(r) is False
        assert any("MESSAGES_UPDATE" in a and "ACK" in a for a in r["avisos"])
        _sem_segredo(r)

    def test_by_events_ligado_e_pego(self, tmp_path):
        from app.evolution_diagnostico import diagnosticar, pronto

        r = diagnosticar(_config_diag(tmp_path), client=_evolution_real(por_evento=True))
        assert r["webhook_events"]["by_events"] is True and pronto(r) is False
        assert any("byEvents" in a for a in r["avisos"])
        _sem_segredo(r)

    def test_outra_versao_avisa(self, tmp_path):
        from app.evolution_diagnostico import diagnosticar

        r = diagnosticar(_config_diag(tmp_path), client=_evolution_real(versao="2.4.0"))
        assert r["version"] == "2.4.0"
        assert any("2.4.0" in a and "2.3.7" in a for a in r["avisos"])

    def test_chave_recusada_nao_inventa_avisos(self, tmp_path):
        from app.evolution_diagnostico import diagnosticar, pronto

        cliente = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)),
                               base_url="http://evolution-real:8080")
        r = diagnosticar(_config_diag(tmp_path), client=cliente)
        assert r["api_key_valid"] is False and r["avisos"] == [] and pronto(r) is False

    def test_a_versao_esperada_e_a_do_compose(self):
        from app.evolution_diagnostico import VERSAO_ESPERADA

        compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(
            encoding="utf-8")
        assert f"evoapicloud/evolution-api:v{VERSAO_ESPERADA}" in compose


class TestODocumentoConfiguraOWebhookCerto:
    """O ``webhook/set`` que a documentação manda rodar. O antigo não
    assinava MESSAGES_UPDATE (nenhum ACK chegaria) e usava ``webhookByEvents``,
    nome que o schema da v2.3.7 ignora (``event/webhook/webhook.schema.ts``
    lê ``byEvents``)."""

    @pytest.mark.parametrize("nome", ["ROTEIRO-TESTE-REAL.md", "MIGRACAO-EVOLUTION.md"])
    def test_assina_os_tres_eventos_e_usa_by_events(self, nome):
        texto = (Path(__file__).resolve().parent.parent / nome).read_text(encoding="utf-8")
        comando = next(l for l in texto.splitlines() if "/webhook/set/" in l)
        for evento in ("MESSAGES_UPSERT", "MESSAGES_UPDATE", "CONNECTION_UPDATE"):
            assert evento in comando, f"{nome}: o webhook/set não assina {evento}"
        assert "byEvents" in comando and "webhookByEvents" not in comando
