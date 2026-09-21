from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware
from werkzeug.security import check_password_hash

from .accounts import (
    ALLOW_SIGNUP,
    authenticate as authenticate_account,
    count_admins,
    ensure_schema as ensure_accounts_schema,
    get_user,
    list_users,
    register_user,
    reset_user_password,
)
from .agent_runtime import EditingAgent, LocalEditingTools, save_outcome
from .db import describe as describe_database
from .legacy_engine import LEGACY_PROFILES, SuchenEngineClient, run_suchen_edit
from .policies import POLICIES, RequestedProductType, policy_catalog


ROOT = Path(__file__).resolve().parents[2]
DATABASE = Path(os.getenv("FULIN_DATABASE", ROOT / "data" / "fulin_editor.sqlite3")).resolve()
RAW_VIDEO_DIR = Path(
    os.getenv("FULIN_RAW_VIDEO_DIR", r"C:\Users\david\Desktop\未剪辑视频")
).resolve()
UPLOAD_DIR = Path(os.getenv("FULIN_UPLOAD_DIR", ROOT / "data" / "uploads")).resolve()
OUTPUT_DIR = Path(os.getenv("FULIN_OUTPUT_DIR", ROOT / "outputs" / "web")).resolve()
OUTPUT_ROOT = Path(os.getenv("FULIN_OUTPUT_ROOT", ROOT / "outputs")).resolve()
CACHE_DIR = Path(os.getenv("FULIN_CACHE_DIR", ROOT / "data" / "cache")).resolve()
POSE_MODEL = Path(
    os.getenv("FULIN_POSE_MODEL", ROOT / "models" / "pose_landmarker_lite.task")
).resolve()
LEGACY_ROOT = Path(
    os.getenv("FULIN_LEGACY_ROOT", r"C:\Users\david\Desktop\suchen-便携版-1.2.3")
).resolve()
PUBLIC_MODE = os.getenv("FULIN_PUBLIC_MODE", "").strip().lower() in {"1", "true", "yes"}
PUBLIC_LOGIN_EMAIL = os.getenv("SUCHEN_LOGIN_EMAIL", "").strip().lower()
PUBLIC_LOGIN_PASSWORD_HASH = os.getenv("SUCHEN_LOGIN_PASSWORD_HASH", "").strip()
PUBLIC_SESSION_SECRET = os.getenv("SUCHEN_SESSION_SECRET", "").strip()
AUTH_REQUIRED = os.getenv("FULIN_AUTH_REQUIRED", "1").strip().lower() not in {
    "0", "false", "no"
}


def _session_secret() -> str:
    """Return a stable local secret without committing it into the source tree."""
    if PUBLIC_SESSION_SECRET:
        return PUBLIC_SESSION_SECRET
    if PUBLIC_MODE:
        raise RuntimeError("公网部署必须配置 SUCHEN_SESSION_SECRET")
    secret_file = DATABASE.parent / ".suchen-session-secret"
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        if secret_file.is_file():
            value = secret_file.read_text(encoding="utf-8").strip()
            if len(value) >= 32:
                return value
        value = secrets.token_urlsafe(48)
        secret_file.write_text(value, encoding="utf-8")
        return value
    except OSError:
        # Read-only package fallback. Public deployments must always provide the
        # environment value; local fallback only keeps the service startable.
        return secrets.token_urlsafe(48)
LEGACY_UPLOAD_DIR = (LEGACY_ROOT / "便携数据" / "runtime" / "uploads").resolve()
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
STAGES = ("queued", "transcribing", "analyzing", "planning", "rendering", "quality")

# Extra browser origins allowed to call the API (comma separated). The cloud
# deployment sets this to the public front-end domain, e.g.
# FULIN_CORS_ORIGINS=https://suchen-ai.vercel.app
CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5000",
    "http://127.0.0.1:5000",
    *[item.strip() for item in os.getenv("FULIN_CORS_ORIGINS", "").split(",") if item.strip()],
]

# Account endpoints stay reachable without a session so the login/register
# screen can render and submit on a public deployment.
AUTH_OPEN_PATHS = {"/api/auth/login", "/api/auth/register", "/api/auth/me"}

app = FastAPI(title="suchen AI 全自动剪辑 API", version="0.5.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fulin-editor")
schema_lock = threading.Lock()

INTEGRATED_UI_ROOT = ROOT / "integration" / "suchen-ui"
if (INTEGRATED_UI_ROOT / "static").is_dir():
    app.mount("/static", StaticFiles(directory=INTEGRATED_UI_ROOT / "static"), name="static")
    integrated_templates = Jinja2Templates(directory=INTEGRATED_UI_ROOT / "templates")


def _installed_engine_checks() -> dict[str, bool]:
    internal = LEGACY_ROOT / "_internal"
    pose_candidates = (
        POSE_MODEL,
        internal / "models" / "pose_landmarker_lite.task",
        internal / "models" / "pose_landmarker_full.task",
    )
    return {
        "asr_model": (internal / "models" / "faster-whisper-small").is_dir(),
        "pose_model": any(path.is_file() for path in pose_candidates),
        "ffmpeg": (internal / "bin" / "ffmpeg.exe").is_file() or shutil.which("ffmpeg") is not None,
        "ffprobe": (internal / "bin" / "ffprobe.exe").is_file() or shutil.which("ffprobe") is not None,
        "database": DATABASE.is_file(),
    }


@app.middleware("http")
async def protect_public_api(request: Request, call_next):
    """Protect every public UI/API route with one signed SOCHEN session."""

    if AUTH_REQUIRED:
        path = request.url.path
        allowed = (
            path == "/login"
            or path in AUTH_OPEN_PATHS
            or path == "/api/health"
            or path == "/favicon.ico"
            or path.startswith("/static/")
        )
        signed_in = current_user(request) is not None
        if not allowed and not signed_in:
            if path.startswith("/api/") or path == "/stream":
                return JSONResponse({"detail": "请先登录 SOCHEN 云端工作区"}, status_code=401)
            return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin", "")
            external_host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
            if origin:
                parsed = urlparse(origin)
                allowed_schemes = {"https"} if PUBLIC_MODE else {"http", "https"}
                if parsed.scheme not in allowed_schemes or parsed.netloc != external_host:
                    return JSONResponse({"detail": "跨站请求已拒绝"}, status_code=403)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if PUBLIC_MODE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    session_cookie="suchen_session",
    same_site="lax",
    https_only=PUBLIC_MODE,
    max_age=60 * 60 * 12,
)


@app.get("/", include_in_schema=False)
def integrated_home(request: Request):
    user = current_user(request)
    return integrated_templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "cloud_mode": PUBLIC_MODE,
            "auth_enabled": AUTH_REQUIRED,
            "account_email": user["email"] if user else "",
            "account_name": (user.get("display_name") or user["email"].split("@")[0]) if user else "",
            "account_role": user.get("role", "") if user else "",
            "can_manage_knowledge": bool(user and user.get("role") == "admin"),
            "default_output": str(OUTPUT_DIR),
        },
    )


@app.api_route("/login", methods=["GET", "POST"], include_in_schema=False)
async def public_login(request: Request):
    # The login screen is available on every deployment: a public host needs
    # accounts, and a local operator may still want to sign in or register.
    if current_user(request):
        return RedirectResponse(url="/", status_code=303)
    error = ""
    if request.method == "POST":
        form = await request.form()
        email = str(form.get("email", "")).strip().lower()
        password = str(form.get("password", ""))
        # Database accounts first, then the legacy single-account fallback.
        user = authenticate_account(email, password)
        if user is None and PUBLIC_LOGIN_EMAIL and PUBLIC_LOGIN_PASSWORD_HASH:
            if email == PUBLIC_LOGIN_EMAIL and check_password_hash(
                PUBLIC_LOGIN_PASSWORD_HASH, password
            ):
                user = {
                    "id": "",
                    "email": email,
                    "display_name": email.split("@")[0],
                    "role": "admin",
                }
        if user is not None:
            request.session.clear()
            request.session[AUTH_SESSION_KEY] = user.get("id") or ""
            request.session["suchen_user"] = user["email"]
            destination = request.query_params.get("next", "/")
            if not destination.startswith("/") or destination.startswith("//"):
                destination = "/"
            return RedirectResponse(url=destination, status_code=303)
        error = "邮箱或密码不正确，或者云端登录尚未配置。"
    return integrated_templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": error,
            "signup_enabled": ALLOW_SIGNUP,
            "first_account": count_admins() == 0,
        },
        status_code=401 if error else 200,
    )


