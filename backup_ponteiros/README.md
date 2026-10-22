# Backup de ponteiros e offsets — 19/10/2026

- offsets.snapshot.json: cópia integral do offsets.json na data do backup; não é carregada automaticamente.
- offsets.schema.json: contrato da cópia (inclui `signature`, `signature_disp`, `signature_length` e `deref`).
- pokebot-xyz.ct: tabela CE histórica, sem nova validação.
- evidencias/: resultados locais de scans e validações; excluídos do GitHub.
- HP-20261017.md: histórico da recuperação do HP; mantido como referência da sessão antiga.
- Consulte ../CONTEXTO_RETOMADA.md para campos validados, candidatos rejeitados e pendências.

## O que mudou desde o snapshot de 17/10/2026

- player.hp / player.hp_max: RVA 0x3904D38 (`59788600`), terminal 0 / 4. Validado no PID 31952 (94 -> 88 -> 79) e resolveu de novo no PID 18352 após restart. O RVA antigo 0x3922260 ficou nulo.
- player.x / player.y / player.z: deixaram de depender do RVA fixo 0x3917F80. Agora usam a assinatura `48 8B 0D ?? ?? ?? ?? 48 85 C9 ...` (disp 3, len 7), que resolve `PokeAlliance_dx.exe+0x38FADD0`; `deref` + terminais 0x20 / 0x24 / 0x28, com Z mascarado em 0xFF. Validado no PID 20516.
- pokemon.hp_percent: mesma assinatura do player.x (o ponteiro global é compartilhado), `deref` + terminal 0x3C8 (`968`). O objeto do player e o HP do Pokémon ativo saem da mesma cadeia.
- pokemon.roster: entrada nova, `0x38FD530 -> +0x1EF8 -> +0x0A30 -> +0x1448`, slots com stride 0x10 e ponteiro do Pokémon em +0x08.
- world.entity_list_ptr / world.entity_list_end: entradas novas por assinatura (`+0x38F4510` e `+0x38F4520`), começo e fim do registry de criaturas (stride 0x10). Não é a lista de spawns ativos; é o pool/registry.
- Ambos os arquivos passam no `offset_contract.py` e carregam pelo `core.offsets.load_offsets` (37 entradas).

## Restaurar

1. Guarde o offsets.json atual antes de substituir.
2. Copie `offsets.snapshot.json` para `../offsets.json` (e o schema, se necessário).
3. Valide antes de rodar o bot:

```powershell
python -c "import json; from offset_contract import validate_offsets; validate_offsets(json.load(open('offsets.json', encoding='utf-8'))); print('ok')"
python -c "from core.offsets import load_offsets; load_offsets(); print('ok')"
```

Endereços de heap e PIDs não sobrevivem necessariamente ao restart. `verified` indica validação na sessão, não estabilidade entre builds do cliente. A versão anterior deste snapshot continua no histórico do git.
