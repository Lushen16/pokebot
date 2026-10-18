"""Contrato de roster dinamico, sem inferir nomes ou layouts desconhecidos."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RosterMember:
    slot: int
    name: str | None = None

    def __post_init__(self):
        if type(self.slot) is not int or not 1 <= self.slot <= 6:
            raise ValueError("slot deve estar entre 1 e 6")
        if self.name is not None and (
            not isinstance(self.name, str) or not self.name.strip()
            or len(self.name) > 128 or any(ord(character) < 32 for character in self.name)
        ):
            raise ValueError("nome de Pokemon invalido")

    @property
    def label(self):
        return f"{self.name} - Slot {self.slot} (tecla {self.slot})" if self.name else f"Slot {self.slot} (tecla {self.slot})"


@dataclass(frozen=True)
class RosterSnapshot:
    pid: int | None
    members: tuple[RosterMember, ...]
    detected: bool
    reason: str = ""


def unavailable_roster(pid, reason):
    return RosterSnapshot(pid, tuple(RosterMember(slot) for slot in range(1, 7)), False, reason)


class RosterProvider(Protocol):
    def get_roster(self, pid: int | None) -> RosterSnapshot: ...


class MemoryRosterProvider:
    """Adapter para um decoder futuro; nao trata um ponteiro como lista de nomes."""

    def __init__(self, resolver_provider=None):
        self.resolver_provider = resolver_provider or (lambda: None)

    def get_roster(self, pid):
        if type(pid) is not int or pid <= 0:
            return unavailable_roster(pid, "Selecione um cliente.")
        try:
            resolver = self.resolver_provider()
            if resolver is None:
                return unavailable_roster(pid, "Leitura de nomes da mochila ainda nao configurada.")
            if not resolver.proc.is_attached or resolver.proc.pid != pid:
                return unavailable_roster(pid, "Conexao de memoria nao corresponde ao PID selecionado.")
            entry = resolver.entries.get("pokemon.roster")
            if not entry or entry.get("verified") is not True or entry.get("base_rva") is None:
                return unavailable_roster(pid, "Offsets de pokemon.roster ainda nao validados.")
            decoder = getattr(resolver, "get_party_roster", None)
            if not callable(decoder):
                return unavailable_roster(pid, "Layout dos nomes da mochila ainda nao mapeado.")
            rows = decoder()
            if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 6:
                return unavailable_roster(pid, "Leitura da mochila vazia ou invalida.")
            found = {}
            for row in rows:
                if not isinstance(row, RosterMember) or row.name is None or row.slot in found:
                    return unavailable_roster(pid, "Nomes ou slots invalidos na leitura da mochila.")
                found[row.slot] = row
            members = tuple(found.get(slot, RosterMember(slot)) for slot in range(1, 7))
            return RosterSnapshot(pid, members, True)
        except Exception as exc:
            return unavailable_roster(pid, f"Falha ao ler mochila: {exc}")
