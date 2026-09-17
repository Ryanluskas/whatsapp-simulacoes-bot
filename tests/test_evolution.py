"""A camada Evolution, testada contra um servidor falso.

Nenhum teste daqui toca a Evolution de verdade: tudo passa por
``httpx.MockTransport``. Isso e' o que permite provar o payload e a
classificacao de erro sem docker, sem instancia, sem celular -- e e' o que
faltava na camada antiga, onde so' dava para saber se o envio funcionou
mandando mensagem no grupo do cliente.

Os tres primeiros grupos de teste sao os tres defeitos historicos, agora
verificados em vez de esperados.
"""

from __future__ import annotations

import base64
import inspect
import json
from pathlib import Path

import httpx
import pytest

from app import mensagens
from app.evolution import (DELAY_HUMANO_MS, EvolutionClient, LICENCA_PENDENTE,
                           classificar_resposta)
from app.models import Desfecho, QuoteStatus
from app.whatsapp_port import METODOS_DO_CONTRATO
from tests.test_concurrency import png_valido

GRUPO = "120363111222333@g.us"
ID_ORIGINAL = "3EB0C5A277F7F9B6C599"


def _resposta_ok(key_id: str = "3EB0DEADBEEF") -> httpx.Response:
    return httpx.Response(200, json={"key": {"id": key_id, "remoteJid": GRUPO},
                                     "status": "PENDING"})


class Espiao:
    """Guarda o que foi enviado, para os testes olharem o payload."""

    def __init__(self, resposta=None) -> None:
        self.resposta = resposta or _resposta_ok()
        self.chamadas: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        corpo = json.loads(request.content) if request.content else {}
        self.chamadas.append((request.url.path, corpo))
        if callable(self.resposta):
            return self.resposta(request)
        return self.resposta

    @property
    def ultimo(self) -> dict:
        return self.chamadas[-1][1]


@pytest.fixture()
def espiao():
    return Espiao()


@pytest.fixture()
def cliente(espiao, tmp_path):
    """Cliente com transporte falso e renderizador desligado.

    O renderizador nao entra: ele sobe um Chromium de verdade, e nenhum teste
    daqui precisa de imagem renderizada -- so' de imagem ENVIADA.
    """
    http = httpx.Client(transport=httpx.MockTransport(espiao),
                        base_url="http://evolution:8080",
                        headers={"apikey": "segredo"})

    class RenderizadorFalso:
        def start(self): pass
        def stop(self, timeout=10.0): pass
        def render_png(self, html, path, width=900, timeout=60.0): return str(path)

    return EvolutionClient(
        base_url="http://evolution:8080", api_key="segredo", instance="allana",
        group_jid=GRUPO, group_name="Simulações",
        client=http, renderer=RenderizadorFalso(),
    )


@pytest.fixture()
def png(tmp_path) -> Path:
    """Um PNG minusculo, mas real: 1x1 pixel."""
    dados = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    alvo = tmp_path / "REQ000182.png"
    alvo.write_bytes(dados)
    return alvo


