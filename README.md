# PokéBot

Automação e leitura de memória de alta performance para client de jogo (Tibia / PokeAlliance / PXG).

## Requisitos
- Windows 10/11 x64
- Python 3.11+
- Executar como Administrador (o cliente PokeAlliance roda elevado)
- offsets.json no formato schema v2

## Escolha do Pokémon de cura e skill
Contexto atualizado e pendências: [CONTEXTO_RETOMADA.md](CONTEXTO_RETOMADA.md).
Snapshot dos offsets: [backup_ponteiros](backup_ponteiros/README.md).
A skill é escolhida manualmente entre M1–M5, sem ler habilidades ou
cooldowns da telemetria. O painel ainda não executa cura por Pokémon.

Na aba Combat, o painel Pokémon de cura lista os Pokémon da pokébar
do cliente escolhido e permite escolher manualmente M1, M2, M3, M4 ou M5.
A seleção usa UUID, não nome ou slot: continua identificando o mesmo
Pokémon quando a pokébar muda de ordem e distingue nomes repetidos.
As opções são atualizadas a cada segundo e configurações ficam nas chaves
`healer.heal_pokemon_uuid`, `healer.heal_pokemon_name` e `healer.heal_move`.
Use o botão de salvar configurações ou feche normalmente a GUI para persistir.
A fonte atual é somente leitura de `%LOCALAPPDATA%/OTPbot/telemetry_<PID>.json`,
publicada pelo PKA Tools; não é detecção independente por MemoryReader.
Telemetria ausente/desatualizada desabilita a seleção, sem apagar a escolha.
O usuário informa o slot do move que cura o personagem. A lista fixa não
depende das habilidades reportadas, nem consulta cooldowns. M1–M5 ainda
não são mapeados automaticamente para teclas Windows.
Este painel não aciona troca de Pokémon nem skills; o healer por hotkey
legado abaixo continua separado. A GUI requer PySide6.

## Pré-checagem da memória na GUI
Antes de criar o Engine, a GUI anexa ao PID selecionado e valida player.hp e
player.hp_max no mesmo processo que será entregue aos módulos. Se falhar,
mostra o motivo e não inicia a automação. O log registra PID, base e caminho
absoluto dos offsets; o arquivo padrão é o offsets.json da raiz do projeto.
Ponteiro nulo, leitura ilegível e campo não verificado são diagnósticos distintos.
Os alarmes de morte/conexão só consultam offsets completos e verificados;
estado indisponível permanece desconhecido, sem simular online ou morto.

## Healer por hotkey
O healer usa somente `player.hp` e `player.hp_max`; MP não bloqueia a cura.
Em `config.json`, preserva `hp_percent_threshold`, `hp_hotkey`,
`panic_hp_percent` e `panic_hotkey`, inclusive alterações pela GUI.
`hold_ms` controla apenas a duração da tecla (padrão 50 ms).
`cooldown_ms` controla o intervalo normal; quando ausente, usa
`delay_between_heals_ms` (650 ms na configuração atual; sem ambos, 800 ms).
`emergency_cooldown_ms` controla o intervalo de emergência (padrão 400 ms).
Esta versão cura abaixo do threshold, sem prolongar a cura até
`hp_above_percent`; as opções de MP e `panic_delay_ms` não são usadas.
Os contadores indicam ações aceitas na fila, não cura confirmada no jogo.

## Scan guiado de pokémons
Execute como Administrador: `python -m core.tools.scan_pokemon PokeAlliance_dx.exe`.
Ative os slots solicitados (padrão 2 → 3 → 1 → 2) e pressione ENTER
no terminal após cada troca. A ferramenta não envia teclas ao jogo.
Testa separadamente as hipóteses de índices 0-based e 1-based em int32;
use `--slot-type uint8` para testar bytes. Não presume F1–F6 para selecionar.
`--hp` solicita HP absoluto e máximo do Pokémon em cada etapa; não usa
o HP do personagem nem presume um intervalo de 1 a 999.
As leituras iniciais usam blocos de 4 MB; as demais releem só candidatos.
Com menos de 50 sobreviventes por campo, busca chains automaticamente
(pode demorar). Use `--skip-chains` para fazer somente o scan diferencial.
Salva `pokemon_candidates.json` com PID, histórico, cobertura e candidatos;
não altera `offsets.json` nem marca chains como verificadas. Valide cada
campo durante trocas, sem Pokémon ativo e após reiniciar o cliente.
Um campo pode não existir como índice simples, ou mudar de endereço ao trocar.
O limite de candidatos interrompe explicitamente o scan, sem truncar resultados.

## Walker: ações e gravação de rotas

Cada waypoint contém x, y, z, action e param. A aba Cavebot oferece Walk,
Wait (milissegundos), Say (texto), Safe Spot, BOX e Stair. As três últimas
ações são marcadores passivos, sem automação especial nesta etapa.

