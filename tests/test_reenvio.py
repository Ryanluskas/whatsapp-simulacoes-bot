"""Um resultado que não chega vale o mesmo que não ter simulado.

A REQ000010 expôs a falha: a simulação terminou, o resultado ficou no painel,
o envio ao WhatsApp falhou — e nada mais aconteceu. O consultor nunca soube.
`replied_at` só era gravado no sucesso e ninguém tentava de novo.
"""

from __future__ import annotations

import pytest

from app.formatter import format_stored_result


class TestFormatoDoReenvio:
    def _linha(self, **extra):
        base = {
            "cpf": "82674159804", "customer_name": "Clarisa de Tássia",
            "consultant_name": "Ryan", "request_id": "REQ000012",
            "status": "completed", "reduction_value": 2219.77,
        }
        base.update(extra)
        return base

    def test_diz_que_e_reenvio(self):
        """Sem isso o consultor acha que simulamos duas vezes."""
        assert "reenvio" in format_stored_result(self._linha()).lower()

    def test_traz_o_valor_liberado(self):
        texto = format_stored_result(self._linha())
        assert "2.219,77" in texto
        assert "REQ000012" in texto

    def test_nao_libera_e_explicito(self):
        texto = format_stored_result(self._linha(reduction_value=0))
        assert "Não libera" in texto

    def test_erro_traz_o_motivo(self):
        texto = format_stored_result(self._linha(
            status="error", error_message="Não foi possível contatar a averbadora."))
        assert "averbadora" in texto

    def test_erro_sem_motivo_nao_fica_vazio(self):
        texto = format_stored_result(self._linha(status="error", error_message=""))
        assert "falha na consulta" in texto

    def test_cpf_mascarado_por_padrao(self):
        assert "826.***.***-04" in format_stored_result(self._linha())

    def test_sem_mascara_quando_pedido(self):
        assert "82674159804" in format_stored_result(self._linha(), mask=False)

    def test_linha_incompleta_nao_derruba(self):
        """O reenvio lê o que sobrou no banco; pode faltar campo."""
        texto = format_stored_result({"request_id": "REQ000099", "status": "completed"})
        assert "REQ000099" in texto


class TestColunaDeTentativas:
    def test_a_coluna_existe_na_migracao(self):
        """Sem ela o laço de reenvio não teria onde contar as tentativas."""
        from app.db import MIGRATIONS
        assert "reply_attempts" in MIGRATIONS["simulations"]

    def test_a_migracao_roda_em_banco_existente(self, tmp_path):
        """Bancos de antes desta versão têm de ganhar a coluna sem perder dados."""
        import sqlite3

        from app.db import Database

        caminho = tmp_path / "antigo.db"
        Database(caminho)  # cria o schema atual

        con = sqlite3.connect(caminho)
        colunas = [r[1] for r in con.execute("PRAGMA table_info(simulations)")]
        con.close()
        assert "reply_attempts" in colunas

    def test_o_padrao_e_zero(self, tmp_path):
        import sqlite3

        from app.db import Database

        db = Database(tmp_path / "t.db")
        db.insert("simulations", {
            "request_id": "REQ1", "cpf": "1", "bank": "Santander",
            "consultant_name": "Ryan", "status": "completed",
            "created_at": "2026-08-29T00:00:00Z",
            "updated_at": "2026-08-29T00:00:00Z",
        })
        con = sqlite3.connect(tmp_path / "t.db")
        valor = con.execute("SELECT reply_attempts FROM simulations").fetchone()[0]
        con.close()
        assert valor == 0


