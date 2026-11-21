#!/usr/bin/env python3
# ربات خبر فرمول یک
# هر چند ساعت یک‌بار (پیش‌فرض ۵ ساعت) جدیدترین خبرها رو از چند سایت می‌گیره،
# به فارسی ترجمه می‌کنه و مستقیم توی کانال منتشر می‌کنه.
# با فرمان‌های /news_off و /news_on (توی چت خود ربات) خاموش و روشن می‌شه.
# فقط با کتابخونه‌های خود پایتون کار می‌کنه (بدون pip).
#
# حالت‌های اجرا:
#   python f1_news.py          اجرای عادی (برای GitHub Actions)
#   python f1_news.py now      بدون صبر کردن برای زمان‌بندی همین الان منتشر می‌کنه
#   python f1_news.py check    فقط سایت‌ها و ترجمه رو چک می‌کنه (چیزی منتشر نمی‌کنه)
#   python f1_news.py test     یک پیام آزمایشی مستقیماً در کانال منتشر می‌کند

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ====================== تنظیمات ======================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL = os.environ.get("CHANNEL", "@formulaone_esi")
PROXY = os.environ.get("PROXY") or None
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")     # آیدی عددی خودت؛ برای فرمان‌های روشن/خاموش

# ترجمه با Gemini (رایگان): کلید رو از aistudio.google.com بگیر و توی Secrets بذار.
# اگه کلید نباشه یا خطا بده، از مترجم رایگان گوگل استفاده می‌شه (کیفیت پایین‌تر).
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# اگه مدلی خطا یا محدودیت داد، بعدی امتحان می‌شه؛ اگه همه نبودن، لیست مدل‌ها از خود گوگل پرسیده می‌شه
GEMINI_MODELS = [m for m in os.environ.get(
    "GEMINI_MODELS",
    "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemma-4-31b-it,gemini-flash-latest"
).split(",") if m]
IRAN = timezone(timedelta(hours=3, minutes=30))

FOOTER_URL = "https://t.me/formulaone_esi"

# منبع‌ها. type=rss  یعنی فید RSS ، type=html یعنی خوندن صفحه لیست خبرها
SOURCES = [
    {"name": "Autosport", "type": "rss",
     "url": "https://www.autosport.com/rss/feed/f1", "require": "/f1/"},
    {"name": "Motorsport.com", "type": "rss",
     "url": "https://www.motorsport.com/rss/f1/news/", "require": "/f1/"},
    {"name": "RacingNews365", "type": "html",
     "url": "https://racingnews365.com/f1-news", "base": "https://racingnews365.com"},
    # فید RSS فارسی سایت فرمول یک ایران
    {"name": "F1Iran", "type": "rss", "url": "https://f1iran.com/feed", "language": "fa"},
]

POST_INTERVAL_HOURS = float(os.environ.get("POST_INTERVAL_HOURS", "5"))  # فاصله بین دو نوبت انتشار
POSTS_PER_SLOT = int(os.environ.get("POSTS_PER_SLOT", "3"))    # در هر نوبت چند خبر (جدیدترین‌ها)
MAX_WINDOW_HOURS = 12                 # حتی بعد از خاموشی طولانی، خبر قدیمی‌تر از این منتشر نمی‌شه
SUMMARY_CHARS = 300                   # طول خلاصه انگلیسی قبل از ترجمه

EXCLUDE_TITLE = re.compile(r"\b(formula e|indycar|nascar|motogp|wrc|wec)\b", re.I)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_state.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ====================== ابزارهای شبکه ======================


def _opener():
    if PROXY:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return urllib.request.build_opener()


