#!/usr/bin/env python3
"""每日新聞摘要:RSS -> Gemini 篩選 -> 抓全文 -> Gemini 摘要+趨勢 -> PDF(存桌面)+ Gmail 寄出

執行:  py main.py
金鑰不要寫在程式裡,請放在同資料夾的 .env 檔(格式見最下方說明)。
"""
import html
import io
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import feedparser
from google import genai
from google.genai import types
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from xhtml2pdf import pisa

try:  # 抓全文用;沒安裝也能跑,只是趨勢分析會比較淺
    import trafilatura
except ImportError:
    trafilatura = None
try:  # 把 Google News 的加密連結還原成原文網址
    from googlenewsdecoder import gnewsdecoder
except ImportError:
    gnewsdecoder = None


# ==================== 讀取設定(.env) ====================
def load_env():
    base = Path(__file__).resolve().parent
    # 容許常見的檔名:.env、.env.txt、env.txt(Windows 常把副檔名藏起來)
    p = next((base / n for n in (".env", ".env.txt", "env.txt") if (base / n).exists()), None)
    if p is None:
        return
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = os.getenv("GMAIL_USER", "")
SENDER_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
RECEIVER_EMAIL = os.getenv("MAIL_TO") or SENDER_EMAIL
MODELS = [os.getenv("GEMINI_MODEL", "gemini-3.5-flash"), "gemini-3.5-flash-lite"]

# ==================== 參數 ====================
HOURS = 36               # 只看最近幾小時的新聞
PER_FEED = 20            # 每個來源最多取幾則
PER_REGION_POOL = 30     # 每區最多送幾則標題給 AI 篩選
PICKS_PER_REGION = 2     # 每區最多入選幾則
ARTICLE_CHARS = 3500     # 每篇全文最多送多少字給 AI
UA = "Mozilla/5.0 (compatible; DailyNewsBot/1.0)"


def gnews(query):
    q = query.replace(" ", "+")
    return f"https://news.google.com/rss/search?q={q}+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"


# 想加「生活風格 / 文化旅遊」就在這裡新增一個區域,例如:
#   "生活": [("生活", gnews("生活風格 OR 文化旅遊"))],
FEEDS = {
    "台灣": [("中央社", gnews("site:cna.com.tw")), ("報導者", gnews("site:twreporter.org")),
           ("公視新聞", gnews("site:news.pts.org.tw"))],
    "國際": [("BBC中文", "https://feeds.bbci.co.uk/zhongwen/trad/rss.xml"),
           ("DW中文", gnews("site:dw.com/zh")), ("法廣", gnews("site:rfi.fr/tw")),
           ("NHK", gnews("site:nhk.or.jp"))],
}
REGIONS = list(FEEDS)
COLORS = {"台灣": "#1f7a4d", "國際": "#1a5fb4"}

client = None


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


# ==================== Gemini ====================
def llm_json(prompt, retries=4):
    last = None
    models = list(MODELS)
    for attempt in range(retries):
        for model in list(models):
            try:
                r = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json", temperature=0.3))
                text = re.sub(r"^```(?:json)?|```$", "", (r.text or "").strip(), flags=re.M).strip()
                return json.loads(text)
            except Exception as e:
                last = e
                log(f"[Gemini] {model} 第 {attempt + 1} 次失敗:{type(e).__name__}: {str(e)[:150]}")
                if "NOT_FOUND" in str(e) or "404" in str(e)[:40]:
                    models.remove(model)  # 模型名稱不能用,不要再重試
                    continue
                time.sleep(6)
        if not models:
            break
        time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"Gemini 多次失敗:{last}")


# ==================== 1. 抓新聞 ====================
def fetch_all():
    cutoff = datetime.utcnow() - timedelta(hours=HOURS)
    items = []
    for region, sources in FEEDS.items():
        pool, seen = [], []
        for name, url in sources:
            try:
                feed = feedparser.parse(url, agent=UA)
            except Exception as e:
                log(f"[RSS] {region}/{name} 失敗:{e}")
                continue
            if not feed.entries:
                log(f"[RSS] {region}/{name} 沒有資料")
                continue
            for e in feed.entries[:PER_FEED]:
                ts = e.get("published_parsed") or e.get("updated_parsed")
                if ts and datetime(*ts[:6]) < cutoff:
                    continue
                title = strip_tags(e.get("title", ""))
                if not title or any(SequenceMatcher(None, title, t).ratio() > 0.8 for t in seen):
                    continue
                seen.append(title)
                pool.append({"region": region, "source": name, "title": title,
                             "summary": strip_tags(e.get("summary", ""))[:200],
                             "link": e.get("link", "")})
        items.extend(pool[:PER_REGION_POOL])
    for i, it in enumerate(items):
        it["id"] = i
    log(f"[RSS] 共 {len(items)} 則候選新聞")
    return items


