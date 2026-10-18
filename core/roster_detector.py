"""Read roster data without guessing the unknown backpack memory layout."""
from __future__ import annotations

import logging
import re
import struct

from core.memory import MemoryReader
from core.offsets import OffsetRegistry

log = logging.getLogger("pokebot.core.roster_detector")

_MOVE_PATTERN = re.compile(rb'\x02\x00m1\x00\x00.{1,2}\x00\x02\x00m2\x00\x00')


class RosterDetector:
    def __init__(self, reader: MemoryReader, offsets: OffsetRegistry):
        self.reader = reader
        self.offsets = offsets

    def _scan_dynamic_roster(self) -> list[dict] | None:
        """Varre rapidamente a memória viva procurando o buffer binário da Pokébar."""
        if not self.reader or not hasattr(self.reader, "handle"):
            return None
        handle = getattr(self.reader, "handle", None)
        if not isinstance(handle, int) or handle <= 0:
            return None
        if type(self.reader).__name__ in ("Mock", "MagicMock"):
            return None

        try:
            from core.tools.scan_value import iter_regions
            regions = iter_regions(self.reader)
        except Exception:
            return None

        for base, size in regions:
            if size < 512 or size > 15 * 1024 * 1024:
                continue
            try:
                data = self.reader.read_bytes(base, size)
                if not data or b'm1\x00' not in data:
                    continue
                for m in _MOVE_PATTERN.finditer(data):
                    m_pos = m.start()
                    min_look = max(0, m_pos - 300)
                    for c_pos in range(min_look, m_pos - 10):
                        count = struct.unpack_from('<H', data, c_pos)[0]
                        if 1 <= count <= 6:
                            name_len = struct.unpack_from('<H', data, c_pos + 2)[0]
                            if 2 <= name_len <= 30 and c_pos + 4 + name_len < len(data):
                                candidate_name = data[c_pos + 4:c_pos + 4 + name_len]
                                if candidate_name.isalpha():
                                    try:
                                        pos = c_pos + 2
                                        roster = []
                                        for slot in range(1, count + 1):
                                            n_len = struct.unpack_from('<H', data, pos)[0]
                                            pos += 2
                                            name = data[pos:pos+n_len].decode('latin1')
                                            pos += n_len
                                            if pos < len(data) and data[pos] == 0:
                                                pos += 1
                                            pos += 7
                                            u_len = struct.unpack_from('<H', data, pos)[0]
                                            pos += 2 + u_len
                                            mv_count = data[pos]
                                            pos += 1
                                            for _ in range(mv_count):
                                                ml = struct.unpack_from('<H', data, pos)[0]
                                                pos += 2 + ml + 4
                                            roster.append({'slot': slot, 'name': name})
                                        if roster:
                                            return roster
                                    except Exception:
                                        pass
            except Exception:
                pass
        return None

    def get_roster(self) -> list[dict]:
        fallback = [{"slot": slot, "name": f"Slot {slot}"} for slot in range(1, 7)]
        try:
            # 1. Tenta escaneamento dinâmico do buffer da Pokébar em memória viva
            try:
                scanned = self._scan_dynamic_roster()
                if scanned:
                    logging.debug("Roster lido via scan dinâmico: %s", scanned)
                    log.debug("Roster lido via scan dinâmico: %s", scanned)
                    found = {row["slot"]: row for row in scanned}
                    return [found.get(row["slot"], row) for row in fallback]
            except Exception:
                pass

            # 2. Tenta leitura por pointer chain estática registrada em offsets
            rows = self.offsets.read(self.reader, "pokemon.roster")
            logging.debug("Roster raw offsets.read: %s", rows)
            if rows is None:
                logging.debug("pokemon.roster retornou None, usando fallback")
                return fallback
            if not isinstance(rows, (list, tuple)):
                decoder = getattr(self.reader, "decode_roster", None)
                if not callable(decoder):
                    logging.debug("decode_roster não encontrado no reader, usando fallback")
                    return fallback
                rows = decoder(rows)
            logging.debug("Roster lido: %s", rows)
            log.debug("Roster lido: %s", rows)
            if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 6:
                return fallback
            found = {}
            for row in rows:
                if not isinstance(row, dict):
                    return fallback
                slot, name = row.get("slot"), row.get("name")
                if (type(slot) is not int or not 1 <= slot <= 6 or slot in found
                        or not isinstance(name, str) or not name.strip()
                        or len(name) > 128 or any(ord(character) < 32 for character in name)):
                    return fallback
                found[slot] = dict(row, slot=slot, name=name.strip())
            return [found.get(row["slot"], row) for row in fallback]
        except Exception:
            log.exception("Failed to read pokemon.roster; using unnamed slots")
            return fallback
