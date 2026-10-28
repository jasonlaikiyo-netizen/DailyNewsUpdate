#!/usr/bin/env python3
"""每日焦點國際新聞電子報(免費版:RSS + Gemini + Gmail + GitHub Actions)

流程:抓 RSS → 階段一 AI 篩選(排除選舉/空泛)→ 抓全文 → 階段二 AI 摘要+趨勢
     → 程式排版 HTML → 寄信 + 存檔到 docs/

用法:
    python newsletter.py                # 正常執行(沒設 Gmail 金鑰就只存檔不寄信)
    python newsletter.py --check-feeds  # 檢查每個新聞來源還能不能抓
"""
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import feedparser
import trafilatura
from google import genai
from google.genai import types

# ───────────────────────── 設定區 ─────────────────────────
TZ = timezone(timedelta(hours=8))  # 台灣時間
HOURS = 36                         # 只看最近幾小時內的新聞
PER_FEED = 25                      # 每個來源最多取幾則
PER_REGION_POOL = 40               # 每個區域最多送幾則標題給 AI 篩選
PICKS_PER_REGION = 3               # 每個區域最多入選幾則
ARTICLE_CHARS = 3500               # 每篇全文最多送給 AI 多少字
MODELS = [
    os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
    "gemini-3.5-flash-lite",       # 主模型不可用或額度用完時的備援
]
UA = "Mozilla/5.0 (compatible; DailyNewsBot/1.0)"
DOCS = Path(__file__).parent / "docs"


def gnews(query: str) -> str:
    """Google News RSS(依站點查詢)。沒有官方 RSS 的媒體用這個。"""
    q = query.replace(" ", "+")
    return (f"https://news.google.com/rss/search?q={q}+when:1d"
            "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")


# 來源可自由增減。有確認可用的官方 RSS 就直接換上去,抓全文成功率比 Google News 高。
# 先跑一次 `python newsletter.py --check-feeds` 看哪些抓得到。
FEEDS = {
    "台灣": [
        ("中央社", gnews("site:cna.com.tw")),
        ("報導者", gnews("site:twreporter.org")),
        ("公視新聞", gnews("site:news.pts.org.tw")),
    ],
    "國際": [
        ("BBC中文", "https://feeds.bbci.co.uk/zhongwen/trad/rss.xml"),
        ("DW中文", gnews("site:dw.com/zh")),
        ("法廣", gnews("site:rfi.fr/tw")),
        ("NHK", gnews("site:nhk.or.jp")),
    ],
}
REGIONS = list(FEEDS.keys())
REGION_COLORS = {"台灣": "#1f7a4d", "國際": "#1a5fb4"}

client = genai.Client()  # 讀環境變數 GEMINI_API_KEY


# ───────────────────────── 工具函式 ─────────────────────────
def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def llm_json(prompt: str, retries: int = 4):
    """呼叫 Gemini 並解析 JSON;失敗時重試、換備援模型。模型不存在(404)就直接跳過該模型。"""
    last = None
    models = list(MODELS)
    for attempt in range(retries):
        for model in list(models):
            try:
                r = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.3,
                    ),
                )
                text = re.sub(r"^```(?:json)?|```$", "", (r.text or "").strip(), flags=re.M).strip()
                return json.loads(text)
            except Exception as e:  # 額度、網路、JSON 格式錯誤都走這裡
                last = e
                log(f"[LLM] {model} 第 {attempt + 1} 次失敗:{type(e).__name__}: {str(e)[:160]}")
                if "NOT_FOUND" in str(e) or "404" in str(e)[:40]:
                    models.remove(model)  # 這個模型名稱不能用,不要再重試
                    continue
                time.sleep(6)
        if not models:
            break
        time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"Gemini 多次失敗:{last}")


# ───────────────────────── 1. 抓新聞 ─────────────────────────
def fetch_all() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    items: list[dict] = []
    for region, sources in FEEDS.items():
        pool, seen_titles = [], []
        for name, url in sources:
            try:
                feed = feedparser.parse(url, agent=UA)
            except Exception as e:
                log(f"[RSS] {region}/{name} 失敗:{e}")
                continue
            if not feed.entries:
                log(f"[RSS] {region}/{name} 沒有資料(網址可能失效)")
                continue
            for e in feed.entries[:PER_FEED]:
                ts = e.get("published_parsed") or e.get("updated_parsed")
                if ts and datetime(*ts[:6], tzinfo=timezone.utc) < cutoff:
                    continue
                title = strip_tags(e.get("title", ""))
                if not title:
                    continue
                # 去重:同區域內標題太像就略過(同一事件多家報導時保留先出現的)
                if any(SequenceMatcher(None, title, t).ratio() > 0.8 for t in seen_titles):
                    continue
                seen_titles.append(title)
                pool.append({
                    "region": region,
                    "source": name,
                    "title": title,
                    "summary": strip_tags(e.get("summary", ""))[:200],
                    "link": e.get("link", ""),
                })
        items.extend(pool[:PER_REGION_POOL])
    for i, it in enumerate(items):
        it["id"] = i
    log(f"[RSS] 共 {len(items)} 則候選新聞")
    return items


