from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication


ICONS = {
    "play": ("fa5s.play", "#00e676", '<path d="M8 4l13 8-13 8z" fill="currentColor"/>'),
    "stop": ("fa5s.stop", "#ff1744", '<rect x="5" y="5" width="14" height="14" rx="2" fill="currentColor"/>'),
    "pause": ("fa5s.pause", "#ffd54f", '<path d="M7 5v14M17 5v14" stroke-width="5"/>'),
    "map": ("fa5s.map-marked-alt", "#00b8ff", '<path d="M3 6l6-3 6 3 6-3v15l-6 3-6-3-6 3zM9 3v15M15 6v15"/>'),
    "target": ("fa5s.crosshairs", "#ff647c", '<circle cx="12" cy="12" r="7"/><path d="M12 1v6M12 17v6M1 12h6M17 12h6"/>'),
    "settings": ("fa5s.cog", "#aab5c5", '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="3"/><path d="M12 1v4M12 19v4M1 12h4M19 12h4M4 4l3 3M17 17l3 3M4 20l3-3M17 7l3-3"/>'),
    "save": ("fa5s.save", "#00b8ff", '<path d="M4 3h13l4 4v14H3V3zM7 3v6h10V3M7 21v-8h10v8"/>'),
    "record": ("fa5s.circle", "#ff1744", '<circle cx="12" cy="12" r="8" fill="currentColor"/>'),
    "dashboard": ("fa5s.tachometer-alt", "#00b8ff", '<path d="M3 19a10 10 0 1118 0zM12 14l5-7"/><circle cx="12" cy="14" r="2"/>'),
    "loot": ("fa5s.coins", "#ffd54f", '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 4 16 4 16 0V6M4 12c0 4 16 4 16 0"/>'),
    "debug": ("fa5s.bug", "#b69cff", '<rect x="7" y="7" width="10" height="14" rx="5"/><path d="M9 7V4h6v3M3 10h4M17 10h4M3 16h4M17 16h4M12 7v14"/>'),
    "load": ("fa5s.folder-open", "#ffc857", '<path d="M3 7V4h7l3 3h8v4M3 21V10h19l-4 11z"/>'),
    "trash": ("fa5s.trash-alt", "#ff647c", '<path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"/>'),
    "walk": ("fa5s.walking", "#00b8ff", '<circle cx="14" cy="4" r="2"/><path d="M4 12l5-4 5 3 5 1M12 9l-3 7-5 5M9 16l6 1 3 4"/>'),
    "wait": ("fa5s.hourglass-half", "#ffd54f", '<path d="M6 3h12M6 21h12M7 3v4l10 10v4M17 3v4L7 17v4M7 6h10M7 18h10"/>'),
    "say": ("fa5s.comment-dots", "#b69cff", '<path d="M3 4h18v13H9l-6 4zM7 10h1M11 10h1M15 10h1"/>'),
    "safe": ("fa5s.shield-alt", "#00e676", '<path d="M12 2l9 4-2 10-7 6-7-6L3 6zM8 12l3 3 5-6"/>'),
    "box": ("fa5s.box", "#ffc857", '<path d="M3 7l9-5 9 5v12l-9 3-9-3zM3 7l9 4 9-4M12 11v11"/>'),
    "stair": ("fa5s.level-up-alt", "#00b8ff", '<path d="M3 21h6v-6h6V9h6V3M17 7l4-4 2 4"/>'),
    "refresh": ("fa5s.sync-alt", "#00b8ff", '<path d="M20 8a8 8 0 00-14-3L3 8M3 3v5h5M4 16a8 8 0 0014 3l3-3M21 21v-5h-5"/>'),
    "plus": ("fa5s.plus", "#00e676", '<path d="M12 4v16M4 12h16"/>'),
    "position": ("fa5s.map-marker-alt", "#00b8ff", '<path d="M19 9c0 5-7 13-7 13S5 14 5 9a7 7 0 1114 0z"/><circle cx="12" cy="9" r="2"/>'),
    "power": ("fa5s.power-off", "#00e676", '<path d="M12 2v10M6 5a9 9 0 1012 0"/>'),
    "heart": ("fa5s.heart", "#ff647c", '<path d="M12 21L3 12C-2 4 8-1 12 6c4-7 14-2 9 6z"/>'),
    "lure": ("fa5s.fish", "#33c7ff", '<path d="M3 12c4-6 12-6 16 0-4 6-12 6-16 0z"/><circle cx="9" cy="12" r="1.4"/><path d="M19 9l3-3v6l-3-3z"/>'),
    "combo": ("fa5s.fire", "#ff8a3d", '<path d="M12 2s5 5 5 10a5 5 0 01-10 0c0-2 1-3 1-3s-1 4 2 6c2 1 4-1 4-3 0-3-4-4-2-10z"/>'),
    "revive": ("fa5s.heartbeat", "#3dffa0", '<path d="M2 12h4l2-4 3 8 2-4h9"/>'),
    "shiny": ("fa5s.star", "#ffd54f", '<path d="M12 3l3 6 6 .9-4.5 4.3 1 6.3L12 18l-5.5 3 1-6.3L3 9.9 9 9z"/>'),
    "skull": ("fa5s.skull", "#c9d1d9", '<circle cx="12" cy="10" r="7"/><circle cx="9" cy="10" r="1.6"/><circle cx="15" cy="10" r="1.6"/><path d="M9 20h6v-3H9z"/>'),
    "range": ("fa5s.ruler-combined", "#33c7ff", '<path d="M3 17l14-14 4 4L7 21zM8 12l2 2M11 9l2 2M14 6l2 2"/>'),
    "clock": ("fa5s.clock", "#33c7ff", '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    "stopwatch": ("fa5s.stopwatch", "#ffd54f", '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l3 2M9 2h6M12 5V2"/>'),
    "sort": ("fa5s.sort-amount-down", "#b69cff", '<path d="M4 6h9M4 12h7M4 18h5"/><path d="M18 8v10M15 15l3 3 3-3"/>'),
    "players": ("fa5s.user-slash", "#ff647c", '<circle cx="9" cy="8" r="4"/><path d="M3 21v-2a6 6 0 016-6M15 6l6 12M21 6l-6 12"/>'),
    "npc": ("fa5s.robot", "#8fb3d9", '<rect x="4" y="8" width="16" height="11" rx="3"/><circle cx="9" cy="13" r="1.5"/><circle cx="15" cy="13" r="1.5"/><path d="M12 4v4M18 11h3M3 11h3"/>'),
    "filter": ("fa5s.filter", "#b69cff", '<path d="M3 5h18l-7 8v6l-4-2v-4z"/>'),
    "chase": ("fa5s.running", "#33c7ff", '<circle cx="15" cy="4" r="1.8"/><path d="M9 21l3-6 4 2 1 4M6 10l4-3 4 3 4-1"/>'),
    "loop": ("fa5s.redo", "#3dffa0", '<path d="M21 8a9 9 0 10-2 10M21 3v5h-5"/>'),
    "bolt": ("fa5s.bolt", "#ffd54f", '<path d="M13 2L4 14h6l-1 8 9-12h-6z"/>'),
    "eye": ("fa5s.eye", "#33c7ff", '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>'),
    "sliders": ("fa5s.sliders-h", "#8fb3d9", '<path d="M4 8h10M18 8h2M4 16h4M12 16h8"/><circle cx="16" cy="8" r="2"/><circle cx="10" cy="16" r="2"/>'),
    "food": ("fa5s.utensils", "#ffd54f", '<path d="M6 2v8a2 2 0 004 0V2M8 12v10M17 2c-2 0-3 2-3 5s1 4 3 4v11"/>'),
    "catch": ("fa5s.bullseye", "#ff647c", '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1"/>'),
    "pokeball": ("fa5s.bullseye", "#dff8ff", '<circle cx="12" cy="12" r="9" fill="#0a1a29"/><path d="M3 12h18" stroke-width="3"/><circle cx="12" cy="12" r="3.4" fill="#53d8ff" stroke="#dff8ff"/>'),
}

ACTION_ICONS = {"Walk": "walk", "Wait": "wait", "Say": "say", "Safe Spot": "safe",
                "BOX": "box", "Stair": "stair"}


def _font_icon(name, color):
    import qtawesome
    return qtawesome.icon(ICONS[name][0], color=color, color_disabled="#667181")


def _svg_icon(name, color):
    artwork = ICONS[name][2].replace("currentColor", color)
    source = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
              f'fill="none" stroke="{color}" stroke-width="2.4" stroke-linecap="round" '
              f'stroke-linejoin="round">{artwork}</svg>')
    renderer = QSvgRenderer(QByteArray(source.encode("utf-8")))
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    return QIcon(pixmap)


def icon(name, color=None):
    default_color = ICONS[name][1]
    if not isinstance(QApplication.instance(), QApplication):
        return QIcon()
    resolved_color = color or default_color
    try:
        result = _font_icon(name, resolved_color)
        if not result.isNull():
            return result
    except Exception:
        return _svg_icon(name, resolved_color)
    return _svg_icon(name, resolved_color)


def set_button_icon(button, name, size=20, color=None):
    button.setProperty("iconName", name)
    button.setIcon(icon(name, color))
    button.setIconSize(QSize(size, size))
