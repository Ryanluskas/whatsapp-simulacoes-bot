"""Adaptador do bot de simulacao do projeto Arqueiro.

A logica de consulta **nao** e' reimplementada aqui. Este modulo carrega o
``bot.py`` original e chama exatamente as mesmas funcoes que o loop dele usa
(``preencher_formulario``, ``clicar_simular_consignado``, ``fechar_modais``,
``verificar_refinanciamento``, ``_calcular_reducao``, ``aguardar_relogin``...).

O que mudou em relacao ao adaptador anterior:

* O navegador vive numa thread dona (``ThreadActor``), como o do WhatsApp.
* A falta do ``bot.py`` deixou de derrubar o processo inteiro no boot. O painel
  sobe, mostra o problema e as solicitacoes falham com uma mensagem clara.
* Cada etapa publica progresso real (``consulting``, ``extracting``), em vez de
  um unico evento emitido antes de a consulta comecar.
* O ``bot.py`` do Arqueiro chama ``logging.basicConfig`` ao ser importado, o
  que sequestraria o logger raiz deste projeto. O import e' feito com um
  handler ja' instalado e o logger do modulo e' isolado depois.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .actor import ThreadActor
from .config import ROOT, Config
from . import motivos, tela_do_portal
from .models import SimulationJob, SimulationResult, Stage

# ``os.chdir`` e ``sys.path`` sao globais do processo: um unico import por vez.
_IMPORT_LOCK = threading.Lock()
_MODULE_CACHE: dict[str, Any] = {}

# Mensagens do portal que significam "consultado com sucesso, sem oferta" e nao
# "a automacao falhou". Sem essa distincao, um cliente sem produto disponivel
# era contabilizado como erro do sistema.
NO_OFFER_MARKERS = (
    "não disponível",
    "nao disponivel",
    "produto não disponível",
    "consórcio",
    "consorcio",
    "sem oferta",
)

RETRYABLE_MARKERS = (
    "timeout",
    "target page",
    "browser has been closed",
    "net::",
    "connection",
    "econnreset",
    "sessão santander expirada",
)


class SimulatorUnavailable(RuntimeError):
    pass


def load_bot_module(sim_bot_path: Path):
    """Carrega ``bot.py`` do Arqueiro sem alterar nada no projeto de origem."""
    key = str(sim_bot_path)
    with _IMPORT_LOCK:
        if key in _MODULE_CACHE:
            return _MODULE_CACHE[key]

        module_path = sim_bot_path / "bot.py"
        if not module_path.exists():
            raise SimulatorUnavailable(
                f"bot.py não encontrado em {sim_bot_path}. "
                "Ajuste SIM_BOT_PATH no arquivo .env."
            )

        # Um handler qualquer no logger raiz faz o logging.basicConfig() do
        # bot.py virar no-op, preservando a configuracao de log deste projeto.
        root = logging.getLogger()
        if not root.handlers:
            root.addHandler(logging.NullHandler())

        previous_cwd = os.getcwd()
        added_to_path = False
        if key not in sys.path:
            sys.path.insert(0, key)
            added_to_path = True
        # O bot le' credenciais.ini por caminho relativo; precisa do cwd certo.
        os.chdir(sim_bot_path)
        try:
            spec = importlib.util.spec_from_file_location("arqueiro_bot_original", module_path)
            if spec is None or spec.loader is None:
                raise SimulatorUnavailable(f"não foi possível carregar {module_path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules["arqueiro_bot_original"] = module
            spec.loader.exec_module(module)
        except SimulatorUnavailable:
            raise
        except Exception as exc:
            if added_to_path:
                sys.path.remove(key)
            raise SimulatorUnavailable(f"erro ao importar bot.py: {exc}") from exc
        finally:
            os.chdir(previous_cwd)

        _isolate_bot_logger(module, sim_bot_path)
        _MODULE_CACHE[key] = module
        return module


def _isolate_bot_logger(module, sim_bot_path: Path) -> None:
    """Mantem o log do Arqueiro no arquivo dele, fora do nosso."""
    try:
        bot_logger = logging.getLogger("arqueiro_bot_original")
        bot_logger.propagate = False
        if not bot_logger.handlers:
            handler = logging.FileHandler(sim_bot_path / "bot_log.txt", encoding="utf-8")
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
            )
            bot_logger.addHandler(handler)
        bot_logger.setLevel(logging.INFO)
        if hasattr(module, "logger"):
            module.logger = bot_logger
    except Exception:
        pass


class SimulatorService(ThreadActor):
    def __init__(
        self,
        config: Config,
        index: int = 0,
        on_log: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__(f"simulator-{index + 1}")
        self.config = config
        self.index = index
        self._on_log = on_log
        self._bot = None
        self._playwright = None
        self._context = None
        self._page = None
        self._ready = threading.Event()
        self._last_error = ""
        self._busy_since: float = 0.0

    # ------------------------------------------------------------------ estado
    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def busy(self) -> bool:
        return self._busy_since > 0

    def status(self) -> dict:
        return {
            "name": self.name,
            "running": self.running,
            "ready": self.ready,
            "busy": self.busy,
            "busy_seconds": round(time.monotonic() - self._busy_since, 1) if self.busy else 0.0,
            "last_error": self._last_error,
        }

    def _log(self, level: str, message: str) -> None:
        if self._on_log:
            try:
                self._on_log(level, message)
            except Exception:
                pass

    # ------------------------------------------------------------- ciclo dono
    def run(self) -> None:
        try:
            self._bot = load_bot_module(self.config.sim_bot_path)
            self._ready.set()
            self._last_error = ""
            self._log("INFO", f"Simulador carregado de {self.config.sim_bot_path}.")
        except SimulatorUnavailable as exc:
            self._last_error = str(exc)
            self._log("ERROR", str(exc))

        while not self.stopping:
            if not self._drain(0.5):
                break
        self._close_browser()

    # -------------------------------------------------------------- API publica
    def execute(
        self,
        job: SimulationJob,
        on_stage: Callable[[str], None] | None = None,
        timeout: float | None = None,
    ) -> SimulationResult:
        """Roda uma simulacao na thread dona e devolve o resultado."""
        limit = timeout or self.config.job_timeout_seconds
        return self.call(self._run_job, job, on_stage, timeout=limit + 20.0)

    def warm_up(self, timeout: float = 180.0) -> bool:
        return bool(self.call(self._ensure_browser, timeout=timeout))

    def shutdown_browser(self, timeout: float = 30.0) -> None:
        try:
            self.call(self._close_browser, timeout=timeout)
        except Exception:
            pass

    # ------------------------------------------------------------- navegador
    def _resolve_executable(self) -> str | None:
        # Resolução única em Config.browser_path: WhatsApp e simulador precisam
        # abrir o MESMO navegador que gerou os perfis copiados, senão as senhas
        # salvas não são lidas.
        return self.config.browser_path or None

    def _profile_dir(self) -> Path:
        """Diretório de perfil deste worker.

        Cada worker precisa do seu: o Chromium tranca o diretório e dois
        navegadores no mesmo perfil não sobem. Com WORKER_COUNT=1 (o padrão)
        usa-se o configurado — que pode ser o perfil real do Brave.

        Do segundo worker em diante NÃO derivamos um nome ao lado do perfil
        real: isso criaria pastas dentro do AppData do usuário e, pior, um
        perfil vazio sem as senhas salvas — falha silenciosa. Nesse caso o
        perfil extra vive na pasta do projeto.
        """
        base = self.config.simulator_profile_dir
        if self.index == 0:
            return base
        return ROOT / f".simulator-profile-{self.index + 1}"

    def _ensure_browser(self) -> bool:
        if self._context is not None and self._page is not None:
            return True
        if self._bot is None:
            raise SimulatorUnavailable(self._last_error or "simulador indisponível")

        profile = self._profile_dir()
        profile.mkdir(parents=True, exist_ok=True)

        self._playwright = sync_playwright().start()
        launch_kwargs: dict[str, Any] = dict(
            user_data_dir=str(profile),
            headless=False,
            ignore_default_args=["--enable-automation"],
            args=["--no-first-run", "--lang=pt-BR", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
            locale="pt-BR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Brave/136"
            ),
        )
        executable = self._resolve_executable()
        if executable:
            launch_kwargs["executable_path"] = executable

        try:
            self._context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:
            if not _perfil_ocupado(str(exc)):
                self._close_browser()
                raise
            # Um navegador de uma execucao morta ainda segura o perfil (o
            # Chromium aceita um processo por perfil). So' os que declaram
            # EXATAMENTE este perfil sao encerrados -- e nunca o perfil real
            # de um navegador instalado. Uma tentativa, nao um laco.
            from .navegador_zumbi import encerrar_orfaos

            if not encerrar_orfaos(profile, on_log=self._log):
                self._close_browser()
                raise
            try:
                self._context = self._playwright.chromium.launch_persistent_context(
                    **launch_kwargs)
            except Exception:
                # Sem isto o Playwright desta thread ficava iniciado, e o
                # proximo `sync_playwright().start()` na mesma thread falha
                # com "Sync API inside the asyncio loop".
                self._close_browser()
                raise
        try:
            self._context.grant_permissions(
                ["geolocation"], origin="https://www.parceirosantander.com.br"
            )
        except Exception:
            pass
        self._context.add_init_script(self._bot.STEALTH_JS)
        self._context.on("page", lambda page: _safe(self._bot._aplicar_stealth, page))
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        _safe(self._bot._aplicar_stealth, self._page)
        self._open_form()
        return True

    def _close_browser(self) -> None:
        for closer in (
            lambda: self._context.close() if self._context else None,
            lambda: self._playwright.stop() if self._playwright else None,
        ):
            try:
                closer()
            except Exception:
                pass
        self._context = None
        self._playwright = None
        self._page = None

    def _tentar(self, descricao: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Como ``_safe``, mas REGISTRA a falha em vez de engoli-la.

        O ``_safe`` mudo era o motivo de o painel anunciar "simulador pronto"
        com a aba parada em about:blank: se a navegacao ou o login falhassem,
        a excecao sumia e ninguem ficava sabendo. Onde a falha importa, usar
        este.
        """
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            self._log("WARNING", f"{descricao} falhou: {_short(exc)}")
            return None

    def _open_form(self) -> None:
        """Deixa o navegador na tela do formulario, ou diz por que nao conseguiu."""
        # Se o operador ja' esta' numa tela do portal, NAO navegar por cima.
        # Ele pode estar no meio de um login, e recarregar apaga o que ele
        # digitou. So' abrimos a landing quando a aba nao esta' no portal.
        url_atual = (self._tentar("Ler o endereço atual", lambda: self._page.url) or "").lower()
        if "parceirosantander" not in url_atual:
            self._tentar("Abrir a página do Santander", self._page.goto,
                         self._bot.URL_LANDING, wait_until="domcontentloaded",
                         timeout=20_000)
        else:
            self._log("INFO", "O portal já estava aberto; não recarreguei a página.")
        self._tentar("Login automático no Santander",
                     self._bot.tentar_login_automatico, self._page)

        found = self._tentar("Procurar a página do formulário",
                             self._bot._encontrar_pagina_formulario, self._context)
        if found is not None:
            self._page = found
            self._log("INFO", "Formulário do Santander pronto.")
            return

        # Insistir no formulario com a sessao caida so' devolve a tela de
        # login de novo -- e recarrega por cima de quem esta' digitando.
        if not self._tentar("Conferir a sessão", self._bot.sessao_expirada, self._page):
            self._tentar("Abrir o formulário direto", self._page.goto,
                         self._bot.URL_FORMULARIO, wait_until="domcontentloaded",
                         timeout=20_000)

        # Ultima palavra: confirmar onde o navegador realmente parou. Sem isto
        # o sistema anuncia "pronto" mesmo com a sessao caida, e a primeira
        # simulacao e' que descobre -- tarde, e para o consultor.
        url = (self._tentar("Ler o endereço atual", lambda: self._page.url) or "").lower()
        if not url or "about:blank" in url:
            self._log(
                "WARNING",
                "O navegador do simulador ficou em branco — não consegui abrir o "
                "portal do Santander. Confira a conexão e a janela do simulador.",
            )
        elif self._tentar("Conferir a sessão", self._bot.sessao_expirada, self._page):
            self._log(
                "WARNING",
                "O Santander está na tela de login: a sessão caiu. As simulações "
                "vão falhar até você entrar na janela do simulador — ou até o "
                "credenciais.ini do Arqueiro ter CPF e senha corretos.",
            )
        else:
            self._log("INFO", "Formulário do Santander pronto.")

    def _ensure_session(self) -> None:
        found = _safe(self._bot._encontrar_pagina_formulario, self._context)
        if found is not None:
            self._page = found
            return
        if _safe(self._bot.sessao_expirada, self._page):
            found = self._esperar_login_do_operador()
            if found is None:
                raise RuntimeError(
                    "Sessão Santander expirada. Faça login na janela do simulador — "
                    "não vou mexer na página enquanto isso."
                )
            self._page = found
            return
        _safe(self._page.goto, self._bot.URL_FORMULARIO, wait_until="domcontentloaded", timeout=20_000)
        time.sleep(1)

    # ------------------------------------------------------------------ job
    # Quanto esperar o operador logar, e de quanto em quanto conferir.
    _ESPERA_LOGIN_SEGUNDOS = 300.0
    _INTERVALO_DA_CONFERENCIA = 3.0

    def _esperar_login_do_operador(self):
        """Espera o login SEM tocar na pagina.

        O ``aguardar_relogin`` do Arqueiro recarrega a landing page a cada 5
        minutos e tenta o login automatico. Enquanto o operador esta' digitando
        CPF e senha, isso apaga o que ele escreveu -- foi o que tornou o login
        manual praticamente impossivel. Aqui a espera e' passiva: so' olha se
        a tela do formulario apareceu.

        O ``bot.py`` do Arqueiro nao foi alterado; apenas deixamos de chamar a
        funcao dele que navega.
        """
        self._log(
            "WARNING",
            "Sessão do Santander caiu. Faça login na janela do simulador — "
            "não vou recarregar a página enquanto você digita. "
            f"Aguardo por até {int(self._ESPERA_LOGIN_SEGUNDOS / 60)} minutos.",
        )
        limite = time.monotonic() + self._ESPERA_LOGIN_SEGUNDOS
        while time.monotonic() < limite:
            if self.stopping:
                return None
            found = _safe(self._bot._encontrar_pagina_formulario, self._context)
            if found is not None:
                self._log("INFO", "Login concluído. Retomando as simulações.")
                return found
            time.sleep(self._INTERVALO_DA_CONFERENCIA)
        return None

    def _run_job(self, job: SimulationJob, on_stage: Callable[[str], None] | None) -> SimulationResult:
        self._busy_since = time.monotonic()
        notify = on_stage or (lambda _stage: None)
        try:
            if self._bot is None:
                return SimulationResult(
                    job=job, ok=False, status="Indisponível",
                    error=self._last_error or "simulador indisponível", retryable=False,
                )

            try:
                self._ensure_browser()
            except Exception as exc:
                return SimulationResult(
                    job=job, ok=False, status="Erro",
                    error=f"falha ao abrir o navegador do simulador: {_short(exc)}",
                    retryable=True,
                )

            request = job.request
            ddd, celular = self._split_phone(request.phone)

            try:
                notify(Stage.CONSULTING)
                self._ensure_session()
                _safe(self._bot._keepalive, self._page)
                _safe(self._bot._fechar_abas_extras, self._page.context, self._page)
                self._bot.voltar_ao_formulario(self._page)
                self._bot.preencher_formulario(
                    self._page,
                    request.customer_name or request.consultant_name,
                    request.cpf,
                    ddd,
                    celular,
                )
                self._bot.clicar_simular_consignado(self._page)
                self._bot.fechar_modais(self._page)

                notify(Stage.EXTRACTING)
                status, _reducao, contratos, margem = self._bot.verificar_refinanciamento(self._page)
                # A FOTO VEM ANTES de voltar ao formulario: `voltar_ao_formulario`
                # navega, e a tela do resultado deixa de existir. Foi o mesmo
                # cuidado que ja' se toma com o texto do erro do portal.
                foto = self._fotografar_o_resultado(job)
                self._safe_return_to_form()
                return self._build_result(job, status, contratos, margem, portal_png=foto)

            except PlaywrightTimeout as exc:
                # Ler o banner ANTES de voltar ao formulario: a navegacao
                # apaga a mensagem, e ela e' justamente o que o consultor
                # precisa saber.
                detalhe, _ = self._ler_erro_do_portal()
                self._safe_return_to_form()
                return SimulationResult(
                    job=job, ok=False, status="Timeout",
                    error=detalhe or f"o portal não respondeu a tempo ({_short(exc)})",
                    retryable=True,
                )
            except PlaywrightError as exc:
                detalhe, _ = self._ler_erro_do_portal()
                self._recover_browser()
                return SimulationResult(
                    job=job, ok=False, status="Erro",
                    error=detalhe or _short(exc), retryable=True,
                )
            except Exception as exc:
                message = str(exc)
                lowered = message.lower()
                detalhe, repetir_portal = self._ler_erro_do_portal()
                self._safe_return_to_form()
                if any(marker in lowered for marker in NO_OFFER_MARKERS):
                    # Consulta concluida: o cliente simplesmente nao tem oferta.
                    return self._build_result(job, "Não", [], "")
                return SimulationResult(
                    job=job, ok=False, status="Erro", error=detalhe or _short(exc),
                    # O que o PORTAL disse manda: "não foi possível completar a
                    # operação" e' falha transitoria dele, mesmo quando a
                    # excecao do bot.py nao parece repetivel.
                    retryable=(repetir_portal
                               or any(marker in lowered for marker in RETRYABLE_MARKERS)),
                )
        finally:
            self._busy_since = 0.0

    def _fotografar_o_resultado(self, job: SimulationJob) -> str:
        """O print da tela do Santander. Caminho do PNG, ou "" .

        Acessorio de proposito: nunca levanta e nunca atrasa a resposta. Sem
        o print o consultor recebe o card montado por nos; sem a resposta ele
        nao recebe nada.

        A recusa e' registrada em INFO com o motivo. Ela e' esperada -- o
        recorte se descarta sozinho quando pega o topo da pagina, que traz a
        identificacao do operador e da empresa no portal do banco.
        """
        if self._page is None:
            return ""
        destino = ROOT / "comprovantes" / f"{job.request_id}_portal.png"
        caminho, motivo = tela_do_portal.capturar(self._page, destino)
        if motivo:
            self._log("INFO", f"{job.request_id}: sem print do portal — {motivo}")
        return caminho

    def _ler_motivos_do_portal(self) -> list[dict]:
        """Tudo que o portal disse, LITERAL, na ordem em que apareceu.

        Nada e' descartado por nao estar num mapa conhecido: o mapa
        classifica, nao filtra. Motivo desconhecido vai literal para o
        consultor e vira INFO no log -- e' assim que o mapa cresce com a
        realidade em vez de com suposicao.
        """
        if self._page is None:
            return []
        try:
            textos = self._page.evaluate(ERRO_PORTAL_JS) or []
        except Exception:
            return []

        achados = motivos.motivos_do_portal([str(x) for x in textos])
        if achados:
            self._log("INFO", "Portal informou: "
                              + " | ".join(m["texto"] for m in achados))
        novos = motivos.desconhecidos(achados)
        if novos:
            # O mapa nao conhece estas frases ainda. Registrar em INFO para
            # que a proxima versao as classifique -- sem nunca deixar de
            # entrega-las ao consultor.
            self._log("INFO", "Motivo(s) fora do mapa conhecido: "
                              + " | ".join(novos))
        return achados

    def _ler_erro_do_portal(self) -> tuple[str, bool]:
        """(mensagem do portal, vale repetir). Compatibilidade.

        Devolve o texto LITERAL do primeiro motivo, nao mais uma parafrase.
        Traduzir "NEGADO PELA POLITICA DE CREDITO" para "não foi possível
        contatar a averbadora" ficava mais bonito e dizia menos: o consultor
        conhece as frases do portal e sabe o que fazer com cada uma.
        """
        achados = self._ler_motivos_do_portal()
        if not achados:
            return "", False
        return achados[0]["texto"], motivos.vale_repetir(achados)

    def _build_result(self, job, status, contratos, margem,
                      portal_png: str = "") -> SimulationResult:
        contratos = list(contratos or [])
        # Ler os motivos SEMPRE, nao so' quando da' erro: uma recusa vem com
        # `ok=True` e `status="Nao"`, e e' justamente nela que o motivo e' a
        # informacao que o consultor precisa.
        achados = self._ler_motivos_do_portal() if not contratos else []
        reducao = self._bot._calcular_reducao(contratos) if contratos else 0.0
        if status == "Sim" and reducao <= 0:
            status = "Não"

        soma_parcelas = sum(
            self._bot._parse_br_float(str(c.get("valor_parcela", "0")).replace("R$", "").strip())
            for c in contratos
            if c.get("valor_parcela")
        )
        total_parcelas = sum(
            int(c["parcelas"]) for c in contratos if str(c.get("parcelas", "")).isdigit()
        )
        soma_saldo = sum(
            self._bot._parse_br_float(str(c.get("saldo_devedor", "0")).replace("R$", "").strip())
            for c in contratos
            if c.get("saldo_devedor")
        )

        return SimulationResult(
            job=job,
            ok=True,
            status=status,
            reduction_value=reducao,
            margin=margem or "",
            contracts=tuple(contratos),
            installment_sum=soma_parcelas,
            installment_count=total_parcelas,
            debt_sum=soma_saldo,
            motivos=tuple(achados),
            portal_png=portal_png,
        )

    def _safe_return_to_form(self) -> None:
        _safe(self._bot.voltar_ao_formulario, self._page)

    def _recover_browser(self) -> None:
        """A pagina morreu: derruba tudo para que o proximo job reabra limpo."""
        self._log("WARNING", f"{self.name}: navegador em estado inválido, reiniciando.")
        self._close_browser()

    def _split_phone(self, phone: str) -> tuple[str, str]:
        digits = "".join(ch for ch in (phone or "") if ch.isdigit())
        if len(digits) not in (10, 11):
            digits = self.config.default_phone
        return digits[:2], digits[2:]


