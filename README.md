# BooM

**Monitor de sistema local para Linux**, com interface GTK e painel de terminal.

[![CI](https://github.com/FernandoBolzan/BooM/actions/workflows/tests.yml/badge.svg)](https://github.com/FernandoBolzan/BooM/actions/workflows/tests.yml)
[![Licença MIT](https://img.shields.io/badge/licen%C3%A7a-MIT-green.svg)](LICENSE)
[![Versão](https://img.shields.io/badge/vers%C3%A3o-0.2.2-blue.svg)](CHANGELOG.md)

O BooM reúne informações do computador e ferramentas básicas de manutenção em uma aplicação Linux. A organização das áreas foi inspirada no [Reset Explode](https://github.com/GustavoReset/ResetExplode); a implementação deste projeto é própria e não reutiliza arquivos de código daquele repositório.

> **Versão 0.2.2:** inclui benchmarks locais de CPU/RAM, integração com bibliotecas Steam e refinamentos da interface GTK. Os campos exibidos dependem do hardware, do driver, das permissões e dos serviços disponíveis na distribuição.

## Recursos

- Painel geral de CPU, frequência reportada por núcleo, memória, placa de vídeo, discos, sensores e aplicativos.
- Áreas separadas para Sistema, Memória e Gráficos. Memória mostra uso/disponibilidade de RAM, swap, VRAM por adaptador e especificações físicas por módulo.
- Informações de Linux, kernel, processador, placa-mãe, BIOS e clocks da CPU na área Sistema.
- Detalhes de RAM por módulo quando o firmware fornece os dados: capacidade, tipo, fabricante, part number, tensão e velocidades nominal/configurada.
- Uso de volumes e informações de interfaces e rotas de rede.
- Inventário de discos físicos e consulta SMART somente leitura após ação explícita; a autorização administrativa, quando necessária, ocorre apenas após o clique.
- Estado de bateria/energia pelas interfaces locais do kernel; quando a máquina não tem bateria, o BooM informa isso claramente.
- Perfis de energia oferecidos pelo gerenciador Linux, com confirmação e restauração do perfil anterior; o controle aparece somente quando o kernel e o serviço anunciam suporte. A preferência EPP da CPU é mostrada separadamente, sem escrita direta em `sysfs`.
- Gerenciador de processos com busca por nome/PID e colunas PID/PPID, CPU, memória e estado. **Encerrar processo selecionado** sinaliza somente a seleção; **Encerrar árvore** inclui o selecionado e seus descendentes, dos filhos para os pais. Ambos pedem confirmação, usam `SIGTERM` e protegem processos do sistema, do BooM e de outros usuários; a árvore não inclui filhos criados depois da captura. No TUI, `K` encerra o selecionado e `T`/`t` a árvore.
- Lista e abre aplicativos gráficos. **Programas** desinstala pacotes APT identificados com simulação e confirmação (purga a configuração do sistema, preserva dependências e dados pessoais); para Flatpak, remove o aplicativo e seus dados associados. Origens não identificadas não podem ser desinstaladas pelo BooM.
- **Programas** combina jogos instalados detectados nos manifestos locais do Steam, na base SQLite do Lutris e nos caches e índices locais do Heroic (Epic, GOG, Amazon e jogos adicionados manualmente; instalação nativa ou Flatpak). Também lista programas externos salvos na configuração local dos bottles Bottles, nativos ou Flatpak. A descoberta lê YAML com PyYAML sem executar a CLI do Bottles; nomes duplicados no mesmo bottle ficam ocultos para evitar abrir o item errado. Ao selecionar um item, o BooM encaminha a abertura ao lançador ou bottle correspondente; não acessa contas, baixa nem instala jogos/programas. A CLI oficial pode validar ou manter o bottle antes da abertura, segundo o comportamento do Bottles. A leitura usa o pacote opcional `python3-yaml`, disponível pela instalação assistida. As opções copiáveis `mangohud %command%` e `gamemoderun %command%` ativam, respectivamente, o overlay de FPS compatível e o GameMode quando as ferramentas estão instaladas. No Steam Flatpak, o overlay exige a camada MangoHud compatível dentro do sandbox; o pacote APT atende ao Steam nativo. Se combinar os wrappers e o jogo não abrir, teste cada prefixo separado. O BooM não muda opções salvas no Steam nem estima FPS fora do jogo.
- Consulta de aplicativos iniciados com a sessão; o BooM altera somente a própria entrada.
- Monitoramento de CPU, clocks, RAM e GPUs NVIDIA/AMD/Intel na bandeja e nas telas Visão geral, Sistema, Memória e Gráficos, com atualização a cada cinco segundos enquanto a janela está aberta. NVIDIA inclui utilização de codificador/decodificador; AMD usa DRM/sysfs; Intel i915 usa `intel_gpu_top` quando disponível. O driver Intel Xe mostra apenas métricas que expõe via sysfs.
- Área **Testes** com carga controlada de CPU, RAM e GPU por `stress-ng`, confirmação antes de iniciar, duração de até 120 segundos, cancelamento e parada automática a 85 °C ou se um sensor necessário parar de responder. CPU usa até quatro workers a 80% de carga; RAM usa um worker, até 25% disponível e no máximo 1 GiB; GPU usa um worker no adaptador selecionado pelo endereço PCI e exige temperatura ao vivo e render node DRM acessível.
- Benchmarks opcionais de CPU e RAM pela ferramenta `sysbench`: CPU mede eventos/s por 10 s, com até quatro threads; RAM mede leitura e escrita sequenciais por 5 s cada com buffer privado, de no máximo 256 MiB e 1/16 da memória disponível. Ambos começam somente com temperatura da CPU abaixo de 80 °C, param a 85 °C ou se a leitura térmica falhar, pedem confirmação e podem ser cancelados. São pontuações locais desta execução, não equivalem a FPS nem a uma classificação universal entre CPUs.
- Benchmark de armazenamento com `fio`: após escolher uma pasta e confirmar, mede leitura/gravação sequenciais de 1 MiB e aleatórias de 4 KiB em QD1/QD32. Quatro perfis aleatórios duram 5 s cada; todos usam o mesmo arquivo temporário novo de 128 MiB, removido ao terminar ou cancelar. O fluxo nunca escreve em dispositivos brutos nem altera arquivos existentes; o disco ainda não tem watchdog térmico.
- Leituras de temperatura, ventoinhas, tensão, corrente e potência por `lm-sensors`; o BooM também usa hwmon/sysfs quando `sensors` não está disponível.
- Área Sistema consulta dispositivos e versões de firmware reconhecidos pelo `fwupd`, além das atualizações presentes nos metadados locais. Consultar não baixa catálogo nem instala firmware; se `fwupd` faltar, ele aparece entre as ferramentas opcionais.
- Cartão de atividade de disco com taxa de leitura/gravação, ocupação aproximada e espera média de I/O pelo `/proc/diskstats`.
- Histórico de desempenho local e opcional: amostras agregadas de CPU, RAM/swap, GPU/VRAM, temperaturas e disco, com análise baseada em regras que explica sinais sustentados e suas evidências sem alterar o sistema. A coleta começa só após ativação explícita e não grava nomes de processos, rede ou identificadores.
- Explicação opcional desses diagnósticos com um modelo instalado no Ollama local. O BooM envia somente achados determinísticos e métricas agregadas para `127.0.0.1`, exige histórico suficiente e mostra evidências, riscos e limitações; não usa nuvem, não salva respostas nem aplica ajustes. Se Ollama não estiver instalado, o BooM abre a página oficial de instalação sob ação do usuário.
- Relatório HTML local pela janela ou CLI; não carrega recursos externos, oculta serial/UUID/MAC e identifica que endereços IP e rotas locais podem aparecer.
- Interface em português, com GTK 3 no desktop e `curses` no terminal.

## Requisitos

- Linux e Python 3.10 ou superior.
- Para a janela GTK no Pop!_OS, Ubuntu ou Debian:

  ```bash
  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
  ```

- O painel da sessão precisa oferecer StatusNotifier/AppIndicator para mostrar o ícone. A interface de terminal não depende de GTK.
- `pciutils`, `lm-sensors`, `dmidecode`, `fwupd`, `pkexec`, `smartmontools`, `iputils-ping`, `stress-ng`, `sysbench`, `fio`, `mangohud`, `gamemode`, `python3-yaml` e `nvidia-smi` são opcionais. `python3-yaml` é usado somente para ler os registros locais do Bottles e pode ser instalado pelo fluxo assistido. `sysbench` fornece benchmarks de CPU e RAM; o BooM limita threads, duração e buffer alocado, sem gravar arquivos. `fio` é necessário apenas para o benchmark de arquivo temporário; são exigidos 160 MiB livres e o tempo máximo é 120 segundos. `smartctl` lista discos e só lê atributos SMART depois de ação explícita; o polkit poderá pedir autorização. `fwupdmgr` consulta dispositivos e atualizações já presentes nos metadados locais; o BooM não atualiza catálogo nem instala firmware nessa consulta. Quando disponível, `nvidia-smi` fornece telemetria NVIDIA via NVML, inclusive codificador/decodificador, VRAM, clocks, temperatura, potência, ventoinha e PCIe. Em AMD, o BooM lê os campos publicados pelo driver `amdgpu` no DRM/sysfs e mantém VRAM dedicada separada da memória GTT compartilhada. Em Intel i915, `intel_gpu_top` (`intel-gpu-tools`) acrescenta atividade por motor, clock e potência quando o hardware e as permissões de perf counters permitem; o instalador só oferece esse pacote quando detecta i915. Intel Xe não usa `intel_gpu_top`; ali aparecem somente métricas expostas pelo sysfs. `mangohud` fornece FPS e métricas sobrepostas ao jogo quando o backend gráfico é compatível; `gamemode` solicita o daemon GameMode para a sessão do jogo. Ambos podem ser instalados pelo BooM somente após confirmação. Quando há API CPU/platform profile, `power-profiles-daemon` é opcional e instalado somente após confirmação. Campos ausentes ficam indisponíveis, sem estimativas. O BooM não instala drivers de vídeo.
- `stress-ng` é necessário para os testes de carga de CPU/RAM/GPU. `sysbench` é necessário para pontuar CPU e RAM e pode ser instalado pelo fluxo opcional após confirmação. Nenhuma dessas ferramentas pede privilégios para executar o benchmark/teste. Os testes de carga e benchmarks de CPU/RAM exigem sensor de CPU abaixo de 80 °C; param a 85 °C ou quando a leitura térmica falha. O teste de GPU também exige temperatura ao vivo para a GPU selecionada e um render node DRM acessível. A carga é interrompida a 85 °C, quando um sensor necessário desaparece ou ao completar até 120 segundos. A GPU usa o stressor OpenGL incluído no `stress-ng`; isso é teste de carga/estabilidade, não benchmark de FPS. Os benchmarks de CPU (`sysbench cpu`) duram 10 s, com até quatro threads. RAM usa leitura e escrita sequenciais em passagens de 5 s, com buffer privado limitado a 1/16 da memória disponível e 256 MiB; exige ao menos 512 MiB livres, não toca arquivos nem disputa o benchmark de arquivo `fio`. Instalação manual em Pop!_OS/Ubuntu/Debian: `sudo apt install pciutils lm-sensors smartmontools dmidecode fwupd pkexec iputils-ping stress-ng sysbench fio mangohud gamemode python3-yaml`. Em máquinas Intel com driver i915, `intel-gpu-tools` é opcional para amostrar atividade dos motores gráficos.
- Para explicações por IA, Ollama e ao menos um modelo local são opcionais e instalados separadamente pela distribuição ou pelo canal oficial do projeto. O BooM não executa instaladores ou downloads de modelos por conta própria.
- Se ferramentas opcionais estiverem ausentes, o BooM oferece **Instalar ferramentas ausentes** na área Sistema/Testes (ou `I` no TUI; na lista Steam, `I` também abre esse fluxo). A lista inclui `mangohud` e `gamemode`, além de `python3-gi`, GTK 3 e Ayatana AppIndicator quando a janela ou bandeja não estiver disponível, e `power-profiles-daemon` apenas quando o kernel expõe uma interface compatível e o serviço TLP não está ativo. Após confirmação, o BooM pede autenticação pelo polkit, atualiza os índices do APT e instala somente os pacotes ausentes, inclusive `fwupd` quando necessário. Sem GTK, o BooM continua acessível pelo terminal.

## Instalação e execução

No Pop!_OS, Ubuntu ou Debian, clone o projeto e inicie o BooM:

```bash
git clone https://github.com/FernandoBolzan/BooM.git
cd BooM
./install-linux.sh
./start-linux.sh
```

O instalador cria, sem `sudo`, um lançador chamado **BooM** no menu de aplicativos do usuário e instala o ícone. Depois, você também pode abrir o BooM pela busca do Pop!_OS. Para remover somente o lançador e o ícone instalados pelo BooM, execute `./install-linux.sh --uninstall`.

Para a janela e o ícone da bandeja, instale GTK 3 e Ayatana AppIndicator se a distribuição ainda não os fornecer:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
```

Ferramentas adicionais são opcionais. Na área **Sistema** ou **Testes**, use **Instalar ferramentas ausentes** (tecla `I` no terminal) para revisar os pacotes detectados; a instalação só começa após confirmação e autenticação do sistema. A interface de terminal continua disponível sem GTK.

Sem uma sessão gráfica, abra o painel de terminal:

```bash
python3 boom.py --tui
```

Outros comandos:

```bash
python3 boom.py --version          # versão instalada
python3 boom.py --snapshot          # inventário local em JSON
python3 boom.py --report-html ~/boom-relatorio.html  # relatório local, sem telemetria remota
python3 boom.py --network-test     # ping ao gateway e a 1.1.1.1, sob ação explícita
python3 boom.py --history-enable   # permite gravar métricas agregadas enquanto um monitor BooM estiver ativo
python3 boom.py --history-status   # estado e quantidade de amostras
python3 boom.py --diagnose         # hipóteses locais com evidências (últimos 15 min)
python3 boom.py --history-disable  # pausa novas amostras e mantém as já gravadas
python3 boom.py --history-clear    # pede a confirmação literal APAGAR
./start-linux.sh --disable-autostart
```

Quando a bandeja está disponível, o BooM monitora os recursos em segundo plano. A janela e o terminal interativo atualizam métricas de CPU, memória, GPU e temperatura a cada cinco segundos. A frequência da CPU vem de CPUFreq/sysfs; em alguns drivers, `scaling_cur_freq` é o último estado solicitado e pode diferir da frequência física instantânea. A primeira abertura gráfica com bandeja ativa cria o início automático do usuário. No menu do ícone, **Abrir painel BooM** mostra a janela; fechar a janela a oculta; **Sair do BooM** encerra o processo. O início automático pode ser desativado pelo menu ou pelo comando acima.

Na tela **Inicialização**, o BooM lista os lançadores da sessão. Apenas a entrada `boom-monitor.desktop` é editável; as demais são somente leitura.

Na área **Memória**, RAM, swap e VRAM são exibidas em blocos separados; para AMD, a memória GTT compartilhada também aparece separada da VRAM dedicada. Uso do núcleo ou do motor gráfico, clock, temperatura, potência, ventoinha e link PCIe ficam em **Gráficos**. A Visão geral mantém um resumo desses recursos.

Na área **Testes**, escolha CPU, RAM ou um adaptador gráfico, a duração e a quantidade de workers da CPU. Cada teste solicita confirmação e pode ser cancelado. Os benchmarks de CPU e RAM (`A` e `E` no TUI) usam `sysbench`: CPU mede eventos/s por até 10 s; RAM mede leitura e escrita sequenciais por 5 s cada, com buffer limitado. O teste de GPU associa a placa ao render node DRM pelo endereço PCI e só começa com leituras térmicas válidas de CPU e GPU. O benchmark de disco (`B` no TUI) pede uma pasta existente no volume a avaliar, mostra o caminho e solicita confirmação. O `fio` mede sequencial de 1 MiB e aleatório de 4 KiB, leitura e escrita, QD1 e QD32; os quatro perfis aleatórios duram 5 s cada e o fluxo inteiro para em até 120 s. Tudo ocorre em um arquivo temporário novo de 128 MiB na pasta escolhida; são necessários 160 MiB livres e o arquivo é removido após terminar/cancelar. O resultado mede o caminho do sistema de arquivos, não isola o disco físico; não há acesso bruto nem watchdog térmico do disco. Se `fio` estiver ausente, o botão ou a tecla `B` abre a instalação assistida após confirmação. No TUI, `C`, `M` e `G` iniciam cargas de CPU, RAM e GPU; `[` e `]` trocam o adaptador e `X` cancela o teste ativo.

Em **Sistema**, o BooM mostra a preferência energética EPP reportada por política CPUFreq e, quando `powerprofilesctl` responde, permite aplicar os perfis anunciados pelo gerenciador do Linux. A primeira alteração guarda o perfil anterior em `~/.local/state/boom/power-profile-restore.json` com acesso somente para o usuário; **Restaurar perfil** pede confirmação antes de voltar. O BooM não escreve diretamente em `sysfs`, não cria valores de clock e bloqueia o controle quando detecta TLP ativo. Os efeitos dependem do backend e do hardware.

O histórico pode ser ativado/pausado em **Sistema** (tecla `H` no TUI) e analisado por regras locais em **Sistema** (tecla `D`). Ele guarda amostras a cada cinco segundos somente enquanto o BooM estiver monitorando. A retenção de sete dias é aplicada durante a gravação; ao pausar, as amostras permanecem até retomar ou apagar. O diagnóstico exige pelo menos 12 amostras ao longo de 55 segundos, informa confiança/evidências e apresenta gargalos como hipóteses.

Na interface GTK, **Explicação de gargalos com IA local** permite verificar o Ollama em `127.0.0.1`, selecionar um modelo instalado e pedir uma explicação (no TUI: `O`, `M` e `A` na tela Sistema). A IA só recebe a análise agregada do BooM; não recebe processos, nomes/identificadores de hardware ou a base de histórico. As recomendações são apresentadas para revisão, nunca executadas. O Ollama não vem embutido; se estiver ausente, o BooM mostra a página oficial de instalação. A resposta existe apenas em memória até fechar ou executar outra análise.

## Privacidade e segurança

- As leituras de hardware e sistema são locais; o BooM não envia telemetria nem abre portas de rede.
- O relatório HTML é criado somente após ação explícita, salvo localmente com permissão privada e oculta serial, UUID e MAC. IPs e rotas locais podem aparecer no relatório.
- O teste de rede é iniciado somente por ação explícita e usa destinos fixos.
- Testes de carga são iniciados somente após confirmação, executam `stress-ng` sem privilégios e param em 85 °C, quando a leitura térmica da CPU/GPU necessária falha ou ao atingir o tempo máximo de dois minutos. O BooM não inicia testes de GPU automaticamente.
- O benchmark de disco é iniciado somente após escolha de pasta e confirmação. Ele cria um diretório privado temporário no volume e executa leituras/gravações sequenciais de 1 MiB e aleatórias de 4 KiB (QD1/QD32) somente no arquivo novo de 128 MiB, por até 120 segundos. Remove o diretório quando confirma o encerramento do processo; se o kernel não confirmar imediatamente, preserva o temporário até esse momento. Não escreve em `/dev` nem em arquivos preexistentes; pode aquecer ou ocupar o volume temporariamente e ainda não aplica limite térmico por disco.
- A instalação de dependências opcionais nunca acontece durante a inicialização: a tela lista os pacotes ausentes, pede confirmação e delega a autenticação ao polkit antes de chamar apt.
- A desinstalação de aplicativos exige confirmação. O APT purga apenas o pacote selecionado, após simular e bloquear a operação se outros pacotes também forem removidos; não apaga dados pessoais nem remove dependências compartilhadas. Flatpak apaga também os dados do aplicativo deste usuário. APT pode solicitar autorização pelo polkit.
- O histórico é opt-in, local, guardado com permissões privadas e apagável pelo usuário. `--history-enable` apenas grava a preferência; a coleta só acontece enquanto uma instância BooM monitora o computador.
- A explicação com IA é opcional e usa somente um modelo local do Ollama via loopback. O BooM não usa modelos de nuvem nem transmite métricas para fora; a explicação fica apenas em memória e nunca altera o sistema.
- O encerramento de processos pede confirmação e usa somente `SIGTERM`, sem forçar com `SIGKILL`. O encerramento da árvore atua sobre os processos encontrados na captura, dos descendentes para o processo selecionado; processos protegidos bloqueiam a operação e filhos criados depois da captura podem continuar ativos.
- A autorização administrativa para leitura SMBIOS só é solicitada após ação explícita.
- Consulte [Avisos de terceiros](THIRD-PARTY-NOTICES.md) para a proveniência e as dependências opcionais.

## Desenvolvimento

```bash
python3 -m py_compile boom.py boom_performance.py boom_local_ai.py boom_disk_benchmark.py boom_sysbench.py boom_test_workflow.py boom_games.py tests/test_boom.py tests/test_local_ai.py tests/test_disk_benchmark.py tests/test_sysbench.py tests/test_games.py
python3 -m unittest discover -s tests -v
python3 boom.py --snapshot
```

Veja [CONTRIBUTING.md](CONTRIBUTING.md), [arquitetura](docs/ARCHITECTURE.md) e [política de segurança](SECURITY.md).

## Histórico e licença

As mudanças estão em [CHANGELOG.md](CHANGELOG.md). O BooM é distribuído sob a licença [MIT](LICENSE).
