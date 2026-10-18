# Contrato Qt — Cavebot / Walker

## Estrutura

A janela principal existente é um QMainWindow. A aba Cavebot é um QWidget
composto por layouts, sem coordenadas absolutas:

- Grupo de diagnóstico: posição e estado do Walker.
- Grupo de entrada: coordenadas, ação, parâmetro, intervalo SQM e gravação.
- Grupo de rota: QListWidget com snapshots dos waypoints.
- Grupo de controle: loop, início, início mais próximo e parada.

Os layouts possuem os widgets. O QTimer tem a aba como parent e é destruído
com ela. Fechar a aba interrompe o timer; exibi-la reinicia o timer. Destruir
a aba solicita a parada da rota, sem esperar por uma thread na GUI. Ao trocar
de Walker, a conexão de limpeza anterior é removida e a rota antiga é parada.

## Estado e threads

Walker é o proprietário da rota, do estado de movimento e da gravação.
Um RLock compartilhado com AutomationModule protege as operações de rota,
ticks, snapshots e mudanças de estado. A GUI usa waypoint_snapshot() para
renderizar cópias, sem iterar a lista que o worker está modificando.

Walker.state expõe IDLE, MOVING ou WAITING. Walker.stats mantém o estado de
ciclo de vida em state (stopped/running/error) e acrescenta walker_state para
o estado de movimento. walker_state também permanece disponível como alias
de state por compatibilidade.

AutomationModule executa ticks numa thread Python e espera pelo próximo tick
com Event.wait. Wait usa time.monotonic e não bloqueia o tick. A GUI chama
request_stop(), sem join; stop(timeout) é a API de encerramento com espera
para o proprietário do worker e para testes.

InputBus é o único proprietário do envio nativo de input. Say enfileira uma
única transação de chat, sem digitar no thread da GUI ou do Walker. A transação
abre o chat com Enter, digita via VK/SendInput e envia com Enter. A conversão
usa VkKeyScanW, ou VkKeyScanExW com o layout do cliente. A parada cancela o
texto pendente; foco perdido interrompe a digitação. A aceitação na fila não
confirma que o jogo processou a mensagem. O chat deve estar fechado no início.

## Estados da interface

- Sem conexão: diagnóstico indisponível.
- Rota vazia: início não cria worker e mostra SEM WAYPOINTS.
- Parâmetro inválido: aviso; a rota não é alterada.
- Gravação: estado GRAVANDO e início desabilitado.
- Movimento: estado MOVENDO.
- Espera: estado AGUARDANDO, sem espera síncrona na GUI.
- Parada solicitada: PARANDO até o worker encerrar; depois PARADO.
- Falha transitória de posição: Atualizando, com tolerância limitada no Walker.
- Falha persistente de posição: teclas liberadas após 500 ms ou 11 ticks.

Safe Spot, BOX e Stair são marcadores passivos nesta etapa. Não iniciam cura,
interações ou mudanças de andar automaticamente. Waypoints de outro andar
não são descartados; a rota aguarda uma posição compatível.

## Validação

tests/test_walker.py cobre relógio monotônico simulado, parada, curvas,
intervalos, mudança de andar, rotação e acesso concorrente à gravação.
tests/test_cavebot_tab.py usa QApplication offscreen e QTest para ações,
parâmetros, rótulos, parada sem join e destruição do timer pelo parent.
tests/test_input_text.py simula o backend Win32 para validar VK, Shift,
Caps Lock, cancelamento, foco e ausência de envio em falhas.



## Dashboard, conexão e perfis

MainWindow mantém uma única seleção de processo no cabeçalho fixo. LiveStatusPanel
exibe conexão, HP e XYZ; o campo de nome indica Não calibrado sem inventar dados.
As estatísticas são secundárias ao painel de leitura. O seletor do Combat fica
oculto na integração; a instância independente do PokemonHealerSelector mantém
sua API. Settings apenas orienta o usuário ao cabeçalho.

MemoryMonitor (QThread com run próprio) abre o processo e publica connected,
snapshot_ready e failed. O loop usa threading.Event.wait(0.1), sem operar
widgets. MainWindow recebe snapshots na thread Qt e limpa valores inválidos.
Apenas selecionar o processo cria um Walker inativo; não inicia Engine/InputBus.
Iniciar rota inicia InputBus explicitamente; o botão global inicia os módulos.
Desligar Engine mantém o monitor ativo e reconstrói um Walker inativo com a rota.
Trocar de processo exige automação/gravação parada; o monitor anterior termina
antes de detach. Fechar a janela solicita parada e verifica finalização por
QTimer, sem join/wait na thread da GUI, antes de liberar os handles.
QObjects de trabalho usam deleteLater ao terminar; timers pertencem à janela/aba.

ProfileManager é proprietário da persistência hunts.json, com validação de
waypoints/configuração, cópias independentes, RLock e escrita temporária seguida
de os.replace. Falhas não destroem o arquivo anterior. CavebotTab usa um único
combo, botões de salvar/carregar/deletar e diálogos modais parent-owned. Trocar perfil chama
replace_route sob o lock do Walker; não recria timers nem liga a rota. O sinal
healer_profile_loaded atualiza configuração e controles com QSignalBlocker.

