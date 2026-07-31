from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from backend.app.services import intelligence


def _segment(code: str, minutes: float = 60) -> dict:
    number = int(code[1:])
    return {
        "complexity_code": code,
        "category": "GNP Complex" if number >= 9 else ("GNP" if number >= 5 else "SNP"),
        "type": "GNP" if number >= 5 else "SNP",
        "minutes": minutes,
    }


def _raw_folder_day(
    run_date: str,
    folder: str,
    codes: list[str],
    *,
    waiting: float = 0,
    spare: float = 42,
    loss: float = 34,
) -> dict:
    return {
        "run_date": run_date,
        "plant_name": "Test Plant",
        "folder": folder,
        "available_capacity": 240,
        "runtime": 120,
        "lost_time": loss,
        "waiting_time": waiting,
        "downtime": 4,
        "buffer_time": spare,
        "idle_time": 0,
        "runtime_segments": [_segment(code, 120 / len(codes)) for code in codes],
        "editions": ["Test Edition"],
    }


def _folder_day_fact(
    run_date: str,
    folder: str,
    *,
    has_gnp: bool,
    waiting_pct: float,
    spare_pct: float,
) -> dict:
    folder_name = folder.split("/")[-1].strip()
    return {
        "run_date": run_date,
        "folder": folder,
        "folder_name": folder_name,
        "folder_has_gnp": has_gnp,
        "folder_has_gnp_complex": False,
        "folder_has_snp": True,
        "folder_has_snp_only": not has_gnp,
        "waiting_time_pct": waiting_pct,
        "spare_capacity_pct": spare_pct,
        "waiting_time_min": waiting_pct * 2.4,
        "spare_time_min": spare_pct * 2.4,
        "available_capacity_min": 240,
    }


def _wrong_folder_gnp_plan(metric_field: str = "waiting_time_pct") -> dict:
    return {
        "intent": "average",
        "primary_source": "exact_dashboard.folder_days",
        "entities": [{"type": "folder", "value": "Folder B"}],
        "metrics": [{"field": metric_field, "label": metric_field, "aggregation": "avg"}],
        "conditions": [{
            "field": "night_type",
            "op": "=",
            "value": "GNP/UV",
            "label": "plant GNP night",
        }],
        "condition_logic": "AND",
        "time_scope": {"type": "none"},
        "group_by": "none",
        "required_sources": [
            "exact_dashboard.folder_days",
            "gnp_snp_folder_analysis.comparison_by_product_type",
        ],
        "sub_questions": [],
        "output_format": "single_value",
    }


def _finish_row(
    run_date: str,
    machine: str,
    folder_name: str,
    finish_time: str,
) -> dict:
    return {
        "plant": "Chinhat",
        "machine": machine,
        "folder_name": folder_name,
        "folder": f"{machine} / {folder_name}",
        "run_date": run_date,
        "print_finish_time": finish_time,
    }


def _bad_print_finish_plan() -> dict:
    return {
        "intent": "count",
        "primary_source": "exact_dashboard.folder_days",
        "entities": [{"type": "folder", "value": "Hiline-1"}],
        "metrics": [{
            "field": "run_date",
            "label": "distinct nights with finish after 3:30 am",
            "aggregation": "count",
        }],
        "conditions": [
            {
                "field": "folder",
                "op": "=",
                "value": "Hiline-1",
                "label": "Folder Hiline-1",
            },
            {
                "field": "print_finish_time",
                "op": ">",
                "value": "03:30",
                "label": "Print finish after 3:30 am",
            },
        ],
        "condition_logic": "AND",
        "time_scope": {"type": "none"},
        "group_by": "none",
        "sort_by": None,
        "limit": None,
        "sub_questions": [{
            "id": "q1",
            "intent": "count",
            "primary_source": "exact_dashboard.folder_days",
            "description": "Count distinct matching run dates.",
        }],
        "required_sources": ["exact_dashboard.folder_days"],
        "output_format": "single_value",
    }


