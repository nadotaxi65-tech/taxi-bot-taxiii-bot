"""
Telegram ішіндегі толық админ панель.

Веб-панельдегі (admin_app.py) барлық негізгі функцияларды Telegram-ның өз
чаты арқылы қолжетімді етеді: статистика, пайдаланушылар, жүргізушілер,
сапарлар, қалалар/тарифтер, broadcast, қолдау (support) және баптаулар.

Іске қосу: /admin командасын ADMIN_CHAT_ID-дегі адам ғана жаза алады.

bot.py-ге қосу үшін:

    import admin_telegram

    ...
    admin_telegram.register(app, ADMIN_CHAT_ID, contact_name, driver_menu)

`register()` барлық handler-лерді -1 тобына (group=-1) тіркейді, сондықтан
олар қалыпты пайдаланушы ConversationHandler-лерімен қақтығыспайды және
ADMIN_CHAT_ID-ден басқа ешкімге әсер етпейді.
"""
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters,
)
import database as db

logger = logging.getLogger(__name__)

PAGE_SIZE = 6

# Осы модуль ішінде толтырылады (register() шақырылғанда)
_ADMIN_CHAT_ID = ""
_contact_name = lambda user: (user.get("full_name") or user.get("username") or str(user.get("telegram_id")))
_driver_menu = lambda user: None


# ---------- Көмекші функциялар ----------

def _is_admin_update(update: Update) -> bool:
    return bool(_ADMIN_CHAT_ID) and update.effective_user and str(update.effective_user.id) == str(_ADMIN_CHAT_ID)


def _paginate(items, page, size=PAGE_SIZE):
    total_pages = max(1, (len(items) + size - 1) // size)
    page = max(0, min(page, total_pages - 1))
    start = page * size
    return items[start:start + size], page, total_pages


def _nav_row(prefix, page, total_pages, extra=""):
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️", callback_data=f"{prefix}_{page - 1}{extra}"))
    row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="adm_noop"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("➡️", callback_data=f"{prefix}_{page + 1}{extra}"))
    return row


def _back_btn(callback_data="adm_main"):
    return InlineKeyboardButton("⬅️ Бас мәзір", callback_data=callback_data)


async def _reply(update: Update, text, kb=None):
    """callback query болса edit, әйтпесе жаңа хабарлама."""
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def _guard(update: Update) -> bool:
    if not _is_admin_update(update):
        if update.callback_query:
            await update.callback_query.answer("Бұл панель тек әкімшіге арналған.", show_alert=True)
        return False
    if update.callback_query:
        await update.callback_query.answer()
    return True


# ---------- Бас мәзір ----------

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton("👥 Пайдаланушылар", callback_data="adm_users_0")],
        [InlineKeyboardButton("🚗 Жүргізушілер", callback_data="adm_drivers_0_all")],
        [InlineKeyboardButton("🚕 Сапарлар", callback_data="adm_rides_0_all")],
        [InlineKeyboardButton("🏙 Қалалар / Тарифтер", callback_data="adm_cities")],
        [InlineKeyboardButton("💳 Транзакциялар", callback_data="adm_tx_0")],
        [InlineKeyboardButton("📢 Хабарлама жіберу", callback_data="adm_broadcast")],
        [InlineKeyboardButton("💬 Қолдау қызметі", callback_data="adm_support_0")],
        [InlineKeyboardButton("⚙️ Баптаулар", callback_data="adm_settings")],
    ])


async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    context.user_data.pop("adm_wait", None)
    await _reply(update, "🛠 <b>Әкімші панелі</b>\n\nБөлімді таңдаңыз:", main_menu_kb())


async def adm_main_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    context.user_data.pop("adm_wait", None)
    await _reply(update, "🛠 <b>Әкімші панелі</b>\n\nБөлімді таңдаңыз:", main_menu_kb())


async def adm_noop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()


# ---------- 📊 Статистика ----------

async def adm_stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    s = db.get_stats()
    commission_pct = db.get_commission_percent()
    total_commission = db.get_total_commission()
    daily = db.get_daily_revenue()
    monthly = db.get_monthly_revenue()
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Барлық пайдаланушы: <b>{s['total_users']}</b>\n"
        f"🧍 Жолаушылар: <b>{s['total_clients']}</b>\n"
        f"🚗 Жүргізушілер: <b>{s['total_drivers']}</b> (онлайн: {s['online_drivers']}, күтуде: {s['pending_drivers']})\n\n"
        f"🚕 Барлық сапар: <b>{s['total_rides']}</b>\n"
        f"🟢 Ашық: {s['open_rides']} | ✅ Аяқталған: {s['completed_rides']}\n"
        f"📦 Жеткізу: {s['total_deliveries']} | 🛣 Қалааралық: {s['total_intercity']}\n"
        f"⭐ Орташа рейтинг: {s['avg_rating'] or '—'} ({s['rating_count']} баға)\n\n"
        f"💰 Күндік табыс: <b>{daily} тг</b>\n"
        f"💰 Айлық табыс: <b>{monthly} тг</b>\n"
        f"💼 Комиссия: {commission_pct}% (жиналған: <b>{total_commission} тг</b>)"
    )
    kb = InlineKeyboardMarkup([[_back_btn()]])
    await _reply(update, text, kb)


