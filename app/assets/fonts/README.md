# Fonte Inter

[Inter](https://github.com/rsms/inter), de Rasmus Andersson, sob
**SIL Open Font License 1.1** — texto completo em [LICENSE.txt](LICENSE.txt).

Três pesos, em `.woff2`: Regular (400), SemiBold (600), Bold (700).

## Por que embutida

O card é embutido no HTML como `@font-face` com `data:` URI. Depender da
fonte instalada na máquina faria o mesmo comprovante sair diferente no
Windows do operador e no container Linux — larguras diferentes, quebras de
linha diferentes, tabela desalinhada. O comprovante do consultor tem de ser
o mesmo em qualquer lugar.

`.woff2` e não `.ttf`: mesma renderização, ~3x menor, e o card é gerado
dentro do Chromium, que lê woff2 nativamente.
