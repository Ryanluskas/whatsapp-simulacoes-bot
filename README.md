# Allana — Central de Simulações via WhatsApp

Os consultores pedem simulações num grupo de WhatsApp. O bot lê a mensagem,
identifica quem pediu, enfileira, executa a simulação no portal do banco e
responde no mesmo grupo — e tudo isso é acompanhado ao vivo por um painel web.

```
mensagem recebida → consultor identificado → dados validados → fila
   → processando → consultando → resultado → resposta enviada → histórico
```

Cada uma dessas etapas é gravada e publicada em tempo real. O monitor e o
histórico leem a mesma fonte, então painel e banco nunca discordam.

---

## O que o bot faz

| Função | Como funciona |
|---|---|
| **Lê o grupo** | Verifica o WhatsApp Web a cada 3 s. O gatilho é um **CPF válido** na mensagem — não uma palavra mágica, porque é assim que o grupo já escreve. |
| **Identifica o consultor** | Pelo número de telefone de quem enviou, não pelo nome exibido. Cada um tem ficha própria no painel. |
| **Enfileira** | Vários consultores podem pedir ao mesmo tempo; cada pedido é isolado e recebe um `REQ000000`. |
| **Simula** | Abre o portal do Santander no Brave, com a sessão do operador, e usa a lógica do projeto Arqueiro (nada é reimplementado aqui). |
| **Responde** | Manda uma **imagem** com os cards dos contratos e quanto libera, mais um resumo em texto. Se a imagem falhar, o texto sai assim mesmo. |
| **Reenvia** | Só quando a API PROVA que nada saiu (429, 502/503/504, conexão recusada): até 5 vezes **na mesma solicitação**, citando a mesma mensagem. |
| **Não duplica o pedido** | A mensagem recebida é gravada antes de qualquer coisa. O mesmo `message_id` nunca vira segunda solicitação — nem com webhook reentregue, nem depois de reiniciar. |
| **Não duplica a resposta** | 500, timeout depois de enviar, 2xx sem id, queda no meio do envio: a mensagem pode ter chegado. Vira **"Entrega incerta — verificar WhatsApp"** e ninguém manda uma segunda. |
| **Explica os erros** | Traduz o que o portal disse: "não foi possível contatar a averbadora", "matrícula inválida". Erros passageiros geram nova tentativa; erros de cadastro, não. |
| **Registra tudo** | Banco SQLite com mensagens, simulações, consultores e logs. O painel lê daí. |

### O que ele NÃO faz

- Não responde a conversa comum do grupo. Sem CPF válido, ele fica quieto.
- Não reprocessa mensagens antigas: na primeira leitura de um grupo, marca o
  que está na tela como visto e só age no que chegar depois.
- Não responde a si mesmo — duas camadas impedem isso (veja *Diagnóstico*).
- Não encaminha nada. Encaminhar mandaria dados de cliente para outra
  conversa, então há três travas contra isso.

---

## Como iniciar

Dois cliques em **`iniciar.bat`**.

Ele descobre o Python, cria o ambiente virtual, instala as dependências,
baixa o Chromium do Playwright na primeira vez, sobe o sistema, espera o
`/api/health` responder, imprime o estado **real** de cada serviço e abre o
painel no navegador.

Manualmente:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
python main.py
```

Painel em `http://localhost:8000`. A senha é a `DASHBOARD_PASSWORD` do `.env`.

Na primeira execução, abra a aba **Status** e leia o QR Code com o celular.

---

## Formato da mensagem

É o formato livre que o grupo já usa — nome, órgão (opcional) e CPF:

```text
Jose da Silva
Amapá
529.982.247-25
```

```text
MARIA DE SOUZA
11144477735
```

(exemplos com CPFs de teste — este repositório é público)

**O CPF é o gatilho.** Conversa comum não tem CPF; pedido tem. Como a
validação confere os dígitos verificadores, um telefone ou um número de
contrato solto não é confundido com CPF. Não é preciso escrever "fazer
simulação" (mas continua sendo aceito, e `REQUIRE_TRIGGER=true` volta a exigir).

**O contrato não é informado.** Ele é *descoberto* pela simulação e aparece nos
cards do resultado (`7*****56`). O banco é assumido a partir de
`SUPPORTED_BANKS`, já que o grupo é dedicado a um banco só.

