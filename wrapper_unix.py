"""Mac/Linux agent injection — uses tmux send-keys to type into the agent CLI.

Called by wrapper.py on Mac and Linux. Requires tmux to be installed.
  - Mac:   brew install tmux
  - Linux: apt install tmux  (or yum, pacman, etc.)

How it works:
  1. Creates a tmux session running the agent CLI
  2. Queue watcher sends keystrokes via 'tmux send-keys'
  3. Wrapper attaches to the session so you see the full TUI
  4. Ctrl+B, D to detach (agent keeps running in background)
"""

import shlex
import shutil
import subprocess
import sys
import time


def _session_exists(session_name: str) -> bool:
    """Return True while the tmux session is still alive."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    return result.returncode == 0


def _check_tmux():
    """Verify tmux is installed, exit with helpful message if not."""
    if shutil.which("tmux"):
        return
    print("\n  Error: tmux is required for auto-trigger on Mac/Linux.")
    if sys.platform == "darwin":
        print("  Install: brew install tmux")
    else:
        print("  Install: apt install tmux  (or yum/pacman equivalent)")
    sys.exit(1)


def inject(text: str, *, tmux_session: str, delay: float = 0.3):
    """Send text + Enter to a tmux session via send-keys."""
    # Use -l to send text literally (avoids misinterpreting as key names),
    # then send Enter as a separate key press
    subprocess.run(
        ["tmux", "send-keys", "-t", tmux_session, "-l", text],
        capture_output=True,
    )
    # Scale delay with text length so longer prompts get more processing time
    time.sleep(max(delay, len(text) * 0.001))
    subprocess.run(
        ["tmux", "send-keys", "-t", tmux_session, "Enter"],
        capture_output=True,
    )


def get_activity_checker(session_name, trigger_flag=None):
    """Return a callable that detects tmux pane output by hashing content."""
    last_hash = [None]

    def check():
        # External trigger: queue watcher injected a message
        if trigger_flag is not None and trigger_flag[0]:
            trigger_flag[0] = False
            return True
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", session_name, "-p"],
                capture_output=True, timeout=2,
            )
            h = hash(result.stdout)
            changed = last_hash[0] is not None and h != last_hash[0]
            last_hash[0] = h
            return changed
        except Exception:
            return False

    return check


def run_agent(
    command,
    extra_args,
    cwd,
    env,
    queue_file,
    agent,
    no_restart,
    start_watcher,
    strip_env=None,
    pid_holder=None,
    session_name=None,
    inject_env=None,
    inject_delay: float = 0.3,
    ready_gate_cfg=None,
    on_starting=None,
    on_ready=None,
    on_failed=None,
    clear_ready=None,
    stash_inject_fn=None,
):
    """Run agent inside a tmux session, inject via tmux send-keys.

    Ready gate (TD-006, opt-in via ready_gate_cfg): EVERY launch iteration -
    initial and automatic restart - closes the gate, tells the server
    `starting` BEFORE the session exists, probes the pane, and only a
    successful server-side ready transition releases anything locally
    (#3925.3). Any non-ready verdict kills the session, cancels the
    registration, and exits 3 without entering the restart loop."""
    _check_tmux()

    session_name = session_name or f"agentchattr-{agent}"
    agent_cmd = " ".join(
        [shlex.quote(command)] + [shlex.quote(a) for a in extra_args]
    )

    # Build env(1) prefix for the command INSIDE the tmux session.
    # subprocess.run(env=...) only affects the tmux client binary — the
    # session shell inherits from the tmux server instead.  Use env(1)
    # to set (-u to unset, VAR=val to inject) vars in the actual session.
    env_parts = []
    if strip_env:
        env_parts.extend(f"-u {shlex.quote(v)}" for v in strip_env)
    if inject_env:
        env_parts.extend(
            f"{shlex.quote(k)}={shlex.quote(v)}"
            for k, v in inject_env.items()
        )
    if env_parts:
        agent_cmd = f"env {' '.join(env_parts)} {agent_cmd}"

    # Resolve cwd to absolute path (tmux -c needs it)
    from pathlib import Path
    abs_cwd = str(Path(cwd).resolve())

    # Wire up injection with the tmux session name
    inject_fn = lambda text: inject(text, tmux_session=session_name, delay=inject_delay)
    if ready_gate_cfg is None:
        start_watcher(inject_fn)        # ungated: today's behavior, unchanged
    else:
        # Gated: the wrapper starts the watcher only at the first successful
        # ready transition; until then nothing may read or clear the queue.
        stash_inject_fn(inject_fn)

    print(f"  Using tmux session: {session_name}")
    print(f"  Detach: Ctrl+B, D  (agent keeps running)")
    print(f"  Reattach: tmux attach -t {session_name}\n")

    while True:
        try:
            if ready_gate_cfg is not None:
                # Close the gate FIRST on every iteration (initial + restart),
                # then tell the server `starting` BEFORE any session exists.
                if clear_ready:
                    clear_ready()
                try:
                    if on_starting:
                        on_starting()
                except Exception as exc:
                    print(f"  READY-GATE: starting transition failed ({exc}); aborting")
                    sys.exit(3)

            # Clean up stale session from a previous crash
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
            )

            # Create tmux session running the agent CLI
            result = subprocess.run(
                ["tmux", "new-session", "-d", "-s", session_name,
                 "-c", abs_cwd, agent_cmd],
                env=env,
            )
            if result.returncode != 0:
                print(f"  Error: failed to create tmux session (exit {result.returncode})")
                break

            if ready_gate_cfg is not None:
                from wrapper_ready import wait_cli_ready

                def _cap():
                    r = subprocess.run(
                        ["tmux", "capture-pane", "-p", "-t", session_name],
                        capture_output=True, timeout=5,
                    )
                    return r.stdout.decode(errors="replace") if r.returncode == 0 else None

                verdict = wait_cli_ready(
                    _cap,
                    ready_gate_cfg["pattern"],
                    ready_gate_cfg["blockers"],
                    ready_gate_cfg["timeout"],
                    session_alive_fn=lambda: _session_exists(session_name),
                )
                if verdict != "ready":
                    # No raw pane in the log (OAuth material): classification
                    # plus the exact command to look at the live screen.
                    print(f"  READY-GATE FAILED ({verdict}) - inspect: "
                          f"tmux capture-pane -p -t {session_name} -S -100")
                    subprocess.run(["tmux", "kill-session", "-t", session_name],
                                   capture_output=True)
                    if on_failed:
                        try:
                            on_failed(verdict)
                        except Exception:
                            pass
                    sys.exit(3)      # never falls into the restart loop
                try:
                    if on_ready:
                        on_ready()   # server transition first; raises = failure
                except Exception as exc:
                    print(f"  READY-GATE FAILED (ready-post: {exc}) - inspect: "
                          f"tmux capture-pane -p -t {session_name} -S -100")
                    subprocess.run(["tmux", "kill-session", "-t", session_name],
                                   capture_output=True)
                    if on_failed:
                        try:
                            on_failed("ready-post-failed")
                        except Exception:
                            pass
                    sys.exit(3)

            # Attach — blocks until agent exits or user detaches (Ctrl+B, D)
            subprocess.run(["tmux", "attach-session", "-t", session_name])

            # Check: did the agent exit, or did the user just detach?
            if _session_exists(session_name):
                # Session still alive — user detached, agent running in background.
                # Keep the wrapper alive so the local proxy and heartbeats survive.
                print(f"\n  Detached. {agent.capitalize()} still running in tmux.")
                print(f"  Reattach: tmux attach -t {session_name}")
                while _session_exists(session_name):
                    time.sleep(1)
                # CLI died while detached: close the gate before winding down
                # so the watcher cannot read/clear a mention for a dead pane.
                if ready_gate_cfg is not None and clear_ready:
                    clear_ready()
                break

            # Session gone — agent exited. Close the gate NOW (#3925.2): the
            # loop-top re-entry only runs after the restart delay, and a
            # mention arriving in that window must be neither consumed nor
            # erased by the still-released watcher.
            if ready_gate_cfg is not None and clear_ready:
                clear_ready()
            if no_restart:
                break

            print(f"\n  {agent.capitalize()} exited.")
            print(f"  Restarting in 3s... (Ctrl+C to quit)")
            time.sleep(3)
        except KeyboardInterrupt:
            # Kill the tmux session on Ctrl+C
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
            )
            break
