# Instalador para Windows

Gera um **`AllanaBot-setup.exe`** que instala o bot e o painel em qualquer PC
com Windows 10/11, sem precisar de administrador.

O executável é pequeno (~1 MB): ele leva o código e baixa o resto na hora da
instalação. **O PC de destino precisa de internet durante a instalação** —
depois disso, o painel funciona offline (nenhum arquivo vem de CDN).

## Para quem vai instalar

1. Rode o `AllanaBot-setup.exe` e confirme.
2. Responda: senha do painel, nome do grupo do WhatsApp e porta.
3. Espere — ele baixa Python (se faltar), as dependências e o Chromium.
4. Abra pelo atalho **Allana Bot** (Menu Iniciar ou Área de Trabalho).

Antes do primeiro pedido de verdade:

- preencha `arqueiro\credenciais.ini` (CPF e senha de acesso ao portal), ou
  defina `SANTANDER_CPF` / `SANTANDER_SENHA` no ambiente;
- na aba **Status** do painel, leia o QR Code com o WhatsApp do bot.

Onde as coisas ficam (tudo dentro de `%LOCALAPPDATA%\AllanaBot`):

| Pasta | O que guarda |
|---|---|
| `dados\` | banco (`bot.db`) e `state.json` |
| `comprovantes\` | imagens enviadas no grupo |
| `perfis\` | sessão do WhatsApp Web e perfil do navegador do portal |
| `.env` | configuração, **com a senha do painel e o SESSION_SECRET desta máquina** |

Desinstalar: **Configurações → Aplicativos → Allana Bot**. Ele pergunta se
apaga os dados; se você disser que não, eles vão para
`Documentos\AllanaBot-dados`.

## Para quem gera o instalador

```powershell
powershell -ExecutionPolicy Bypass -File instalador\montar-setup.ps1
```

Sai em `instalador\dist\AllanaBot-setup.exe` (fora do Git).

O que entra no pacote:

- **o repositório**, via `git archive HEAD` — ou seja, só o que está
  versionado. `.env`, banco, perfis e comprovantes ficam de fora por
  construção, não por lembrança;
- **o código do Arqueiro** (`bot.py`, `gui.py`, `gmail_otp.py`, os `.bat`),
  a partir de uma **lista de permitidos**. Dados de cliente (`clientes.csv`,
  as planilhas, `bot_log.txt`) e credenciais (`credenciais.ini`,
  `credentials.json`) **não entram**.

Duas travas, nesta ordem:

1. **higiene** — CPF de exemplo em comentário vira `000.000.000-00` *na cópia*
   (o arquivo original do Arqueiro nunca é alterado);
2. **conferente** — se sobrar arquivo proibido, ou um número de 11 dígitos
   fora de comentário no Arqueiro, a montagem **para** e diz qual arquivo é.

Opções úteis:

```powershell
# Arqueiro em outro lugar
... montar-setup.ps1 -Arqueiro "D:\projetos\arqueiro"

# pacote sem o Arqueiro (o PC de destino usa SIMULATOR_MODE=remote)
... montar-setup.ps1 -SemArqueiro
```

O `.exe` é gerado com o **IExpress**, que já vem no Windows — nenhuma
ferramenta extra é instalada para compilar.

### O que o instalador faz na máquina de destino

1. procura Python 3.11+ (`py`, depois `python`); se não achar, baixa o
   instalador oficial e instala **só para o usuário**;
2. copia os arquivos para `%LOCALAPPDATA%\AllanaBot`;
3. cria a `.venv` e instala `requirements.txt`;
4. roda `playwright install chromium`;
5. escreve o `.env` com **SESSION_SECRET novo** e a senha que você digitou —
   nenhum segredo viaja dentro do pacote;
6. cria `AllanaBot.cmd`, os atalhos e a entrada em *Aplicativos*.

### Limites conhecidos

- O executável **não é assinado**: o SmartScreen vai avisar que o editor é
  desconhecido (*Mais informações → Executar assim mesmo*). Assinar exige um
  certificado de code signing.
- Um PC por instalação: o bot **trava o banco e o perfil do navegador**, então
  duas cópias apontando para a mesma pasta não sobem juntas — é proposital.
- O WhatsApp precisa de um QR novo em cada máquina; a sessão não é copiável
  junto com o pacote.
- Testado no Windows 10 desta máquina. Em um PC limpo, o passo que mais
  costuma falhar é o download do Python — se falhar, instale o Python 3.11
  manualmente e rode o setup de novo.
