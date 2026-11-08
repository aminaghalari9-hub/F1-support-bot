#!/usr/bin/env python3
# ربات تلگرام فرمول یک: جدول هفتگی + یادآوری شروع سشن‌ها + لینک پخش
# بدون نیاز به نصب هیچ کتابخونه‌ای (فقط پایتون)

import json
import os
import time
from datetime import datetime, timedelta, timezone

import urllib.error
import urllib.parse
import urllib.request

# ====================== تنظیمات (فقط این بخش رو عوض کن) ======================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")  # توی گیت‌هاب از Secrets خونده می‌شه
CHANNEL = os.environ.get("CHANNEL", "@formulaone_esi")

# اگه سرورت ایرانه و تلگرام فیلتره، پروکسی بذار. مثال: "socks5://127.0.0.1:1080"
# (برای socks باید: pip install requests[socks])
PROXY = None

TZ = timezone(timedelta(hours=3, minutes=30))  # ایران: ثابت +۳:۳۰ (ساعت تابستانی لغو شده)

# یادآوری‌ها: چند دقیقه قبل از شروع هر سشن پیام بده
REMINDERS_MIN = [1440, 60, 15]   # ۲۴ ساعت، ۱ ساعت، ۱۵ دقیقه

# پست جدول هفتگی: روز (۰=دوشنبه ... ۶=یکشنبه) و ساعت به وقت ایران
WEEKLY_DAY = 0
WEEKLY_HOUR = 10

# سشن‌هایی که یادآوری بگیرن
REMIND_SESSIONS = ["FirstPractice", "SecondPractice", "ThirdPractice",
                   "SprintQualifying", "Sprint", "Qualifying", "Race"]

# لینک پخش: برای کدوم سشن‌ها لینک گذاشته بشه
LINK_SESSIONS = ["Qualifying", "Sprint", "Race"]
DEFAULT_LINK = os.environ.get("STREAM_LINK", "")   # لینک پیش‌فرض پخش؛ خالی = بدون لینک
# لینک مخصوص هر مسابقه (شماره راند: لینک). مثال: {23: "https://t.me/..."}
LINKS_BY_ROUND = {}

# ---------------------------- متن پیام‌ها ----------------------------
# متغیرهای قابل استفاده: {gp} {session} {time} {day} {left} {link_line}
# وقتی متن آماده‌ات رو دادی، همین‌جا عوضش می‌کنیم.

REMINDER_TEXT = (
    "🏁 {session} {gp}\n"
    "⏰ {left} دیگه شروع میشه\n"
    "🕒 {day} ساعت {time} (به وقت ایران)"
    "{link_line}"
)

LINK_LINE = "\n📺 لینک پخش: {link}"

WEEKLY_HEADER = "⭐️ تقویم‌زمان‌بندی {gp_fa}\n\n"
WEEKLY_ROW = "🔘{session}:\n📆{jdate} ({gdate})\n⏰ساعت {time}\n\n"
WEEKLY_FOOTER = (
    "● زمان‌بندی این لیست ممکن است در ادامه دچار تغییر شود؛ "
    "در این صورت لیست آپدیت‌شده دوباره در کانال قرار می‌گیرد.\n"
    "⚪️ {gp_en} 🔜\n\n"
    "● FORMULAONEESI ⬅️"
)

# ---------------------------------------------------------------------------

SESSION_FA = {
    "FirstPractice": ("تمرین اول", "🔧"),
    "SecondPractice": ("تمرین دوم", "🔧"),
    "ThirdPractice": ("تمرین سوم", "🔧"),
    "SprintQualifying": ("کوالیفای اسپرینت", "⏱"),
    "Sprint": ("مسابقه اسپرینت", "⚡"),
    "Qualifying": ("کوالیفای", "⏱"),
    "Race": ("مسابقه اصلی", "🏆"),
}

DAYS_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]
MONTHS_FA = ["ژانویه", "فوریه", "مارس", "آوریل", "مه", "ژوئن",
             "ژوئیه", "اوت", "سپتامبر", "اکتبر", "نوامبر", "دسامبر"]

SESSION_EN = {
    "FirstPractice": "PRACTICE 1",
    "SecondPractice": "PRACTICE 2",
    "ThirdPractice": "PRACTICE 3",
    "SprintQualifying": "SPRINT QUALIFYING",
    "Sprint": "SPRINT",
    "Qualifying": "QUALIFYING",
    "Race": "GRAND PRIX FINAL RACE",
}

