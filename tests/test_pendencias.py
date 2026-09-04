"""Ninguém pode ficar esperando sem que alguém saiba.

O episódio que motivou este arquivo, 01/09 às 15:20: o grupo despejou 59
mensagens em dois minutos, viraram 50 solicitações, o bot respondeu **uma** e
o WhatsApp caiu. Quarenta e nove consultores ficaram esperando — e nada na
tela dizia isso. A fila estava correta, o banco estava correto, e o painel não
mostrava número nenhum.

São dois problemas com limiares separados:

* **fila grande** é enxurrada — normal, só demora;
* **espera longa** é travamento — não é normal.
"""

from __future__ import annotations

import pytest

from app.clock import iso_atras, now_iso
from app.db import Database
from app.events import EventHub
from app.jobs import QueueService
from app.models import Stage, Status


@pytest.fixture()
def fila(tmp_path):
    db = Database(tmp_path / "t.db")
    return QueueService(db=db, hub=EventHub(db), simulators=[],
                        max_attempts=2, job_timeout=30.0,
                        alerta_fila=5, alerta_espera_s=300.0)


def _inserir(fila, request_id: str, status: str, esperando_ha: float = 0.0) -> None:
    quando = iso_atras(esperando_ha) if esperando_ha else now_iso()
    fila.db.insert("simulations", {
        "request_id": request_id, "cpf": "31619614391", "bank": "Santander",
        "consultant_name": "Ryan", "customer_name": "Maria",
        "status": status, "stage": Stage.QUEUED,
        "queued_at": quando, "created_at": quando, "updated_at": quando,
    })


class TestContarQuemEspera:
    def test_fila_vazia(self, fila):
        estado = fila.pendencias()
        assert estado["pendentes"] == 0
        assert estado["espera_maxima_s"] == 0
        assert estado["fila_cheia"] is False and estado["espera_longa"] is False

    def test_conta_os_enfileirados(self, fila):
        for i in range(3):
            _inserir(fila, f"REQ{i:06d}", Status.QUEUED)
        assert fila.pendencias()["pendentes"] == 3

    def test_processing_tambem_conta(self, fila):
        """Presa em processamento espera igual — foi a REQ000037."""
        _inserir(fila, "REQ000001", Status.QUEUED)
        _inserir(fila, "REQ000002", Status.PROCESSING)
        assert fila.pendencias()["pendentes"] == 2

    @pytest.mark.parametrize("status", [Status.COMPLETED, Status.ERROR,
                                        Status.INTERRUPTED])
    def test_o_que_terminou_nao_conta(self, fila, status):
        _inserir(fila, "REQ000001", status)
        assert fila.pendencias()["pendentes"] == 0

    def test_a_espera_e_a_do_mais_antigo(self, fila):
        _inserir(fila, "REQ000001", Status.QUEUED, esperando_ha=600)
        _inserir(fila, "REQ000002", Status.QUEUED, esperando_ha=60)
        estado = fila.pendencias()
        assert 590 <= estado["espera_maxima_s"] <= 610


class TestOsDoisLimiares:
    def test_fila_grande_acende(self, fila):
        for i in range(5):                      # alerta_fila=5
            _inserir(fila, f"REQ{i:06d}", Status.QUEUED)
        estado = fila.pendencias()
        assert estado["fila_cheia"] is True
        assert estado["espera_longa"] is False, "recém-chegadas não são travamento"

    def test_fila_pequena_nao_acende(self, fila):
        for i in range(4):
            _inserir(fila, f"REQ{i:06d}", Status.QUEUED)
        assert fila.pendencias()["fila_cheia"] is False

    def test_espera_longa_acende_mesmo_com_um_so(self, fila):
        """Um pedido parado há uma hora é pior que dez recém-chegados."""
        _inserir(fila, "REQ000001", Status.QUEUED, esperando_ha=3600)
        estado = fila.pendencias()
        assert estado["espera_longa"] is True
        assert estado["fila_cheia"] is False

    def test_o_episodio_real_acende_os_dois(self, fila):
        """50 pedidos, o mais antigo há 20 min: enxurrada E travamento."""
        for i in range(50):
            _inserir(fila, f"REQ{i:06d}", Status.QUEUED, esperando_ha=1200)
        estado = fila.pendencias()
        assert estado["pendentes"] == 50
        assert estado["fila_cheia"] and estado["espera_longa"]


