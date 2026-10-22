"""Scanner de entidades (criaturas) e da Entity List.

Estrategia:
  1. Le a posicao do player (offsets.json ou --player X Y).
  2. Varre as regioes legiveis procurando structs de entidade: um par
     int32 (x, y) proximo do player, byte z em +0x8 e um nome ASCII
     null-terminated em +0x50 (invariante descoberta no player:
     name_addr = pos_addr + 0x50).
  3. Com --find-list, procura arrays de ponteiros que apontem para essas
     structs (a Entity List) e, opcionalmente, resolve a cadeia estatica
     ate o modulo principal.

Uso (PowerShell elevado, na raiz do projeto):
    python -m core.tools.scan_entities
    python -m core.tools.scan_entities --name Pidgey --save entities-scan.json
    python -m core.tools.scan_entities --find-list --max-depth 4

Os resultados sao CANDIDATOS nao verificados; offsets.json nunca e alterado.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left
import json
import logging
from pathlib import Path
import struct
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import MemoryReader
from core.tools.pointer_scan import build_chains
from core.tools.scan_value import iter_regions

log = logging.getLogger("pokebot.tools.scan_entities")

NAME_OFFSET = 0x50
Z_OFFSET = 0x8
MIN_COORDINATE = 0
MAX_COORDINATE = 200000
MAX_NAME_LENGTH = 24
POINTER_STRUCT_ZONE = 0x40


def parse_integer(text):
    try:
        return int(text, 0)
    except ValueError:
        try:
            return int(text, 10)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("Informe um inteiro decimal ou hexadecimal") from exc


def read_offsets(path):
    from core.offsets import load_offsets

    root = Path(__file__).resolve().parents[2]
    offsets_path = Path(path) if path else root / "offsets.json"
    schema_path = offsets_path.with_name("offsets.schema.json")
    return load_offsets(offsets_path, schema_path if schema_path.exists() else None)


def read_player_position(memory, offsets):
    if offsets is None:
        return None
    values = []
    for key in ("player.x", "player.y", "player.z"):
        entry = offsets.find(key)
        if entry is None or not entry.is_resolved:
            return None
        values.append(memory.read_entry(entry.to_reader_dict()))
    if any(v is None for v in values):
        return None
    return int(values[0]), int(values[1]), int(values[2])


def player_anchor(memory, offsets):
    if offsets is None:
        return None
    entry = offsets.find("player.x")
    if entry is None or not entry.is_resolved:
        return None
    return memory.resolve_entry_address(entry.to_reader_dict())


def decode_name(memory, address):
    raw = memory.read_bytes(address, MAX_NAME_LENGTH)
    if not raw:
        return None
    end = raw.find(b"\x00")
    chunk = raw if end == -1 else raw[:end]
    if not chunk or len(chunk) > MAX_NAME_LENGTH:
        return None
    if any(byte < 0x20 or byte > 0x7E for byte in chunk):
        return None
    return chunk.decode("ascii")


def find_entities(memory, regions, px, py, radius, *, name_filter=None,
                  max_hits=4096, chunk_size=4 * 1024 * 1024):
    """Retorna candidatos de struct de entidade perto de (px, py)."""
    pair = struct.Struct("<ii")
    step = 4
    hits = []
    for base, size in regions:
        offset = 0
        while offset < size:
            to_read = min(chunk_size, size - offset)
            chunk = memory.read_bytes(base + offset, to_read)
            if not chunk:
                offset += to_read
                continue
            end = len(chunk) - pair.size + 1
            index = 0
            while index < end:
                x, y = pair.unpack_from(chunk, index)
                if (MIN_COORDINATE <= x <= MAX_COORDINATE
                        and MIN_COORDINATE <= y <= MAX_COORDINATE
                        and abs(x - px) <= radius and abs(y - py) <= radius):
                    address = base + offset + index
                    z = memory.read_uint8(address + Z_OFFSET)
                    if z is not None and 0 <= z <= 15:
                        if memory.read_uint8(address + NAME_OFFSET - 1) != 0:
                            index += step
                            continue
                        name = decode_name(memory, address + NAME_OFFSET)
                        if name and (name_filter is None or name.lower() == name_filter.lower()):
                            hits.append({"anchor": address, "x": x, "y": y, "z": z,
                                         "name": name,
                                         "distance": max(abs(x - px), abs(y - py))})
                            if len(hits) >= max_hits:
                                return hits
                index += step
            offset += to_read
    hits.sort(key=lambda h: (h["distance"], h["name"], h["anchor"]))
    return hits


def find_entity_list(memory, regions, anchors, *, max_delta=POINTER_STRUCT_ZONE,
                     max_arrays=32, chunk_size=4 * 1024 * 1024):
    """Procura arrays contiguos de ponteiros que apontem para as structs."""
    ordered = sorted(set(anchors))
    if not ordered:
        return []
    unpacker = struct.Struct("<Q")
    slots = {}
    for base, size in regions:
        offset = 0
        while offset < size:
            to_read = min(chunk_size, size - offset)
            chunk = memory.read_bytes(base + offset, to_read)
            if not chunk:
                offset += to_read
                continue
            end = len(chunk) - unpacker.size + 1
            index = 0
            while index < end:
                value = unpacker.unpack_from(chunk, index)[0]
                if value > 0x10000:
                    position = bisect_left(ordered, value)
                    if position < len(ordered) and ordered[position] - value <= max_delta:
                        slots[base + offset + index] = ordered[position]
                index += 8
            offset += to_read

    arrays = []
    for slot in sorted(slots):
        entry = (slot, slots[slot])
        if arrays and slot - arrays[-1]["_last_slot"] <= POINTER_STRUCT_ZONE:
            arrays[-1]["_last_slot"] = slot
            arrays[-1]["entries"].append(entry)
        else:
            arrays.append({"base": slot, "_last_slot": slot, "entries": [entry]})

    results = []
    for array in arrays:
        distinct = {anchor for _, anchor in array["entries"]}
        if len(distinct) < 2:
            continue
        positions = [slot for slot, _ in array["entries"]]
        gaps = [later - earlier for earlier, later in zip(positions, positions[1:])]
        results.append({"base": array["base"], "slots": len(array["entries"]),
                        "distinct_entities": len(distinct),
                        "stride": min(gaps) if gaps else 0,
                        "annotations": [{"slot": slot, "anchor": anchor}
                                        for slot, anchor in array["entries"]]})
    results.sort(key=lambda r: (r["distinct_entities"], r["slots"]), reverse=True)
    return results[:max_arrays]


def print_report(report):
    print("\n=== RESULTADO DO SCAN DE ENTIDADES ===")
    print(f"PID: {report['pid']} | Player: ({report['player']['x']},{report['player']['y']},{report['player']['z']})")
    if report.get("player_anchor") is not None:
        print(f"Anchor do player: 0x{report['player_anchor']:X} (nome em +0x{NAME_OFFSET:X})")
    print(f"Entidades encontradas: {len(report['entities'])} (raio {report['radius']})")
    for index, entity in enumerate(report["entities"]):
        print(f"  #{index} {entity['name']:<20} ({entity['x']},{entity['y']},{entity['z']}) "
              f"dist={entity['distance']} anchor=0x{entity['anchor']:X}")
    if not report["entities"]:
        print("  Nenhuma struct de entidade no raio. Aproxime-se de uma criatura ou aumente --radius.")
    for index, array in enumerate(report.get("lists", []), 1):
        print(f"\nEntity List candidata #{index}: base=0x{array['base']:X} "
              f"slots={array['slots']} entidades={array['distinct_entities']} "
              f"stride=0x{array.get('stride', 0):X}")
        for chain in array.get("chains", []):
            steps = " -> ".join(f"+0x{s:X}" for s in chain["steps"]) if chain["steps"] else "-"
            print(f"  chain level={chain['level']} {chain['module']}+0x{chain['rva']:X} "
                  f"steps=[{steps}] terminal=+0x{chain['terminal']:X}")
    print(f"\nCandidatos, nao offsets confirmados. offsets.json permanece intacto.")
    if report.get("truncated"):
        print("AVISO: resultado PARCIAL por limite de hits/candidatos.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("process", nargs="?", default="PokeAlliance_dx.exe")
    parser.add_argument("--offsets", default=None, help="Caminho de offsets.json")
    parser.add_argument("--player", nargs=2, type=int, metavar=("X", "Y"),
                        help="Sobrescreve a posicao do player")
    parser.add_argument("--radius", type=int, default=40)
    parser.add_argument("--name", help="Filtra por nome exato da criatura")
    parser.add_argument("--ptr-delta", type=parse_integer, default=POINTER_STRUCT_ZONE,
                        help="Delta maximo entre ponteiro e anchor da struct")
    parser.add_argument("--max-hits", type=int, default=4096)
    parser.add_argument("--chunk-size", type=parse_integer, default=4 * 1024 * 1024)
    parser.add_argument("--find-list", action="store_true",
                        help="Procurar a Entity List e resolver a cadeia estatica")
    parser.add_argument("--max-arrays", type=int, default=8,
                        help="Maximo de arrays candidatos reportados")
    parser.add_argument("--chain-lists", type=int, default=1,
                        help="Quantos arrays candidatos terao a chain resolvida")
    parser.add_argument("--max-offset", type=parse_integer, default=0x2000)
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--save", type=Path)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if args.radius <= 0:
        parser.error("--radius deve ser positivo")
    if args.max_arrays <= 0 or args.chain_lists < 0:
        parser.error("--max-arrays deve ser positivo e --chain-lists >= 0")
    if args.save and args.save.resolve().name.casefold() == "offsets.json":
        parser.error("Use outro arquivo para o relatorio; offsets.json deve permanecer intacto")

    memory = None
    try:
        if not MemoryReader.require_admin():
            print("FALHA: abra o PowerShell como Administrador.", file=sys.stderr)
            return 1
        target = int(args.process) if args.process.isdecimal() else args.process
        memory = MemoryReader(target)
        if not memory.attach():
            print(f"FALHA ao anexar {args.process}: {memory.last_error}", file=sys.stderr)
            return 1

        offsets = None
        try:
            offsets = read_offsets(args.offsets)
        except Exception as exc:
            log.warning("offsets.json indisponivel (%s); use --player X Y", exc)

        if args.player:
            px, py = args.player
            pz = 0
        else:
            position = read_player_position(memory, offsets)
            if position is None:
                print("FALHA: posicao do player indisponivel; informe --player X Y", file=sys.stderr)
                return 2
            px, py, pz = position

        print(f"Anexado ao PID {memory.pid}; player=({px},{py},{pz}); enumerando regioes.", flush=True)
        regions = list(iter_regions(memory))
        if not regions:
            raise RuntimeError("Nenhuma regiao legivel; verifique o processo e as permissoes")

        anchors = player_anchor(memory, offsets)
        print(f"Buscando structs de entidade em {len(regions)} regioes (raio {args.radius}).", flush=True)
        entities = find_entities(memory, regions, px, py, args.radius, name_filter=args.name,
                                 max_hits=args.max_hits, chunk_size=args.chunk_size)

        report = {"pid": memory.pid, "player": {"x": px, "y": py, "z": pz},
                  "player_anchor": anchors, "radius": args.radius,
                  "entities": entities, "lists": [], "verified": False,
                  "truncated": len(entities) >= args.max_hits}

        if args.find_list and len(entities) >= 2:
            print(f"Procurando Entity List para {len(entities)} candidatos.", flush=True)
            lists = find_entity_list(memory, regions, [e["anchor"] for e in entities],
                                     max_delta=args.ptr_delta, max_arrays=args.max_arrays,
                                     chunk_size=args.chunk_size)
            for index, array in enumerate(lists):
                if index >= args.chain_lists:
                    array["chains"] = []
                    continue
                print(f"Resolvendo chain para 0x{array['base']:X} "
                      f"({index + 1}/{min(len(lists), args.chain_lists)})...", flush=True)
                chains = build_chains(memory, target=array["base"], max_offset=args.max_offset,
                                      max_depth=args.max_depth)
                array["chains"] = sorted(chains, key=lambda c: (c["level"], c["rva"]))[:20]
            report["lists"] = lists

        print_report(report)
        if args.save:
            args.save.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Relatorio salvo em: {args.save.resolve()}")
        return 2 if report["truncated"] else 0
    except (KeyboardInterrupt, EOFError):
        print("\nScan cancelado; nenhum offset foi modificado.", file=sys.stderr)
        return 130
    except Exception:
        log.exception("Falha no scan de entidades")
        return 1
    finally:
        if memory is not None:
            memory.detach()


if __name__ == "__main__":
    raise SystemExit(main())
