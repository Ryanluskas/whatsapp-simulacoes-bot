# Migrar o WhatsApp para a Evolution API

O código está pronto e testado. O que falta são os passos que dependem da sua
máquina e do seu celular — Docker instalado, licença ativada, QR lido.

O modo `dom` (o de hoje) continua funcionando o tempo todo. Nada aqui muda o
comportamento do bot até você trocar `WHATSAPP_MODE` no `.env`.

---

## Por que a troca

Os três defeitos que custaram este mês inteiro têm a mesma raiz: o bot executa
ações na tela do WhatsApp Web e torce para terem funcionado.

| Defeito | Hoje (modo `dom`) | Com a Evolution |
|---|---|---|
| Não cita a mensagem | cascata de cliques no menu de contexto | campo `quoted` no JSON |
| Manda como documento | `input[type=file]` errado ou item errado do menu | `mediatype: "image"` |
| Falha calado | ação sem verificação | resposta HTTP com status e `key.id` |

---

## Riscos, antes da primeira linha

**1. É não-oficial.** A Evolution usa Baileys, que implementa o protocolo do
WhatsApp por engenharia reversa. **Existe risco de banimento do número.** O
código já mantém `delay` de 1,2 s entre envios por esse motivo — não remova.
Se puder, use um número que não seja o seu principal.

**2. Versão e licença.** A imagem está presa em `v2.3.7` no
`docker-compose.yml`: é a última estável publicada no Docker Hub (a `v2.4.1`
que constava aqui não existe; a 2.4.0 só saiu como release candidate). A
v2.3.7 sobe sem ativação de licença. Se um dia subir para a 2.4, a instância
precisa ser ativada no `/manager` contra o servidor da Evolution Foundation —
sem isso **todo endpoint responde 503**, e o `evolution_check` avisa.

**3. LID.** O WhatsApp está trocando os identificadores: o remetente pode
chegar como `@lid` em vez de `@s.whatsapp.net`, e aí o número antes do `@`
**não é o telefone**. O código trata os dois formatos e, quando não resolve,
registra WARNING e **processa o pedido mesmo assim** — perder a solicitação
seria pior que não saber a ficha de quem mandou.

**4. Slot de dispositivo.** A Evolution ocupa um slot de aparelho vinculado,
igual ao WhatsApp Web. Seu celular continua funcionando. Depois que a
Evolution estiver validada, **remova o dispositivo antigo do WhatsApp Web**
para não ter duas sessões brigando.

---

## Passo 1 — Subir a Evolution

Docker ainda não está instalado nesta máquina. O instalador está em
`~/Downloads/Docker Desktop Installer.exe`.

Depois de instalar, gere os dois segredos e coloque no `.env`:

```bash
python -c "import secrets; print('EVOLUTION_API_KEY=' + secrets.token_urlsafe(32)); print('EVOLUTION_WEBHOOK_TOKEN=' + secrets.token_urlsafe(32))"
```

Suba (a Evolution está em perfil próprio, não sobe junto com o painel):

```bash
docker compose --profile evolution up -d
```

Na v2.3.7 não há ativação de licença. (Na 2.4+, abra
`http://localhost:8080/manager` e ative antes de tudo: sem isso, tudo abaixo
responde 503.)

Crie a instância:

```bash
curl -X POST http://localhost:8080/instance/create -H "apikey: SUA_CHAVE" -H "Content-Type: application/json" -d "{\"instanceName\":\"allana\",\"qrcode\":true,\"integration\":\"WHATSAPP-BAILEYS\"}"
```

Leia o QR pelo `/manager`, com o celular que assina como **Operacional
Capital**.

Descubra o JID do grupo e confira tudo de uma vez:

```bash
.venv/Scripts/python.exe -m app.evolution_check
```

Ele diz se a Evolution responde, se a licença está ativa, se a instância
conectou, e lista os grupos com os JIDs. Copie o do grupo certo para
`EVOLUTION_GROUP_JID` no `.env` e rode de novo até ele terminar com
"Tudo certo".

---

## Passo 2 — Provar com `curl` antes de qualquer código

**Este é o passo mais importante.** Se não funcionar no `curl`, não vai
funcionar no bot, e você economiza horas.

