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
from lib import archive as archive_mod  # noqa: E402
from lib import fbx as fbx_mod  # noqa: E402
from lib import geojson as geojson_mod  # noqa: E402
from lib import materials as materials_mod  # noqa: E402
from lib import raster as raster_mod  # noqa: E402
from lib import package as package_mod  # noqa: E402
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
        self.assertEqual(len(got), 14)

    def test_11a_impact_map_outranks_source_lineage(self):
        """Правка витрины не тянет за собой правила приложения 2.

        Приложение 2 входит в Регламент по partOf, поэтому попадает в closure
        изменения. Но его текст лежит отдельным файлом с собственным хешем и от
        правки страницы госуслуги не двигается. Когда карта влияния известна,
        она и решает, что задето.
        """
        self.approve()
        ev = self.run_fixture("mos-v2-doc-added.html")
        self.assertIn("msk-prilozhenie-2-sostav-svedeniy", ev["impact"]["sourceClosure"])
        self.assertEqual([r["ruleId"] for r in ev["affectedRules"]
                          if r["ruleId"].startswith("form-sec-")], [])

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


class TestSourceReconciliation(MonitorTestCase):
    """Профиль обязан покрывать источник по составу, а не приблизительно.

    Эти тесты — то, что превращает разовую ручную сверку в инвариант: если
    на mos.ru добавится документ или основание для отказа, тест упадёт и
    потребует пересмотра профиля. Без них расхождение обнаруживается только
    отказом в приёме на реальной подаче.
    """

    def setUp(self):
        super().setUp()
        raw = read("mos-v1.html").decode("utf-8")
        self.blocks = extract_mod.extract(raw)["data"]["blocks"]
        self.profile = self.profiles["agr-request-package"]
        self.imap = self.monitor["impactMap"]

    def rules_for(self, branch):
        ids = set(self.imap[branch])
        return [r for r in self.profile["rules"] if r["id"] in ids]

    def test_29_every_incoming_document_has_a_rule(self):
        docs = self.blocks["incomingDocuments"]
        self.assertEqual(len(docs), 14, "состав документов на источнике изменился")
        self.assertEqual(len(self.rules_for("incomingDocuments")), len(docs))

    def test_30_mandatory_documents_are_blockers(self):
        """Обязательный документ не может быть major: без него откажут в приёме."""
        mandatory = [d for d in self.blocks["incomingDocuments"]
                     if d["inDocumentType"] == "Обязательный"]
        self.assertEqual(len(mandatory), 9)
        doc_rules = [r for r in self.profile["rules"] if r["id"].startswith("doc-")]
        self.assertEqual(len(doc_rules), 9)
        self.assertTrue(all(r["severity"] == "blocker" for r in doc_rules))

    def test_31_every_recipient_category_is_covered(self):
        cats = self.blocks["recipientCategories"]
        self.assertEqual(len(cats), 5, "перечень категорий заявителей изменился")
        self.assertTrue(self.rules_for("recipientCategories"))

    def test_32_refusal_grounds_count_matches(self):
        groups = {g["title"]: len(g["descriptions"]) for g in self.blocks["groundsOfRefusal"]}
        self.assertEqual(sorted(groups.values()), [6, 15],
                         f"число оснований для отказа изменилось: {groups}")

    def test_33_source_refs_point_into_the_source(self):
        """sourceRef обязан адресовать проверяемый источник, а не выдуманный пункт.

        Проверяемый — это либо ветка машиночитаемой витрины госуслуги, либо
        зарегистрированный источник, файл которого лежит в репозитории и чей
        sha256 сверяется валидатором. Второе строже первого: витрина может
        измениться молча, файл — нет.
        """
        anchors = ("incomingDocuments", "recipientCategories", "groundsOfRefusal", "offDocs")
        sources = json.loads((ROOT / "sources" / "index.json").read_text(encoding="utf-8"))["sources"]
        pinned = {sid for sid, s in sources.items()
                  if s.get("sha256") and (ROOT / (s.get("file") or "нет")).exists()}
        loose = [r["id"] for r in self.profile["rules"]
                 if not r["sourceRef"].startswith(anchors)
                 and r.get("dependsOnSource") not in pinned]
        self.assertEqual(loose, [], f"правила без проверяемого sourceRef: {loose}")
        self.assertTrue(pinned, "ни один источник не подтверждён файлом в репозитории")

    def test_34_normative_acts_registry_is_pinned(self):
        """Реквизиты НПА из offDocs — сигнал о новой редакции. Фиксируем состав."""
        acts = {a["sn"]: a["approvalDate"] for a in self.blocks["offDocs"]}
        self.assertEqual(len(acts), 12)
        self.assertEqual(acts["284-ПП"], "2013-04-30")
        # Требования к материалам в формате IFC — корпус ещё не оцифрован
        self.assertEqual(acts["ДГП-Р-1/26/64-16-6/26"], "2026-01-16")


