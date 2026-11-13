"""统一质量判定：抽检、复核与交付检查共用同一套规则和同一份结论。

三个入口（标注抽检、失败样本复核、输出交付检查）都必须调用 ``quality.evaluate`` /
``quality.gate``，不允许各自实现一套字段判断，否则同一个数据集会在三个地方得到不同结论。

判定口径：
- ``通过``：样本全部满足字段规则；
- ``未通过``：存在明确的规则违例（空值、类型错误、超出范围、关键字段重复）；
- ``需复核``：样本本身不完整、无法判定（例如缺少关键字段值）。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

PASS = "通过"
FAIL = "未通过"
REVIEW = "需复核"

MISSING = object()

_FIELD_SPLIT_RE = re.compile(r"[,，、;；\s]+")
_RANGE_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[~～\-至到]\s*(-?\d+(?:\.\d+)?)\s*$")
_MIN_RE = re.compile(r"^>=\s*(-?\d+(?:\.\d+)?)\s*$")
_MAX_RE = re.compile(r"^<=\s*(-?\d+(?:\.\d+)?)\s*$")

NUMERIC_WORDS = ("数值", "数字", "number", "float", "int", "integer")
TRUE_WORDS = ("是", "开启", "启用", "true", "yes", "on", "1")
FALSE_WORDS = ("否", "关闭", "停用", "false", "no", "off", "0")


def split_fields(raw: Any) -> list[str]:
    """把「标题、价格、品牌」或 ["标题", "价格"] 统一成字段名列表。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in _FIELD_SPLIT_RE.split(str(raw)) if part.strip()]


def _is_false(raw: Any) -> bool:
    return str(raw or "").strip().lower() in FALSE_WORDS


def _as_int(raw: Any) -> int | None:
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _parse_range(raw: Any) -> tuple[float | None, float | None]:
    """解析取值范围，支持 ``0~99999``、``0-99999``、``>=0``、``<=100``。"""
    text = str(raw or "").strip()
    if not text:
        return None, None
    matched = _RANGE_RE.match(text)
    if matched:
        return float(matched.group(1)), float(matched.group(2))
    matched = _MIN_RE.match(text)
    if matched:
        return float(matched.group(1)), None
    matched = _MAX_RE.match(text)
    if matched:
        return None, float(matched.group(1))
    return None, None


def _rule(fields: dict[str, dict[str, Any]], field: str) -> dict[str, Any]:
    return fields.setdefault(field, {"required": True, "duplicate": False})


def _structured_items(check: dict[str, Any]) -> list[dict[str, Any]]:
    raw = check.get("检查项") or check.get("checks")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict) and item.get("字段")]
    return []


def build_policy(
    label: dict[str, Any],
    check: dict[str, Any] | None = None,
    *,
    fields: Any = None,
    record_threshold: Any = None,
) -> dict[str, Any]:
    """根据标注任务的字段列表与校验规则生成统一判定策略。

    校验规则来源按优先级：显式 checks 结构 > resultcheck 记录里的中文字段
    > 标注任务自身的最小配置。所有入口都只读取这一个策略。
    """
    check = check or {}
    label_fields = split_fields(fields if fields is not None else label.get("字段列表"))
    target_fields = split_fields(check.get("目标字段"))
    duplicate_fields = split_fields(check.get("重复检查"))

    rules: dict[str, dict[str, Any]] = {
        field: {"required": True, "duplicate": field in duplicate_fields}
        for field in label_fields
    }
    for field in target_fields:
        rules.setdefault(field, {"required": True, "duplicate": False})
    for field in duplicate_fields:
        rules.setdefault(field, {"required": True, "duplicate": True})

    if _is_false(check.get("非空检查")):
        for fed_rule in rules.values():
            fed_rule["required"] = False

    # 结构化检查项：类型 + 范围 + 重复 + 正则，可覆盖同名字段规则。
    structured = _structured_items(check)
    type_text = str(check.get("类型匹配") or "")
    numeric_rule = any(word in type_text.lower() for word in NUMERIC_WORDS)
    min_value, max_value = _parse_range(check.get("取值范围"))
    regex_text = str(check.get("正则表达式") or "").strip()

    for item in structured:
        field = str(item["字段"]).strip()
        kind = str(item.get("类型") or item.get("check") or "").strip()
        fed_rule = _rule(rules, field)
        if kind in ("重复", "duplicate", "唯一"):
            fed_rule["duplicate"] = True
            if field not in duplicate_fields:
                duplicate_fields.append(field)
        elif kind in ("数值范围", "范围", "取值范围", "range"):
            fed_rule["type"] = "number"
            low = item.get("最小值", item.get("min", item.get("下限")))
            high = item.get("最大值", item.get("max", item.get("上限")))
            fed_rule["min"] = float(low) if low is not None else min_value
            fed_rule["max"] = float(high) if high is not None else max_value
        elif kind in ("数值", "数字", "number", "类型", "type"):
            fed_rule["type"] = "number" if kind not in ("类型", "type") or numeric_rule else "text"
        elif kind in ("非空", "required"):
            fed_rule["required"] = True
        elif kind in ("正则", "regex", "pattern"):
            pattern = str(item.get("正则表达式") or item.get("pattern") or regex_text).strip()
            if pattern:
                fed_rule["pattern"] = pattern
        elif kind in ("文本", "text", "string"):
            fed_rule["type"] = "text"

    if not structured:
        for field in target_fields:
            fed_rule = _rule(rules, field)
            if numeric_rule:
                fed_rule["type"] = "number"
            fed_rule["min"] = min_value
            fed_rule["max"] = max_value
            if regex_text:
                fed_rule["pattern"] = regex_text
        for field in duplicate_fields:
            _rule(rules, field)["duplicate"] = True

    threshold = _as_int(
        record_threshold
        if record_threshold is not None
        else check.get("记录数阈值")
    )
    if threshold is None:
        threshold = _as_int(label.get("记录数阈值"))
    if threshold is None:
        threshold = max(len(label_fields), 1)

    return {
        "fields": label_fields if label_fields else list(rules),
        "field_rules": rules,
        "duplicate_fields": [field for field in duplicate_fields if field in rules],
        "record_threshold": threshold,
        "check_id": check.get("校验编号"),
        "label_id": label.get("id"),
        "label_code": label.get("标注编号"),
    }