# ---------- 👥 Пайдаланушылар ----------

async def adm_users_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    page = int(update.callback_query.data.split("_")[2])
    all_users = db.get_all_users()
    items, page, total_pages = _paginate(all_users, page)
    rows = []
    for u in items:
        role_icon = "🚗" if u["role"] == "driver" else "🧍"
        blocked_icon = "🚫" if u.get("is_blocked") else ""
        rows.append([InlineKeyboardButton(
            f"{role_icon} {_contact_name(u)} {blocked_icon}", callback_data=f"adm_user_{u['id']}"
        )])
    rows.append(_nav_row("adm_users", page, total_pages))
    rows.append([_back_btn()])
    text = f"👥 <b>Пайдаланушылар</b> ({len(all_users)})"
    await _reply(update, text, InlineKeyboardMarkup(rows))


async def adm_user_detail_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    user_id = int(update.callback_query.data.split("_")[2])
    u = db.get_user_by_id(user_id)
    if not u:
        await _reply(update, "Пайдаланушы табылмады.", InlineKeyboardMarkup([[_back_btn("adm_users_0")]]))
        return
    lines = [
        f"👤 <b>{_contact_name(u)}</b>",
        f"ID: {u['id']} | TG: <code>{u['telegram_id']}</code>",
        f"Рөлі: {'Жүргізуші' if u['role'] == 'driver' else 'Жолаушы'}",
        f"Телефон: {u.get('phone') or '—'}",
        f"Блок: {'✅ Иә' if u.get('is_blocked') else '❌ Жоқ'}",
        f"Қалааралық рұқсат: {'✅ Иә' if u.get('intercity_access') else '❌ Жоқ'}",
    ]
    if u["role"] == "driver":
        r = db.get_driver_rating(u["id"])
        earn = db.get_driver_earnings(u["id"])
        lines.append(f"Рейтинг: {'⭐' + str(r['avg']) + ' (' + str(r['count']) + ')' if r['count'] else '—'}")
        lines.append(f"Табыс: {earn['total']} тг ({earn['count']} сапар)")
        lines.append(f"💳 Баланс: {db.get_balance(u['id'])} тг")
        lines.append(f"Верификация: {u.get('verification_status') or 'none'}")
        lines.append(f"Онлайн: {'🟢' if u.get('is_online') else '🔴'}")

    rows = []
    if u["role"] == "driver" and (u.get("verification_status") or "none") != "approved":
        rows.append([InlineKeyboardButton("✅ Рұқсат беру (растау)", callback_data=f"adm_driver_approve_{user_id}")])
    rows.append([InlineKeyboardButton("✉️ Хабарлама жазу", callback_data=f"adm_supportreply_{user_id}")])
    if u.get("is_blocked"):
        rows.append([InlineKeyboardButton("✅ Блокты алу", callback_data=f"adm_user_unblock_{user_id}")])
    else:
        rows.append([InlineKeyboardButton("🚫 Блоктау", callback_data=f"adm_user_block_{user_id}")])
    if u.get("intercity_access"):
        rows.append([InlineKeyboardButton("❌ Қалааралық рұқсатты алу", callback_data=f"adm_user_revoke_{user_id}")])
    else:
        rows.append([InlineKeyboardButton("🛣 Қалааралық рұқсат беру", callback_data=f"adm_user_grant_{user_id}")])
    rows.append([InlineKeyboardButton("🗑 Жою", callback_data=f"adm_user_delconfirm_{user_id}")])
    rows.append([_back_btn("adm_users_0")])
    await _reply(update, "\n".join(lines), InlineKeyboardMarkup(rows))


async def adm_user_delete_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    user_id = int(update.callback_query.data.split("_")[3])
    u = db.get_user_by_id(user_id)
    if not u:
        await _reply(update, "Пайдаланушы табылмады.", InlineKeyboardMarkup([[_back_btn("adm_users_0")]]))
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Иә, жою", callback_data=f"adm_user_deldo_{user_id}")],
        [InlineKeyboardButton("Бас тарту", callback_data=f"adm_user_{user_id}")],
    ])
    await _reply(
        update,
        f"⚠️ <b>{_contact_name(u)}</b> толығымен жойылады, соның ішінде барлық сапарлары мен хат-хабары.\n"
        "Бұл әрекетті қайтару мүмкін емес. Растайсыз ба?",
        kb,
    )


async def adm_user_delete_do_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    user_id = int(update.callback_query.data.split("_")[3])
    u = db.get_user_by_id(user_id)
    name = _contact_name(u) if u else str(user_id)
    db.delete_user(user_id)
    await _reply(update, f"🗑 {name} жойылды.", InlineKeyboardMarkup([[_back_btn("adm_users_0")]]))