JALALI_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                 "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]

# نام فارسی و پرچم هر مسابقه (کلید = اسم مسابقه در API)
# اگه مسابقه‌ای اینجا نبود، همون اسم انگلیسی بدون پرچم میاد؛ خودت اضافه‌ش کن
GP_FA = {
    "Australian Grand Prix": "جایزه‌بزرگ استرالیا🇦🇺",
    "Chinese Grand Prix": "جایزه‌بزرگ چین🇨🇳",
    "Japanese Grand Prix": "جایزه‌بزرگ ژاپن🇯🇵",
    "Bahrain Grand Prix": "جایزه‌بزرگ بحرین🇧🇭",
    "Saudi Arabian Grand Prix": "جایزه‌بزرگ عربستان🇸🇦",
    "Miami Grand Prix": "جایزه‌بزرگ میامی🇺🇸",
    "Emilia Romagna Grand Prix": "جایزه‌بزرگ ایمولا🇮🇹",
    "Monaco Grand Prix": "جایزه‌بزرگ موناکو🇲🇨",
    "Canadian Grand Prix": "جایزه‌بزرگ کانادا🇨🇦",
    "Spanish Grand Prix": "جایزه‌بزرگ اسپانیا🇪🇸",
    "Barcelona Grand Prix": "جایزه‌بزرگ بارسلونا🇪🇸",
    "Austrian Grand Prix": "جایزه‌بزرگ اتریش🇦🇹",
    "British Grand Prix": "جایزه‌بزرگ بریتانیا🇬🇧",
    "Belgian Grand Prix": "جایزه‌بزرگ بلژیک🇧🇪",
    "Hungarian Grand Prix": "جایزه‌بزرگ مجارستان🇭🇺",
    "Dutch Grand Prix": "جایزه‌بزرگ هلند🇳🇱",
    "Italian Grand Prix": "جایزه‌بزرگ ایتالیا🇮🇹",
    "Azerbaijan Grand Prix": "جایزه‌بزرگ آذربایجان🇦🇿",
    "Singapore Grand Prix": "جایزه‌بزرگ سنگاپور🇸🇬",
    "United States Grand Prix": "جایزه‌بزرگ آمریکا🇺🇸",
    "Mexico City Grand Prix": "جایزه‌بزرگ مکزیک🇲🇽",
    "São Paulo Grand Prix": "جایزه‌بزرگ برزیل🇧🇷",
    "Las Vegas Grand Prix": "جایزه‌بزرگ لاس‌وگاس🇺🇸",
    "Qatar Grand Prix": "جایزه‌بزرگ قطر🇶🇦",
    "Abu Dhabi Grand Prix": "جایزه‌بزرگ ابوظبی🇦🇪",
}


def to_jalali(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100
            + (gy2 + 399) // 400 + gd + g_d_m[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd

API_URL = "https://api.jolpi.ca/ergast/f1/current.json"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent.json")

_cache = {"time": 0, "races": []}


def fa_digits(text):
    return str(text).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def load_sent():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_sent(sent):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(sent), f)


def _opener():
    if PROXY:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return urllib.request.build_opener()


