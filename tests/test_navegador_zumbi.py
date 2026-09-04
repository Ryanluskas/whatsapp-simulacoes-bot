"""Navegador órfão segurando o perfil — e por que a mira é estreita.

01/09, 15:22: o WhatsApp caiu e o bot não conseguiu reabrir::

    ERROR  Falha ao iniciar o WhatsApp: ... provavelmente o perfil
           '.whatsapp-profile' já está aberto em outro navegador.

Um `brave.exe` da execução morta continuou vivo segurando o `user_data_dir`.
A `TravaDeInstancia` não cobre isso: ela impede dois **bots**, e aqui o
problema é um **navegador** sem dono.

O risco destes testes protegerem é o oposto do óbvio: matar processo é
irreversível, e o navegador pessoal do operador não pode ser tocado nunca.
Por isso quase todo teste aqui verifica o que **não** é alvo.
"""

from __future__ import annotations

import os

import pytest

from app.navegador_zumbi import NAVEGADORES, _normalizar, donos_do_perfil, encerrar_orfaos


class TestAMiraEEstreita:
    """Só casa o perfil EXATO. Qualquer dúvida deixa o processo de pé."""

    PERFIL = r"C:\Users\Ryyan\Downloads\bot\.whatsapp-profile"

    def _com_processos(self, monkeypatch, processos):
        monkeypatch.setattr("app.navegador_zumbi._processos_do_windows",
                            lambda: processos)

    def test_acha_quem_usa_o_perfil(self, monkeypatch):
        self._com_processos(monkeypatch, [{
            "ProcessId": "1234", "Name": "brave.exe",
            "CommandLine": f'brave.exe --user-data-dir="{self.PERFIL}" --no-first-run',
        }])
        donos = donos_do_perfil(self.PERFIL)
        assert [d["pid"] for d in donos] == [1234]

    def test_perfil_vizinho_nao_e_alvo(self, monkeypatch):
        """`.whatsapp-profile-2` não é `.whatsapp-profile`."""
        self._com_processos(monkeypatch, [{
            "ProcessId": "1234", "Name": "brave.exe",
            "CommandLine": f'brave.exe --user-data-dir="{self.PERFIL}-2"',
        }])
        assert donos_do_perfil(self.PERFIL) == []

    def test_o_navegador_pessoal_nao_e_alvo(self, monkeypatch):
        """Sem `--user-data-dir`, é o Brave do operador. Nunca tocar."""
        self._com_processos(monkeypatch, [
            {"ProcessId": "1", "Name": "brave.exe", "CommandLine": "brave.exe"},
            {"ProcessId": "2", "Name": "brave.exe",
             "CommandLine": "brave.exe https://youtube.com"},
        ])
        assert donos_do_perfil(self.PERFIL) == []

    def test_outro_perfil_qualquer_nao_e_alvo(self, monkeypatch):
        self._com_processos(monkeypatch, [{
            "ProcessId": "1", "Name": "brave.exe",
            "CommandLine": r'brave.exe --user-data-dir="C:\Users\Ryyan\AppData\Brave"',
        }])
        assert donos_do_perfil(self.PERFIL) == []

    def test_barra_e_caixa_nao_atrapalham(self, monkeypatch):
        """O mesmo diretório escrito de outro jeito continua sendo o mesmo."""
        self._com_processos(monkeypatch, [{
            "ProcessId": "7", "Name": "brave.exe",
            "CommandLine": f'brave.exe --user-data-dir={self.PERFIL.upper()}',
        }])
        assert len(donos_do_perfil(self.PERFIL)) == 1

    def test_sem_aspas_tambem_casa(self, monkeypatch):
        self._com_processos(monkeypatch, [{
            "ProcessId": "8", "Name": "brave.exe",
            "CommandLine": f"brave.exe --user-data-dir={self.PERFIL} --lang=pt-BR",
        }])
        assert len(donos_do_perfil(self.PERFIL)) == 1

    def test_varios_orfaos_do_mesmo_perfil(self, monkeypatch):
        """O Chromium abre vários processos: o pai e os filhos."""
        self._com_processos(monkeypatch, [
            {"ProcessId": str(p), "Name": "brave.exe",
             "CommandLine": f'brave.exe --user-data-dir="{self.PERFIL}"'}
            for p in (10, 11, 12)
        ])
        assert sorted(d["pid"] for d in donos_do_perfil(self.PERFIL)) == [10, 11, 12]

    def test_pid_invalido_e_descartado(self, monkeypatch):
        self._com_processos(monkeypatch, [{
            "ProcessId": "0", "Name": "brave.exe",
            "CommandLine": f'brave.exe --user-data-dir="{self.PERFIL}"',
        }])
        assert donos_do_perfil(self.PERFIL) == []


