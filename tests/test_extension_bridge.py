import json
import math
from threading import Event, Thread
import unittest

from trade_snapshot.extension_bridge import (
    PROTOCOL_VERSION,
    V1_CAPABILITIES,
    BridgeAuthenticationError,
    BridgeBusyError,
    BridgeCancelledError,
    BridgeClosedError,
    BridgeCommandError,
    BridgeDisconnectedError,
    BridgePayloadError,
    BridgeProtocolError,
    BridgeStaleCommandError,
    BridgeStateError,
    BridgeTimeoutError,
    ExtensionCommandBridge,
    is_valid_loopback_host,
)


class _Secrets:
    def __init__(self):
        self._counter = 0

    def __call__(self):
        self._counter += 1
        return f"test-secret-{self._counter:02d}-" + ("x" * 32)


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class _Call:
    def __init__(self, bridge, op, payload, *, timeout=2.0, cancelled=None):
        self.result = None
        self.error = None
        self._thread = Thread(
            target=self._run,
            args=(bridge, op, payload, timeout, cancelled),
            daemon=True,
        )
        self._thread.start()

    def _run(self, bridge, op, payload, timeout, cancelled):
        try:
            self.result = bridge.execute(op, payload, timeout, cancelled)
        except Exception as error:
            self.error = error

    def join(self):
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise AssertionError("bridge call did not finish")


class ExtensionBridgePairingTests(unittest.TestCase):
    def setUp(self):
        self.secrets = _Secrets()
        self.bridge = ExtensionCommandBridge(secret_factory=self.secrets)

    def tearDown(self):
        self.bridge.close()

    def connect(self):
        offer = self.bridge.create_pairing()
        return self.bridge.connect(
            offer["pair_code"],
            protocol_version=PROTOCOL_VERSION,
            capabilities=V1_CAPABILITIES,
            extension_version="1.2.3",
        )

    def test_pairing_is_explicit_one_use_and_uses_a_separate_session_secret(self):
        self.assertEqual(
            self.bridge.public_status(),
            {
                "protocol_version": 1,
                "state": "unpaired",
                "capabilities": list(V1_CAPABILITIES),
                "command_busy": False,
            },
        )
        offer = self.bridge.create_pairing()
        self.assertEqual(self.bridge.state, "pairing")
        self.assertEqual(offer["protocol_version"], 1)
        self.assertEqual(tuple(offer["capabilities"]), V1_CAPABILITIES)

        connection = self.bridge.connect(
            offer["pair_code"],
            protocol_version=1,
            capabilities=V1_CAPABILITIES,
            extension_version="1.2.3",
        )
        self.assertEqual(self.bridge.state, "paired")
        self.assertNotEqual(connection["session_token"], offer["pair_code"])
        self.assertEqual(connection["state"], "paired")
        self.assertNotIn("pair_code", connection)

        with self.assertRaises(BridgeStateError):
            self.bridge.connect(
                offer["pair_code"],
                protocol_version=1,
                capabilities=V1_CAPABILITIES,
                extension_version="1.2.3",
            )

        status = self.bridge.status(connection["session_token"])
        self.assertEqual(status["state"], "paired")
        self.assertEqual(status["extension_version"], "1.2.3")
        self.assertIsNone(status["command"])
        public = self.bridge.public_status()
        self.assertEqual(public["state"], "paired")
        self.assertEqual(public["extension_version"], "1.2.3")
        serialized = json.dumps(public)
        self.assertNotIn(offer["pair_code"], serialized)
        self.assertNotIn(connection["session_token"], serialized)

    def test_protocol_capabilities_and_extension_version_are_strict(self):
        self.assertEqual(
            V1_CAPABILITIES,
            (
                "session.open",
                "session.navigate",
                "analyzer.begin",
                "analyzer.finish",
                "analyzer.abort",
                "analyzer.bundle",
                "analyzer.activate_full",
                "page.provenance",
                "projection.capture",
                "ecr.capture",
                "league.capture",
                "espn.authenticated_json",
                "yahoo.scoring",
                "session.wait",
                "session.close",
            ),
        )
        offer = self.bridge.create_pairing()
        with self.assertRaises(BridgeProtocolError):
            self.bridge.connect(
                offer["pair_code"],
                protocol_version=2,
                capabilities=V1_CAPABILITIES,
                extension_version="1.0.0",
            )
        with self.assertRaises(BridgeProtocolError):
            self.bridge.connect(
                offer["pair_code"],
                protocol_version=1,
                capabilities=V1_CAPABILITIES[:-1],
                extension_version="1.0.0",
            )
        with self.assertRaises(BridgeProtocolError):
            self.bridge.connect(
                offer["pair_code"],
                protocol_version=1,
                capabilities=(*V1_CAPABILITIES, V1_CAPABILITIES[0]),
                extension_version="1.0.0",
            )
        with self.assertRaises(BridgeProtocolError):
            self.bridge.connect(
                offer["pair_code"],
                protocol_version=1,
                capabilities=V1_CAPABILITIES,
                extension_version="bad version with spaces",
            )

    def test_pair_code_expires_and_can_be_rotated(self):
        clock = _Clock()
        bridge = ExtensionCommandBridge(
            pair_ttl_seconds=10,
            clock=clock,
            secret_factory=_Secrets(),
        )
        self.addCleanup(bridge.close)
        first = bridge.create_pairing()
        second = bridge.create_pairing()
        self.assertNotEqual(first["pair_code"], second["pair_code"])
        with self.assertRaises(BridgeAuthenticationError):
            bridge.connect(
                first["pair_code"], 1, V1_CAPABILITIES, "1.0.0"
            )
        clock.value += 11
        with self.assertRaises(BridgeAuthenticationError):
            bridge.connect(
                second["pair_code"], 1, V1_CAPABILITIES, "1.0.0"
            )
        self.assertEqual(bridge.state, "unpaired")

    def test_disconnect_revokes_the_session_token(self):
        connection = self.connect()
        token = connection["session_token"]
        response = self.bridge.disconnect(token)
        self.assertEqual(response, {"protocol_version": 1, "state": "unpaired"})
        self.assertEqual(self.bridge.state, "unpaired")
        with self.assertRaises(BridgeAuthenticationError):
            self.bridge.status(token)

    def test_app_authorized_pairing_can_replace_a_lost_extension(self):
        token = self.connect()["session_token"]
        pending = _Call(self.bridge, "session.open", {})
        self.bridge.poll(token, wait_seconds=1)

        replacement = self.bridge.create_pairing()
        pending.join()
        self.assertIsInstance(pending.error, BridgeDisconnectedError)
        self.assertEqual(self.bridge.state, "pairing")
        with self.assertRaises(BridgeAuthenticationError):
            self.bridge.status(token)

        connected = self.bridge.connect(
            replacement["pair_code"], 1, V1_CAPABILITIES, "1.2.4"
        )
        self.assertEqual(self.bridge.status(connected["session_token"])["state"], "paired")


