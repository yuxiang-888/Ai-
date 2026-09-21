from __future__ import annotations

import sqlite3

from fulin_editor import web_api


def _insert_job(database, job_id: str = "job-1") -> None:
    now = web_api._utc_now()
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            INSERT INTO web_jobs (
                id, source_name, source_path, status, stage, product_type,
                target_duration, created_at, updated_at
            ) VALUES (?, 'sample.mp4', 'sample.mp4', 'queued', 'queued',
                      'single', 27, ?, ?)
            """,
            (job_id, now, now),
        )
        connection.commit()
    finally:
        connection.close()


def test_agent_trace_persists_state_plan_and_quality(tmp_path, monkeypatch):
    database = tmp_path / "agent.sqlite3"
    monkeypatch.setattr(web_api, "DATABASE", database)
    web_api._ensure_schema()
    _insert_job(database)

    run_id = web_api._start_agent_run("job-1", product_type="single", target_duration=27.0)
    web_api._record_agent_event(
        run_id,
        "job-1",
        "ANALYZING",
        input_summary={"engine_stage": "transcribing"},
        elapsed_seconds=1.25,
    )
    web_api._store_agent_result(
        run_id,
        "job-1",
        {
            "plan": {"product_type": "single", "stages": [{"name": "opening"}]},
            "quality": {"passed": True, "issues": []},
        },
        "approved",
    )

    trace = web_api.get_agent_trace("job-1")
    assert trace["run"]["current_state"] == "PASSED"
    assert [event["state"] for event in trace["events"]] == [
        "RECEIVED",
        "ANALYZING",
        "PASSED",
    ]
    assert trace["plans"][0]["plan"]["product_type"] == "single"
    assert trace["quality"][0]["passed"] is True
    assert "source_path" not in trace["events"][0]["input_summary"]


def test_agent_failure_stops_at_needs_review(tmp_path, monkeypatch):
    database = tmp_path / "agent.sqlite3"
    monkeypatch.setattr(web_api, "DATABASE", database)
    web_api._ensure_schema()
    _insert_job(database)

    run_id = web_api._start_agent_run("job-1", product_type="auto", target_duration=27.0)
    web_api._fail_agent_run(run_id, "job-1", "RuntimeError: ffmpeg unavailable", 2.5)

    trace = web_api.get_agent_trace("job-1")
    assert trace["run"]["status"] == "needs_review"
    assert trace["run"]["current_state"] == "NEEDS_REVIEW"
    assert trace["run"]["error_message"] == "RuntimeError: ffmpeg unavailable"
    assert trace["events"][-1]["event_type"] == "tool_failure"