def _normalize_sample(sample: Any, index: int) -> dict[str, Any]:
    if isinstance(sample, dict) and isinstance(sample.get("values"), dict):
        values = dict(sample["values"])
        sample_id = str(sample.get("sample_id") or sample.get("id") or f"S-{index + 1:03d}")
        result_index = sample.get("index", index)
    elif isinstance(sample, dict):
        values = dict(sample)
        sample_id = str(values.pop("sample_id", None) or values.pop("id", None) or f"S-{index + 1:03d}")
        values.pop("values", None)
        result_index = index
    else:
        values = {}
        sample_id = f"S-{index + 1:03d}"
        result_index = index
    return {"sample_id": sample_id, "index": result_index, "values": values}


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _dedupe_key(rule: dict[str, Any], value: Any) -> str:
    if rule.get("type") == "number":
        try:
            return f"{float(str(value).strip()):g}"
        except (TypeError, ValueError):
            pass
    return str(value).strip().lower()


def _field_label(field: str) -> str:
    return field or "未知字段"


def latest_audit(label: dict[str, Any]) -> dict[str, Any] | None:
    """取最新一次确认的审核版本；旧版本留在审核历史里，不参与门禁判定。"""
    history = [
        item for item in label.get("审核历史") or []
        if item.get("审核版本") and item.get("有效")
    ]
    if not history:
        return None
    return max(history, key=lambda item: int(item.get("审核版本") or 0))


def current_round_record(label: dict[str, Any]) -> dict[str, Any] | None:
    current = int(label.get("当前抽检轮次") or 0)
    for record in label.get("抽检记录") or []:
        if int(record.get("轮次") or 0) == current:
            return record
    return None


