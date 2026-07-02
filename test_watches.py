"""watches.py pure predicates — host-free (like roles/analysis): the tripwire logic
must be right BEFORE it's wired to live verifiers, because a wrong tripwire either
cries wolf (wakes the agent for nothing) or sleeps through the incident it exists for."""

from __future__ import annotations

import watches as W


# ── reset_changed ────────────────────────────────────────────────────────────────────

def test_first_sighting_is_baseline_not_change():
    changed, epoch = W.reset_changed({"resetDate": "2026-06-27"}, None)
    assert (changed, epoch) == (False, "2026-06-27")


def test_same_epoch_is_no_change():
    assert W.reset_changed({"resetDate": "2026-06-27"}, "2026-06-27") == (False, "2026-06-27")


def test_new_epoch_trips():
    changed, epoch = W.reset_changed({"resetDate": "2026-07-04"}, "2026-06-27")
    assert (changed, epoch) == (True, "2026-07-04")


def test_missing_reset_date_never_trips():
    assert W.reset_changed({}, "2026-06-27") == (False, "")


# ── drawdown ─────────────────────────────────────────────────────────────────────────

def test_first_reading_sets_mark_without_tripping():
    tripped, hw = W.drawdown(42_000, None, 0.5)
    assert (tripped, hw) == (False, 42_000)


def test_mark_ratchets_up_only():
    _, hw = W.drawdown(172_000, 100_000, 0.5)
    assert hw == 172_000
    _, hw = W.drawdown(90_000, 172_000, 0.5)
    assert hw == 172_000  # a dip never lowers the mark


def test_crash_below_half_trips():
    # The June incident: 172k peak → 36k band. This is the watch that catches it.
    tripped, _ = W.drawdown(36_000, 172_000, 0.5)
    assert tripped is True


def test_dip_above_frac_does_not_trip():
    tripped, _ = W.drawdown(90_000, 172_000, 0.5)
    assert tripped is False


# ── deadline_close ───────────────────────────────────────────────────────────────────

_NOW = "2026-07-01T12:00:00Z"


def _contract(deadline: str, *, accepted=True, fulfilled=False, cid="c1"):
    return {"id": cid, "accepted": accepted, "fulfilled": fulfilled,
            "terms": {"deadline": deadline}}


def test_deadline_inside_margin_trips_with_detail():
    close, detail = W.deadline_close([_contract("2026-07-01T16:00:00Z")], _NOW, hours=6.0)
    assert close is True
    assert "c1" in detail and "4.0h" in detail


def test_deadline_far_out_does_not_trip():
    assert W.deadline_close([_contract("2026-07-02T12:00:00Z")], _NOW, hours=6.0)[0] is False


def test_already_past_deadline_does_not_trip():
    # Nothing to reprioritize — the contract is already lost; don't wake the agent for it.
    assert W.deadline_close([_contract("2026-07-01T11:00:00Z")], _NOW, hours=6.0)[0] is False


def test_unaccepted_and_fulfilled_are_ignored():
    cons = [_contract("2026-07-01T13:00:00Z", accepted=False),
            _contract("2026-07-01T13:00:00Z", fulfilled=True)]
    assert W.deadline_close(cons, _NOW, hours=6.0)[0] is False


# ── net worth + flatline bucketing ───────────────────────────────────────────────────

def _ship(frame):
    return {"frame": {"symbol": frame}}


def test_net_worth_counts_credits_plus_fleet_book_value():
    ships = [_ship("FRAME_FRIGATE"), _ship("FRAME_PROBE")]
    assert W.net_worth(100_000, ships) == 100_000 + 300_000 + 25_000


def test_unknown_frame_counts_conservatively():
    assert W.net_worth(0, [_ship("FRAME_FUTURE_HULL")]) == 25_000


def test_bucket_absorbs_jitter_but_registers_motion():
    # Same-bucket drift → same evidence (reads as a stall); a real trade jumps buckets →
    # new evidence (progress). A drift that happens to CROSS a bucket edge also reads as
    # motion — that can only delay flatline detection by a check, never false-trip it.
    assert W.worth_bucket(40_100) == W.worth_bucket(41_900)   # sub-step drift: still flat
    assert W.worth_bucket(40_100) != W.worth_bucket(45_000)   # real movement: not flat


# ── opportunity margin ───────────────────────────────────────────────────────────────

def test_best_margin_pct_picks_the_best_route():
    routes = [{"buy_price": 100, "profit_per_unit": 10},   # 10%
              {"buy_price": 40, "profit_per_unit": 10}]    # 25%
    assert W.best_margin_pct(routes) == 25.0


def test_best_margin_handles_empty_and_zero_buy():
    assert W.best_margin_pct([]) == 0.0
    assert W.best_margin_pct([{"buy_price": 0, "profit_per_unit": 5}]) == 0.0


# ── state + arming (host-free paths) ─────────────────────────────────────────────────

def test_state_roundtrip_and_epoch_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "_state_path", lambda: tmp_path / "state.json")
    W.save_state({"high_water": 172_000, "reset_date": "2026-06-27"})
    assert W.load_state() == {"high_water": 172_000, "reset_date": "2026-06-27"}
    W.clear_epoch_state()   # a wipe drops the mark but keeps the (new) epoch
    assert W.load_state() == {"reset_date": "2026-06-27"}


def test_arm_all_is_a_safe_noop_without_host():
    res = W.arm_all()
    assert res["ok"] is False and res["armed"] == 0


def test_flatline_spec_never_mets_and_uses_stall():
    # The flatline tripwire works through the stall detector — if someone gives it a
    # reachable min or drops stall_after, it silently becomes a different watch.
    spec = next(s for s in W.WATCH_SPECS if s["watch_id"] == "st-flatline")
    assert spec["verifier_args"]["min"] >= 10**12
    assert spec["stall_after"] == 3 and spec["run_prompt"] == ""


def test_every_armed_prompt_names_its_tripwire():
    # run_prompts land in the Activity thread with no other context — the prompt itself
    # must say which tripwire fired.
    for spec in W.WATCH_SPECS:
        if spec["run_prompt"]:
            assert spec["watch_id"] in spec["run_prompt"]
