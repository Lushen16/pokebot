"""Pattern scanner (IDA-style) para localizar enderecos no modulo do jogo.

As assinaturas resolvem alvos por bytes de codigo em vez de RVA fixo, o que
sobrevive a ASLR e a pequenas mudancas de build. Formato:

    "48 8B 0D ?? ?? ?? ?? 48 85 C9"

Cada token e um byte hex (`48`, `8B`) ou curinga (`??`, `?`, `**`). Tambem
aceita string continua ("488B0D????????").

Para referencias RIP-relativas, informe `disp_offset` (posicao do disp32 dentro
da assinatura) e `instr_length` (tamanho total da instrucao). O alvo e:

    alvo = endereco_do_match + instr_length + int32(mem[ match + disp_offset ])
"""

from __future__ import annotations

import re
import struct
from typing import List, Optional, Sequence, Tuple

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
WILDCARDS = frozenset({"??", "?", "**", ".."})
BOUNDARY_OVERLAP_MIN = 1


class SignatureError(Exception):
    """Assinatura invalida ou nao encontrada."""


class ModuleView:
    """Adapta um leitor simples (addr, size) -> bytes ao contrato do scanner."""

    def __init__(self, read_bytes, base: int, size: int, name: Optional[str] = None):
        self._read_bytes = read_bytes
        self.main_module_base = int(base)
        self.main_module_size = int(size)
        self.modules = ({name.lower(): {"base": int(base), "size": int(size)}}
                        if name else {})

    def read_bytes(self, address: int, size: int):
        return self._read_bytes(address, size)


def compile_pattern(pattern: str) -> Tuple[bytes, bytes]:
    """Converte texto em (bytes, mascara). Mascara 1 = byte obrigatorio."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise SignatureError("assinatura vazia")

    tokens = pattern.replace(",", " ").split()
    if len(tokens) == 1 and len(tokens[0]) > 2:
        compact = tokens[0]
        if len(compact) % 2 != 0:
            raise SignatureError(f"hex continuo com tamanho impar: {compact!r}")
        tokens = [compact[i:i + 2] for i in range(0, len(compact), 2)]

    values = bytearray()
    mask = bytearray()
    for token in tokens:
        if token in WILDCARDS:
            values.append(0)
            mask.append(0)
            continue
        if not re.fullmatch(r"[0-9A-Fa-f]{2}", token):
            raise SignatureError(f"token invalido na assinatura: {token!r}")
        values.append(int(token, 16))
        mask.append(1)

    if not values:
        raise SignatureError("assinatura sem bytes")
    return bytes(values), bytes(mask)


def _compile_regex(values: bytes, mask: bytes) -> "re.Pattern[bytes]":
    parts = [re.escape(bytes([value])) if required else b"."
             for value, required in zip(values, mask)]
    return re.compile(b"".join(parts), re.DOTALL)


def _module_extent(memory, module_name: Optional[str]) -> Tuple[int, int]:
    name = (module_name or "").strip()
    if not name:
        base = getattr(memory, "main_module_base", 0)
        size = getattr(memory, "main_module_size", 0)
    else:
        mod = (getattr(memory, "modules", {}) or {}).get(name.lower())
        if not mod:
            raise SignatureError(f"modulo nao encontrado: {module_name!r}")
        base, size = int(mod["base"]), int(mod["size"])
    if not base or not size:
        raise SignatureError(f"extensao invalida para modulo {module_name!r}")
    return base, size


def find_pattern(
    memory,
    pattern: str,
    module_name: Optional[str] = None,
    *,
    max_hits: int = 32,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> List[int]:
    """Retorna os enderecos (VA) de todas as ocorrencias, ate `max_hits`."""
    if max_hits <= 0:
        raise SignatureError("max_hits deve ser > 0")
    if chunk_size <= 0:
        raise SignatureError("chunk_size deve ser > 0")

    values, mask = compile_pattern(pattern)
    regex = _compile_regex(values, mask)
    base, size = _module_extent(memory, module_name)
    overlap = max(BOUNDARY_OVERLAP_MIN, len(values) - 1)
    step = max(1, chunk_size)

    hits: List[int] = []
    offset = 0
    while offset < size:
        take = min(step + overlap, size - offset)
        chunk = memory.read_bytes(base + offset, take)
        if chunk:
            for match in regex.finditer(chunk):
                if match.start() >= step:
                    continue  # pertence a janela primaria seguinte; evita duplicar
                hits.append(base + offset + match.start())
                if len(hits) >= max_hits:
                    return hits
        offset += step
    return hits


def read_rip_displacement(memory, match_addr: int, disp_offset: int) -> Optional[int]:
    raw = memory.read_bytes(match_addr + int(disp_offset), 4)
    if raw is None or len(raw) != 4:
        return None
    return struct.unpack("<i", raw)[0]


def resolve_address(
    memory,
    match_addr: int,
    *,
    disp_offset: Optional[int] = None,
    instr_length: Optional[int] = None,
) -> Optional[int]:
    """Resolve o alvo de um match. Sem `disp_offset`, devolve o proprio match."""
    if disp_offset is None:
        return match_addr
    if instr_length is None:
        raise SignatureError("instr_length e obrigatorio quando disp_offset e usado")
    disp = read_rip_displacement(memory, match_addr, disp_offset)
    if disp is None:
        return None
    return match_addr + int(instr_length) + disp


def resolve_signature(
    memory,
    pattern: str,
    module_name: Optional[str] = None,
    *,
    disp_offset: Optional[int] = None,
    instr_length: Optional[int] = None,
    max_hits: int = 2,
) -> int:
    """Resolve uma assinatura unica para um VA. Levanta SignatureError se falhar."""
    hits = find_pattern(memory, pattern, module_name, max_hits=max_hits)
    if not hits:
        raise SignatureError(f"assinatura nao encontrada: {pattern[:32]}...")
    if len(hits) > 1:
        raise SignatureError(f"assinatura ambigua ({len(hits)} ocorrencias)")
    target = resolve_address(memory, hits[0], disp_offset=disp_offset,
                             instr_length=instr_length)
    if target is None:
        raise SignatureError("falha ao resolver deslocamento RIP")
    return target


def count_matches(memory, pattern: str, module_name: Optional[str] = None, *,
                  chunk_size: int = DEFAULT_CHUNK_SIZE) -> int:
    """Conta ocorrencias (pode ser usado para validar unicidade offline)."""
    values, mask = compile_pattern(pattern)
    regex = _compile_regex(values, mask)
    base, size = _module_extent(memory, module_name)
    overlap = max(BOUNDARY_OVERLAP_MIN, len(values) - 1)
    step = max(1, chunk_size)

    total = 0
    offset = 0
    while offset < size:
        take = min(step + overlap, size - offset)
        chunk = memory.read_bytes(base + offset, take)
        if chunk:
            total += sum(1 for match in regex.finditer(chunk) if match.start() < step)
        offset += step
    return total


__all__ = [
    "SignatureError",
    "ModuleView",
    "compile_pattern",
    "find_pattern",
    "resolve_address",
    "resolve_signature",
    "read_rip_displacement",
    "count_matches",
]
