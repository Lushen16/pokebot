from types import SimpleNamespace
from unittest.mock import Mock

from core.chat_snapshot import ChatEvent
from modules.alarms import Alarms


class Ctx:
    def __init__(self, alarms=None):
        self.config = {"alarms": dict(alarms or {})}

    def get(self, *keys, default=None):
        current = self.config
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
        return current


def make_alarm(monkeypatch, **alarms):
    cfg = {
        "chat_player_enabled": True,
        "chat_gm_enabled": True,
        "chat_ignore_self": True,
        "chat_include_trade": False,
        "chat_beep": False,
        "chat_webhook": False,
        "gm_name_patterns": [r".*Gamemaster.*", r"(^|\s)GM(\s|$)"],
    }
    cfg.update(alarms)
    alarm = Alarms(ctx=Ctx(cfg), resolver=SimpleNamespace(proc=None))
    alarm._beep_once = Mock()
    alarm._send_webhook = Mock()
    return alarm


def event(text="oi", name="Player", mode=1, self_=False, event_id=1):
    return ChatEvent(id=event_id, mode=mode, name=name, text=text, self=self_)


def test_player_message_is_recorded(monkeypatch):
    alarm = make_alarm(monkeypatch)
    alarm._handle_chat_event(event(text="hello"))
    assert alarm.recent_chat[-1]["text"] == "hello"
    assert alarm.recent_chat[-1]["gm"] is False
    alarm._beep_once.assert_not_called()
    alarm._send_webhook.assert_not_called()


def test_self_monster_and_trade_are_ignored(monkeypatch):
    alarm = make_alarm(monkeypatch)
    alarm._handle_chat_event(event(self_=True))
    alarm._handle_chat_event(event(mode=44))
    alarm._handle_chat_event(event(mode=40))
    alarm._handle_chat_event(event(mode=7, text="selling"))
    assert alarm.recent_chat == []
    assert alarm._beep_once.call_count == 0


def test_trade_can_be_included(monkeypatch):
    alarm = make_alarm(monkeypatch, chat_include_trade=True)
    alarm._handle_chat_event(event(mode=7, text="selling"))
    assert alarm.recent_chat[-1]["text"] == "selling"


def test_gm_pattern_marks_and_filters(monkeypatch):
    alarm = make_alarm(monkeypatch)
    alarm._handle_chat_event(event(name="GameMaster Bob", text="stop"))
    assert alarm.recent_chat[-1]["gm"] is True

    alarm = make_alarm(monkeypatch, chat_gm_enabled=False)
    alarm._handle_chat_event(event(name="GM Bob", text="stop"))
    assert alarm.recent_chat == []


def test_player_filter_and_beep(monkeypatch):
    alarm = make_alarm(monkeypatch, chat_player_enabled=False)
    alarm._handle_chat_event(event())
    assert alarm.recent_chat == []

    alarm = make_alarm(monkeypatch, chat_beep=True)
    alarm._handle_chat_event(event())
    alarm._beep_once.assert_called_once_with()


def test_invalid_gm_pattern_is_ignored(monkeypatch):
    alarm = make_alarm(monkeypatch, gm_name_patterns=["(", r"GM"])
    alarm._handle_chat_event(event(name="GM Bob", text="stop"))
    assert alarm.recent_chat[-1]["gm"] is True


def test_webhook_uses_label_and_detail(monkeypatch):
    alarm = make_alarm(monkeypatch, chat_webhook=True)
    alarm._handle_chat_event(event(name="GM Bob", text="hello"))
    alarm._send_webhook.assert_called_once()
    args = alarm._send_webhook.call_args.args
    assert args[0] == "chat_gm"
    assert "GM Bob: hello" in args[1]
