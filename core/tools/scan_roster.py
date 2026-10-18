"""Find Pokemon name strings and module-relative pointer-chain candidates.

Run from an elevated PowerShell in the project root:
    python -m core.tools.scan_roster
    python -m core.tools.scan_roster --name Togepi --save roster-scan.json

Results are unverified candidates; offsets.json is never modified.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
import json
import logging
from pathlib import Path
import struct
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import MemoryReader
from core.tools.pointer_scan import format_static_chain
from core.tools.scan_value import iter_regions

log = logging.getLogger("pokebot.tools.scan_roster")
PAGE_SIZE = 4096


def parse_integer(text):
    try:
        return int(text, 0)
    except ValueError:
        try:
            return int(text, 10)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("Informe um inteiro decimal ou hexadecimal") from exc


def iter_blocks(memory, base, size, chunk_size, stats):
    def read_block(address, length):
        raw = memory.read_bytes(address, length)
        if raw is not None and len(raw) == length:
            stats["bytes_read"] += length
            yield address, raw
        elif length > PAGE_SIZE:
            split = max(PAGE_SIZE, (length // 2 // PAGE_SIZE) * PAGE_SIZE)
            yield from read_block(address, split)
            yield from read_block(address + split, length - split)
        else:
            stats["unreadable_blocks"] += 1
            yield address, None

    offset = 0
    while offset < size:
        length = min(chunk_size, size - offset)
        yield from read_block(base + offset, length)
        offset += length


def name_patterns(name):
    patterns = [("UTF-16LE", name.encode("utf-16le"))]
    try:
        patterns.insert(0, ("ASCII", name.encode("ascii")))
    except UnicodeEncodeError:
        log.warning("Nome fora de ASCII; procurando apenas UTF-16LE")
    return patterns


def find_names(memory, regions, name, chunk_size, max_hits, stats):
    patterns = name_patterns(name)
    overlap = max(len(needle) for _, needle in patterns) - 1
    hits = []
    seen = set()
    for base, size in regions:
        tail = b""
        expected = base
        for address, raw in iter_blocks(memory, base, size, chunk_size, stats):
            if raw is None:
                tail = b""
                expected = None
                continue
            if address != expected:
                tail = b""
            data = tail + raw
            data_base = address - len(tail)
            for encoding, needle in patterns:
                position = data.find(needle)
                while position >= 0:
                    location = data_base + position
                    identity = (location, encoding)
                    if identity not in seen:
                        seen.add(identity)
                        hits.append({"address": location, "encoding": encoding,
                                     "byte_length": len(needle), "region_base": base,
                                     "region_size": size, "matched_hex": needle.hex()})
                        if len(hits) >= max_hits:
                            return hits, True
                    position = data.find(needle, position + 1)
            tail = data[-overlap:] if overlap else b""
            expected = address + len(raw)
    return sorted(hits, key=lambda hit: (hit["address"], hit["encoding"])), False


def read_context(memory, hit, lookbehind):
    address = hit["address"]
    start = max(hit["region_base"], address - lookbehind)
    end = min(hit["region_base"] + hit["region_size"],
              address + hit["byte_length"] + lookbehind)
    raw = memory.read_bytes(start, end - start)
    return {"address": start, "size": end - start,
            "hex": raw.hex() if raw is not None else None}


def make_entry(memory, chain):
    static = format_static_chain(memory, list(chain))
    if static is None:
        return None
    return {"module": static["module"], "base_rva": static["rva"],
            "steps": static["steps"], "terminal": 0, "deref": False,
            "type": "uint8", "verified": False, "source": "assumed",
            "note": "scan_roster candidate; struct and roster layout not verified"}


def find_static_chains(memory, regions, hits, *, lookbehind=0x100,
                       max_offset=0x2000, max_depth=4, chunk_size=4 * 1024 * 1024,
                       alignment=None, max_frontier=20000, max_chains=256, stats=None):
    if memory.pointer_size not in (4, 8):
        raise ValueError("Largura de ponteiro deve ser 4 ou 8 bytes")
    stats = stats if stats is not None else {"bytes_read": 0, "unreadable_blocks": 0}
    alignment = memory.pointer_size if alignment is None else alignment
    if alignment not in (1, memory.pointer_size):
        raise ValueError("Alinhamento deve ser 1 ou a largura do ponteiro")
    unpacker = struct.Struct("<I" if memory.pointer_size == 4 else "<Q")
    frontier = {}
    for index, hit in enumerate(hits):
        frontier.setdefault(hit["address"], []).append((index, (), None))
    results = []
    seen_results = set()
    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        print(f"[Ponteiros {depth}/{max_depth}] {sum(map(len, frontier.values()))} caminhos candidatos", flush=True)
        targets = sorted(frontier)
        next_frontier = {}
        next_seen = set()
        window = lookbehind if depth == 1 else max_offset
        for base, size in regions:
            tail = b""
            expected = base
            for address, raw in iter_blocks(memory, base, size, chunk_size, stats):
                if raw is None:
                    tail = b""
                    expected = None
                    continue
                if address != expected:
                    tail = b""
                data = tail + raw
                data_base = address - len(tail)
                for position in range((-data_base) % alignment, len(data) - unpacker.size + 1, alignment):
                    parent = data_base + position
                    if parent + unpacker.size <= address:
                        continue
                    value = unpacker.unpack_from(data, position)[0]
                    if value <= 0x10000:
                        continue
                    lower = bisect_left(targets, value)
                    upper = bisect_right(targets, value + window)
                    for child in targets[lower:upper]:
                        for index, chain, struct_base in frontier[child]:
                            if parent == hits[index]["address"] or any(parent == link[0] for link in chain):
                                continue
                            candidate_base = value if struct_base is None else struct_base
                            if candidate_base < hits[index]["region_base"]:
                                continue
                            new_chain = ((parent, child - value),) + chain
                            entry = make_entry(memory, new_chain)
                            if entry is not None:
                                struct_chain = new_chain[:-1] + ((new_chain[-1][0], 0),)
                                struct_entry = make_entry(memory, struct_chain)
                                identity = (index, entry["module"], entry["base_rva"], tuple(entry["steps"]))
                                if identity in seen_results:
                                    continue
                                if (memory.resolve_entry_address(entry) != hits[index]["address"]
                                        or memory.resolve_entry_address(struct_entry) != candidate_base):
                                    continue
                                original = bytes.fromhex(hits[index]["matched_hex"])
                                current = memory.read_bytes(hits[index]["address"], hits[index]["byte_length"])
                                if current != original:
                                    continue
                                seen_results.add(identity)
                                results.append({"hit_index": index, "string_address": hits[index]["address"],
                                                "candidate_struct_address": candidate_base,
                                                "name_offset": hits[index]["address"] - candidate_base,
                                                "string_entry": entry, "struct_entry": struct_entry})
                                if len(results) >= max_chains:
                                    return results, True
                            else:
                                identity = (index, new_chain)
                                if identity not in next_seen:
                                    if len(next_seen) >= max_frontier:
                                        return results, True
                                    next_seen.add(identity)
                                    next_frontier.setdefault(parent, []).append((index, new_chain, candidate_base))
                tail = data[-(unpacker.size - 1):]
                expected = address + len(raw)
        frontier = next_frontier
    return results, False


def print_report(report):
    print("\n=== RESULTADO DO SCAN DE ROSTER ===")
    print(f"PID: {report['pid']} | Ponteiros: {report['pointer_size'] * 8} bits")
    print(f"Nome: {report['name']} | Strings: {len(report['hits'])} | Chains: {len(report['chains'])}")
    for index, hit in enumerate(report["hits"]):
        print(f"\nString #{index}: 0x{hit['address']:X} [{hit['encoding']}]")
        context = hit["context"]
        if context["hex"] is None:
            print("  Bloco anterior indisponivel; memoria pode ter mudado.")
        else:
            raw = bytes.fromhex(context["hex"])
            print(f"  Contexto desde 0x{context['address']:X} ({len(raw)} bytes):")
            for offset in range(0, len(raw), 16):
                row = raw[offset:offset + 16]
                text = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in row)
                print(f"  {context['address'] + offset:016X}  {row.hex(' '):47}  {text}")
    for index, result in enumerate(report["chains"], 1):
        print(f"\nChain #{index}: string 0x{result['string_address']:X}")
        print(f"  Base CANDIDATA: 0x{result['candidate_struct_address']:X}; nome +0x{result['name_offset']:X}")
        print("  Entrada candidata para a base (formato offsets.json):")
        print(json.dumps({"pokemon.roster": result["struct_entry"]}, indent=2, ensure_ascii=False))
        print("  Entrada que resolve a string:")
        print(json.dumps(result["string_entry"], indent=2, ensure_ascii=False))
    if not report["chains"]:
        print("Nenhuma chain estatica revalidada dentro dos limites configurados.")
    print(f"Blocos ilegíveis: {report['stats']['unreadable_blocks']}")
    if report["truncated"]:
        print("ATENCAO: resultado PARCIAL por limite de hits, caminhos ou chains. Aumente os limites.")
    print("Candidatos, nao offsets confirmados. Revalide apos reiniciar o jogo e trocar os slots.")
    print("Uma string pode pertencer ao chat, interface ou cache; mochila e layout ainda exigem validacao.")
    print("offsets.json nao foi alterado; verified permanece false.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("process", nargs="?", default="PokeAlliance_dx.exe", help="Nome do processo ou PID")
    parser.add_argument("--name", help="Nome exato; se omitido, pergunta no terminal")
    parser.add_argument("--lookbehind", type=parse_integer, default=0x100)
    parser.add_argument("--max-offset", type=parse_integer, default=0x2000)
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--max-hits", type=int, default=128)
    parser.add_argument("--max-frontier", type=int, default=20000)
    parser.add_argument("--max-chains", type=int, default=256)
    parser.add_argument("--chunk-size", type=parse_integer, default=4 * 1024 * 1024)
    parser.add_argument("--unaligned", action="store_true", help="Incluir ponteiros desalinhados (mais lento)")
    parser.add_argument("--save", type=Path, help="Salvar relatorio JSON sem modificar offsets.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    for key in ("max_depth", "max_hits", "max_frontier", "max_chains", "chunk_size"):
        if getattr(args, key) <= 0:
            parser.error(f"--{key.replace('_', '-')} deve ser positivo")
    if args.lookbehind < 0 or args.max_offset < 0:
        parser.error("Offsets maximos nao podem ser negativos")
    if args.save and args.save.resolve().name.casefold() == "offsets.json":
        parser.error("Use outro arquivo para o relatorio; offsets.json deve permanecer intacto")
    memory = None
    try:
        name = (args.name if args.name is not None else input("Nome do Pokemon (ex.: Togepi): ")).strip()
        if not name or len(name) > 128 or any(ord(character) < 32 for character in name):
            parser.error("Informe um nome de 1 a 128 caracteres sem controles")
        if not MemoryReader.require_admin():
            print("FALHA: abra o PowerShell como Administrador.", file=sys.stderr)
            return 1
        target = int(args.process) if args.process.isdecimal() else args.process
        memory = MemoryReader(target)
        if not memory.attach():
            print(f"FALHA ao anexar {args.process}: {memory.last_error}", file=sys.stderr)
            return 1
        print(f"Anexado ao PID {memory.pid}; enumerando regioes legiveis.", flush=True)
        regions = list(iter_regions(memory))
        if not regions:
            raise RuntimeError("Nenhuma regiao legivel; verifique o processo e as permissoes")
        stats = {"bytes_read": 0, "unreadable_blocks": 0}
        print(f"Buscando ASCII e UTF-16LE em {len(regions)} regioes.", flush=True)
        hits, truncated = find_names(memory, regions, name, args.chunk_size, args.max_hits, stats)
        for hit in hits:
            hit["context"] = read_context(memory, hit, args.lookbehind)
        chains, chain_limit = find_static_chains(
            memory, regions, hits, lookbehind=args.lookbehind, max_offset=args.max_offset,
            max_depth=args.max_depth, chunk_size=args.chunk_size,
            alignment=1 if args.unaligned else memory.pointer_size,
            max_frontier=args.max_frontier, max_chains=args.max_chains, stats=stats)
        report = {"pid": memory.pid, "name": name, "pointer_size": memory.pointer_size,
                  "hits": hits, "chains": chains, "stats": stats, "truncated": truncated or chain_limit,
                  "regions": len(regions), "verified": False,
                  "limits": {key: getattr(args, key) for key in
                             ("lookbehind", "max_offset", "max_depth", "max_hits", "max_frontier", "max_chains")}}
        print_report(report)
        if args.save:
            args.save.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Relatorio salvo em: {args.save.resolve()}")
        return 2 if report["truncated"] else 0
    except (KeyboardInterrupt, EOFError):
        print("\nScan cancelado; nenhum offset foi modificado.", file=sys.stderr)
        return 130
    except Exception:
        log.exception("Falha no scan de roster")
        return 1
    finally:
        if memory is not None:
            memory.detach()


if __name__ == "__main__":
    raise SystemExit(main())
