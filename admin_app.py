import os
import csv
import io
import time
import secrets
import hmac
import logging
import urllib.request
import urllib.parse
import json
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, Response, abort
import database as db

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

if app.secret_key == "change-this-secret-key":
    logger.warning("⚠️ SECRET_KEY орнатылмаған — әдепкі мәнмен жұмыс істеп тұр. Railway/Render Variables-те SECRET_KEY қойыңыз!")
if ADMIN_PASSWORD == "admin123":
    logger.warning("⚠️ ADMIN_PASSWORD орнатылмаған — әдепкі 'admin123' қолданылып тұр. Тез арада ауыстырыңыз!")

# Cookies only travel over HTTPS by default (Render/Render/Railway always serve HTTPS in
# production). Set SESSION_COOKIE_SECURE=0 in the environment only for local http:// testing.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "1") == "1"

# ---------- CSRF protection ----------
# Every POST request must carry the same token that was handed to that browser's
# session — otherwise a malicious page on another site can't submit forms to this
# admin panel using the admin's already-logged-in cookies (classic CSRF attack).

def _csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = _csrf_token


@app.before_request
def _check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, description="CSRF тексеруден өтпеді. Парақты жаңартып, қайта көріңіз.")


# ---------- Login brute-force protection ----------
# In-memory is enough here (single admin-panel process): after 5 wrong passwords from
# the same IP, that IP is locked out for 5 minutes.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 5 * 60
_login_attempts = {}  # ip -> [count, first_attempt_epoch]


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "unknown")


def _login_locked_out(ip):
    entry = _login_attempts.get(ip)
    if not entry:
        return False, 0
    count, first_ts = entry
    if count < _LOGIN_MAX_ATTEMPTS:
        return False, 0
    remaining = _LOGIN_LOCKOUT_SECONDS - (time.time() - first_ts)
    if remaining <= 0:
        _login_attempts.pop(ip, None)
        return False, 0
    return True, int(remaining)


def _register_failed_login(ip):
    count, first_ts = _login_attempts.get(ip, (0, time.time()))
    if time.time() - first_ts > _LOGIN_LOCKOUT_SECONDS:
        count, first_ts = 0, time.time()  # window expired, start fresh
    _login_attempts[ip] = (count + 1, first_ts)


@app.context_processor
def inject_unread_support():
    if not session.get("logged_in"):
        return {}
    try:
        total_unread = sum(t.get("unread") or 0 for t in db.get_support_threads())
    except Exception:
        total_unread = 0
    try:
        pending_tx = len(db.get_pending_transactions())
    except Exception:
        pending_tx = 0
    return {"unread_support": total_unread, "pending_tx": pending_tx}


def send_telegram_message(chat_id, text):
    if not BOT_TOKEN:
        return False, "BOT_TOKEN орнатылмаған"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            return body.get("ok", False), body.get("description", "")
    except Exception as e:
        return False, str(e)