def check_feeds():
    for region, sources in FEEDS.items():
        for name, url in sources:
            try:
                n = len(feedparser.parse(url, agent=UA).entries)
            except Exception as e:
                n = f"錯誤 {e}"
            print(f"{'OK ' if isinstance(n, int) and n else 'XX '} {region}/{name}: {n}")


# ───────────────────────── 2. 階段一:篩選 ─────────────────────────
def stage1_pick(items: list[dict]) -> list[dict]:
    listing = "\n".join(
        f"{it['id']}|{it['region']}|{it['source']}|{it['title']}|{it['summary'][:100]}" for it in items
    )
    prompt = f"""你是嚴謹的國際新聞編輯。以下是最近 {HOURS} 小時的新聞清單,
格式為:id|區域|來源|標題|摘要

任務:為「{'、'.join(REGIONS)}」各挑出最多 {PICKS_PER_REGION} 則最值得讀的新聞。

【必須排除】
- 選舉、民調、選情、政黨或政治人物互相攻訐的口水戰
- 名人八卦、純表態、口號式聲明、沒有新事實的評論或社論
- 重複事件(同一事件只選資訊最完整的一則)

【優先選擇】
有具體事件、數據、政策或制度變動,且影響範圍大、帶來新資訊的新聞
(經濟與產業、科技、外交與安全、能源氣候、社會制度、重大災害等)。

若某區域沒有合格新聞,可以少選甚至不選,不要湊數。
只輸出 JSON,格式:{{"picks":[{{"id":數字,"reason":"一句話理由"}}]}}

新聞清單:
{listing}"""
    data = llm_json(prompt)
    by_id = {it["id"]: it for it in items}
    count = {r: 0 for r in REGIONS}
    picked = []
    for p in data.get("picks", []):
        it = by_id.get(p.get("id"))
        if not it or count[it["region"]] >= PICKS_PER_REGION:
            continue
        count[it["region"]] += 1
        picked.append(it)
    log(f"[篩選] 入選 {len(picked)} 則:{count}")
    return picked


# ───────────────────────── 3. 抓全文 ─────────────────────────
def resolve_link(url: str) -> str:
    """Google News 的連結是加密轉址,嘗試還原成原文網址。失敗就原樣回傳。"""
    if "news.google.com" not in url:
        return url
    try:
        from googlenewsdecoder import gnewsdecoder
        r = gnewsdecoder(url, interval=1)
        if r.get("status") and r.get("decoded_url"):
            return r["decoded_url"]
    except Exception as e:
        log(f"[解碼] 失敗:{type(e).__name__}")
    return url


def get_fulltext(it: dict) -> str:
    it["link"] = resolve_link(it["link"])
    try:
        raw = trafilatura.fetch_url(it["link"])
        text = trafilatura.extract(raw, include_comments=False, include_tables=False) if raw else None
    except Exception:
        text = None
    return (text or "")[:ARTICLE_CHARS] if text and len(text) > 300 else ""


# ───────────────────────── 4. 階段二:摘要 + 趨勢 ─────────────────────────
def stage2_analyze(picked: list[dict]) -> dict[int, dict]:
    blocks = []
    for it in picked:
        text = get_fulltext(it)
        it["basis"] = "全文" if text else "僅標題與摘要"
        body = text or f"(無法取得全文,只有標題與摘要)\n摘要:{it['summary']}"
        blocks.append(f"### id={it['id']}|{it['region']}|{it['source']}\n標題:{it['title']}\n{body}")
        time.sleep(0.5)
    articles = "\n\n".join(blocks)

    prompt = f"""你是資深國際新聞分析師,請用繁體中文為下列每篇新聞撰寫電子報條目。

【鐵則】
1. 「事實摘要」只能使用文章提供的內容,不可自行補充文中沒有的人名、數字、事件。
2. 「延伸趨勢」屬於推論,必須寫出推論依據;不確定時給出 2 種可能情境,不要只給單一預測。
3. 若該篇只有標題與摘要(無全文),趨勢要更保守,並在 caveat 欄位註明資訊有限。
4. 若讀完發現其實屬於選舉、民調、口水戰或空泛內容,將 skip 設為 true。

每篇輸出欄位:
- id:數字(照原樣)
- headline:改寫成清楚的繁中標題
- summary:事實摘要,三句以內
- why:為什麼重要(一到兩句)
- short_term:短期(1 到 3 個月)走向推論,含依據
- mid_term:中期(半年以上)走向推論,含依據
- indicators:接下來可觀察的 2 到 3 個指標或事件(字串陣列)
- caveat:資訊限制或不確定性(沒有就給空字串)
- skip:true 或 false

只輸出 JSON:{{"items":[...]}}

文章:
{articles}"""
    data = llm_json(prompt)
    return {x["id"]: x for x in data.get("items", []) if isinstance(x, dict) and "id" in x}


