"""ad-519 (Max #9594): a wake reaches a pane only at its provider's known empty prompt.

No live server, queue or pane. tmux is a recording stand-in, so "nothing typed" is asserted on the argv it received.
"""
import itertools
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wake_gate
import wrapper
import wrapper_unix
from agents import AgentTrigger

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "wake_gate"
INPUT_VERBS = ("load-buffer", "paste-buffer", "send-keys", "copy-mode")
WAKE = "use mcp to read #general - you're mentioned, take appropriate action and respond"


def screen(name):
    return (FIXTURES / f"{name}.ansi").read_text("utf-8")


def claude_prompt(markup):
    """The idle Claude screen with `markup` after the prompt glyph."""
    return re.sub("❯[ \xa0]*\n", lambda _: "❯ " + markup + "\n", screen("claude-idle-start"), count=1)


def agy_prompt(markup):
    return re.sub(r">(\x1b\[39m)?[ \xa0]*\n", lambda _: "> " + markup + "\n", screen("agy-idle-after-turn"), count=1)


CODEX_PLACEHOLDER = "\x1b[2mAsk Codex to do anything\x1b[0m"


def codex_prompt(markup):
    return screen("codex-idle-start").replace(CODEX_PLACEHOLDER, markup)


def codex_after_turn(markup):
    """The later idle screen, which has a status row between the prompt and its footer."""
    return screen("codex-idle-after-turn").replace(CODEX_PLACEHOLDER, markup)


UNKNOWN_DIALOG = "Some new question from a future version\n> 1. Sure\n  2. Nope\n"
HOLDS = [
    ("claude", "claude-dialog", "dialog"),
    ("claude", "claude-trust", "dialog"),
    ("claude", "claude-working", "working"),
    ("claude", "claude-working-paste-hint", "working"),
    ("claude", "claude-typed", "typed"),
    ("codex", "codex-dialog", "dialog"),
    ("codex", "codex-working", "working"),
    # Real drafts in a real composer, Codex 0.166.0, 130 columns. Codex removes its footer as soon as there is a draft.
    ("codex", "codex-draft-two-lines", "typed"),
    ("codex", "codex-draft-empty-first-line", "no-prompt"),
    ("codex", "codex-draft-imitation", "no-prompt"),
    ("codex", "codex-draft-pasted", "typed"),
    ("codex", "codex-draft-command-mention", "typed"),
    # Real capture, Codex 0.169.0, 87 columns, mid-turn: its status row wraps onto a second row (ad-44nu).
    ("codex", "codex-0169-working-wrapped", "working"),
    ("agy", "agy-dialog", "dialog"),
    ("agy", "agy-trust", "dialog"),
    ("agy", "agy-working", "working"),
    ("agy", "agy-typed", "typed"),
]
IDLES = [
    ("claude", "claude-idle-start"),
    ("claude", "claude-idle-after-turn"),
    ("codex", "codex-idle-start"),
    ("codex", "codex-idle-after-turn"),
    ("codex", "codex-idle-after-clear"),
    # Real capture, Codex 0.169.0, 87 columns, after a turn: the status row wraps onto a second row (ad-44nu).
    ("codex", "codex-0169-idle-wrapped"),
    ("agy", "agy-idle-start-banner"),
    ("agy", "agy-idle-after-turn"),
]


