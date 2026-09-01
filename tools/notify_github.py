#!/usr/bin/env python3
"""Создание и обновление GitHub Issue по отчёту check_sources.

    python3 tools/notify_github.py reports/source-check.json          # боевой режим
    python3 tools/notify_github.py reports/source-check.json --print  # только текст

Использует `gh` CLI, который предустановлен на раннерах. Никаких зависимостей.

Дедупликация. Ключ подавления — sourceId + previousHash + newHash, и ищется он
ТОЛЬКО среди открытых issue. Ключ без previousHash ломается на сценарии A -> B -> A:
возврат к прежнему состоянию источника выглядел бы как уже виденный хеш и молча
подавлялся. Закрытые issue ничего не подавляют — если человек закрыл вопрос, а
источник снова уехал, звать надо заново.

На один sourceId держим один «катящийся» открытый issue; новые хеши добавляются
комментариями. Иначе одно волатильное поле, протёкшее в whitelist, породит по
issue в неделю навсегда.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

LABEL = "regulatory-change"

CHECKLIST = """
### Требуемые действия

- [ ] проверить официальный источник
- [ ] определить юридическую значимость изменения
- [ ] сопоставить изменение с requirements
- [ ] при необходимости создать новую версию profile
- [ ] обновить baseline только после review
- [ ] закрыть issue
"""


def marker(ev):
    return f"<!-- urban-os:{ev['sourceId']}:{ev.get('previousHash')}:{ev.get('currentHash')} -->"


def title(ev):
    return f"[REGULATORY CHANGE] {ev['sourceId']}"


def render(ev):
    """Тело issue и есть человекочитаемый отчёт. Второго рендерера не делаем."""
    lines = [
        marker(ev),
        "",
        f"**Источник:** {ev.get('name')}",
        f"**sourceId:** `{ev['sourceId']}`",
        f"**URL:** {ev.get('url')}",
        f"**Обнаружено:** {ev.get('checkedAt')}",
        "",
        f"**Событие:** `{ev['event']}` · **классификация:** `{ev['classification']}`",
        "",
        f"| | |",
        f"|---|---|",
        f"| Previous hash | `{ev.get('previousHash')}` |",
        f"| New hash | `{ev.get('currentHash')}` |",
        f"| Снапшот | `{ev.get('snapshot')}` |",
    ]
    if ev.get("maxSeverity"):
        lines.append(f"| Максимальная severity | **{ev['maxSeverity']}** |")
    lines.append("")

    if ev.get("detail"):
        lines += [f"> {ev['detail']}", ""]

    if ev.get("changedBranches"):
        lines += ["**Изменившиеся разделы:** " + ", ".join(f"`{b}`" for b in ev["changedBranches"]), ""]

    if ev.get("affectedProfiles"):
        lines += ["### Затронутые профили", ""]
        lines += [f"- `{p}`" for p in ev["affectedProfiles"]]
        lines.append("")

    if ev.get("affectedRules"):
        lines += ["### Затронутые правила", "",
                  "| правило | severity | название |", "|---|---|---|"]
        for r in ev["affectedRules"]:
            lines.append(f"| `{r['ruleId']}` | {r['severity']} | {r['title']} |")
        lines.append("")
        lines += [
            "> Это **не** означает, что правила изменились. Это означает: источник",
            "> под ними сдвинулся, и они требуют повторной юридической верификации.",
            "",
        ]

    for branch, diff in (ev.get("diff") or {}).items():
        if not diff:
            continue
        lines += [f"<details><summary>diff · {branch}</summary>", "", "```diff", diff, "```", "", "</details>", ""]

    lines.append(CHECKLIST)
    lines += [
        "---",
        "",
        "После review baseline утверждается вручную:",
        "",
        "```bash",
        f"python3 tools/check_sources.py --approve {ev['sourceId']} \\",
        f"    --snapshot {ev.get('snapshot')} --by <ваш-логин>",
        "```",
    ]
    return "\n".join(lines)


def gh(*args, check=True):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check).stdout


def find_open_issue(source_id):
    out = gh("issue", "list", "--state", "open", "--label", LABEL,
             "--search", source_id, "--json", "number,title,body", check=False)
    try:
        for item in json.loads(out or "[]"):
            if item["title"] == f"[REGULATORY CHANGE] {source_id}":
                return item
    except json.JSONDecodeError:
        pass
    return None


def already_reported(issue, ev):
    if marker(ev) in (issue.get("body") or ""):
        return True
    out = gh("issue", "view", str(issue["number"]), "--json", "comments", check=False)
    try:
        for c in json.loads(out or "{}").get("comments", []):
            if marker(ev) in (c.get("body") or ""):
                return True
    except json.JSONDecodeError:
        pass
    return False


def notify(ev, dry_run=False):
    body = render(ev)
    if dry_run:
        print(body)
        return "printed"

    issue = find_open_issue(ev["sourceId"])
    if issue is None:
        gh("issue", "create", "--title", title(ev), "--body", body, "--label", LABEL)
        return "created"
    if already_reported(issue, ev):
        return "duplicate"
    gh("issue", "comment", str(issue["number"]), "--body", body)
    return "commented"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--print", dest="dry", action="store_true", help="только вывести текст")
    args = ap.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    actions = []
    for ev in report["events"]:
        # Об изменении зовём человека. Технические сбои в issue не превращаем:
        # одиночная недоступность сайта не является событием нормативного контроля.
        if ev["event"] != "CHANGED":
            actions.append((ev["sourceId"], ev["event"], "skipped"))
            continue
        actions.append((ev["sourceId"], ev["event"], notify(ev, args.dry)))

    for sid, event, action in actions:
        print(f"{sid}: {event} -> {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
