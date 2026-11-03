# Histórico de mudanças

Todas as mudanças relevantes do BooM são registradas aqui. As versões seguem
[Versionamento Semântico](https://semver.org/lang/pt-BR/).

## [Unreleased]

Sem mudanças publicadas.

## [0.2.1] - 2026-11-02

Pequena atualização de instalação e integração com a busca de aplicativos Linux.

### Adicionado

- `install-linux.sh` instala e remove, sem privilégios administrativos, o lançador
  **BooM** e seu ícone para o usuário atual.
- O lançador oferece abertura normal da janela e uma ação para iniciar o
  monitoramento oculto na bandeja.
- A CI valida a instalação e remoção do `.desktop` em um diretório temporário.

## [0.2.0] - 2026-11-02

Expansão do BooM para monitoramento detalhado, diagnóstico local e testes com
limites de segurança.

### Adicionado

- Telemetria de CPU com frequência reportada por núcleo e fonte identificada.
- Telemetria de GPU por fabricante: NVIDIA via NVML/`nvidia-smi`, AMD via DRM/sysfs
  e Intel conforme os dados disponíveis no driver. Inclui uso do núcleo e do
  controlador de memória, VRAM usada/total/percentual, clocks, temperatura,
  potência, ventoinha, PCIe e codificador/decodificador quando expostos.
- Áreas dedicadas a **Memória** e **Gráficos**. RAM, swap, VRAM por adaptador e,
  em AMD, GTT compartilhada são apresentadas separadamente.
- Leitura de temperaturas, ventoinhas, tensão, corrente e potência por
  `lm-sensors`, com fallback para `hwmon`/sysfs.
- Área **Testes** com cargas de CPU, RAM e GPU via `stress-ng`, confirmação,
  cancelamento, duração limitada e parada térmica.
- Benchmark sequencial de armazenamento via `fio`, limitado a um arquivo
  temporário novo de 128 MiB no diretório escolhido, com confirmação,
  cancelamento e limpeza. Não escreve em dispositivos brutos ou arquivos existentes.
- Detecção de dependências opcionais ausentes e instalação assistida por APT e
  polkit. O BooM mostra os pacotes, pede confirmação e instala somente os que faltam.
- Inventário local de firmware via `fwupd`, informações de bateria/energia e
  consulta SMART somente leitura após ação explícita.
- Atividade e taxas aproximadas de leitura/gravação por dispositivo inteiro,
  calculadas a partir de `/proc/diskstats`.
- Histórico de desempenho local e opcional, com retenção de até sete dias,
  controles para pausar/apagar e diagnóstico baseado em regras com evidências.
- Explicações opcionais desses diagnósticos por um modelo do Ollama local. A
  análise usa somente achados agregados, apresenta riscos e evidências e nunca
  aplica recomendações nem persiste a resposta.
- Perfis de energia oferecidos por `power-profiles-daemon`, quando compatíveis,
  com confirmação e opção de restaurar o perfil anterior; EPP permanece somente
  leitura.
- Atualizações ao vivo na janela GTK e na bandeja, relatório HTML local e
  dependências GTK/AppIndicator incluídas na verificação assistida.

### Segurança e privacidade

- Instalações, mudanças de perfil, consultas privilegiadas, encerramento de
  processos e testes exigem ação explícita; controles privilegiados passam pelo
  polkit. Nenhuma ferramenta opcional é instalada na inicialização.
- Testes de carga param a 85 °C, se um sensor necessário falhar ou ao atingir
  120 segundos. A carga de CPU é limitada por worker; o uso de RAM é limitado a
  25% da memória disponível e a 1 GiB.
- Histórico desativado por padrão. Os dados permanecem locais, agregados e sem
  nomes de processos ou identificadores de hardware.
- A consulta de firmware usa os metadados já disponíveis e não instala firmware.
  O BooM também não instala drivers de vídeo nem executa ajustes sugeridos por IA.

### Limitações conhecidas

- A disponibilidade e precisão das métricas dependem do hardware, do driver,
  das permissões e das interfaces expostas pelo kernel. Os caminhos AMD e Intel
  têm testes com fixtures, mas ainda precisam de validação em hardware físico.
- Valores CPUFreq identificam a fonte: alguns drivers reportam a frequência
  solicitada, que pode diferir da frequência física instantânea.
- Ocupação e espera de disco são estimativas agregadas e não identificam o
  processo responsável pela atividade.
- O benchmark `fio` mede I/O sequencial pelo diretório e sistema de arquivos
  escolhidos; não mede o dispositivo inteiro nem acesso aleatório. Não houve
  benchmark real de volume durante esta implementação.
- A explicação de IA exige Ollama e um modelo instalados à parte. Ela usa apenas
  `127.0.0.1`; inferência remota não é usada.

## [0.1.0] - 2026-11-01

Primeira versão pública do BooM para Linux.

### Adicionado

- Interface GTK 3 para desktop e interface de terminal com `curses`, inicialmente
  com sete áreas.
- Inventário local de hardware e sistema, inclusive detalhes de módulos de RAM
  quando fornecidos pelo SMBIOS.
- Inventário de discos e rede, com diagnóstico de rede iniciado explicitamente.
- Gerenciador de processos com confirmação, proteção contra reutilização de PID
  e encerramento por `SIGTERM`.
- Inventário e abertura segura de aplicativos a partir de metadados `.desktop`.
- Inventário de inicialização da sessão e controle do início automático do BooM.
- Ícone StatusNotifier/Ayatana com monitoramento em segundo plano.
- Snapshot JSON, testes automatizados, documentação e fluxo CI para Linux.
- Licença MIT.

### Observações

- O ícone depende de um host StatusNotifier/AppIndicator ativo na sessão gráfica.
- Detalhes de hardware variam conforme firmware, sensores e permissões; valores
  indisponíveis são identificados sem estimativas.
- A interação visual da bandeja ainda não foi verificada em todos os ambientes
  de desktop compatíveis.

[Unreleased]: https://github.com/FernandoBolzan/BooM/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/FernandoBolzan/BooM/tree/v0.2.1
[0.2.0]: https://github.com/FernandoBolzan/BooM/tree/v0.2.0
[0.1.0]: https://github.com/FernandoBolzan/BooM/tree/v0.1.0
