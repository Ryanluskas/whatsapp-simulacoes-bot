"""Entrega incerta + ACK: a reconciliação, em qualquer ordem.

O cenário: o POST para a Evolution volta SEM prova de entrega, mas com o
``key.id`` da mensagem. Na v2.3.7 isso é o 2xx com ``status: "ERROR"`` -- o
único envio incerto que traz o id. (500 e timeout de leitura não trazem id
nenhum: não há o que reconciliar, e eles continuam esperando uma pessoa.)
Depois, ou ANTES, o webhook entrega o ACK dessa mensagem.

A regra: ACK de entrega (DELIVERY_ACK, READ, PLAYED) para o MESMO id, na
MESMA conversa, promove a entrega a ``delivered``. Nada é reenviado, nunca.

Todos os testes passam pelo sistema real -- webhook, fila, manager, banco e o
cliente HTTP da Evolution --, com a Evolution falsa respondendo no formato da
v2.3.7. Nenhum fala com uma Evolution de verdade.
"""

from __future__ import annotations

import random
import threading
import time

import httpx
import pytest

from app.models import Delivery, Stage
from ferramentas.e2e_simulado import resposta_v237
from tests.test_concurrency import _aguardar
from tests.test_producao import CPF_A, CPF_B, GRUPO, Sistema, _pedido, recusa

OUTRO_GRUPO = "120363999888777@g.us"


# ------------------------------------------------------------------ formatos
def ack_v237(key_id: str, status: str, chat: str = GRUPO) -> dict:
    """``messages.update`` da Evolution v2.3.7: ``data`` PLANO."""
    return {"event": "messages.update", "instance": "allana",
            "data": {"keyId": key_id, "remoteJid": chat, "fromMe": True,
                     "status": status, "instanceId": "00000000-0000-4000-8000-000000000237"},
            "server_url": "http://evolution:8080", "apikey": "chave-do-envelope"}


def incerta_com_id(key_id: str, *, antes=None):
    """Passo do roteiro: 2xx com ``key.id`` e ``status: ERROR``.

    ``antes`` roda DENTRO do POST, antes da resposta voltar: é como o ACK
    chega pelo webhook enquanto o bot ainda espera a Evolution responder.
    """
    def passo(rota, payload):
        if antes is not None:
            antes()
        corpo = resposta_v237(payload, key_id, imagem="sendMedia" in rota)
        corpo["status"] = "ERROR"
        return httpx.Response(201, json=corpo)
    return passo


def mandar_ack(s: Sistema, key_id: str, status: str = "DELIVERY_ACK", chat: str = GRUPO):
    r = s.cliente.post("/webhook/whatsapp", json=ack_v237(key_id, status, chat),
                       headers={"X-Webhook-Token": s.config.evolution_webhook_token})
    assert r.status_code == 200, r.text
    return r


def logs(s: Sistema, trecho: str) -> list[str]:
    return [l["message"] for l in s.db.fetchall(
        "SELECT message FROM logs WHERE message LIKE ?", (f"%{trecho}%",))]


def saida(s: Sistema, simulation_id: int) -> dict:
    return s.db.fetchone("SELECT * FROM messages WHERE simulation_id=? AND direction='out' "
                         "ORDER BY id DESC LIMIT 1", (simulation_id,))


def incerta(s: Sistema, key_id: str, id_msg: str, cpf: str = CPF_A) -> dict:
    """Uma solicitação cuja entrega ficou incerta COM o id da mensagem."""
    s.servidor.roteiro.append(incerta_com_id(key_id))
    s.webhook(_pedido("Cliente Teste", cpf), id_msg)
    assert _aguardar(lambda: (s.linha(source_message_id=id_msg) or {}).get(
        "delivery_status") == Delivery.UNCONFIRMED, timeout=20), s.linha(source_message_id=id_msg)
    return s.linha(source_message_id=id_msg)


@pytest.fixture()
def s(tmp_path):
    sistema = Sistema(tmp_path).ligar()
    try:
        yield sistema
    finally:
        sistema.desligar()