def gate(
    label: dict[str, Any] | None,
    *,
    policy: dict[str, Any] | None = None,
    dataset_label: str = "数据集",
) -> dict[str, Any]:
    """交付门禁：预处理与输出共用的唯一判定入口。

    判定只看最新一次确认的审核版本。后出现的抽检不会推翻已确认的审核，但如果它还没有
    形成有效审核结论，下游仍然被阻止，直到重新审核。旧结论保留在审核历史里。
    """
    if label is None:
        return {
            "allowed": False,
            "label_id": None,
            "label_code": None,
            "audit_version": None,
            "verdict": "未审核",
            "duplicate_ok": False,
            "reasons": [f"未关联标注任务，无法确认{dataset_label}质量结论"],
            "failed_samples": [],
            "review_samples": [],
            "failures": [],
            "counts": {"passed": 0, "failed": 0, "review": 0, "total": 0},
        }

    latest = latest_audit(label)
    record = current_round_record(label)
    current_round = int(label.get("当前抽检轮次") or 0)
    reasons: list[str] = []
    if latest is None:
        reasons.append(f"{dataset_label} {label.get('标注编号') or label.get('id')} 尚未提交审核结论")
    else:
        version = int(latest.get("审核版本") or 0)
        if latest.get("verdict") != PASS:
            reasons.append(
                f"审核版本 v{version} 未通过（通过 {latest.get('passed', 0)}、"
                f"失败 {latest.get('failed', 0)}、需复核 {latest.get('review', 0)}）"
            )
        if not latest.get("duplicate_ok", False):
            fields = latest.get("duplicate_fields") or []
            suffix = f"：{'、'.join(fields)}" if fields else ""
            reasons.append(f"审核版本 v{version} 关键字段重复检查未通过{suffix}")
    if (
        latest is not None
        and current_round
        and int(latest.get("抽检轮次") or 0) < current_round
    ):
        reasons.append(f"第 {current_round} 轮抽检尚未形成有效审核结论")
    if record is not None and record.get("审核版本") is None:
        pending = f"第 {current_round} 轮抽检结果为「{record.get('判定', {}).get('verdict')}」，尚未确认审核版本"
        if pending not in reasons:
            reasons.append(pending)
    elif latest is not None and record is not None:
        # 复核改了样本但还没重新审核时，旧版本不能代表当前样本。
        record_status = record.get("状态")
        if record_status in ("已复核", "待审核"):
            reasons.append(
                f"第 {current_round} 轮复核结果尚未确认审核版本，"
                f"最新有效版本仍为 v{latest.get('审核版本')}"
            )
    if policy is not None and not (policy.get("fields") or []):
        reasons.append("字段列表为空，无法确认质量")

    return {
        "allowed": not reasons,
        "label_id": label.get("id"),
        "label_code": label.get("标注编号"),
        "dataset": label.get("数据集名称"),
        "audit_version": int(latest.get("审核版本") or 0) if latest else None,
        "verdict": latest.get("verdict") if latest else "未审核",
        "duplicate_ok": bool(latest.get("duplicate_ok")) if latest else False,
        "duplicate_fields": latest.get("duplicate_fields", []) if latest else [],
        "reasons": reasons,
        "failed_samples": latest.get("failed_samples", []) if latest else [],
        "review_samples": latest.get("review_samples", []) if latest else [],
        "failures": latest.get("failures", []) if latest else [],
        "counts": {
            "passed": int(latest.get("passed") or 0) if latest else 0,
            "failed": int(latest.get("failed") or 0) if latest else 0,
            "review": int(latest.get("review") or 0) if latest else 0,
            "total": int(latest.get("total") or 0) if latest else 0,
        },
        "inspection_round": label.get("抽检轮次"),
        "confirmed_at": latest.get("确认时间") if latest else None,
    }


