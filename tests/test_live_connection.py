import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import memory
from modules.alarms import Alarms


@pytest.fixture
def connection(tmp_path, monkeypatch):
    entry = dict(module="client.exe", base_rva=0x1000, steps=[0], terminal=0,
                 deref=False, type="int32", verified=True, source="verified_ce", note="synthetic")
    entries = {"player.hp": entry, "player.hp_max": dict(entry, terminal=4)}
    for field in ("is_dead", "is_online"):
        entries[f"player.{field}"] = dict(entry, base_rva=None, steps=None, terminal=None,
                                           verified=False, source="assumed")
    offsets = tmp_path / "offsets.json"
    offsets.write_text(json.dumps({"$schema": "offsets.schema.json", "schema_version": 2,
                                   "entries": entries}), encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    proc = SimpleNamespace(pid=123, base=0x10000000, pointer_size=8, is_attached=True,
                           last_error=None, attach=Mock(return_value=True), detach=Mock(),
                           modules={"client.exe": {"base": 0x10000000, "size": 0x100000}},
                           read_ptr=Mock(return_value=0x30000000), is_valid_addr=Mock(return_value=True),
                           read_value=Mock(side_effect=lambda addr, kind: 100 if addr == 0x30000000 else 200))
    factory = Mock(return_value=proc)
    monkeypatch.setattr(memory, "Process", factory)
    return proc, factory, offsets, config


def connect(connection):
    _, _, offsets, config = connection
    return memory.connect_live_resolver(123, offsets_path=offsets, config_path=config)


def test_connection_reuses_selected_process_and_logs_paths(connection, caplog):
    proc, factory, offsets, _ = connection
    with caplog.at_level("INFO", logger="pokebot.memory"):
        resolver = connect(connection)
    factory.assert_called_once_with(123, config={})
    proc.attach.assert_called_once_with()
    proc.detach.assert_not_called()
    assert resolver.proc is proc
    assert resolver.read_field("player", "hp") == 100
    assert str(offsets.resolve()) in caplog.text
    assert "PID=123" in caplog.text


def test_failed_attach_aborts_and_releases_process(connection):
    proc = connection[0]
    proc.attach.return_value = False
    proc.last_error = "acesso negado"
    with pytest.raises(RuntimeError, match="acesso negado"):
        connect(connection)
    proc.detach.assert_called_once_with()
    proc.read_ptr.assert_not_called()


def test_mismatched_pid_aborts_before_reading(connection):
    proc = connection[0]
    proc.pid = 999
    with pytest.raises(RuntimeError, match="PID selecionado"):
        connect(connection)
    proc.detach.assert_called_once_with()
    proc.read_ptr.assert_not_called()


@pytest.mark.parametrize("attached", [False])
def test_unattached_process_cannot_pass_preflight(connection, attached):
    proc = connection[0]
    proc.is_attached = attached
    with pytest.raises(RuntimeError):
        connect(connection)
    proc.read_ptr.assert_not_called()


@pytest.mark.parametrize("pointer,message", [(0, "ponteiro nulo"), (None, "falha ao ler ponteiro")])
def test_broken_root_aborts_with_specific_reason(connection, pointer, message):
    proc = connection[0]
    proc.read_ptr.return_value = pointer
    with pytest.raises(RuntimeError, match=message) as error:
        connect(connection)
    assert "0x10001000" in str(error.value)
    assert "PID 123" in str(error.value)
    proc.detach.assert_called_once_with()
    proc.read_value.assert_not_called()


@pytest.mark.parametrize("values", [(None, 200), (100, None), (100, 0), (-1, 200), (201, 200)])
def test_unreadable_or_invalid_hp_blocks_start(connection, values):
    proc = connection[0]
    proc.read_value.side_effect = values
    with pytest.raises(RuntimeError):
        connect(connection)
    proc.detach.assert_called_once_with()


def test_resolver_initialization_error_releases_handle(connection):
    proc, _, offsets, _ = connection
    offsets.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        connect(connection)
    proc.detach.assert_called_once_with()


def test_optional_unverified_fields_never_read_or_warn(connection, caplog):
    resolver = connect(connection)
    resolver.proc.read_ptr.reset_mock()
    for field in ("is_dead", "is_online", "missing"):
        assert resolver.read_optional_field("player", field) is None
    resolver.proc.read_ptr.assert_not_called()
    assert "ASSUMED" not in caplog.text


def test_alarm_unknown_status_preserves_prior_state(connection, caplog):
    resolver = connect(connection)
    resolver.read_entry = Mock(wraps=resolver.read_entry)
    alarm = Alarms.__new__(Alarms)
    alarm.resolver = resolver
    alarm.ctx = SimpleNamespace(get=lambda *keys, default=None: ["death"] if keys[-1] == "beep_on" else default)
    alarm._was_dead = True
    alarm._was_online = True
    alarm._disconnect_grace = 0.0
    alarm._gm_patterns = []
    alarm._trigger = Mock()
    alarm.tick()
    assert alarm._was_dead is True
    assert alarm._was_online is True
    assert resolver.read_entry.call_args_list == [call("player.hp"), call("player.hp_max")]
    alarm._trigger.assert_not_called()
    assert "ASSUMED" not in caplog.text


def test_optional_verified_field_keeps_real_read(connection):
    resolver = connect(connection)
    assert resolver.read_optional_field("player", "hp") == 100


def test_default_offset_path_is_independent_of_working_directory(connection, monkeypatch):
    proc, _, _, config = connection
    monkeypatch.chdir(config.parent)
    monkeypatch.setattr(memory.Resolver, "read_field", lambda self, block, field: 100 if field == "hp" else 200)
    resolver = memory.connect_live_resolver(123, config_path=config)
    assert Path(resolver.offsets_path) == Path(memory.DEFAULT_OFFSETS_PATH)
    assert resolver.proc is proc
