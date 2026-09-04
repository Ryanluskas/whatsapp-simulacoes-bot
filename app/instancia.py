"""Trava de instância única — um processo por perfil do navegador.

Por que existe
--------------
Numa madrugada de depuração descobrimos DUAS instâncias do bot rodando ao
mesmo tempo (uma na ``.venv``, outra no Python global), as duas apontando
para o mesmo ``.whatsapp-profile``. O Chromium aceita um processo por
``user_data_dir``: a segunda subia, brigava pelo lock, e o resultado era
instabilidade que parecia bug de código -- aba em branco, sessão caindo,
conversa fechando sozinha.

O recurso disputado é o **perfil**, não o programa. Por isso a trava é por
diretório de perfil: o bot e as ferramentas de diagnóstico competem pelo
mesmo lock, e é isso que se quer -- rodar o ``testar_citacao.py`` com o bot
no ar é exatamente o mesmo conflito.

Como funciona
-------------
Um arquivo com o PID e a hora de criação do processo. Na subida:

* sem arquivo, ou dono morto -> assume a trava;
* dono vivo -> **recusa subir**, dizendo qual é o PID.

Recusar é melhor que subir e brigar: um bot que não sobe e explica por quê
custa um minuto; dois bots disputando o perfil custaram uma noite.

Sobre reaproveitamento de PID
-----------------------------
Depois de um reinício, o sistema pode entregar o PID antigo do bot a
qualquer outro programa -- e a trava recusaria subir por causa de um Excel
aberto. Por isso o arquivo guarda também a hora de criação do processo: PID
igual com hora diferente é outro processo, e a trava está livre.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import time
from pathlib import Path


class InstanciaEmUso(RuntimeError):
    """Já existe um processo vivo usando este perfil."""

    def __init__(self, pid: int, rotulo: str, desde: str = "") -> None:
        self.pid = pid
        self.rotulo = rotulo
        self.desde = desde
        quando = f", desde {desde}" if desde else ""
        super().__init__(
            f"já existe uma instância rodando, PID {pid} ({rotulo}{quando})")


# --------------------------------------------------------------- o processo
def _criacao_windows(pid: int) -> tuple[bool, int]:
    """(está vivo, hora de criação). Sem matar nada.

    ``os.kill(pid, 0)`` NÃO serve no Windows: fora de CTRL_C_EVENT e
    CTRL_BREAK_EVENT o Python chama ``TerminateProcess``, ou seja, perguntar
    "está vivo?" mataria o processo. Daí o caminho pelo ctypes.
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False, 0
    try:
        codigo = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(codigo)):
            if codigo.value != STILL_ACTIVE:
                return False, 0

        criacao = wintypes.FILETIME()
        resto = (wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME())
        if kernel32.GetProcessTimes(handle, ctypes.byref(criacao),
                                    *[ctypes.byref(f) for f in resto]):
            quando = (criacao.dwHighDateTime << 32) | criacao.dwLowDateTime
            return True, quando
        return True, 0
    finally:
        kernel32.CloseHandle(handle)


def _criacao_posix(pid: int) -> tuple[bool, int]:
    try:
        os.kill(pid, 0)          # seguro fora do Windows: só consulta
    except ProcessLookupError:
        return False, 0
    except PermissionError:
        return True, 0           # existe, mas é de outro usuário
    try:
        campos = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return True, int(campos[19])       # starttime
    except (OSError, IndexError, ValueError):
        return True, 0


def processo(pid: int) -> tuple[bool, int]:
    """(está vivo, marca de criação). A marca é 0 quando não dá para saber."""
    if pid <= 0:
        return False, 0
    try:
        if os.name == "nt":
            return _criacao_windows(pid)
        return _criacao_posix(pid)
    except Exception:
        # Na dúvida, dizer que está VIVO. Um falso "livre" deixaria duas
        # instâncias subirem, que é o problema que esta trava existe para
        # impedir; um falso "ocupado" só pede uma intervenção manual.
        return True, 0


