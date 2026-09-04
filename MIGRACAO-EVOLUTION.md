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

**2. Licença.** Da v2.4.0 em diante a instância precisa ser ativada contra o
servidor da Evolution Foundation antes de servir tráfego. Sem isso **todo
endpoint responde 503** e você vai caçar erro de payload que não existe. A
ativação é gratuita e sem limite de instâncias, mas é um passo manual no
`/manager`. A imagem está presa em `v2.4.1` no `docker-compose.yml`.

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

**Abra `http://localhost:8080/manager` e faça a ativação da licença.** Não
pule: sem ela, tudo abaixo responde 503.

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

## O que fazer se der errado

Volte para `WHATSAPP_MODE=dom` no `.env` e reinicie. O código antigo continua
inteiro e é o plano de retorno — ele não foi apagado nesta rodada de
propósito.

## Onde está cada coisa

| Arquivo | O que faz |
|---|---|
| `app/whatsapp_port.py` | o contrato que as duas camadas cumprem |
| `app/evolution.py` | envio pela API (HTTP puro) |
| `app/evolution_webhook.py` | leitura do que a Evolution entrega |
| `app/evolution_check.py` | diagnóstico (`python -m app.evolution_check`) |
| `app/renderer.py` | gera o PNG sem depender do navegador do WhatsApp |
| `app/whatsapp.py` | a camada antiga, intocada |

A automação do Santander (`app/simulator.py`, `app/actor.py`, o Arqueiro)
**não muda em nada**. Ela continua no Playwright, no Brave, com thread dona.