async def adm_user_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    parts = update.callback_query.data.split("_")  # adm_user_<action>_<id>
    action, user_id = parts[2], int(parts[3])
    u = db.get_user_by_id(user_id)
    if not u:
        await _reply(update, "Пайдаланушы табылмады.", InlineKeyboardMarkup([[_back_btn("adm_users_0")]]))
        return
    if action == "block":
        db.set_blocked(user_id, True)
    elif action == "unblock":
        db.set_blocked(user_id, False)
    elif action == "grant":
        db.set_intercity_access(user_id, True)
        try:
            await context.bot.send_message(
                u["telegram_id"],
                "✅ Сізге 'Жанақала — Орал' бағыты бойынша тапсырыс беруге рұқсат берілді!",
            )
        except Exception as e:
            logger.warning(f"notify grant failed: {e}")
    elif action == "revoke":
        db.set_intercity_access(user_id, False)
    # detail бетін жаңарту
    update.callback_query.data = f"adm_user_{user_id}"
    await adm_user_detail_cb(update, context)


# ---------- 🚗 Жүргізушілер ----------

DRIVER_FILTERS = {"all": None, "pending": "pending", "approved": "approved", "rejected": "rejected"}


async def adm_drivers_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    _, _, page, flt = update.callback_query.data.split("_")
    page = int(page)
    all_drivers = db.get_all_drivers()
    status_val = DRIVER_FILTERS.get(flt)
    if status_val:
        all_drivers = [d for d in all_drivers if (d.get("verification_status") or "none") == status_val]
    items, page, total_pages = _paginate(all_drivers, page)
    rows = []
    for d in items:
        status_icon = {"approved": "✅", "pending": "⏳", "rejected": "❌"}.get(d.get("verification_status"), "•")
        online_icon = "🟢" if d.get("is_online") else "🔴"
        rows.append([InlineKeyboardButton(
            f"{status_icon}{online_icon} {_contact_name(d)}", callback_data=f"adm_driver_{d['id']}"
        )])
    filter_row = [
        InlineKeyboardButton(("• " if flt == "all" else "") + "Барлығы", callback_data="adm_drivers_0_all"),
        InlineKeyboardButton(("• " if flt == "pending" else "") + "Күтуде", callback_data="adm_drivers_0_pending"),
        InlineKeyboardButton(("• " if flt == "approved" else "") + "Расталған", callback_data="adm_drivers_0_approved"),
    ]
    rows.append(filter_row)
    rows.append(_nav_row("adm_drivers", page, total_pages, extra=f"_{flt}"))
    rows.append([_back_btn()])
    await _reply(update, f"🚗 <b>Жүргізушілер</b> ({len(all_drivers)})", InlineKeyboardMarkup(rows))


async def adm_driver_detail_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    user_id = int(update.callback_query.data.split("_")[2])
    d = db.get_user_by_id(user_id)
    if not d:
        await _reply(update, "Жүргізуші табылмады.", InlineKeyboardMarkup([[_back_btn("adm_drivers_0_all")]]))
        return
    r = db.get_driver_rating(d["id"])
    earn = db.get_driver_earnings(d["id"])
    lines = [
        f"🚗 <b>{_contact_name(d)}</b>",
        f"Телефон: {d.get('phone') or '—'}",
        f"Қала: {d.get('work_city') or '—'}",
        f"Көлік: {db.vehicle_str(d)}",
        f"Верификация: {d.get('verification_status') or 'none'}"
        + (f" (себебі: {d.get('reject_reason')})" if d.get("verification_status") == "rejected" and d.get("reject_reason") else ""),
        f"Онлайн: {'🟢' if d.get('is_online') else '🔴'}",
        f"Рейтинг: {'⭐' + str(r['avg']) + ' (' + str(r['count']) + ')' if r['count'] else '—'}",
        f"Табыс: {earn['total']} тг ({earn['count']} сапар)",
        f"💳 Баланс: {db.get_balance(d['id'])} тг",
        f"Блок: {'✅ Иә' if d.get('is_blocked') else '❌ Жоқ'}",
    ]
    rows = []
    if d.get("id_photo") or d.get("car_doc_photo"):
        rows.append([InlineKeyboardButton("🪪📄 Құжаттарды көру", callback_data=f"adm_driverdocs_{user_id}")])
    if (d.get("verification_status") or "none") != "approved":
        rows.append([InlineKeyboardButton("✅ Растау", callback_data=f"adm_driver_approve_{user_id}")])
        rows.append([InlineKeyboardButton("❌ Қабылдамау", callback_data=f"adm_driver_reject_{user_id}")])
    rows.append([InlineKeyboardButton(
        "🔴 Offline ету" if d.get("is_online") else "🟢 Online ету",
        callback_data=f"adm_driver_toggleonline_{user_id}",
    )])
    if d.get("is_blocked"):
        rows.append([InlineKeyboardButton("✅ Блокты алу", callback_data=f"adm_user_unblock_{user_id}")])
    else:
        rows.append([InlineKeyboardButton("🚫 Блоктау", callback_data=f"adm_user_block_{user_id}")])
    rows.append([_back_btn("adm_drivers_0_all")])
    await _reply(update, "\n".join(lines), InlineKeyboardMarkup(rows))


