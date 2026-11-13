"""May a wake be typed into this pane? Decided from a styled tmux capture (ad-519, Max #9594).

Positive match only: a pane is idle when its provider's known empty input prompt is on the
screen. A dialog, a working agent, typed text, an unknown provider and an unreadable screen all
hold, because none of them matches; a dialog is never recognised by a deny list. The labels below
only name the hold for a person. This narrows the race with a dialog opening; it cannot close it,
because a capture is not atomic with the CLI's own state.

Pure: no tmux, no I/O. A provider's screens are qualified per CLI version; a version that changes
them makes that pane hold, which is the safe way to find out.
"""
import re

HELD = "held-before-input"  # inject() result: nothing reached the pane, the wake stays queued

_ESCAPE = re.compile(r"\x1b\[([0-9;:]*)([A-Za-z])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.")
_LOOKS_LIKE_DIALOG = re.compile(r"Do you want|Would you like|Run this command|Allow \w+|Press enter to confirm"
                                r"|Esc to cancel|\btrust\b", re.IGNORECASE)
_PASTE_FORM = re.compile(r"\[Pasted", re.IGNORECASE)
_CODEX_PARTICLES = " ⠁⠂⠄⠈⠐⠠⡀⢀"  # wrapper_unix._without_codex_input_animation

PROFILES = {
    # glyph: the prompt character. ruled: the prompt sits between two horizontal rules.
    # footer: text that must sit below an EMPTY prompt. working: the provider's busy text.
    # working_above: lines above the prompt also searched, for a CLI that shows it there.
    # lit_glyph: the glyph is grey while busy. Claude's footer alone is not enough: a transient hint
    # ("paste again to expand") replaces "esc to interrupt" mid-turn (real-claude-1, a-answered).
    # Measured on Claude Code 2.1.318, Codex 0.166.0 and Antigravity 1.4.12; also Codex 0.169.0's status row
    # wrapping onto a second row in an 87-column pane (ad-44nu).
    "claude": {"glyph": "❯", "ruled": True, "footer": "", "working": re.compile("esc to interrupt"), "working_above": 0,
               "lit_glyph": True},
    # status: the one row an unruled composer may have between it and its footer. Group 1 must be painted: Codex
    # colours that field and a composer does not colour what is typed, so words alone never qualify a row.
    # A wrapped status row qualifies only as _status_rows() says.
    "codex": {"glyph": "›", "ruled": False, "footer": "? for shortcuts", "status": re.compile(r"·.*\b(Context \d+% left)"),
              "working": re.compile(r"\(.*esc to interrupt\)"), "working_above": 6, "lit_glyph": False},
    "agy": {"glyph": ">", "ruled": True, "footer": "? for shortcuts", "working": re.compile("esc to cancel"), "working_above": 0,
            "lit_glyph": False},
}


def gate_provider(command):
    """The qualified screen profile for an exact executable name, or '' (which always holds)."""
    if not isinstance(command, str):
        return ""
    executable = command.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    return executable if executable in PROFILES else ""


def _apply_sgr(params, style):
    codes = [int(p) if p.isdigit() else 0 for p in params.replace(":", ";").split(";")]
    i = 0
    while i < len(codes):
        code = codes[i]
        if code == 0:
            style.update(dim=False, grey=False, reverse=False, painted=False)
        elif code == 2:
            style["dim"] = True
        elif code == 22:
            style["dim"] = False
        elif code == 7:
            style["reverse"] = True
        elif code == 27:
            style["reverse"] = False
        elif code == 39:
            style.update(grey=False, painted=False)
        elif 30 <= code <= 37 or 90 <= code <= 97:
            style.update(grey=code == 90, painted=True)
        elif code in (38, 48):
            kind = codes[i + 1] if i + 1 < len(codes) else 0
            if kind == 5:
                if code == 38 and i + 2 < len(codes):
                    style.update(grey=240 <= codes[i + 2] <= 250, painted=True)  # Claude's "inactive" text is 246
                i += 2
            elif kind == 2:
                if code == 38 and i + 4 < len(codes):
                    style.update(grey=False, painted=True)
                i += 4
        i += 1


def _parse(styled):
    """[(plain text, per-character style, per-character explicit colour)] per line. tmux carries SGR state across line ends."""
    style = {"dim": False, "grey": False, "reverse": False, "painted": False}
    lines, text, marks, painted = [], [], [], []
    position = 0

    def add(chunk):
        for ch in chunk:
            if ch == "\n":
                lines.append(("".join(text), list(marks), list(painted)))
                text.clear()
                marks.clear()
                painted.clear()
            else:
                text.append(ch)
                marks.append("cursor" if style["reverse"] else "faint" if style["dim"] or style["grey"] else "")
                painted.append(style["painted"])

    for match in _ESCAPE.finditer(styled):
        add(styled[position:match.start()])
        position = match.end()
        if match.group(2) == "m":
            _apply_sgr(match.group(1) or "0", style)
    add(styled[position:])
    if text:
        lines.append(("".join(text), list(marks), list(painted)))
    return lines


def _is_rule(line):
    stripped = line[0].strip()
    return stripped.startswith("─") and stripped.count("─") >= 20


def _prompt_start(profile, line):
    """Offset just past the glyph when this line starts with it, else None."""
    plain = line[0]
    lead = len(plain) - len(plain.lstrip(_CODEX_PARTICLES))
    rest = plain[lead:]
    if rest.startswith(profile["glyph"]) and (len(rest) == 1 or rest[1] in _CODEX_PARTICLES or rest[1] == "\xa0"):
        return lead + 1
    return None


