"""质量闭环回归测试：抽检、复核、审核版本与交付门禁共用同一套判定。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.datalabel import DatalabelService
from app.services.exportconf import ExportconfService
from app.services.pipeline import PipelineService
from app.store import store

LABEL_ID = 4  # LABEL-22 / 商品目录


class QualityLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.labels = DatalabelService()
        self.exports = ExportconfService()
        self.pipelines = PipelineService()
        self.labels.run_action(LABEL_ID, "开始标注")

    def _fix_all(self, failed_only: bool = False) -> dict[str, dict]:
        values = {
            "SKU-1003": {"标题": "无线鼠标 M1 Pro", "品牌": "罗技（新）"},
            "SKU-1004": {"价格": 1999.00},
            "SKU-1007": {"标题": "静音风扇 F2 Pro", "品牌": "猫头鹰（新）"},
            "SKU-1008": {"价格": 599.00},
        }
        if failed_only:
            values.pop("SKU-1007")
        return values

    def test_inspect_reports_counts_and_field_level_failures(self) -> None:
        record, message = self.labels.inspect(LABEL_ID, {})
        self.assertEqual(record["判定"]["passed"], 3)
        self.assertEqual(record["判定"]["failed"], 2)
        self.assertEqual(record["判定"]["review"], 0)
        fields = {(item["sample_id"], item["field"]) for item in record["判定"]["failures"]}
        self.assertIn(("SKU-1003", "标题"), fields)
        self.assertIn(("SKU-1003", "品牌"), fields)
        self.assertIn(("SKU-1004", "价格"), fields)
        self.assertFalse(record["判定"]["duplicate_ok"])

    def test_gate_blocks_until_audit_and_duplicate_check_pass(self) -> None:
        self.assertFalse(self.labels.gate_for_code("LABEL-22")["allowed"])

        self.labels.inspect(LABEL_ID, {})
        blocked = self.exports.run_action(1, "提交配置测试")
        self.assertIsNone(blocked[0])
        self.assertIn("质量门禁未通过", blocked[1])

        self.labels.audit(LABEL_ID, {"审核人": "质检员", "审核结论": "不通过"})
        self.assertFalse(self.labels.gate_for_code("LABEL-22")["allowed"])

        self.labels.recheck(
            LABEL_ID, 1, {"审核人": "质检员", "sample_values": self._fix_all()}
        )
        message = self.labels.audit(LABEL_ID, {"审核人": "质检员", "审核结论": "通过"})[1]
        self.assertIn("审核版本 v2 已确认", message)
        gate = self.labels.gate_for_code("LABEL-22")
        self.assertTrue(gate["allowed"])
        self.assertEqual(self.exports.run_action(1, "提交配置测试")[0]["status"], "配置测试中")
        self.assertEqual(self.pipelines.run_action(1, "提交校验")[0]["status"], "校验中")

    def test_latest_audit_version_wins_and_old_is_history(self) -> None:
        self.labels.inspect(LABEL_ID, {})
        self.labels.audit(LABEL_ID, {"审核人": "质检员", "审核结论": "不通过"})
        self.labels.recheck(LABEL_ID, 1, {"审核人": "质检员", "sample_values": self._fix_all()})
        self.labels.audit(LABEL_ID, {"审核人": "质检员", "审核结论": "通过"})
        entry = self.labels.get_entry(LABEL_ID)
        versions = [(item["审核版本"], item["有效"], item["审核结论"]) for item in entry["审核历史"]]
        self.assertEqual(versions, [(1, False, "不通过"), (2, True, "通过")])
        self.assertEqual(self.labels.gate_for_code("LABEL-22")["audit_version"], 2)

    def test_recheck_keeps_round_and_completion_is_counted_once(self) -> None:
        self.labels.inspect(LABEL_ID, {})
        self.labels.audit(LABEL_ID, {"审核人": "质检员", "审核结论": "不通过"})
        self.assertEqual(self.labels.get_entry(LABEL_ID)["status"], "标注中")
        self.assertEqual(len(self.labels.get_entry(LABEL_ID)["抽检记录"]), 1)

        self.labels.recheck(LABEL_ID, 1, {"审核人": "质检员", "sample_values": self._fix_all()})
        self.labels.audit(LABEL_ID, {"审核人": "质检员", "审核结论": "通过"})
        self.assertEqual(self.labels.get_entry(LABEL_ID)["完成条目数"], 805)

        duplicate = self.labels.audit(
            LABEL_ID, {"审核人": "质检员", "审核结论": "通过", "抽检轮次": 1}
        )
        self.assertIsNone(duplicate[0])
        self.assertEqual(self.labels.get_entry(LABEL_ID)["完成条目数"], 805)

    def test_concurrent_audit_accepts_single_conclusion(self) -> None:
        self.labels.inspect(LABEL_ID, {})
        results: list = []

        def submit(index: int) -> None:
            results.append(
                self.labels.audit(LABEL_ID, {"审核人": "质检员", "请求号": f"req-{index}"})
            )

        threads = [threading.Thread(target=submit, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        accepted = [item for item in results if item[0] is not None]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(self.labels.get_entry(LABEL_ID)["审核历史"]), 1)

    def test_partial_recheck_can_target_specific_failed_samples(self) -> None:
        self.labels.inspect(LABEL_ID, {})
        record, message = self.labels.recheck(
            LABEL_ID,
            1,
            {"审核人": "质检员", "sample_ids": ["SKU-1003"], "values": {"标题": "无线鼠标 M1 Pro", "品牌": "罗技（新）"}},
        )
        self.assertIn("复核完成", message)
        self.assertEqual(record["判定"]["failed"], 1)
        self.assertEqual(record["复核历史"][-1]["复核样本"], ["SKU-1003"])

    def test_quality_rules_are_shared_by_inspection_and_delivery(self) -> None:
        """三个入口都引用同一份 field_rules，重复/范围口径不会分叉。"""
        record, _ = self.labels.inspect(LABEL_ID, {})
        policy = record["策略"]
        self.assertEqual(policy["duplicate_fields"], ["标题", "品牌"])
        self.assertEqual(policy["field_rules"]["价格"]["type"], "number")
        self.assertEqual(policy["field_rules"]["价格"]["min"], 0.0)
        self.assertEqual(policy["field_rules"]["价格"]["max"], 99999.0)


if __name__ == "__main__":
    unittest.main()
