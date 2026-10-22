# AGENTS.md — Contexto do agente (PokeBot)

> Instruções permanentes do projeto para o agente do Zed (e para opencode/Codex/Claude).
> O Zed usa o primeiro arquivo encontrado nesta ordem: `.rules`, `.cursorrules`,
> `.windsurfrules`, `.clinerules`, `.github/copilot-instructions.md`, `AGENT.md`,
> `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`. Não criar `.rules` sem apagar/atualizar este.

## 1. Identidade

- Responder começando com `[Aoid]` sozinho na primeira linha.
- Português brasileiro casual, direto, sem preâmbulo, sem recap, sem disclaimer.
- Baixo calão permitido quando o usuário puxar esse tom.
- Código completo e funcional; nunca stub, `TODO` ou `NotImplementedError` em implementação nova.
- Colar a saída bruta do pytest depois de qualquer tarefa que toque código.

## 2. Ambiente

- Windows 10/11 x64; Python 3.11+.
- Raiz do projeto: `C:\Users\yago\Desktop\pokebot`.
- Rodar `python main.py` como **Administrador** (o cliente PokeAlliance roda elevado).
- Testes: `python -m pytest` na raiz (o `pytest.ini` define `pythonpath=.`).
- Classes de teste precisam começar com `Test`.
- GUI: PySide6. Entrada: Win32 `SendInput` via ctypes. Memória: `ReadProcessMemory` via ctypes.

## 3. Ferramentas, plugins e skills

Carregar a skill correspondente antes de agir quando a tarefa casar. Não inventar
resultado de ferramenta; se o MCP/plugin não estiver disponível, usar o equivalente
de linha de comando e dizer o que foi usado.

| Skill | Usar quando | Ferramenta/base |
| --- | --- | --- |
| `reverse-engineer-anything` (REA) | RE multi-etapa, artefato empacotado, Electron, firmware, WASM | MCP `rea` (`npx -y rea-agents@6.5.0 mcp`) e CLI do REA |
| `ghidra-reverse` | decompilar/desmontar sem IDA, xrefs, headless | Ghidra `analyzeHeadless`, Ghidra MCP |
| `rev-idapython` | script IDAPython, IDALib, Hex-Rays, batch | IDA Pro |
| `dotnet-reverse` | PE com CLR, C#, NativeAOT, ConfuserEx, Sharp* | dnSpyEx + de4dot |
| `anti-reversing-techniques` | anti-debug, packing, VM detect, CTF de proteção | x64dbg, Frida, ScyllaHide |
| `protocol-reverse-engineering` | tráfego de rede, protocolo proprietário | Wireshark, tcpdump, mitmproxy |
| `game-ctf` | Unity Mono/IL2CPP, saves, dumps, memória de jogo | Il2CppDumper, Frida, Cheat Engine |
| `reverse-engineering` | guarda-chuva de RE binária | radare2, x64dbg, jadx, apktool, strings |
| `ui-designer` | landing page, dashboard, componente, beleza de UI | HTML/CSS, React, Tailwind |
| `pyside6-engineer` | widget, signal/slot, model-view, thread, teste de GUI | PySide6/Qt |
| `python-testing` | fixture, mock, parametrize, cobertura | pytest |
| `systematic-debugging` | bug, teste falhando, comportamento inesperado | diagnóstico antes de patch |

Regras de uso:

- REA primeiro para alvo desconhecido/empacotado; Ghidra/IDA quando precisar de decompilação profunda.
- Em binário .NET, ir direto de dnSpyEx; não perder tempo com IDA no IL.
- Em jogo com memória, `game-ctf` + leitura externa; injeção quando for o caminho pedido.
- Design de UI: `ui-designer`; se for Qt, somar `pyside6-engineer`.
- Antes de corrigir bug sem causa óbvia, carregar `systematic-debugging`.
- Citar caminho e linha ao referenciar código: `arquivo.py:123`.

## 4. Visão do projeto

Bot próprio para o cliente **PokeAlliance** (`PokeAlliance_dx.exe`), com paridade
funcional ao Shiba Bots (Litfix), mas código próprio, modular e mantível.

- Leitura externa de memória (`ReadProcessMemory`), sem injeção de DLL e sem driver de kernel.
- Input via `SendInput` (ctypes), sem pydirectinput.
- Offsets versionados em JSON (schema v2) + validação por contrato.
- O Shiba serve só como oráculo/validação; nunca copiar código dele.

## 5. Estrutura

- `core/memory.py`: `MemoryReader` Win32. **Não tem `read_raw`** — usar `read_bytes(addr, size)`.
  Tem `main_module_base`, `read_int32/64`, `read_ptr`, `resolve_entry_address`, `require_admin`.
- `memory.py` (raiz): stack antiga com `Process` + `Resolver` (pymem) e `read_raw`. Não misturar com `core/`.
- `core/offsets.py`: loader tipado; `offsets.read(mem, "chave")`.
- `offset_contract.py` + `offsets.schema.json`: validação das entradas.
- `modules/`: `healer.py`, `auto_target.py`, `auto_catch.py`, `cavebot.py`, `perfect_lure.py`.
- `engine.py`: orquestrador/registry dos módulos.
- `config.json`: configuração de runtime (lure, cavebot, revive etc.).
- `gui/`, `ui/`: PySide6.
- `backup_ponteiros/`: snapshot congelado de offsets + schema + README. **Não editar sem pedido.**

## 6. Offsets validados ao vivo (19/10/2026)

Validar de novo a cada sessão; PID e base mudam.