class ExtensionBridgeCommandTests(unittest.TestCase):
    def setUp(self):
        self.bridge = ExtensionCommandBridge(secret_factory=_Secrets())
        offer = self.bridge.create_pairing()
        connection = self.bridge.connect(
            offer["pair_code"], 1, V1_CAPABILITIES, "1.2.3"
        )
        self.token = connection["session_token"]

    def tearDown(self):
        self.bridge.close()

    def test_one_command_is_claimed_once_and_the_session_is_reused(self):
        first = _Call(self.bridge, "session.open", {"headed": True})
        command = self.bridge.poll(self.token, wait_seconds=1)
        self.assertTrue(self.bridge.public_status()["command_busy"])
        self.assertEqual(command["state"], "command")
        self.assertEqual(command["op"], "session.open")
        self.assertEqual(command["payload"], {"headed": True})
        self.assertEqual(
            self.bridge.status(self.token)["command"],
            {
                "command_id": command["command_id"],
                "op": "session.open",
                "state": "claimed",
            },
        )
        self.assertEqual(
            self.bridge.poll(self.token, wait_seconds=0),
            {"protocol_version": 1, "state": "idle"},
        )
        self.bridge.complete(
            self.token, command["command_id"], result={"opened": True}
        )
        first.join()
        self.assertIsNone(first.error)
        self.assertEqual(first.result, {"opened": True})
        self.assertFalse(self.bridge.public_status()["command_busy"])

        second = _Call(self.bridge, "session.wait", {"timeout_ms": 25})
        next_command = self.bridge.poll(self.token, wait_seconds=1)
        self.assertNotEqual(next_command["command_id"], command["command_id"])
        self.bridge.complete(self.token, next_command["command_id"], result=None)
        second.join()
        self.assertIsNone(second.error)
        self.assertIsNone(second.result)

    def test_identical_normalized_result_replays_a_lost_acknowledgement(self):
        call = _Call(self.bridge, "projection.capture", {})
        command = self.bridge.poll(self.token, wait_seconds=1)
        accepted = self.bridge.complete(
            self.token,
            command["command_id"],
            result={"nested": {"second": 2, "first": 1}, "rows": (1, 2)},
        )
        call.join()

        replay = self.bridge.complete(
            self.token,
            command["command_id"],
            result={"rows": [1, 2], "nested": {"first": 1, "second": 2}},
        )

        self.assertEqual(replay, accepted)
        self.assertEqual(
            call.result,
            {"nested": {"second": 2, "first": 1}, "rows": [1, 2]},
        )

    def test_replay_rejects_changed_completion_and_keeps_only_latest_receipt(self):
        first = _Call(self.bridge, "page.provenance", {})
        first_command = self.bridge.poll(self.token, wait_seconds=1)
        first_accepted = self.bridge.complete(
            self.token, first_command["command_id"], result={"ok": True}
        )
        first.join()

        with self.assertRaises(BridgeStaleCommandError):
            self.bridge.complete(
                self.token, first_command["command_id"], result={"ok": False}
            )
        with self.assertRaises(BridgeStaleCommandError):
            self.bridge.complete(
                self.token, first_command["command_id"], error="same identifier"
            )

        second = _Call(self.bridge, "session.wait", {})
        second_command = self.bridge.poll(self.token, wait_seconds=1)
        self.assertEqual(
            self.bridge.complete(
                self.token, first_command["command_id"], result={"ok": True}
            ),
            first_accepted,
        )
        second_accepted = self.bridge.complete(
            self.token, second_command["command_id"], result=None
        )
        second.join()

        with self.assertRaises(BridgeStaleCommandError):
            self.bridge.complete(
                self.token, first_command["command_id"], result={"ok": True}
            )
        self.assertEqual(
            self.bridge.complete(
                self.token, second_command["command_id"], result=None
            ),
            second_accepted,
        )

    def test_error_replay_is_session_scoped_and_cleared_by_repairing(self):
        call = _Call(self.bridge, "league.capture", {})
        command = self.bridge.poll(self.token, wait_seconds=1)
        accepted = self.bridge.complete(
            self.token, command["command_id"], error="capture failed"
        )
        call.join()
        self.assertIsInstance(call.error, BridgeCommandError)
        self.assertEqual(
            self.bridge.complete(
                self.token, command["command_id"], error="capture failed"
            ),
            accepted,
        )
        with self.assertRaises(BridgeStaleCommandError):
            self.bridge.complete(
                self.token, command["command_id"], result="capture failed"
            )

        self.bridge.disconnect(self.token)
        with self.assertRaises(BridgeAuthenticationError):
            self.bridge.complete(
                self.token, command["command_id"], error="capture failed"
            )

        offer = self.bridge.create_pairing()
        replacement_token = self.bridge.connect(
            offer["pair_code"], 1, V1_CAPABILITIES, "1.2.4"
        )["session_token"]
        with self.assertRaises(BridgeStaleCommandError):
            self.bridge.complete(
                replacement_token,
                command["command_id"],
                error="capture failed",
            )

    def test_allowlist_busy_and_stale_completion_are_enforced(self):
        with self.assertRaises(BridgeProtocolError):
            self.bridge.execute("arbitrary.javascript", {}, 1, None)

        active = _Call(self.bridge, "analyzer.begin", {"phase": "full"})
        command = self.bridge.poll(self.token, wait_seconds=1)
        with self.assertRaises(BridgeBusyError):
            self.bridge.execute("analyzer.abort", {}, 1, None)
        with self.assertRaises(BridgeStaleCommandError):
            self.bridge.complete(self.token, "0" * 32, result=None)
        with self.assertRaises(BridgeAuthenticationError):
            self.bridge.complete("wrong-token", command["command_id"], result=None)
        self.bridge.complete(self.token, command["command_id"], result=None)
        active.join()

    def test_extension_error_is_sanitized_and_raised_to_the_producer(self):
        call = _Call(self.bridge, "page.provenance", {"url": "https://example.test"})
        command = self.bridge.poll(self.token, wait_seconds=1)
        self.bridge.complete(
            self.token,
            command["command_id"],
            error="page provenance did not match",
        )
        call.join()
        self.assertIsInstance(call.error, BridgeCommandError)
        self.assertEqual(str(call.error), "page provenance did not match")

    def test_payload_and_result_sizes_and_json_values_are_bounded(self):
        bridge = ExtensionCommandBridge(
            secret_factory=_Secrets(),
            maximum_command_bytes=64,
            maximum_result_bytes=32,
        )
        self.addCleanup(bridge.close)
        offer = bridge.create_pairing()
        token = bridge.connect(
            offer["pair_code"], 1, V1_CAPABILITIES, "1.0.0"
        )["session_token"]
        with self.assertRaises(BridgePayloadError):
            bridge.execute("session.navigate", {"url": "x" * 128}, 1, None)
        with self.assertRaises(BridgePayloadError):
            bridge.execute("session.wait", {"value": math.nan}, 1, None)

        call = _Call(bridge, "analyzer.finish", {})
        command = bridge.poll(token, wait_seconds=1)
        with self.assertRaises(BridgePayloadError):
            bridge.complete(token, command["command_id"], result={"x": "y" * 64})
        bridge.complete(token, command["command_id"], result={"ok": True})
        call.join()
        self.assertEqual(call.result, {"ok": True})

    def test_timeout_cancellation_disconnect_and_close_reject_late_results(self):
        timed_out = _Call(
            self.bridge, "projection.capture", {}, timeout=0.05
        )
        command = self.bridge.poll(self.token, wait_seconds=1)
        timed_out.join()
        self.assertIsInstance(timed_out.error, BridgeTimeoutError)
        with self.assertRaises(BridgeStaleCommandError):
            self.bridge.complete(self.token, command["command_id"], result=None)

        cancellation = Event()
        cancelled = _Call(
            self.bridge,
            "ecr.capture",
            {},
            timeout=1,
            cancelled=cancellation.is_set,
        )
        command = self.bridge.poll(self.token, wait_seconds=1)
        cancellation.set()
        cancelled.join()
        self.assertIsInstance(cancelled.error, BridgeCancelledError)
        with self.assertRaises(BridgeStaleCommandError):
            self.bridge.complete(self.token, command["command_id"], result=None)

        disconnected = _Call(self.bridge, "league.capture", {})
        self.bridge.poll(self.token, wait_seconds=1)
        self.bridge.disconnect(self.token)
        disconnected.join()
        self.assertIsInstance(disconnected.error, BridgeDisconnectedError)

        replacement = self.bridge.create_pairing()
        token = self.bridge.connect(
            replacement["pair_code"], 1, V1_CAPABILITIES, "1.0.1"
        )["session_token"]
        closed = _Call(self.bridge, "session.close", {})
        self.bridge.poll(token, wait_seconds=1)
        self.bridge.close()
        closed.join()
        self.assertIsInstance(closed.error, BridgeClosedError)

    def test_completion_after_deadline_or_cancellation_cannot_win_the_race(self):
        clock = _Clock()
        bridge = ExtensionCommandBridge(clock=clock, secret_factory=_Secrets())
        self.addCleanup(bridge.close)
        offer = bridge.create_pairing()
        token = bridge.connect(
            offer["pair_code"], 1, V1_CAPABILITIES, "1.0.0"
        )["session_token"]

        late = _Call(bridge, "projection.capture", {}, timeout=5)
        command = bridge.poll(token, wait_seconds=1)
        clock.value += 6
        with self.assertRaises(BridgeStaleCommandError):
            bridge.complete(token, command["command_id"], result={"late": True})
        late.join()
        self.assertIsInstance(late.error, BridgeTimeoutError)

        cancellation = Event()
        cancelled = _Call(
            bridge,
            "ecr.capture",
            {},
            timeout=5,
            cancelled=cancellation.is_set,
        )
        command = bridge.poll(token, wait_seconds=1)
        cancellation.set()
        with self.assertRaises(BridgeStaleCommandError):
            bridge.complete(token, command["command_id"], result={"late": True})
        cancelled.join()
        self.assertIsInstance(cancelled.error, BridgeCancelledError)

    def test_poll_and_command_completion_shape_are_strict(self):
        with self.assertRaises(BridgePayloadError):
            self.bridge.poll(self.token, wait_seconds=21)
        call = _Call(self.bridge, "analyzer.abort", {})
        command = self.bridge.poll(self.token, wait_seconds=1)
        with self.assertRaises(BridgePayloadError):
            self.bridge.complete(
                self.token, command["command_id"], result={}, error="failed"
            )
        with self.assertRaises(BridgePayloadError):
            self.bridge.complete(self.token, command["command_id"])
        self.bridge.complete(self.token, command["command_id"], result=None)
        call.join()


class LoopbackHostTests(unittest.TestCase):
    def test_validator_matches_the_local_application_host_policy(self):
        for host in ("127.0.0.1", "127.0.0.1:43123", "localhost", "localhost:43123"):
            self.assertTrue(is_valid_loopback_host(host, 43123), host)
        for host in (
            "",
            "attacker.example",
            "127.0.0.1:80",
            "localhost.attacker.example:43123",
            "[::1]:43123",
            "localhost:43123:80",
        ):
            self.assertFalse(is_valid_loopback_host(host, 43123), host)


if __name__ == "__main__":
    unittest.main()