def _composer(profile, lines):
    """Index of the input line: the last glyph line, with a rule above where the provider has rules."""
    for index in range(len(lines) - 1, -1, -1):
        if _prompt_start(profile, lines[index]) is None:
            continue
        if not profile["ruled"]:
            if lines[index][0].startswith(" "):  # an indented glyph is a draft's continuation line, not the prompt
                continue
            return index
        if index > 0 and _is_rule(lines[index - 1]):
            return index
    return None


def _working(profile, lines, index):
    if profile["lit_glyph"] and lines[index][1][_prompt_start(profile, lines[index]) - 1] != "":
        return True
    start = max(0, index - profile["working_above"])
    return any(profile["working"].search(line[0]) for line in lines[start:index] + lines[index + 1:])


def _typed(profile, line):
    """Text in the prompt that is not placeholder styling. A lone cursor on a letter is typed text."""
    start = _prompt_start(profile, line)
    cells = [(ch, mark) for ch, mark in zip(line[0][start:], line[1][start:])
             if not ch.isspace() and ch not in _CODEX_PARTICLES]
    if any(mark == "" for _, mark in cells):
        return True
    return bool(cells) and not any(mark == "faint" for _, mark in cells)


def _status_row(profile, line):
    """The provider's status row: its words alone never qualify, the field must be in the provider's own colour."""
    match = profile["status"].search(line[0])
    return bool(match) and all(line[2][match.start(1):match.end(1)])


def _painted_throughout(line):
    """Every visible character in an explicit colour. A composer does not colour what is typed."""
    cells = [painted for ch, painted in zip(line[0], line[2]) if not ch.isspace() and ch not in _CODEX_PARTICLES]
    return bool(cells) and all(cells)


def _status_rows(profile, rows):
    """How many rows at the end of `rows` are the provider's status row, 0 when none.

    One row qualifies as before. Codex 0.169.0 wraps the row onto a second row in an 87-column pane (ad-44nu), and only
    that measured shape qualifies as two: an upper row that qualifies on its own and is painted throughout, then a row
    painted throughout (a composer does not colour what is typed). Any other shape holds.
    """
    if rows and _status_row(profile, rows[-1]):
        return 1
    if (len(rows) >= 2 and _status_row(profile, rows[-2]) and _painted_throughout(rows[-2])
            and _painted_throughout(rows[-1])):
        return 2
    return 0


def _draft_below(profile, below):
    """An unruled composer has no border under it, so a draft's later lines sit between it and the footer.

    Empty means blank rows, then at most one status row (which may wrap) directly above a footer that ends the screen.
    """
    rows = list(below)
    while rows and not rows[-1][0].strip():
        rows.pop()
    if not rows or profile["footer"] not in rows[-1][0]:
        return True
    rows.pop()
    del rows[len(rows) - _status_rows(profile, rows):]
    return any(row[0].strip(_CODEX_PARTICLES + "\t\xa0") for row in rows)


def _no_prompt_label(lines):
    bottom = "\n".join([line[0] for line in lines if line[0].strip()][-25:])
    return "dialog" if _LOOKS_LIKE_DIALOG.search(bottom) else "no-prompt"


def plain_text(styled):
    """The capture without its styling, for comparing two screens."""
    return "\n".join(line[0] for line in _parse(styled))


def classify(provider, styled):
    """('idle', '') or ('hold', reason)."""
    profile = PROFILES.get(provider)
    if profile is None:
        return "hold", "unknown-provider"
    if not isinstance(styled, str) or not styled.strip():
        return "hold", "unreadable"
    lines = _parse(styled)
    index = _composer(profile, lines)
    if index is None:
        return "hold", _no_prompt_label(lines)
    below = lines[index + 1:]
    if profile["ruled"] and not (below and _is_rule(below[0])):
        return "hold", _no_prompt_label(lines)
    if _working(profile, lines, index):
        return "hold", "working"
    if _typed(profile, lines[index]):
        # Codex draws its menu with the prompt glyph; the footer below tells the two apart, for the label.
        return "hold", ("dialog" if _LOOKS_LIKE_DIALOG.search("\n".join(line[0] for line in below)) else "typed")
    if profile["footer"] and not any(profile["footer"] in line[0] for line in below[:4]):
        return "hold", _no_prompt_label(lines)
    if not profile["ruled"] and _draft_below(profile, below):
        return "hold", "typed"
    return "idle", ""


def enter_hold_reason(provider, styled, text):
    """After the paste, before Enter: '' only when the input prompt visibly holds the pasted text."""
    profile = PROFILES.get(provider)
    if profile is None:
        return "unknown-provider"
    if not isinstance(styled, str) or not styled.strip():
        return "unreadable"
    lines = _parse(styled)
    index = _composer(profile, lines)
    if index is None:
        return _no_prompt_label(lines)
    if _working(profile, lines, index) or any("Press enter to confirm" in line[0] for line in lines[index + 1:]):
        return "working-or-dialog"
    shown = " ".join(lines[index][0][_prompt_start(profile, lines[index]):].split())
    wanted = " ".join(text.split())[:12]
    if wanted and (shown.startswith(wanted) or _PASTE_FORM.search(shown)):
        return ""
    return "paste-not-in-prompt"