@app.post("/logout", include_in_schema=False)
def public_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ---------------------------------------------------------------------------
# Multi-user accounts (stored in the database, shared by every deployment)
# ---------------------------------------------------------------------------

AUTH_SESSION_KEY = "suchen_user_id"


class RegisterRequest(BaseModel):
    email: str = Field(min_length=5, max_length=160)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=60)


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=160)
    password: str = Field(min_length=1, max_length=128)


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


def current_user(request: Request) -> dict | None:
    """Resolve the signed-in account from the session."""
    user_id = request.session.get(AUTH_SESSION_KEY)
    if user_id:
        return get_user(str(user_id))
    # Backwards compatibility with the legacy single-account login.
    email = request.session.get("suchen_user")
    if email:
        return {
            "id": "",
            "email": str(email),
            "display_name": str(email).split("@")[0],
            "role": "admin",
            "created_at": None,
            "last_login_at": None,
        }
    return None


@app.post("/api/auth/register", status_code=201)
def auth_register(request: Request, payload: RegisterRequest) -> dict:
    if count_admins() == 0 and (not request.client or request.client.host not in {"127.0.0.1", "::1"}):
        raise HTTPException(status_code=403, detail="请先在服务器桌面打开 AI 智能剪辑，注册管理员账号后再开放成员注册")
    try:
        user = register_user(payload.email, payload.password, payload.display_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user": user, "message": "注册成功，请登录"}


@app.post("/api/auth/login")
def auth_login(request: Request, payload: LoginRequest) -> dict:
    user = authenticate_account(payload.email, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")
    request.session.clear()
    request.session[AUTH_SESSION_KEY] = user["id"]
    # The legacy middleware and template still read the email key.
    request.session["suchen_user"] = user["email"]
    return {"user": user, "message": "登录成功"}


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict:
    return {
        "user": current_user(request),
        "signup_enabled": ALLOW_SIGNUP,
        "public_mode": PUBLIC_MODE,
        "auth_required": AUTH_REQUIRED,
    }


def _require_admin(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以修改和应用公司知识库")
    return user


@app.get("/accounts", include_in_schema=False)
def accounts_page(request: Request):
    user = _require_admin(request)
    return integrated_templates.TemplateResponse(
        request=request,
        name="accounts.html",
        context={
            "account_email": user["email"],
            "account_name": user.get("display_name") or user["email"].split("@")[0],
        },
    )


@app.get("/api/admin/users")
def admin_list_users(
    request: Request,
    q: str = Query(default="", max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
) -> dict:
    _require_admin(request)
    return list_users(query=q, page=page, page_size=page_size)


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(user_id: str, payload: ResetPasswordRequest, request: Request) -> dict:
    _require_admin(request)
    try:
        reset_user_password(user_id, payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "message": "密码已安全重置，原密码无法查看"}


WEB_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_jobs (
    id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    output_path TEXT,
    report_path TEXT,
    engine_job_id TEXT,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    product_type TEXT NOT NULL DEFAULT 'auto',
    target_duration REAL NOT NULL,
    error_message TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_jobs_created_at ON web_jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_web_jobs_status ON web_jobs(status);
CREATE TABLE IF NOT EXISTS legacy_evidence_snapshots (
    source_path TEXT PRIMARY KEY,
    snapshot_json TEXT NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_edit_runs (
    source_run_id INTEGER PRIMARY KEY,
    run_uuid TEXT,
    created_at TEXT NOT NULL,
    source_name TEXT NOT NULL,
    product_type TEXT,
    product_label TEXT,
    target_duration REAL,
    actual_duration REAL,
    decision_status TEXT,
    publish_status TEXT,
    confidence REAL,
    elapsed_seconds REAL,
    ok INTEGER NOT NULL DEFAULT 0,
    quality_pass INTEGER NOT NULL DEFAULT 0,
    is_fallback INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    review_status TEXT,
    review_score INTEGER,
    review_note TEXT,
    source_available INTEGER NOT NULL DEFAULT 0,
    output_available INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_legacy_edit_runs_created_at ON legacy_edit_runs(created_at DESC);
CREATE TABLE IF NOT EXISTS knowledge_entries (
    id TEXT PRIMARY KEY,
    rule_key TEXT UNIQUE,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 50,
    enabled INTEGER NOT NULL DEFAULT 1,
    protected INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_category
ON knowledge_entries(category, enabled, priority DESC);
CREATE TABLE IF NOT EXISTS knowledge_migrations (
    version TEXT PRIMARY KEY,
    previous_entries_json TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES web_jobs(id),
    status TEXT NOT NULL,
    current_state TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 2,
    rule_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_state ON agent_runs(current_state, updated_at DESC);
CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES agent_runs(id),
    job_id TEXT NOT NULL REFERENCES web_jobs(id),
    state TEXT NOT NULL,
    event_type TEXT NOT NULL,
    input_summary_json TEXT NOT NULL DEFAULT '{}',
    output_summary_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT,
    elapsed_seconds REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_events_job ON agent_events(job_id, id);
CREATE TABLE IF NOT EXISTS agent_plan_versions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id),
    job_id TEXT NOT NULL REFERENCES web_jobs(id),
    version INTEGER NOT NULL,
    decision TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, version)
);
CREATE TABLE IF NOT EXISTS agent_quality_results (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id),
    job_id TEXT NOT NULL REFERENCES web_jobs(id),
    attempt INTEGER NOT NULL,
    passed INTEGER NOT NULL DEFAULT 0,
    quality_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, attempt)
);
CREATE TABLE IF NOT EXISTS human_feedback (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES web_jobs(id),
    user_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    issue_types_json TEXT NOT NULL DEFAULT '[]',
    corrected_timeline_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_human_feedback_job ON human_feedback(job_id, created_at DESC);
"""


KNOWLEDGE_SEEDS = (
    ("action.opening", "body_action", "首帧全身正面", "第1秒优先保留模特全身正面稳定画面，用作商品主图。", {"mode": "required", "opening_max_sec": 1.5}, 100),
    ("action.size", "spoken_content", "尺码信息开场", "识别主播的身高、体重、试穿码数与尺码建议，优先保留完整口播。", {"mode": "required"}, 100),
    ("action.center", "body_action", "上前中央介绍商品", "识别模特从原位走向画面中央的动作，保留面料、版型、工艺和颜色介绍。", {"mode": "required", "approach_preroll_sec": 0.75}, 95),
    ("action.back", "body_action", "转身背面展示", "保留转身、背面全身展示和背面工艺口播，避免在转身中间切断。", {"mode": "required", "back_min_duration_sec": 0.6}, 95),
    ("action.return", "body_action", "回原位转回正面", "保留模特回到原位并转回正面的收尾动作，结尾画面需稳定。", {"mode": "required", "return_stable_sec": 1.0}, 95),
    ("content.core", "spoken_content", "口播保留内容", "优先完整保留尺码推荐和面料讲解，覆盖原片出现的各个商品颜色及背面展示。品牌、版型、工艺有价值且时长允许时保留；重复和无关口播可删除。原片缺失必要证据时标记待复核，不编造颜色、尺码、面料或动作。", {"required": ["尺码", "面料", "颜色", "后背"], "preferred": ["品牌", "版型", "工艺"]}, 100),
    ("framing.jianying_y", "editing_sop", "剪映 Y=500（保持人物比例）", "记录剪映工程参数Y=500，输出固定3:4、1080x1440。Y=500不是纵向拉伸五倍，渲染必须保持人物比例。", {"jianying_position_y_px": 500, "vertical_scale_percent": 100, "calibration_status": "documented_not_stretched"}, 100),
    ("quality.continuity", "quality_gate", "画面连续性硬标准", "保持原速、原声和同一景别；同一远景或同一近景内允许手部、肢体讲解动作不连续，不因此判为闪现；禁止跨景别突跳、黑帧、整帧闪现、闪退或诡异画面；不加变速、转场、字幕或音乐。", {"pixel_flicker_gate_locked": True, "continuity_gate_locked": True, "same_shot_body_motion_allowed": True, "view_jump_blocked": True}, 100),
    ("duration.single", "product_rule", "单品及小件", "成片30秒以内，优先尺码、面料、版型、工艺、颜色和后背。", {"product_type": "single", "max_seconds": 30}, 90),
    ("duration.set", "product_rule", "上下套装及两件套", "成片60秒以内，完整保留上下装尺码、面料、版型与工艺。", {"product_type": "set", "max_seconds": 60}, 90),
    ("duration.bulky", "product_rule", "大货外套", "皮草、羽绒、派克和双面呢成片不得超过120秒。", {"product_type": "bulky", "max_seconds": 120}, 100),
)


class CreateJobRequest(BaseModel):
    source_name: str = Field(min_length=1, max_length=255)
    source_origin: Literal["library", "upload", "legacy"] = "library"
    product_type: RequestedProductType = "auto"
    target_duration: float | None = Field(default=None, ge=15.0, le=120.0)


class HumanFeedbackRequest(BaseModel):
    verdict: Literal["approved", "rejected"]
    issue_types: list[str] = Field(default_factory=list, max_length=20)
    corrected_timeline: list[dict] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=500)


class BatchSource(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    origin: Literal["library", "upload", "legacy"] = "library"


class BatchCreateJobRequest(BaseModel):
    sources: list[BatchSource] = Field(min_length=1, max_length=100)
    product_type: RequestedProductType = "auto"
    target_duration: float | None = Field(default=None, ge=15.0, le=120.0)


KnowledgeCategory = Literal[
    "editing_sop",
    "body_action",
    "spoken_content",
    "quality_gate",
    "product_rule",
]


class KnowledgeEntryCreate(BaseModel):
    category: KnowledgeCategory
    title: str = Field(min_length=2, max_length=100)
    content: str = Field(min_length=2, max_length=4000)
    priority: int = Field(default=50, ge=0, le=100)


class KnowledgeEntryUpdate(BaseModel):
    category: KnowledgeCategory | None = None
    title: str | None = Field(default=None, min_length=2, max_length=100)
    content: str | None = Field(default=None, min_length=2, max_length=4000)
    priority: int | None = Field(default=None, ge=0, le=100)
    enabled: bool | None = None


def _connect() -> sqlite3.Connection:
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _ensure_schema() -> None:
    with schema_lock:
        connection = _connect()
        try:
            connection.executescript(WEB_SCHEMA)
            _seed_knowledge(connection)
            web_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(web_jobs)").fetchall()
            }
            if "product_type" not in web_columns:
                connection.execute(
                    "ALTER TABLE web_jobs ADD COLUMN product_type TEXT NOT NULL DEFAULT 'auto'"
                )
            existing_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='edit_jobs'"
            ).fetchone()
            if existing_table:
                rows = connection.execute(
                    "SELECT * FROM edit_jobs ORDER BY created_at DESC"
                ).fetchall()
                for row in rows:
                    report = json.loads(row["report_json"])
                    report_plan = report.get("plan") or {}
                    imported_product_type = report_plan.get("product_type", "single")
                    output_path = Path(row["output_path"])
                    report_path = output_path.with_suffix(".json")
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO web_jobs (
                            id, source_name, source_path, output_path, report_path,
                            engine_job_id, status, stage, product_type, target_duration,
                            result_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?)
                        """,
                        (
                            row["id"],
                            Path(row["source_path"]).name,
                            row["source_path"],
                            row["output_path"],
                            str(report_path) if report_path.is_file() else None,
                            row["id"],
                            row["status"],
                            imported_product_type,
                            row["total_duration"],
                            json.dumps(report, ensure_ascii=False),
                            row["created_at"],
                            row["created_at"],
                        ),
                    )
            connection.commit()
        finally:
            connection.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_knowledge(connection: sqlite3.Connection) -> None:
    now = _utc_now()
    for rule_key, category, title, content, config, priority in KNOWLEDGE_SEEDS:
        connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_entries (
                id, rule_key, category, title, content, config_json, priority,
                enabled, protected, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 'company-sop', ?, ?)
            """,
            (
                f"system-{rule_key.replace('.', '-')}",
                rule_key,
                category,
                title,
                content,
                json.dumps(config, ensure_ascii=False),
                priority,
                now,
                now,
            ),
        )

    # Apply the user's revised SOP once, retaining old values for recovery and
    # allowing subsequent management edits to persist across reads/restarts.
    version = "2026-09-20-taobao-women-agent-v4"
    if not connection.execute(
        "SELECT 1 FROM knowledge_migrations WHERE version = ?", (version,)
    ).fetchone():
        previous = []
        for rule_key, _category, title, content, config, _priority in KNOWLEDGE_SEEDS:
            if rule_key not in {"quality.continuity", "content.core", "framing.jianying_y"}:
                continue
            row = connection.execute(
                "SELECT * FROM knowledge_entries WHERE rule_key=? AND source='company-sop'",
                (rule_key,),
            ).fetchone()
            if row is not None:
                previous.append(dict(row))
                connection.execute(
                    "UPDATE knowledge_entries SET content=?, config_json=?, updated_at=? WHERE id=?",
                    (content, json.dumps(config, ensure_ascii=False), now, row["id"]),
                )
                if rule_key == "framing.jianying_y":
                    connection.execute("UPDATE knowledge_entries SET title=? WHERE id=?", (title, row["id"]))
        connection.execute(
            "INSERT INTO knowledge_migrations VALUES (?, ?, ?)",
            (version, json.dumps(previous, ensure_ascii=False), now),
        )


def _safe_child(directory: Path, name: str) -> Path:
    if not name or Path(name).name != name:
        raise HTTPException(status_code=400, detail="文件名无效")
    candidate = (directory / name).resolve()
    try:
        candidate.relative_to(directory.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="文件路径越界") from exc
    return candidate


def _job_dict(row: sqlite3.Row) -> dict:
    payload = dict(row)
    result = None
    if payload.pop("result_json", None):
        try:
            result = json.loads(row["result_json"])
        except json.JSONDecodeError:
            result = None
    payload["result"] = result
    payload["report"] = None
    report_path = payload.get("report_path")
    if report_path:
        candidate = Path(report_path).resolve()
        try:
            candidate.relative_to(OUTPUT_ROOT)
        except ValueError:
            candidate = Path("__untrusted__")
        if candidate.is_file():
            try:
                payload["report"] = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload["report"] = None
    payload["media_url"] = (
        f"/api/jobs/{payload['id']}/media" if payload.get("output_path") else None
    )
    payload["product_type"] = payload.get("product_type") or "single"
    return payload


def _read_job(job_id: str) -> dict:
    _ensure_schema()
    connection = _connect()
    try:
        row = connection.execute("SELECT * FROM web_jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        raise HTTPException(status_code=404, detail="剪辑任务不存在")
    return _job_dict(row)


def _update_job(job_id: str, **values: object) -> None:
    if not values:
        return
    values["updated_at"] = _utc_now()
    columns = ", ".join(f"{key} = ?" for key in values)
    connection = _connect()
    try:
        connection.execute(
            f"UPDATE web_jobs SET {columns} WHERE id = ?",
            (*values.values(), job_id),
        )
        connection.commit()
    finally:
        connection.close()


AGENT_RULE_VERSION = "taobao-women-v3-2026-09-21"
AGENT_STATE_BY_STAGE = {
    "queued": "RECEIVED",
    "transcribing": "ANALYZING",
    "analyzing": "ANALYZING",
    "planning": "PLANNING",
    "rendering": "RENDERING",
    "quality": "EVALUATING",
}


def _json_object(value: object) -> str:
    """Serialize bounded operational summaries, never credentials or full prompts."""

    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def _start_agent_run(job_id: str, *, product_type: str, target_duration: float) -> str:
    run_id = uuid4().hex
    now = _utc_now()
    connection = _connect()
    try:
        connection.execute(
            """
            INSERT INTO agent_runs (
                id, job_id, status, current_state, retry_count, max_retries,
                rule_version, started_at, updated_at
            ) VALUES (?, ?, 'running', 'RECEIVED', 0, 2, ?, ?, ?)
            """,
            (run_id, job_id, AGENT_RULE_VERSION, now, now),
        )
        connection.execute(
            """
            INSERT INTO agent_events (
                run_id, job_id, state, event_type, input_summary_json,
                output_summary_json, elapsed_seconds, created_at
            ) VALUES (?, ?, 'RECEIVED', 'state_entered', ?, '{}', 0, ?)
            """,
            (
                run_id,
                job_id,
                _json_object(
                    {"product_type": product_type, "target_duration": target_duration}
                ),
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return run_id


def _record_agent_event(
    run_id: str,
    job_id: str,
    state: str,
    *,
    event_type: str = "state_entered",
    input_summary: object | None = None,
    output_summary: object | None = None,
    error_message: str | None = None,
    elapsed_seconds: float = 0.0,
) -> None:
    now = _utc_now()
    connection = _connect()
    try:
        connection.execute(
            """
            INSERT INTO agent_events (
                run_id, job_id, state, event_type, input_summary_json,
                output_summary_json, error_message, elapsed_seconds, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                job_id,
                state,
                event_type,
                _json_object(input_summary),
                _json_object(output_summary),
                error_message,
                round(max(0.0, elapsed_seconds), 3),
                now,
            ),
        )
        connection.execute(
            "UPDATE agent_runs SET current_state=?, updated_at=?, error_message=? WHERE id=?",
            (state, now, error_message, run_id),
        )
        connection.commit()
    finally:
        connection.close()


def _store_agent_result(run_id: str, job_id: str, report: dict, status: str) -> None:
    now = _utc_now()
    plan = report.get("plan") if isinstance(report.get("plan"), dict) else {}
    quality = report.get("quality") if isinstance(report.get("quality"), dict) else {}
    quality_passed = bool(quality.get("passed")) and status == "approved"
    decision = "render" if plan and not plan.get("warnings") else "needs_review"
    final_state = "PASSED" if quality_passed else "NEEDS_REVIEW"
    connection = _connect()
    try:
        connection.execute(
            """
            INSERT OR REPLACE INTO agent_plan_versions (
                id, run_id, job_id, version, decision, plan_json, created_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            """,
            (uuid4().hex, run_id, job_id, decision, _json_object(plan), now),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO agent_quality_results (
                id, run_id, job_id, attempt, passed, quality_json, created_at
            ) VALUES (?, ?, ?, 0, ?, ?, ?)
            """,
            (uuid4().hex, run_id, job_id, int(quality_passed), _json_object(quality), now),
        )
        connection.execute(
            """
            UPDATE agent_runs
            SET status=?, current_state=?, completed_at=?, updated_at=?
            WHERE id=?
            """,
            ("passed" if quality_passed else "needs_review", final_state, now, now, run_id),
        )
        connection.execute(
            """
            INSERT INTO agent_events (
                run_id, job_id, state, event_type, input_summary_json,
                output_summary_json, elapsed_seconds, created_at
            ) VALUES (?, ?, ?, 'run_completed', '{}', ?, 0, ?)
            """,
            (
                run_id,
                job_id,
                final_state,
                _json_object(
                    {
                        "quality_passed": quality_passed,
                        "issue_codes": [
                            item.get("code")
                            for item in quality.get("issues", [])
                            if isinstance(item, dict) and item.get("code")
                        ],
                    }
                ),
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _fail_agent_run(run_id: str, job_id: str, message: str, elapsed_seconds: float) -> None:
    _record_agent_event(
        run_id,
        job_id,
        "NEEDS_REVIEW",
        event_type="tool_failure",
        error_message=message,
        elapsed_seconds=elapsed_seconds,
    )
    now = _utc_now()
    connection = _connect()
    try:
        connection.execute(
            """
            UPDATE agent_runs
            SET status='needs_review', current_state='NEEDS_REVIEW',
                completed_at=?, updated_at=?, error_message=?
            WHERE id=?
            """,
            (now, now, message, run_id),
        )
        connection.commit()
    finally:
        connection.close()


def _knowledge_dict(row: sqlite3.Row) -> dict:
    payload = dict(row)
    payload["enabled"] = bool(payload.get("enabled"))
    payload["protected"] = bool(payload.get("protected"))
    try:
        payload["config"] = json.loads(payload.pop("config_json", "{}"))
    except json.JSONDecodeError:
        payload["config"] = {}
    return payload


def _knowledge_entries(enabled_only: bool = False) -> list[dict]:
    _ensure_schema()
    connection = _connect()
    try:
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = connection.execute(
            f"""
            SELECT * FROM knowledge_entries {where}
            ORDER BY priority DESC, updated_at DESC, title COLLATE NOCASE
            """
        ).fetchall()
    finally:
        connection.close()
    return [_knowledge_dict(row) for row in rows]


def _compile_knowledge_contract(entries: list[dict]) -> dict:
    by_key = {entry.get("rule_key"): entry for entry in entries if entry.get("rule_key")}
    action_keys = {
        "opening_full_front": "action.opening",
        "origin_size_intro": "action.size",
        "move_to_center_product_detail": "action.center",
        "back_full_body_show": "action.back",
        "return_origin_front_finish": "action.return",
    }
    modes = {
        role: str((by_key.get(rule_key, {}).get("config") or {}).get("mode", "required"))
        if by_key.get(rule_key, {}).get("enabled")
        else "off"
        for role, rule_key in action_keys.items()
    }
    timing: dict[str, float] = {}
    for entry in entries:
        config = entry.get("config") or {}
        for key in (
            "opening_max_sec",
            "approach_preroll_sec",
            "back_min_duration_sec",
            "return_stable_sec",
        ):
            if key in config:
                timing[key] = float(config[key])
    durations = {}
    for key in ("duration.single", "duration.set", "duration.bulky"):
        entry = by_key.get(key) or {}
        config = entry.get("config") or {}
        if entry.get("enabled") and config.get("product_type"):
            durations[str(config["product_type"])] = min(120.0, float(config.get("max_seconds") or 120))
    framing = (by_key.get("framing.jianying_y") or {}).get("config") or {}
    continuity = (by_key.get("quality.continuity") or {}).get("config") or {}
    content_policy = (by_key.get("content.core") or {}).get("config") or {}
    return {
        "modes": modes,
        **timing,
        "continuity_gate_locked": True,
        "pixel_flicker_gate_locked": True,
        "maximum_output_seconds": min(120.0, max(durations.values(), default=120.0)),
        "duration_limits": durations,
        "jianying_position_y_px": float(framing.get("jianying_position_y_px", 500.0)),
        "vertical_scale_percent": 100.0,
        "framing_calibration_status": framing.get("calibration_status", "pending_reference"),
        "same_shot_body_motion_allowed": bool(
            continuity.get("same_shot_body_motion_allowed", True)
        ),
        "view_jump_blocked": bool(continuity.get("view_jump_blocked", True)),
        "content_policy": content_policy,
        "active_rule_count": len(entries),
    }


def _apply_knowledge_contract() -> dict:
    entries = _knowledge_entries(enabled_only=True)
    contract = _compile_knowledge_contract(entries)
    client = SuchenEngineClient(
        os.getenv("FULIN_SUCHEN_ENGINE_URL", "http://127.0.0.1:5000")
    )
    current = client.action_template()
    payload = {
        **current,
        "name": current.get("name") or "福临女装知识库动作模板",
        "modes": contract["modes"],
        "continuity_gate_locked": True,
        "pixel_flicker_gate_locked": True,
        "jianying_position_y_px": contract["jianying_position_y_px"],
        "vertical_scale_percent": 100.0,
    }
    for key in (
        "opening_max_sec",
        "approach_preroll_sec",
        "back_min_duration_sec",
        "return_stable_sec",
    ):
        if key in contract:
            payload[key] = contract[key]
    saved = client.save_action_template(payload)
    if saved.get("error"):
        raise RuntimeError(str(saved["error"]))
    return contract


def _run_job(
    job_id: str,
    source: Path,
    output: Path,
    target_duration: float | None,
    product_type: RequestedProductType,
) -> None:
    started = perf_counter()
    effective_target = float(target_duration or LEGACY_PROFILES[product_type][2])
    run_id = _start_agent_run(
        job_id,
        product_type=product_type,
        target_duration=effective_target,
    )
    last_state = "RECEIVED"
    try:
        def progress(stage: str) -> None:
            nonlocal last_state
            if stage == "completed":
                return
            _update_job(job_id, status="running", stage=stage)
            state = AGENT_STATE_BY_STAGE.get(stage, stage.upper())
            if state == "RENDERING" and last_state != "VALIDATING":
                _record_agent_event(
                    run_id,
                    job_id,
                    "VALIDATING",
                    output_summary={"result": "legacy_engine_plan_validation"},
                    elapsed_seconds=perf_counter() - started,
                )
                last_state = "VALIDATING"
            if state != last_state:
                _record_agent_event(
                    run_id,
                    job_id,
                    state,
                    input_summary={"engine_stage": stage},
                    elapsed_seconds=perf_counter() - started,
                )
                last_state = state

        progress("planning")
        knowledge_contract = _apply_knowledge_contract()
        result = run_suchen_edit(
            source=source,
            output_dir=output.parent / job_id,
            legacy_root=LEGACY_ROOT,
            product_type=product_type,
            target_duration=target_duration,
            progress=progress,
        )
        report_payload = json.loads(result.report.read_text(encoding="utf-8"))
        plan_payload = report_payload.get("plan") or {}
        _store_agent_result(run_id, job_id, report_payload, result.status)
        _update_job(
            job_id,
            output_path=str(result.output) if result.output else None,
            report_path=str(result.report),
            engine_job_id=result.job_id,
            status=result.status,
            stage="completed",
            product_type=plan_payload.get("product_type", product_type),
            target_duration=plan_payload.get("target_duration", target_duration or 27.0),
            result_json=json.dumps(
                {**result.payload, "knowledge_contract": knowledge_contract},
                ensure_ascii=False,
            ),
            error_message=result.error,
        )
    except Exception as exc:  # A background worker must persist actionable failure state.
        error_message = f"{type(exc).__name__}: {exc}"
        _fail_agent_run(run_id, job_id, error_message, perf_counter() - started)
        _update_job(
            job_id,
            status="failed",
            stage="failed",
            error_message=error_message,
        )


def _run_agent_job(
    job_id: str,
    source: Path,
    output: Path,
    target_duration: float | None,
    product_type: RequestedProductType,
) -> None:
    started = perf_counter()
    effective_target = float(target_duration or LEGACY_PROFILES[product_type][2])
    run_id = _start_agent_run(
        job_id,
        product_type=product_type,
        target_duration=effective_target,
    )
    last_state = "RECEIVED"
    state_to_stage = {
        "RECEIVED": "queued",
        "ANALYZING": "analyzing",
        "PLANNING": "planning",
        "VALIDATING": "planning",
        "RENDERING": "rendering",
        "EVALUATING": "quality",
        "REVISING": "planning",
    }
    try:
        def progress(state: str, payload: dict) -> None:
            nonlocal last_state
            stage = state_to_stage.get(state, "planning")
            _update_job(job_id, status="running", stage=stage)
            if state != last_state:
                _record_agent_event(
                    run_id,
                    job_id,
                    state,
                    input_summary=payload,
                    elapsed_seconds=perf_counter() - started,
                )
                last_state = state

        tools = LocalEditingTools(
            cache_dir=CACHE_DIR,
            whisper_model="small",
            pose_model=POSE_MODEL,
        )
        outcome = EditingAgent(tools, max_retries=2).run(
            source,
            output,
            product_type=product_type,
            target_duration=target_duration,
            progress=progress,
        )
        report_path = output.with_suffix(".agent.json")
        save_outcome(outcome, report_path)
        last_attempt = outcome.attempts[-1] if outcome.attempts else None
        normalized_report = {
            "plan": last_attempt.plan if last_attempt else {},
            "quality": last_attempt.quality if last_attempt else {
                "passed": False,
                "issues": [
                    {"code": "planning_incomplete", "message": warning, "severity": "review"}
                    for warning in outcome.warnings
                ],
            },
        }
        _store_agent_result(run_id, job_id, normalized_report, outcome.status)
        _update_job(
            job_id,
            output_path=outcome.output,
            report_path=str(report_path),
            engine_job_id=run_id,
            status=outcome.status,
            stage="completed",
            product_type=outcome.product_type,
            target_duration=target_duration or effective_target,
            result_json=json.dumps(
                {"engine": "taobao-women-agent-v1", "agent": outcome.to_dict()},
                ensure_ascii=False,
            ),
            error_message="；".join(outcome.warnings) or None,
        )
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        _fail_agent_run(run_id, job_id, error_message, perf_counter() - started)
        _update_job(
            job_id,
            status="failed",
            stage="failed",
            error_message=error_message,
        )


@app.on_event("startup")
def startup() -> None:
    ensure_accounts_schema()
    _ensure_schema()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _sync_legacy_evidence()


@app.get("/api/health")
def health(request: Request) -> dict:
    _ensure_schema()
    connection = _connect()
    try:
        legacy_run_count = connection.execute(
            "SELECT COUNT(*) FROM legacy_edit_runs"
        ).fetchone()[0]
    finally:
        connection.close()
    legacy_engine_online = False
    legacy_status: dict = {}
    try:
        legacy_status = SuchenEngineClient(
            os.getenv("FULIN_SUCHEN_ENGINE_URL", "http://127.0.0.1:5000"),
            timeout=1.0,
        ).request_json("GET", "/api/status")
        legacy_engine_online = isinstance(legacy_status, dict) and bool(legacy_status)
    except Exception:
        pass
    legacy_checks = _installed_engine_checks()
    components_ready = all(legacy_checks.values())
    payload = {
        "status": "ok",
        "service": "fulin-editor-api",
        "ready": legacy_engine_online and components_ready,
        "component_check_pending": False,
        "checks": legacy_checks,
        "mode": "CPU 本地离线" if legacy_engine_online else "SOCHEN 原引擎未连接",
        "output": "1080P · 1080x1440 / 3:4 / 30FPS / 1.0x / 原声" if legacy_engine_online else "等待原引擎报告",
        "agent_version": "v2",
        "deployment_root": str(ROOT),
        "database": DATABASE.is_file(),
        "raw_video_directory": RAW_VIDEO_DIR.is_dir(),
        "pose_model": POSE_MODEL.is_file(),
        "legacy_memory": (LEGACY_ROOT / "便携数据" / "runtime" / "memory" / "suchen_memory.sqlite3").is_file(),
        "legacy_runs_imported": legacy_run_count,
        "editing_engine": "suchen-1.2.3-original",
        "editing_method_preserved": True,
        "legacy_engine_online": legacy_engine_online,
        "worker_capacity": 1,
    }
    if PUBLIC_MODE and not request.session.get("suchen_user"):
        return {"status": payload["status"], "service": payload["service"]}
    return payload


@app.get("/api/knowledge")
def list_knowledge(
    q: str = Query(default="", max_length=100),
    category: KnowledgeCategory | Literal["all"] = "all",
    enabled: Literal["all", "true", "false"] = "all",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    all_items = _knowledge_entries()
    items = all_items
    needle = q.strip().casefold()
    if category != "all":
        items = [item for item in items if item["category"] == category]
    if needle:
        items = [
            item
            for item in items
            if needle in f"{item['title']} {item['content']}".casefold()
        ]
    if enabled != "all":
        expected = enabled == "true"
        items = [item for item in items if item["enabled"] is expected]
    filtered_total = len(items)
    start = (page - 1) * page_size
    items = items[start : start + page_size]
    active_items = [item for item in all_items if item["enabled"]]
    contract = _compile_knowledge_contract(active_items)
    engine_status: dict = {}
    try:
        client = SuchenEngineClient(
            os.getenv("FULIN_SUCHEN_ENGINE_URL", "http://127.0.0.1:5000"),
            timeout=1.5,
        )
        engine_status = client.request_json("GET", "/api/status")
    except Exception:
        pass
    checks = _installed_engine_checks()
    category_counts = {
        key: sum(1 for item in all_items if item["category"] == key)
        for key in (
            "editing_sop",
            "body_action",
            "spoken_content",
            "quality_gate",
            "product_rule",
        )
    }
    return {
        "items": items,
        "total": filtered_total,
        "all_total": len(all_items),
        "active": len(active_items),
        "page": page,
        "page_size": page_size,
        "pages": max(1, (filtered_total + page_size - 1) // page_size),
        "category_counts": category_counts,
        "updated_at": max((item["updated_at"] for item in all_items), default=None),
        "contract": contract,
        "system": {
            "engine_online": bool(engine_status),
            "editing_engine": "SOCHEN 1.2.3 原剪辑引擎",
            "speech_recognition": bool(engine_status) and bool(checks.get("asr_model")),
            "body_recognition": bool(engine_status) and bool(checks.get("pose_model")),
            "text_action_fusion": bool(engine_status) and bool(active_items),
            "continuity_gate": bool(contract.get("continuity_gate_locked")),
            "database": describe_database()["backend"],
            "database_target": describe_database()["target"],
            "database_online": True,
            "maximum_output_seconds": 120,
        },
    }


@app.post("/api/knowledge", status_code=201)
def create_knowledge(http_request: Request, payload: KnowledgeEntryCreate) -> dict:
    _require_admin(http_request)
    _ensure_schema()
    now = _utc_now()
    entry_id = uuid4().hex
    connection = _connect()
    try:
        connection.execute(
            """
            INSERT INTO knowledge_entries (
                id, category, title, content, priority, enabled, protected,
                source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, 0, 'manual', ?, ?)
            """,
            (
                entry_id,
                payload.category,
                payload.title.strip(),
                payload.content.strip(),
                payload.priority,
                now,
                now,
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM knowledge_entries WHERE id = ?", (entry_id,)
        ).fetchone()
    finally:
        connection.close()
    return _knowledge_dict(row)


@app.patch("/api/knowledge/{entry_id}")
def update_knowledge(entry_id: str, http_request: Request, payload: KnowledgeEntryUpdate) -> dict:
    _require_admin(http_request)
    _ensure_schema()
    values = payload.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="没有需要保存的修改")
    if "enabled" in values:
        values["enabled"] = int(bool(values["enabled"]))
    values["updated_at"] = _utc_now()
    columns = ", ".join(f"{key} = ?" for key in values)
    connection = _connect()
    try:
        cursor = connection.execute(
            f"UPDATE knowledge_entries SET {columns} WHERE id = ?",
            (*values.values(), entry_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="知识条目不存在")
        connection.commit()
        row = connection.execute(
            "SELECT * FROM knowledge_entries WHERE id = ?", (entry_id,)
        ).fetchone()
    finally:
        connection.close()
    return _knowledge_dict(row)


@app.delete("/api/knowledge/{entry_id}", status_code=204)
def delete_knowledge(entry_id: str, http_request: Request) -> None:
    _require_admin(http_request)
    _ensure_schema()
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT protected FROM knowledge_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="知识条目不存在")
        if row["protected"]:
            raise HTTPException(status_code=409, detail="系统核心规则不能删除，可以暂时停用")
        connection.execute("DELETE FROM knowledge_entries WHERE id = ?", (entry_id,))
        connection.commit()
    finally:
        connection.close()


@app.post("/api/knowledge/apply")
def apply_knowledge(request: Request) -> dict:
    _require_admin(request)
    try:
        contract = _apply_knowledge_contract()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"规则应用失败：{exc}") from exc
    return {"ok": True, "contract": contract, "message": "知识库规则已应用到 SOCHEN 原剪辑引擎"}


@app.get("/api/source-videos")
def source_videos() -> dict:
    items: list[dict] = []
    for origin, directory in (
        ("library", RAW_VIDEO_DIR),
        ("upload", UPLOAD_DIR),
        ("legacy", LEGACY_UPLOAD_DIR),
    ):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES:
                stat = path.stat()
                items.append(
                    {
                        "name": path.name,
                        "origin": origin,
                        "size_bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    }
                )
    items.sort(key=lambda item: item["modified_at"], reverse=True)
    return {"items": items}


@app.post("/api/uploads", status_code=201)
def upload_video(file: Annotated[UploadFile, File(...)]) -> dict:
    filename = Path(file.filename or "").name
    if not filename or Path(filename).suffix.lower() not in VIDEO_SUFFIXES:
        raise HTTPException(status_code=415, detail="仅支持 MP4、MOV、MKV、AVI、M4V、WebM 视频")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    destination = _safe_child(UPLOAD_DIR, filename)
    if destination.exists():
        destination = destination.with_name(
            f"{destination.stem}-{uuid4().hex[:8]}{destination.suffix.lower()}"
        )
    size = 0
    try:
        with destination.open("wb") as target:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="单个视频不能超过 2 GB")
                target.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        file.file.close()
    return {"name": destination.name, "origin": "upload", "size_bytes": size}


def _enqueue_job(
    source_name: str,
    source_origin: Literal["library", "upload", "legacy"],
    product_type: RequestedProductType,
    target_duration: float | None,
    *,
    agent_mode: bool = False,
) -> dict:
    directory = {
        "library": RAW_VIDEO_DIR,
        "upload": UPLOAD_DIR,
        "legacy": LEGACY_UPLOAD_DIR,
    }[source_origin]
    source = _safe_child(directory, source_name)
    if not source.is_file() or source.suffix.lower() not in VIDEO_SUFFIXES:
        raise HTTPException(status_code=404, detail="原视频不存在或格式不支持")
    connection = _connect()
    try:
        duplicate = connection.execute(
            """
            SELECT id FROM web_jobs
            WHERE source_path = ? AND status IN ('queued', 'running')
            ORDER BY created_at DESC LIMIT 1
            """,
            (str(source),),
        ).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail="该视频已有进行中的剪辑任务")
        job_id = uuid4().hex
        now = _utc_now()
        initial_target = target_duration or LEGACY_PROFILES[product_type][2]
        output = OUTPUT_DIR / f"{source.stem}_{job_id[:8]}_full_auto.mp4"
        connection.execute(
            """
            INSERT INTO web_jobs (
                id, source_name, source_path, status, stage, product_type, target_duration,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'queued', 'queued', ?, ?, ?, ?)
            """,
            (
                job_id,
                source.name,
                str(source),
                product_type,
                initial_target,
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    executor.submit(
        _run_agent_job if agent_mode else _run_job,
        job_id,
        source,
        output,
        target_duration,
        product_type,
    )
    return _read_job(job_id)


@app.post("/api/jobs", status_code=202)
def create_job(request: CreateJobRequest) -> dict:
    _ensure_schema()
    return _enqueue_job(
        request.source_name,
        request.source_origin,
        request.product_type,
        request.target_duration,
    )


@app.post("/api/agent/jobs", status_code=202)
def create_agent_job(request: CreateJobRequest) -> dict:
    """Run the real tool-using Agent; the ordinary endpoint keeps legacy compatibility."""

    _ensure_schema()
    return _enqueue_job(
        request.source_name,
        request.source_origin,
        request.product_type,
        request.target_duration,
        agent_mode=True,
    )


@app.post("/api/jobs/batch", status_code=202)
def create_batch_jobs(request: BatchCreateJobRequest) -> dict:
    _ensure_schema()
    items: list[dict] = []
    errors: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for source in request.sources:
        key = (source.origin, source.name.casefold())
        if key in seen:
            continue
        seen.add(key)
        try:
            items.append(
                _enqueue_job(
                    source.name,
                    source.origin,
                    request.product_type,
                    request.target_duration,
                )
            )
        except HTTPException as exc:
            errors.append({"name": source.name, "reason": str(exc.detail)})
    return {"items": items, "errors": errors, "created": len(items)}


@app.post("/api/agent/jobs/batch", status_code=202)
def create_agent_batch_jobs(request: BatchCreateJobRequest) -> dict:
    """Queue a bounded Agent run for every source in the requested batch."""

    _ensure_schema()
    items: list[dict] = []
    errors: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for source in request.sources:
        key = (source.origin, source.name.casefold())
        if key in seen:
            continue
        seen.add(key)
        try:
            items.append(
                _enqueue_job(
                    source.name,
                    source.origin,
                    request.product_type,
                    request.target_duration,
                    agent_mode=True,
                )
            )
        except HTTPException as exc:
            errors.append({"name": source.name, "reason": str(exc.detail)})
    return {"items": items, "errors": errors, "created": len(items), "mode": "agent"}


@app.get("/api/jobs")
def list_jobs(
    q: str = Query(default="", max_length=100),
    status: Literal["all", "active", "approved", "needs_review", "failed"] = "all",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=8, ge=1, le=50),
) -> dict:
    _ensure_schema()
    clauses: list[str] = []
    values: list[object] = []
    if q.strip():
        clauses.append("source_name LIKE ? ESCAPE '\\'")
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        values.append(f"%{escaped}%")
    if status == "active":
        clauses.append("status IN ('queued', 'running')")
    elif status != "all":
        clauses.append("status = ?")
        values.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    connection = _connect()
    try:
        total = connection.execute(
            f"SELECT COUNT(*) FROM web_jobs {where}", values
        ).fetchone()[0]
        rows = connection.execute(
            f"SELECT * FROM web_jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*values, page_size, (page - 1) * page_size),
        ).fetchall()
    finally:
        connection.close()
    return {
        "items": [_job_dict(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    return _read_job(job_id)


def _decode_json(value: object, fallback: object) -> object:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


@app.get("/api/jobs/{job_id}/agent-trace")
def get_agent_trace(job_id: str) -> dict:
    _read_job(job_id)
    connection = _connect()
    try:
        run = connection.execute(
            "SELECT * FROM agent_runs WHERE job_id=? ORDER BY started_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if run is None:
            return {"job_id": job_id, "run": None, "events": [], "plans": [], "quality": [], "feedback": []}
        run_payload = dict(run)
        events = []
        for row in connection.execute(
            "SELECT * FROM agent_events WHERE run_id=? ORDER BY id", (run["id"],)
        ).fetchall():
            item = dict(row)
            item["input_summary"] = _decode_json(item.pop("input_summary_json", "{}"), {})
            item["output_summary"] = _decode_json(item.pop("output_summary_json", "{}"), {})
            events.append(item)
        plans = []
        for row in connection.execute(
            "SELECT * FROM agent_plan_versions WHERE run_id=? ORDER BY version", (run["id"],)
        ).fetchall():
            item = dict(row)
            item["plan"] = _decode_json(item.pop("plan_json", "{}"), {})
            plans.append(item)
        quality = []
        for row in connection.execute(
            "SELECT * FROM agent_quality_results WHERE run_id=? ORDER BY attempt", (run["id"],)
        ).fetchall():
            item = dict(row)
            item["passed"] = bool(item["passed"])
            item["quality"] = _decode_json(item.pop("quality_json", "{}"), {})
            quality.append(item)
        feedback = []
        for row in connection.execute(
            "SELECT id, verdict, issue_types_json, corrected_timeline_json, note, created_at FROM human_feedback WHERE job_id=? ORDER BY created_at DESC",
            (job_id,),
        ).fetchall():
            item = dict(row)
            item["issue_types"] = _decode_json(item.pop("issue_types_json", "[]"), [])
            item["corrected_timeline"] = _decode_json(item.pop("corrected_timeline_json", "[]"), [])
            feedback.append(item)
    finally:
        connection.close()
    return {
        "job_id": job_id,
        "run": run_payload,
        "events": events,
        "plans": plans,
        "quality": quality,
        "feedback": feedback,
    }


@app.post("/api/jobs/{job_id}/feedback", status_code=201)
def save_job_feedback(job_id: str, payload: HumanFeedbackRequest, request: Request) -> dict:
    user = _require_admin(request)
    _read_job(job_id)
    allowed_issues = {
        "missing_size",
        "missing_fabric",
        "missing_color",
        "missing_back",
        "missing_return",
        "flash",
        "view_jump",
        "audio_cut",
        "wrong_duration",
        "wrong_framing",
        "other",
    }
    issue_types = list(dict.fromkeys(payload.issue_types))
    if any(item not in allowed_issues for item in issue_types):
        raise HTTPException(status_code=422, detail="问题类型无效")
    previous_end = -1.0
    cleaned_timeline: list[dict] = []
    for index, item in enumerate(payload.corrected_timeline):
        try:
            start = round(float(item["source_start"]), 3)
            end = round(float(item["source_end"]), 3)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"第 {index + 1} 个人工切点格式无效") from exc
        if start < 0 or end <= start or start < previous_end:
            raise HTTPException(status_code=422, detail="人工切点必须非负、正序且不能重叠")
        previous_end = end
        cleaned_timeline.append(
            {
                "stage": str(item.get("stage", "manual")).strip()[:40] or "manual",
                "source_start": start,
                "source_end": end,
            }
        )
    feedback_id = uuid4().hex
    connection = _connect()
    try:
        connection.execute(
            """
            INSERT INTO human_feedback (
                id, job_id, user_id, verdict, issue_types_json,
                corrected_timeline_json, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback_id,
                job_id,
                str(user["id"]),
                payload.verdict,
                _json_object(issue_types),
                _json_object(cleaned_timeline),
                payload.note.strip(),
                _utc_now(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "id": feedback_id,
        "job_id": job_id,
        "verdict": payload.verdict,
        "issue_types": issue_types,
        "corrected_timeline": cleaned_timeline,
        "message": "负责人反馈已保存；只有明确合格的记录才可进入合格案例候选库",
    }


@app.get("/api/jobs/{job_id}/media")
def get_job_media(job_id: str) -> FileResponse:
    job = _read_job(job_id)
    if not job.get("output_path"):
        raise HTTPException(status_code=409, detail="成片尚未生成")
    path = Path(job["output_path"]).resolve()
    try:
        path.relative_to(OUTPUT_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="成片路径不受信任") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="成片文件不存在")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/architecture")
def architecture() -> dict:
    return {
        "active": [
            {"name": "SOCHEN 内嵌 HTML/CSS/JavaScript", "layer": "前端", "detail": "原工作台、知识库、任务状态和成片审核统一入口"},
            {"name": "Python + FastAPI", "layer": "AI 后端", "detail": "任务编排、知识库与 SOCHEN 原引擎桥接"},
            {"name": "SQLite", "layer": "数据库", "detail": "新任务与 suchen 590 条历史记录统一持久化"},
            {"name": "SOCHEN 1.2.3 原剪辑引擎", "layer": "剪辑核心", "detail": "保留文件1原剪辑方式，网站只负责提交、记录与展示"},
            {"name": "suchen 1.2.3 数据桥", "layer": "旧系统整合", "detail": "素材目录、运行记录、复核结论与规则快照"},
            {"name": "faster-whisper", "layer": "语音识别", "detail": "主播口播与尺码语义识别"},
            {"name": "MediaPipe + OpenCV", "layer": "视觉识别", "detail": "人物、服装、转身与画面构图"},
            {"name": "FFmpeg", "layer": "渲染", "detail": "3:4 合成、稳定点直接切换、原声音画输出"},
            {"name": "本地指纹缓存", "layer": "支持库", "detail": "ASR 与视觉结果复用，加快重复处理"},
            {"name": "品类 SOP 策略引擎", "layer": "AI Agent", "detail": "单品、套装、大货分别执行时长与内容硬约束"},
            {"name": "Agent 可审计运行轨迹", "layer": "AI Agent", "detail": "记录状态、工具摘要、计划版本、质检结果与负责人反馈"},
            {"name": "suchen 1.2.3 记忆桥", "layer": "历史数据", "detail": "只读汇总旧运行、复核与动作模板，隔离未验证样本"},
        ],
        "planned": [
            {"name": "Java + Spring Boot", "layer": "企业后端", "detail": "账号、权限、跨部门业务网关"},
            {"name": "PostgreSQL / MySQL", "layer": "生产数据库", "detail": "多用户与生产任务扩容"},
            {"name": "Redis + 任务队列", "layer": "调度", "detail": "多机器并发、重试与排队"},
            {"name": "Dify + RAG", "layer": "知识库", "detail": "尺码话术、商品卖点和 SOP 检索"},
            {"name": "MCP", "layer": "工具协议", "detail": "连接商品库、审核台与发布工具"},
            {"name": "剪映工程导出", "layer": "人工复核", "detail": "将自动成片转为可继续精修的草稿"},
            {"name": "失败项驱动自动重剪", "layer": "AI Agent", "detail": "只替换失败阶段并限制最多两轮；完成候选池映射后接入"},
        ],
    }


def _legacy_runs(limit: int = 12) -> list[dict]:
    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT source_run_id, created_at, source_name, product_type, product_label,
                   target_duration, actual_duration, decision_status, publish_status,
                   confidence, elapsed_seconds, ok, quality_pass, is_fallback, error,
                   review_status, review_score, review_note, source_available, output_available
            FROM legacy_edit_runs ORDER BY source_run_id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _snapshot_legacy_assets(database: Path) -> tuple[dict[str, int], int]:
    snapshot = DATABASE.parent / "suchen_memory_snapshot.sqlite3"
    temporary = snapshot.with_suffix(".sqlite3.tmp")
    temporary.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=5)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
        tables = [
            row[0]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        counts = {
            table: source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }
    finally:
        target.close()
        source.close()
    temporary.replace(snapshot)
    settings_directory = DATABASE.parent / "suchen_settings"
    settings_directory.mkdir(parents=True, exist_ok=True)
    settings_count = 0
    for name in ("action_template.json", "manual_edit_profiles.json"):
        source_file = LEGACY_ROOT / "便携数据" / "runtime" / "models" / name
        if source_file.is_file():
            shutil.copy2(source_file, settings_directory / name)
            settings_count += 1
    return counts, settings_count


def _sync_legacy_runs(database: Path) -> int:
    legacy = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro", uri=True, timeout=5
    )
    legacy.row_factory = sqlite3.Row
    try:
        rows = legacy.execute(
            """
            SELECT r.id, r.run_uuid, r.created_at, r.source_name, r.source_path,
                   r.output_path, r.product_type, r.product_label, r.target_duration,
                   r.actual_duration, r.decision_status, r.publish_status, r.confidence,
                   r.elapsed_seconds, r.ok, r.quality_pass, r.is_fallback, r.error,
                   v.status AS review_status, v.score AS review_score, v.note AS review_note
            FROM edit_runs r
            LEFT JOIN reviews v ON v.id = (
                SELECT v2.id FROM reviews v2 WHERE v2.run_id = r.id
                ORDER BY v2.updated_at DESC, v2.id DESC LIMIT 1
            )
            ORDER BY r.id
            """
        ).fetchall()
    finally:
        legacy.close()
    synced_at = _utc_now()
    target = _connect()
    try:
        target.execute("DELETE FROM legacy_edit_runs")
        target.executemany(
            """
            INSERT INTO legacy_edit_runs (
                source_run_id, run_uuid, created_at, source_name, product_type,
                product_label, target_duration, actual_duration, decision_status,
                publish_status, confidence, elapsed_seconds, ok, quality_pass,
                is_fallback, error, review_status, review_score, review_note,
                source_available, output_available, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["id"], row["run_uuid"], row["created_at"], row["source_name"],
                    row["product_type"], row["product_label"], row["target_duration"],
                    row["actual_duration"], row["decision_status"], row["publish_status"],
                    row["confidence"], row["elapsed_seconds"], row["ok"],
                    row["quality_pass"], row["is_fallback"], row["error"],
                    row["review_status"], row["review_score"], row["review_note"],
                    int(bool(row["source_path"] and Path(row["source_path"]).is_file())),
                    int(bool(row["output_path"] and Path(row["output_path"]).is_file())),
                    synced_at,
                )
                for row in rows
            ],
        )
        target.commit()
    finally:
        target.close()
    return len(rows)


def _collect_legacy_evidence() -> dict:
    result = {
        "available": False,
        "version": None,
        "source_path": str(LEGACY_ROOT),
        "uploads": 0,
        "finished_outputs": 0,
        "memory_runs": 0,
        "quality_pass_runs": 0,
        "fallback_runs": 0,
        "learnable_runs": 0,
        "reviews": 0,
        "rejected_reviews": 0,
        "profile_samples": 0,
        "matched_gold_pairs": 0,
        "imported_runs": 0,
        "database_tables": {},
        "settings_synced": 0,
        "recent_runs": [],
        "conflicts": [],
        "policies": policy_catalog(),
    }
    database = LEGACY_ROOT / "便携数据" / "runtime" / "memory" / "suchen_memory.sqlite3"
    profile_path = LEGACY_ROOT / "便携数据" / "runtime" / "models" / "manual_edit_profiles.json"
    action_path = LEGACY_ROOT / "便携数据" / "runtime" / "models" / "action_template.json"
    if not database.is_file():
        return result
    result["available"] = True
    version_path = LEGACY_ROOT / "版本.txt"
    if version_path.is_file():
        result["version"] = version_path.read_text(encoding="utf-8-sig").splitlines()[0]
    result["uploads"] = len(list((LEGACY_ROOT / "便携数据" / "runtime" / "uploads").glob("*.mp4")))
    result["finished_outputs"] = len(
        list((LEGACY_ROOT / "便携数据" / "outputs" / "suchen" / "成品").glob("*.mp4"))
    )
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(quality_pass), 0), COALESCE(SUM(is_fallback), 0),
                   COALESCE(SUM(CASE WHEN ok=1 AND quality_pass=1 AND is_fallback=0 THEN 1 ELSE 0 END), 0)
            FROM edit_runs
            """
        ).fetchone()
        result["memory_runs"], result["quality_pass_runs"], result["fallback_runs"], result["learnable_runs"] = row
        review_row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END), 0) FROM reviews"
        ).fetchone()
        result["reviews"], result["rejected_reviews"] = review_row
    finally:
        connection.close()
    if profile_path.is_file():
        profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
        result["profile_samples"] = int(profile.get("sample_count", 0))
        result["matched_gold_pairs"] = int(profile.get("matched_count", 0))
    if action_path.is_file():
        action = json.loads(action_path.read_text(encoding="utf-8-sig"))
        if (action.get("modes") or {}).get("move_to_center_product_detail") != "required":
            result["conflicts"].append("旧模板未强制‘走到中央介绍商品’，已按当前 SOP 覆盖为 HARD")
    if result["matched_gold_pairs"] == 0:
        result["conflicts"].append("旧画像没有已验证的原片—人工成片配对，不能直接作为金标准学习")
    if result["rejected_reviews"]:
        result["conflicts"].append("旧库现有人工复核均为拒绝，拒绝记录只用于反例")
    return result


