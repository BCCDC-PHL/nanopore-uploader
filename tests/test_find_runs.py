import logging
import os

import pytest

import nanopore_uploader.core as core

from conftest import GRIDION_RUN_ID, GRIDION_RUN_ID_2, PROMETHION_RUN_ID, make_run


def test_parse_final_summary_skips_lines_without_equals(tmp_path):
    final_summary = tmp_path / "final_summary.txt"
    final_summary.write_text("\ninstrument=GXB03493\n\nprotocol=a=b\nnot a key value line\n")

    assert core.parse_final_summary(final_summary) == {"instrument": "GXB03493", "protocol": "a=b"}


def test_find_runs_finds_complete_runs(config, run_output_dir):
    run_dir = make_run(run_output_dir, GRIDION_RUN_ID, protocol_group_id="RESP-01")

    runs = core.find_runs(config)

    assert runs == [{
        "sequencing_run_id": GRIDION_RUN_ID,
        "protocol_group_id": "RESP-01",
        "instrument_type": "GRIDION",
        "instrument_id": "GXB03493",
        "run_directory": str(run_dir),
    }]


def test_find_runs_recognizes_promethion_run_ids(config, run_output_dir):
    make_run(run_output_dir, PROMETHION_RUN_ID)

    assert [run["sequencing_run_id"] for run in core.find_runs(config)] == [PROMETHION_RUN_ID]


def test_find_runs_ignores_incomplete_and_badly_named_runs(config, run_output_dir):
    make_run(run_output_dir, GRIDION_RUN_ID, complete=False)
    make_run(run_output_dir, "20261014_not_a_run_id")
    # Too shallow: run dirs are three levels below run_output_dir.
    (run_output_dir / GRIDION_RUN_ID_2).mkdir()
    (run_output_dir / GRIDION_RUN_ID_2 / "final_summary_x.txt").write_text("a=b\n")

    assert core.find_runs(config) == []


def test_find_runs_handles_blank_lines_in_final_summary(config, run_output_dir):
    make_run(run_output_dir, GRIDION_RUN_ID, final_summary_text="\nprotocol_group_id=PG-01\n\n")

    runs = core.find_runs(config)

    assert [run["protocol_group_id"] for run in runs] == ["PG-01"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read files without read permission")
def test_find_runs_skips_unreadable_run_and_finds_the_rest(config, run_output_dir, caplog):
    bad_run_dir = make_run(run_output_dir, GRIDION_RUN_ID)
    make_run(run_output_dir, GRIDION_RUN_ID_2, sample_id="sample-02")
    for final_summary in bad_run_dir.glob("final_summary*"):
        final_summary.chmod(0)

    try:
        with caplog.at_level(logging.ERROR):
            runs = core.find_runs(config)
    finally:
        for final_summary in bad_run_dir.glob("final_summary*"):
            final_summary.chmod(0o644)

    assert [run["sequencing_run_id"] for run in runs] == [GRIDION_RUN_ID_2]
    errors = [r.msg for r in caplog.records if isinstance(r.msg, dict) and r.msg.get("event_type") == "check_run_dir_failed"]
    assert len(errors) == 1
    assert errors[0]["error_type"] == "PermissionError"
    assert errors[0]["run_directory"].endswith(GRIDION_RUN_ID)


def test_scan_skips_excluded_runs(config, run_output_dir):
    make_run(run_output_dir, GRIDION_RUN_ID)
    make_run(run_output_dir, GRIDION_RUN_ID_2, sample_id="sample-02")
    config["excluded_runs"] = [GRIDION_RUN_ID]

    assert [run["sequencing_run_id"] for run in core.scan(config)] == [GRIDION_RUN_ID_2]
