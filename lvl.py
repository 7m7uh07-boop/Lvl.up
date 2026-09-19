import os
import io
import json
import time
import random
import asyncio
import logging
import tempfile
from threading import Thread, Lock, Event

import sys
import shutil
import importlib
import subprocess

# ==================== تثبيت المكتبات تلقائياً ====================
REQUIRED_PACKAGES = {
    "cv2": "opencv-python-headless",
    "numpy": "numpy",
    "requests": "requests",
    "PIL": "pillow",
    "flask": "flask",
    "telegram": "python-telegram-bot",
    "google.generativeai": "google-generativeai",
}
OPTIONAL_PACKAGES = {"pytesseract": "pytesseract"}


def _pip_install(package):
    base = [sys.executable, "-m", "pip", "install", "--quiet", package]
    try:
        subprocess.check_call(base)
    except subprocess.CalledProcessError:
        subprocess.check_call(base + ["--break-system-packages"])


def ensure_packages():
    for module, package in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(module)
        except ImportError:
            print(f"📦 تثبيت {package} ...")
            _pip_install(package)
    for module, package in OPTIONAL_PACKAGES.items():
        try:
            importlib.import_module(module)
        except ImportError:
            try:
                _pip_install(package)
            except Exception:
                print(f"⚠️ تعذر تثبيت {package} (اختياري)")
    if (
        shutil.which("tesseract") is None
        and hasattr(os, "geteuid")
        and os.geteuid() == 0
        and shutil.which("apt-get")
    ):
        try:
            subprocess.call(["apt-get", "install", "-y", "-qq", "tesseract-ocr"])
        except Exception:
            pass
    importlib.invalidate_caches()


ensure_packages()

import cv2
import numpy as np
import requests
from PIL import Image
from flask import Flask, send_file, send_from_directory, render_template_string, abort
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import google.generativeai as genai

try:
    import pytesseract
except ImportError:
    pytesseract = None

# ==================== 1. التهيئات ومتغيرات البيئة ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8932000604:AAEL6m8dPLxUMZUD9uq-q5fAQSrBqCyJbKw")
VMOS_AK = os.getenv("VMOS_AK", "ReLRr5zgaUsrSF6hydjDfw9ZwbreHgGf")
VMOS_SK = os.getenv("VMOS_SK", "pIQRQ7X5olMut4bdNTs4LccE")
VMOS_DEVICE_ID = os.getenv("VMOS_DEVICE_ID", "ATP64N6TCE70Q6T5")
VMOS_EXTRA_DEVICES = os.getenv("VMOS_EXTRA_DEVICES", "")
DEVICES_FILE = "devices.json"
FAILOVER_THRESHOLD = 3   # إخفاقات متتالية قبل التحويل للجهاز الاحتياطي
DEVICE_COOLDOWN = 300    # ثواني قبل إعادة استخدام جهاز تعطل
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6KFUi4C4LJJTcJ8opjmynJWeZjrVFxm_1G1FY1Su9NQ5w")

# جلب رابط السيرفر تلقائياً إن وُجد في Render أو استخدام القيمة الافتراضية
SERVER_URL = os.getenv("SERVER_URL", "http://192.168.0.110:5000")

VMOS_BASE = os.getenv("VMOS_BASE", "https://api.vmoscloud.com/v1")
AUTOCLICKER_NAME = os.getenv("AUTOCLICKER_NAME", "Auto Clicker")
GAME_NAME = os.getenv("GAME_NAME", "Free Fire")

STATS_FILE = "stats.json"
CALIB_FILE = "calibration.json"
LATEST_SCREENSHOT_PATH = "latest_screen.png"
TEMPLATES_DIR = "templates"
APK_DIR = "apks"

LOOP_INTERVAL = 3
IDLE_SHOT_INTERVAL = 5
MATCH_THRESHOLD = 0.80

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_MAX_CALLS_PER_HOUR = 20
UNKNOWN_BEFORE_LEARN = 3
LEARN_ENABLED = True

JOYSTICK = (0.15, 0.72)
FIRE_BTN = (0.86, 0.66)
LEVEL_REGION = (0.04, 0.05, 0.12, 0.13)

CATEGORIES = ("start", "close", "jump", "continue", "guest")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

GEMINI_READY = bool(GEMINI_API_KEY) and not GEMINI_API_KEY.startswith("YOUR_")
ai_model = None
if GEMINI_READY:
    genai.configure(api_key=GEMINI_API_KEY)
    ai_model = genai.GenerativeModel(GEMINI_MODEL)

app = Flask(__name__)
screenshot_lock = Lock()
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(APK_DIR, exist_ok=True)

# ==================== 2. الإحصائيات والمعايرة ====================
stats_lock = Lock()
stats = {
    "level": None,
    "start_level": None,
    "matches": 0,
    "state": "-",
    "running": False,
    "started_at": None,
    "last_update": None,
}


def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            stats.update({k: saved[k] for k in ("level", "start_level", "matches") if k in saved})
        except Exception as e:
            log.error("تعذر تحميل الإحصائيات: %s", e)


