# Hotkeys do healer

Mapeamento informado pelo usuario em 17/10/2026:

- Teclas 1 a 6 selecionam os Pokemon dos slots 1 a 6.
- M1 a M5 correspondem as teclas F1 a F5 configuradas no jogo.
- Exemplo atual: selecionar slot 4 com 4, usar M4 com F4, retornar ao slot 1 com 1.
- O bot nao digita comandos no chat neste modo.
- O retorno e configurado manualmente, nao detectado pelo active_slot.

A decisao de cura usa somente player.hp / player.hp_max.
Sequencia enfileirada nao comprova envio pelo Windows nem cura pelo jogo.
O cooldown comeca depois dos delays da sequencia.

Feche a GUI antiga antes de reabrir como administrador para carregar o codigo
e a configuracao corrigidos. Auto-Food foi desativado no config.json para
isolar o teste: sua tecla F4 conflita com a habilidade de cura.
Reconfigure a comida antes de reativar esse modulo.

O input exige a janela do PID selecionado em primeiro plano. O refoco
automatico foi desativado na configuracao para evitar piscar e roubar foco.
Clique no jogo depois de START; enquanto outra janela estiver em foco, as
teclas sao descartadas, nao enviadas a GUI.

Confira os atalhos manualmente com o bot parado. Depois selecione o PID atual
na GUI e inicie apenas o healer. Confirme a troca, o move e o aumento de HP.
O log detalhado de resolucao de enderecos fica no nivel DEBUG, nao INFO.

## Dropdowns de mochila e reset opcional

Na aba Combat, Detectar Mochila consulta o RosterProvider para o PID escolhido.
Os campos de nomes manuais foram substituidos por dropdowns nao editaveis.
Sem uma leitura validada, mostram apenas Slot 1-6 e Nomes nao detectados.
Nomes manuais antigos no config.json nao sao apresentados como detectados.
Nenhuma lista de especies populares e usada como se fosse a mochila real.

Cura, retorno e reset continuam salvos pelas teclas dos slots (1-6), nao
pelos nomes. As skills M1-M5 continuam usando F1-F5. Use Salvar configuracoes
para persistir as escolhas. Uma falha de deteccao preserva os slots escolhidos
e remove nomes antigos da tela. Trocar o PID invalida a leitura anterior.

O adapter de memoria exige o resolver conectado ao mesmo PID, uma entrada
pokemon.roster validada e um decoder get_party_roster() que retorne registros
RosterMember(slot, name). Ainda NAO temos esse decoder nem os offsets da lista.
Um ponteiro isolado nao informa codificacao, stride, contagem ou string layout.
Por isso nao foi adicionada uma entrada ficticia ao schema escalar de offsets.
Os testes de nomes usam somente fixtures, nao demonstram deteccao no jogo.
O provider nao le telemetry.json e nao executa scans nem envia teclas.

pokemon_reset_hotkey vazio desativa o reset. Se preenchido, a sequencia
inclui essa hotkey apos a skill e antes do retorno. Opcionalmente escolha
outro Pokemon para reset: ele sera selecionado antes da hotkey de reset.
Selecionar esse Pokemon sem configurar a acao de reset e um erro de
configuracao, para evitar uma troca sem acao definida.

Sequencia: cura -> delay de troca -> skill -> delay da skill ->
[Pokemon de reset -> delay de troca] -> [acao de reset -> delay de reset]
-> Pokemon de retorno. Colchetes indicam etapas opcionais.

Uma tecla numerica de reset so deve ser usada se realmente estiver
vinculada a essa acao no jogo: o slot 1 da action bar nao equivale
automaticamente ao Pokemon 1. Nao foi inventado um atalho para reset.
O modo atual nao detecta fim de combate, nao verifica cooldown da skill
na memoria e nao confirma o efeito da cura automaticamente.
