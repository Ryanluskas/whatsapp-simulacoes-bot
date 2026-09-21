# Roteiro do teste real — Evolution + WhatsApp + celular

Os testes automáticos usam uma Evolution **falsa**. Eles não provam que a
Evolution de verdade responde no formato esperado, que a citação aparece no
celular nem que o Santander devolve o que o bot lê. Isto é o que falta, feito
à mão, no **grupo de teste**, antes de apontar o bot para o grupo real.

Regras do teste:

- **Nunca registre CPF real, nome de cliente ou telefone** neste roteiro, em
  print, em chamado ou em chat. Registre `request_id` e ids de mensagem.
- Use um número de WhatsApp **do bot** (vinculado à Evolution) e **outro**
  número como consultor. Mensagem enviada pelo próprio número do bot
  (`fromMe`) é ignorada de propósito.
- Primeiro com o CPF sintético `529.982.247-25`: o Santander não acha o
  cliente e o bot responde com erro — isso já testa webhook, fila, imagem,
  entrega e citação sem consultar ninguém. Consulta com CPF real só com um
  cliente que autorizou, e nunca anotada aqui.

## 0. Pré-requisitos

| # | Item | Como conferir | Resultado | Timestamp |
|---|---|---|---|---|
| 1 | `.env` com `WHATSAPP_MODE=evolution` | abrir o `.env` | | |
| 2 | `EVOLUTION_URL` (ex.: `http://localhost:8080`) | diagnóstico abaixo: `reachable: true` | | |
| 3 | `EVOLUTION_API_KEY` | diagnóstico: `api_key_valid: true` | | |
| 4 | `EVOLUTION_INSTANCE` (ex.: `allana`) conectada | diagnóstico: `state: "open"` | | |
| 5 | `EVOLUTION_GROUP_JID` do **grupo de teste** | `python -m app.evolution_check` (lista os JIDs — não compartilhe a saída) | | |
| 6 | `EVOLUTION_WEBHOOK_TOKEN` (32+ caracteres) | diagnóstico: `webhook_token_configured: true` | | |
| 7 | Webhook apontando para `/webhook/whatsapp` com o token | diagnóstico: `webhook.points_to_bot: true` | | |
| 8 | Grupo de teste com o número do bot e o número consultor | celular | | |
| 9 | Consultor de teste cadastrado (ou deixar o bot criar) | painel → Consultores | | |
| 10 | `DASHBOARD_PASSWORD` e `SESSION_SECRET` definidos | o bot sobe sem "RECUSANDO INICIAR" | | |

```bash
.venv/Scripts/python.exe -m app.evolution_diagnostico
```

Só siga com saída `0` (tudo pronto). O JSON não traz chave, token, JID nem
telefone: pode ser colado no registro abaixo.

Webhook (troque os três valores; não salve o comando com eles):

```bash
curl -X POST http://localhost:8080/webhook/set/allana -H "apikey: SUA_CHAVE" -H "Content-Type: application/json" -d "{\"webhook\":{\"enabled\":true,\"url\":\"http://ENDERECO_DO_BOT:8000/webhook/whatsapp\",\"byEvents\":false,\"headers\":{\"X-Webhook-Token\":\"SEU_TOKEN\"},\"events\":[\"MESSAGES_UPSERT\",\"MESSAGES_UPDATE\",\"CONNECTION_UPDATE\"]}}"
```

Os três eventos são obrigatórios: a Evolution v2.3.7 só entrega o que está em `events`. Sem `MESSAGES_UPDATE`, nenhum ACK chega ao bot. O campo é `byEvents` (é o nome que o schema da v2.3.7 lê; `webhookByEvents` é ignorado em silêncio) e fica `false`: ligado, a Evolution acrescenta o nome do evento ao fim da URL e a rota do bot não recebe nada. Confira tudo com `python -m app.evolution_diagnostico` (campos `webhook_events` e `avisos`).

A Evolution precisa **alcançar** esse endereço. Se ela roda em Docker/WSL e o
bot no Windows, `127.0.0.1` não serve, e o firewall do Windows costuma
bloquear a entrada: resolva isso antes (regra de firewall para a porta, ou o
bot rodando na mesma rede da Evolution) — o passo 13 mostra se chegou.

## 1. Pedido real (caminho feliz)

