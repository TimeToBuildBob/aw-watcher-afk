"""Pure diagnostic tests; no Windows hooks or display required."""

import importlib.util
from pathlib import Path
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    "diagnostic", Path(__file__).parents[1] / "misc" / "windows_input_diagnostic.py"
)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


class DiagnosticTests(unittest.TestCase):
    def test_keyboard_injection_bits(self):
        self.assertEqual(diagnostic.classify("keyboard", 0), "unflagged")
        self.assertEqual(diagnostic.classify("keyboard", 0x10), "injected")
        self.assertEqual(
            diagnostic.classify("keyboard", 0x12), "lower_integrity_injected"
        )
        self.assertEqual(diagnostic.classify("keyboard", 0x80), "unflagged")

    def test_mouse_injection_bits_differ(self):
        self.assertEqual(diagnostic.classify("mouse", 1), "injected")
        self.assertEqual(diagnostic.classify("mouse", 3), "lower_integrity_injected")
        self.assertEqual(diagnostic.classify("mouse", 0x10), "unflagged")

    def test_idle_wraparound_matches_windows_backend(self):
        self.assertEqual(diagnostic.idle_ms(2**32 + 25, 2**32 - 25), 50)
        self.assertEqual(diagnostic.idle_ms(5000, 3000), 2000)

    def test_counter_snapshot_is_interval_only(self):
        counts = diagnostic.EventCounts()
        counts.observe("mouse", 1)
        counts.observe("keyboard", 0)
        first = counts.snapshot()
        self.assertEqual(first["mouse_injected"], 1)
        self.assertEqual(first["keyboard_unflagged"], 1)
        self.assertTrue(all(value == 0 for value in counts.snapshot().values()))

    def test_idle_change_is_not_causality(self):
        counts = diagnostic.EventCounts()
        row = diagnostic.sample_row(1.0, 5000, 4900, 3000, counts.snapshot())
        self.assertEqual(row["idle_ms"], 100)
        self.assertTrue(row["last_input_tick_changed"])
        self.assertTrue(all(value == 0 for value in row["events"].values()))
        self.assertIsNone(
            diagnostic.sample_row(0, 5000, 4900, None, {})["last_input_tick_changed"]
        )
        self.assertNotIn("key", row)
        self.assertNotIn("coordinates", row)


class NativeLifecycleTests(unittest.TestCase):
    """API doubles exercise control flow, not Windows ABI/hook delivery."""

    def run_native(
        self, fail_hook=0, fail_query=False, callback_failure=False, interrupt=False
    ):
        api = SimpleNamespace()
        api.SetWindowsHookExW = Mock(
            side_effect=[101, 0] if fail_hook == 2 else [101, 102]
        )
        api.UnhookWindowsHookEx = Mock(return_value=1)
        api.CallNextHookEx = Mock(return_value=99)
        api.SetTimer = Mock(return_value=77)
        api.KillTimer = Mock(return_value=1)
        api.TranslateMessage = Mock(return_value=1)
        api.DispatchMessageW = Mock(return_value=0)
        api.GetLastInputInfo = Mock(return_value=not fail_query)
        api.GetMessageW = Mock(return_value=0)
        kernel = SimpleNamespace(
            GetTickCount64=Mock(return_value=5000),
            GetModuleHandleW=Mock(return_value=1),
        )
        output = StringIO()
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    diagnostic.ctypes, "WinDLL", side_effect=[api, kernel], create=True
                )
            )
            stack.enter_context(
                patch.object(
                    diagnostic.ctypes,
                    "WINFUNCTYPE",
                    return_value=lambda fn: fn,
                    create=True,
                )
            )
            stack.enter_context(
                patch.object(
                    diagnostic.ctypes, "get_last_error", return_value=5, create=True
                )
            )
            stack.enter_context(
                patch.object(
                    diagnostic.ctypes,
                    "WinError",
                    side_effect=lambda code: OSError(code),
                    create=True,
                )
            )
            stack.enter_context(redirect_stdout(output))
            if callback_failure or interrupt:

                def deliver(msg, *args):
                    callback = api.SetWindowsHookExW.call_args_list[0].args[1]
                    # A classification exception must never suppress the event.
                    with patch.object(
                        diagnostic.ctypes,
                        "cast",
                        side_effect=KeyboardInterrupt if interrupt else ValueError,
                    ):
                        self.assertEqual(callback(0, 0, 0), 99)
                    # A negative hook code must pass straight through.
                    self.assertEqual(callback(-1, 0, 0), 99)
                    msg._obj.message = 0x0113
                    msg._obj.wParam = 77
                    return 1

                api.GetMessageW.side_effect = deliver
            error = None
            try:
                diagnostic.run(1)
            except (OSError, RuntimeError, KeyboardInterrupt) as exc:
                error = exc
        return api, output.getvalue(), error

    def test_normal_exit_removes_both_hooks_and_timer(self):
        api, output, error = self.run_native()
        self.assertIsNone(error)
        self.assertIn('"status": "hooks_installed"', output)
        self.assertEqual(
            [call.args[0] for call in api.UnhookWindowsHookEx.call_args_list],
            [102, 101],
        )
        api.KillTimer.assert_called_once_with(None, 77)

    def test_partial_hook_install_failure_removes_first_hook(self):
        api, output, error = self.run_native(fail_hook=2)
        self.assertIsInstance(error, OSError)
        self.assertEqual(output, "")
        api.UnhookWindowsHookEx.assert_called_once_with(101)
        api.KillTimer.assert_not_called()

    def test_idle_query_failure_cleans_up(self):
        api, output, error = self.run_native(fail_query=True)
        self.assertIsInstance(error, OSError)
        self.assertEqual(api.UnhookWindowsHookEx.call_count, 2)
        api.KillTimer.assert_called_once_with(None, 77)
        self.assertNotIn('"idle_ms"', output)

    def test_callback_failure_passes_input_and_fails_next_sample(self):
        api, output, error = self.run_native(callback_failure=True)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(api.CallNextHookEx.call_count, 2)
        self.assertEqual(api.UnhookWindowsHookEx.call_count, 2)
        api.KillTimer.assert_called_once_with(None, 77)

    def test_callback_interrupt_passes_input_before_cancellation(self):
        api, output, error = self.run_native(interrupt=True)
        self.assertIsInstance(error, KeyboardInterrupt)
        self.assertEqual(api.CallNextHookEx.call_count, 2)
        self.assertEqual(api.UnhookWindowsHookEx.call_count, 2)
        api.KillTimer.assert_called_once_with(None, 77)


if __name__ == "__main__":
    unittest.main()