Walk aceita uma margem de chegada configurável de 0 a 2 SQM por eixo
(padrão 1), sempre no mesmo andar. Todas as demais ações exigem o tile exato.
A tolerância é salva no perfil da Hunt; perfis antigos usam o padrão 1.
Ao concluir cada waypoint, o Walker libera as teclas e faz uma pausa aleatória
de 50 a 200 ms, controlada por relógio monotônico sem bloquear a thread.
Parar, limpar ou substituir a rota cancela a pausa pendente. Essa variação
não garante prevenção de detecção por sistemas de anti-cheat.

Wait usa uma máquina de estados IDLE/MOVING/WAITING e relógio monotônico,
sem sleep no tick. O botão Parar solicita encerramento sem join na GUI.
Say enfileira Enter, texto via VK/SendInput e Enter, sem clipboard. Comece
com o chat fechado. O retorno true indica aceitação na fila, não confirmação
da mensagem pelo jogo.

A gravação confirma posições estáveis por 300 ms, com relógio monotônico,
e usa intervalo padrão de 3 SQM entre posições confirmadas. Pare por pelo
menos 300 ms nos pontos e curvas que deseja gravar; amostras transitórias
da diagonal são descartadas. Finalizar gravação preserva somente posições
confirmadas. A execução alinha X primeiro, solta A/D e então alinha Y;
não envia duas direções simultaneamente e não substitui um pathfinder.
Iniciar no Mais Próximo
rotaciona a rota sem perder pontos; considera apenas o andar atual.
Detalhes de ownership, threads e testes: [contrato Qt](docs/QT_CONTRACT.md).

### Dashboard e Hunts configuradas

Selecione o processo no cabeçalho fixo da janela (disponível em todas as abas). A conexão e a leitura de
HP/X/Y/Z acontecem em background, sem ligar os módulos nem enviar teclas.
O botão LIGAR BOT / DESLIGAR BOT controla a automação; desligar mantém a
leitura ativa. Falhas de posição removem valores antigos do painel e mostram
Atualizando, sem desligar a rota por uma única amostra inválida. O Walker mantém
a tecla atual por até 500 ms ou dez ticks inválidos; no décimo primeiro tick,
ou ao atingir 500 ms, libera as teclas. A rota fica intacta e retoma a partir
da próxima posição válida. Parada manual e falhas de HP continuam imediatas.
X/Y aceitam 0 a 10000 e Z aceita 0 a 15; o teto XY é conservador e centralizado
em core/position.py, não uma medição do limite real do mapa. O nome do
personagem aparece como Não calibrado enquanto
não houver um leitor de nome validado; nenhum nome é inferido dos offsets XYZ.

Na aba Cavebot, Salvar Nova Hunt grava waypoints, Loop e configuração do
Healer em hunts.json, ao lado de config.json. Carregar Hunt restaura esses
valores sem iniciar a rota. Pare a rota/gravação antes de trocar de perfil.
O arquivo é criado no primeiro salvamento, escrito atomicamente e mantido
fora do Git. Erros de JSON ou disco são exibidos sem sobrescrever o arquivo
anterior. A gravação Wait/Say preserva suas ações e parâmetros.

## Módulos
- **Engine**: Loop principal e processamento assíncrono.
- **Memory**: Leitura de estruturas PE, offsets de memória, coordenadas (X, Y, Z) e status de vida (HP / HP Max).
- **Navigator**: Navegação por waypoints e desvio de obstáculos.
- **Modules**: Healer, Auto Target, Auto Catch, Cavebot, Alarms e Auto Food.
- **GUI**: Interface gráfica integrada.

### Interface e controles

O tema escuro é definido em gui/style.qss. O cabeçalho reúne a lista de
processos, Atualizar Processos e LIGAR/DESLIGAR BOT. Atualizar a lista não
anexa o cliente; escolher um processo inicia apenas a leitura de memória.
Dashboard reúne personagem, HP visual, XYZ, perfil, estado do Healer/Loop e
as últimas cinco mensagens. Os botões de executar/parar rota ficam no Dashboard.

Combate exibe Pokémon de Cura, Pokémon de Caça, Skill de Cura e limite de HP.
Tempos e revive ficam em Avançado; comida e captura em outra seção recolhida.
O teste de cura exige memória válida e executa uma sequência real de teclas.
Cavebot usa tabela de waypoints somente leitura e permite salvar, carregar e
deletar hunts com confirmação. Deletar o perfil preserva a rota atual.

### Ícones e Power UI

Instale as dependências visuais com `python -m pip install -r requirements.txt`. A interface usa ícones
FontAwesome do QtAwesome nas abas, controles de rota, perfis e gravação. O
helper gui/icons.py mantém SVGs locais para falhas de importação ou de fontes,
sem downloads de imagens em runtime. Os rótulos continuam visíveis sem ícones.

O botão mestre usa verde vibrante para iniciar e vermelho para parar. O brilho
fica ativo apenas quando o botão está disponível para iniciar. A barra de HP é
verde acima de 60%, amarela de 20% até 60% e vermelha abaixo de 20%; uma leitura
inválida limpa a barra e o percentual. Selecionar o processo continua iniciando
somente a leitura de memória, sem ligar os módulos.
