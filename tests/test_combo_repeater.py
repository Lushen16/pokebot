from core.combo_repeater import ComboRepeater


def test_trigger_sends_first_and_repeats_three_times(monkeypatch):
    sent = []
    repeater = ComboRepeater(lambda: sent.append(1) or True, count=3, interval=4.5)
    assert repeater.trigger(100.0) is True
    assert len(sent) == 1
    assert repeater.remaining == 2

    assert repeater.tick(104.0) == 0
    assert repeater.tick(104.5) == 1
    assert repeater.tick(109.0) == 1
    assert repeater.remaining == 0
    assert len(sent) == 3


def test_active_sequence_is_not_restarted():
    sent = []
    repeater = ComboRepeater(lambda: sent.append(1) or True, count=3, interval=4.5)
    repeater.trigger(0.0)
    assert repeater.trigger(1.0) is True
    assert repeater.remaining == 2
    assert len(sent) == 1


def test_failed_send_retries_without_losing_count():
    calls = {"n": 0}

    def send():
        calls["n"] += 1
        return calls["n"] > 1

    repeater = ComboRepeater(send, count=2, interval=1.0)
    repeater.trigger(0.0)
    assert repeater.remaining == 2
    assert repeater.tick(1.0) == 1
    assert repeater.remaining == 1


def test_cancel_stops_sequence():
    sent = []
    repeater = ComboRepeater(lambda: sent.append(1) or True, count=3, interval=1.0)
    repeater.trigger(0.0)
    repeater.cancel()
    assert repeater.tick(10.0) == 0
    assert len(sent) == 1
