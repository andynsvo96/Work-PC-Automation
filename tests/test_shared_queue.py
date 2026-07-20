import json
import unittest
from unittest import mock

from shared_queue import (
    SharedQueueBlocked,
    SupabaseQueueClient,
    TaskPayloadCipher,
    make_test_config,
    normalize_node_key,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class SharedQueueTests(unittest.TestCase):
    def test_cipher_round_trip(self):
        cipher = TaskPayloadCipher(TaskPayloadCipher.generate_key())
        payload = {"order_id": "123", "dry_run": False}
        encrypted = cipher.encrypt(payload)
        self.assertNotEqual(encrypted, json.dumps(payload, sort_keys=True))
        self.assertEqual(cipher.decrypt(encrypted), {"dry_run": False, "order_id": "123"})

    def test_cipher_rejects_wrong_key(self):
        encrypted = TaskPayloadCipher(TaskPayloadCipher.generate_key()).encrypt({"safe": True})
        with self.assertRaises(SharedQueueBlocked):
            TaskPayloadCipher(TaskPayloadCipher.generate_key()).decrypt(encrypted)

    def test_normalize_node_key(self):
        self.assertEqual(normalize_node_key("Andy's MacBook Pro"), "andy-s-macbook-pro")

    def test_enqueue_sends_encrypted_payload_and_node_identity(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _Response({"id": "task-1", "sequence": 1, "status": "queued"})

        config = make_test_config(node_key="windows-pc")
        client = SupabaseQueueClient(config, opener=opener)
        result = client.enqueue(
            label="Validate Address",
            category="Processing",
            task_type="crm.validate_address",
            arguments={"order_id": "123"},
            commit="abc123",
        )

        self.assertEqual(result["id"], "task-1")
        self.assertTrue(captured["url"].endswith("/rest/v1/rpc/automation_enqueue_task"))
        self.assertEqual(captured["body"]["p_requested_by_node"], "windows-pc")
        self.assertEqual(captured["body"]["p_task_type"], "crm.validate_address")
        self.assertNotIn("123", captured["body"]["p_encrypted_payload"])
        self.assertEqual(
            client.cipher.decrypt(captured["body"]["p_encrypted_payload"]),
            {"order_id": "123"},
        )

    def test_clear_finished_uses_scoped_rpc(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Response(4)

        config = make_test_config(node_key="windows-pc")
        result = SupabaseQueueClient(config, opener=opener).clear_finished()

        self.assertEqual(result, 4)
        self.assertTrue(captured["url"].endswith("/rest/v1/rpc/automation_clear_finished_tasks"))
        self.assertEqual(captured["body"], {"p_workspace_id": config.workspace_id})

    def test_get_version_gate_reads_workspace_control(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            return _Response([{"required_commit": "abc123", "required_protocol_version": 1, "paused": False}])

        config = make_test_config(node_key="windows-pc")
        result = SupabaseQueueClient(config, opener=opener).get_version_gate()

        self.assertEqual(result["required_commit"], "abc123")
        self.assertIn("automation_queue_control?select=", captured["url"])
        self.assertIn(config.workspace_id, captured["url"])

    def test_claim_decrypts_arguments(self):
        config = make_test_config(node_key="macbook")
        cipher = TaskPayloadCipher(config.encryption_key)

        def opener(_request, timeout):
            self.assertEqual(timeout, config.request_timeout_seconds)
            return _Response(
                {
                    "id": "task-2",
                    "task_type": "crm.unlock",
                    "encrypted_payload": cipher.encrypt({"dry_run": True}),
                    "lease_token": "lease",
                }
            )

        task = SupabaseQueueClient(config, opener=opener).claim_next(commit="abc123")
        self.assertEqual(task["arguments"], {"dry_run": True})
        self.assertNotIn("encrypted_payload", task)

    def test_configuration_requires_all_secrets(self):
        from shared_queue import SharedQueueConfig

        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SharedQueueBlocked):
                SharedQueueConfig.from_env()

    def test_password_authentication_gets_fresh_access_token(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if "/auth/v1/token" in request.full_url:
                return _Response({"access_token": "fresh-token", "expires_in": 3600})
            self.assertEqual(request.get_header("Authorization"), "Bearer fresh-token")
            return _Response({"eligible": True})

        config = make_test_config(access_token="", user_email="node@example.com", user_password="secret")
        client = SupabaseQueueClient(config, opener=opener)
        response = client.heartbeat(commit="abc", capabilities={"crm": True})
        self.assertTrue(response["eligible"])
        self.assertEqual(len([url for url in calls if "/auth/v1/token" in url]), 1)


if __name__ == "__main__":
    unittest.main()