async def adm_driver_docs_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    user_id = int(update.callback_query.data.split("_")[2])
    d = db.get_user_by_id(user_id)
    if not d:
        return
    if d.get("id_photo"):
        await context.bot.send_photo(update.effective_chat.id, d["id_photo"], caption="🪪 Жеке куәлік")
    if d.get("car_doc_photo"):
        await context.bot.send_photo(update.effective_chat.id, d["car_doc_photo"], caption="📄 Көлік құжаты")
    if d.get("car_photo"):
        await context.bot.send_photo(update.effective_chat.id, d["car_photo"], caption="🚗 Көлік фотосы")
    if not (d.get("id_photo") or d.get("car_doc_photo") or d.get("car_photo")):
        await context.bot.send_message(update.effective_chat.id, "Құжат фотолары жоқ.")


async def adm_driver_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    parts = update.callback_query.data.split("_")  # adm_driver_<action>_<id>
    action, user_id = parts[2], int(parts[3])
    d = db.get_user_by_id(user_id)
    if not d:
        await _reply(update, "Жүргізуші табылмады.", InlineKeyboardMarkup([[_back_btn("adm_drivers_0_all")]]))
        return
    if action == "approve":
        db.set_verification_status(user_id, "approved")
        try:
            await context.bot.send_message(
                d["telegram_id"], "✅ Құжаттарың расталды! Енді 🟢 Online режиміне кіре аласың.",
                reply_markup=_driver_menu(db.get_user_by_id(user_id)),
            )
        except Exception as e:
            logger.warning(f"notify approve failed: {e}")
    elif action == "reject":
        context.user_data["adm_wait"] = ("driver_reject", user_id)
        await _reply(
            update,
            f"❌ {_contact_name(d)} үшін қабылдамау себебін жазып жіберіңіз (мәтін хабарлама):",
            InlineKeyboardMarkup([[InlineKeyboardButton("Бас тарту", callback_data=f"adm_driver_{user_id}")]]),
        )
        return
    elif action == "toggleonline":
        db.set_online(d["telegram_id"], not bool(d.get("is_online")))
    update.callback_query.data = f"adm_driver_{user_id}"
    await adm_driver_detail_cb(update, context)


# ---------- 🚕 Сапарлар ----------

RIDE_FILTERS = ["all", "open", "accepted", "completed", "cancelled"]


async def adm_rides_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    _, _, page, flt = update.callback_query.data.split("_")
    page = int(page)
    all_rides = db.get_all_rides()
    if flt != "all":
        all_rides = [r for r in all_rides if r["status"] == flt]
    items, page, total_pages = _paginate(all_rides, page)
    rows = []
    for r in items:
        status_icon = {"open": "🟢", "accepted": "🟡", "completed": "✅", "cancelled": "❌"}.get(r["status"], "•")
        rows.append([InlineKeyboardButton(
            f"{status_icon} #{r['id']} {r.get('from_location') or ''}→{r.get('to_location') or ''} ({r.get('price') or '—'}тг)",
            callback_data=f"adm_ride_{r['id']}",
        )])
    filter_row = [InlineKeyboardButton(("• " if flt == f else "") + f, callback_data=f"adm_rides_0_{f}") for f in RIDE_FILTERS[:3]]
    filter_row2 = [InlineKeyboardButton(("• " if flt == f else "") + f, callback_data=f"adm_rides_0_{f}") for f in RIDE_FILTERS[3:]]
    rows.append(filter_row)
    rows.append(filter_row2)
    rows.append(_nav_row("adm_rides", page, total_pages, extra=f"_{flt}"))
    rows.append([_back_btn()])
    await _reply(update, f"🚕 <b>Сапарлар</b> ({len(all_rides)})", InlineKeyboardMarkup(rows))


async def adm_ride_detail_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    ride_id = int(update.callback_query.data.split("_")[2])
    r = db.get_ride(ride_id)
    if not r:
        await _reply(update, "Сапар табылмады.", InlineKeyboardMarkup([[_back_btn("adm_rides_0_all")]]))
        return
    lines = [
        f"🚕 <b>Сапар #{r['id']}</b>",
        f"Статус: {r['status']}",
        f"Клиент: {r.get('client_name') or '—'}",
        f"Жүргізуші: {r.get('driver_name') or '—'}",
        f"Бағыт: {r.get('from_location') or '—'} → {r.get('to_location') or '—'}",
        f"👥 Адам саны: {r.get('passenger_count') or 1}",
        f"Уақыты: {r.get('ride_time') or '—'}",
        f"Баға: {r.get('price') or '—'} тг",
    ]
    rows = []
    if r["status"] in ("open", "accepted"):
        rows.append([InlineKeyboardButton("✅ Аяқтау", callback_data=f"adm_ride_complete_{ride_id}")])
        rows.append([InlineKeyboardButton("❌ Болдырмау", callback_data=f"adm_ride_cancel_{ride_id}")])
    rows.append([_back_btn("adm_rides_0_all")])
    await _reply(update, "\n".join(lines), InlineKeyboardMarkup(rows))


async def adm_ride_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    parts = update.callback_query.data.split("_")  # adm_ride_<action>_<id>
    action, ride_id = parts[2], int(parts[3])
    if action == "complete":
        db.complete_ride(ride_id)
    elif action == "cancel":
        db.cancel_ride(ride_id)
    update.callback_query.data = f"adm_ride_{ride_id}"
    await adm_ride_detail_cb(update, context)


