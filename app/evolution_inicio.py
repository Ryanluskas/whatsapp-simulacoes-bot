"""Liga a Evolution junto com o bot -- Windows + WSL.

Chamado pelo ``main.py`` numa thread, antes do ``manager.start()``: o painel
sobe na hora, e a Evolution entra quando estiver pronta. O que faz, e por que:

1. **Evolution fora do ar -> liga o WSL.** O Ubuntu do WSL desliga sozinho
   quando fica ocioso (``instanceIdleTimeout``, 15 s por padrao) e leva a
   Evolution junto. ``wsl -d <distro> --exec true`` o liga; o Docker sobe os
   containers sozinho (``restart: unless-stopped``).
2. **Espera a Evolution responder** (``GET /``, sem chave).
3. **Reaponta o webhook quando precisa.** A Evolution chama o bot por
   ``http://<IP do Windows visto pelo WSL>:<porta>/webhook/whatsapp``, e esse
   IP pode mudar quando o Windows reinicia. O IP vem da rota padrao do proprio
   WSL -- o mesmo caminho que o container usa. Com ``EVOLUTION_WEBHOOK_URL``
   no .env, usa esse valor e nao calcula. Tambem reaponta se o webhook estiver
   desligado, sem algum dos eventos, com ``byEvents`` ligado ou com outro token.
4. **Confere o repasse da porta** (``netsh interface portproxy``). Criar exige
   administrador: aqui so' se avisa, com o comando pronto.

Nunca levanta excecao, nunca envia mensagem, nunca escreve a chave ou o
token em log.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from typing import Callable

import httpx

from .evolution import EVENTOS_NECESSARIOS
from .evolution_diagnostico import VERSAO_ESPERADA
from .whatsapp_port import MODO_EVOLUTION

Log = Callable[[str, str], None]


def em_teste() -> bool:
    """Dentro do pytest? Entao nada de WSL nem Evolution de verdade.

    Esta maquina pode ter uma Evolution real escutando em localhost:8080. Um
    teste que subisse o ``main()`` com partida automatica religaria o WSL e
    reapontaria o webhook de producao com um token de teste.
    """
    return "PYTEST_CURRENT_TEST" in os.environ


def deve_ligar(config) -> bool:
    return bool(config.whatsapp_mode == MODO_EVOLUTION
                and getattr(config, "evolution_autostart", False)
                and config.evolution_api_key
                and not em_teste())


def _saida(rodar, args: list[str], timeout: float) -> str:
    """Roda um comando e devolve o stdout; "" se falhar por qualquer motivo."""
    try:
        r = rodar(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    bruto = r.stdout or b""
    texto = bruto.decode("utf-8", "replace") if isinstance(bruto, bytes) else str(bruto)
    return texto.replace("\x00", "")


def ip_do_windows_pelo_wsl(rodar, distro: str) -> str:
    """O IP que o WSL (e o container dentro dele) usa para chegar no Windows."""
    saida = _saida(rodar, ["wsl.exe", "-d", distro, "--", "ip", "route", "show", "default"], 30)
    achado = re.search(r"default via (\d{1,3}(?:\.\d{1,3}){3})", saida)
    return achado.group(1) if achado else ""


def repasse_existe(rodar, porta: int, destino: int) -> bool | None:
    """Ha' um ``portproxy`` da ``porta`` para ``127.0.0.1:destino``? None = nao deu para saber."""
    saida = _saida(rodar, ["netsh", "interface", "portproxy", "show", "v4tov4"], 15)
    if not saida.strip():
        return None
    for linha in saida.splitlines():
        campos = linha.split()
        if (len(campos) >= 4 and campos[1] == str(porta)
                and campos[2] in ("127.0.0.1", "localhost") and campos[3] == str(destino)):
            return True
    return False


def _versao(cliente: httpx.Client) -> str:
    try:
        r = cliente.get("/", timeout=3)
        dados = r.json() if r.status_code < 400 else {}
    except (httpx.HTTPError, ValueError):
        return ""
    versao = dados.get("version") if isinstance(dados, dict) else None
    return versao.strip()[:32] if isinstance(versao, str) else ""


def _webhook_certo(atual, url: str, token: str) -> bool:
    if isinstance(atual, dict) and isinstance(atual.get("webhook"), dict):
        atual = atual["webhook"]
    if not isinstance(atual, dict):
        return False
    eventos = {str(e).strip().upper() for e in (atual.get("events") or [])}
    cabecalhos = atual.get("headers") if isinstance(atual.get("headers"), dict) else {}
    return bool(atual.get("enabled")
                and atual.get("url") == url
                and set(EVENTOS_NECESSARIOS) <= eventos
                and atual.get("webhookByEvents") is not True
                and cabecalhos.get("X-Webhook-Token") == token)


def _url_de(atual) -> str:
    if isinstance(atual, dict) and isinstance(atual.get("webhook"), dict):
        atual = atual["webhook"]
    if not isinstance(atual, dict) or not atual.get("enabled"):
        return "desligado"
    return str(atual.get("url") or "sem URL")