def http_get(url, timeout=25, max_bytes=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with _opener().open(req, timeout=timeout) as r:
        return r.read(max_bytes) if max_bytes else r.read()


def tg(method, params=None, timeout=30):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        with _opener().open(urllib.request.Request(url, data=data), timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "description": str(e)}


# ====================== وضعیت (خبرهای دیده‌شده و ...) ======================


def load_state():
    st = {"seen": [], "titles": [], "offset": 0, "done": [],
          "enabled": True, "last_post": "", "cmds_set": False}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st.update(json.load(f))
    except Exception:
        pass
    return st


def save_state(st):
    st["seen"] = st["seen"][-3000:]
    st["titles"] = st["titles"][-400:]
    st["done"] = st["done"][-800:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


# ====================== پاک‌سازی متن ======================

_TRAIL = re.compile(
    r"(\.\.\.|…)?\s*(keep reading|read more|read on|continue reading|read the full story)\W*$",
    re.I)


def clean_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"(?i)\s*the post .* appeared first on .*$", "", s)
    s = _TRAIL.sub("", s).strip()
    return s


def make_summary(text, limit=SUMMARY_CHARS):
    text = clean_html(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if m > 80:
        return cut[:m + 1]
    return cut.rsplit(" ", 1)[0] + "…"


def canonical(url):
    p = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def item_id(url):
    return hashlib.sha1(canonical(url).encode()).hexdigest()[:12]


_STOP = set("the a an of to in for on at and is are as with after by from his her its this "
            "that be has have will could would says say over into out up new و از به در را با برای که این آن یک پس تا هم اما".split())


def tokens(title):
    # پشتیبانی از عنوان‌های فارسی و انگلیسی برای تشخیص خبرهای تکراری
    words = re.findall(r"[a-z0-9آ-یءئٔ‌']+", title.lower())
    return sorted({w for w in words if w not in _STOP and len(w) > 2})


def is_similar(a, b):
    a, b = set(a), set(b)
    if not a or not b:
        return False
    inter = len(a & b)
    return inter >= 3 and inter / len(a | b) >= 0.5


# ====================== خوندن منبع‌ها ======================


def meta(page, prop):
    p = re.escape(prop)
    for q in ('"', "'"):
        for pat in (
            r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=%s([^%s]*)%s',
            r'<meta[^>]+content=%s([^%s]*)%s[^>]*(?:property|name)=["\']%s["\']',
        ):
            if pat.startswith(r'<meta[^>]+(?:'):
                rx = pat % (p, q, q, q)
            else:
                rx = pat % (q, q, q, p)
            m = re.search(rx, page, re.I)
            if m:
                return html.unescape(m.group(1)).strip()
    return ""


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def rss_image(it, desc_raw):
    for child in it.iter():
        tag = child.tag.split("}")[-1].lower()
        url = child.attrib.get("url", "")
        if tag in ("thumbnail",) and url:
            return url
        if tag == "content" and url and (child.attrib.get("medium") == "image"
                                         or child.attrib.get("type", "").startswith("image")
                                         or re.search(r"\.(jpe?g|png|webp)", url, re.I)):
            return url
        if tag == "enclosure" and url and (child.attrib.get("type", "").startswith("image")
                                           or re.search(r"\.(jpe?g|png|webp)", url, re.I)):
            return url
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc_raw or "", re.I)
    return m.group(1) if m else ""


def read_rss(src):
    root = ET.fromstring(http_get(src["url"]))
    items = []
    for it in root.iter("item"):
        title = clean_html(it.findtext("title") or "")
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        desc_raw = it.findtext("description") or ""
        items.append({
            "id": item_id(link),
            "source": src["name"],
            "title": title,
            "summary_raw": desc_raw,
            "link": link,
            "image": rss_image(it, desc_raw),
            "published": parse_date(it.findtext("pubDate")),
        })
    return items


_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){3,}$")