O formato rotulado também funciona, para quem prefere:

```text
CPF: 000.000.000-00
Banco: Santander
Contrato: 123456
```

Quem pediu é identificado pelo **número do WhatsApp**, não pelo texto — o nome
escrito na mensagem é o do cliente, não o do consultor.

---

## A resposta no grupo

**Uma mensagem por solicitação.** O bot cita a mensagem do consultor e responde
com uma **imagem** dos cards de contrato — os mesmos campos que hoje eles mandam
como print da tela do Santander — com o valor liberado em destaque e uma legenda
curta:

```
✅ Libera R$ 4.913,52
Jose da Silva
```

Não existe "Simulação recebida", "processando" nem "consulta concluída". O aviso
de fila só sai quando há alguém na frente, porque aí ele informa algo:

```
Ryan, peguei. Tem 2 na frente, já te respondo.
```

Sendo o próximo da fila, o resultado sai em ~90 s e o "recebi" seria a segunda
mensagem onde uma bastava.

### Onde mudar o que o bot fala

Tudo em **`app/mensagens.py`**. Antes os textos estavam divididos entre
`formatter.py` e `cards.py`, e a legenda da imagem acabava falando diferente da
resposta em texto. O tom é **direto, curto, natural** — uma pessoa trabalhando,
não um sistema anunciando etapas.

`tests/test_mensagens.py` falha se alguém reintroduzir "Simulação recebida",
"com sucesso", "Aguarde", mensagens longas demais ou excesso de emoji.

A imagem é **gerada**, não é foto da tela. Os cards do portal são Web Components
em shadow DOM sem seletor estável (o `bot.py` do Arqueiro os lê varrendo texto e
nunca tira screenshot), então um print dependeria de coordenadas fixas e
quebraria na primeira mudança de layout deles. Como os seis campos do card já
são extraídos, a imagem mostra o mesmo conteúdo sem depender de terceiro.

Se qualquer etapa falhar — renderização, anexo, DOM do WhatsApp mudou — o bot
**cai para a resposta em texto sozinho** e registra o motivo nos logs. O
consultor nunca fica sem resposta por causa da imagem.

As imagens ficam em `comprovantes/REQ000182.png` e aparecem no detalhe da
solicitação no painel: é o comprovante exato do que o consultor recebeu.

Duas chaves controlam isso:

| Chave | Efeito |
|---|---|
| `SEND_RESULT_IMAGE` | `false` volta a responder só em texto |
| `IMAGE_SHOW_CLIENT_DATA` | `false` envia o CPF mascarado na imagem |

`IMAGE_SHOW_CLIENT_DATA` é separada de `MASK_CPF_IN_UI` de propósito: o painel e
o grupo do WhatsApp são públicos diferentes.

---

## O painel

| Tela | O que faz |
|---|---|
| **Visão geral** | KPIs do dia, volume e resultado por dia, horários de pico, ranking de consultores, funil operacional |
| **Monitor** | Console ao vivo do bot trabalhando: cada mensagem, etapa e resposta, com filtros e pausa |
| **Fila** | Posição, consultor, cliente, etapa atual, tentativas e tempo em execução |
| **Histórico** | Busca por consultor, CPF, contrato, banco, status, refin, período e ID; timeline completa por solicitação |
| **Relatórios** | Números por período e por consultor, com exportação CSV / Excel / PDF |
| **Consultores** | Cadastro, edição, ativação e desempenho individual |
| **Logs** | INFO / WARNING / ERROR / DEBUG com filtros e busca |
| **Status** | Conexão do WhatsApp, QR Code, simuladores e configuração ativa |

O painel atualiza sozinho por **Server-Sent Events**. Não precisa de F5, e o
monitor reconstrói o histórico recente do banco ao abrir.

---

## Perfis do navegador

O sistema abre **dois** navegadores: um para o WhatsApp Web, outro para o
portal do banco. Cada um precisa do seu próprio diretório de perfil — o
Chromium tranca o diretório, e o segundo navegador a subir no mesmo perfil
morre com `Target page, context or browser has been closed`.

Para que os dois já nasçam logados, com as senhas salvas do seu Brave:

