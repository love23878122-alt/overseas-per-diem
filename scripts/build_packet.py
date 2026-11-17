#!/usr/bin/env python3
"""Build the printable 國外出差日支 folder:
    01 日支數額表.pdf        — only the needed source pages, circled, with conversion lines
    02 臺銀美元匯率證明.pdf   — BOT USD month table printed to PDF, cash-sell circled

Example:
  build_packet.py --pdf "SRC.pdf" --departure 2026-08-26 \
      --dest 倫敦 --dest "愛丁堡=英國:其他" --dest 布達佩斯 \
      --traveler 王小明 --out "/path/王小明 歐洲出差"

--dest forms:  KEY   or   顯示名稱=KEY
  KEY = 城市中文/英文名（表上有獨立列項）, or 國家:其他, or a country that has a single rate.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from decimal import Decimal, ROUND_DOWN

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import perdiem_table as T  # noqa: E402
import bot_rate as B  # noqa: E402
from fonts import cjk_font  # noqa: E402

RED = (0.72, 0.1, 0.14)
INK = (0.08, 0.08, 0.08)
F01 = "01 日支數額表.pdf"
F02 = "02 臺銀美元匯率證明.pdf"

_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_int(s):
    total, cur = 0, 0
    for ch in s:
        if ch in _CN:
            cur = _CN[ch]
        elif ch == "十":
            total += (cur or 1) * 10
            cur = 0
        elif ch == "百":
            total += (cur or 1) * 100
            cur = 0
    return total + cur


def effective_date(eff_texts):
    for t in eff_texts:
        m = re.search(r"中華民國(.+?)年(.+?)月(.+?)日", t)
        if m:
            return dt.date(cn_int(m.group(1)) + 1911, cn_int(m.group(2)), cn_int(m.group(3)))
    return None


def in_season(season: str, d: dt.date) -> bool:
    md = (d.month, d.day)
    for a, b in re.findall(r"(\d{2}/\d{2})\s*-\s*(\d{2}/\d{2})", season):
        s = tuple(map(int, a.split("/")))
        e = tuple(map(int, b.split("/")))
        if (s <= md <= e) if s <= e else (md >= s or md <= e):
            return True
    return False


def resolve(entries, spec, departure):
    label, key = spec.split("=", 1) if "=" in spec else (None, spec)
    label = label.strip() if label else None
    hits = T.lookup(entries, key)
    if not hits:
        raise SystemExit(f"[查表] 「{key}」無符合列項。若城市無獨立列項，請用「國家:其他」；"
                         f"可先執行 perdiem_table.py lookup 確認。")
    if len(hits) > 1:
        raise SystemExit(f"[查表] 「{key}」有多個符合列項，請指定更明確的名稱：\n" +
                         "\n".join("  " + T._show(h) for h in hits))
    e = hits[0]
    if not e.rates:
        raise SystemExit(f"[查表] 「{e.name}」有多種城市費率，請指定城市或「{e.zh}:其他」：\n" +
                         "\n".join("  " + T._show(c) for c in T.country_block(entries, e.name) if c is not e))
    notes = []
    if len(e.rates) == 1 and e.rates[0].season is None:
        rate = e.rates[0]
    else:
        ok = [r for r in e.rates if r.season and in_season(r.season, departure)]
        if len(ok) != 1:
            raise SystemExit(f"[查表] 「{e.name}」為季節費率，無法依出發日 {departure} 唯一判定：" +
                             "; ".join(f"{r.season} USD {r.usd}" for r in e.rates))
        rate = ok[0]
        notes.append(f"{e.name} 為季節費率，依出發日 {departure} 採 {rate.season} USD {rate.usd}；"
                     f"若停留跨越季節請與使用者確認。")
    country_zh = T.split_name(e.country)[0]
    if label is None:
        label = e.zh if e.zh != "其他" else f"{country_zh}其他"
    if e.zh == "其他" and label != f"{country_zh}其他":
        shown = f"{label}（{country_zh}其他）"
    elif e.kind == "city" and label != e.zh:
        shown = f"{label}（{e.zh}）"
    else:
        shown = label
    if rate.season:
        shown += f" {rate.season}"
    return dict(entry=e, rate=rate, label=shown, notes=notes)


def ntd(usd: int, fx: str) -> int:
    return int((Decimal(usd) * Decimal(fx)).quantize(Decimal(1), rounding=ROUND_DOWN))


def oval(page, rect, pad_x=6.5, pad_y=2.5):
    r = pymupdf.Rect(rect)
    sh = page.new_shape()
    sh.draw_oval(pymupdf.Rect(r.x0 - pad_x, r.y0 - pad_y, r.x1 + pad_x, r.y1 + pad_y))
    sh.finish(color=RED, width=1.3)
    sh.commit()


def page_layout(page):
    """table bbox and page-number top on a source page."""
    rects = [d["rect"] for d in page.get_drawings() if d["rect"].width > 2 or d["rect"].height > 2]
    foot = [b for b in page.get_text("blocks") if T.PAGE_FOOT.search(b[4])]
    pn_top = min(b[1] for b in foot) if foot else page.rect.height - 20
    rects = [r for r in rects if r.y1 < pn_top - 1]
    x0 = min(r.x0 for r in rects)
    x1 = max(r.x1 for r in rects)
    return pymupdf.Rect(x0, min(r.y0 for r in rects), x1, max(r.y1 for r in rects)), pn_top


def _plan(lines, n_bottom, n_top, cols):
    """Rows: header line alone, then formulas `cols` per row. Returns (bottom_rows, top_rows) or None."""
    head, rest = lines[0], lines[1:]
    rows = [[head]] + [rest[i:i + cols] for i in range(0, len(rest), cols)]
    if len(rows) <= n_bottom:
        return rows, []
    if len(rows) <= n_bottom + n_top:
        return rows[:n_bottom], rows[n_bottom:]
    return None


def write_lines(page, lines, fontname, fontfile):
    """Put the rate line + formulas in the blank band under the table (above the page number).
    Prefers one column; then two columns; smaller type down to 8 pt; the blank margin above the
    table is used only as a last resort. Never covers existing content."""
    table, pn_top = page_layout(page)
    font = pymupdf.Font(fontfile=fontfile) if fontfile else pymupdf.Font(fontname)
    gap = 14
    choice = None
    for allow_top in (False, True):
        for size in (9.5, 9, 8.5, 8):
            lead = size * 1.25
            n_bottom = int((pn_top - 2 - (table.y1 + 2)) // lead)
            n_top = int((table.y0 - 2 - 18) // lead) if allow_top else 0
            if font.text_length(lines[0], size) > table.width:
                continue
            for cols in (1, 2):
                colw = (table.width - gap * (cols - 1)) / cols
                if any(font.text_length(t, size) > colw for t in lines[1:]):
                    continue
                plan = _plan(lines, n_bottom, n_top, cols)
                if plan:
                    choice = (size, lead, cols, colw, plan)
                    break
            if choice:
                break
        if choice:
            break
    if not choice:
        raise SystemExit("[排版] 頁尾空白不足以放入所有換算式，請分批（兩份資料夾）或縮短顯示名稱。")
    size, lead, cols, colw, (bottom, top) = choice

    def put(rows, y):
        for row in rows:
            for k, t in enumerate(row):
                page.insert_text((table.x0 + k * (colw + gap), y), t, fontsize=size,
                                 fontname=fontname, fontfile=fontfile, color=INK)
            y += lead

    put(bottom, table.y1 + 2 + size)
    if top:
        put(top, table.y0 - 2 - lead * (len(top) - 1) - size * 0.25)
    return dict(size=size, cols=cols, bottom_rows=len(bottom), top_rows=len(top))


def build_table_pdf(src_pdf, picks, fx, fx_date, traveler, out_path):
    src = pymupdf.open(src_pdf)
    pages = {}
    for p in picks:
        e, r = p["entry"], p["rate"]
        pages.setdefault(r.page, []).append(p)
        if e.zh == "其他" and e.country_page != r.page:
            pages.setdefault(e.country_page, [])
    order = sorted(pages)
    out = pymupdf.open()
    for pi in order:
        out.insert_pdf(src, from_page=pi, to_page=pi)
    fontname, fontfile = cjk_font()
    head = (f"{traveler}｜" if traveler else "") + f"臺銀 {fx_date.replace('-', '/')} 美元現金賣出 {fx}"
    report = []
    for idx, pi in enumerate(order):
        pg = out[idx]
        # circle the city/其他 name (or season label) and the USD figure
        for p in pages[pi]:
            e, r = p["entry"], p["rate"]
            oval(pg, e.name_bbox if e.page == pi else r.label_bbox)
            if r.season:
                oval(pg, r.label_bbox)
            oval(pg, r.rate_bbox, pad_x=6)
        for p in picks:  # country header on a separate page (for 其他)
            e = p["entry"]
            if e.zh == "其他" and e.country_page == pi and p["rate"].page != pi:
                oval(pg, e.country_bbox)
        lines = [head] + [f'{p["label"]}：USD {p["rate"].usd} × {fx} → NT$ {p["ntd"]:,}／日'
                          for p in pages[pi]] if pages[pi] else []
        info = write_lines(pg, lines, fontname, fontfile) if lines else None
        report.append(dict(source_pdf_page=pi + 1, lines=lines, layout=info))
    out.subset_fonts()
    out.save(out_path, garbage=4, deflate=True)
    return report


def render(pdf, prefix, dpi=110):
    paths = []
    with pymupdf.open(pdf) as d:
        for i, p in enumerate(d):
            fn = f"{prefix}_p{i + 1}.png"
            p.get_pixmap(dpi=dpi).save(fn)
            paths.append(fn)
    return paths


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="出差人員生活費日支數額表 PDF（適用版本）")
    ap.add_argument("--departure", required=True)
    ap.add_argument("--dest", action="append", required=True)
    ap.add_argument("--traveler")
    ap.add_argument("--out", required=True, help="輸出資料夾（完整路徑，含資料夾名稱）")
    ap.add_argument("--preview-dir", help="驗證用 PNG/JSON 存放處（預設為系統暫存資料夾，不放進列印資料夾）")
    ap.add_argument("--force", action="store_true", help="允許覆寫既有 01/02 檔")
    a = ap.parse_args(argv)

    dep = dt.date.fromisoformat(a.departure)
    data = T.parse(a.pdf)
    eff = effective_date(data["effective"])
    warnings = []
    if eff is None:
        warnings.append("找不到生效日期文字，請人工確認此版本適用於出發日。")
    elif dep < eff:
        raise SystemExit(f"[版本] 出發日 {dep} 早於此表生效日 {eff}，請改用前一版日支數額表。")
    elif dep.year > eff.year:
        warnings.append(f"此表自 {eff} 生效；出發日在 {dep.year} 年，請確認是否已有更新版本。")

    picks = [resolve(data["entries"], s, dep) for s in a.dest]
    for p in picks:
        warnings += p["notes"]

    final1, final2 = os.path.join(a.out, F01), os.path.join(a.out, F02)
    for f in (final1, final2):
        if os.path.exists(f) and not a.force:
            raise SystemExit(f"[輸出] {f} 已存在；為保護已交付檔案不覆寫（確認後加 --force）。")
    work = tempfile.mkdtemp(prefix="per_diem_build_")
    o1, o2 = os.path.join(work, F01), os.path.join(work, F02)

    # exchange rate (one rate for the whole trip)
    try:
        sess = B.BotSession()
    except Exception as e:
        print(f"ACCESS_ERROR 無法啟動瀏覽器：{e}", file=sys.stderr)
        return 2
    try:
        fxinfo = B.find_rate(sess, dep)
        fxinfo["circle"] = sess.print_month(fxinfo["year"], fxinfo["month"], fxinfo["rate_date"], o2)
    except B.AccessError as e:
        print(f"ACCESS_ERROR 臺銀網站存取失敗（不是無牌告）：{e}", file=sys.stderr)
        return 2
    except B.NoPosting as e:
        print(f"NO_POSTING {e}", file=sys.stderr)
        return 3
    except B.NotYet as e:
        print(f"NOT_YET {e}", file=sys.stderr)
        return 4
    finally:
        sess.close()
    fx = fxinfo["cash_sell"]
    for p in picks:
        p["ntd"] = ntd(p["rate"].usd, fx)

    report = build_table_pdf(a.pdf, picks, fx, fxinfo["rate_date"], a.traveler, o1)
    os.makedirs(a.out, exist_ok=True)
    import shutil
    shutil.move(o1, final1)
    shutil.move(o2, final2)
    shutil.rmtree(work, ignore_errors=True)
    o1, o2 = final1, final2

    pv = a.preview_dir or tempfile.mkdtemp(prefix="per_diem_preview_")
    os.makedirs(pv, exist_ok=True)
    previews = render(o1, os.path.join(pv, "01")) + render(o2, os.path.join(pv, "02"))
    summary = dict(
        departure=a.departure, source_pdf=a.pdf, table_effective=str(eff) if eff else None,
        rate=dict(date=fxinfo["rate_date"], cash_sell=fx, target_day=fxinfo["target_day"],
                  backtracked_days=fxinfo["backtracked_days"], months_checked=fxinfo["months_checked"],
                  url=fxinfo["month_url"], print_pages=fxinfo["circle"]["pages"]),
        destinations=[dict(label=p["label"], table_name=p["entry"].name, country=p["entry"].country,
                           usd=p["rate"].usd, season=p["rate"].season, source_pdf_page=p["rate"].page + 1,
                           ntd_per_day=p["ntd"],
                           formula=f'USD {p["rate"].usd} × {fx} = {Decimal(p["rate"].usd) * Decimal(fx)}')
                      for p in picks],
        pages=report, outputs=[o1, o2], previews=previews, warnings=warnings)
    with open(os.path.join(pv, "summary.json"), "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
