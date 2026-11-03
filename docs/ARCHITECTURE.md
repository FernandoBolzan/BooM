# Arquitetura BooM Linux

## Aplicação

- Aplicação local em Python 3.10+, sem serviço de rede ou instalação de dependências Python.
- `boom.py` implementa a janela GTK 3, o modo de terminal `curses`, o inventário local e as opções CLI.
- `start-linux.sh` inicia a aplicação. Com display e PyGObject disponíveis, abre a janela GTK; em um terminal sem display, abre o modo interativo.
- `install-linux.sh` instala ou remove um lançador `.desktop` e o ícone do BooM no diretório de dados XDG do usuário; não exige elevação. O atalho inicia a janela e oferece uma ação secundária para executar somente na bandeja.
- PyGObject/GTK 3 é opcional para o modo de terminal e necessário para a janela.

## Leituras do Linux

- CPU: `/proc/cpuinfo`, `/proc/stat` e `os.cpu_count()`.
- Memória e tempo ligado: `/proc/meminfo` e `/proc/uptime`.
- Distribuição e kernel: `/etc/os-release` e `platform.release()`.
- Armazenamento: `df -B1 -P -T`, executado com argumentos fixos.
- Gráficos: `lspci -nn`, quando o `pciutils` estiver instalado.
- Temperaturas: `/sys/class/hwmon`, zonas térmicas do kernel e `sensors -j` opcional do `lm-sensors`.
- Modelo do computador, placa-mãe e BIOS: arquivos legíveis em `/sys/devices/virtual/dmi/id`.
- Módulos de RAM: SMBIOS Type 17 via `dmidecode --type 17`. Se a leitura sem privilégio for negada, o usuário pode acionar explicitamente `pkexec`; a aplicação não eleva privilégios sozinha. Frequência DDR é apresentada em MT/s e, como aproximação do clock, em MHz (MT/s ÷ 2).
- Rede: interfaces por `socket.if_nameindex()` com fallback em `/sys/class/net`; endereços via `ip -j -4 addr show`; rota padrão em `/proc/net/route`.
- Diagnóstico: ping fixo para o gateway detectado e `1.1.1.1`, iniciado só pela tela Rede ou por `--network-test`.

## Gerenciamento local

- Processos: `/proc`; CPU é amostrada durante uma janela curta e RAM vem do RSS. O encerramento exige confirmação e envia somente `SIGTERM` a processos do usuário atual.
- Segurança contra PID reutilizado: o BooM compara `starttime` de `/proc/<pid>/stat` e usa `pidfd` quando disponível. PID 1, BooM, processo pai, processos de root e de outros usuários ficam protegidos.
- Aplicativos: lançadores `.desktop` visíveis em diretórios XDG/Flatpak; a abertura usa `Gio.DesktopAppInfo`, sem interpretar ou executar `Exec` através de shell.
- Instância gráfica: `Gtk.Application` usa nome único via sessão D-Bus. Invocações subsequentes ativam o BooM existente; `--tray` mantém o processo oculto.
- Bandeja: Ayatana AppIndicator anuncia um SNI no painel e exibe CPU, RAM e maior temperatura. Um worker coleta as métricas imediatamente e depois a cada 5 segundos; GTK é atualizado pelo loop principal. Janela fechada fica oculta; “Sair do BooM” termina a instância.
- A bandeja só é usada após confirmar via D-Bus que a sessão publicou `org.kde.StatusNotifierWatcher`. Se o registro inicial falhar, o BooM tenta de novo a cada 500 ms por até 10 segundos; em modo oculto mantém a instância viva durante a espera. Se Ayatana ou o serviço continuar indisponível, abre a janela e informa a causa, evitando um processo escondido sem acesso.
- Inicialização: na primeira abertura gráfica com bandeja disponível, o BooM cria `boom-monitor.desktop` em `$XDG_CONFIG_HOME/autostart` (padrão `~/.config/autostart`). Menu e CLI (`--enable-autostart` e `--disable-autostart`) podem controlar essa entrada; outros lançadores continuam somente para consulta.

## Segurança e falhas

- O BooM não abre portas, não envia dados durante a inicialização e não faz alterações no sistema.
- Consultas de inventário são somente leitura; o diagnóstico ICMP, o encerramento de processo e a ativação do autostart exigem ação explícita; encerramento também pede confirmação.
- Comandos usam listas de argumentos fixos, `subprocess.run` sem shell e timeout. Não há campo para comando ou destino arbitrário.
- Leituras ausentes ou sem permissão aparecem como indisponíveis, sem valores simulados.
- As leituras normais não precisam de privilégios. A leitura opcional de SMBIOS pode solicitar autorização pelo polkit após ação explícita do usuário. O autostart é limitado ao perfil do usuário, sem elevação.

## Limites atuais

- GTK/PyGObject pode precisar ser instalado pela distribuição para abrir a janela gráfica.
- Sensores, GPU e endereços IPv4 dependem do hardware, dos utilitários e das permissões disponíveis.
- Firmware pode omitir o modelo, fabricante ou frequências dos módulos; leitura SMBIOS também depende de `dmidecode` e `pkexec`.
- A exibição do ícone depende de o painel da sessão responder ao protocolo StatusNotifier; em caso de falha, o BooM permanece acessível pela janela.
- A listagem de processos e aplicativos é local à sessão; o encerramento de aplicativos com trabalho não salvo pode perder alterações.
- Não há instalador `.deb`, AppImage ou serviço em segundo plano.
