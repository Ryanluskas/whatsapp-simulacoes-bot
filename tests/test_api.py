"""Testes da API do painel: autenticacao, mascaramento e exports.

Cobrem os buracos de seguranca da versao anterior - rotas abertas, CPF completo
trafegando e sessao que nao sobrevivia a reinicio.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.events import EventHub
from app.manager import BotManager
from app.models import Stage, Status
from app.web import create_app
from tests.test_concurrency import CPFS, FakeSimulator, FakeWhatsApp, _config

SENHA = "senha-de-teste"


def _db_logs(db) -> list[dict]:
    return db.fetchall("SELECT message FROM logs ORDER BY id")


@pytest.fixture()
def cliente(tmp_path):
    config = _config(tmp_path)
    db = Database(config.db_path)
    hub = EventHub(db)
    manager = BotManager(config, db, hub)
    manager.whatsapp = FakeWhatsApp()
    manager.simulators = [FakeSimulator()]

    _semear(db)
    app = create_app(config, db, hub, manager)
    with TestClient(app) as client:
        yield client, db, manager


def _semear(db: Database) -> None:
    sim_id = db.insert("simulations", {
        "request_id": "REQ000001", "consultant_name": "Ryan", "chat_id": "grupo@g.us",
        "cpf": CPFS[0], "bank": "Santander", "contract": "998877", "customer_name": "Cliente A",
        "status": Status.COMPLETED, "stage": Stage.COMPLETED, "refin": "Sim",
        "reduction_value": 5230.55, "margin": "R$ 180,00", "installment_sum": 420.0,
        "installment_count": 72, "debt_sum": 18000.0, "contracts_count": 1,
        "contracts_json": json.dumps([{"contrato": "998877", "valor_parcela": "R$ 420,00"}]),
        "attempts": 1, "max_attempts": 2, "processing_seconds": 24.3,
        "raw_message": f"Fazer simulação\nCPF: {CPFS[0]}",
        "created_at": "2026-08-27T13:00:00Z", "updated_at": "2026-08-27T13:00:30Z",
        "started_at": "2026-08-27T13:00:05Z", "finished_at": "2026-08-27T13:00:29Z",
        "replied_at": "2026-08-27T13:00:30Z",
    })
    db.insert("simulations", {
        "request_id": "REQ000002", "consultant_name": "João", "chat_id": "grupo@g.us",
        "cpf": CPFS[1], "bank": "Santander", "contract": "112233",
        "status": Status.ERROR, "stage": Stage.ERROR, "error_message": "portal indisponível",
        "attempts": 2, "max_attempts": 2, "processing_seconds": 61.0,
        "created_at": "2026-08-27T14:00:00Z", "updated_at": "2026-08-27T14:01:01Z",
    })
    db.insert("messages", {
        "simulation_id": sim_id, "request_id": "REQ000001", "direction": "in",
        "chat_id": "grupo@g.us", "wa_message_id": "false_grupo@g.us_M1_x@c.us",
        "consultant_name": "Ryan", "sender_id": "x@c.us", "text": "Fazer simulação",
        "status": Stage.RECEIVED, "created_at": "2026-08-27T13:00:00Z",
    })
    db.insert("events", {
        "type": "job_done", "stage": Stage.COMPLETED, "level": "success",
        "title": "Simulação concluída", "detail": "Ryan", "request_id": "REQ000001",
        "simulation_id": sim_id, "consultant_name": "Ryan", "chat_id": "grupo@g.us",
        "payload_json": "{}", "created_at": "2026-08-27T13:00:29Z",
    })
    db.insert("logs", {
        "level": "INFO", "service": "bot", "message": "Solicitação REQ000001 concluída",
        "request_id": "REQ000001", "consultant": "Ryan", "created_at": "2026-08-27T13:00:29Z",
    })
    db.insert("consultants", {
        "name": "Ryan", "wa_id": "5567908887777@c.us", "phone": "5567908887777",
        "active": 1, "pinned_name": 0, "notes": "",
        "created_at": "2026-08-27T12:00:00Z", "updated_at": "2026-08-27T13:00:00Z",
        "last_seen_at": "2026-08-27T13:00:00Z",
    })


def _login(client) -> None:
    resposta = client.post("/api/login", data={"password": SENHA})
    assert resposta.status_code == 200


# ============================================================== autenticacao
class TestAutenticacao:
    ROTAS = [
        "/api/metrics", "/api/queue", "/api/simulations", "/api/simulations/1",
        "/api/consultants", "/api/reports", "/api/logs", "/api/system",
        "/api/monitor", "/api/bootstrap", "/api/whatsapp/status", "/api/whatsapp/qr",
        "/api/export?format=csv",
    ]

    @pytest.mark.parametrize("rota", ROTAS)
    def test_rota_protegida_sem_sessao(self, cliente, rota):
        client, _db, _m = cliente
        assert client.get(rota).status_code == 401

    def test_rotas_de_escrita_tambem_sao_protegidas(self, cliente):
        client, _db, _m = cliente
        assert client.post("/api/consultants", json={"name": "X"}).status_code == 401
        assert client.put("/api/consultants/1", json={"name": "X"}).status_code == 401
        assert client.delete("/api/consultants/1").status_code == 401
        assert client.post("/api/whatsapp/reconnect").status_code == 401

    def test_login_e_logout(self, cliente):
        client, _db, _m = cliente
        assert client.get("/api/session").json()["authenticated"] is False

        assert client.post("/api/login", data={"password": "errada"}).status_code == 401
        _login(client)

        sessao = client.get("/api/session").json()
        assert sessao["authenticated"] is True and sessao["user"] == "admin"
        assert client.get("/api/metrics").status_code == 200

        client.post("/api/logout")
        assert client.get("/api/metrics").status_code == 401

    def test_cookie_e_httponly(self, cliente):
        client, _db, _m = cliente
        resposta = client.post("/api/login", data={"password": SENHA})
        assert "httponly" in resposta.headers["set-cookie"].lower()
        assert "samesite=lax" in resposta.headers["set-cookie"].lower()

    def test_limite_de_tentativas_de_login(self, cliente):
        client, _db, _m = cliente
        codigos = [
            client.post("/api/login", data={"password": "errada"}).status_code
            for _ in range(12)
        ]
        assert 429 in codigos, "login sem limite de tentativas"

    def test_o_limite_nao_cai_com_x_forwarded_for_inventado(self, cliente):
        """O limite existe contra forca bruta. Um cabecalho que o proprio
        atacante escreve nao pode zera-lo: bastaria um IP diferente por
        tentativa para testar senha a noite inteira."""
        client, _db, _m = cliente
        codigos = [
            client.post("/api/login", data={"password": "errada"},
                        headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code
            for i in range(12)
        ]
        assert 429 in codigos, (
            "trocar o X-Forwarded-For zerou o limite de tentativas")

    def test_o_ip_registrado_no_log_nao_aceita_texto_do_cliente(self, cliente):
        """O que vai para o log do painel nao pode ser escrito pelo cliente."""
        client, _db, _m = cliente
        client.post("/api/login", data={"password": "errada"},
                    headers={"X-Forwarded-For": "1.2.3.4 SENHA ACEITA - login ok"})
        logs = _db_logs(_db)
        assert not any("SENHA ACEITA" in (linha.get("message") or "") for linha in logs), (
            "o cliente escreveu no log de auditoria pelo cabecalho")


# =============================================================== dados
class TestDadosSensiveis:
    def test_cpf_nao_trafega_completo(self, cliente):
        client, _db, _m = cliente
        _login(client)

        corpo = client.get("/api/simulations").text
        assert CPFS[0] not in corpo, "CPF completo vazou na listagem"
        assert "529.***.***-25" in corpo

        detalhe = client.get("/api/simulations/1").json()["simulation"]
        assert "cpf" not in detalhe
        assert detalhe["cpf_display"] == "529.***.***-25"

    def test_mensagem_original_nao_e_exposta_quando_mascarado(self, cliente):
        client, _db, _m = cliente
        _login(client)
        detalhe = client.get("/api/simulations/1").json()["simulation"]
        assert "raw_message" not in detalhe

    def test_telefone_do_consultor_e_mascarado(self, cliente):
        client, _db, _m = cliente
        _login(client)
        item = client.get("/api/consultants").json()["items"][0]
        assert "phone" not in item
        assert item["phone_display"] != "5567908887777"


# ================================================================== leitura
class TestEndpointsDeLeitura:
    def test_bootstrap_traz_tudo_para_a_primeira_pintura(self, cliente):
        client, _db, _m = cliente
        _login(client)
        dados = client.get("/api/bootstrap").json()
        for chave in ("metrics", "whatsapp", "queue", "system", "monitor", "labels"):
            assert chave in dados
        assert dados["labels"]["stages"]["completed"] == "Concluído"

    def test_metricas_refletem_o_banco(self, cliente):
        client, _db, _m = cliente
        _login(client)
        k = client.get("/api/metrics").json()["kpis"]
        assert k["total"] == 2 and k["completed"] == 1 and k["errors"] == 1
        assert k["success_rate"] == 50.0

    def test_detalhe_traz_timeline_mensagens_e_contratos(self, cliente):
        client, _db, _m = cliente
        _login(client)
        dados = client.get("/api/simulations/1").json()
        assert dados["timeline"][0]["stage_label"] == "Concluído"
        assert dados["messages"][0]["direction"] == "in"
        assert dados["contracts"][0]["contrato"] == "998877"

    def test_simulacao_inexistente(self, cliente):
        client, _db, _m = cliente
        _login(client)
        assert client.get("/api/simulations/9999").status_code == 404

    def test_filtros_e_paginacao(self, cliente):
        client, _db, _m = cliente
        _login(client)
        assert client.get("/api/simulations?status=error").json()["total"] == 1
        assert client.get("/api/simulations?consultant=Ryan").json()["total"] == 1
        assert client.get("/api/simulations?q=998877").json()["total"] == 1
        pagina = client.get("/api/simulations?limit=1&offset=0").json()
        assert pagina["total"] == 2 and len(pagina["items"]) == 1

    def test_relatorios_por_periodo(self, cliente):
        client, _db, _m = cliente
        _login(client)
        for periodo in ("today", "yesterday", "7d", "30d", "month"):
            resposta = client.get(f"/api/reports?period={periodo}")
            assert resposta.status_code == 200, periodo
            assert "general" in resposta.json()

    def test_logs_com_filtro(self, cliente):
        client, _db, _m = cliente
        _login(client)  # o proprio login grava um log de auditoria
        dados = client.get("/api/logs?level=INFO").json()
        assert dados["total"] >= 1
        assert "bot" in dados["services"]
        assert client.get("/api/logs?service=bot").json()["total"] == 1
        assert client.get("/api/logs?request_id=REQ000001").json()["total"] == 1
        assert client.get("/api/logs?level=ERROR").json()["total"] == 0

    def test_login_gera_trilha_de_auditoria(self, cliente):
        client, _db, _m = cliente
        client.post("/api/login", data={"password": "errada"})
        _login(client)
        mensagens = " ".join(
            item["message"] for item in client.get("/api/logs?service=auth").json()["items"]
        )
        assert "Login realizado" in mensagens
        assert "inválida" in mensagens


# ============================================================== consultores
class TestConsultores:
    def test_criar_editar_desativar(self, cliente):
        client, db, _m = cliente
        _login(client)

        novo = client.post("/api/consultants", json={"name": "Pedro", "phone": "67911112222"})
        assert novo.status_code == 200
        pid = novo.json()["id"]

        assert client.put(f"/api/consultants/{pid}", json={"name": "Pedro Silva"}).status_code == 200
        assert db.fetchone("SELECT * FROM consultants WHERE id=?", (pid,))["name"] == "Pedro Silva"

        assert client.delete(f"/api/consultants/{pid}").status_code == 200
        assert db.fetchone("SELECT * FROM consultants WHERE id=?", (pid,))["active"] == 0

    def test_nome_obrigatorio(self, cliente):
        client, _db, _m = cliente
        _login(client)
        assert client.post("/api/consultants", json={"name": "  "}).status_code == 400

    def test_telefone_duplicado_e_recusado(self, cliente):
        client, _db, _m = cliente
        _login(client)
        assert client.post(
            "/api/consultants", json={"name": "Outro", "phone": "5567908887777"}
        ).status_code == 409

    def test_consultor_inexistente(self, cliente):
        client, _db, _m = cliente
        _login(client)
        assert client.put("/api/consultants/999", json={"name": "X"}).status_code == 404
        assert client.delete("/api/consultants/999").status_code == 404

    def test_estatisticas_vem_junto(self, cliente):
        client, _db, _m = cliente
        _login(client)
        item = client.get("/api/consultants").json()["items"][0]
        assert item["total"] == 1 and item["completed"] == 1
        assert item["success_rate"] == 100.0


# ================================================================= exports
class TestExports:
    def test_csv(self, cliente):
        client, _db, _m = cliente
        _login(client)
        resposta = client.get("/api/export?format=csv&period=30d")
        assert resposta.status_code == 200
        texto = resposta.content.decode("utf-8")
        assert "Consultor" in texto and "REQ000001" in texto
        assert CPFS[0] not in texto, "CPF completo vazou no CSV"
        assert "529.***.***-25" in texto
        assert texto.startswith("﻿"), "sem BOM: o Excel abre com acentos quebrados"

    def test_xlsx(self, cliente):
        client, _db, _m = cliente
        _login(client)
        resposta = client.get("/api/export?format=xlsx&period=30d")
        assert resposta.status_code == 200
        assert resposta.content[:2] == b"PK"  # arquivo zip valido
        assert len(resposta.content) > 4000

    def test_pdf(self, cliente):
        client, _db, _m = cliente
        _login(client)
        resposta = client.get("/api/export?format=pdf&period=30d")
        assert resposta.status_code == 200
        assert resposta.content[:5] == b"%PDF-"

    def test_formato_invalido(self, cliente):
        client, _db, _m = cliente
        _login(client)
        assert client.get("/api/export?format=docx").status_code == 422


# =========================================================== comprovantes
class TestComprovantes:
    """A rota serve imagens de dentro de uma pasta e nada mais."""

    ATAQUES = [
        "../../../.env",
        "..%2F..%2F.env",
        "REQ1.png/../../../.env",
        "....//....//.env",
        "simulacoes.db",
        "REQ1.PNG.exe",
        "REQ.png",           # sem dígitos
        "REQ99999999999999.png",  # mais dígitos que o padrão aceita
    ]

    @pytest.mark.parametrize("nome", ATAQUES)
    def test_nomes_fora_do_padrao_sao_recusados(self, cliente, nome):
        client, _db, _m = cliente
        _login(client)
        resposta = client.get(f"/api/comprovantes/{nome}")
        assert resposta.status_code in (404, 400, 405), nome
        assert b"SESSION_SECRET" not in resposta.content

    def test_exige_sessao(self, cliente):
        client, _db, _m = cliente
        assert client.get("/api/comprovantes/REQ000001.png").status_code == 401

    def test_arquivo_inexistente_da_404(self, cliente):
        client, _db, _m = cliente
        _login(client)
        assert client.get("/api/comprovantes/REQ000999.png").status_code == 404

    def test_serve_a_imagem_quando_existe(self, cliente, tmp_path):
        from app.manager import COMPROVANTES_DIR

        client, _db, _m = cliente
        _login(client)
        alvo = COMPROVANTES_DIR / "REQ000777.png"
        alvo.write_bytes(b"\x89PNG\r\n\x1a\nconteudo-de-teste")
        try:
            resposta = client.get("/api/comprovantes/REQ000777.png")
            assert resposta.status_code == 200
            assert resposta.headers["content-type"] == "image/png"
            assert resposta.content.startswith(b"\x89PNG")
        finally:
            alvo.unlink(missing_ok=True)


# ================================================================ estaticos
class TestPainel:
    def test_index_e_servido(self, cliente):
        client, _db, _m = cliente
        resposta = client.get("/")
        assert resposta.status_code == 200
        assert "<!DOCTYPE html>" in resposta.text or "<!doctype html>" in resposta.text.lower()
