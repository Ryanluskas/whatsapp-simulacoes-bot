"""UMA SOLICITAÇÃO → UMA ORIGEM → UMA RESPOSTA → UMA CITAÇÃO DAQUELA ORIGEM.

O bug que estes testes travam foi visto no grupo real em 20/09/2026: algumas
respostas saíam presas ao pedido certo e outras saíam soltas, sem ninguém
conseguir dizer por quê. O banco daquele dia mostrou o porquê e o tamanho:

* 25 mensagens de saída gravadas, **0** com ``quoted_message_id``;
* 10 solicitações com ``quote_status=ok`` — nenhuma com a prova do "ok";
* sete pares de solicitações com o **mesmo cliente** (REQ000014 e REQ000015
  entre eles), e a conferência do modo ``dom`` comparava TEXTO.

Aqui a pergunta é sempre a mesma: *a resposta desta solicitação citou a
mensagem desta solicitação?* Nada de "a maioria acertou".
"""

from __future__ import annotations

import threading
import time

import pytest

from app.citacao import QUOTE_ID_MISMATCH
from app.db import Database
from app.events import EventHub
from app.manager import BotManager
from app.models import (IncomingMessage, ParsedRequest, QuoteStatus, ResultadoEnvio,
                        SimulationJob, SimulationResult, Stage, Status)
from tests.test_concurrency import CPFS, _config, png_valido

GRUPO = "120363000000000001@g.us"


class WhatsAppQueRegistraOAlvo:
    """Guarda o que foi pedido para citar -- e pode citar OUTRA coisa.

    `quoted_devolvido` simula a camada respondendo com um id diferente do
    pedido: é o caso que o portão de integridade tem de pegar (a Evolution
    devolvendo outro ``stanzaId``, ou o DOM tendo mirado na linha errada).
    """

    def __init__(self, quoted_devolvido: dict | None = None, atraso: float = 0.0):
        self.enviados: list[dict] = []
        self.inbox: list = []
        self._lock = threading.Lock()
        self._quoted = quoted_devolvido or {}
        self._atraso = atraso
        from app.whatsapp import WhatsAppStatus
        self.status = WhatsAppStatus(state="connected", phone="+55 67 90000-0000",
                                     chat_name="Consultores", chat_id=GRUPO)

    def render_png(self, html, path, width=900, timeout=60.0):
        from pathlib import Path
        destino = Path(path)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(png_valido())
        return str(destino)

    def _registrar(self, tipo, chat_id, quote_message_id, texto):
        if self._atraso:
            time.sleep(self._atraso)
        with self._lock:
            self.enviados.append({"tipo": tipo, "chat_id": chat_id,
                                  "quote": quote_message_id, "texto": texto})
        devolvido = self._quoted.get(quote_message_id, quote_message_id)
        return ResultadoEnvio(
            ok=True, via=tipo, provider="dublê", tipo_midia="imagem" if tipo == "imagem" else "nenhum",
            quote_status=QuoteStatus.OK if devolvido == quote_message_id else QuoteStatus.OK,
            quoted_ok=True, quoted_message_id=devolvido,
            enviado_id=f"3EB0RESP{len(self.enviados):03d}",
            evidencia={"key_id": f"3EB0RESP{len(self.enviados):03d}"})

    def send(self, chat_id, chat_name, text, quote_message_id="", timeout=90.0,
             texto_sem_citacao="", quote_text="", quote_participant=""):
        return self._registrar("texto", chat_id, quote_message_id, text)

    def send_image(self, chat_id, chat_name, image_path, caption="",
                   quote_message_id="", timeout=120.0, caption_sem_citacao="",
                   quote_text="", quote_participant=""):
        return self._registrar("imagem", chat_id, quote_message_id, caption)

    def ja_enviado(self, marca, timeout=20.0):
        return False

    def start(self): pass
    def stop(self, timeout=None): pass


