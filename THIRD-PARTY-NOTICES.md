# Avisos de terceiros

## Reset Explode

- Referência de produto: <https://github.com/GustavoReset/ResetExplode>.
- O repositório declara licença MIT e o arquivo LICENSE identifica `Copyright (c) 2026 Gustavo (GustavoReset)`.
- A organização das funcionalidades serviu como referência; nenhum arquivo de código do Reset Explode foi copiado.

## Bibliotecas e utilitários

- O BooM não inclui bibliotecas de terceiros no código nem instala dependências Python via projeto.
- A janela gráfica usa PyGObject/GTK 3 fornecido pela distribuição Linux; a bandeja usa Ayatana AppIndicator quando instalado. O modo de terminal usa `curses` da biblioteca padrão Python.
- `lspci` (pciutils), `sensors` (lm-sensors), `ip` e `ping` podem ser chamados quando disponíveis no sistema. O BooM não inclui nem modifica esses utilitários.