# ---------------------------------------------------------------- 1, 2: imagem
class TestOPayloadDaImagem:
    """Defeito histórico: a imagem chegava como documento.

    No DOM isso dependia de acertar o ``input[type=file]`` ou o item certo do
    menu do clipe -- duas coisas que a Meta muda quando quer. Aqui e' um campo
    literal, e estes testes existem para que ele nunca deixe de ser "image".
    """

    def test_monta_o_payload_certo(self, cliente, espiao, png):
        r = cliente.send_image(GRUPO, "Simulações", png,
                               caption="✅ Libera R$ 4.913,52\nIvone Teste",
                               quote_message_id=ID_ORIGINAL)
        assert r.ok
        rota, payload = espiao.chamadas[-1]
        assert rota == "/message/sendMedia/allana"
        assert payload["mediatype"] == "image"
        assert payload["mimetype"] == "image/png"
        assert payload["number"] == GRUPO, "grupo vai por JID, não por telefone"
        assert payload["fileName"] == "REQ000182.png"
        assert payload["quoted"]["key"]["id"] == ID_ORIGINAL

    def test_base64_sem_o_prefixo_data(self, cliente, espiao, png):
        """O erro mais comum de quem integra: mandar com ``data:image/png;base64,``."""
        cliente.send_image(GRUPO, "Simulações", png, caption="x")
        media = espiao.ultimo["media"]
        assert not media.startswith("data:"), "base64 tem de ser PURO"
        # e tem de ser o arquivo de verdade, decodificável de volta
        assert base64.b64decode(media) == png.read_bytes()

    def test_nunca_manda_como_documento(self, cliente, espiao, png):
        """Em NENHUM caminho. Nem como plano B."""
        cliente.send_image(GRUPO, "Simulações", png, caption="x")
        cliente.send(GRUPO, "Simulações", "texto")
        for _, payload in espiao.chamadas:
            assert payload.get("mediatype") != "document"

        # E nem escrito no código. Comentários saem da varredura: o próprio
        # arquivo explica por que "document" é proibido, e essa explicação não
        # pode fazer o teste falhar.
        codigo = [linha.split("#")[0] for linha in
                  Path("app/evolution.py").read_text(encoding="utf-8").splitlines()]
        assert not any('"document"' in linha for linha in codigo)

    def test_png_vazio_nao_vira_envio(self, cliente, espiao, tmp_path):
        """Um PNG de zero byte chegaria como arquivo quebrado no grupo."""
        vazio = tmp_path / "vazio.png"
        vazio.write_bytes(b"")
        r = cliente.send_image(GRUPO, "Simulações", vazio, caption="x")
        assert not r.ok
        assert not espiao.chamadas, "não pode nem tentar enviar"


# ------------------------------------------------------------------ 1: citação
class TestCitacao:
    """Defeito histórico: não citava a mensagem do consultor."""

    def test_cita_usando_o_id_que_chegou_no_webhook(self, cliente, espiao):
        """O chamador entrega id, texto e autor; a camada não lembra nada sozinha.

        Antes o texto citado vinha de um dicionário em RAM preenchido pelo
        webhook -- depois de reiniciar, a citação saía vazia.
        """
        r = cliente.send(GRUPO, "Simulações", "resposta", quote_message_id=ID_ORIGINAL,
                         quote_text="Ivone Teste\n42888832453",
                         quote_participant="5562999990000@s.whatsapp.net")
        # A resposta falsa padrão não traz stanzaId: a citação foi PEDIDA, mas
        # não há prova de que pegou. `quoted_ok` só afirma com prova.
        assert r.ok and r.quote_status == QuoteStatus.UNVERIFIED
        assert r.quoted_ok is False
        citada = espiao.ultimo["quoted"]
        assert citada["key"]["id"] == ID_ORIGINAL
        assert citada["key"]["remoteJid"] == GRUPO
        assert citada["key"]["participant"] == "5562999990000@s.whatsapp.net"
        assert citada["message"]["conversation"] == "Ivone Teste\n42888832453"

    def test_sem_id_nao_manda_campo_quoted(self, cliente, espiao):
        r = cliente.send(GRUPO, "Simulações", "resposta")
        assert r.ok and not r.quoted_ok
        assert "quoted" not in espiao.ultimo

    def test_id_desconhecido_ainda_cita(self, cliente, espiao):
        """Se o texto original se perdeu, citar pelo id ainda é melhor que nada."""
        r = cliente.send(GRUPO, "Simulações", "resposta", quote_message_id="ID_QUE_NAO_GUARDEI")
        assert espiao.ultimo["quoted"]["key"]["id"] == "ID_QUE_NAO_GUARDEI"
        assert r.quote_status == QuoteStatus.UNVERIFIED and r.quoted_ok is False

    def test_quoted_ok_so_com_stanza_igual_ao_pedido(self, cliente, espiao):
        """O único caminho para `quoted_ok=True`: a API devolve o stanzaId pedido."""
        espiao.resposta = httpx.Response(201, json={
            "key": {"id": "3EB0RESPOSTA", "remoteJid": GRUPO, "fromMe": True},
            "message": {"extendedTextMessage": {
                "text": "resposta", "contextInfo": {"stanzaId": ID_ORIGINAL}}}})
        r = cliente.send(GRUPO, "Simulações", "resposta", quote_message_id=ID_ORIGINAL)
        assert r.quote_status == QuoteStatus.OK and r.quoted_ok is True

    def test_stanza_de_outra_mensagem_nao_e_quoted_ok(self, cliente, espiao):
        espiao.resposta = httpx.Response(201, json={
            "key": {"id": "3EB0RESPOSTA", "remoteJid": GRUPO, "fromMe": True},
            "message": {"extendedTextMessage": {
                "text": "resposta", "contextInfo": {"stanzaId": "3EB0OUTRA"}}}})
        r = cliente.send(GRUPO, "Simulações", "resposta", quote_message_id=ID_ORIGINAL)
        assert r.ok and r.quote_status == QuoteStatus.NOT_APPLIED and r.quoted_ok is False

    def test_unverified_nao_e_registrada_como_citacao_recusada(self, tmp_path):
        """Sem stanzaId a citação é INCERTA, não recusada: o log não pode mentir."""
        from app.db import Database
        from app.events import EventHub
        from app.manager import BotManager
        from app.models import ResultadoEnvio
        from tests.test_concurrency import _config

        config = _config(tmp_path)
        db = Database(config.db_path)
        manager = BotManager(config, db, EventHub(db))
        manager._anotar_citacao(
            ResultadoEnvio(ok=True, via="texto", provider="evolution",
                           quote_status=QuoteStatus.UNVERIFIED, quoted_ok=False,
                           enviado_id="3EB0RESPOSTA"),
            "REQ000001", "Ryan")
        mensagens_de_log = [linha["message"] for linha in db.fetchall(
            "SELECT message FROM logs WHERE request_id='REQ000001'")]
        assert any("NÃO confirmada" in m for m in mensagens_de_log), mensagens_de_log
        assert not any("RECUSADA" in m for m in mensagens_de_log), mensagens_de_log