def _sistema(tmp_path, whatsapp):
    config = _config(tmp_path, send_image=False, imagem_da_resposta="card")
    db = Database(config.db_path)
    manager = BotManager(config, db, EventHub(db))
    manager.comprovantes_dir = tmp_path / "comprovantes"
    manager.whatsapp = whatsapp
    return manager, db


def _mensagem(indice: int, *, texto: str | None = None, chat_id: str = GRUPO):
    """Uma mensagem de pedido, com id próprio e dados sintéticos."""
    return IncomingMessage(
        message_id=f"2AORIGEM{indice:04d}",
        chat_id=chat_id,
        chat_name="Consultores",
        sender_id="5567988887777@c.us",
        sender_name="Consultor",
        text=texto or f"Cliente Teste {indice}\n{CPFS[indice % len(CPFS)]}\nAmapá",
        participant="5567988887777@c.us",
    )


def _resultado(indice: int, mensagem=None, request_id=None):
    msg = mensagem or _mensagem(indice)
    pedido = ParsedRequest(consultant_name="Consultor", cpf=CPFS[indice % len(CPFS)],
                           bank="Santander", contract="", customer_name=f"Cliente Teste {indice}")
    job = SimulationJob(request=pedido, message=msg,
                        request_id=request_id or f"REQ{indice:06d}",
                        simulation_id=indice)
    return SimulationResult(job=job, ok=True, status="Sim", reduction_value=1000.0 + indice)


def _linha(db, indice: int, mensagem=None, request_id=None):
    """A solicitação GRAVADA -- é dela que a origem tem de sair."""
    msg = mensagem or _mensagem(indice)
    agora = "2026-09-20T12:00:00Z"
    return db.insert("simulations", {
        "id": indice, "request_id": request_id or f"REQ{indice:06d}",
        "consultant_name": "Consultor", "chat_id": msg.chat_id,
        "source_message_id": msg.message_id, "participant": msg.participant,
        "sender_id": msg.sender_id, "sender_name": msg.sender_name,
        "chat_name": msg.chat_name, "raw_message": msg.text,
        "cpf": CPFS[indice % len(CPFS)], "bank": "Santander",
        "customer_name": f"Cliente Teste {indice}", "status": Status.PROCESSING,
        "stage": Stage.REPLYING, "result_ok": 1, "delivery_status": "pending",
        "created_at": agora, "updated_at": agora,
    })