# Le a mensagem de erro que o portal mostra na tela.
#
# O bot.py levanta excecoes genericas ("nao disponivel"), enquanto o portal
# exibe o motivo de verdade num banner: "ERRO NO CAM: 1106 - USUARIO NAO TEM
# ACESSO A OPERACAO", "nao foi possivel contatar a averbadora", "matricula
# invalida". Para o consultor essas coisas sao MUITO diferentes de "nao
# libera" -- uma ele resolve, a outra nao.
ERRO_PORTAL_JS = r"""
() => {
  const vistos = [];
  const limpar = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const guardar = (s) => {
    const t = limpar(s);
    if (t.length > 8 && t.length < 400 && !vistos.includes(t)) vistos.push(t);
  };

  document.querySelectorAll(
    '[role="alert"], [class*="error"], [class*="erro"], [class*="alert"], [class*="message"]'
  ).forEach((el) => guardar(el.innerText));

  const padrao = /mensagem do servi|erro no cam|averbadora|matr\u00edcula|matricula|n\u00e3o foi poss\u00edvel|nao foi possivel|indispon\u00edvel|sem acesso|n\u00e3o tem acesso/i;
  document.querySelectorAll('div, span, p, li').forEach((el) => {
    if (el.children.length > 3) return;
    const t = el.innerText || '';
    if (t.length < 300 && padrao.test(t)) guardar(t);
  });

  return vistos.slice(0, 6);
}
"""