class TestGraph(unittest.TestCase):
    """Граф для визуализации — производная реестров, а не отдельный источник."""

    def test_35_graph_is_in_sync_with_registries(self):
        import build_graph
        self.assertEqual(build_graph.main.__module__, "build_graph")
        graph = build_graph.build()
        ids = {n["id"] for n in graph["nodes"]}
        dangling = [l for l in graph["links"]
                    if l["source"] not in ids or l["target"] not in ids]
        self.assertEqual(dangling, [], f"висячие рёбра: {dangling[:3]}")

    def test_36_every_rule_is_a_node(self):
        import build_graph
        graph = build_graph.build()
        _s, _m, _d, profiles = build_graph.load()
        rules = {r["id"] for p in profiles.values() for r in p["rules"]}
        in_graph = {n["ruleId"] for n in graph["nodes"] if n["layer"] == "rule"}
        self.assertEqual(rules, in_graph)

    def test_37_deliverables_reference_existing_rules(self):
        import build_graph
        _s, _m, delivs, profiles = build_graph.load()
        rules = {r["id"] for p in profiles.values() for r in p["rules"]}
        for did, d in delivs.items():
            for rid in d.get("producedBy", []) or []:
                self.assertIn(rid, rules, f"{did}.producedBy -> {rid} не существует")


class TestRefusalCoverage(MonitorTestCase):
    """Каждое основание для отказа обязано иметь правило.

    Непокрытое основание — это отказ, который система не предскажет. Проверка
    держит соответствие по номерам, а не по счёту: пропуск в середине списка
    иначе компенсируется лишним правилом в конце.
    """

    GROUPS = {"приём": "Основания для отказа в приеме документов",
              "предоставление": "Основания для отказа в предоставлении услуги"}

    def setUp(self):
        super().setUp()
        import re
        self.re = re
        raw = read("mos-v1.html").decode("utf-8")
        blocks = extract_mod.extract(raw)["data"]["blocks"]
        self.grounds = {g["title"]: len(g["descriptions"])
                        for g in blocks["groundsOfRefusal"]}
        self.rules = self.profiles["agr-request-package"]["rules"]

    def covered(self, key):
        out = set()
        for r in self.rules:
            for m in self.re.finditer(rf"{key}, п\. (\d+)", r["sourceRef"]):
                out.add(int(m.group(1)))
        return out

    def test_38_acceptance_grounds_fully_covered(self):
        total = self.grounds[self.GROUPS["приём"]]
        missing = sorted(set(range(1, total + 1)) - self.covered("приём"))
        self.assertEqual(missing, [], f"основания отказа в приёме без правила: {missing}")

    def test_39_provision_grounds_fully_covered(self):
        total = self.grounds[self.GROUPS["предоставление"]]
        missing = sorted(set(range(1, total + 1)) - self.covered("предоставление"))
        self.assertEqual(missing, [], f"основания отказа в предоставлении без правила: {missing}")


class TestIfcDictionary(unittest.TestCase):
    """Словарь атрибутов ЦИМ АГР — данные, на которые ссылаются правила профиля."""

    @classmethod
    def setUpClass(cls):
        cls.d = json.loads((ROOT / "dictionaries" / "ifc-attributes.v1.json").read_text(encoding="utf-8"))
        cls.p = json.loads(
            (ROOT / "requirements" / "moscow-ifc-agr-2026-01-16.v1.json").read_text(encoding="utf-8"))

    def test_40_every_type_is_declared(self):
        """Тип атрибута обязан быть описан в приложении 3, иначе инспектор его не проверит."""
        used = {a["type"] for c in self.d["classes"].values() for a in c["attributes"] if a.get("type")}
        self.assertTrue(used <= set(self.d["typeMap"]), f"неизвестные типы: {used - set(self.d['typeMap'])}")

    def test_41_attribute_names_follow_prefix(self):
        """п. 5.3.13: имена атрибутов начинаются с RUS_. Опечатки источника фиксируются, а не чинятся."""
        bad = [(k, a["param"]) for k, c in self.d["classes"].items() for a in c["attributes"]
               if a.get("param") and not a["param"].startswith("RUS")]
        self.assertEqual(bad, [], f"атрибуты вне соглашения RUS_: {bad}")

    def test_42_property_sets_follow_prefix(self):
        """п. 5.3.12: учитываются только наборы с префиксом RusSet_."""
        bad = {a["set"] for c in self.d["classes"].values() for a in c["attributes"]
               if a.get("set") and not a["set"].startswith("RusSet_")}
        self.assertEqual(bad, set(), f"наборы вне соглашения RusSet_: {bad}")

    def test_43_every_class_has_a_rule(self):
        """Каждый класс словаря обоснован правилом профиля, иначе он не проверяется ничем."""
        refs = {r["sourceRef"].split("табл. ")[-1] for r in self.p["rules"]
                if r["sourceRef"].startswith("прил. 2")}
        self.assertEqual(set(self.d["classes"]) - refs, set(),
                         f"классы без правила: {set(self.d['classes']) - refs}")

    def test_44_detail_stages_match_table_5_1(self):
        """Стадии берутся из таблицы 5.1, а не проставляются одинаково.

        Ошибка здесь опаснее пропуска: требовать окна и двери на стадии НПМ
        значит отбраковывать корректные модели.
        """
        vpm_only = {"Б.6", "Б.7", "Б.9", "Б.10", "Б.12", "Б.13"}
        for k, c in self.d["classes"].items():
            if k == "Б.0":
                self.assertIsNone(c["detailStages"])
            elif k in vpm_only:
                self.assertEqual(c["detailStages"], ["ВПМ"], f"{k} требуется только на ВПМ")
            else:
                self.assertEqual(c["detailStages"], ["НПМ", "ВПМ"], f"{k} требуется на обеих стадиях")