# =============================================================== o caso base
class TestCadaRespostaCitaASuaOrigem:
    def test_uma_mensagem_uma_resposta_uma_citacao(self, tmp_path):
        wa = WhatsAppQueRegistraOAlvo()
        manager, db = _sistema(tmp_path, wa)
        _linha(db, 1)
        manager._deliver_result(_resultado(1))

        assert len(wa.enviados) == 1
        assert wa.enviados[0]["quote"] == "2AORIGEM0001"
        saida = db.fetchone("SELECT quoted_message_id, quote_status FROM messages "
                            "WHERE direction='out'")
        assert saida["quoted_message_id"] == "2AORIGEM0001", (
            "a saída não registrou QUAL mensagem foi citada -- era o buraco de 20/09")
        assert saida["quote_status"] == QuoteStatus.OK

    def test_cinco_pedidos_cada_um_com_a_sua(self, tmp_path):
        wa = WhatsAppQueRegistraOAlvo()
        manager, db = _sistema(tmp_path, wa)
        for i in range(1, 6):
            _linha(db, i)
        for i in range(1, 6):
            manager._deliver_result(_resultado(i))

        alvos = [e["quote"] for e in wa.enviados]
        assert alvos == [f"2AORIGEM{i:04d}" for i in range(1, 6)]
        for i in range(1, 6):
            linha = db.fetchone(
                "SELECT quoted_message_id FROM messages WHERE request_id=? AND direction='out'",
                (f"REQ{i:06d}",))
            assert linha["quoted_message_id"] == f"2AORIGEM{i:04d}"

    def test_conclusao_fora_de_ordem_nao_troca_o_alvo(self, tmp_path):
        """REQ3 termina primeiro, depois REQ1. Cada uma cita a sua."""
        wa = WhatsAppQueRegistraOAlvo()
        manager, db = _sistema(tmp_path, wa)
        for i in (1, 2, 3, 4):
            _linha(db, i)
        for i in (3, 1, 4, 2):
            manager._deliver_result(_resultado(i))

        por_request = {e["texto"]: e["quote"] for e in wa.enviados}
        assert len(wa.enviados) == 4
        for i in (1, 2, 3, 4):
            saida = db.fetchone(
                "SELECT quoted_message_id FROM messages WHERE request_id=? AND direction='out'",
                (f"REQ{i:06d}",))
            assert saida["quoted_message_id"] == f"2AORIGEM{i:04d}", (
                f"REQ{i:06d} citou {saida['quoted_message_id']}; ordem de conclusão "
                f"trocou o alvo. Envios: {por_request}")

    def test_mesmo_texto_e_mesmo_cliente_nao_se_confundem(self, tmp_path):
        """Duas mensagens idênticas -- o caso REQ000014/REQ000015 do grupo real."""
        wa = WhatsAppQueRegistraOAlvo()
        manager, db = _sistema(tmp_path, wa)
        igual = "Cliente Teste\n52998224725\nAmapá"
        m1 = _mensagem(1, texto=igual)
        m2 = _mensagem(2, texto=igual)
        _linha(db, 1, m1)
        _linha(db, 2, m2)

        manager._deliver_result(_resultado(2, m2))
        manager._deliver_result(_resultado(1, m1))

        assert [e["quote"] for e in wa.enviados] == ["2AORIGEM0002", "2AORIGEM0001"], (
            "texto igual fez a resposta escolher a mensagem errada")

    def test_dois_consultores_no_mesmo_grupo(self, tmp_path):
        wa = WhatsAppQueRegistraOAlvo()
        manager, db = _sistema(tmp_path, wa)
        outro = IncomingMessage(
            message_id="2AORIGEM0009", chat_id=GRUPO, chat_name="Consultores",
            sender_id="5567911112222@c.us", sender_name="Outro Consultor",
            text="Cliente Teste 9\n11144477735\nAmapá",
            participant="5567911112222@c.us")
        _linha(db, 1)
        _linha(db, 9, outro)
        manager._deliver_result(_resultado(1))
        manager._deliver_result(_resultado(9, outro))

        assert [e["quote"] for e in wa.enviados] == ["2AORIGEM0001", "2AORIGEM0009"]


