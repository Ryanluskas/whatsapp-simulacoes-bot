# AGENTS.md — regras para agentes de IA neste repositório

Vale para qualquer agente (Claude, Copilot, Antigravity, Codex, Cursor…).
Responda e documente em **português**. O `README.md` explica o produto; este
arquivo diz o que um agente **não pode quebrar**.

## O que é

Bot que lê pedidos de simulação num grupo de WhatsApp, simula no portal do
Santander (Playwright) e responde no grupo **citando o pedido**. Python 3.11,
FastAPI + SSE no painel, SQLite (WAL). Duas camadas de WhatsApp com o mesmo
contrato (`app/whatsapp_port.py`): `dom` (navegador, `app/whatsapp.py`) e
`evolution` (API, `app/evolution.py`), escolhidas por `WHATSAPP_MODE`.

| Onde | O quê |
|---|---|
| `app/manager.py` | orquestra: entrada, entrega, reenvio, decisão manual de entrega incerta |
| `app/jobs.py` | fila, recuperação após reinício, `situacao_do_envio` |
| `app/evolution.py` | cliente da Evolution: classificação HTTP, citação, prova de entrega |
| `app/models.py` | `Delivery`, `QuoteStatus`, `Desfecho`, `ResultadoEnvio` — leia antes de mexer em entrega |
| `app/security.py` | sessão HMAC, máscaras, regra de partida com configuração fraca |
| `dashboard/static/js/` | painel (sem framework; DOM só por `h()`, nunca `innerHTML` com dado de terceiro) |
| `ferramentas/e2e_simulado.py` | E2E de processo real com Evolution e Santander falsos |

## Regras que não se negociam

1. **Nunca duas respostas no grupo.** A pergunta de todo envio é: *a mensagem
   pode ter saído?* Se puder (500, timeout de leitura, 2xx sem `key.id`, queda
   com a linha `sending` gravada), o desfecho é `unconfirmed` e **ninguém
   reenvia sozinho** — nem sem citação, nem em texto, nem pelo laço. Só uma
   pessoa desempata, no painel (`POST /api/simulations/{id}/entrega`).
2. **Entrega e citação são perguntas separadas.** `delivery_status` diz se
   chegou; `quote_status` diz o que houve com a citação. `quoted_ok=True` e
   `quote_status=ok` só com **prova** (`stanzaId` igual ao id pedido). Sem
   prova é `unverified` — nunca `ok`.
3. **Identidade vem do banco, não da tela nem da RAM.** Um `message_id` = uma
   solicitação. Citação usa o id, o autor (`participant`) e o texto gravados.
4. **Grave antes de agir.** A saída é gravada como `sending` antes da chamada
   à API; a decisão depois de uma interrupção lê o banco, não adivinha.

## Dados e segredos (o repositório é PÚBLICO)

- **Nada real em código, teste, comentário ou doc:** CPF, nome de cliente, de
  operador ou de empresa, telefone, JID, id de mensagem, token de página.
  Use dados sintéticos: CPFs de teste `529.982.247-25`, `111.444.777-35`;
  nomes como "Cliente Teste".
- Nunca versione: `.env`, `*.db`, `state.json`, `comprovantes/`,
  `diagnostico/`, `evolution-respostas/`, perfis de navegador, logs. O
  `.gitignore` já bloqueia; não o afrouxe.
- Log e relatório só com valor **mascarado** (`123.***.***-09`,
  `eyJhbGci...REDACTED`). Nunca chave de API, senha, token ou credencial do
  Santander.
- Não cole conteúdo de `diagnostico/`, do banco ou de logs reais em issue, PR,
  prompt de outra IA ou serviço externo.

## Git

- Não faça push em `main`, não faça merge de PR e não use `--force`: isso é
  decisão humana. Se reescrever histórico for inevitável, prepare com
  `--force-with-lease=<ref>:<sha>` e peça autorização.
- Commits pequenos, mensagem em português dizendo o **porquê**.
- Não apague backup, branch ou worktree de outra pessoa.

## Testes

```bash
.venv/Scripts/python.exe -m pytest -q
```

```bash
.venv/Scripts/python.exe ferramentas/e2e_simulado.py
```

- Toda correção de comportamento vem com teste que **falharia sem ela**.
- Não enfraqueça um teste para ele passar. Se a expectativa antiga estava
  errada, diga no teste por quê.
- Há testes de tamanho de função (`tests/test_mensagens.py`): quebre em
  funções menores em vez de subir o limite.
- Ao reportar, separe **TESTE UNITÁRIO**, **TESTE DE INTEGRAÇÃO SIMULADO** e
  **TESTE REAL PENDENTE**. Teste com servidor falso não valida a Evolution
  nem o Santander de verdade; não diga "validado" sem o teste real.

## Estilo

- Siga o código ao redor: comentários de código em português sem acento
  (`nao`, `e'`), textos para pessoas (log, painel, WhatsApp) com acento.
- Comente o **porquê** e o defeito que motivou, não o óbvio.
- Mudança pequena e localizada; não reescreva módulo que não foi pedido.
