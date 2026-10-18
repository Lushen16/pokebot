from unittest.mock import Mock
import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from gui.process_selector import ProcessSelectorDialog


@pytest.fixture
def mock_processes():
    return [
        ("[System Process]", 0),
        ("AdobeCollabSync.exe", 7716),
        ("PokeAlliance_dx.exe", 31952),
        ("powershell.exe", 33560),
        ("spoolsv.exe", 4880),
    ]


@pytest.fixture
def dialog(monkeypatch, mock_processes):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    dlg = ProcessSelectorDialog(process_provider=lambda: mock_processes)
    yield dlg
    dlg.close()
    dlg.deleteLater()
    app.processEvents()


def test_dialog_populates_all_processes(dialog, mock_processes):
    assert dialog.list_widget.count() == len(mock_processes)
    items = [dialog.list_widget.item(i).text() for i in range(dialog.list_widget.count())]
    assert "PokeAlliance_dx.exe [PID 31952]" in items
    assert "[System Process] [PID 0]" in items


def test_dialog_filter_by_name(dialog):
    dialog.filter_edit.setText("po")
    # Must match PokeAlliance_dx.exe, powershell.exe, spoolsv.exe
    items = [dialog.list_widget.item(i).text() for i in range(dialog.list_widget.count())]
    assert len(items) == 3
    assert "PokeAlliance_dx.exe [PID 31952]" in items
    assert "powershell.exe [PID 33560]" in items
    assert "spoolsv.exe [PID 4880]" in items
    assert "[System Process] [PID 0]" not in items


def test_dialog_filter_by_pid(dialog):
    dialog.filter_edit.setText("31952")
    assert dialog.list_widget.count() == 1
    assert dialog.list_widget.item(0).text() == "PokeAlliance_dx.exe [PID 31952]"


def test_dialog_select_item(dialog):
    dialog.filter_edit.setText("PokeAlliance")
    assert dialog.list_widget.count() == 1
    item = dialog.list_widget.item(0)
    dialog.list_widget.setCurrentItem(item)

    mock_receiver = Mock()
    dialog.process_selected.connect(mock_receiver)
    dialog._on_select()

    assert dialog.selected_name == "PokeAlliance_dx.exe"
    assert dialog.selected_pid == 31952
    mock_receiver.assert_called_once_with("PokeAlliance_dx.exe", 31952)


def test_dialog_refresh(dialog):
    dialog.process_provider = lambda: [("NewGame.exe", 9999)]
    dialog.btn_refresh.click()
    assert dialog.list_widget.count() == 1
    assert dialog.list_widget.item(0).text() == "NewGame.exe [PID 9999]"