def read_html_listing(src, seen):
    page = http_get(src["url"]).decode("utf-8", "ignore")
    base = src["base"]
    links, order = {}, []
    for h in re.findall(r'href=["\']([^"\']+)["\']', page):
        if h.startswith("/"):
            h = base + h
        h = canonical(h)
        if not h.startswith(base + "/"):
            continue
        slug = h[len(base) + 1:]
        if "/" in slug or not _SLUG.match(slug) or h in links:
            continue
        links[h] = True
        order.append(h)
    items = []
    for link in order[:25]:
        iid = item_id(link)
        if iid in seen:
            continue
        if len(items) >= 6:       # برای هر اجرا حداکثر ۶ مقاله جدید رو باز می‌کنه
            break
        try:
            art = http_get(link, max_bytes=400000).decode("utf-8", "ignore")
        except Exception as e:
            print(f"  [{src['name']}] article fetch failed: {e}")
            continue
        title = meta(art, "og:title")
        title = re.sub(r"\s*[|–-]\s*RacingNews365.*$", "", title, flags=re.I)
        if not title:
            continue
        items.append({
            "id": iid,
            "source": src["name"],
            "title": clean_html(title),
            "summary_raw": meta(art, "og:description") or meta(art, "description"),
            "link": link,
            "image": meta(art, "og:image"),
            "published": parse_date(meta(art, "article:published_time")
                                    or meta(art, "og:updated_time")),
        })
    return items


def fetch_all(seen):
    out = []
    for src in SOURCES:
        try:
            if src["type"] == "rss":
                items = read_rss(src)
                req = src.get("require")
                if req:
                    items = [i for i in items if req in i["link"]]
            else:
                items = read_html_listing(src, seen)
            print(f"[{src['name']}] {len(items)} items")
            out.extend(items)
        except Exception as e:
            print(f"[{src['name']}] FAILED: {e}")
    return out


def og_image(link):
    try:
        page = http_get(link, max_bytes=400000).decode("utf-8", "ignore")
        return meta(page, "og:image")
    except Exception:
        return ""


# ====================== ترجمه ======================


def google_tr(text):
    if not text:
        return ""
    url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
        {"client": "gtx", "sl": "en", "tl": "fa", "dt": "t", "q": text})
    last_error = None
    for attempt in range(3):
        try:
            data = json.loads(http_get(url, timeout=30).decode("utf-8"))
            return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Google Translate موقتاً پاسخ نداد: {last_error}")


# واژه‌نامهٔ سبک برای یکدست شدن نام‌ها و اصطلاحات رایج F1 بعد از ترجمهٔ رایگان.
# ترجمهٔ ماشینی رایگان است؛ این جایگزینی‌ها فقط خروجی را یکدست‌تر می‌کنند.
F1_GLOSSARY = {
    "لوئیس همیلتون": "لوئیس همیلتون", "لوییس همیلتون": "لوئیس همیلتون",
    "مکس ورستاپن": "مکس فرستاپن", "ماکس ورستاپن": "مکس فرستاپن",
    "چارلز لکلرک": "شارل لکلرک",
    "لاندو نوریس": "لندو نوریس", "لندو نوریس": "لندو نوریس",
    "اسکار پیاستری": "اسکار پیاستری", "فرناندو آلونسو": "فرناندو آلونسو",
    "کارلوس ساینز": "کارلوس ساینز", "جورج راسل": "جورج راسل",
    "رد بول": "ردبول", "ردبول ریسینگ": "ردبول", "مک لارن": "مک‌لارن",
    "مک‌لارن": "مک‌لارن", "مرسدس": "مرسدس", "فراری": "فراری",
    "گرندپری": "گرندپری", "جایزه بزرگ": "گرندپری",
    "تمرین آزاد": "تمرین آزاد", "مرحله تعیین خط": "دور تعیین خط",
    "تعیین صلاحیت": "دور تعیین خط", "پیت استاپ": "پیت‌استاپ",
}


