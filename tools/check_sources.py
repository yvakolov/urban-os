#!/usr/bin/env python3
"""Проверка официальных источников на изменение.

    python3 tools/check_sources.py                          проверить все включённые
    python3 tools/check_sources.py --source-id msk-284-pp   один источник
    python3 tools/check_sources.py --fixture <file.html>    без сети, из файла
    python3 tools/check_sources.py --approve <id> --snapshot <rawHash>
    python3 tools/check_sources.py --report reports/source-check.json

Инструмент ОБНАРУЖИВАЕТ изменение источника. Он никогда не меняет requirements и
никогда не утверждает baseline сам. Изменение источника не означает изменения
требования — это означает, что человек обязан пересмотреть их заново.

Код возврата 0 даже при обнаруженном изменении: сигналом служит отчёт и issue,
а не падение джобы. Ненулевой код — только внутренняя ошибка инструмента.
"""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import extract as extract_mod  # noqa: E402
from lib import impact as impact_mod  # noqa: E402
from lib import normalize as norm_mod  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "sources" / "index.json"
MONITOR_FILE = ROOT / "monitoring" / "sources.json"
REQ_DIR = ROOT / "requirements"
SNAP_DIR = ROOT / "sources" / "snapshots"

USER_AGENT = "urban-os-source-monitor/1.0 (+https://github.com/yvakolov/urban-os)"
ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9"
TIMEOUT = 30
RETRIES = 3
RETRY_PAUSE = 5


# ---------------------------------------------------------------- сеть

class FetchError(Exception):
    pass


def fetch(url):
    """Вернуть (raw_bytes, meta). Ретраи внутри прогона — от одиночного 503.

    verify=False здесь нет и быть не может: вся ценность в аутентичности контента.
    Ошибка TLS — это UNAVAILABLE, а не повод понизить требования к соединению.
    """
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept-Language": ACCEPT_LANGUAGE}
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw, {
                    "status": resp.status,
                    "contentType": resp.headers.get("Content-Type", ""),
                    "attempt": attempt,
                }
        except Exception as exc:  # включая URLError, ssl.SSLError, socket.timeout
            last = exc
            if attempt < RETRIES:
                time.sleep(RETRY_PAUSE)
    raise FetchError(f"{type(last).__name__}: {last}")


# ---------------------------------------------------------------- снапшоты

def snapshot_dir(source_id):
    return SNAP_DIR / source_id