class ClassifyTests(unittest.TestCase):
    def test_real_idle_screens_are_idle(self):
        for provider, name in IDLES:
            with self.subTest(name=name):
                self.assertEqual(wake_gate.classify(provider, screen(name)), ("idle", ""))

    def test_real_screens_that_must_hold(self):
        for provider, name, reason in HOLDS:
            with self.subTest(name=name):
                self.assertEqual(wake_gate.classify(provider, screen(name)), ("hold", reason))

    def test_an_unknown_dialog_holds_on_every_provider(self):
        """No deny list: a screen nobody has seen before matches no prompt."""
        for provider in wake_gate.PROFILES:
            with self.subTest(provider=provider):
                self.assertEqual(wake_gate.classify(provider, UNKNOWN_DIALOG)[0], "hold")

    def test_the_prompt_needs_its_rules_and_its_footer(self):
        rule = "─" * 40
        self.assertEqual(wake_gate.classify("claude", f"{rule}\n❯ \n{rule}\n"), ("idle", ""))
        self.assertEqual(wake_gate.classify("claude", f"some text\n❯ \n{rule}\n")[0], "hold")
        self.assertEqual(wake_gate.classify("claude", f"{rule}\n❯ \nsome text\n")[0], "hold")
        self.assertEqual(wake_gate.classify("agy", f"{rule}\n> \n{rule}\n? for shortcuts\n"), ("idle", ""))
        self.assertEqual(wake_gate.classify("agy", f"{rule}\n> \n{rule}\n\n")[0], "hold")
        self.assertEqual(wake_gate.classify("codex", "› \n\n  ? for shortcuts\n"), ("idle", ""))
        self.assertEqual(wake_gate.classify("codex", "› \n\n\n\n\n\n")[0], "hold")

    def test_unknown_provider_and_unreadable_screens_hold(self):
        for provider in ("", "gemini", "kimi", None):
            with self.subTest(provider=provider):
                self.assertEqual(wake_gate.classify(provider, screen("claude-idle-start")), ("hold", "unknown-provider"))
        for text in ("", "   \n  ", None):
            with self.subTest(text=text):
                self.assertEqual(wake_gate.classify("claude", text), ("hold", "unreadable"))

    def test_placeholder_styling_counts_as_empty(self):
        for markup in ("\x1b[2mfix the lint errors\x1b[22m", "\x1b[38;5;246mfix the lint errors\x1b[39m",
                       "\x1b[7mf\x1b[27m\x1b[2mix the lint errors\x1b[22m"):
            with self.subTest(markup=markup):
                self.assertEqual(wake_gate.classify("claude", claude_prompt(markup)), ("idle", ""))

    def test_undimmed_text_is_typed_text(self):
        for markup in ("hello", "\x1b[1mhello\x1b[0m", "\x1b[38;5;231mhello\x1b[39m", "\x1b[7mh\x1b[27m"):
            with self.subTest(markup=markup):
                self.assertEqual(wake_gate.classify("claude", claude_prompt(markup)), ("hold", "typed"))
        self.assertEqual(wake_gate.classify("codex", codex_prompt("a draft")), ("hold", "typed"))
        self.assertEqual(wake_gate.classify("agy", agy_prompt("a draft")), ("hold", "typed"))

    def test_dim_state_carries_across_line_ends(self):
        """tmux does not repeat SGR on each line: a dim started earlier still applies."""
        carried = "\x1b[2mearlier\n› ghost\n\n  ? for shortcuts\n"
        self.assertEqual(wake_gate.classify("codex", carried), ("idle", ""))
        ended = "\x1b[2mearlier\x1b[22m\n› ghost\n\n  ? for shortcuts\n"
        self.assertEqual(wake_gate.classify("codex", ended), ("hold", "typed"))

    def test_codex_input_particles_are_not_typed_text(self):
        """The forms in test_wrapper_activity: a particle after the glyph, and after the placeholder."""
        for form in ("\x1b[1m›\x1b[0m⠁" + CODEX_PLACEHOLDER + "    ⠄", "\x1b[1m›\x1b[0m " + CODEX_PLACEHOLDER + " ⢀"):
            text = screen("codex-idle-start").replace("\x1b[1m›\x1b[0m " + CODEX_PLACEHOLDER, form)
            self.assertIn(form, text)
            self.assertEqual(wake_gate.classify("codex", text), ("idle", ""))

    def test_a_past_mention_of_the_busy_text_above_the_prompt_does_not_hold_for_ever(self):
        idle = screen("claude-idle-after-turn")
        self.assertEqual(wake_gate.classify("claude", "the words esc to interrupt in a reply\n" + idle), ("idle", ""))
        codex = screen("codex-idle-after-turn")
        said = codex.replace("\x1b[1m›\x1b[0m " + CODEX_PLACEHOLDER,
                             "the words esc to interrupt in a reply\n\x1b[1m›\x1b[0m " + CODEX_PLACEHOLDER)
        self.assertNotEqual(said, codex)
        self.assertEqual(wake_gate.classify("codex", said), ("idle", ""))

    def test_a_draft_below_the_codex_prompt_line_holds(self):
        """Codex has no rule under its composer: the whole area down to the footer must be empty (review F1)."""
        for markup in ("\n  DRAFT ON THE SECOND LINE", "\n\n\n  DRAFT AFTER BLANK LINES", "\n  › looks like the glyph",
                       "REAL DRAFT\n  ›", "\n  ? for shortcuts\n", "\n  x · Context 5% left\n"):
            for make in (codex_prompt, codex_after_turn):
                with self.subTest(markup=markup, make=make.__name__):
                    self.assertEqual(wake_gate.classify("codex", make(markup))[0], "hold")

    def test_a_draft_directly_above_the_codex_footer_is_not_taken_for_the_status_row(self):
        idle = screen("codex-idle-start").replace(CODEX_PLACEHOLDER, "")
        footer = "\n\n\n  \x1b[1m?"
        self.assertIn(footer, idle)
        self.assertEqual(wake_gate.classify("codex", idle), ("idle", ""))
        draft = idle.replace(footer, "\n\n  DRAFT ABOVE THE FOOTER\n  \x1b[1m?")
        self.assertEqual(wake_gate.classify("codex", draft), ("hold", "typed"))

    def test_the_status_row_is_known_by_codex_s_colour_not_by_its_words(self):
        """Recheck F1: typed text carries no colour, whatever it says. Real screens: codex-draft-*."""
        idle = screen("codex-idle-start").replace(CODEX_PLACEHOLDER, "")
        footer = "\n\n\n  \x1b[1m?"
        self.assertIn(footer, idle)
        above_footer = lambda row: idle.replace(footer, "\n\n  " + row + "\n  \x1b[1m?")
        for row in ("PLEASE KEEP THIS DRAFT · Context 5% left",
                    "GPT-6.1-Sol low · /private/tmp/x/work · Context 98% left · Context 2% used",
                    "x · \x1b[38;2;242;181;144mContext\x1b[39m 98% left",
                    "x · Context \x1b[38;2;242;181;144m98% left\x1b[39m",
                    "x · \x1b[1mContext 98% left\x1b[0m"):
            with self.subTest(row=row):
                self.assertEqual(wake_gate.classify("codex", above_footer(row)), ("hold", "typed"))
        for paint in ("\x1b[38;2;242;181;144m", "\x1b[38;5;180m", "\x1b[33m"):
            with self.subTest(paint=paint):
                row = "x · " + paint + "Context 98% left\x1b[39m"
                self.assertEqual(wake_gate.classify("codex", above_footer(row)), ("idle", ""))

    def test_the_codex_status_colour_does_not_carry_past_its_end(self):
        idle = screen("codex-idle-start").replace(CODEX_PLACEHOLDER, "")
        footer = "\n\n\n  \x1b[1m?"
        for close in ("\x1b[39m", "\x1b[0m"):
            with self.subTest(close=close):
                row = "\x1b[38;2;1;2;3mpainted" + close + " · Context 98% left"
                self.assertEqual(wake_gate.classify("codex", idle.replace(footer, "\n\n  " + row + "\n  \x1b[1m?")), ("hold", "typed"))

    def test_the_codex_footer_must_end_the_screen(self):
        for name in ("codex-idle-start", "codex-idle-after-turn"):
            with self.subTest(name=name):
                self.assertEqual(wake_gate.classify("codex", screen(name) + "  TEXT BELOW THE FOOTER\n"), ("hold", "typed"))

    def test_a_wrapped_codex_status_row_is_one_status_row(self):
        """ad-44nu: Codex 0.169.0 wraps its status row onto a second row in an 87-column pane. Only that measured shape
        qualifies: an upper row that is a status row on its own, painted throughout, then a row painted throughout (a
        composer does not colour what is typed). Any other shape holds."""
        idle = screen("codex-0169-idle-wrapped")
        first, second = [row for row in idle.split("\n") if "% left" in row]
        self.assertIn("\n\n" + first + "\n" + second + "\n", idle)  # Codex pads one blank row above it
        self.assertIn("Context", first)
        self.assertNotIn("Context", second)
        status = lambda *rows: idle.replace(first + "\n" + second, "\n".join(rows))
        unpadded = lambda *rows: idle.replace("\n\n" + first + "\n" + second, "\n" + "\n".join(rows))
        paint, grey, cyan, end = "\x1b[38;2;242;181;144m", "\x1b[38;2;132;132;133m", "\x1b[36m", "\x1b[39m"
        pasted = "  " + cyan + "[Pasted Content 2048 chars]" + end
        # tmux carries colour across line ends, so an uncoloured row after a coloured one opens with `end`, as in `second`.
        for name, text in (("the real two rows", idle), ("one row, as before the wrap", status(first))):
            with self.subTest(name=name):
                self.assertEqual(wake_gate.classify("codex", text), ("idle", ""))
        for name, text in (
                ("a draft above the wrapped row", status("  DRAFT ABOVE THE STATUS", first, second)),
                ("a draft between its rows", status(first, end + "  DRAFT BETWEEN", second)),
                ("an unpainted second row", status(first, end + "  80% left · weekly 81% left")),
                ("a second row partly unpainted", status(first, "  " + paint + "80% left" + end + " · weekly 81% left")),
                ("typed words imitating both rows", status("  GPT-6.1-Sol xhigh · Context 89% left · 5h", "  80% left")),
                ("an upper row partly unpainted",
                 status("  KEEP THIS · " + paint + "Context 89% left" + end, "  " + paint + "80% left" + end)),
                ("two painted rows without the status field",
                 status("  " + paint + "GPT-6.1-Sol xhigh" + grey + " · " + paint + "5h", "  " + paint + "80% left" + end)),
                ("the field split across the rows, not measured",
                 status("  " + paint + "GPT-6.1-Sol xhigh" + grey + " · " + paint + "Context",
                        "  " + paint + "89% left" + grey + " · " + paint + "weekly 81% left" + end)),
                ("the field on the second row, not measured",
                 status("  " + paint + "GPT-6.1-Sol xhigh",
                        "  " + paint + "~/Projects/x" + grey + " · " + paint + "Context 89% left" + end)),
                # Independent review of ad-44nu: a painted row next to the status row is not part of it.
                ("a painted placeholder directly above a one-row status", unpadded(pasted, first)),
                ("a painted placeholder directly above the wrapped rows", unpadded(pasted, first, second)),
                ("a painted row between the wrapped rows and the footer", unpadded(first, second, pasted)),
                ("three painted rows with no blank row above", unpadded(first, second, "  " + paint + "475K window" + end))):
            with self.subTest(name=name):
                self.assertEqual(wake_gate.classify("codex", text)[0], "hold")

    def test_a_wake_pasted_at_the_wrapped_codex_prompt_may_be_entered(self):
        idle = screen("codex-0169-idle-wrapped")
        self.assertIn(CODEX_PLACEHOLDER, idle)
        self.assertEqual(wake_gate.enter_hold_reason("codex", idle.replace(CODEX_PLACEHOLDER, WAKE), WAKE), "")

    def test_a_draft_below_the_prompt_line_holds_on_the_ruled_providers(self):
        self.assertEqual(wake_gate.classify("claude", claude_prompt("\n  DRAFT"))[0], "hold")
        self.assertEqual(wake_gate.classify("agy", agy_prompt("\n  DRAFT"))[0], "hold")

    def test_gate_provider_takes_exact_executable_names(self):
        for command, expected in (("claude", "claude"), ("/opt/homebrew/bin/codex", "codex"), (" agy ", "agy"),
                                  ("gemini", ""), ("claude2", ""), ("", ""), (None, "")):
            with self.subTest(command=command):
                self.assertEqual(wake_gate.gate_provider(command), expected)


