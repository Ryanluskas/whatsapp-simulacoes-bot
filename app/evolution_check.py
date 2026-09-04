"""Diagnostico da Evolution API. O substituto do ``dump_dom.py``.

Uso::

    .venv/Scripts/python.exe -m app.evolution_check

Ele responde, em ordem, as perguntas que fazem o bot ficar mudo:

1. A Evolution esta' no ar?
2. A licenca da instancia foi ativada? (sem isso, TODO endpoint da 503, e
   voce perde a noite caçando erro de payload que nao existe)
3. A instancia esta' conectada ao WhatsApp?
4. Quais grupos ela enxerga, e o ``EVOLUTION_GROUP_JID`` configurado esta'
   entre eles?

**Este arquivo nao pode falhar calado.** O ``dump_dom.py`` uma vez rodou e
nao gerou nada, sem dizer por que -- e a investigacao seguinte foi feita as
cegas. Entao aqui: toda saida e' impressa na hora, todo erro vira uma linha
explicando o que fazer, e o codigo de saida diz se passou.
"""

from __future__ import annotations

import sys

import httpx

from .config import load_config
from .evolution import LICENCA_PENDENTE

OK = "  [ok]  "
FALHA = "  [FALHA] "
AVISO = "  [aviso] "


def _linha(texto: str = "") -> None:
    print(texto, flush=True)      # flush: se travar, o que ja' passou fica visivel


def _pedir(cliente: httpx.Client, rota: str) -> tuple[int, dict | list | str]:
    try:
        resposta = cliente.get(rota)
    except httpx.TransportError as exc:
        return 0, f"nao consegui conectar: {exc}"
    try:
        return resposta.status_code, resposta.json()
    except ValueError:
        return resposta.status_code, resposta.text[:400]


def main() -> int:
    config = load_config()
    _linha("=" * 66)
    _linha("Diagnostico da Evolution API")
    _linha("=" * 66)
    _linha(f"  URL        {config.evolution_url}")
    _linha(f"  Instancia  {config.evolution_instance}")
    _linha(f"  Modo atual {config.whatsapp_mode}")
    # A chave nunca e' impressa -- so' se ela existe.
    _linha(f"  API key    {'configurada' if config.evolution_api_key else 'AUSENTE'}")
    _linha(f"  Webhook    {'com token' if config.evolution_webhook_token else 'SEM TOKEN'}")
    _linha()

    if not config.evolution_api_key:
        _linha(FALHA + "EVOLUTION_API_KEY vazia no .env. Nada mais funciona sem ela.")
        return 1

    cliente = httpx.Client(
        base_url=config.evolution_url,
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"apikey": config.evolution_api_key},
    )
    problemas = 0

    try:
        # ---------------------------------------------------------- 1. no ar?
        _linha("1. A Evolution responde?")
        status, corpo = _pedir(cliente, "/")
        if status == 0:
            _linha(FALHA + str(corpo))
            _linha("       Suba com:  docker compose up -d evolution")
            return 1
        _linha(OK + f"HTTP {status}")

        # ------------------------------------------------------- 2. licenca?
        _linha()
        _linha("2. A licenca da instancia esta ativada?")
        status, corpo = _pedir(cliente, f"/instance/connectionState/{config.evolution_instance}")
        if status == 503 and LICENCA_PENDENTE in str(corpo):
            _linha(FALHA + "licenca NAO ativada -- todo endpoint vai responder 503.")
            _linha(f"       Abra {config.evolution_url}/manager e faca a ativacao.")
            _linha("       E' gratuita e sem limite de instancias.")
            return 1
        if status == 404:
            _linha(FALHA + f"a instancia '{config.evolution_instance}' nao existe.")
            _linha("       Crie com POST /instance/create (ver o README).")
            return 1
        if status >= 400:
            _linha(FALHA + f"HTTP {status}: {str(corpo)[:300]}")
            return 1
        _linha(OK + "sem 503 de licenca")

        # ----------------------------------------------------- 3. conectada?
        _linha()
        _linha("3. A instancia esta conectada ao WhatsApp?")
        estado = ""
        if isinstance(corpo, dict):
            estado = ((corpo.get("instance") or {}).get("state")
                      or corpo.get("state") or "")
        if estado == "open":
            _linha(OK + "conectada (state=open)")
        else:
            problemas += 1
            _linha(AVISO + f"state={estado or 'desconhecido'} -- nao esta conectada.")
            _linha(f"       Leia o QR em {config.evolution_url}/manager")

        # --------------------------------------------------------- 4. grupos
        _linha()
        _linha("4. Grupos que a instancia enxerga:")
        status, corpo = _pedir(
            cliente,
            f"/group/fetchAllGroups/{config.evolution_instance}?getParticipants=false")
        if status >= 400 or not isinstance(corpo, list):
            problemas += 1
            _linha(AVISO + f"nao consegui listar (HTTP {status}): {str(corpo)[:200]}")
            _linha("       Normal se a instancia ainda nao conectou.")
        elif not corpo:
            problemas += 1
            _linha(AVISO + "nenhum grupo. A instancia conectou ha pouco?")
        else:
            achou = False
            for grupo in corpo:
                jid = (grupo or {}).get("id", "")
                nome = (grupo or {}).get("subject", "")
                marca = "  <<< EVOLUTION_GROUP_JID" if jid == config.evolution_group_jid else ""
                if marca:
                    achou = True
                _linha(f"       {jid:34} {nome}{marca}")
            _linha()
            if not config.evolution_group_jid:
                problemas += 1
                _linha(AVISO + "EVOLUTION_GROUP_JID vazio no .env.")
                _linha("       Copie acima o id do grupo certo e coloque no .env.")
            elif achou:
                _linha(OK + "o JID configurado existe e a instancia o enxerga")
            else:
                problemas += 1
                _linha(FALHA + f"'{config.evolution_group_jid}' NAO esta na lista.")
                _linha("       O bot nao vai receber nem responder nada nesse grupo.")
    finally:
        cliente.close()

    _linha()
    _linha("=" * 66)
    if problemas:
        _linha(f"{problemas} ponto(s) a resolver antes de usar WHATSAPP_MODE=evolution.")
        return 1
    _linha("Tudo certo. Pode trocar WHATSAPP_MODE para evolution.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
