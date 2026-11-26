"""恢复处置台账：以隔离指纹为唯一键，记录自动结果、处置资格、恢复产物与尝试结果。

处置资格（disposal eligibility）是同一指纹上的终态资格：自动重试回执与人工复核
认领会并发竞争同一份样本，只有先取得资格的一方可以产出恢复产物并办结；另一方
必须留下可查询的“未取得处置资格”处理结果，不能重复办结或产出第二份产物。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_fingerprint(value: Any) -> str:
    """隔离指纹统一去空白，自动回执与人工认领都按同一个键落账。"""
    return str(value or "").strip()


def as_int(value: Any, default: int = 0) -> int:
    """示例数据里数字字段可能是描述文本，计数时按缺省值处理而不是抛错。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class RecoveryLedger:
    OWNER_AUTO = "自动恢复"
    OWNER_MANUAL = "人工复核"

    def __init__(self) -> None:
        # 并发认领/回执由同一把可重入锁串行化，保证资格只被一方取得。
        self._lock = threading.RLock()
        self._receipts: dict[str, list[dict[str, Any]]] = {}
        self._artifacts: dict[str, list[dict[str, Any]]] = {}
        self._disposals: dict[str, dict[str, Any]] = {}
        self._attempts: dict[str, list[dict[str, Any]]] = {}
        self._receipt_seq = 0
        self._artifact_seq = 0
        self._attempt_seq = 0

    # ---- 内部结构 ----

    def _disposal(self, fingerprint: str) -> dict[str, Any]:
        disposal = self._disposals.get(fingerprint)
        if disposal is None:
            disposal = {
                "fingerprint": fingerprint,
                "state": "待处置",
                "eligibility_owner": None,
                "holder": "",
                "review_id": None,
                "quarantine_id": None,
                "artifact_id": None,
                "final_status": "",
                "message": "样本待处置",
                "closed_at": "",
                "auto_recovery_available": False,
                "manual_continue_available": True,
                "last_failure": None,
                "escalated_review_id": None,
                "updated_at": _now(),
            }
            self._disposals[fingerprint] = disposal
        return disposal

    def _add_attempt_locked(
        self,
        fingerprint: str,
        *,
        kind: str,
        owner: str | None,
        holder: str,
        acquired: bool,
        reason: str,
    ) -> dict[str, Any]:
        self._attempt_seq += 1
        attempt = {
            "id": self._attempt_seq,
            "fingerprint": fingerprint,
            "kind": kind,
            "owner": owner,
            "holder": holder,
            "acquired": acquired,
            "reason": reason,
            "at": _now(),
        }
        self._attempts.setdefault(fingerprint, []).append(attempt)
        return attempt

    def _artifact_view_locked(self, fingerprint: str) -> dict[str, Any] | None:
        artifacts = self._artifacts.get(fingerprint, [])
        return artifacts[-1] if artifacts else None

    def _snapshot_locked(self, fingerprint: str) -> dict[str, Any]:
        disposal = self._disposal(fingerprint)
        snapshot = dict(disposal)
        artifact = self._artifact_view_locked(fingerprint)
        snapshot["artifact"] = artifact
        snapshot["artifacts"] = list(self._artifacts.get(fingerprint, []))
        snapshot["receipts"] = list(self._receipts.get(fingerprint, []))
        snapshot["attempts"] = list(self._attempts.get(fingerprint, []))
        snapshot["effective_receipt"] = next(
            (r for r in reversed(self._receipts.get(fingerprint, [])) if r.get("ok")),
            None,
        )
        return snapshot

    # ---- 隔离区关联与升级 ----

    def link_quarantine(self, fingerprint: str, entry_id: int | None) -> dict[str, Any]:
        fp = normalize_fingerprint(fingerprint)
        with self._lock:
            disposal = self._disposal(fp)
            if entry_id is not None:
                disposal["quarantine_id"] = entry_id
            disposal["updated_at"] = _now()
            return dict(disposal)

    def set_escalation(self, fingerprint: str, review_id: int) -> dict[str, Any]:
        fp = normalize_fingerprint(fingerprint)
        with self._lock:
            disposal = self._disposal(fp)
            disposal["escalated_review_id"] = review_id
            disposal["review_id"] = review_id
            disposal["manual_continue_available"] = True
            disposal["updated_at"] = _now()
            return self._snapshot_locked(fp)

    # ---- 自动结果 / 回执 ----

    def record_receipt(
        self,
        fingerprint: str,
        *,
        ok: bool,
        attempt: int | None = None,
        detail: str = "",
        payload: dict[str, Any] | None = None,
        source: str = "自动重试",
        kind: str = "自动恢复回执",
    ) -> dict[str, Any]:
        """登记一次自动处置结果，并让成功回执参与处置资格竞争。

        成功回执在资格空闲时立即取得自动恢复资格；若人工复核已先认领，则回执
        迟到且未取得资格，但仍按同一指纹留档，后续可查询到它。
        """
        fp = normalize_fingerprint(fingerprint)
        with self._lock:
            disposal = self._disposal(fp)
            self._receipt_seq += 1
            receipt = {
                "id": self._receipt_seq,
                "fingerprint": fp,
                "ok": bool(ok),
                "attempt": attempt,
                "detail": detail,
                "source": source,
                "kind": kind,
                "payload": dict(payload or {}),
                "received_at": _now(),
                "effective": False,
            }
            self._receipts.setdefault(fp, []).append(receipt)
            disposal["updated_at"] = _now()
            result: dict[str, Any] = {
                "ok": True,
                "fingerprint": fp,
                "receipt": receipt,
                "acquired": False,
                "artifact": None,
                "disposal": None,
            }

            if not ok:
                disposal["last_failure"] = {
                    "attempt": attempt,
                    "detail": detail,
                    "source": source,
                    "at": receipt["received_at"],
                }
                disposal["manual_continue_available"] = True
                result["message"] = "自动处置失败已登记，已保留人工继续入口"
                result["disposal"] = self._snapshot_locked(fp)
                return result

            disposal["auto_recovery_available"] = True
            if disposal["state"] == "已办结":
                effective = (
                    disposal["eligibility_owner"] == self.OWNER_AUTO
                    and disposal["artifact_id"] is not None
                )
                receipt["effective"] = effective
                result["acquired"] = effective
                result["artifact"] = self._artifact_view_locked(fp)
                result["message"] = (
                    "样本已由自动恢复办结，回执按同一指纹留档"
                    if effective
                    else "样本已由人工复核办结，迟到的自动回执仅留档"
                )
                if not effective:
                    self._add_attempt_locked(
                        fp,
                        kind=kind,
                        owner=disposal["eligibility_owner"],
                        holder=disposal["holder"],
                        acquired=False,
                        reason="样本已办结，自动回执未取得处置资格",
                    )
                result["disposal"] = self._snapshot_locked(fp)
                return result

            if disposal["eligibility_owner"] is None:
                disposal["eligibility_owner"] = self.OWNER_AUTO
                disposal["holder"] = source
                disposal["state"] = "资格已占用"
                disposal["message"] = "自动恢复回执已取得处置资格"
                receipt["effective"] = True
                result["acquired"] = True
                result["message"] = "自动恢复回执已取得处置资格"
                self._add_attempt_locked(
                    fp,
                    kind=kind,
                    owner=self.OWNER_AUTO,
                    holder=source,
                    acquired=True,
                    reason="成功回执先取得处置资格",
                )
            elif disposal["eligibility_owner"] == self.OWNER_AUTO:
                receipt["effective"] = True
                result["acquired"] = True
                result["message"] = "自动恢复回执已登记"
            else:
                disposal["message"] = "自动回执迟到，未取得处置资格：人工复核已认领"
                result["message"] = "自动回执迟到，未取得处置资格"
                result["blocked_by"] = disposal["holder"]
                self._add_attempt_locked(
                    fp,
                    kind=kind,
                    owner=self.OWNER_MANUAL,
                    holder=disposal["holder"],
                    acquired=False,
                    reason="人工复核已先取得处置资格，自动回执仅留档",
                )

            result["disposal"] = self._snapshot_locked(fp)
            return result

    # ---- 处置资格 ----

    def acquire(
        self,
        fingerprint: str,
        *,
        owner: str,
        holder: str,
        review_id: int | None = None,
        kind: str = "处置资格认领",
    ) -> dict[str, Any]:
        fp = normalize_fingerprint(fingerprint)
        with self._lock:
            disposal = self._disposal(fp)
            acquired = False
            if disposal["state"] == "已办结":
                reason = "样本已办结，不能再取得处置资格"
                message = "样本已办结，未取得处置资格"
            elif disposal["eligibility_owner"] is None:
                disposal["eligibility_owner"] = owner
                disposal["holder"] = holder
                disposal["state"] = "资格已占用"
                if review_id is not None:
                    disposal["review_id"] = review_id
                disposal["message"] = f"{owner}已取得处置资格"
                acquired = True
                reason = f"{owner}先取得处置资格"
                message = f"{owner}已取得处置资格"
            elif disposal["eligibility_owner"] == owner:
                acquired = True
                if holder:
                    disposal["holder"] = holder
                if review_id is not None:
                    disposal["review_id"] = review_id
                reason = f"同一入口重复认领，保持{owner}资格"
                message = f"已保持{owner}处置资格"
            else:
                reason = f"{disposal['eligibility_owner']}已取得处置资格"
                message = f"未取得处置资格：{disposal['eligibility_owner']}已先行取得"
                disposal["message"] = message
            attempt = self._add_attempt_locked(
                fp,
                kind=kind,
                owner=disposal["eligibility_owner"],
                holder=disposal["holder"],
                acquired=acquired,
                reason=reason,
            )
            disposal["updated_at"] = _now()
            return {
                "ok": acquired,
                "acquired": acquired,
                "fingerprint": fp,
                "message": message,
                "blocked_by": None if acquired else disposal["holder"],
                "blocked_owner": None if acquired else disposal["eligibility_owner"],
                "attempt": attempt,
                "disposal": self._snapshot_locked(fp),
            }

    # ---- 恢复产物与办结 ----

    def produce_artifact(
        self,
        fingerprint: str,
        *,
        owner: str,
        holder: str,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, bool, str]:
        fp = normalize_fingerprint(fingerprint)
        with self._lock:
            disposal = self._disposal(fp)
            if disposal["eligibility_owner"] is not None and disposal["eligibility_owner"] != owner:
                return None, False, f"未取得处置资格：{disposal['eligibility_owner']}已先行取得"
            existing = self._artifact_view_locked(fp)
            if existing is not None:
                disposal["artifact_id"] = existing["id"]
                return existing, False, "已存在唯一恢复产物"
            if disposal["eligibility_owner"] != owner:
                return None, False, "尚未取得处置资格，不能产出恢复产物"
            self._artifact_seq += 1
            artifact = {
                "id": self._artifact_seq,
                "fingerprint": fp,
                "owner": owner,
                "holder": holder,
                "kind": kind,
                "payload": dict(payload or {}),
                "created_at": _now(),
                "valid": True,
                "handed_back": False,
            }
            self._artifacts.setdefault(fp, []).append(artifact)
            disposal["artifact_id"] = artifact["id"]
            disposal["updated_at"] = _now()
            self._add_attempt_locked(
                fp,
                kind=kind,
                owner=owner,
                holder=holder,
                acquired=True,
                reason="产出恢复产物",
            )
            return artifact, True, "恢复产物已产出"

    def close(
        self,
        fingerprint: str,
        *,
        owner: str,
        holder: str,
        final_status: str,
        message: str,
    ) -> dict[str, Any] | None:
        fp = normalize_fingerprint(fingerprint)
        with self._lock:
            disposal = self._disposal(fp)
            if disposal["eligibility_owner"] != owner:
                return None
            disposal["state"] = "已办结"
            disposal["final_status"] = final_status
            disposal["message"] = message
            disposal["closed_at"] = _now()
            disposal["updated_at"] = disposal["closed_at"]
            disposal["auto_recovery_available"] = False
            self._add_attempt_locked(
                fp,
                kind="办结",
                owner=owner,
                holder=holder,
                acquired=True,
                reason=message,
            )
            artifact = self._artifact_view_locked(fp)
            if artifact is not None:
                artifact["handed_back"] = True
            return self._snapshot_locked(fp)

    def complete_auto(
        self,
        fingerprint: str,
        *,
        holder: str = "自动恢复",
        kind: str = "自动恢复产物",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """隔离区继续恢复：自动入口在同一把锁内取得资格、产出产物并办结。"""
        fp = normalize_fingerprint(fingerprint)
        with self._lock:
            disposal = self._disposal(fp)
            if disposal["state"] == "已办结":
                artifact = self._artifact_view_locked(fp)
                is_auto = disposal["eligibility_owner"] == self.OWNER_AUTO and artifact is not None
                return {
                    "ok": is_auto,
                    "acquired": is_auto,
                    "fingerprint": fp,
                    "artifact": artifact,
                    "message": (
                        "样本已由自动恢复办结，恢复产物可按同一指纹交回"
                        if is_auto
                        else f"样本已由{disposal['eligibility_owner']}办结，自动入口不能重复办结"
                    ),
                    "disposal": self._snapshot_locked(fp),
                }
            if disposal["eligibility_owner"] is None:
                if not disposal["auto_recovery_available"]:
                    return {
                        "ok": False,
                        "acquired": False,
                        "fingerprint": fp,
                        "artifact": None,
                        "message": "没有可用的自动恢复回执，无法继续恢复；请升级人工复核",
                        "disposal": self._snapshot_locked(fp),
                    }
                disposal["eligibility_owner"] = self.OWNER_AUTO
                disposal["holder"] = holder
                disposal["state"] = "资格已占用"
                self._add_attempt_locked(
                    fp,
                    kind="继续恢复",
                    owner=self.OWNER_AUTO,
                    holder=holder,
                    acquired=True,
                    reason="隔离区继续恢复取得处置资格",
                )
            elif disposal["eligibility_owner"] != self.OWNER_AUTO:
                message = f"未取得处置资格：{disposal['eligibility_owner']}已先行取得"
                disposal["message"] = message
                self._add_attempt_locked(
                    fp,
                    kind="继续恢复",
                    owner=disposal["eligibility_owner"],
                    holder=disposal["holder"],
                    acquired=False,
                    reason=message,
                )
                return {
                    "ok": False,
                    "acquired": False,
                    "fingerprint": fp,
                    "artifact": None,
                    "message": message,
                    "blocked_by": disposal["holder"],
                    "disposal": self._snapshot_locked(fp),
                }

            effective = next(
                (r for r in reversed(self._receipts.get(fp, [])) if r.get("ok")), None
            )
            artifact, created, artifact_message = self.produce_artifact(
                fp,
                owner=self.OWNER_AUTO,
                holder=holder,
                kind=kind,
                payload=payload or (effective or {}).get("payload") or {},
            )
            if artifact is None:
                return {
                    "ok": False,
                    "acquired": True,
                    "fingerprint": fp,
                    "artifact": None,
                    "message": artifact_message,
                    "disposal": self._snapshot_locked(fp),
                }
            snapshot = self.close(
                fp,
                owner=self.OWNER_AUTO,
                holder=holder,
                final_status="已恢复",
                message="自动恢复已产出恢复产物并办结",
            )
            return {
                "ok": True,
                "acquired": True,
                "fingerprint": fp,
                "artifact": artifact,
                "artifact_created": created,
                "message": "隔离区继续恢复完成，恢复产物可按同一指纹交回",
                "disposal": snapshot or self._snapshot_locked(fp),
            }

    def release(
        self,
        fingerprint: str,
        *,
        owner: str,
        holder: str,
        reason: str,
    ) -> dict[str, Any]:
        """人工退出：释放人工持有的处置资格，让样本回到可自动恢复状态。"""
        fp = normalize_fingerprint(fingerprint)
        with self._lock:
            disposal = self._disposal(fp)
            if disposal["state"] == "已办结":
                return {
                    "ok": False,
                    "released": False,
                    "fingerprint": fp,
                    "message": "样本已办结，不能人工退出",
                    "auto_recovery_available": False,
                    "disposal": self._snapshot_locked(fp),
                }
            if disposal["eligibility_owner"] != owner:
                return {
                    "ok": False,
                    "released": False,
                    "fingerprint": fp,
                    "message": f"人工未持有处置资格：{disposal['eligibility_owner'] or '无'}",
                    "auto_recovery_available": disposal["auto_recovery_available"],
                    "disposal": self._snapshot_locked(fp),
                }
            disposal["eligibility_owner"] = None
            disposal["holder"] = ""
            disposal["state"] = "待处置"
            disposal["message"] = f"人工退出：{reason}"
            disposal["auto_recovery_available"] = any(
                r.get("ok") for r in self._receipts.get(fp, [])
            )
            disposal["manual_continue_available"] = True
            disposal["updated_at"] = _now()
            self._add_attempt_locked(
                fp,
                kind="人工退出",
                owner=None,
                holder=holder,
                acquired=True,
                reason=disposal["message"],
            )
            return {
                "ok": True,
                "released": True,
                "fingerprint": fp,
                "message": "人工已退出，处置资格已释放",
                "auto_recovery_available": disposal["auto_recovery_available"],
                "disposal": self._snapshot_locked(fp),
            }

    def mark_terminal(self, fingerprint: str, final_status: str, message: str) -> dict[str, Any]:
        fp = normalize_fingerprint(fingerprint)
        with self._lock:
            disposal = self._disposal(fp)
            if disposal["state"] != "已办结":
                disposal["state"] = "已办结"
                disposal["final_status"] = final_status
                disposal["message"] = message
                disposal["closed_at"] = _now()
                disposal["updated_at"] = disposal["closed_at"]
                disposal["auto_recovery_available"] = False
                self._add_attempt_locked(
                    fp,
                    kind="终态处置",
                    owner=disposal["eligibility_owner"],
                    holder=disposal["holder"],
                    acquired=True,
                    reason=message,
                )
            return self._snapshot_locked(fp)

    # ---- 查询 ----

    def query(self, fingerprint: str) -> dict[str, Any]:
        fp = normalize_fingerprint(fingerprint)
        with self._lock:
            return self._snapshot_locked(fp)

    def has_open_review(self, fingerprint: str) -> int | None:
        fp = normalize_fingerprint(fingerprint)
        with self._lock:
            disposal = self._disposal(fp)
            return disposal.get("escalated_review_id")


ledger = RecoveryLedger()
