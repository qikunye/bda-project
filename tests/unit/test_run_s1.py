from scripts.run_s1 import comparable, first_difference


def output(*, latency=1, finding="PASS_THROUGH"):
    return {
        "cases": [{"findings": [{"code": finding}]}],
        "rows": [{"transaction_id": "txn_a", "latency_ms": latency}],
        "review_list": [{"txn_id": "txn_a"}],
    }


def test_timing_alone_is_not_a_difference():
    assert (
        first_difference(comparable(output(latency=1)), comparable(output(latency=9)))
        is None
    )


def test_a_changed_finding_is_a_difference_and_is_named():
    found = first_difference(
        comparable(output(finding="PASS_THROUGH")), comparable(output(finding="FAN_OUT"))
    )
    assert found == ".cases[0].findings[0].code: 'PASS_THROUGH' vs 'FAN_OUT'"


def test_different_lengths_are_a_difference():
    assert first_difference({"a": [1]}, {"a": [1, 2]}) == ".a: 1 items vs 2 items"


def test_a_missing_key_is_a_difference():
    assert first_difference({"a": 1}, {}) == ".a: only in one run"
