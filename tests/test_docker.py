"""Coerência da configuração do container.

Não há Docker nesta máquina, então estes testes não constroem a imagem. Eles
cobrem a classe de erro que só apareceria dentro do container e que é chata de
descobrir lá: caminho do Windows vazando para o Linux, versão de Playwright
que não casa com a imagem base, segredo faltando, `.dockerignore` deixando o
`.env` entrar na imagem.

São checagens de arquivo e de configuração — baratas, e pegam justamente o que
quebraria no primeiro `docker compose up`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.config import load_config

RAIZ = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((RAIZ / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICO = COMPOSE["services"]["allana"]
AMBIENTE = SERVICO["environment"]
DOCKERFILE = (RAIZ / "Dockerfile").read_text(encoding="utf-8")

CAMINHO_WINDOWS = re.compile(r"^[A-Za-z]:[\\/]")


class TestCaminhosDoWindows:
    """O .env é a configuração do Windows; o compose o injeta inteiro."""

    def test_environment_nao_tem_caminho_windows(self):
        vazando = {
            chave: valor for chave, valor in AMBIENTE.items()
            if isinstance(valor, str) and CAMINHO_WINDOWS.match(valor)
        }
        assert not vazando, f"caminho do Windows dentro do container: {vazando}"

    @pytest.mark.parametrize("chave", [
        "SIM_BOT_PATH",            # C:\...\arqueiro no host
        "SIMULATOR_PROFILE_DIR",   # C:\...\Brave-Browser\User Data no host
        "WHATSAPP_PROFILE_DIR",
        "DB_PATH",
        "STATE_PATH",
        "BROWSER_EXECUTABLE",
    ])
    def test_toda_chave_de_caminho_e_sobrescrita(self, chave):
        """Sem override, o valor do .env do Windows chega ao Linux."""
        assert chave in AMBIENTE, (
            f"{chave} não é sobrescrita no compose — o valor do .env do Windows "
            "vazaria para dentro do container")

    def test_caminhos_do_container_sao_absolutos_e_linux(self):
        for chave in ("DB_PATH", "STATE_PATH", "WHATSAPP_PROFILE_DIR"):
            valor = AMBIENTE[chave]
            assert valor.startswith("/app/"), f"{chave}={valor} não está sob /app"


class TestVersaoDoPlaywright:
    """Playwright novo + imagem antiga = navegador em caminho inexistente."""

    def _versao_da_imagem(self) -> str:
        m = re.search(r"FROM\s+mcr\.microsoft\.com/playwright/python:v([\d.]+)", DOCKERFILE)
        assert m, "imagem base do Playwright não encontrada no Dockerfile"
        return m.group(1)

    def _versao_do_requirements(self) -> str:
        texto = (RAIZ / "requirements.txt").read_text(encoding="utf-8")
        m = re.search(r"^playwright==([\d.]+)", texto, re.M)
        assert m, "playwright precisa estar PRESO (==) por causa da imagem do Docker"
        return m.group(1)

    def test_requirements_casa_com_a_imagem_base(self):
        assert self._versao_do_requirements() == self._versao_da_imagem(), (
            "a versão do playwright no requirements.txt não casa com a tag da "
            "imagem base; o container subiria com "
            "'Executable doesn't exist at /ms-playwright/chromium-XXXX'")

    def test_build_falha_alto_se_divergir(self):
        """A checagem tem de estar no Dockerfile, não só na nossa cabeça."""
        assert "importlib.metadata" in DOCKERFILE and "nao casa com a imagem base" in DOCKERFILE

    def test_versao_instalada_aqui_e_a_mesma(self):
        from importlib.metadata import version
        assert version("playwright") == self._versao_do_requirements()


class TestSegredosForaDaImagem:
    def test_dockerignore_bloqueia_o_essencial(self):
        ignorados = set(
            (RAIZ / ".dockerignore").read_text(encoding="utf-8").splitlines()
        )
        for alvo in (".env", "*.db", ".venv/", ".whatsapp-profile/",
                     "comprovantes/", ".simulator-profile*/"):
            assert alvo in ignorados, f"{alvo} entraria na imagem"

    def test_imagem_nao_copia_o_env(self):
        assert not re.search(r"^COPY\s+.*\.env", DOCKERFILE, re.M)

    def test_segredos_chegam_por_env_file(self):
        assert ".env" in SERVICO["env_file"]


class TestConfiguracaoResultante:
    """O que o app enxerga quando roda com o ambiente do container."""

    @pytest.fixture()
    def config_do_container(self, monkeypatch, tmp_path):
        # load_config usa override=False, então o ambiente daqui vence o .env
        for chave, valor in AMBIENTE.items():
            monkeypatch.setenv(chave, str(valor).replace(
                "${TIMEZONE:-America/Sao_Paulo}", "America/Sao_Paulo"))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "teste.db"))
        monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
        monkeypatch.setenv("AGENT_TOKEN", "token-do-teste")
        return load_config()

    def test_sobe_em_modo_remoto(self, config_do_container):
        assert config_do_container.simulator_mode == "remote"

    def test_escuta_em_todas_as_interfaces(self, config_do_container):
        """127.0.0.1 dentro do container não é alcançável de fora."""
        assert config_do_container.web_host == "0.0.0.0"

    def test_whatsapp_headless(self, config_do_container):
        assert config_do_container.whatsapp_headless is True

    def test_no_linux_cai_no_chromium_do_playwright(self, config_do_container, monkeypatch):
        """Dentro do container não há Brave: o navegador tem de ser o da imagem.

        Este teste roda no Windows, onde o Brave existe de verdade — por isso
        finge o sistema operacional. Sem a guarda por plataforma, o resultado
        dependeria de o caminho `C:\\Program Files` simplesmente não existir no
        Linux, que é acerto por acaso, não por decisão.
        """
        monkeypatch.setattr("app.config.os.name", "posix")
        assert config_do_container.browser_path == ""

    def test_no_windows_encontra_o_brave(self, config_do_container):
        """E no host o autodetect continua funcionando."""
        import os as _os

        if _os.name == "nt" and Path(
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
        ).exists():
            assert config_do_container.browser_path.endswith("brave.exe")
        else:
            pytest.skip("sem Brave nesta máquina")

    def test_manager_cria_simulador_remoto(self, config_do_container, tmp_path):
        from app.db import Database
        from app.events import EventHub
        from app.manager import BotManager
        from app.remote import RemoteSimulator

        db = Database(config_do_container.db_path)
        manager = BotManager(config_do_container, db, EventHub(db))
        assert all(isinstance(s, RemoteSimulator) for s in manager.simulators)


class TestComposeSanidade:
    def test_shm_grande_para_o_chromium(self):
        """Os 64 MB padrão de /dev/shm derrubam a aba do WhatsApp sob carga."""
        assert SERVICO.get("shm_size") in ("1gb", "1g", "1024m")

    def test_dados_em_volume_nomeado(self):
        montagens = " ".join(SERVICO["volumes"])
        for caminho in ("/app/dados", "/app/comprovantes", "/app/.whatsapp-profile"):
            assert caminho in montagens, f"{caminho} não é persistido — some no recreate"

    def test_healthcheck_usa_a_rota_real(self):
        teste = " ".join(SERVICO["healthcheck"]["test"])
        assert "/api/health" in teste

    def test_reinicia_sozinho(self):
        assert SERVICO.get("restart") in ("unless-stopped", "always")


class TestGuardaDeExposicao:
    """O container publica a porta na rede. Senha padrão ali é painel aberto.

    Estes testes chamam `main()` de verdade: a guarda roda antes de qualquer
    serviço subir, então o retorno acontece sem abrir banco nem navegador.
    """

    def _env(self, tmp_path, **valores) -> str:
        base = {
            "WEB_HOST": "0.0.0.0",
            "WEB_PORT": "8899",
            "DASHBOARD_PASSWORD": "admin",
            "SESSION_SECRET": "segredo-de-teste-bem-longo-mesmo",
            "SIMULATOR_MODE": "remote",
            "AGENT_TOKEN": "token-de-agente-bem-longo",
            "SIM_BOT_PATH": str(tmp_path / "sem-bot"),
            "DB_PATH": str(tmp_path / "t.db"),
            "STATE_PATH": str(tmp_path / "s.json"),
        }
        base.update(valores)
        caminho = tmp_path / "guarda.env"
        caminho.write_text("\n".join(f"{k}={v}" for k, v in base.items()), encoding="utf-8")
        return str(caminho)

    def test_recusa_exposto_com_senha_padrao(self, tmp_path, monkeypatch):
        import main as entrada

        for chave in ("WEB_HOST", "DASHBOARD_PASSWORD", "SIMULATOR_MODE"):
            monkeypatch.delenv(chave, raising=False)
        assert entrada.main(["--env", self._env(tmp_path)]) == 2

    @pytest.mark.parametrize("senha", ["", "senha", "123456"])
    def test_recusa_outras_senhas_obvias(self, tmp_path, monkeypatch, senha):
        import main as entrada

        for chave in ("WEB_HOST", "DASHBOARD_PASSWORD", "SIMULATOR_MODE"):
            monkeypatch.delenv(chave, raising=False)
        assert entrada.main(
            ["--env", self._env(tmp_path, DASHBOARD_PASSWORD=senha)]) == 2

    @pytest.mark.parametrize("chave,valor", [
        ("SESSION_SECRET", ""),                    # sorteado a cada partida: sessões caem
        ("SESSION_SECRET", "troque-por-um-valor-longo-e-aleatorio"),   # placeholder: forjável
        ("AGENT_TOKEN", "x"),                      # token curto no modo remoto
    ])
    def test_recusa_exposto_com_segredo_fraco(self, tmp_path, monkeypatch, chave, valor):
        """Exposto na rede, segredo fraco é recusa de partida -- não aviso."""
        import main as entrada

        for nome in ("WEB_HOST", "DASHBOARD_PASSWORD", "SIMULATOR_MODE",
                     "SESSION_SECRET", "AGENT_TOKEN"):
            monkeypatch.delenv(nome, raising=False)
        env = self._env(tmp_path, DASHBOARD_PASSWORD="uma-senha-de-verdade", **{chave: valor})
        assert entrada.main(["--env", env]) == 2

    def test_em_localhost_os_defaults_passam_com_aviso(self, tmp_path, monkeypatch):
        """Desenvolvimento não pode virar cerimônia: em 127.0.0.1 sobe."""
        import main as entrada

        for nome in ("WEB_HOST", "DASHBOARD_PASSWORD", "SIMULATOR_MODE",
                     "SESSION_SECRET", "AGENT_TOKEN"):
            monkeypatch.delenv(nome, raising=False)
        monkeypatch.setattr(entrada.uvicorn, "run", lambda *a, **k: None)
        env = self._env(tmp_path, WEB_HOST="127.0.0.1", DASHBOARD_PASSWORD="admin",
                        SESSION_SECRET="", AGENT_TOKEN="x")
        assert entrada.main(["--env", env]) == 0

    def test_com_senha_propria_a_guarda_libera(self, tmp_path, monkeypatch):
        """Não pode barrar quem configurou direito — só chega a subir o servidor."""
        import main as entrada

        for chave in ("WEB_HOST", "DASHBOARD_PASSWORD", "SIMULATOR_MODE",
                      "SESSION_SECRET", "AGENT_TOKEN"):
            monkeypatch.delenv(chave, raising=False)

        # Corta em uvicorn.run: interessa saber que a guarda deixou passar.
        chamou = {"sim": False}

        def falso_run(*_a, **_k):
            chamou["sim"] = True

        monkeypatch.setattr(entrada.uvicorn, "run", falso_run)
        codigo = entrada.main(
            ["--env", self._env(tmp_path, DASHBOARD_PASSWORD="uma-senha-de-verdade")])
        assert codigo == 0
        assert chamou["sim"], "a guarda barrou uma configuração válida"


class TestTextoDoSessionSecret:
    """Cada caso de SESSION_SECRET fraco diz o risco que ele TEM.

    O texto antigo dizia "qualquer um pode forjar um cookie" também para o
    vazio -- e vazio não é forjável: o segredo é sorteado a cada partida. O
    custo real ali é outro (todo mundo deslogado a cada reinício).
    """

    def test_vazio_nao_fala_em_forjar(self):
        from app.security import problema_do_session_secret

        texto = problema_do_session_secret("")
        assert "sorteado a cada partida" in texto
        assert "deslogado" in texto
        assert "Não dá para forjar" in texto

    def test_placeholder_e_forjavel(self):
        from app.security import problema_do_session_secret

        texto = problema_do_session_secret("troque-por-um-valor-longo-e-aleatorio")
        assert "placeholder" in texto and "cookie de sessão válido" in texto

    def test_curto_e_forca_bruta(self):
        from app.security import problema_do_session_secret

        texto = problema_do_session_secret("curtinho")
        assert "força bruta" in texto and "8 caracteres" in texto

    def test_segredo_bom_nao_reclama(self):
        from app.security import problema_do_session_secret

        assert problema_do_session_secret("um-segredo-longo-e-aleatorio-de-verdade") == ""

    def test_vazio_realmente_nao_e_forjavel(self):
        """A afirmação do texto, verificada: sem segredo, um cookie assinado
        por outro processo (ou com a chave vazia) não vale."""
        import base64
        import hashlib
        import hmac

        from app.security import SessionManager

        corpo = "admin.9999999999.abcd"
        forjado = base64.urlsafe_b64encode(
            hmac.new(b"", corpo.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        assert SessionManager("").verify(f"{corpo}.{forjado}") is None
        assert SessionManager("").verify(SessionManager("").issue()) is None


class TestServicoDaEvolution:
    """A camada nova de WhatsApp, quando ligada por perfil.

    Ela não sobe no `docker compose up` normal de propósito: enquanto o modo
    `dom` for o que está provado, subir a Evolution junto só gastaria memória
    e consumiria um slot de dispositivo vinculado no WhatsApp do operador.
    """

    EVOLUTION = COMPOSE["services"]["evolution"]
    BANCO = COMPOSE["services"]["evolution-db"]

    def test_a_tag_da_imagem_e_fixa(self):
        """`latest` traria mudança de variável de ambiente sem aviso.

        A Evolution muda nomes de variáveis entre releases; uma atualização
        silenciosa derrubaria a instância no meio do expediente.
        """
        imagem = self.EVOLUTION["image"]
        assert ":" in imagem.rsplit("/", 1)[-1], "imagem sem tag"
        assert not imagem.endswith(":latest")

    def test_nao_sobe_por_padrao(self):
        assert "evolution" in self.EVOLUTION.get("profiles", [])
        assert "evolution" in self.BANCO.get("profiles", [])

    def test_a_sessao_sobrevive_ao_recreate(self):
        """Sem volume, cada `up` pediria o QR de novo."""
        montagens = [v.split(":")[1] for v in self.EVOLUTION.get("volumes", [])]
        assert "/evolution/instances" in montagens

    def test_a_chave_vem_do_env_e_nao_esta_escrita(self):
        chave = self.EVOLUTION["environment"]["AUTHENTICATION_API_KEY"]
        assert chave == "${EVOLUTION_API_KEY}", "segredo não pode ficar no compose"

    def test_espera_o_banco_ficar_pronto(self):
        """A Evolution falha ao subir se o Postgres ainda não aceita conexão."""
        assert self.EVOLUTION["depends_on"]["evolution-db"]["condition"] == "service_healthy"
        assert "pg_isready" in str(self.BANCO["healthcheck"]["test"])

    def test_webhook_global_desligado(self):
        """O webhook é por instância; o global valeria para todas."""
        assert self.EVOLUTION["environment"]["WEBHOOK_GLOBAL_ENABLED"] == "false"


class TestChavesNovasDocumentadas:
    ENV_EXEMPLO = (RAIZ / ".env.example").read_text(encoding="utf-8")

    @pytest.mark.parametrize("chave", [
        "WHATSAPP_MODE", "EVOLUTION_URL", "EVOLUTION_API_KEY",
        "EVOLUTION_INSTANCE", "EVOLUTION_GROUP_JID", "EVOLUTION_WEBHOOK_TOKEN",
    ])
    def test_esta_no_env_exemplo(self, chave):
        assert f"{chave}=" in self.ENV_EXEMPLO

    @pytest.mark.parametrize("segredo", ["EVOLUTION_API_KEY", "EVOLUTION_WEBHOOK_TOKEN"])
    def test_segredo_vem_vazio_no_exemplo(self, segredo):
        """Um valor de exemplo viraria o valor de produção de alguém."""
        linha = next(l for l in self.ENV_EXEMPLO.splitlines() if l.startswith(f"{segredo}="))
        assert linha == f"{segredo}=", f"{segredo} tem valor no .env.example"

    def test_o_padrao_continua_sendo_dom(self):
        """A camada nova só entra quando pedida, nunca por descuido."""
        assert "WHATSAPP_MODE=dom" in self.ENV_EXEMPLO

    def test_o_env_esta_no_gitignore(self):
        assert ".env" in (RAIZ / ".gitignore").read_text(encoding="utf-8")