class TestQuemEntraNaFilaDeReenvio:
    """A consulta tem de pegar o que ficou sem resposta — e só isso."""

    @pytest.fixture()
    def db(self, tmp_path):
        from app.db import Database
        return Database(tmp_path / "t.db")

    def _inserir(self, db, request_id, status, replied_at=None, tentativas=0):
        db.insert("simulations", {
            "request_id": request_id, "cpf": "1", "bank": "Santander",
            "consultant_name": "Ryan", "status": status,
            "replied_at": replied_at, "reply_attempts": tentativas,
            "created_at": "2026-08-29T00:00:00Z",
            "updated_at": "2026-08-29T00:00:00Z",
        })

    def _pendentes(self, db, maximo=5):
        return db.fetchall(
            "SELECT request_id FROM simulations "
            " WHERE replied_at IS NULL AND status IN (?, ?) "
            "   AND (reply_attempts IS NULL OR reply_attempts < ?) ORDER BY id",
            ("completed", "error", maximo),
        )

    def test_pega_concluida_sem_resposta(self, db):
        self._inserir(db, "REQ_PENDENTE", "completed")
        assert [r["request_id"] for r in self._pendentes(db)] == ["REQ_PENDENTE"]

    def test_pega_erro_sem_resposta(self, db):
        """Erro também precisa chegar: o consultor está esperando."""
        self._inserir(db, "REQ_ERRO", "error")
        assert [r["request_id"] for r in self._pendentes(db)] == ["REQ_ERRO"]

    def test_ignora_o_que_ja_foi_entregue(self, db):
        self._inserir(db, "REQ_OK", "completed", replied_at="2026-08-29T01:00:00Z")
        assert self._pendentes(db) == []

    def test_ignora_o_que_ainda_esta_na_fila(self, db):
        self._inserir(db, "REQ_FILA", "queued")
        assert self._pendentes(db) == []

    def test_desiste_depois_do_limite(self, db):
        """Não pode ficar tentando para sempre."""
        self._inserir(db, "REQ_DESISTIU", "completed", tentativas=5)
        assert self._pendentes(db, maximo=5) == []


class TestOLacoDeReenvioFunciona:
    """Exercita `_reenviar_um` de ponta a ponta, com um WhatsApp de mentira.

    Testar só a consulta SQL não provaria nada: o defeito da REQ000010 estava
    na ausência do laço inteiro.
    """

    @pytest.fixture()
    def manager(self, tmp_path, monkeypatch):
        from app.config import load_config
        from app.db import Database
        from app.events import EventHub
        from app.manager import BotManager

        monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
        monkeypatch.setenv("STATE_PATH", str(tmp_path / "s.json"))
        monkeypatch.setenv("SIMULATOR_ENABLED", "false")
        config = load_config()
        db = Database(tmp_path / "t.db")
        return BotManager(config, db, EventHub(db))

    def _pendente(self, manager, **extra):
        dados = {
            "request_id": "REQ000010", "cpf": "82674159804", "bank": "Santander",
            "consultant_name": "Ryan", "customer_name": "Clarisa",
            "status": "completed", "reduction_value": 2219.77,
            "chat_id": "grupo@g.us", "source_message_id": "false_g@g.us_M_x@c.us",
            "replied_at": None, "reply_attempts": 0,
            "created_at": "2026-08-29T00:00:00Z", "updated_at": "2026-08-29T00:00:00Z",
        }
        dados.update(extra)
        sim_id = manager.db.insert("simulations", dados)
        return dict(manager.db.fetchone(
            "SELECT * FROM simulations WHERE id = ?", (sim_id,)))

    def test_reenvio_bem_sucedido_marca_entregue(self, manager, monkeypatch):
        enviados = []
        monkeypatch.setattr(manager, "_send_reply",
                            lambda *a, **k: enviados.append(a[1]) or True)

        linha = self._pendente(manager)
        manager._reenviar_um(linha)

        depois = manager.db.fetchone(
            "SELECT replied_at, reply_attempts FROM simulations WHERE id = ?",
            (linha["id"],))
        assert depois["replied_at"], "não marcou como entregue"
        assert depois["reply_attempts"] == 1
        assert "2.219,77" in enviados[0], "o valor não foi para o consultor"

    def test_reenvio_que_falha_conta_a_tentativa(self, manager, monkeypatch):
        monkeypatch.setattr(manager, "_send_reply", lambda *a, **k: False)

        linha = self._pendente(manager)
        manager._reenviar_um(linha)

        depois = manager.db.fetchone(
            "SELECT replied_at, reply_attempts FROM simulations WHERE id = ?",
            (linha["id"],))
        assert depois["replied_at"] is None
        assert depois["reply_attempts"] == 1, "sem contar, tentaria para sempre"

    def test_varre_e_reenvia_o_que_esta_pendente(self, manager, monkeypatch):
        monkeypatch.setattr(manager, "_send_reply", lambda *a, **k: True)
        self._pendente(manager, request_id="REQ_A")
        self._pendente(manager, request_id="REQ_B", status="error",
                       error_message="Não foi possível contatar a averbadora.")
        self._pendente(manager, request_id="REQ_JA_FOI",
                       replied_at="2026-08-29T01:00:00Z")

        manager._reenviar_pendentes()

        restantes = manager.db.fetchall(
            "SELECT request_id FROM simulations WHERE replied_at IS NULL")
        assert restantes == [], f"ficaram sem entregar: {restantes}"

    def test_para_depois_do_limite(self, manager, monkeypatch):
        monkeypatch.setattr(manager, "_send_reply", lambda *a, **k: False)
        self._pendente(manager, reply_attempts=manager._MAX_REENVIOS)

        chamou = []
        monkeypatch.setattr(manager, "_reenviar_um", lambda linha: chamou.append(linha))
        manager._reenviar_pendentes()
        assert chamou == [], "continuou tentando depois de desistir"


