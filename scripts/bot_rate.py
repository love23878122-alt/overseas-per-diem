#!/usr/bin/env python3
"""臺灣銀行「歷史本行營業時間牌告匯率」美元月表：查匯率、列印月表 PDF、圈出現金賣出。

Rule: use the posting for the day before departure; if that day has no posting
(holiday / market closed) walk back to the nearest earlier posting day, crossing
month/year boundaries when needed.

Exit codes:
  0 ok
  2 website access failed (network / challenge / timeout / page structure)  -> 不可當作「無牌告」
  3 month pages loaded fine but no usable posting was found within the search window
  4 the day before departure has not finished yet (rate not final)

CLI:
  bot_rate.py --departure 2026-08-26 [--pdf-out 02.pdf] [--json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from decimal import Decimal

URL = "https://rate.bot.com.tw/xrt/quote/{ym}/USD?Lang=zh-TW"
HISTORY = "https://rate.bot.com.tw/xrt/history?Lang=zh-TW"


class AccessError(RuntimeError):
    pass


class NoPosting(RuntimeError):
    pass


class NotYet(RuntimeError):
    pass


def _prev_month(y, m):
    return (y - 1, 12) if m == 1 else (y, m - 1)


class BotSession:
    def __init__(self, headless=True):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self.browser = self._pw.chromium.launch(channel="chrome", headless=headless)
        except Exception:
            self.browser = self._pw.chromium.launch(headless=headless)
        self.ctx = self.browser.new_context(locale="zh-TW", viewport={"width": 1280, "height": 900})
        self.page = self.ctx.new_page()
        self.cache = {}

    def close(self):
        try:
            self.browser.close()
        finally:
            self._pw.stop()

    def load_month(self, y, m, tries=3, use_cache=True):
        """Return list of rows [{date, cash_buy, cash_sell, spot_buy, spot_sell}] for the month.
        Raises AccessError if the page could not be loaded/validated."""
        key = (y, m)
        if use_cache and key in self.cache:
            return self.cache[key]
        url = URL.format(ym=f"{y:04d}-{m:02d}")
        last_err = None
        for _ in range(tries):
            try:
                self.page.goto(url, timeout=60000, wait_until="domcontentloaded")
                # page passes the anti-bot challenge, reloads, then shows the query form
                self.page.wait_for_function(
                    """() => document.title.includes('牌告') &&
                         (document.querySelector('table.table tbody tr td') ||
                          document.body.innerText.includes('查無') ||
                          document.body.innerText.includes('無資料'))""",
                    timeout=60000)
                text = self.page.inner_text("body")
                heads = self.page.eval_on_selector_all(
                    "table.table thead th", "ths=>ths.map(t=>t.innerText.trim())")
                rows = self.page.eval_on_selector_all(
                    "table.table tbody tr",
                    "rs=>rs.map(r=>[...r.querySelectorAll('td')].map(t=>t.innerText.trim()))")
                period_ok = f"{y:04d}/{m:02d}/01" in text
                if rows:
                    if not ({"掛牌日期", "現金匯率", "本行賣出"} <= set(heads)):
                        raise AccessError(f"表頭不符預期：{heads}")
                    parsed = []
                    for r in rows:
                        if len(r) < 6 or "USD" not in r[1]:
                            raise AccessError(f"列格式不符預期：{r}")
                        d = dt.datetime.strptime(r[0], "%Y/%m/%d").date()
                        parsed.append(dict(date=d.isoformat(), cash_buy=r[2], cash_sell=r[3],
                                           spot_buy=r[4], spot_sell=r[5]))
                    self.cache[key] = parsed
                    return parsed
                if period_ok and ("查無" in text or "無資料" in text):
                    self.cache[key] = []
                    return []
                raise AccessError("月表頁面未出現資料表或「查無資料」訊息")
            except AccessError as e:
                last_err = e
            except Exception as e:  # timeouts, network
                last_err = AccessError(f"{type(e).__name__}: {e}")
        raise last_err

    def print_month(self, y, m, target_date: str, pdf_out: str):
        """Print the month table as the site's print layout to PDF, then circle cash-sell."""
        rows = self.load_month(y, m, use_cache=False)  # navigate: the browser must show this month
        want = [r for r in rows if r["date"] == target_date]
        if not want:
            raise RuntimeError(f"{y}-{m:02d} 月表中沒有 {target_date}")
        self.page.evaluate("""() => {
            // the 2nd 「掛牌日期」 header cell only gets rowspan=2 from the site's JS at wide
            // screen widths; restore it so the print header lines up with the columns.
            const th = document.querySelector('table.table thead th.hasscript');
            if (th && !th.getAttribute('rowspan')) th.setAttribute('rowspan', '2');
            // trend charts are animated and not part of the posted figures; leave them out
            // of the print so the table is captured whole (no data cell is touched).
            // (.chart-key-values is the 查詢期間/幣別/項目 block and must stay.)
            document.querySelectorAll('.canvas.chart')
                .forEach(e => e.style.setProperty('display', 'none', 'important'));
        }""")
        self.page.emulate_media(media="print")
        stamp = dt.datetime.now().strftime("%Y/%m/%d %H:%M")
        url = URL.format(ym=f"{y:04d}-{m:02d}")
        pages = 0
        for scale in (0.8, 0.72, 0.65):
            self.page.pdf(path=pdf_out, format="A4", print_background=True, scale=scale,
                          margin={"top": "12mm", "bottom": "16mm", "left": "12mm", "right": "12mm"})
            import pymupdf
            with pymupdf.open(pdf_out) as d:
                pages = d.page_count
            if pages == 1:
                break
        self.page.emulate_media(media="screen")
        hit = circle_cash_sell(pdf_out, target_date,
                               stamp_text=f"資料來源：{url}　　列印時間：{stamp}")
        hit["pages"] = pages
        if hit["value"] != want[0]["cash_sell"]:
            raise RuntimeError(f"圈選值 {hit['value']} 與網頁現金賣出 {want[0]['cash_sell']} 不符")
        return hit