# ============================================== o id da entrega incerta fica
class TestOIdDaEntregaIncertaEGuardado:
    def test_o_id_devolvido_fica_na_solicitacao_e_na_saida(self, s):
        """Sem o id, o ACK não tem como achar a entrega. Não é prova: continua incerta."""
        linha = incerta(s, "3EB0INC_ID", "3EB0ORIG_ID")
        assert linha["delivery_status"] == Delivery.UNCONFIRMED
        assert linha["replied_at"] is None, "id não é prova de entrega"
        assert linha["sent_message_id"] == "3EB0INC_ID"
        assert saida(s, linha["id"])["wa_message_id"] == "3EB0INC_ID"
        assert saida(s, linha["id"])["status"] == Delivery.UNCONFIRMED

    def test_500_continua_sem_id_e_sem_prova_inventada(self, tmp_path):
        s = Sistema(tmp_path).ligar()
        try:
            s.servidor.roteiro = [recusa(500, "Internal server error")]
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0ORIG_500")
            assert _aguardar(lambda: (s.linha(source_message_id="3EB0ORIG_500") or {}).get(
                "delivery_status") == Delivery.UNCONFIRMED, timeout=20)
            linha = s.linha(source_message_id="3EB0ORIG_500")
            assert linha["sent_message_id"] == "" and saida(s, linha["id"])["wa_message_id"] == ""
        finally:
            s.desligar()


# ========================================================= ACK DEPOIS
class TestAckDepoisDoRegistro:
    def test_delivery_ack_promove_sem_reenviar(self, s):
        linha = incerta(s, "3EB0INC_D1", "3EB0ORIG_D1")
        mandar_ack(s, "3EB0INC_D1", "DELIVERY_ACK")

        depois = s.linha(id=linha["id"])
        assert depois["delivery_status"] == Delivery.DELIVERED
        assert depois["stage"] == Stage.COMPLETED and depois["replied_at"]
        assert depois["sent_message_id"] == "3EB0INC_D1"
        m = saida(s, linha["id"])
        assert (m["status"], m["desfecho"]) == ("delivered", "entregue (DELIVERY_ACK)")
        assert len(s.servidor.envios()) == 1, "o ACK disparou um envio"

    def test_o_log_da_promocao_e_gravado(self, s):
        """Antes, o log era escrito DENTRO da transação e se perdia em silêncio."""
        linha = incerta(s, "3EB0INC_LOG", "3EB0ORIG_LOG")
        mandar_ack(s, "3EB0INC_LOG", "READ")
        registros = logs(s, f"A entrega #{linha['id']} estava incerta")
        assert len(registros) == 1, registros
        assert "3EB0INC_LOG" in registros[0] and "READ" in registros[0]

    @pytest.mark.parametrize("status", ["SERVER_ACK", "ERROR", "PENDING"])
    def test_ack_que_nao_prova_entrega_nao_promove(self, s, status):
        linha = incerta(s, f"3EB0INC_{status}", f"3EB0ORIG_{status}")
        mandar_ack(s, f"3EB0INC_{status}", status)
        assert s.linha(id=linha["id"])["delivery_status"] == Delivery.UNCONFIRMED

    def test_ack_de_outra_conversa_nao_promove(self, s):
        linha = incerta(s, "3EB0INC_OUTRO", "3EB0ORIG_OUTRO")
        mandar_ack(s, "3EB0INC_OUTRO", "DELIVERY_ACK", chat=OUTRO_GRUPO)
        assert s.linha(id=linha["id"])["delivery_status"] == Delivery.UNCONFIRMED

    def test_ack_de_outra_mensagem_nao_promove(self, s):
        linha = incerta(s, "3EB0INC_CERTA", "3EB0ORIG_CERTA")
        mandar_ack(s, "3EB0ALGUMA_OUTRA", "READ")
        assert s.linha(id=linha["id"])["delivery_status"] == Delivery.UNCONFIRMED


