"""数据标注业务规则：抽检、复核、审核版本与状态流转都收在这里。

质量闭环：
1. 按字段列表 + 记录数阈值抽检样本，统一交给 ``quality.evaluate`` 判定；
2. 失败样本保留字段名与原因，可只对失败样本复核，复核结果进入历史；
3. 提交审核后生成审核版本，只有最新版本「通过 + 关键字段无重复」才放行下游；
4. 审核退回标注中保留本轮抽检结果；同一轮完成条目数只计入一次；
5. 并发提交同一轮审核只接受一个有效结论，旧结论保留为历史。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services import quality
from app.store import store

MODULE = "datalabel"
REQUIRED_FIELDS = ["标注编号", "数据集名称", "标注类型"]
STATUS_ORDER = ["草稿", "标注中", "审核中", "已完成", "已废弃"]
ACTION_RULES = {"开始标注": "标注中", "提交审核": "审核中", "废弃任务": "已废弃"}
NEGATIVE_ACTIONS = ["废弃任务"]

VERDICT_ALIASES = {
    "通过": quality.PASS,
    "审核通过": quality.PASS,
    "pass": quality.PASS,
    "不通过": quality.FAIL,
    "未通过": quality.FAIL,
    "审核失败": quality.FAIL,
    "fail": quality.FAIL,
    "需复核": quality.REVIEW,
    "review": quality.REVIEW,
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_int(raw: Any, default: int = 0) -> int:
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return max(value, 0)


def _normalize(entry: dict[str, Any]) -> None:
    """把历史示例数据里的字符串数字统一成整数，避免相加时拼接字符串。"""
    entry["总条目数"] = _to_int(entry.get("总条目数"))
    entry["完成条目数"] = min(_to_int(entry.get("完成条目数")), entry["总条目数"])
    entry.setdefault("字段清单", quality.split_fields(entry.get("字段列表")))
    entry["字段列表"] = "、".join(entry["字段清单"]) if entry["字段清单"] else str(entry.get("字段列表") or "")
    entry.setdefault("记录数阈值", max(len(entry["字段清单"]), 1))
    entry["记录数阈值"] = _to_int(entry.get("记录数阈值"), max(len(entry["字段清单"]), 1)) or max(len(entry["字段清单"]), 1)
    entry.setdefault("抽检轮次", 0)
    entry.setdefault("抽检记录", [])
    entry.setdefault("审核历史", [])
    entry.setdefault("质量事件序号", 0)
    entry.setdefault("已计入轮次", [])
    entry.setdefault("样本数据", [])
    entry.setdefault("审核状态", "未提交")
    entry.setdefault("关闭原因", "")
    entry.setdefault("阻止原因", [])
    entry.setdefault("质量结论", "未审核")
    entry.setdefault("交付状态", "阻止")
    entry.setdefault("当前抽检轮次", entry.get("抽检轮次") or 0)


class DatalabelService:
    # ---- 查询 ---------------------------------------------------------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for row in rows:
            _normalize(row)
            gate = self._gate(row)
            row["质量结论"] = gate["verdict"]
            row["交付状态"] = "允许" if gate["allowed"] else "阻止"
            row["阻止原因"] = gate["reasons"]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("标注编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            _normalize(entry)
            entry["质量门禁"] = self._gate(entry)
        return entry

    def find_by_code(self, code: str | None) -> dict[str, Any] | None:
        """按标注编号找任务，输出配置与预处理管道用它关联质量结论。"""
        if not code:
            return None
        text = str(code).strip()
        for row in store.rows(MODULE):
            _normalize(row)
            if text in (str(row.get("标注编号")), str(row.get("数据集名称"))):
                return row
        return None

    def stats(self) -> dict[str, Any]:
        with store.lock:
            rows = store.rows(MODULE)
            for row in rows:
                _normalize(row)
            gate_results = [self._gate(row) for row in rows]
            return {
                "标注任务总数": len(rows),
                "标注中任务": sum(1 for row in rows if row.get("status") == "标注中"),
                "已完成任务": sum(1 for row in rows if row.get("status") == "已完成"),
                "通过质量门禁": sum(1 for item in gate_results if item["allowed"]),
                "被阻止交付": sum(1 for item in gate_results if not item["allowed"]),
                "需复核样本": sum(item["counts"]["review"] for item in gate_results),
                "失败样本": sum(item["counts"]["failed"] for item in gate_results),
            }

    # ---- 登记 ---------------------------------------------------------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            for field in REQUIRED_FIELDS:
                entry[field] = values.get(field)
            entry["标注类型"] = values.get("标注类型")
            entry["字段清单"] = quality.split_fields(values.get("字段列表"))
            entry["字段列表"] = "、".join(entry["字段清单"])
            entry["标注人"] = values.get("标注人") or "未分配"
            entry["审核人"] = values.get("审核人") or "未分配"
            entry["总条目数"] = _to_int(values.get("总条目数"))
            entry["完成条目数"] = min(_to_int(values.get("完成条目数")), entry["总条目数"])
            entry["记录数阈值"] = _to_int(
                values.get("记录数阈值"), max(len(entry["字段清单"]), 1)
            ) or max(len(entry["字段清单"]), 1)
            entry["样本数据"] = list(values.get("样本数据") or values.get("samples") or [])
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            _normalize(entry)
            rows.append(entry)
            return entry, []

    # ---- 抽检 ---------------------------------------------------------

    def inspect(
        self,
        entry_id: int,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """按字段列表与记录数阈值抽取样本并给出统一质量判定。"""
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"标注任务 {entry_id} 不存在或已归档"
            _normalize(entry)
            if entry["status"] == "已废弃":
                return None, "任务已废弃，不能继续抽检"
            if entry["status"] in ("草稿",):
                return None, "任务尚未开始标注，不能执行抽检"

            check = self._resolve_check(entry, payload)
            policy = quality.build_policy(
                entry,
                check,
                fields=entry["字段清单"],
                record_threshold=payload.get("记录数阈值") or entry["记录数阈值"],
            )
            round_no = int(entry["抽检轮次"]) + 1
            pool = self._sample_pool(entry, payload)
            samples, sample_messages = self._draw_samples(pool, policy["record_threshold"], round_no)

            result = quality.evaluate(policy, samples)
            result["messages"] = result["messages"] + sample_messages
            record = {
                "轮次": round_no,
                "抽检时间": _now(),
                "抽检人": payload.get("抽检人") or entry.get("标注人"),
                "记录数阈值": policy["record_threshold"],
                "字段列表": list(policy["fields"]),
                "校验编号": policy.get("check_id"),
                "策略": policy,
                "样本": samples,
                "判定": result,
                "状态": "待审核",
                "复核次数": 0,
                "复核历史": [],
                "审核版本": None,
                "退回原因": "",
            }
            entry["抽检轮次"] = round_no
            entry["当前抽检轮次"] = round_no
            entry["抽检记录"].append(record)
            entry["质量事件序号"] = int(entry["质量事件序号"]) + 1
            entry["status"] = "审核中"
            entry["pending"] = True
            entry["abnormal"] = result["verdict"] != quality.PASS
            return record, f"第 {round_no} 轮抽检完成：通过 {result['passed']}、失败 {result['failed']}、需复核 {result['review']}"

    def recheck(
        self,
        entry_id: int,
        round_no: int,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """从失败样本继续复核：只合并修正后的样本，重新用同一套规则判定。"""
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"标注任务 {entry_id} 不存在或已归档"
            _normalize(entry)
            record = self._find_round(entry, round_no)
            if record is None:
                return None, f"第 {round_no} 轮抽检记录不存在"
            if record.get("状态") == "已确认":
                return None, "该轮审核已确认，如需复核请重新抽检"
            failed_ids = list(record["判定"].get("failed_samples") or []) + list(
                record["判定"].get("review_samples") or []
            )
            if not failed_ids and not record["判定"].get("failures"):
                return None, "该轮没有失败或需复核样本，无需复核"
            policy = self._policy_of(record)

            updates = self._updates(payload, failed_ids)
            if not updates and payload.get("sample_ids") is None and payload.get("samples") is None:
                return None, "未提供需要复核的样本"
            if not updates and payload.get("sample_ids"):
                return None, "复核样本未匹配到本轮失败样本"

            before = record["判定"]
            samples = self._merge_samples(record["样本"], updates)
            # 复核后若样本量仍低于记录数阈值，保留原先的需复核提示。
            result = quality.evaluate(policy, samples)
            for message in before.get("messages") or []:
                if message not in result["messages"]:
                    result["messages"].append(message)
            if (
                len(samples) < int(policy.get("record_threshold") or 0)
                and result["verdict"] == quality.PASS
            ):
                result["verdict"] = quality.REVIEW
            record["复核历史"].append({
                "复核次数": int(record["复核次数"]) + 1,
                "复核时间": _now(),
                "复核人": payload.get("审核人") or payload.get("复核人") or entry.get("审核人"),
                "复核样本": sorted(updates),
                "复核前": {
                    "verdict": before.get("verdict"),
                    "passed": before.get("passed"),
                    "failed": before.get("failed"),
                    "review": before.get("review"),
                },
                "复核后": {
                    "verdict": result.get("verdict"),
                    "passed": result.get("passed"),
                    "failed": result.get("failed"),
                    "review": result.get("review"),
                },
            })
            record["复核次数"] = int(record["复核次数"]) + 1
            record["样本"] = samples
            record["判定"] = result
            # 复核只修正样本、不改审核版本；上一轮审核结论保留为历史。
            record["状态"] = "已复核" if result["verdict"] != quality.REVIEW else "待审核"
            entry["质量事件序号"] = int(entry["质量事件序号"]) + 1
            entry["abnormal"] = result["verdict"] != quality.PASS
            return record, (
                f"第 {round_no} 轮复核完成：通过 {result['passed']}、"
                f"失败 {result['failed']}、需复核 {result['review']}"
            )

    # ---- 审核 ---------------------------------------------------------

    def audit(self, entry_id: int, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """确认审核结论。同一轮并发提交只接受一个有效版本，旧版本保留为历史。"""
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"标注任务 {entry_id} 不存在或已归档"
            _normalize(entry)
            if entry["status"] == "已废弃":
                return None, "任务已废弃，不能提交审核"

            round_no = _to_int(payload.get("抽检轮次") or payload.get("轮次"), entry["当前抽检轮次"])
            record = self._find_round(entry, round_no)
            if record is None:
                return None, "尚未完成抽检，不能提交审核"
            if not record.get("样本"):
                return None, "本轮没有可审核的样本，请重新抽检"

            raw_verdict = payload.get("审核结论") or payload.get("结论") or payload.get("verdict")
            verdict = VERDICT_ALIASES.get(str(raw_verdict or "").strip(), None)
            if verdict is None:
                verdict = record["判定"]["verdict"]
            # 抽检仍有失败/需复核样本时不允许直接判通过，必须先复核修正。
            if verdict == quality.PASS and record["判定"].get("verdict") != quality.PASS:
                return None, (
                    "本轮抽检仍有失败或需复核样本，请先复核修正后再审核通过"
                )
            auditor = str(payload.get("审核人") or entry.get("审核人") or "").strip()
            if not auditor or auditor == "未分配":
                return None, "缺少审核人，不能确认审核结论"

            # 幂等：同一个请求号重复提交时返回既有结论，不新增版本。
            request_id = str(payload.get("请求号") or payload.get("request_id") or "").strip()
            if request_id:
                for item in reversed(entry["审核历史"]):
                    if item.get("请求号") == request_id:
                        return item, f"请求号 {request_id} 已处理，沿用审核版本 v{item['审核版本']}"

            latest = quality.latest_audit(entry)
            expected = payload.get("expected_version", payload.get("审核版本"))
            if expected is not None and latest is not None:
                if _to_int(expected) != int(latest["审核版本"]):
                    return None, (
                        f"审核版本已更新（当前 v{latest['审核版本']}），"
                        "请刷新后基于最新版本提交"
                    )

            # 同一轮、同一质量事件周期内只允许一个有效结论。
            if latest is not None and latest.get("抽检轮次") == round_no:
                same_cycle = int(latest.get("质量事件序号") or 0) == int(entry["质量事件序号"])
                if same_cycle:
                    return None, (
                        f"第 {round_no} 轮已有有效审核结论 v{latest['审核版本']}，"
                        "如需重新审核请先复核或重新抽检"
                      )

            version = max((int(item.get("审核版本") or 0) for item in entry["审核历史"]), default=0) + 1
            for item in entry["审核历史"]:
                item["有效"] = False

            duplicate_ok = bool(record["判定"].get("duplicate_ok"))
            failures = record["判定"].get("failures") or []
            passed_gate = verdict == quality.PASS and duplicate_ok
            # 完成条目数按「轮次」只结算一次且只在门禁真正通过时结算：
            # 退回标注后修正再审核不会重复累加，失败轮次也不虚增完成量。
            completed_now = passed_gate and round_no not in entry["已计入轮次"]
            if completed_now:
                sample_count = len(record.get("样本") or [])
                entry["完成条目数"] = min(
                    entry["总条目数"],
                    int(entry["完成条目数"]) + sample_count,
                )
                entry["已计入轮次"].append(round_no)

            conclusion = {
                "审核版本": version,
                "抽检轮次": round_no,
                "审核结论": "通过" if verdict == quality.PASS else ("需复核" if verdict == quality.REVIEW else "不通过"),
                "verdict": verdict,
                "passed": int(record["判定"].get("passed") or 0),
                "failed": int(record["判定"].get("failed") or 0),
                "review": int(record["判定"].get("review") or 0),
                "total": int(record["判定"].get("total") or 0),
                "记录数阈值": record.get("记录数阈值"),
                "判定说明": list(record["判定"].get("messages") or []),
                "duplicate_ok": duplicate_ok,
                "duplicate_fields": record["判定"].get("duplicate_fields") or [],
                "failed_samples": record["判定"].get("failed_samples") or [],
                "review_samples": record["判定"].get("review_samples") or [],
                "failures": failures,
                "字段列表": record.get("字段列表") or [],
                "审核人": auditor,
                "确认时间": _now(),
                "质量事件序号": int(entry["质量事件序号"]),
                "请求号": request_id or None,
                "有效": True,
                "备注": payload.get("备注") or payload.get("remark") or "",
            }
            entry["审核历史"].append(conclusion)

            record["审核版本"] = version
            if verdict == quality.PASS:
                record["状态"] = "已确认"
                entry["status"] = "已完成"
                entry["审核状态"] = "已通过"
            else:
                record["状态"] = "已退回"
                record["退回原因"] = (
                    "审核未通过" if verdict == quality.FAIL else "审核需复核"
                )
                entry["status"] = "标注中"
                entry["审核状态"] = "未通过" if verdict == quality.FAIL else "需复核"
            entry["pending"] = entry["status"] != "已完成"
            entry["abnormal"] = not passed_gate
            entry["质量结论"] = verdict
            entry["交付状态"] = "允许" if passed_gate else "阻止"
            entry["阻止原因"] = self._gate(entry)["reasons"]
            if not passed_gate:
                entry["关闭原因"] = "；".join(entry["阻止原因"])
            else:
                entry["关闭原因"] = ""

            message = (
                f"审核版本 v{version} 已确认：{conclusion['审核结论']}；"
                f"通过 {conclusion['passed']}、失败 {conclusion['failed']}、需复核 {conclusion['review']}"
            )
            if not duplicate_ok:
                message += "；关键字段重复检查未通过"
            if completed_now:
                message += f"；完成条目数 +{len(record.get('样本') or [])}"
            return conclusion, message

    # ---- 交付门禁 -----------------------------------------------------

    def _gate(self, entry: dict[str, Any]) -> dict[str, Any]:
        gate = quality.gate(entry, dataset_label="数据集")
        gate["record_threshold"] = entry.get("记录数阈值")
        gate["field_list"] = entry.get("字段清单") or []
        return gate

    def gate_for_code(self, code: str | None) -> dict[str, Any]:
        with store.lock:
            entry = self.find_by_code(code)
            if entry is None:
                return quality.gate(None, dataset_label="数据集")
            return self._gate(entry)

    def gate_for_entry(self, entry_id: int | None) -> dict[str, Any]:
        with store.lock:
            entry = store.find(MODULE, entry_id) if entry_id is not None else None
            if entry is None:
                return quality.gate(None, dataset_label="数据集")
            _normalize(entry)
            return self._gate(entry)

    # ---- 状态动作 -----------------------------------------------------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"标注任务 {entry_id} 不存在或已归档"
            _normalize(entry)
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于数据标注可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"
            if action == "提交审核":
                record = self._find_round(entry, entry["当前抽检轮次"])
                if record is None:
                    return None, "尚未完成抽检，不能提交审核"
                if not record.get("样本"):
                    return None, "本轮没有可审核的样本，请重新抽检"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            if action == "废弃任务":
                entry["交付状态"] = "阻止"
            return entry, f"标注任务已{action}"

    # ---- 内部工具 -----------------------------------------------------

    def _resolve_check(self, entry: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        inline = payload.get("校验规则") or payload.get("check")
        if isinstance(inline, dict):
            return inline
        code = str(payload.get("校验编号") or entry.get("校验编号") or "").strip()
        if code:
            for row in store.rows("resultcheck"):
                if str(row.get("校验编号")) == code:
                    return row
        return {}

    def _sample_pool(self, entry: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        provided = payload.get("samples") or payload.get("样本")
        if isinstance(provided, list) and provided:
            pool = provided
            entry["样本数据"] = list(provided)
        else:
            pool = list(entry.get("样本数据") or [])
        return pool

    def _draw_samples(
        self,
        pool: list[Any],
        threshold: int,
        round_no: int,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if not pool:
            return [], ["样本池为空，请先导入样本数据后再抽检"]
        ordered = list(pool)
        start = (round_no - 1) * threshold
        if start >= len(ordered):
            start = 0
        drawn = ordered[start:start + threshold]
        if len(drawn) < len(ordered) and len(drawn) < threshold:
            drawn = ordered[start:] + ordered[: threshold - len(drawn)]
        normalized: list[dict[str, Any]] = []
        for index, sample in enumerate(drawn):
            normalized.append(self._as_sample(sample, index))
        return normalized, []

    @staticmethod
    def _as_sample(sample: Any, index: int) -> dict[str, Any]:
        if isinstance(sample, dict) and isinstance(sample.get("values"), dict):
            values = dict(sample["values"])
            sample_id = str(sample.get("sample_id") or sample.get("id") or f"S-{index + 1:03d}")
        elif isinstance(sample, dict):
            values = dict(sample)
            sample_id = str(values.pop("sample_id", None) or values.pop("id", None) or f"S-{index + 1:03d}")
        else:
            values = {}
            sample_id = f"S-{index + 1:03d}"
        return {"sample_id": sample_id, "values": values}

    def _updates(self, payload: dict[str, Any], failed_ids: list[str]) -> dict[str, dict[str, Any]]:
        updates: dict[str, dict[str, Any]] = {}
        raw = payload.get("samples") or payload.get("样本")
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                sample = self._as_sample(item, index)
                updates[sample["sample_id"]] = sample["values"]
        raw_values = payload.get("sample_values")
        if isinstance(raw_values, dict):
            for sample_id, values in raw_values.items():
                if isinstance(values, dict):
                    updates[str(sample_id)] = dict(values)
        selected = payload.get("sample_ids")
        if isinstance(selected, (list, tuple)):
            for sample_id in selected:
                sample_id = str(sample_id)
                if sample_id in updates:
                    continue
                values = payload.get("values")
                if isinstance(values, dict):
                    updates[sample_id] = dict(values)
                elif sample_id in failed_ids:
                    updates[sample_id] = {}
        return updates

    @staticmethod
    def _merge_samples(
        samples: list[dict[str, Any]], updates: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        merged = []
        for sample in samples:
            values = dict(sample.get("values") or {})
            if sample["sample_id"] in updates:
                values.update(updates[sample["sample_id"]])
            merged.append({
                "sample_id": sample["sample_id"],
                "values": values,
                "updated": sample["sample_id"] in updates,
            })
        return merged

    @staticmethod
    def _policy_of(record: dict[str, Any]) -> dict[str, Any]:
        policy = record.get("策略")
        if isinstance(policy, dict) and policy.get("field_rules"):
            return policy
        result = record.get("判定") or {}
        return {
            "fields": record.get("字段列表") or result.get("fields") or [],
            "field_rules": {},
            "duplicate_fields": result.get("duplicate_fields") or [],
            "record_threshold": record.get("记录数阈值"),
            "check_id": record.get("校验编号"),
        }

    @staticmethod
    def _find_round(entry: dict[str, Any], round_no: int) -> dict[str, Any] | None:
        for record in entry.get("抽检记录") or []:
            if int(record.get("轮次") or 0) == int(round_no):
                return record
        return None