# ==================== 2. 篩選 ====================
def pick_articles(items):
    listing = "\n".join(
        f"{it['id']}|{it['region']}|{it['source']}|{it['title']}|{it['summary'][:100]}" for it in items)
    prompt = f"""你是嚴謹的國際新聞編輯。以下是最近 {HOURS} 小時的新聞清單,格式:id|區域|來源|標題|摘要

任務:為「{'、'.join(REGIONS)}」各挑出最多 {PICKS_PER_REGION} 則最值得讀的新聞。

【必須排除】選舉、民調、選情、政黨或政治人物互相攻訐的口水戰、社會案件與名人八卦、
純表態或口號式聲明、沒有新事實的評論、重複事件(同一事件只選資訊最完整的一則)。
【優先選擇】有具體事件、數據、政策或制度變動,影響範圍大、有新資訊的新聞。
若某區域沒有合格新聞,可以少選或不選,不要湊數。

只輸出 JSON:{{"picks":[{{"id":數字,"reason":"一句話"}}]}}

新聞清單:
{listing}"""
    data = llm_json(prompt)
    by_id = {it["id"]: it for it in items}
    count = {r: 0 for r in REGIONS}
    picked = []
    for p in data.get("picks", []):
        it = by_id.get(p.get("id")) if isinstance(p, dict) else None
        if it and count[it["region"]] < PICKS_PER_REGION:
            count[it["region"]] += 1
            picked.append(it)
    log(f"[篩選] 入選 {len(picked)} 則:{count}")
    return picked


# ==================== 3. 抓全文 ====================
def resolve_link(url):
    if "news.google.com" in url and gnewsdecoder:
        try:
            r = gnewsdecoder(url, interval=1)
            if r.get("status") and r.get("decoded_url"):
                return r["decoded_url"]
        except Exception as e:
            log(f"[解碼] 失敗:{type(e).__name__}")
    return url


def get_fulltext(it):
    it["link"] = resolve_link(it["link"])
    if not trafilatura:
        return ""
    try:
        raw = trafilatura.fetch_url(it["link"])
        text = trafilatura.extract(raw, include_comments=False) if raw else None
    except Exception:
        text = None
    return text[:ARTICLE_CHARS] if text and len(text) > 300 else ""


# ==================== 4. 摘要 + 趨勢 ====================
def analyze(picked):
    blocks = []
    for it in picked:
        text = get_fulltext(it)
        it["basis"] = "全文" if text else "僅標題與摘要"
        body = text or f"(無法取得全文,只有標題與摘要)\n摘要:{it['summary']}"
        blocks.append(f"### id={it['id']}|{it['region']}|{it['source']}\n標題:{it['title']}\n{body}")
        time.sleep(0.5)
    prompt = f"""你是資深國際新聞分析師,請用繁體中文為下列每篇新聞撰寫電子報條目。

【鐵則】
1. 事實摘要只能使用文章提供的內容,不可自行補充文中沒有的人名、數字、事件。
2. 延伸趨勢屬於推論,必須寫出推論依據;不確定時給 2 種可能情境,不要只給單一預測。
3. 若只有標題與摘要(無全文),趨勢要更保守,並在 caveat 註明資訊有限。
4. 若讀完發現其實屬於選舉、民調、口水戰或空泛內容,skip 設為 true。

每篇輸出欄位:id(照原樣的數字)、headline(清楚的繁中標題)、summary(事實摘要,三句內)、
why(為什麼重要,一到兩句)、short_term(短期 1-3 個月走向推論,含依據)、
mid_term(中期半年以上走向推論,含依據)、indicators(2-3 個觀察指標,字串陣列)、
caveat(資訊限制,沒有就給空字串)、skip(true/false)

只輸出 JSON:{{"items":[...]}}

文章:
{chr(10).join(blocks)}"""
    data = llm_json(prompt)
    return {x["id"]: x for x in data.get("items", []) if isinstance(x, dict) and "id" in x}


# ==================== 5. 排版(HTML,同一份給 PDF 與信件)====================
def e(s):
    return html.escape(str(s or ""), quote=True)


