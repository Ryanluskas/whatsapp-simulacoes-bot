# Changelog

Mudanças que alguém operando o bot precisa saber. Formato inspirado em
[Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/); versões seguem
[SemVer](https://semver.org/lang/pt-BR/) enquanto o projeto está em `0.x`:

- **MINOR** (`0.2.0`) — comportamento novo ou mudança de comportamento;
- **PATCH** (`0.1.1`) — correção sem mudar o que o operador vê.

Cada versão é uma **tag anotada** (`v0.1.0`) num commit da `main`, criada
depois do merge. Mudança nova entra primeiro em **[Não lançado]**, no mesmo
PR que a introduz.

## [Não lançado]

PR #1 — camada Evolution pronta para o teste real. **Ainda não validado com
Evolution, WhatsApp e Santander de verdade** (ver `ROTEIRO-TESTE-REAL.md`).

### Entrega

- A resposta sai pela Evolution API (`WHATSAPP_MODE=evolution`), citando o
  pedido com o id, o autor e o texto gravados no banco. O modo `dom` continua
  funcionando e é o padrão.
- Um `message_id` vira uma única solicitação, inclusive com webhook repetido,
  em paralelo ou depois de reiniciar.
- **Nunca duas respostas:** 500, 502, 504, timeout de leitura, connection reset,
  2xx sem `key.id` e queda no meio do envio ficam como **entrega incerta**
  (`unconfirmed`) e não são reenviados sozinhos.
- Só 408, 429, 503 e falha antes de conectar repetem a mesma requisição.
- Citação recusada (400/422 apontando o `quoted`) vira um único envio sem
  citação, terminando em `↩ <consultor>`.
- Cada resposta da Evolution é classificada num lugar só, com categoria
  explícita (QUOTE_REJECTED, VALIDATION_REJECTED, POST_SEND_ERROR, TRANSIENT,
  PERMANENT, UNCERTAIN).
- `quoted_ok` só é verdadeiro com prova da citação (`stanzaId` igual ao pedido).
- Reinício em qualquer ponto da entrega decide pelo que está gravado: não
  simula de novo no Santander e não duplica no WhatsApp.

### Painel

- Entrega incerta mostra **Chegou no grupo** / **Não chegou — reenviar**.
  "Não chegou" vale uma vez por solicitação; quem decidiu e quando ficam
  gravados.
- Aba Status e `/api/health` mostram o diagnóstico da Evolution (alcance,
  chave, instância, webhook, último webhook) sem segredos.

### Segurança

- O sistema se recusa a subir exposto na rede com senha fraca, `SESSION_SECRET`
  vazio/placeholder/curto ou tokens curtos.
- **`DASHBOARD_PASSWORD` vazio não abre o painel** (antes a senha vazia valia).
  `.env.example` não traz mais senha nem segredo de exemplo.
- `IMAGE_SHOW_CLIENT_DATA=false` tira da imagem nome, CPF e número de contrato,
  e desliga o print do portal.
- Print do portal é descartado quando o que aparece na imagem pode não ser o
  texto conferido (fora da janela, cabeçalho fixo por cima, texto grande
  demais).
- `diagnostico/` e `evolution-respostas/` fora do Git; dados pessoais reais
  removidos do histórico publicado.

### Ferramentas

- `python -m app.evolution_diagnostico` — estado da Evolution real, sem chave,
  token, JID ou telefone.
- `ferramentas/observar_entrega.py` — o que o banco diz de uma solicitação, só
  com campos seguros.
- `ferramentas/e2e_simulado.py` — E2E de processo real com Evolution e
  Santander falsos.

### Corrigido

- A imagem da Evolution no `docker-compose.yml` apontava para uma tag
  inexistente (`v2.4.1`); agora `v2.3.7`.
- **Modo `dom`, resposta duplicada:** comando do navegador que estourava o
  tempo ainda na fila rodava depois (imagem chegando minutos após o texto);
  agora é cancelado. Falha depois do clique em "enviar" deixou de mandar o
  texto por cima: vira entrega incerta, salvo prova de que nada saiu.
- **Modo `dom`, pedido perdido:** a leitura marcava a mensagem como vista antes
  de gravá-la; uma queda com o pedido na fila o perdia. Agora grava primeiro.
- **Dois processos no mesmo banco:** a trava era só por perfil do navegador;
  agora também por `DB_PATH`, antes de qualquer `recover()`.
- **Falso "entregue" na Evolution:** 2xx com `key.id` e `status: "ERROR"` vira
  entrega incerta.
- Reenvio que parava no meio ficava `pending` até o próximo reinício; o laço
  de reenvio agora o reavalia pelo que está gravado.
- Vigia da fila: a foto do que está em execução passou a ser tirada antes da
  consulta das linhas presas.
- Timers de nova tentativa não se acumulam mais em memória.
- O log de envio mascara o telefone do participante.

## [0.1.0] — 2026-09-16

Primeira versão marcada: o bot em produção no modo `dom` (WhatsApp Web).

### Adicionado

- Leitura dos pedidos no grupo, simulação no portal do Santander e resposta
  com card de resultado.
- Recorte da tela do portal como imagem da resposta
  (`IMAGEM_DA_RESPOSTA=portal`) e correção da conferência da barra de citação
  no WhatsApp Web.

[Não lançado]: https://github.com/Ryanluskas/whatsapp-simulacoes-bot/compare/v0.1.0...claude/whatsapp-santander-bot-prod-96a55e
[0.1.0]: https://github.com/Ryanluskas/whatsapp-simulacoes-bot/releases/tag/v0.1.0
