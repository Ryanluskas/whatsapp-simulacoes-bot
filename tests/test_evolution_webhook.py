"""O webhook da Evolution: o que entra no sistema e o que é ruído.

Estes testes usam payloads fixos, no formato que a Evolution manda. Eles
existem porque cada um destes filtros já falhou de verdade na camada antiga:

* ler a própria resposta rendeu 53 mensagens em cadeia no grupo do cliente;
* processar duas vezes o mesmo pedido rendeu simulação duplicada;
* identificar errado o consultor gravou todo mundo numa linha só.

A diferença agora é que cada um deles é um campo do JSON, e cada um tem um
teste que trava o comportamento.
"""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.evolution_webhook import extrair_texto, interpretar
from app.events import EventHub
from app.manager import BotManager
from app.web import create_app
from tests.test_concurrency import FakeWhatsApp, _config

GRUPO = "120363111222333@g.us"
OUTRO_GRUPO = "120363999888777@g.us"
TOKEN = "token-do-webhook-de-teste"
CONSULTOR = "5567999990001@s.whatsapp.net"
PNG_1X1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _esperar(condicao, timeout=30.0):
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        resultado = condicao()
        if resultado:
            return resultado
        time.sleep(0.05)
    raise AssertionError("não aconteceu dentro do tempo")


def _payload(texto: str = "Ivone Teste\n42888832453\nAmapá", *, id_msg="3EB0AAA",
             de_mim=False, grupo=GRUPO, participante=CONSULTOR,
             estendida=False, push="Ryan") -> dict:
    if estendida:
        # Formato usado quando a mensagem cita outra, tem link ou formatação --
        # é assim que um consultor manda correção.
        conteudo = {"extendedTextMessage": {"text": texto}}
    else:
        conteudo = {"conversation": texto}
    return {
        "event": "messages.upsert",
        "instance": "allana",
        "data": {
            "key": {"remoteJid": grupo, "fromMe": de_mim, "id": id_msg,
                    "participant": participante},
            "pushName": push,
            "message": conteudo,
            "messageTimestamp": 1756600000,
        },
    }


# ------------------------------------------------------------- 9, 10: filtros
class TestOQueNaoDeveEntrar:
    def test_mensagem_nossa_e_ignorada(self):
        """A guarda anti-laço, que no DOM era uma cascata de heurísticas."""
        leitura = interpretar(_payload(de_mim=True), GRUPO)
        assert not leitura
        assert "fromMe" in leitura.motivo

    def test_outra_conversa_e_ignorada(self):
        leitura = interpretar(_payload(grupo=OUTRO_GRUPO), GRUPO)
        assert not leitura
        assert OUTRO_GRUPO in leitura.motivo

    def test_mensagem_sem_texto_e_ignorada(self):
        """Figurinha, áudio, imagem: não há pedido nenhum ali."""
        p = _payload()
        p["data"]["message"] = {"imageMessage": {"mimetype": "image/jpeg"}}
        assert not interpretar(p, GRUPO)

    def test_sem_key_id_e_recusada(self):
        """Sem id não dá para citar nem deduplicar -- melhor recusar."""
        assert not interpretar(_payload(id_msg=""), GRUPO)

    def test_outro_evento_e_ignorado_sem_barulho(self):
        p = _payload()
        p["event"] = "presence.update"
        assert not interpretar(p, GRUPO)

    @pytest.mark.parametrize("lixo", [None, "texto", 42, [], {"event": "messages.upsert"}])
    def test_payload_estranho_nao_derruba(self, lixo):
        """O webhook é uma porta aberta: nunca pode estourar exceção."""
        leitura = interpretar(lixo, GRUPO)
        assert not leitura and leitura.motivo


# --------------------------------------------------------------- 12: o texto
class TestDeOndeVemOTexto:
    def test_conversation(self):
        leitura = interpretar(_payload("Maria\n31619614391"), GRUPO)
        assert leitura.mensagem.text == "Maria\n31619614391"

    def test_extended_text_message(self):
        """Mensagem que responde a outra chega aqui, não em conversation."""
        leitura = interpretar(_payload("Maria\n31619614391", estendida=True), GRUPO)
        assert leitura.mensagem.text == "Maria\n31619614391"

    def test_extrair_texto_sozinho(self):
        assert extrair_texto({"conversation": " oi "}) == "oi"
        assert extrair_texto({"extendedTextMessage": {"text": "oi"}}) == "oi"
        assert extrair_texto({}) == ""
        assert extrair_texto({"conversation": "   "}) == ""


