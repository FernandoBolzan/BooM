# Avisos de terceiros

## Reset Explode

- Referência de produto: <https://github.com/GustavoReset/ResetExplode>.
- O repositório declara licença MIT e o arquivo LICENSE identifica `Copyright (c) 2026 Gustavo (GustavoReset)`.
- A organização das funcionalidades serviu como referência; nenhum arquivo de código do Reset Explode foi copiado.

## Bibliotecas e utilitários

- O BooM não inclui bibliotecas de terceiros no código nem instala dependências Python via projeto.
- A janela gráfica usa PyGObject/GTK 3 fornecido pela distribuição Linux; a bandeja usa Ayatana AppIndicator quando instalado. O modo de terminal usa `curses` da biblioteca padrão Python.
- `lspci` (pciutils), `sensors` (lm-sensors), `ip` e `ping` podem ser chamados quando disponíveis no sistema. O BooM não inclui nem modifica esses utilitários.
- `nvidia-smi` pode ser chamado quando o driver NVIDIA o fornece. O BooM consulta métricas publicadas pelo driver/NVML e não instala nem redistribui o utilitário.
- `smartctl`, fornecido pelo pacote `smartmontools`, pode listar discos durante o inventário. A leitura detalhada é iniciada somente pelo usuário e executa uma chamada fixa `smartctl --json --all` via `pkexec` quando necessário; o BooM não inclui nem instala o pacote.
- As frequências da CPU são lidas das interfaces CPUFreq em `/sys/devices/system/cpu`. `cpuinfo_cur_freq` é preferido quando o kernel/driver oferece uma leitura de hardware; `scaling_cur_freq` é o fallback e pode representar o último estado solicitado pelo driver, não a frequência física instantânea.
