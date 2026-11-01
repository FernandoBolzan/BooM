# BooM

**Monitor de sistema local para Linux**, com interface GTK e painel de terminal.

[![CI](https://github.com/FernandoBolzan/BooM/actions/workflows/tests.yml/badge.svg)](https://github.com/FernandoBolzan/BooM/actions/workflows/tests.yml)
[![Licença MIT](https://img.shields.io/badge/licen%C3%A7a-MIT-green.svg)](LICENSE)
[![Versão](https://img.shields.io/badge/vers%C3%A3o-0.1.0-blue.svg)](CHANGELOG.md)

O BooM reúne informações do computador e ferramentas básicas de manutenção em uma aplicação Linux. A organização das áreas foi inspirada no [Reset Explode](https://github.com/GustavoReset/ResetExplode); a implementação deste projeto é própria e não reutiliza arquivos de código daquele repositório.

> **Estado:** versão inicial para testes. Sensores, detalhes SMBIOS e ícone da bandeja dependem do hardware, permissões e serviços disponíveis na distribuição.

## Recursos

- Painel geral de CPU, memória, discos, sensores e aplicativos.
- Informações de Linux, kernel, processador, placa-mãe, BIOS e gráficos.
- Detalhes de RAM por módulo quando o firmware fornece os dados: capacidade, tipo, fabricante, part number, tensão e velocidades nominal/configurada.
- Uso de volumes e informações de interfaces e rotas de rede.
- Lista de processos com CPU, memória e estado. O encerramento pede confirmação e envia `SIGTERM` apenas para processos do usuário atual que não estejam protegidos.
- Lista de aplicativos gráficos e abertura pelo lançador `.desktop`.
- Consulta de aplicativos iniciados com a sessão; o BooM altera somente a própria entrada.
- Monitoramento de CPU, RAM e temperatura pela bandeja, com atualização a cada cinco segundos.
- Interface em português, com GTK 3 no desktop e `curses` no terminal.

## Requisitos

- Linux e Python 3.10 ou superior.
- Para a janela GTK no Pop!_OS, Ubuntu ou Debian:

  ```bash
  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
  ```

- O painel da sessão precisa oferecer StatusNotifier/AppIndicator para mostrar o ícone. A interface de terminal não depende de GTK.
- `pciutils`, `lm-sensors`, `dmidecode` e `pkexec` são opcionais. Sem eles, os dados correspondentes aparecem como indisponíveis. A leitura SMBIOS pode exigir autorização e só é solicitada quando o usuário aciona essa opção.

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
python3 boom.py --network-test     # ping ao gateway e a 1.1.1.1, sob ação explícita
./start-linux.sh --disable-autostart
```

Quando a bandeja está disponível, o BooM monitora os recursos em segundo plano. A primeira abertura gráfica com bandeja ativa cria o início automático do usuário. No menu do ícone, **Abrir painel BooM** mostra a janela; fechar a janela a oculta; **Sair do BooM** encerra o processo. O início automático pode ser desativado pelo menu ou pelo comando acima.

Na tela **Inicialização**, o BooM lista os lançadores da sessão. Apenas a entrada `boom-monitor.desktop` é editável; as demais são somente leitura.

## Privacidade e segurança

- As leituras de hardware e sistema são locais; o BooM não envia telemetria nem abre portas de rede.
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
