import unittest

from task_registry import InvalidTaskArguments, TaskRegistry, TaskRegistryError, UnknownTaskType


class TaskRegistryTests(unittest.TestCase):
    def test_register_and_execute(self):
        registry = TaskRegistry()
        registry.register("crm.validate", lambda order_id: (True, f"Validated {order_id}"))
        self.assertEqual(registry.execute("crm.validate", {"order_id": "123"}), (True, "Validated 123"))

    def test_unknown_task_is_fail_closed(self):
        with self.assertRaises(UnknownTaskType):
            TaskRegistry().execute("crm.missing", {})

    def test_argument_validation_happens_before_execution(self):
        registry = TaskRegistry()
        registry.register("crm.validate", lambda order_id: True)
        with self.assertRaises(InvalidTaskArguments):
            registry.execute("crm.validate", {"unexpected": True})

    def test_duplicate_registration_is_rejected(self):
        registry = TaskRegistry()
        registry.register("crm.validate", lambda: True)
        with self.assertRaises(TaskRegistryError):
            registry.register("crm.validate", lambda: False)


if __name__ == "__main__":
    unittest.main()