def save_stats():
    try:
        with stats_lock:
            data = dict(stats)
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.error("تعذر حفظ الإحصائيات: %s", e)


def update_level(level):
    if not isinstance(level, int) or level <= 0 or level > 100:
        return
    with stats_lock:
        cur = stats["level"]
        if cur is not None and (level < cur or level - cur > 5):
            return
        if stats["start_level"] is None:
            stats["start_level"] = level
        stats["level"] = level
        stats["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_stats()


def load_calibration():
    global JOYSTICK, FIRE_BTN, LEVEL_REGION
    if os.path.exists(CALIB_FILE):
        try:
            with open(CALIB_FILE, "r", encoding="utf-8") as f:
                c = json.load(f)
            if "joystick" in c:
                JOYSTICK = tuple(c["joystick"])
            if "fire" in c:
                FIRE_BTN = tuple(c["fire"])
            if "level_region" in c:
                LEVEL_REGION = tuple(c["level_region"])
        except Exception as e:
            log.error("تعذر تحميل المعايرة: %s", e)


def save_calibration():
    try:
        with open(CALIB_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"joystick": JOYSTICK, "fire": FIRE_BTN, "level_region": LEVEL_REGION}, f
            )
    except Exception as e:
        log.error("تعذر حفظ المعايرة: %s", e)


