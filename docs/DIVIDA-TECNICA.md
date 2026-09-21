# Dívida técnica

O que se sabe que está frágil, por quê, e o que custaria consertar. Entra aqui
o que foi decidido conscientemente — não o que ninguém olhou ainda.

---

## 1. O modo DOM identifica a conversa pelo NOME do grupo

**Onde:** `app/whatsapp.py` (`ACHAR_CONVERSA_JS`, `_abrir_pela_lista`,
`_garantir_conversa`) e, por consequência, `simulations.chat_id` de toda
solicitação criada em `WHATSAPP_MODE=dom`.

**O que acontece hoje:** o WhatsApp Web não mostra o JID do grupo na lista
lateral. O bot acha a conversa comparando o `title` do item com
`WHATSAPP_GROUP_NAME`, e grava esse nome como `chat_id`. No banco de produção
de 20/09/2026, todas as 19 solicitações têm
`chat_id = "Santander Capital Simulações"` — o nome, não
`120363...@g.us`.

**Por que é risco:**

- nome é mutável e não é único. Dois grupos homônimos — uma cópia, um grupo
  antigo, o grupo de outra empresa com o mesmo nome — são indistinguíveis
  para o bot;
- a conferência `outbound.chat_id == request.chat_id` continua valendo, mas
  compara dois nomes: ela pega "a resposta ia para outra conversa" e não
  pegaria "as duas conversas se chamam igual";
- se o grupo for renomeado, as solicitações antigas ficam com um `chat_id`
  que não corresponde a nada.

**O que já foi feito (20/09/2026):** o risco deixou de ser silencioso. Com
mais de uma conversa de mesmo nome na lista, o bot **não escolhe**: registra
`ERROR` dizendo quantas encontrou e não abre nenhuma. Antes ele abria a
primeira. Teste:
`tests/test_leitura_dom.py::TestAcharConversaNaLista::test_dois_grupos_com_o_mesmo_nome_nao_sao_adivinhados`.

**O que falta (a dívida):** migrar o modo DOM para um identificador estável.
Caminhos possíveis, do mais barato ao mais caro:

1. extrair o JID do `data-id` das mensagens da conversa aberta
   (`false_<chat>@g.us_<msg>_<autor>@c.us` traz o chat no segundo campo) e
   gravar ESSE valor como `chat_id`, mantendo o nome só para exibição;
2. confirmar o JID pelo painel de dados do grupo ao abrir a conversa;
3. migrar de vez para `WHATSAPP_MODE=evolution`, onde o `remoteJid` vem
   pronto no webhook.

O caminho (1) parece o melhor custo/benefício: o dado já está na tela, em todo
`data-id` de mensagem recebida, e não exige interação nenhuma. Exige migração
das linhas antigas (ou conviver com dois formatos de `chat_id` no histórico).

**Enquanto não for feito:** não usar o mesmo nome em dois grupos do WhatsApp
onde o bot opera.
