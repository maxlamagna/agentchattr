"""Agent trigger — writes to queue files picked up by visible worker terminals."""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


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
