"""Integration-test harness for the ready gate (TD-006 T5).

Provides:
  - Server: a REAL `run.py` subprocess on scratch ports + a scratch data dir
    (stdlib urllib only — the fork suite has no TestClient precedent).
  - Wrapper: a real `wrapper.py` subprocess whose tmux is the fake-tmux
    PATH-shim, so the CLI pane is a deterministic scripted scenario.
  - api/poll_until/queue helpers shared by the tests.

Isolation contract: subprocess environments are scrubbed of every ambient
AGENTCHATTR_* variable, then pinned to this fixture's scratch values, so the
suite can never touch (or be steered by) the live server or live data dir.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
FAKE_TMUX = Path(__file__).resolve().parent / "fake-tmux"
PYTHON = sys.executable


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def scrubbed_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("AGENTCHATTR_")}
    # Subprocess stdout goes to a log file the tests poll while the process
    # is still alive; block buffering would hide lines until exit.
    env["PYTHONUNBUFFERED"] = "1"
    env.update({k: str(v) for k, v in extra.items()})
    return env


def poll_until(predicate, deadline_s: float, desc: str, interval: float = 0.05):
    """Poll-with-deadline (plan Task 5 step 3: no bare sleeps in assertions).

    Returns the first truthy predicate() value; raises AssertionError with
    `desc` if the deadline passes first.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out after {deadline_s}s waiting for: {desc}")


def assert_stable(sample_fn, duration_s: float, desc: str, interval: float = 0.1):
    """Assert sample_fn() stays EQUAL to its initial value for duration_s."""
    initial = sample_fn()
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        current = sample_fn()
        if current != initial:
            raise AssertionError(
                f"{desc}: changed during observation window "
                f"({initial!r} -> {current!r})")
        time.sleep(interval)
    return initial


def api(base_url: str, method: str, path: str, *, token: str | None = None,
        session_token: str | None = None, body=None, timeout: float = 5.0):
    """One HTTP call. Returns (status_code, parsed_json_or_text)."""
    data = None
    if body is not None:
        data = json.dumps(body).encode()
    elif method == "POST":
        data = b""
    req = Request(base_url + path, method=method, data=data)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if session_token:
        req.add_header("X-Session-Token", session_token)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except HTTPError as exc:
        raw = exc.read()
        status = exc.code
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw.decode(errors="replace")


class Server:
    """Real run.py subprocess on scratch ports with a scratch data dir."""

    def __init__(self, tmp: Path):
        self.tmp = Path(tmp)
        self.data_dir = self.tmp / "data"
        self.port = free_port()
        self.mcp_http_port = free_port()
        self.mcp_sse_port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path = self.tmp / "server.log"
        self.env = scrubbed_env(
            AGENTCHATTR_DATA_DIR=self.data_dir,
            AGENTCHATTR_PORT=self.port,
            AGENTCHATTR_MCP_HTTP_PORT=self.mcp_http_port,
            AGENTCHATTR_MCP_SSE_PORT=self.mcp_sse_port,
            AGENTCHATTR_UPLOAD_DIR=self.tmp / "uploads",
        )
        self._launch()

    def _launch(self):
        self._log_fh = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [PYTHON, str(ROOT / "run.py")],
            cwd=str(self.tmp), env=self.env,
            stdout=self._log_fh, stderr=subprocess.STDOUT,
        )
        try:
            poll_until(self._is_up, 30, f"server on port {self.port} to accept HTTP")
        except Exception:
            self.stop()
            raise

    def restart_wiped(self):
        """Kill the server and restart it on the SAME port with a WIPED data
        dir — the box-respin event: every previously issued instance token
        now resolves nowhere, so live wrappers' heartbeats 409."""
        self.kill()
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.log_path.unlink(missing_ok=True)   # fresh log, fresh session token
        self._launch()

    def _is_up(self) -> bool:
        if self.proc.poll() is not None:
            raise AssertionError(
                f"server exited rc={self.proc.returncode}; log:\n{self.log()}")
        try:
            with urlopen(f"{self.base_url}/api/roles", timeout=1) as resp:
                return resp.status < 500
        except HTTPError:
            return True          # any HTTP response proves the server is up
        except (URLError, OSError):
            return False

    def log(self) -> str:
        self._log_fh.flush()
        return self.log_path.read_text(errors="replace")

    @property
    def session_token(self) -> str:
        match = re.search(r"Session token: ([0-9a-f]+)", self.log())
        if not match:
            raise AssertionError("session token not found in server log")
        return match.group(1)

    def status(self) -> dict:
        code, payload = api(self.base_url, "GET", "/api/status",
                            session_token=self.session_token)
        if code != 200:
            raise AssertionError(f"/api/status -> {code}: {payload}")
        return payload

    def register(self, base: str, *, ready_gate: bool = False) -> dict:
        code, payload = api(self.base_url, "POST", "/api/register",
                            body={"base": base, "ready_gate": ready_gate})
        if code != 200:
            raise AssertionError(f"/api/register {base} -> {code}: {payload}")
        return payload

    def messages(self, token: str, limit: int = 50) -> list:
        code, payload = api(self.base_url, "GET",
                            f"/api/messages?limit={limit}", token=token)
        if code != 200:
            raise AssertionError(f"/api/messages -> {code}: {payload}")
        return payload

    def send(self, token: str, text: str, channel: str = "general"):
        code, payload = api(self.base_url, "POST", "/api/send", token=token,
                            body={"text": text, "channel": channel})
        if code != 200:
            raise AssertionError(f"/api/send -> {code}: {payload}")
        return payload

    def queue_path(self, name: str) -> Path:
        return self.data_dir / f"{name}_queue.jsonl"

    def write_queue_entry(self, name: str, text: str = "direct entry") -> str:
        """Simulate a pre-existing queue entry (bypasses routing on purpose)."""
        line = json.dumps({"sender": "probe", "text": text,
                           "time": "00:00:00", "channel": "general"}) + "\n"
        path = self.queue_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return line

    def read_queue(self, name: str) -> str | None:
        path = self.queue_path(name)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def suspend(self):
        """SIGSTOP: the port stays open but nothing is served — requests to
        the server hang until their client-side timeout (the 'hung server'
        ready-POST failure shape)."""
        import signal
        self.proc.send_signal(signal.SIGSTOP)

    def resume(self):
        import signal
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self._log_fh.close()

    def kill(self):
        """Hard-kill (ready-POST-failure scenario). No graceful shutdown."""
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self._log_fh.close()