# ========================================================== ACK ANTES
class TestAckAntesDoRegistro:
    def test_ack_que_chega_durante_o_post_promove(self, s):
        """O webhook entrega o ACK enquanto o POST ainda não voltou."""
        s.servidor.roteiro.append(incerta_com_id(
            "3EB0INC_A1", antes=lambda: mandar_ack(s, "3EB0INC_A1", "DELIVERY_ACK")))
        s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0ORIG_A1")
        assert _aguardar(lambda: (s.linha(source_message_id="3EB0ORIG_A1") or {}).get(
            "delivery_status") not in (None, "", Delivery.PENDING), timeout=20)

        linha = s.linha(source_message_id="3EB0ORIG_A1")
        assert linha["delivery_status"] == Delivery.DELIVERED, linha
        assert linha["stage"] == Stage.COMPLETED and linha["sent_message_id"] == "3EB0INC_A1"
        assert saida(s, linha["id"])["status"] == "delivered"
        assert len(s.servidor.envios()) == 1
        assert logs(s, "já tinha chegado"), "a promoção não ficou no log"
        # O painel recebe "entregue", não "incerta".
        tipos = {e["type"] for e in s.db.fetchall(
            "SELECT type FROM events WHERE request_id=?", (linha["request_id"],))}
        assert "request_completed" in tipos and "delivery_unconfirmed" not in tipos

    def test_ack_antes_com_resposta_em_texto(self, tmp_path):
        s = Sistema(tmp_path, com_imagem=False).ligar()
        try:
            s.servidor.roteiro.append(incerta_com_id(
                "3EB0INC_A2", antes=lambda: mandar_ack(s, "3EB0INC_A2", "READ")))
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0ORIG_A2")
            assert _aguardar(lambda: (s.linha(source_message_id="3EB0ORIG_A2") or {}).get(
                "delivery_status") == Delivery.DELIVERED, timeout=20)
            rotas = [r for r, _ in s.servidor.envios()]
            assert rotas == ["/message/sendText/allana"], rotas
        finally:
            s.desligar()

    def test_ack_entre_a_saida_gravada_e_o_registro(self, s):
        """A intercalação mais apertada: a saída já tem o id, a solicitação
        ainda está em curso. O ACK acha a saída mas não pode promover a
        solicitação; o registro, logo depois, acha o ACK e promove."""
        original = s.manager._registrar_entrega
        estado_no_meio = {}

        def com_ack_no_meio(job, result, entrega):
            mandar_ack(s, "3EB0INC_MEIO", "DELIVERY_ACK")
            estado_no_meio.update(s.linha(id=job.simulation_id))
            return original(job, result, entrega)

        s.manager._registrar_entrega = com_ack_no_meio
        s.servidor.roteiro.append(incerta_com_id("3EB0INC_MEIO"))
        s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0ORIG_MEIO")
        assert _aguardar(lambda: (s.linha(source_message_id="3EB0ORIG_MEIO") or {}).get(
            "delivery_status") == Delivery.DELIVERED, timeout=20)
        assert estado_no_meio["delivery_status"] == Delivery.PENDING, (
            "o teste não pegou a intercalação que queria")
        assert len(s.servidor.envios()) == 1

    def test_ack_antes_do_reenvio_ser_registrado(self, tmp_path):
        """O mesmo caminho no reenvio: 503 primeiro, reenvio incerto com o ACK
        chegando durante o POST. Só texto: com imagem, o 503 da imagem cai
        para o texto na hora e não sobra reenvio."""
        s = Sistema(tmp_path, com_imagem=False).ligar()
        try:
            s.servidor.roteiro.append(recusa(503, "Service Unavailable"))
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0ORIG_RE")
            assert _aguardar(lambda: (s.linha(source_message_id="3EB0ORIG_RE") or {}).get(
                "delivery_status") == Delivery.RETRYING, timeout=20)
            s.db.execute("UPDATE simulations SET next_delivery_at='2000-01-01T00:00:00Z'")
            s.servidor.roteiro.append(incerta_com_id(
                "3EB0INC_RE", antes=lambda: mandar_ack(s, "3EB0INC_RE", "DELIVERY_ACK")))

            s.manager._reenviar_pendentes()

            linha = s.linha(source_message_id="3EB0ORIG_RE")
            assert linha["delivery_status"] == Delivery.DELIVERED, linha
            assert linha["sent_message_id"] == "3EB0INC_RE"
            assert len(s.servidor.envios()) == 2, "503 + um reenvio, e só"
            assert logs(s, "antes do registro do reenvio")
        finally:
            s.desligar()


