#!/usr/bin/env python3
"""Тесты мониторинга нормативных источников.

Сети нет ни в одном тесте — только локальные фикстуры. Тест 15 проверяет это
явно: если инструмент попытается открыть сокет, он упадёт.

    python3 -m unittest discover tests -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_sources  # noqa: E402
from lib import extract as extract_mod  # noqa: E402
from lib import impact as impact_mod  # noqa: E402
from lib import normalize as norm_mod  # noqa: E402
import validate as validate_mod  # noqa: E402

FIX = ROOT / "tests" / "source_fixtures"
SOURCE_ID = "msk-284-pp"


def read(name):
    return (FIX / name).read_bytes()


class MonitorTestCase(unittest.TestCase):
    """Общая обвязка: снапшоты уводим во временный каталог."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="urbanos-snap-"))
        self._orig = check_sources.SNAP_DIR
        check_sources.SNAP_DIR = self.tmp
        self.sources, self.monitors, self.profiles = check_sources.load_all()
        self.monitor = self.monitors[SOURCE_ID]

    def tearDown(self):
        check_sources.SNAP_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_fixture(self, name):
        return check_sources.process(
            SOURCE_ID, self.monitor, self.sources, self.profiles,
            raw=read(name), http_meta={"status": 200, "contentType": "text/html"},
        )

    def approve(self, name="mos-v1.html"):
        ev = self.run_fixture(name)
        self.assertEqual(ev["event"], "BASELINE_MISSING")
        check_sources.do_approve(SOURCE_ID, ev["snapshot"], self.monitors, "test")
        return ev


class TestPipeline(MonitorTestCase):

    def test_01_whitespace_is_unchanged(self):
        """Изменение пробелов вне содержательной части не является изменением."""
        self.approve()
        ev = self.run_fixture("mos-v1-whitespace.html")
        self.assertEqual(ev["event"], "UNCHANGED")

    def test_02_real_change_detected(self):
        """Добавленный документ в перечне — настоящее изменение."""
        self.approve()
        ev = self.run_fixture("mos-v2-doc-added.html")
        self.assertEqual(ev["event"], "CHANGED")
        self.assertIn("incomingDocuments", ev["changedBranches"])

    def test_03_waf_stub_is_parser_error(self):
        """200 с заглушкой не должен выглядеть как изменение НПА."""
        self.approve()
        ev = self.run_fixture("mos-waf-stub.html")
        self.assertEqual(ev["event"], "PARSER_ERROR")
        self.assertEqual(ev["classification"], "TECHNICAL_FAILURE")

    def test_04_wrong_target_is_parser_error(self):
        """Страница другой услуги с валидным window.DATA — тоже не изменение."""
        self.approve()
        ev = self.run_fixture("mos-wrong-target.html")
        self.assertEqual(ev["event"], "PARSER_ERROR")
        self.assertIn("TARGET_ID", ev["detail"])

    def test_05_truncated_is_parser_error(self):
        self.approve()
        ev = self.run_fixture("mos-truncated.html")
        self.assertEqual(ev["event"], "PARSER_ERROR")

    def test_06_baseline_missing_on_first_run(self):
        """Первый прогон не принимает baseline автоматически."""
        ev = self.run_fixture("mos-v1.html")
        self.assertEqual(ev["event"], "BASELINE_MISSING")
        self.assertIsNone(check_sources.load_baseline(SOURCE_ID))

    def test_07_unchanged_after_approve(self):
        self.approve()
        ev = self.run_fixture("mos-v1.html")
        self.assertEqual(ev["event"], "UNCHANGED")

    def test_08_baseline_stale_on_version_bump(self):
        """Бамп версии нормализатора — не CHANGED, а требование переутвердить."""
        self.approve()
        base = check_sources.load_baseline(SOURCE_ID)
        base["normalizationVersion"] = base["normalizationVersion"] + 1
        check_sources.save_baseline(SOURCE_ID, base)
        ev = self.run_fixture("mos-v2-doc-added.html")
        self.assertEqual(ev["event"], "BASELINE_STALE")
        self.assertEqual(ev["classification"], "TECHNICAL_FAILURE")

    def test_09_same_change_twice_gives_same_hash(self):
        """Дедупликация issue опирается на стабильность хеша."""
        self.approve()
        a = self.run_fixture("mos-v2-doc-added.html")
        b = self.run_fixture("mos-v2-doc-added.html")
        self.assertEqual(a["currentHash"], b["currentHash"])

    def test_10_revert_to_baseline_is_unchanged(self):
        """Сценарий A -> B -> A: возврат к baseline снова UNCHANGED."""
        self.approve()
        self.assertEqual(self.run_fixture("mos-v2-doc-added.html")["event"], "CHANGED")
        self.assertEqual(self.run_fixture("mos-v1.html")["event"], "UNCHANGED")