| # | Passo | Como observar | Esperado | Resultado | Timestamp | request_id | message_id do pedido | message_id da resposta |
|---|---|---|---|---|---|---|---|---|
| 11 | Subir o bot | `.venv/Scripts/python.exe main.py` | "Painel em 127.0.0.1…", sem RECUSANDO | | | | | |
| 12 | Do número consultor, mandar no grupo: nome fictício / `529.982.247-25` / estado | celular | mensagem enviada | | | | | |
| 13 | Webhook chegou | painel → Status: "último webhook" atualizado; log sem 401 | < 5 s | | | | | |
| 14 | Virou UMA solicitação | `python ferramentas/observar_entrega.py --ultima` | `encontrada: true`, 1 mensagem `in` | | | | | |
| 15 | Santander consultado | log do simulador / etapa no painel | etapa passa por consulta e extração | | | | | |
| 16 | PNG gerado | `comprovantes/<REQ>.png` abre e é miniatura legível | PNG válido, do REQ certo | | | | | |
| 17 | Evolution enviou | log do bot: `envio=imagem … desfecho=entregue sent_message_id=…` | HTTP 201 com `key.id` | | | | | |
| 18 | Chegou no celular | grupo de teste | **uma** mensagem, imagem em miniatura (não arquivo) | | | | | |
| 19 | Citação visual | no celular a resposta aparece **citando o pedido do passo 12** | citação do pedido certo | | | | | |
| 20 | Painel | detalhe da solicitação | Entrega "Entregue", Citação "Citada (confirmada)" ou "(sem como conferir)" | | | | | |

No passo 20, "sem como conferir" (`unverified`) quer dizer que a Evolution não
devolveu o `stanzaId`. Se no celular a citação estiver certa, anote: é o
formato real da resposta desta versão. Guarde a resposta fora do git e rode
`ferramentas/conferir_resposta_evolution.py` (ver MIGRACAO-EVOLUTION.md).

## 2. Falha de citação

Objetivo: provar que, quando a Evolution **recusa** o `quoted`, sai **uma**
resposta sem citação, terminando em `↩ <consultor>` — e nunca duas.

| # | Passo | Esperado | Resultado | Timestamp | request_id |
|---|---|---|---|---|---|
| 21 | Mandar um pedido e **apagar para todos** a mensagem do pedido antes de o bot responder | a Evolution responde 400/422 citando o `quoted` **ou** envia sem conseguir citar | | | |
| 22 | `observar_entrega.py <REQ>` | 1 saída entregue; `quote_status` = `fallback` (recusa) ou `not_applied`/`unverified` (saiu sem citar) | | | |
| 23 | Celular | **uma** resposta; sem citação termina em `↩ <consultor>` | | | |
| 24 | Log | `A Evolution RECUSOU a citação` com o corpo do 400 **ou** `Citação enviada, mas NÃO confirmada` | | | |

Se o 400 real não for classificado como recusa de citação (ficar `unconfirmed`),
**isso não duplica**, mas registre o corpo do erro: é o dado que calibra
`classificar_resposta`.

## 3. Entrega incerta

Objetivo: provar que "pode ter saído" nunca vira segunda mensagem.

| # | Passo | Esperado | Resultado | Timestamp | request_id |
|---|---|---|---|---|---|
| 25 | Mandar um pedido e, quando o painel mostrar "Enviando", **pausar** a Evolution (`docker pause allana-evolution`); despausar depois de ~40 s | timeout de leitura → incerta | | | |
| 26 | Painel | Entrega "Entrega incerta — verificar WhatsApp"; botões aparecem | | | |
| 27 | Esperar 2 minutos | **nenhuma** mensagem nova no grupo; `reply_attempts` não sobe sozinho | | | |

## 4. Reenvio manual

| # | Passo | Esperado | Resultado | Timestamp | request_id | message_id do reenvio |
|---|---|---|---|---|---|---|
| 28 | Conferir o grupo. Se a resposta **está lá**: "Chegou no grupo" | nada é enviado; `delivery_resolution=manual:chegou`, `delivery_resolved_by` preenchido | | | | |
| 29 | Se **não está**: "Não chegou — reenviar" | em até ~30 s sai **uma** resposta, citando o mesmo pedido, com o mesmo REQ | | | | |
| 30 | Clicar "Não chegou" de novo (outra aba) | 409; nada sai | | | | |
| 31 | `observar_entrega.py <REQ>` | saídas antigas `failed` com `[conferido no painel: não chegou]`; uma `completed` com `wa_message_id` | | | | |

## Resultado

| Item | Passou? | Observação (sem dado pessoal) |
|---|---|---|
| Caminho feliz (11–20) | | |
| Citação visual correta (19) | | |
| Falha de citação sem duplicar (21–24) | | |
| Entrega incerta sem duplicar (25–27) | | |
| Reenvio manual único (28–31) | | |

Só com as cinco linhas aprovadas o PR pode ser considerado validado em
ambiente real. Qualquer "não" vira issue, com o `request_id` e o trecho de log.