# --------------------------------------------------------- 3-7: falhar alto
class TestFalhaNuncaEhSilenciosa:
    """Defeito histórico: a ação falhava e o fluxo seguia como se tivesse ido."""

    def test_200_com_key_id_e_prova_de_entrega(self, cliente, espiao):
        r = cliente.send(GRUPO, "Simulações", "oi")
        assert r.ok
        assert r.evidencia["key_id"] == "3EB0DEADBEEF"

    def test_200_sem_key_id_nao_conta_como_entregue(self, tmp_path):
        """2xx sozinho não é prova. Etapa sem prova é etapa que falhou."""
        espiao = Espiao(httpx.Response(200, json={"status": "PENDING"}))
        c = EvolutionClient("http://e:8080", "k", "allana", GRUPO,
                            client=httpx.Client(transport=httpx.MockTransport(espiao),
                                                base_url="http://e:8080"),
                            renderer=None)
        r = c.send(GRUPO, "g", "oi")
        assert not r.ok
        assert "key.id" in r.motivo

    @pytest.mark.parametrize("status,desfecho", [
        # nada saiu, repetir a MESMA requisição depois pode dar certo
        (429, Desfecho.TRANSITORIA), (503, Desfecho.TRANSITORIA),
        # a mensagem PODE ter saído: não se manda outra. 502/504 vêm de um
        # proxy na frente da Evolution -- ela pode ter recebido e enviado.
        (500, Desfecho.INCERTA), (502, Desfecho.INCERTA), (504, Desfecho.INCERTA),
        # nada saiu e repetir não resolve
        (401, Desfecho.PERMANENTE), (403, Desfecho.PERMANENTE), (404, Desfecho.PERMANENTE),
    ])
    def test_classifica_para_saber_se_reenvia(self, status, desfecho):
        assert classificar_resposta(status, "detalhe").desfecho == desfecho

    def test_400_so_e_recusa_quando_a_evolution_diz_o_que_recusou(self):
        """Um 400 opaco pode ser erro DEPOIS do envio: não vira "nada saiu"."""
        assert classificar_resposta(400, 'requires property "number"').desfecho == Desfecho.RECUSADA
        assert classificar_resposta(400, "Bad Request").desfecho == Desfecho.INCERTA

    def test_licenca_pendente_e_nomeada_e_nao_reenvia(self):
        """503 comum é transitório; este 503 específico não adianta repetir."""
        classificacao = classificar_resposta(503, json.dumps({"error": LICENCA_PENDENTE}))
        assert classificacao.desfecho == Desfecho.PERMANENTE, "reenviar não ativa licença"
        assert classificacao.transitorio is False
        assert "/manager" in classificacao.motivo, "a mensagem tem de dizer o que fazer"

    def test_erro_500_nao_e_transitorio_e_sim_incerto(self, png):
        """O defeito que este teste trava: 500 disparava um segundo envio."""
        espiao = Espiao(httpx.Response(500, text="boom"))
        c = EvolutionClient("http://e:8080", "k", "allana", GRUPO,
                            client=httpx.Client(transport=httpx.MockTransport(espiao),
                                                base_url="http://e:8080"),
                            renderer=None)
        r = c.send_image(GRUPO, "g", png, caption="x", quote_message_id=ID_ORIGINAL)
        assert not r.ok
        assert r.desfecho == Desfecho.INCERTA
        assert r.sem_prova is True and r.transitorio is False
        assert len(espiao.chamadas) == 1, "tentou de novo depois de um 500"

    def test_erro_400_registra_o_corpo_inteiro(self):
        """A Evolution explica bem o que recusou -- jogar fora custa uma noite."""
        registrado: list[str] = []
        espiao = Espiao(httpx.Response(400, text="media inválido: base64 malformado"))
        c = EvolutionClient("http://e:8080", "k", "allana", GRUPO,
                            client=httpx.Client(transport=httpx.MockTransport(espiao),
                                                base_url="http://e:8080"),
                            renderer=None,
                            on_log=lambda nivel, msg: registrado.append(f"{nivel}:{msg}"))
        r = c.send(GRUPO, "g", "oi")
        assert not r.ok
        assert r.evidencia["transitorio"] is False
        assert any("base64 malformado" in linha for linha in registrado)

    def test_timeout_e_transitorio(self):
        def estoura(request):
            raise httpx.ConnectTimeout("demorou")
        c = EvolutionClient("http://e:8080", "k", "allana", GRUPO,
                            client=httpx.Client(transport=httpx.MockTransport(estoura),
                                                base_url="http://e:8080"),
                            renderer=None)
        r = c.send(GRUPO, "g", "oi")
        assert not r.ok
        assert r.evidencia["transitorio"] is True

    def test_erro_de_leitura_da_resposta_e_incerto(self):
        """A requisição subiu e a resposta veio quebrada: pode ter saído."""
        def quebrada(request):
            raise httpx.DecodingError("gzip cortado", request=request)
        c = EvolutionClient("http://e:8080", "k", "allana", GRUPO,
                            client=httpx.Client(transport=httpx.MockTransport(quebrada),
                                                base_url="http://e:8080"),
                            renderer=None)
        r = c.send(GRUPO, "g", "oi")
        assert not r.ok
        assert r.desfecho == Desfecho.INCERTA and r.sem_prova is True
        assert r.evidencia["transitorio"] is False

    def test_a_chave_nunca_aparece_no_motivo(self):
        espiao = Espiao(httpx.Response(401, text="unauthorized"))
        c = EvolutionClient("http://e:8080", "CHAVE-SUPER-SECRETA", "allana", GRUPO,
                            client=httpx.Client(transport=httpx.MockTransport(espiao),
                                                base_url="http://e:8080"),
                            renderer=None)
        r = c.send(GRUPO, "g", "oi")
        assert "CHAVE-SUPER-SECRETA" not in r.motivo
        assert "CHAVE-SUPER-SECRETA" not in json.dumps(r.evidencia)