class TestImpact(MonitorTestCase):

    def test_11_change_narrowed_by_impact_map(self):
        """Изменение одной ветки не расширяется на весь профиль."""
        self.approve()
        ev = self.run_fixture("mos-v2-doc-added.html")
        self.assertTrue(ev["impact"]["narrowedByImpactMap"])
        self.assertEqual(ev["affectedProfiles"], ["agr-request-package"])
        expected = set(self.monitor["impactMap"]["incomingDocuments"])
        got = {r["ruleId"] for r in ev["affectedRules"]}
        self.assertEqual(got, expected)
        self.assertEqual(len(got), 12)

    def test_12_blocker_gives_critical_review(self):
        self.approve()
        ev = self.run_fixture("mos-v2-doc-added.html")
        self.assertEqual(ev["maxSeverity"], "blocker")
        self.assertEqual(ev["classification"], "CRITICAL_REVIEW")

    def test_13_pending_activation_excluded(self):
        """Правило, ещё не вступившее в силу, влияние не поднимает."""
        pending = [r["id"] for p in self.profiles.values() for r in p["rules"]
                   if r.get("status") == "pending_activation"]
        self.assertTrue(pending, "в корпусе нет ни одного pending_activation — тест бесполезен")
        imp = impact_mod.analyse(SOURCE_ID, [], self.sources, self.profiles, self.monitor)
        touched = {r["ruleId"] for r in imp["affectedRules"]}
        self.assertFalse(touched & set(pending))

    def test_14_part_of_cycle_raises(self):
        broken = {"a": {"partOf": "b"}, "b": {"partOf": "a"}}
        with self.assertRaises(impact_mod.GraphError):
            impact_mod.source_closure("a", broken)

    def test_15_unavailable_is_not_critical(self):
        """Недоступность сайта не может быть CRITICAL — это не изменение права."""
        self.assertEqual(impact_mod.classify("UNAVAILABLE", None), "TECHNICAL_FAILURE")

    def test_16_closure_follows_part_of(self):
        """Изменение родительского акта достаёт профиль, привязанный к дочернему."""
        closure = impact_mod.source_closure(SOURCE_ID, self.sources)
        self.assertIn("msk-admin-reglament-agr", closure)


class TestNormalization(MonitorTestCase):

    def test_17_idempotent(self):
        data = extract_mod.extract(read("mos-v1.html").decode("utf-8"))["data"]
        spec = self.monitor["normalization"]
        once = norm_mod.normalize(data, spec)
        twice = norm_mod.normalize({"blocks": once}, spec)
        self.assertEqual(norm_mod.canonical(once), norm_mod.canonical(twice))

    def test_18_key_order_does_not_matter(self):
        a = {"blocks": {"terms": {"x": 1, "y": 2}}}
        b = {"blocks": {"terms": {"y": 2, "x": 1}}}
        spec = {"includeBranches": ["terms"]}
        self.assertEqual(
            norm_mod.content_hash(norm_mod.normalize(a, spec)),
            norm_mod.content_hash(norm_mod.normalize(b, spec)),
        )

    def test_19_absent_branch_differs_from_empty(self):
        """Исчезновение раздела не должно читаться как «не изменилось»."""
        spec = {"includeBranches": ["terms"]}
        absent = norm_mod.normalize({"blocks": {}}, spec)
        empty = norm_mod.normalize({"blocks": {"terms": {}}}, spec)
        self.assertNotEqual(norm_mod.content_hash(absent), norm_mod.content_hash(empty))

    def test_20_tricky_json_parses(self):
        """window.DATA со скобкой и кавычкой внутри строки."""
        got = extract_mod.extract(read("mos-tricky-json.html").decode("utf-8"))
        title = got["data"]["blocks"]["incomingDocuments"][0]["title"]
        self.assertIn("}", title)
        self.assertIn('"', title)

    def test_21_size_corridor_is_escaping_independent(self):
        """Кириллица как \\uXXXX и литералами даёт одинаковый размер."""
        raw = read("mos-v1.html").decode("utf-8")
        data = extract_mod.extract(raw)["data"]
        escaped = "window.TARGET_ID = \"x\"\nwindow.DATA = " + json.dumps(data, ensure_ascii=True)
        literal = "window.TARGET_ID = \"x\"\nwindow.DATA = " + json.dumps(data, ensure_ascii=False)
        self.assertEqual(
            extract_mod.extract(escaped)["payloadBytes"],
            extract_mod.extract(literal)["payloadBytes"],
        )


