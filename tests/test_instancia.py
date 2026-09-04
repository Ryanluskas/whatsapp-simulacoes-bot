"""Trava de instância única — um processo por perfil do navegador.

Numa madrugada de depuração descobrimos DUAS instâncias do bot rodando ao
mesmo tempo (uma na `.venv`, outra no Python global), as duas apontando para
o mesmo `.whatsapp-profile`. O Chromium aceita um processo por
`user_data_dir`: a segunda subia, brigava pelo lock, e o resultado era
instabilidade que parecia bug de código — aba em branco, sessão caindo,
conversa fechando sozinha.

A trava é keyed pelo **perfil**, não pelo programa: o recurso disputado é o
diretório, e rodar um laboratório de diagnóstico com o bot no ar é exatamente
o mesmo conflito.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from app.instancia import InstanciaEmUso, TravaDeInstancia, explicar, processo


@pytest.fixture()
def trava(tmp_path):
    return TravaDeInstancia(tmp_path / "perfil", rotulo="teste", pasta=tmp_path)


class TestSaberSeUmProcessoEstaVivo:
    """A consulta não pode ter efeito colateral.

    `os.kill(pid, 0)` NÃO serve no Windows: fora de CTRL_C_EVENT e
    CTRL_BREAK_EVENT o Python chama `TerminateProcess`, ou seja, perguntar
    "está vivo?" **mataria** o processo. Daí o caminho pelo ctypes.
    """

    def test_o_proprio_processo_esta_vivo(self):
        vivo, criacao = processo(os.getpid())
        assert vivo is True
        if os.name == "nt":
            assert criacao > 0, "sem a hora de criação, PID reaproveitado engana"

    def test_pid_inexistente(self):
        assert processo(999_999)[0] is False

    @pytest.mark.parametrize("pid", [0, -1, -999])
    def test_pid_invalido_nao_esta_vivo(self, pid):
        assert processo(pid)[0] is False

    def test_consultar_nao_mata_ninguem(self):
        """Se `processo()` matasse, este teste derrubaria a própria suíte."""
        for _ in range(5):
            assert processo(os.getpid())[0] is True
        assert processo(os.getpid())[0] is True


class TestUmProcessoPorPerfil:
    def test_livre_no_comeco(self, trava):
        assert trava.dono() is None

    def test_adquirir_e_liberar(self, trava):
        trava.adquirir()
        assert (trava.dono() or {}).get("pid") == os.getpid()
        trava.liberar()
        assert trava.dono() is None

    def test_a_segunda_e_recusada(self, trava, tmp_path):
        trava.adquirir()
        outra = TravaDeInstancia(tmp_path / "perfil", rotulo="segunda", pasta=tmp_path)
        with pytest.raises(InstanciaEmUso) as erro:
            outra.adquirir()
        assert erro.value.pid == os.getpid()
        assert "teste" in str(erro.value)

    def test_perfis_diferentes_nao_colidem(self, tmp_path):
        """A trava é do PERFIL. Dois perfis são dois recursos."""
        a = TravaDeInstancia(tmp_path / "perfil-a", rotulo="a", pasta=tmp_path)
        b = TravaDeInstancia(tmp_path / "perfil-b", rotulo="b", pasta=tmp_path)
        a.adquirir()
        b.adquirir()          # não pode levantar
        assert a.dono() and b.dono()

    def test_o_mesmo_perfil_por_caminhos_diferentes_colide(self, tmp_path):
        """`perfil` e `./perfil` são o mesmo diretório — e o mesmo Chromium."""
        a = TravaDeInstancia(tmp_path / "perfil", rotulo="a", pasta=tmp_path)
        b = TravaDeInstancia(tmp_path / "." / "perfil", rotulo="b", pasta=tmp_path)
        a.adquirir()
        with pytest.raises(InstanciaEmUso):
            b.adquirir()

    def test_programas_diferentes_no_mesmo_perfil_colidem(self, tmp_path):
        """Rodar um laboratório com o bot no ar é o MESMO conflito."""
        bot = TravaDeInstancia(tmp_path / "perfil", rotulo="bot", pasta=tmp_path)
        lab = TravaDeInstancia(tmp_path / "perfil", rotulo="laboratório", pasta=tmp_path)
        bot.adquirir()
        with pytest.raises(InstanciaEmUso):
            lab.adquirir()


class TestTravaQueNaoTrancaPorEngano:
    """Recusar subir é caro. Recusar sem motivo é pior."""

    def test_pid_reaproveitado_nao_conta_como_dono(self, trava):
        """Depois de um reinício, o PID antigo pode ser de um Excel aberto."""
        trava.adquirir()
        dados = json.loads(trava.arquivo.read_text(encoding="utf-8"))
        dados["criacao"] = 1          # hora impossível: é outro processo
        trava.arquivo.write_text(json.dumps(dados), encoding="utf-8")
        assert trava.dono() is None

    def test_arquivo_corrompido_nao_trava_o_boot_para_sempre(self, trava):
        trava.arquivo.parent.mkdir(parents=True, exist_ok=True)
        trava.arquivo.write_text("{isto não é json", encoding="utf-8")
        assert trava.dono() is None

    def test_dono_morto_libera_a_trava(self, trava):
        trava.adquirir()
        dados = json.loads(trava.arquivo.read_text(encoding="utf-8"))
        dados["pid"] = 999_999
        dados.pop("criacao", None)
        trava.arquivo.write_text(json.dumps(dados), encoding="utf-8")
        assert trava.dono() is None, "lock órfão de um processo morto trava tudo"

    def test_liberar_nunca_apaga_a_trava_de_outro(self, trava, tmp_path):
        """Nosso encerramento não pode soltar o perfil de quem está usando."""
        trava.adquirir()
        outra = TravaDeInstancia(tmp_path / "perfil", rotulo="outra", pasta=tmp_path)
        outra.liberar()                      # nunca adquiriu: não é dela
        assert (trava.dono() or {}).get("pid") == os.getpid()

    def test_liberar_duas_vezes_nao_estoura(self, trava):
        trava.adquirir()
        trava.liberar()
        trava.liberar()


class TestUsoComoContexto:
    def test_solta_no_fim_do_bloco(self, trava):
        with trava:
            assert trava.dono() is not None
        assert trava.dono() is None

    def test_solta_mesmo_com_excecao(self, trava):
        with pytest.raises(ValueError):
            with trava:
                raise ValueError("algo deu errado no meio")
        assert trava.dono() is None


class TestConcorrencia:
    def test_so_uma_thread_adquire(self, tmp_path):
        """Duas instâncias subindo ao mesmo tempo — o caso que motivou tudo."""
        travas = [TravaDeInstancia(tmp_path / "perfil", rotulo=f"t{i}",
                                   pasta=tmp_path) for i in range(8)]
        ganhou: list[int] = []
        porteira = threading.Barrier(len(travas))

        def corre(i: int) -> None:
            porteira.wait()
            try:
                travas[i].adquirir()
                ganhou.append(i)
            except InstanciaEmUso:
                pass

        fios = [threading.Thread(target=corre, args=(i,)) for i in range(len(travas))]
        for f in fios:
            f.start()
        for f in fios:
            f.join()
        assert len(ganhou) == 1, f"{len(ganhou)} instâncias subiram no mesmo perfil"


class TestAMensagemDizOQueFazer:
    """"Recusando iniciar" sem dizer por quê custa uma ligação."""

    def test_traz_o_pid_e_o_comando(self, tmp_path):
        erro = InstanciaEmUso(4321, "bot (main.py)", "2026-09-01T09:01:20Z")
        texto = explicar(erro, tmp_path / "perfil")
        assert "4321" in texto
        assert "bot (main.py)" in texto
        assert str(tmp_path / "perfil") in texto
        assert ("taskkill" in texto) if os.name == "nt" else ("kill" in texto)

    def test_explica_o_motivo_e_nao_so_o_fato(self, tmp_path):
        texto = explicar(InstanciaEmUso(1, "bot"), tmp_path)
        assert "um processo por perfil" in texto.lower()


class TestAJanelaEntreCriarEEscrever:
    """"Sendo escrito" não é "corrompido" — confundir os dois abre a trava.

    Entre criar o arquivo com `O_EXCL` e gravar o conteúdo existe uma janela.
    Quem lê nela vê um arquivo VAZIO, o JSON falha, `dono()` devolve None — e
    o código conclui "lock órfão", apaga e assume. Duas instâncias no mesmo
    perfil, que é exatamente o que a trava existe para impedir.

    A janela é minúscula: o teste de concorrência passou 20 vezes seguidas
    isolado e só falhou sob a carga da suíte inteira, com CPU disputada.
    Falha rara em trava de exclusão é pior que falha frequente — ela aparece
    quando o sistema está ocupado, que é justamente quando dói.
    """

    def test_arquivo_vazio_nao_e_lock_livre(self, trava, tmp_path):
        """O caso exato: alguém criou e ainda não escreveu."""
        import json
        import os
        import threading
        import time

        trava.pasta.mkdir(parents=True, exist_ok=True)
        trava.arquivo.write_text("", encoding="utf-8")

        def terminar_de_escrever():
            time.sleep(0.15)
            trava.arquivo.write_text(
                json.dumps({"pid": os.getpid(), "rotulo": "a"}), encoding="utf-8")

        threading.Thread(target=terminar_de_escrever).start()

        outra = TravaDeInstancia(tmp_path / "perfil", rotulo="b", pasta=tmp_path)
        with pytest.raises(InstanciaEmUso):
            outra.adquirir()

    def test_lock_corrompido_de_verdade_continua_sendo_limpo(self, trava):
        """Esperar não pode virar travar para sempre."""
        import time

        trava.pasta.mkdir(parents=True, exist_ok=True)
        trava.arquivo.write_text("{lixo permanente", encoding="utf-8")
        comeco = time.monotonic()
        trava.adquirir()
        gasto = time.monotonic() - comeco
        assert trava.dono() is not None, "não assumiu um lock realmente quebrado"
        assert gasto < 3.0, f"esperou {gasto:.1f}s — paciência demais trava o boot"

    def test_a_paciencia_e_curta(self):
        """Ela atrasa TODO boot que encontra um lock ilegível."""
        assert 0 < TravaDeInstancia._PACIENCIA_COM_LOCK_NOVO <= 2.0

    def test_arquivo_que_some_no_meio_da_espera(self, trava):
        """O dono desistiu e apagou: a trava está livre de verdade."""
        import threading
        import time

        trava.pasta.mkdir(parents=True, exist_ok=True)
        trava.arquivo.write_text("", encoding="utf-8")

        def desistir():
            time.sleep(0.1)
            try:
                trava.arquivo.unlink()
            except OSError:
                pass

        threading.Thread(target=desistir).start()
        trava.adquirir()
        assert (trava.dono() or {}).get("pid") == os.getpid()

    @pytest.mark.parametrize("rodada", range(6))
    def test_a_corrida_com_muitas_threads_repetida(self, tmp_path, rodada):
        """Repetido: uma falha rara não aparece numa execução só."""
        import threading

        travas = [TravaDeInstancia(tmp_path / "perfil", rotulo=f"t{i}",
                                   pasta=tmp_path) for i in range(12)]
        ganhou: list[int] = []
        porteira = threading.Barrier(len(travas))

        def corre(i: int) -> None:
            porteira.wait()
            try:
                travas[i].adquirir()
                ganhou.append(i)
            except InstanciaEmUso:
                pass

        fios = [threading.Thread(target=corre, args=(i,)) for i in range(len(travas))]
        for f in fios:
            f.start()
        for f in fios:
            f.join()
        assert len(ganhou) == 1, f"{len(ganhou)} instâncias no mesmo perfil"
