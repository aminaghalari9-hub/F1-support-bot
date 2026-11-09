#!/usr/bin/env python3
# ربات خبر فرمول یک
# ۱) خبر رو از چند سایت می‌گیره  ۲) به فارسی ترجمه می‌کنه
# ۳) پیش‌نویس (عکس + متن) رو با دکمه «تایید / رد» برای ادمین می‌فرسته
# ۴) بعد از «تایید» همون پست رو توی کانال منتشر می‌کنه
# فقط با کتابخونه‌های خود پایتون کار می‌کنه (بدون pip و API پولی).
#
# حالت‌های اجرا:
#   python f1_news.py          اجرای عادی (برای GitHub Actions)
#   python f1_news.py check    فقط چک می‌کنه سایت‌ها خونده می‌شن (چیزی نمی‌فرسته)
#   python f1_news.py test     یه پیش‌نویس تستی برای ادمین می‌فرسته

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
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")      # آیدی عددی خودت
CHANNEL = os.environ.get("CHANNEL", "@formulaone_esi")
PROXY = os.environ.get("PROXY") or None

FOOTER_URL = "https://t.me/formulaone_esi"

# منبع‌ها. type=rss  یعنی فید RSS ، type=html یعنی خوندن صفحه لیست خبرها
SOURCES = [
    {"name": "Autosport", "type": "rss",
     "url": "https://www.autosport.com/rss/feed/f1", "require": "/f1/"},
    {"name": "Motorsport.com", "type": "rss",
     "url": "https://www.motorsport.com/rss/f1/news/", "require": "/f1/"},
    {"name": "RacingNews365", "type": "html",
     "url": "https://racingnews365.com/f1-news", "base": "https://racingnews365.com"},
]

MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "6"))      # خبرهای قدیمی‌تر نادیده گرفته می‌شن
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "3"))  # حداکثر پیش‌نویس در هر اجرا
# بعد از ارسال پیش‌نویس، چند ثانیه منتظر زدن دکمه می‌مونه (۰ = منتظر نمی‌مونه).
# دکمه‌هایی که بعداً زده بشن توی اجرای بعدی (حداکثر ~۵ دقیقه بعد) پردازش می‌شن.
APPROVAL_WAIT_SECONDS = int(os.environ.get("APPROVAL_WAIT_SECONDS", "120"))
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
    st = {"seen": [], "titles": [], "offset": 0, "done": []}
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
            "that be has have will could would says say over into out up new".split())


def tokens(title):
    words = re.findall(r"[a-z0-9']+", title.lower())
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


def translate(title, summary):
    """ترجمهٔ رایگان؛ بدون نیاز به کلید API یا سرویس پولی."""
    return polish_f1_translation(google_tr(title)), polish_f1_translation(google_tr(summary))


# ====================== ساخت پست ======================


def build_caption(fa_title, fa_summary, source):
    e = html.escape

    def make(summ):
        parts = [f"📰 <b>{e(fa_title)}</b>"]
        if summ:
            parts.append(f"<blockquote><b>{e(summ)}</b></blockquote>")
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
    # طول عنوان و قالب ثابت باید زیر محدودیت کپشن تلگرام بماند.
    if len(cap) > 1024:
        cap = make("")
    return cap[:1024]


DRAFT_MARKUP = json.dumps({"inline_keyboard": [[
    {"text": "✅ تایید", "callback_data": "ok"},
    {"text": "❌ رد", "callback_data": "no"},
]]})


def send_draft(caption, image):
    if image:
        r = tg("sendPhoto", {"chat_id": ADMIN_CHAT_ID, "photo": image, "caption": caption,
                             "parse_mode": "HTML", "reply_markup": DRAFT_MARKUP})
        if r.get("ok"):
            return r["result"]["message_id"]
        print("  sendPhoto failed, sending text only:", r.get("description"))
    r = tg("sendMessage", {"chat_id": ADMIN_CHAT_ID, "text": caption, "parse_mode": "HTML",
                           "disable_web_page_preview": "true", "reply_markup": DRAFT_MARKUP})
    if r.get("ok"):
        return r["result"]["message_id"]
    print("  sendMessage failed:", r.get("description"))
    return None


# ====================== پیدا کردن خبر جدید و ارسال پیش‌نویس ======================


def new_drafts(st):
    seen = set(st["seen"])
    now = datetime.now(timezone.utc)
    items = fetch_all(seen)

    fresh = []
    for it in items:
        if it["id"] in seen:
            continue
        pub = it["published"]
        if pub and now - pub > timedelta(hours=MAX_AGE_HOURS):
            seen.add(it["id"])          # قدیمی: دیگه بررسی نشه
            st["seen"].append(it["id"])
            continue
        if EXCLUDE_TITLE.search(it["title"]):
            seen.add(it["id"])
            st["seen"].append(it["id"])
            continue
        fresh.append(it)

    fresh.sort(key=lambda i: i["published"] or now)      # قدیمی‌تر اول
    sent = 0
    for it in fresh:
        if sent >= MAX_NEW_PER_RUN:
            break
        tk = tokens(it["title"])
        if any(is_similar(tk, old) for old in st["titles"]):
            print("  duplicate story, skipped:", it["title"])
            seen.add(it["id"])
            st["seen"].append(it["id"])
            continue

        summary_en = make_summary(it["summary_raw"])
        if summary_en.lower().startswith(it["title"].lower()[:40]):
            summary_en = ""
        try:
            fa_title, fa_summary = translate(it["title"], summary_en)
        except Exception as e:
            print("  translation failed, will retry next run:", e)
            continue
        if not fa_title:
            continue

        image = it["image"] or og_image(it["link"])
        caption = build_caption(fa_title, fa_summary, it["source"])
        mid = send_draft(caption, image)
        if mid:
            sent += 1
            seen.add(it["id"])
            st["seen"].append(it["id"])
            st["titles"].append(tk)
            print("  draft sent:", it["title"])
            save_state(st)
    return sent