# ------------------------------------------------------- 13, 14: o consultor
class TestIdentificacaoDoConsultor:
    """O único lugar onde a migração pode piorar algo que hoje funciona."""

    def test_jid_normal_vira_telefone(self):
        leitura = interpretar(_payload(), GRUPO)
        assert leitura.mensagem.sender_id == CONSULTOR
        assert leitura.mensagem.sender_phone == "5567999990001"

    def test_lid_nao_resolvido_nao_descarta_o_pedido(self):
        """Perder a solicitação seria muito pior que não saber a ficha."""
        leitura = interpretar(_payload(participante="12345678@lid"), GRUPO)
        assert leitura, "o pedido tem de sobreviver"
        assert leitura.mensagem.sender_id == "12345678@lid"
        assert leitura.aviso, "e tem de avisar que não resolveu"

    def test_lid_com_telefone_junto_usa_o_telefone(self):
        """Versões novas mandam o telefone ao lado do LID."""
        p = _payload(participante="12345678@lid")
        p["data"]["key"]["senderPn"] = CONSULTOR
        leitura = interpretar(p, GRUPO)
        assert leitura.mensagem.sender_phone == "5567999990001"
        assert not leitura.aviso

    def test_conversa_individual_usa_o_proprio_chat(self):
        """Sem `participant`, quem fala é o próprio chat."""
        individual = "5567999990001@s.whatsapp.net"
        p = _payload(grupo=individual, participante="")
        leitura = interpretar(p, individual)
        assert leitura.mensagem.sender_id == individual

    def test_push_name_vem_junto(self):
        assert interpretar(_payload(push="Ryan"), GRUPO).mensagem.sender_name == "Ryan"


