"""Native SendInput ABI. Structure alignment follows the calling Python process."""
import ctypes
import logging
import time

WORD = ctypes.c_uint16
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
ULONG_PTR = ctypes.c_size_t
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD),
                ("dwFlags", DWORD), ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("payload",)
    _fields_ = [("type", DWORD), ("payload", INPUTUNION)]


def validate_layout():
    width = ctypes.sizeof(ctypes.c_void_p)
    expected = {4: (28, 16, 24, 4), 8: (40, 24, 32, 8)}[width]
    actual = (ctypes.sizeof(INPUT), ctypes.sizeof(KEYBDINPUT),
              ctypes.sizeof(MOUSEINPUT), INPUT.payload.offset)
    if actual != expected:
        raise RuntimeError(f"SendInput ABI mismatch: {actual}, expected {expected}")
    return actual


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint
    user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
    user32.MapVirtualKeyW.restype = ctypes.c_uint
    return user32


def send_inputs(events):
    validate_layout()
    if not events:
        return
    inputs = (INPUT * len(events))(*events)
    ctypes.set_last_error(0)
    sent = _user32().SendInput(len(inputs), inputs, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        error = ctypes.get_last_error()
        raise OSError(error, f"SendInput inserted {sent}/{len(inputs)} events; last_error={error}")


def keyboard_event(*, scan=0, vk=0, flags=0):
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, scan, flags, 0, 0))


def send_scancode(scancode, down):
    if not 0 < scancode <= 0xFFFF:
        raise ValueError("Invalid or unmapped scan code")
    flags = KEYEVENTF_SCANCODE | (0 if down else KEYEVENTF_KEYUP)
    prefix = scancode >> 8
    if prefix == 0xE0:
        flags |= KEYEVENTF_EXTENDEDKEY
    elif prefix:
        raise ValueError(f"Unsupported scan-code prefix: 0x{prefix:X}")
    send_inputs([keyboard_event(scan=scancode & 0xFF, flags=flags)])


def vk_to_scancode(vk):
    # MAPVK_VK_TO_VSC_EX preserves E0 for arrows, Insert, Delete, etc.
    return _user32().MapVirtualKeyW(vk, 4)


def scancode_to_vk(scancode):
    # MAPVK_VSC_TO_VK_EX
    return _user32().MapVirtualKeyW(scancode & 0xFF, 3)


def text_key_codes(text, thread_id=0):
    user32 = _user32()
    user32.GetKeyboardLayout.argtypes = [DWORD]
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    user32.VkKeyScanExW.argtypes = [ctypes.c_wchar, ctypes.c_void_p]
    user32.VkKeyScanExW.restype = ctypes.c_short
    user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
    user32.VkKeyScanW.restype = ctypes.c_short
    user32.GetKeyState.argtypes = [ctypes.c_int]
    user32.GetKeyState.restype = ctypes.c_short
    layout = user32.GetKeyboardLayout(thread_id)
    caps_lock = bool(user32.GetKeyState(0x14) & 1)
    strokes = []
    for character in text:
        mapped = (user32.VkKeyScanExW(character, layout) if thread_id
                  else user32.VkKeyScanW(character))
        if mapped == -1 or ((mapped >> 8) & ~7):
            raise ValueError(f"Caractere sem VK no layout selecionado: {character!r}")
        modifiers = (mapped >> 8) & 7
        if caps_lock and character.isascii() and character.isalpha():
            modifiers ^= 1
        keys = tuple(code for mask, code in ((1, 0x10), (2, 0x11), (4, 0x12))
                     if modifiers & mask) + (mapped & 0xFF,)
        strokes.append(keys)
    return strokes


def send_virtual_keys(codes):
    codes = tuple(codes)
    if not codes:
        return
    modifiers, key = codes[:-1], codes[-1]
    if not modifiers:
        # Tecla simples: down/up no mesmo lote e rapido.
        send_inputs([keyboard_event(vk=key),
                     keyboard_event(vk=key, flags=KEYEVENTF_KEYUP)])
        return
    # Shift/Ctrl/Alt precisam de tempo para o cliente processar o estado antes
    # do WM_CHAR. No mesmo lote o jogo le Shift solto e digita "1" em vez de "!".
    for modifier in modifiers:
        send_inputs([keyboard_event(vk=modifier)])
        time.sleep(0.020)
    send_inputs([keyboard_event(vk=key)])
    time.sleep(0.020)
    send_inputs([keyboard_event(vk=key, flags=KEYEVENTF_KEYUP)])
    time.sleep(0.010)
    for modifier in reversed(modifiers):
        send_inputs([keyboard_event(vk=modifier, flags=KEYEVENTF_KEYUP)])
        time.sleep(0.010)


def send_mouse_button(down, button="left"):
    if button not in ("left", "right"):
        raise ValueError("Mouse button must be left or right")
    pressed, released = ((MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP) if button == "left"
                         else (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP))
    flags = pressed if down else released
    send_inputs([INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, flags, 0, 0))])


def sendinput_selftest():
    """Explicit debug action only: send an F24 press/release, without changing focus."""
    log = logging.getLogger("pokebot.inputbus")
    try:
        send_inputs([keyboard_event(vk=0x87), keyboard_event(vk=0x87, flags=KEYEVENTF_KEYUP)])
    except OSError:
        # A partial batch may have inserted only the down event: attempt release.
        try:
            send_inputs([keyboard_event(vk=0x87, flags=KEYEVENTF_KEYUP)])
        except OSError:
            log.exception("SendInput self-test: F24 release also failed")
        log.exception("SendInput self-test failed")
        return False
    log.info("SendInput self-test OK: F24 down/up accepted by Windows")
    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="SendInput debug entry point")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.selftest:
        raise SystemExit(0 if sendinput_selftest() else 1)
    parser.print_help()
