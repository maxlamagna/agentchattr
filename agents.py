"""Agent trigger — writes to queue files picked up by visible worker terminals."""

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# A wake addressed by identity must carry the full lowercase hex id the server
# issued at registration. Anything else is a malformed request, not a name.
_IDENTITY_RE = re.compile(r"[0-9a-f]{32}")


def strict_wake_response(result: dict, identity_id: str) -> tuple[int, dict]:
    """HTTP answer for an identity-addressed wake: (status_code, body).

    The status is reported honestly - a wake that was not queued never claims
    to have been. An invalid request echoes no identity_id: it is not a valid
    identity to echo.
    """
    status = result.get("status")
    target = result.get("target")
    if status == "queued":
        return (200, {"ok": True, "queued": True, "target": target,
                      "identity_id": identity_id})
    if status == "not_live":
        return (404, {"ok": False, "queued": False, "error": "identity_not_live",
                      "identity_id": identity_id})
    if status == "not_ready":
        return (409, {"ok": False, "queued": False, "error": "not_ready",
                      "target": target, "identity_id": identity_id})
    return (400, {"ok": False, "queued": False, "error": "invalid_request"})


class AgentTrigger:
    def __init__(self, registry, data_dir: str = "./data", store=None):
        self._registry = registry
        self._data_dir = Path(data_dir)
        self._store = store   # optional; reserved for delivery-layer notices

    def is_available(self, name: str) -> bool:
        if not self._registry.is_registered(name):
            return False
        # Ready gate (TD-006): a starting instance is registered (identity
        # exists so the CLI's MCP config could be written) but must not
        # receive mention routing until it is marked ready.
        get_state = getattr(self._registry, "get_state", None)
        return get_state is None or get_state(name) != "starting"

    def get_status(self) -> dict:
        from mcp_bridge import is_online, is_active, get_role
        get_state = getattr(self._registry, "get_state", None)
        instances = self._registry.get_all()
        return {
            name: {
                "available": is_online(name),
                "busy": is_active(name),
                "label": info["label"],
                "color": info["color"],
                "role": get_role(name),
                "state": get_state(name) if get_state else info.get("state"),
            }
            for name, info in instances.items()
        }

    def _guard_starting(self, agent_name: str) -> bool:
        """True = blocked (agent is starting under the ready gate).

        Defense in depth: the routing layer (app._route_targets) already
        short-circuits starting targets with a visible system notice; this
        guard keeps any OTHER trigger path from writing a queue entry that
        startup cleanup could silently erase (spec D1.4)."""
        get_state = getattr(self._registry, "get_state", None)
        if get_state is None or get_state(agent_name) != "starting":
            return False
        log.info("NOT queued @%s: agent is starting (ready gate)", agent_name)
        return True

    async def trigger(self, agent_name: str, message: str = "", channel: str = "general",
                      job_id: int | None = None, **kwargs):
        """Write to the agent's queue file. The worker terminal picks it up.

        Returns True when a queue entry was written, False when the ready
        gate blocked delivery (TD-006)."""
        if self._guard_starting(agent_name):
            return False
        queue_file = self._data_dir / f"{agent_name}_queue.jsonl"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        import time
        entry = {
            "sender": message.split(":")[0].strip() if ":" in message else "?",
            "text": message,
            "time": time.strftime("%H:%M:%S"),
            "channel": channel,
        }
        custom_prompt = kwargs.get("prompt", "")
        if isinstance(custom_prompt, str) and custom_prompt.strip():
            entry["prompt"] = custom_prompt.strip()
        if job_id is not None:
            entry["job_id"] = job_id

        with open(queue_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        log.info("Queued @%s trigger (ch=%s, job=%s): %s", agent_name, channel, job_id, message[:80])
        return True

    def trigger_sync(self, agent_name: str, message: str = "", channel: str = "general",
                     job_id: int | None = None, **kwargs):
        """Synchronous version of trigger — writes to queue file without async.

        Returns True when a queue entry was written, False when the ready
        gate blocked delivery (TD-006)."""
        if self._guard_starting(agent_name):
            return False
        queue_file = self._data_dir / f"{agent_name}_queue.jsonl"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        import time
        entry = {
            "sender": message.split(":")[0].strip() if ":" in message else "?",
            "text": message,
            "time": time.strftime("%H:%M:%S"),
            "channel": channel,
        }
        custom_prompt = kwargs.get("prompt", "")
        if isinstance(custom_prompt, str) and custom_prompt.strip():
            entry["prompt"] = custom_prompt.strip()
        if job_id is not None:
            entry["job_id"] = job_id

        with open(queue_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        log.info("Queued @%s trigger (ch=%s, job=%s): %s", agent_name, channel, job_id, message[:80])
        return True

    def trigger_identity(self, identity_id: str, prompt: str, *, message: str = "",
                         channel: str = "general") -> dict:
        """Queue a wake for exactly one instance, addressed by its identity_id.

        The name-free alternative to trigger()/trigger_sync(): the caller cannot
        hit a whole family, a name that was recycled after someone left, or a
        name that was renamed away from. Returns exactly two keys -
        {"status": "queued"|"not_live"|"not_ready"|"invalid", "target": name|None}.

        A malformed request is refused before the registry or the filesystem is
        touched, so a refusal can never leave a queue file behind.
        """
        if not isinstance(identity_id, str) or not _IDENTITY_RE.fullmatch(identity_id):
            return {"status": "invalid", "target": None}
        if not isinstance(prompt, str) or not prompt.strip():
            return {"status": "invalid", "target": None}

        inst = self._registry.resolve_identity(identity_id)
        if not inst:
            return {"status": "not_live", "target": None}
        name = inst.get("name")
        if inst.get("state") == "starting":
            # The ready gate blocks this wake, but an identity-addressed request
            # has no chat message to annotate, so the answer is just "not_ready"
            # (deliberately not routed through _guard_starting's log-only notice).
            log.info("NOT queued by identity %s: %s is starting (ready gate)",
                     identity_id[:8], name)
            return {"status": "not_ready", "target": name}

        queue_file = self._data_dir / f"{name}_queue.jsonl"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        import time
        entry = {
            "sender": message.split(":")[0].strip() if ":" in message else "?",
            "text": message,
            "time": time.strftime("%H:%M:%S"),
            "channel": channel,
            "prompt": prompt.strip(),
            "identity_id": identity_id,
        }

        with open(queue_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        log.info("Queued identity wake %s -> @%s (ch=%s)", identity_id[:8], name, channel)
        return {"status": "queued", "target": name}
