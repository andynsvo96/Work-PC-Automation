import io
import time
import unittest

from PIL import Image

from clipboard_runtime import (
    ClipboardError,
    ClipboardItem,
    ClipboardPeerClient,
    ClipboardRuntime,
    PeerRequestAuthenticator,
)


class FakeAdapter:
    available = True

    def __init__(self, item=None):
        self.item = item
        self.token = 1
        self.writes = []

    def change_token(self):
        return self.token

    def read(self):
        return self.item

    def write(self, item):
        self.item = item
        self.writes.append(item)
        self.token += 1


class FakePeerClient:
    configured = True

    def __init__(self):
        self.enabled = True
        self.sent = []
        self.read_item = ClipboardItem.text("from peer")

    def status(self):
        return {"success": True, "enabled": self.enabled}

    def send(self, item, *, automatic):
        self.sent.append((item, automatic))
        return {"success": True}

    def read(self):
        return self.read_item


def tiny_png():
    output = io.BytesIO()
    Image.new("RGBA", (2, 2), (10, 20, 30, 128)).save(output, format="PNG")
    return output.getvalue()


class ClipboardItemTests(unittest.TestCase):
    def test_text_round_trip(self):
        item = ClipboardItem.text("hello \N{EARTH GLOBE AMERICAS}")
        restored = ClipboardItem.from_payload(item.to_payload())
        self.assertEqual(restored, item)

    def test_png_round_trip(self):
        item = ClipboardItem.png(tiny_png())
        restored = ClipboardItem.from_payload(item.to_payload())
        self.assertEqual(restored.digest, item.digest)
        self.assertEqual(restored.data, item.data)

    def test_hash_mismatch_is_rejected(self):
        payload = ClipboardItem.text("safe").to_payload()
        payload["text"] = "changed"
        with self.assertRaises(ClipboardError):
            ClipboardItem.from_payload(payload)

    def test_non_png_image_data_is_rejected(self):
        output = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(output, format="JPEG")
        payload = ClipboardItem.png(output.getvalue()).to_payload()
        with self.assertRaises(ClipboardError):
            ClipboardItem.from_payload(payload)


class PeerRequestAuthenticatorTests(unittest.TestCase):
    def setUp(self):
        self.auth = PeerRequestAuthenticator(lambda: "s" * 48)
        self.path = "/api/clipboard/peer/receive"
        self.body = b'{"hello":"world"}'

    def test_valid_request_and_replay_rejection(self):
        headers = self.auth.headers("POST", self.path, self.body, now=1000, nonce="n" * 20)
        self.assertTrue(self.auth.verify("POST", self.path, self.body, headers, now=1000))
        with self.assertRaises(ClipboardError):
            self.auth.verify("POST", self.path, self.body, headers, now=1000)

    def test_tampered_body_is_rejected(self):
        headers = self.auth.headers("POST", self.path, self.body, now=1000, nonce="a" * 20)
        with self.assertRaises(ClipboardError):
            self.auth.verify("POST", self.path, b"tampered", headers, now=1000)

    def test_expired_request_is_rejected(self):
        headers = self.auth.headers("POST", self.path, self.body, now=1000, nonce="b" * 20)
        with self.assertRaises(ClipboardError):
            self.auth.verify("POST", self.path, self.body, headers, now=1200)


class ClipboardPeerClientTests(unittest.TestCase):
    def test_peer_url_must_be_private_tailscale_https(self):
        auth = PeerRequestAuthenticator(lambda: "s" * 48)
        client = ClipboardPeerClient("https://example.com:8443", auth)
        self.assertFalse(client.configured)
        with self.assertRaises(ClipboardError):
            client.status()

    def test_device_specific_tailscale_url_is_accepted(self):
        auth = PeerRequestAuthenticator(lambda: "s" * 48)
        client = ClipboardPeerClient("https://macbook.example-tailnet.ts.net:8443", auth)
        self.assertTrue(client.configured)


class ClipboardRuntimeTests(unittest.TestCase):
    def test_manual_send_and_pull_work_while_auto_sync_is_off(self):
        adapter = FakeAdapter(ClipboardItem.text("local"))
        peer = FakePeerClient()
        runtime = ClipboardRuntime(adapter, peer, enabled=False)

        sent = runtime.manual_send()
        self.assertEqual(sent.data, b"local")
        self.assertFalse(peer.sent[0][1])

        received = runtime.manual_pull()
        self.assertEqual(received.data, b"from peer")
        self.assertEqual(adapter.writes[-1].data, b"from peer")

    def test_automatic_receive_requires_local_opt_in(self):
        runtime = ClipboardRuntime(FakeAdapter(), FakePeerClient(), enabled=False)
        with self.assertRaises(ClipboardError):
            runtime.apply_remote(ClipboardItem.text("blocked"), automatic=True)

    def test_toggle_persists_only_boolean_preference(self):
        updates = []
        runtime = ClipboardRuntime(
            FakeAdapter(),
            FakePeerClient(),
            enabled=False,
            preference_updater=updates.append,
        )
        state = runtime.set_enabled(True)
        self.assertTrue(state["enabled"])
        self.assertEqual(updates, [True])
        self.assertNotIn("item", state)
        self.assertNotIn("content", state)

    def test_sync_state_records_metadata_not_content(self):
        runtime = ClipboardRuntime(FakeAdapter(ClipboardItem.text("secret")), FakePeerClient())
        runtime.manual_send()
        state = runtime.state()
        self.assertEqual(state["last_kind"], "text")
        self.assertEqual(state["last_direction"], "sent")
        self.assertNotIn("secret", repr(state))

    def test_stale_change_token_is_not_sent_back(self):
        adapter = FakeAdapter(ClipboardItem.text("incoming"))
        adapter.token = 2
        peer = FakePeerClient()
        runtime = ClipboardRuntime(adapter, peer, enabled=True)
        runtime._send_automatic_change(expected_token=1)
        self.assertEqual(peer.sent, [])

    def test_successful_automatic_send_marks_change_as_seen(self):
        adapter = FakeAdapter(ClipboardItem.text("new local value"))
        adapter.token = 2
        peer = FakePeerClient()
        runtime = ClipboardRuntime(adapter, peer, enabled=True)
        runtime._last_seen_token = 1

        self.assertTrue(runtime._send_automatic_change(expected_token=2))
        self.assertEqual(runtime._last_seen_token, 2)
        self.assertEqual(peer.sent[0][0].data, b"new local value")
        self.assertTrue(peer.sent[0][1])


if __name__ == "__main__":
    unittest.main()