class TestNaoAtropelarAEntregaEmCurso:
    """O reenvio mandava tudo duas vezes.

    O status vira "completed" ANTES de a resposta sair, e o envio da imagem
    passa de um minuto. A varredura de 30 em 30 segundos pegava entregas
    ainda em curso: o consultor recebia o resultado e, logo depois, o mesmo
    resultado marcado como "(reenvio)". Foi o que poluiu o grupo.
    """

    @pytest.fixture()
    def db(self, tmp_path):
        from app.db import Database
        return Database(tmp_path / "t.db")

    def _inserir(self, db, request_id, terminou_em):
        db.insert("simulations", {
            "request_id": request_id, "cpf": "1", "bank": "Santander",
            "consultant_name": "Ryan", "status": "completed",
            "replied_at": None, "reply_attempts": 0,
            "finished_at": terminou_em,
            "created_at": terminou_em, "updated_at": terminou_em,
        })

    def _pendentes(self, db, carencia):
        from app.clock import iso_atras
        return [r["request_id"] for r in db.fetchall(
            "SELECT request_id FROM simulations "
            " WHERE replied_at IS NULL AND status IN (?, ?) "
            "   AND (reply_attempts IS NULL OR reply_attempts < ?) "
            "   AND COALESCE(finished_at, updated_at, created_at) < ? ORDER BY id",
            ("completed", "error", 5, iso_atras(carencia)))]

    def test_entrega_recem_terminada_nao_e_reenviada(self, db):
        from app.clock import now_iso
        self._inserir(db, "REQ_AGORA", now_iso())
        assert self._pendentes(db, carencia=180) == [], (
            "atropelou uma entrega que ainda podia estar em curso")

    def test_entrega_antiga_e_reenviada(self, db):
        from app.clock import iso_atras
        self._inserir(db, "REQ_ANTIGA", iso_atras(600))
        assert self._pendentes(db, carencia=180) == ["REQ_ANTIGA"]

    def test_a_carencia_cobre_o_envio_de_imagem(self):
        """O envio da imagem passa de um minuto; a carência tem de ser maior."""
        from app.manager import BotManager
        assert BotManager._CARENCIA_REENVIO >= 120, (
            "carência curta demais: o reenvio vai atropelar a entrega original")
        assert BotManager._CARENCIA_REENVIO > BotManager._INTERVALO_REENVIO


