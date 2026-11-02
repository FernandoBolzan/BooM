# Avisos de terceiros

## Reset Explode

- Referência de produto: <https://github.com/GustavoReset/ResetExplode>.
- O repositório declara licença MIT e o arquivo LICENSE identifica `Copyright (c) 2026 Gustavo (GustavoReset)`.
- A organização das funcionalidades serviu como referência; nenhum arquivo de código do Reset Explode foi copiado.

## Bibliotecas e utilitários

- O BooM não inclui bibliotecas de terceiros no código nem instala dependências Python via projeto.
- A janela gráfica usa PyGObject/GTK 3 fornecido pela distribuição Linux; a bandeja usa Ayatana AppIndicator quando instalado. O modo de terminal usa `curses` da biblioteca padrão Python.
- `lspci` (pciutils), `sensors` (lm-sensors), `stress-ng`, `ip` e `ping` podem ser chamados quando disponíveis no sistema. `sensors` e `stress-ng` são ferramentas GPL-2+ distribuídas pelo sistema; o BooM as executa como processos separados, sem incluir o código delas nem instalá-las.
- `fio`, do pacote fornecido pela distribuição, é opcional e usado como processo separado para o benchmark sequencial limitado a arquivo temporário. O upstream declara GPL-2.0-or-later; o BooM não inclui o binário e usa uma configuração fixa, argumentos sem shell e um arquivo novo em uma pasta privada escolhida pelo usuário. Consulte [fio COPYING](https://github.com/axboe/fio/blob/master/COPYING).
- `fwupdmgr`, fornecido pelo projeto fwupd e pela distribuição, pode ser consultado para inventariar dispositivos e versões de firmware e listar atualizações nos metadados locais. O BooM não inclui o fwupd, não atualiza os metadados durante a consulta e não aplica firmware por esse fluxo.
- `nvidia-smi` pode ser chamado quando o driver NVIDIA o fornece. O BooM consulta métricas publicadas pelo driver/NVML e não instala nem redistribui o utilitário.
- Em GPUs AMD, o BooM consulta interfaces DRM/sysfs e hwmon do kernel Linux; nenhum utilitário de terceiros é necessário para esse backend. Em Intel i915, o utilitário opcional `intel_gpu_top`, do pacote `intel-gpu-tools`, é usado como processo separado. O pacote é licenciado sob MIT, fornecido pela distribuição, não incluído no BooM e só é sugerido pelo instalador quando i915 é detectado. O kernel/driver controla o acesso a perf counters; BooM não solicita privilégios nem altera essa política. `intel_gpu_top` não oferece suporte ao driver Intel Xe.
- `smartctl`, fornecido pelo pacote `smartmontools`, pode listar discos durante o inventário. A leitura detalhada é iniciada somente pelo usuário e executa uma chamada fixa `smartctl --json --all` via `pkexec` quando necessário; o BooM não inclui nem instala o pacote.
- As frequências da CPU são lidas das interfaces CPUFreq em `/sys/devices/system/cpu`. `cpuinfo_cur_freq` é preferido quando o kernel/driver oferece uma leitura de hardware; `scaling_cur_freq` é o fallback e pode representar o último estado solicitado pelo driver, não a frequência física instantânea.
- `powerprofilesctl`, fornecido por `power-profiles-daemon`, pode ser usado como processo separado para consultar e selecionar apenas perfis anunciados pelo serviço do sistema. O projeto é GPL-3.0, não está incluído no BooM e só é oferecido pelo instalador quando há interface de perfil no kernel e o serviço TLP não está ativo. O BooM guarda o perfil anterior localmente, pede confirmação para aplicar e restaurar, e não escreve diretamente em CPUFreq/sysfs.
- O Ollama é uma dependência opcional separada, não incluída nem instalada pelo BooM. Quando a pessoa solicita uma explicação, o BooM usa a API local em `127.0.0.1:11434` e modelos que o próprio Ollama reporta como instalados localmente; instalação do runtime e dos modelos fica a cargo do usuário e dos termos/licenças de cada projeto.
- Para os testes de carga, o BooM chama somente opções fixas de `stress-ng` (CPU, RAM ou o stressor OpenGL de GPU), sem shell e sem privilégios. O teste GPU usa um render node DRM correspondente ao endereço PCI selecionado e um worker. O binário e seus termos GPL-2+ são fornecidos pelo pacote da distribuição, não pelo BooM.
- O benchmark de disco chama somente o `fio` do sistema com arquivo de configuração fixo. Não aceita destino de dispositivo bruto, não substitui arquivo existente e limita escrita/leitura a um arquivo novo de 128 MiB, removido após o fluxo.
- A instalação assistida atualiza os índices e executa `apt-get install` por `pkexec` para uma lista fixa de pacotes opcionais ausentes, somente após confirmação do usuário; não instala driver NVIDIA.