# ================================================================ concorrência
class TestConcorrencia:
    def test_ack_e_registro_em_corrida(self, tmp_path):
        """Dois trabalhadores entregando e ACKs chegando em momentos aleatórios:
        antes do POST voltar, logo depois, ou bem depois. Toda entrega termina
        `delivered`, uma promoção por solicitação, e nenhum envio a mais."""
        s = Sistema(tmp_path, workers=2).ligar()
        sorteio = random.Random(237)
        disparos: list[threading.Thread] = []
        n = 12
        try:
            for i in range(n):
                key = f"3EB0CORRIDA{i:02d}"
                modo = sorteio.choice(["antes", "logo", "depois"])
                if modo == "antes":
                    passo = incerta_com_id(key, antes=lambda k=key: mandar_ack(s, k))
                else:
                    espera = 0.0 if modo == "logo" else sorteio.uniform(0.01, 0.08)

                    def disparar(k=key, t=espera):
                        time.sleep(t)
                        mandar_ack(s, k)

                    def passo(rota, payload, k=key, f=disparar):
                        th = threading.Thread(target=f, daemon=True)
                        disparos.append(th)
                        th.start()
                        corpo = resposta_v237(payload, k, imagem="sendMedia" in rota)
                        corpo["status"] = "ERROR"
                        return httpx.Response(201, json=corpo)
                s.servidor.roteiro.append(passo)
            for i in range(n):
                s.webhook(_pedido("Cliente Teste", CPF_A if i % 2 else CPF_B), f"3EB0ORIGC{i:02d}")

            assert _aguardar(lambda: s.db.scalar(
                "SELECT COUNT(*) FROM simulations WHERE delivery_status=?",
                (Delivery.DELIVERED,)) == n, timeout=60), [
                (l["request_id"], l["delivery_status"]) for l in s.db.fetchall(
                    "SELECT request_id, delivery_status FROM simulations")]
            for th in disparos:
                th.join(timeout=5)

            assert len(s.servidor.envios()) == n, "houve envio a mais"
            for linha in s.db.fetchall("SELECT * FROM simulations"):
                promocoes = (logs(s, f"A entrega #{linha['id']} estava incerta")
                             + logs(s, f"{linha['request_id']}: o envio ficou sem prova, mas o ACK"))
                assert len(promocoes) == 1, (linha["request_id"], promocoes)
                assert saida(s, linha["id"])["status"] == "delivered"
        finally:
            s.desligar()


# ============================================================== reinício
class TestReinicio:
    def test_ack_gravado_e_queda_antes_de_promover(self, tmp_path):
        """O ACK foi gravado e o processo caiu antes da promoção. O boot resolve."""
        s = Sistema(tmp_path).ligar()
        linha = incerta(s, "3EB0INC_Q1", "3EB0ORIG_Q1")
        # O estado exato da queda: o ACK está na tabela, a promoção não rodou.
        s.db.execute("INSERT INTO evolution_acks(message_id, status, rank, chat_id, created_at) "
                     "VALUES (?, 'DELIVERY_ACK', 2, ?, '2026-09-21T10:00:00Z')",
                     ("3EB0INC_Q1", GRUPO))
        s.desligar()
        assert s.linha(id=linha["id"])["delivery_status"] == Delivery.UNCONFIRMED

        novo = Sistema(tmp_path, db_path=s.config.db_path)
        novo.manager.start()
        try:
            assert novo.linha(id=linha["id"])["delivery_status"] == Delivery.DELIVERED
            assert logs(novo, "na varredura")
            assert len(s.servidor.envios()) == 1 and novo.servidor.envios() == []
        finally:
            novo.manager.stop()

    def test_ack_que_chega_depois_do_reinicio(self, tmp_path):
        s = Sistema(tmp_path).ligar()
        linha = incerta(s, "3EB0INC_Q2", "3EB0ORIG_Q2")
        s.desligar()

        novo = Sistema(tmp_path, db_path=s.config.db_path).ligar()
        try:
            mandar_ack(novo, "3EB0INC_Q2", "READ")
            assert novo.linha(id=linha["id"])["delivery_status"] == Delivery.DELIVERED
            assert novo.servidor.envios() == [], "o reinício reenviou"
        finally:
            novo.desligar()

    def test_o_laco_reconcilia_mesmo_com_o_whatsapp_fora(self, s):
        linha = incerta(s, "3EB0INC_LACO", "3EB0ORIG_LACO")
        s.db.execute("INSERT INTO evolution_acks(message_id, status, rank, chat_id, created_at) "
                     "VALUES (?, 'READ', 3, ?, '2026-09-21T10:00:00Z')", ("3EB0INC_LACO", GRUPO))
        assert not s.manager.whatsapp.status.connected
        s.manager._INTERVALO_REENVIO = 0.05
        s.manager._stop.clear()
        laco = threading.Thread(target=s.manager._reenvio_loop, daemon=True)
        laco.start()
        try:
            assert _aguardar(lambda: s.linha(id=linha["id"])["delivery_status"]
                             == Delivery.DELIVERED, timeout=5)
        finally:
            s.manager._stop.set()
            laco.join(timeout=2)
        assert len(s.servidor.envios()) == 1