class EnterCheckTests(unittest.TestCase):
    def test_the_pasted_text_in_the_prompt_allows_enter(self):
        self.assertEqual(wake_gate.enter_hold_reason("claude", claude_prompt(WAKE), WAKE), "")
        self.assertEqual(wake_gate.enter_hold_reason("codex", codex_prompt(WAKE), WAKE), "")
        self.assertEqual(wake_gate.enter_hold_reason("agy", agy_prompt(WAKE), WAKE), "")

    def test_a_wrapped_line_of_the_paste_that_starts_with_the_glyph_is_not_the_prompt(self):
        rule = "─" * 40
        text = f"{rule}\n> {WAKE}\n> a wrapped line that starts with the glyph\n{rule}\n"
        self.assertEqual(wake_gate.enter_hold_reason("agy", text, WAKE), "")

    def test_the_collapsed_paste_form_allows_enter(self):
        self.assertEqual(wake_gate.enter_hold_reason("claude", screen("claude-pasted-long"), "x" * 2000), "")

    def test_a_dialog_or_a_busy_pane_or_a_lost_paste_blocks_enter(self):
        for provider, name in (("claude", "claude-dialog"), ("codex", "codex-dialog"), ("agy", "agy-dialog"),
                               ("claude", "claude-working"), ("codex", "codex-working"), ("agy", "agy-working"),
                               ("claude", "claude-idle-start"), ("agy", "agy-idle-after-turn")):
            with self.subTest(name=name):
                self.assertNotEqual(wake_gate.enter_hold_reason(provider, screen(name), WAKE), "")
        self.assertEqual(wake_gate.enter_hold_reason("claude", claude_prompt("someone else's draft"), WAKE),
                         "paste-not-in-prompt")
        self.assertEqual(wake_gate.enter_hold_reason("nobody", claude_prompt(WAKE), WAKE), "unknown-provider")
        self.assertEqual(wake_gate.enter_hold_reason("claude", "", WAKE), "unreadable")

    def test_a_previous_identical_wake_in_the_history_is_not_taken_for_the_prompt(self):
        """Antigravity's history shows the last prompt under a rule; its dialog must still block Enter."""
        text = screen("agy-dialog")
        self.assertNotEqual(wake_gate.enter_hold_reason("agy", text, "Run this exact shell command"), "")


