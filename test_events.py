"""events.py — host-free tests: the bus wire must be a safe no-op without a host,
a correctly-namespaced pass-through with one, and never raise into the engine."""

from __future__ import annotations

import events


class _Registry:
    """The two registry methods events.py leans on, capturing calls."""

    def __init__(self, *, boom: bool = False):
        self.boom = boom
        self.emitted: list[tuple[str, dict]] = []
        self.navigated: list[str] = []

    def emit(self, topic, data=None):
        if self.boom:
            raise RuntimeError("bus down")
        self.emitted.append((topic, data or {}))

    def navigate(self, view=""):
        if self.boom:
            raise RuntimeError("bus down")
        self.navigated.append(view)


def setup_function(_fn):
    events.bind(None)  # each test starts unbound (module state is global)


def test_unbound_is_silent_noop():
    # Host-free context (bare import, pytest, frozen probe): no bus, no error.
    assert events.emit("window_closed", {"gained": 1}) is False
    assert events.navigate("fleet") is False


def test_emit_passes_event_and_payload_through_registry():
    reg = _Registry()
    events.bind(reg)
    assert events.emit("trade_executed", {"ship": "X-1", "units": 3}) is True
    # registry.emit owns the spacetraders.* namespacing (ADR 0039) — events.py hands
    # over the BARE event name so the host rule stays the single namespacer.
    assert reg.emitted == [("trade_executed", {"ship": "X-1", "units": 3})]


def test_emit_defaults_payload_to_empty_dict():
    reg = _Registry()
    events.bind(reg)
    assert events.emit("engine_started") is True
    assert reg.emitted == [("engine_started", {})]


def test_navigate_passes_view():
    reg = _Registry()
    events.bind(reg)
    assert events.navigate("fleet") is True
    assert reg.navigated == ["fleet"]


def test_bus_failure_never_raises_into_the_engine():
    # supervise() treats an exception as an engine crash — an emission must not be
    # able to cause one (telemetry ≠ control flow).
    events.bind(_Registry(boom=True))
    assert events.emit("window_closed", {"gained": 0}) is False
    assert events.navigate("fleet") is False


def test_rebind_replaces_handle():
    a, b = _Registry(), _Registry()
    events.bind(a)
    events.emit("engine_started")
    events.bind(b)  # hot-reload path: register() runs again
    events.emit("engine_stopped")
    assert [t for t, _ in a.emitted] == ["engine_started"]
    assert [t for t, _ in b.emitted] == ["engine_stopped"]