# ============================================================ idempotência
class TestIdempotencia:
    def test_ack_repetido_e_fora_de_ordem(self, s):
        linha = incerta(s, "3EB0INC_ID2", "3EB0ORIG_ID2")
        for status in ("DELIVERY_ACK", "DELIVERY_ACK", "READ", "SERVER_ACK", "ERROR", "READ"):
            mandar_ack(s, "3EB0INC_ID2", status)
        depois = s.linha(id=linha["id"])
        assert depois["delivery_status"] == Delivery.DELIVERED
        assert len(logs(s, f"A entrega #{linha['id']} estava incerta")) == 1
        ack = s.db.fetchone("SELECT status, rank FROM evolution_acks WHERE message_id=?",
                            ("3EB0INC_ID2",))
        assert (ack["status"], ack["rank"]) == ("READ", 3)
        assert len(s.servidor.envios()) == 1

    def test_varredura_repetida_nao_faz_nada_de_novo(self, s):
        linha = incerta(s, "3EB0INC_V", "3EB0ORIG_V")
        s.db.execute("INSERT INTO evolution_acks(message_id, status, rank, chat_id, created_at) "
                     "VALUES (?, 'DELIVERY_ACK', 2, ?, '2026-09-21T10:00:00Z')",
                     ("3EB0INC_V", GRUPO))
        assert s.manager.reconciliar_entregas_incertas() == 1
        assert s.manager.reconciliar_entregas_incertas() == 0
        assert s.linha(id=linha["id"])["delivery_status"] == Delivery.DELIVERED
        assert len(logs(s, f"A entrega #{linha['id']} estava incerta")) == 1


# ================================================= a decisão de uma pessoa
class TestADecisaoManualNaoEDesfeita:
    def _login(self, s: Sistema) -> None:
        assert s.cliente.post("/api/login", data={"password": "senha-de-teste"}).status_code == 200

    def test_nao_chegou_e_depois_o_ack_da_primeira(self, s):
        """A pessoa disse "não chegou" e liberou o reenvio. O ACK atrasado da
        primeira não desfaz a decisão, nem a saída que ela marcou."""
        linha = incerta(s, "3EB0INC_NC", "3EB0ORIG_NC")
        self._login(s)
        r = s.cliente.post(f"/api/simulations/{linha['id']}/entrega", json={"acao": "nao_chegou"})
        assert r.status_code == 200, r.text
        antes = s.linha(id=linha["id"])

        mandar_ack(s, "3EB0INC_NC", "DELIVERY_ACK")
        depois = s.linha(id=linha["id"])
        assert depois["delivery_status"] == antes["delivery_status"] == Delivery.RETRYING
        assert depois["delivery_resolution"] == "manual:nao_chegou"
        assert saida(s, linha["id"])["status"] == "failed"
        assert s.manager.reconciliar_entregas_incertas() == 0

    def test_chegou_e_depois_o_ack(self, s):
        linha = incerta(s, "3EB0INC_CH", "3EB0ORIG_CH")
        self._login(s)
        s.cliente.post(f"/api/simulations/{linha['id']}/entrega", json={"acao": "chegou"})
        mandar_ack(s, "3EB0INC_CH", "READ")
        depois = s.linha(id=linha["id"])
        assert depois["delivery_status"] == Delivery.DELIVERED
        assert depois["delivery_resolution"] == "manual:chegou"
        assert not logs(s, f"A entrega #{linha['id']} estava incerta"), (
            "promoveu de novo o que uma pessoa já tinha fechado")
        assert len(s.servidor.envios()) == 1


# ============================================ o que continua sem solução
class TestSemIdNaoHaReconciliacao:
    def test_timeout_sem_id_continua_esperando_uma_pessoa(self, tmp_path):
        """Timeout de leitura: a Evolution pode ter enviado, e o id nunca veio.
        Nenhum ACK pode ser ligado a esta entrega -- ela fica incerta."""
        s = Sistema(tmp_path).ligar()
        try:
            def cai(rota, payload):
                raise httpx.ReadTimeout("sem resposta")
            s.servidor.roteiro = [cai]
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0ORIG_TO")
            assert _aguardar(lambda: (s.linha(source_message_id="3EB0ORIG_TO") or {}).get(
                "delivery_status") == Delivery.UNCONFIRMED, timeout=20)
            for k in ("3EB0QUALQUER1", "3EB0QUALQUER2"):
                mandar_ack(s, k, "READ")
            assert s.manager.reconciliar_entregas_incertas() == 0
            linha = s.linha(source_message_id="3EB0ORIG_TO")
            assert linha["delivery_status"] == Delivery.UNCONFIRMED
            assert len(s.servidor.envios()) == 1
        finally:
            s.desligar()