# ==================== 3. سيرفر الويب (متوافق مع Render) ====================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>بث شاشة VMOS Cloud المباشر</title>
    <style>
        body { background:#0f172a; color:#f8fafc; text-align:center; font-family:system-ui,sans-serif; margin:0; padding:20px; }
        h2 { color:#38bdf8; }
        .card { background:#1e293b; display:inline-block; padding:15px; border-radius:12px; }
        img { max-width:100%; height:auto; max-height:80vh; border:2px solid #38bdf8; border-radius:8px; }
    </style>
</head>
<body>
    <div class="card">
        <h2>🎮 بث الشاشة الحي</h2>
        <img id="live" src="/stream.png" alt="Live Stream">
    </div>
    <script>
        setInterval(function () {
            document.getElementById("live").src = "/stream.png?t=" + Date.now();
        }, 3000);
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/stream.png")
def get_stream():
    with screenshot_lock:
        if os.path.exists(LATEST_SCREENSHOT_PATH):
            with open(LATEST_SCREENSHOT_PATH, "rb") as f:
                data = f.read()
            resp = send_file(io.BytesIO(data), mimetype="image/png")
            resp.headers["Cache-Control"] = "no-store"
            return resp
    abort(404)


@app.route("/apk/<path:filename>")
def serve_apk(filename):
    return send_from_directory(APK_DIR, filename, as_attachment=True)


def run_web_server():
    # Render يمرر المنافذ عبر متغير البيئة PORT تلقائياً
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ==================== 4. إدارة الأجهزة + VMOS Cloud API ====================
devices_lock = Lock()
devices = []
current_idx = 0
apk_history = []
notify_chat_id = None
needs_recovery = Event()


def _new_device(dev_id, prepared=False):
    return {"id": dev_id, "failures": 0, "cooldown_until": 0.0, "prepared": prepared}


def save_devices():
    try:
        with devices_lock:
            data = {
                "devices": [d["id"] for d in devices],
                "current": current_idx,
                "prepared": [d["id"] for d in devices if d["prepared"]],
                "apk_history": list(apk_history),
                "notify_chat_id": notify_chat_id,
            }
        with open(DEVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.error("تعذر حفظ الأجهزة: %s", e)


def load_devices():
    global current_idx, notify_chat_id
    ids, prepared, cur = [], set(), 0
    if os.path.exists(DEVICES_FILE):
        try:
            with open(DEVICES_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            ids = list(saved.get("devices", []))
            prepared = set(saved.get("prepared", []))
            apk_history[:] = saved.get("apk_history", [])
            notify_chat_id = saved.get("notify_chat_id")
            cur = int(saved.get("current", 0))
        except Exception as e:
            log.error("تعذر تحميل الأجهزة: %s", e)
    for dev in [VMOS_DEVICE_ID] + [d.strip() for d in VMOS_EXTRA_DEVICES.split(",")]:
        if dev and dev not in ids:
            ids.append(dev)
    with devices_lock:
        devices[:] = [
            _new_device(i, prepared=(i in prepared or i == VMOS_DEVICE_ID)) for i in ids
        ]
        current_idx = cur if 0 <= cur < len(devices) else 0
    save_devices()


def current_device():
    with devices_lock:
        return devices[current_idx]["id"] if devices else VMOS_DEVICE_ID


def device_ids():
    with devices_lock:
        return [d["id"] for d in devices]


def set_notify_chat(chat_id):
    global notify_chat_id
    if notify_chat_id != chat_id:
        notify_chat_id = chat_id
        save_devices()


def notify(text):
    if not notify_chat_id or TELEGRAM_BOT_TOKEN.startswith("YOUR_"):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": notify_chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        log.error("تعذر إرسال الإشعار: %s", e)


def add_device(dev_id):
    with devices_lock:
        if any(d["id"] == dev_id for d in devices):
            return False
        devices.append(_new_device(dev_id, prepared=False))
    save_devices()
    return True


def remove_device(dev_id):
    global current_idx
    removed_current = False
    with devices_lock:
        ids = [d["id"] for d in devices]
        if len(devices) < 2 or dev_id not in ids:
            return False
        cur_id = ids[current_idx]
        devices.pop(ids.index(dev_id))
        if cur_id == dev_id:
            current_idx = 0
            removed_current = True
        else:
            current_idx = [d["id"] for d in devices].index(cur_id)
    save_devices()
    if removed_current:
        needs_recovery.set()
    return True


def switch_device(dev_id):
    global current_idx
    with devices_lock:
        for i, d in enumerate(devices):
            if d["id"] == dev_id:
                current_idx = i
                d["failures"] = 0
                d["cooldown_until"] = 0.0
                break
        else:
            return False
    save_devices()
    needs_recovery.set()
    return True


def failover(reason=""):
    global current_idx
    with devices_lock:
        if not devices:
            return False
        old = devices[current_idx]
        if len(devices) < 2:
            old["failures"] = 0
            return False
        old["cooldown_until"] = time.time() + DEVICE_COOLDOWN
        old_id = old["id"]
        new_id = None
        for step in range(1, len(devices)):
            i = (current_idx + step) % len(devices)
            if time.time() >= devices[i]["cooldown_until"]:
                current_idx = i
                devices[i]["failures"] = 0
                new_id = devices[i]["id"]
                break
        if new_id is None:
            old["failures"] = 0
            return False
    save_devices()
    needs_recovery.set()
    log.warning("تحويل من %s إلى %s (%s)", old_id, new_id, reason)
    notify(f"⚠️ تعطل الجهاز {old_id} ({reason}).\nتم التحويل تلقائياً إلى الجهاز {new_id}.")
    return True


def record_result(ok, reason=""):
    with devices_lock:
        if not devices:
            return
        d = devices[current_idx]
        if ok:
            d["failures"] = 0
            return
        d["failures"] += 1
        tripped = d["failures"] >= FAILOVER_THRESHOLD
    if tripped:
        failover(reason)


def vmos_headers(json_body=False):
    headers = {
        "Authorization": f"Bearer {VMOS_AK}",
        "X-Secret-Key": VMOS_SK
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def vmos_url(path, device_id=None):
    return f"{VMOS_BASE}/device/{device_id or current_device()}/{path}"


def get_vmos_screenshot():
    try:
        res = requests.get(vmos_url("screenshot"), headers=vmos_headers(), timeout=15)
        if res.status_code == 200 and res.content:
            with screenshot_lock:
                fd, tmp = tempfile.mkstemp(suffix=".png")
                with os.fdopen(fd, "wb") as f:
                    f.write(res.content)
                os.replace(tmp, LATEST_SCREENSHOT_PATH)
            record_result(True)
            return True
        log.error("Screenshot failed: %s %s", res.status_code, res.text[:200])
        record_result(False, f"HTTP {res.status_code}")
    except Exception as e:
        log.error("خطأ لقطة الشاشة: %s", e)
        record_result(False, "لا استجابة")
    return False


def install_apk_from_url(apk_url, device_id=None, remember=True):
    try:
        res = requests.post(
            vmos_url("install", device_id),
            json={"download_url": apk_url},
            headers=vmos_headers(True),
            timeout=30,
        )
        if res.status_code != 200:
            log.error("Install failed: %s %s", res.status_code, res.text[:200])
            return None
        if remember and apk_url not in apk_history:
            apk_history.append(apk_url)
            save_devices()
        return res.json()
    except Exception as e:
        log.error("خطأ تثبيت APK: %s", e)
        return None


def install_apk_everywhere(apk_url):
    results = {}
    for dev in device_ids():
        results[dev] = install_apk_from_url(apk_url, device_id=dev) is not None
    return results if any(results.values()) else None


def prepare_device(dev_id):
    all_ok = True
    for url in list(apk_history):
        if install_apk_from_url(url, device_id=dev_id, remember=False) is None:
            all_ok = False
    if all_ok:
        with devices_lock:
            for d in devices:
                if d["id"] == dev_id:
                    d["prepared"] = True
        save_devices()
    return all_ok


def recover_on_current_device():
    dev = current_device()
    with devices_lock:
        prepared = next((d["prepared"] for d in devices if d["id"] == dev), True)
    if not prepared:
        notify(f"📦 جاري تثبيت التطبيقات على الجهاز {dev}...")
        prepare_device(dev)
        time.sleep(20)
    if GEMINI_READY and agent_lock.acquire(blocking=False):
        try:
            agent_stop.clear()
            ok, msg = run_agent(
                f'Open the game "{GAME_NAME}" and reach its main lobby. If it is not installed yet or is '
                'still installing, use "wait". Close any popup on the way. Answer "done" when the lobby is visible.',
                max_steps=25,
            )
        finally:
            agent_lock.release()
        notify(("✅ " if ok else "⚠️ ") + f"فتح اللعبة على {dev}: {msg}")
    else:
        notify(f"ℹ️ الجهاز {dev} هو النشط الآن. افتح اللعبة يدوياً إن لم تفتح تلقائياً.")


def _post(path, payload):
    try:
        res = requests.post(
            vmos_url(path), json=payload, headers=vmos_headers(True), timeout=10
        )
        ok = res.status_code == 200
    except Exception as e:
        log.error("خطأ %s: %s", path, e)
        ok = False
    record_result(ok, f"فشل {path}")
    return ok


def send_click_to_vmos(x, y):
    return _post("touch", {"type": "click", "x": int(x), "y": int(y)})


def send_swipe_to_vmos(x1, y1, x2, y2, duration_ms=1500):
    return _post(
        "touch",
        {"type": "swipe", "x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2),
         "duration": int(duration_ms)},
    )


def send_key_to_vmos(key):
    return _post("key", {"key": key})


# ==================== 5. Gemini ====================
gemini_lock = Lock()
gemini_calls = []


def gemini_allowed():
    if not GEMINI_READY:
        return False
    now = time.time()
    with gemini_lock:
        gemini_calls[:] = [t for t in gemini_calls if now - t < 3600]
        if len(gemini_calls) >= GEMINI_MAX_CALLS_PER_HOUR:
            return False
        gemini_calls.append(now)
        return True


def gemini_json(prompt, image):
    resp = ai_model.generate_content(
        [prompt, image],
        generation_config=genai.GenerationConfig(response_mime_type="application/json"),
    )
    text = resp.text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def load_screen_pil():
    with screenshot_lock:
        if not os.path.exists(LATEST_SCREENSHOT_PATH):
            return None
        with open(LATEST_SCREENSHOT_PATH, "rb") as f:
            data = f.read()
    return Image.open(io.BytesIO(data)).convert("RGB")


LEARN_PROMPT = """This is a screenshot of the Free Fire mobile game. Find these UI elements if clearly visible
and reply with JSON only:
{"elements":[{"label":"start|close|jump|continue|guest|level|joystick|fire","box_2d":[ymin,xmin,ymax,xmax]}]}
- start: the main Start / battle button in the lobby.
- close: an X / close icon of a popup, ad or announcement (the X icon itself only, one entry per X).
- jump: the jump / parachute button while in the plane.
- continue: continue / OK / confirm / back-to-lobby button after a match or elimination.
- guest: the guest login button on the login screen.
- level: the number that shows the player's account level in the lobby.
- joystick: the movement joystick. fire: the fire button (only during a match).
box_2d uses a 0-1000 scale. Return an empty list if nothing matches."""


def learn_from_screen():
    global JOYSTICK, FIRE_BTN, LEVEL_REGION
    img = load_screen_pil()
    if img is None:
        return [], []
    w, h = img.size
    gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    data = gemini_json(LEARN_PROMPT, img)

    saved, calibrated = [], []
    stamp = int(time.time())
    for n, el in enumerate(data.get("elements", []) or []):
        if not isinstance(el, dict):
            continue
        label = str(el.get("label", "")).lower()
        box = el.get("box_2d")
        if not (isinstance(box, list) and len(box) == 4):
            continue
        try:
            ymin, xmin, ymax, xmax = [float(v) for v in box]
        except (TypeError, ValueError):
            continue
        if xmax <= xmin or ymax <= ymin:
            continue

        if label == "level":
            LEVEL_REGION = (xmin / 1000, ymin / 1000, xmax / 1000, ymax / 1000)
            calibrated.append("level")
        elif label == "joystick":
            JOYSTICK = ((xmin + xmax) / 2000, (ymin + ymax) / 2000)
            calibrated.append("joystick")
        elif label == "fire":
            FIRE_BTN = ((xmin + xmax) / 2000, (ymin + ymax) / 2000)
            calibrated.append("fire")
        elif label in CATEGORIES:
            x1 = max(0, int(xmin / 1000 * w) - 4)
            y1 = max(0, int(ymin / 1000 * h) - 4)
            x2 = min(w, int(xmax / 1000 * w) + 4)
            y2 = min(h, int(ymax / 1000 * h) + 4)
            if (x2 - x1) < 12 or (y2 - y1) < 12:
                continue
            if find_template(gray, label, 0.9):
                continue
            img.crop((x1, y1, x2, y2)).save(
                os.path.join(TEMPLATES_DIR, f"{label}_auto_{stamp}_{n}.png")
            )
            saved.append(label)

    if saved:
        load_templates()
    if calibrated:
        save_calibration()
    return saved, calibrated


AGENT_PROMPT = """You control an Android cloud phone by looking at its screenshot, one action at a time.
GOAL: {goal}
Actions done so far:
{history}
Reply with JSON only:
{{"action":"click|swipe|key|wait|done|fail","x":n,"y":n,"x2":n,"y2":n,"key":"home|back","reason":"short"}}
Coordinates use a 0-1000 scale (x horizontal, y vertical). "swipe" goes from (x,y) to (x2,y2).
Answer "done" only when the goal is really achieved, "fail" if it is impossible."""

agent_lock = Lock()
agent_stop = Event()


def norm_to_px(x, y, size):
    try:
        return int(float(x) / 1000 * size[0]), int(float(y) / 1000 * size[1])
    except (TypeError, ValueError):
        return None


def run_agent(goal, max_steps=30):
    history = []
    for step in range(max_steps):
        if agent_stop.is_set():
            return False, "تم الإيقاف"
        if not get_vmos_screenshot():
            time.sleep(2)
            continue
        img = load_screen_pil()
        if img is None:
            continue
        prompt = AGENT_PROMPT.format(goal=goal, history="\n".join(history[-8:]) or "(none)")
        try:
            act = gemini_json(prompt, img)
        except Exception as e:
            log.error("خطأ Gemini: %s", e)
            time.sleep(3)
            continue

        action = str(act.get("action", "")).lower()
        reason = str(act.get("reason", ""))[:80]
        log.info("Agent %d: %s %s", step + 1, action, reason)

        if action == "done":
            return True, reason or "تم"
        if action == "fail":
            return False, reason or "تعذر"
        if action == "click":
            p = norm_to_px(act.get("x"), act.get("y"), img.size)
            if p:
                send_click_to_vmos(*p)
        elif action == "swipe":
            p1 = norm_to_px(act.get("x"), act.get("y"), img.size)
            p2 = norm_to_px(act.get("x2"), act.get("y2"), img.size)
            if p1 and p2:
                send_swipe_to_vmos(p1[0], p1[1], p2[0], p2[1], 800)
        elif action == "key":
            key = str(act.get("key", "")).lower()
            if key in ("home", "back"):
                send_key_to_vmos(key)
        history.append(f"{step + 1}. {action} - {reason}")
        time.sleep(2 if action != "wait" else 4)
    return False, "انتهت الخطوات المسموحة"


def setup_autoclicker():
    goal1 = (
        f'Open the app named "{AUTOCLICKER_NAME}" (press HOME first if needed and look for its icon). '
        "Grant every permission it asks for: enable its Accessibility service in Android settings "
        "(Accessibility > installed/downloaded apps > this app > switch ON > Allow) and allow "
        "'display over other apps'. Finish when the app's floating control panel or bubble is visible."
    )
    goal2 = (
        f'Open the game "{GAME_NAME}" and use "wait" until its main lobby with the big Start/battle button '
        "is visible. Then use the auto clicker's floating panel to add a single click target, drag that "
        "target exactly on top of the Start button, and press the clicker's play/start control so it begins "
        'clicking. Answer "done" once the target sits on the Start button and the clicker is running.'
    )
    ok, msg = run_agent(goal1)
    if not ok:
        return False, f"المرحلة 1 (الصلاحيات): {msg}"
    ok, msg = run_agent(goal2)
    if not ok:
        return False, f"المرحلة 2 (وضع النقطة): {msg}"
    return True, msg


# ==================== 6. التعرف المحلي بـ OpenCV ====================
templates = {c: [] for c in CATEGORIES}
templates_lock = Lock()


def load_templates():
    new = {c: [] for c in CATEGORIES}
    for fn in sorted(os.listdir(TEMPLATES_DIR)):
        name = fn.lower()
        if not name.endswith((".png", ".jpg", ".jpeg")):
            continue
        cat = name.split("_")[0].split(".")[0]
        if cat not in new:
            continue
        img = cv2.imread(os.path.join(TEMPLATES_DIR, fn), cv2.IMREAD_GRAYSCALE)
        if img is not None and img.size > 0:
            new[cat].append(img)
    with templates_lock:
        for c in CATEGORIES:
            templates[c] = new[c]
    log.info("القوالب: %s", {c: len(v) for c, v in new.items()})


def load_screen_gray():
    with screenshot_lock:
        if not os.path.exists(LATEST_SCREENSHOT_PATH):
            return None
        with open(LATEST_SCREENSHOT_PATH, "rb") as f:
            data = f.read()
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)


def find_template(screen, category, threshold=MATCH_THRESHOLD):
    with templates_lock:
        tpls = list(templates.get(category, []))
    best, center = 0.0, None
    sh, sw = screen.shape[:2]
    for tpl in tpls:
        th, tw = tpl.shape[:2]
        if th > sh or tw > sw:
            continue
        res = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, maxloc = cv2.minMaxLoc(res)
        if maxv >= threshold and maxv > best:
            best = maxv
            center = (maxloc[0] + tw // 2, maxloc[1] + th // 2)
    return center


def read_level(gray):
    if pytesseract is None:
        return None
    try:
        h, w = gray.shape[:2]
        x1, y1, x2, y2 = LEVEL_REGION
        crop = gray[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]
        if crop.size == 0:
            return None
        crop = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        _, th = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        for img in (th, cv2.bitwise_not(th)):
            txt = pytesseract.image_to_string(
                img, config="--psm 7 -c tessedit_char_whitelist=0123456789"
            )
            digits = "".join(ch for ch in txt if ch.isdigit())
            if digits and 1 <= int(digits) <= 100:
                return int(digits)
    except Exception as e:
        log.error("خطأ OCR: %s", e)
    return None


# ==================== 7. حلقة اللعب ====================
stop_event = Event()
game_thread = None


def jitter_click(x, y):
    return send_click_to_vmos(x + random.randint(-3, 3), y + random.randint(-3, 3))


def play_step(size):
    h, w = size
    jx, jy = JOYSTICK[0] * w, JOYSTICK[1] * h
    send_swipe_to_vmos(
        jx, jy, jx + random.randint(-70, 70), jy + random.randint(-110, -40),
        random.randint(1500, 3000),
    )
    if random.random() < 0.4:
        send_click_to_vmos(FIRE_BTN[0] * w, FIRE_BTN[1] * h)


def set_state(state):
    with stats_lock:
        stats["state"] = state


def game_loop():
    log.info("بدأت حلقة اللعب")
    with stats_lock:
        stats["running"] = True
        stats["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    prev = None
    unknown = 0
    in_match = ("in_plane", "in_match")

    while not stop_event.is_set():
        wait = LOOP_INTERVAL
        try:
            if needs_recovery.is_set():
                needs_recovery.clear()
                prev, unknown = None, 0
                set_state("recovery")
                recover_on_current_device()
                continue
            if not get_vmos_screenshot():
                stop_event.wait(5)
                continue
            gray = load_screen_gray()
            if gray is None:
                stop_event.wait(LOOP_INTERVAL)
                continue

            found = None
            for cat in ("close", "continue", "guest", "start", "jump"):
                pt = find_template(gray, cat)
                if pt:
                    found = (cat, pt)
                    break

            if found:
                unknown = 0
                cat, pt = found
                if cat == "close":
                    jitter_click(*pt)
                    set_state("popup")
                    wait = 1.5
                elif cat in ("continue", "guest"):
                    jitter_click(*pt)
                    set_state("results" if cat == "continue" else "login")
                    wait = 3
                elif cat == "start":
                    lvl = read_level(gray)
                    if lvl:
                        update_level(lvl)
                    jitter_click(*pt)
                    set_state("lobby")
                    wait = 8
                elif cat == "jump":
                    if prev not in in_match:
                        with stats_lock:
                            stats["matches"] += 1
                        save_stats()
                    jitter_click(*pt)
                    set_state("in_plane")
                    wait = 2
                prev = {"jump": "in_plane", "start": "lobby"}.get(cat, cat)
            elif prev in in_match:
                set_state("in_match")
                play_step(gray.shape[:2])
                prev = "in_match"
            else:
                set_state("unknown")
                unknown += 1
                if LEARN_ENABLED and unknown >= UNKNOWN_BEFORE_LEARN and gemini_allowed():
                    log.info("شاشة مجهولة: طلب تعلم من Gemini")
                    saved, calibrated = learn_from_screen()
                    log.info("تعلّم: %s | معايرة: %s", saved, calibrated)
                    unknown = 0
                    wait = 1
        except Exception as e:
            log.error("خطأ في حلقة اللعب: %s", e)
        stop_event.wait(wait)

    with stats_lock:
        stats["running"] = False
        stats["state"] = "-"
    log.info("توقفت حلقة اللعب")


def start_game():
    global game_thread
    if game_thread and game_thread.is_alive():
        return False
    stop_event.clear()
    game_thread = Thread(target=game_loop, daemon=True)
    game_thread.start()
    return True


def idle_screenshot_loop():
    while True:
        if not stats["running"] and not agent_lock.locked():
            get_vmos_screenshot()
        time.sleep(IDLE_SHOT_INTERVAL)


# ==================== 8. أوامر التلغرام ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_notify_chat(update.effective_chat.id)
    await update.message.reply_text(
        "👋 بوت Free Fire على VMOS Cloud\n\n"
        "• /setup ← Gemini يقص صور الأزرار من الشاشة الحالية ويضبط الإعدادات\n"
        "• /autoclick ← Gemini يفعّل تطبيق الأوتو كليكر ويضع النقطة على زر البدء\n"
        "• /installapk <رابط أو اسم ملف> ← تثبيت APK (الملفات من مجلد apks)\n"
        "• UID:PASS ← بدء اللعب التلقائي\n"
        "• /level ← المستوى | /stop ← إيقاف | /stream ← البث\n"
        "• /learn on|off ← التعلم الاحتياطي التلقائي\n"
        "• /devices ← الأجهزة | /adddevice <معرّف> ← إضافة جهاز احتياطي\n"
        "• /removedevice <معرّف> | /switch <معرّف> ← تحويل يدوي\n"
        "• /snap ← لقطة شاشة | /templates ← عدد القوالب | /cleartemplates ← حذف القوالب التلقائية\n"
        "• يمكنك أيضاً إرسال صورة زر مع كابشن: start / close / jump / continue / guest"
    )


async def stream_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🌐 رابط البث المباشر:\n{SERVER_URL}")


async def snap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await asyncio.to_thread(get_vmos_screenshot):
        await update.message.reply_text("❌ تعذر التقاط الشاشة.")
        return
    with screenshot_lock:
        with open(LATEST_SCREENSHOT_PATH, "rb") as f:
            data = f.read()
    await update.message.reply_document(document=io.BytesIO(data), filename="screen.png")


async def setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GEMINI_READY:
        await update.message.reply_text("⚠️ ضع GEMINI_API_KEY أولاً.")
        return
    if not gemini_allowed():
        await update.message.reply_text("⏳ وصلت للحد الأقصى لطلبات Gemini هذه الساعة.")
        return
    if not await asyncio.to_thread(get_vmos_screenshot):
        await update.message.reply_text("❌ تعذر التقاط الشاشة.")
        return
    try:
        saved, calibrated = await asyncio.to_thread(learn_from_screen)
    except Exception as e:
        log.error("setup: %s", e)
        await update.message.reply_text("❌ فشل تحليل Gemini، راجع السجل.")
        return
    if not saved and not calibrated:
        await update.message.reply_text(
            "ℹ️ لم أجد عناصر جديدة في هذه الشاشة. افتح شاشة اللوبي أو نافذة فيها X وأعد /setup."
        )
        return
    await update.message.reply_text(
        f"✅ قوالب جديدة: {', '.join(saved) or '-'}\n🎯 معايرة: {', '.join(calibrated) or '-'}"
    )


async def autoclick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GEMINI_READY:
        await update.message.reply_text("⚠️ ضع GEMINI_API_KEY أولاً.")
        return
    if stats["running"]:
        await update.message.reply_text("⚠️ أوقف اللعب أولاً بـ /stop ثم أعد المحاولة.")
        return
    if not agent_lock.acquire(blocking=False):
        await update.message.reply_text("ℹ️ عملية إعداد تعمل بالفعل.")
        return
    agent_stop.clear()
    await update.message.reply_text("🤖 Gemini يعمل على إعداد الأوتو كليكر، قد يستغرق دقائق...")
    try:
        ok, msg = await asyncio.to_thread(setup_autoclicker)
    finally:
        agent_lock.release()
    await update.message.reply_text(("✅ " if ok else "❌ ") + msg)


async def learn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LEARN_ENABLED
    if context.args and context.args[0].lower() in ("on", "off"):
        LEARN_ENABLED = context.args[0].lower() == "on"
    await update.message.reply_text(f"🧠 التعلم الاحتياطي: {'مفعّل' if LEARN_ENABLED else 'متوقف'}")


async def templates_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with templates_lock:
        counts = {c: len(v) for c, v in templates.items()}
    await update.message.reply_text(
        "🧩 القوالب:\n" + "\n".join(f"• {c}: {n}" for c, n in counts.items())
    )


async def cleartemplates_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    removed = 0
    for fn in os.listdir(TEMPLATES_DIR):
        if "_auto_" in fn:
            try:
                os.remove(os.path.join(TEMPLATES_DIR, fn))
                removed += 1
            except OSError:
                pass
    await asyncio.to_thread(load_templates)
    await update.message.reply_text(f"🗑️ حُذف {removed} قالب تلقائي.")


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    caption = (msg.caption or "").strip().lower()
    cat = caption.split()[0] if caption else ""
    if cat not in CATEGORIES:
        await msg.reply_text("⚠️ أضف كابشن: " + " / ".join(CATEGORIES))
        return
    tg_file = await (msg.document.get_file() if msg.document else msg.photo[-1].get_file())
    await tg_file.download_to_drive(os.path.join(TEMPLATES_DIR, f"{cat}_{int(time.time())}.png"))
    await asyncio.to_thread(load_templates)
    await msg.reply_text(f"✅ حُفظ قالب «{cat}».")


async def installapk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        files = [f for f in os.listdir(APK_DIR) if f.lower().endswith(".apk")]
        listing = "\n".join(f"• {f}" for f in files) or "(لا توجد ملفات)"
        await update.message.reply_text(f"الاستخدام: /installapk <رابط أو اسم ملف>\n\n{listing}")
        return
    arg = context.args[0]
    if arg.startswith(("http://", "https://")):
        apk_url = arg
    else:
        name = os.path.basename(arg)
        if not os.path.exists(os.path.join(APK_DIR, name)):
            await update.message.reply_text("❌ الملف غير موجود في مجلد apks.")
            return
        apk_url = f"{SERVER_URL}/apk/{name}"
    await update.message.reply_text("📥 جاري إرسال أمر التثبيت...")
    res = await asyncio.to_thread(install_apk_everywhere, apk_url)
    await update.message.reply_text(
        "✅ بدأ التنزيل والتثبيت." if res is not None else "❌ فشل أمر التثبيت، راجع السجل."
    )


async def devices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_notify_chat(update.effective_chat.id)
    now = time.time()
    lines = []
    with devices_lock:
        for i, d in enumerate(devices):
            mark = "▶️" if i == current_idx else "⏸️"
            health = "🔴 معطل مؤقتاً" if d["cooldown_until"] > now else "🟢"
            prep = "" if d["prepared"] else " (لم يُجهَّز بعد)"
            lines.append(f"{mark} {d['id']} {health}{prep}")
    await update.message.reply_text("📱 الأجهزة:\n" + "\n".join(lines))


async def adddevice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_notify_chat(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("الاستخدام: /adddevice <معرّف الجهاز>")
        return
    dev = context.args[0].strip()
    if not add_device(dev):
        await update.message.reply_text("ℹ️ الجهاز موجود بالفعل.")
        return
    await update.message.reply_text(f"✅ أُضيف {dev} كجهاز احتياطي. جاري تثبيت التطبيقات عليه...")
    ok = await asyncio.to_thread(prepare_device, dev)
    await update.message.reply_text(
        "📦 اكتملت أوامر التثبيت." if ok else "⚠️ فشل بعض أوامر التثبيت، راجع السجل."
    )


async def removedevice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام: /removedevice <معرّف الجهاز>")
        return
    if remove_device(context.args[0].strip()):
        await update.message.reply_text("🗑️ تم حذف الجهاز.")
    else:
        await update.message.reply_text("❌ تعذر الحذف (غير موجود أو هو الجهاز الوحيد).")


async def switch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام: /switch <معرّف الجهاز>")
        return
    if switch_device(context.args[0].strip()):
        await update.message.reply_text("🔀 تم التحويل للجهاز المطلوب.")
    else:
        await update.message.reply_text("❌ الجهاز غير موجود في القائمة.")


async def level_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with stats_lock:
        s = dict(stats)
    if s["level"] is None:
        extra = "" if pytesseract else "\n(قراءة المستوى تحتاج pytesseract + tesseract-ocr)"
        await update.message.reply_text("ℹ️ لم يُقرأ المستوى بعد." + extra)
        return
    gained = s["level"] - (s["start_level"] or s["level"])
    await update.message.reply_text(
        f"⭐ المستوى الحالي: {s['level']}\n"
        f"📈 عند البداية: {s['start_level']}\n"
        f"➕ الزيادة: {gained}\n"
        f"🎮 المباريات: {s['matches']}\n"
        f"🔄 الحالة: {'يعمل' if s['running'] else 'متوقف'} ({s['state']})\n"
        f"🕒 آخر تحديث: {s['last_update']}\n"
        f"📱 الجهاز: {current_device()}"
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stop_event.set()
    agent_stop.set()
    await update.message.reply_text("⏹️ تم الإيقاف.")


async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    set_notify_chat(update.effective_chat.id)

    if text.startswith(("http://", "https://")):
        await update.message.reply_text("📥 جاري إرسال أمر التثبيت لكل الأجهزة...")
        res = await asyncio.to_thread(install_apk_everywhere, text)
        await update.message.reply_text(
            "✅ بدأ التنزيل والتثبيت." if res is not None else "❌ فشل أمر التثبيت، راجع السجل."
        )
    elif ":" in text:
        uid, password = text.split(":", 1)
        if not uid.strip() or not password.strip():
            await update.message.reply_text("⚠️ الصيغة غير صحيحة. استخدم UID:PASS")
            return
        with templates_lock:
            missing = [c for c in ("start", "close") if not templates[c]]
        if missing and not (LEARN_ENABLED and GEMINI_READY):
            await update.message.reply_text(
                "⚠️ ينقص قوالب: " + ", ".join(missing) + "\nاستخدم /setup أولاً."
            )
            return
        if start_game():
            await update.message.reply_text("🎮 بدأ اللعب التلقائي. تابع المستوى بـ /level")
        else:
            await update.message.reply_text("ℹ️ اللعب التلقائي يعمل بالفعل.")
    else:
        await update.message.reply_text("⚠️ أرسل رابط APK، أو UID:PASS، أو /start للتعليمات.")


# ==================== 9. التشغيل الرئيسي ====================
def main():
    load_stats()
    load_devices()
    load_calibration()
    load_templates()
    
    # تشغيل سيرفر الويب في خلفية مستقلة (Thread)
    Thread(target=run_web_server, daemon=True).start()
    Thread(target=idle_screenshot_loop, daemon=True).start()

    bot_app = (
        ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()
    )
    handlers = {
        "start": start_command,
        "stream": stream_command,
        "snap": snap_command,
        "setup": setup_command,
        "autoclick": autoclick_command,
        "learn": learn_command,
        "templates": templates_command,
        "cleartemplates": cleartemplates_command,
        "installapk": installapk_command,
        "level": level_command,
        "stop": stop_command,
        "devices": devices_command,
        "adddevice": adddevice_command,
        "removedevice": removedevice_command,
        "switch": switch_command,
    }
    for name, fn in handlers.items():
        bot_app.add_handler(CommandHandler(name, fn))
    bot_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    log.info("🚀 تم تشغيل الخادم وبوت التلغرام بنجاح على Render...")
    bot_app.run_polling()


if __name__ == "__main__":
    main()