# ---------- 🏙 Қалалар / Тарифтер ----------

async def adm_cities_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    cities = db.get_cities()
    tariffs = db.get_all_tariffs()  # {"local": {...}, "intercity": {...}}
    rows = []
    for c in cities:
        icon = "🟢" if c["is_active"] else "🔴"
        rows.append([
            InlineKeyboardButton(f"{icon} {c['name']}", callback_data=f"adm_city_toggle_{c['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"adm_city_delete_{c['id']}"),
        ])
    rows.append([InlineKeyboardButton("➕ Қала/аудан қосу", callback_data="adm_city_add")])
    local = tariffs.get("local", {})
    intercity = tariffs.get("intercity", {})
    rows.append([InlineKeyboardButton(
        f"💵 Local тариф: {local.get('base_price', '—')}тг + {local.get('price_per_km', '—')}тг/км",
        callback_data="adm_tariff_set_local",
    )])
    rows.append([InlineKeyboardButton(
        f"💵 Қалааралық тариф: {intercity.get('base_price', '—')}тг + {intercity.get('price_per_km', '—')}тг/км",
        callback_data="adm_tariff_set_intercity",
    )])
    rows.append([_back_btn()])
    await _reply(update, "🏙 <b>Қалалар / Тарифтер</b>\n(қаланы басу — қосу/өшіру, 🗑 — жою)", InlineKeyboardMarkup(rows))


async def adm_city_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    data = update.callback_query.data
    if data == "adm_city_add":
        context.user_data["adm_wait"] = ("city_add", None)
        await _reply(update, "🏙 Жаңа қала/аудан атауын жазып жіберіңіз:",
                     InlineKeyboardMarkup([[InlineKeyboardButton("Бас тарту", callback_data="adm_cities")]]))
        return
    parts = data.split("_")  # adm_city_<action>_<id>
    action, city_id = parts[2], int(parts[3])
    if action == "toggle":
        all_cities = {c["id"]: c for c in db.get_cities()}
        c = all_cities.get(city_id)
        if c:
            db.set_city_active(city_id, not bool(c["is_active"]))
    elif action == "delete":
        db.delete_city(city_id)
    await adm_cities_cb(update, context)


async def adm_tariff_set_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    route = update.callback_query.data.split("_")[3]  # adm_tariff_set_<route>
    context.user_data["adm_wait"] = ("tariff", route)
    await _reply(
        update,
        f"💵 '{route}' тарифін жазыңыз, форматы: <code>база,км-баға</code>\nМысалы: <code>500,100</code>",
        InlineKeyboardMarkup([[InlineKeyboardButton("Бас тарту", callback_data="adm_cities")]]),
    )


# ---------- 💳 Транзакциялар (баланс толтыру / шығару) ----------

async def adm_tx_list_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    page = int(update.callback_query.data.split("_")[2])
    pending = db.get_pending_transactions()
    items, page, total_pages = _paginate(pending, page, size=5)
    rows = []
    icon = {"topup": "➕", "withdrawal": "💸"}
    for t in items:
        label = f"{icon.get(t['type'], '•')} {_contact_name(t)} — {abs(t['amount']):.0f} ₸"
        rows.append([InlineKeyboardButton(label, callback_data=f"adm_txview_{t['id']}")])
    rows.append(_nav_row("adm_tx", page, total_pages))
    rows.append([_back_btn()])
    header = f"💳 <b>Күтудегі транзакциялар</b> ({len(pending)})"
    await _reply(update, header, InlineKeyboardMarkup(rows))


async def adm_tx_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    tx_id = int(update.callback_query.data.split("_")[2])
    tx = db.get_transaction(tx_id)
    if not tx:
        await _reply(update, "Транзакция табылмады.", InlineKeyboardMarkup([[_back_btn("adm_tx_0")]]))
        return
    u = db.get_user_by_id(tx["user_id"])
    type_name = {"topup": "💳 Баланс толтыру", "withdrawal": "💸 Ақша шығару"}.get(tx["type"], tx["type"])
    lines = [
        f"{type_name}",
        f"👤 {_contact_name(u) if u else tx['user_id']}",
        f"📱 {u.get('phone') or '—' if u else '—'}",
        f"💰 Сома: {abs(tx['amount']):.0f} ₸",
        f"Статус: {tx['status']}",
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Растау", callback_data=f"adm_tx_approve_{tx_id}"),
         InlineKeyboardButton("❌ Бас тарту", callback_data=f"adm_tx_reject_{tx_id}")],
        [_back_btn("adm_tx_0")],
    ])
    await _reply(update, "\n".join(lines), kb)


