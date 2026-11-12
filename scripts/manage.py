#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
manage.py - правит config/playlists.yml и config/delete_groups.txt (вызывается workflow 'manage-sources').

  add-playlist     --name N --url U [--group G] [--radio] [--no-check] [--allow-tokens]
  remove-playlist  --name N | --url U         (достаточно имени ИЛИ ссылки)
  add-delete-group    --group "Название"      (или /регэксп/)
  remove-delete-group --group "Название"
  list
"""
import argparse, os, re, sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
PL = ROOT / "config" / "playlists.yml"
DG = ROOT / "config" / "delete_groups.txt"
HEADER = ("# Источники плейлистов. Файл меняет workflow 'manage-sources' (Actions -> Run workflow),\n"
          "# можно править и руками. Порядок = приоритет при дублях.\n"
          "# check: false - не проверять потоки | force_group: X | radio: true | allow_tokens: true\n")
out_lines = []


def say(msg):
    print(msg)
    out_lines.append(msg)


def load():
    if not PL.exists():
        return []
    return (yaml.safe_load(PL.read_text(encoding="utf-8")) or {}).get("sources") or []


def save(src):
    PL.write_text(HEADER + yaml.safe_dump({"sources": src}, allow_unicode=True, sort_keys=False), encoding="utf-8")


def auto_name(url):
    u = urlsplit(url)
    base = (u.netloc + u.path).strip("/")
    base = re.sub(r"\.(m3u8?|txt)$", "", base, flags=re.I)
    return re.sub(r"[^0-9A-Za-z_.\-]+", "-", base).strip("-")[:60] or "playlist"


def read_dg():
    if not DG.exists():
        return []
    return DG.read_text(encoding="utf-8-sig").splitlines()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["add-playlist", "remove-playlist", "add-delete-group",
                                       "remove-delete-group", "list"])
    ap.add_argument("--name", default="")
    ap.add_argument("--url", default="")
    ap.add_argument("--group", default="")
    ap.add_argument("--radio", action="store_true")
    ap.add_argument("--no-check", action="store_true")
    ap.add_argument("--allow-tokens", action="store_true")
    a = ap.parse_args()
    name, url, group = a.name.strip(), a.url.strip(), a.group.strip()
    changed = False

    if a.action == "add-playlist":
        if not re.match(r"^https?://\S+$", url):
            sys.exit("Нужна ссылка http(s)://... в поле url")
        src = load()
        if any(s.get("url") == url for s in src):
            sys.exit("Такая ссылка уже есть в списке")
        name = re.sub(r"[^0-9A-Za-z_.\-]+", "-", name).strip("-") or auto_name(url)
        if any(s.get("name") == name for s in src):
            sys.exit(f"Имя '{name}' уже занято - укажи другое")
        item = {"name": name, "url": url}
        if group:
            item["force_group"] = group
        if a.radio:
            item["radio"] = True
        if a.no_check:
            item["check"] = False
        if a.allow_tokens:
            item["allow_tokens"] = True
        src.append(item)
        save(src)
        say(f"Добавлен источник: {name} -> {url}")
        changed = True

    elif a.action == "remove-playlist":
        key = name or url
        if not key:
            sys.exit("Укажи имя или ссылку источника")
        src = load()
        new = [s for s in src if s.get("name") != key and s.get("url") != key]
        if len(new) == len(src):
            sys.exit(f"Источник '{key}' не найден. Список: " + ", ".join(s["name"] for s in src))
        save(new)
        say(f"Удалён источник: {key} (осталось {len(new)})")
        changed = True

    elif a.action == "add-delete-group":
        if not group:
            sys.exit("Укажи группу в поле group")
        lines = read_dg()
        if group.casefold() in {l.strip().casefold() for l in lines}:
            sys.exit("Эта группа уже в delete_groups.txt")
        DG.write_text("\n".join(lines + [group]) + "\n", encoding="utf-8", newline="\n")
        say(f"Группа добавлена в список удаляемых: {group}")
        changed = True

    elif a.action == "remove-delete-group":
        if not group:
            sys.exit("Укажи группу в поле group")
        lines = read_dg()
        new = [l for l in lines if l.strip().casefold() != group.casefold()]
        if len(new) == len(lines):
            sys.exit("Такой группы нет в delete_groups.txt")
        DG.write_text("\n".join(new) + "\n", encoding="utf-8", newline="\n")
        say(f"Группа убрана из списка удаляемых: {group}")
        changed = True

    src = load()
    say("")
    say(f"Источники плейлистов ({len(src)}):")
    for i, s in enumerate(src, 1):
        flags = [f"группа={s['force_group']}" if s.get("force_group") else "", "радио" if s.get("radio") else "",
                 "без проверки" if s.get("check") is False else "", "токены разрешены" if s.get("allow_tokens") else ""]
        say(f"  {i}. {s['name']}  {' '.join(f for f in flags if f)}\n     {s['url']}")
    dg = [l for l in read_dg() if l.strip() and not l.startswith("#")]
    say(f"\nУдаляемые группы ({len(dg)}): " + "; ".join(dg[:30]) + (" ..." if len(dg) > 30 else ""))

    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write("### manage-sources\n```\n" + "\n".join(out_lines) + "\n```\n")
    gout = os.environ.get("GITHUB_OUTPUT")
    if gout:
        with open(gout, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")


if __name__ == "__main__":
    main()
