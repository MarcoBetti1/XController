from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .lab import ControllerLabManager


if os.name == "nt":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass


manager = ControllerLabManager()


class SessionStartRequest(BaseModel):
    profile_path: str = Field(min_length=1)
    account_handle: str | None = None


class ActionRunRequest(BaseModel):
    action: str = Field(min_length=1)
    args: dict[str, Any] | None = None


class WalkthroughStartRequest(BaseModel):
    search_query: str = "unpopular opinion"
    timeline_limit: int = 12
    cooldown_min_seconds: int = 8
    cooldown_max_seconds: int = 18
    reply_text: str = "Walkthrough reply check."
    post_text: str = ""
    include_eval: bool = True
    eval_limit: int = 12


def _profiles_base() -> Path:
    return (Path.cwd() / "data" / "profiles").resolve()


def _list_profiles() -> list[dict[str, str]]:
    base = _profiles_base()
    rows: list[dict[str, str]] = []
    if not base.exists():
        return rows
    for item in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not item.is_dir():
            continue
        rows.append({"name": item.name, "path": str(item.resolve())})
    return rows


def _raise_http(exc: Exception) -> None:
    msg = str(exc)
    if msg in {
        "profile_path_required",
        "action_required",
        "query_required",
        "post_id_required",
        "text_required",
        "username_required",
    }:
        raise HTTPException(status_code=400, detail=msg)
    if msg.startswith("unknown_action:"):
        raise HTTPException(status_code=400, detail=msg)
    if msg in {"session_not_started", "walkthrough_running"}:
        raise HTTPException(status_code=409, detail=msg)
    if msg in {"login_required", "no_posts_found", "view_failed", "like_failed", "reply_failed", "post_failed"}:
        raise HTTPException(status_code=422, detail=msg)
    if "profile_in_use" in msg:
        raise HTTPException(status_code=409, detail="profile_in_use")
    if "target_page_or_context_closed" in msg or "playwright_driver_connection_closed" in msg:
        raise HTTPException(status_code=503, detail=msg)
    raise HTTPException(status_code=500, detail=msg)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await manager.stop_session()


app = FastAPI(title="X Controller Lab API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/profiles")
async def profiles() -> dict[str, Any]:
    base = _profiles_base()
    return {
        "base": str(base),
        "profiles": _list_profiles(),
    }


@app.get("/session")
async def session_status() -> dict[str, Any]:
    return manager.session_status()


@app.post("/session/start")
async def start_session(payload: SessionStartRequest) -> dict[str, Any]:
    try:
        return await manager.start_session(payload.profile_path, payload.account_handle)
    except Exception as exc:
        _raise_http(exc)


@app.post("/session/stop")
async def stop_session() -> dict[str, Any]:
    try:
        return await manager.stop_session()
    except Exception as exc:
        _raise_http(exc)


@app.post("/session/open-login")
async def open_login() -> dict[str, Any]:
    try:
        return await manager.open_login()
    except Exception as exc:
        _raise_http(exc)


@app.post("/session/refresh-login")
async def refresh_login() -> dict[str, Any]:
    try:
        return await manager.refresh_login_status()
    except Exception as exc:
        _raise_http(exc)


@app.post("/action/run")
async def run_action(payload: ActionRunRequest) -> dict[str, Any]:
    try:
        return await manager.run_action(payload.action, payload.args or {})
    except Exception as exc:
        _raise_http(exc)


@app.get("/walkthrough/status")
async def walkthrough_status() -> dict[str, Any]:
    return manager.walkthrough_status()


@app.post("/walkthrough/start")
async def start_walkthrough(payload: WalkthroughStartRequest) -> dict[str, Any]:
    try:
        return await manager.start_walkthrough(
            search_query=payload.search_query,
            timeline_limit=payload.timeline_limit,
            cooldown_min_seconds=payload.cooldown_min_seconds,
            cooldown_max_seconds=payload.cooldown_max_seconds,
            reply_text=payload.reply_text,
            post_text=payload.post_text,
            include_eval=payload.include_eval,
            eval_limit=payload.eval_limit,
        )
    except Exception as exc:
        _raise_http(exc)


@app.post("/walkthrough/stop")
async def stop_walkthrough() -> dict[str, Any]:
    try:
        return await manager.stop_walkthrough()
    except Exception as exc:
        _raise_http(exc)


@app.get("/logs/lab", response_class=PlainTextResponse)
async def lab_log(lines: int = 300):
    count = max(20, min(abs(int(lines)), 4000))
    log_path = (Path.cwd() / "logs" / "x_controller_lab.log").resolve()
    if not log_path.exists():
        return "No x_controller lab log yet."
    with log_path.open("r", encoding="utf-8") as fp:
        rows = fp.readlines()
    return "".join(rows[-count:])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("X_CONTROLLER_LAB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("X_CONTROLLER_LAB_PORT", "8010")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "x_controller.lab_api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
