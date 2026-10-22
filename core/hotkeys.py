from __future__ import annotations


KEY_CODES = {
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "pause": 0x13, "printscreen": 0x2C, "capslock": 0x14,
    "numlock": 0x90, "scrolllock": 0x91,
    "=": 0xBB, "plus": 0xBB, "-": 0xBD, ";": 0xBA, ",": 0xBC,
    ".": 0xBE, "/": 0xBF, "`": 0xC0, "[": 0xDB, chr(92): 0xDC, "]": 0xDD, "'": 0xDE,
    "numplus": 0x6B, "numminus": 0x6D, "nummultiply": 0x6A,
    "numdivide": 0x6F, "numdecimal": 0x6E, "numenter": 0x0D,
}
KEY_CODES.update({f"f{number}": 0x6F + number for number in range(1, 25)})
KEY_CODES.update({f"num{number}": 0x60 + number for number in range(10)})


def split_hotkey(text: str) -> list[str]:
    text = str(text or "").strip()
    if text == "+":
        text = "Plus"
    elif text.endswith("++"):
        text = text[:-1] + "Plus"
    return [part.strip() for part in text.split("+")] if text else []


def resolve_key(text: str) -> int:
    key = str(text).strip().lower()
    if key in KEY_CODES:
        return KEY_CODES[key]
    if len(key) == 1 and key in "abcdefghijklmnopqrstuvwxyz0123456789":
        return ord(key.upper())
    if key.startswith("vk_0x"):
        try:
            code = int(key[3:], 16)
        except ValueError:
            pass
        else:
            if 1 <= code <= 0xFE:
                return code
    raise ValueError(f"Tecla não reconhecida: {text}")