Gravação exige estabilidade de 300 ms antes de processar intervalo SQM/curvas/Z.
Parar antes dessa confirmação descarta a posição transitória. Movimento usa
somente uma tecla direcional por vez, priorizando X sem tolerância de chegada.
Testes legados de espaçamento usam debounce zero; testes específicos simulam
monotonic para verificar o padrão de 300 ms e descarte de amostras intermediárias.



## Tolerância a falhas de posição

core.position.is_valid_position é a validação compartilhada por Walker,
MemoryMonitor, LiveStatusPanel e perfis. O intervalo XY 0..10000 é um teto
conservador configurado no código, não uma afirmação sobre o tamanho do mapa;
Z aceita 0..15. Sentinelas, None, booleanos e valores não inteiros são rejeitados.
O Resolver bruto mantém sua semântica e evidência diagnóstica sem normalização.

Walker.read_player_position converte exceções de leitura em None. O tick mantém
as teclas somente durante a janela de tolerância: até dez falhas consecutivas,
limitadas também por time.monotonic a 500 ms. Na décima primeira falha ou ao
atingir esse tempo, libera as teclas sem consumir/reorganizar waypoints.
A primeira leitura válida zera o contador/relógio e recalcula a direção a partir
da posição nova, nunca de coordenadas antigas. request_stop e reset da rota
liberam teclas e zeram a tolerância imediatamente, sem esperar o timeout.

MemoryMonitor publica position_valid e hp_valid separadamente, inclusive quando
um campo gera exceção. MainWindow deixa falhas somente de XYZ para o Walker;
não interrompe o Engine, nem desabilita o botão DESLIGAR durante essa tolerância.
Falhas de HP/estado geral continuam solicitando parada imediata. Os painéis
limpam coordenadas inválidas, indicam Atualizando e retornam ao verde quando
uma posição válida chega. Atualizações visuais não incrementam nem reiniciam
o contador do Walker, que pertence exclusivamente aos ticks sob seu RLock.

A origem de 65535/255 no cliente permanece não comprovada. Esse padrão isolado
não prova uma atualização transitória nem equivale a um ponteiro nulo. A
proteção limita o movimento sem visibilidade, mas não valida a cadeia de offsets.


## Redesign visual e navegação

O cabeçalho fixo concentra QComboBox de processos, atualização da lista e o
botão mestre. Enumerar processos ou atualizar a lista não conecta nem inicia
InputBus. A seleção explícita inicia somente MemoryMonitor, reutilizando o
ciclo de conexão/desconexão existente. A enumeração Toolhelp declara tipos de
handle Win32 para não truncar handles em Windows de 64 bits.

Dashboard contém cards de personagem (barra de HP, nome não calibrado e XYZ)
e automação (perfil efetivamente carregado/salvo, Healer, Loop). Os controles
de iniciar/parar rota ficam exclusivamente no Dashboard na janela integrada;
Cavebot mantém edição, gravação e repetição. A instância independente preserva
seus controles de rota. O log rápido retém cinco blocos e escapa HTML. Logs do
teste do Healer cruzam a thread por Signal, sem acessar QTextEdit no worker.

O tema é carregado de gui/style.qss, com cores e estados de foco/hover/disabled.
Botão mestre usa ícones nativos Qt e muda entre verde/vermelho. Configurações
avançadas são CollapsibleSection com widgets parent-owned, sem reconstrução
de controles/timers ao expandir. Combat preserva a API do seletor independente;
na integração mostra quatro campos essenciais e teste, com tempos/revive em
Avançado. Comida e captura permanecem acessíveis em seção recolhida.

Cavebot apresenta QTableWidget somente leitura, preservando seleção e scroll
quando a rota muda. ProfileManager.delete_profile usa a mesma escrita atômica
de save_profile. A GUI exige confirmação para deletar, bloqueia exclusão durante
rota/gravação e preserva os waypoints carregados. Pré-visualizações usam dados
simulados: screenshots não comprovam leitura ou execução no jogo.


## Power UI: ícones, cores e efeitos

Os ícones são centralizados em gui/icons.py com nomes FontAwesome verificados
por testes de renderização. QtAwesome é carregado apenas ao pedir um ícone com
QApplication existente; falhas de fonte/importação usam SVG local via QtSvg.
Sem QApplication, o helper retorna QIcon vazio e nunca cria uma aplicação.
Isso não tenta recuperar uma falha fatal do plugin de display: testes de GUI
usam QT_QPA_PLATFORM=offscreen antes de criar QApplication.

O cabeçalho mantém um único botão mestre. Seu QGraphicsDropShadowEffect pertence
ao botão e é reutilizado nas transições, sem efeitos/timers novos por tick.
O brilho só é habilitado quando o início está disponível, ficando desabilitado
durante execução ou desconexão. Ícone e cor acompanham o estado do botão.
Dashboard usa três SetupStep informativos sem ações ou conexões adicionais.

A propriedade health da barra de HP é good para percentual maior que 60,
warning entre 20 e 60 inclusive, critical abaixo de 20 e unknown para dados
inválidos. O percentual é calculado antes do arredondamento visual. A legenda
percentual fica fora da barra para manter contraste em preenchimentos curtos.
A estilização só é reaplicada ao trocar health. Waypoints exibem ícone da ação
na primeira coluna e tooltip textual; a ordem e os dados da rota não mudam.