class FakeTmux:
    """A tmux on PATH that records argv and serves screens from files, like the real command line."""

    def __init__(self, root):
        self.root = Path(root)
        self.calls = self.root / "calls"
        self.calls.write_text("")
        exe = self.root / "tmux"
        exe.write_text("#!" + sys.executable + "\n" + r'''
import os, sys
from pathlib import Path
root = Path(os.environ["TEST_TMUX_ROOT"])
a = sys.argv[1:]
with (root / "calls").open("a") as f: f.write(" ".join(a) + "\n")
if a[0] == "display-message":
    print("0" if "#{pane_in_mode}" in a else "%7")
elif a[0] == "paste-buffer":
    (root / "pasted").write_text("1")
elif a[0] == "capture-pane":
    if (root / "capture-fails").exists(): sys.exit(1)
    name = "screen-after-paste" if (root / "pasted").exists() else "screen"
    sys.stdout.write((root / name).read_text("utf-8"))
''')
        exe.chmod(0o755)

    def set_screens(self, before, after=None):
        (self.root / "screen").write_text(before, "utf-8")
        (self.root / "screen-after-paste").write_text(before if after is None else after, "utf-8")
        (self.root / "pasted").unlink(missing_ok=True)
        (self.root / "capture-fails").unlink(missing_ok=True)
        self.calls.write_text("")

    def verbs(self):
        return [line.split()[0] for line in self.calls.read_text().splitlines()]

    def input_verbs(self):
        return [verb for verb in self.verbs() if verb in INPUT_VERBS]

    def env(self):
        return {"PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""), "TEST_TMUX_ROOT": str(self.root)}


class InjectGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fake = FakeTmux(tmp.name)

    def inject(self, provider, text=WAKE):
        with mock.patch.dict(os.environ, self.fake.env()), mock.patch("sys.stdout"):
            return wrapper_unix.inject(text, tmux_session="s", delay=0, provider=provider)

    def test_every_hold_state_gets_zero_input(self):
        for provider, name, _ in HOLDS:
            with self.subTest(name=name):
                self.fake.set_screens(screen(name))
                self.assertIs(self.inject(provider), wake_gate.HELD)
                self.assertEqual(self.fake.input_verbs(), [])

    def test_unknown_dialog_empty_screen_and_unknown_provider_get_zero_input(self):
        for provider, text in (("claude", UNKNOWN_DIALOG), ("codex", UNKNOWN_DIALOG), ("agy", UNKNOWN_DIALOG),
                               ("claude", ""), ("", screen("claude-idle-start")), ("gemini", screen("claude-idle-start"))):
            with self.subTest(provider=provider, text=text[:20]):
                self.fake.set_screens(text)
                self.assertIs(self.inject(provider), wake_gate.HELD)
                self.assertEqual(self.fake.input_verbs(), [])

    def test_a_capture_that_fails_gets_zero_input(self):
        self.fake.set_screens(screen("claude-idle-start"))
        (self.fake.root / "capture-fails").write_text("1")
        self.assertIs(self.inject("claude"), wake_gate.HELD)
        self.assertEqual(self.fake.input_verbs(), [])

    def test_the_default_provider_holds(self):
        """A caller that names no provider is not qualified; it does not get the old behaviour."""
        self.fake.set_screens(screen("claude-idle-start"))
        with mock.patch.dict(os.environ, self.fake.env()), mock.patch("sys.stdout"):
            self.assertIs(wrapper_unix.inject(WAKE, tmux_session="s", delay=0), wake_gate.HELD)
        self.assertEqual(self.fake.input_verbs(), [])

    def test_a_copy_mode_is_left_alone_while_holding(self):
        self.fake.set_screens(screen("claude-dialog"))
        self.inject("claude")
        self.assertNotIn("copy-mode", self.fake.verbs())

    def test_an_idle_prompt_and_a_placeholder_deliver(self):
        for provider, before, after in (
                ("claude", screen("claude-idle-start"), claude_prompt(WAKE)),
                ("claude", claude_prompt("\x1b[2mghost text\x1b[22m"), claude_prompt(WAKE)),
                ("codex", screen("codex-idle-start"), codex_prompt(WAKE)),
                ("codex", screen("codex-0169-idle-wrapped"), screen("codex-0169-idle-wrapped").replace(CODEX_PLACEHOLDER, WAKE)),
                ("agy", screen("agy-idle-start-banner"), agy_prompt(WAKE))):
            with self.subTest(provider=provider, before=before[-40:]):
                self.fake.set_screens(before, after)
                self.assertIs(self.inject(provider), True)
                self.assertEqual(self.fake.verbs().count("paste-buffer"), 1)
                self.assertEqual(self.fake.verbs().count("send-keys"), 1)

    def test_a_dialog_opening_during_the_paste_stops_enter_and_is_uncertain(self):
        for provider, before, name in (("claude", screen("claude-idle-start"), "claude-dialog"),
                                       ("codex", screen("codex-idle-start"), "codex-dialog"),
                                       ("agy", screen("agy-idle-after-turn"), "agy-dialog")):
            with self.subTest(provider=provider):
                self.fake.set_screens(before, screen(name))
                result = self.inject(provider)
                self.assertIsInstance(result, wrapper_unix.Uncertain, "a stopped Enter carries its reason (ad-ruq)")
                self.assertIn(result.reason, ("dialog", "working-or-dialog"))
                self.assertEqual(self.fake.verbs().count("paste-buffer"), 1)
                self.assertNotIn("send-keys", self.fake.verbs())
                self.assertEqual(self.fake.verbs().count("capture-pane"), 3, "a dialog is never re-looked at")

    def test_a_screen_that_turns_after_the_first_look_gets_zero_input(self):
        """Two looks: the second finds the dialog, so nothing is typed."""
        self.fake.set_screens(screen("claude-idle-start"))
        looks = iter([(screen("claude-idle-start")), screen("claude-dialog")])
        with mock.patch.object(wrapper_unix, "_capture_styled", side_effect=lambda target: next(looks)), \
             mock.patch.dict(os.environ, self.fake.env()), mock.patch("sys.stdout"):
            self.assertIs(wrapper_unix.inject(WAKE, tmux_session="s", delay=0, provider="claude"), wake_gate.HELD)
        self.assertEqual(self.fake.input_verbs(), [])

    def test_a_multiline_codex_draft_gets_zero_input(self):
        for make in (codex_prompt, codex_after_turn):
            with self.subTest(make=make.__name__):
                self.fake.set_screens(make("\n  USER DRAFT"), make(WAKE + "\n  USER DRAFT"))
                self.assertIs(self.inject("codex"), wake_gate.HELD)
                self.assertEqual(self.fake.input_verbs(), [])

    def test_a_moving_stream_gets_zero_input_on_the_direct_path(self):
        """Both looks read idle; the text moving between them gives the turn away (review F4)."""
        self.fake.set_screens(screen("codex-streaming"))
        first, second = ("x" * n + "\n" + screen("codex-streaming") for n in (1, 2))
        self.assertEqual(wake_gate.classify("codex", first), ("idle", ""))
        self.assertEqual(wake_gate.classify("codex", second), ("idle", ""))
        looks = iter([first, second, codex_prompt(WAKE)])
        with mock.patch.object(wrapper_unix, "_capture_styled", side_effect=lambda target: next(looks)), \
             mock.patch.dict(os.environ, self.fake.env()), mock.patch("sys.stdout"):
            self.assertIs(wrapper_unix.inject(WAKE, tmux_session="s", delay=0, provider="codex"), wake_gate.HELD)
        self.assertEqual(self.fake.input_verbs(), [])

    def test_codex_prompt_particles_do_not_hold_the_direct_path(self):
        idle = screen("codex-idle-start")
        sparkled = idle.replace("\x1b[1m›\x1b[0m " + CODEX_PLACEHOLDER, "\x1b[1m›\x1b[0m⠁" + CODEX_PLACEHOLDER + "    ⠄")
        self.assertNotEqual(idle, sparkled)
        self.fake.set_screens(idle)
        looks = iter([idle, sparkled, codex_prompt(WAKE)])
        with mock.patch.object(wrapper_unix, "_capture_styled", side_effect=lambda target: next(looks)), \
             mock.patch.dict(os.environ, self.fake.env()), mock.patch("sys.stdout"):
            self.assertIs(wrapper_unix.inject(WAKE, tmux_session="s", delay=0, provider="codex"), True)


class FakeClock:
    """wrapper_unix's view of `time`: sleep advances it, monotonic reads it. Nothing else in the process is touched."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class PostPasteRecheckTests(unittest.TestCase):
    """ad-ruq (Max #9848): after the paste, a bounded re-look while the text is not drawn yet; no Enter otherwise.

    Measured on a real Claude Code 2.1.319 (2026-11-09-ad-ruq-recheck/evidence): the paste is drawn 7 to 298 ms after
    `paste-buffer` returns, and the only interim state is `paste-not-in-prompt`.
    """

    IDLE = {"claude": "claude-idle-start", "codex": "codex-idle-start", "agy": "agy-idle-after-turn"}
    DRAWN = {"claude": lambda: claude_prompt(WAKE), "codex": lambda: codex_prompt(WAKE), "agy": lambda: agy_prompt(WAKE)}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fake = FakeTmux(tmp.name)
        self.fake.set_screens(screen("claude-idle-start"))
        self.clock = FakeClock()

    def inject(self, looks, provider="claude", leave_mode=None, text=WAKE):
        """looks: the screens successive captures return (the last repeats); None = the capture fails.

        A fresh fake clock per call (subtests share the fixture). `self.since_paste` is the simulated time from the
        paste to the return, the budget's own reference: the first post-paste look comes after the unchanged
        first-look sleep (0.3 s for an 80-character wake)."""
        looks = list(looks)
        captures = []
        self.clock = FakeClock() if not getattr(self, "keep_clock", False) else self.clock
        pasted_at = []

        first_wait = max(0.3, len(text) * 0.001)

        def capture(target):
            if (self.fake.root / "pasted").exists() and not pasted_at:
                # the first post-paste capture comes after the first-look wait (plus any simulated slow step)
                pasted_at.append(self.clock.now - first_wait - getattr(self.clock, "first_overshoot", 0.0))
            captures.append(target)
            return looks.pop(0) if len(looks) > 1 else looks[0]
        patches = [mock.patch.object(wrapper_unix, "_capture_styled", side_effect=capture),
                   mock.patch.object(wrapper_unix, "time", self.clock),
                   mock.patch.dict(os.environ, self.fake.env()), mock.patch("sys.stdout")]
        if leave_mode is not None:
            patches.append(mock.patch.object(wrapper_unix, "_leave_mode", side_effect=leave_mode))
        with patches[0], patches[1], patches[2], patches[3]:
            if leave_mode is None:
                result = wrapper_unix.inject(text, tmux_session="s", delay=0.3, provider=provider)
            else:
                with patches[4]:
                    result = wrapper_unix.inject(text, tmux_session="s", delay=0.3, provider=provider)
        self.since_paste = self.clock.now - pasted_at[0] if pasted_at else None
        return result, len(captures) - 2  # the two pre-paste looks are not post-paste looks

    def pre(self, provider="claude"):
        return [screen(self.IDLE[provider]), screen(self.IDLE[provider])]

    def assert_one_enter(self):
        self.assertEqual(self.fake.verbs().count("paste-buffer"), 1)
        self.assertEqual(self.fake.verbs().count("send-keys"), 1)

    def assert_no_enter(self):
        self.assertEqual(self.fake.verbs().count("paste-buffer"), 1, "the paste is never repeated")
        self.assertNotIn("send-keys", self.fake.verbs())

    def test_the_constants_are_the_measured_bound(self):
        self.assertEqual(wrapper_unix.POST_PASTE_DEADLINE_S, 2.0)
        self.assertEqual(wrapper_unix.POST_PASTE_RELOOK_S, 0.1)

    def test_text_drawn_at_the_first_look_gives_one_enter_as_before(self):
        for provider in ("claude", "codex", "agy"):
            with self.subTest(provider=provider):
                self.fake.set_screens(screen(self.IDLE[provider]))
                result, looks = self.inject(self.pre(provider) + [self.DRAWN[provider]()], provider)
                self.assertIs(result, True)
                self.assertEqual(looks, 1)
                self.assert_one_enter()

    def test_text_drawn_late_gives_exactly_one_enter(self):
        for provider in ("claude", "codex", "agy"):
            for late_looks in (1, 16):  # the second look (~0.4 s) and the last one before the deadline
                with self.subTest(provider=provider, late_looks=late_looks):
                    self.fake.set_screens(screen(self.IDLE[provider]))
                    idle = screen(self.IDLE[provider])
                    result, looks = self.inject(self.pre(provider) + [idle] * late_looks + [self.DRAWN[provider]()], provider)
                    self.assertIs(result, True)
                    self.assertEqual(looks, late_looks + 1)
                    self.assert_one_enter()
                    self.assertLess(self.since_paste, wrapper_unix.POST_PASTE_DEADLINE_S)

    def test_text_never_drawn_gives_no_enter_within_the_bound_and_the_reason(self):
        result, looks = self.inject(self.pre() + [screen("claude-idle-start")])
        self.assertIsInstance(result, wrapper_unix.Uncertain)
        self.assertEqual(result.reason, "paste-not-in-prompt")
        self.assert_no_enter()
        bound = int(wrapper_unix.POST_PASTE_DEADLINE_S / wrapper_unix.POST_PASTE_RELOOK_S) + 2
        self.assertLessEqual(looks, bound)
        self.assertGreater(looks, 3, "a re-look happened")
        self.assertAlmostEqual(self.since_paste, wrapper_unix.POST_PASTE_DEADLINE_S, places=6,
                               msg="the wait ends exactly at the deadline, and Enter is never pressed at it")

    def test_the_original_approval_dialog_is_one_look_and_no_enter(self):
        result, looks = self.inject(self.pre() + [screen("claude-dialog")])
        self.assertIsInstance(result, wrapper_unix.Uncertain)
        self.assertIn(result.reason, ("dialog", "working-or-dialog"))
        self.assertEqual(looks, 1)
        self.assert_no_enter()

    def test_a_dialog_a_turn_or_an_unreadable_screen_during_the_window_stops_at_that_look(self):
        for name, reasons in (("claude-dialog", ("dialog", "working-or-dialog")),
                              ("claude-working", ("working-or-dialog",)),
                              (None, ("unreadable",))):
            with self.subTest(interrupt=name):
                self.fake.set_screens(screen("claude-idle-start"))
                idle = screen("claude-idle-start")
                interrupt = None if name is None else screen(name)
                result, looks = self.inject(self.pre() + [idle, idle, interrupt, claude_prompt(WAKE)])
                self.assertIsInstance(result, wrapper_unix.Uncertain)
                self.assertIn(result.reason, reasons)
                self.assertEqual(looks, 3, "stop at the look that saw it; the later drawn screen is never reached")
                self.assert_no_enter()

    def test_a_tmux_mode_the_helper_cannot_leave_during_the_window_stops_without_enter(self):
        idle = screen("claude-idle-start")
        result, looks = self.inject(self.pre() + [idle, idle, claude_prompt(WAKE)], leave_mode=[True, True, False])
        self.assertIsInstance(result, wrapper_unix.Uncertain)
        self.assertEqual(result.reason, "tmux-mode")
        self.assert_no_enter()

    def test_a_tmux_mode_the_helper_leaves_is_no_stop(self):
        """_leave_mode clears a mode and returns True (G2 F2 note): the look then proceeds as today."""
        idle = screen("claude-idle-start")
        result, looks = self.inject(self.pre() + [idle, idle, claude_prompt(WAKE)], leave_mode=[True, True, True, True])
        self.assertIs(result, True)
        self.assert_one_enter()

    def test_an_unexpected_draft_is_never_waited_on_and_never_entered(self):
        """G2 F1: `paste-not-in-prompt` also names a composer holding someone's draft (the claude-typed fixture reads
        hold/typed and paste-not-in-prompt at once). No wait on it, and a later frame that would pass the Enter
        predicate (wake prefix plus the draft) is never reached."""
        idle = screen("claude-idle-start")
        passes_later = claude_prompt(WAKE + " DRAFT")
        self.assertEqual(wake_gate.enter_hold_reason("claude", passes_later, WAKE), "", "the later frame would pass")
        for name, draft, at in (("first look, real typed fixture", screen("claude-typed"), 1),
                                ("first look, synthetic draft", claude_prompt("DRAFT"), 1),
                                ("later look, synthetic draft", claude_prompt("DRAFT"), 3)):
            with self.subTest(case=name):
                self.fake.set_screens(idle)
                result, looks = self.inject(self.pre() + [idle] * (at - 1) + [draft, passes_later])
                self.assertIsInstance(result, wrapper_unix.Uncertain)
                self.assertEqual(result.reason, "paste-not-in-prompt+typed")
                self.assertEqual(looks, at, "no look after the unexpected draft was seen")
                self.assert_no_enter()

    def test_a_capture_completing_after_the_deadline_is_never_accepted(self):
        """G2 F2: the sleep oversleeps (a slow capture or scheduler), so the last capture completes past the
        deadline although it was started inside it; it shows the text and still gets no Enter."""
        idle = screen("claude-idle-start")
        self.clock = FakeClock()
        self.keep_clock = True
        real_sleep = self.clock.sleep

        def oversleep(seconds):
            real_sleep(seconds)
            if self.clock.now - 1000.0 > 2.1:  # 0.15 pre-paste + 1.95 after it: from here each step runs 0.2 s long
                self.clock.now += 0.2
        self.clock.sleep = oversleep
        # 17 idle frames fill the looks up to 1.9 s; the 18th capture, started inside the budget, completes at
        # about 2.2 s because of the oversleep and shows the text: it must not be accepted
        result, looks = self.inject(self.pre() + [idle] * 17 + [claude_prompt(WAKE)])
        self.assertIsInstance(result, wrapper_unix.Uncertain)
        self.assertEqual(result.reason, "recheck-deadline")
        self.assertEqual(looks, 18)
        self.assert_no_enter()
        self.assertLessEqual(self.since_paste, wrapper_unix.POST_PASTE_DEADLINE_S + 0.3)

    def test_a_slow_first_look_completing_after_the_deadline_is_never_accepted(self):
        """G3 F2: the FIRST look's own blocking steps can be slow (a 1.9 s capture at the ordinary 0.3 s wait). A
        first capture that completes after the budget and shows the text gets no Enter either."""
        self.clock = FakeClock()
        self.keep_clock = True
        real_sleep = self.clock.sleep

        def slow_first_look(seconds):
            real_sleep(seconds)
            if seconds == 0.3:  # the first-look wait: the capture that follows takes 1.9 s
                self.clock.now += 1.9
                self.clock.first_overshoot = 1.9
        self.clock.sleep = slow_first_look
        result, looks = self.inject(self.pre() + [claude_prompt(WAKE)])
        self.assertIsInstance(result, wrapper_unix.Uncertain)
        self.assertEqual(result.reason, "recheck-deadline")
        self.assertEqual(looks, 1, "no re-look after the budget")
        self.assert_no_enter()

    def test_a_slow_first_mode_check_or_first_capture_is_never_accepted_after_the_budget(self):
        """G3 P2, both first-look blocking steps named by Codex (#9988): the slowness sits in the mode check itself or
        in the capture itself, the screen shows the text, and no Enter follows. Positive control: the same frames with
        fast steps give one Enter."""
        idle = screen("claude-idle-start")
        for slow_step in ("mode", "capture", None):
            with self.subTest(slow_step=slow_step):
                self.fake.set_screens(idle)
                self.clock = FakeClock()
                self.keep_clock = True
                self.clock.first_overshoot = 0.0
                calls = {"mode": 0, "capture": 0}
                real_leave = wrapper_unix._leave_mode

                def leave(pane):
                    calls["mode"] += 1
                    if slow_step == "mode" and calls["mode"] == 2:  # the first POST-paste mode check
                        self.clock.now += 1.9
                        self.clock.first_overshoot = 1.9
                    return True
                looks_seq = self.pre() + [claude_prompt(WAKE)]

                def slow_capture(target):
                    calls["capture"] += 1
                    if slow_step == "capture" and calls["capture"] == 3:  # the first post-paste capture
                        self.clock.now += 1.9
                        self.clock.first_overshoot = 1.9
                    return looks_seq.pop(0) if len(looks_seq) > 1 else looks_seq[0]
                with mock.patch.object(wrapper_unix, "_leave_mode", side_effect=leave), \
                     mock.patch.object(wrapper_unix, "_capture_styled", side_effect=slow_capture), \
                     mock.patch.object(wrapper_unix, "time", self.clock), \
                     mock.patch.dict(os.environ, self.fake.env()), mock.patch("sys.stdout"):
                    result = wrapper_unix.inject(WAKE, tmux_session="s", delay=0.3, provider="claude")
                if slow_step is None:
                    self.assertIs(result, True)
                    self.assert_one_enter()
                else:
                    self.assertIsInstance(result, wrapper_unix.Uncertain)
                    self.assertEqual(result.reason, "recheck-deadline")
                    self.assertEqual(calls["capture"], 3, "no re-look after the budget")
                    self.assert_no_enter()
                del real_leave

    def test_a_long_prompt_keeps_its_first_look_and_its_bound_follows_it(self):
        """A 3000-character prompt waits 3.0 s before its first look today; the budget is then that wait plus one
        re-look, never less than the first look, so long wakes keep today's behaviour and stay bounded."""
        long = "x" * 3000
        drawn = claude_prompt(long[:60])
        self.assertEqual(wake_gate.enter_hold_reason("claude", drawn, long), "")
        result, looks = self.inject(self.pre() + [drawn], text=long)
        self.assertIs(result, True)
        self.assertEqual(looks, 1)
        self.assert_one_enter()
        self.fake.set_screens(screen("claude-idle-start"))
        result, looks = self.inject(self.pre() + [screen("claude-idle-start")], text=long)
        self.assertIsInstance(result, wrapper_unix.Uncertain)
        self.assertEqual(result.reason, "paste-not-in-prompt")
        self.assertEqual(looks, 2, "the first look at 3.0 s and one re-look at 3.1 s")
        self.assertAlmostEqual(self.since_paste, 3.0 + wrapper_unix.POST_PASTE_RELOOK_S, places=6)
        self.assert_no_enter()

    def test_text_drawn_in_a_late_capture_inside_the_deadline_is_entered_once(self):
        """The complement of the previous test: the same frames with an exact clock are accepted at the last look."""
        idle = screen("claude-idle-start")
        result, looks = self.inject(self.pre() + [idle] * 17 + [claude_prompt(WAKE)])
        self.assertIs(result, True)
        self.assertEqual(looks, 18)
        self.assert_one_enter()
        self.assertAlmostEqual(self.since_paste, wrapper_unix.POST_PASTE_DEADLINE_S, places=6)

    def test_uncertain_is_a_plain_string_for_the_watcher_and_carries_its_reason(self):
        result = wrapper_unix.Uncertain("paste-not-in-prompt")
        self.assertIsInstance(result, str)
        self.assertEqual(result.reason, "paste-not-in-prompt")
        self.assertIsNot(result, True)
        self.assertIsNot(result, False)
        self.assertIsNot(result, wake_gate.HELD)


class SettledCheckTests(unittest.TestCase):
    def check(self, provider, screens):
        results = []
        feed = iter(screens)
        with mock.patch.object(wrapper_unix, "_capture_styled", side_effect=lambda target: next(feed)):
            hold = wrapper_unix.settled_hold_check("s", provider)
            for _ in screens:
                results.append(hold())
        return results

    def test_the_same_idle_screen_twice_is_settled(self):
        idle = screen("claude-idle-start")
        self.assertEqual(self.check("claude", [idle, idle, idle]), ["settling", "", ""])

    def test_a_stream_that_looks_idle_never_settles_while_it_moves(self):
        moving = ["x" * n + "\n" + screen("codex-streaming") for n in range(3)]
        self.assertEqual(self.check("codex", moving), ["settling"] * 3)

    def test_codex_prompt_particles_do_not_stop_it_settling(self):
        idle = screen("codex-idle-start")
        sparkled = idle.replace("\x1b[1m›\x1b[0m " + CODEX_PLACEHOLDER,
                                "\x1b[1m›\x1b[0m⠁" + CODEX_PLACEHOLDER + "    ⠄")
        self.assertNotEqual(idle, sparkled)
        self.assertEqual(self.check("codex", [idle, sparkled]), ["settling", ""])

    def test_a_hold_in_between_starts_settling_again(self):
        idle = screen("claude-idle-start")
        self.assertEqual(self.check("claude", [idle, screen("claude-dialog"), idle, idle]),
                         ["settling", "dialog", "settling", ""])

    def test_an_unreadable_screen_holds(self):
        self.assertEqual(self.check("claude", [None]), ["unreadable"])


class StopWatcher(BaseException):
    pass


class WatcherHoldTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.queue = self.root / "tester_queue.jsonl"
        self.trigger = AgentTrigger(SimpleNamespace(get_state=lambda name: "ready"), str(self.root))

    def put(self, prompt="COMPLETION-A", channel="general"):
        self.trigger.trigger_sync("tester", "test: wake", channel, None, prompt=prompt)

    def side(self, suffix):
        return self.queue.with_suffix(suffix)

    def trace(self):
        path = self.side(".delivery.jsonl")
        return path.read_text() if path.exists() else ""

    def watch(self, holds, outcomes=(True,), polls=None, after_inject=None, **kwargs):
        """Real _queue_watcher. holds: successive hold_check answers (the last repeats; an Exception raises)."""
        calls = []
        checks = []
        ticks = [0]
        polls = polls or len(holds) + 2

        def inject(prompt):
            calls.append(prompt)
            if after_inject:
                after_inject()
            return outcomes[min(len(calls) - 1, len(outcomes) - 1)]

        def hold_check():
            answer = holds[min(len(checks), len(holds) - 1)]
            checks.append(answer)
            if isinstance(answer, Exception):
                raise answer
            return answer
        inject.hold_check = hold_check

        def sleep(seconds):
            if seconds == 1:
                ticks[0] += 1
                if ticks[0] >= polls:
                    raise StopWatcher()

        with mock.patch.object(wrapper.time, "sleep", sleep), \
             mock.patch.object(wrapper, "_fetch_role", return_value=""), \
             mock.patch.object(wrapper, "_fetch_active_rules", return_value=None):
            with self.assertRaises(StopWatcher):
                wrapper._queue_watcher(lambda: ("tester", self.queue), inject, **kwargs)
        return calls

    def test_a_hold_longer_than_the_old_budget_keeps_every_byte_and_counts_no_attempt(self):
        self.put()
        before = self.queue.read_bytes()
        with mock.patch.object(wrapper, "begin_batch") as begin:
            self.assertEqual(self.watch(["dialog"], polls=8), [])
        begin.assert_not_called()
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertFalse(self.side(".attempt.json").exists())
        self.assertFalse(self.side(".failed.jsonl").exists())
        self.assertEqual(self.trace(), "")
        state = json.loads(self.side(".hold.json").read_text())
        self.assertEqual((state["reason"], state["count"]), ("dialog", 1))

    def test_a_hold_survives_a_restart_and_a_later_append_joins_it(self):
        self.put("FIRST")
        self.assertEqual(self.watch(["working"], polls=3), [])
        since = json.loads(self.side(".hold.json").read_text())["since"]
        self.put("SECOND")
        self.assertEqual(self.watch(["working"], polls=3), [])
        state = json.loads(self.side(".hold.json").read_text())
        self.assertEqual(state["since"], since, "a restart does not reset how long it has been held")
        self.assertEqual(state["count"], 2)
        self.assertEqual(self.trace(), "")
        self.assertEqual(sum("FIRST" in l or "SECOND" in l for l in self.queue.read_text().splitlines()), 2)

    def test_a_hold_that_clears_delivers_with_no_intervention(self):
        self.put("FIRST")
        self.put("SECOND")
        calls = self.watch(["dialog", "working", "typed", "settling", ""], polls=8)
        self.assertEqual(len(calls), 1)
        self.assertIn("FIRST", calls[0])
        self.assertIn("SECOND", calls[0])
        self.assertEqual(self.queue.read_text(), "")
        self.assertFalse(self.side(".hold.json").exists())
        self.assertIn("transport-accepted", self.trace())

    def test_a_wake_appended_during_a_hold_joins_the_batch_that_goes_in(self):
        self.put("FIRST")
        answers = iter(["dialog", "dialog", ""])
        checks = []

        def hold_check():
            checks.append(1)
            if len(checks) == 2:
                self.put("LATER")
            return next(answers, "")
        calls = []

        def inject(prompt):
            calls.append(prompt)
            return True
        inject.hold_check = hold_check
        ticks = [0]

        def sleep(seconds):
            if seconds == 1:
                ticks[0] += 1
                if ticks[0] >= 6:
                    raise StopWatcher()
        with mock.patch.object(wrapper.time, "sleep", sleep), mock.patch.object(wrapper, "_fetch_role", return_value=""), \
             mock.patch.object(wrapper, "_fetch_active_rules", return_value=None):
            with self.assertRaises(StopWatcher):
                wrapper._queue_watcher(lambda: ("tester", self.queue), inject)
        self.assertEqual(len(calls), 1, "one submission, carrying both")
        self.assertIn("FIRST", calls[0])
        self.assertIn("LATER", calls[0])
        self.assertEqual(self.queue.read_text(), "")

    def test_tmux_accepting_the_keys_is_not_recorded_as_delivered(self):
        """A dialog can swallow the wake while tmux reports success (claude-2, codex-5)."""
        self.put()
        self.watch([""], outcomes=(True,))
        self.assertIn("transport-accepted", self.trace())
        self.assertNotIn("delivered", self.trace())

    def test_a_held_result_after_the_journal_keeps_the_wake_and_clears_the_journal(self):
        self.put()
        before = self.queue.read_bytes()
        calls = self.watch(["", "dialog"], outcomes=(wake_gate.HELD,), polls=5)
        self.assertEqual(len(calls), 1, "the second poll holds before injecting")
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertFalse(self.side(".attempt.json").exists())
        self.assertFalse(self.side(".failed.jsonl").exists())
        self.assertIn('"outcome": "held"', self.trace())
        self.assertTrue(self.side(".hold.json").exists())

    def test_a_held_result_gives_the_rules_and_identity_hint_back(self):
        self.put()
        rules = {"epoch": 7, "rules": ["BE KIND"], "refresh_interval": 0}
        prompts = []
        results = iter([wake_gate.HELD, True])

        def inject(prompt):
            prompts.append(prompt)
            return next(results)
        inject.hold_check = lambda: ""
        ticks = [0]

        def sleep(seconds):
            if seconds == 1:
                ticks[0] += 1
                if ticks[0] >= 4:
                    raise StopWatcher()
        with mock.patch.object(wrapper.time, "sleep", sleep), mock.patch.object(wrapper, "_fetch_role", return_value=""), \
             mock.patch.object(wrapper, "_fetch_active_rules", return_value=rules), \
             mock.patch.object(wrapper, "_report_rule_sync"):
            with self.assertRaises(StopWatcher):
                wrapper._queue_watcher(lambda: ("tester", self.queue), inject, is_multi_instance=True)
        self.assertEqual(len(prompts), 2)
        self.assertIn("BE KIND", prompts[1])
        self.assertIn("multi-instance", prompts[1])

    def test_a_check_that_raises_holds(self):
        self.put()
        self.assertEqual(self.watch([RuntimeError("boom")], polls=3), [])
        self.assertIn("check-error", json.loads(self.side(".hold.json").read_text())["reason"])
        self.assertTrue(self.queue.read_text())

    def test_an_ambiguous_write_is_still_never_replayed(self):
        for outcome in (None, wrapper_unix.Uncertain("paste-not-in-prompt")):
            with self.subTest(outcome=outcome):
                self.put()
                calls = self.watch([""], outcomes=(outcome,), polls=5)
                self.assertEqual(len(calls), 1)
                self.assertIn("uncertain", self.trace())
                self.assertEqual(self.queue.read_text(), "")
                self.assertIn("COMPLETION-A", self.side(".failed.jsonl").read_text())
                self.side(".failed.jsonl").unlink()
                self.side(".delivery.jsonl").unlink()

    def test_the_journal_row_of_an_uncertain_delivery_names_its_reason_and_no_prompt_text(self):
        """ad-ruq criterion 3: today the reason is printed on the wrapper's terminal only (2026-11-08 record, D4)."""
        def raising(prompt):
            raise RuntimeError("boom")
        cases = ((wrapper_unix.Uncertain("paste-not-in-prompt"), "paste-not-in-prompt"),
                 (wrapper_unix.Uncertain("working-or-dialog"), "working-or-dialog"),
                 (raising, "inject-raised"),
                 (None, None))
        for outcome, reason in cases:
            with self.subTest(reason=reason):
                for suffix in (".failed.jsonl", ".delivery.jsonl"):
                    self.side(suffix).unlink(missing_ok=True)
                self.put("COMPLETION-SECRET")
                if callable(outcome):
                    outcome.hold_check = lambda: ""
                    ticks = [0]

                    def sleep(seconds):
                        if seconds == 1:
                            ticks[0] += 1
                            if ticks[0] >= 4:
                                raise StopWatcher()
                    with mock.patch.object(wrapper.time, "sleep", sleep), \
                         mock.patch.object(wrapper, "_fetch_role", return_value=""), \
                         mock.patch.object(wrapper, "_fetch_active_rules", return_value=None):
                        with self.assertRaises(StopWatcher):
                            wrapper._queue_watcher(lambda: ("tester", self.queue), outcome)
                else:
                    self.watch([""], outcomes=(outcome,), polls=5)
                rows = [json.loads(line) for line in self.trace().splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["outcome"], "uncertain")
                self.assertEqual(rows[0].get("reason"), reason)
                self.assertNotIn("COMPLETION-SECRET", self.trace(), "the journal never holds prompt text")
                self.assertIn("COMPLETION-SECRET", self.side(".failed.jsonl").read_text())
                self.assertFalse(self.side(".hold.json").exists())

    def test_a_held_pane_does_not_turn_an_interrupted_old_attempt_into_a_hold(self):
        """A journal left by a dead wrapper means a paste may have arrived: quarantined, not replayed."""
        self.put()
        lines = self.queue.read_text().splitlines(keepends=True)
        wrapper.begin_batch(self.queue, lines, 1)
        self.assertEqual(self.watch(["dialog"], polls=3), [])
        self.assertIn("COMPLETION-A", self.side(".failed.jsonl").read_text())
        self.assertEqual(self.queue.read_text(), "")

    def test_an_acknowledgement_failure_retries_only_the_acknowledgement(self):
        self.put()
        original = wrapper.finish_batch
        tries = []

        def finish(*args, **kwargs):
            tries.append(1)
            if len(tries) == 1:
                raise OSError("temporary")
            return original(*args, **kwargs)
        with mock.patch.object(wrapper, "finish_batch", finish):
            self.assertEqual(len(self.watch([""], polls=5)), 1)
        self.assertEqual(self.queue.read_text(), "")

    def test_a_wake_for_another_identity_stays_dropped_after_the_hold(self):
        self.queue.write_text(json.dumps({"prompt": "NOT-MINE", "identity_id": "other"}) + "\n")
        calls = self.watch(["dialog", ""], polls=5, get_identity_id_fn=lambda: "mine")
        self.assertEqual(calls, [])
        self.assertEqual(self.queue.read_text(), "")

    def test_a_wake_for_a_retired_identity_is_kept_for_recovery_not_lost(self):
        """A canary passing must not hide an old completion (review F2)."""
        old = json.dumps({"prompt": "OLD-JOB-END", "identity_id": "old"}) + "\n"
        new = json.dumps({"prompt": "CANARY", "identity_id": "mine"}) + "\n"
        self.queue.write_text(old + new)
        calls = self.watch([""], polls=4, get_identity_id_fn=lambda: "mine")
        self.assertEqual(len(calls), 1)
        self.assertIn("CANARY", calls[0])
        self.assertNotIn("OLD-JOB-END", calls[0])
        self.assertEqual(self.queue.read_text(), "")
        self.assertEqual(self.side(".failed.jsonl").read_text(), old)
        outcomes = [json.loads(line)["outcome"] for line in self.trace().splitlines()]
        self.assertEqual(outcomes, ["stale-identity", "transport-accepted"])

    def test_a_retired_wake_is_kept_once_across_a_held_attempt(self):
        old = json.dumps({"prompt": "OLD-JOB-END", "identity_id": "old"}) + "\n"
        new = json.dumps({"prompt": "CANARY", "identity_id": "mine"}) + "\n"
        self.queue.write_text(old + new)
        calls = self.watch([""], outcomes=(wake_gate.HELD, True), polls=5, get_identity_id_fn=lambda: "mine")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.side(".failed.jsonl").read_text(), old)
        self.assertEqual(self.queue.read_text(), "")

    def test_a_queue_with_only_a_retired_wake_still_keeps_it(self):
        old = json.dumps({"prompt": "OLD-JOB-END", "identity_id": "old"}) + "\n"
        self.queue.write_text(old)
        self.assertEqual(self.watch([""], polls=3, get_identity_id_fn=lambda: "mine"), [])
        self.assertEqual(self.side(".failed.jsonl").read_text(), old)
        self.assertEqual(self.queue.read_text(), "")

    def test_recurring_holds_at_the_injector_keep_their_age_and_reach_the_notice(self):
        """The first check passing is not the wake being resolved (review F3)."""
        self.put()
        clock = itertools.count(1000, 20)
        with mock.patch.object(wrapper.time, "time", lambda: next(clock)), \
             mock.patch.object(wrapper, "_post_hold_notice", return_value=True) as post:
            self.watch([""], outcomes=(wake_gate.HELD,), polls=30, get_token_fn=lambda: "tok")
        self.assertEqual(post.call_count, 1)
        record = json.loads(self.side(".hold.json").read_text())
        self.assertTrue(record["noticed"])
        self.assertLess(record["since"], 1100)
        self.assertNotEqual(self.queue.read_text(), "")

    def test_the_hold_record_goes_when_the_wake_is_resolved(self):
        for outcome in (True, None):
            with self.subTest(outcome=outcome):
                self.put()
                self.watch(["dialog", ""], outcomes=(outcome,), polls=3)
                self.assertFalse(self.side(".hold.json").exists())

    def test_a_wake_arriving_as_the_last_goes_in_does_not_inherit_its_hold_age(self):
        self.put("COMPLETION-A")
        clock = itertools.count(1000, 100)
        with mock.patch.object(wrapper.time, "time", lambda: next(clock)):
            calls = self.watch(["dialog", "", "dialog"], polls=4, after_inject=lambda: self.put("COMPLETION-B"))
        self.assertEqual(len(calls), 1)
        self.assertIn("COMPLETION-B", self.queue.read_text())
        self.assertGreater(json.loads(self.side(".hold.json").read_text())["since"], 1000)

    def test_the_hold_record_stays_while_a_not_pasted_retry_is_pending(self):
        self.put()
        self.watch(["dialog", ""], outcomes=(False,), polls=3)
        self.assertTrue(self.side(".hold.json").exists())
        self.assertNotEqual(self.queue.read_text(), "")

    def test_the_hold_record_goes_when_not_pasted_is_exhausted(self):
        self.put()
        self.watch(["dialog", ""], outcomes=(False,), polls=6)
        self.assertFalse(self.side(".hold.json").exists())
        self.assertTrue(self.side(".failed.jsonl").exists())

    def test_one_notice_only_once_the_threshold_passes(self):
        self.put(channel="ops")
        posted = []
        with mock.patch.object(wrapper, "_post_hold_notice",
                               side_effect=lambda port, token, channel, text: posted.append((channel, text)) or True), \
             mock.patch.object(wrapper, "HOLD_NOTICE_AFTER_S", 0):
            self.watch(["dialog"], polls=6, get_token_fn=lambda: "tok")
        self.assertEqual(len(posted), 1)
        channel, text = posted[0]
        self.assertEqual(channel, "ops")
        self.assertIn("dialog", text)
        self.assertNotIn("@", text, "a notice must not wake anybody")
        self.assertTrue(json.loads(self.side(".hold.json").read_text())["noticed"])

    def test_no_notice_before_the_threshold(self):
        self.put()
        with mock.patch.object(wrapper, "_post_hold_notice") as post:
            self.watch(["dialog"], polls=4)
        post.assert_not_called()

    def test_a_notice_that_cannot_post_is_retried_later_not_on_every_poll(self):
        self.put()
        with mock.patch.object(wrapper, "_post_hold_notice", return_value=False) as post, \
             mock.patch.object(wrapper, "HOLD_NOTICE_AFTER_S", 0):
            self.watch(["dialog"], polls=6)
        self.assertEqual(post.call_count, 1)
        self.assertFalse(json.loads(self.side(".hold.json").read_text())["noticed"])

    def test_a_notice_survives_a_restart_without_repeating(self):
        self.put()
        with mock.patch.object(wrapper, "_post_hold_notice", return_value=True) as post, \
             mock.patch.object(wrapper, "HOLD_NOTICE_AFTER_S", 0):
            self.watch(["dialog"], polls=3)
            self.watch(["dialog"], polls=3)
        self.assertEqual(post.call_count, 1)

    def test_a_stale_sidecar_goes_when_the_queue_is_empty(self):
        self.queue.write_text("")
        self.side(".hold.json").write_text('{"since": 1, "noticed": true}')
        self.watch([""], polls=2)
        self.assertFalse(self.side(".hold.json").exists())


class WiringTests(unittest.TestCase):
    def test_run_agent_hands_out_an_injector_that_holds(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeTmux(tmp)
            fake.set_screens(screen("claude-dialog"))
            captured = []
            with mock.patch.dict(os.environ, fake.env()), mock.patch("sys.stdout"), \
                 mock.patch.object(wrapper_unix, "_check_tmux"), \
                 mock.patch.object(wrapper_unix, "_session_exists", return_value=False):
                wrapper_unix.run_agent("claude", [], tmp, os.environ.copy(), None, "claude", True,
                                       captured.append, session_name="s", provider="claude")
                inject_fn = captured[0]
                self.assertTrue(inject_fn.hold_check())
                self.assertIs(inject_fn(WAKE), wake_gate.HELD)
            self.assertEqual(fake.input_verbs(), [])

    def test_both_watcher_threads_get_that_injector_and_the_wrapper_names_the_provider(self):
        """The monitor's replacement thread reuses the same function object, hold_check included."""
        source = (Path(wrapper.__file__)).read_text("utf-8")
        self.assertIn("args=(get_identity, inject_fn)", source)
        self.assertIn("args=(get_identity, _watcher_inject_fn)", source)
        self.assertIn("_watcher_inject_fn = inject_fn", source)
        self.assertIn('run_kwargs["provider"] = gate_provider(command)', source)
        self.assertIn("getattr(inject_fn, 'hold_check', None)", source)

    def test_the_release_builder_ships_the_new_module(self):
        source = (Path(wrapper.__file__).parent / "build_release.py").read_text("utf-8")
        self.assertIn('"wake_gate.py"', source)


if __name__ == "__main__":
    unittest.main()
