"""O print da tela do Santander — e o que ele nunca pode mostrar.

O consultor confia no que ele mesmo veria no portal. Um card montado por nós
é uma transcrição: se a leitura errar um campo, o erro chega bonito. O print
é a tela.

O risco mora no recorte. A página inteira carrega, no topo, quem está logado::

    Parceiro Santander
    Fulaninha
    Xyz Promotora Ltda Me
    Home  Meu negócio  Produtos  Propostas  Serviços  Central de ajuda

Isso é o acesso da empresa ao banco. Num grupo com dezenas de consultores,
mandar isso é um vazamento — e um print bonito é justamente o tipo de coisa
que ninguém revisa antes de enviar. Por isso quase todo teste aqui verifica o
que o recorte **recusa**.

O texto da página vem de `debug_cards.txt`, capturado do portal de verdade.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from app.tela_do_portal import (ANCORAS_DO_RESULTADO, MARCAS_DO_TOPO,
                                RECORTE_JS, capturar, recusar)

@pytest.fixture(scope="module")
def navegador():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


#: O topo, exatamente como `debug_cards.txt` o registrou.
TOPO = """
<header style="height:120px">
  <div>Parceiro Santander</div>
  <div>Fulaninha</div>
  <div>Xyz Promotora Ltda Me</div>
  <nav>Home Meu negócio Produtos Propostas Serviços Central de ajuda Expandir menu</nav>
</header>"""

#: O bloco do cliente, que fica ACIMA dos contratos.
CLIENTE = """
<section style="height:100px">
  <p>Nome do cliente</p><p>LUCIÂNGELA TESTADO</p>
  <p>CPF</p><p>728.455.547-53</p>
</section>"""

#: Os contratos, com os campos que o portal mostra.
CARDS = """
<section id="resultado" style="width:900px">
  <h2>Selecione os contratos que deseja refinanciar</h2>
  <div class="lista" style="height:300px">
    <article>
      <span>7******46</span>
      <p>Parcelas</p><p>70</p>
      <p>Valor da parcela</p><p>R$ 2.480,00</p>
      <p>Taxa</p><p>1,43% a.m</p>
      <p>Parcelas pagas</p><p>24</p>
      <p>Saldo devedor</p><p>R$ 83.585,39</p>
    </article>
  </div>
