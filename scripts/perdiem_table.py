#!/usr/bin/env python3
"""Parse the 「中央政府各機關派赴國外各地區出差人員生活費日支數額表」 PDF.

Only the main overseas table section is parsed (not the 修正對照表 or the
大陸/港澳 table that may be bundled in the same PDF). Page numbers are found
from the text, never hard-coded.

CLI:
  perdiem_table.py info   --pdf SRC.pdf
  perdiem_table.py lookup --pdf SRC.pdf QUERY [QUERY ...]
  perdiem_table.py dump   --pdf SRC.pdf            (all rows as JSON)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict

import pymupdf

TITLE = "派赴國外各地區出差人員生活費日支數額表"
CN_NUM = re.compile(r"^[一二三四五六七八九十百０0]+$")
_RANGE = r"\d{2}/\d{2}\s*-\s*\d{2}/\d{2}"
SEASON = re.compile(rf"^\(\s*{_RANGE}(\s*,\s*{_RANGE})*\s*\)$")
PAGE_FOOT = re.compile(r"第\s*(\d+)\s*頁\s*，\s*共\s*(\d+)\s*頁")


@dataclass
class Rate:
    usd: int
    season: str | None          # e.g. "(05/01-09/30)" or None
    page: int                   # 0-based page index in the PDF
    rate_bbox: list             # [x0,y0,x1,y1]
    label_bbox: list            # bbox of name (or season) text on that row


@dataclass
class Entry:
    kind: str                   # "country" | "city"
    no: str | None              # city/other number (阿拉伯數字)
    name: str                   # full text e.g. "倫敦(London)"
    zh: str
    en: str
    country: str                # country full name, e.g. "英國(United Kingdom)"
    region: str | None          # e.g. "A 亞太地區"
    page: int
    name_bbox: list
    country_page: int
    country_bbox: list
    rates: list = field(default_factory=list)


def split_name(full: str):
    m = re.match(r"^(.*?)\((.*)\)$", full.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return full.strip(), ""


def find_section(doc):
    """Return (first_page, last_page, version_text) of the main overseas table."""
    start = None
    for i, p in enumerate(doc):
        t = p.get_text()
        if TITLE in t and "對照" not in t.split(TITLE)[1][:6] and "日支數額" in t:
            start = i
            break
    if start is None:
        raise SystemExit("找不到「派赴國外各地區出差人員生活費日支數額表」主表頁面。")
    m = PAGE_FOOT.search(doc[start].get_text())
    total = int(m.group(2)) if m else None
    end = start
    if total:
        end = start + total - 1
        mm = PAGE_FOOT.search(doc[end].get_text())
        if not (mm and int(mm.group(1)) == total):
            # fall back: walk pages while footer counts upward
            end = start
            for j in range(start + 1, doc.page_count):
                mm = PAGE_FOOT.search(doc[j].get_text())
                if mm and int(mm.group(2)) == total and int(mm.group(1)) == j - start + 1:
                    end = j
                else:
                    break
    # effective-date hints anywhere in the document
    alltext = "".join(doc[i].get_text() for i in range(min(doc.page_count, start + 1)))
    eff = re.findall(r"自中華民國[一二三四五六七八九十百零〇]+年[一二三四五六七八九十]+月[一二三四五六七八九十]+日生效", alltext.replace("\n", ""))
    return start, end, eff


def _join_lines(ws):
    """Join words: space within a visual line, nothing between wrapped lines."""
    out, last_y = "", None
    for w in ws:
        yc = (w[1] + w[3]) / 2
        if last_y is None:
            out = w[4]
        elif abs(yc - last_y) <= 4:
            out += " " + w[4]
        else:
            out += w[4]
        last_y = yc
    return out


def _band_rows(page, body, name_x0, rate_x):
    """Group words into table rows using horizontal ruling lines / cell fills."""
    ys = []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) < 1 and abs(p1.x - p2.x) > 40:
                    ys.append(p1.y)
            elif it[0] == "re":
                r = it[1]
                if r.height < 1.5 and r.width > 40:
                    ys.append((r.y0 + r.y1) / 2)
                elif r.width > 40:
                    ys += [r.y0, r.y1]
    ys = sorted(ys)
    lines = []
    for y in ys:
        if not lines or y - lines[-1] > 2:
            lines.append(y)
    if len(lines) < 3:
        # fallback: cluster by y
        rows, cur, cy = [], [], None
        for w in body:
            yc = (w[1] + w[3]) / 2
            if cy is None or abs(yc - cy) <= 4:
                cur.append(w)
                cy = yc if cy is None else cy
            else:
                rows.append(cur)
                cur, cy = [w], yc
        if cur:
            rows.append(cur)
        return rows
    bands = {}
    import bisect
    for w in body:
        yc = (w[1] + w[3]) / 2
        k = bisect.bisect(lines, yc)
        bands.setdefault(k, []).append(w)
    return [bands[k] for k in sorted(bands)]


def _rows(page):
    words = page.get_text("words")
    # column boundaries from the header
    hdr = {w[4]: w for w in words if w[4] in ("城市或", "日支數額", "地區、")}
    if "日支數額" not in hdr or "城市或" not in hdr:
        return [], None
    rate_x = hdr["日支數額"][0] - 25
    city_x0 = hdr["城市或"][0] - 8
    name_x0 = hdr["城市或"][2] + 8
    head_bottom = max(w[3] for w in words if w[4] in ("日支數額", "其他", "國家", "(地區、國家、城市或其他)"))
    body = [w for w in words if w[1] > head_bottom + 2 and not PAGE_FOOT.search(w[4])
            and not re.match(r"^(第|頁，共|頁)$", w[4])]
    # drop footer words ("第 1 頁，共 28 頁" split into pieces)
    foot = [w for w in words if "頁" in w[4] and "共" in w[4]]
    if foot:
        fy = min(w[1] for w in foot) - 2
        body = [w for w in body if w[1] < fy]
    body.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows = _band_rows(page, body, name_x0, rate_x)
    out = []
    for r in rows:
        r.sort(key=lambda w: (round((w[1] + w[3]) / 2 / 4), w[0]))
        reg = [w for w in r if w[2] <= city_x0 + 2]
        cno = [w for w in r if city_x0 <= w[0] and w[2] <= name_x0]
        nam = [w for w in r if w[0] >= name_x0 - 2 and w[2] < rate_x]
        rat = [w for w in r if w[0] >= rate_x]
        out.append(dict(
            reg=" ".join(w[4] for w in reg) or None,
            no=" ".join(w[4] for w in cno) or None,
            name=_join_lines(nam) or None,
            name_x=nam[0][0] if nam else None,
            name_bbox=list(pymupdf.Rect(nam[0][:4]) | pymupdf.Rect(nam[-1][:4])) if nam else None,
            rate=" ".join(w[4] for w in rat) or None,
            rate_bbox=list(pymupdf.Rect(rat[0][:4]) | pymupdf.Rect(rat[-1][:4])) if rat else None,
        ))
    return out, name_x0


def parse(pdf_path):
    doc = pymupdf.open(pdf_path)
    start, end, eff = find_section(doc)
    entries: list[Entry] = []
    country = None
    country_page = None
    country_bbox = None
    region = None
    last = None
    base_x = None
    for pi in range(start, end + 1):
        rows, _ = _rows(doc[pi])
        for r in rows:
            if r["name"] is None and r["rate"] is None:
                if r["reg"] and r["reg"].isalpha() and len(r["reg"]) <= 2:
                    region = r["reg"]
                continue
            # region header row: single latin letter + region name (e.g. "D 北美洲地區")
            if r["reg"] and re.fullmatch(r"[A-Z]", r["reg"]) and r["rate"] is None:
                region = f'{r["reg"]} {r["name"] or ""}'.strip()
                continue
            if r["name"] and SEASON.match(r["name"]):
                if last is None or not r["rate"]:
                    continue
                last.rates.append(Rate(int(r["rate"]), r["name"], pi, r["rate_bbox"], r["name_bbox"]))
                continue
            if base_x is None and r["reg"] and r["name_x"] is not None:
                base_x = r["name_x"]
            indented = (r["name_x"] is not None and base_x is not None and r["name_x"] > base_x + 4)
            if r["reg"] and CN_NUM.match(r["reg"].replace(" ", "")) or (not indented and r["name"]):
                # country row
                country = r["name"]
                country_page, country_bbox = pi, r["name_bbox"]
                zh, en = split_name(r["name"])
                e = Entry("country", r["no"], r["name"], zh, en, r["name"], region, pi,
                          r["name_bbox"], pi, r["name_bbox"])
                if r["rate"]:
                    e.rates.append(Rate(int(r["rate"]), None, pi, r["rate_bbox"], r["name_bbox"]))
                entries.append(e)
                last = e
                continue
            if indented and r["name"]:
                if not r["rate"] and not r["no"] and last is not None and last.kind == "city":
                    # wrapped name continuation
                    last.name += r["name"]
                    last.zh, last.en = split_name(last.name)
                    continue
                zh, en = split_name(r["name"])
                e = Entry("city", r["no"], r["name"], zh, en, country, region, pi, r["name_bbox"],
                          country_page, country_bbox)
                if r["rate"]:
                    e.rates.append(Rate(int(r["rate"]), None, pi, r["rate_bbox"], r["name_bbox"]))
                entries.append(e)
                last = e
    return dict(pdf=pdf_path, section_pages=[start, end], effective=eff, entries=entries)


def _norm(s):
    return re.sub(r"[\s・·.,'’\-()（）]", "", s).lower()


def lookup(entries, query):
    """Return list of matching entries. Query forms:
    '倫敦' / 'London' / '英國:其他' / 'United Kingdom:Other' / '英國' (country)."""
    q = query.strip()
    if ":" in q or "：" in q:
        c, sub = re.split(r"[:：]", q, maxsplit=1)
        cs = [e for e in entries if e.kind == "country" and _norm(c) in (_norm(e.zh), _norm(e.en))]
        res = []
        for ce in cs:
            for e in entries:
                if e.kind == "city" and e.country == ce.name and _norm(sub) in (_norm(e.zh), _norm(e.en)):
                    res.append(e)
        return res
    exact = [e for e in entries if _norm(q) in (_norm(e.zh), _norm(e.en), _norm(e.name))]
    if exact:
        return exact
    return [e for e in entries if _norm(q) in _norm(e.name)]


def country_block(entries, country_name):
    return [e for e in entries if e.country == country_name]


def _show(e: Entry):
    rs = "; ".join(f'{(r.season + " ") if r.season else ""}USD {r.usd} (PDF p.{r.page + 1})' for r in e.rates) or "（子項目，見下列城市）"
    return f'[{e.kind}] {e.no or "-":>4}  {e.name}  ／ 國家：{e.country}  → {rs}'


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["info", "lookup", "dump"])
    ap.add_argument("--pdf", required=True)
    ap.add_argument("queries", nargs="*")
    a = ap.parse_args(argv)
    data = parse(a.pdf)
    ents = data["entries"]
    if a.cmd == "info":
        s, e = data["section_pages"]
        print(f"主表頁面：PDF 第 {s + 1}–{e + 1} 頁；共解析 {len(ents)} 列；有費率列 {sum(1 for x in ents if x.rates)}")
        print("生效文字：", data["effective"] or "（未在主表前找到，請人工確認版本）")
        return
    if a.cmd == "dump":
        print(json.dumps([asdict(x) for x in ents], ensure_ascii=False, indent=1))
        return
    for q in a.queries:
        print(f"## {q}")
        hits = lookup(ents, q)
        if not hits:
            print("  （無符合列項；若為城市，請改查其所屬國家，並使用該國「其他」或國家費率）")
        for h in hits:
            print("  " + _show(h))
            if h.kind == "country" and not h.rates:
                for c in country_block(ents, h.name):
                    if c is not h:
                        print("      " + _show(c))


if __name__ == "__main__":
    main()
