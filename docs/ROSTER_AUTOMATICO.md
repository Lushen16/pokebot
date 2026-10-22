# Roster automatico e cura

A aba Combat usa `RosterDetector` e `OffsetRegistry` para consultar
`pokemon.roster` no arquivo de offsets do cliente selecionado. Sem uma leitura
valida, mostra Slot 1 a Slot 6, sem solicitar nomes digitados nem reutilizar
nomes manuais antigos. START e Salvar configuracoes persistem os slots como
F1 a F6 nas chaves `pokemon_heal_hotkey`, `pokemon_return_hotkey` e
`pokemon_reset_hotkey`. A opcao sem reset grava uma string vazia.

A sequencia e cura, espera de troca, skill, espera de skill, reset opcional,
espera de reset e retorno. Os delays padrao sao 500, 500 e 300 ms. Os
thresholds padrao sao HP 90% e panic 20%. A antiga chave
`pokemon_reset_pokemon_hotkey` nao adiciona mais uma quinta tecla.

## Contrato de leitura

O detector aceita uma lista de dicionarios com `slot` (1 a 6), `name` e
metadados opcionais, preservando identificadores reais quando presentes.
Registros invalidos, slots duplicados e falhas de memoria retornam o fallback.
O leitor Win32 atual resolve entradas escalares: configurar somente um
ponteiro nao revela o layout dos nomes. Quando esse layout for mapeado, o
leitor pode implementar `decode_roster(value)`, recebendo o valor retornado
por `OffsetRegistry.read` e devolvendo os registros. Nenhum layout, endereco,
UUID ou nome e inventado. A GUI ja consome esses registros sem alteracoes.

A deteccao abre um `MemoryReader` ctypes temporario para o PID selecionado
somente quando o offset esta resolvido, e fecha o handle ao terminar.
Leitores injetados pertencem ao chamador e precisam estar anexados ao mesmo
PID. Nao foram adicionadas dependencias.

## Scanner de strings e ponteiros

No PowerShell como Administrador, a partir da raiz do projeto:

```powershell
python -m core.tools.scan_roster
python -m core.tools.scan_roster --name Togepi --save roster-scan.json
python -m core.tools.scan_roster 12345 --name Togepi --max-depth 4 --max-offset 0x2000
```

Sem `--name`, o scanner pergunta o nome no terminal. O processo padrao e
`PokeAlliance_dx.exe`; informe o PID para escolher entre varios clientes.
Tambem e possivel executar `python core/tools/scan_roster.py` diretamente.

O scanner procura ASCII e UTF-16LE, preserva strings que cruzam chunks,
mostra um dump desde ate 0x100 bytes antes de cada nome e busca referencias
a bases candidatas nessa janela. A busca reversa reutiliza o formato de
chains de `pointer_scan.py`, respeita ponteiros de 32/64 bits e testa todos
os filhos possiveis. Cada chain emitida e resolvida novamente com o
`MemoryReader`; o nome tambem e relido antes de aceitar o candidato.
Ponteiros desalinhados podem ser incluidos com `--unaligned`.

O console mostra a entrada para a base candidata e a entrada para a string,
ambas com `base_rva`, `steps`, `terminal` e `verified: false`. Isso nao
confirma a base da struct nem o roster: nomes tambem podem existir em chats,
interfaces e caches. Nao se presume que o endereco menos 0x100 seja uma
struct. Valide candidatos apos reiniciar o cliente e alterar os slots antes
de preencher offsets definitivos ou implementar o decoder do roster.

Limites configuraveis: `--max-hits` (128), `--max-frontier` (20000),
`--max-chains` (256), `--max-depth` (4), `--lookbehind` (0x100) e
`--max-offset` (0x2000). Um limite de quantidade atingido e anunciado como
resultado parcial. Leituras grandes que falham sao repetidas em blocos
menores; paginas indisponiveis sao contadas no relatorio. O scan nao e um
snapshot atomico do processo. Nenhuma memoria nem offsets.json sao escritos.

Codigos de saida: 0 = scan concluido dentro dos limites (mesmo sem hits),
1 = erro/permissao, 2 = argumentos invalidos ou resultado parcial por limite,
130 = cancelamento. `--save` grava um relatorio independente em JSON.