class TestTepSchema(unittest.TestCase):
    """XML-схема ТЭП — приложение 5. Закрывает документ 8 комплекта запроса."""

    @classmethod
    def setUpClass(cls):
        cls.d = json.loads((ROOT / "dictionaries" / "tep-xml-schema.v1.json").read_text(encoding="utf-8"))

    def test_45_paths_match_nesting(self):
        """path обязан соответствовать depth, иначе вложенность в схеме врёт."""
        root = self.d["rootElement"]
        for e in self.d["elements"]:
            self.assertEqual(len(e["path"].split("/")), e["depth"] + 1,
                             f"{e['name']}: path={e['path']} не соответствует depth={e['depth']}")
            self.assertTrue(e["path"].startswith(root + "/"), f"{e['name']}: путь не от корня")

    def test_46_nested_elements_have_a_parent(self):
        """Элемент глубины 2 обязан лежать внутри существующего complexType."""
        containers = {e["name"] for e in self.d["elements"] if e["type"] == "complexType"}
        for e in self.d["elements"]:
            if e["depth"] == 2:
                parent = e["path"].split("/")[-2]
                self.assertIn(parent, containers, f"{e['name']}: родитель {parent} не complexType")

    def test_47_types_are_known(self):
        known = {"xs:string", "xs:date", "xs:decimal", "xs:integer", "xs:positiveInteger",
                 "complexType", "simpleType (enumeration)"}
        used = {e["type"] for e in self.d["elements"] if e.get("type")}
        self.assertTrue(used <= known, f"неизвестные типы: {used - known}")

    def test_48_tep_composition_covered(self):
        """Показатели из таблицы 7.1 должны иметь поля в схеме — иначе ТЭП не собрать."""
        names = " ".join(e["name"] for e in self.d["elements"])
        for token in ("Height", "Area", "Apartment"):
            self.assertIn(token, names, f"в схеме нет ни одного элемента с «{token}»")


class TestIfcInspector(unittest.TestCase):
    """Инспектор IFC: разбор SPF и сбор фактов под evaluator-ы профиля.

    Тесты держат две вещи сразу: что парсер читает файл верно и что правила,
    написанные до появления инспектора, на живых фактах срабатывают там и
    только там, где должны.
    """

    FIX = ROOT / "tests" / "ifc_fixtures"

    @classmethod
    def setUpClass(cls):
        from lib import ifc
        cls.ifc = ifc
        cls.dict = json.loads(
            (ROOT / "dictionaries" / "ifc-attributes.v1.json").read_text(encoding="utf-8"))
        cls.profile = json.loads(
            (ROOT / "requirements" / "moscow-ifc-agr-2026-01-16.v1.json").read_text(encoding="utf-8"))

    def context(self, name, file_name="НН_К01_С01_АР_АГР.ifc"):
        import os
        text = (self.FIX / name).read_text(encoding="utf-8")
        facts = self.ifc.inspect(text, file_name=file_name,
                                 file_size=len(text.encode()), dictionary=self.dict)
        return {
            "ifc": facts,
            "package": {"fileName": file_name, "fileStem": os.path.splitext(file_name)[0],
                        "signatureNamesMatch": True, "allSignersCovered": True},
            "project": {"locationIsStateSecret": False},
            "tep": {"schemaValid": True},
        }

    def failing(self, name, **kw):
        ctx = self.context(name, **kw)
        return {r["id"] for r in self.profile["rules"]
                if validate_mod._state_for(r, ctx, None) == "auto_fail"}

    def test_49_not_ifc_raises(self):
        with self.assertRaises(self.ifc.IfcError):
            self.ifc.inspect((self.FIX / "not-ifc.ifc").read_text(encoding="utf-8"))

    def test_50_header_is_parsed(self):
        f = self.context("valid.ifc")["ifc"]
        self.assertEqual(f["schema"], "IFC4")
        self.assertEqual(f["mvd"], "ReferenceView_V1.2")
        self.assertEqual(f["lengthUnit"], "MILLIMETRE")

    def test_51_strings_with_quotes_and_commas(self):
        """Токенизатор SPF: '' внутри строки и запятая не должны рвать разбор."""
        f = self.context("tricky-strings.ifc")["ifc"]
        self.assertTrue(f["attributes"]["IfcWall"]["complete"])
        self.assertEqual(self.failing("tricky-strings.ifc"), set())

    def test_52_valid_file_has_no_false_positives(self):
        """Ни одно правило не должно обвинять корректный файл."""
        self.assertEqual(self.failing("valid.ifc"), set())

    def test_53_valid_file_actually_passes_checks(self):
        """Отсутствие провалов бессмысленно, если ничего и не проверялось."""
        counts = validate_mod.evaluate(self.profile, self.context("valid.ifc"))
        self.assertGreaterEqual(counts["auto_pass"], 15)
        self.assertEqual(counts["rule_error"], 0)

    def test_54_wrong_schema_detected(self):
        self.assertIn("ifc-schema-version", self.failing("wrong-schema.ifc"))

    def test_55_metre_unit_detected(self):
        """п. 4.6.1: метрическая система в миллиметрах, не в метрах."""
        self.assertIn("ifc-scale-and-units", self.failing("metre-unit.ifc"))

    def test_56_proxy_detected(self):
        """п. 4.1.4: IfcBuildingElementProxy запрещён."""
        self.assertIn("ifc-no-building-element-proxy", self.failing("has-proxy.ifc"))

    def test_57_incomplete_attributes_detected(self):
        """Стена без RusSet_Quantities и RusSet_Location — неполный набор по Б.4."""
        self.assertIn("ifc-attributes-wall", self.failing("wall-incomplete.ifc"))

    def test_58_wrong_fno_detected(self):
        """п. 5.3.5: RUS_FNO принимает только «Жилое здание» или «Нежилое здание»."""
        self.assertIn("ifc-rus-fno", self.failing("wrong-fno.ifc"))

    def test_59_pset_name_with_space_detected(self):
        """п. 5.3.15: наименования наборов пишутся слитно без пробелов."""
        self.assertIn("ifc-names-written-together", self.failing("pset-name-with-space.ifc"))

    def test_60_absent_class_is_not_a_violation(self):
        """Класса нет в файле — это не нарушение: состав определяется проектом."""
        f = self.context("valid.ifc")["ifc"]
        absent = [v for v in f["attributes"].values() if v["instances"] == 0]
        self.assertTrue(absent)
        self.assertTrue(all(v["complete"] is None for v in absent))

    def test_61_filename_checked_without_extension(self):
        """Расширение содержит точку, запрещённую п. 4.3.1.5 в самом имени."""
        self.assertEqual(self.failing("valid.ifc", file_name="НН_К01_С01_АР_АГР.ifc"), set())
        self.assertIn("ifc-filename-structure", self.failing("valid.ifc", file_name="модель 1.ifc"))


