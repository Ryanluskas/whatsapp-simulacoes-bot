"""Encerra navegadores órfãos que ficaram segurando o perfil.

O que aconteceu
---------------
Em 01/09, 15:22, o WhatsApp caiu e o bot tentou reabrir::

    WARNING  WhatsApp desconectou.
    ERROR    Falha ao iniciar o WhatsApp: BrowserContext.new_page: Target
             page, context or browser has been closed -- provavelmente o
             perfil '.whatsapp-profile' já está aberto em outro navegador.

Um ``brave.exe`` da execução morta continuou vivo, segurando o
``user_data_dir``. O Chromium aceita **um** processo por perfil, então o bot
não conseguia mais subir -- e as 49 solicitações na fila ficaram esperando
alguém perceber.

A ``TravaDeInstancia`` não cobre isto: ela impede dois **bots**, e aqui o
problema é um **navegador** sem dono.

O cuidado
---------
Matar processo é irreversível, então o alvo é estreito de propósito:

* só ``brave.exe``/``chrome.exe``/``msedge.exe`` -- nunca outro programa;
* só os que declaram ``--user-data-dir`` apontando **exatamente** para o
  nosso perfil;
* nunca por nome, nunca por padrão amplo, nunca "todos os brave".

O navegador pessoal do operador não usa este perfil e não é tocado. Se o
caminho não bater ao pé da letra, o processo fica de pé.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: Os executáveis que o bot pode ter aberto. Fora desta lista, nada é tocado.
NAVEGADORES = ("brave.exe", "chrome.exe", "msedge.exe")


def _normalizar(caminho: str) -> str:
    """Compara caminhos sem tropeçar em barra, caixa ou aspas."""
    return str(Path(caminho.strip().strip('"')).resolve()).lower().rstrip("\\/")


def _processos_do_windows() -> list[dict]:
    """(pid, nome, linha de comando) de cada navegador vivo.

    Via WMIC porque ele já existe no Windows e não exige dependência nova.
    Uma falha aqui devolve lista vazia: não conseguir olhar não pode virar
    "não há nada", mas também não pode impedir o bot de subir -- quem chama
    trata os dois casos.
    """
    try:
        saida = subprocess.run(
            ["wmic", "process", "where",
             "(name='brave.exe' or name='chrome.exe' or name='msedge.exe')",
             "get", "ProcessId,Name,CommandLine", "/format:list"],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    processos: list[dict] = []
    atual: dict = {}
    for linha in saida.splitlines():
        linha = linha.strip()
        if not linha:
            if atual.get("ProcessId"):
                processos.append(atual)
            atual = {}
            continue
        if "=" in linha:
            chave, _, valor = linha.partition("=")
            atual[chave.strip()] = valor.strip()
    if atual.get("ProcessId"):
        processos.append(atual)
    return processos


def _processos_do_linux() -> list[dict]:
    """(pid, nome, linha de comando) de cada navegador vivo no Linux."""
    try:
        saida = subprocess.run(
            ["ps", "-eo", "pid,comm,args"],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    processos: list[dict] = []
    for linha in saida.splitlines()[1:]:
        partes = linha.strip().split(maxsplit=2)
        if len(partes) < 3:
            continue
        pid_str, comm, args = partes
        if comm in ("brave", "chrome", "msedge", "chromium", "chromium-browser", "google-chrome"):
            try:
                processos.append({
                    "ProcessId": int(pid_str),
                    "Name": comm,
                    "CommandLine": args
                })
            except ValueError:
                pass
    return processos


def donos_do_perfil(perfil: Path | str) -> list[dict]:
    """Navegadores vivos que declaram este perfil no ``--user-data-dir``.

    Só conta a igualdade EXATA do caminho já resolvido. Um perfil vizinho
    (``.whatsapp-profile-2``) não casa, e o navegador pessoal também não.
    """
    alvo = _normalizar(str(perfil))
    encontrados = []
    lista_processos = _processos_do_windows() if os.name == "nt" else _processos_do_linux()
    for processo in lista_processos:
        cmd = processo.get("CommandLine") or ""
        if "--user-data-dir" not in cmd:
            continue
        for pedaco in cmd.split("--user-data-dir")[1:]:
            bruto = pedaco.lstrip("= ").split(" --")[0]
            if not bruto:
                continue
            try:
                if _normalizar(bruto) == alvo:
                    encontrados.append({
                        "pid": int(processo.get("ProcessId") or 0),
                        "nome": processo.get("Name") or "",
                        "cmd": cmd[:200],
                    })
            except (OSError, ValueError):
                continue
            break
    return [p for p in encontrados if p["pid"] > 0]


#: Pedaços de caminho que só existem no perfil REAL de um navegador instalado.
#: Um perfil do bot nunca mora aqui; o navegador do dia a dia do operador, sim.
_PERFIS_PESSOAIS = (
    ("bravesoftware", "brave-browser", "user data"),
    ("google", "chrome", "user data"),
    ("microsoft", "edge", "user data"),
    ("chromium", "user data"),
)


def perfil_pessoal(perfil: Path | str) -> bool:
    """O caminho é (ou está dentro de) o perfil real de um navegador instalado?

    Se alguém apontar ``WHATSAPP_PROFILE_DIR`` ou ``SIMULATOR_PROFILE_DIR``
    para o ``User Data`` do Brave, "encerrar quem segura o perfil" seria
    fechar o navegador pessoal do operador -- com as abas dele. Nesse caso a
    limpeza não roda.
    """
    import re

    # Separar à mão, e não com Path: o caminho é do Windows, e o teste (ou o
    # container) pode rodar noutro sistema, onde "\" não separa nada.
    partes = [p for p in re.split(r"[\\/]+", str(perfil).lower()) if p]
    for trecho in _PERFIS_PESSOAIS:
        tamanho = len(trecho)
        for i in range(len(partes) - tamanho + 1):
            if tuple(partes[i:i + tamanho]) == trecho:
                return True
    return False


def encerrar_orfaos(perfil: Path | str, on_log=None) -> int:
    """Encerra os navegadores que sobraram segurando o perfil.

    Chamada no boot, ANTES de abrir o navegador. Se não houver órfão -- o
    caso normal -- ela não faz nada e não diz nada.
    """
    def registrar(nivel: str, texto: str) -> None:
        if on_log:
            try:
                on_log(nivel, texto)
            except Exception:
                pass

    if perfil_pessoal(perfil):
        registrar(
            "WARNING",
            f"O perfil '{perfil}' é o de um navegador instalado, não um perfil do "
            "bot. Não vou encerrar processo nenhum nele: seria fechar o navegador "
            "pessoal do operador. Use um perfil próprio (sincronizar_perfis.ps1).",
        )
        return 0

    donos = donos_do_perfil(perfil)
    if not donos:
        return 0

    registrar(
        "WARNING",
        f"{len(donos)} navegador(es) ainda seguram o perfil '{Path(perfil).name}' "
        f"(PIDs {', '.join(str(d['pid']) for d in donos)}). São restos de uma "
        "execução anterior: o Chromium aceita um processo por perfil, e com "
        "eles vivos o WhatsApp não sobe. Encerrando.",
    )

    encerrados = 0
    for dono in donos:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(dono["pid"]), "/T", "/F"],
                               capture_output=True, timeout=15)
            else:
                import signal
                os.kill(dono["pid"], signal.SIGKILL)
            encerrados += 1
            registrar("INFO", f"Encerrado {dono['nome']} PID {dono['pid']}.")
        except (OSError, subprocess.SubprocessError) as exc:
            registrar("ERROR",
                      f"Não consegui encerrar o PID {dono['pid']}: {exc}. "
                      "Feche-o pelo Gerenciador de Tarefas.")

    if encerrados:
        registrar("INFO", f"{encerrados} navegador(es) órfão(s) encerrado(s); "
                          "o perfil está livre.")
    return encerrados