def circle_cash_sell(pdf_path, target_date: str, stamp_text: str | None = None):
    """Draw a red ellipse around the cash-sell value on the target date's row."""
    import pymupdf
    doc = pymupdf.open(pdf_path)
    ds = target_date.replace("-", "/")
    hit = None
    for pi, p in enumerate(doc):
        words = p.get_text("words")
        sells = [w for w in words if w[4] == "本行賣出"]
        dates = [w for w in words if w[4] == ds]
        if not dates:
            continue
        if not sells:
            # header on a previous page: reuse column x from page 0
            sells = [w for w in doc[0].get_text("words") if w[4] == "本行賣出"]
        cash_sell_hdr = min(sells, key=lambda w: w[0])  # first 本行賣出 = 現金匯率 column
        nums = []
        for dw in dates:  # the date can also appear in 查詢期間; take the table row
            yc = (dw[1] + dw[3]) / 2
            row = [w for w in words if abs((w[1] + w[3]) / 2 - yc) < 4 and w is not dw]
            nums = [w for w in row if _isnum(w[4])]
            if len(nums) >= 4 and any("USD" in w[4] for w in row):
                break
        else:
            continue
        hdr_xc = (cash_sell_hdr[0] + cash_sell_hdr[2]) / 2
        nums.sort(key=lambda w: w[0])
        # choose the number whose cell is nearest to the header column centre
        cell = min(nums, key=lambda w: abs((w[0] + w[2]) / 2 - hdr_xc))
        if cell is not nums[1]:
            raise RuntimeError("現金賣出欄位定位與欄序不一致，請人工檢查")
        r = pymupdf.Rect(cell[:4])
        tight = p.search_for(cell[4], clip=r + (-1, -1, 1, 1))
        if tight:
            r = tight[0]
        oval = pymupdf.Rect(r.x0 - 7, r.y0 - 3, r.x1 + 7, r.y1 + 3)
        sh = p.new_shape()
        sh.draw_oval(oval)
        sh.finish(color=(0.75, 0.1, 0.12), width=1.4)
        sh.commit()
        hit = dict(page=pi, value=cell[4], bbox=list(oval))
        break
    if not hit:
        raise RuntimeError(f"列印稿中找不到 {ds}")
    if stamp_text:
        from fonts import cjk_font
        fname, ffile = cjk_font()
        for pi, p in enumerate(doc):
            txt = stamp_text + (f"　　第 {pi + 1}／{doc.page_count} 頁" if doc.page_count > 1 else "")
            p.insert_text((34, p.rect.height - 20), txt, fontsize=7.5, fontname=fname,
                          fontfile=ffile, color=(0.2, 0.2, 0.2))
    tmp = pdf_path + ".tmp.pdf"
    doc.subset_fonts()
    doc.save(tmp, garbage=4, deflate=True)
    doc.close()
    import os
    os.replace(tmp, pdf_path)
    return hit


