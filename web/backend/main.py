#!/usr/bin/env python3
"""FastAPI entry for facing-reply eval closed-loop Web UI."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root (sibling of web/) is on sys.path for loop_* imports
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from loop_ctl import list_loops
from loop_lib import CONFIG_FILE

from .orchestrator import (
    build_loop_detail,
    do_adopt_and_advance,
    do_annotate,
    do_decide,
    do_optimize,
    get_pe,
    list_agents,
    list_datasets,
    list_scorers,
    load_report,
    read_eval_preview,
    resume_advance,
    save_draft,
    start_and_advance,
)
from .schemas import (
    AnnotateRequest,
    DecideRequest,
    DraftUpdate,
    OptimizeRequest,
    ResumeRequest,
    StartLoopRequest,
)

app = FastAPI(title="面谈回复评测闭环", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8765",
        "http://127.0.0.1:8765",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "config_exists": CONFIG_FILE.exists(),
        "root": str(_ROOT),
    }


@app.get("/api/datasets")
def api_datasets():
    return {"ok": True, "data": list_datasets()}


@app.get("/api/agents")
def api_agents():
    return {"ok": True, "data": list_agents()}


@app.get("/api/scorers")
def api_scorers():
    return {"ok": True, "data": list_scorers()}


@app.get("/api/loops")
def api_list_loops():
    return {"ok": True, "data": list_loops()}


@app.post("/api/loops")
def api_start_loop(body: StartLoopRequest):
    try:
        result = start_and_advance(
            dataset=body.dataset,
            scenes=body.scenes,
            items=body.items,
            pe_file=body.pe_file,
            ignore_fail=body.ignore_fail,
            agent_id=body.agent_id,
            scorer_id=body.scorer_id,
        )
        return {"ok": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/loops/{loop_id}")
def api_get_loop(loop_id: str):
    try:
        return {"ok": True, "data": build_loop_detail(loop_id)}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/loops/{loop_id}/resume")
def api_resume(loop_id: str, body: ResumeRequest = Body(default_factory=ResumeRequest)):
    try:
        return {
            "ok": True,
            "data": resume_advance(loop_id, body.ignore_fail, body.force_phase),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/loops/{loop_id}/retry-incomplete")
def api_retry_incomplete(
    loop_id: str,
    body: ResumeRequest = Body(default_factory=ResumeRequest),
):
    """暂时关闭：额度耗尽时重跑只会反复 403。"""
    raise HTTPException(
        status_code=410,
        detail="重跑未成功已暂时关闭。请先解决模型额度/密钥，再新开 loop。",
    )


@app.post("/api/loops/{loop_id}/annotate")
def api_annotate(loop_id: str, body: AnnotateRequest):
    try:
        return {
            "ok": True,
            "data": do_annotate(
                loop_id,
                body.business_summary,
                body.adopted_indices,
                body.adopt_all,
            ),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/loops/{loop_id}/pe/{which}")
def api_get_pe(loop_id: str, which: str):
    if which not in ("v1", "draft", "v2"):
        raise HTTPException(status_code=400, detail="which 须为 v1|draft|v2")
    return {"ok": True, "data": {"which": which, "content": get_pe(loop_id, which)}}


@app.put("/api/loops/{loop_id}/pe/draft")
def api_put_draft(loop_id: str, body: DraftUpdate):
    try:
        return {"ok": True, "data": save_draft(loop_id, body.content)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/loops/{loop_id}/optimize")
def api_optimize(loop_id: str, body: OptimizeRequest):
    try:
        return {"ok": True, "data": do_optimize(loop_id, body.mode)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/loops/{loop_id}/adopt-draft")
def api_adopt(loop_id: str, body: ResumeRequest = Body(default_factory=ResumeRequest)):
    try:
        return {
            "ok": True,
            "data": do_adopt_and_advance(loop_id, ignore_fail=body.ignore_fail),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/loops/{loop_id}/decide")
def api_decide(loop_id: str, body: DecideRequest):
    try:
        return {
            "ok": True,
            "data": do_decide(loop_id, body.decision, body.note, body.write_config),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/loops/{loop_id}/reports/{name}")
def api_report(loop_id: str, name: str):
    try:
        return {"ok": True, "data": load_report(loop_id, name)}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/loops/{loop_id}/eval/{version}")
def api_eval(loop_id: str, version: str):
    if version not in ("v1", "v2"):
        raise HTTPException(status_code=400, detail="version 须为 v1|v2")
    return {"ok": True, "data": read_eval_preview(loop_id, version)}


# Serve built frontend if present
_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/")
    def spa_index():
        return FileResponse(_DIST / "index.html")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = _DIST / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
