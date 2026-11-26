"""失败隔离区领域服务：隔离解析与匹配失败的样本供延迟重试，负责重试次数与丢弃治理。

数据链路位置：失败治理层，承接解析模板、比价计算、调度计划与代理线路
写入的失败上下文，恢复结果回写采集量指标与相关模块。

恢复处置采用按隔离指纹落账的台账：自动重试回执、人工复核认领、恢复产物和
最终处置状态都挂在同一个指纹下，只允许一方取得处置资格并办结。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.recovery_ledger import as_int, ledger, normalize_fingerprint
from app.services.base import BaseModuleService
from app.store import store


class FailureQuarantineService(BaseModuleService):
    MODULE = "failure_quarantine"
    ENTITY = "隔离记录"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["样本指纹", "失败阶段", "失败原因", "隔离时间", "重试次数"]
    REQUIRED_FIELDS = ["样本指纹", "失败阶段", "失败原因"]
    STATUS_ORDER = ["待处理", "重试中", "已丢弃", "已恢复"]
    ACTION_RULES = {"立即重试": "待处理", "丢弃样本": "已丢弃"}
    NEGATIVE_ACTIONS = []

    # ---- 通用动作分派：把界面动作接到处置台账 ----

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "立即重试":
            result = self.retry_now(entry_id)
        elif action == "升级人工复核":
            result = self.escalate_to_review(entry_id)
        elif action in ("继续恢复", "恢复样本"):
            result = self.continue_recovery(entry_id)
        elif action == "丢弃样本":
            result = self.discard(entry_id, "值班员手工丢弃")
        else:
            return super().run_action(entry_id, action)
        entry = store.find(self.MODULE, entry_id) if result.get("ok") else None
        return entry, result["message"]

    # ---- 业务扩展：失败重试与恢复治理 ----

    def retry_now(
        self,
        entry_id: int,
        *,
        ok: bool = False,
        detail: str = "",
    ) -> dict[str, Any]:
        """立即重试隔离样本：按失败阶段回放到对应模块。

        隔离样本默认仍按失败回执处理：失败数据当次落账，并保留人工继续入口。
        `ok=True` 时登记成功回执，由台账决定自动恢复是否取得处置资格。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        stage = entry.get("失败阶段", "")
        if stage not in ("采集", "解析", "计算", "匹配", "调度", "出口", "留存溢出", "导出"):
            return {"ok": False, "message": f"未知失败阶段: {stage}"}
        retries = as_int(entry.get("重试次数")) + 1
        if retries > 5:
            entry["status"] = "已丢弃"
            entry["pending"] = False
            fingerprint = normalize_fingerprint(entry.get("样本指纹", ""))
            ledger.link_quarantine(fingerprint, entry_id)
            ledger.mark_terminal(fingerprint, "已丢弃", "重试次数超限，样本已丢弃")
            return {"ok": False, "message": "重试次数超限，样本已丢弃"}

        fingerprint = normalize_fingerprint(entry.get("样本指纹", ""))
        ledger.link_quarantine(fingerprint, entry_id)
        entry["重试次数"] = str(retries)
        entry["status"] = "重试中"
        entry["pending"] = True
        entry["上次重试结果"] = "成功" if ok else "失败"

        receipt = ledger.record_receipt(
            fingerprint,
            ok=ok,
            attempt=retries,
            detail=detail or (
                f"{stage} 阶段第 {retries} 次重试成功"
                if ok
                else f"{stage} 阶段第 {retries} 次重试仍失败"
            ),
            source="隔离区立即重试",
            payload={"stage": stage, "retries": retries},
        )
        result: dict[str, Any] = {
            "ok": True,
            "message": receipt["message"],
            "retries": retries,
            "fingerprint": fingerprint,
            "receipt": receipt["receipt"],
            "manual_continue": True,
            "auto_recovery_available": ledger.query(fingerprint)["auto_recovery_available"],
            "disposal": receipt["disposal"],
        }
        if not ok:
            result["failure"] = receipt["disposal"]["last_failure"]
            result["message"] = (
                f"样本已按 {stage} 阶段第 {retries} 次重试仍失败，已保留人工继续入口"
            )
        return result

    def record_external_receipt(
        self,
        fingerprint: str,
        *,
        ok: bool,
        attempt: int | None = None,
        detail: str = "",
        source: str = "自动重试进程",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """登记另一个进程回传的自动处置结果，成功回执参与同一指纹的资格竞争。"""
        fp = normalize_fingerprint(fingerprint)
        if not fp:
            return {"ok": False, "message": "缺少隔离指纹，无法登记自动结果"}
        entry = self.find_by_fingerprint(fp)
        if entry is not None:
            ledger.link_quarantine(fp, int(entry.get("id", 0)))
        result = ledger.record_receipt(
            fp,
            ok=ok,
            attempt=attempt,
            detail=detail or ("自动处置回传成功" if ok else "自动处置回传失败"),
            source=source,
            payload=payload,
        )
        result["quarantine_id"] = int(entry.get("id", 0)) if entry is not None else None
        return result

    def mark_restored(self, entry_id: int) -> dict[str, Any]:
        """标记样本已恢复并把恢复量回写采集量指标。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "隔离记录不存在"}
        fingerprint = normalize_fingerprint(entry.get("样本指纹", ""))
        ledger.link_quarantine(fingerprint, entry_id)
        entry["status"] = "已恢复"
        entry["pending"] = False
        entry["abnormal"] = False
        metrics = store.rows("collect_metrics")
        row = next((m for m in metrics if m.get("status") != "断采"), None)
        if row is not None:
            row["产出量"] = str(as_int(row.get("产出量")) + 1)
        return {"ok": True, "message": "样本已恢复", "fingerprint": entry.get("样本指纹", "")}

    def discard(self, entry_id: int, reason: str) -> dict[str, Any]:
        """丢弃无法恢复的样本并记录最终原因。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "隔离记录不存在"}
        if entry.get("status") == "已恢复":
            return {"ok": False, "message": "已恢复样本不能丢弃"}
        fingerprint = normalize_fingerprint(entry.get("样本指纹", ""))
        ledger.link_quarantine(fingerprint, entry_id)
        snapshot = ledger.query(fingerprint)
        if snapshot["state"] == "已办结" and snapshot["final_status"] == "已恢复":
            return {"ok": False, "message": "样本已恢复办结，不能丢弃"}
        entry["status"] = "已丢弃"
        entry["失败原因"] = f"{entry.get('失败原因', '')};丢弃:{reason}"
        entry["pending"] = False
        ledger.mark_terminal(fingerprint, "已丢弃", f"样本已丢弃: {reason}")
        return {"ok": True, "message": f"样本已丢弃: {reason}", "fingerprint": fingerprint}

    def retryable_by_stage(self, stage: str) -> dict[str, Any]:
        """筛选某失败阶段可重试的样本清单。"""
        rows = [
            r for r in store.rows(self.MODULE)
            if r.get("失败阶段") == stage and r.get("status") == "待处理"
            and as_int(r.get("重试次数")) <= 3
        ]
        return {
            "ok": True,
            "message": "可重试样本已筛选",
            "stage": stage,
            "retryable": len(rows),
            "fingerprints": [r.get("样本指纹", "") for r in rows][:10],
        }

    def find_by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        fp = normalize_fingerprint(fingerprint)
        candidates = [
            row for row in store.rows(self.MODULE)
            if normalize_fingerprint(row.get("样本指纹", "")) == fp
        ]
        if not candidates:
            return None
        # 有未办结记录时优先返回它，避免已丢弃的历史记录遮蔽当前处理对象。
        active = [row for row in candidates if row.get("status") != "已丢弃"]
        return (active or candidates)[-1]

    def escalate_to_review(self, entry_id: int) -> dict[str, Any]:
        """多次重试仍失败的样本升级人工复核，并带上已失败的重试数据。

        同一隔离指纹只保留一个待办结的复核任务：重复提交升级返回已有任务。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "隔离记录不存在"}
        retries = as_int(entry.get("重试次数"))
        if retries < 3:
            return {"ok": False, "message": "重试次数不足，暂不升级人工复核"}
        fingerprint = normalize_fingerprint(entry.get("样本指纹", ""))
        ledger.link_quarantine(fingerprint, entry_id)
        snapshot = ledger.query(fingerprint)
        if snapshot["state"] == "已办结":
            return {
                "ok": False,
                "message": f"样本已{fingerprint}办结，不能再升级复核",
                "fingerprint": fingerprint,
                "final_status": snapshot["final_status"],
            }

        rows = store.rows("match_review")
        existing = next(
            (
                row for row in rows
                if normalize_fingerprint(row.get("来源指纹", "")) == fingerprint
                and row.get("status") == "待复核"
            ),
            None,
        )
        if existing is not None:
            ledger.set_escalation(fingerprint, int(existing.get("id", 0)))
            return {
                "ok": True,
                "duplicate": True,
                "message": "该样本已有待复核任务，未重复创建",
                "review_id": int(existing.get("id", 0)),
                "review_number": existing.get("记录编号", ""),
                "fingerprint": fingerprint,
            }

        refused = next(
            (
                row for row in rows
                if normalize_fingerprint(row.get("来源指纹", "")) == fingerprint
                and row.get("status") == "已通过"
            ),
            None,
        )
        if refused is not None:
            return {
                "ok": False,
                "message": "该样本复核已通过并办结，不能重复升级",
                "fingerprint": fingerprint,
                "review_id": int(refused.get("id", 0)),
            }

        failed_receipts = [r for r in snapshot["receipts"] if not r.get("ok")]
        failed_data = snapshot["last_failure"] or {}
        rid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        remaining_actions = self.remaining_actions(fingerprint, for_review=True)
        rows.append({
            "id": rid,
            "记录编号": f"FQ-{entry_id}-{rid}",
            "匹配记录": fingerprint,
            "来源指纹": fingerprint,
            "复核结论": f"重试 {retries} 次仍未恢复",
            "复核角色": "",
            "认领人": "",
            "复核时间": datetime.now(timezone.utc).isoformat(),
            "隔离记录": entry_id,
            "失败次数": str(retries),
            "失败回执": failed_receipts[-1] if failed_receipts else failed_data,
            "失败回执次数": str(len(failed_receipts)),
            "剩余动作": remaining_actions,
            "状态": "待复核",
            "status": "待复核",
            "pending": True,
            "abnormal": False,
        })
        ledger.set_escalation(fingerprint, rid)
        return {
            "ok": True,
            "duplicate": False,
            "message": "已升级人工复核",
            "review_id": rid,
            "review_number": f"FQ-{entry_id}-{rid}",
            "fingerprint": fingerprint,
            "retries": retries,
            "failed_retries": len(failed_receipts),
            "remaining_actions": remaining_actions,
        }

    def continue_recovery(self, entry_id: int) -> dict[str, Any]:
        """隔离区继续恢复：只有自动回执实际取得处置资格时才能产出恢复产物。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        fingerprint = normalize_fingerprint(entry.get("样本指纹", ""))
        ledger.link_quarantine(fingerprint, entry_id)
        result = ledger.complete_auto(
            fingerprint,
            holder=f"隔离区#{entry_id}",
            kind="样本恢复产物",
            payload={
                "stage": entry.get("失败阶段", ""),
                "retries": entry.get("重试次数", ""),
                "quarantine_id": entry_id,
            },
        )
        if result.get("ok"):
            entry["status"] = "已恢复"
            entry["pending"] = False
            entry["abnormal"] = False
            result["quarantine_id"] = entry_id
            metrics = store.rows("collect_metrics")
            row = next((m for m in metrics if m.get("status") != "断采"), None)
            if row is not None:
                row["产出量"] = str(as_int(row.get("产出量")) + 1)
        elif result.get("acquired") is False:
            entry["status"] = "重试中" if entry.get("status") != "已恢复" else entry["status"]
        return result

    def remaining_actions(self, fingerprint: str, *, for_review: bool = False) -> list[str]:
        """按台账当前状态列出仍可执行的动作，供交接单与复核队列读取。"""
        snapshot = ledger.query(fingerprint)
        if snapshot["state"] == "已办结":
            return []
        owner = snapshot["eligibility_owner"]
        if owner == "人工复核":
            return ["通过记录", "驳回记录", "人工退出"]
        if owner == "自动恢复":
            return ["继续恢复"]
        actions = ["升级人工复核", "丢弃样本"]
        if snapshot["auto_recovery_available"]:
            actions.insert(0, "继续恢复")
        if for_review:
            actions.insert(0, "认领复核")
        return actions

    def disposal_handover(self, entry_id: int) -> dict[str, Any]:
        """处置交接单：值班员据此找到认领人、剩余动作与实际生效的恢复结果。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        fingerprint = normalize_fingerprint(entry.get("样本指纹", ""))
        ledger.link_quarantine(fingerprint, entry_id)
        snapshot = ledger.query(fingerprint)
        review = self._review_for(fingerprint)
        claimant = ""
        review_id = None
        if review is not None:
            review_id = int(review.get("id", 0))
            claimant = str(review.get("认领人") or review.get("复核角色") or "")
        if not claimant and snapshot["eligibility_owner"]:
            claimant = snapshot["holder"]
        effective = snapshot["effective_receipt"]
        return {
            "ok": True,
            "message": "处置交接单已输出",
            "fingerprint": fingerprint,
            "quarantine_id": entry_id,
            "quarantine_status": entry.get("status"),
            "failure_stage": entry.get("失败阶段", ""),
            "retries": entry.get("重试次数", ""),
            "claimant": claimant or "未认领",
            "claimed_by": snapshot["eligibility_owner"],
            "review_id": review_id,
            "review_status": review.get("status") if review else None,
            "disposal_state": snapshot["state"],
            "remaining_actions": self.remaining_actions(fingerprint),
            "effective_recovery": {
                "owner": snapshot["eligibility_owner"],
                "final_status": snapshot["final_status"],
                "message": snapshot["message"],
                "effective_receipt": effective,
                "artifact": snapshot["artifact"],
            },
            "artifact": snapshot["artifact"],
            # 实际生效的恢复结果：已办结取处置终态，未办结则如实反映隔离记录当前状态。
            "final_status": snapshot["final_status"] or "处理中",
            "last_failure": snapshot["last_failure"],
            "auto_recovery_available": snapshot["auto_recovery_available"],
            "attempts": snapshot["attempts"],
            "receipts": snapshot["receipts"],
        }

    def recovery_snapshot(self, fingerprint: str) -> dict[str, Any]:
        """按隔离指纹查询恢复产物与最终处置状态。"""
        fp = normalize_fingerprint(fingerprint)
        snapshot = ledger.query(fp)
        entry = self.find_by_fingerprint(fp)
        result = dict(snapshot)
        result.update({
            "ok": True,
            "message": "恢复回执数据已输出",
            "quarantine_id": int(entry.get("id", 0)) if entry is not None else None,
            "quarantine_status": entry.get("status") if entry is not None else None,
            "last_failure": snapshot["last_failure"],
            "auto_recovery_available": snapshot["auto_recovery_available"],
            "manual_continue_available": snapshot["manual_continue_available"],
            "remaining_actions": self.remaining_actions(fp),
        })
        return result

    def _review_for(self, fingerprint: str) -> dict[str, Any] | None:
        fp = normalize_fingerprint(fingerprint)
        candidates = [
            row for row in store.rows("match_review")
            if normalize_fingerprint(row.get("来源指纹", "")) == fp
        ]
        if not candidates:
            return None
        open_rows = [row for row in candidates if row.get("status") == "待复核"]
        return (open_rows or candidates)[-1]

    def quarantine_stats(self) -> dict[str, Any]:
        """输出隔离区统计快照供采集量指标消费。"""
        rows = store.rows(self.MODULE)
        by_stage: dict[str, int] = {}
        for r in rows:
            stage = str(r.get("失败阶段", "未知"))
            by_stage[stage] = by_stage.get(stage, 0) + 1
        return {
            "ok": True,
            "message": "隔离区统计完成",
            "total": len(rows),
            "by_stage": by_stage,
            "restored": sum(1 for r in rows if r.get("status") == "已恢复"),
        }
