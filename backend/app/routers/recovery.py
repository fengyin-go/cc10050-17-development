"""恢复处置接口：交接单、按指纹查询恢复回执、自动结果回传、复核认领与人工退出。

这些固定路径必须先于通用资源路由注册，否则 `/api/{module}/{entry_id}` 会抢先
匹配 `queue` 之类的固定段。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api", tags=["恢复处置"])


class ReceiptPayload(BaseModel):
    ok: bool = True
    attempt: int | None = None
    detail: str = ""
    source: str = "自动重试进程"
    payload: dict[str, Any] = Field(default_factory=dict)


class ClaimPayload(BaseModel):
    role: str = "数据治理专员"
    fingerprint: str | None = None


class ReviewerPayload(BaseModel):
    reviewer: str | None = None


def _quarantine():
    service = service_for("failure_quarantine")
    if service is None:  # pragma: no cover - 注册表固定存在
        raise HTTPException(status_code=500, detail="失败隔离区服务未注册")
    return service


def _review():
    service = service_for("match_review")
    if service is None:  # pragma: no cover
        raise HTTPException(status_code=500, detail="人工复核队列服务未注册")
    return service


@router.get("/recovery/{fingerprint}")
def recovery_by_fingerprint(fingerprint: str) -> dict[str, Any]:
    """按同一隔离指纹查询恢复产物、恢复回执与最终处置状态。"""
    return _quarantine().recovery_snapshot(fingerprint)


@router.post("/recovery/{fingerprint}/receipts")
def report_receipt(fingerprint: str, payload: ReceiptPayload) -> dict[str, Any]:
    """另一个进程回传自动处置结果；成功回执按同一指纹参与处置资格竞争。"""
    return _quarantine().record_external_receipt(
        fingerprint,
        ok=payload.ok,
        attempt=payload.attempt,
        detail=payload.detail,
        source=payload.source,
        payload=payload.payload,
    )


@router.get("/failure_quarantine/{entry_id}/handover")
def quarantine_handover(entry_id: int) -> dict[str, Any]:
    """处置交接单：认领人、剩余动作与实际生效的恢复结果。"""
    return _quarantine().disposal_handover(entry_id)


@router.post("/failure_quarantine/{entry_id}/escalate")
def escalate(entry_id: int) -> dict[str, Any]:
    """升级人工复核，同一指纹重复提交返回已有待复核任务。"""
    return _quarantine().escalate_to_review(entry_id)


@router.post("/failure_quarantine/{entry_id}/continue")
def continue_recovery(entry_id: int) -> dict[str, Any]:
    """隔离区继续恢复；未取得处置资格时返回可查询的处理结果。"""
    return _quarantine().continue_recovery(entry_id)


@router.get("/match_review/queue")
def review_queue(role: str | None = None) -> dict[str, Any]:
    """待复核队列，附带来源指纹的恢复回执数据与剩余动作。"""
    return _review().pending_queue(role)


@router.post("/match_review/{entry_id}/claim")
def claim(entry_id: int, payload: ClaimPayload | None = None) -> dict[str, Any]:
    """认领复核：校验角色与来源指纹，并实际取得处置资格。"""
    values = payload or ClaimPayload()
    return _review().claim_task(entry_id, values.role, fingerprint=values.fingerprint)


@router.post("/match_review/{entry_id}/withdraw")
def withdraw(entry_id: int, payload: ReviewerPayload | None = None) -> dict[str, Any]:
    """人工退出并返回该样本能否回到自动恢复。"""
    return _review().withdraw(entry_id)