| Chave | RVA / cadeia | Tipo | Observação |
| --- | --- | --- | --- |
| player chain | `base + 0x38FADD0`, deref 1x → `p` | ptr | endereço do jogador |
| player.x | `p + 0x20` | float | |
| player.y | `p + 0x24` | float | |
| player.z | `p + 0x28` | float | mascarar com `0xFF` |
| player.hp | RVA `0x3904D38` | int32 | validado em jogo (94→88→79) |
| player.hp_max | RVA `0x3904D38 + 4` | int32 | |
| pokemon.hp_percent | RVA `0x3921B48` (terminal 968) | int32 | mesma assinatura de `player.x` |
| pokemon.roster | RVA `0x38FD530` | ptr | preferir `RosterDetector` |

- Assinatura compartilhada de `player.x` e `pokemon.hp_percent`:
  `48 8B 0D ?? ?? ?? ?? 48 85 C9 0F 84 9D 00 00 00 80 B9 B0 00 00 00 00 0F`, disp 3, len 7.
- RVA antigo `0x3922260` para HP está morto/nulo. Não usar.
- Tripla candidata `p + 0x0BFC` (3300/2025/7) pode ser a posição do Pokémon ativo — **não confirmada**.

### Struct de entidade (world scan)

- Anchor = endereço do X.
- `x @ +0x00`, `y @ +0x04`, `z @ +0x08` (float).
- Nome ASCII NUL-terminated `@ +0x50`.
- Base do objeto: `anchor - 0x20`.
- ID único `@ +0x4c`; HP `u32 @ +0xa4`; shiny/estrela `u32 @ +0x150 == 0x100` (não usar cor do nome).
- Com `focus_shiny`, `CombatController.enemies/run` só aceitam shiny, ignoram `lure_target_count` e não pausam a rota sem shiny; um único shiny já inicia o combate.
- `EntityReader._to_entities` **não seta** `is_player`, `is_npc`, `is_dead` nem `creature_id`.

### Roster

- `RosterDetector(mem, offsets).get_roster()` é a fonte confiável de nomes/slots.
- `offsets.read(mem, "pokemon.roster")` não é confiável.
- Não existe telemetria de `activeSlot`; inferir com cautela.

### Chat

- Chat não tem offset fixo; `core/chat_snapshot.py` lê o JSON `{"schema":2,"tick":...}` do processo do jogo.
- `alertEvents` traz `kind:"talk"`, `mode`, `name`, `text`, `self`; trade = 7; criaturas/NPC = 36..44.
- A cópia canônica avança o tick a cada ~500 ms; varredura inicial ~6 s; candidatos são cacheados e relidos.
- O snapshot é alimentado pela automação do PKAengine; se ele for fechado, validar se o cliente continua atualizando o buffer.

## 7. Contrato do Perfect Lure

- Nunca usar a posição do personagem como fallback da posição do Pokémon.
- Só considerar posição confirmada quando identidade **e** posição estiverem confirmadas.
- Nome ou slot sozinhos **não** são identidade.
- Identidade: nome do roster ou `active_pokemon`, HP ±1 (`LURE_HP_TOL=1`),
  Chebyshev ≤ 3 (`LURE_FOLLOWER_MAX_DIST=3`), 2 ticks estáveis
  (`LURE_IDENTITY_STABLE_TICKS=2`), TTL de HP 2s (`LURE_IDENTITY_TTL_S=2.0`).
- Troca de Pokémon invalida identidade + HP + posição imediatamente.
- Sem posição confirmada: `near=0`, sem agrupamento, sem `maxwait`;
  logar exatamente `Perfect Lure indisponível: posição do Pokémon não confirmada`.
- Combate normal nunca é bloqueado pelo lure.
- Emergência sem posição só com HP recente e válido ligado ao Pokémon ativo.
- Nunca cancelar sequência já enviada.

## 8. Regras de código

- Comentários em português; identificadores em inglês.
- Não reescrever arquivo funcional sem pedido explícito.
- Se faltar offset, usar `OFFSET_UNKNOWN = 0x0` com comentário `PREENCHER`; nunca inventar valor.
- APIs reais de ctypes/struct/json; nada de pseudocódigo.
- Não criar documentação (.md) nem README novo sem pedido.
- Não commitar, dar push nem abrir PR sem pedido explícito.
- Não logar nem gravar segredo, token ou API key.
- Não apagar backups (`backup_ponteiros/`, `hp-*`, `*-backup-*`) sem pedido.
- Preferir editar arquivo existente a criar paralelo.
- Manter o estilo do arquivo em volta (imports, nomes, estrutura).

## 9. Testes

- Toda mudança funcional vem com teste; rodar a suíte inteira ao final.
- Baseline atual: `948 passed, 40 subtests passed` (filtro shiny, leitor de chat, aba Alertar, redesign da interface, regressão da aba Targeting, HP% por criatura, leitura de snapshot em chunks, minimapa OTMM/best tile, detour do Walker, lure dinâmico com comando move, timeout de lure 2500–5000 ms e ciclo de dano/cura do Perfect Lure incluídos).
- Não afrouxar teste para passar; corrigir o código.
- Em `tests/test_perfect_lure.py`, o helper `snap()` usa
  `position_confirmed=True, hp_confirmed=True` por padrão.
- Colar a saída crua, mesmo quando falhar.

## 10. Fluxo de trabalho

1. Ler o contexto e o código envolvido antes de editar.
2. Se for RE de alvo desconhecido: inspecionar artefato com a skill certa antes de propor offset.
3. Alterar o mínimo necessário, sem refatoração de brinde.
4. Rodar `python -m pytest` e colar a saída.
5. Resumir em no máximo 4 linhas, citando `arquivo.py:linha` quando útil.
