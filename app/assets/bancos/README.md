# Logos dos bancos

`santander.svg` e `caixa.svg` vêm do projeto
[bancos-brasil](https://github.com/eduardolecdt/bancos-brasil), de
Eduardo Sites (Lecdt.com), sob **licença MIT** — o texto completo está em
[LICENSE](LICENSE).

Os arquivos foram extraídos de `src/icones.js` e salvos aqui como SVG.
Única alteração: `fill="#FFFFFF"` explícito em cada `<path>`, porque a
biblioteca original herda a cor do CSS de quem a usa, e o card não pode
depender de contexto externo para o logo sair certo.

## Por que estão versionados aqui

O card é gerado **offline**, dentro do navegador que já está aberto. Nada é
baixado em tempo de execução: uma falha de rede não pode transformar o
comprovante do consultor num quadrado vazio. Também não instalamos o pacote
npm — são dois arquivos estáticos, e uma dependência de build inteira para
isso seria desproporcional.

## Cores oficiais

| Banco | Ícone | Fundo |
|---|---|---|
| Santander | `#FFFFFF` | `#EC0000` |
| Caixa | `#FFFFFF` | `#0066A1` |
