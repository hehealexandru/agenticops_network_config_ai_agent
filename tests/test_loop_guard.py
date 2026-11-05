from loop_guard import LoopGuard


def test_same_tool_with_different_arguments_is_allowed():
    # The old limiter blocked the third call here.
    guard = LoopGuard()
    for host in ("10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"):
        assert guard.check("configure_ip_on_interface", {"host": host}) is None
        guard.record("configure_ip_on_interface", True)
    assert not guard.should_stop


def test_identical_call_is_blocked_after_two():
    guard = LoopGuard()
    args = {"device_name": "R1", "commands": ["show ip interface brief"]}
    assert guard.check("send_console_raw", args) is None
    assert guard.check("send_console_raw", args) is None
    blocked = guard.check("send_console_raw", args)
    assert blocked and "not a rate limit" in blocked
    assert not guard.should_stop


def test_argument_order_does_not_matter():
    guard = LoopGuard(max_identical=1)
    assert guard.check("t", {"a": 1, "b": 2}) is None
    assert guard.check("t", {"b": 2, "a": 1}) is not None


def test_ignoring_the_block_stops_the_turn():
    guard = LoopGuard()
    args = {"host": "10.0.0.1"}
    for _ in range(3):
        guard.check("send_show_command", args)
    assert not guard.should_stop
    guard.check("send_show_command", args)
    assert guard.should_stop
    assert "identical arguments" in guard.stop_reason


def test_consecutive_errors_stop_the_turn():
    guard = LoopGuard()
    for i in range(3):
        guard.check("send_console_raw", {"commands": [str(i)]})
        guard.record("send_console_raw", False)
    assert guard.should_stop
    assert "failed 3 times" in guard.stop_reason


def test_a_success_resets_the_error_count():
    guard = LoopGuard()
    for ok in (False, False, True, False, False):
        guard.record("send_show_command", ok)
    assert not guard.should_stop


def test_errors_from_different_tools_do_not_add_up():
    guard = LoopGuard()
    for name in ("a", "b", "a", "b"):
        guard.record(name, False)
    assert not guard.should_stop