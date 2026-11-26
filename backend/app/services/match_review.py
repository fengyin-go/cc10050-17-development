"""人工复核队列领域服务：人工裁决低置信度规格匹配结果，负责复核办结、驳回联动与角色范围控制。

数据链路位置：裁决层，消费规格匹配器与失败隔离区升级的待复核记录，
通过的结论回填匹配记录，驳回的样本回流失败隔离区。

复核队列按来源指纹关联隔离样本的恢复回执数据；认领请求必须带上来源指纹并
实际取得处置资格，才能继续办结。通过的复核会产出可交回该样本的恢复产物。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.recovery_ledger import as_int, ledger, normalize_fingerprint
from app.services.base import BaseModuleService
from app.store import store


class MatchReviewService(BaseModuleService):
    MODULE = "match_review"
    ENTITY = "复核记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "匹配记录", "复核结论", "复核角色", "复核时间"]
    REQUIRED_FIELDS = ["记录编号", "匹配记录", "复核结论"]
    STATUS_ORDER = ["待复核", "已通过", "已驳回"]
    ACTION_RULES = {"通过记录": "已通过", "驳回记录": "已驳回"}
    NEGATIVE_ACTIONS = ["驳回记录"]

    ALLOWED_ROLES = ("规格审核员", "商品运营", "数据治理专员")

    # ---- 通用动作分派：界面动作复用认领/办结逻辑 ----

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        reviewer = str((entry or {}).get("认领人") or "数据治理专员")
        if action == "通过记录":
            result = self.approve(entry_id, reviewer)
        elif action == "驳回记录":
            result = self.reject(entry_id, reviewer)
        elif action == "认领复核":
            result = self.claim_task(
                entry_id, reviewer, fingerprint=(entry or {}).get("来源指纹") or None
            )
        elif action == "人工退出":
            result = self.withdraw(entry_id)
        else:
            return super().run_action(entry_id, action)
        entry = store.find(self.MODULE, entry_id) if result.get("ok") else None
        return entry, result["message"]

    # ---- 业务扩展：复核办结与结论回填 ----

    def claim_task(
        self,
        entry_id: int,
        role: str,
        *,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """认领待复核记录：校验角色与来源指纹，并实际取得处置资格。

        认领请求必须能按来源指纹关联到恢复回执，且资格未被自动回执占用，
        才视为认领成功；未取得资格的认领会留下可查询的处理结果。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "复核记录不存在"}
        if role not in self.ALLOWED_ROLES:
            return {"ok": False, "message": f"角色 {role} 无复核资格"}
        if entry.get("status") != "待复核":
            return {"ok": False, "message": "记录不处于待复核状态"}

        source_fp = normalize_fingerprint(entry.get("来源指纹", ""))
        supplied_fp = normalize_fingerprint(fingerprint)
        if not source_fp:
            source_fp = supplied_fp
        if not source_fp:
            return {"ok": False, "message": "缺少来源指纹，无法关联恢复回执"}
        if supplied_fp and supplied_fp != source_fp:
            return {
                "ok": False,
                "message": f"来源指纹不匹配：记录为 {source_fp}，请求为 {supplied_fp}",
                "fingerprint": source_fp,
            }

        recovery = ledger.query(source_fp)
        if not recovery["receipts"] and recovery["state"] == "待处置":
            return {
                "ok": False,
                "message": "未找到该指纹的恢复回执，认领请求无法关联",
                "fingerprint": source_fp,
            }
        entry["来源指纹"] = source_fp

        claim = ledger.acquire(
            source_fp,
            owner=ledger.OWNER_MANUAL,
            holder=f"{role}@{datetime.now(timezone.utc).isoformat()}",
            review_id=entry_id,
            kind="人工复核认领",
        )
        result = dict(claim)
        result["review_id"] = entry_id
        result["fingerprint"] = source_fp
        if claim["acquired"]:
            entry["复核角色"] = role
            entry["认领人"] = role
            entry["认领时间"] = datetime.now(timezone.utc).isoformat()
            entry["来源指纹"] = source_fp
            entry["剩余动作"] = self._remaining_actions(source_fp)
            entry["pending"] = True
            result["message"] = f"记录已由 {role} 认领并取得处置资格"
        else:
            entry["认领结果"] = claim["message"]
            result["message"] = f"认领失败：{claim['message']}"
        result["recovery"] = ledger.query(source_fp)
        return result

    def approve(self, entry_id: int, reviewer: str) -> dict[str, Any]:
        """通过复核：产出可交回样本的恢复产物并办结。

        只有实际持有处置资格的一方可以办结；若自动回执已先取得资格，则本入口
        只返回可查询的处理结果，不产生第二份恢复产物。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        if entry.get("status") != "待复核":
            return {"ok": False, "message": "记录不处于待复核状态"}
        fingerprint = normalize_fingerprint(entry.get("来源指纹", "")) or normalize_fingerprint(
            entry.get("匹配记录", "")
        )

        claim = ledger.acquire(
            fingerprint,
            owner=ledger.OWNER_MANUAL,
            holder=reviewer,
            review_id=entry_id,
            kind="复核办结认领",
        )
        if not claim["acquired"]:
            entry["办结结果"] = claim["message"]
            entry["剩余动作"] = self._remaining_actions(fingerprint)
            return {
                "ok": False,
                "message": f"未取得处置资格，不能办结：{claim['message']}",
                "acquired": False,
                "fingerprint": fingerprint,
                "result": claim["attempt"],
                "recovery": ledger.query(fingerprint),
            }

        artifact, created, artifact_message = ledger.produce_artifact(
            fingerprint,
            owner=ledger.OWNER_MANUAL,
            holder=reviewer,
            kind="人工复核恢复产物",
            payload={
                "review_id": entry_id,
                "review_number": entry.get("记录编号", ""),
                "conclusion": entry.get("复核结论", ""),
            },
        )
        if artifact is None:
            entry["办结结果"] = artifact_message
            return {
                "ok": False,
                "message": artifact_message,
                "fingerprint": fingerprint,
                "recovery": ledger.query(fingerprint),
            }

        match_id = str(entry.get("匹配记录", "") or "")
        match = next(
            (m for m in store.rows("sku_match") if str(m.get("id", "")) == match_id), None
        )
        if match is not None:
            match["status"] = "已匹配"
            match["pending"] = False
        entry["status"] = "已通过"
        entry["复核角色"] = reviewer
        entry["认领人"] = reviewer
        entry["复核时间"] = datetime.now(timezone.utc).isoformat()
        entry["恢复产物"] = artifact["id"]
        entry["剩余动作"] = "[]"
        entry["pending"] = False
        entry["abnormal"] = False
        snapshot = ledger.close(
            fingerprint,
            owner=ledger.OWNER_MANUAL,
            holder=reviewer,
            final_status="已通过",
            message="人工复核通过并产出恢复产物",
        )
        # 通过的复核让隔离样本回到已恢复，恢复量回写采集量指标。
        quarantine = self._quarantine_for(fingerprint)
        if quarantine is not None:
            quarantine["status"] = "已恢复"
            quarantine["pending"] = False
            quarantine["abnormal"] = False
            self._bump_output()
        return {
            "ok": True,
            "message": "复核已通过，恢复产物可按同一指纹交回",
            "acquired": True,
            "match_backfilled": match is not None,
            "fingerprint": fingerprint,
            "artifact": artifact,
            "artifact_created": created,
            "final_status": "已通过",
            "disposal": snapshot,
        }

    def reject(self, entry_id: int, reviewer: str) -> dict[str, Any]:
        """驳回复核记录，样本回流失败隔离区并释放处置资格。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "复核记录不存在"}
        if entry.get("status") != "待复核":
            return {"ok": False, "message": "记录不处于待复核状态"}
        fingerprint = normalize_fingerprint(entry.get("来源指纹", "")) or normalize_fingerprint(
            entry.get("匹配记录", "")
        )
        claim = ledger.acquire(
            fingerprint,
            owner=ledger.OWNER_MANUAL,
            holder=reviewer,
            review_id=entry_id,
            kind="复核驳回认领",
        )
        if not claim["acquired"]:
            entry["办结结果"] = claim["message"]
            return {
                "ok": False,
                "message": f"未取得处置资格，不能驳回：{claim['message']}",
                "fingerprint": fingerprint,
                "result": claim["attempt"],
                "recovery": ledger.query(fingerprint),
            }

        entry["status"] = "已驳回"
        entry["复核角色"] = reviewer
        entry["认领人"] = reviewer
        entry["复核时间"] = datetime.now(timezone.utc).isoformat()
        entry["abnormal"] = True
        entry["pending"] = False
        rows = store.rows("failure_quarantine")
        fid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": fid,
            "样本指纹": fingerprint or f"review-{entry_id}",
            "失败阶段": "匹配",
            "失败原因": f"复核驳回: {entry.get('复核结论', '')}",
            "隔离时间": datetime.now(timezone.utc).isoformat(),
            "重试次数": "0",
            "status": "待处理",
            "pending": True,
            "abnormal": False,
        })
        snapshot = ledger.release(
            fingerprint,
            owner=ledger.OWNER_MANUAL,
            holder=reviewer,
            reason="复核驳回，样本回流隔离区",
        )
        ledger.mark_terminal(fingerprint, "已驳回", "复核驳回，样本回流隔离区")
        return {
            "ok": True,
            "message": "复核已驳回，样本回流隔离区",
            "quarantine_id": fid,
            "fingerprint": fingerprint,
            "disposal": ledger.query(fingerprint),
            "released": snapshot.get("released", False),
        }

    def withdraw(self, entry_id: int) -> dict[str, Any]:
        """人工退出：释放处置资格并返回该样本能否回到自动恢复。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "复核记录不存在"}
        fingerprint = normalize_fingerprint(entry.get("来源指纹", "")) or normalize_fingerprint(
            entry.get("匹配记录", "")
        )
        reviewer = str(entry.get("认领人") or entry.get("复核角色") or "")
        result = ledger.release(
            fingerprint,
            owner=ledger.OWNER_MANUAL,
            holder=reviewer,
            reason="人工主动退出复核",
        )
        if result["released"]:
            entry["复核角色"] = ""
            entry["认领人"] = ""
            entry["认领结果"] = "人工已退出，资格已释放"
        entry["能否回到自动恢复"] = "可以" if result["auto_recovery_available"] else "不可以"
        entry["自动恢复可用"] = result["auto_recovery_available"]
        result["review_id"] = entry_id
        result["auto_recovery_available"] = result["auto_recovery_available"]
        return result

    def pending_queue(self, role: str | None = None) -> dict[str, Any]:
        """输出待复核队列，附带来源指纹的恢复回执数据与剩余动作。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("status") == "待复核"]
        if role:
            rows = [r for r in rows if r.get("复核角色") == role]
        records = []
        for row in rows:
            fingerprint = normalize_fingerprint(row.get("来源指纹", ""))
            recovery = ledger.query(fingerprint) if fingerprint else None
            records.append({
                "record_number": row.get("记录编号", ""),
                "id": row.get("id"),
                "fingerprint": fingerprint,
                "claimant": row.get("认领人") or row.get("复核角色") or "",
                "remaining_actions": self._remaining_actions(fingerprint) if fingerprint else [],
                "claimable": bool(fingerprint) and (
                    recovery is None or recovery["eligibility_owner"] in (None, ledger.OWNER_MANUAL)
                ),
                "recovery": recovery,
            })
        return {
            "ok": True,
            "message": "待复核队列已输出",
            "count": len(records),
            "records": [r["record_number"] for r in records][:10],
            "items": records[:10],
        }

    def today_throughput(self) -> dict[str, Any]:
        """统计今日办结量并回写采集量指标。"""
        today = datetime.now(timezone.utc).date().isoformat()
        rows = [
            r for r in store.rows(self.MODULE)
            if str(r.get("复核时间", "")).startswith(today)
            and r.get("status") in ("已通过", "已驳回")
        ]
        approved = sum(1 for r in rows if r.get("status") == "已通过")
        return {
            "ok": True,
            "message": "今日办结统计完成",
            "total": len(rows),
            "approved": approved,
            "rejected": len(rows) - approved,
        }

    def link_goods_registry(self, entry_id: int) -> dict[str, Any]:
        """把复核结论同步到商品档案中心状态。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "复核记录不存在"}
        conclusion = str(entry.get("复核结论", ""))
        affected = 0
        for goods in store.rows("goods_registry"):
            if conclusion and conclusion in str(goods.get("登记来源", "")):
                goods["pending"] = entry.get("status") == "已通过"
                affected += 1
        return {
            "ok": True,
            "message": "复核结论已同步商品档案",
            "affected": affected,
        }

    # ---- 内部辅助 ----

    def _remaining_actions(self, fingerprint: str) -> list[str]:
        snapshot = ledger.query(fingerprint)
        if snapshot["state"] == "已办结":
            return []
        if snapshot["eligibility_owner"] == ledger.OWNER_MANUAL:
            return ["通过记录", "驳回记录", "人工退出"]
        if snapshot["eligibility_owner"] == ledger.OWNER_AUTO:
            return ["继续恢复"]
        actions = ["认领复核", "通过记录", "驳回记录", "人工退出"]
        return actions

    def _quarantine_for(self, fingerprint: str) -> dict[str, Any] | None:
        candidates = [
            row for row in store.rows("failure_quarantine")
            if normalize_fingerprint(row.get("样本指纹", "")) == fingerprint
        ]
        if not candidates:
            return None
        active = [row for row in candidates if row.get("status") != "已丢弃"]
        return (active or candidates)[-1]

    def _bump_output(self) -> None:
        metrics = store.rows("collect_metrics")
        row = next((m for m in metrics if m.get("status") != "断采"), None)
        if row is not None:
            row["产出量"] = str(as_int(row.get("产出量")) + 1)