# ------------------------------------------------------------------- a trava
class TravaDeInstancia:
    """Um processo por perfil. Use como context manager."""

    def __init__(self, perfil: Path | str, rotulo: str = "bot",
                 pasta: Path | None = None) -> None:
        self.perfil = Path(perfil).resolve()
        self.rotulo = rotulo
        raiz = Path(pasta) if pasta else Path(__file__).resolve().parent.parent
        self.pasta = raiz / ".locks"
        # Nome derivado do CAMINHO do perfil: é ele o recurso disputado, e
        # dois programas diferentes no mesmo perfil têm de colidir.
        seguro = "".join(c if c.isalnum() else "-" for c in str(self.perfil))[-80:]
        self.arquivo = self.pasta / f"{seguro.strip('-')}.lock"
        self._minha = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------ leitura
    def dono(self) -> dict | None:
        """Quem está com a trava, ou None se ela está livre.

        Um arquivo ilegível conta como livre: lock corrompido não pode
        impedir o sistema de subir para sempre.
        """
        try:
            dados = json.loads(self.arquivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            pid = int(dados.get("pid", 0))
        except (TypeError, ValueError):
            return None

        vivo, criacao = processo(pid)
        if not vivo:
            return None
        # PID reaproveitado depois de um reinício: mesmo número, processo
        # diferente. Só conta como dono se a hora de criação bater.
        gravada = dados.get("criacao")
        if gravada and criacao and int(gravada) != int(criacao):
            return None
        return dados

    # ---------------------------------------------------------- aquisição
    def adquirir(self) -> None:
        """Assume a trava, ou levanta ``InstanciaEmUso``.

        A criacao do arquivo e' EXCLUSIVA (``O_CREAT | O_EXCL``), e nao um
        "verifica e depois escreve". A versao anterior fazia os dois passos
        separados e perdia a corrida que a trava existe para impedir: num
        teste com oito threads partindo juntas, **as oito** adquiriram. Entre
        processos seria igual -- e dois bots subindo ao mesmo tempo e'
        exatamente o caso que motivou tudo isto.

        O ``threading.Lock`` de instancia nao ajudava: cada objeto tem o seu,
        e processos diferentes nem compartilham memoria. Quem arbitra tem de
        ser o sistema de arquivos.
        """
        with self._lock:
            self.pasta.mkdir(parents=True, exist_ok=True)

            for tentativa in range(2):
                if self._criar_exclusivo():
                    self._minha = True
                    atexit.register(self.liberar)
                    return

                atual = self._dono_com_paciencia()
                if atual is not None:
                    raise InstanciaEmUso(int(atual.get("pid", 0)),
                                         str(atual.get("rotulo") or "desconhecido"),
                                         str(atual.get("desde") or ""))

                # O arquivo existe mas o dono morreu (ou o registro esta'
                # ilegivel): limpar e tentar de novo, uma vez so'. Um lock
                # orfao nao pode impedir o boot para sempre.
                if tentativa == 0:
                    try:
                        self.arquivo.unlink()
                    except OSError:
                        pass

            raise InstanciaEmUso(0, "desconhecido", "")

    #: Quanto esperar um lock recém-criado terminar de ser escrito.
    _PACIENCIA_COM_LOCK_NOVO = 0.6

    def _dono_com_paciencia(self) -> dict | None:
        """``dono()``, mas sem confundir "sendo escrito" com "corrompido".

        Entre criar o arquivo com ``O_EXCL`` e gravar o conteudo existe uma
        janela. Quem le' nela ve' um arquivo VAZIO, o JSON falha, ``dono()``
        devolve None -- e o codigo conclui "lock orfao", apaga e assume. Duas
        instancias no mesmo perfil, que e' exatamente o que a trava existe
        para impedir.

        A janela e' minuscula e nao aparecia rodando o teste sozinho: so' sob
        a carga da suite inteira, com CPU disputada, ela abriu. Por isso um
        arquivo ilegivel e' relido por um instante antes de ser dado como
        perdido -- um lock a meio caminho vira legivel; um corrompido de
        verdade continua ilegivel e segue sendo limpo.
        """
        dono = self.dono()
        if dono is not None:
            return dono
        # Existe mas nao deu para ler: pode estar sendo escrito agora.
        if not self.arquivo.exists():
            return None
        limite = time.monotonic() + self._PACIENCIA_COM_LOCK_NOVO
        while time.monotonic() < limite:
            time.sleep(0.05)
            dono = self.dono()
            if dono is not None:
                return dono
            if not self.arquivo.exists():
                return None
        return None

    def _criar_exclusivo(self) -> bool:
        """Cria o arquivo da trava so' se ele ainda nao existir. Atomico."""
        from .clock import now_iso

        _, criacao = processo(os.getpid())
        conteudo = json.dumps({
            "pid": os.getpid(),
            "criacao": criacao,
            "rotulo": self.rotulo,
            "desde": now_iso(),
            "perfil": str(self.perfil),
            "executavel": sys.executable,
        }, indent=2)
        try:
            descritor = os.open(self.arquivo,
                                os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            with os.fdopen(descritor, "w", encoding="utf-8") as saida:
                saida.write(conteudo)
        except OSError:
            return False
        return True

    def liberar(self) -> None:
        """Solta a trava, se for nossa.

        Nunca apaga a trava de outro processo: um encerramento nosso não pode
        liberar o perfil de quem está usando de verdade.
        """
        with self._lock:
            if not self._minha:
                return
            try:
                dados = json.loads(self.arquivo.read_text(encoding="utf-8"))
                if int(dados.get("pid", 0)) == os.getpid():
                    self.arquivo.unlink()
            except (OSError, ValueError):
                pass
            self._minha = False

    # --------------------------------------------------------- açúcar
    def __enter__(self) -> "TravaDeInstancia":
        self.adquirir()
        return self

    def __exit__(self, *_) -> None:
        self.liberar()


def explicar(erro: InstanciaEmUso, perfil: Path | str) -> str:
    """A mensagem que o operador lê no terminal. Diz o que fazer."""
    return "\n".join([
        "",
        "=" * 68,
        f"  JÁ EXISTE UMA INSTÂNCIA RODANDO — PID {erro.pid}",
        "=" * 68,
        f"  processo   {erro.rotulo}"
        + (f", desde {erro.desde}" if erro.desde else ""),
        f"  perfil     {perfil}",
        "",
        "  O Chromium aceita um processo por perfil. Subir uma segunda",
        "  instância faria as duas brigarem pelo mesmo navegador — aba em",
        "  branco, sessão caindo, conversa fechando sozinha.",
        "",
        "  Para encerrar a que está rodando:",
        f"      taskkill /PID {erro.pid} /F" if os.name == "nt"
        else f"      kill {erro.pid}",
        "=" * 68,
        "",
    ])