# Traducoes do jargao do portal, com a decisao de repetir ou nao.
#
# A distincao importa: repetir "usuario sem acesso" ou "matricula invalida"
# so' gasta tempo e da' o mesmo resultado -- sao problemas de cadastro ou de
# permissao. Ja' "nao foi possivel completar a operacao" e' falha transitoria
# do portal, e a segunda tentativa costuma passar.
#
# (padrao, mensagem para o consultor, vale repetir)
_TRADUCOES_DE_ERRO = (
    (re.compile(r"n[ãa]o foi poss[íi]vel completar a opera", re.I),
     "O portal não completou a operação.", True),
    (re.compile(r"averbadora", re.I),
     "Não foi possível contatar a averbadora.", True),
    (re.compile(r"indispon[íi]vel|fora do ar|instabilidade|tente novamente", re.I),
     "O sistema do banco está indisponível no momento.", True),
    (re.compile(r"tempo|timeout", re.I),
     "O portal do banco não respondeu a tempo.", True),
    (re.compile(r"matr[ií]cula", re.I),
     "Matrícula inválida ou não encontrada.", False),
    (re.compile(r"n[ãa]o tem acesso|sem acesso|1106", re.I),
     "O usuário do portal não tem acesso a esta operação.", False),
    (re.compile(r"conv[êe]nio", re.I),
     "Problema com o convênio do cliente.", False),
    (re.compile(r"cpf.*(inv[áa]lid|n[ãa]o encontrad)", re.I),
     "CPF inválido ou não encontrado.", False),
)