# ----------------------------------------------------------------- 11: dedup
class TestReentregaNaoDuplica:
    """A Evolution reentrega quando não recebe 200 a tempo.

    A trava era um dicionário em RAM (`MemoriaDeIds`): um reinício apagava a
    memória e a reentrega virava segunda simulação. Agora a trava é a própria
    gravação da mensagem no banco (`Database.registrar_entrada`).
    """

    @staticmethod
    def _dados(message_id: str) -> dict:
        return {"chat_id": GRUPO, "wa_message_id": message_id, "text": "x",
                "created_at": "2026-09-16T12:00:00Z"}

    def test_o_mesmo_id_so_passa_uma_vez(self, tmp_path):
        db = Database(tmp_path / "t.db")
        assert db.registrar_entrada(self._dados("3EB0AAA"))["novo"] is True
        assert db.registrar_entrada(self._dados("3EB0AAA"))["novo"] is False
        assert db.registrar_entrada(self._dados("3EB0BBB"))["novo"] is True

    def test_id_vazio_nunca_trava(self, tmp_path):
        """Senão a primeira mensagem sem id bloquearia todas as seguintes."""
        db = Database(tmp_path / "t.db")
        assert db.registrar_entrada(self._dados(""))["novo"] is True
        assert db.registrar_entrada(self._dados(""))["novo"] is True

    def test_a_trava_sobrevive_a_reinicio(self, tmp_path):
        """O defeito da versão em RAM: reiniciar esquecia o que já tinha entrado."""
        assert Database(tmp_path / "t.db").registrar_entrada(self._dados("3EB0R"))["novo"]
        assert not Database(tmp_path / "t.db").registrar_entrada(self._dados("3EB0R"))["novo"]

    def test_duas_entregas_ao_mesmo_tempo(self, tmp_path):
        """Consultar e marcar em passos separados abriria uma janela entre eles."""
        db = Database(tmp_path / "t.db")
        resultados: list[bool] = []
        trava = threading.Barrier(8)

        def corre():
            trava.wait()
            resultados.append(db.registrar_entrada(self._dados("mesmo-id"))["novo"])

        threads = [threading.Thread(target=corre) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert resultados.count(True) == 1, "exatamente uma entrega pode passar"


# ------------------------------------------------------------ 15, 16: a rota
class FilaFalsa(FakeWhatsApp):
    """FakeWhatsApp com `inbox` de verdade -- a rota faz `put`, não `append`."""

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


CABECALHO = {"X-Webhook-Token": TOKEN}


class TestARotaDoWebhook:
    def test_sem_token_recusa_e_nao_processa(self, sistema):
        """A rota recebe dado de cliente e ENFILEIRA trabalho: não pode ficar aberta."""
        client, manager = sistema
        r = client.post("/webhook/whatsapp", json=_payload())
        assert r.status_code == 401
        assert manager.whatsapp.inbox.empty()

    def test_token_errado_recusa(self, sistema):
        client, manager = sistema
        r = client.post("/webhook/whatsapp", json=_payload(),
                        headers={"X-Webhook-Token": "chute"})
        assert r.status_code == 401
        assert manager.whatsapp.inbox.empty()

    def test_com_token_enfileira(self, sistema):
        client, manager = sistema
        r = client.post("/webhook/whatsapp", json=_payload(), headers=CABECALHO)
        assert r.status_code == 200
        mensagem = manager.whatsapp.inbox.get(timeout=2)
        assert mensagem.message_id == "3EB0AAA"
        assert "42888832453" in mensagem.text

    def test_guarda_o_texto_original_para_citar(self, sistema):
        """A Evolution monta `quoted` com o key.id E o conteúdo citado.

        O texto fica NO BANCO, antes do 200: a memória em RAM que existia
        antes sumia no reinício, e a citação saía vazia.
        """
        client, manager = sistema
        client.post("/webhook/whatsapp", json=_payload(), headers=CABECALHO)
        linha = manager.db.fetchone(
            "SELECT * FROM messages WHERE direction='in' AND wa_message_id='3EB0AAA'")
        assert linha is not None, "a mensagem não foi gravada antes de aceitar"
        assert "42888832453" in linha["text"]
        assert linha["chat_id"] == GRUPO

    def test_reentrega_do_mesmo_payload_rende_uma_mensagem_so(self, sistema):
        client, manager = sistema
        for _ in range(3):
            assert client.post("/webhook/whatsapp", json=_payload(),
                               headers=CABECALHO).status_code == 200
        manager.whatsapp.inbox.get(timeout=2)
        assert manager.whatsapp.inbox.empty(), "reentrega virou pedido novo"

    def test_ignorado_responde_200(self, sistema):
        """4xx faria a Evolution reentregar para sempre algo que nunca queremos."""
        client, _ = sistema
        r = client.post("/webhook/whatsapp", json=_payload(de_mim=True), headers=CABECALHO)
        assert r.status_code == 200
        assert r.json()["ignorado"]

    def test_corpo_que_nao_e_json(self, sistema):
        client, _ = sistema
        r = client.post("/webhook/whatsapp", content=b"nada disso", headers=CABECALHO)
        assert r.status_code == 400

    def test_no_modo_dom_a_rota_recusa(self, tmp_path):
        """Aceitar mensagem pelos dois caminhos duplicaria toda solicitação."""
        config = _config(tmp_path, whatsapp_mode="dom", evolution_webhook_token=TOKEN)
        db = Database(config.db_path)
        hub = EventHub(db)
        manager = BotManager(config, db, hub)
        manager.whatsapp = FilaFalsa()
        with TestClient(create_app(config, db, hub, manager)) as client:
            r = client.post("/webhook/whatsapp", json=_payload(), headers=CABECALHO)
            assert r.status_code == 409

    def test_sem_token_no_env_o_sistema_nem_sobe(self, tmp_path):
        """Garantia mais forte que um 503: a rota nunca chega a existir aberta.

        Deixar o bot subir com a porta do webhook sem senha seria dar a
        qualquer um na rede o poder de mandar simular o que quisesse. Então
        falta de token não é degradação -- é recusa de partida.
        """
        config = _config(tmp_path, whatsapp_mode="evolution",
                         evolution_group_jid=GRUPO, evolution_webhook_token="")
        db = Database(config.db_path)
        with pytest.raises(RuntimeError, match="EVOLUTION_WEBHOOK_TOKEN"):
            BotManager(config, db, EventHub(db))

    def test_em_modo_dom_sem_configuracao_a_rota_responde_503(self, tmp_path):
        """Webhook perdido chegando num sistema que nem usa Evolution."""
        config = _config(tmp_path, whatsapp_mode="dom", evolution_webhook_token="")
        db = Database(config.db_path)
        hub = EventHub(db)
        manager = BotManager(config, db, hub)
        manager.whatsapp = FilaFalsa()
        with TestClient(create_app(config, db, hub, manager)) as client:
            assert client.post("/webhook/whatsapp", json=_payload(),
                               headers=CABECALHO).status_code == 503


class TestQuemDecideSeViraSolicitacao:
    def test_mensagem_sem_cpf_nao_vira_simulacao(self, sistema):
        """Quem decide é o parser, como sempre foi. O webhook só filtra ruído."""
        client, manager = sistema
        manager.start_reading = lambda: None
        client.post("/webhook/whatsapp",
                    json=_payload("bom dia pessoal", id_msg="3EB0ZZZ"),
                    headers=CABECALHO)
        # A mensagem entra na fila de leitura (é texto de alguém que não é o
        # bot), mas o parser não acha CPF e nada vira simulação.
        mensagem = manager.whatsapp.inbox.get(timeout=2)
        assert mensagem.text == "bom dia pessoal"
        assert manager.db.fetchall("SELECT id FROM simulations") == []


# ------------------------------------------------------- o caminho completo
class TestDaMensagemAteAResposta:
    """A migração inteira, de ponta a ponta, sem Evolution de verdade.

    Este é o teste que importa: os outros provam pedaços. Aqui um webhook
    entra pela rota e a resposta sai pelo cliente HTTP -- citada e como
    imagem, que são exatamente os dois defeitos que motivaram a troca.
    """

    @pytest.fixture()
    def sistema_completo(self, tmp_path):
        import httpx

        from app.evolution import EvolutionClient
        from app.jobs import QueueService
        from tests.test_concurrency import FakeSimulator

        enviados: list[tuple[str, dict]] = []

        def servidor(request: httpx.Request) -> httpx.Response:
            corpo = json.loads(request.content) if request.content else {}
            enviados.append((request.url.path, corpo))
            return httpx.Response(200, json={"key": {"id": "3EB0RESPOSTA"}})

        class RenderizadorFalso:
            def start(self): pass
            def stop(self, timeout=10.0): pass

            def render_png(self, html, path, width=900, timeout=60.0):
                destino = Path(path)
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(base64.b64decode(PNG_1X1))
                return str(destino)

        config = _config(tmp_path, whatsapp_mode="evolution", send_image=True,
                         evolution_group_jid=GRUPO, evolution_webhook_token=TOKEN)
        db = Database(config.db_path)
        hub = EventHub(db)
        manager = BotManager(config, db, hub)

        manager.whatsapp = EvolutionClient(
            base_url="http://evolution:8080", api_key="k", instance="allana",
            group_jid=GRUPO, group_name="Consultores",
            client=httpx.Client(transport=httpx.MockTransport(servidor),
                                base_url="http://evolution:8080"),
            renderer=RenderizadorFalso(),
        )
        simulador = FakeSimulator()
        manager.simulators = [simulador]
        manager.queue = QueueService(
            db=db, hub=hub, simulators=[simulador], max_attempts=2,
            job_timeout=30.0, on_result=manager._deliver_result,
            on_log=manager._queue_log)
        manager.queue.start()
        # O laço real que consome a inbox -- o mesmo que roda em produção. É
        # ele que liga o webhook ao parser, então tem de ser o de verdade.
        leitor = threading.Thread(target=manager._inbox_loop, daemon=True)
        leitor.start()
        try:
            with TestClient(create_app(config, db, hub, manager)) as client:
                yield client, manager, enviados
        finally:
            manager._stop.set()
            leitor.join(timeout=5)
            manager.queue.stop()

    def test_o_pedido_entra_e_a_resposta_sai_citada_e_como_imagem(self, sistema_completo):
        client, manager, enviados = sistema_completo

        r = client.post("/webhook/whatsapp",
                        json=_payload("Ivone Teste\n42888832453\nSantander",
                                      id_msg="3EB0PEDIDO"),
                        headers=CABECALHO)
        assert r.status_code == 200

        _esperar(lambda: any(rota.startswith("/message/send") for rota, _ in enviados))

        rota, payload = next((r, p) for r, p in enviados
                             if r.startswith("/message/send"))
        assert rota == "/message/sendMedia/allana", "tinha de sair como mídia"
        assert payload["mediatype"] == "image", "o defeito do documento"
        assert payload["quoted"]["key"]["id"] == "3EB0PEDIDO", "o defeito da citação"
        assert payload["quoted"]["message"]["conversation"].startswith("Ivone Teste")
        assert payload["number"] == GRUPO
        assert not payload["media"].startswith("data:")

    def test_a_simulacao_ficou_registrada(self, sistema_completo):
        client, manager, enviados = sistema_completo
        client.post("/webhook/whatsapp",
                    json=_payload("Ivone Teste\n42888832453", id_msg="3EB0PEDIDO2"),
                    headers=CABECALHO)
        _esperar(lambda: manager.db.fetchall(
            "SELECT id FROM simulations WHERE replied_at IS NOT NULL"))
        linha = manager.db.fetchone("SELECT * FROM simulations")
        assert linha["cpf"] == "42888832453"
        assert linha["replied_at"]

    def test_a_propria_resposta_nao_vira_pedido_novo(self, sistema_completo):
        """O laço de 53 mensagens, agora impossível: fromMe resolve na entrada."""
        client, manager, enviados = sistema_completo
        client.post("/webhook/whatsapp",
                    json=_payload("Ivone Teste\n42888832453", id_msg="3EB0PEDIDO3"),
                    headers=CABECALHO)
        _esperar(lambda: any(r.startswith("/message/send") for r, _ in enviados))

        # A resposta volta pelo webhook, como o WhatsApp de fato faz.
        eco = _payload("✅ Libera R$ 4.913,52", id_msg="3EB0RESPOSTA", de_mim=True)
        assert client.post("/webhook/whatsapp", json=eco,
                           headers=CABECALHO).json()["ignorado"]
        time.sleep(0.5)
        assert len(manager.db.fetchall("SELECT id FROM simulations")) == 1