class FolderNightFactTests(unittest.TestCase):
    def test_folder_product_flags_do_not_inherit_plant_gnp_classification(self) -> None:
        plant_lookup = {"2026-03-27": True}
        snp_row = intelligence._exact_folder_day_row(
            _raw_folder_day("2026-03-27", "Colorman-B\nCOLOR B", ["C1"]),
            plant_lookup,
        )

        self.assertTrue(snp_row["plant_gnp_night"])
        self.assertEqual("GNP/UV", snp_row["plant_night_type"])
        self.assertFalse(snp_row["folder_has_gnp"])
        self.assertTrue(snp_row["folder_has_snp_only"])

    def test_folder_day_exposes_complete_metrics_and_matches_dashboard_values(self) -> None:
        row = intelligence._exact_folder_day_row(
            _raw_folder_day(
                "2026-03-27",
                "Colorman-B\nCOLOR B",
                ["C1", "C8", "C12"],
                waiting=24,
                spare=42,
                loss=34,
            ),
            {"2026-03-27": True},
        )

        self.assertEqual(34, row["loss_time_min"])
        self.assertEqual(42, row["spare_time_min"])
        self.assertEqual(10, row["waiting_time_pct"])
        self.assertEqual(17.5, row["spare_capacity_pct"])
        self.assertTrue(row["folder_has_gnp"])
        self.assertTrue(row["folder_has_gnp_complex"])
        self.assertTrue(row["folder_has_snp"])
        self.assertFalse(row["folder_has_snp_only"])
        self.assertEqual(["SNP", "GNP", "GNP Complex"], row["folder_product_types"])


class DecomposerCorrectionTests(unittest.TestCase):
    def test_folder_matching_normalizes_spacing_and_dashboard_aliases(self) -> None:
        self.assertTrue(
            intelligence._entity_value_matches("folder", "COLOR B", "Colorman-B / COLOR  B")
        )
        self.assertTrue(
            intelligence._entity_value_matches("folder", "Folder B", "Colorman-B / COLOR  B")
        )
        self.assertTrue(
            intelligence._entity_value_matches("folder", "FOLDER 2", "Press / FOLDER  2")
        )

    def test_wait_percentage_plan_is_corrected_to_folder_specific_gnp(self) -> None:
        plan = intelligence._normalize_qu_plan_for_question(
            _wrong_folder_gnp_plan(),
            "Calculate the average wait-time percentage for FOLDER B across all days on which it ran GNP, including GNP Complex.",
        )

        self.assertEqual("exact_dashboard.folder_days", plan["primary_source"])
        self.assertEqual(["exact_dashboard.folder_days"], plan["required_sources"])
        self.assertEqual("waiting_time_pct", plan["metrics"][0]["field"])
        self.assertEqual(
            [{"field": "folder_has_gnp", "op": "=", "value": True, "label": "folder ran GNP (including GNP Complex)"}],
            plan["conditions"],
        )

    def test_spare_percentage_plan_uses_precomputed_folder_night_percentage(self) -> None:
        plan = intelligence._normalize_qu_plan_for_question(
            _wrong_folder_gnp_plan("spare_time_min"),
            "Calculate the average spare-time percentage for FOLDER B across all days it ran GNP, including GNP Complex.",
        )

        self.assertEqual("spare_capacity_pct", plan["metrics"][0]["field"])
        self.assertEqual("avg", plan["metrics"][0]["aggregation"])

    def test_fast_executor_uses_every_matching_gnp_day_for_folder_alias(self) -> None:
        rows = [
            _folder_day_fact("2026-01-01", "Colorman-B / COLOR B", has_gnp=False, waiting_pct=1, spare_pct=20),
            _folder_day_fact("2026-01-08", "Colorman-B / COLOR B", has_gnp=True, waiting_pct=10, spare_pct=15),
            _folder_day_fact("2026-03-27", "Colorman-B / COLOR B", has_gnp=True, waiting_pct=20, spare_pct=17.5),
            _folder_day_fact("2026-03-27", "Colorman-A / COLOR A", has_gnp=True, waiting_pct=99, spare_pct=0),
        ]
        context = {"exact_dashboard": {"folder_days_all": rows}}
        plan = intelligence._normalize_qu_plan_for_question(
            _wrong_folder_gnp_plan(),
            "Calculate the average wait-time percentage for FOLDER B across all days on which it ran GNP, including GNP Complex.",
        )

        answer = intelligence._execute_qu_plan(plan, "average wait-time percentage for FOLDER B on GNP days", context)

        self.assertIn("15%", answer)