def _sync_legacy_evidence() -> dict:
    legacy_database = LEGACY_ROOT / "便携数据" / "runtime" / "memory" / "suchen_memory.sqlite3"
    table_counts, settings_synced = (
        _snapshot_legacy_assets(legacy_database) if legacy_database.is_file() else ({}, 0)
    )
    imported_runs = _sync_legacy_runs(legacy_database) if legacy_database.is_file() else 0
    evidence = _collect_legacy_evidence()
    evidence["imported_runs"] = imported_runs
    evidence["database_tables"] = table_counts
    evidence["settings_synced"] = settings_synced
    evidence["recent_runs"] = _legacy_runs()
    connection = _connect()
    try:
        connection.execute(
            """
            INSERT OR REPLACE INTO legacy_evidence_snapshots (source_path, snapshot_json, synced_at)
            VALUES (?, ?, ?)
            """,
            (str(LEGACY_ROOT), json.dumps(evidence, ensure_ascii=False), _utc_now()),
        )
        connection.commit()
    finally:
        connection.close()
    return evidence


@app.get("/api/legacy-evidence")
def legacy_evidence(refresh: bool = False) -> dict:
    _ensure_schema()
    if refresh:
        return _sync_legacy_evidence()
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT snapshot_json, synced_at FROM legacy_evidence_snapshots WHERE source_path = ?",
            (str(LEGACY_ROOT),),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return _sync_legacy_evidence()
    payload = json.loads(row["snapshot_json"])
    payload["synced_at"] = row["synced_at"]
    return payload


