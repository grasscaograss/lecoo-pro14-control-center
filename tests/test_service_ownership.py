"""The GUI must delegate fan writes when the system service owns them."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("lecoo_gui", Path(__file__).resolve().parents[1] / "GUI.py")
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)


class OwnershipTests(unittest.TestCase):
    def run_command(self, managed):
        results = []
        thread = gui.IpcCommandThread("set_fans", {"requests": [(gui.FanIndex.CPU, gui.FanMode.Custom, 75)]})
        thread.result_signal.connect(lambda success, message: results.append(success))
        with patch.object(gui, "is_fan_service_installed", return_value=managed), \
                patch.object(gui.IpcClient, "__enter__", return_value=gui.IpcClient()), \
                patch.object(gui.IpcClient, "__exit__"), \
                patch.object(gui.IpcClient, "set_fan_mode", return_value=True) as send:
            thread.run()
        return results, send.call_count

    def test_service_owns_fan_writes(self):
        self.assertEqual(self.run_command(True), ([True], 0))

    def test_legacy_install_can_still_use_gui_control(self):
        self.assertEqual(self.run_command(False), ([True], 1))


if __name__ == "__main__":
    unittest.main()
