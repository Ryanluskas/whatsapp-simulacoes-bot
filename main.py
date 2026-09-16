"""Ponto de entrada do sistema de simulacoes.

Sobe, na ordem: banco -> barramento de eventos -> manager (WhatsApp, fila e
simulador) -> servidor do painel. O manager e' encerrado sempre, inclusive
quando o uvicorn cai por Ctrl+C.
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime
import logging
import signal
import sys
import threading
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from app.config import ROOT, load_config
from app.db import Database
from app.events import EventHub
from app.instancia import InstanciaEmUso, TravaDeInstancia, explicar
from app.manager import BotManager
from app.security import esta_exposto, problemas_de_seguranca
from app.web import create_app

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging() -> None:
    """Instala handlers antes de qualquer import do bot do Arqueiro.

    O ``bot.py`` chama ``logging.basicConfig`` no topo do modulo; com o logger
    raiz ja' configurado aqui, aquela chamada vira no-op e nao sequestra o log
    deste projeto.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(LOG_FORMAT, "%H:%M:%S"))
    root.addHandler(console)

    log_dir = ROOT / "logs"
    try:
        log_dir.mkdir(exist_ok=True)
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            log_dir / "sistema.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, "%Y-%m-%d %H:%M:%S"))
        root.addHandler(file_handler)
    except OSError:
        pass

    for noisy in ("uvicorn.access", "engineio.server", "socketio.server", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def versao_do_codigo() -> str:
    """Impressao digital do codigo carregado: hash do CONTEUDO.

    Python le' cada modulo UMA vez, no import. Editar um arquivo com o
    sistema no ar nao muda nada ate' reiniciar -- e mais de uma sessao de
    depuracao foi gasta testando uma versao antiga sem saber. Com esta linha
    no log, a duvida "o conserto entrou?" se responde olhando o proprio log.

    Era a data do arquivo mais recente, e isso falhou: o relogio desta
    maquina andou para tras entre duas sessoes, e a data passou a apontar
    para o passado enquanto o codigo era novo. Comparar datas virou palpite.
    Hash de conteudo nao depende de relogio -- ou bate, ou nao bate.

    Confira com:  .venv/Scripts/python.exe -c "import main; print(main.versao_do_codigo())"
    Se o valor for igual ao da ultima linha "Código carregado" do log, o bot
    esta' rodando exatamente este codigo.
    """
    try:
        arquivos = sorted([*ROOT.glob("app/*.py"), ROOT / "main.py"])
        digestor = hashlib.sha256()
        for arquivo in arquivos:
            digestor.update(arquivo.read_bytes())
        impressao = digestor.hexdigest()[:8]

        recente = max(arquivos, key=lambda f: f.stat().st_mtime)
        quando = datetime.fromtimestamp(recente.stat().st_mtime)
        return f"{impressao} ({len(arquivos)} arquivos, último {quando:%d/%m %H:%M})"
    except (OSError, ValueError):
        return "desconhecida"


def banner(config) -> None:
    url = f"http://{'localhost' if config.web_host in {'0.0.0.0', '127.0.0.1'} else config.web_host}:{config.web_port}"
    group = config.whatsapp_group_name or "(nenhum — abra a conversa manualmente)"
    print("", flush=True)
    print("=" * 52, flush=True)
    print("        A L L A N A  -  CENTRAL DE SIMULACOES", flush=True)
    print("=" * 52, flush=True)
    print(f"  Painel .......: {url}", flush=True)
    print(f"  Grupo ........: {group}", flush=True)
    print(f"  Simulador ....: {config.sim_bot_path}", flush=True)
    print(f"  Workers ......: {config.worker_count}", flush=True)
    print(f"  Fuso .........: {config.timezone}", flush=True)
    print(f"  Codigo .......: {versao_do_codigo()}", flush=True)
    print("=" * 52, flush=True)
    print("", flush=True)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    log = logging.getLogger("main")

    parser = argparse.ArgumentParser(description="Sistema de simulações via WhatsApp")
    parser.add_argument("--env", default=None,
                        help="arquivo .env alternativo (útil para testes)")
    args = parser.parse_args(argv)

    if args.env:
        load_dotenv(args.env, override=True)
    config = load_config(args.env)

    # A regra, dita em voz alta no log: em localhost os defaults passam com
    # aviso (desenvolvimento); escutando na rede — que é como o container roda —
    # configuração fraca é RECUSA DE PARTIDA. O painel mostra CPF de cliente.
    problemas, avisos = problemas_de_seguranca(config)
    if esta_exposto(config.web_host):
        log.info("Painel EXPOSTO na rede (WEB_HOST=%s): exigindo senha e segredos fortes.",
                 config.web_host)
    else:
        log.info("Painel em %s (só esta máquina): defaults aceitos para desenvolvimento.",
                 config.web_host)
    for aviso in avisos:
        log.warning("%s (aceito porque o painel só escuta em %s)", aviso, config.web_host)
    if problemas:
        for problema in problemas:
            log.error("RECUSANDO INICIAR: %s", problema)
        log.error("Corrija o .env e suba de novo. Gere segredos com: "
                  'python -c "import secrets; print(secrets.token_urlsafe(32))"')
        return 2
    if not Path(config.sim_bot_path, "bot.py").exists():
        log.warning(
            "bot.py não encontrado em %s. O painel sobe, mas as simulações vão falhar "
            "até SIM_BOT_PATH ser corrigido no .env.",
            config.sim_bot_path,
        )

    log.info("Código carregado: %s", versao_do_codigo())

    # Um processo por perfil do navegador. Antes desta trava chegamos a ter
    # DUAS instâncias no ar (uma na .venv, outra no Python global), as duas
    # no mesmo .whatsapp-profile: o Chromium aceita um processo por perfil, e
    # as duas brigavam pelo mesmo navegador. O sintoma parecia bug de código
    # -- aba em branco, sessão caindo, conversa fechando sozinha.
    #
    # Recusar subir e dizer o PID custa um minuto. Duas instâncias custaram
    # uma noite.
    trava = TravaDeInstancia(config.whatsapp_profile_dir, rotulo="bot (main.py)")
    try:
        trava.adquirir()
    except InstanciaEmUso as conflito:
        print(explicar(conflito, config.whatsapp_profile_dir), flush=True)
        log.error("Recusando iniciar: %s", conflito)
        return 1

    db = Database(config.db_path)
    hub = EventHub(db)
    manager = BotManager(config, db, hub)
    app = create_app(config, db, hub, manager)

    banner(config)

    shutting_down = threading.Event()

    def shutdown(*_args) -> None:
        if shutting_down.is_set():
            return
        shutting_down.set()
        log.info("Encerrando serviços...")
        manager.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: shutdown())
        except (ValueError, OSError):
            pass

    manager.start()
    try:
        uvicorn.run(
            app,
            host=config.web_host,
            port=config.web_port,
            log_level="info",
            access_log=False,
        )
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Servidor web encerrado com erro")
        return 1
    finally:
        shutdown()
        # Soltar o perfil explicitamente, alem do atexit: um encerramento por
        # sinal nao passa pelo atexit em todos os casos, e um lock orfao faria
        # o proximo boot recusar subir sem motivo.
        trava.liberar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
