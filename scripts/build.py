#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py - ежедневная сборка IPTV: плейлисты -> фильтр -> проверка -> группы -> EPG -> публикация.

Этапы:
 1. загрузка плейлистов (при сбое - последний снимок из state/snapshots)
 2. разбор, назначение групп, определение радио
 3. исключение по маскам (спорт и т.п.), дедуп по URL
 4. проверка потоков + история сбоев (state/health.json)
 5. дедуп по имени (основной + запасные варианты)
 6. сопоставление с EPG (id -> алиас -> имя -> нечётко), сборка своего epg.xml.gz
 7. запись плейлистов, index.html, report.txt, unmatched_epg.txt в dist/
"""
import argparse, difflib, gzip, http.client, json, lzma, os, re, shutil, socket, sys, tempfile
import ssl, threading, time, urllib.error, urllib.parse, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

import yaml

UA = "VLC/3.0.20 LibVLC/3.0.20"
NOW = datetime.now(timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg=""):
    print(msg, flush=True)


# ------------------------------------------------------------ утилиты
def load_yaml(p):
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def rx(items):
    return [re.compile(x, re.I) for x in (items or [])]


def any_rx(rxs, s):
    return any(r.search(s) for r in rxs)


def http_get(url, timeout=60, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise
            last = e
            time.sleep(2 * (i + 1))
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def http_download(url, path, timeout=120, retries=2, cap=500 * 1024 * 1024):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            size = 0
            with urllib.request.urlopen(req, timeout=timeout) as r, open(path, "wb") as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > cap:
                        raise ValueError("файл больше лимита")
                    f.write(chunk)
            return
        except Exception as e:
            last = e
            time.sleep(3 * (i + 1))
    raise last


def decode(raw):
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


# ------------------------------------------------------------ нормализация имён
QUAL = re.compile(r"\b(hd|fhd|uhd|sd|hq|lq|4k|8k|hevc|h\.?26[45]|1080[pi]?|720[pi]?|576[pi]?|480[pi]?|360[pi]?)\b", re.I)
TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
              ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "j", "k", "l", "m", "n", "o", "p", "r", "s", "t",
               "u", "f", "h", "c", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def norm(s):
    s = (s or "").lower().replace("ё", "е")
    s2 = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s)
    if re.sub(r"\W", "", s2):
        s = s2
    s = s.replace("+", " plus ")
    s = QUAL.sub(" ", s)
    s = re.sub(r"[^0-9a-zа-я]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def slug(n):
    t = "".join(TR.get(c, c) for c in n)
    return re.sub(r"[^0-9a-z]+", "-", t).strip("-") or "ch"


# ------------------------------------------------------------ разбор M3U
ATTR = re.compile(r'([\w\-\.]+)="([^"]*)"')


def split_info(line):
    body = line.split(":", 1)[1] if ":" in line else line
    inq, first = False, -1
    for i, c in enumerate(body):
        if c == '"':
            inq = not inq
        elif c == "," and not inq:
            first = i
            break
    return (body, "") if first < 0 else (body[:first], body[first + 1:].strip())


class Ch:
    __slots__ = ("name", "attrs", "extra", "url", "src", "prio", "radio", "group", "orig_group",
                 "cid", "norm", "logo", "order", "q")

    def __init__(self):
        self.logo = ""


def parse_m3u(text, src, prio):
    out, extra, info = [], [], None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#EXTM3U"):
            continue
        if s.upper().startswith("#EXTINF"):
            info = s
            continue
        if s.startswith("#"):
            extra.append(s)
            continue
        c = Ch()
        c.url, c.src, c.prio = s, src["name"], prio
        if info:
            head, name = split_info(info)
            c.attrs = {k.lower(): v for k, v in ATTR.findall(head)}
        else:
            name, c.attrs = "", {}
        c.name = (name or c.attrs.get("tvg-name") or s).strip()
        grp = [e for e in extra if e.upper().startswith("#EXTGRP:")]
        if grp and not c.attrs.get("group-title"):
            c.attrs["group-title"] = grp[0].split(":", 1)[1].strip()
        c.extra = [e for e in extra if not e.upper().startswith("#EXTGRP:")]
        c.radio = bool(src.get("radio"))
        out.append(c)
        info, extra = None, []
    return out


def fetch_source(src, snap_dir):
    snap = snap_dir / (src["name"] + ".m3u.gz")
    try:
        raw = http_get(src["url"])
        text = decode(raw)
        if "#EXTINF" not in text:
            raise ValueError("нет #EXTINF - это не плейлист")
        snap_dir.mkdir(parents=True, exist_ok=True)
        data = gzip.compress(raw, mtime=0)
        if not snap.exists() or snap.read_bytes() != data:
            snap.write_bytes(data)
        return text, "свежий"
    except Exception as e:
        if snap.exists():
            log(f"  ! {src['name']}: не скачался ({e}); беру снимок из state/snapshots")
            return decode(gzip.decompress(snap.read_bytes())), "снимок"
        raise


# ------------------------------------------------------------ токены / блок-листы
TOKEN_KEYS = {"token", "tokens", "tkn", "key", "apikey", "api_key", "auth", "authorization", "sign", "signature",
              "sig", "wmsauthsign", "hdnts", "hdnea", "hash", "hmac", "expires", "expire", "exp", "jwt", "session",
              "sid", "secret", "pass", "password", "passwd", "username", "user", "login", "md5", "nimblesessionid"}
EXPIRY_KEYS = {"e", "exp", "expires", "expire", "expiry", "end", "until", "valid", "validto", "ts", "time", "timestamp", "t", "st"}
LONG_VAL = re.compile(r"^[A-Za-z0-9_\-\.=+/%]{40,}$")
XTREAM = re.compile(r"^/(live|movie|series)/[^/]+/[^/]+/\d+\.(ts|m3u8)$", re.I)


def token_reason(url):
    """Почему ссылка 'сложная' (быстро протухает), либо ''."""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if "@" in u.netloc:
        return "логин:пароль в ссылке"
    if XTREAM.match(u.path):
        return "xtream логин/пароль"
    for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
        kl = k.lower()
        if kl in TOKEN_KEYS:
            return f"параметр {kl}"
        if kl in EXPIRY_KEYS and re.fullmatch(r"\d{10}|\d{13}", v):
            return f"срок действия ({kl})"
        if LONG_VAL.match(v):
            return "длинный ключ в параметре"
    return ""


def canon(url):
    """Ключ ссылки без токенов и сроков: переживает смену токена."""
    try:
        u = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url
    keep = []
    for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
        kl = k.lower()
        if kl in TOKEN_KEYS or kl in EXPIRY_KEYS or kl == "s" or LONG_VAL.match(v):
            continue
        keep.append((kl, v))
    q = urllib.parse.urlencode(sorted(keep))
    return f"{u.netloc.lower()}{u.path.rstrip('/')}" + (f"?{q}" if q else "")


def load_blocked(path):
    """TSV (формат removed_channels.tsv): 3-я колонка = ссылка."""
    keys = set()
    if not path.exists():
        return keys
    for ln in decode(path.read_bytes()).splitlines():
        parts = ln.rstrip("\r\n").split("\t")
        if len(parts) >= 3 and re.match(r"^[a-z]+://", parts[2].strip(), re.I):
            keys.add(canon(parts[2]))
    return keys


def load_delete_groups(path):
    exact, rxs = set(), []
    if not path.exists():
        return exact, rxs
    for ln in decode(path.read_bytes()).splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if len(ln) >= 3 and ln.startswith("/") and ln.endswith("/"):
            rxs.append(re.compile(ln[1:-1], re.I))
        else:
            exact.add(ln.casefold())
    return exact, rxs


def quality(name):
    n = name.lower()
    if re.search(r"\b(4k|uhd|2160)", n):
        return 4
    if re.search(r"\b(fhd|full ?hd|1080[pi]?)\b", n):
        return 3
    if re.search(r"\b(hd|720[pi]?)\b", n):
        return 2
    if re.search(r"\b(sd|480[pi]?|576[pi]?|360[pi]?|lq)\b", n):
        return 0
    return 1


# ------------------------------------------------------------ проверка потоков
started, lock = {}, threading.Lock()
CTX = ssl.create_default_context()      # проверка потоков: просроченный/кривой сертификат не считаем смертью канала
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def safe_url(u):
    return u.replace(" ", "%20")


def check(url, headers, timeout):
    """(True|False|None, причина)"""
    if not url.lower().startswith(("http://", "https://")):
        return None, "не http"
    with lock:
        started[url] = time.time()
    reason = "?"
    for attempt in range(2):
        try:
            req = urllib.request.Request(safe_url(url), headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                if r.status >= 400:
                    return False, f"HTTP {r.status}"
                data = r.read1(4096) if hasattr(r, "read1") else r.read(1024)
                if not data:
                    return False, "пустой ответ"
                if ".m3u8" in url.lower() or data.lstrip().startswith(b"#EXTM3U"):
                    return (True, "hls") if b"#EXT" in data[:2048] else (False, "не HLS")
                if "text/html" in (r.headers.get("Content-Type") or "").lower():
                    return False, "html вместо потока"
                return True, "ok"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}"
        except http.client.BadStatusLine as e:
            if "ICY" in str(e):
                return True, "icy"
            reason = "bad status"
        except socket.timeout:
            reason = "таймаут"
        except urllib.error.URLError as e:
            reason = f"ошибка: {getattr(e, 'reason', e)}"
        except Exception as e:
            reason = f"ошибка: {e}"
    return False, reason


def hdrs(c):
    h = {"User-Agent": UA}
    for m in c.extra:
        low = m.lower()
        if low.startswith("#extvlcopt:http-user-agent="):
            h["User-Agent"] = m.split("=", 1)[1]
        elif low.startswith("#extvlcopt:http-referrer="):
            h["Referer"] = m.split("=", 1)[1]
    return h


def run_checks(jobs, cfg):
    """jobs: {url: headers} -> {url: (ok, reason)}"""
    timeout, hard = cfg["timeout"], cfg["hard_timeout"]
    deadline = time.time() + cfg["max_minutes"] * 60
    pool = ThreadPoolExecutor(max_workers=cfg["workers"])
    pending = {pool.submit(check, u, h, timeout): u for u, h in jobs.items()}
    results, last_beat, dead = {}, time.time(), 0
    total = len(pending)
    while pending:
        fin, _ = wait(list(pending), timeout=1, return_when=FIRST_COMPLETED)
        now = time.time()
        for fu in fin:
            u = pending.pop(fu)
            try:
                res = fu.result()
            except Exception as e:
                res = (False, f"сбой: {e}")
            results[u] = res
            if res[0] is False:
                dead += 1
        for fu, u in list(pending.items()):
            st = started.get(u)
            if st and now - st > hard:
                pending.pop(fu)
                results[u] = (False, f"завис >{hard}с")
                dead += 1
        if now - last_beat >= 30:
            last_beat = now
            log(f"  ... проверено {len(results)}/{total}, мёртвых {dead}, в работе {len(pending)}")
        if now > deadline:
            log(f"  ! лимит {cfg['max_minutes']} мин: остальные {len(pending)} ссылок не проверены")
            break
    pool.shutdown(wait=False, cancel_futures=True)
    return results


def is_alive(h, thr):
    if not h:
        return True
    f = h.get("fails", 0)
    if f == 0:
        return True
    if h.get("last_ok") is None:
        return False          # ни разу не работал
    return f < thr


def update_health(health, results, url_src, cfg):
    per = {}
    for u, (ok, _) in results.items():
        if ok is None:
            continue
        p = per.setdefault(url_src[u], [0, 0])
        p[0] += 1
        p[1] += ok is False
    suspicious = {s for s, (n, f) in per.items()
                  if n >= cfg["mass_fail_min"] and f / n > cfg["mass_fail_ratio"]}
    for s in suspicious:
        log(f"  ! {s}: упало {per[s][1]} из {per[s][0]} - похоже на гео/сеть, провалы НЕ засчитываю")
    for u, (ok, reason) in results.items():
        if ok is None:
            continue
        h = health.setdefault(u, {"fails": 0, "last_ok": None})
        h["seen"] = TODAY
        if ok:
            h["fails"], h["last_ok"] = 0, TODAY
            h.pop("reason", None)
        elif url_src[u] not in suspicious:
            h["fails"] += 1
            h["reason"] = reason
    limit = (NOW - timedelta(days=30)).strftime("%Y-%m-%d")
    for u in [u for u, h in health.items() if h.get("seen", TODAY) < limit]:
        del health[u]


# ------------------------------------------------------------ EPG
def xml_open(path):
    with open(path, "rb") as f:
        magic = f.read(6)
    if magic[:2] == b"\x1f\x8b":
        return gzip.open(path, "rb")
    if magic[:6] == b"\xfd7zXZ\x00":
        return lzma.open(path, "rb")
    return open(path, "rb")


def epg_index(path):
    chans = {}
    with xml_open(path) as f:
        for ev, el in ET.iterparse(f, events=("start", "end")):
            if ev == "start" and el.tag == "programme":
                break                       # по DTD все <channel> идут раньше <programme>
            if ev == "end" and el.tag == "channel":
                names = [(d.text or "").strip() for d in el.findall("display-name") if d.text]
                ic = el.find("icon")
                chans[el.get("id")] = {"names": names, "icon": ic.get("src") if ic is not None else ""}
                el.clear()
    return chans


def match_epg(chans, wanted, alias, fuzzy):
    ids_ci = {k.lower(): k for k in chans if k}
    by_name, buckets = {}, {}
    for k, v in chans.items():
        for n in v["names"]:
            nn = norm(n)
            if nn:
                by_name.setdefault(nn, k)
                buckets.setdefault(nn[:3], []).append((nn, k))
    res = {}
    for cid, ch in wanted.items():
        eid = None
        a = alias.get(ch["norm"])
        if a:
            eid = ids_ci.get(a[3:].lower()) if a.lower().startswith("id:") else by_name.get(norm(a))
        if not eid:
            for oid in ch["orig_ids"]:
                if oid and oid.lower() in ids_ci:
                    eid = ids_ci[oid.lower()]
                    break
        if not eid:
            eid = by_name.get(ch["norm"])
        if not eid and fuzzy and len(ch["norm"]) >= 5:
            best, bs = None, fuzzy
            nd, hp = re.findall(r"\d+", ch["norm"]), "plus" in ch["norm"]
            for nn, k in buckets.get(ch["norm"][:3], []):
                if re.findall(r"\d+", nn) != nd or ("plus" in nn) != hp:
                    continue
                sc = difflib.SequenceMatcher(None, ch["norm"], nn).ratio()
                if sc >= bs:
                    best, bs = k, sc
            eid = best
        if eid:
            res[cid] = eid
    return res


def xmltv_dt(s):
    m = re.match(r"\s*(\d{4})(\d\d)(\d\d)(\d\d)?(\d\d)?(\d\d)?\s*([+-]\d{4})?", s or "")
    if not m:
        return None
    y, mo, d, h, mi, se, tz = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(se or 0))
    except ValueError:
        return None
    off = 0
    if tz:
        off = (int(tz[1:3]) * 60 + int(tz[3:5])) * (1 if tz[0] == "+" else -1)
    return (dt - timedelta(minutes=off)).replace(tzinfo=timezone.utc)


def epg_programmes(path, wanted_map, out_full, out_lite, t_from, t_to):
    counts, n, root = {}, 0, None
    with xml_open(path) as f:
        for ev, el in ET.iterparse(f, events=("start", "end")):
            if root is None:
                root = el
            if ev != "end" or el.tag != "programme":
                continue
            cids = wanted_map.get(el.get("channel"))
            if cids:
                st, sp = xmltv_dt(el.get("start")), xmltv_dt(el.get("stop"))
                if (sp is None or sp >= t_from) and (st is None or st <= t_to):
                    el.tail = None
                    for cid in cids:
                        el.set("channel", cid)
                        out_full.write(ET.tostring(el, encoding="unicode"))
                        lite = ET.Element("programme", dict(el.attrib))
                        t = el.find("title")
                        if t is not None:
                            lite.append(t)
                        out_lite.write(ET.tostring(lite, encoding="unicode"))
                        counts[cid] = counts.get(cid, 0) + 1
            el.clear()
            n += 1
            if n % 2000 == 0:
                root.clear()
    return counts


def build_epg(epg_sources, wanted, alias, cfg, dist, tmp):
    """-> (matched {cid: source}, icons {cid: url}); пишет dist/epg.xml.gz и epg_lite.xml.gz"""
    t_from = NOW - timedelta(hours=cfg["hours_back"])
    t_to = NOW + timedelta(days=cfg["days_ahead"])
    full_p, lite_p = Path(tmp) / "prog_full.xml", Path(tmp) / "prog_lite.xml"
    matched, icons = {}, {}
    remaining = dict(wanted)
    with open(full_p, "w", encoding="utf-8") as ff, open(lite_p, "w", encoding="utf-8") as fl:
        for s in epg_sources:
            if not remaining:
                break
            p = Path(tmp) / f"epg_{s['name']}.xml"
            try:
                log(f"  EPG {s['name']}: загрузка")
                http_download(s["url"], p)
                chans = epg_index(p)
                m = match_epg(chans, remaining, alias, cfg["fuzzy"])
                wm = {}
                for cid, eid in m.items():
                    wm.setdefault(eid, []).append(cid)
                counts = epg_programmes(p, wm, ff, fl, t_from, t_to)
                got = [cid for cid in m if counts.get(cid)]
                for cid in got:
                    matched[cid] = s["name"]
                    icons[cid] = chans[m[cid]]["icon"]
                    remaining.pop(cid, None)
                log(f"  EPG {s['name']}: каналов в EPG {len(chans)}, сопоставлено {len(m)}, с программой {len(got)}")
            except Exception as e:
                log(f"  ! EPG {s['name']}: пропущен ({e})")
            finally:
                p.unlink(missing_ok=True)
    if not matched:
        return {}, {}

    def write(path, prog_path):
        with gzip.open(path, "wt", encoding="utf-8") as g:
            g.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="brandmauer/iptv">\n')
            for cid in sorted(matched):
                w = wanted[cid]
                g.write(f'<channel id={quoteattr(cid)}><display-name lang="ru">{escape(w["name"])}</display-name>')
                if icons.get(cid):
                    g.write(f"<icon src={quoteattr(icons[cid])}/>")
                g.write("</channel>\n")
            with open(prog_path, "r", encoding="utf-8") as pf:
                shutil.copyfileobj(pf, g, 1024 * 1024)
            g.write("</tv>\n")

    write(dist / "epg.xml.gz", full_p)
    write(dist / "epg_lite.xml.gz", lite_p)
    return matched, icons


# ------------------------------------------------------------ вывод
def fmt_entry(c):
    a = {"tvg-id": c.cid, "tvg-name": c.name, "tvg-logo": c.logo or c.attrs.get("tvg-logo", ""),
         "group-title": c.group}
    for k, v in c.attrs.items():
        a.setdefault(k, v)
    attrs = " ".join(f'{k}="{v.replace(chr(34), chr(39))}"' for k, v in a.items() if v)
    lines = [f"#EXTINF:-1 {attrs},{c.name}"] + c.extra + [c.url]
    return "\n".join(lines)


def write_playlist(path, chs, header_epg):
    head = "#EXTM3U"
    if header_epg:
        head += f' url-tvg="{header_epg}" x-tvg-url="{header_epg}"'
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(head + "\n" + "\n".join(fmt_entry(c) for c in chs) + "\n")


# ------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent.parent
    ap.add_argument("--config-dir", default=str(root / "config"))
    ap.add_argument("--state-dir", default=str(root / "state"))
    ap.add_argument("--dist", default=str(root / "dist"))
    ap.add_argument("--no-check", action="store_true")
    ap.add_argument("--no-epg", action="store_true")
    a = ap.parse_args()

    cdir = Path(a.config_dir)
    src_cfg = load_yaml(cdir / "sources.yml")
    rules = load_yaml(cdir / "rules.yml")
    if (cdir / "playlists.yml").exists():
        src_cfg["sources"] = load_yaml(cdir / "playlists.yml").get("sources") or []
    if not src_cfg.get("sources"):
        sys.exit("Нет источников плейлистов (config/playlists.yml)")
    del_exact, del_rx = load_delete_groups(cdir / "delete_groups.txt")
    blocked = load_blocked(cdir / "blocked_channels.tsv")
    log(f"Списки: delete_groups {len(del_exact) + len(del_rx)} строк, blocked_channels {len(blocked)} ссылок")
    st = rules["settings"]
    state_dir, dist = Path(a.state_dir), Path(a.dist)
    snap_dir = state_dir / "snapshots"
    base = (src_cfg.get("publish") or {}).get("base_url", "").rstrip("/")
    t0 = time.time()

    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir(parents=True)

    # ---- 1-2. загрузка и разбор
    log("== 1. Загрузка плейлистов")
    entries, stats = [], {}
    for prio, s in enumerate(src_cfg["sources"]):
        stats[s["name"]] = {"total": 0, "excluded": 0, "dup_url": 0, "dead": 0, "kept": 0, "dupes": 0, "mode": "-"}
        try:
            text, mode = fetch_source(s, snap_dir)
        except Exception as e:
            log(f"  ! {s['name']}: НЕ ЗАГРУЖЕН и снимка нет ({e})")
            stats[s["name"]]["mode"] = "ОШИБКА"
            continue
        chs = parse_m3u(text, s, prio)
        for c in chs:
            c.order = len(entries)
            entries.append(c)
        stats[s["name"]].update(total=len(chs), mode=mode)
        log(f"  {s['name']}: {len(chs)} каналов ({mode})")
    if not entries:
        sys.exit("Нет ни одного канала - прерываю, чтобы не затереть публикацию")

    # ---- группы / радио
    rd = rules["radio"]
    radio_g, rg, ru_ = rd["group"], re.compile(rd["detect_group"], re.I), re.compile(rd["detect_url"], re.I)
    gcfg = rules["groups"]
    grules = [(re.compile(r["group"], re.I) if r.get("group") else None,
               re.compile(r["name"], re.I) if r.get("name") else None, r["to"]) for r in gcfg["rules"]]
    forced = {s["name"]: s.get("force_group") for s in src_cfg["sources"]}
    for c in entries:
        og = c.attrs.get("group-title", "").strip()
        c.orig_group = og
        if not c.radio and (rg.search(og) or ru_.search(c.url)):
            c.radio = True
        if forced.get(c.src):
            c.group = forced[c.src]
        elif c.radio:
            c.group = radio_g
        else:
            c.group = None
            for g_rx, n_rx, to in grules:
                if (g_rx or n_rx) and (not g_rx or g_rx.search(og)) and (not n_rx or n_rx.search(c.name)):
                    c.group = to
                    break
            if not c.group:
                c.group = og if (gcfg["unmapped"] == "keep" and og) else gcfg["default"]
        c.norm = norm(c.name) or c.name.lower()
        c.cid = slug(c.norm)

    # ---- 3. исключения и дедуп URL
    log("\n== 2. Фильтр по маскам и дедуп ссылок")
    ex = rules.get("exclude") or {}
    ex_g, ex_n, ex_u, keep_n = rx(ex.get("group")), rx(ex.get("name")), rx(ex.get("url")), rx(ex.get("keep_names"))
    tok_on = ex.get("tokens", True)
    allow_tok = {s["name"] for s in src_cfg["sources"] if s.get("allow_tokens")}
    seen_url, kept, n_tok, n_del = {}, [], 0, 0

    def in_delete_groups(c):
        for g in (c.orig_group, c.group):
            if g and (g.casefold() in del_exact or any_rx(del_rx, g)):
                return True
        return False

    for c in entries:
        keep_by_name = any_rx(keep_n, c.name)
        if not keep_by_name and (any_rx(ex_g, c.orig_group) or any_rx(ex_g, c.group)
                                 or any_rx(ex_n, c.name) or any_rx(ex_u, c.url)):
            stats[c.src]["excluded"] += 1
            continue
        if in_delete_groups(c):
            stats[c.src]["excluded"] += 1
            n_del += 1
            continue
        if tok_on and c.src not in allow_tok and not keep_by_name and token_reason(c.url):
            stats[c.src]["excluded"] += 1
            n_tok += 1
            continue
        if c.url in seen_url:
            stats[c.src]["dup_url"] += 1
            continue
        seen_url[c.url] = c.src
        kept.append(c)
    log(f"  из них по delete_groups: {n_del}, ссылки с токенами/ключами: {n_tok}")
    log(f"  было {len(entries)}, исключено масками {sum(v['excluded'] for v in stats.values())}, "
        f"дублей URL {sum(v['dup_url'] for v in stats.values())}, осталось {len(kept)}")

    # ---- 4. проверка
    health_p = state_dir / "health.json"
    health = json.loads(health_p.read_text(encoding="utf-8")) if health_p.exists() else {}
    nocheck = {s["name"] for s in src_cfg["sources"] if s.get("check") is False}
    if not a.no_check:
        log("\n== 3. Проверка потоков")
        jobs = {c.url: hdrs(c) for c in kept if c.src not in nocheck and canon(c.url) not in blocked}
        log(f"  к проверке: {len(jobs)} (источники без проверки: {', '.join(sorted(nocheck)) or '-'})")
        results = run_checks(jobs, st["check"])
        update_health(health, results, seen_url, st["check"])
        state_dir.mkdir(parents=True, exist_ok=True)
        health_p.write_text(json.dumps(health, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    thr = st["check"]["fail_threshold"]
    alive, dead = [], []
    for c in kept:
        if canon(c.url) in blocked:
            dead.append(c)                       # недоступен из домашней сети (blocked_channels.tsv)
        elif c.src in nocheck or is_alive(health.get(c.url), thr):
            alive.append(c)
        else:
            dead.append(c)
    for c in dead:
        stats[c.src]["dead"] += 1
    log(f"  живых/оставленных: {len(alive)}, удалено мёртвых: {len(dead)}")

    # ---- 3б. группы, где недоступно > ratio каналов, удаляются целиком
    dg = st["dead_groups"]
    gtot, gdead = Counter(), Counter()
    for c in kept:
        k = c.orig_group or c.group
        gtot[k] += 1
    for c in dead:
        gdead[c.orig_group or c.group] += 1
    bad_groups = {g for g, n in gtot.items()
                  if g and n >= dg["min_channels"] and gdead[g] / n > dg["ratio"]}
    dead_report = []
    if bad_groups:
        before = len(alive)
        alive = [c for c in alive if (c.orig_group or c.group) not in bad_groups]
        for g in sorted(bad_groups):
            dead_report.append(f"{g}\t{gdead[g]}/{gtot[g]}")
        log(f"  групп удалено целиком (> {int(dg['ratio'] * 100)}% недоступно): {len(bad_groups)}, "
            f"вместе с ещё живыми каналами: {before - len(alive)}")
        for line in dead_report:
            log("    - " + line.replace("\t", "  "))

    # ---- 5. дедуп по имени
    byname, final, dupes = {}, [], []
    for c in alive:
        byname.setdefault((c.radio, c.norm), []).append(c)
    dcfg = st["dupes"]
    n_lowq = 0
    for lst in byname.values():
        for c in lst:
            c.q = quality(c.name)
        if st["quality"]["prefer_hd"]:
            best = max(c.q for c in lst)
            if best >= 2:                       # есть HD - обычные версии не нужны
                n_lowq += sum(1 for c in lst if c.q < best)
                lst[:] = [c for c in lst if c.q == best]
        lst.sort(key=lambda c: (c.prio, c.order))
        final.append(lst[0])
        for i, c in enumerate(lst[1:1 + dcfg["max_per_channel"]], 2):
            c.name = f"{c.name} ({i})"
            dupes.append(c)
            stats[c.src]["dupes"] += 1
    log(f"  обычных версий удалено из-за HD: {n_lowq}, дублей в {dcfg['file']}: {len(dupes)}")
    order = {g: i for i, g in enumerate(gcfg.get("order") or [])}
    final.sort(key=lambda c: (order.get(c.group, 999), c.group, c.order))
    for c in final:
        stats[c.src]["kept"] += 1
    if len(final) < st["min_total"]:
        sys.exit(f"Каналов после чистки {len(final)} < min_total {st['min_total']} - прерываю, публикация не затронута")
    log(f"\n== 4. Итого каналов: {len(final)}")

    # ---- 6. EPG
    matched = {}
    epg_files = False
    if not a.no_epg:
        log("\n== 5. EPG")
        wanted = {}
        for c in final:
            if c.radio:
                continue
            w = wanted.setdefault(c.cid, {"name": c.name.split(" (")[0], "norm": c.norm, "orig_ids": set()})
            if c.attrs.get("tvg-id"):
                w["orig_ids"].add(c.attrs["tvg-id"])
        alias = {norm(k): v for k, v in (rules.get("aliases") or {}).items()}
        with tempfile.TemporaryDirectory() as tmp:
            matched, icons = build_epg(src_cfg.get("epg") or [], wanted, alias, st["epg"], dist, tmp)
        for c in final:
            if c.cid in icons and icons[c.cid] and not c.attrs.get("tvg-logo"):
                c.logo = icons[c.cid]
        if not matched and base:      # запасной вариант: прошлая опубликованная версия
            log("  ! EPG не собран - пробую взять прошлую опубликованную версию")
            for fn in ("epg.xml.gz", "epg_lite.xml.gz"):
                try:
                    (dist / fn).write_bytes(http_get(f"{base}/{fn}", retries=1))
                except Exception as e:
                    log(f"    не вышло {fn}: {e}")
        epg_files = (dist / "epg.xml.gz").exists()
        log(f"  сопоставлено с программой: {len(matched)} из {len(wanted)} телеканалов")
        un = sorted(f"{w['name']}\t{cid}" for cid, w in wanted.items() if cid not in matched)
        (dist / "unmatched_epg.txt").write_text(
            "# каналы без программы: добавь их в aliases (config/rules.yml)\n# имя\tid\n" + "\n".join(un) + "\n",
            encoding="utf-8")

    # ---- 7. запись
    log("\n== 6. Запись файлов")
    for o in rules["outputs"]:
        chs = final
        if o.get("include_groups"):
            chs = [c for c in chs if c.group in o["include_groups"]]
        if o.get("exclude_groups"):
            chs = [c for c in chs if c.group not in o["exclude_groups"]]
        kind = o.get("epg", "full")
        fn = {"full": "epg.xml.gz", "lite": "epg_lite.xml.gz"}.get(kind)
        hdr = f"{base}/{fn}" if fn and epg_files and base else ""
        write_playlist(dist / o["file"], chs, hdr)
        o["_count"] = len(chs)
        log(f"  {o['file']}: {len(chs)} каналов")

    dupes.sort(key=lambda c: (order.get(c.group, 999), c.group, c.order))
    write_playlist(dist / dcfg["file"], dupes, "")
    log(f"  {dcfg['file']}: {len(dupes)} каналов (дубли)")
    (dist / "dead_groups.txt").write_text(
        "# группы, удалённые целиком (недоступно > порога): группа, недоступно/всего\n"
        "# добавь нужные в config/delete_groups.txt, чтобы они не проверялись вообще\n"
        + "\n".join(dead_report) + ("\n" if dead_report else ""), encoding="utf-8")
    rep = [f"Сборка {NOW:%Y-%m-%d %H:%M} UTC, {int(time.time() - t0)} с", ""]
    rep.append(f"{'источник':26}{'режим':9}{'всего':>7}{'маски':>7}{'=URL':>7}{'мёртв':>7}{'в итоге':>9}{'в dupes':>9}")
    for n, v in stats.items():
        rep.append(f"{n:26}{v['mode']:9}{v['total']:>7}{v['excluded']:>7}{v['dup_url']:>7}{v['dead']:>7}{v['kept']:>9}{v['dupes']:>9}")
    by_group = {}
    for c in final:
        by_group[c.group] = by_group.get(c.group, 0) + 1
    rep += ["", "Группы: " + ", ".join(f"{g}={n}" for g, n in by_group.items()),
            f"EPG: {len(matched)} каналов с программой"]
    (dist / "report.txt").write_text("\n".join(rep) + "\n", encoding="utf-8")
    rows = "".join(f'<li><a href="{o["file"]}">{o["file"]}</a> - {o["_count"]} каналов</li>' for o in rules["outputs"])
    extra = f'<li><a href="{dcfg["file"]}">{dcfg["file"]}</a> - {len(dupes)} каналов (дубли)</li>'
    if epg_files:
        extra = '<li><a href="epg.xml.gz">epg.xml.gz</a></li><li><a href="epg_lite.xml.gz">epg_lite.xml.gz</a></li>'
    (dist / "index.html").write_text(
        f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>IPTV</title><h1>IPTV</h1><p>Обновлено {NOW:%Y-%m-%d %H:%M} UTC</p><ul>{rows}{extra}"
        f'<li><a href="report.txt">report.txt</a></li><li><a href="unmatched_epg.txt">unmatched_epg.txt</a></li><li><a href="dead_groups.txt">dead_groups.txt</a></li></ul>',
        encoding="utf-8")
    log("\n".join(rep))
    sys.stdout.flush()
    os._exit(0)           # не ждём зависшие потоки проверки


if __name__ == "__main__":
    main()