Crie um grupo de teste com você e mais um número. Pegue o JID dele pelo
`evolution_check`. Mande uma mensagem qualquer nesse grupo pelo celular e
anote o `key.id` que aparecer no log da Evolution — ou só teste sem citação
primeiro.

Texto com citação:

```bash
curl -X POST http://localhost:8080/message/sendText/allana -H "apikey: SUA_CHAVE" -H "Content-Type: application/json" -d "{\"number\":\"SEU_GRUPO_DE_TESTE@g.us\",\"text\":\"teste de citacao\",\"delay\":1200,\"quoted\":{\"key\":{\"id\":\"ID_DA_MENSAGEM\"},\"message\":{\"conversation\":\"texto original\"}}}"
```

Imagem inline (o base64 tem de ser **puro**, sem `data:image/png;base64,`):

```bash
.venv/Scripts/python.exe -c "import base64,json,pathlib; p=pathlib.Path('comprovantes/REQ000034.png'); print(json.dumps({'number':'SEU_GRUPO_DE_TESTE@g.us','mediatype':'image','mimetype':'image/png','media':base64.b64encode(p.read_bytes()).decode(),'fileName':p.name,'caption':'teste de imagem','delay':1200}))" > /tmp/media.json
```

```bash
curl -X POST http://localhost:8080/message/sendMedia/allana -H "apikey: SUA_CHAVE" -H "Content-Type: application/json" --data-binary @/tmp/media.json
```

No celular, a imagem tem de aparecer como **miniatura**, não como card de
arquivo. Se aparecer como arquivo, pare aqui: algo está errado no
`mediatype`, e nenhum código vai consertar isso.

---

## Passo 3 — Apontar o webhook para o painel

```bash
curl -X POST http://localhost:8080/webhook/set/allana -H "apikey: SUA_CHAVE" -H "Content-Type: application/json" -d "{\"webhook\":{\"enabled\":true,\"url\":\"http://SEU_IP:8000/webhook/whatsapp\",\"webhookByEvents\":false,\"headers\":{\"X-Webhook-Token\":\"SEU_TOKEN_DO_WEBHOOK\"},\"events\":[\"MESSAGES_UPSERT\",\"CONNECTION_UPDATE\"]}}"
```

O `X-Webhook-Token` tem de bater com `EVOLUTION_WEBHOOK_TOKEN` no `.env`. Sem
ele a rota responde 401 — e é assim mesmo: ela recebe dado de cliente e
enfileira trabalho, não pode ficar aberta.

---

## Passo 4 — Trocar o modo

No `.env`:

```
WHATSAPP_MODE=evolution
```

Reinicie o bot. Se faltar alguma chave, ele **se recusa a subir** e diz qual —
melhor que subir mudo e ninguém entender por quê.

---

## Passo 5 — Validar, no grupo de TESTE primeiro

Só aponte para o grupo real depois de passar nos seis:

1. Mandar um CPF: chega **uma** mensagem, citando o pedido, com a imagem
   inline (miniatura, não card de arquivo).
2. Três pedidos seguidos de pessoas diferentes: cada resposta cita o pedido
   certo.
3. Um pedido que dá erro no portal: a mensagem de erro também chega citada.
4. `docker stop allana-evolution` no meio de uma simulação: a solicitação não
   some, o painel mostra o erro, e ela é reenviada quando voltar.
5. Repetir o mesmo `curl` de webhook duas vezes: uma resposta só.
6. O bot não responde a si mesmo (a resposta dele volta com `fromMe: true`).

---

## Como a entrega funciona na camada Evolution

Estas regras valem a partir desta versão e estão travadas por
`tests/test_producao.py` (unidade) e `ferramentas/e2e_simulado.py` (processo
real com Evolution e agente falsos).

**Idempotência.** O webhook grava a mensagem no banco (`messages`,
`direction='in'`) ANTES de responder 200. A gravação é a trava: a mesma
`key.id` no mesmo chat nunca vira segunda solicitação — nem em paralelo, nem
depois de reiniciar. Se o processo cair entre o 200 e a criação do `REQ`, o
boot retoma a mensagem (`status='received'`).

**Citação.** O `quoted` é montado com o que ficou gravado da mensagem
original: `key.id`, `remoteJid`, `participant` (o autor, como chegou — pode
ser `@lid`) e o texto. Nada vem de memória em RAM. A resposta da Evolution é
conferida: `contextInfo.stanzaId` igual ao id pedido = citação confirmada.