def render_html(date_str, sections, font_family, page_size="A4", font_css=""):
    parts = []
    for region in REGIONS:
        cards = sections.get(region, [])
        if not cards:
            continue
        color = COLORS.get(region, "#333333")
        parts.append(f'<h2 style="color:{color};border-bottom:2px solid {color};'
                     f'padding-bottom:3px;margin-top:18px;">{e(region)}</h2>')
        for c in cards:
            inds = "".join(f"<li>{e(i)}</li>" for i in (c.get("indicators") or []))
            caveat = f'<p style="color:#888888;">注意:{e(c["caveat"])}</p>' if c.get("caveat") else ""
            parts.append(f"""
<div style="border:1px solid #cccccc;padding:8px;margin-bottom:10px;">
  <h3 style="margin:0 0 4px 0;"><a href="{e(c['link'])}" style="color:#111111;text-decoration:none;">{e(c['headline'])}</a></h3>
  <p style="color:#888888;margin:0 0 4px 0;">{e(c['source'])} / 依據:{e(c['basis'])}</p>
  <p>{e(c['summary'])}</p>
  <p><b>為什麼重要:</b>{e(c['why'])}</p>
  <div style="background-color:#eef2f7;padding:6px;">
    <p style="color:{color};margin:0 0 3px 0;"><b>延伸趨勢(AI 推論)</b></p>
    <p style="margin:2px 0;"><b>短期(1-3 個月):</b>{e(c['short_term'])}</p>
    <p style="margin:2px 0;"><b>中期(半年以上):</b>{e(c['mid_term'])}</p>
    <p style="margin:4px 0 0 0;"><b>觀察指標:</b></p>
    <ul style="margin:0;">{inds}</ul>
  </div>{caveat}
  <p><a href="{e(c['link'])}" style="color:#1a5fb4;">閱讀原文</a></p>
</div>""")
    body = "\n".join(parts) or "<p>今天沒有符合條件的新聞。</p>"
    return f"""<html><head><meta charset="utf-8">
<style>
{font_css}
@page {{ size: {page_size}; margin: 1.5cm; }}
body {{ font-family: {font_family}; font-size: 11px; line-height: 1.5; color: #222222; }}
body, p, li, div, h1, h2, h3 {{ -pdf-word-wrap: CJK; }}
h1 {{ font-size: 22px; margin-bottom: 2px; }}
h2 {{ font-size: 16px; }}
h3 {{ font-size: 13px; }}
</style></head><body>
<h1>每日焦點電子報</h1>
<p style="color:#666666;">{e(date_str)} / 台灣 · 國際</p>
{body}
<p style="color:#999999;font-size:9px;margin-top:20px;">本內容由 AI 自動整理。事實摘要依據原文;「延伸趨勢」為 AI 推論,僅供參考。</p>
</body></html>"""


# ==================== 6. PDF ====================
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/notosanstc/NotoSansTC%5Bwght%5D.ttf"


def setup_pdf_font():
    """準備 PDF 用的中文字型。回傳 (font-family 名稱, @font-face CSS)。
    xhtml2pdf 需要用 @font-face 指向 .ttf 檔(.ttc 集合檔不支援)。"""
    here = Path(__file__).resolve().parent
    local = here / "fonts" / "NotoSansTC.ttf"

    def face(path):
        u = Path(path).as_posix()
        return ("".join(
            f'@font-face {{ font-family: cjk; font-weight: {w}; src: url("{u}"); }}\n'
            for w in ("normal", "bold")))

    env_font = os.getenv("FONT_PATH")
    if env_font and Path(env_font).exists():
        return "cjk", face(env_font)
    if local.exists():
        return "cjk", face(local)
    try:  # 下載開源的 Noto Sans TC(只需下載一次)
        local.parent.mkdir(exist_ok=True)
        log("[PDF] 下載 Noto Sans TC 字型(首次執行才需要)...")
        urllib.request.urlretrieve(FONT_URL, local)
        return "cjk", face(local)
    except Exception as ex:
        log(f"[PDF] 字型下載失敗:{ex}")
    for path in (r"C:\Windows\Fonts\kaiu.ttf",):  # 標楷體(Windows 內建的單檔 TTF)
        if os.path.exists(path):
            return "cjk", face(path)
    log("[PDF] 找不到中文字型檔,改用內建 MSung-Light(PDF 閱讀器需支援繁中顯示)")
    pdfmetrics.registerFont(UnicodeCIDFont("MSung-Light"))
    pdfmetrics.registerFontFamily("MSung-Light", normal="MSung-Light", bold="MSung-Light",
                                  italic="MSung-Light", boldItalic="MSung-Light")
    return "MSung-Light", ""


def output_dir():
    desktop = Path.home() / "Desktop"
    return desktop if desktop.exists() else Path(__file__).resolve().parent


MM = 72 / 25.4  # 1 mm = 幾個 PDF 點


