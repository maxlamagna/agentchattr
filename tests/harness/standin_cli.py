"""A stand-in CLI for ad-519's tmux tests: draws Claude-shaped screens and logs every byte it receives.

Runs inside a tmux pane on a private server. argv: <state file> <events file>. The state file holds one word:
idle, working, typed or dialog; the test flips it and this redraws within about 5 ms. An approval dialog here
accepts on Enter, like the real ones. Not a model of any real CLI's timing.
"""
import json
import os
import select
import sys
import time
import tty

state_path, events_path = sys.argv[1], sys.argv[2]
RULE = "─" * 60
SCREENS = {
    "idle": "%s\r\n❯ {buffer}\r\n%s\r\n  ? for shortcuts\r\n" % (RULE, RULE),
    "typed": "%s\r\n❯ draft by a person\r\n%s\r\n  ? for shortcuts\r\n" % (RULE, RULE),
    "working": "· Working…\r\n\r\n%s\r\n\x1b[38;5;246m❯ \x1b[39m\r\n%s\r\n  esc to interrupt\r\n" % (RULE, RULE),
    "dialog": "%s\r\n Bash command\r\n Do you want to proceed?\r\n ❯ 1. Yes\r\n   2. No\r\n\r\n Esc to cancel\r\n" % RULE,
}


def read_state():
    try:
        with open(state_path, encoding="utf-8") as fh:
            return fh.read().strip() or "idle"
    except OSError:
        return "idle"


def event(kind, **fields):
    with open(events_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": time.time(), "kind": kind, **fields}) + "\n")


def draw(state, buffer):
    text = SCREENS.get(state, "unknown screen\r\n").replace("{buffer}", buffer)
    os.write(1, ("\x1b[2J\x1b[H" + text).encode("utf-8"))


fd = sys.stdin.fileno()
tty.setraw(fd)
os.write(1, b"\x1b[?2004h")
state, buffer = read_state(), ""
draw(state, buffer)
event("ready")
while True:
    ready, _, _ = select.select([fd], [], [], 0.005)
    now_state = read_state()
    if now_state != state:
        state = now_state
        if state == "reset":
            buffer = ""
            state = "idle"
        draw(state, buffer)
        event("state", state=state)
    if not ready:
        continue
    data = os.read(fd, 65536)
    if not data:
        break
    text = data.decode("utf-8", errors="replace")
    event("bytes", state=state, text=text, enter="\r" in text or "\n" in text)
    if state == "dialog":
        if "\r" in text or "\n" in text:
            event("approved")
        continue
    body = text.replace("\x1b[200~", "").replace("\x1b[201~", "")
    if state == "idle":
        if "\r" in body or "\n" in body:
            event("submitted", text=buffer + body.replace("\r", "").replace("\n", ""))
            buffer = ""
        else:
            buffer += body
        draw(state, buffer)