# ───────────────────────── 5. 排版 ─────────────────────────
def e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def render_html(date_str: str, sections: dict[str, list[dict]]) -> str:
    parts = []
    for region in REGIONS:
        cards = sections.get(region, [])
        if not cards:
            continue
        color = REGION_COLORS.get(region, "#333")
        parts.append(f'<h2 style="margin:32px 0 12px;padding-left:10px;border-left:5px solid {color};'
                     f'color:{color};font-size:20px;">{e(region)}</h2>')
        for c in cards:
            inds = "".join(f"<li>{e(i)}</li>" for i in (c.get("indicators") or []))
            caveat = (f'<p style="margin:8px 0 0;color:#888;font-size:13px;">⚠ {e(c["caveat"])}</p>'
                      if c.get("caveat") else "")
            parts.append(f"""
<div style="background:#fff;border:1px solid #e3e3e3;border-radius:10px;padding:16px 18px;margin-bottom:16px;">
  <h3 style="margin:0 0 8px;font-size:17px;line-height:1.4;">
    <a href="{e(c['link'])}" style="color:#111;text-decoration:none;">{e(c['headline'])}</a></h3>
  <p style="margin:0 0 4px;color:#888;font-size:12px;">{e(c['source'])} · 依據:{e(c['basis'])}</p>
  <p style="margin:8px 0;line-height:1.7;">{e(c['summary'])}</p>
  <p style="margin:8px 0;line-height:1.7;"><b>為什麼重要:</b>{e(c['why'])}</p>
  <div style="background:#f6f8fb;border-radius:8px;padding:10px 14px;margin-top:10px;">
    <p style="margin:0 0 6px;font-weight:bold;color:{color};">📈 延伸趨勢(AI 推論)</p>
    <p style="margin:4px 0;line-height:1.7;"><b>短期(1–3 個月):</b>{e(c['short_term'])}</p>
    <p style="margin:4px 0;line-height:1.7;"><b>中期(半年以上):</b>{e(c['mid_term'])}</p>
    <p style="margin:8px 0 2px;"><b>觀察指標:</b></p>
    <ul style="margin:0;padding-left:20px;line-height:1.7;">{inds}</ul>
  </div>{caveat}
  <p style="margin:10px 0 0;font-size:13px;"><a href="{e(c['link'])}" style="color:#1a5fb4;">閱讀原文 →</a></p>
</div>""")
    body = "\n".join(parts) or "<p>今天沒有符合條件的新聞。</p>"
    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日焦點電子報 {e(date_str)}</title></head>
<body style="margin:0;background:#f0f2f5;font-family:-apple-system,'Noto Sans TC','PingFang TC','Microsoft JhengHei',sans-serif;color:#222;">
<div style="max-width:680px;margin:0 auto;padding:24px 16px;">
  <h1 style="margin:0;font-size:24px;">每日焦點電子報</h1>
  <p style="margin:4px 0 0;color:#666;">{e(date_str)} · 台灣 / 國際</p>
  {body}
  <p style="margin-top:32px;color:#999;font-size:12px;line-height:1.6;">
    本電子報由 AI 自動整理。事實摘要依據原文;「延伸趨勢」為 AI 推論,僅供參考,請以原始報導與後續事件為準。</p>
</div></body></html>"""


def update_index():
    pages = sorted((p for p in DOCS.glob("20*.html")), reverse=True)
    rows = "\n".join(f'<li><a href="{p.name}">{p.stem}</a></li>' for p in pages)
    (DOCS / "index.html").write_text(
        f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>電子報存檔</title></head>
<body style="font-family:-apple-system,'Noto Sans TC',sans-serif;max-width:480px;margin:40px auto;padding:0 16px;">
<h1>每日焦點電子報存檔</h1><ul style="line-height:2;">{rows}</ul></body></html>""",
        encoding="utf-8",
    )


# ───────────────────────── 6. 寄信 ─────────────────────────
def send_mail(subject: str, html_body: str):
    user = os.getenv("GMAIL_USER")
    pwd = os.getenv("GMAIL_APP_PASSWORD")
    if not (user and pwd):
        log("[寄信] 未設定 GMAIL_USER / GMAIL_APP_PASSWORD,略過寄信")
        return
    to = os.getenv("MAIL_TO") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText("請使用支援 HTML 的信箱閱讀本電子報。", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pwd)
        s.send_message(msg)
    log(f"[寄信] 已寄給 {to}")


# ───────────────────────── 主程式 ─────────────────────────
def main():
    if "--check-feeds" in sys.argv:
        check_feeds()
        return

    date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    items = fetch_all()
    if not items:
        sys.exit("沒有抓到任何新聞,請檢查 RSS 來源(python newsletter.py --check-feeds)")

    picked = stage1_pick(items)
    analyzed = stage2_analyze(picked) if picked else {}

    sections: dict[str, list[dict]] = {r: [] for r in REGIONS}
    for it in picked:
        a = analyzed.get(it["id"])
        if not a or a.get("skip"):
            continue
        sections[it["region"]].append({**it, **a})

    page = render_html(date_str, sections)
    DOCS.mkdir(exist_ok=True)
    (DOCS / f"{date_str}.html").write_text(page, encoding="utf-8")
    update_index()
    send_mail(f"每日焦點電子報 {date_str}", page)
    log(f"[完成] docs/{date_str}.html")


if __name__ == "__main__":
    main()
