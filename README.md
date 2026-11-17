# 國外出差每日生活費換算

供 Codex 與 Claude Code 使用的 skill，依出發日期、目的地城市和使用者提供的生活費日支數額表，產出可列印的報帳附件。

## 計算規則

- 使用臺灣銀行出發前一日美元現金賣出匯率，遇無牌告日往前查找。
- 依城市查美元日支，乘以匯率後捨去小數至新臺幣整元。
- 計算各目的地每日生活費。
- 日支表保留對應原始頁，圈出城市和費率，空白處加換算式。
- 臺銀歷史月表列印成 PDF，圈出採用匯率。

## 安裝

將此儲存庫下載後放到個人 skills 目錄，資料夾命名為 overseas-per-diem。

Codex 路徑為 ~/.codex/skills/overseas-per-diem，Claude Code 路徑為 ~/.claude/skills/overseas-per-diem。

需要 Python 3.10 以上與 zsh。首次使用，在 skill 目錄執行以下指令，建立獨立環境並安裝 PyMuPDF、Playwright。

```bash
zsh scripts/setup.sh
```

腳本優先使用已安裝的 Google Chrome，若無 Chrome，安裝程序會下載 Chromium。中文 PDF 字型優先使用本機字型，亦提供內建中文字型備援。

## 使用範例

```text
請用 overseas-per-diem 計算 2026/08/26 出發，
目的地倫敦、愛丁堡、布達佩斯的每日生活費。
日支表使用我提供的適用版本 PDF。
請將兩份 PDF 放入指定的輸出資料夾。
```

亦可直接執行，在儲存庫根目錄執行以下指令，將 PDF 路徑改為實際來源。

```bash
.venv/bin/python scripts/build_packet.py \
  --pdf "/absolute/path/to/per-diem-table.pdf" \
  --departure 2026-08-26 \
  --dest 倫敦 --dest "愛丁堡=英國:其他" --dest 布達佩斯 \
  --out "/absolute/path/to/output-folder"
```

## 輸出

- 01 日支數額表.pdf
- 02 臺銀美元匯率證明.pdf

執行後會提供 PNG 預覽與摘要 JSON，交付前須核對圈選、算式與版面。需自行提供適用年度的日支數額表 PDF。臺銀網站可用性及頁面格式會影響線上查詢，失敗時腳本會回報原因。

## 已驗證案例

2026/08/26 出發，採 2026/08/25 美元現金賣出匯率 32.14。

| 城市 | 美元日支 | 新臺幣每日生活費 |
|---|---:|---:|
| 倫敦 | 510 | 16,391 |
| 愛丁堡（英國其他） | 324 | 10,413 |
| 布達佩斯 | 261 | 8,388 |

匯率來源為 [臺灣銀行歷史牌告](https://rate.bot.com.tw/xrt/history?Lang=zh-TW)。完整操作規則見 [SKILL.md](SKILL.md)。