def polish_f1_translation(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    for wrong, right in F1_GLOSSARY.items():
        text = re.sub(re.escape(wrong), right, text, flags=re.I)
    return text


TRANSLATE_PROMPT = (
    "You are a professional Persian (Farsi) sports journalist who translates Formula 1 news "
    "for a popular Telegram channel.\n"
    "Write natural, fluent, idiomatic Persian news style (not word-for-word). Be faithful: never "
    "add, remove or change facts, numbers, names or quotes. Headline: short and punchy. "
    "Summary: at most 2-3 short sentences.\n"
    "Use the Persian spellings Persian F1 media use for names (e.g. لوئیس همیلتون، مکس فرستاپن، "
    "شارل لکلرک، لندو نوریس، جورج راسل، اسکار پیاستری، کیمی آنتونلی، فراری، مرسدس، مک‌لارن، ردبول). "
    "Common terms: pole position = پول‌پوزیشن, qualifying = کوالیفای, practice = تمرین آزاد, "
    "grid penalty = پنالتی گرید, safety car = سیفتی‌کار, pit stop = پیت‌استاپ, "
    "Grand Prix = گرندپری, constructors' championship = قهرمانی سازندگان, "
    "drivers' championship = قهرمانی رانندگان, power unit = پاور یونیت, "
    "stewards = کمیسرها. Keep acronyms like FIA, DRS, F1 in Latin letters.\n"
    'Return ONLY JSON: {"title": "...", "summary": "..."} (summary may be empty).\n\n'
    "Example input:\nTITLE: Hamilton hit with grid penalty for Brazilian GP\n"
    "SUMMARY: The Ferrari driver will start from the back after taking a new engine.\n"
    'Example output:\n{"title": "همیلتون در گرندپری برزیل پنالتی گرید گرفت", '
    '"summary": "راننده فراری پس از استفاده از موتور جدید از انتهای گرید استارت می‌زند."}\n\n')

LAST_PROVIDER = ""
_BAD_MODELS = set()      # مدل‌هایی که در این اجرا خطا دادن؛ دوباره امتحان نمی‌شن


def _parse_tr_json(text):
    obj = json.loads(text[text.index("{"): text.rindex("}") + 1])
    return obj["title"].strip(), (obj.get("summary") or "").strip()


def _gemini_body(model, prompt):
    cfg = {"temperature": 0.3}
    if not model.startswith("gemma"):          # مدل‌های Gemma حالت JSON رو پشتیبانی نمی‌کنن
        cfg["responseMimeType"] = "application/json"
    return json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": cfg}).encode()


def _gemini_call(model, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    req = urllib.request.Request(
        url, data=_gemini_body(model, prompt),
        headers={"x-goog-api-key": GEMINI_API_KEY, "content-type": "application/json"})
    with _opener().open(req, timeout=60) as r:
        resp = json.loads(r.read().decode())
    return resp["candidates"][0]["content"]["parts"][0]["text"]


def _http_error_text(e):
    try:
        body = e.read().decode("utf-8", "ignore")
        msg = json.loads(body).get("error", {}).get("message", body)
    except Exception:
        msg = ""
    return f"HTTP {e.code} {msg[:160]}".strip()


def _gemini_discover():
    # اگه اسم مدل‌ها عوض شده باشه، مدل‌های فعلی رو از خود گوگل می‌پرسه
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        headers={"x-goog-api-key": GEMINI_API_KEY})
    with _opener().open(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    names = []
    for m in data.get("models", []):
        n = m.get("name", "").replace("models/", "")
        if (re.search(r"flash|gemma", n)
                and "generateContent" in m.get("supportedGenerationMethods", [])
                and not re.search(r"image|tts|live|audio|embedding|thinking|exp|omni|robotics|"
                                  r"computer|vision|preview-\d", n)):
            names.append(n)
    names = sorted(names, reverse=True)[:6]
    print("  Gemini models found:", ", ".join(names) or "(none)")
    return names


def gemini_tr(title, summary):
    prompt = TRANSLATE_PROMPT + f"TITLE: {title}\nSUMMARY: {summary}"
    tried, last = [], None
    candidates = list(GEMINI_MODELS)
    for attempt in range(2):
        for model in candidates:
            if model in tried or model in _BAD_MODELS:
                continue
            tried.append(model)
            for retry in range(3):
                try:
                    t, sm = _parse_tr_json(_gemini_call(model, prompt))
                    return t, sm, model
                except urllib.error.HTTPError as e:
                    last = f"{model}: {_http_error_text(e)}"
                    print("  Gemini failed ->", last)
                    if e.code == 503 and retry < 2:      # شلوغی موقت: کمی صبر و تلاش دوباره
                        time.sleep(4 * (retry + 1))
                        continue
                    _BAD_MODELS.add(model)
                    break
                except Exception as e:
                    last = f"{model}: {e}"
                    print("  Gemini failed ->", last)
                    break
        if attempt == 0:
            try:
                candidates = _gemini_discover()
            except Exception as e:
                print("  Gemini model discovery failed:", e)
                break
    raise RuntimeError(last or "gemini failed")


LINGVA_HOSTS = ["lingva.ml", "lingva.garudalinux.org", "translate.plausibility.cloud"]


def lingva_tr(text):
    # فال‌بک رایگان دوم، وقتی مترجم گوگل به سرورهای گیت‌هاب 429 می‌ده
    if not text:
        return ""
    last = None
    for host in LINGVA_HOSTS:
        try:
            url = f"https://{host}/api/v1/en/fa/" + urllib.parse.quote(text, safe="")
            data = json.loads(http_get(url, timeout=20).decode("utf-8"))
            if data.get("translation"):
                return data["translation"].strip()
        except Exception as e:
            last = e
    raise RuntimeError(f"Lingva هم پاسخ نداد: {last}")


def translate(title, summary):
    global LAST_PROVIDER
    if GEMINI_API_KEY:
        try:
            t, sm, model = gemini_tr(title, summary)
            if t:
                LAST_PROVIDER = f"Gemini ({model})"
                return t, sm
        except Exception as e:
            print("Gemini translation failed, falling back to Google Translate:", e)
    try:
        res = polish_f1_translation(google_tr(title)), polish_f1_translation(google_tr(summary))
        LAST_PROVIDER = "Google Translate (رایگان، کیفیت پایین‌تر)"
        return res
    except Exception as e:
        print("Google Translate failed, trying Lingva:", e)
    res = polish_f1_translation(lingva_tr(title)), polish_f1_translation(lingva_tr(summary))
    LAST_PROVIDER = "Lingva (رایگان، کیفیت پایین‌تر)"
    return res


# ====================== ساخت پست ======================


def build_caption(fa_title, fa_summary, source, source_url=""):
    e = html.escape

    def make(summ):
        parts = [f"📰 <b>{e(fa_title)}</b>"]
        if summ:
            parts.append(f"<blockquote><b>{e(summ)}</b></blockquote>")
        if source_url:
            safe_url = html.escape(source_url, quote=True)
            parts.append(f'منبع: <a href="{safe_url}">{e(source)}</a>')
        else:
            parts.append(f"منبع: {e(source)}")
        parts.append(f'<a href="{FOOTER_URL}">⚪️ FORMULAONEESI ⚪️</a>')
        return "\n\n".join(parts)

    summ = fa_summary
    cap = make(summ)
    while len(cap) > 1024 and summ:
        summ = summ[: int(len(summ) * 0.85)].rsplit(" ", 1)[0].rstrip(" ،,.…") + "…"
        if len(summ) < 20:
            summ = ""
        cap = make(summ)
    if len(cap) > 1024:
        cap = make("")
    return cap[:1024]


def publish_post(caption, image):
    """انتشار مستقیم در کانال؛ در صورت شکست عکس، متن به‌تنهایی ارسال می‌شود."""
    if image:
        r = tg("sendPhoto", {"chat_id": CHANNEL, "photo": image, "caption": caption,
                             "parse_mode": "HTML"})
        if r.get("ok"):
            return r["result"].get("message_id")
        print("  sendPhoto failed, falling back to text:", r.get("description"))
    r = tg("sendMessage", {"chat_id": CHANNEL, "text": caption, "parse_mode": "HTML",
                           "disable_web_page_preview": "true"})
    if r.get("ok"):
        return r["result"].get("message_id")
    print("  channel publish failed:", r.get("description"))
    return None


# ====================== پیدا کردن خبر جدید و انتشار مستقیم ======================


def _mark_seen(st, seen, iid):
    if iid not in seen:
        seen.add(iid)
        st["seen"].append(iid)


def new_posts(st, since):
    """جدیدترین خبرهای بعد از زمان `since` رو انتخاب و منتشر می‌کنه (حداکثر POSTS_PER_SLOT تا)."""
    seen = set(st["seen"])
    now = datetime.now(timezone.utc)
    items = fetch_all(seen)

    fresh = []
    for it in items:
        if it["id"] in seen:
            continue
        pub = it["published"]
        if (pub and pub < since) or EXCLUDE_TITLE.search(it["title"]):
            _mark_seen(st, seen, it["id"])      # قدیمی یا نامرتبط: دیگه بررسی نشه
            continue
        fresh.append(it)

    fresh.sort(key=lambda i: i["published"] or now, reverse=True)    # جدیدترین اول

    chosen, failed, round_tokens = [], set(), []
    for it in fresh:
        if len(chosen) >= POSTS_PER_SLOT:
            break
        known = st["titles"] + round_tokens
        tk = tokens(it["title"])
        if tk and any(is_similar(tk, old) for old in known):
            print("  duplicate story, skipped:", it["title"])
            continue
        try:
            if it.get("language") == "fa" or it.get("source") == "F1Iran":
                # خبر F1Iran از ابتدا فارسی است؛ دوباره از مترجم عبور نمی‌کند.
                fa_title = clean_html(it["title"])
                fa_summary = make_summary(it["summary_raw"], 520)
            else:
                summary_en = make_summary(it["summary_raw"])
                if summary_en.lower().startswith(it["title"].lower()[:40]):
                    summary_en = ""
                fa_title, fa_summary = translate(it["title"], summary_en)
        except Exception as e:
            print("  preparing news failed, will retry later:", e)
            failed.add(it["id"])
            continue
        if not fa_title:
            failed.add(it["id"])
            continue
        fa_tokens = tokens(fa_title)
        if fa_tokens and any(is_similar(fa_tokens, old) for old in known):
            print("  translated duplicate story, skipped:", fa_title)
            continue
        chosen.append({"it": it, "fa_title": fa_title, "fa_summary": fa_summary,
                       "tk": tk, "fa_tokens": fa_tokens})
        round_tokens += [t for t in (tk, fa_tokens) if t]

    chosen_ids = {c["it"]["id"] for c in chosen}
    for it in fresh:            # بقیه خبرها (انتخاب‌نشده/تکراری) برای نوبت بعد نمی‌مونن
        if it["id"] not in chosen_ids and it["id"] not in failed:
            _mark_seen(st, seen, it["id"])

    published = 0
    for c in reversed(chosen):          # قدیمی‌تر اول، تا جدیدترین خبر آخر کانال باشه
        it = c["it"]
        image = it.get("image") or og_image(it["link"])
        caption = build_caption(c["fa_title"], c["fa_summary"], it["source"], it["link"])
        mid = publish_post(caption, image)
        if mid:
            published += 1
            _mark_seen(st, seen, it["id"])
            st["titles"] += [t for t in (c["tk"], c["fa_tokens"]) if t]
            print("  published:", it["title"], "(message", mid, ")")
            save_state(st)
            time.sleep(2)
    return published


# ====================== فرمان‌های روشن / خاموش ======================

HELP = ("/news_off ← خاموش کردن بخش خبر\n"
        "/news_on ← روشن کردن بخش خبر\n"
        "/news_status ← وضعیت فعلی")


def _parse_iso(x):
    try:
        d = datetime.fromisoformat(x)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _fmt_iran(dt):
    return dt.astimezone(IRAN).strftime("%m/%d %H:%M")


def status_text(st):
    on = st.get("enabled", True)
    lines = [f"وضعیت بخش خبر: {'🟢 روشن' if on else '🔴 خاموش'}"]
    last = _parse_iso(st.get("last_post", ""))
    if last:
        lines.append(f"آخرین انتشار: {_fmt_iran(last)} (به وقت ایران)")
    if on:
        if last:
            lines.append("انتشار بعدی: بعد از " + _fmt_iran(last + timedelta(hours=POST_INTERVAL_HOURS)))
        else:
            lines.append("انتشار بعدی: در اولین اجرا")
    lines.append(f"تنظیم: هر {POST_INTERVAL_HOURS:g} ساعت، {POSTS_PER_SLOT} خبر")
    return "\n".join(lines)


def _reply(chat_id, text):
    tg("sendMessage", {"chat_id": chat_id, "text": text})


def process_commands(st):
    if not ADMIN_CHAT_ID:
        return
    r = tg("getUpdates", {"offset": st["offset"], "timeout": 0,
                          "allowed_updates": json.dumps(["message"])}, timeout=20)
    if not r.get("ok"):
        print("getUpdates failed:", r.get("description"))
        return
    now = time.time()
    for upd in r.get("result", []):
        st["offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        if str(chat_id) != str(ADMIN_CHAT_ID):
            continue                                   # فقط ادمین
        if now - msg.get("date", now) > 12 * 3600:
            continue                                   # فرمان خیلی قدیمی
        words = (msg.get("text") or "").strip().split()
        cmd = words[0].split("@")[0].lower() if words else ""
        if cmd == "/news_off":
            st["enabled"] = False
            _reply(chat_id, "🔴 بخش خبر خاموش شد. تا /news_on نزنی خبری منتشر نمی‌شه.")
            print("news turned OFF by admin")
        elif cmd == "/news_on":
            st["enabled"] = True
            _reply(chat_id, "🟢 بخش خبر روشن شد. در اولین نوبت خبرهای جدید منتشر می‌شن.")
            print("news turned ON by admin")
        elif cmd == "/news_status":
            _reply(chat_id, status_text(st))
        elif cmd in ("/start", "/id", "/help"):
            _reply(chat_id, f"Chat ID: {chat_id}\n\n{HELP}")
    if not st.get("cmds_set"):
        cmds = [{"command": "news_on", "description": "روشن کردن بخش خبر"},
                {"command": "news_off", "description": "خاموش کردن بخش خبر"},
                {"command": "news_status", "description": "وضعیت بخش خبر"}]
        r = tg("setMyCommands", {"commands": json.dumps(cmds)})
        st["cmds_set"] = bool(r.get("ok"))


# ====================== اجرا ======================


def check_mode():
    st = load_state()
    items = fetch_all(set(st["seen"]))
    for it in items[:12]:
        print(f"- [{it['source']}] {it['title'][:80]} | image={'yes' if it['image'] else 'no'}"
              f" | {it['published']}")
    sample = next((i for i in items if i["source"] != "F1Iran"), None)
    if sample:
        try:
            t, sm = translate(sample["title"], make_summary(sample["summary_raw"]))
            print("Translation provider:", LAST_PROVIDER)
            print("EN:", sample["title"])
            print("FA:", t)
            print("FA summary:", sm)
        except Exception as e:
            print("Translation FAILED:", e)
    else:
        print("Translation test:", google_tr("Hamilton leads practice") or "(empty)")
    print(status_text(st))


def test_mode():
    caption = build_caption("این یک خبر آزمایشی است", "انتشار مستقیم ربات در کانال فعال است.",
                            "Test", FOOTER_URL)
    mid = publish_post(caption, "")
    print("test post published" if mid else "test post FAILED")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if not BOT_TOKEN:
        print("BOT_TOKEN باید در Secrets تنظیم شده باشد.")
        sys.exit(1)
    if mode == "check":
        return check_mode()
    if mode == "test":
        return test_mode()

    st = load_state()
    process_commands(st)
    now = datetime.now(timezone.utc)

    if not st.get("enabled", True):
        print("بخش خبر خاموش است (با /news_on روشن می‌شود).")
        save_state(st)
        return

    interval = timedelta(hours=POST_INTERVAL_HOURS)
    last = _parse_iso(st.get("last_post", ""))
    if mode != "now" and last and now - last < interval:
        print("هنوز نوبت انتشار نرسیده؛ بعدی بعد از", _fmt_iran(last + interval))
        save_state(st)
        return

    since = max(last or (now - interval), now - timedelta(hours=MAX_WINDOW_HOURS))
    count = new_posts(st, since)
    if count:
        st["last_post"] = datetime.now(timezone.utc).isoformat()
    print(f"Published {count} new posts.")
    save_state(st)


if __name__ == "__main__":
    main()