class Wrapper:
    """Real wrapper.py subprocess with fake-tmux first on PATH."""

    _counter = 0

    def __init__(self, tmp: Path, server: Server, *, agent: str = "claude",
                 scenario: list[str], gated: bool = True,
                 pattern: str = "READY>", blockers: tuple = (),
                 ready_timeout: int = 30, no_restart: bool = True):
        Wrapper._counter += 1
        self.agent = agent
        self.dir = Path(tmp) / f"wrapper-{Wrapper._counter}"
        self.dir.mkdir(parents=True)
        self.fake_dir = self.dir / "tmux-state"
        self.fake_dir.mkdir()
        self.set_scenario(scenario)

        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        (bin_dir / "tmux").symlink_to(FAKE_TMUX)

        env = dict(server.env)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_TMUX_DIR"] = str(self.fake_dir)
        env.pop("AGENTCHATTR_REPO_SLUG", None)   # session name = agentchattr-<name>

        argv = [PYTHON, str(ROOT / "wrapper.py"), agent]
        if no_restart:
            argv.append("--no-restart")
        if gated:
            argv += ["--ready-gate", "--ready-pattern", pattern,
                     "--ready-timeout", str(ready_timeout)]
            for spec in blockers:
                argv += ["--ready-blocker", spec]

        self.log_path = self.dir / "wrapper.log"
        self._log_fh = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            argv, cwd=str(self.dir), env=env,
            stdout=self._log_fh, stderr=subprocess.STDOUT,
        )

    # --- scripted pane -----------------------------------------------------
    def set_scenario(self, lines: list[str]):
        (self.fake_dir / "scenario").write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8")

    # --- observation -------------------------------------------------------
    def log(self) -> str:
        self._log_fh.flush()
        return self.log_path.read_text(errors="replace")

    def registered_name(self, deadline_s: float = 10) -> str:
        def find():
            match = re.search(r"Registered as: (\S+)", self.log())
            return match.group(1) if match else None
        return poll_until(find, deadline_s, "wrapper to log its registered name")

    def session_flag(self, name: str | None = None) -> Path:
        session = f"agentchattr-{name or self.agent}"
        return self.fake_dir / f"session.{session}"

    def kill_session(self, name: str | None = None):
        """End the scripted CLI session (real tmux: the CLI process exited)."""
        self.session_flag(name).unlink(missing_ok=True)

    def keys_log(self) -> str:
        path = self.fake_dir / "keys.log"
        return path.read_text(errors="replace") if path.exists() else ""

    def calls(self, subcommand: str | None = None) -> list[str]:
        path = self.fake_dir / "calls.log"
        if not path.exists():
            return []
        lines = path.read_text(errors="replace").splitlines()
        if subcommand:
            lines = [l for l in lines if l.startswith(subcommand)]
        return lines

    def wait_exit(self, deadline_s: float) -> int:
        try:
            return self.proc.wait(timeout=deadline_s)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"wrapper still running after {deadline_s}s; log:\n{self.log()}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self._log_fh.close()
