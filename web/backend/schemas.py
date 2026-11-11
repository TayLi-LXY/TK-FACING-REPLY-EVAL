from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class StartLoopRequest(BaseModel):
    agent_id: str = ""
    dataset: str = "测评集_抽样版.xlsx"
    scorer_id: str = "default"
    scenes: str = ""
    items: str = ""
    pe_file: str = ""
    ignore_fail: bool = True


class AnnotateRequest(BaseModel):
    business_summary: str = ""
    adopted_indices: list[int] = Field(
        default_factory=list,
        description="1-based indices into optimization_priority; empty = all",
    )
    adopt_all: bool = False


class OptimizeRequest(BaseModel):
    mode: Literal["auto", "keep"] = "auto"


class DraftUpdate(BaseModel):
    content: str


class DecideRequest(BaseModel):
    decision: Literal["ADOPTED", "ROLLED_BACK"]
    note: str = ""
    write_config: bool = True


class ResumeRequest(BaseModel):
    ignore_fail: bool = True
    force_phase: str = ""


class ApiOk(BaseModel):
    ok: bool = True
    data: Any = None
    error: str | None = None