class FocusedContextTests(unittest.TestCase):
    def test_invalid_decomposer_condition_does_not_erase_relevant_rows(self) -> None:
        rows = [
            _folder_day_fact(
                "2026-03-27",
                "Colorman-B / COLOR B",
                has_gnp=True,
                waiting_pct=10,
                spare_pct=17.5,
            )
        ]
        plan = _wrong_folder_gnp_plan()
        plan["conditions"] = [{
            "field": "invented_metric",
            "op": ">",
            "value": 0,
            "label": "invalid planner condition",
        }]
        context = {"exact_dashboard": {"folder_days_all": rows}}

        focused = intelligence._focused_chat_context_for_llm(context, plan)

        self.assertEqual(
            "invalid_field_unapplied",
            focused["source_selection"][0]["condition_status"],
        )
        self.assertEqual(
            rows,
            focused["selected_sources"]["exact_dashboard.folder_days"],
        )

    def test_context_filters_before_serialization_and_avoids_row_caps(self) -> None:
        rows = []
        for day in range(1, 91):
            for folder_letter in "ABCDE":
                rows.append(
                    _folder_day_fact(
                        f"2026-{((day - 1) // 30) + 1:02d}-{((day - 1) % 30) + 1:02d}",
                        f"Colorman-{folder_letter} / COLOR {folder_letter}",
                        has_gnp=folder_letter == "B" and day in {8, 45, 86},
                        waiting_pct=float(day),
                        spare_pct=20,
                    )
                )
        context = {
            "scope": {"production_days": 90},
            "summary": {},
            "exact_dashboard": {"folder_days_all": rows},
            "gnp_snp_folder_analysis": {
                "comparison_by_product_type": [{"product_type": "GNP"}, {"product_type": "SNP"}]
            },
        }
        plan = intelligence._normalize_qu_plan_for_question(
            _wrong_folder_gnp_plan(),
            "Average wait-time percentage for FOLDER B on all days it ran GNP including GNP Complex",
        )

        focused = intelligence._compact_chat_context_for_llm(context, qu_plan=plan)
        selected = focused["selected_sources"]["exact_dashboard.folder_days"]

        self.assertEqual(3, len(selected))
        self.assertEqual([8, 45, 86], [int(row["waiting_time_pct"]) for row in selected])
        self.assertEqual(450, focused["source_selection"][0]["source_row_count"])
        self.assertEqual(3, focused["source_selection"][0]["selected_row_count"])
        self.assertNotIn("gnp_snp_folder_analysis.comparison_by_product_type", focused["selected_sources"])
        self.assertLess(len(intelligence._chat_context_to_toon(focused)), 10_000)

    def test_zero_percentage_is_still_a_valid_numeric_metric(self) -> None:
        self.assertTrue(intelligence._field_has_numeric_values([{"waiting_time_pct": 0}], "waiting_time_pct"))


class PrintFinishThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            _finish_row("2025-12-11", "Hiline-1", "Folder 2", "03:35"),
            _finish_row("2025-12-25", "Hiline-1", "Folder 2", "03:38"),
            _finish_row("2025-12-26", "Hiline-1", "Folder 2", "03:30"),
            _finish_row("2025-12-27", "Hiline-1", "Folder 2", "03:28"),
            _finish_row("2025-12-28", "Hiline-1", "Folder 2", ""),
            _finish_row("2025-12-29", "Hiline-2", "Folder 3", "04:00"),
        ]
        self.context = {"exact_dashboard": {"folder_days_all": self.rows}}
        self.question = "On how many days the print finish on Hiline-1 was after 3:30 am?"

    def test_clock_parser_supports_24_hour_seconds_and_am_pm(self) -> None:
        self.assertEqual(210, intelligence._clock_minutes("03:30"))
        self.assertEqual(210.5, intelligence._clock_minutes("03:30:30"))
        self.assertEqual(210, intelligence._clock_minutes("3:30 AM"))
        self.assertEqual(210, intelligence._clock_minutes("3:30 a.m."))
        self.assertEqual(930, intelligence._clock_minutes("3:30 pm"))
        self.assertEqual(210, intelligence._clock_minutes("3.30 AM"))
        self.assertIsNone(intelligence._clock_minutes(""))
        self.assertIsNone(intelligence._clock_minutes("not a time"))

    def test_bad_plan_resolves_machine_removes_duplicate_filter_and_forces_table(self) -> None:
        plan = intelligence._normalize_qu_plan_for_question(
            _bad_print_finish_plan(),
            self.question,
            self.context,
        )

        self.assertEqual([{"type": "machine", "value": "Hiline-1"}], plan["entities"])
        self.assertEqual(["print_finish_time"], [condition["field"] for condition in plan["conditions"]])
        self.assertEqual("table", plan["output_format"])

    def test_strict_threshold_returns_two_raw_data_dates_with_evidence(self) -> None:
        plan = intelligence._normalize_qu_plan_for_question(
            _bad_print_finish_plan(),
            self.question,
            self.context,
        )

        answer = intelligence._execute_qu_plan(plan, self.question, self.context)
        focused = intelligence._focused_chat_context_for_llm(
            self.context,
            plan,
            self.question,
        )

        self.assertIn("| Date | Machine | Folder | Print Finish Time |", answer)
        self.assertIn("2025-12-11", answer)
        self.assertIn("03:35", answer)
        self.assertIn("2025-12-25", answer)
        self.assertIn("03:38", answer)
        self.assertIn("**2 distinct days**", answer)
        self.assertNotIn("2025-12-26", answer)
        self.assertNotIn("2025-12-27", answer)
        self.assertNotIn("2025-12-29", answer)
        self.assertEqual(
            ["2025-12-11", "2025-12-25"],
            [
                row["run_date"]
                for row in focused["selected_sources"]["exact_dashboard.folder_days"]
            ],
        )
        self.assertEqual(2, focused["authoritative_result"]["distinct_date_count"])

    def test_multiple_matching_folders_preserve_rows_but_dedupe_headline_dates(self) -> None:
        rows = [
            _finish_row("2025-12-11", "Hiline-1", "Folder 1", "03:40"),
            _finish_row("2025-12-11", "Hiline-1", "Folder 2", "03:35"),
            _finish_row("2025-12-25", "Hiline-1", "Folder 2", "03:38"),
        ]
        context = {"exact_dashboard": {"folder_days_all": rows}}
        plan = intelligence._normalize_qu_plan_for_question(
            _bad_print_finish_plan(),
            self.question,
            context,
        )

        answer = intelligence._execute_qu_plan(plan, self.question, context)

        self.assertEqual(2, answer.count("2025-12-11"))
        self.assertIn("Folder 1", answer)
        self.assertIn("Folder 2", answer)
        self.assertIn("**2 distinct days**", answer)

    def test_other_day_count_queries_also_include_matching_dates(self) -> None:
        rows = [
            {"run_date": "2025-10-01", "downtime_min": 4},
            {"run_date": "2025-10-02", "downtime_min": 0},
            {"run_date": "2025-10-03", "downtime_min": 8},
        ]
        context = {"exact_dashboard": {"daily": rows}}
        plan = {
            "intent": "count",
            "primary_source": "exact_dashboard.daily",
            "entities": [],
            "metrics": [{"field": "run_date", "aggregation": "count"}],
            "conditions": [{"field": "downtime_min", "op": ">", "value": 0}],
            "condition_logic": "AND",
            "time_scope": {"type": "none"},
            "group_by": "none",
            "sub_questions": [],
            "required_sources": ["exact_dashboard.daily"],
            "output_format": "single_value",
        }
        question = "How many days had downtime?"
        plan = intelligence._normalize_qu_plan_for_question(plan, question, context)

        answer = intelligence._execute_qu_plan(plan, question, context)

        self.assertEqual("table", plan["output_format"])
        self.assertIn("2025-10-01", answer)
        self.assertIn("2025-10-03", answer)
        self.assertIn("**2 distinct days**", answer)


class ScopeGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_general_programming_request_is_rejected_without_llm_calls(self) -> None:
        decomposer = AsyncMock()
        answer_llm = AsyncMock()
        with (
            patch.object(intelligence, "_call_qu_decomposer_async", decomposer),
            patch.object(intelligence, "_call_plain_chat_completion_async", answer_llm),
        ):
            result = await intelligence.build_chat_response(
                message="Can you write me Python code for a small game?",
                force_full_llm=True,
            )

        self.assertEqual("out_of_scope", result["status"])
        self.assertFalse(result["llm_used"])
        decomposer.assert_not_awaited()
        answer_llm.assert_not_awaited()

    async def test_rewriting_request_is_rejected_without_llm_calls(self) -> None:
        result = await intelligence.build_chat_response(
            message="Rewrite this productivity improvement paragraph.",
            force_full_llm=True,
        )
        self.assertEqual("out_of_scope", result["status"])


class FullAIRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_mode_returns_authoritative_date_table_without_answering_llm(self) -> None:
        rows = [
            _finish_row("2025-12-11", "Hiline-1", "Folder 2", "03:35"),
            _finish_row("2025-12-25", "Hiline-1", "Folder 2", "03:38"),
            _finish_row("2025-12-26", "Hiline-1", "Folder 2", "03:30"),
        ]
        context = {
            "scope": {"start_date": "2025-10-01", "end_date": "2025-12-31"},
            "summary": {},
            "exact_dashboard": {"folder_days_all": rows},
        }
        decomposer = AsyncMock(return_value=_bad_print_finish_plan())
        answer_llm = AsyncMock()

        def configured_env(name: str, default: str = "") -> str:
            return {"AZURE_ENDPOINT": "https://example.test", "API_KEY": "test-key"}.get(name, default)

        with (
            patch.object(intelligence, "_build_chat_context", return_value=context),
            patch.object(intelligence, "_get_env", side_effect=configured_env),
            patch.object(intelligence, "_call_qu_decomposer_async", decomposer),
            patch.object(intelligence, "_call_plain_chat_completion_async", answer_llm),
            patch.object(intelligence, "_chart_for_answer", return_value=None),
        ):
            result = await intelligence.build_chat_response(
                message="On how many days the print finish on Hiline-1 was after 3:30 am?",
                force_full_llm=False,
            )

        self.assertEqual("fast_path", result["llm_status"])
        self.assertFalse(result["llm_used"])
        self.assertIn("2025-12-11", result["answer"])
        self.assertIn("2025-12-25", result["answer"])
        self.assertIn("**2 distinct days**", result["answer"])
        answer_llm.assert_not_awaited()

    async def test_full_ai_replaces_incorrect_single_value_with_authoritative_table(self) -> None:
        rows = [
            _finish_row("2025-12-11", "Hiline-1", "Folder 2", "03:35"),
            _finish_row("2025-12-25", "Hiline-1", "Folder 2", "03:38"),
            _finish_row("2025-12-26", "Hiline-1", "Folder 2", "03:30"),
        ]
        context = {
            "scope": {"start_date": "2025-10-01", "end_date": "2025-12-31"},
            "summary": {},
            "exact_dashboard": {"folder_days_all": rows},
        }
        decomposer = AsyncMock(return_value=_bad_print_finish_plan())
        answer_llm = AsyncMock(return_value="**0**")

        def configured_env(name: str, default: str = "") -> str:
            return {"AZURE_ENDPOINT": "https://example.test", "API_KEY": "test-key"}.get(name, default)

        with (
            patch.object(intelligence, "_build_chat_context", return_value=context),
            patch.object(intelligence, "_get_env", side_effect=configured_env),
            patch.object(intelligence, "_call_qu_decomposer_async", decomposer),
            patch.object(intelligence, "_call_plain_chat_completion_async", answer_llm),
            patch.object(intelligence, "_chart_for_answer", return_value=None),
        ):
            result = await intelligence.build_chat_response(
                message="On how many days the print finish on Hiline-1 was after 3:30 am?",
                force_full_llm=True,
            )

        self.assertEqual("authoritative_fallback", result["llm_status"])
        self.assertEqual("table", result["plan"]["output_format"])
        self.assertIn("2025-12-11", result["answer"])
        self.assertIn("2025-12-25", result["answer"])
        self.assertIn("**2 distinct days**", result["answer"])
        system_prompt = answer_llm.await_args.args[2][0]["content"]
        self.assertIn("authoritative_result", system_prompt)
        self.assertIn("distinct_date_count: 2", system_prompt)

    async def test_full_ai_uses_corrected_decomposer_plan_and_focused_rows(self) -> None:
        rows = []
        for day in range(1, 91):
            for folder_letter in "ABCDE":
                rows.append(
                    _folder_day_fact(
                        f"2026-{((day - 1) // 30) + 1:02d}-{((day - 1) % 30) + 1:02d}",
                        f"Colorman-{folder_letter} / COLOR {folder_letter}",
                        has_gnp=folder_letter == "B" and day in {8, 45, 86},
                        waiting_pct=float(day),
                        spare_pct=20,
                    )
                )
        context = {
            "scope": {"production_days": 90},
            "summary": {},
            "exact_dashboard": {"folder_days_all": rows},
            "gnp_snp_folder_analysis": {
                "comparison_by_product_type": [{"product_type": "GNP"}, {"product_type": "SNP"}]
            },
        }
        decomposer = AsyncMock(return_value=_wrong_folder_gnp_plan())
        answer_llm = AsyncMock(return_value="Average wait-time percentage: 46.33%")

        def configured_env(name: str, default: str = "") -> str:
            return {"AZURE_ENDPOINT": "https://example.test", "API_KEY": "test-key"}.get(name, default)

        with (
            patch.object(intelligence, "_build_chat_context", return_value=context),
            patch.object(intelligence, "_get_env", side_effect=configured_env),
            patch.object(intelligence, "_call_qu_decomposer_async", decomposer),
            patch.object(intelligence, "_call_plain_chat_completion_async", answer_llm),
            patch.object(intelligence, "_chart_for_answer", return_value=None),
        ):
            result = await intelligence.build_chat_response(
                message=(
                    "Calculate the average wait-time percentage for FOLDER B across all days "
                    "on which it ran GNP, including GNP Complex."
                ),
                force_full_llm=True,
            )

        decomposer.assert_awaited_once()
        answer_llm.assert_awaited_once()
        system_prompt = answer_llm.await_args.args[2][0]["content"]
        self.assertIn("exact_dashboard.folder_days,450,3,applied", system_prompt)
        self.assertIn("waiting_time_pct", system_prompt)
        self.assertIn("available_capacity_min (normally 240 minutes) as the denominator", system_prompt)
        self.assertIn("never runtime + wait", system_prompt)
        self.assertNotIn("waiting_time_pct: 99", system_prompt)
        self.assertEqual(["exact_dashboard.folder_days"], result["plan"]["required_sources"])
        self.assertTrue(result["llm_used"])


if __name__ == "__main__":
    unittest.main()
