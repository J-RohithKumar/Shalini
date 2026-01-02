
# backend/run_service.py
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from typing import Optional
import os
import logging
import uuid

from dotenv import load_dotenv  # NEW: load .env so PG_* are available

from .executor import run_code

app = FastAPI(title="Multi-language Compiler Web App", version="1.1.0")

# -------------------- Logging --------------------
logger = logging.getLogger("run_service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# -------------------- Load .env from project root --------------------
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
env_path = os.path.join(project_root, ".env")
if os.path.exists(env_path):
    load_dotenv(env_path)
else:
    logger.warning(".env not found at %s; relying on process env", env_path)

# -------------------- Security headers --------------------
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response

# -------------------- CORS (allow local dev) --------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=600,
)

# -------------------- Static frontend dir --------------------
frontend_dir = os.path.join(project_root, "frontend")
if not os.path.isdir(frontend_dir):
    logger.warning("frontend directory not found at %s", frontend_dir)

# -------------------- Models --------------------
class RunRequest(BaseModel):
    language: str = Field(..., description="python | java | javascript | postgres | postgresql")
    code: str = Field(..., description="Program source code")
    input: Optional[str] = Field(None, description="Optional stdin input")

class RunResponse(BaseModel):
    ok: bool
    stdout: str
    stderr: str
    exit_code: int
    runtime_ms: int
    request_id: str

# 👇 Add postgres/postgresql
SUPPORTED = {"python", "java", "javascript", "postgres", "postgresql"}

MAX_CODE_BYTES = 500_000
MAX_STDIN_BYTES = 200_000

def normalize_language(lang: str) -> str:
    """Map UI labels to executor tokens."""
    s = (lang or "").strip().lower()
    if s == "postgresql":
        return "postgres"
    if s == "javascript (node)":
        return "javascript"
    return s

def validate_request(req: RunRequest):
    # Normalize BEFORE validation to avoid 'Unsupported language' on synonyms like PostgreSQL
    lang_norm = normalize_language(req.language)
    if lang_norm not in SUPPORTED:
        raise HTTPException(status_code=400, detail=f"Unsupported language: {req.language}")

    code_bytes = (req.code or "").encode("utf-8")
    if len(code_bytes) > MAX_CODE_BYTES:
        raise HTTPException(status_code=413, detail=f"Code too large (> {MAX_CODE_BYTES} bytes)")

    if req.input:
        stdin_bytes = req.input.encode("utf-8")
        if len(stdin_bytes) > MAX_STDIN_BYTES:
            raise HTTPException(status_code=413, detail=f"stdin too large (> {MAX_STDIN_BYTES} bytes)")

# -------------------- Routes --------------------
@app.post("/api/run", response_model=RunResponse)
async def api_run(req: RunRequest, request: Request):
    validate_request(req)
    lang = normalize_language(req.language)

    request_id = uuid.uuid4().hex[:12]
    logger.info("incoming req_id=%s raw_lang=%s normalized_lang=%s", request_id, req.language, lang)

    try:
        ok, stdout, stderr, exit_code, runtime_ms = await run_in_threadpool(
            run_code, lang, req.code, req.input
        )
    except Exception as e:
        logger.exception("Executor crashed (req_id=%s): %s", request_id, e)
        raise HTTPException(status_code=500, detail="Executor error")

    if not ok and isinstance(stderr, str) and "Unsupported language" in stderr:
        raise HTTPException(status_code=400, detail=stderr)

    logger.info(
        "req_id=%s method=%s path=%s lang=%s exit=%s ok=%s runtime_ms=%s ua=%s",
        request_id,
        request.method,
        request.url.path,
        lang,
        exit_code,
        ok,
        runtime_ms,
        request.headers.get("user-agent", "-"),
    )

    return RunResponse(
        ok=ok,
        stdout=stdout or "",
        stderr=stderr or "",
        exit_code=exit_code,
        runtime_ms=runtime_ms,
        request_id=request_id,
    )

@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})

@app.get("/version")
async def version():
    return JSONResponse({"name": "Multi-language Compiler Web App", "version": "1.1.0"})

# Mount static AFTER API routes so /api/run isn’t shadowed
app.mount("/", StaticFiles(directory=frontend_dir, html=True, check_dir=False), name="static")