# ------------------------------------------------------------------ 8: textos
class TestOsTextosContinuamVindoDeMensagensPy:
    def test_o_texto_passa_intacto(self, cliente, espiao):
        texto = mensagens.faltando(["CPF"], "Ryan")
        cliente.send(GRUPO, "Simulações", texto)
        assert espiao.ultimo["text"] == texto

    def test_a_legenda_passa_intacta(self, cliente, espiao, png):
        legenda = "✅ Libera R$ 4.913,52\nIvone Teste"
        cliente.send_image(GRUPO, "Simulações", png, caption=legenda)
        assert espiao.ultimo["caption"] == legenda

    def test_evolution_py_nao_escreve_fala_de_bot(self):
        """Se alguém escrever texto de resposta aqui, o tom se parte em dois.

        Foi o motivo de ``mensagens.py`` existir: os textos estavam espalhados
        e o bot falava de um jeito na legenda e de outro na resposta.
        """
        fonte = Path("app/evolution.py").read_text(encoding="utf-8")
        for assinatura in mensagens.ASSINATURAS:
            assert assinatura not in fonte, (
                f"fala de bot ({assinatura!r}) escrita em evolution.py")


# ----------------------------------------------------------- delay / banimento
class TestDelayHumano:
    """O número é do operador, e o risco de banimento é real."""

    def test_todo_envio_leva_delay(self, cliente, espiao, png):
        cliente.send(GRUPO, "g", "oi")
        cliente.send_image(GRUPO, "g", png, caption="x")
        for _, payload in espiao.chamadas:
            assert payload.get("delay") == DELAY_HUMANO_MS

    def test_o_delay_nao_e_zero(self):
        assert 1000 <= DELAY_HUMANO_MS <= 2000


