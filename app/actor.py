"""Modelo de ator para navegadores Playwright.

Esta e' a correcao do defeito que derrubava o sistema inteiro.

A Sync API do Playwright amarra o navegador a *uma* thread e a um greenlet
dela. O codigo anterior criava o navegador do WhatsApp na thread ``wa-loop``,
mas mandava mensagem a partir da thread do worker e fechava o navegador a
partir da thread principal e da thread do servidor HTTP. Depois da primeira
violacao, o event loop daquela thread ficava marcado como *running* e todo
``sync_playwright().start()` seguinte falhava com::

    It looks like you are using Playwright Sync API inside the asyncio loop.

Sem recuperacao possivel a nao ser reiniciar o processo - foi exatamente o que
os 138 erros identicos no banco mostravam.

A regra passa a ser absoluta: **nenhum objeto do Playwright cruza a fronteira
da thread que o criou**. Quem esta' de fora envia um comando pela caixa de
entrada e espera o resultado; a thread dona executa tudo, inclusive o
encerramento.
"""

from __future__ import annotations

import queue
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

SHUTDOWN = object()


class ActorError(RuntimeError):
    pass


class ActorStopped(ActorError):
    pass


class ActorTimeout(ActorError):
    """Quem chamou desistiu de esperar.

    ``iniciado`` diz se o comando CHEGOU A COMECAR na thread dona. Nao
    iniciado: ele foi cancelado e nunca vai rodar -- nada aconteceu. Iniciado:
    ele pode estar rodando agora e terminar depois (a thread dona nao e'
    interrompida). Para um envio, isso quer dizer "a mensagem pode ter saido":
    ``sem_prova``.
    """

    def __init__(self, mensagem: str = "", *, iniciado: bool = True) -> None:
        super().__init__(mensagem)
        self.iniciado = iniciado

    @property
    def sem_prova(self) -> bool:
        return self.iniciado


@dataclass
class Command:
    fn: Callable[..., Any]
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None
    label: str = ""
    iniciado: bool = False
    cancelado: bool = False
    _estado: threading.Lock = field(default_factory=threading.Lock)

    def run(self) -> None:
        # Quem chamou ja' desistiu (tempo esgotado) e ja' tratou como falha:
        # executar agora seria uma acao fantasma -- um envio que sai minutos
        # depois de a resposta ter ido por outro caminho.
        with self._estado:
            if self.cancelado:
                return
            self.iniciado = True
        try:
            self.result = self.fn(*self.args, **self.kwargs)
        except BaseException as exc:  # noqa: BLE001 - repassado para quem chamou
            self.error = exc
        finally:
            self.done.set()

    def wait(self, timeout: float | None) -> Any:
        if not self.done.wait(timeout):
            with self._estado:
                terminou = self.done.is_set()
                if not terminou and not self.iniciado:
                    self.cancelado = True
                iniciado = self.iniciado
            if not terminou:
                raise ActorTimeout(
                    f"tempo esgotado aguardando '{self.label or self.fn.__name__}' "
                    + ("(em execução: pode concluir depois)" if iniciado
                       else "(nem começou: cancelado)"),
                    iniciado=iniciado)
        if self.error is not None:
            raise self.error
        return self.result


class ThreadActor:
    """Thread dona de um recurso nao thread-safe (aqui, um navegador)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._mailbox: queue.Queue[Command | object] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._started = threading.Event()

    # ------------------------------------------------------------ ciclo de vida
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._main, name=self.name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        if self._thread is None:
            return
        self._stopping.set()
        self._mailbox.put(SHUTDOWN)
        self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def owns_current_thread(self) -> bool:
        return threading.current_thread() is self._thread

    # ---------------------------------------------------------------- comandos
    def call(self, fn: Callable[..., Any], *args: Any, timeout: float | None = 60.0, **kwargs: Any) -> Any:
        """Executa ``fn`` na thread dona e espera o resultado."""
        if self.owns_current_thread():
            return fn(*args, **kwargs)  # ja' estamos no lugar certo
        if not self.running:
            raise ActorStopped(f"{self.name} não está em execução")
        cmd = Command(fn=fn, args=args, kwargs=kwargs, label=getattr(fn, "__name__", ""))
        self._mailbox.put(cmd)
        return cmd.wait(timeout)

    def post(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Enfileira sem esperar o resultado."""
        if not self.running:
            return
        self._mailbox.put(Command(fn=fn, args=args, kwargs=kwargs, label=getattr(fn, "__name__", "")))

    def _drain(self, timeout: float) -> bool:
        """Processa comandos pendentes. Devolve False se pediram encerramento.

        Bloqueia ate' ``timeout`` esperando o primeiro comando e depois esvazia
        o resto sem bloquear - assim a thread dona serve como relogio do proprio
        loop, sem precisar de ``sleep`` separado.
        """
        try:
            item = self._mailbox.get(timeout=timeout)
        except queue.Empty:
            return True
        while True:
            if item is SHUTDOWN:
                return False
            if isinstance(item, Command):
                item.run()
            try:
                item = self._mailbox.get_nowait()
            except queue.Empty:
                return True

    def _reject_pending(self, error: BaseException) -> None:
        while True:
            try:
                item = self._mailbox.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, Command):
                item.error = error
                item.done.set()

    # --------------------------------------------------------------- subclasse
    def _main(self) -> None:
        self._started.set()
        try:
            self.run()
        except BaseException:  # noqa: BLE001
            traceback.print_exc()
        finally:
            self._reject_pending(ActorStopped(f"{self.name} encerrado"))

    def run(self) -> None:  # pragma: no cover - implementado nas subclasses
        raise NotImplementedError