def send_telegram_photo(chat_id, file_id, caption=""):
    if not BOT_TOKEN:
        return False, "BOT_TOKEN орнатылмаған"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    data = urllib.parse.urlencode({"chat_id": chat_id, "photo": file_id, "caption": caption}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            return body.get("ok", False), body.get("description", "")
    except Exception as e:
        return False, str(e)


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = _client_ip()
    if request.method == "POST":
        locked, remaining = _login_locked_out(ip)
        if locked:
            flash(f"⛔ Тым көп қате әрекет. {remaining} секундтан кейін қайталап көріңіз.")
            return render_template("login.html")
        if hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            _login_attempts.pop(ip, None)
            session["logged_in"] = True
            session.permanent = False
            return redirect(url_for("dashboard"))
        _register_failed_login(ip)
        flash("Қате құпия сөз")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    stats = db.get_stats()
    stats["commission_percent"] = db.get_commission_percent()
    stats["total_commission"] = db.get_total_commission()
    daily = db.get_daily_revenue()
    monthly = db.get_monthly_revenue()
    rides = db.get_all_rides(limit=20)
    revenue_by_day = db.get_revenue_by_day(7)
    return render_template("dashboard.html", stats=stats, rides=rides, daily=daily, monthly=monthly,
                            revenue_by_day=revenue_by_day)


@app.route("/rides")
@login_required
def rides():
    status_filter = request.args.get("status", "")
    all_rides = db.get_all_rides()
    if status_filter:
        all_rides = [r for r in all_rides if r["status"] == status_filter]
    return render_template("rides.html", rides=all_rides, status_filter=status_filter)


@app.route("/rides/<int:ride_id>/complete", methods=["POST"])
@login_required
def complete_ride(ride_id):
    db.complete_ride(ride_id)
    return redirect(url_for("rides"))


@app.route("/rides/<int:ride_id>/cancel", methods=["POST"])
@login_required
def cancel_ride(ride_id):
    db.cancel_ride(ride_id)
    return redirect(url_for("rides"))


@app.route("/users")
@login_required
def users():
    q = (request.args.get("q") or "").strip().lower()
    all_users = db.get_all_users()
    if q:
        all_users = [
            u for u in all_users
            if q in (u.get("full_name") or "").lower()
            or q in (u.get("username") or "").lower()
            or q in (u.get("phone") or "").lower()
        ]
    for u in all_users:
        if u["role"] == "driver":
            r = db.get_driver_rating(u["id"])
            u["rating_display"] = f"⭐{r['avg']} ({r['count']})" if r["count"] else "—"
    return render_template("users.html", users=all_users, q=q)


@app.route("/users/export.csv")
@login_required
def export_users():
    q = (request.args.get("q") or "").strip().lower()
    all_users = db.get_all_users()
    if q:
        all_users = [
            u for u in all_users
            if q in (u.get("full_name") or "").lower()
            or q in (u.get("username") or "").lower()
            or q in (u.get("phone") or "").lower()
        ]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "full_name", "username", "phone", "role", "is_online", "is_blocked",
                      "intercity_access", "verification_status", "created_at"])
    for u in all_users:
        writer.writerow([u.get(k) for k in
                          ["id", "full_name", "username", "phone", "role", "is_online", "is_blocked",
                           "intercity_access", "verification_status", "created_at"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": "attachment; filename=users.csv"})


@app.route("/users/<int:user_id>/block", methods=["POST"])
@login_required
def block_user(user_id):
    db.set_blocked(user_id, True)
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/unblock", methods=["POST"])
@login_required
def unblock_user(user_id):
    db.set_blocked(user_id, False)
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
def delete_user(user_id):
    db.delete_user(user_id)
    flash("Пайдаланушы жойылды.")
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/grant_intercity", methods=["POST"])
@login_required
def grant_intercity(user_id):
    db.set_intercity_access(user_id, True)
    user = db.get_user_by_id(user_id)
    if user:
        send_telegram_message(
            user["telegram_id"],
            "✅ Сізге 'Жанақала — Орал' бағыты бойынша тапсырыс беруге рұқсат берілді! Тапсырысты қайта бастап көріңіз.",
        )
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/revoke_intercity", methods=["POST"])
@login_required
def revoke_intercity(user_id):
    db.set_intercity_access(user_id, False)
    return redirect(url_for("users"))


@app.route("/drivers")
@login_required
def drivers():
    status_filter = request.args.get("status", "")
    all_drivers = db.get_all_drivers()
    for d in all_drivers:
        r = db.get_driver_rating(d["id"])
        d["rating_display"] = f"⭐{r['avg']} ({r['count']})" if r["count"] else "—"
        earn = db.get_driver_earnings(d["id"])
        d["earnings_display"] = f"{earn['total']} тг ({earn['count']} сапар)"
    if status_filter:
        all_drivers = [d for d in all_drivers if (d.get("verification_status") or "none") == status_filter]
    return render_template("drivers.html", drivers=all_drivers, status_filter=status_filter)


@app.route("/drivers/<int:user_id>/approve", methods=["POST"])
@login_required
def approve_driver(user_id):
    db.set_verification_status(user_id, "approved")
    user = db.get_user_by_id(user_id)
    if user:
        send_telegram_message(user["telegram_id"], "✅ Құжаттарың расталды! Енді 🟢 Online режиміне кіре аласың.")
    return redirect(url_for("drivers"))


@app.route("/drivers/<int:user_id>/reject", methods=["POST"])
@login_required
def reject_driver(user_id):
    reason = request.form.get("reason", "").strip() or "Талапқа сай емес"
    db.set_verification_status(user_id, "rejected", reason)
    user = db.get_user_by_id(user_id)
    if user:
        send_telegram_message(
            user["telegram_id"],
            f"❌ Құжаттарың қабылданбады. Себебі: {reason}\n🔄 Деректерді түзетіп қайта тіркеле аласың.",
        )
    return redirect(url_for("drivers"))


@app.route("/drivers/<int:user_id>/toggle_online", methods=["POST"])
@login_required
def toggle_driver_online(user_id):
    user = db.get_user_by_id(user_id)
    if user:
        db.set_online(user["telegram_id"], not bool(user["is_online"]))
    return redirect(url_for("drivers"))


@app.route("/cities", methods=["GET", "POST"])
@login_required
def cities():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            if db.add_city(name):
                flash(f"'{name}' қосылды")
            else:
                flash("Бұл қала/аудан бұрын қосылған")
        else:
            flash("Атауын жаз")
    all_cities = db.get_cities()
    tariffs = db.get_all_tariffs()
    return render_template("cities.html", cities=all_cities, tariffs=tariffs)


@app.route("/cities/<int:city_id>/toggle", methods=["POST"])
@login_required
def toggle_city(city_id):
    all_cities = {c["id"]: c for c in db.get_cities()}
    c = all_cities.get(city_id)
    if c:
        db.set_city_active(city_id, not bool(c["is_active"]))
    return redirect(url_for("cities"))


@app.route("/cities/<int:city_id>/delete", methods=["POST"])
@login_required
def delete_city(city_id):
    db.delete_city(city_id)
    return redirect(url_for("cities"))


@app.route("/tariffs", methods=["POST"])
@login_required
def update_tariffs():
    for route in ("local", "intercity"):
        base = request.form.get(f"{route}_base", "").strip()
        per_km = request.form.get(f"{route}_per_km", "").strip()
        if base or per_km:
            db.set_tariff(route, base, per_km)
    flash("Тарифтер сақталды")
    return redirect(url_for("cities"))


@app.route("/broadcast", methods=["GET", "POST"])
@login_required
def broadcast():
    result = None
    if request.method == "POST":
        text = request.form.get("text", "").strip()
        role_filter = request.form.get("role") or None
        if not text:
            flash("Хабарлама мәтінін жаз")
        else:
            recipients = db.get_broadcast_recipients(role_filter)
            sent, failed = 0, 0
            for tg_id in recipients:
                ok, _ = send_telegram_message(tg_id, text)
                if ok:
                    sent += 1
                else:
                    failed += 1
            result = {"sent": sent, "failed": failed, "total": len(recipients)}
    return render_template("broadcast.html", result=result, bot_token_set=bool(BOT_TOKEN))


@app.route("/rides/export.csv")
@login_required
def export_rides():
    status_filter = request.args.get("status", "")
    all_rides = db.get_all_rides()
    if status_filter:
        all_rides = [r for r in all_rides if r["status"] == status_filter]
    buf = io.StringIO()
    writer = csv.writer(buf)
    cols = ["id", "client_name", "driver_name", "service_type", "route", "from_location",
            "to_location", "ride_time", "price", "status", "rating", "client_rating", "created_at"]
    writer.writerow(cols)
    for r in all_rides:
        writer.writerow([r.get(c) for c in cols])
    return Response(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": "attachment; filename=rides.csv"})


@app.route("/support", methods=["GET", "POST"])
@login_required
def support():
    user_id = request.args.get("user_id", type=int)

    if request.method == "POST" and user_id:
        text = request.form.get("text", "").strip()
        user = db.get_user_by_id(user_id)
        if text and user:
            db.add_support_message(user_id, "out", text)
            ok, err = send_telegram_message(user["telegram_id"], f"💬 Әкімші: {text}")
            if not ok:
                flash(f"Хабарлама Telegram-ға жіберілмеді: {err}")
        return redirect(url_for("support", user_id=user_id))

    if user_id:
        selected = db.get_user_by_id(user_id)
        if not selected:
            return redirect(url_for("support"))
        db.mark_support_read(user_id)
        messages = db.get_support_messages(user_id)
        return render_template("support.html", selected=selected, messages=messages, threads=None)

    threads = db.get_support_threads()
    return render_template("support.html", threads=threads, selected=None)


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        percent = request.form.get("commission_percent", "10")
        min_balance = request.form.get("min_balance", "500")
        try:
            float(percent)
            db.set_setting("commission_percent", percent)
            float(min_balance)
            db.set_min_balance(min_balance)
            flash("Баптаулар сақталды")
        except ValueError:
            flash("Дұрыс сан жаз")
    current = db.get_commission_percent()
    total = db.get_total_commission()
    min_balance = db.get_min_balance()
    return render_template("settings.html", commission_percent=current, total_commission=total,
                            min_balance=min_balance)


@app.route("/transactions")
@login_required
def transactions():
    pending = db.get_pending_transactions()
    return render_template("transactions.html", pending=pending)


@app.route("/transactions/<int:tx_id>/approve", methods=["POST"])
@login_required
def approve_tx(tx_id):
    tx = db.approve_transaction(tx_id)
    if tx and tx.get("insufficient_balance"):
        flash(f"⚠️ Растай алмадым: балансында жеткілікті қаражат жоқ (қазіргі: {tx['current_balance']} ₸). Сұрау әлі 'күтуде'.")
    elif tx:
        user = db.get_user_by_id(tx["user_id"])
        if user:
            new_balance = db.get_balance(tx["user_id"])
            kind = "толтыру" if tx["type"] == "topup" else "шығару"
            send_telegram_message(
                user["telegram_id"],
                f"✅ {abs(tx['amount']):.0f} ₸ {kind} расталды.\n💳 Жаңа баланс: {new_balance} ₸",
            )
        flash("Транзакция расталды")
    else:
        flash("Бұл транзакция бұрын өңделген")
    return redirect(url_for("transactions"))


@app.route("/transactions/<int:tx_id>/reject", methods=["POST"])
@login_required
def reject_tx(tx_id):
    tx = db.get_transaction(tx_id)
    if db.reject_transaction(tx_id):
        if tx:
            user = db.get_user_by_id(tx["user_id"])
            if user:
                kind = "толтыру" if tx["type"] == "topup" else "шығару"
                send_telegram_message(user["telegram_id"], f"❌ {abs(tx['amount']):.0f} ₸ {kind} сұрауы қабылданбады.")
        flash("Транзакция қабылданбады")
    else:
        flash("Бұл транзакция бұрын өңделген")
    return redirect(url_for("transactions"))


if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", "5000"))
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