class TestPackageInspector(unittest.TestCase):
    """Инспектор комплекта запроса: то, на чём отказывают на приёме.

    Манифест вместо каталога на диске — фикстура воспроизводима, а проверка
    не зависит от того, где лежат файлы заявителя.
    """

    @classmethod
    def setUpClass(cls):
        cls.deliverables = json.loads(
            (ROOT / "deliverables" / "index.json").read_text(encoding="utf-8"))["deliverables"]
        cls.profile = json.loads(
            (ROOT / "requirements" / "agr-request-package.v1.json").read_text(encoding="utf-8"))
        cls.sections = json.loads(
            (ROOT / "dictionaries" / "request-form-sections.v1.json").read_text(encoding="utf-8"))

    def manifest(self, name):
        return json.loads((ROOT / "tests" / "package_fixtures" / f"{name}.json").read_text(encoding="utf-8"))

    def facts(self, name):
        return package_mod.inspect(self.manifest(name), self.deliverables, self.sections)

    def context(self, name):
        return {
            "package": self.facts(name),
            "applicant": {"kind": "legal_entity"},
            "object": {"isLinear": False},
            "request": {"viaRepresentative": True},
            "representative": {"kind": "legal_entity"},
        }

    def failing(self, name):
        ctx = self.context(name)
        return {r["id"] for r in self.profile["rules"]
                if validate_mod._state_for(r, ctx, None) == "auto_fail"}

    def test_62_complete_package_passes(self):
        """Полный комплект не даёт ни одного провала — и проверок при этом много."""
        counts = validate_mod.evaluate(self.profile, self.context("complete"))
        self.assertEqual(counts["auto_fail"], 0)
        self.assertEqual(counts["rule_error"], 0)
        self.assertGreaterEqual(counts["auto_pass"], 11)

    def test_63_form_is_not_a_missing_document(self):
        """Запрос подаётся формой на Портале: файла нет по определению."""
        f = self.facts("complete")
        self.assertNotIn("request-form", f["mandatoryMissing"])
        self.assertEqual(f["mandatoryMissingCount"], 0)

    def test_63a_form_counts_in_the_data_volume(self):
        """Файла у формы нет, но сведения есть — в объём данных она входит."""
        f = self.facts("complete")
        self.assertEqual(f["dataItemsRequired"], f["mandatoryRequired"] + 1)
        self.assertTrue(f["form"]["present"])
        self.assertEqual(f["dataItemsMissing"], [])

    def test_63b_unfilled_form_breaks_completeness(self):
        """Документы собраны, форма пуста — комплект неполон."""
        f = self.facts("form-empty")
        self.assertEqual(f["mandatoryMissingCount"], 0)
        self.assertEqual(f["dataItemsMissing"], ["request-form"])
        self.assertIn("form-package-complete", self.failing("form-empty"))

    def test_63c_missing_form_section_detected(self):
        """Юрлицо не заполнило раздел 1.1 — запрос неполон по приложению 2."""
        self.assertEqual(self.failing("form-no-applicant-section"), {"form-sec-1-1"})

    def test_63d_sections_of_other_applicant_kinds_are_not_demanded(self):
        """Заявитель — юрлицо: разделы про ИП и физлицо к нему не относятся."""
        ctx = self.context("complete")
        by_id = {r["id"]: r for r in self.profile["rules"]}
        for rid in ("form-sec-1-2", "form-sec-1-3", "form-sec-2-2"):
            self.assertEqual(validate_mod._state_for(by_id[rid], ctx, None), "not_applicable", rid)

    def test_63e_interpreted_applicability_is_not_a_blocker(self):
        """Обязательность разделов 3-11 источником не оговорена: это наш вывод.

        Цена ошибки в интерпретации не должна равняться отказу в приёме,
        поэтому такие правила имеют severity major, а не blocker.
        """
        dict_by_num = {s["number"]: s for s in self.sections["sections"]}
        for r in self.profile["rules"]:
            if not r["id"].startswith("form-sec-") or "-" in r["id"][9:]:
                continue
            num = r["id"][len("form-sec-"):]
            applicability = dict_by_num[num]["applicability"]
            if applicability == "interpreted":
                self.assertEqual(r["severity"], "major", r["id"])
            elif applicability == "undetermined":
                self.assertEqual(r["verification"], "expert_evidence", r["id"])

    def test_63f_every_section_of_appendix_2_has_a_rule(self):
        """Оцифровка полна: 11 разделов приложения 2 покрыты правилами.

        Разделы 1 и 2 существуют только через подразделы — у них своих полей
        нет, условия заполнения источник задаёт именно на подразделах.
        """
        covered = {r["id"][len("form-sec-"):].replace("-", ".")
                   for r in self.profile["rules"] if r["id"].startswith("form-sec-")}
        expected = set()
        for sec in self.sections["sections"]:
            subs = sec.get("subsections") or []
            expected.update(s["number"] for s in subs) if subs else expected.add(sec["number"])
        self.assertEqual(covered, expected)
        self.assertEqual(len(self.sections["sections"]), 11)

    def test_64_missing_document_detected(self):
        """Нет СПОЗУ — падает и правило документа, и полнота комплекта."""
        self.assertEqual(self.failing("missing-spozu"), {"doc-04-spozu", "form-package-complete"})
        self.assertEqual(self.facts("missing-spozu")["mandatoryMissing"], ["spozu"])

    def test_65_wrong_format_detected(self):
        """ТЭП принимается только XML: он формируется из состава ЦИМ, а не рисуется."""
        self.assertEqual(self.failing("tep-as-pdf"), {"doc-08-tep-table", "form-readable"})

    def test_66_missing_signature_detected(self):
        self.assertFalse(self.facts("npm-unsigned")["documents"]["npm-package"]["signatureOk"])
        self.assertEqual(self.facts("npm-unsigned")["signatureMismatch"], 1)

    def test_67_signature_name_must_match(self):
        """Подпись есть, но названа иначе — сопоставить её с файлом нечем."""
        self.assertFalse(self.facts("vpm-signature-renamed")["documents"]["vpm-package"]["signatureOk"])

    def test_68_oversize_detected(self):
        """1 ГБ на пакет НПМ — граница, а не ориентир."""
        self.assertFalse(self.facts("npm-oversize")["documents"]["npm-package"]["sizeOk"])
        self.assertTrue(self.facts("complete")["documents"]["npm-package"]["sizeOk"])

    def test_69_gpzu_must_survive_the_review(self):
        """ГПЗУ, истекающий в ходе рассмотрения, — основание для отказа."""
        self.assertTrue(self.facts("complete")["gpzu"]["survivesReview"])
        self.assertIn("ext-gpzu-survives-review", self.failing("gpzu-expiring"))

    def test_70_container_counts_as_valid_format(self):
        """СПОЗУ подаётся как IFC внутри ZIP — снаружи виден ZIP, и это норма."""
        self.assertTrue(self.facts("complete")["documents"]["spozu"]["formatValid"])

    def test_71_inspector_does_not_touch_the_disk(self):
        """Инспектор работает с манифестом. Обращение к файловой системе — дефект."""
        real = package_mod.os.path.getsize
        package_mod.os.path.getsize = lambda p: self.fail("инспектор полез на диск")
        try:
            self.facts("complete")
        finally:
            package_mod.os.path.getsize = real


