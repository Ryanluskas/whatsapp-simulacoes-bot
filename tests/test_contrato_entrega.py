"""Contrato de entrega: o que cada resposta da Evolution significa, sem servidor.

A pergunta de todo envio é **a mensagem pode ter saído?** Este arquivo trava,
como tabela, a resposta que o código dá a cada evento -- e as garantias que a
documentação promete (``quoted_ok`` só com prova, senha vazia não abre o
painel, diagnóstico sem segredo). Nenhum teste daqui chama a Evolution real.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import evolution as modulo
from app.evolution import (Categoria, EvolutionClient, classificar_falha_de_transporte,
                           classificar_resposta)
from app.models import Desfecho, QuoteStatus, ResultadoEnvio

GRUPO = "120363000000000001@g.us"
PEDIDO = "3EB0PEDIDO0000000001"


def _corpo(mensagem: str, status: int = 400) -> str:
    """O formato de erro da Evolution v2."""
    return json.dumps({"status": status, "error": "Bad Request",
                       "response": {"message": [mensagem]}})


# =========================================================== respostas HTTP
class TestCategoriaDeCadaResposta:
    """Uma linha por evento. ``categoria`` diz O QUE a resposta disse;
    ``desfecho`` diz o que o envio faz. As duas vêm da MESMA função."""

    @pytest.mark.parametrize("status,corpo,com_citacao,categoria", [
        # nada saiu: a citação foi recusada -> um único envio sem citação
        (400, _corpo("quoted message not found"), True, Categoria.QUOTE_REJECTED),
        (422, _corpo("instance.quoted.key is not allowed", 422), True, Categoria.QUOTE_REJECTED),
        (400, _corpo("contextInfo.stanzaId inválido"), True, Categoria.QUOTE_REJECTED),
        # nada saiu: validação -> imagem cai para texto; texto falha
        (400, _corpo('instance requires property "number"'), False, Categoria.VALIDATION_REJECTED),
        (400, _corpo("Owned media must be a url or base64"), False, Categoria.VALIDATION_REJECTED),
        (413, "payload too large", False, Categoria.VALIDATION_REJECTED),
        # pode ter saído: erro depois do envio vence qualquer marca de citação
        (400, "Invalid prisma.message.create() with quotedMessage", True, Categoria.POST_SEND_ERROR),
        (400, "database error while saving", False, Categoria.POST_SEND_ERROR),
        (400, "InvalidAccessKeyId: bucket evolution", False, Categoria.POST_SEND_ERROR),
        (400, "minio upload failed", True, Categoria.POST_SEND_ERROR),
        (400, "rabbitmq channel closed", False, Categoria.POST_SEND_ERROR),
        # pode ter saído: sem explicação
        (400, "Bad Request", False, Categoria.UNCERTAIN),
        (400, "Bad Request", True, Categoria.UNCERTAIN),
        (422, "", True, Categoria.UNCERTAIN),
        (500, "Internal Server Error", True, Categoria.UNCERTAIN),
        (502, "Bad Gateway", False, Categoria.UNCERTAIN),
        (504, "Gateway Timeout", False, Categoria.UNCERTAIN),
        # nada processado; repetir depois
        (408, "", False, Categoria.TRANSIENT),
        (429, "Too Many Requests", True, Categoria.TRANSIENT),
        (503, "Service Unavailable", True, Categoria.TRANSIENT),
        # nada processado; repetir não resolve
        (401, "unauthorized", False, Categoria.PERMANENT),
        (403, "forbidden", False, Categoria.PERMANENT),
        (404, "instance not found", False, Categoria.PERMANENT),
        (503, '{"error":"LICENSE_REQUIRED"}', False, Categoria.PERMANENT),
    ])
    def test_tabela(self, status, corpo, com_citacao, categoria):
        c = classificar_resposta(status, corpo, com_citacao=com_citacao)
        assert c.categoria == categoria, c
        assert c.desfecho == Categoria.DESFECHO[categoria]

    def test_quote_sozinho_nao_e_mais_citacao_recusada(self):
        """"quote" casava com qualquer texto e abria caminho para um reenvio."""
        c = classificar_resposta(400, "unquoted value in request", com_citacao=True)
        assert c.categoria != Categoria.QUOTE_REJECTED

    def test_marca_de_citacao_sem_quoted_no_payload_nao_e_recusa_de_citacao(self):
        c = classificar_resposta(400, _corpo("quoted message not found"), com_citacao=False)
        assert c.categoria != Categoria.QUOTE_REJECTED

    @pytest.mark.parametrize("excecao,categoria", [
        (httpx.ConnectError("recusada"), Categoria.TRANSIENT),
        (httpx.ConnectTimeout("demorou a conectar"), Categoria.TRANSIENT),
        (httpx.WriteError("corpo nao subiu"), Categoria.TRANSIENT),
        (httpx.ReadTimeout("subiu e a resposta nao veio"), Categoria.UNCERTAIN),
        (httpx.RemoteProtocolError("connection reset"), Categoria.UNCERTAIN),
        (httpx.ReadError("reset by peer"), Categoria.UNCERTAIN),
        (httpx.DecodingError("gzip cortado"), Categoria.UNCERTAIN),
    ])
    def test_falha_de_transporte(self, excecao, categoria):
        c = classificar_falha_de_transporte(excecao)
        assert c.categoria == categoria and c.desfecho == Categoria.DESFECHO[categoria]


# ================================================================ quoted_ok
def _cliente(*respostas):
    fila = list(respostas)
    enviados: list[dict] = []

    def servidor(request):
        enviados.append(json.loads(request.content) if request.content else {})
        resposta = fila.pop(0) if fila else httpx.Response(201, json={"key": {"id": "BAE5OK"}})
        if isinstance(resposta, Exception):
            raise resposta
        return resposta

    c = EvolutionClient("http://e:8080", "k", "allana", GRUPO,
                        client=httpx.Client(transport=httpx.MockTransport(servidor),
                                            base_url="http://e:8080"),
                        renderer=object())
    return c, enviados


def _com_stanza(stanza: str, key_id: str = "BAE5RESPOSTA") -> httpx.Response:
    return httpx.Response(201, json={
        "key": {"id": key_id, "remoteJid": GRUPO, "fromMe": True},
        "message": {"extendedTextMessage": {"text": "r", "contextInfo": {"stanzaId": stanza}}}})


class TestQuotedOkSoComProva:
    """``quoted_ok`` quer dizer "temos prova da citação" -- nunca "tentamos citar"."""

    @pytest.mark.parametrize("situacao,esperado", [
        (QuoteStatus.OK, True),
        (QuoteStatus.UNVERIFIED, False),
        (QuoteStatus.NOT_APPLIED, False),
        (QuoteStatus.FALLBACK, False),
        (QuoteStatus.NONE, False),
    ])
    def test_invariante_no_resultado(self, situacao, esperado):
        """Mesmo se uma camada errar e mandar quoted_ok=True, o resultado corrige."""
        r = ResultadoEnvio(ok=True, quoted_ok=True, quote_status=situacao)
        assert r.quoted_ok is esperado

    def test_ok_com_stanza_igual_ao_pedido(self):
        c, _ = _cliente(_com_stanza(PEDIDO))
        r = c.send(GRUPO, "g", "resposta", quote_message_id=PEDIDO)
        assert r.quote_status == QuoteStatus.OK and r.quoted_ok is True

    def test_unverified_sem_stanza(self):
        c, _ = _cliente(httpx.Response(201, json={"key": {"id": "BAE5X"}}))
        r = c.send(GRUPO, "g", "resposta", quote_message_id=PEDIDO)
        assert r.ok and r.quote_status == QuoteStatus.UNVERIFIED and r.quoted_ok is False

    def test_not_applied_com_stanza_de_outra_mensagem(self):
        c, _ = _cliente(_com_stanza("3EB0OUTRA"))
        r = c.send(GRUPO, "g", "resposta", quote_message_id=PEDIDO)
        assert r.ok and r.quote_status == QuoteStatus.NOT_APPLIED and r.quoted_ok is False

    def test_fallback_depois_da_citacao_recusada(self):
        c, enviados = _cliente(httpx.Response(400, text=_corpo("quoted message not found")),
                               httpx.Response(201, json={"key": {"id": "BAE5SEM"}}))
        r = c.send(GRUPO, "g", "resposta", quote_message_id=PEDIDO,
                   texto_sem_citacao="resposta\n↩ Consultor")
        assert len(enviados) == 2 and "quoted" not in enviados[1]
        assert r.ok and r.quote_status == QuoteStatus.FALLBACK and r.quoted_ok is False

    @pytest.mark.parametrize("resposta", [
        httpx.Response(500, text="Internal Server Error"),
        httpx.Response(201, json={"status": "PENDING"}),        # 2xx sem key.id
        httpx.ReadTimeout("subiu e nao voltou"),
    ])
    def test_falha_nunca_e_quoted_ok(self, resposta):
        c, _ = _cliente(resposta)
        r = c.send(GRUPO, "g", "resposta", quote_message_id=PEDIDO)
        assert not r.ok and r.quoted_ok is False and r.quote_status == QuoteStatus.UNVERIFIED


# ========================================================= depois do POST
class TestExcecaoDepoisDoPostAceito:
    """A Evolution aceitou; o código quebrou ao LER a resposta.

    Antes a exceção subia até o manager, que a tratava como "transitório" --
    e o laço de reenvio mandava de novo uma mensagem que tinha saído.
    """

    def test_com_key_id_e_entregue_sem_prova_de_citacao(self, monkeypatch):
        def quebra(*_a, **_k):
            raise KeyError("contextInfo")
        monkeypatch.setattr(modulo, "conferir_citacao", quebra)
        c, enviados = _cliente(_com_stanza(PEDIDO, key_id="BAE5SAIU"))
        r = c.send(GRUPO, "g", "resposta", quote_message_id=PEDIDO)
        assert len(enviados) == 1
        assert r.ok and r.desfecho == Desfecho.ENTREGUE and r.enviado_id == "BAE5SAIU"
        assert r.quote_status == QuoteStatus.UNVERIFIED and r.quoted_ok is False

    def test_sem_key_id_legivel_e_incerta_nunca_transitoria(self, monkeypatch):
        def quebra(*_a, **_k):
            raise RuntimeError("resposta inesperada")
        monkeypatch.setattr(modulo, "extrair_key_id", quebra)
        c, enviados = _cliente(httpx.Response(201, json={"key": {"id": "BAE5X"}}))
        r = c.send(GRUPO, "g", "resposta", quote_message_id=PEDIDO)
        assert len(enviados) == 1
        assert not r.ok and r.desfecho == Desfecho.INCERTA
        assert r.sem_prova is True and r.transitorio is False


# ================================================================== senha
class TestSenhaVaziaNaoAbreOPainel:
    """``DASHBOARD_PASSWORD=`` vazio: antes a senha vazia VALIA (sha256 de "" == sha256 de "")."""

    def test_check_password(self):
        from app.security import check_password

        assert check_password("", "") is False
        assert check_password("   ", "   ") is False
        assert check_password("senha-forte-123", "senha-forte-123") is True

    def test_login_recusa_e_explica(self, tmp_path):
        from dataclasses import replace

        from fastapi.testclient import TestClient

        from app.db import Database
        from app.events import EventHub
        from app.manager import BotManager
        from app.web import create_app
        from tests.test_concurrency import _config

        config = replace(_config(tmp_path), dashboard_password="")
        db = Database(config.db_path)
        hub = EventHub(db)
        with TestClient(create_app(config, db, hub, BotManager(config, db, hub))) as cliente:
            # Campo vazio o FastAPI já rejeita (422); o que importa é que NADA abre.
            assert cliente.post("/api/login", data={"password": ""}).status_code != 200
            r = cliente.post("/api/login", data={"password": "qualquer-coisa"})
            assert r.status_code == 401 and "DASHBOARD_PASSWORD" in r.json()["detail"]
            assert cliente.get("/api/bootstrap").status_code == 401

    def test_partida_avisa_em_localhost_e_recusa_exposto(self, tmp_path):
        from dataclasses import replace

        from app.security import problemas_de_seguranca
        from tests.test_concurrency import _config

        local = replace(_config(tmp_path), dashboard_password="", web_host="127.0.0.1")
        problemas, avisos = problemas_de_seguranca(local)
        assert not problemas and any("não aceita login" in a for a in avisos)
        exposto = replace(local, web_host="0.0.0.0")
        problemas, _ = problemas_de_seguranca(exposto)
        assert any("DASHBOARD_PASSWORD" in p for p in problemas)


# ============================================================ diagnóstico
CHAVE = "chave-secreta-que-nao-pode-vazar-0123456789"
JID_REAL = "120363999888777666@g.us"
TOKEN = "token-do-webhook-que-nao-pode-vazar-987"


def _config_evolution(tmp_path, **mudancas):
    from dataclasses import replace

    from tests.test_concurrency import _config

    base = replace(_config(tmp_path, whatsapp_mode="evolution", evolution_group_jid=JID_REAL,
                           evolution_webhook_token=TOKEN),
                   evolution_api_key=CHAVE, evolution_url="http://evolution-real:8080")
    return replace(base, **mudancas)


class TestDiagnosticoSeguro:
    def test_sem_configuracao_diz_o_que_falta(self, tmp_path):
        from app.evolution_diagnostico import diagnosticar

        r = diagnosticar(_config_evolution(tmp_path, evolution_api_key="", evolution_url=""))
        assert r["configured"] is False
        assert "EVOLUTION_URL" in r["reason"] and "EVOLUTION_API_KEY" in r["reason"]

    def test_pronto_sem_expor_nada(self, tmp_path):
        from app.evolution_diagnostico import diagnosticar, pronto

        def servidor(request):
            assert request.headers.get("apikey") == CHAVE
            if "/connectionState/" in request.url.path:
                return httpx.Response(200, json={"instance": {"state": "open"}})
            if "/webhook/find/" in request.url.path:
                return httpx.Response(200, json={
                    "enabled": True, "url": "http://192.168.0.10:8000/webhook/whatsapp",
                    "headers": {"X-Webhook-Token": TOKEN}})
            return httpx.Response(404)

        cliente = httpx.Client(transport=httpx.MockTransport(servidor),
                               base_url="http://evolution-real:8080",
                               headers={"apikey": CHAVE})
        r = diagnosticar(_config_evolution(tmp_path), client=cliente)
        assert r["reachable"] is True and r["api_key_valid"] is True
        assert r["state"] == "open" and r["webhook"] == {"configured": True, "points_to_bot": True}
        assert r["group_configured"] is True and r["webhook_token_configured"] is True
        assert pronto(r) is True
        saida = json.dumps(r)
        for segredo in (CHAVE, TOKEN, JID_REAL, "120363999888777666", "192.168.0.10",
                        "webhook/whatsapp"):
            assert segredo not in saida, f"o diagnóstico expôs {segredo!r}"

    def test_chave_recusada(self, tmp_path):
        from app.evolution_diagnostico import diagnosticar, pronto

        cliente = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)),
                               base_url="http://evolution-real:8080")
        r = diagnosticar(_config_evolution(tmp_path), client=cliente)
        assert r["reachable"] is True and r["api_key_valid"] is False
        assert pronto(r) is False and CHAVE not in json.dumps(r)

    def test_evolution_fora_do_ar(self, tmp_path):
        from app.evolution_diagnostico import diagnosticar

        def cai(request):
            raise httpx.ConnectError("sem rota")

        cliente = httpx.Client(transport=httpx.MockTransport(cai),
                               base_url="http://evolution-real:8080")
        r = diagnosticar(_config_evolution(tmp_path), client=cliente)
        assert r["configured"] is True and r["reachable"] is False


# ============================================================ observador
class TestObservadorDaEntregaSemDadoPessoal:
    """``ferramentas/observar_entrega.py`` é usado no teste real e colado em
    registro: não pode levar CPF, nome, telefone, JID nem texto de mensagem."""

    def test_so_campos_seguros(self, tmp_path):
        from app.db import Database
        from ferramentas.observar_entrega import observar

        db = Database(tmp_path / "t.db")
        sid = db.insert("simulations", {
            "request_id": "REQ000901", "consultant_name": "Consultor Teste",
            "chat_id": "120363000000000009@g.us", "sender_id": "5562900000009@s.whatsapp.net",
            "source_message_id": "3EB0PEDIDO", "cpf": "52998224725",
            "customer_name": "Cliente Teste Unico", "phone": "62900000009",
            "raw_message": "Cliente Teste Unico\n529.982.247-25", "status": "completed",
            "stage": "completed", "result_ok": 1, "delivery_status": "delivered",
            "quote_status": "ok", "sent_message_id": "BAE5RESPOSTA",
            "error_message": "CPF 529.982.247-25 nao localizado",
            "created_at": "2026-09-17T10:00:00Z", "updated_at": "2026-09-17T10:00:05Z"})
        db.insert("messages", {
            "simulation_id": sid, "request_id": "REQ000901", "direction": "out", "kind": "text",
            "chat_id": "120363000000000009@g.us", "text": "Cliente Teste Unico 529.982.247-25",
            "status": "completed", "wa_message_id": "BAE5RESPOSTA",
            "origin_message_id": "3EB0PEDIDO", "created_at": "2026-09-17T10:00:05Z"})

        resultado = observar(db.path, "REQ000901")
        saida = json.dumps(resultado, ensure_ascii=False)
        assert resultado["encontrada"] is True
        assert resultado["solicitacao"]["sent_message_id"] == "BAE5RESPOSTA"
        assert resultado["mensagens"][0]["origin_message_id"] == "3EB0PEDIDO"
        for proibido in ("52998224725", "529.982.247-25", "Cliente Teste Unico",
                         "62900000009", "120363000000000009", "5562900000009"):
            assert proibido not in saida, f"o observador expôs {proibido!r}"

    def test_request_inexistente(self, tmp_path):
        from app.db import Database
        from ferramentas.observar_entrega import observar

        Database(tmp_path / "t.db")
        assert observar(tmp_path / "t.db", "REQ999999")["encontrada"] is False