def _measure(pdf_bytes):
    """回傳 (頁數, 第一頁高度, 第一頁最低一行文字的 y 座標),單位為 PDF 點。"""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    page = reader.pages[0]
    ys = []

    def visitor(text, cm, tm, font_dict, font_size):
        if text and text.strip():
            ys.append(tm[4] * cm[1] + tm[5] * cm[3] + cm[5])

    page.extract_text(visitor_text=visitor)
    return len(reader.pages), float(page.mediabox.height), (min(ys) if ys else 0.0)


def make_pdf(date_str, sections, font, font_css, path):
    """產生「一頁式」PDF:整份內容排在一張連續的長頁上,不分頁。
    作法:先用超高頁面排版,量出內容實際高度,再用剛好的高度重排一次。
    失敗時退回一般 A4 多頁 PDF。"""
    def build(h_mm):
        return render_html(date_str, sections, font, f"210mm {h_mm:.0f}mm", font_css)

    try:
        tall = io.BytesIO()
        if pisa.CreatePDF(build(3000), dest=tall, encoding="utf-8").err:
            raise RuntimeError("排版失敗")
        _, page_h, low_y = _measure(tall.getvalue())
        need_mm = (page_h - low_y) / MM + 15  # 下方留 1.5cm 邊界
        for extra in (10, 30, 80):
            out = io.BytesIO()
            pisa.CreatePDF(build(max(150, need_mm + extra)), dest=out, encoding="utf-8")
            if _measure(out.getvalue())[0] == 1:
                Path(path).write_bytes(out.getvalue())
                return True
        log("[PDF] 一頁式高度計算未成功,改用 A4 多頁")
    except Exception as ex:
        log(f"[PDF] 一頁式失敗({type(ex).__name__}: {ex}),改用 A4 多頁")
    with open(path, "wb") as f:
        status = pisa.CreatePDF(render_html(date_str, sections, font, font_css=font_css), dest=f, encoding="utf-8")
    return not status.err


# ==================== 7. 寄信 ====================
def send_mail(subject, html_body, pdf_path):
    if not (SENDER_EMAIL and SENDER_PASSWORD):
        log("[寄信] 未設定 GMAIL_USER / GMAIL_APP_PASSWORD,略過寄信")
        return
    msg = MIMEMultipart("mixed")
    msg["Subject"], msg["From"], msg["To"] = subject, SENDER_EMAIL, RECEIVER_EMAIL
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    if pdf_path and Path(pdf_path).exists():
        part = MIMEApplication(Path(pdf_path).read_bytes(), _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", Path(pdf_path).name))
        msg.attach(part)
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(SENDER_EMAIL, SENDER_PASSWORD)
        s.send_message(msg)
    log(f"[寄信] 已寄給 {RECEIVER_EMAIL}")


# ==================== 主程式 ====================
def main():
    global client
    if not GEMINI_API_KEY:
        sys.exit("找不到 GEMINI_API_KEY。請在 main.py 同資料夾建立 .env 檔,內容見檔案最下方說明。")
    client = genai.Client(api_key=GEMINI_API_KEY)

    date_str = datetime.now().strftime("%Y-%m-%d")
    items = fetch_all()
    if not items:
        sys.exit("沒有抓到任何新聞,請檢查網路或 RSS 來源。")

    picked = pick_articles(items)
    analyzed = analyze(picked) if picked else {}
    sections = {r: [] for r in REGIONS}
    for it in picked:
        a = analyzed.get(it["id"])
        if a and not a.get("skip"):
            sections[it["region"]].append({**it, **a})

    font, font_css = setup_pdf_font()
    pdf_path = output_dir() / f"每日新聞摘要_{datetime.now().strftime('%Y%m%d')}.pdf"
    ok = make_pdf(date_str, sections, font, font_css, pdf_path)
    log(f"[PDF] {'已儲存:' + str(pdf_path) if ok else '產生失敗(信件仍會寄出,但不含附件)'}")

    mail_html = render_html(date_str, sections, "'Microsoft JhengHei','PingFang TC',sans-serif")
    send_mail(f"每日焦點電子報 {date_str}", mail_html, pdf_path if ok else None)


if __name__ == "__main__":
    main()

# ====================================================================
# .env 檔內容範例(存成「.env」,放在 main.py 同一個資料夾,不要加引號以外的空格):
#
#   GEMINI_API_KEY=你的新金鑰
#   GMAIL_USER=你的Gmail
#   GMAIL_APP_PASSWORD=新的16碼應用程式密碼
#   MAIL_TO=收件信箱(可省略,預設寄給自己)
#
# 需要安裝:pip install feedparser google-genai xhtml2pdf trafilatura googlenewsdecoder pypdf
# ====================================================================