**Citação recusada.** SÓ quando a Evolution responde 400/422 apontando o
`quoted` (e sem sinal de erro pós-envio): a resposta sai de novo SEM `quoted`,
com a versão que termina em `↩ <consultor>`. Falha de citação não é falha de
resposta.

**500 NÃO é citação recusada.** Era, e esse era o defeito mais perigoso: um
500 podia ser erro DEPOIS de a mensagem sair, e o reenvio sem citação
entregava o resultado duas vezes no grupo. Hoje 500, timeout de leitura,
conexão caída depois de enviar e 2xx sem `key.id` são **entrega incerta**.

**Imagem.** Renderizada num arquivo provisório, validada (PNG legível:
assinatura, CRC, dados descomprimidos batendo com as dimensões) e só então
movida para `comprovantes/<REQ>.png`. Falhou render, validação ou envio → a
resposta sai em texto.

**A pergunta que decide tudo: a mensagem pode ter saído?** Só se a resposta
PROVAR que nada saiu é que o bot manda outra (sem citação, em texto, ou mais
tarde). Na dúvida, ele registra e para.

| Resposta | Desfecho | O que o bot faz |
|---|---|---|
| 2xx com `key.id` | entregue | grava o id; fim |
| 2xx sem `key.id` | **incerta** | `unconfirmed`; nada por cima, nada de reenvio |
| 2xx com corpo ilegível | **incerta** | idem |
| 400/422 apontando o `quoted` | citação recusada | reenvia SEM citação, com `↩ consultor` |
| 400/422 de validação (`requires property`, `must be`, `exists:false`…) | recusada | imagem → cai para texto; texto → `failed` |
| 400/422 sem explicação | **incerta** | `unconfirmed`; não cai para texto |
| 400 com sinal de erro pós-envio (`prisma`, `database`, `timeout`…) | **incerta** | idem — vence a marca de citação |
| 401 / 403 / 404 / licença | permanente | `failed`; não repete |
| 408 / 429 / 503 | transitória | `retrying`: repete a MESMA requisição (com citação) |
| 500, 502, 504 e outros 5xx | **incerta** | `unconfirmed`; **nunca** dispara reenvio sem citação. 502/504 costumam vir de um proxy na frente da Evolution: ela pode ter recebido e enviado |
| erro do httpx ao ler a resposta (corpo quebrado, protocolo) | **incerta** | `unconfirmed` |
| timeout de conexão, conexão recusada, falha ao subir o corpo | transitória | `retrying` |
| timeout de leitura, conexão caída depois de enviar | **incerta** | `unconfirmed` |

**`unconfirmed` quer dizer**: "a API não permitiu provar se saiu". O painel
mostra **"Entrega incerta — verificar WhatsApp"**, o log traz `attempt`,
`origin_message_id`, `quoted_message_id`, `http`, `quote_status` e o erro, e
**ninguém reenvia sozinho** — uma segunda mensagem no grupo é pior que uma
entrega que precisa ser conferida.

**Quem confere decide no painel.** No detalhe da solicitação aparecem dois
botões (e a rota `POST /api/simulations/{id}/entrega`):

* **Chegou no grupo** (`{"acao": "chegou"}`) — fecha como entregue; nada é
  enviado; `delivery_resolution = manual:chegou`;
* **Não chegou — reenviar** (`{"acao": "nao_chegou"}`) — libera **um**
  reenvio pelo laço de sempre, citando o mesmo pedido, mesmo com as
  tentativas esgotadas; `delivery_resolution = manual:nao_chegou`.

A troca só vale enquanto a linha está `unconfirmed` (UPDATE condicional): dois
cliques ou duas abas não viram dois reenvios — o segundo recebe 409. A decisão
vai para o log e para a timeline (`delivery_manual`), com quem decidiu.

**`quoted_ok` só com prova.** `unverified` (sem `stanzaId` na resposta) sai com
a versão curta da legenda, porque a requisição foi COM `quoted` — mas
`quoted_ok` fica `false` e o log diz "Citação enviada, mas NÃO confirmada", não
"recusada".

**Falha transitória**: `delivery_status = retrying`, etapa `delivery_retry`. O
laço de reenvio tenta de novo na MESMA solicitação (30 s, 60 s, 120 s… até 5
vezes), citando a mesma mensagem.

