# BooM

**Monitor de sistema local para Linux**, com interface GTK e painel de terminal.

[![CI](https://github.com/FernandoBolzan/BooM/actions/workflows/tests.yml/badge.svg)](https://github.com/FernandoBolzan/BooM/actions/workflows/tests.yml)
[![Licença MIT](https://img.shields.io/badge/licen%C3%A7a-MIT-green.svg)](LICENSE)
[![Versão](https://img.shields.io/badge/vers%C3%A3o-0.1.0-blue.svg)](CHANGELOG.md)

O BooM reúne informações do computador e ferramentas básicas de manutenção em uma aplicação Linux. A organização das áreas foi inspirada no [Reset Explode](https://github.com/GustavoReset/ResetExplode); a implementação deste projeto é própria e não reutiliza arquivos de código daquele repositório.

> **Estado:** versão inicial para testes. Sensores, detalhes SMBIOS e ícone da bandeja dependem do hardware, permissões e serviços disponíveis na distribuição.

## Recursos

- Painel geral de CPU, frequência reportada por núcleo, memória, placa de vídeo, discos, sensores e aplicativos.
- Áreas separadas para Sistema, Memória e Gráficos. Memória mostra uso/disponibilidade de RAM, swap, VRAM por adaptador e especificações físicas por módulo.
- Informações de Linux, kernel, processador, placa-mãe, BIOS e clocks da CPU na área Sistema.
- Detalhes de RAM por módulo quando o firmware fornece os dados: capacidade, tipo, fabricante, part number, tensão e velocidades nominal/configurada.
- Uso de volumes e informações de interfaces e rotas de rede.
- Inventário de discos físicos e consulta SMART somente leitura após ação explícita; a autorização administrativa, quando necessária, ocorre apenas após o clique.
- Estado de bateria/energia pelas interfaces locais do kernel; quando a máquina não tem bateria, o BooM informa isso claramente.
- Lista de processos com CPU, memória e estado. O encerramento pede confirmação e envia `SIGTERM` apenas para processos do usuário atual que não estejam protegidos.
- Lista de aplicativos gráficos e abertura pelo lançador `.desktop`.
- Consulta de aplicativos iniciados com a sessão; o BooM altera somente a própria entrada.
- Monitoramento de CPU, clocks, RAM, GPU NVIDIA e temperaturas na bandeja e nas telas Visão geral, Sistema, Memória e Gráficos, com atualização a cada cinco segundos enquanto a janela está aberta.
- Relatório HTML local pela janela ou CLI; não carrega recursos externos, oculta serial/UUID/MAC e identifica que endereços IP e rotas locais podem aparecer.
- Interface em português, com GTK 3 no desktop e `curses` no terminal.

## Requisitos

- Linux e Python 3.10 ou superior.
- Para a janela GTK no Pop!_OS, Ubuntu ou Debian:

  ```bash
  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
  ```

- O painel da sessão precisa oferecer StatusNotifier/AppIndicator para mostrar o ícone. A interface de terminal não depende de GTK.
- `pciutils`, `lm-sensors`, `dmidecode`, `pkexec`, `smartmontools` e `nvidia-smi` são opcionais. `smartctl` lista discos e só lê atributos SMART depois de ação explícita; o polkit poderá pedir autorização. Quando disponível, `nvidia-smi` fornece telemetria NVIDIA via NVML: uso do núcleo e do controlador de memória, VRAM usada/total e percentual, temperatura, clocks, potência, ventoinha e link PCIe. Sem ele, a GPU ainda pode aparecer no inventário PCI, mas suas métricas ao vivo ficam indisponíveis. A leitura SMBIOS pode exigir autorização e só é solicitada quando o usuário aciona essa opção.

## Executar

Clone ou baixe o repositório, entre na pasta e inicie o desktop:

```bash
./start-linux.sh
```

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
./start-linux.sh --disable-autostart
```

Quando a bandeja está disponível, o BooM monitora os recursos em segundo plano. A janela atualiza as métricas de CPU, memória, GPU e temperatura a cada cinco segundos enquanto está aberta. A frequência da CPU vem de CPUFreq/sysfs; em alguns drivers, `scaling_cur_freq` é o último estado solicitado e pode diferir da frequência física instantânea. A primeira abertura gráfica com bandeja ativa cria o início automático do usuário. No menu do ícone, **Abrir painel BooM** mostra a janela; fechar a janela a oculta; **Sair do BooM** encerra o processo. O início automático pode ser desativado pelo menu ou pelo comando acima.

Na tela **Inicialização**, o BooM lista os lançadores da sessão. Apenas a entrada `boom-monitor.desktop` é editável; as demais são somente leitura.

Na área **Memória**, RAM, swap e VRAM são exibidas em blocos separados. Uso do núcleo gráfico, clock gráfico, temperatura, potência e link PCIe ficam em **Gráficos**. A Visão geral mantém um resumo desses recursos.

## Privacidade e segurança

- As leituras de hardware e sistema são locais; o BooM não envia telemetria nem abre portas de rede.
- O relatório HTML é criado somente após ação explícita, salvo localmente com permissão privada e oculta serial, UUID e MAC. IPs e rotas locais podem aparecer no relatório.
- O teste de rede é iniciado somente por ação explícita e usa destinos fixos.
- O encerramento de processos pede confirmação, usa `SIGTERM` e protege processos do sistema e de outros usuários.
- A autorização administrativa para leitura SMBIOS só é solicitada após ação explícita.
- Consulte [Avisos de terceiros](THIRD-PARTY-NOTICES.md) para a proveniência e as dependências opcionais.

## Desenvolvimento

```bash
python3 -m py_compile boom.py tests/test_boom.py
python3 -m unittest discover -s tests -v
python3 boom.py --snapshot
```

Veja [CONTRIBUTING.md](CONTRIBUTING.md), [arquitetura](docs/ARCHITECTURE.md) e [política de segurança](SECURITY.md).

## Histórico e licença

As mudanças estão em [CHANGELOG.md](CHANGELOG.md). O BooM é distribuído sob a licença [MIT](LICENSE).