def send(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": CHANNEL,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with _opener().open(urllib.request.Request(url, data=body), timeout=30) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        print("Telegram error:", e.read().decode(errors="ignore"))
        return False
    except Exception as e:
        print("Send failed:", e)
        return False


def parse_dt(date, t):
    t = (t or "00:00:00Z").replace("Z", "")
    return datetime.fromisoformat(f"{date}T{t}").replace(tzinfo=timezone.utc)


def get_races():
    # برنامه رو هر ۶ ساعت یک بار از API می‌گیره
    if time.time() - _cache["time"] < 6 * 3600 and _cache["races"]:
        return _cache["races"]
    try:
        req = urllib.request.Request(API_URL, headers={"User-Agent": "f1-bot"})
        with _opener().open(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        raw = data["MRData"]["RaceTable"]["Races"]
    except Exception as e:
        print("Schedule fetch failed:", e)
        return _cache["races"]

    races = []
    for r in raw:
        sessions = {}
        for key in ["FirstPractice", "SecondPractice", "ThirdPractice",
                    "SprintQualifying", "Sprint", "Qualifying"]:
            if key in r:
                sessions[key] = parse_dt(r[key]["date"], r[key].get("time"))
        sessions["Race"] = parse_dt(r["date"], r.get("time"))
        races.append({
            "round": int(r["round"]),
            "gp": r["raceName"],
            "sessions": sessions,
        })
    _cache["time"] = time.time()
    _cache["races"] = races
    return races


def fmt_day(dt):
    d = dt.astimezone(TZ)
    return f"{DAYS_FA[d.weekday()]} {fa_digits(d.day)} {MONTHS_FA[d.month - 1]}"


def fmt_time(dt):
    return fa_digits(dt.astimezone(TZ).strftime("%H:%M"))


def fmt_left(minutes):
    if minutes >= 60:
        h = round(minutes / 60)
        return f"{fa_digits(h)} ساعت"
    return f"{fa_digits(max(1, round(minutes)))} دقیقه"


def link_line(race, session_key):
    if session_key not in LINK_SESSIONS:
        return ""
    link = LINKS_BY_ROUND.get(race["round"], DEFAULT_LINK)
    return LINK_LINE.format(link=link) if link else ""


def check_reminders(now, sent):
    for race in get_races():
        for key, start in race["sessions"].items():
            if key not in REMIND_SESSIONS:
                continue
            diff = (start - now).total_seconds() / 60
            if diff < 0:
                continue
            # کوچک‌ترین بازه‌ای که الان توش هستیم
            for m in sorted(REMINDERS_MIN):
                if diff <= m:
                    sid = f"rem-{race['round']}-{key}-{m}"
                    if sid not in sent:
                        name, _ = SESSION_FA[key]
                        text = REMINDER_TEXT.format(
                            gp=race["gp"], session=name, time=fmt_time(start),
                            day=fmt_day(start), left=fmt_left(diff),
                            link_line=link_line(race, key),
                        )
                        if send(text):
                            # بازه‌های بزرگ‌تر رو هم ثبت کن تا تکراری نره
                            for bigger in REMINDERS_MIN:
                                if bigger >= m:
                                    sent.add(f"rem-{race['round']}-{key}-{bigger}")
                            save_sent(sent)
                    break


def build_weekly_text(race):
    gp_fa = GP_FA.get(race["gp"], race["gp"])
    text = WEEKLY_HEADER.format(gp_fa=gp_fa)
    ordered = sorted(race["sessions"].items(), key=lambda kv: kv[1])
    for key, start in ordered:
        if key not in SESSION_EN:
            continue
        d = start.astimezone(TZ)
        _, jm, jd = to_jalali(d.year, d.month, d.day)
        text += WEEKLY_ROW.format(
            session=SESSION_EN[key],
            jdate=f"{fa_digits(jd)} {JALALI_MONTHS[jm - 1]}",
            gdate=f"{fa_digits(d.day)} {MONTHS_FA[d.month - 1]}",
            time=fmt_time(start))
    text += WEEKLY_FOOTER.format(gp_en=race["gp"].upper())
    return text


def check_weekly(now, sent):
    local = now.astimezone(TZ)
    if local.weekday() != WEEKLY_DAY or local.hour < WEEKLY_HOUR:
        return
    for race in get_races():
        race_start = race["sessions"]["Race"]
        if now < race_start <= now + timedelta(days=7):
            sid = f"weekly-{race['round']}"
            if sid in sent:
                return
            if send(build_weekly_text(race)):
                sent.add(sid)
                save_sent(sent)
            return


def run_test():
    # تست: جدول مسابقه بعدی رو همین الان توی چنل می‌فرسته
    now = datetime.now(timezone.utc)
    for race in get_races():
        if race["sessions"]["Race"] > now:
            print("Sending test for:", race["gp"])
            ok = send(build_weekly_text(race))
            print("OK، پیام ارسال شد" if ok else "ارسال نشد؛ خطا بالاست")
            return
    print("مسابقه آینده‌ای پیدا نشد (یا دریافت برنامه ناموفق بود)")


def main():
    print("Bot started.")
    sent = load_sent()
    while True:
        now = datetime.now(timezone.utc)
        try:
            check_weekly(now, sent)
            check_reminders(now, sent)
        except Exception as e:
            print("Loop error:", e)
        time.sleep(30)


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "test":
        run_test()
    elif mode == "once":
        # یک بار اجرا و خروج (برای GitHub Actions)
        _sent = load_sent()
        _now = datetime.now(timezone.utc)
        check_weekly(_now, _sent)
        check_reminders(_now, _sent)
    else:
        main()