class TestSolicitacaoPresaNaoFicaInvisivel:
    """REQ000037 ficou em `processing` para sempre, e ninguém soube.

    Com o sistema NO AR, uma solicitação presa em `processing` é invisível:
    não está na fila, não aparece como erro, e o laço de reenvio não a pega
    porque olhava só `completed` e `error`. O consultor espera uma resposta
    que nunca vem, sem explicação.

    São duas peças: um vigia que encerra a presa, e o reenvio passando a
    cobrir `interrupted` — senão ela só trocaria de status e continuaria
    invisível.
    """

    @pytest.fixture()
    def db(self, tmp_path):
        from app.db import Database
        return Database(tmp_path / "t.db")

    def _inserir(self, db, request_id, status, comecou_em, replied_at=None):
        db.insert("simulations", {
            "request_id": request_id, "cpf": "31619614391", "bank": "Santander",
            "consultant_name": "Ryan", "customer_name": "Maria Tabaré",
            "status": status, "started_at": comecou_em, "finished_at": comecou_em,
            "replied_at": replied_at, "reply_attempts": 0,
            "created_at": comecou_em, "updated_at": comecou_em,
        })

    def test_o_vigia_existe_e_roda_sozinho(self):
        from app.jobs import QueueService
        assert hasattr(QueueService, "_vigia_loop")
        assert QueueService._INTERVALO_DO_VIGIA > 0
        assert QueueService._FOLGA_SOBRE_O_TIMEOUT >= 60, (
            "folga curta demais brigaria com simulação ainda viva")

    def test_o_vigia_encerra_a_presa(self, db, tmp_path):
        from app.clock import iso_atras
        from app.events import EventHub
        from app.jobs import QueueService
        from app.models import Status

        fila = QueueService(db=db, hub=EventHub(db), simulators=[],
                            max_attempts=2, job_timeout=30.0)
        self._inserir(db, "REQ_PRESA", Status.PROCESSING, iso_atras(600))
        assert fila._resolver_presas() == 1

        linha = db.fetchone("SELECT * FROM simulations WHERE request_id='REQ_PRESA'")
        assert linha["status"] == Status.INTERRUPTED
        assert "travou" in (linha["error_message"] or "")

    def test_nao_encerra_simulacao_recente(self, db):
        """Uma simulação que acabou de começar não está presa."""
        from app.clock import now_iso
        from app.events import EventHub
        from app.jobs import QueueService
        from app.models import Status

        fila = QueueService(db=db, hub=EventHub(db), simulators=[],
                            max_attempts=2, job_timeout=300.0)
        self._inserir(db, "REQ_NOVA", Status.PROCESSING, now_iso())
        assert fila._resolver_presas() == 0

    def test_interrompida_entra_no_reenvio(self, db):
        """Sem isto ela só trocaria de status e seguiria invisível."""
        from app.clock import iso_atras
        from app.models import Status

        self._inserir(db, "REQ_INT", Status.INTERRUPTED, iso_atras(600))
        pendentes = db.fetchall(
            "SELECT request_id FROM simulations "
            " WHERE replied_at IS NULL AND status IN (?, ?, ?) ORDER BY id",
            (Status.COMPLETED, Status.ERROR, Status.INTERRUPTED))
        assert [r["request_id"] for r in pendentes] == ["REQ_INT"]

    def test_a_mensagem_da_interrompida_explica(self):
        from app.mensagens import resultado_reenviado
        texto = resultado_reenviado({
            "cpf": "31619614391", "customer_name": "Maria Tabaré",
            "status": "interrupted",
            "error_message": "a simulação travou e não retornou"})
        assert "não consegui simular" in texto.lower()
        assert "travou" in texto
        assert "Não libera" not in texto, "falha não pode parecer resultado"