# -------------------------------------------------------------------- contrato
class TestContratoEntreAsDuasCamadas:
    """O que torna a troca de modo segura.

    Sem isto, um parâmetro a mais numa implementação só apareceria em
    produção, no primeiro envio, com o consultor esperando.
    """

    def test_as_duas_tem_os_mesmos_metodos(self):
        from app.whatsapp import WhatsAppService

        for nome in METODOS_DO_CONTRATO:
            assert hasattr(EvolutionClient, nome), f"EvolutionClient sem {nome}"
            assert hasattr(WhatsAppService, nome), f"WhatsAppService sem {nome}"

    @pytest.mark.parametrize("nome", ["send", "send_image", "ja_enviado", "render_png"])
    def test_as_assinaturas_batem(self, nome):
        from app.whatsapp import WhatsAppService

        dom = inspect.signature(getattr(WhatsAppService, nome))
        evo = inspect.signature(getattr(EvolutionClient, nome))
        assert list(dom.parameters) == list(evo.parameters), (
            f"{nome} tem parâmetros diferentes entre as camadas")

    def test_as_duas_tem_inbox_e_status(self, cliente):
        from app.whatsapp import WhatsAppStatus

        assert hasattr(cliente, "inbox")
        assert isinstance(cliente.status, WhatsAppStatus)


# ------------------------------------------------------------------- ja_enviado
class TestGuardaDeReenvio:
    def test_lembra_do_que_mandou(self, cliente):
        assert cliente.ja_enviado("REQ000182") is False
        cliente.send(GRUPO, "g", "resultado\n_REQ000182_")
        assert cliente.ja_enviado("REQ000182") is True

    def test_envio_que_falhou_nao_conta_como_enviado(self):
        """Senão o reenvio nunca aconteceria justamente quando é necessário."""
        espiao = Espiao(httpx.Response(500, text="boom"))
        c = EvolutionClient("http://e:8080", "k", "allana", GRUPO,
                            client=httpx.Client(transport=httpx.MockTransport(espiao),
                                                base_url="http://e:8080"),
                            renderer=None)
        c.send(GRUPO, "g", "resultado _REQ000182_")
        assert c.ja_enviado("REQ000182") is False


# ---------------------------------------------------------------------- estado
class TestEstadoDaInstancia:
    def test_open_vira_conectado(self, tmp_path):
        espiao = Espiao(httpx.Response(200, json={"instance": {"state": "open"}}))
        c = EvolutionClient("http://e:8080", "k", "allana", GRUPO,
                            client=httpx.Client(transport=httpx.MockTransport(espiao),
                                                base_url="http://e:8080"),
                            renderer=None)
        assert c.atualizar_estado() == "open"
        assert c.status.connected

    def test_close_derruba_o_status(self):
        espiao = Espiao(httpx.Response(200, json={"instance": {"state": "close"}}))
        c = EvolutionClient("http://e:8080", "k", "allana", GRUPO,
                            client=httpx.Client(transport=httpx.MockTransport(espiao),
                                                base_url="http://e:8080"),
                            renderer=None)
        c.atualizar_estado()
        assert not c.status.connected

    def test_evolution_fora_do_ar_nao_fica_conectado(self):
        """O pior cenário conhecido: "conectado" na tela e mudo no grupo."""
        def estoura(request):
            raise httpx.ConnectError("recusou")
        c = EvolutionClient("http://e:8080", "k", "allana", GRUPO,
                            client=httpx.Client(transport=httpx.MockTransport(estoura),
                                                base_url="http://e:8080"),
                            renderer=None)
        c.atualizar_estado()
        assert not c.status.connected
        assert c.status.last_error