```powershell
# com o Brave FECHADO
.\sincronizar_perfis.ps1
```

Ele copia o seu perfil (sem os caches, ~170 MB cada) para `.simulator-profile`
e `.whatsapp-profile`, incluindo o `Local State` — que guarda a chave sem a
qual as senhas salvas não são decifradas. O perfil anterior é preservado com
sufixo `.bak-<data>`.

As cópias são independentes: o seu Brave do dia a dia continua livre, e nada
que o bot faça mexe na sua navegação. Em compensação elas **não se atualizam
sozinhas** — rode o script de novo depois de trocar a senha do banco.

---

## Docker

O container leva **painel + bot do WhatsApp + fila + banco**. A automação do
Santander **não** entra nele: ela depende de navegador real, perfil já logado e
de você por perto no relogin e no OTP — e Chromium headless em container é o
cenário mais fácil de um portal bancário bloquear.

```
[container]  painel + WhatsApp + fila + banco
     ^  |
resultado |  job
     |  v
[Windows]   agente.py -> bot.py do Arqueiro -> Brave -> Santander
```

**No servidor (ou em qualquer PC com Docker):**

```bash
docker compose up -d
```

Abra `http://localhost:8000`, vá em **Status** e leia o QR Code. A sessão fica
num volume, então não é preciso ler de novo a cada reinício.

**Na máquina Windows onde está o Brave:**

Duplo clique em **`iniciar-agente.bat`**. Ele confere o Python e o
`AGENT_TOKEN` antes de conectar, e lê `PANEL_URL` do `.env` — sem token, para
com a explicação em vez de falhar com 401 lá na frente.

Pela linha de comando dá no mesmo:

```bash
python agente.py --url http://IP_DO_SERVIDOR:8000 --token SEU_AGENT_TOKEN
```

O agente pede trabalho, executa com o `bot.py` do Arqueiro e devolve o
resultado. Fila, tentativas, isolamento por `request_id` e os eventos do
monitor continuam valendo — só mudou *onde* a simulação roda.

Configuração mínima no `.env`:

| Chave | Valor |
|---|---|
| `SIMULATOR_MODE` | `remote` no container, `local` sem Docker |
| `AGENT_TOKEN` | mesmo segredo nos dois lados |
| `PANEL_URL` | endereço do painel, usado pelo agente |

Se o agente não estiver rodando, as simulações expiram com "nenhum agente do
simulador está conectado" e o consultor é avisado — a fila não trava.

### Duas travas que o container liga

**Senha.** No Windows o painel escuta em `127.0.0.1` e só a sua máquina
alcança. O container publica a porta na rede, e aí a senha padrão viraria um
painel com CPF de cliente aberto para quem chegar. Então o sistema **recusa
iniciar** com `DASHBOARD_PASSWORD` padrão quando `WEB_HOST` não é local. Defina
uma senha sua no `.env`.

**Versão do Playwright.** A imagem base traz os navegadores num caminho que
depende da versão (`mcr.microsoft.com/playwright/python:v1.62.0-noble`), então
`requirements.txt` prende `playwright==1.62.0`. Ao trocar um, troque o outro —
o `docker build` falha de propósito se divergirem, em vez de subir e só quebrar
na primeira mensagem.

---

## Arquitetura

```
main.py                 entrada: sobe banco, eventos, manager e painel
iniciar.bat             instalador + inicializador para Windows

app/
  config.py             configuração vinda do .env
  clock.py              tempo: grava em UTC, corta períodos no fuso local
  db.py                 SQLite com WAL, migração automática do schema antigo
  models.py             tipos de domínio e vocabulário de etapas/estados
  events.py             barramento de eventos (broadcast + persistência)
  security.py           sessão assinada, mascaramento, limite de tentativas
  parser.py             leitura da mensagem do consultor
  formatter.py          textos que o bot envia no grupo
  actor.py              modelo de ator — a thread dona do navegador
  whatsapp.py           cliente do WhatsApp Web
  simulator.py          adaptador do bot.py do Arqueiro
  jobs.py               fila persistente com tentativas e recuperação
  consultants.py        identificação e cadastro de consultores
  analytics.py          métricas e relatórios
  manager.py            orquestração
  web.py                API HTTP + stream SSE

  remote.py             simulador remoto (fila aqui, execução no Windows)

mensagens.py            TUDO o que o bot fala no WhatsApp
agente.py               worker do Windows, quando SIMULATOR_MODE=remote
iniciar-agente.bat      lancador do agente no Windows (duplo clique)
Dockerfile              imagem do painel + bot do WhatsApp
docker-compose.yml      volumes, portas e variáveis

dashboard/static/       painel (sem build, sem CDN, funciona offline)
tests/                  175 testes
```

