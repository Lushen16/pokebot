import os
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QPushButton

from gui import icons


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("name", ["play", "stop", "pause", "map", "target", "settings", "save", "record",
                                  "dashboard", "loot", "debug", "load", "trash", "walk", "wait", "say",
                                  "safe", "box", "stair", "refresh", "plus", "position", "power", "heart"])
def test_fontawesome_icons_exist_and_render(app, monkeypatch, name):
    fallback = Mock(side_effect=AssertionError("FontAwesome name must exist"))
    monkeypatch.setattr(icons, "_svg_icon", fallback)
    image = icons.icon(name).pixmap(24, 24)
    assert not image.isNull()
    fallback.assert_not_called()


@pytest.mark.parametrize("name", ["play", "stop", "map", "save", "record", "wait", "say", "power"])
def test_native_svg_icons_work_without_qtawesome(app, monkeypatch, name):
    monkeypatch.setattr(icons, "_font_icon", Mock(side_effect=ImportError("not installed")))
    image = icons.icon(name).pixmap(24, 24).toImage()
    assert not image.isNull()
    assert any(image.pixelColor(horizontal, vertical).alpha() > 0
               for horizontal in range(image.width()) for vertical in range(image.height()))


def test_button_keeps_label_and_accessible_text(app):
    button = QPushButton("Salvar Hunt")
    icons.set_button_icon(button, "save", size=22)
    assert button.text() == "Salvar Hunt"
    assert not button.icon().isNull()
    assert button.iconSize().width() == 22
    button.deleteLater()


def test_icon_before_application_does_not_create_gui():
    import subprocess
    import sys
    command = "from gui.icons import icon; from PySide6.QtWidgets import QApplication; assert icon('play').isNull(); assert QApplication.instance() is None"
    result = subprocess.run([sys.executable, "-c", command], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_damaged_font_uses_native_svg(app, monkeypatch):
    monkeypatch.setattr(icons, "_font_icon", Mock(side_effect=Exception("font data damaged")))
    assert not icons.icon("power").pixmap(24, 24).isNull()
