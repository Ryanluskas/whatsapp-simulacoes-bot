"""Testes das regras que sustentam o sistema.

Cada bloco cobre um defeito real encontrado na auditoria, para que ele nao
volte silenciosamente.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from app import analytics
from app.actor import ActorStopped, ThreadActor
from app.clock import day_bounds, parse_iso, range_bounds, to_iso, utc_now
from app.consultants import ConsultantRepository
from app.db import Database
from app.events import EventHub
from app.formatter import format_brl, format_result
from app.models import ParsedRequest, SimulationJob, SimulationResult, Status
from app.parser import is_valid_cpf, parse_request
from app.security import RateLimiter, SessionManager, mask_cpf, redact
from app.state_store import StateStore
from app.whatsapp import clean_text, parse_data_id, parse_pre_plain

TZ = ZoneInfo("America/Sao_Paulo")
CPF_VALIDO = "52998224725"  # CPF de teste com digitos verificadores corretos


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(tmp_path / "teste.db")


# ============================================================== identificacao
class TestIdentificacaoDoConsultor:
    """O bug mais grave da versao anterior: sender_id virava a string 'false'."""

    def test_data_id_de_grupo_extrai_o_remetente_real(self):
        data_id = "false_120363044556677889@g.us_3EB0A1B2C3D4E5F6_5567988887777@c.us"
        chat, sender = parse_data_id(data_id)
        assert chat == "120363044556677889@g.us"
        assert sender == "5567988887777@c.us"
        assert sender != "false"

    def test_data_id_de_conversa_individual(self):
        chat, sender = parse_data_id("false_5511999998888@c.us_3EB0FFEE")
        assert chat == "5511999998888@c.us"
        assert sender == "5511999998888@c.us"

    def test_data_id_invalido_nao_explode(self):
        assert parse_data_id("") == ("", "")
        assert parse_data_id("lixo") == ("", "")

    def test_pre_plain_text_extrai_nome_e_hora(self):
        hora, nome = parse_pre_plain("[15:32, 27/08/2026] Ryan Souza: ")
        assert nome == "Ryan Souza"
        assert hora == "15:32, 27/08/2026"

    def test_consultores_distintos_nao_colidem(self, db):
        """Antes, todos caiam na mesma linha porque phone era sempre 'false'."""
        repo = ConsultantRepository(db)
        a = repo.resolve("5567988887777@c.us", "Ryan")
        b = repo.resolve("5511977776666@c.us", "João")
        c = repo.resolve("5567988887777@c.us", "Ryan")

        assert a["id"] != b["id"]
        assert a["id"] == c["id"]
        assert a["name"] == "Ryan"
        assert b["name"] == "João"
        assert db.scalar("SELECT COUNT(*) FROM consultants") == 2

    def test_nome_editado_no_painel_nao_e_sobrescrito(self, db):
        repo = ConsultantRepository(db)
        criado = repo.resolve("5567988887777@c.us", "Ryan")
        repo.update(criado["id"], name="Ryan Souza (Sênior)")
        depois = repo.resolve("5567988887777@c.us", "Ryan")
        assert depois["name"] == "Ryan Souza (Sênior)"

    def test_cadastro_manual_e_vinculado_ao_jid_no_primeiro_contato(self, db):
        repo = ConsultantRepository(db)
        manual_id = repo.create("Pedro", phone="5567911112222")
        db.update("consultants", {"wa_id": None}, {"id": manual_id})
        resolvido = repo.resolve("5567911112222@c.us", "Pedro P.")
        assert resolvido["id"] == manual_id
        assert resolvido["name"] == "Pedro"  # nome fixado no cadastro manual


# ==================================================================== parser
class TestParser:
    def test_formato_real_do_grupo(self):
        """Nome / órgão / CPF — sem rótulos e sem palavra-gatilho.

        É o formato dos prints do grupo "Santander Capital Simulações".
        A versão anterior do parser ignoraria todas estas mensagens.
        """
        pedido, faltando = parse_request("Paulo Testes\nAmapá\n182.841.754.87", "Tester")
        assert faltando == []
        assert pedido.customer_name == "Paulo Testes"
        assert pedido.origin == "Amapá"
        assert pedido.cpf == "18284175487"
        assert pedido.consultant_name == "Tester"
        assert pedido.bank == "Santander"   # assumido: o grupo é de um banco só
        assert pedido.contract == ""        # o contrato é resultado, não entrada

    def test_formato_real_sem_orgao(self):
        pedido, faltando = parse_request("LUIZ FERNANDO TESTE\n\n31611176034", "Camila")
        assert faltando == []
        assert pedido.customer_name == "LUIZ FERNANDO TESTE"
        assert pedido.origin == ""
        assert pedido.cpf == "31611176034"

    def test_so_o_cpf_ainda_e_um_pedido(self):
        pedido, faltando = parse_request("11278412972", "Camila")
        assert faltando == []
        assert pedido.cpf == "11278412972"
        assert pedido.customer_name == "Lead"

    def test_formato_rotulado_continua_aceito(self):
        texto = (
            "Ryan\n"
            f"CPF: {CPF_VALIDO}\n"
            "Banco: Santander\n"
            "Contrato: 123456\n"
            "Telefone: 67999998888\n"
            "Fazer simulação"
        )
        pedido, faltando = parse_request(texto, "Contato")
        assert faltando == []
        assert pedido.cpf == CPF_VALIDO
        assert pedido.contract == "123456"
        assert pedido.phone == "67999998888"

    def test_conversa_do_grupo_nao_vira_pedido(self):
        """Sem CPF não há pedido — é assim que separamos conversa de solicitação."""
        for conversa in ("bom dia pessoal", "alguém já mandou o relatório?",
                         "libera nada", "sem matricula", "obrigado!",
                         "NAO ELEGIVEL - OFERTAR CONSIG REORGANIZACAO",
                         "POSSUI PRODUTO DE RENEGOCIACAO"):
            pedido, faltando = parse_request(conversa, "Contato")
            assert pedido is None and faltando == [], conversa

    def test_gatilho_sem_cpf_reporta_pendencia(self):
        pedido, faltando = parse_request("Fazer simulação\nBanco: Santander", "Contato")
        assert pedido is None
        assert "CPF" in faltando

    def test_cpf_invalido_e_recusado_antes_de_consultar(self):
        pedido, faltando = parse_request("Cliente Teste\n111.111.111-11", "X")
        assert pedido is None
        assert any("válido" in f for f in faltando)

    def test_validacao_de_cpf(self):
        assert is_valid_cpf(CPF_VALIDO)
        assert is_valid_cpf("529.982.247-25")
        assert not is_valid_cpf("52998224726")
        assert not is_valid_cpf("00000000000")
        assert not is_valid_cpf("123")

    def test_apelidos_de_campo(self):
        texto = (
            "simular\n"
            f"cpf/cnpj: {CPF_VALIDO}\n"
            "Instituição: Santander\n"
            "Nº do contrato: 77 88 99\n"
            "Cel: (67) 99999-8888"
        )
        pedido, faltando = parse_request(texto, "Fallback")
        assert faltando == []
        assert pedido.contract == "778899"
        assert pedido.phone == "67999998888"

    def test_gatilho_opcional_nao_atrapalha(self):
        """A palavra continua sendo aceita, só deixou de ser obrigatória."""
        for gatilho in ("Fazer simulação", "fazer simulacao", "SIMULAR", "consultar"):
            pedido, _ = parse_request(f"Cliente X\n{CPF_VALIDO}\n{gatilho}", "X")
            assert pedido is not None and pedido.cpf == CPF_VALIDO, gatilho

    def test_require_trigger_ligado_volta_a_exigir_a_palavra(self):
        pedido, _ = parse_request(f"Cliente X\n{CPF_VALIDO}", "X", require_trigger=True)
        assert pedido is None
        pedido, _ = parse_request(f"Cliente X\n{CPF_VALIDO}\nsimular", "X",
                                  require_trigger=True)
        assert pedido is not None

    def test_cpf_em_varios_formatos(self):
        """O grupo escreve o CPF de três jeitos diferentes."""
        for escrito in ("31611176034", "182.841.754.87", "529.982.247-25"):
            pedido, _ = parse_request(f"Cliente\n{escrito}", "X")
            assert pedido is not None, escrito
            assert len(pedido.cpf) == 11

    def test_telefone_nao_e_confundido_com_cpf(self):
        """11 dígitos que não passam nos verificadores não viram pedido."""
        pedido, faltando = parse_request("Cliente\n67999998888", "X")
        assert pedido is None
        assert faltando  # reporta pendência em vez de simular lixo


# =============================================================== state store
class TestStateStore:
    def test_primeira_leitura_cria_linha_de_base(self, tmp_path):
        """Sem isso, o bot responde as 30 ultimas mensagens antigas do grupo."""
        store = StateStore(tmp_path / "state.json")
        antigas = [f"false_g@g.us_MSG{i}_x@c.us" for i in range(30)]

        assert store.baseline_if_new("grupo@g.us", antigas) is True
        assert all(store.has_seen(m) for m in antigas)

        # Segunda leitura ja' processa normalmente
        assert store.baseline_if_new("grupo@g.us", antigas) is False
        assert not store.has_seen("false_g@g.us_NOVA_x@c.us")

    def test_persistencia_entre_reinicios(self, tmp_path):
        caminho = tmp_path / "state.json"
        store = StateStore(caminho)
        store.baseline_if_new("grupo@g.us", ["m1"])
        store.mark_seen("m2")

        recarregado = StateStore(caminho)
        assert recarregado.has_seen("m1")
        assert recarregado.has_seen("m2")
        assert recarregado.is_baselined("grupo@g.us")

    def test_arquivo_corrompido_nao_derruba(self, tmp_path):
        caminho = tmp_path / "state.json"
        caminho.write_text("{ isso nao e json", encoding="utf-8")
        store = StateStore(caminho)
        assert not store.has_seen("qualquer")

    def test_limite_de_memoria(self, tmp_path):
        store = StateStore(tmp_path / "state.json", max_seen=10)
        for i in range(50):
            store.mark_seen(f"m{i}")
        assert store.has_seen("m49")
        assert not store.has_seen("m0")


# ============================================================ modelo de ator
class _ContadorActor(ThreadActor):
    """Simula o recurso preso a uma thread, como o Playwright sync."""

    def __init__(self):
        super().__init__("contador")
        self.owner_thread_ids: set[int] = set()
        self.valor = 0

    def run(self):
        while not self.stopping:
            if not self._drain(0.1):
                return

    def _incrementar(self, quanto: int) -> int:
        self.owner_thread_ids.add(threading.get_ident())
        atual = self.valor
        time.sleep(0.005)  # janela para uma condicao de corrida aparecer
        self.valor = atual + quanto
        return self.valor

    def incrementar(self, quanto: int = 1) -> int:
        return self.call(self._incrementar, quanto, timeout=10)


class TestModeloDeAtor:
    """Garante a invariante que a versao anterior violava."""

    def test_tudo_executa_na_thread_dona(self):
        actor = _ContadorActor()
        actor.start()
        try:
            resultados = []
            threads = [
                threading.Thread(target=lambda: resultados.append(actor.incrementar()))
                for _ in range(20)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            assert actor.valor == 20, "houve corrida: o recurso foi tocado de fora"
            assert len(actor.owner_thread_ids) == 1, "mais de uma thread tocou o recurso"
            assert threading.get_ident() not in actor.owner_thread_ids
            assert sorted(resultados) == list(range(1, 21))
        finally:
            actor.stop()

    def test_erro_e_propagado_para_quem_chamou(self):
        actor = _ContadorActor()
        actor.start()
        try:
            def explode():
                raise ValueError("falha proposital")

            with pytest.raises(ValueError, match="falha proposital"):
                actor.call(explode, timeout=5)
            assert actor.running, "o ator deve sobreviver a um comando com erro"
        finally:
            actor.stop()

    def test_chamada_apos_parada_e_recusada(self):
        actor = _ContadorActor()
        actor.start()
        actor.stop()
        with pytest.raises(ActorStopped):
            actor.incrementar()

    def test_chamada_de_dentro_nao_gera_deadlock(self):
        actor = _ContadorActor()
        actor.start()
        try:
            def reentrante():
                return actor.call(actor._incrementar, 5, timeout=5)

            assert actor.call(reentrante, timeout=5) == 5
        finally:
            actor.stop()


# ========================================================= tempo e relatorios
class TestFusoHorario:
    def test_hoje_respeita_o_fuso_local(self):
        inicio, fim = day_bounds(TZ)
        abertura = parse_iso(inicio)
        # No Brasil (UTC-3) o dia local comeca as 03:00 UTC.
        assert abertura.hour == 3
        assert (parse_iso(fim) - abertura) == timedelta(days=1)

    def test_periodos_nomeados(self):
        for periodo in ("today", "yesterday", "7d", "30d", "month"):
            inicio, fim = range_bounds(periodo, TZ)
            assert inicio < fim, periodo

    def test_periodo_personalizado_inclui_o_dia_final_inteiro(self):
        inicio, fim = range_bounds("custom", TZ, "2026-08-01", "2026-08-31")
        assert inicio < fim
        assert parse_iso(fim) - parse_iso(inicio) == timedelta(days=31)

    def test_datas_invertidas_sao_corrigidas(self):
        inicio, fim = range_bounds("custom", TZ, "2026-08-31", "2026-08-01")
        assert inicio < fim


# ==================================================================== banco
class TestBanco:
    def test_migracao_preserva_dados_do_schema_antigo(self, tmp_path):
        caminho = tmp_path / "antigo.db"
        legado = sqlite3.connect(str(caminho))
        legado.executescript(
            """
            CREATE TABLE simulations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT UNIQUE NOT NULL,
                consultant_name TEXT, sender_id TEXT, sender_name TEXT,
                cpf TEXT, bank TEXT, contract TEXT, phone TEXT,
                status TEXT NOT NULL DEFAULT 'received',
                error_message TEXT, reduction_value REAL, margin TEXT,
                installment_sum REAL, installment_count INTEGER, debt_sum REAL,
                contracts_count INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
                raw_message TEXT, created_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT, processing_seconds REAL
            );
            CREATE TABLE consultants (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                phone TEXT UNIQUE, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, simulation_id INTEGER,
                direction TEXT NOT NULL, consultant_name TEXT, sender_id TEXT,
                text TEXT, status TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL,
                service TEXT, message TEXT, request_id TEXT, consultant TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO simulations (request_id, consultant_name, status, created_at)
                 VALUES ('REQ000001', 'Ryan', 'completed', '2026-08-20T12:00:00Z');
            INSERT INTO consultants (name, phone, active, created_at)
                 VALUES ('Ryan', 'false', 1, '2026-08-20T12:00:00Z');
            """
        )
        legado.commit()
        legado.close()

        db = Database(caminho)

        assert db.scalar("SELECT COUNT(*) FROM simulations") == 1
        linha = db.fetchone("SELECT * FROM simulations WHERE request_id='REQ000001'")
        assert linha["consultant_name"] == "Ryan"
        assert "stage" in linha and "max_attempts" in linha

        # O telefone lixo 'false' e' limpo para nao colidir no UNIQUE
        consultor = db.fetchone("SELECT * FROM consultants WHERE name='Ryan'")
        assert consultor["phone"] == ""
        assert consultor["wa_id"] is None

    def test_contador_atomico_sob_concorrencia(self, db):
        erros: list[Exception] = []

        def trabalhar():
            try:
                for _ in range(25):
                    db.bump_meta("contador", 1)
            except Exception as exc:
                erros.append(exc)

        threads = [threading.Thread(target=trabalhar) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not erros, erros
        assert db.get_meta_int("contador") == 200

    def test_escrita_concorrente_nao_gera_database_locked(self, db):
        erros: list[Exception] = []

        def inserir(indice: int):
            try:
                for i in range(20):
                    db.insert(
                        "logs",
                        {
                            "level": "INFO", "service": "t", "message": f"{indice}-{i}",
                            "request_id": "", "consultant": "", "created_at": to_iso(utc_now()),
                        },
                    )
            except Exception as exc:
                erros.append(exc)

        threads = [threading.Thread(target=inserir, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not erros, erros
        assert db.scalar("SELECT COUNT(*) FROM logs") == 120


# ================================================================= eventos
class TestBarramentoDeEventos:
    def test_evento_e_persistido_e_entregue(self, db):
        hub = EventHub(db)
        sub = hub.subscribe()
        hub.publish("job_done", {"x": 1}, request_id="REQ000001", title="Concluído")

        recebido = sub.get(timeout=2)
        assert recebido["type"] == "job_done"
        assert recebido["payload"]["x"] == 1
        assert len(hub.recent(10)) == 1
        assert len(hub.timeline("REQ000001")) == 1

    def test_eventos_transitorios_nao_vao_para_o_banco(self, db):
        hub = EventHub(db)
        hub.publish("metrics", {"a": 1}, persist=False)
        hub.publish("queue_update", {"b": 2})
        assert hub.recent(10) == []

    def test_fila_cheia_descarta_o_mais_antigo_em_vez_de_vazar(self, db):
        """A versao antiga acumulava eventos sem limite quando ninguem estava conectado."""
        hub = EventHub(db)
        sub = hub.subscribe(maxsize=5)
        for i in range(50):
            hub.publish("log", {"i": i}, persist=False)

        assert sub.queue.qsize() == 5
        assert sub.dropped == 45
        assert sub.get(timeout=1)["payload"]["i"] == 45  # ficaram os mais recentes

    def test_assinante_removido_para_de_receber(self, db):
        hub = EventHub(db)
        sub = hub.subscribe()
        sub.close()
        hub.publish("log", {}, persist=False)
        assert hub.subscriber_count == 0
        assert sub.get(timeout=0.2) is None


# ================================================================ seguranca
class TestSeguranca:
    def test_token_valido_e_aceito(self):
        s = SessionManager("segredo-forte", ttl_hours=1)
        assert s.verify(s.issue()) == "admin"

    def test_token_adulterado_e_recusado(self):
        s = SessionManager("segredo-forte", ttl_hours=1)
        token = s.issue()
        corpo, assinatura = token.rsplit(".", 1)
        assert s.verify(f"{corpo}.{'A' * len(assinatura)}") is None
        assert s.verify("admin.1.2.3") is None
        assert s.verify(None) is None

    def test_token_de_outro_segredo_e_recusado(self):
        emissor = SessionManager("segredo-a", ttl_hours=1)
        verificador = SessionManager("segredo-b", ttl_hours=1)
        assert verificador.verify(emissor.issue()) is None

    def test_revogacao(self):
        s = SessionManager("segredo", ttl_hours=1)
        token = s.issue()
        s.revoke(token)
        assert s.verify(token) is None

    def test_sessao_expirada(self):
        s = SessionManager("segredo", ttl_hours=1)
        s._ttl = -1
        assert s.verify(s.issue()) is None

    def test_limite_de_tentativas_de_login(self):
        limiter = RateLimiter(max_attempts=3, window_seconds=60)
        assert all(limiter.allow("1.2.3.4") for _ in range(3))
        assert not limiter.allow("1.2.3.4")
        assert limiter.allow("5.6.7.8")  # outro IP nao e' afetado
        limiter.reset("1.2.3.4")
        assert limiter.allow("1.2.3.4")

    def test_mascara_de_cpf(self):
        assert mask_cpf(CPF_VALIDO) == "529.***.***-25"
        assert mask_cpf("") == "-"
        assert mask_cpf("123") == "***"

    def test_cpf_e_removido_dos_logs(self):
        limpo = redact(f"Consulta do CPF {CPF_VALIDO} falhou")
        assert CPF_VALIDO not in limpo
        assert "529.***.***-25" in limpo
        assert redact("529.982.247-25") == "529.***.***-25"


# ================================================================ formatacao
class TestFormatacao:
    def test_valores_em_real(self):
        assert format_brl(1234.5) == "R$ 1.234,50"
        assert format_brl(0) == "R$ 0,00"
        assert format_brl(None) == "R$ 0,00"
        assert format_brl(1_000_000) == "R$ 1.000.000,00"

    def test_resposta_identifica_consultor_e_solicitacao(self):
        """Requisito: uma solicitacao nunca pode ser confundida com outra."""
        pedido = ParsedRequest("Ryan", CPF_VALIDO, "Santander", "998877")
        job = SimulationJob(pedido, _mensagem_falsa(), "REQ000182", 182)
        texto = format_result(
            SimulationResult(job=job, ok=True, status="Sim", reduction_value=5000.0), "REQ000182"
        )
        assert "Ryan" in texto
        assert "REQ000182" in texto
        assert CPF_VALIDO not in texto  # CPF sai mascarado
        assert "529.***.***-25" in texto
        assert "R$ 5.000,00" in texto

    def test_resposta_de_erro_tambem_identifica(self):
        pedido = ParsedRequest("João", CPF_VALIDO, "Santander", "111")
        job = SimulationJob(pedido, _mensagem_falsa(), "REQ000200", 200)
        texto = format_result(
            SimulationResult(job=job, ok=False, status="Erro", error="portal fora do ar"),
            "REQ000200",
        )
        assert "João" in texto and "REQ000200" in texto and "portal fora do ar" in texto


# ================================================================ analytics
class TestRelatorios:
    def test_series_vazias_quando_nao_ha_dados(self, db):
        snap = analytics.dashboard_snapshot(db, TZ)
        assert snap["kpis"]["total"] == 0
        assert snap["by_day"] == []
        assert snap["by_hour"] == []
        assert snap["by_consultant"] == []

    def test_kpis_com_dados_reais(self, db):
        agora = utc_now()
        for i in range(5):
            _inserir_simulacao(db, f"REQ{i:06d}", "Ryan", Status.COMPLETED,
                               to_iso(agora - timedelta(minutes=i)), segundos=20.0 + i)
        for i in range(2):
            _inserir_simulacao(db, f"REQE{i:05d}", "João", Status.ERROR,
                               to_iso(agora - timedelta(minutes=i)))

        inicio, fim = range_bounds("30d", TZ)
        k = analytics.kpis(db, TZ, inicio, fim)
        assert k["total"] == 7
        assert k["completed"] == 5
        assert k["errors"] == 2
        assert k["success_rate"] == round(5 / 7 * 100, 1)
        assert k["avg_seconds"] == 22.0

    def test_agrupamento_por_consultor(self, db):
        agora = to_iso(utc_now())
        _inserir_simulacao(db, "R1", "Ryan", Status.COMPLETED, agora)
        _inserir_simulacao(db, "R2", "Ryan", Status.ERROR, agora)
        _inserir_simulacao(db, "R3", "João", Status.COMPLETED, agora)

        inicio, fim = range_bounds("30d", TZ)
        por_consultor = {c["nome"]: c for c in analytics.by_consultant(db, inicio, fim)}
        assert por_consultor["Ryan"]["total"] == 2
        assert por_consultor["Ryan"]["errors"] == 1
        assert por_consultor["Ryan"]["success_rate"] == 50.0
        assert por_consultor["João"]["success_rate"] == 100.0

    def test_dias_sem_movimento_aparecem_como_zero(self, db):
        agora = utc_now()
        _inserir_simulacao(db, "A", "Ryan", Status.COMPLETED, to_iso(agora - timedelta(days=4)))
        _inserir_simulacao(db, "B", "Ryan", Status.COMPLETED, to_iso(agora))

        inicio, fim = range_bounds("30d", TZ)
        serie = analytics.by_day(db, TZ, inicio, fim)
        assert len(serie) == 5
        assert serie[0]["total"] == 1 and serie[-1]["total"] == 1
        assert all(d["total"] == 0 for d in serie[1:-1])

    def test_filtros_do_historico(self, db):
        agora = to_iso(utc_now())
        _inserir_simulacao(db, "R1", "Ryan", Status.COMPLETED, agora, cpf=CPF_VALIDO,
                           contrato="998877", banco="Santander")
        _inserir_simulacao(db, "R2", "João", Status.ERROR, agora, cpf="11144477735",
                           contrato="112233", banco="Santander")

        linhas, total = analytics.list_simulations(db, {"consultant": "Ryan"}, TZ)
        assert total == 1 and linhas[0]["request_id"] == "R1"

        linhas, total = analytics.list_simulations(db, {"cpf": "529.982"}, TZ)
        assert total == 1

        linhas, total = analytics.list_simulations(db, {"status": "error"}, TZ)
        assert total == 1 and linhas[0]["consultant_name"] == "João"

        linhas, total = analytics.list_simulations(db, {"q": "998877"}, TZ)
        assert total == 1

    def test_paginacao(self, db):
        agora = utc_now()
        for i in range(25):
            _inserir_simulacao(db, f"P{i:03d}", "Ryan", Status.COMPLETED,
                               to_iso(agora - timedelta(minutes=i)))
        pagina1, total = analytics.list_simulations(db, {}, TZ, limit=10, offset=0)
        pagina3, _ = analytics.list_simulations(db, {}, TZ, limit=10, offset=20)
        assert total == 25 and len(pagina1) == 10 and len(pagina3) == 5
        assert {r["id"] for r in pagina1}.isdisjoint({r["id"] for r in pagina3})

    def test_serializacao_mascara_o_cpf(self, db):
        agora = to_iso(utc_now())
        _inserir_simulacao(db, "R1", "Ryan", Status.COMPLETED, agora, cpf=CPF_VALIDO)
        linha = db.fetchone("SELECT * FROM simulations WHERE request_id='R1'")

        mascarado = analytics.serialize_simulation(linha, mask=True)
        assert mascarado["cpf_display"] == "529.***.***-25"
        assert "cpf" not in mascarado
        assert "raw_message" not in mascarado

        aberto = analytics.serialize_simulation(linha, mask=False)
        assert aberto["cpf"] == CPF_VALIDO


# ================================================================= helpers
def _mensagem_falsa():
    from app.models import IncomingMessage

    return IncomingMessage(
        message_id="false_g@g.us_MSG1_5567988887777@c.us",
        chat_id="g@g.us",
        chat_name="Consultores",
        sender_id="5567988887777@c.us",
        sender_name="Ryan",
        text="Fazer simulação",
    )


def _inserir_simulacao(
    db: Database,
    request_id: str,
    consultor: str,
    status: str,
    criado_em: str,
    segundos: float | None = None,
    cpf: str = CPF_VALIDO,
    contrato: str = "123456",
    banco: str = "Santander",
) -> int:
    return db.insert(
        "simulations",
        {
            "request_id": request_id,
            "consultant_name": consultor,
            "cpf": cpf,
            "bank": banco,
            "contract": contrato,
            "status": status,
            "stage": status,
            "refin": "Sim" if status == Status.COMPLETED else "",
            "reduction_value": 1000.0 if status == Status.COMPLETED else 0.0,
            "processing_seconds": segundos,
            "attempts": 1,
            "max_attempts": 2,
            "created_at": criado_em,
            "updated_at": criado_em,
            "finished_at": criado_em if status in Status.TERMINAL else None,
            "raw_message": "Fazer simulação",
        },
    )


class TestLimpezaDeTexto:
    def test_remove_marcadores_do_whatsapp(self):
        assert clean_text("Encaminhada\nCPF: 123\n\nEditada") == "CPF: 123"

    def test_texto_vazio(self):
        assert clean_text("") == ""
        assert clean_text("   \n  \n ") == ""


class TestMensagensReaisDoGrupo:
    """Casos colhidos do grupo, com o formato exato que os consultores usam."""

    def test_cpf_sem_pontuacao(self):
        """29/08/2026, 19:47 — a mensagem que o bot deixou passar em branco.

        O parser sempre esteve certo; quem falhava era a leitura do HTML.
        Fica aqui para que o formato sem pontuação nunca deixe de ser aceito.
        """
        pedido, erros = parse_request("Ivone Teste\n42888832453\nAmapá")
        assert erros == []
        assert pedido is not None
        assert pedido.cpf == "42888832453"
        assert pedido.customer_name == "Ivone Teste"
        assert pedido.origin == "Amapá"

    @pytest.mark.parametrize("conversa", [
        "sem matricula",
        "CLIENTE EM ATRASO EM PRODUTOS DO BANCO",
        "NAO PASSIVEL A DECISAO MANUAL",
        "nada",
    ])
    def test_conversa_do_grupo_nao_vira_simulacao(self, conversa):
        pedido, erros = parse_request(conversa)
        assert pedido is None and erros == []