class TestOAvisoNaoViraRuido:
    def test_avisa_uma_vez_por_episodio(self, fila):
        """Cinquenta linhas iguais no log escondem em vez de avisar."""
        avisos: list[str] = []
        fila._on_log = lambda nivel, servico, msg, **k: avisos.append(f"{nivel}:{msg}")
        for i in range(10):
            _inserir(fila, f"REQ{i:06d}", Status.QUEUED, esperando_ha=1200)

        for _ in range(5):
            fila._conferir_a_espera()
        assert len([a for a in avisos if a.startswith("WARNING")]) == 1

    def test_volta_a_avisar_depois_de_normalizar(self, fila):
        avisos: list[str] = []
        fila._on_log = lambda nivel, servico, msg, **k: avisos.append(msg)
        for i in range(10):
            _inserir(fila, f"REQ{i:06d}", Status.QUEUED, esperando_ha=1200)
        fila._conferir_a_espera()
        assert len(avisos) == 1

        fila.db.execute("UPDATE simulations SET status=?", (Status.COMPLETED,))
        fila._conferir_a_espera()               # normalizou: nada a dizer
        assert len(avisos) == 1

        _inserir(fila, "REQ000099", Status.QUEUED, esperando_ha=1200)
        fila._conferir_a_espera()               # aconteceu de novo: avisa
        assert len(avisos) == 2

    def test_o_aviso_diz_quantos_e_ha_quanto_tempo(self, fila):
        avisos: list[str] = []
        fila._on_log = lambda nivel, servico, msg, **k: avisos.append(msg)
        for i in range(12):
            _inserir(fila, f"REQ{i:06d}", Status.QUEUED, esperando_ha=1800)
        fila._conferir_a_espera()
        assert "12" in avisos[0]
        assert "30 min" in avisos[0]

    def test_fila_tranquila_nao_avisa(self, fila):
        avisos: list[str] = []
        fila._on_log = lambda nivel, servico, msg, **k: avisos.append(msg)
        _inserir(fila, "REQ000001", Status.QUEUED)
        fila._conferir_a_espera()
        assert avisos == []


class TestOPainelEnxerga:
    def test_o_snapshot_leva_as_pendencias(self, tmp_path):
        """É o único lugar onde alguém percebe sem ir olhar o grupo."""
        from app.manager import BotManager
        from tests.test_concurrency import _config

        config = _config(tmp_path)
        db = Database(config.db_path)
        manager = BotManager(config, db, EventHub(db))
        for i in range(3):
            _inserir(manager.queue, f"REQ{i:06d}", Status.QUEUED, esperando_ha=900)

        snap = manager.queue_snapshot()
        assert snap["pendentes"] == 3
        assert snap["espera_maxima_s"] >= 890
        assert "fila_cheia" in snap and "espera_longa" in snap


class TestRecuperacaoNoBoot:
    """`recover()` já existia; estes testes travam o que ele garante.

    Sem ele, as 49 solicitações de 01/09 ficariam `queued` para sempre —
    ninguém as processaria, porque a fila em memória morreu com o processo.
    """

    def test_queued_orfao_volta_para_a_fila(self, fila):
        _inserir(fila, "REQ000001", Status.QUEUED)
        assert fila.recover() == 1
        linha = fila.db.fetchone("SELECT status FROM simulations")
        assert linha["status"] == Status.QUEUED
        assert fila.depth() == 1

    def test_processing_orfao_tambem_volta(self, fila):
        """Ele estava rodando quando o processo morreu: ninguém vai terminar."""
        _inserir(fila, "REQ000001", Status.PROCESSING)
        assert fila.recover() == 1
        assert fila.depth() == 1

    def test_quem_esgotou_as_tentativas_e_encerrado(self, fila):
        _inserir(fila, "REQ000001", Status.PROCESSING)
        fila.db.execute("UPDATE simulations SET attempts=?, max_attempts=?", (2, 2))
        fila.recover()
        linha = fila.db.fetchone("SELECT status FROM simulations")
        assert linha["status"] == Status.INTERRUPTED, (
            "ficar em loop de retomada é pior que encerrar e avisar")

    def test_o_que_terminou_nao_e_retomado(self, fila):
        _inserir(fila, "REQ000001", Status.COMPLETED)
        assert fila.recover() == 0

    def test_a_enxurrada_inteira_volta(self, fila):
        """O caso real: 49 presas com o bot caído."""
        for i in range(49):
            _inserir(fila, f"REQ{i:06d}", Status.QUEUED)
        assert fila.recover() == 49
        assert fila.depth() == 49