</section>"""

PAGINA = f"<!doctype html><html><body>{TOPO}{CLIENTE}{CARDS}</body></html>"


def _recorte(navegador, html: str) -> dict:
    pagina = navegador.new_page()
    try:
        pagina.set_content(html)
        return pagina.evaluate(
            RECORTE_JS, {"ancoras": list(ANCORAS_DO_RESULTADO), "cards": "saldo devedor"})
    finally:
        pagina.close()


class TestOndeORecorteCai:
    def test_acha_a_area_dos_contratos(self, navegador):
        r = _recorte(navegador, PAGINA)
        assert r["achou"] is True
        assert "Saldo devedor" in r["texto"]

    def test_o_topo_fica_de_fora(self, navegador):
        """O que o consultor não pode ver: quem está logado no portal."""
        texto = _recorte(navegador, PAGINA)["texto"]
        assert "Parceiro Santander" not in texto
        assert "Fulaninha" not in texto
        assert "Xyz Promotora" not in texto

    def test_o_cpf_do_cliente_fica_de_fora(self, navegador):
        """O bloco do cliente está acima dos contratos; subir demais o pega."""
        assert "728.455.547-53" not in _recorte(navegador, PAGINA)["texto"]

    def test_para_no_primeiro_ancestral_com_contratos(self, navegador):
        """Cada nível a mais aproxima o recorte do topo da página."""
        assert _recorte(navegador, PAGINA)["subidas"] <= 3

    def test_sem_a_tela_de_resultado_nao_ha_recorte(self, navegador):
        r = _recorte(navegador, f"<!doctype html><html><body>{TOPO}{CLIENTE}</body></html>")
        assert r["achou"] is False
        assert r["motivo"]

    def test_titulo_sem_contratos_nao_vale(self, navegador):
        """A tela pode ter o título e ainda estar carregando os cards."""
        so_titulo = ("<section><h2>Selecione os contratos que deseja "
                     "refinanciar</h2></section>")
        r = _recorte(navegador, f"<!doctype html><html><body>{so_titulo}</body></html>")
        assert r["achou"] is False


class TestOQueERecusado:
    """A regra de recusa vive fora do navegador de propósito.

    Ela é o que impede um vazamento; uma decisão dessas precisa de teste, e
    teste que dependa de navegador logado não roda.
    """

    BOM = {"achou": True, "largura": 900, "altura": 400,
           "texto": "Selecione os contratos\n7******46\nSaldo devedor R$ 83.585,39"}

    def test_recorte_bom_passa(self):
        assert recusar(self.BOM) == ""

    @pytest.mark.parametrize("marca", MARCAS_DO_TOPO)
    def test_qualquer_marca_do_topo_descarta(self, marca):
        ruim = {**self.BOM, "texto": self.BOM["texto"] + "\n" + marca.upper()}
        assert "topo" in recusar(ruim)

    def test_acento_nao_escapa_da_regra(self):
        """"Meu negócio" na tela, "meu negocio" na lista."""
        ruim = {**self.BOM, "texto": "Home Meu negócio Produtos\n" + self.BOM["texto"]}
        assert recusar(ruim) != ""

    @pytest.mark.parametrize("cpf", ["728.455.547-53", "72845554753"])
    def test_cpf_a_vista_descarta(self, cpf):
        ruim = {**self.BOM, "texto": f"Nome do cliente\n{cpf}\n" + self.BOM["texto"]}
        assert "CPF" in recusar(ruim)

    def test_recorte_gigante_descarta(self):
        """Recorte do tamanho da página é 'subi até o body'."""
        assert recusar({**self.BOM, "altura": 5000}) != ""
        assert recusar({**self.BOM, "largura": 4000}) != ""

    def test_recorte_minusculo_descarta(self):
        assert recusar({**self.BOM, "altura": 10}) != ""

    def test_sem_recorte_devolve_o_motivo_de_quem_mediu(self):
        assert recusar({"achou": False, "motivo": "nao achei o titulo"}) == "nao achei o titulo"

    def test_nada_nao_estoura(self):
        assert recusar({}) != "" and recusar(None) != ""


class TestCapturarNuncaDerruba:
    """Sem print o consultor ainda recebe a resposta escrita."""

    class _PaginaQueFalha:
        def evaluate(self, *_a, **_k):
            raise RuntimeError("o portal caiu")

    class _PaginaSemFoto:
        def evaluate(self, *_a, **_k):
            return {"achou": True, "x": 0, "y": 0, "largura": 900, "altura": 400,
                    "texto": "Selecione os contratos Saldo devedor"}

        def screenshot(self, **_k):
            raise RuntimeError("sem espaço em disco")

    def test_portal_indisponivel_vira_motivo(self, tmp_path):
        caminho, motivo = capturar(self._PaginaQueFalha(), tmp_path / "x.png")
        assert caminho == "" and motivo

    def test_falha_ao_fotografar_vira_motivo(self, tmp_path):
        caminho, motivo = capturar(self._PaginaSemFoto(), tmp_path / "x.png")
        assert caminho == "" and motivo

    def test_recusa_nao_gera_arquivo(self, tmp_path):
        class ComTopo(self._PaginaSemFoto):
            def evaluate(self, *_a, **_k):
                return {"achou": True, "x": 0, "y": 0, "largura": 900, "altura": 400,
                        "texto": "Parceiro Santander Fulaninha Saldo devedor"}

        destino = tmp_path / "x.png"
        caminho, motivo = capturar(ComTopo(), destino)
        assert caminho == "" and "topo" in motivo
        assert not destino.exists(), "descartado, mas o arquivo ficou no disco"

    def test_captura_boa_devolve_o_caminho(self, navegador, tmp_path):
        pagina = navegador.new_page()
        try:
            pagina.set_content(PAGINA)
            destino = tmp_path / "REQ000182_portal.png"
            caminho, motivo = capturar(pagina, destino)
            assert motivo == "" and caminho
            assert destino.exists() and destino.stat().st_size > 0
        finally:
            pagina.close()


class TestOPrintVemNaFrenteDoCard:
    """Qual imagem o consultor recebe.

    O card é a nossa transcrição da tela; o print é a tela. Quando os dois
    existem, o print ganha — foi o que o Ryan pediu. Quando o recorte se
    descarta (topo da página à vista), o card entra no lugar: o consultor
    nunca fica sem imagem por causa disso.
    """

    def _manager(self, tmp_path, whatsapp, imagem="portal"):
        from app.db import Database
        from app.events import EventHub
        from app.manager import BotManager
        from tests.test_concurrency import _config

        config = _config(tmp_path, send_image=True, imagem_da_resposta=imagem)
        db = Database(config.db_path)
        manager = BotManager(config, db, EventHub(db))
        manager.comprovantes_dir = tmp_path / "comprovantes"
        manager.whatsapp = whatsapp
        return manager

    def _resultado(self, portal_png=""):
        from app.models import (IncomingMessage, ParsedRequest, SimulationJob,
                                SimulationResult)

        msg = IncomingMessage(message_id="3EB0X", chat_id="g@g.us", chat_name="g",
                              sender_id="55@s.whatsapp.net", sender_name="Ryan",
                              text="Jose\n52998224725")
        pedido = ParsedRequest(consultant_name="Ryan", cpf="52998224725",
                               bank="Santander", contract="",
                               customer_name="Jose da Silva")
        job = SimulationJob(request_id="REQ000900", request=pedido, message=msg,
                            simulation_id=1)
        return SimulationResult(job=job, ok=True, status="Sim",
                                reduction_value=4913.52, portal_png=portal_png)

    @staticmethod
    def _foto(tmp_path, dados=None):
        from tests.test_concurrency import png_valido

        foto = tmp_path / "REQ000900_portal.png"
        foto.write_bytes(dados if dados is not None else png_valido(7, 5, b"portal"))
        return foto

    class _Whatsapp:
        def __init__(self):
            self.enviada = ""
            self.bytes_enviados = b""
            self.renderizou = False

        def render_png(self, html, path, width=900, timeout=60.0):
            from pathlib import Path

            from tests.test_concurrency import png_valido

            self.renderizou = True
            destino = Path(path)
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(png_valido(4, 3, b"card"))
            return str(destino)

        def send_image(self, *, image_path, **k):
            from pathlib import Path

            from app.models import ResultadoEnvio

            self.enviada = str(image_path)
            self.bytes_enviados = Path(image_path).read_bytes()
            return ResultadoEnvio(ok=True, via="imagem", tipo_midia="imagem",
                                  evidencia={"key_id": "3EB0PROVA"})

    def test_com_print_o_card_nem_e_montado(self, tmp_path):
        foto = self._foto(tmp_path)
        wa = self._Whatsapp()
        manager = self._manager(tmp_path, wa)
        assert manager._send_result_image(self._resultado(str(foto))) is True
        assert wa.renderizou is False, "montou o card à toa"
        assert wa.bytes_enviados == foto.read_bytes(), "não foi o print que saiu"
        # Vai com o nome do pedido, na pasta de comprovantes: é o que amarra a
        # imagem ao REQ (e o que o painel e o reenvio procuram).
        assert Path(wa.enviada) == tmp_path / "comprovantes" / "REQ000900.png"

    def test_sem_print_o_card_entra_no_lugar(self, tmp_path):
        """O recorte recusado não pode deixar o consultor sem imagem."""
        wa = self._Whatsapp()
        manager = self._manager(tmp_path, wa)
        assert manager._send_result_image(self._resultado("")) is True
        assert wa.renderizou is True
        assert wa.enviada.endswith("REQ000900.png")

    def test_print_que_sumiu_do_disco_cai_para_o_card(self, tmp_path):
        wa = self._Whatsapp()
        manager = self._manager(tmp_path, wa)
        caminho = str(tmp_path / "apagado.png")
        assert manager._send_result_image(self._resultado(caminho)) is True
        assert wa.renderizou is True

    def test_print_truncado_cai_para_o_card(self, tmp_path):
        """Um print quebrado nunca vai para o grupo: o card entra no lugar."""
        from tests.test_concurrency import png_valido

        foto = self._foto(tmp_path, png_valido(7, 5, b"portal")[:-20])
        wa = self._Whatsapp()
        manager = self._manager(tmp_path, wa)
        assert manager._send_result_image(self._resultado(str(foto))) is True
        assert wa.renderizou is True
        assert wa.bytes_enviados != foto.read_bytes()

    def test_quem_prefere_o_card_continua_com_o_card(self, tmp_path):
        foto = self._foto(tmp_path)
        wa = self._Whatsapp()
        manager = self._manager(tmp_path, wa, imagem="card")
        assert manager._send_result_image(self._resultado(str(foto))) is True
        assert wa.renderizou is True
        assert wa.enviada.endswith("REQ000900.png")

    def test_sem_dado_do_cliente_o_print_nao_sai(self, tmp_path):
        """IMAGE_SHOW_CLIENT_DATA=false vale para a imagem INTEIRA.

        O print é a tela do banco: nome e CPF vão como pixels e não há como
        mascarar. Antes a flag só valia para o card, e o print -- o padrão --
        saía com tudo à mostra.
        """
        from dataclasses import replace

        foto = self._foto(tmp_path)
        wa = self._Whatsapp()
        htmls = []
        renderizar = wa.render_png
        wa.render_png = lambda html, path, **k: (htmls.append(html), renderizar(html, path, **k))[1]
        manager = self._manager(tmp_path, wa)
        manager.config = replace(manager.config, image_show_client_data=False)

        assert manager._send_result_image(self._resultado(str(foto))) is True
        assert wa.renderizou is True, "mandou o print da tela, com o dado do cliente"
        assert wa.bytes_enviados != foto.read_bytes()
        assert "52998224725" not in htmls[0] and "529.982.247-25" not in htmls[0], (
            "o card também tem de sair mascarado")


class TestSimuladorSoFotografaQuandoOPrintPodeSair:
    """Com IMAGE_SHOW_CLIENT_DATA=false o print nem é tirado: um PNG com CPF
    parado no disco, sem uso, é só mais um lugar de onde o dado vaza."""

    def _chamar(self, tmp_path, monkeypatch, mostrar: bool):
        from dataclasses import replace
        from types import SimpleNamespace

        from app import simulator as modulo
        from app.models import IncomingMessage, ParsedRequest, SimulationJob
        from tests.test_concurrency import _config

        capturas = []
        monkeypatch.setattr(modulo.tela_do_portal, "capturar",
                            lambda pagina, destino, *a, **k: (capturas.append(destino)
                                                             or (str(destino), "")))
        falso = SimpleNamespace(_page=object(), _log=lambda *a, **k: None,
                                config=replace(_config(tmp_path), image_show_client_data=mostrar))
        msg = IncomingMessage(message_id="3EB0Y", chat_id="g@g.us", chat_name="g",
                              sender_id="55@s.whatsapp.net", sender_name="Ryan", text="x")
        job = SimulationJob(request_id="REQ000901", message=msg, simulation_id=1,
                            request=ParsedRequest(consultant_name="Ryan", cpf="52998224725",
                                                  bank="Santander", contract="",
                                                  customer_name="Jose da Silva"))
        return modulo.SimulatorService._fotografar_o_resultado(falso, job), capturas

    def test_flag_false_nao_fotografa(self, tmp_path, monkeypatch):
        caminho, capturas = self._chamar(tmp_path, monkeypatch, mostrar=False)
        assert caminho == "" and capturas == []

    def test_flag_true_continua_fotografando(self, tmp_path, monkeypatch):
        caminho, capturas = self._chamar(tmp_path, monkeypatch, mostrar=True)
        assert caminho.endswith("REQ000901_portal.png") and len(capturas) == 1