# O texto so' e' tratado como motivo de falha se tiver marca de erro. O portal
# mostra muita coisa neutra ("Carregado", "Ver produtos") que nao explica nada.
_PARECE_ERRO = re.compile(
    r"erro|falha|inv[áa]lid|n[ãa]o foi poss[íi]vel|n[ãa]o conseguimos|"
    r"indispon[íi]vel|sem acesso|n[ãa]o tem acesso|negad|recusad|expirad|"
    r"tente novamente|mensagem do servi",
    re.I,
)


def traduzir_erro_do_portal(textos: list[str]) -> tuple[str, bool]:
    """Devolve (mensagem para o consultor, vale a pena repetir).

    Sem casar com nenhum padrao conhecido, devolve o proprio texto do portal
    e NAO repete: especifico e errado e' pior que cru, e repetir um erro que
    nao entendemos so' atrasa a resposta ao consultor.
    """
    for bruto in textos:
        for padrao, amigavel, repetir in _TRADUCOES_DE_ERRO:
            if padrao.search(bruto):
                return amigavel, repetir
    # Sem padrao conhecido, so' devolve o texto se ele PARECER um erro.
    #
    # Sem esta checagem qualquer coisa na tela virava "motivo da falha": o
    # portal exibe a palavra "Carregado" e o consultor recebeu
    # "não foi possível simular / Carregado", que nao explica nada e ainda
    # da' a impressao de que o sistema esta' confuso.
    for bruto in textos:
        if not _PARECE_ERRO.search(bruto):
            continue
        limpo = re.sub(r"^MENSAGEM DO SERVI[ÇC]O:\s*", "", bruto, flags=re.I).strip()
        if limpo:
            return limpo[:200], False
    return "", False


def _perfil_ocupado(mensagem: str) -> bool:
    """O Chromium recusou abrir porque outro processo segura o perfil?"""
    texto = (mensagem or "").lower()
    return any(sinal in texto for sinal in (
        "processsingleton", "user data directory is already in use",
        "profile appears to be in use", "target page, context or browser has been closed",
    ))


def _safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _short(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0][:240] if text else exc.__class__.__name__
