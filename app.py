from flask import Flask, render_template, request, redirect, session, send_from_directory, jsonify
import json, os, math, calendar
from datetime import datetime, timedelta, time
from google.oauth2 import service_account
from googleapiclient.discovery import build
from pywebpush import webpush, WebPushException
from pymongo import MongoClient
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "supersecretkey"
ADMIN_CODE = os.getenv("ADMIN_CODE", "admin1234")

app.permanent_session_lifetime = timedelta(days=3650)

with open("driver.json", "r") as f:
    DRIVERS = json.load(f)

sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
if not sa_json:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")

creds = service_account.Credentials.from_service_account_info(
    json.loads(sa_json),
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
sheets = build("sheets", "v4", credentials=creds)

# ---------- VAPID ----------
VAPID_PUBLIC_KEY  = os.getenv("VAPID_PUBLIC_KEY",  "BOuqrSKgWKp_oxCo3B21vfHo2_-zCD-MbkEDUljwkLE01U4bt_UU1Oah_gpgpbSoE-3QntQYPo1WxcbU8iVhX5A")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "h_QIorXoPcAXH_3nTHYTBSSJXvtLsr2-zZvc0MFZ_lw")
VAPID_EMAIL       = os.getenv("VAPID_EMAIL", "mailto:admin@brajwasitravels.com")

# ---------- MongoDB ----------
MONGO_URI     = os.getenv("MONGO_URI", "")
_mongo_client = None
_mongo_col    = None

def get_col():
    """Return MongoDB collection, connecting once and reusing. None if unavailable."""
    global _mongo_client, _mongo_col
    if _mongo_col is not None:
        return _mongo_col
    if not MONGO_URI:
        print("⚠️  MONGO_URI not set — using file fallback for subscriptions")
        return None
    try:
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _mongo_client.admin.command("ping")
        _mongo_col = _mongo_client["brajwasi"]["subscriptions"]
        print("✅ MongoDB connected")
        return _mongo_col
    except Exception as e:
        print(f"❌ MongoDB connection failed: {e}")
        return None

# ---------- Subscription CRUD (MongoDB primary, file fallback) ----------
SUBS_FILE = "subscriptions.json"


# ---------- Entry page photo / watermark config ----------
ENTRY_PHOTO_DIR = os.path.join("static", "uploads")
ENTRY_PHOTO_SETTINGS_FILE = "entry_photo_settings.json"
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}


def allowed_image_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def load_entry_photo_settings():
    """Return admin-controlled entry photo settings."""
    default = {"mode": "hide", "url": "", "filename": ""}
    if os.path.exists(ENTRY_PHOTO_SETTINGS_FILE):
        try:
            with open(ENTRY_PHOTO_SETTINGS_FILE, "r") as f:
                data = json.load(f)
            default.update({k: data.get(k, default[k]) for k in default})
        except Exception as e:
            print(f"❌ entry photo settings load error: {e}")
    filename = default.get("filename") or ""
    if filename:
        photo_path = os.path.join(ENTRY_PHOTO_DIR, filename)
        if os.path.exists(photo_path):
            default["url"] = f"/static/uploads/{filename}"
            default["version"] = int(os.path.getmtime(photo_path))
        else:
            default["url"] = ""
            default["version"] = 0
    else:
        default["version"] = 0
    if default.get("mode") not in ["hide", "watermark", "bottom"]:
        default["mode"] = "hide"
    return default


