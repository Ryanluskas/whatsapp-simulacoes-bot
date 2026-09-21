"""Diagnostico SEGURO da Evolution real: so' estado, nunca segredo nem numero.

Uso::

    .venv/Scripts/python.exe -m app.evolution_diagnostico

Imprime um JSON com, e somente com:

* ``configured`` / ``reason`` -- se ha' o minimo para perguntar, e o que falta;
* ``reachable``, ``api_key_valid`` -- a Evolution responde e aceita a chave?
* ``instance``, ``instance_found``, ``state`` -- a instancia existe e conectou?
* ``webhook`` -- ha' webhook ligado, e ele aponta para ``/webhook/whatsapp``?
* ``webhook_events`` -- quais dos eventos necessarios faltam na assinatura, e
  se ``byEvents`` esta' ligado (com ele a rota do bot nao recebe nada);
* ``groups_ignored`` -- ``groupsIgnore`` ligado descarta os pedidos do grupo
  antes do webhook;
* ``version`` / ``version_expected`` -- a versao que a Evolution diz ser, e a
  que o projeto conferiu no codigo-fonte;
* ``webhook_token_configured``, ``group_configured`` -- booleanos do ``.env``;
* ``avisos`` -- cada problema acima em uma frase, para quem vai consertar.

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

#: A versao cujo codigo-fonte foi conferido (formatos de resposta, de webhook e
#: de ACK). E' a mesma tag fixada no ``docker-compose.yml``; um teste garante
#: que as duas nao se separam.
VERSAO_ESPERADA = "2.3.7"


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
    evolution.conferir_grupos_ignorados()
    evolution.conferir_versao()
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
        "webhook_events": {"missing": d.get("webhook_events_missing"),
                           "by_events": d.get("webhook_by_events")},
        "groups_ignored": d.get("groups_ignored"),
        "version": d.get("evolution_version"),
        "version_expected": VERSAO_ESPERADA,
    }
    if config.whatsapp_mode != MODO_EVOLUTION:
        resultado["reason"] = ("a Evolution responde, mas o bot ainda está em "
                               f"WHATSAPP_MODE={config.whatsapp_mode}")
    resultado["avisos"] = _avisos(resultado)
    return resultado


def _avisos(r: dict) -> list[str]:
    """Cada problema de infraestrutura em uma frase. Nunca URL, chave ou JID."""
    if not (r.get("reachable") and r.get("api_key_valid")):
        return []   # sem API alcancavel e chave aceita, o resto nao foi conferido
    avisos = []
    if r.get("groups_ignored") is True:
        avisos.append("groupsIgnore está LIGADO na instância: a Evolution descarta "
                      "mensagens de grupo antes do webhook, e os pedidos do grupo NÃO "
                      "chegam ao bot. Desligue groupsIgnore nas configurações da instância.")
    elif r.get("groups_ignored") is None:
        avisos.append("não consegui conferir groupsIgnore (/settings/find); confira na "
                      "instância antes do teste real — ligado, os pedidos do grupo não chegam.")
    eventos = r.get("webhook_events") or {}
    if eventos.get("missing"):
        avisos.append("o webhook não assina " + ", ".join(eventos["missing"])
                      + ": a Evolution só entrega os eventos da lista. Sem MESSAGES_UPSERT "
                        "nenhum pedido chega; sem MESSAGES_UPDATE nenhum ACK chega.")
    elif eventos.get("missing") is None and (r.get("webhook") or {}).get("configured"):
        avisos.append("não consegui ler os eventos assinados pelo webhook; confira se "
                      "MESSAGES_UPSERT, MESSAGES_UPDATE e CONNECTION_UPDATE estão na lista.")
    if eventos.get("by_events") is True:
        avisos.append("byEvents está ligado no webhook: a Evolution acrescenta o nome do "
                      "evento ao fim da URL, e a rota do bot não atende esse caminho.")
    versao = r.get("version")
    if versao and versao != r.get("version_expected"):
        avisos.append(f"a Evolution diz ser {versao}; o projeto foi conferido contra "
                      f"{r.get('version_expected')}. Formatos de resposta e de webhook "
                      "podem ser outros.")
    elif not versao:
        avisos.append("não consegui ler a versão da Evolution (GET /).")
    return avisos


def pronto(resultado: dict) -> bool:
    """Pronto para o teste real. O que nao deu para conferir vira aviso, nao
    recusa; o que foi conferido e esta' ERRADO recusa."""
    eventos = resultado.get("webhook_events") or {}
    return bool(resultado.get("configured") and resultado.get("reachable")
                and resultado.get("api_key_valid") and resultado.get("state") == "open"
                and (resultado.get("webhook") or {}).get("points_to_bot")
                and resultado.get("group_configured")
                and resultado.get("webhook_token_configured")
                and resultado.get("groups_ignored") is not True
                and not eventos.get("missing")
                and eventos.get("by_events") is not True)


def main() -> int:
    resultado = diagnosticar(load_config())
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
    if not resultado.get("configured"):
        return 2
    return 0 if pronto(resultado) else 1


if __name__ == "__main__":
    sys.exit(main())