def preparar_evolution(config, log: Log, *, rodar=subprocess.run,
                       cliente: httpx.Client | None = None, espera: float = 120.0,
                       intervalo: float = 2.0, dormir=time.sleep,
                       relogio=time.monotonic, plataforma: str = sys.platform) -> dict:
    """Deixa a Evolution no ar e o webhook apontando para este bot."""
    resultado = {"feito": False, "motivo": "", "wsl_ligado": False, "versao": "",
                 "webhook": "pulado", "repasse": None}
    try:
        return _preparar(config, log, resultado, rodar=rodar, cliente=cliente, espera=espera,
                         intervalo=intervalo, dormir=dormir, relogio=relogio,
                         plataforma=plataforma)
    except Exception as exc:  # noqa: BLE001 - a partida do bot nao pode cair por isto
        log("ERROR", f"Falha ao preparar a Evolution: {exc.__class__.__name__}. "
                     "O bot segue; confira a aba Status.")
        resultado["motivo"] = f"excecao: {exc.__class__.__name__}"
        return resultado


def _preparar(config, log: Log, resultado: dict, *, rodar, cliente, espera, intervalo,
              dormir, relogio, plataforma) -> dict:
    if plataforma != "win32" or not (config.evolution_wsl_distro or "").strip():
        # Em container (docker-compose) a Evolution e o bot ja' sobem juntos,
        # e o IP do WSL nao existe: nada para fazer aqui.
        resultado["motivo"] = "partida automatica so' no Windows com WSL"
        return resultado
    distro = config.evolution_wsl_distro.strip()
    proprio = cliente is None
    cliente = cliente or httpx.Client(base_url=config.evolution_url,
                                      headers={"apikey": config.evolution_api_key},
                                      timeout=httpx.Timeout(10.0, connect=3.0))
    try:
        inicio = relogio()
        versao = _versao(cliente)
        if not versao:
            log("INFO", f"Evolution fora do ar: ligando o WSL ({distro}) para ela subir.")
            _saida(rodar, ["wsl.exe", "-d", distro, "--exec", "true"], 90)
            resultado["wsl_ligado"] = True
            while not versao and relogio() - inicio < espera:
                dormir(intervalo)
                versao = _versao(cliente)
        if not versao:
            log("ERROR", f"A Evolution não respondeu em {espera:.0f} s. Confira no PowerShell: "
                         f"wsl -d {distro} -- docker ps")
            resultado["motivo"] = "evolution nao respondeu"
            return resultado
        resultado["versao"] = versao
        log("INFO", f"Evolution no ar (versão {versao})"
                    + (f" depois de {relogio() - inicio:.0f} s." if resultado["wsl_ligado"] else "."))
        if versao != VERSAO_ESPERADA:
            log("WARNING", f"A Evolution diz ser {versao}; o bot foi conferido contra "
                           f"{VERSAO_ESPERADA}. Formatos podem ser outros.")

        porta = int(config.evolution_webhook_porta)
        resultado["repasse"] = repasse_existe(rodar, porta, int(config.web_port))
        if resultado["repasse"] is False:
            log("ERROR", f"Não existe o repasse da porta {porta} para o bot: a Evolution não "
                         "consegue entregar os pedidos. Num PowerShell de ADMINISTRADOR, rode:\n"
                         f"netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 "
                         f"listenport={porta} connectaddress=127.0.0.1 "
                         f"connectport={config.web_port}\n"
                         f"New-NetFirewallRule -DisplayName \"Allana Webhook Evolution (WSL)\" "
                         f"-Direction Inbound -LocalPort {porta} -Action Allow -Protocol TCP")

        url = (config.evolution_webhook_url or "").strip()
        if not url:
            ip = ip_do_windows_pelo_wsl(rodar, distro)
            if not ip:
                log("WARNING", "Não consegui ler o IP do Windows pelo WSL; o webhook ficou "
                               "como estava.")
                resultado["motivo"] = "ip do wsl desconhecido"
                return resultado
            url = f"http://{ip}:{porta}/webhook/whatsapp"

        instancia = config.evolution_instance
        token = config.evolution_webhook_token
        atual = cliente.get(f"/webhook/find/{instancia}")
        atual = atual.json() if atual.status_code < 400 and (atual.text or "").strip() else None
        if _webhook_certo(atual, url, token):
            log("INFO", "Webhook da Evolution já aponta para o bot.")
            resultado.update(webhook="ok", feito=True)
            return resultado

        antes = _url_de(atual)
        corpo = {"webhook": {"enabled": True, "url": url, "byEvents": False, "base64": False,
                             "headers": {"X-Webhook-Token": token},
                             "events": list(EVENTOS_NECESSARIOS)}}
        cliente.post(f"/webhook/set/{instancia}", json=corpo)
        conferido = cliente.get(f"/webhook/find/{instancia}")
        conferido = conferido.json() if conferido.status_code < 400 else None
        if _webhook_certo(conferido, url, token):
            log("INFO", f"Webhook da Evolution reapontado para {url} (antes: {antes}).")
            resultado.update(webhook="reapontado", feito=True)
        else:
            log("ERROR", f"Não consegui apontar o webhook da Evolution para {url}. "
                         "Rode: python -m app.evolution_diagnostico")
            resultado["webhook"] = "falhou"
        return resultado
    finally:
        if proprio:
            cliente.close()