### O caminho de uma mensagem

`_handle_message` é só o orquestrador — cada etapa mora na própria função:

```python
if self._e_mensagem_nossa(...):        # é nossa? ignora (anti-laço)
consultor = self._identificar_e_registrar(...)   # quem enviou
parsed, missing = parse_request(...)             # que dados vieram
if parsed is None and not missing: return        # conversa comum
if missing: self._recusar(...)                   # falta dado
if not bank_supported: self._recusar(...)        # banco errado
request_id, simulation_id = self._gravar_solicitacao(...)
self._enfileirar(...)                            # fila + avisa se esperar
```

O `request_id` nasce em **um lugar só** e acompanha a solicitação até a
resposta: é ele que liga a mensagem do WhatsApp, a linha do banco, os eventos
do painel e a imagem enviada.

### A regra que sustenta tudo

A Sync API do Playwright amarra o navegador a **uma thread**. Por isso cada
navegador vive dentro de um `ThreadActor`: quem está de fora envia um comando
pela caixa de entrada e espera o resultado; a thread dona executa tudo,
inclusive o encerramento. Nenhum objeto do Playwright cruza a fronteira da
thread que o criou.

Violar isso foi o que quebrou a versão anterior — ver `app/actor.py`.

### Isolamento entre solicitações

Cada pedido tem `request_id` próprio (`REQ000182`), gerado por um contador
atômico no banco, e carrega o chat e a mensagem de origem do início ao fim.
A resposta cita a mensagem original e repete consultor + ID no texto. A fila
aceita quantos pedidos simultâneos chegarem; o processamento é serial por
worker, porque a sessão do portal é uma só.

### Preservação da lógica existente

`app/simulator.py` **importa** o `bot.py` do Arqueiro e chama as mesmas
funções (`preencher_formulario`, `clicar_simular_consignado`,
`verificar_refinanciamento`, `_calcular_reducao`, `aguardar_relogin`…).
Nada do cálculo foi reescrito, e nenhum arquivo do projeto Arqueiro é alterado.

---

## Testes

```bash
.venv\Scripts\python.exe -m pytest tests/ -q
```

E o E2E de processo real (o `main.py` de verdade, com Evolution e agente do
Santander falsos, incluindo matar o processo no meio da fila):

```bash
.venv\Scripts\python.exe ferramentas/e2e_simulado.py
```

Ele **não** substitui validar no grupo de teste com a Evolution de verdade —
ver [MIGRACAO-EVOLUTION.md](MIGRACAO-EVOLUTION.md).

São **1003 testes** (mais um que só roda com respostas reais da Evolution
capturadas). Cobrem, entre outros: identificação do consultor pelo
telefone, isolamento de thread do Playwright, execução de 1/2/5 solicitações
simultâneas sem cruzar resultados, tentativas e erros permanentes, recuperação
da fila após reinício, migração do banco antigo, fusos horários, autenticação,
mascaramento de CPF, geração da imagem e a queda para texto quando ela falha.

Os que valem destaque, porque nasceram de defeitos reais em produção:

