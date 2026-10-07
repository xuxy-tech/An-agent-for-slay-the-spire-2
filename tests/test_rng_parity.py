from controller.rng_parity import (
    compare_rng_snapshots,
    compare_rng_transitions,
    rng_counter_delta,
)


def snapshot(counter=0, state=10):
    return {
        "schema_version": 1,
        "complete": True,
        "run_seed": "SEED",
        "run_streams": {
            "Shuffle": {"counter": counter, "seed": 1, "s0": state, "s1": 2, "s2": 3, "s3": 4},
        },
        "players": [{
            "net_id": "1",
            "streams": {
                "Shops": {"counter": counter, "seed": 2, "s0": state, "s1": 6, "s2": 7, "s3": 8},
            },
        }],
    }


def test_identical_rng_snapshots_pass():
    assert compare_rng_snapshots(snapshot(), snapshot()).status == "PASS"


def test_internal_state_difference_fails_even_when_counter_matches():
    result = compare_rng_snapshots(snapshot(), snapshot(state=11))
    assert result.status == "FAIL"
    assert {row.get("field") for row in result.differences} == {"s0"}


def test_counter_delta_reports_each_stream():
    delta = rng_counter_delta(snapshot(3), snapshot(8, state=12))
    assert delta["run.Shuffle"]["delta"] == 5
    assert delta["player[1].Shops"]["delta"] == 5
    assert delta["run.Shuffle"]["state_changed"] is True


def test_transition_requires_equal_before_after_and_deltas():
    result = compare_rng_transitions(snapshot(3), snapshot(8, state=12), snapshot(3), snapshot(7, state=12))
    assert result["status"] == "FAIL"
    assert any(row.get("stream") == "run.Shuffle" for row in result["differences"])


def test_incomplete_snapshot_fails_closed():
    value = snapshot()
    value["complete"] = False
    assert compare_rng_snapshots(value, snapshot()).status == "INCOMPLETE"