def _isnum(s):
    try:
        Decimal(s)
        return True
    except Exception:
        return False


def find_rate(sess: BotSession, departure: dt.date, max_back_days=31):
    target = departure - dt.timedelta(days=1)
    if target >= dt.date.today():
        raise NotYet(f"出發前一日 {target} 尚未過完（今天 {dt.date.today()}），當日牌告未定，不能以較早日期代替")
    y, m = target.year, target.month
    checked = []
    floor = target - dt.timedelta(days=max_back_days)
    while True:
        rows = sess.load_month(y, m)
        checked.append(f"{y:04d}-{m:02d}")
        cands = [r for r in rows if r["date"] <= target.isoformat() and r["cash_sell"] not in ("", "-")]
        if cands:
            best = max(cands, key=lambda r: r["date"])
            skipped = (target - dt.date.fromisoformat(best["date"])).days
            return dict(departure=departure.isoformat(), target_day=target.isoformat(),
                        rate_date=best["date"], cash_sell=best["cash_sell"],
                        backtracked_days=skipped, months_checked=checked,
                        month_url=URL.format(ym=f"{y:04d}-{m:02d}"), year=y, month=m,
                        row=best)
        if dt.date(y, m, 1) <= floor:
            raise NoPosting(f"{target} 前 {max_back_days} 天內查無牌告（已查 {checked}）")
        y, m = _prev_month(y, m)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--departure", required=True, help="出發日期 YYYY-MM-DD")
    ap.add_argument("--pdf-out", help="輸出匯率證明 PDF 路徑")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    dep = dt.date.fromisoformat(a.departure)
    try:
        sess = BotSession()
    except Exception as e:
        print(f"ACCESS_ERROR 無法啟動瀏覽器：{e}", file=sys.stderr)
        return 2
    try:
        info = find_rate(sess, dep)
        if a.pdf_out:
            info["circle"] = sess.print_month(info["year"], info["month"], info["rate_date"], a.pdf_out)
    except AccessError as e:
        print(f"ACCESS_ERROR 臺銀網站存取失敗（非無牌告）：{e}", file=sys.stderr)
        return 2
    except NoPosting as e:
        print(f"NO_POSTING {e}", file=sys.stderr)
        return 3
    except NotYet as e:
        print(f"NOT_YET {e}", file=sys.stderr)
        return 4
    finally:
        sess.close()
    print(json.dumps(info, ensure_ascii=False, indent=1) if a.json else
          f'{info["departure"]} 出發 → 前一日 {info["target_day"]} → 採用 {info["rate_date"]} '
          f'現金賣出 {info["cash_sell"]}（回推 {info["backtracked_days"]} 天；查 {info["months_checked"]}）')
    return 0


if __name__ == "__main__":
    sys.exit(main())