class TestSoNavegadores:
    def test_a_lista_e_fechada(self):
        """Nenhum outro programa entra na varredura."""
        assert set(NAVEGADORES) == {"brave.exe", "chrome.exe", "msedge.exe"}

    def test_a_consulta_filtra_por_nome(self):
        """O WMIC já recusa qualquer processo fora da lista."""
        import inspect

        from app import navegador_zumbi

        fonte = inspect.getsource(navegador_zumbi._processos_do_windows)
        for nome in NAVEGADORES:
            assert nome in fonte
        assert "python.exe" not in fonte


class TestEncerrarNaoFazNadaSemMotivo:
    def test_sem_orfaos_nao_mexe_e_nao_fala(self, monkeypatch, tmp_path):
        monkeypatch.setattr("app.navegador_zumbi.donos_do_perfil", lambda _p: [])
        falas: list[str] = []
        assert encerrar_orfaos(tmp_path, on_log=lambda n, m: falas.append(m)) == 0
        assert falas == [], "silêncio quando não há o que fazer"

    def test_fora_do_windows_nao_faz_nada(self, monkeypatch, tmp_path):
        """No container o Chromium é descartado a cada execução."""
        monkeypatch.setattr(os, "name", "posix")
        chamou = []
        monkeypatch.setattr("app.navegador_zumbi.donos_do_perfil",
                            lambda _p: chamou.append(1) or [])
        assert encerrar_orfaos(tmp_path) == 0

    def test_encerra_e_conta_o_que_fez(self, monkeypatch, tmp_path):
        if os.name != "nt":
            pytest.skip("o caminho de encerramento é do Windows")
        monkeypatch.setattr("app.navegador_zumbi.donos_do_perfil",
                            lambda _p: [{"pid": 4321, "nome": "brave.exe", "cmd": "x"}])
        mortos: list[list[str]] = []

        class _Resultado:
            returncode = 0

        monkeypatch.setattr("subprocess.run",
                            lambda cmd, **k: mortos.append(cmd) or _Resultado())
        falas: list[str] = []
        assert encerrar_orfaos(tmp_path, on_log=lambda n, m: falas.append(m)) == 1
        assert mortos and "4321" in mortos[0]
        assert any("4321" in f for f in falas), "tem de dizer o que matou"

    def test_falha_ao_encerrar_nao_derruba_o_boot(self, monkeypatch, tmp_path):
        if os.name != "nt":
            pytest.skip("o caminho de encerramento é do Windows")
        monkeypatch.setattr("app.navegador_zumbi.donos_do_perfil",
                            lambda _p: [{"pid": 1, "nome": "brave.exe", "cmd": "x"}])
        monkeypatch.setattr("subprocess.run",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("negado")))
        falas: list[str] = []
        assert encerrar_orfaos(tmp_path, on_log=lambda n, m: falas.append(m)) == 0
        assert any("Gerenciador de Tarefas" in f for f in falas), (
            "se não conseguiu, tem de dizer o que a pessoa faz")

    def test_wmic_indisponivel_nao_derruba(self, monkeypatch, tmp_path):
        monkeypatch.setattr("subprocess.run",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("sem wmic")))
        assert donos_do_perfil(tmp_path) == []


class TestOBootLimpaAntesDeAbrir:
    def test_a_limpeza_vem_antes_do_playwright(self):
        """Depois de abrir não adianta: o erro já aconteceu."""
        import inspect

        from app.whatsapp import WhatsAppService

        fonte = inspect.getsource(WhatsAppService._launch)
        assert fonte.index("encerrar_orfaos") < fonte.index("sync_playwright"), (
            "a limpeza tem de acontecer antes de tentar abrir o navegador")

    def test_falha_na_limpeza_nao_impede_o_boot(self):
        import inspect

        from app.whatsapp import WhatsAppService

        fonte = inspect.getsource(WhatsAppService._launch)
        trecho = fonte[fonte.index("encerrar_orfaos"):]
        assert "except Exception" in trecho[:400], (
            "limpeza que estoura não pode virar bot que não sobe")


class TestNormalizacaoDeCaminho:
    @pytest.mark.parametrize("a,b", [
        (r"C:\bot\perfil", r'"C:\bot\perfil"'),
        (r"C:\bot\perfil", r"C:\bot\perfil\\"),
        (r"C:\bot\perfil", r"  C:\bot\perfil  "),
    ])
    def test_formas_diferentes_do_mesmo_caminho(self, a, b):
        assert _normalizar(a) == _normalizar(b)

    def test_caminhos_diferentes_continuam_diferentes(self):
        assert _normalizar(r"C:\bot\perfil") != _normalizar(r"C:\bot\perfil2")