| Arquivo | O que trava |
|---|---|
| `test_producao.py` | O fluxo de produção na camada Evolution, e **a regra de não duplicar**: 500/timeout/2xx-sem-id viram entrega incerta sem segunda mensagem; 400/422 que apontam o `quoted` viram um único envio sem citação; 429/503 repetem COM citação; queda entre o POST e a gravação não vira reenvio; um `message_id` = uma solicitação; PNG validado e do pedido certo; dois consultores simultâneos sem cruzar |
| `test_contrato_evolution.py` | A leitura da resposta da Evolution: acha `key.id` e `stanzaId` em níveis diferentes, e **nunca inventa um `ok`** quando não acha |
| `test_leitura_dom.py` | Roda o JS num **Chromium de verdade** contra as gerações de HTML que o WhatsApp já serviu: leitura de mensagens, escolha do grupo, menu de contexto, anexo de foto, e a garantia de que o bot **nunca lê nem encaminha as próprias mensagens** |
| `test_abas.py` | A aba certa do navegador. Uma `about:blank` restaurada pelo perfil já fez o bot pilotar uma página vazia a sessão inteira |
| `test_reenvio.py` | Entrega que falhou é reenviada, e o reenvio **não atropela** a entrega em curso |
| `test_erro_portal.py` | O erro do Santander chega traduzido ao consultor, e só o que é passageiro gera nova tentativa |
| `test_docker.py` | Caminho do Windows vazando para o container, versão do Playwright, segredo fora da imagem |
| `test_whatsapp_service.py` | A invariante que sustenta tudo: nada do Playwright fora da thread dona |
| `test_mensagens.py` | O tom das falas e, principalmente, **o que o bot não fala** — falha se voltar "Simulação recebida", "com sucesso" ou mensagem longa demais |

---

## Segurança

- Sessão em cookie `HttpOnly` assinado por HMAC (`SESSION_SECRET`).
- Todas as rotas de dados exigem sessão — **inclusive o stream de eventos**.
- `MASK_CPF_IN_UI=true` mascara o CPF na API, no painel e nos exports.
- CPF é removido das mensagens de log.
- Limite de tentativas de login por IP.
- `.gitignore` bloqueia `.env`, banco, `state.json`, `comprovantes/`,
  `diagnostico/` e os perfis de navegador — os perfis contêm a sessão do
  WhatsApp e do banco, e as capturas de diagnóstico contêm a conversa real.
- **Partida recusada com configuração insegura.** Com `WEB_HOST` fora de
  `127.0.0.1` (o caso do Docker), o sistema não sobe com senha fraca/padrão,
  `SESSION_SECRET` vazio ou placeholder, nem com `EVOLUTION_WEBHOOK_TOKEN` /
  `AGENT_TOKEN` curtos. Em localhost os defaults passam, com aviso — e o log
  diz qual regra está valendo.

---

## Diagnóstico

O bot foi feito para **explicar o que deu errado**, em vez de falhar calado —
uma noite inteira foi perdida com ele "conectado" e sem responder nada, sem
uma linha de log. Toda falha conhecida hoje produz uma mensagem que nomeia a
causa. Estas são as principais, no painel em **Logs**:

| Mensagem | O que significa | O que fazer |
|---|---|---|
| `Código carregado: DD/MM HH:MM:SS` | Sai no boot e diz **qual versão** está rodando | Se o horário for anterior à sua última alteração, reinicie — Python só lê o código no import |
| `Linha de base criada para '<grupo>'` | A leitura funcionou; as mensagens já visíveis foram ignoradas de propósito | Mande um CPF **novo** para testar |
| `Nenhuma conversa aberta na janela` | O bot não conseguiu abrir o grupo | Abra a conversa na janela do bot; ele também tenta sozinho a cada 30 s |
| `O grupo NÃO aparece na lista lateral` | O nome no `.env` não bate com o do WhatsApp | Confira `WHATSAPP_GROUP_NAME` — o log mostra os nomes reais |
| `os seletores não casam mais com o HTML` | O WhatsApp mudou a página | O log traz a contagem de cada seletor; é o dado para corrigir |
| `O navegador estava numa aba em branco` | `about:blank` tinha virado a aba ativa | Corrigido sozinho; se repetir muito, feche o Brave e suba de novo |
| `Não consegui citar por nenhuma via` | Não achou o menu "Responder" | Vem com os ícones e itens de menu reais da sua tela |
| `Não achei o item de foto no menu de anexo` | A imagem vai como arquivo | O log diz se o menu chegou a abrir |
| `A tela de ENCAMINHAR abriu` | Uma trava impediu um encaminhamento | Nada a fazer: nada foi enviado |
| `Ignorando uma mensagem com o formato das nossas respostas` | O bot quase leu a si mesmo | A segunda camada funcionou; avise para investigar a primeira |
| `perfil já está aberto em outro navegador` | Sobrou `brave.exe` de uma execução anterior | Feche todas as janelas do Brave e os `brave.exe` no Gerenciador de Tarefas |
| `O Santander está na tela de login` | A sessão do portal caiu | Faça login na janela do simulador, ou preencha o `credenciais.ini` do Arqueiro |
| `resultado pronto mas não entregue` | A resposta falhou por motivo passageiro | Ele reenvia sozinho (30 s, 60 s, 120 s…), até 5 vezes, na mesma solicitação |
| `A Evolution RECUSOU a citação` | 400/422 apontando o `quoted` | Nada: a resposta saiu sem citação, com `↩ consultor`. Se repetir sempre, a mensagem original não está no histórico da instância |
| `Entrega incerta — verificar WhatsApp` | 500, timeout depois de enviar, 2xx sem id, ou queda no meio do envio | **Olhe o grupo.** A mensagem pode ter chegado; o bot não manda outra de propósito |
| `envio=texto tentativa=1 provider=… quote_status=…` | Uma linha por envio, com id de origem, id citado, autor, HTTP e id enviado | É o suficiente para reconstruir a entrega sem abrir o banco |
| `a entrega falhou de um jeito que repetir não resolve` | 401/403/404, licença, sem `chat_id` | Corrija a configuração da Evolution; o resultado está no painel |
| `já recebida antes ... Reentrega ignorada` | Webhook reentregue | Nada: a trava de duplicidade funcionou |