class TestMaterialsInspector(unittest.TestCase):
    """Приложение 1: состав материалов АГР и требования к ним.

    Материалы АГР — самый крупный документ комплекта, и до сих пор он был
    единственным, чей внутренний состав нигде не описан.
    """

    @classmethod
    def setUpClass(cls):
        cls.profile = json.loads(
            (ROOT / "requirements" / "agr-materials-composition.v1.json").read_text(encoding="utf-8"))
        cls.items = cls.profile["compositionItems"]

    def context(self, name, **over):
        manifest = json.loads(
            (ROOT / "tests" / "materials_fixtures" / f"{name}.json").read_text(encoding="utf-8"))
        ctx = {
            "materials": materials_mod.inspect(manifest, self.items),
            "object": {"inHeritageTerritory": False, "hasSignage": False, "isLinear": False,
                       "isTransportStructure": False, "hasMultipleBuildings": False,
                       "hasRetainedBuildings": False, "isAccommodationFacility": False,
                       "isPartialReconstruction": False, "isCompositionallyActive": False,
                       "modelsRequired": True, "isRoadBridge": False, "hasMayorExemption": False},
            "territory": {"hasApprovedPpt": False, "hasProtectedZones": False, "isAerodrome": False},
            "applicant": {"isRenovationFund": False, "isRenovationFundOrRoadAuthority": False},
        }
        ctx.update(over)
        return ctx

    def failing(self, name, **over):
        ctx = self.context(name, **over)
        return {r["id"] for r in self.profile["rules"]
                if validate_mod._state_for(r, ctx, None) == "auto_fail"}

    def test_79_complete_materials_pass(self):
        counts = validate_mod.evaluate(self.profile, self.context("complete"))
        self.assertEqual(counts["auto_fail"], 0)
        self.assertEqual(counts["rule_error"], 0)
        self.assertGreaterEqual(counts["auto_pass"], 20)

    def test_80_missing_composition_item_detected(self):
        """Схем разрезов нет в файле — пункт 1.9 не выполнен."""
        self.assertEqual(self.failing("no-sections-scheme"), {"mat-1-9-sections"})

    def test_81_bookmarks_must_cover_what_is_in_the_file(self):
        """п. 2.1: закладки формируются по перечню раздела 1."""
        self.assertEqual(self.failing("missing-bookmarks"), {"mat-2-1-bookmarks"})

    def test_82_password_and_multiple_files_detected(self):
        """п. 2.1: один файл, без защиты паролем."""
        self.assertEqual(self.failing("password-protected"), {"mat-2-1-single-pdf"})
        self.assertEqual(self.failing("two-files"), {"mat-2-1-single-pdf"})

    def test_83_coordinate_precision_depends_on_the_system(self):
        """п. 2.1.4: 7 знаков для WGS84, 2 знака для МСК77 — порог разный."""
        self.assertIn("mat-2-1-4-coordinates", self.failing("coords-wgs84-coarse"))
        self.assertEqual(self.failing("coords-wgs84-fine"), set())
        self.assertEqual(self.failing("complete"), set())

    def test_84_foreign_coordinate_system_is_its_own_violation(self):
        """Чужая система координат — нарушение, а не низкая точность.

        Требование к числу знаков для неё попросту не определено, поэтому
        точность уходит в pending, а нарушением объявляется сама система.
        """
        self.assertIn("mat-2-1-4-coord-system", self.failing("coords-unknown-system"))
        self.assertNotIn("mat-2-1-4-coordinates", self.failing("coords-unknown-system"))

    def test_85_tep_indicators_checked_one_by_one(self):
        """п. 2.1.11: каждый показатель ведомости — отдельное требование."""
        self.assertEqual(self.failing("tep-incomplete"),
                         {"mat-2-1-11-underground-area", "mat-2-1-11-parking"})

    def test_86_transport_structures_have_a_shorter_tep_list(self):
        """п. 2.1.11(1): требовать полный перечень с моста нельзя."""
        short = set(materials_mod.TEP_TRANSPORT)
        self.assertTrue(short < set(materials_mod.TEP_INDICATORS))
        by_id = {r["id"]: r for r in self.profile["rules"]}
        self.assertIn("mat-2-1-11-1-transport-objects", by_id)
        self.assertEqual(by_id["mat-2-1-11-1-transport-objects"]["appliesWhen"],
                         {"path": "object.isTransportStructure", "equals": True})

    def test_87_model_exemptions_are_transcribed(self):
        """Разделы 3.2, 3.3 и 3.5 снимают обязанность делать модели.

        Без них профили НПМ/ВПМ требовали бы модели с дорожно-мостовых
        объектов и с фонда реновации, то есть отбраковывали бы корректные
        комплекты.
        """
        by_id = {r["id"]: r for r in self.profile["rules"]}
        for rid, path in (("mat-3-2-road-bridge-npm-only", "object.isRoadBridge"),
                          ("mat-3-3-renovation-npm-only", "applicant.isRenovationFund"),
                          ("mat-3-5-mayor-exemption", "object.hasMayorExemption")):
            self.assertEqual(by_id[rid]["appliesWhen"]["path"], path, rid)

    def test_88_repealed_clauses_are_recorded_not_dropped(self):
        """Отменённый пункт обязан быть виден как отменённый.

        Иначе «пропустили при транскрипции» и «отменено законодателем»
        выглядят одинаково — пустым местом.
        """
        repealed = self.profile["repealed"]
        self.assertEqual(len(repealed), 18)
        clauses = {r["clause"] for r in repealed}
        self.assertIn("1.1", clauses)
        self.assertIn("2.1.7.4", clauses)
        rule_refs = {r["sourceRef"].split("п. ")[1] for r in self.profile["rules"]}
        self.assertEqual(clauses & rule_refs, set(),
                         "пункт числится и действующим, и отменённым")

    def test_89_tep_gap_against_the_xml_schema_is_pinned(self):
        """Три показателя ведомости ТЭП не имеют элемента в XML-схеме.

        Приложение 1 к Регламенту требует девять показателей, приложение 5
        распоряжения от 16.01.2026 описывает шесть из них. Оба документа
        действующие, свести их — не наше решение, поэтому расхождение
        зафиксировано как данные. Тест сторожит его от молчаливого дрейфа:
        появится элемент в схеме или изменится перечень в Регламенте — тест
        упадёт и позовёт человека.
        """
        schema = json.loads(
            (ROOT / "dictionaries" / "tep-xml-schema.v1.json").read_text(encoding="utf-8"))
        mapping = schema["crossCheck"]["mapping"]
        self.assertEqual(set(mapping), set(materials_mod.TEP_INDICATORS))
        unmapped = {k for k, v in mapping.items() if v["element"] is None}
        self.assertEqual(unmapped, {"built-up-area", "absolute-height", "parking"})
        names = {e["name"] for e in schema["elements"]}
        for key, v in mapping.items():
            if v["element"]:
                self.assertIn(v["element"], names, f"{key} ссылается на несуществующий элемент")
            self.assertTrue(v.get("comment") or v["element"],
                            f"{key} без элемента и без объяснения")