def save_entry_photo_settings(settings):
    with open(ENTRY_PHOTO_SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


def save_uploaded_entry_photo(file_storage, mode):
    """Save admin-uploaded image and return updated settings."""
    os.makedirs(ENTRY_PHOTO_DIR, exist_ok=True)
    current = load_entry_photo_settings()

    # If mode is hide, keep the image saved but hide it on driver page.
    if mode == "hide":
        current["mode"] = "hide"
        save_entry_photo_settings(current)
        return current

    # New upload is optional if an old image already exists.
    if file_storage and file_storage.filename:
        if not allowed_image_file(file_storage.filename):
            raise ValueError("Only PNG, JPG, JPEG, WEBP or GIF images are allowed")

        # Delete old uploaded entry photos made by this feature.
        for old in os.listdir(ENTRY_PHOTO_DIR):
            if old.startswith("entry_photo."):
                try:
                    os.remove(os.path.join(ENTRY_PHOTO_DIR, old))
                except Exception:
                    pass

        ext = secure_filename(file_storage.filename).rsplit(".", 1)[1].lower()
        filename = f"entry_photo.{ext}"
        file_storage.save(os.path.join(ENTRY_PHOTO_DIR, filename))
        current["filename"] = filename
        current["url"] = f"/static/uploads/{filename}"

    if not current.get("filename"):
        raise ValueError("Please upload an image first")

    current["mode"] = mode
    save_entry_photo_settings(current)
    return current

def _file_load():
    if os.path.exists(SUBS_FILE):
        with open(SUBS_FILE) as f:
            return json.load(f)
    return {}

def _file_save(subs):
    with open(SUBS_FILE, "w") as f:
        json.dump(subs, f, indent=2)

def load_subs():
    col = get_col()
    if col is not None:
        try:
            return {d["_id"]: d["sub"] for d in col.find()}
        except Exception as e:
            print(f"❌ load_subs MongoDB error: {e}")
    return _file_load()

def save_sub(car_key, sub_info):
    col = get_col()
    if col is not None:
        try:
            col.update_one(
                {"_id": car_key},
                {"$set": {"sub": sub_info, "updated_at": datetime.utcnow()}},
                upsert=True
            )
            print(f"✅ MongoDB: saved subscription for {car_key}")
            return
        except Exception as e:
            print(f"❌ save_sub MongoDB error: {e}")
    subs = _file_load()
    subs[car_key] = sub_info
    _file_save(subs)
    print(f"✅ File: saved subscription for {car_key}")

def delete_sub(car_key):
    col = get_col()
    if col is not None:
        try:
            col.delete_one({"_id": car_key})
            print(f"🗑️  MongoDB: deleted subscription for {car_key}")
            return
        except Exception as e:
            print(f"❌ delete_sub MongoDB error: {e}")
    subs = _file_load()
    subs.pop(car_key, None)
    _file_save(subs)

# ---------- Push ----------
def send_push(sub_info, message, title="Brajwasi Travels"):
    """Send a single web push notification. Raises WebPushException on failure."""
    webpush(
        subscription_info=sub_info,
        data=json.dumps({"title": title, "body": message, "url": "/entry"}),
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims={"sub": VAPID_EMAIL}
    )

# ---------- Helpers ----------
def today_date():
    return datetime.now().date()

def parse_time(t):
    return datetime.strptime(t, "%H:%M").time()

def hours_between(start, end):
    d1 = datetime.combine(today_date(), start)
    d2 = datetime.combine(today_date(), end)
    if d2 < d1:
        d2 += timedelta(days=1)
    return (d2 - d1).total_seconds() / 3600

def calculate_ot(start, end):
    hrs = hours_between(start, end)
    extra = hrs - 12
    if extra <= 0:
        return 0
    full_hours = int(extra)
    fraction   = extra - full_hours
    if fraction > 0.5:
        return full_hours + 1
    elif full_hours == 0:
        return 0
    else:
        return full_hours

def get_remarks(start, end, date):
    night_start = start < time(5, 0)
    night_end   = end >= time(22, 0)
    sunday      = date.weekday() == 6
    parts = []
    if night_start and night_end:
        parts.append("Night/Night")
    elif night_start or night_end:
        parts.append("Night")
    if sunday:
        parts.append("Sunday")
    return "/".join(parts)

def ordinal_suffix(n):
    if 11 <= n <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")

# ---------- PWA ----------
@app.route('/manifest.json')
def manifest():
    return send_from_directory(os.getcwd(), 'manifest.json')

@app.route('/service-worker.js')
def sw():
    return send_from_directory(os.getcwd(), 'service-worker.js')

# ---------- Login ----------
@app.route("/", methods=["GET", "POST"])
def login():
    if "car" in session:
        return redirect("/entry")
    msg = ""
    if request.method == "POST":
        code = request.form["code"]
        for car, info in DRIVERS.items():
            if info["code"] == code:
                session.permanent = True
                session["car"] = car
                return redirect("/entry")
        msg = "Invalid code"
    return render_template("login.html", msg=msg)

# ---------- Session safety ----------
def current_driver_or_redirect():
    """Prevents 500 errors when old browser cookies contain a car not in driver.json."""
    car = session.get("car")
    if not car or car not in DRIVERS:
        session.clear()
        return None, None, redirect("/")
    return car, DRIVERS[car], None

@app.route("/hard-reset")
def hard_reset():
    """Clear old Flask session cookie and send user back to login.
    Use this when normal Chrome has stale login but Incognito works.
    """
    session.clear()
    resp = redirect("/")
    cookie_name = app.config.get("SESSION_COOKIE_NAME", "session")
    resp.delete_cookie(cookie_name)
    resp.set_cookie(cookie_name, "", expires=0, max_age=0)
    return resp


# ---------- Entry ----------
@app.route("/entry", methods=["GET", "POST"])
def entry():
    car, info, bad = current_driver_or_redirect()
    if bad:
        return bad
    msg  = ""
    cls  = "success"

    if request.method == "POST":
        try:
            opening = int(request.form["opening"])
            closing = int(request.form["closing"])
            start   = parse_time(request.form["start"])
            end     = parse_time(request.form["end"])

            entry_date_str = request.form.get("entry_date", "")
            entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d").date() if entry_date_str else today_date()

            remarks  = get_remarks(start, end, entry_date)
            ot       = calculate_ot(start, end)
            total_km = closing - opening

            row = entry_date.day + 7
            rng = f"{info['sheet']}!C{row}:I{row}"

            values = [[
                opening, closing, total_km,
                start.strftime("%I:%M %p"),
                end.strftime("%I:%M %p"),
                ot, remarks
            ]]

            sheets.spreadsheets().values().update(
                spreadsheetId=info["file_id"],
                range=rng,
                valueInputOption="USER_ENTERED",
                body={"values": values}
            ).execute()

            msg = f"Saved successfully ✅ | Total KMs: {total_km} km"

        except Exception as e:
            msg = str(e)
            cls = "error"

    return render_template("entry.html", car=car, msg=msg, cls=cls,
                           today=today_date().isoformat(),
                           vapid_public_key=VAPID_PUBLIC_KEY,
                           entry_photo=load_entry_photo_settings())

# ---------- Check entry ----------
@app.route("/check-entry", methods=["POST"])
def check_entry():
    car, info, bad = current_driver_or_redirect()
    if bad:
        return jsonify({"filled": False, "error": "Not logged in"})
    try:
        entry_date_str = request.json.get("entry_date", "")
        entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
        row = entry_date.day + 7
        rng = f"{info['sheet']}!C{row}"
        result = sheets.spreadsheets().values().get(
            spreadsheetId=info["file_id"], range=rng).execute()
        values = result.get("values", [])
        filled = bool(values and values[0] and str(values[0][0]).strip() != "")
        return jsonify({"filled": filled})
    except Exception as e:
        return jsonify({"filled": False, "error": str(e)})

# ---------- Last closing KM ----------
@app.route("/get-last-closing", methods=["POST"])
def get_last_closing():
    car, info, bad = current_driver_or_redirect()
    if bad:
        return jsonify({"closing": None, "error": "Not logged in"})
    try:
        entry_date_str = request.json.get("entry_date", "")
        entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
        for i in range(1, 8):
            prev_date = entry_date - timedelta(days=i)
            prev_row  = prev_date.day + 7
            rng       = f"{info['sheet']}!D{prev_row}"
            result    = sheets.spreadsheets().values().get(
                spreadsheetId=info["file_id"], range=rng).execute()
            values = result.get("values", [])
            if values and values[0] and str(values[0][0]).strip() != "":
                return jsonify({"closing": values[0][0]})
        return jsonify({"closing": None})
    except Exception as e:
        return jsonify({"closing": None, "error": str(e)})

# ---------- Groq voice transcription ----------
@app.route("/transcribe", methods=["POST"])
def transcribe():
    car, info, bad = current_driver_or_redirect()
    if bad:
        return jsonify({"error": "Not logged in"}), 401
    try:
        import requests as req_lib
        audio_file = request.files.get("audio")
        if not audio_file:
            return jsonify({"error": "No audio"}), 400

        groq_key = os.getenv("GROQ_API_KEY")
        if not groq_key:
            return jsonify({"error": "GROQ_API_KEY not set"}), 500

        resp = req_lib.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {groq_key}"},
            files={"file": (audio_file.filename or "audio.webm",
                            audio_file.read(),
                            audio_file.content_type or "audio/webm")},
            data={"model": "whisper-large-v3-turbo",
                  "language": "hi",
                  "response_format": "text"}
        )
        if resp.status_code != 200:
            return jsonify({"error": resp.text}), 500

        raw_text = resp.text.strip()

        parse_resp = req_lib.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}",
                     "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": """You are a number and time parser for a vehicle daily log app used by Indian drivers.
Input is spoken Hindi, English, Hinglish, or mixed language, transcribed by speech-to-text. Extract ONLY the number or time being said.

═══ STEP 1: DETECT THE SPEAKING STYLE ═══
STYLE A — DIGIT-BY-DIGIT (each word is a single digit 0-9, spoken in sequence with NO multiplier words):
  "aath nau ek do" → digits 8,9,1,2 → concatenate → 8912
  "do teen char paanch" → 2,3,4,5 → 2345
  "one two three four" → 1234
  "नौ नौ एक दो" → 9912
  RULE: If you hear single-digit words (ek/do/teen/char/paanch/chhe/saat/aath/nau, or 1-9, or zero/sunya) in sequence with NO sau/hazaar/lakh connecting them, concatenate digits directly. Do NOT do place-value math.

STYLE B — WORD-VALUE (place-value words with multipliers: sau/hazaar/lakh/crore):
  "char hazaar nau sau nabbe" → char(4)*1000 + nau(9)*100 + nabbe(90) = 4990
  "battees hazaar paanch sau" → 32*1000 + 5*100 = 32500
  RULE: Only use place-value math when multiplier words (sau, hazaar, lakh, laakh, crore) are present.

DEFAULT: For short sequences of 3-6 simple digit words with NO sau/hazaar/lakh present, ALWAYS use DIGIT-BY-DIGIT — this is how Indian drivers read odometer readings aloud.

═══ HINDI NUMBER WORDS ═══
Digits 0-9: sunya/zero=0, ek=1, do=2, teen=3, char=4, paanch=5, chhe/chheh=6, saat=7, aath=8, nau=9
Compound (word-value mode only): das=10, gyarah=11, barah=12, terah=13, chaudah=14, pandrah=15, solah=16, satrah=17, atharah=18, unnis=19, bees=20, tees=30, chaalees=40, pachaas=50, saath=60, sattar=70, assi=80, nabbe=90, pachpan=55, sattavan=57
Multipliers: sau=100, hazaar/hazar=1000, lakh/laakh=100000 (SAME WORD), crore=10000000

CRITICAL: lakh = laakh = 100000 always. "nau lakh" = "nau laakh" = 900000

═══ WORD-VALUE EXAMPLES ═══
  "char hazaar nau sau nabbe" → 4990
  "barah hazaar teen sau pachaas" → 12350
  "nau lakh" / "nau laakh" → 900000
  "paanch lakh bees hazaar" → 520000
  "saat lakh" → 700000
  "bees hazaar" → 20000
  "teen sau" → 300
  "1 lakh" → 100000
  "ek crore" → 10000000

═══ DIGIT-BY-DIGIT EXAMPLES ═══
  "aath nau ek do" → 8912
  "do teen char paanch" → 2345
  "nau nau nau nau nau" → 99999
  "saat saat nau nau" → 7799
  "do do do" → 222
  "one two three four five" → 12345

═══ TIME PARSING RULES ═══
Return 24-hour HH:MM format.
Context: subah=morning AM, dopahar=afternoon 12-4PM, shaam=evening 4-8PM, raat=night PM
Special: saade X = X:30, paune X = (X-1):45, sawa X = X:15, dhaai = 2:30

Time examples:
  "paanch bajke pandrah minute" → 05:15
  "shaam ke saat baje" → 19:00
  "raat ke das baje" → 22:00
  "subah chhe bajkar bis minute" → 06:20
  "dopahar ke do baje" → 14:00
  "saade aath subah" → 08:30
  "paune nau raat" → 20:45
  "sawa chhe subah" → 06:15
  "5:15 PM" → 17:15
  "6 AM" → 06:00
  "7 baje" → 07:00

═══ OUTPUT RULES ═══
- Return ONLY: an integer number OR HH:MM time OR the word INVALID
- No explanation, no units, no extra words
- Ignore filler words: um, uh, matlab, yaani, woh, toh, haan, theek hai
- Pure digit input (e.g. "8912") passes through unchanged
- Try hard before giving up — prefer a best-guess number over INVALID when audio is partially clear"""},
                    {"role": "user", "content": raw_text}
                ]
            }
        )
        if parse_resp.status_code != 200:
            return jsonify({"raw": raw_text, "parsed": None})

        parsed = parse_resp.json()["choices"][0]["message"]["content"].strip()
        if parsed == "INVALID":
            parsed = None

        return jsonify({"raw": raw_text, "parsed": parsed})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------- Push subscription ----------