### Consultando o banco direto

```bash
.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('simulacoes.db'); c.row_factory=sqlite3.Row; [print(r['created_at'], r['level'], r['message'][:90]) for r in c.execute('SELECT * FROM logs ORDER BY id DESC LIMIT 20')]"
```

---

## O DOM real desta instalação

Capturado com `ferramentas/dump_dom.py` em 30/08/2026. **Estes são fatos
observados, não suposições** — e vários contradizem o que a documentação do
WhatsApp Web sugere. Quando eles mudarem, rode a ferramenta de novo e
atualize esta seção.

| O que | Como é aqui |
|---|---|
| `.message-in` / `.message-out` | **Não existem.** Nenhuma linha tem. |
| Quem enviou | `data-pre-plain-text="[20:14, 30/08/2026] Ryan: "` — o nome fica entre `] ` e `: ` |
| Nome do bot no grupo | `Operacional Capital` (`BOT_SELF_NAME`) — vem do **nome do perfil** da conta logada (+55 62 8000-1001), **não** de um autor visível no grupo |
| `data-id` | Vem **pelado**: `2A4813...` recebidas, `3EB065...` enviadas |
| Botão do menu da bolha | `aria-label="Menu de contexto para a mensagem de <quem enviou>"` — carrega o NOME, então a comparação é por **prefixo** |
| Onde clicar na bolha | No **balão**, nunca no centro de `div[role="row"]`: a linha ocupa a largura toda e o centro cai no fundo vazio |
| Menu de contexto (recebida) | **Responder** · Responder em particular · Conversar com _nome_ · Copiar · Reagir · Encaminhar · Fixar · Pergunte à Meta AI · Favoritar · Denunciar · Apagar |
| Menu de anexo | **Documento** · Fotos e vídeos · Câmera · Áudio · Contato · Enquete · Evento · Nova figurinha |
| Input de arquivo | Com a conversa aberta há **um só**: `accept="image/*"`, e já no DOM **sem abrir menu** |
| Compositor | `[contenteditable="true"][data-tab="10"]`, dentro do `footer` |
| Cabeçalho | `document.querySelector('header')` pega o da **lista lateral**. Use `#main header` |
| Menu de contexto, tamanho | O seletor de reações **também** é `role="menu"` e mede **4×1 px**. Exija ≥120×60 |
| Campo da legenda | `aria-label="Digite uma mensagem"` (sem sufixo), `data-tab="undefined"`, **fora** do `footer` e do `#main` |
| Compositor da conversa | `aria-label="Digite uma mensagem para o grupo <nome>"`, `data-tab="10"`, dentro do `footer` |
| Barra de citação armada | `[data-testid="quoted-message"]` **fora** de `div[role="row"]` + botão `aria-label="Cancelar"` |
| Citação no histórico | mesmo `data-testid`, mas **dentro** de `div[role="row"]` — não confundir |
| Fechar a pré-visualização | Escape **não** fecha: abre `"Deseja descartar a seleção?"` (Cancelar / **Descartar**) |
| `img[src^="blob:"]` | **Não** indica preview aberto — imagens já enviadas na conversa também são blob |

