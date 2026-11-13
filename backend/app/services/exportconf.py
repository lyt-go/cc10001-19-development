"""输出配置业务规则：状态流转、质量门禁与筛选口径都收在这里。

输出配置清单必须同步显示阻止原因：数据集未通过审核或关键字段重复检查时，
提交配置测试与启用配置都会被拦下，并把原因写回配置明细。
"""
from __future__ import annotations

from typing import Any

from app.services.pipeline import delivery_gate
from app.store import store

MODULE = "exportconf"
REQUIRED_FIELDS = ["配置编号", "输出格式", "目标路径"]
STATUS_ORDER = ["草稿", "配置测试中", "已启用", "已停用"]
ACTION_RULES = {"提交配置测试": "配置测试中", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]
GATED_ACTIONS = {"提交配置测试", "启用配置"}


class ExportconfService:
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
                rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
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
                return None, f"输出配置 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于输出配置可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"

            if action in GATED_ACTIONS:
                gate = delivery_gate(entry)
                if not gate["allowed"]:
                    entry["阻止原因"] = gate["reasons"]
                    entry["质量门禁"] = "阻止"
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
            return entry, f"输出配置已{action}"