class TestAQuedaParaTextoFuncionaNasDuasCamadas:
    """As duas camadas avisam de falha de jeitos diferentes.

    A do navegador LEVANTA exceção; a da Evolution DEVOLVE ``ok=False``,
    porque ela tem a resposta HTTP para explicar o motivo. O manager só olhava
    a exceção -- então uma imagem recusada pela Evolution passaria como
    enviada, e o consultor ficaria sem resposta nenhuma.

    É exatamente o defeito "falhar calado" que esta migração existe para
    encerrar, e ele reapareceu na costura entre as camadas.
    """

    def _manager_com(self, tmp_path, whatsapp):
        from app.db import Database
        from app.events import EventHub
        from app.manager import BotManager
        from tests.test_concurrency import _config

        config = _config(tmp_path, send_image=True)
        db = Database(config.db_path)
        manager = BotManager(config, db, EventHub(db))
        manager.whatsapp = whatsapp
        return manager

    def _resultado(self, tmp_path):
        from app.models import (IncomingMessage, ParsedRequest, SimulationJob,
                                SimulationResult)

        mensagem = IncomingMessage(
            message_id="3EB0X", chat_id=GRUPO, chat_name="g",
            sender_id="556799@s.whatsapp.net", sender_name="Ryan",
            text="Ivone\n42888832453")
        pedido = ParsedRequest(consultant_name="Ryan", cpf="42888832453",
                               bank="Santander", contract="900001",
                               customer_name="Ivone Teste")
        job = SimulationJob(request_id="REQ000900", request=pedido,
                            message=mensagem, simulation_id=1)
        return SimulationResult(job=job, ok=True, status="Sim",
                                reduction_value=4913.52)

    def test_imagem_recusada_cai_para_texto(self, tmp_path):
        """O caso da Evolution: devolve ok=False em vez de levantar."""
        from app.models import ResultadoEnvio

        class RecusaAImagem:
            def render_png(self, html, path, width=900, timeout=60.0):
                destino = Path(path)
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(png_valido())
                return str(destino)

            def send_image(self, *a, **k):
                return ResultadoEnvio(ok=False, via="imagem",
                                      motivo="a Evolution recusou (400)")

        manager = self._manager_com(tmp_path, RecusaAImagem())
        assert manager._send_result_image(self._resultado(tmp_path)) is False, (
            "ok=False tem de derrubar o envio, senão o consultor fica sem resposta")

    def test_imagem_que_levanta_tambem_cai_para_texto(self, tmp_path):
        """O caso do navegador, que já funcionava -- e tem de continuar."""
        class EstouraNaImagem:
            def render_png(self, html, path, width=900, timeout=60.0):
                destino = Path(path)
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(png_valido())
                return str(destino)

            def send_image(self, *a, **k):
                raise RuntimeError("não encontrei o campo de anexo")

        manager = self._manager_com(tmp_path, EstouraNaImagem())
        assert manager._send_result_image(self._resultado(tmp_path)) is False

    def test_a_prova_de_entrega_e_gravada(self, tmp_path):
        """O key.id liga a linha do banco à mensagem que existe no WhatsApp."""
        from app.models import ResultadoEnvio

        class Aceita:
            def render_png(self, html, path, width=900, timeout=60.0):
                destino = Path(path)
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(png_valido())
                return str(destino)

            def send_image(self, *a, **k):
                return ResultadoEnvio(ok=True, via="imagem", tipo_midia="imagem",
                                      evidencia={"key_id": "3EB0PROVA"})

        manager = self._manager_com(tmp_path, Aceita())
        assert manager._send_result_image(self._resultado(tmp_path)) is True
        linha = manager.db.fetchone(
            "SELECT wa_message_id FROM messages WHERE direction='out'")
        assert linha["wa_message_id"] == "3EB0PROVA"

    def test_camada_antiga_sem_prova_nao_quebra(self, tmp_path):
        """O modo dom devolve booleano: sem key.id, e está tudo bem."""
        from app.manager import BotManager

        assert BotManager._prova_de_entrega(True) == ""
        assert BotManager._prova_de_entrega(None) == ""