async def adm_tx_approve_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    tx_id = int(update.callback_query.data.split("_")[3])
    tx = db.approve_transaction(tx_id)
    if not tx:
        await _reply(update, "Бұл сұрау бұрын өңделген немесе табылмады.", InlineKeyboardMarkup([[_back_btn("adm_tx_0")]]))
        return
    if tx.get("insufficient_balance"):
        u = db.get_user_by_id(tx["user_id"])
        await _reply(
            update,
            f"⚠️ Растай алмадым: {_contact_name(u) if u else tx['user_id']} балансында жеткілікті қаражат жоқ "
            f"(қазіргі баланс: {tx['current_balance']} ₸, сұралған: {abs(tx['amount']):.0f} ₸).\n"
            "Сұрау әлі 'күтуде' күйінде қалды.",
            InlineKeyboardMarkup([[_back_btn("adm_tx_0")]]),
        )
        return
    u = db.get_user_by_id(tx["user_id"])
    new_balance = db.get_balance(tx["user_id"])
    type_name = "Баланс толтырылды" if tx["type"] == "topup" else "Ақша шығарылды"
    await _reply(update, f"✅ {type_name}: {abs(tx['amount']):.0f} ₸\n👤 {_contact_name(u) if u else tx['user_id']}",
                 InlineKeyboardMarkup([[_back_btn("adm_tx_0")]]))
    if u:
        try:
            msg = (
                f"✅ {abs(tx['amount']):.0f} ₸ балансыңызға қосылды!" if tx["type"] == "topup"
                else f"✅ {abs(tx['amount']):.0f} ₸ шығару расталды, ақша беріледі."
            )
            await context.bot.send_message(u["telegram_id"], f"{msg}\n💳 Жаңа баланс: {new_balance} ₸")
        except Exception as e:
            logger.warning(f"tx approve notify failed: {e}")


async def adm_tx_reject_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    tx_id = int(update.callback_query.data.split("_")[3])
    tx = db.get_transaction(tx_id)
    ok = db.reject_transaction(tx_id)
    if not ok or not tx:
        await _reply(update, "Бұл сұрау бұрын өңделген немесе табылмады.", InlineKeyboardMarkup([[_back_btn("adm_tx_0")]]))
        return
    u = db.get_user_by_id(tx["user_id"])
    await _reply(update, f"❌ Сұрау бас тартылды.\n👤 {_contact_name(u) if u else tx['user_id']}",
                 InlineKeyboardMarkup([[_back_btn("adm_tx_0")]]))
    if u:
        try:
            await context.bot.send_message(u["telegram_id"], "❌ Сіздің сұрауыңыз бас тартылды. Әкімшіге хабарласыңыз.")
        except Exception as e:
            logger.warning(f"tx reject notify failed: {e}")


# ---------- 📢 Broadcast ----------

async def adm_broadcast_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Барлығына", callback_data="adm_bc_all")],
        [InlineKeyboardButton("🧍 Жолаушыларға", callback_data="adm_bc_client")],
        [InlineKeyboardButton("🚗 Жүргізушілерге", callback_data="adm_bc_driver")],
        [_back_btn()],
    ])
    await _reply(update, "📢 <b>Хабарлама жіберу</b>\nАлушыларды таңдаңыз:", kb)


async def adm_broadcast_audience_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    audience = update.callback_query.data.split("_")[2]  # adm_bc_<audience>
    role_filter = None if audience == "all" else audience
    context.user_data["adm_wait"] = ("broadcast", role_filter)
    await _reply(
        update, "✍️ Жіберілетін хабарлама мәтінін жазыңыз:",
        InlineKeyboardMarkup([[InlineKeyboardButton("Бас тарту", callback_data="adm_broadcast")]]),
    )


# ---------- 💬 Қолдау қызметі ----------

async def adm_support_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    page = int(update.callback_query.data.split("_")[2])
    threads = db.get_support_threads()
    items, page, total_pages = _paginate(threads, page)
    rows = []
    for t in items:
        unread = t.get("unread") or 0
        badge = f" 🔴{unread}" if unread else ""
        rows.append([InlineKeyboardButton(
            f"{_contact_name(t)}{badge}", callback_data=f"adm_supportthread_{t['user_id']}"
        )])
    rows.append(_nav_row("adm_support", page, total_pages))
    rows.append([_back_btn()])
    total_unread = sum((t.get("unread") or 0) for t in threads)
    header = f"💬 <b>Қолдау қызметі</b> ({len(threads)} чат{', 🔴' + str(total_unread) + ' жаңа' if total_unread else ''})"
    await _reply(update, header, InlineKeyboardMarkup(rows))


async def adm_support_thread_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    user_id = int(update.callback_query.data.split("_")[2])
    user = db.get_user_by_id(user_id)
    if not user:
        await _reply(update, "Пайдаланушы табылмады.", InlineKeyboardMarkup([[_back_btn("adm_support_0")]]))
        return
    db.mark_support_read(user_id)
    messages = db.get_support_messages(user_id)[-10:]
    lines = [f"💬 <b>{_contact_name(user)}</b> ({user.get('phone') or '—'})", ""]
    for m in messages:
        who = "👤" if m["direction"] == "in" else "🛠"
        lines.append(f"{who} {m['text']}")
    if not messages:
        lines.append("(хабарлама жоқ)")
    rows = [
        [InlineKeyboardButton("✍️ Жауап жазу", callback_data=f"adm_supportreply_{user_id}")],
        [_back_btn("adm_support_0")],
    ]
    await _reply(update, "\n".join(lines), InlineKeyboardMarkup(rows))


