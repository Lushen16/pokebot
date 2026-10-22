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
interações ou mudanças de andar automaticamente. Walk, BOX, Safe Spot e Wait
ignoram Z no movimento, na gravação e na escolha do ponto mais próximo.
Stair alinham X/Y exatos e aguardam o Z configurado antes de concluir.
A tabela começa sem linhas quando a rota está vazia. Inclusão manual lê Z da
memória ou usa 7; leitura inválida em Usar Posição Atual não cria coordenadas
fictícias. Exclusão de uma linha usa Walker.remove_waypoint sob RLock, somente
com rota e gravação paradas, e atualiza o snapshot e a numeração da tabela.

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

Gravação contínua processa intervalo SQM/curvas sem exigir parada no tile.
O filtro record_stable_ms é opcional (padrão 0); se configurado, parar antes
da confirmação descarta a posição transitória. Z só participa da estabilidade,
deduplicação e mudança de andar para Stair. Movimento usa
somente uma tecla direcional por vez, priorizando X. Walk aceita a tolerância
configurada (0..2 SQM por eixo); demais ações exigem X/Y exatos.
Testes usam as opções padrão para verificar gravação durante movimento contínuo.
Testes específicos simulam monotonic para verificar o filtro opcional de 300 ms.
A GUI informa quando a gravação aguarda posição válida, sem criar outro timer.



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


## Entrega de movimento

Dashboard e Engine repassam cavebot.walk_keys para Walker; o padrão isolado
continua WASD. O envio usa apenas um eixo de cada vez. InputBus mantém
solicitações KEY_DOWN pendentes separadas dos scancodes efetivamente enviados.
Walker consulta movement_key_active antes de manter seu estado local: uma
solicitação descartada por foco ou erro permite nova tentativa, enquanto uma
solicitação pendente não é duplicada. Encerrar o worker limpa a fila pendente,
libera as teclas e restaura _running=False para que start possa reiniciá-lo.
Testes usam backend Win32 simulado; não comprovam aceitação pelo jogo.


## Retentativas e progresso

O lock da rota protege os contadores de retentativa e watchdog. A GUI lê
posição para diagnóstico sem alterar prazos de retentativa do tick. Somente
_read_route_position agenda a próxima tentativa, usando monotonic e lotes de
cinco leituras com intervalos 50/100/200/400 ms. read_player_position retorna
imediatamente após uma falha e preserva a razão do primeiro campo ilegível;
não confunde uma coordenada zero com um ponteiro nulo já rejeitado pelo resolver.
A falha persistente libera as teclas em 500 ms ou 11 ticks sem consumir pontos.

O watchdog compara apenas X/Y válidos com movimento solicitado. Após 2 s sem
mudança, libera teclas e usa _advance_waypoint, preservando a semântica de Loop.
Esperas intencionais e falhas de memória não contam como posição imóvel.
Parada, nova rota e avanço reinicializam o relógio de progresso.

CavebotTab reutiliza seu QTimer parent-owned de 100 ms; não cria outra thread
nem timers por waypoint. O alerta de 5 s só observa uma rota ativa, distingue
memória indisponível de waypoint/andar pendente e respeita wait_until. Cada
episódio gera um log; recuperação limpa o aviso e a cor sem reiniciar workers.

## Settings e atalhos globais

A aba Settings persiste pause/minimize/restore em settings.hotkeys. Os campos
usam HotkeyEdit; vazios desativam a ação. Salvar registra as combinações pelo
RegisterHotKey com MOD_NOREPEAT e recebe WM_HOTKEY na thread da GUI. Não há
polling de teclado, hook nem envio de teclas ao jogo para esses atalhos.
Combinações duplicadas são rejeitadas antes de alterar registros; conflito
com o Windows desfaz registros parciais e tenta restaurar os anteriores.
Os registros são liberados no início do fechamento da janela.

Pausar/retomar não inicia automação parada. A pausa atinge o contexto dos
módulos e o Walker isolado, libera suas teclas e preserva a fila. A pausa
manual é independente da pausa por combate. Minimizar não pausa; restaurar
mostra a instância existente sem iniciar Engine ou rota. Testes usam backend
simulado e mensagens nativas sintéticas, sem enviar comandos ao jogo.

A captura e os parsers de Settings, Target e Healer compartilham core.hotkeys.
Pontuação (incluindo = e -), navegação e Num0..Num9/NumPlus/NumMinus usam VK,
não o código ASCII do símbolo. Plus evita ambiguidade com o separador de
modificadores. Se o VK nativo do Windows difere do nome Qt (layouts ABNT2/US),
a captura persiste VK_0xXX para preservar a tecla física, sem adivinhar o layout.
Esc sozinho cancela e Delete/Backspace sozinhos limpam o campo; com modificadores
essas teclas podem compor um atalho.

No modo background, mensagens de chat usam WM_CHAR com o texto literal
em unidades UTF-16; Enter/Escape continuam como teclas. Não simular
Shift+1 para escrever ! nesse modo. Falha ou cancelamento fecha o chat
sem enviar Enter de confirmação. O modo foreground mantém o caminho
de teclas do layout do cliente. Aceitação das mensagens pelo Windows
não comprova execução do comando dentro do jogo.

No Target normal, ataque vazio permite o caminho do combo: golpes
separados, comando de chat ou hotkey configurada. Sem texto, a hotkey
do combo é enviada explicitamente; falha do combo não deve disparar
F5 como ataque implícito. Uma hotkey C não representa uma sequência
de skills por si só: depende do vínculo configurado no cliente.

## Espera por distância e pausas do Walker

A opção "Aguardar Pokémon distante" é independente do lure dinâmico: não
envia comandos move nem cria desvios. Pausa quando a distância de Chebyshev
atinge o limite superior e retoma no inferior, preservando o estado entre
eles. A GUI exige 0 <= retomada < aguardar <= 30 SQM. Os valores ficam no
objeto distance_guard do perfil da Hunt; perfis antigos mantêm a opção
desligada. Salvar/carregar preserva os controles sem disparar o movimento.

O recurso depende do provedor existente de posição confirmada do Pokémon;
não cria offsets, não substitui sua posição pela do treinador e não torna
mais confiável a identificação/cache desse provedor. Dados ausentes,
inválidos ou em outro andar pausam a rota até uma leitura utilizável.

As pausas manual, combate, distância, dados indisponíveis e watchdog têm
motivos separados e aparecem no status. Restaurar um desvio não remove
pausa de combate. Durante o desvio, apenas a pausa de combate é suspensa
para permitir a movimentação temporária; pausas manual/de distância e
watchdog continuam impedindo o movimento. O watchdog libera as teclas e
preserva o waypoint e a ordem do loop, sem descarte automático. Para
recuperar, conferir o obstáculo e parar/reiniciar a rota; uma retomada
manual também reconhece a pausa do watchdog.

Validação ao vivo desta etapa: primeiro testar só o Walker, com lure
dinâmico desligado; configurar aguardar=5 e retomar=2; observar parada
em 5, manutenção em 3/4 e retomada em 2. Repetir com pausa manual e
combate, conferindo que retirar a espera por distância não libera essas
pausas. Um bloqueio físico deve preservar o waypoint. Esses cenários
estão cobertos por simulações; confirmação no jogo continua pendente.
