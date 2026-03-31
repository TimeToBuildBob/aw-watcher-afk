"""Tests for listener health checking and auto-recovery."""

from unittest.mock import MagicMock, patch

from aw_watcher_afk.listeners import GamepadListener, KeyboardListener, MouseListener


def test_keyboard_listener_is_alive_before_start():
    listener = KeyboardListener()
    assert not listener.is_alive()


def test_mouse_listener_is_alive_before_start():
    listener = MouseListener()
    assert not listener.is_alive()


def test_keyboard_listener_is_alive_with_mock():
    listener = KeyboardListener()
    # Simulate a started listener
    mock_pynput = MagicMock()
    mock_pynput.is_alive.return_value = True
    listener._listener = mock_pynput
    assert listener.is_alive()


def test_keyboard_listener_dead_after_x_restart():
    listener = KeyboardListener()
    # Simulate a dead listener (X server died)
    mock_pynput = MagicMock()
    mock_pynput.is_alive.return_value = False
    listener._listener = mock_pynput
    assert not listener.is_alive()


def test_mouse_listener_is_alive_with_mock():
    listener = MouseListener()
    mock_pynput = MagicMock()
    mock_pynput.is_alive.return_value = True
    listener._listener = mock_pynput
    assert listener.is_alive()


def test_mouse_listener_dead_after_x_restart():
    listener = MouseListener()
    mock_pynput = MagicMock()
    mock_pynput.is_alive.return_value = False
    listener._listener = mock_pynput
    assert not listener.is_alive()


@patch("aw_watcher_afk.unix.KeyboardListener")
@patch("aw_watcher_afk.unix.MouseListener")
@patch("aw_watcher_afk.unix.GamepadListener")
def test_unix_reinitializes_dead_listeners(MockGamepad, MockMouse, MockKeyboard):
    """When listeners die (e.g. X server restart), they should be restarted."""
    from aw_watcher_afk.unix import LastInputUnix

    # First call: listeners are alive
    mock_kb_instance = MockKeyboard.return_value
    mock_mouse_instance = MockMouse.return_value
    mock_gamepad_instance = MockGamepad.return_value
    mock_kb_instance.is_alive.return_value = True
    mock_mouse_instance.is_alive.return_value = True
    mock_gamepad_instance.is_alive.return_value = False  # no gamepad connected
    mock_kb_instance.has_new_event.return_value = False
    mock_mouse_instance.has_new_event.return_value = False
    mock_gamepad_instance.has_new_event.return_value = False

    unix = LastInputUnix()
    unix.seconds_since_last_input()

    # Listeners should NOT have been restarted
    assert MockKeyboard.call_count == 1
    assert MockMouse.call_count == 1

    # Now simulate X server death
    mock_kb_instance.is_alive.return_value = False

    unix.seconds_since_last_input()

    # Listeners should have been restarted (new instances created)
    assert MockKeyboard.call_count == 2
    assert MockMouse.call_count == 2
    assert MockGamepad.call_count == 2


# ---------------------------------------------------------------------------
# GamepadListener tests
# ---------------------------------------------------------------------------


def test_gamepad_listener_not_alive_before_start():
    listener = GamepadListener()
    assert not listener.is_alive()


def test_gamepad_listener_not_alive_without_evdev(monkeypatch):
    """GamepadListener.start() should be a no-op when evdev is not installed."""
    import builtins

    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "evdev":
            raise ImportError("No module named 'evdev'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)

    listener = GamepadListener()
    listener.start()
    assert not listener.is_alive()


def test_gamepad_listener_not_alive_without_devices():
    """GamepadListener.start() should be a no-op when no gamepads are found."""
    listener = GamepadListener()
    with patch.object(listener, "_find_gamepads", return_value=[]):
        listener.start()
    assert not listener.is_alive()


def test_gamepad_listener_starts_with_device():
    """GamepadListener.start() should launch a reader thread per device."""
    import sys
    from unittest.mock import MagicMock

    listener = GamepadListener()

    mock_device = MagicMock()
    mock_device.path = "/dev/input/event5"
    mock_device.name = "Xbox Controller"

    # evdev is an optional dependency; mock the import so the test runs without it
    mock_evdev = MagicMock()
    with (
        patch.dict(sys.modules, {"evdev": mock_evdev}),
        patch.object(listener, "_find_gamepads", return_value=[mock_device]),
        patch.object(listener, "_read_events"),
    ):
        listener.start()

    assert len(listener._threads) == 1
    listener.stop()


def test_gamepad_listener_detects_button_press():
    """Simulated button press should set has_new_event() and count the press."""
    listener = GamepadListener()
    assert not listener.has_new_event()

    # Simulate what _read_events does on BTN_SOUTH press
    listener.event_data["buttons"] += 1
    listener.new_event.set()

    assert listener.has_new_event()
    data = listener.next_event()
    assert data["buttons"] == 1
    # After next_event the counter is reset
    assert not listener.has_new_event()


@patch("aw_watcher_afk.unix.KeyboardListener")
@patch("aw_watcher_afk.unix.MouseListener")
@patch("aw_watcher_afk.unix.GamepadListener")
def test_unix_gamepad_event_resets_afk_timer(MockGamepad, MockMouse, MockKeyboard):
    """A gamepad button press should reset the AFK timer."""
    from aw_watcher_afk.unix import LastInputUnix

    mock_kb_instance = MockKeyboard.return_value
    mock_mouse_instance = MockMouse.return_value
    mock_gamepad_instance = MockGamepad.return_value

    mock_kb_instance.is_alive.return_value = True
    mock_mouse_instance.is_alive.return_value = True
    mock_gamepad_instance.is_alive.return_value = True

    # No keyboard/mouse events, but gamepad has an event
    mock_kb_instance.has_new_event.return_value = False
    mock_mouse_instance.has_new_event.return_value = False
    mock_gamepad_instance.has_new_event.return_value = True

    unix = LastInputUnix()
    seconds = unix.seconds_since_last_input()

    # Timer was just reset by the gamepad event → nearly 0 seconds since last input
    assert seconds < 1.0
    # Event was consumed
    mock_gamepad_instance.next_event.assert_called_once()