async def adm_support_reply_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    user_id = int(update.callback_query.data.split("_")[2])
    context.user_data["adm_wait"] = ("support_reply", user_id)
    await _reply(
        update, "✍️ Жауап мәтінін жазыңыз:",
        InlineKeyboardMarkup([[InlineKeyboardButton("Бас тарту", callback_data=f"adm_supportthread_{user_id}")]]),
    )


# ---------- ⚙️ Баптаулар ----------

async def adm_settings_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    current = db.get_commission_percent()
    total = db.get_total_commission()
    pending_count = len(db.get_pending_transactions())
    min_balance = db.get_min_balance()
    text = (
        f"⚙️ <b>Баптаулар</b>\n\n"
        f"💼 Комиссия: <b>{current:g}%</b>\n"
        f"💰 Жиналған комиссия: <b>{total} тг</b>\n"
        f"💳 Тапсырыс қабылдауға минималды баланс: <b>{min_balance:g} ₸</b>\n"
        f"📥 Күтудегі транзакциялар: {pending_count}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➖", callback_data="adm_comm_dec"),
            InlineKeyboardButton(f"{current:g}%", callback_data="adm_noop"),
            InlineKeyboardButton("➕", callback_data="adm_comm_inc"),
        ],
        [InlineKeyboardButton("✏️ Комиссияны дәл санмен жазу", callback_data="adm_settings_commission")],
        [InlineKeyboardButton(f"💳 Мин. баланс: {min_balance:g} ₸ (өзгерту)", callback_data="adm_settings_minbalance")],
        [InlineKeyboardButton("📥 Күтудегі транзакциялар", callback_data="adm_tx_0")],
        [_back_btn()],
    ])
    await _reply(update, text, kb)


async def adm_commission_nudge_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    delta = 1 if update.callback_query.data == "adm_comm_inc" else -1
    db.adjust_commission_percent(delta)
    await adm_settings_cb(update, context)


async def adm_settings_commission_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    context.user_data["adm_wait"] = ("commission", None)
    await _reply(
        update, "✏️ Жаңа комиссия пайызын санмен жазыңыз (мысалы: 10):",
        InlineKeyboardMarkup([[InlineKeyboardButton("Бас тарту", callback_data="adm_settings")]]),
    )


async def adm_settings_minbalance_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    context.user_data["adm_wait"] = ("min_balance", None)
    await _reply(
        update,
        "✏️ Тапсырыс қабылдауға қажет минималды баланс сомасын жазыңыз (мысалы: 500).\n"
        "Балансы осы соманан төмен жүргізуші Online бола алмайды және тапсырыс қабылдай алмайды.",
        InlineKeyboardMarkup([[InlineKeyboardButton("Бас тарту", callback_data="adm_settings")]]),
    )


# ---------- Мәтіндік енгізуді өңдеу (broadcast, tariff, т.б.) ----------

async def adm_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin_update(update):
        return
    wait = context.user_data.get("adm_wait")
    if not wait:
        return  # админнің қалыпты жазысуына кедергі жасамау үшін
    action, payload = wait
    text = (update.message.text or "").strip()
    context.user_data.pop("adm_wait", None)

    if action == "broadcast":
        recipients = db.get_broadcast_recipients(payload)
        sent, failed = 0, 0
        for tg_id in recipients:
            try:
                await context.bot.send_message(tg_id, text)
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(
            f"✅ Жіберілді: {sent} | ❌ Сәтсіз: {failed} | Барлығы: {len(recipients)}",
            reply_markup=main_menu_kb(),
        )

    elif action == "city_add":
        if db.add_city(text):
            await update.message.reply_text(f"✅ '{text}' қосылды.", reply_markup=main_menu_kb())
        else:
            await update.message.reply_text(f"⚠️ '{text}' бұрын қосылған.", reply_markup=main_menu_kb())

    elif action == "tariff":
        route = payload
        try:
            base, per_km = [p.strip() for p in text.split(",")]
            db.set_tariff(route, base, per_km)
            await update.message.reply_text(f"✅ '{route}' тарифі сақталды: {base}тг + {per_km}тг/км",
                                             reply_markup=main_menu_kb())
        except Exception:
            await update.message.reply_text("⚠️ Формат қате. Мысалы: 500,100", reply_markup=main_menu_kb())

    elif action == "commission":
        try:
            float(text)
            db.set_setting("commission_percent", text)
            await update.message.reply_text(f"✅ Комиссия {text}% етіп сақталды.", reply_markup=main_menu_kb())
        except ValueError:
            await update.message.reply_text("⚠️ Дұрыс сан жазыңыз.", reply_markup=main_menu_kb())

    elif action == "min_balance":
        try:
            val = float(text)
            db.set_min_balance(val)
            await update.message.reply_text(
                f"✅ Минималды баланс {val:g} ₸ етіп сақталды.", reply_markup=main_menu_kb()
            )
        except ValueError:
            await update.message.reply_text("⚠️ Дұрыс сан жазыңыз.", reply_markup=main_menu_kb())

    elif action == "support_reply":
        user_id = payload
        user = db.get_user_by_id(user_id)
        if user:
            db.add_support_message(user_id, "out", text)
            try:
                await context.bot.send_message(user["telegram_id"], f"💬 Әкімші: {text}")
                await update.message.reply_text("✅ Жауап жіберілді.", reply_markup=main_menu_kb())
            except Exception as e:
                await update.message.reply_text(f"⚠️ Telegram-ға жіберілмеді: {e}", reply_markup=main_menu_kb())
        else:
            await update.message.reply_text("⚠️ Пайдаланушы табылмады.", reply_markup=main_menu_kb())

    elif action == "driver_reject":
        user_id = payload
        user = db.get_user_by_id(user_id)
        if user:
            db.set_verification_status(user_id, "rejected", text)
            try:
                await context.bot.send_message(
                    user["telegram_id"],
                    f"❌ Құжаттарың қабылданбады. Себебі: {text}\n🔄 Деректерді түзетіп қайта тіркеле аласың.",
                )
            except Exception as e:
                logger.warning(f"notify reject failed: {e}")
            await update.message.reply_text(f"❌ {_contact_name(user)} қабылданбады. Себебі: {text}",
                                             reply_markup=main_menu_kb())
        else:
            await update.message.reply_text("⚠️ Пайдаланушы табылмады.", reply_markup=main_menu_kb())


