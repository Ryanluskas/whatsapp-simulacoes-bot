"""Imagem de resultado enviada no grupo.

Os números usados aqui são os do print real do grupo "Operacional Capital",
onde a consultora concluiu à mão "libera nada" — serve como conferência da
fórmula do Arqueiro (`soma_parcelas / 0,021 - saldo_devedor`).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from app.cards import build_result_html
from app.db import Database
from app.events import EventHub
from app.jobs import QueueService
from app.manager import BotManager
from app.models import IncomingMessage, ParsedRequest, SimulationJob, SimulationResult
from tests.test_concurrency import (
    CPFS, FakeSimulator, FakeWhatsApp, _aguardar, _config, _mensagem,
)

TZ = ZoneInfo("America/Sao_Paulo")

# Cards exatamente como aparecem no print do grupo.
CARDS_DO_PRINT = [
    {"contrato": "7*****56", "parcelas": "120", "valor_parcela": "R$ 450,00",
     "taxa": "1,76", "parcelas_pagas": "9", "saldo_devedor": "R$ 22.079,91"},
    {"contrato": "7*****36", "parcelas": "120", "valor_parcela": "R$ 379,08",
     "taxa": "1,79", "parcelas_pagas": "7", "saldo_devedor": "R$ 18.485,03"},
]


def _resultado(*, contratos=CARDS_DO_PRINT, libera=-1084.94, ok=True,
               cliente="Luiz Fernando Teste", consultor="Camila Testes",
               cpf=CPFS[0], erro=""):
    pedido = ParsedRequest(
        consultant_name=consultor, cpf=cpf, bank="Santander",
        contract="7000045656", customer_name=cliente,
    )
    mensagem = IncomingMessage(
        message_id="false_grupo@g.us_MSG1_5566999998888@c.us",
        chat_id="grupo@g.us", chat_name="Operacional Capital",
        sender_id="5566999998888@c.us", sender_name=consultor,
        text="Fazer simulação",
    )
    job = SimulationJob(request=pedido, message=mensagem,
                        request_id="REQ000182", simulation_id=182)
    return SimulationResult(
        job=job, ok=ok,
        status=("Sim" if libera > 0 else "Não") if ok else "Erro",
        error=erro,
        reduction_value=libera,
        margin="R$ 1.284,00",
        contracts=tuple(contratos),
        installment_sum=829.08, installment_count=240, debt_sum=40564.94,
    )


class TestConteudoDaImagem:
    def test_traz_todos_os_campos_dos_cards(self):
        html = build_result_html(_resultado(), "REQ000182", tz=TZ)
        for esperado in (
            "7*****56", "7*****36", "120", "R$ 450,00", "R$ 379,08",
            "1,76% a.m", "1,79% a.m", "R$ 22.079,91", "R$ 18.485,03",
        ):
            assert esperado in html, f"faltou {esperado!r} na imagem"

    def test_caso_do_print_real_nao_libera(self):
        """A fórmula do Arqueiro dá -1.084,94 — a consultora escreveu 'libera nada'."""
        html = build_result_html(_resultado(libera=-1084.94), "REQ000182", tz=TZ)
        assert "Não libera" in html
        assert "Valor liberado" not in html

    def test_quando_libera_mostra_o_valor(self):
        """Sem diferenciar caixa: o rótulo virou CAIXA ALTA no desenho novo,
        e travar a grafia faria o teste quebrar a cada ajuste tipográfico
        sem que nada do que ele protege tivesse mudado."""
        html = build_result_html(_resultado(libera=32027.25), "REQ000183", tz=TZ)
        assert "valor liberado" in html.casefold()
        assert "R$ 32.027,25" in html
        assert "não libera" not in html.casefold()

    def test_libera_zero_conta_como_nao_libera(self):
        html = build_result_html(_resultado(libera=0.0), "REQ1", tz=TZ)
        assert "não libera" in html.casefold()

    def test_identifica_consultor_cliente_e_solicitacao(self):
        html = build_result_html(_resultado(), "REQ000182", tz=TZ)
        assert "LUIZ FERNANDO TESTE" in html
        assert "Camila Testes" in html
        assert "REQ000182" in html
        assert "Santander" in html

    def test_cpf_conforme_a_configuracao(self):
        aberto = build_result_html(_resultado(), "REQ1", show_client_data=True, tz=TZ)
        assert "529.982.247-25" in aberto

        fechado = build_result_html(_resultado(), "REQ1", show_client_data=False, tz=TZ)
        assert "529.982.247-25" not in fechado
        assert "529.***.***-25" in fechado

    def test_placeholder_lead_nao_vira_nome_de_cliente(self):
        """"Lead" é o marcador de "o consultor não informou" — não é nome.

        O card antigo OMITIA o bloco inteiro nesse caso. O novo mantém o
        bloco e escreve "CLIENTE NÃO INFORMADO": num documento de banco, um
        bloco que aparece e some faz o layout pular entre um comprovante e
        outro, e o consultor lê a ausência como defeito. O que não pode,
        e é o que este teste protege, é "LEAD" virar nome de cliente.
        """
        html = build_result_html(_resultado(cliente="Lead"), "REQ1", tz=TZ)
        assert "LEAD" not in html
        assert "CLIENTE NÃO INFORMADO" in html

    def test_sem_contratos_explica_em_vez_de_ficar_vazio(self):
        html = build_result_html(_resultado(contratos=[]), "REQ1", tz=TZ)
        assert "Nenhum contrato encontrado" in html

    def test_erro_aparece_na_imagem(self):
        html = build_result_html(
            _resultado(ok=False, erro="portal fora do ar"), "REQ1", tz=TZ)
        assert "não foi possível simular" in html.casefold()
        assert "portal fora do ar" in html

    def test_nome_de_terceiro_e_escapado(self):
        """Nome vem da mensagem do WhatsApp: é dado de terceiro."""
        html = build_result_html(
            _resultado(cliente='<script>alert(1)</script>',
                       consultor='Ryan & "Cia" <b>'),
            "REQ1", tz=TZ,
        )
        # Nenhuma tag de terceiro chega ao HTML...
        assert "<script>" not in html
        assert "</script>" not in html
        # ...e o texto continua legível, só que escapado.
        # O nome do cliente passa por .upper() antes do escape.
        assert "&lt;SCRIPT&gt;ALERT(1)&lt;/SCRIPT&gt;" in html
        assert 'Ryan &amp; &quot;Cia&quot; &lt;b&gt;' in html

    def test_campos_faltando_nao_quebram(self):
        html = build_result_html(
            _resultado(contratos=[{"contrato": "7*****56"}]), "REQ1", tz=TZ)
        assert "7*****56" in html
        assert "—" in html  # os campos ausentes viram travessão

    def test_taxa_sem_sufixo_recebe_a_unidade(self):
        html = build_result_html(_resultado(), "REQ1", tz=TZ)
        assert "1,76% a.m" in html
        # e uma taxa que ja' vem com a unidade nao e' duplicada
        html2 = build_result_html(
            _resultado(contratos=[{"contrato": "1", "taxa": "1,80% a.m"}]), "REQ1", tz=TZ)
        assert "1,80% a.m" in html2 and "a.m% a.m" not in html2


def _montar(tmp_path, **kwargs):
    """Manager real, com WhatsApp e simulador substituídos.

    Reconstruído: ele vivia logo abaixo de `TestLegenda`, que testava
    `cards.build_caption` — função morta desde que os textos foram
    centralizados em `mensagens.py`. Ao remover a classe, o helper foi junto
    por descuido.
    """
    config = _config(tmp_path, send_image=True)
    db = Database(config.db_path)
    hub = EventHub(db)
    manager = BotManager(config, db, hub)

    whatsapp = FakeWhatsApp(**kwargs)
    simulator = FakeSimulator()
    manager.whatsapp = whatsapp
    manager.simulators = [simulator]
    manager.queue = QueueService(
        db=db, hub=hub, simulators=[simulator],
        max_attempts=config.max_attempts,
        job_timeout=30.0, on_result=manager._deliver_result, on_log=manager._queue_log,
    )
    manager.queue.start()
    return manager, whatsapp, db


class TestEnvioNoFluxoReal:
    def test_resultado_sai_como_imagem(self, tmp_path):
        manager, whatsapp, db = _montar(tmp_path)
        try:
            manager._handle_message(_mensagem(0))
            assert _aguardar(lambda: len(whatsapp.images) == 1)

            enviada = whatsapp.images[0]
            assert enviada["path"].endswith(".png")
            assert "Ryan" in enviada["caption"]
            # A resposta continua ancorada na mensagem original do consultor
            assert enviada["quote"] == _mensagem(0).message_id

            linha = db.fetchone("SELECT * FROM messages WHERE kind='image'")
            assert linha["media_path"].endswith(".png")
            assert linha["direction"] == "out"
            assert _aguardar(lambda: db.fetchone(
                "SELECT replied_at FROM simulations")["replied_at"] is not None)
        finally:
            manager.queue.stop()

    def test_falha_no_envio_da_imagem_cai_para_texto(self, tmp_path):
        """O consultor nunca pode ficar sem resposta por causa da imagem."""
        manager, whatsapp, db = _montar(tmp_path, image_fails=True)
        try:
            manager._handle_message(_mensagem(0))
            # A confirmacao de recebimento e' a 1a; o resultado em texto e' a 2a
            assert _aguardar(lambda: len(whatsapp.sent) >= 1)
            assert whatsapp.images == []

            resultado = whatsapp.sent[-1]["text"]
            assert "Libera" in resultado or "Não libera" in resultado
            assert "REQ000001" in resultado
            # Nenhuma imagem consta como ENTREGUE -- o painel não pode mostrar
            # um comprovante que o consultor não recebeu. A tentativa que
            # falhou fica gravada como evidência, marcada como falha.
            assert db.scalar("SELECT COUNT(*) FROM messages WHERE kind='image' "
                             "AND status NOT IN ('failed', 'unconfirmed')") == 0
            falha = db.fetchone("SELECT * FROM messages WHERE kind='image'")
            assert falha["status"] == "failed"
            assert "anexo" in falha["error"]
        finally:
            manager.queue.stop()

    def test_falha_ao_renderizar_tambem_cai_para_texto(self, tmp_path):
        manager, whatsapp, db = _montar(tmp_path, render_fails=True)
        try:
            manager._handle_message(_mensagem(0))
            assert _aguardar(lambda: len(whatsapp.sent) >= 1)
            assert whatsapp.images == []
            assert ("Libera" in whatsapp.sent[-1]["text"]
                    or "Não libera" in whatsapp.sent[-1]["text"])
            # E o motivo fica registrado para o operador
            assert _aguardar(lambda: db.scalar(
                "SELECT COUNT(*) FROM logs WHERE message LIKE '%imagem%'") >= 1)
        finally:
            manager.queue.stop()

    def test_imagem_desligada_responde_so_texto(self, tmp_path):
        config = _config(tmp_path, send_image=False)
        db = Database(config.db_path)
        hub = EventHub(db)
        manager = BotManager(config, db, hub)
        whatsapp = FakeWhatsApp()
        simulator = FakeSimulator()
        manager.whatsapp = whatsapp
        manager.simulators = [simulator]
        manager.queue = QueueService(
            db=db, hub=hub, simulators=[simulator], max_attempts=1, job_timeout=30.0,
            on_result=manager._deliver_result, on_log=manager._queue_log,
        )
        manager.queue.start()
        try:
            manager._handle_message(_mensagem(0))
            assert _aguardar(lambda: len(whatsapp.sent) >= 1)
            assert whatsapp.images == []
        finally:
            manager.queue.stop()

    def test_cada_imagem_vai_para_a_sua_solicitacao(self, tmp_path):
        """Cinco pedidos simultâneos: nenhuma imagem pode trocar de dono."""
        manager, whatsapp, db = _montar(tmp_path)
        try:
            import threading
            threads = [
                threading.Thread(target=manager._handle_message, args=(_mensagem(i),))
                for i in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

            assert _aguardar(lambda: len(whatsapp.images) == 5, timeout=30)
            for linha in db.fetchall("SELECT * FROM simulations"):
                enviada = [i for i in whatsapp.images
                           if i["path"].endswith(f"{linha['request_id']}.png")]
                assert len(enviada) == 1, f"{linha['request_id']} sem imagem própria"
                assert linha["consultant_name"] in enviada[0]["caption"]
        finally:
            manager.queue.stop()
