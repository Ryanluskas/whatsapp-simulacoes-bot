"""Diagnostico SEGURO da Evolution real: so' estado, nunca segredo nem numero.

Uso::

    .venv/Scripts/python.exe -m app.evolution_diagnostico

Imprime um JSON com, e somente com:

* ``configured`` / ``reason`` -- se ha' o minimo para perguntar, e o que falta;
* ``reachable``, ``api_key_valid`` -- a Evolution responde e aceita a chave?
* ``instance``, ``instance_found``, ``state`` -- a instancia existe e conectou?
* ``webhook`` -- ha' webhook ligado, e ele aponta para ``/webhook/whatsapp``?
* ``webhook_token_configured``, ``group_configured`` -- booleanos do ``.env``.

Nunca: API key, token do webhook, URL do webhook, JID completo, telefone.
Diferente do ``app.evolution_check`` (que lista os grupos com o JID para
configurar o ``.env``), a saida daqui pode ser colada num chamado ou num chat.

Codigo de saida: 0 = tudo pronto (conectada, webhook no bot, grupo e token
definidos); 1 = alguma coisa falta; 2 = nao configurado.
"""

from __future__ import annotations

import json
import sys

import httpx

from .config import load_config
from .evolution import EvolutionClient
from .whatsapp_port import MODO_EVOLUTION


def _faltando(config) -> list[str]:
    faltam = []
    if not (config.evolution_url or "").strip():
        faltam.append("EVOLUTION_URL")
    if not (config.evolution_api_key or "").strip():
        faltam.append("EVOLUTION_API_KEY")
    if not (config.evolution_instance or "").strip():
        faltam.append("EVOLUTION_INSTANCE")
    return faltam


def diagnosticar(config, client: httpx.Client | None = None) -> dict:
    """O diagnostico em si. ``client`` permite testar sem rede."""
    base = {
        "whatsapp_mode": config.whatsapp_mode,
        "instance": config.evolution_instance or "",
        "group_configured": bool((config.evolution_group_jid or "").strip()),
        "webhook_token_configured": bool((config.evolution_webhook_token or "").strip()),
    }
    faltam = _faltando(config)
    if faltam:
        return {**base, "configured": False,
                "reason": "faltam no .env: " + ", ".join(faltam)}

    # Nenhum envio aqui: o renderizador nunca e' usado, entao nao sobe Chromium.
    evolution = EvolutionClient(
        config.evolution_url, config.evolution_api_key, config.evolution_instance,
        config.evolution_group_jid, renderer=object(), client=client)
    evolution.atualizar_estado()
    evolution.conferir_webhook()
    d = evolution.diagnostico()
    resultado = {
        **base,
        "configured": True,
        "reason": "",
        "reachable": d.get("evolution_api_reachable"),
        "api_key_valid": d.get("api_key_valid"),
        "instance_found": d.get("instance_found"),
        "state": d.get("evolution_state"),
        "webhook": {"configured": d.get("webhook_configured"),
                    "points_to_bot": d.get("webhook_points_to_bot")},
    }
    if config.whatsapp_mode != MODO_EVOLUTION:
        resultado["reason"] = ("a Evolution responde, mas o bot ainda está em "
                               f"WHATSAPP_MODE={config.whatsapp_mode}")
    return resultado


def pronto(resultado: dict) -> bool:
    return bool(resultado.get("configured") and resultado.get("reachable")
                and resultado.get("api_key_valid") and resultado.get("state") == "open"
                and (resultado.get("webhook") or {}).get("points_to_bot")
                and resultado.get("group_configured")
                and resultado.get("webhook_token_configured"))


def main() -> int:
    resultado = diagnosticar(load_config())
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
    if not resultado.get("configured"):
        return 2
    return 0 if pronto(resultado) else 1


if __name__ == "__main__":
    sys.exit(main())