### O que cada fato mudou no código

**Autoria** deixou de depender de classe. A cascata agora é: nome do autor →
prefixo `3EB0` → recibo de entrega. O **autor vence o prefixo**, e tem de
vencer: um consultor usando WhatsApp Web também gera ids `3EB0`, e confiar no
prefixo faria o bot ignorar pedidos legítimos.

**No boot** o bot confere o `BOT_SELF_NAME` contra os autores que estão na
tela. Se não bater, sai `ERROR` com a lista de nomes encontrados — melhor
descobrir ali do que quando ele começar a responder a si mesmo.

> **`BOT_SELF_NAME` vem do NOME DO PERFIL da conta logada.** Não é "algum nome
> que aparece no grupo": os autores visíveis numa conversa são os
> **consultores**. Esse engano quase entrou em produção — o nome de uma
> consultora foi lido como assinatura do bot porque ela aparecia como autora
> de um pedido. Com o nome de um consultor ali, o bot passaria a tratar os
> pedidos dessa pessoa como se fossem dele e a **ignorá-los em silêncio**.
> A lista de "autores visíveis" do `ERROR` de boot serve para diagnosticar,
> nunca como fonte do valor.

**A imagem** não passa mais pelo menu de anexo no caminho feliz: colar primeiro,
depois o input com `accept="image/*"`, que já existe. O menu ficou por último
porque "Documento" é o **primeiro item** dele, e clicar ali abre o seletor
nativo do sistema — que trava a automação e entrega um card de download.

**A citação** usa igualdade exata de texto, nunca `includes` — e agora há um
motivo a mais: o menu tem **"Responder"** e **"Responder em particular"** lado
a lado, e o segundo começa com o primeiro. Um `includes` abriria conversa
privada com o consultor, fora do grupo. "Encaminhar" e "Apagar" estão no mesmo
menu, e a busca é escopada ao menu aberto — varrer o documento inteiro pegava
a lista de conversas da lateral.

**Validar a citação** com `ferramentas/testar_citacao.py`: ele roda os seis
passos contra o WhatsApp real, importando o JS de `app/whatsapp.py` (não uma
cópia), e imprime cada passo com screenshot.

### Clicar em coordenada não funciona neste app

Três defeitos diferentes tiveram a mesma raiz: medir o retângulo de um
elemento e depois clicar em `mouse.click(x, y)`. O WhatsApp **anima a entrada**
da setinha de contexto, do diálogo de descarte e de outros controles — entre
medir e clicar, o elemento saiu do ponto. A sondagem que provou isso:
`document.elementFromPoint()` no ponto medido devolvia `div`s anônimos.

A regra: **marcar o elemento no JS e clicar nele com `locator.click()`**, que
espera o retângulo ficar estável e o elemento receber eventos de ponteiro.
Trocar isso levou o serviço real de 0/3 para 10/10 citações.

A exceção é o botão direito na bolha, que precisa mesmo de coordenada — e aí
o alvo é o **balão**, nunca o centro de `div[role="row"]`: a linha ocupa a
largura toda e o centro cai no fundo vazio, onde o botão direito abre o menu
do **grupo**.

### Os laboratórios

| Ferramenta | Responde |
|---|---|
| `testar_citacao.py` | os seis passos da citação, e se ela sobrevive ao anexo |
| `testar_legenda.py` | qual campo recebe a legenda com o preview aberto |
| `testar_envio_real.py` | o `WhatsAppService` inteiro citando; `--completo` roda o envio **sem disparar** |
| `testar_estado_da_conversa.py` | se a linha sai do DOM ao rolar ou ao reabrir a conversa |
| `testar_hover.py` | se a configuração de abertura do navegador afeta o hover |

Todos importam o JS de produção. `testar_envio_real.py --completo` só é
possível porque o disparo mora sozinho em `_disparar_envio` — sem isso,
testar o fluxo completo significaria mandar mensagem para consultores de
verdade.