@app.get("/api/legacy-runs")
def legacy_runs(limit: int = Query(default=30, ge=1, le=100)) -> dict:
    _ensure_schema()
    connection = _connect()
    try:
        total = connection.execute("SELECT COUNT(*) FROM legacy_edit_runs").fetchone()[0]
    finally:
        connection.close()
    return {"items": _legacy_runs(limit), "total": total}


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


def _read_engine_log(path: Path, offset: int | None) -> tuple[int, bytes]:
    """Read a bounded log increment without occupying a legacy HTTP worker."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            if offset is None:
                return size, b""
            handle.seek(offset if offset <= size else 0)
            data = handle.read(65536)
            return handle.tell(), data
    except FileNotFoundError:
        return 0, b""


@app.get("/stream", include_in_schema=False)
async def stream_engine_log(request: Request):
    log_path = LEGACY_ROOT / "便携数据" / "runtime" / "logs" / "suchen.log"

    async def events():
        offset, _ = await asyncio.to_thread(_read_engine_log, log_path, None)
        pending = b""
        yield ": connected\n\n"
        while not await request.is_disconnected():
            try:
                offset, data = await asyncio.to_thread(_read_engine_log, log_path, offset)
                pending += data
                lines = pending.split(b"\n")
                pending = lines.pop()
                for line in lines:
                    yield "data: " + line.decode("utf-8", errors="replace").rstrip("\r") + "\n\n"
                if len(pending) > 65536:
                    yield "data: " + pending.decode("utf-8", errors="replace") + "\n\n"
                    pending = b""
            except OSError:
                yield ": log temporarily unavailable\n\n"
            yield ": ping\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _close_proxy_response(response: httpx.Response, client: httpx.AsyncClient) -> None:
    await response.aclose()
    await client.aclose()


@app.api_route("/", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.api_route("/{proxy_path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_suchen(request: Request, proxy_path: str = "") -> StreamingResponse:
    """Expose the unchanged SOCHEN UI/API through the merged server origin.

    Exact FastAPI routes registered above keep ownership of the AI knowledge,
    job, and health APIs. Everything else is streamed to the local SOCHEN
    engine so remote browsers still see one application and one origin.
    """

    upstream_base = os.getenv("FULIN_SUCHEN_ENGINE_URL", "http://127.0.0.1:5000").rstrip("/")
    upstream_url = f"{upstream_base}/{proxy_path}"
    request_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() != "host"
    }
    if PUBLIC_MODE:
        external_host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        request_headers["host"] = external_host
        request_headers["x-forwarded-proto"] = request.headers.get("x-forwarded-proto", "https")
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=5.0, write=300.0), follow_redirects=False
    )
    try:
        upstream_request = client.build_request(
            request.method,
            upstream_url,
            params=request.query_params.multi_items(),
            headers=request_headers,
            content=None if request.method in {"GET", "HEAD"} else request.stream(),
        )
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail="SOCHEN 原剪辑引擎未连接") from exc
    except BaseException:
        await client.aclose()
        raise
    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    }
    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        background=BackgroundTask(_close_proxy_response, upstream_response, client),
    )


def main() -> None:
    import uvicorn

    uvicorn.run("fulin_editor.web_api:app", host="127.0.0.1", port=8000, reload=False)