@app.route("/subscribe-push", methods=["POST"])
def subscribe_push():
    car, info, bad = current_driver_or_redirect()
    if bad:
        return jsonify({"error": "Not logged in"}), 401
    sub = request.json
    if not sub or "endpoint" not in sub:
        print(f"❌ subscribe-push: invalid data from {car}")
        return jsonify({"error": "Invalid subscription data"}), 400
    print(f"📥 subscribe-push received for {car}")
    save_sub(car, sub)
    return jsonify({"ok": True, "car": car})

# ---------- Admin ----------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    msg = ""
    cls = "error"

    if request.method == "POST":
        action = request.form.get("action", "reset")
        code   = request.form.get("code", "")

        if code != ADMIN_CODE:
            msg = "Invalid admin code"

        elif action == "notify":
            try:
                target  = request.form.get("target", "all")
                message = request.form.get("message", "").strip()
                if not message:
                    raise ValueError("Message cannot be empty")

                subs   = load_subs()
                sent   = 0
                failed = 0
                dead   = []

                targets = list(subs.keys()) if target == "all" else ([target] if target in subs else [])

                for car_key in targets:
                    try:
                        send_push(subs[car_key], message)
                        sent += 1
                        print(f"✅ Push sent to {car_key}")
                    except WebPushException as e:
                        failed += 1
                        err_str = str(e)
                        print(f"❌ WebPushException for {car_key}: {err_str}")
                        if "410" in err_str or "404" in err_str:
                            dead.append(car_key)
                    except Exception as e:
                        failed += 1
                        print(f"❌ Push error for {car_key}: {e}")

                for k in dead:
                    delete_sub(k)

                if sent > 0:
                    msg = f"✅ Sent to {sent} driver(s)." + (f" {failed} failed." if failed else "")
                    cls = "success"
                elif failed > 0:
                    msg = f"❌ Sent to 0 driver(s). {failed} failed. Ask drivers to tap 🔔 again."
                    cls = "error"
                else:
                    msg = "⚠️ No subscribed drivers. They must tap the 🔔 button first."
                    cls = "error"
            except Exception as e:
                msg = f"Error: {e}"

        elif action == "entry_photo":
            try:
                mode = request.form.get("photo_mode", "hide")
                if mode not in ["hide", "watermark", "bottom"]:
                    mode = "hide"
                photo_file = request.files.get("entry_photo")
                settings = save_uploaded_entry_photo(photo_file, mode)
                if settings.get("mode") == "hide":
                    msg = "✅ Entry page photo hidden."
                elif settings.get("mode") == "watermark":
                    msg = "✅ Entry page photo set as light watermark."
                else:
                    msg = "✅ Entry page photo set at bottom of entry page."
                cls = "success"
            except Exception as e:
                msg = f"Error: {e}"
                cls = "error"

        elif action == "reset":
            try:
                month         = int(request.form["month"])
                year          = int(request.form["year"])
                days_in_month = calendar.monthrange(year, month)[1]
                month_name    = datetime(year, month, 1).strftime("%B")
                first_ord     = f"1{ordinal_suffix(1)}"
                last_ord      = f"{days_in_month}{ordinal_suffix(days_in_month)}"
                title_text    = (f" Vehicle Bill for the period from "
                                 f"{first_ord} {month_name} {year} "
                                 f"to {last_ord} {month_name} {year}")

                with open("driver.json") as f:
                    drivers = json.load(f)

                for car, info in drivers.items():
                    sheet   = info["sheet"]
                    file_id = info["file_id"]

                    sheets.spreadsheets().values().update(
                        spreadsheetId=file_id, range=f"{sheet}!A3",
                        valueInputOption="USER_ENTERED",
                        body={"values": [[title_text]]}
                    ).execute()

                    date_values = [
                        [day, datetime(year, month, day).strftime("%d-%b-%y")]
                        for day in range(1, days_in_month + 1)
                    ]
                    sheets.spreadsheets().values().update(
                        spreadsheetId=file_id,
                        range=f"{sheet}!A8:B{7 + days_in_month}",
                        valueInputOption="USER_ENTERED",
                        body={"values": date_values}
                    ).execute()

                    if days_in_month < 31:
                        sheets.spreadsheets().values().clear(
                            spreadsheetId=file_id,
                            range=f"{sheet}!A{8 + days_in_month}:I{7 + 31}"
                        ).execute()

                    sheets.spreadsheets().values().clear(
                        spreadsheetId=file_id,
                        range=f"{sheet}!C8:I{7 + days_in_month}"
                    ).execute()

                msg = (f"✅ All sheets updated for {month_name} {year} "
                       f"({days_in_month} days).")
                cls = "success"
            except Exception as e:
                msg = f"Error: {e}"

    now     = datetime.now()
    subs    = load_subs()
    drivers = list(DRIVERS.keys())

    return render_template("admin.html", msg=msg, cls=cls,
                           cur_month=now.month, cur_year=now.year,
                           drivers=drivers, subs=subs,
                           entry_photo_settings=load_entry_photo_settings())