def write_snapshot(source_id, raw, meta):
    """Immutable: имя по хешу СЫРЬЯ, не нормализованного вида.

    Иначе бамп версии нормализатора переименовал бы существующие снапшоты и
    аннулировал историю. Сырьё позволяет пересчитать старый нормализованный хеш
    новым кодом, не доверяя новому скачиванию.
    """
    raw_hash = hashlib.sha256(raw).hexdigest()
    d = snapshot_dir(source_id)
    d.mkdir(parents=True, exist_ok=True)
    short = raw_hash[:12]

    blob = d / f"{short}.raw.gz"
    if not blob.exists():
        blob.write_bytes(gzip.compress(raw))

    meta_path = d / f"{short}.meta.json"
    if not meta_path.exists():
        meta_path.write_text(
            json.dumps({**meta, "rawHash": raw_hash, "sourceId": source_id},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return raw_hash, short


def read_snapshot(source_id, short):
    blob = snapshot_dir(source_id) / f"{short}.raw.gz"
    if not blob.exists():
        raise FileNotFoundError(f"снапшот {short} для {source_id} не найден")
    return gzip.decompress(blob.read_bytes())


def load_baseline(source_id):
    p = snapshot_dir(source_id) / "baseline.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_baseline(source_id, payload):
    d = snapshot_dir(source_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "baseline.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------- пайплайн

def process(source_id, monitor, sources, profiles, raw=None, http_meta=None):
    """Вернуть событие по одному источнику."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    baseline = load_baseline(source_id)
    ev = {
        "sourceId": source_id,
        "name": sources.get(source_id, {}).get("authority", source_id),
        "url": monitor.get("url"),
        "checkedAt": now,
        "event": None,
        "classification": None,
        "previousHash": (baseline or {}).get("normalizedHash"),
        "currentHash": None,
    }

    if raw is None:
        try:
            raw, http_meta = fetch(monitor["url"])
        except FetchError as exc:
            ev.update(event="UNAVAILABLE", detail=str(exc))
            ev["classification"] = impact_mod.classify("UNAVAILABLE", None)
            return ev

    http_meta = http_meta or {}
    expected_ct = monitor.get("expectedContentType")
    if expected_ct and expected_ct not in http_meta.get("contentType", expected_ct):
        ev.update(event="PARSER_ERROR",
                  detail=f"content-type {http_meta.get('contentType')!r}, ожидался {expected_ct!r}")
        ev["classification"] = impact_mod.classify("PARSER_ERROR", None)
        return ev

    raw_hash, short = write_snapshot(source_id, raw, {"fetchedAt": now, "url": monitor.get("url"), **http_meta})
    ev["rawHash"] = raw_hash
    ev["snapshot"] = short

    ex_spec = monitor["extraction"]
    inv = dict(ex_spec.get("invariants") or {})
    if baseline and baseline.get("payloadBytes"):
        inv["baselinePayloadBytes"] = baseline["payloadBytes"]

    try:
        extracted = extract_mod.extract(raw.decode("utf-8", "replace"), inv)
    except extract_mod.ExtractionError as exc:
        ev.update(event="PARSER_ERROR", detail=str(exc))
        ev["classification"] = impact_mod.classify("PARSER_ERROR", None)
        return ev

    n_spec = monitor["normalization"]
    current = norm_mod.normalize(extracted["data"], n_spec)
    ev["currentHash"] = norm_mod.content_hash(current)
    ev["payloadBytes"] = extracted["payloadBytes"]
    ev["extractionVersion"] = extracted["extractionVersion"]
    ev["normalizationVersion"] = norm_mod.NORMALIZATION_VERSION

    if baseline is None:
        ev.update(event="BASELINE_MISSING",
                  detail="baseline не утверждён; снапшот сохранён, автопринятия нет")
        ev["classification"] = impact_mod.classify("BASELINE_MISSING", None)
        return ev

    if (baseline.get("extractionVersion") != extracted["extractionVersion"]
            or baseline.get("normalizationVersion") != norm_mod.NORMALIZATION_VERSION):
        ev.update(
            event="BASELINE_STALE",
            detail=(f"baseline снят на extraction v{baseline.get('extractionVersion')}/"
                    f"normalization v{baseline.get('normalizationVersion')}, сейчас "
                    f"v{extracted['extractionVersion']}/v{norm_mod.NORMALIZATION_VERSION} — "
                    "требуется переутверждение, сравнение недостоверно"),
        )
        ev["classification"] = impact_mod.classify("BASELINE_STALE", None)
        return ev

    if ev["currentHash"] == baseline.get("normalizedHash"):
        ev.update(event="UNCHANGED")
        ev["classification"] = impact_mod.classify("UNCHANGED", None)
        return ev

    # изменение: восстанавливаем baseline из СЫРЬЯ, а не доверяем сохранённому хешу
    try:
        base_raw = read_snapshot(source_id, baseline["snapshot"])
        base_extracted = extract_mod.extract(base_raw.decode("utf-8", "replace"), {})
        before = norm_mod.normalize(base_extracted["data"], n_spec)
    except (FileNotFoundError, extract_mod.ExtractionError) as exc:
        ev.update(event="BASELINE_STALE", detail=f"снапшот baseline не восстанавливается: {exc}")
        ev["classification"] = impact_mod.classify("BASELINE_STALE", None)
        return ev

    branches = norm_mod.changed_branches(before, current)
    imp = impact_mod.analyse(source_id, branches, sources, profiles, monitor)

    ev.update(
        event="CHANGED",
        changedBranches=branches,
        impact=imp,
        maxSeverity=imp["maxSeverity"],
        affectedProfiles=imp["affectedProfiles"],
        affectedRules=imp["affectedRules"],
        diff={b: norm_mod.branch_diff(before, current, b) for b in branches},
    )
    ev["classification"] = impact_mod.classify("CHANGED", imp)
    return ev


# ---------------------------------------------------------------- CLI

def load_all():
    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))["sources"]
    monitors = json.loads(MONITOR_FILE.read_text(encoding="utf-8"))["monitors"]
    profiles = {}
    for p in sorted(REQ_DIR.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        profiles[d["id"]] = d
    return sources, monitors, profiles


def do_approve(source_id, short, monitors, who):
    """Утверждение baseline. В СЕТЬ НЕ ХОДИТ — принципиально.

    Человек утверждает ровно тот контент, который видел в отчёте прогона. Если бы
    инструмент скачивал заново, он утвердил бы версию, которую никто не смотрел, и
    human-in-the-loop стал бы декорацией.
    """
    monitor = monitors[source_id]
    raw = read_snapshot(source_id, short)
    extracted = extract_mod.extract(raw.decode("utf-8", "replace"),
                                    monitor["extraction"].get("invariants") or {})
    normalized = norm_mod.normalize(extracted["data"], monitor["normalization"])
    meta_path = snapshot_dir(source_id) / f"{short}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    payload = {
        "snapshot": short,
        "rawHash": meta.get("rawHash"),
        "normalizedHash": norm_mod.content_hash(normalized),
        "payloadBytes": extracted["payloadBytes"],
        "targetId": extracted["targetId"],
        "extractionVersion": extracted["extractionVersion"],
        "normalizationVersion": norm_mod.NORMALIZATION_VERSION,
        "approvedBy": who,
        "approvedOn": dt.date.today().isoformat(),
        "httpMeta": {k: meta.get(k) for k in ("status", "contentType", "fetchedAt", "url")},
    }
    save_baseline(source_id, payload)
    return payload


def main():
    ap = argparse.ArgumentParser(description="Проверка официальных источников на изменение")
    ap.add_argument("--source-id")
    ap.add_argument("--fixture", help="локальный файл вместо сетевого запроса")
    ap.add_argument("--report", help="куда положить JSON-отчёт")
    ap.add_argument("--approve", metavar="SOURCE_ID", help="утвердить baseline из сохранённого снапшота")
    ap.add_argument("--snapshot", help="короткий rawHash снапшота для --approve")
    ap.add_argument("--by", default="unknown", help="кто утверждает")
    args = ap.parse_args()

    sources, monitors, profiles = load_all()

    if args.approve:
        if not args.snapshot:
            ap.error("--approve требует --snapshot <rawHash12>")
        payload = do_approve(args.approve, args.snapshot, monitors, args.by)
        print(f"baseline для {args.approve} утверждён: снапшот {payload['snapshot']}")
        print(f"  normalizedHash: {payload['normalizedHash']}")
        print("\nrequirements НЕ изменены. Профили, которые следует пересмотреть:")
        imp = impact_mod.analyse(args.approve, [], sources, profiles, monitors[args.approve])
        for pid in imp["affectedProfiles"] or ["— нет привязанных профилей"]:
            print(f"  · {pid}")
        return 0

    selected = [args.source_id] if args.source_id else [
        sid for sid, m in monitors.items() if m.get("enabled")
    ]

    raw = http_meta = None
    if args.fixture:
        if len(selected) != 1:
            ap.error("--fixture требует --source-id")
        raw = Path(args.fixture).read_bytes()
        http_meta = {"status": 200, "contentType": "text/html", "attempt": 0, "fixture": args.fixture}

    events = [process(sid, monitors[sid], sources, profiles, raw, http_meta) for sid in selected]
    report = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "events": events,
    }

    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for e in events:
        line = f"{e['sourceId']}: {e['event']} · {e['classification']}"
        if e.get("maxSeverity"):
            line += f" · max severity {e['maxSeverity']}"
        if e.get("affectedRules"):
            line += f" · правил затронуто {len(e['affectedRules'])}"
        print(line)
        if e.get("detail"):
            print(f"    {e['detail']}")
        if e.get("snapshot"):
            print(f"    снапшот {e['snapshot']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
