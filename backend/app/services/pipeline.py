"""预处理管道业务规则：状态流转、质量门禁与筛选口径都收在这里。

上游只有通过「最新审核版本 + 关键字段重复检查」的数据集才能提交校验或启用。
"""
from __future__ import annotations

from typing import Any

from app.services.datalabel import DatalabelService
from app.store import store

MODULE = "pipeline"
REQUIRED_FIELDS = ["管道编号", "数据来源", "字段映射"]
STATUS_ORDER = ["草稿", "校验中", "已启用", "已废弃"]
ACTION_RULES = {"提交校验": "校验中", "启用管道": "已启用", "废弃管道": "已废弃"}
NEGATIVE_ACTIONS = ["废弃管道"]
GATED_ACTIONS = {"提交校验", "启用管道"}

label_service = DatalabelService()


def delivery_gate(entry: dict[str, Any]) -> dict[str, Any]:
    """预处理管道与输出配置共用的交付门禁：只认数据标注的最新审核版本。"""
    code = entry.get("标注编号") or entry.get("数据来源") or entry.get("数据源")
    gate = label_service.gate_for_code(code)
    gate["association"] = {
        "标注编号": code,
        "关联方式": "标注编号/数据集名称",
    }
    return gate


class PipelineService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock:
            rows = store.rows(MODULE)
            for row in rows:
                gate = delivery_gate(row)
                row["质量门禁"] = "通过" if gate["allowed"] else "阻止"
                row["阻止原因"] = gate["reasons"]
                row["关联审核版本"] = gate["audit_version"]
            if keyword:
                rows = [row for row in rows if keyword in str(row.get("管道编号", ""))]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            total = len(rows)
            start = max(page - 1, 0) * size
            return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            entry["质量门禁"] = delivery_gate(entry)
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["标注编号"] = values.get("标注编号")
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"处理管道 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于预处理管道可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"

            if action in GATED_ACTIONS:
                gate = delivery_gate(entry)
                if not gate["allowed"]:
                    entry["阻止原因"] = gate["reasons"]
                    entry["abnormal"] = True
                    return None, (
                        f"数据标注质量门禁未通过，不能{action}："
                        + "；".join(gate["reasons"])
                    )

            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            entry["阻止原因"] = []
            entry["质量门禁"] = "通过"
            entry["关联审核版本"] = delivery_gate(entry)["audit_version"]
            return entry, f"处理管道已{action}"