# ================================================== o que não pode sair calado
class TestCitacaoErradaNaoPassaCaladaEmLugarNenhum:
    def test_camada_que_cita_outra_mensagem_vira_not_applied(self, tmp_path):
        """A mensagem já saiu -- não dá para desfazer. Mas não se chama de ok."""
        wa = WhatsAppQueRegistraOAlvo(quoted_devolvido={"2AORIGEM0001": "2AOUTRA9999"})
        manager, db = _sistema(tmp_path, wa)
        _linha(db, 1)
        manager._deliver_result(_resultado(1))

        saida = db.fetchone("SELECT quoted_message_id, quote_status, quote_error "
                            "FROM messages WHERE direction='out'")
        assert saida["quote_status"] == QuoteStatus.NOT_APPLIED, (
            "citou outra mensagem e o sistema registrou como citação correta")
        assert QUOTE_ID_MISMATCH in (saida["quote_error"] or "")

        logs = db.fetchall("SELECT message FROM logs WHERE message LIKE ?",
                           (f"%{QUOTE_ID_MISMATCH}%",))
        assert logs, "nenhum log explicaria a citação errada"
        assert any("2AOUTRA9999" in l["message"] and "2AORIGEM0001" in l["message"]
                   for l in logs), "o log não diz qual id saiu e qual era o certo"

    def test_mensagem_em_memoria_que_discorda_do_banco_nao_envia(self, tmp_path):
        """A RAM não pode contradizer a linha gravada.

        Um job antigo, um reenvio com a mensagem errada, uma variável que
        sobreviveu a um ciclo: qualquer um deles faria a resposta sair
        pendurada em outra mensagem. O banco decide; divergiu, não envia.
        """
        wa = WhatsAppQueRegistraOAlvo()
        manager, db = _sistema(tmp_path, wa)
        _linha(db, 1, _mensagem(1))
        # A mensagem em memória aponta OUTRA origem, em OUTRO grupo.
        intrusa = IncomingMessage(
            message_id="2AORIGEM9999", chat_id="120363000000000999@g.us",
            chat_name="Outro grupo", sender_id="5567988887777@c.us",
            sender_name="Consultor", text="Cliente Teste 52998224725")

        enviou = manager._send_reply(intrusa, "resposta", status=Status.COMPLETED,
                                     simulation_id=1, request_id="REQ000001")
        assert enviou is False
        assert wa.enviados == [], "a resposta saiu contra a origem gravada"
        logs = db.fetchall("SELECT message FROM logs WHERE message LIKE '%QUOTE_%MISMATCH%'")
        assert logs, "bloqueou calado: ninguém saberia por que a resposta não saiu"

    def test_resposta_vai_para_a_conversa_que_o_banco_registrou(self, tmp_path):
        wa = WhatsAppQueRegistraOAlvo()
        manager, db = _sistema(tmp_path, wa)
        msg = _mensagem(1)
        _linha(db, 1, msg)
        manager._deliver_result(_resultado(1, msg))
        assert wa.enviados[0]["chat_id"] == GRUPO


# ======================================================= a origem sobrevive
class TestAOrigemVemSempreDoBanco:
    def test_depois_do_reinicio_a_origem_ainda_e_a_mesma(self, tmp_path):
        """Manager novo, memória zerada: a citação sai do que está gravado."""
        wa = WhatsAppQueRegistraOAlvo()
        manager, db = _sistema(tmp_path, wa)
        msg = _mensagem(7)
        _linha(db, 7, msg)

        # Outro processo: nada em RAM vem junto.
        outro_wa = WhatsAppQueRegistraOAlvo()
        config = _config(tmp_path, send_image=False, imagem_da_resposta="card")
        db2 = Database(config.db_path)
        manager2 = BotManager(config, db2, EventHub(db2))
        manager2.comprovantes_dir = tmp_path / "comprovantes"
        manager2.whatsapp = outro_wa

        linha = dict(db2.fetchone("SELECT * FROM simulations WHERE id=7"))
        origem = manager2._origem_da_linha(linha)
        assert origem.message_id == msg.message_id
        assert origem.chat_id == msg.chat_id

        manager2._send_reply(origem, "resposta depois do reinício",
                             status=Status.COMPLETED, simulation_id=7,
                             request_id="REQ000007")
        assert outro_wa.enviados[0]["quote"] == msg.message_id

    def test_solicitacao_sem_origem_gravada_responde_sem_citar(self, tmp_path):
        """Sem id não há o que citar -- e não se inventa um."""
        wa = WhatsAppQueRegistraOAlvo()
        manager, db = _sistema(tmp_path, wa)
        msg = _mensagem(3)
        _linha(db, 3, msg)
        db.update("simulations", {"source_message_id": ""}, {"id": 3})
        linha = dict(db.fetchone("SELECT * FROM simulations WHERE id=3"))

        manager._send_reply(manager._origem_da_linha(linha), "resposta",
                            status=Status.COMPLETED, simulation_id=3,
                            request_id="REQ000003")
        assert len(wa.enviados) == 1
        assert wa.enviados[0]["quote"] == "", "citou um id que não existe"