**`completed` só depois da entrega.** Enquanto a resposta sobe, a solicitação
fica `processing/replying`. Um reinício nesse meio NÃO simula de novo no
Santander.

**Reinício no meio de um envio.** A linha da saída é gravada como `sending`
ANTES da chamada à API. Ao voltar, o bot decide pelo que está gravado, nunca
por palpite:

* saída que não está `failed`, `unconfirmed` nem `sending` → **entregue** (só
  faltou gravar o desfecho);
* saída em `sending` (a chamada tinha começado) ou `unconfirmed` (a API
  respondeu sem provar) → **incerta**: a mensagem pode ter saído; não reenvia.
  Vale mesmo se a simulação ainda estiver `pending` — saída e simulação são
  gravadas em momentos diferentes, e uma queda entre as duas não pode virar
  reenvio;
* nenhuma saída que possa ter saído → vai para o reenvio.

Só o "Não chegou — reenviar" do painel tira uma saída incerta dessa conta: ela
vira `failed`, com `[conferido no painel: não chegou]` no erro.

**Evidência.** Cada tentativa de envio vira uma linha em `messages`
(`direction='out'`) com `provider`, `attempt`, `origin_message_id`,
`quoted_message_id`, `quote_status`, `wa_message_id` (id devolvido),
`http_status`, `media_id` e `error`. A solicitação guarda o resumo:
`delivery_status`, `quote_status`, `media_status`, `sent_message_id`.

### Diagnóstico da instância

O painel (aba **Status**) e o `/api/health` respondem, sem expor chave nem
token: a Evolution responde? a chave é aceita? a instância existe e está
conectada? há webhook configurado apontando para `/webhook/whatsapp`? e
**quando chegou o último webhook** — o único sinal que prova o caminho de
volta inteiro. É por aí que se responde "está ligado e não responde, por quê?".

### O que ainda precisa ser validado com a Evolution de verdade

O servidor falso responde no formato da v2, mas não é a Evolution. No grupo
de teste, confira em especial:

1. se a Evolution aceita `quoted.key.participant` (se recusar, o fallback
   manda sem citação e o log mostra `A Evolution RECUSOU a citação`);
2. se a resposta de `sendText`/`sendMedia` traz `message.*.contextInfo.stanzaId`
   (se não trouxer, `quote_status` fica `unverified` em vez de `ok` — nunca o
   contrário). Guarde a resposta e rode:

   ```bash
   .venv/Scripts/python.exe ferramentas/conferir_resposta_evolution.py resposta.json --quote ID_DO_PEDIDO
   ```

   Para travar o formato num teste, aponte `EVOLUTION_RESPOSTAS_REAIS` para a
   pasta das capturas (elas ficam fora do git) e rode
   `pytest tests/test_contrato_evolution.py`;
3. se a citação aparece no celular apontando para o consultor certo;
4. se um 400 real traz texto suficiente para a classificação acima acertar —
   o log mostra `Envio recusado pela Evolution [<desfecho>]` com o corpo.

## O que fazer se der errado

Volte para `WHATSAPP_MODE=dom` no `.env` e reinicie. O código antigo continua
inteiro e é o plano de retorno — ele não foi apagado nesta rodada de
propósito.

## Onde está cada coisa

| Arquivo | O que faz |
|---|---|
| `app/whatsapp_port.py` | o contrato que as duas camadas cumprem |
| `app/evolution.py` | envio pela API (HTTP puro), citação conferida e fallback |
| `app/evolution_webhook.py` | leitura do que a Evolution entrega |
| `app/evolution_check.py` | diagnóstico (`python -m app.evolution_check`) |
| `app/renderer.py` | gera e VALIDA o PNG, sem depender do navegador do WhatsApp |
| `app/manager.py` | registro da entrada, entrega com evidência, reenvio |
| `app/whatsapp.py` | a camada antiga (legado); só ganhou a assinatura comum |
| `ferramentas/e2e_simulado.py` | E2E com o `main.py` real, Evolution e agente falsos |

A automação do Santander (`app/simulator.py`, `app/actor.py`, o Arqueiro)
**não muda em nada**. Ela continua no Playwright, no Brave, com thread dona.