class TestModelInspectors(unittest.TestCase):
    """FBX, PNG, ZIP и GeoJSON — приёмочные файлы моделей.

    До появления этих инспекторов автопроверки НПМ и ВПМ смотрели на glb.* —
    производный просмотрочный файл. Ведомство принимает FBX.
    """

    FBX = ROOT / "tests" / "fbx_fixtures"
    PNG = ROOT / "tests" / "texture_fixtures"
    ZIP = ROOT / "tests" / "archive_fixtures"

    def fbx(self, name):
        return fbx_mod.inspect((self.FBX / f"{name}.fbx").read_bytes(), f"{name}.fbx")

    def test_90_binary_fbx_parsed(self):
        f = self.fbx("npm-oks-valid")
        self.assertEqual(f["version"], 7400)
        self.assertEqual(f["triangles"], 1200)
        self.assertEqual(f["materials"], 3)
        self.assertTrue(f["triangulated"])
        self.assertTrue(f["metreScale"])

    def test_91_wide_offsets_of_fbx_7500(self):
        """С версии 7500 смещения в записях узлов 64-битные."""
        f = self.fbx("fbx-7500-wide")
        self.assertEqual(f["version"], 7500)
        self.assertEqual(f["triangles"], 30)

    def test_92_ascii_fbx_is_refused_not_guessed(self):
        """Требования предписывают binary. Угадывать текстовый формат нельзя."""
        with self.assertRaises(fbx_mod.FbxError):
            self.fbx("not-fbx")

    def test_93_quads_are_not_triangulated(self):
        """Полигон с четырьмя вершинами виден прямо в PolygonVertexIndex."""
        f = self.fbx("quads-not-triangulated")
        self.assertFalse(f["triangulated"])
        self.assertEqual(f["triangles"], 100)
        self.assertGreater(f["nonTriangulatedPolygons"], 0)

    def test_94_centimetre_scene_detected(self):
        """UnitScaleFactor 1 — сантиметры; требование «1 единица = 1 метр» это 100."""
        self.assertFalse(self.fbx("centimetre-scale")["metreScale"])

    def test_95_scene_clean_can_only_be_refuted(self):
        """Машина опровергает чистоту сцены, но не подтверждает её.

        Люди, транспорт и коммуникации из байтов не опознаются, поэтому
        отсутствие камер и костей соответствием не является — это None,
        и подтверждает его человек.
        """
        self.assertFalse(self.fbx("scene-not-clean")["sceneClean"])
        self.assertIsNone(self.fbx("npm-oks-valid")["sceneClean"])

    def test_96_embedded_textures_detected(self):
        """Текстуры предоставляются отдельными PNG, а не внутри FBX."""
        self.assertEqual(self.fbx("embedded-textures")["embeddedTextures"], 2)
        self.assertEqual(self.fbx("npm-oks-valid")["embeddedTextures"], 0)

    def test_97_triangle_threshold_measured_on_generated_scene(self):
        """Порог 150 000 проверяется на файле, который его действительно перешёл.

        Такая сцена весит сотни килобайт и в репозитории не хранится —
        собирается тем же генератором, что и мелкие фикстуры.
        """
        sys.path.insert(0, str(self.FBX))
        from make_fixtures import scene  # noqa: E402
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.fbx"
            scene(str(path), tris=150_050)
            f = fbx_mod.inspect(path.read_bytes())
        self.assertEqual(f["triangles"], 150_050)
        self.assertGreater(f["triangles"], 150_000)

    def test_98_png_ihdr_read(self):
        names = sorted(str(p) for p in self.PNG.glob("*.png"))
        f = raster_mod.inspect(names, allowed_sizes=(256, 512, 1024, 2048),
                               max_bytes=3 * 1024 * 1024, require_no_alpha=True)
        self.assertEqual(f["notPng"], ["not_a_png.png"])
        self.assertIn("non_square_512x256.png", f["wrongSize"])
        self.assertIn("odd_size_300.png", f["wrongSize"])
        self.assertEqual(f["withAlpha"], ["with_alpha_512.png"])
        self.assertEqual(f["notEightBit"], ["sixteen_bit_512.png"])
        self.assertFalse(f["sizesValid"])
        self.assertFalse(f["alphaFree"])

    def test_99_zip_integrity(self):
        self.assertTrue(archive_mod.inspect(str(self.ZIP / "clean.zip"))["valid"])
        traversal = archive_mod.inspect(str(self.ZIP / "traversal.zip"))
        self.assertFalse(traversal["valid"])
        self.assertEqual(traversal["dangerousPaths"], ["../../etc/passwd"])
        dup = archive_mod.inspect(str(self.ZIP / "duplicate.zip"))
        self.assertEqual(dup["duplicateNames"], ["a.fbx"])
        broken = archive_mod.inspect(str(self.ZIP / "not-a-zip.zip"))
        self.assertFalse(broken["valid"])
        self.assertIn("BadZipFile", broken["error"])

    def test_100_geojson_precision_is_about_the_written_form(self):
        """«Ровно 3 знака» — требование к записи, а не к значению.

        Разбор JSON запись теряет: 2.500 становится числом 2.5. Поэтому
        литералы читаются из текста, иначе проверка была бы невыполнимой.
        """
        ok = b'{"type":"Feature","properties":{"coordinates":[12345.678, 9876.543]}}'
        trailing = b'{"type":"Feature","properties":{"coordinates":[12345.6780, 9876.543]}}'
        short = b'{"type":"Feature","properties":{"coordinates":[12345.68, 9876.543]}}'
        self.assertTrue(geojson_mod.inspect(ok)["coordinatesPrecision3"])
        self.assertFalse(geojson_mod.inspect(trailing)["coordinatesPrecision3"])
        self.assertFalse(geojson_mod.inspect(short)["coordinatesPrecision3"])

    def test_101_geojson_duplicate_and_string_numbers(self):
        """п. 2.5, 2.7: числа записываются числами, имена полей не дублируются."""
        dup = b'{"type":"Feature","properties":{"h_abs":1,"h_abs":2}}'
        self.assertEqual(geojson_mod.inspect(dup)["duplicateFields"], ["h_abs"])
        self.assertFalse(geojson_mod.inspect(dup)["valid"])
        as_string = b'{"type":"Feature","properties":{"h_abs":"12.5"}}'
        self.assertEqual(geojson_mod.inspect(as_string)["numericFieldsAsString"], ["h_abs"])

    def test_102_geojson_completeness_is_three_valued(self):
        """Неизвестное не выдаётся за соответствие.

        Полный перечень полей задан приложением 10, которого в репозитории
        нет. Поэтому нарушение доказуемо, а соответствие — нет: отсутствие
        известного поля даёт False, а его наличие даёт None, но не True.
        """
        d = json.loads((ROOT / "dictionaries" / "geojson-fields.v1.json").read_text(encoding="utf-8"))
        full = {f: "x" for f in d["scopes"]["oks"]["fields"]}
        raw = json.dumps({"type": "Feature", "properties": full}).encode()
        self.assertIsNone(geojson_mod.inspect(raw, d, "oks")["oksRequiredComplete"])
        without = dict(full)
        del without["h_abs"]
        raw2 = json.dumps({"type": "Feature", "properties": without}).encode()
        f = geojson_mod.inspect(raw2, d, "oks")
        self.assertFalse(f["oksRequiredComplete"])
        self.assertEqual(f["oksMissingFields"], ["h_abs"])
        # У рекламных конструкций перечень назван исчерпывающе — там True достижим
        ad = {f: "x" for f in d["scopes"]["advertising"]["fields"]}
        raw3 = json.dumps({"type": "Feature", "properties": ad}).encode()
        self.assertTrue(geojson_mod.inspect(raw3, d, "advertising")["advertisingRequiredComplete"])

    def test_103_published_profile_versions_do_not_collide(self):
        """Две редакции одного профиля обязаны различаться и не перекрывать друг друга."""
        seen, live = {}, {}
        for path in sorted((ROOT / "requirements").glob("*.json")):
            p = json.loads(path.read_text(encoding="utf-8"))
            key = (p["id"], p["version"])
            self.assertNotIn(key, seen, f"{path.name} повторяет {seen.get(key)}")
            seen[key] = path.name
            if p["status"] != "superseded":
                live.setdefault(p["id"], []).append(path.name)
        for pid, names in live.items():
            self.assertEqual(len(names), 1, f"{pid}: не перекрытых редакций {names}")

    def test_104_status_is_outside_the_self_hash(self):
        """Иначе объявленный жизненный цикл невозможен.

        Перевод активной редакции в superseded менял бы её собственный хеш,
        а править опубликованный профиль запрещено. Хеш отвечает на вопрос
        «то же ли это требование», а не «в каком оно статусе».
        """
        p = json.loads((ROOT / "requirements" / "moscow-npm-2026-08-18.v1.json").read_text(encoding="utf-8"))
        before = validate_mod.self_hash(p)
        p["status"] = "active"
        self.assertEqual(validate_mod.self_hash(p), before)
        p["rules"][0]["requirement"] += "."
        self.assertNotEqual(validate_mod.self_hash(p), before)