# ---------- Тіркеу ----------

def register(app, admin_chat_id, contact_name_fn=None, driver_menu_fn=None):
    """bot.py-дің build_application() ішінен шақырылады."""
    global _ADMIN_CHAT_ID, _contact_name, _driver_menu
    _ADMIN_CHAT_ID = admin_chat_id
    if contact_name_fn:
        _contact_name = contact_name_fn
    if driver_menu_fn:
        _driver_menu = driver_menu_fn

    admin_only = filters.User(user_id=int(admin_chat_id)) if str(admin_chat_id).lstrip("-").isdigit() else filters.ALL

    app.add_handler(CommandHandler("admin", admin_start), group=-1)
    app.add_handler(CallbackQueryHandler(adm_main_cb, pattern="^adm_main$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_noop_cb, pattern="^adm_noop$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_stats_cb, pattern="^adm_stats$"), group=-1)

    app.add_handler(CallbackQueryHandler(adm_users_cb, pattern="^adm_users_\\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_user_action_cb, pattern="^adm_user_(block|unblock|grant|revoke)_\\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_user_delete_confirm_cb, pattern="^adm_user_delconfirm_\\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_user_delete_do_cb, pattern="^adm_user_deldo_\\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_user_detail_cb, pattern="^adm_user_\\d+$"), group=-1)

    app.add_handler(CallbackQueryHandler(adm_drivers_cb, pattern="^adm_drivers_\\d+_\\w+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_driver_docs_cb, pattern="^adm_driverdocs_\\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_driver_action_cb, pattern="^adm_driver_(approve|reject|toggleonline)_\\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_driver_detail_cb, pattern="^adm_driver_\\d+$"), group=-1)

    app.add_handler(CallbackQueryHandler(adm_rides_cb, pattern="^adm_rides_\\d+_\\w+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_ride_action_cb, pattern="^adm_ride_(complete|cancel)_\\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_ride_detail_cb, pattern="^adm_ride_\\d+$"), group=-1)

    app.add_handler(CallbackQueryHandler(adm_cities_cb, pattern="^adm_cities$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_city_action_cb, pattern="^adm_city_(add|toggle|delete)(_\\d+)?$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_tariff_set_cb, pattern="^adm_tariff_set_\\w+$"), group=-1)

    app.add_handler(CallbackQueryHandler(adm_broadcast_cb, pattern="^adm_broadcast$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_broadcast_audience_cb, pattern="^adm_bc_(all|client|driver)$"), group=-1)

    app.add_handler(CallbackQueryHandler(adm_tx_list_cb, pattern="^adm_tx_\\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_tx_approve_cb, pattern="^adm_tx_approve_\\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_tx_reject_cb, pattern="^adm_tx_reject_\\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_tx_view_cb, pattern="^adm_txview_\\d+$"), group=-1)

    app.add_handler(CallbackQueryHandler(adm_support_cb, pattern="^adm_support_\\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_support_thread_cb, pattern="^adm_supportthread_\\d+$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_support_reply_cb, pattern="^adm_supportreply_\\d+$"), group=-1)

    app.add_handler(CallbackQueryHandler(adm_settings_cb, pattern="^adm_settings$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_commission_nudge_cb, pattern="^adm_comm_(inc|dec)$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_settings_commission_cb, pattern="^adm_settings_commission$"), group=-1)
    app.add_handler(CallbackQueryHandler(adm_settings_minbalance_cb, pattern="^adm_settings_minbalance$"), group=-1)

    # Мәтіндік енгізу (broadcast мәтіні, тариф, комиссия, т.б.) — тек админ чатынан,
    # тек "adm_wait" орнатылған кезде әсер етеді, әйтпесе ештеңе істемейді.
    app.add_handler(MessageHandler(admin_only & filters.TEXT & ~filters.COMMAND, adm_text_input), group=-1)

    logger.info("Admin Telegram panel registered.")
