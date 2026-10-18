"""
core/tools/scan_party_array.py - Scanner do Party Array (Mochila / Roster).

Localiza o Party Array pai (array de 6 ponteiros u64 para structs de Pokémon)
a partir do endereço de struct de um Pokémon individual, valida os 6 slots
e descobre a pointer chain estática para o offsets.json.

Uso via CLI (PowerShell como Administrador):
    python -m core.tools.scan_party_array PokeAlliance_dx.exe --target 0x140F0365270
    python -m core.tools.scan_party_array PokeAlliance_dx.exe --target 0x140F0365270 --max-depth 4
    python -m core.tools.scan_party_array PokeAlliance_dx.exe --target 0x140F0365270 --save party_array.json
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import re
import struct
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import MemoryReader
from core.tools.pointer_scan import build_chains, format_static_chain
from core.tools.scan_value import iter_regions

log = logging.getLogger("pokebot.tools.scan_party_array")
SLOT_COUNT = 6
POINTER_SIZE = 8


def parse_integer(text: str) -> int:
    """Converte string decimal ou hexadecimal para inteiro."""
    try:
        return int(text, 0)
    except ValueError:
        try:
            return int(text, 10)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("Informe um endereço inteiro decimal ou hexadecimal válido.") from exc


def find_pointer_references(
    memory: MemoryReader,
    regions: List[Tuple[int, int]],
    target: int,
    chunk_size: int = 4 * 1024 * 1024,
) -> List[int]:
    """
    Varre a memória procurando por ponteiros u64 (little-endian)
    que apontem exatamente para o endereço 'target'.
    Usa busca em C (bytes.find) para máxima performance.
    """
    needle = target.to_bytes(POINTER_SIZE, "little")
    overlap = POINTER_SIZE - 1
    hits: List[int] = []

    for base, size in regions:
        offset = 0
        tail = b""
        data_base = base

        while offset < size:
            to_read = min(chunk_size, size - offset)
            raw = memory.read_bytes(base + offset, to_read)
            if not raw or len(raw) == 0:
                offset += to_read
                tail = b""
                continue

            data = tail + raw
            block_base = (base + offset) - len(tail)
            search_idx = 0

            while True:
                pos = data.find(needle, search_idx)
                if pos == -1:
                    break
                hit_addr = block_base + pos
                # Se alinhado a pelo menos 4 ou 8 bytes
                if hit_addr % 4 == 0:
                    hits.append(hit_addr)
                search_idx = pos + 1

            tail = data[-overlap:] if overlap else b""
            offset += to_read

    return sorted(list(set(hits)))


def extract_pokemon_name(memory: MemoryReader, struct_addr: int, inspect_size: int = 0x140) -> Optional[str]:
    """
    Tenta identificar o nome de um Pokémon dentro da struct.
    Procura strings ASCII ou UTF-16LE válidas (tamanho 3..25) comuns em jogos.
    """
    if struct_addr <= 0x10000:
        return None

    raw = memory.read_bytes(struct_addr, inspect_size)
    if not raw or len(raw) < 16:
        return None

    # Procura strings legíveis terminadas por \0
    # 1. Padrão ASCII
    ascii_matches = re.findall(rb"([A-Za-z0-9][A-Za-z0-9 _-]{2,24})\x00", raw)
    for m in ascii_matches:
        try:
            cand = m.decode("ascii").strip("\x00 \t\r\n")
            if cand and not cand.lower().endswith((".png", ".dds", ".lua", ".dll", ".exe")):
                return cand
        except UnicodeDecodeError:
            pass

    # 2. Padrão UTF-16LE
    utf16_matches = re.findall(rb"((?:[A-Za-z0-9]\x00(?:[A-Za-z0-9 _-]\x00){2,24}))\x00\x00", raw)
    for m in utf16_matches:
        try:
            cand = m.decode("utf-16le").strip("\x00 \t\r\n")
            if cand and not cand.lower().endswith((".png", ".dds", ".lua", ".dll", ".exe")):
                return cand
        except UnicodeDecodeError:
            pass

    return None


def is_readable_pointer(memory: MemoryReader, ptr: int) -> bool:
    """Verifica se o ponteiro aponta para um endereço válido e legível."""
    if ptr == 0:
        return True  # Slot vazio é válido
    if ptr < 0x10000 or ptr > 0x7FFFFFFFFFFF:
        return False
    # Tenta ler 8 bytes do endereço de destino
    test_read = memory.read_bytes(ptr, 8)
    return test_read is not None and len(test_read) >= 8


def evaluate_party_array(
    memory: MemoryReader,
    array_base: int,
    target_addr: int,
) -> Optional[Dict[str, Any]]:
    """
    Inspeciona os 6 ponteiros consecutivos a partir de 'array_base'.
    Valida se os slots são ponteiros legíveis ou nulos e extrai os nomes.
    """
    # Lê 6 ponteiros de 8 bytes (48 bytes total)
    raw = memory.read_bytes(array_base, SLOT_COUNT * POINTER_SIZE)
    if not raw or len(raw) < SLOT_COUNT * POINTER_SIZE:
        return None

    pointers: List[int] = list(struct.unpack_from(f"<{SLOT_COUNT}Q", raw))

    # O endereço target DEVE estar presente em pelo menos um dos slots
    if target_addr not in pointers:
        return None

    target_slot_index = pointers.index(target_addr)
    slots_info: List[Dict[str, Any]] = []
    valid_pointers_count = 0
    named_pokemon_count = 0
    invalid_pointers_count = 0

    for idx, ptr in enumerate(pointers):
        slot_number = idx + 1
        offset_rel = idx * POINTER_SIZE
        if ptr == 0:
            slots_info.append({
                "slot": slot_number,
                "offset": offset_rel,
                "pointer": 0,
                "is_empty": True,
                "name": None,
                "valid": True,
            })
        else:
            readable = is_readable_pointer(memory, ptr)
            if not readable:
                invalid_pointers_count += 1
                slots_info.append({
                    "slot": slot_number,
                    "offset": offset_rel,
                    "pointer": ptr,
                    "is_empty": False,
                    "name": None,
                    "valid": False,
                })
            else:
                valid_pointers_count += 1
                detected_name = extract_pokemon_name(memory, ptr)
                if detected_name:
                    named_pokemon_count += 1
                slots_info.append({
                    "slot": slot_number,
                    "offset": offset_rel,
                    "pointer": ptr,
                    "is_empty": False,
                    "name": detected_name,
                    "valid": True,
                })

    # Se houver ponteiros inválidos (que não sejam nulos e não sejam legíveis), rejeitamos
    if invalid_pointers_count > 0:
        return None

    # Pontuação de confiança:
    # 50 base + 10 por ponteiro válido + 25 por nome detectado
    score = 50 + (valid_pointers_count * 10) + (named_pokemon_count * 25)

    return {
        "array_base": array_base,
        "target_slot": target_slot_index + 1,
        "valid_count": valid_pointers_count,
        "named_count": named_pokemon_count,
        "score": score,
        "slots": slots_info,
    }


def find_party_arrays(
    memory: MemoryReader,
    regions: List[Tuple[int, int]],
    target_addr: int,
    check_all_slots: bool = True,
) -> List[Dict[str, Any]]:
    """
    Encontra e valida arrays de Party a partir de referências ao Pokémon target.
    Testa se o ponteiro encontrado representa qualquer um dos 6 slots (0 a 5).
    """
    hits = find_pointer_references(memory, regions, target_addr)
    if not hits:
        return []

    candidates: List[Dict[str, Any]] = []
    seen_bases = set()

    for ptr_addr in hits:
        # Se check_all_slots estiver ativo, o target pode ser qualquer slot entre 0 e 5
        slot_range = range(SLOT_COUNT) if check_all_slots else (0,)
        for slot_idx in slot_range:
            cand_base = ptr_addr - (slot_idx * POINTER_SIZE)
            if cand_base in seen_bases:
                continue
            seen_bases.add(cand_base)

            eval_res = evaluate_party_array(memory, cand_base, target_addr)
            if eval_res is not None:
                eval_res["referenced_by"] = ptr_addr
                candidates.append(eval_res)

    # Ordena por maior pontuação de confiança
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def generate_offsets_entries(
    static_chain: Dict[str, Any],
    party_array: Dict[str, Any],
) -> Dict[str, Any]:
    """Gera o dicionário de entradas formatadas para o offsets.json."""
    base_rva = static_chain["rva"]
    steps = static_chain["steps"]
    module_name = static_chain["module"]

    entries: Dict[str, Any] = {}

    # Entrada do Party Array base
    entries["pokemon.roster_ptr"] = {
        "module": module_name,
        "base_rva": base_rva,
        "steps": steps,
        "terminal": 0,
        "deref": False,
        "type": "pointer",
        "verified": False,
        "source": "scan_party_array",
        "note": "Party Array base (6-slot Pokémon pointer array)",
    }

    # Entradas para cada slot individual (1 a 6)
    for slot in party_array["slots"]:
        slot_num = slot["slot"]
        slot_offset = slot["offset"]
        slot_name_comment = f" - {slot['name']}" if slot.get("name") else ""
        entries[f"pokemon.roster.slot_{slot_num}"] = {
            "module": module_name,
            "base_rva": base_rva,
            "steps": steps + [slot_offset] if steps else [slot_offset],
            "terminal": 0,
            "deref": True,
            "type": "pointer",
            "verified": False,
            "source": "scan_party_array",
            "note": f"Slot {slot_num}{slot_name_comment}",
        }

    return entries


def print_report(
    target_addr: int,
    party_array: Dict[str, Any],
    static_chains: List[Dict[str, Any]],
) -> None:
    """Exibe relatório detalhado no terminal com tabelas e JSON pronto."""
    print("\n" + "=" * 78)
    print("=== PARTY ARRAY LOCALIZADO COM SUCESSO! ===")
    print("=" * 78)
    print(f"[*] Endereço do Pokémon Alvo : 0x{target_addr:X} (Slot {party_array['target_slot']})")
    print(f"[+] Endereço Base do Array   : 0x{party_array['array_base']:X}")
    print(f"[+] Confiança do Array       : {party_array['score']} pontos")
    print(f"[+] Slots Válidos / Nomes    : {party_array['valid_count']} válidos / {party_array['named_count']} identificados")

    print("\n" + "-" * 78)
    print("SLOTS DA MOCHILA (PARTY ARRAY):")
    print("-" * 78)
    print(f"{'Slot':<8}{'Offset':<10}{'Ponteiro da Struct':<22}{'Nome Detectado':<25}{'Status'}")
    print("-" * 78)

    for slot in party_array["slots"]:
        slot_str = f"Slot {slot['slot']}"
        offset_str = f"+0x{slot['offset']:02X}"
        ptr_str = f"0x{slot['pointer']:X}" if slot["pointer"] else "0x0 (NULL)"
        name_str = f"'{slot['name']}'" if slot.get("name") else "(sem nome)"
        status_str = "Vazio" if slot["is_empty"] else ("OK" if slot["valid"] else "Inválido")
        target_marker = "  <-- [ALVO INFORMADO]" if slot["pointer"] == target_addr else ""
        print(f"{slot_str:<8}{offset_str:<10}{ptr_str:<22}{name_str:<25}{status_str}{target_marker}")

    print("\n" + "=" * 78)
    print("CADEIA DE PONTEIROS ESTÁTICA (POINTER SCAN):")
    print("=" * 78)

    if not static_chains:
        print("[!] Nenhuma cadeia estática direta encontrada nos limites configurados.")
        print("    Dica: Rode com --max-depth 5 ou --max-offset 0x4000 para aprofundar.")
    else:
        for idx, chain in enumerate(static_chains, 1):
            print(f"\n[Chain #{idx}]")
            print(f"  Módulo   : {chain['module']}")
            print(f"  Base RVA : 0x{chain['rva']:X} ({chain['rva']})")
            print(f"  Steps    : {[hex(s) for s in chain['steps']]}")
            print(f"  Cadeia   : [{chain['module']} + 0x{chain['rva']:X}] -> " +
                  " -> ".join(f"[+{hex(s)}]" for s in chain["steps"]) +
                  f" ==> 0x{party_array['array_base']:X}")

        best_chain = static_chains[0]
        entries = generate_offsets_entries(best_chain, party_array)

        print("\n" + "=" * 78)
        print("JSON PRONTO PARA O offsets.json (adicione dentro de 'entries'):")
        print("=" * 78)
        print(json.dumps(entries, indent=2, ensure_ascii=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="scan_party_array",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("process", nargs="?", default="PokeAlliance_dx.exe", help="Nome do processo ou PID (padrão: PokeAlliance_dx.exe)")
    parser.add_argument("--target", "-t", type=parse_integer, default=None, help="Endereço da struct do Pokémon em memória (ex: 0x140F0365270)")
    parser.add_argument("--max-offset", type=parse_integer, default=0x2000, help="Offset máximo por passo no pointer scan (padrão: 0x2000)")
    parser.add_argument("--max-depth", type=int, default=4, help="Profundidade máxima de cadeia de ponteiros (padrão: 4)")
    parser.add_argument("--slot", type=int, default=None, help="Slot específico (1 a 6) do alvo se já conhecido (padrão: testa todos 1..6)")
    parser.add_argument("--save", type=Path, help="Salvar relatório em arquivo JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Ativar logging detalhado")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not MemoryReader.require_admin():
        print("[!] FALHA: Execute o terminal como Administrador.", file=sys.stderr)
        return 1

    target_addr = args.target
    if target_addr is None:
        raw_in = input("Endereço da struct do Pokémon (ex: 0x140F0365270): ").strip()
        try:
            target_addr = parse_integer(raw_in)
        except Exception:
            print("[!] Endereço inválido.", file=sys.stderr)
            return 1

    if target_addr <= 0x10000:
        print(f"[!] Endereço 0x{target_addr:X} está abaixo do limite de memória de usuário.", file=sys.stderr)
        return 1

    target = int(args.process) if args.process.isdecimal() else args.process
    memory = MemoryReader(target)
    if not memory.attach():
        print(f"[!] FALHA ao anexar a {args.process}: {memory.last_error}", file=sys.stderr)
        return 1

    try:
        print(f"[*] Anexado ao PID {memory.pid} ({'x64' if memory.is_64bit else 'x86'})")
        print(f"[*] Varrendo referências ao Pokémon: 0x{target_addr:X}...")

        regions = list(iter_regions(memory))
        if not regions:
            print("[!] Nenhuma região legível encontrada no processo.", file=sys.stderr)
            return 1

        check_all = args.slot is None
        candidates = find_party_arrays(memory, regions, target_addr, check_all_slots=check_all)

        if not candidates:
            print(f"\n[!] Nenhum Party Array válido encontrado apontando para 0x{target_addr:X}.")
            print("    Dicas para resolução:")
            print("    1. Certifique-se de que a mochila de Pokémon está aberta no jogo.")
            print("    2. Verifique se o endereço passado é o início da struct (candidate_base) do scan_roster.")
            print("    3. Experimente passar o endereço de outro Pokémon da mochila.")
            return 0

        best_array = candidates[0]
        array_base = best_array["array_base"]

        print(f"[+] Candidato a Party Array encontrado em: 0x{array_base:X} (Score: {best_array['score']})")
        print(f"[*] Executando Pointer Scan estático para a base 0x{array_base:X}...")

        raw_chains = build_chains(
            memory,
            target=array_base,
            max_offset=args.max_offset,
            max_depth=args.max_depth,
        )

        formatted_chains: List[Dict[str, Any]] = []
        for c in raw_chains:
            fc = format_static_chain(memory, c)
            if fc is not None:
                formatted_chains.append(fc)

        print_report(target_addr, best_array, formatted_chains)

        if args.save:
            report_data = {
                "process": args.process,
                "pid": memory.pid,
                "target_pokemon_address": target_addr,
                "party_array": best_array,
                "static_chains": formatted_chains,
                "offsets_entries": generate_offsets_entries(formatted_chains[0], best_array) if formatted_chains else {},
            }
            args.save.write_text(json.dumps(report_data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"\n[+] Relatório completo salvo em: {args.save.resolve()}")

        return 0

    except KeyboardInterrupt:
        print("\n[!] Scan interrompido pelo usuário.", file=sys.stderr)
        return 130
    except Exception:
        log.exception("Erro durante o scan do Party Array")
        return 1
    finally:
        memory.detach()


if __name__ == "__main__":
    raise SystemExit(main())