# ====================== دکمه‌های تایید / رد ======================


def _status_markup(text):
    return json.dumps({"inline_keyboard": [[{"text": text, "callback_data": "noop"}]]})


def _done_key(chat_id, message_id):
    return f"{chat_id}:{message_id}"


def handle_update(upd, st):
    """True یعنی update با موفقیت رسیدگی شد؛ False یعنی باید دوباره امتحان شود."""
    msg = upd.get("message")
    if msg and str(msg.get("chat", {}).get("id")) == str(ADMIN_CHAT_ID):
        if (msg.get("text") or "").strip().lower() in ("/start", "/id"):
            tg("sendMessage", {"chat_id": msg["chat"]["id"],
                               "text": f"Chat ID: {msg['chat']['id']}"})
        return True

    cb = upd.get("callback_query")
    if not cb:
        return True
    cb_id = cb.get("id")
    if str(cb.get("from", {}).get("id")) != str(ADMIN_CHAT_ID):
        tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "دسترسی ندارید"})
        return True
    m = cb.get("message")
    if not m:
        tg("answerCallbackQuery", {"callback_query_id": cb_id})
        return True

    mid = m.get("message_id")
    chat_id = m.get("chat", {}).get("id")
    data = cb.get("data")
    key = _done_key(chat_id, mid)
    # سازگاری با state قدیمی که فقط message_id را ذخیره می‌کرد.
    if key in st["done"] or mid in st["done"]:
        tg("answerCallbackQuery", {"callback_query_id": cb_id,
                                    "text": "این خبر قبلاً تعیین تکلیف شده است"})
        return True

    if data == "noop":
        tg("answerCallbackQuery", {"callback_query_id": cb_id})
        return True

    if data == "ok":
        # در صورت خطای موقت، update را مصرف نمی‌کنیم تا اجرای بعدی دوباره امتحان کند.
        r = tg("copyMessage", {"chat_id": CHANNEL, "from_chat_id": chat_id,
                                "message_id": mid})
        if not r.get("ok"):
            err = str(r.get("description") or "خطای نامشخص")[:180]
            tg("answerCallbackQuery", {"callback_query_id": cb_id,
                                       "text": f"انتشار انجام نشد؛ دوباره تلاش می‌کنم. {err}",
                                       "show_alert": "true"})
            print("  publish failed; update will be retried:", err)
            return False

        # ثبت فوری نتیجه برای کاهش احتمال انتشار تکراری در اجرای بعدی.
        st["done"].append(key)
        save_state(st)
        tg("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": mid,
                                      "reply_markup": _status_markup("✅ منتشر شد")})
        tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "منتشر شد ✅"})
        print("  published message", mid)
        return True

    if data == "no":
        st["done"].append(key)
        save_state(st)
        tg("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": mid,
                                      "reply_markup": _status_markup("❌ رد شد")})
        tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "رد شد"})
        return True

    tg("answerCallbackQuery", {"callback_query_id": cb_id})
    return True


def process_updates(st, timeout=0):
    r = tg("getUpdates", {"offset": st["offset"], "timeout": timeout,
                          "allowed_updates": json.dumps(["callback_query", "message"])},
           timeout=timeout + 20)
    if not r.get("ok"):
        print("getUpdates failed:", r.get("description"))
        time.sleep(3)
        return 0

    n = 0
    for upd in r.get("result", []):
        try:
            handled = handle_update(upd, st)
        except Exception as e:
            print("update error; will retry:", e)
            handled = False
        if not handled:
            # تلگرام updateها را تا جلو رفتن offset نگه می‌دارد.
            # توقف اینجا مانع رد شدن از روی درخواست ناموفق می‌شود.
            break
        st["offset"] = upd["update_id"] + 1
        n += 1
        save_state(st)
    return n


# ====================== اجرا ======================


def check_mode():
    st = load_state()
    items = fetch_all(set(st["seen"]))
    for it in items[:12]:
        print(f"- [{it['source']}] {it['title'][:80]} | image={'yes' if it['image'] else 'no'}"
              f" | {it['published']}")
    print("Translation test:", google_tr("Hamilton leads practice") or "(empty)")


def test_mode():
    caption = build_caption("این یک خبر تستی است", "اگر این پیام را می‌بینی، ربات درست کار می‌کند.",
                            "Test")
    mid = send_draft(caption, "")
    print("test draft sent" if mid else "test draft FAILED")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        print("BOT_TOKEN و ADMIN_CHAT_ID باید تنظیم بشن.")
        sys.exit(1)
    if mode == "check":
        return check_mode()
    if mode == "test":
        test_mode()
        # دکمه‌ها رو هم برای تست پردازش می‌کنیم
    st = load_state()
    process_updates(st)                 # دکمه‌هایی که بین دو اجرا زده شدن
    if mode == "run":
        new_drafts(st)
    end = time.time() + APPROVAL_WAIT_SECONDS
    while time.time() < end:
        process_updates(st, timeout=int(max(1, min(25, end - time.time()))))
    process_updates(st)
    save_state(st)


if __name__ == "__main__":
    main()
