import unittest
from unittest import mock

import platform_runtime


class PlatformRuntimeTests(unittest.TestCase):
    def test_platform_capabilities_disable_windows_only_features_on_mac(self):
        capabilities = platform_runtime.platform_capabilities("darwin")
        self.assertTrue(capabilities["crm"])
        self.assertFalse(capabilities["metrics"])
        self.assertFalse(capabilities["system_power"])
        self.assertFalse(capabilities["restart_explorer"])

    def test_worker_recommendation_is_cpu_limited(self):
        result = platform_runtime.recommend_parallel_workers(
            physical_cores=8,
            total_ram_gb=64,
            available_ram_gb=60,
        )
        self.assertEqual(result["recommended_workers"], 4)

    def test_worker_recommendation_can_reach_eight(self):
        result = platform_runtime.recommend_parallel_workers(
            physical_cores=24,
            total_ram_gb=64,
            available_ram_gb=40,
        )
        self.assertEqual(result["recommended_workers"], 8)

    def test_worker_recommendation_lowers_for_available_memory(self):
        result = platform_runtime.recommend_parallel_workers(
            physical_cores=24,
            total_ram_gb=64,
            available_ram_gb=4.6,
        )
        self.assertEqual(result["recommended_workers"], 2)

    def test_manual_override_is_preserved(self):
        snapshot = platform_runtime.PlatformSnapshot(
            node_name="test",
            os_name="windows",
            os_display_name="Windows",
            architecture="x86_64",
            physical_cores=2,
            logical_cores=4,
            total_ram_gb=8,
            available_ram_gb=4,
            capabilities=platform_runtime.platform_capabilities("windows"),
            worker_recommendation={"recommended_workers": 1},
        )
        result = platform_runtime.resolve_worker_count("manual", 7, snapshot=snapshot)
        self.assertEqual(result["effective_workers"], 7)

    @mock.patch("platform_runtime.platform.system", return_value="Darwin")
    @mock.patch("platform_runtime.platform.machine", return_value="arm64")
    def test_mac_architecture_detection(self, _machine, _system):
        self.assertEqual(platform_runtime.normalize_os_name(), "macos")
        self.assertEqual(platform_runtime.normalize_architecture(), "arm64")


if __name__ == "__main__":
    unittest.main()