@app.route("/clear-push-subs")
def clear_push_subs():
    col = get_col()
    if col is not None:
        try:
            result = col.delete_many({})
            return jsonify({"ok": True, "deleted": result.deleted_count})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    if os.path.exists(SUBS_FILE):
        os.remove(SUBS_FILE)
        return jsonify({"ok": True, "deleted": "file_subscriptions"})
    return jsonify({"ok": True, "deleted": 0})

# ---------- Debug routes ----------
@app.route("/mongo-test")
def mongo_test():
    col = get_col()
    if col is None:
        return jsonify({
            "mongo": "❌ not connected",
            "uri_set": bool(MONGO_URI)
        })
    try:
        docs = list(col.find())
        return jsonify({
            "mongo": "✅ connected",
            "subscription_count": len(docs),
            "subscribed_cars": [d["_id"] for d in docs]
        })
    except Exception as e:
        return jsonify({"mongo": f"❌ error: {e}"})

@app.route("/debug-push")
def debug_push():
    subs = load_subs()
    results = {}
    for car_key, sub_info in subs.items():
        try:
            send_push(sub_info, "🔔 Test notification!", "Test")
            results[car_key] = "✅ sent"
        except Exception as e:
            results[car_key] = f"❌ {e}"
    return jsonify({"subscriptions_found": len(subs), "results": results})

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/ping")
def ping():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)