class TestApproval(MonitorTestCase):

    def test_22_approve_does_not_touch_network(self):
        """Утверждение baseline обязано работать с уже сохранённым снапшотом."""
        ev = self.run_fixture("mos-v1.html")

        def boom(*a, **kw):
            raise AssertionError("--approve полез в сеть: human-in-the-loop сломан")

        orig = check_sources.fetch
        check_sources.fetch = boom
        try:
            payload = check_sources.do_approve(SOURCE_ID, ev["snapshot"], self.monitors, "test")
        finally:
            check_sources.fetch = orig
        self.assertEqual(payload["snapshot"], ev["snapshot"])

    def test_23_approve_does_not_change_requirements(self):
        before = {p: json.dumps(d, sort_keys=True) for p, d in self.profiles.items()}
        self.approve()
        _s, _m, after_profiles = check_sources.load_all()
        after = {p: json.dumps(d, sort_keys=True) for p, d in after_profiles.items()}
        self.assertEqual(before, after)

    def test_24_snapshot_is_immutable(self):
        """Повторное сохранение того же сырья не переписывает снапшот."""
        ev = self.run_fixture("mos-v1.html")
        blob = self.tmp / SOURCE_ID / f"{ev['snapshot']}.raw.gz"
        mtime = blob.stat().st_mtime_ns
        self.run_fixture("mos-v1.html")
        self.assertEqual(blob.stat().st_mtime_ns, mtime)


class TestValidator(unittest.TestCase):

    def test_25_validator_passes(self):
        self.assertEqual(validate_mod.main(), 0)

    def test_26_validator_does_not_touch_network(self):
        import urllib.request

        orig = urllib.request.urlopen

        def boom(*a, **kw):
            raise AssertionError("validate.py полез в сеть")

        urllib.request.urlopen = boom
        try:
            self.assertEqual(validate_mod.main(), 0)
        finally:
            urllib.request.urlopen = orig

    def test_27_self_hash_is_stable_on_floats(self):
        """На хеше держится иммутабельность — round-trip не должен плыть."""
        p = {"id": "x", "rules": [{"a": 0.1, "b": 1e-3, "c": 1.0, "d": 1234567890123}]}
        h1 = validate_mod.self_hash(p)
        h2 = validate_mod.self_hash(json.loads(json.dumps(p)))
        self.assertEqual(h1, h2)

    def test_28_broken_evaluator_is_rule_error_not_auto_fail(self):
        """Сломанное правило не должно выглядеть как нарушение со стороны объекта."""
        profile = {"rules": [{
            "id": "broken", "verification": "automatic", "severity": "blocker",
            "evaluator": {"op": "max", "path": "glb.triangles", "value": "не число"},
        }]}
        counts = validate_mod.evaluate(profile, {"glb": {"triangles": 10}})
        self.assertEqual(counts["rule_error"], 1)
        self.assertEqual(counts["auto_fail"], 0)
        self.assertFalse(counts["releaseEligible"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
