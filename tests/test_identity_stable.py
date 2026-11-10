"""Stable identities (ad-e5gz, proposal revision 2): server, registry and bridge.

Invariants under test (proposal section 4):
  I1 a live instance's name changes only by an explicit human rename;
  I2 a token-authenticated caller is the token's instance, everywhere;
  I3 a mention or name-addressed wake reaches only the instance with exactly that name;
  I4 registration and reclaim never take a live name and never act on another instance;
  I6 (server side) a gated record keeps its gate across a reload.

Each group names the regression group of proposal section 6 it implements. The wrapper side
(ownership fence, 409 recovery, capability check, queue disposition) is in
test_identity_stable_wrapper.py; real-process cases are in test_identity_stable_integration.py.
"""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
import mcp_bridge
import registry as registry_mod
from agents import AgentTrigger
from mcp.server.fastmcp import Context
from registry import RuntimeRegistry
from store import MessageStore

BASES = {
    "claude": {"label": "Claude", "color": "#da7756"},
    "codex": {"label": "Codex", "color": "#10a37f"},
}


class FakeRequest:
    def __init__(self, headers=None, body=None):
        self.headers = headers or {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def bearer(token):
    return {"authorization": f"Bearer {token}"}


def auth_ctx(token: str) -> Context:
    request = SimpleNamespace(headers=bearer(token))
    return Context(request_context=SimpleNamespace(request=request))


def renames_on_disk(data_dir) -> dict:
    path = Path(data_dir) / "renames.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


class _Room(unittest.TestCase):
    """A registry, store, trigger and bridge wired the way app.configure wires them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.registry = self._new_registry()
        self.store = MessageStore(str(self.data_dir / "messages.jsonl"))
        self.trigger = AgentTrigger(self.registry, data_dir=str(self.data_dir))
        self._saved = {k: getattr(app, k, None) for k in ("registry", "store", "agents", "_event_loop")}
        app.registry = self.registry
        app.store = self.store
        app.agents = self.trigger
        app._event_loop = None
        mcp_bridge.store = self.store
        mcp_bridge.registry = self.registry
        mcp_bridge.agents = self.trigger
        mcp_bridge._presence.clear()
        mcp_bridge._activity.clear()
        mcp_bridge._cursors.clear()
        mcp_bridge._renamed_from.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            setattr(app, key, value)

    def _new_registry(self):
        reg = RuntimeRegistry(data_dir=str(self.data_dir))
        reg.seed(BASES)
        return reg

    def reload(self):
        """A server restart: a fresh registry over the same data dir."""
        self.registry = self._new_registry()
        self.trigger = AgentTrigger(self.registry, data_dir=str(self.data_dir))
        app.registry = self.registry
        app.agents = self.trigger
        mcp_bridge.registry = self.registry
        mcp_bridge.agents = self.trigger
        mcp_bridge._presence.clear()

    def heartbeat(self, token, path_name="claude"):
        return asyncio.run(app.heartbeat(path_name, FakeRequest(headers=bearer(token))))

    def queue_lines(self, name):
        path = self.data_dir / f"{name}_queue.jsonl"
        return path.read_text("utf-8").splitlines() if path.exists() else []

    def system_texts(self):
        return [m["text"] for m in self.store.get_recent(50) if m.get("type") == "system"]


# --- Group 1: registration order -------------------------------------------------------------

class RegistrationOrderTests(_Room):
    def test_first_three_registrations_take_base_then_numbered_names(self):
        first = self.registry.register("claude")
        second = self.registry.register("claude")
        third = self.registry.register("claude")
        self.assertEqual([first["name"], second["name"], third["name"]],
                         ["claude", "claude-2", "claude-3"])

    def test_registration_reports_no_slot1_rename_and_keeps_earlier_tokens(self):
        first = self.registry.register("claude")
        second = self.registry.register("claude")
        third = self.registry.register("claude")
        for result in (first, second, third):
            self.assertNotIn("_renamed_slot1", result)
        self.assertEqual(self.registry.resolve_token(first["token"])["name"], "claude")
        self.assertEqual(self.registry.resolve_token(second["token"])["name"], "claude-2")
        self.assertEqual(self.registry.get_instance("claude")["identity_id"], first["identity_id"])
        self.assertIsNone(self.registry.get_instance("claude-1"))

    def test_registration_writes_no_rename_mapping(self):
        self.registry.register("claude")
        self.registry.register("claude")
        self.assertEqual(renames_on_disk(self.data_dir), {})

    def test_register_endpoint_returns_the_new_name_only(self):
        asyncio.run(app.register_agent(FakeRequest(body={"base": "claude"})))
        resp = asyncio.run(app.register_agent(FakeRequest(body={"base": "claude"})))
        body = json.loads(resp.body)
        self.assertEqual(body["name"], "claude-2")
        self.assertNotIn("_renamed_slot1", body)
        self.assertIsNotNone(self.registry.get_instance("claude"))


# --- Group 2: second-instance joins and departures --------------------------------------------

class DepartureTests(_Room):
    def test_second_instance_leaving_leaves_slot1_unchanged(self):
        first = self.registry.register("claude")
        second = self.registry.register("claude")
        before = self.registry.get_instance("claude")
        result = self.registry.deregister(second["name"])
        self.assertNotIn("_renamed_back", result)
        after = self.registry.get_instance("claude")
        self.assertEqual(after, before)
        self.assertEqual(self.registry.resolve_token(first["token"])["name"], "claude")

    def test_slot1_leaving_leaves_numbered_instances_unchanged(self):
        first = self.registry.register("claude")
        second = self.registry.register("claude")
        third = self.registry.register("claude")
        self.registry.deregister(first["name"])
        self.assertEqual(self.registry.resolve_token(second["token"])["name"], "claude-2")
        self.assertEqual(self.registry.resolve_token(third["token"])["name"], "claude-3")
        self.assertEqual(self.registry.get_instance("claude-2")["slot"], 2)
        self.assertIsNone(self.registry.get_instance("claude"))

    def test_crash_timeout_departure_renames_nobody(self):
        first = self.registry.register("claude")
        second = self.registry.register("claude")
        result = self.registry.deregister(first["name"], reclaimable=True)
        self.assertNotIn("_renamed_back", result)
        self.assertEqual(self.registry.resolve_token(second["token"])["name"], "claude-2")
        self.assertEqual(renames_on_disk(self.data_dir), {})

    def test_ready_gate_cancel_renames_nobody(self):
        first = self.registry.register("claude")
        second = self.registry.register("claude")
        gated = self.registry.register("claude", ready_gate=True)
        self.registry.deregister(first["name"])
        self.assertTrue(self.registry.cancel_starting(gated["name"]))
        self.assertEqual(self.registry.resolve_token(second["token"])["name"], "claude-2")
        self.assertIsNone(self.registry.get_instance("claude"))

    def test_deregister_endpoint_rewrites_no_history(self):
        first = self.registry.register("claude")
        second = self.registry.register("claude")
        self.store.add("claude-2", "said by the second instance")
        resp = asyncio.run(app.deregister_agent(first["name"], FakeRequest(headers=bearer(first["token"]))))
        self.assertEqual(resp.status_code, 200)
        senders = [m["sender"] for m in self.store.get_recent(10)]
        self.assertIn("claude-2", senders)
        self.assertNotIn("claude", senders)
        self.assertEqual(self.registry.resolve_token(second["token"])["name"], "claude-2")


# --- Group 3: controller restart ----------------------------------------------------------------

class ControllerRestartTests(_Room):
    def test_named_registration_within_grace_returns_the_name(self):
        first = self.registry.register("claude")
        self.registry.deregister(first["name"])
        again = self.registry.register("claude", name="claude")
        self.assertEqual(again["name"], "claude")
        self.assertEqual(again["slot"], 1)
        self.assertNotEqual(again["token"], first["token"])

    def test_unnamed_registration_within_grace_never_takes_the_name(self):
        first = self.registry.register("claude")
        self.registry.deregister(first["name"])
        other = self.registry.register("claude")
        self.assertNotEqual(other["name"], "claude")
        self.assertEqual(other["name"], "claude-2")

    def test_named_registration_of_a_live_name_is_refused_and_touches_nothing(self):
        live = self.registry.register("claude")
        before = self.registry.get_instance("claude")
        with self.assertRaises(registry_mod.NameUnavailable) as caught:
            self.registry.register("claude", name="claude")
        self.assertEqual(caught.exception.code, "name_in_use")
        self.assertEqual(self.registry.get_instance("claude"), before)
        self.assertEqual(self.registry.resolve_token(live["token"])["name"], "claude")
        self.assertEqual(sorted(self.registry.get_all_names()), ["claude"])

    def test_register_endpoint_answers_409_for_a_live_name(self):
        self.registry.register("claude")
        resp = asyncio.run(app.register_agent(FakeRequest(body={"base": "claude", "name": "claude"})))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(json.loads(resp.body)["error"], "name_in_use")

    def test_register_endpoint_grants_a_free_name(self):
        resp = asyncio.run(app.register_agent(FakeRequest(body={"base": "claude", "name": "claude-4"})))
        body = json.loads(resp.body)
        self.assertEqual(body["name"], "claude-4")
        self.assertEqual(body["slot"], 4)


# --- Group 4: stale rename mappings -------------------------------------------------------------

class StaleRenameMappingTests(_Room):
    def _plant(self):
        first = self.registry.register("claude")
        second = self.registry.register("claude")
        (self.data_dir / "renames.json").write_text(json.dumps({"claude": "claude-2"}), "utf-8")
        self.registry._load_renames()
        return first, second

    def test_heartbeat_reports_the_tokens_own_name_despite_a_stale_mapping(self):
        first, _second = self._plant()
        resp = self.heartbeat(first["token"], path_name="claude")
        self.assertEqual(resp["name"], "claude")

    def test_heartbeat_touches_no_other_presence(self):
        first, _second = self._plant()
        self.heartbeat(first["token"], path_name="claude")
        self.assertIn("claude", mcp_bridge._presence)
        self.assertNotIn("claude-2", mcp_bridge._presence)

    def test_stale_mapping_after_a_server_restart(self):
        first, second = self._plant()
        self.reload()
        self.assertEqual(self.heartbeat(first["token"], path_name="claude")["name"], "claude")
        self.assertEqual(self.heartbeat(second["token"], path_name="claude-2")["name"], "claude-2")
        self.assertNotIn("claude-2", [n for n in mcp_bridge._presence if n != "claude-2"])

    def test_heartbeat_has_no_family_fallback(self):
        self.assertFalse(hasattr(self.registry, "get_family_instance"))


# --- Group 6: base-family routing ---------------------------------------------------------------

class ExactNameRoutingTests(_Room):
    def _second_only(self):
        first = self.registry.register("claude")
        second = self.registry.register("claude")
        self.registry.deregister(first["name"])
        mcp_bridge._presence["claude-2"] = time.time()
        return second

    def test_base_name_resolves_to_itself_only(self):
        self._second_only()
        self.assertEqual(self.registry.resolve_to_instances("claude"), ["claude"])

    def route(self, mention):
        """The handler's composition (_handle_new_message): resolve, then route."""
        targets = self.registry.resolve_to_instances(mention)
        asyncio.run(app._route_targets(targets, f"user: @{mention} hello", "general"))

    def test_mention_of_an_absent_base_wakes_no_family_member(self):
        self._second_only()
        self.route("claude")
        self.assertEqual(self.queue_lines("claude-2"), [])
        self.assertEqual(self.queue_lines("claude"), [])

    def test_mention_of_an_absent_name_posts_a_truthful_notice(self):
        self._second_only()
        self.route("claude")
        texts = self.system_texts()
        self.assertIn("@claude is not connected: not delivered", texts)
        self.assertFalse(any("message queued" in t for t in texts))

    def test_numbered_mention_reaches_only_that_instance(self):
        self._second_only()
        asyncio.run(app._route_targets(["claude-2"], "user: @claude-2 hello", "general"))
        self.assertEqual(len(self.queue_lines("claude-2")), 1)

    def test_trigger_agent_by_base_name_wakes_no_family_member(self):
        self._second_only()
        asyncio.run(app.trigger_agent_silent(FakeRequest(body={"agent": "claude", "message": "x"})))
        self.assertEqual(self.queue_lines("claude-2"), [])

    def test_offline_registered_agent_still_gets_queued(self):
        self.registry.register("claude")
        asyncio.run(app._route_targets(["claude"], "user: @claude hello", "general"))
        self.assertEqual(len(self.queue_lines("claude")), 1)
        self.assertIn("claude appears offline — message queued.", self.system_texts())

    def test_strict_identity_wake_unchanged(self):
        second = self._second_only()
        result = self.trigger.trigger_identity(second["identity_id"], "do the thing")
        self.assertEqual(result, {"status": "queued", "target": "claude-2"})


# --- Group 7: token ownership (chat_claim) -------------------------------------------------------

class ClaimOwnershipTests(_Room):
    def _two(self):
        first = self.registry.register("claude")
        second = self.registry.register("claude")
        self.registry._instances["claude-2"].state = "pending"   # an unclaimed sibling
        return first, second

    def test_claim_confirms_only_the_callers_instance(self):
        first, _second = self._two()
        out = json.loads(mcp_bridge.chat_claim(sender="claude", ctx=auth_ctx(first["token"])))
        self.assertEqual(out["confirmed_name"], "claude")
        self.assertEqual(self.registry.get_state("claude-2"), "pending")

    def test_claim_own_name_form_confirms(self):
        first, _second = self._two()
        out = json.loads(mcp_bridge.chat_claim(sender="claude", name="claude", ctx=auth_ctx(first["token"])))
        self.assertEqual(out["confirmed_name"], "claude")

    def test_claim_rename_form_is_refused_for_an_authenticated_session(self):
        first, _second = self._two()
        out = mcp_bridge.chat_claim(sender="claude", name="claude-music", ctx=auth_ctx(first["token"]))
        self.assertTrue(out.startswith("Error:"), out)
        self.assertIn("rename", out)
        self.assertIsNotNone(self.registry.get_instance("claude"))
        self.assertIsNone(self.registry.get_instance("claude-music"))
        self.assertEqual(renames_on_disk(self.data_dir), {})

    def test_claim_never_selects_a_pending_sibling(self):
        first, second = self._two()
        mcp_bridge.chat_claim(sender="claude", ctx=auth_ctx(first["token"]))
        mcp_bridge.chat_claim(sender="claude", name="claude", ctx=auth_ctx(first["token"]))
        self.assertEqual(self.registry.get_state("claude-2"), "pending")
        self.assertEqual(self.registry.resolve_token(second["token"])["name"], "claude-2")

    def test_instructions_no_longer_teach_self_rename(self):
        text = mcp_bridge._MCP_INSTRUCTIONS
        self.assertNotIn("renames slot 1", text)
        self.assertNotIn("name='claude-music') to reclaim", text)


# --- Group 8: reclaim ----------------------------------------------------------------------------

class ReclaimTests(_Room):
    def test_crash_timed_out_token_reactivates_under_the_same_name_beside_a_sibling(self):
        first = self.registry.register("claude")
        second = self.registry.register("claude")
        self.registry.deregister(first["name"], reclaimable=True)
        self.assertEqual(self.registry.get_instance("claude-2")["slot"], 2)
        back = self.registry.resolve_token(first["token"])
        self.assertIsNotNone(back)
        self.assertEqual(back["name"], "claude")
        self.assertEqual(self.registry.resolve_token(second["token"])["name"], "claude-2")

    def test_fresh_registration_still_wins_the_coordinate(self):
        first = self.registry.register("claude")
        self.registry.deregister(first["name"], reclaimable=True)
        self.registry._reserved.clear()
        fresh = self.registry.register("claude")
        self.assertEqual(fresh["name"], "claude")
        self.assertIsNone(self.registry.resolve_token(first["token"]))


# --- Group 9: ready gate --------------------------------------------------------------------------

class ReadyGateIdentityTests(_Room):
    def test_gated_second_registration_renames_nothing(self):
        first = self.registry.register("claude")
        gated = self.registry.register("claude", ready_gate=True)
        self.assertEqual(gated["name"], "claude-2")
        self.assertEqual(gated["state"], "starting")
        self.assertEqual(self.registry.resolve_token(first["token"])["name"], "claude")

    def test_named_gated_registration_starts_under_that_name(self):
        gated = self.registry.register("claude", ready_gate=True, name="claude")
        self.assertEqual(gated["name"], "claude")
        self.assertEqual(self.registry.get_state("claude"), "starting")
        self.assertFalse(self.trigger.is_available("claude"))


# --- Group 10: human rename preserved -------------------------------------------------------------

class HumanRenameTests(_Room):
    def test_ui_rename_moves_the_identity_and_the_heartbeat_reports_it(self):
        first = self.registry.register("claude")
        self.store.add("claude", "before the rename")
        resp = asyncio.run(app.rename_agent_label("claude", FakeRequest(body={"label": "Claude Music"})))
        self.assertEqual(json.loads(resp.body)["new_name"], "claude-music")
        self.assertEqual(self.heartbeat(first["token"], path_name="claude")["name"], "claude-music")
        self.assertIn("claude-music", [m["sender"] for m in self.store.get_recent(10)])


# --- Group 12: persisted reactivation keeps the gate ---------------------------------------------

class PersistedReactivationTests(_Room):
    def test_reloaded_starting_record_reactivates_starting(self):
        gated = self.registry.register("claude", ready_gate=True)
        self.reload()
        back = self.registry.resolve_token(gated["token"])
        self.assertEqual(back["state"], "starting")
        self.assertNotIn("claude", mcp_bridge._presence)
        self.assertFalse(self.trigger.is_available("claude"))
        self.assertEqual(self.trigger.trigger_identity(gated["identity_id"], "p")["status"], "not_ready")
        asyncio.run(app._route_targets(["claude"], "user: @claude hi", "general"))
        self.assertEqual(self.queue_lines("claude"), [])

    def test_ready_transition_then_opens_routing(self):
        gated = self.registry.register("claude", ready_gate=True)
        self.reload()
        self.registry.resolve_token(gated["token"])
        self.assertTrue(self.registry.mark_ready("claude"))
        self.assertTrue(self.trigger.is_available("claude"))

    def test_reloaded_active_record_reactivates_active(self):
        live = self.registry.register("claude")
        self.reload()
        self.assertEqual(self.registry.resolve_token(live["token"])["state"], "active")


# --- Group 13 (server side): restart order and capability --------------------------------------

class RestartOrderTests(_Room):
    def test_fresh_registration_before_the_survivors_heartbeat_wins(self):
        survivor = self.registry.register("claude")
        self.reload()
        fresh = self.registry.register("claude")
        self.assertEqual(fresh["name"], "claude")
        self.assertIsNone(self.registry.resolve_token(survivor["token"]))

    def test_survivor_heartbeat_first_keeps_its_name_and_the_fresh_one_numbers(self):
        survivor = self.registry.register("claude")
        self.reload()
        self.assertEqual(self.heartbeat(survivor["token"])["name"], "claude")
        fresh = self.registry.register("claude")
        self.assertEqual(fresh["name"], "claude-2")
        self.assertEqual(self.registry.resolve_token(survivor["token"])["name"], "claude")

    def test_capability_endpoint_reports_the_identity_contract(self):
        body = asyncio.run(app.register_capabilities())
        self.assertEqual(body["identity_contract"], 2)

    def test_unnamed_registration_after_a_deliberate_exit_takes_a_new_name(self):
        """Pins section 5: an OLD wrapper's 409 recovery is an unnamed registration, and on the
        new server it still lands on a different name (the reservation protects the old one)."""
        first = self.registry.register("claude")
        self.registry.deregister(first["name"])
        recovered = self.registry.register("claude")
        self.assertNotEqual(recovered["name"], "claude")


# --- Group 14: aliases and coordinates -------------------------------------------------------------

class AliasAndCoordinateTests(_Room):
    def test_alias_returns_by_name_with_peer_tokens_unchanged(self):
        first = self.registry.register("claude")
        second = self.registry.register("claude")
        self.assertIsInstance(self.registry.rename("claude", "claude-music"), dict)
        self.registry.deregister("claude-music")
        back = self.registry.register("claude", name="claude-music")
        self.assertEqual(back["name"], "claude-music")
        self.assertEqual(back["slot"], 1)
        self.assertEqual(self.registry.resolve_token(second["token"])["name"], "claude-2")
        self.assertIsNone(self.registry.resolve_token(first["token"]))

    def test_base_name_is_refused_while_an_alias_holds_slot1(self):
        self.registry.register("claude")
        self.registry.rename("claude", "claude-music")
        with self.assertRaises(registry_mod.NameUnavailable) as caught:
            self.registry.register("claude", name="claude")
        self.assertEqual(caught.exception.code, "slot_in_use")
        self.assertIsNotNone(self.registry.get_instance("claude-music"))

    def test_another_familys_name_is_refused(self):
        with self.assertRaises(registry_mod.NameUnavailable) as caught:
            self.registry.register("claude", name="codex-2")
        self.assertEqual(caught.exception.code, "family_conflict")
        self.assertEqual(self.registry.get_all_names(), [])

    def test_malformed_name_is_refused(self):
        for bad in ("Claude Music!", "-claude", "claude-", "claude music", "claude_music",
                    "claude/../x", "", 7):
            with self.subTest(name=bad):
                with self.assertRaises(registry_mod.NameUnavailable) as caught:
                    self.registry.register("claude", name=bad)
                self.assertEqual(caught.exception.code, "invalid_name")
        self.assertEqual(self.registry.get_all_names(), [])

    def test_a_long_human_alias_can_return(self):
        """Codex review F4: the rename path has no length bound, so neither may a return."""
        long_alias = "claude-" + "x" * 58  # 65 characters
        self.registry.register("claude")
        self.assertIsInstance(self.registry.rename("claude", long_alias), dict)
        self.registry.deregister(long_alias)
        back = self.registry.register("claude", name=long_alias)
        self.assertEqual(back["name"], long_alias)

    def test_names_the_label_sanitiser_produces_are_accepted(self):
        """Every name the human rename endpoint can produce is one a returning wrapper may ask for."""
        for label in ("Claude Music", "Claude 2nd Run", "-Claude Edge-", "Claude " + "Long " * 30):
            with self.subTest(label=label):
                reg = self.registry.register("claude")
                resp = asyncio.run(app.rename_agent_label(reg["name"], FakeRequest(body={"label": label})))
                alias = json.loads(resp.body)["new_name"]
                self.registry.deregister(alias)
                back = self.registry.register("claude", name=alias)
                self.assertEqual(back["name"], alias)
                self.registry.deregister(alias)

    def test_register_endpoint_answers_slot_in_use(self):
        self.registry.register("claude")
        self.registry.rename("claude", "claude-music")
        resp = asyncio.run(app.register_agent(FakeRequest(body={"base": "claude", "name": "claude"})))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(json.loads(resp.body)["error"], "slot_in_use")

    def test_named_grant_supersedes_a_reclaimable_on_that_coordinate(self):
        old = self.registry.register("claude")
        self.registry.deregister("claude", reclaimable=True)
        back = self.registry.register("claude", name="claude")
        self.assertEqual(back["name"], "claude")
        self.assertIsNone(self.registry.resolve_token(old["token"]))


if __name__ == "__main__":
    unittest.main()
