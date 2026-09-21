"""A Evolution sobe junto com o bot.

Nenhum teste aqui chama o ``wsl.exe``, o ``netsh`` ou a Evolution de verdade:
os comandos passam por um ``rodar`` falso e o HTTP por um ``MockTransport``.
Esta maquina tem uma Evolution real escutando em localhost:8080 -- um teste
que a tocasse poderia reapontar o webhook de producao com um token de teste.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import evolution_inicio
from app.evolution import EVENTOS_NECESSARIOS
from app.evolution_inicio import deve_ligar, preparar_evolution
from tests.test_concurrency import _config

CHAVE = "chave-da-evolution-que-nao-pode-ir-para-log-123"
TOKEN = "token-do-webhook-que-nao-pode-ir-para-log-456"
IP_NOVO, IP_VELHO = "192.168.208.1", "172.29.160.1"
URL_NOVA = f"http://{IP_NOVO}:8001/webhook/whatsapp"
URL_VELHA = f"http://{IP_VELHO}:8001/webhook/whatsapp"
NETSH_COM_REPASSE = ("\r\nEscuta em ipv4:             Conectar-se a ipv4:\r\n\r\n"
                     "Endereço        Porta       Endereço        Porta\r\n"
                     "--------------- ----------  --------------- ----------\r\n"
                     "0.0.0.0         8001        127.0.0.1       8000\r\n")


def _cfg(**mudancas):
    base = replace(_config(Path(tempfile.gettempdir()) / "evolution-inicio-teste"), whatsapp_mode="evolution",
                   evolution_api_key=CHAVE, evolution_webhook_token=TOKEN,
                   evolution_autostart=True, evolution_url="http://evolution:8080",
                   # O bot de producao escuta na 8000; e' para ela que o repasse aponta.
                   web_port=8000)
    return replace(base, **mudancas)


class Rodar:
    """``subprocess.run`` falso: responde por comando e guarda o que foi pedido."""

    def __init__(self, *, ip=IP_NOVO, netsh=NETSH_COM_REPASSE, ao_ligar=None):
        self.chamadas: list[list[str]] = []
        self.ip, self.netsh, self.ao_ligar = ip, netsh, ao_ligar

    def __call__(self, args, **kw):
        self.chamadas.append(list(args))
        saida = ""
        if args[:2] == ["netsh", "interface"]:
            saida = self.netsh
        elif "route" in args:
            saida = f"default via {self.ip} dev eth0 proto kernel\n" if self.ip else ""
        elif args[-2:] == ["--exec", "true"] and self.ao_ligar:
            self.ao_ligar()
        return SimpleNamespace(returncode=0, stdout=saida.encode("utf-8"))

    def ligou_o_wsl(self) -> bool:
        return any(c[-2:] == ["--exec", "true"] for c in self.chamadas)


class Evolution:
    """Evolution falsa: ``GET /``, ``webhook/find`` e ``webhook/set`` da v2.3.7."""

    def __init__(self, *, no_ar=True, versao="2.3.7", webhook=None):
        self.no_ar, self.versao = no_ar, versao
        self.webhook = webhook
        self.pedidos: list[tuple[str, str, dict | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        corpo = json.loads(request.content) if request.content else None
        self.pedidos.append((request.method, request.url.path, corpo))
        if not self.no_ar:
            raise httpx.ConnectError("sem rota")
        if request.url.path == "/":
            return httpx.Response(200, json={"status": 200, "version": self.versao})
        if request.url.path == "/webhook/find/allana":
            return httpx.Response(200, json=self.webhook)
        if request.url.path == "/webhook/set/allana":
            w = corpo["webhook"]
            self.webhook = {"enabled": w["enabled"], "url": w["url"], "events": w["events"],
                            "webhookByEvents": w["byEvents"], "headers": w["headers"]}
            return httpx.Response(201, json={"webhook": {"instanceName": "allana"}})
        return httpx.Response(404)

    def sets(self):
        return [c for m, r, c in self.pedidos if m == "POST"]


def webhook_certo(**mudancas):
    w = {"enabled": True, "url": URL_NOVA, "events": list(EVENTOS_NECESSARIOS),
         "webhookByEvents": False, "headers": {"X-Webhook-Token": TOKEN}}
    w.update(mudancas)
    return w


class Relogio:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def dormir(self, s):
        self.t += s


def rodar_preparo(cfg, evo: Evolution, rodar: Rodar, **kw):
    logs: list[tuple[str, str]] = []
    relogio = Relogio()
    cliente = httpx.Client(transport=httpx.MockTransport(evo), base_url=cfg.evolution_url)
    r = preparar_evolution(cfg, lambda n, m: logs.append((n, m)), rodar=rodar, cliente=cliente,
                           relogio=relogio, dormir=relogio.dormir, plataforma="win32", **kw)
    return r, logs


def texto(logs):
    return " | ".join(m for _, m in logs)


# ============================================================ quando liga
class TestQuandoLiga:
    def test_nunca_dentro_de_teste(self):
        """A trava que protege a Evolution real desta maquina."""
        assert evolution_inicio.em_teste() is True
        assert deve_ligar(_cfg()) is False

    def test_liga_com_tudo_configurado(self, monkeypatch):
        monkeypatch.setattr(evolution_inicio, "em_teste", lambda: False)
        assert deve_ligar(_cfg()) is True

    @pytest.mark.parametrize("mudanca", [
        {"whatsapp_mode": "dom"}, {"evolution_autostart": False}, {"evolution_api_key": ""},
    ])
    def test_nao_liga_sem_um_dos_requisitos(self, monkeypatch, mudanca):
        monkeypatch.setattr(evolution_inicio, "em_teste", lambda: False)
        assert deve_ligar(_cfg(**mudanca)) is False

    def test_config_montado_a_mao_nao_liga(self):
        """O padrao do dataclass e' desligado; so' o .env liga."""
        from app.config import Config
        assert Config.__dataclass_fields__["evolution_autostart"].default is False

    def test_load_config_liga_por_padrao(self, tmp_path):
        from app.config import load_config
        env = tmp_path / "t.env"
        env.write_text("WHATSAPP_MODE=evolution\n", encoding="utf-8")
        c = load_config(env)
        assert (c.evolution_autostart, c.evolution_wsl_distro, c.evolution_webhook_porta,
                c.evolution_webhook_url) == (True, "Ubuntu", 8001, "")

    def test_fora_do_windows_nao_faz_nada(self):
        evo, rodar = Evolution(), Rodar()
        logs: list = []
        r = preparar_evolution(_cfg(), lambda n, m: logs.append(m), rodar=rodar,
                               cliente=httpx.Client(transport=httpx.MockTransport(evo),
                                                    base_url="http://evolution:8080"),
                               plataforma="linux")
        assert rodar.chamadas == [] and evo.pedidos == [] and not r["feito"]


# ======================================================= subir a Evolution
class TestSubirAEvolution:
    def test_ja_no_ar_nao_liga_o_wsl(self):
        evo, rodar = Evolution(webhook=webhook_certo()), Rodar()
        r, logs = rodar_preparo(_cfg(), evo, rodar)
        assert not rodar.ligou_o_wsl() and r["versao"] == "2.3.7" and r["feito"]
        assert r["repasse"] is True, "o repasse 8001 -> 127.0.0.1:8000 existe no netsh falso"

    def test_fora_do_ar_liga_o_wsl_e_espera(self):
        evo = Evolution(no_ar=False, webhook=webhook_certo())
        rodar = Rodar(ao_ligar=lambda: setattr(evo, "no_ar", True))
        r, logs = rodar_preparo(_cfg(), evo, rodar)
        ligacoes = [c for c in rodar.chamadas if c[-2:] == ["--exec", "true"]]
        assert ligacoes == [["wsl.exe", "-d", "Ubuntu", "--exec", "true"]]
        assert r["wsl_ligado"] and r["versao"] == "2.3.7" and r["webhook"] == "ok"
        assert "ligando o WSL" in texto(logs) and "Evolution no ar" in texto(logs)

    def test_nao_sobe_desiste_com_aviso_e_nao_mexe_no_webhook(self):
        evo, rodar = Evolution(no_ar=False), Rodar()
        r, logs = rodar_preparo(_cfg(), evo, rodar, espera=30, intervalo=2)
        assert not r["feito"] and r["motivo"] == "evolution nao respondeu"
        assert any(n == "ERROR" and "docker ps" in m for n, m in logs)
        assert evo.sets() == []

    def test_outra_versao_avisa(self):
        evo, rodar = Evolution(versao="2.4.0", webhook=webhook_certo()), Rodar()
        _, logs = rodar_preparo(_cfg(), evo, rodar)
        assert any(n == "WARNING" and "2.4.0" in m and "2.3.7" in m for n, m in logs)


# ================================================================ webhook
class TestWebhook:
    def test_ip_mudou_reaponta_com_o_mesmo_token(self):
        """O caso do reinicio do Windows: o WSL ganhou outro IP."""
        evo, rodar = Evolution(webhook=webhook_certo(url=URL_VELHA)), Rodar()
        r, logs = rodar_preparo(_cfg(), evo, rodar)
        (corpo,) = evo.sets()
        w = corpo["webhook"]
        assert w["url"] == URL_NOVA and w["enabled"] is True and w["byEvents"] is False
        assert w["headers"] == {"X-Webhook-Token": TOKEN}
        assert set(w["events"]) == set(EVENTOS_NECESSARIOS)
        assert r["webhook"] == "reapontado"
        assert any(URL_NOVA in m and IP_VELHO in m for _, m in logs), logs

    @pytest.mark.parametrize("estrago", [
        {"enabled": False}, {"events": ["MESSAGES_UPSERT"]}, {"webhookByEvents": True},
        {"headers": {"X-Webhook-Token": "outro-token"}},
    ])
    def test_qualquer_desvio_e_corrigido(self, estrago):
        evo, rodar = Evolution(webhook=webhook_certo(**estrago)), Rodar()
        r, _ = rodar_preparo(_cfg(), evo, rodar)
        assert r["webhook"] == "reapontado" and len(evo.sets()) == 1

    def test_webhook_inexistente_e_criado(self):
        evo, rodar = Evolution(webhook=None), Rodar()
        r, logs = rodar_preparo(_cfg(), evo, rodar)
        assert r["webhook"] == "reapontado" and "antes: desligado" in texto(logs)

    def test_certo_nao_e_regravado(self):
        evo, rodar = Evolution(webhook=webhook_certo()), Rodar()
        r, logs = rodar_preparo(_cfg(), evo, rodar)
        assert evo.sets() == [] and r["webhook"] == "ok"
        assert "já aponta" in texto(logs)

    def test_url_fixa_no_env_nao_calcula_o_ip(self):
        fixa = "http://10.0.0.5:9000/webhook/whatsapp"
        evo, rodar = Evolution(webhook=webhook_certo(url=URL_VELHA)), Rodar()
        rodar_preparo(_cfg(evolution_webhook_url=fixa), evo, rodar)
        assert evo.sets()[0]["webhook"]["url"] == fixa
        assert not any("route" in c for c in rodar.chamadas)

    def test_sem_ip_do_wsl_deixa_como_estava(self):
        evo, rodar = Evolution(webhook=webhook_certo(url=URL_VELHA)), Rodar(ip="")
        r, logs = rodar_preparo(_cfg(), evo, rodar)
        assert evo.sets() == [] and r["motivo"] == "ip do wsl desconhecido"
        assert any(n == "WARNING" for n, _ in logs)

    def test_a_porta_do_repasse_vem_da_config(self):
        evo = Evolution(webhook=webhook_certo(url=f"http://{IP_NOVO}:8123/webhook/whatsapp"))
        rodar = Rodar(netsh=NETSH_COM_REPASSE.replace("8001", "8123"))
        r, _ = rodar_preparo(_cfg(evolution_webhook_porta=8123), evo, rodar)
        assert r["webhook"] == "ok" and r["repasse"] is True


# ================================================================ repasse
class TestRepasseDaPorta:
    def test_sem_repasse_avisa_com_o_comando(self):
        evo, rodar = Evolution(webhook=webhook_certo()), Rodar(netsh="\r\n\r\n  \r\n")
        r, logs = rodar_preparo(_cfg(), evo, rodar)
        assert r["repasse"] is None, "netsh vazio = nao deu para saber, nao 'nao existe'"
        rodar2 = Rodar(netsh="Escuta em ipv4:\r\n0.0.0.0  9999  127.0.0.1  8000\r\n")
        r2, logs2 = rodar_preparo(_cfg(), Evolution(webhook=webhook_certo()), rodar2)
        assert r2["repasse"] is False
        erro = next(m for n, m in logs2 if n == "ERROR")
        assert "netsh interface portproxy add v4tov4" in erro and "listenport=8001" in erro

    def test_repasse_para_outra_porta_do_bot_nao_conta(self):
        rodar = Rodar(netsh=NETSH_COM_REPASSE.replace("127.0.0.1       8000", "127.0.0.1       9000"))
        r, _ = rodar_preparo(_cfg(), Evolution(webhook=webhook_certo()), rodar)
        assert r["repasse"] is False


# ================================================ nunca derruba, nunca vaza
class TestNuncaDerrubaNuncaVaza:
    def test_excecao_vira_log_e_nao_sobe(self):
        def explode(args, **kw):
            raise RuntimeError("inesperado")
        evo = Evolution(no_ar=False)
        logs: list = []
        cliente = httpx.Client(transport=httpx.MockTransport(evo), base_url="http://evolution:8080")
        rel = Relogio()
        r = preparar_evolution(_cfg(), lambda n, m: logs.append((n, m)), rodar=explode,
                               cliente=cliente, relogio=rel, dormir=rel.dormir, plataforma="win32")
        assert r["motivo"].startswith("excecao") and logs[-1][0] == "ERROR"

    def test_comando_que_falha_vira_saida_vazia(self):
        def falha(args, **kw):
            raise subprocess.TimeoutExpired(args, 1)
        assert evolution_inicio.ip_do_windows_pelo_wsl(falha, "Ubuntu") == ""
        assert evolution_inicio.repasse_existe(falha, 8001, 8000) is None

    @pytest.mark.parametrize("webhook", [None, webhook_certo(url=URL_VELHA), webhook_certo()])
    def test_chave_e_token_nunca_no_log(self, webhook):
        evo, rodar = Evolution(webhook=webhook), Rodar()
        _, logs = rodar_preparo(_cfg(), evo, rodar)
        for _, m in logs:
            assert CHAVE not in m and TOKEN not in m


# ================================================================ main()
class TestOMainLigaAEvolution:
    def _env(self, tmp_path, monkeypatch, **extra) -> str:
        valores = {
            "WEB_HOST": "127.0.0.1", "WEB_PORT": "8897",
            "DASHBOARD_PASSWORD": "senha-de-teste-bem-forte",
            "SESSION_SECRET": "segredo-de-sessao-de-teste-bem-longo",
            "SIMULATOR_MODE": "remote", "AGENT_TOKEN": "token-de-agente-bem-longo",
            "SIM_BOT_PATH": str(tmp_path / "sem-bot"), "DB_PATH": str(tmp_path / "t.db"),
            "STATE_PATH": str(tmp_path / "s.json"),
            "WHATSAPP_PROFILE_DIR": str(tmp_path / "perfil"),
            "WHATSAPP_MODE": "evolution", "EVOLUTION_URL": "http://127.0.0.1:9",
            "EVOLUTION_API_KEY": CHAVE, "EVOLUTION_GROUP_JID": "120363000000000000@g.us",
            "EVOLUTION_WEBHOOK_TOKEN": TOKEN,
        }
        valores.update(extra)
        for chave, valor in valores.items():
            monkeypatch.setenv(chave, valor)   # desfeito no fim: o main() grava no os.environ
        caminho = tmp_path / "evolution.env"
        caminho.write_text("\n".join(f"{k}={v}" for k, v in valores.items()), encoding="utf-8")
        return str(caminho)

    def _rodar_main(self, tmp_path, monkeypatch, em_teste: bool, **extra):
        import main as entrada
        chamou = threading.Event()
        recebido: dict = {}

        def preparo(config, log):
            recebido.update(config=config, log=log)
            chamou.set()

        monkeypatch.setattr(evolution_inicio, "em_teste", lambda: em_teste)
        monkeypatch.setattr(evolution_inicio, "preparar_evolution", preparo)
        monkeypatch.setattr(entrada.BotManager, "start", lambda self: None)
        monkeypatch.setattr(entrada, "servir", lambda *a, **k: 0)
        assert entrada.main(["--env", self._env(tmp_path, monkeypatch, **extra)]) == 0
        return chamou.wait(timeout=5), recebido

    def test_o_bot_sobe_e_liga_a_evolution(self, tmp_path, monkeypatch):
        chamou, recebido = self._rodar_main(tmp_path, monkeypatch, em_teste=False)
        assert chamou, "o main() nao ligou a Evolution"
        assert recebido["config"].whatsapp_mode == "evolution" and callable(recebido["log"])

    def test_dentro_de_teste_o_main_nao_liga(self, tmp_path, monkeypatch):
        chamou, _ = self._rodar_main(tmp_path, monkeypatch, em_teste=True)
        assert not chamou

    def test_autostart_false_no_env_nao_liga(self, tmp_path, monkeypatch):
        chamou, _ = self._rodar_main(tmp_path, monkeypatch, em_teste=False,
                                     EVOLUTION_AUTOSTART="false")
        assert not chamou