---

## Duas camadas de WhatsApp

O bot fala com o WhatsApp por uma de duas camadas, escolhida por
`WHATSAPP_MODE` no `.env`:

| Modo | Como funciona | Estado |
|---|---|---|
| `dom` (padrão) | dirige o WhatsApp Web no Brave e lê o HTML da tela | em produção |
| `evolution` | fala a Evolution API (protocolo do WhatsApp direto) | pronto, aguardando validação |

A troca existe porque os três defeitos históricos — não citar a mensagem,
mandar a imagem como documento, falhar sem avisar — nascem todos do mesmo
lugar: **ações de interface executadas no escuro**. Na API eles viram campos
de um JSON (`quoted`, `mediatype: "image"`) e uma resposta HTTP que prova a
entrega.

O resto do sistema não sabe qual camada está em uso. Fila, banco, painel,
parser, consultores e a automação do Santander são idênticos nos dois modos —
o contrato está em [`app/whatsapp_port.py`](app/whatsapp_port.py) e há um
teste que compara as assinaturas das duas implementações.

O modo `dom` **não foi apagado**: ele é o plano de retorno até a Evolution
provar que entrega no grupo real. Para migrar, siga
[MIGRACAO-EVOLUTION.md](MIGRACAO-EVOLUTION.md) — os passos de infraestrutura
(Docker, ativação de licença, leitura do QR) dependem da sua máquina e do
celular do bot.

Diagnóstico da camada nova, o equivalente ao `dump_dom.py` mas que não
depende de HTML nenhum:

```bash
.venv/Scripts/python.exe -m app.evolution_check
```

Ele diz se a Evolution responde, se a licença está ativa, se a instância
conectou, e lista os grupos com seus JIDs.

---

## Por que tanta cascata de seletores

Automatizar o WhatsApp Web é raspar um HTML que a Meta muda sem avisar. Toda
falha desta base teve a mesma forma: **um seletor único, e silêncio quando ele
falhava**. O sistema parecia saudável e não fazia nada.

As regras que saíram disso, e que valem para qualquer mudança futura:

1. **Nunca um seletor só.** Sempre uma cascata, do mais específico ao mais genérico.
2. **Comparar texto em JS, não chutar CSS.** Achar "Responder" ou o nome do
   grupo comparando texto normalizado sobrevive a mudança de classe, de
   `aria-label` e a acento decomposto.
3. **Mirar na linha da mensagem**, não no elemento que carrega o `data-id` —
   o menu de contexto só responde na linha.
4. **Nenhum caminho de falha pode ser silencioso.** Se saiu sem fazer o
   trabalho, tem de dizer por quê.
5. **Testar contra DOM real**, num Chromium de verdade (`tests/test_leitura_dom.py`).

O formato do `data-id` varia entre instalações: em algumas vem
`false_<chat>@g.us_<msg>_<remetente>@c.us`, em outras vem pelado
(`2A729AF702…`). O código aceita as duas formas.

---

## Observações operacionais

- `WORKER_COUNT=1` é intencional. A fila aceita concorrência; o que é serial é
  a execução, porque a automação do Santander usa uma sessão só.
- Na primeira leitura de um grupo o bot marca as mensagens visíveis como
  vistas sem processá-las, para não responder conversas antigas.
- Se o `bot.py` do Arqueiro não for encontrado, o painel sobe assim mesmo e
  informa o problema; as simulações falham com mensagem clara.
- O código da versão anterior está em `_legado/`, só para consulta.
- **Reinicie depois de qualquer alteração no código.** Python lê cada módulo
  uma vez, no import; editar com o sistema no ar não muda nada. O banner mostra
  a versão carregada justamente para tirar essa dúvida.
- Não instale o app do WhatsApp para Windows. Ele é nativo e o Playwright não
  o dirige; além disso, briga pela sessão com a que o bot usa.
- Se o Brave ficar aberto de uma execução anterior, o próximo boot falha com
  "perfil já está aberto". Um perfil do Chromium aceita **um** processo por vez.
- `MAX_ATTEMPTS=3` no `.env` se quiser mais de uma tentativa em erro passageiro.
