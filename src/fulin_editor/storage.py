from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .planner import EditPlan
from .renderer import RenderResult
from .quality import QualityReport


SCHEMA = """
CREATE TABLE IF NOT EXISTS edit_jobs (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    transcript_path TEXT NOT NULL,
    output_path TEXT NOT NULL,
    status TEXT NOT NULL,
    product_type TEXT NOT NULL DEFAULT 'single',
    product_label TEXT NOT NULL DEFAULT '单品及小件',
    total_duration REAL NOT NULL,
    encoder TEXT NOT NULL,
    render_seconds REAL NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edit_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES edit_jobs(id),
    stage_name TEXT NOT NULL,
    stage_label TEXT NOT NULL,
    source_start REAL NOT NULL,
    source_end REAL NOT NULL,
    output_start REAL NOT NULL,
    output_end REAL NOT NULL,
    transcript TEXT NOT NULL,
    confidence REAL NOT NULL,
    center_x REAL NOT NULL DEFAULT 0.5,
    center_y REAL NOT NULL DEFAULT 0.5,
    scale REAL NOT NULL DEFAULT 1.0,
    visual_confidence REAL NOT NULL DEFAULT 0.0,
    evidence_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_edit_segments_job_id ON edit_segments(job_id);
"""


def _ensure_columns(connection: sqlite3.Connection) -> None:
    existing = {
        row[1] for row in connection.execute("PRAGMA table_info(edit_segments)").fetchall()
    }
    additions = {
        "center_x": "REAL NOT NULL DEFAULT 0.5",
        "center_y": "REAL NOT NULL DEFAULT 0.5",
        "scale": "REAL NOT NULL DEFAULT 1.0",
        "visual_confidence": "REAL NOT NULL DEFAULT 0.0",
        "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    for name, declaration in additions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE edit_segments ADD COLUMN {name} {declaration}")
    job_existing = {row[1] for row in connection.execute("PRAGMA table_info(edit_jobs)").fetchall()}
    job_additions = {
        "product_type": "TEXT NOT NULL DEFAULT 'single'",
        "product_label": "TEXT NOT NULL DEFAULT '单品及小件'",
    }
    for name, declaration in job_additions.items():
        if name not in job_existing:
            connection.execute(f"ALTER TABLE edit_jobs ADD COLUMN {name} {declaration}")


def save_job(
    database: str | Path,
    *,
    source: str | Path,
    transcript: str | Path,
    plan: EditPlan,
    render: RenderResult,
    quality: QualityReport | None = None,
    analysis: dict | None = None,
) -> tuple[str, dict]:
    database_path = Path(database).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    job_id = uuid4().hex
    output_cursor = 0.0
    segments = []
    for stage in plan.stages:
        output_end = output_cursor + stage.duration
        segments.append(
            {
                **stage.to_dict(),
                "output_start": round(output_cursor, 3),
                "output_end": round(output_end, 3),
            }
        )
        output_cursor = output_end
    if quality is None:
        status = "needs_review" if plan.warnings else "candidate"
    else:
        status = "approved" if quality.passed else "needs_review"
    report = {
        "job_id": job_id,
        "status": status,
        "source": str(Path(source).resolve()),
        "transcript": str(Path(transcript).resolve()),
        "plan": {**plan.to_dict(), "stages": segments},
        "render": render.to_dict(),
        "quality": quality.to_dict() if quality else None,
        "analysis": analysis or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(SCHEMA)
        _ensure_columns(connection)
        connection.execute(
            """
            INSERT INTO edit_jobs (
                id, source_path, transcript_path, output_path, status, product_type, product_label,
                total_duration, encoder, render_seconds, report_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                report["source"],
                report["transcript"],
                str(render.output),
                status,
                plan.product_type,
                plan.product_label,
                plan.duration,
                render.encoder,
                render.elapsed_seconds,
                json.dumps(report, ensure_ascii=False),
                report["created_at"],
            ),
        )
        for segment in segments:
            connection.execute(
                """
                INSERT INTO edit_segments (
                    job_id, stage_name, stage_label, source_start, source_end,
                    output_start, output_end, transcript, confidence, center_x,
                    center_y, scale, visual_confidence, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    segment["name"],
                    segment["label"],
                    segment["start"],
                    segment["end"],
                    segment["output_start"],
                    segment["output_end"],
                    segment["text"],
                    segment["confidence"],
                    segment["center_x"],
                    segment["center_y"],
                    segment["scale"],
                    segment["visual_confidence"],
                    json.dumps(segment["evidence"], ensure_ascii=False),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return job_id, report