def evaluate(policy: dict[str, Any], samples: Iterable[Any], *, scope: str = "抽检") -> dict[str, Any]:
    """对样本执行统一质量判定，返回通过/失败/需复核数量与逐字段失败原因。"""
    normalized = [_normalize_sample(sample, index) for index, sample in enumerate(samples)]
    rules: dict[str, dict[str, Any]] = policy.get("field_rules") or {}
    fields: list[str] = policy.get("fields") or list(rules)
    duplicate_fields = [field for field in policy.get("duplicate_fields", []) if field in rules or field in fields]

    seen: dict[str, dict[str, str]] = {field: {} for field in duplicate_fields}
    seen_duplicates: dict[str, list[dict[str, Any]]] = {field: [] for field in duplicate_fields}
    sample_results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    if not fields:
        return {
            "verdict": REVIEW,
            "scope": scope,
            "passed": 0,
            "failed": 0,
            "review": len(normalized),
            "total": len(normalized),
            "failures": [{
                "sample_id": item["sample_id"],
                "field": "*",
                "reason": "字段列表为空，无法执行质量判定",
                "value": None,
                "severity": "需复核",
                "check": "配置",
            } for item in normalized],
            "samples": [],
            "duplicate_ok": True,
            "duplicate_checks": [],
            "fields": [],
            "duplicate_fields": [],
            "record_threshold": policy.get("record_threshold"),
            "messages": ["字段列表为空，无法执行质量判定"],
        }

    for sample in normalized:
        values = sample["values"]
        sample_failures: list[dict[str, Any]] = []
        sample_has_fail = False
        sample_has_review = False

        if not isinstance(values, dict) or not values:
            item = {
                "sample_id": sample["sample_id"],
                "field": "*",
                "reason": "样本缺少字段映射，无法判定",
                "value": None,
                "severity": "需复核",
                "check": "结构",
            }
            sample_failures.append(item)
            sample_has_review = True

        for field in fields:
            rule = rules.get(field, {"required": True, "duplicate": False})
            value = values.get(field, MISSING)

            if value is MISSING or _missing(value):
                if rule.get("required", True):
                    sample_failures.append({
                        "sample_id": sample["sample_id"],
                        "field": _field_label(field),
                        "reason": "字段缺失或为空",
                        "value": None,
                        "severity": "需复核",
                        "check": "非空",
                    })
                    sample_has_review = True
                continue

            if rule.get("type") == "number":
                try:
                    number = float(str(value).strip())
                except (TypeError, ValueError):
                    sample_failures.append({
                        "sample_id": sample["sample_id"],
                        "field": _field_label(field),
                        "reason": "不是数值类型",
                        "value": value,
                        "severity": "失败",
                        "check": "类型",
                    })
                    sample_has_fail = True
                    continue
                low, high = rule.get("min"), rule.get("max")
                if isinstance(low, (int, float)) and number < low:
                    sample_failures.append({
                        "sample_id": sample["sample_id"],
                        "field": _field_label(field),
                        "reason": f"数值 {value} 低于下限 {low:g}",
                        "value": value,
                        "severity": "失败",
                        "check": "取值范围",
                    })
                    sample_has_fail = True
                    continue
                if isinstance(high, (int, float)) and number > high:
                    sample_failures.append({
                        "sample_id": sample["sample_id"],
                        "field": _field_label(field),
                        "reason": f"数值 {value} 超出上限 {high:g}",
                        "value": value,
                        "severity": "失败",
                        "check": "取值范围",
                    })
                    sample_has_fail = True
                    continue

            pattern = rule.get("pattern")
            if pattern and not re.search(str(pattern), str(value)):
                sample_failures.append({
                    "sample_id": sample["sample_id"],
                    "field": _field_label(field),
                    "reason": f"不匹配正则 {pattern}",
                    "value": value,
                    "severity": "失败",
                    "check": "正则表达式",
                })
                sample_has_fail = True
                continue

            if rule.get("duplicate"):
                key = _dedupe_key(rule, value)
                first = seen.setdefault(field, {}).get(key)
                if first:
                    sample_failures.append({
                        "sample_id": sample["sample_id"],
                        "field": _field_label(field),
                        "reason": f"与样本 {first} 重复",
                        "value": value,
                        "severity": "失败",
                        "check": "重复",
                        "duplicate_of": first,
                    })
                    seen_duplicates.setdefault(field, []).append({
                        "sample_id": sample["sample_id"],
                        "duplicate_of": first,
                        "value": value,
                    })
                    sample_has_fail = True
                else:
                    seen.setdefault(field, {})[key] = sample["sample_id"]

        if sample_has_fail:
            status = FAIL
        elif sample_has_review:
            status = REVIEW
        else:
            status = PASS

        failures.extend(sample_failures)
        sample_results.append({
            "sample_id": sample["sample_id"],
            "index": sample["index"],
            "status": status,
            "failures": sample_failures,
            "values": sample["values"],
        })

    passed = sum(1 for item in sample_results if item["status"] == PASS)
    failed = sum(1 for item in sample_results if item["status"] == FAIL)
    review = sum(1 for item in sample_results if item["status"] == REVIEW)
    if failed:
        verdict = FAIL
    elif review:
        verdict = REVIEW
    elif sample_results:
        verdict = PASS
    else:
        verdict = REVIEW

    duplicate_checks = []
    for field in duplicate_fields:
        duplicates = seen_duplicates.get(field, [])
        duplicate_checks.append({
            "field": field,
            "ok": not duplicates,
            "duplicate_count": len(duplicates),
            "duplicates": duplicates,
        })
    duplicate_ok = all(item["ok"] for item in duplicate_checks)

    messages: list[str] = []
    if not sample_results:
        messages.append("没有可抽取的样本数据，无法判定质量")
    if failed:
        messages.append(f"抽检失败样本 {failed} 个")
    if review:
        messages.append(f"抽检需复核样本 {review} 个")
    if not duplicate_ok:
        bad_fields = [item["field"] for item in duplicate_checks if not item["ok"]]
        messages.append(f"关键字段重复检查未通过：{'、'.join(bad_fields)}")
    threshold = policy.get("record_threshold")
    if (
        sample_results
        and isinstance(threshold, int)
        and 0 < len(sample_results) < threshold
    ):
        messages.append(
            f"实际抽取 {len(sample_results)} 条低于记录数阈值 {threshold}，需复核"
        )
        if verdict == PASS:
            verdict = REVIEW

    return {
        "verdict": verdict,
        "scope": scope,
        "passed": passed,
        "failed": failed,
        "review": review,
        "total": len(sample_results),
        "failures": failures,
        "samples": sample_results,
        "failed_samples": [item["sample_id"] for item in sample_results if item["status"] == FAIL],
        "review_samples": [item["sample_id"] for item in sample_results if item["status"] == REVIEW],
        "duplicate_ok": duplicate_ok,
        "duplicate_checks": duplicate_checks,
        "fields": fields,
        "duplicate_fields": duplicate_fields,
        "record_threshold": policy.get("record_threshold"),
        "messages": messages,
    }
