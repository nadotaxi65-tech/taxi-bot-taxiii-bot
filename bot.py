import logging
import os
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
import database as db
import admin_telegram

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
# Telegram numeric chat id of the admin — the bot sends intercity-access requests here.
# Get yours by messaging @userinfobot on Telegram.
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")


def is_admin_tg_id(telegram_id) -> bool:
    """Admin's own account is exempt from the commission wallet — unlimited balance."""
    return bool(ADMIN_CHAT_ID) and str(telegram_id) == str(ADMIN_CHAT_ID)

# Conversation states
(CHOOSE_ROLE, SHARE_PHONE, ID_PHOTO, CAR_BRAND, CAR_MODEL, CAR_PLATE, CAR_COLOR, CAR_YEAR, CAR_PHOTO,
 CAR_DOC_PHOTO, WORK_CITY, AGREE_RULES,
 ASK_ROUTE, PASSENGER_COUNT, ASK_ITEM, ASK_FROM, ASK_TO, ASK_TIME, ASK_PRICE,
 DECLINE_REASON, TRANSFER_PICK, OFFER_PRICE, SUPPORT_MSG,
 TOPUP_AMOUNT, WITHDRAW_AMOUNT) = range(25)

# ---------- Navigation: "back" / "main menu" buttons used across every multi-step flow ----------
BACK_BTN = "⬅️ Артқа"
MENU_BTN = "🏠 Бас мәзір"
NAV_MENU_CB = "navmenu"  # inline callback_data used on inline-keyboard steps


def nav_keyboard(extra_rows=None, include_back=True):
    """Reply keyboard used under text-input steps: optional extra rows (e.g. a location button)
    plus a Back / Main menu row at the bottom."""
    rows = [list(r) for r in (extra_rows or [])]
    nav_row = [BACK_BTN, MENU_BTN] if include_back else [MENU_BTN]
    rows.append(nav_row)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def with_menu_button(inline_keyboard_rows):
    """Adds a '🏠 Бас мәзір' row under an inline keyboard (list of lists of InlineKeyboardButton)."""
    rows = list(inline_keyboard_rows) + [[InlineKeyboardButton(MENU_BTN, callback_data=NAV_MENU_CB)]]
    return InlineKeyboardMarkup(rows)


async def go_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generic 'end whatever we were doing and show the main menu' handler."""
    context.user_data.clear()
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    if not user.get("role"):
        await update.effective_message.reply_text(
            "Сен кімсің?", reply_markup=ROLE_KEYBOARD if update.message else None
        )
        return ConversationHandler.END
    menu = CLIENT_MENU if user["role"] == "client" else driver_menu(user)
    await update.effective_message.reply_text("🏠 Бас мәзір", reply_markup=menu)
    return ConversationHandler.END


async def go_main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Same as go_main_menu but triggered from an inline button (callback query)."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    menu = CLIENT_MENU if user["role"] == "client" else driver_menu(user)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("🏠 Бас мәзір", reply_markup=menu)
    return ConversationHandler.END


ROLE_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🧍 Жолаушымын", callback_data="role_client")],
    [InlineKeyboardButton("🚕 Жүргізушімін", callback_data="role_driver")],
])

CLIENT_MENU = ReplyKeyboardMarkup(
    [["🚕 Такси шақыру"], ["📦 Жеткізу шақыру"], ["📋 Менің сұраныстарым"], ["🆘 Қолдау қызметі"], ["🚨 SOS"]],
    resize_keyboard=True,
)

def route_keyboard():
    """Built live from the admin's city list, so adding/removing a city/village
    route in the admin panel immediately shows up (or disappears) here."""
    rows = [[InlineKeyboardButton("🏙 Жанақала ішінде", callback_data="route_local")]]
    for c in db.get_cities():
        if c.get("is_active"):
            rows.append([InlineKeyboardButton(f"🛣 Жанақала — {c['name']}", callback_data=f"route_city_{c['id']}")])
    return with_menu_button(rows)


def route_label(route):
    if not route or route == "local":
        return "🏙 Жанақала ішінде"
    if route.startswith("city:"):
        return f"🛣 Жанақала — {route.split(':', 1)[1]}"
    return "🛣 Жанақала — Орал"  # legacy fallback for rides created before this update

SERVICE_LABEL = {"taxi": "🚕 Такси", "delivery": "📦 Жеткізу"}

DECLINE_REASON_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🚗 Алыс жол", callback_data="dreason_far")],
    [InlineKeyboardButton("💰 Баға төмен", callback_data="dreason_price")],
    [InlineKeyboardButton("🔧 Көлік ақаулы", callback_data="dreason_car")],
    [InlineKeyboardButton("❓ Басқа себеп", callback_data="dreason_other")],
])
DECLINE_REASON_TEXT = {
    "dreason_far": "Алыс жол", "dreason_price": "Баға төмен",
    "dreason_car": "Көлік ақаулы", "dreason_other": "Басқа себеп",
}


def driver_menu(user: dict) -> ReplyKeyboardMarkup:
    """Menu depends on verification stage. Online toggle is only shown once approved."""
    status = user.get("verification_status") or "none"
    if status == "pending":
        return ReplyKeyboardMarkup([["👤 Профилім"], ["🆘 Қолдау қызметі"]], resize_keyboard=True)
    if status == "rejected":
        return ReplyKeyboardMarkup([["🔄 Қайта тіркелу"], ["👤 Профилім"], ["🆘 Қолдау қызметі"]], resize_keyboard=True)
    if status != "approved":
        return ReplyKeyboardMarkup([["👤 Профилім"], ["🆘 Қолдау қызметі"]], resize_keyboard=True)

    toggle = "🔴 Offline болу" if user.get("is_online") else "🟢 Online болу"
    return ReplyKeyboardMarkup(
        [
            [toggle],
            ["📋 Ашық тапсырыстар", "📍 Жақын тапсырыстар"],
            ["💰 Баланс", "📊 Табысым"],
            ["💳 Баланс толтыру", "💸 Ақша шығару"],
            ["⭐ Рейтингім", "📜 Тарихым"],
            ["👤 Профилім", "🆘 Қолдау қызметі"],
            ["🚨 SOS"],
        ],
        resize_keyboard=True,
    )


def contact_name(user: dict) -> str:
    return user.get("phone") or (f"@{user['username']}" if user.get("username") else user.get("full_name") or "белгісіз")


def maps_link(lat, lon):
    if lat is None or lon is None:
        return None
    return f"https://maps.google.com/?q={lat},{lon}"


def rating_str(driver_id) -> str:
    r = db.get_driver_rating(driver_id)
    if r["count"] == 0:
        return "(әлі бағаланбаған)"
    return f"⭐{r['avg']} ({r['count']} баға)"


async def reject_if_blocked(update: Update, tg_user) -> bool:
    """Returns True (and replies) if the user is blocked, so the caller should stop."""
    if db.is_blocked(tg_user.id):
        await update.effective_message.reply_text("🚫 Сіз әкімші тарапынан бұғатталғансыз. Сұрақ болса, әкімшіге хабарласыңыз.")
        return True
    return False


# ---------- ONBOARDING ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if await reject_if_blocked(update, tg_user):
        return ConversationHandler.END
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    if user["role"] and user["phone"]:
        menu = CLIENT_MENU if user["role"] == "client" else driver_menu(user)
        await update.message.reply_text(
            f"Қайта қош келдің, {tg_user.first_name}! Жанақала такси боты жұмыс істеп тұр.",
            reply_markup=menu,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Сәлем! Бұл — Жанақала ауданының такси боты.\nСен кімсің?",
        reply_markup=ROLE_KEYBOARD,
    )
    return CHOOSE_ROLE


async def choose_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    role = "client" if query.data == "role_client" else "driver"
    db.set_role(update.effective_user.id, role)
    context.user_data["role"] = role

    phone_button = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Нөмірімді жіберу", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await query.message.reply_text(
        "Байланысу үшін телефон нөміріңді жібер:",
        reply_markup=phone_button,
    )
    return SHARE_PHONE


async def share_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    tg_user = update.effective_user
    if not contact:
        await update.message.reply_text("Өтінемін, төмендегі батырма арқылы нөміріңді жібер 📱")
        return SHARE_PHONE

    # --- anti-fraud: one phone number = one account ---
    duplicate = db.find_user_by_phone(contact.phone_number, exclude_telegram_id=tg_user.id)
    if duplicate:
        db.set_phone(tg_user.id, contact.phone_number)
        db.set_fraud_flag(tg_user.id, True)
        db.set_blocked(db.get_or_create_user(tg_user.id).get("id"), True)
        await update.message.reply_text(
            "🚫 Бұл телефон нөмірі бұрын басқа аккаунтта тіркелген.\n"
            "Бір адам тек бір ғана аккаунт аша алады, сондықтан тіркелу тоқтатылды.\n"
            "Егер бұл қате деп есептесеңіз, әкімшіге хабарласыңыз.",
            reply_markup=ReplyKeyboardRemove(),
        )
        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(
                    ADMIN_CHAT_ID,
                    "🚨 <b>Антифрод: қайталанған телефон</b>\n"
                    f"Жаңа аккаунт: {contact_name(db.get_or_create_user(tg_user.id))} (TG ID: <code>{tg_user.id}</code>)\n"
                    f"Бұрынғы аккаунт: {contact_name(duplicate)} (TG ID: <code>{duplicate['telegram_id']}</code>)\n"
                    f"Телефон: {contact.phone_number}\n"
                    "Жаңа аккаунт автоматты бұғатталды. Керек болса, әкімші панелінен қолмен блокты алыңыз.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"antifraud notify failed: {e}")
        return ConversationHandler.END

    db.set_phone(tg_user.id, contact.phone_number)
    role = context.user_data.get("role")
    if role == "driver":
        await update.message.reply_text(
            "🪪 Алдымен жеке куәлігіңнің фотосын жібер (алдыңғы беті):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ID_PHOTO

    await update.message.reply_text("Дайын! Енді мәзірді қолдана аласың.", reply_markup=CLIENT_MENU)
    return ConversationHandler.END


async def ask_id_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Өтінемін, жеке куәліктің фотосын сурет ретінде жібер 🪪")
        return ID_PHOTO
    db.set_id_photo(update.effective_user.id, update.message.photo[-1].file_id)
    await update.message.reply_text(
        "Енді көлігіңді тіркейік. " + VEHICLE_PROMPTS[CAR_BRAND],
        reply_markup=nav_keyboard(include_back=False),
    )
    return CAR_BRAND


# ---------- DRIVER: vehicle registration (required before verification) ----------

# Prompt text for each vehicle-registration step, keyed by the state it leads INTO.
# Used both when moving forward and when the driver taps "⬅️ Артқа" to redo a step.
VEHICLE_PROMPTS = {
    CAR_BRAND: "🚗 Көлік маркасын жаз (мыс. Toyota):",
    CAR_MODEL: "🚘 Көлік моделін жаз (мыс. Camry):",
    CAR_PLATE: "🔢 Мемлекеттік нөмірін жаз (мыс. 123ABC02):",
    CAR_COLOR: "🎨 Көліктің түсін жаз:",
    CAR_YEAR: "📅 Көліктің шығарылған жылын жаз:",
    CAR_PHOTO: "📸 Соңында көліктің фотосын жібер:",
}
# What state to go back to from each state
VEHICLE_PREV_STATE = {
    CAR_MODEL: CAR_BRAND, CAR_PLATE: CAR_MODEL, CAR_COLOR: CAR_PLATE,
    CAR_YEAR: CAR_COLOR, CAR_PHOTO: CAR_YEAR,
}


async def reregister_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    if user["role"] != "driver":
        return ConversationHandler.END
    context.user_data["role"] = "driver"
    await update.message.reply_text(
        "Көлік деректерін қайта толтырайық. " + VEHICLE_PROMPTS[CAR_BRAND],
        reply_markup=nav_keyboard(include_back=False),
    )
    return CAR_BRAND


def make_vehicle_back_handler(target_state):
    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            VEHICLE_PROMPTS[target_state],
            reply_markup=nav_keyboard(include_back=(target_state != CAR_BRAND)),
        )
        return target_state
    return _handler


async def ask_car_brand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["car_brand"] = update.message.text
    await update.message.reply_text(VEHICLE_PROMPTS[CAR_MODEL], reply_markup=nav_keyboard())
    return CAR_MODEL


async def ask_car_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["car_model"] = update.message.text
    await update.message.reply_text(VEHICLE_PROMPTS[CAR_PLATE], reply_markup=nav_keyboard())
    return CAR_PLATE


async def ask_car_plate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["car_plate"] = update.message.text
    await update.message.reply_text(VEHICLE_PROMPTS[CAR_COLOR], reply_markup=nav_keyboard())
    return CAR_COLOR


async def ask_car_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["car_color"] = update.message.text
    await update.message.reply_text(VEHICLE_PROMPTS[CAR_YEAR], reply_markup=nav_keyboard())
    return CAR_YEAR


async def ask_car_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["car_year"] = update.message.text
    await update.message.reply_text(VEHICLE_PROMPTS[CAR_PHOTO], reply_markup=nav_keyboard())
    return CAR_PHOTO


async def ask_car_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Өтінемін, көліктің фотосын сурет ретінде жібер 📸", reply_markup=nav_keyboard())
        return CAR_PHOTO
    photo_file_id = update.message.photo[-1].file_id
    tg_user = update.effective_user

    db.set_driver_vehicle(
        tg_user.id,
        brand=context.user_data.get("car_brand"),
        model=context.user_data.get("car_model"),
        plate=context.user_data.get("car_plate"),
        color=context.user_data.get("car_color"),
        year=context.user_data.get("car_year"),
        photo=photo_file_id,
    )
    await update.message.reply_text(
        "📄 Енді көліктің тіркеу куәлігінің (техпаспорт) фотосын жібер:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return CAR_DOC_PHOTO


async def ask_car_doc_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Өтінемін, көлік құжатының фотосын сурет ретінде жібер 📄")
        return CAR_DOC_PHOTO
    db.set_car_doc_photo(update.effective_user.id, update.message.photo[-1].file_id)

    cities = [c for c in db.get_cities() if c.get("is_active")]
    if cities:
        rows = [[InlineKeyboardButton(c["name"], callback_data=f"workcity_{c['id']}")] for c in cities]
        await update.message.reply_text(
            "📍 Қай қалада/ауданда жұмыс істейсің?",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return WORK_CITY
    else:
        # cities тізімі бос болса, осы қадамды өткізіп жібереміз
        return await _ask_agree_rules(update, context)


async def choose_work_city_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    city_id = int(query.data.split("_")[1])
    city = next((c for c in db.get_cities() if c["id"] == city_id), None)
    if city:
        db.set_work_city(update.effective_user.id, city["name"])
    return await _ask_agree_rules(update, context)


async def _ask_agree_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Келісемін", callback_data="agree_rules_yes")]])
    text = (
        "📜 <b>Жүргізуші ережесі</b>\n\n"
        "• Жолаушыларға сыпайы әрі қауіпсіз қызмет көрсетемін.\n"
        "• Көлігім тексеруден өткен, техникалық жағдайы дұрыс.\n"
        "• Тапсырыс деректерін жалған көрсетпеймін.\n"
        "• Әкімшінің талаптарын сақтаймын.\n\n"
        "Жалғастыру үшін ереже мен келісіміңді растаңыз:"
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    return AGREE_RULES


async def agree_rules_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_user = update.effective_user

    db.finalize_driver_registration(tg_user.id)
    user = db.get_user_by_telegram_id(tg_user.id)

    await query.message.reply_text(
        "🟡 Верификацияға өтінім қабылданды.\n"
        "Администратор құжаттарыңызды тексергеннен кейін сізге хабарлама келеді.\n"
        "⏳ Статус: Тексерілуде\n"
        "🔒 Растаудан өтпейінше Online режиміне кіре алмайсың.",
        reply_markup=driver_menu(user),
    )

    if ADMIN_CHAT_ID:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Растау", callback_data=f"vapprove_{user['id']}"),
            InlineKeyboardButton("❌ Қабылдамау", callback_data=f"vreject_{user['id']}"),
        ]])
        caption = (
            f"🟡 Жаңа жүргізуші верификациясы\n"
            f"👤 {contact_name(user)}\n"
            f"📱 {user.get('phone') or '—'}\n"
            f"📍 Қала: {user.get('work_city') or '—'}\n"
            f"🚗 {db.vehicle_str(user)}"
        )
        try:
            if user.get("id_photo"):
                await context.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=user["id_photo"], caption="🪪 Жеке куәлік")
            if user.get("car_doc_photo"):
                await context.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=user["car_doc_photo"], caption="📄 Көлік құжаты")
            await context.bot.send_photo(
                chat_id=ADMIN_CHAT_ID, photo=user["car_photo"], caption=caption, reply_markup=kb,
            )
        except Exception as e:
            logger.warning(f"Could not notify admin of new driver: {e}")
    else:
        logger.warning("ADMIN_CHAT_ID орнатылмаған — жүргізуші верификациясы ешкімге жіберілмеді.")

    return ConversationHandler.END


# ---------- DRIVER: online/offline ----------

async def toggle_online(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if await reject_if_blocked(update, tg_user):
        return
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)

    if user.get("verification_status") != "approved":
        await update.message.reply_text(
            "🔒 Расталмайынша Online режиміне кіре алмайсың. Әкімші тексеруін күте тұр.",
            reply_markup=driver_menu(user),
        )
        return

    if not user.get("is_online") and not is_admin_tg_id(tg_user.id) and db.get_balance(user["id"]) < db.get_min_balance():
        await update.message.reply_text(
            f"⚠️ Балансыңыз жеткіліксіз ({db.get_balance(user['id'])} ₸).\n"
            f"Online режиміне кіру және тапсырыс қабылдау үшін балансыңызда кемінде "
            f"{db.get_min_balance():g} ₸ болуы керек.\n"
            "Алдымен балансты толтырыңыз: 💳 Баланс толтыру.",
            reply_markup=driver_menu(user),
        )
        return

    if not user.get("is_online"):
        rating = db.get_driver_rating(user["id"])
        if rating["count"] >= 5 and rating["avg"] < 3.0:
            await update.message.reply_text(
                f"⚠️ Рейтингіңіз төмен ({rating['avg']}⭐, {rating['count']} баға), "
                "сондықтан уақытша Online режиміне кіре алмайсың. Әкімшіге хабарласыңыз.",
                reply_markup=driver_menu(user),
            )
            if ADMIN_CHAT_ID:
                try:
                    await context.bot.send_message(
                        ADMIN_CHAT_ID,
                        f"⚠️ Төмен рейтингті жүргізуші online болуға тырысты: {contact_name(user)} "
                        f"({rating['avg']}⭐, {rating['count']} баға). Тексеру қажет болуы мүмкін.",
                    )
                except Exception:
                    pass
            return

    new_status = not bool(user["is_online"])
    db.set_online(tg_user.id, new_status)
    user["is_online"] = 1 if new_status else 0
    if new_status:
        await update.message.reply_text(
            "🟢 Сен онлайнсың! Жаңа тапсырыстар келгенде хабарлаймын.",
            reply_markup=driver_menu(user),
        )
    else:
        await update.message.reply_text(
            "🔴 Сен офлайнсың. Жаңа тапсырыстар келмейді.",
            reply_markup=driver_menu(user),
        )


# ---------- CLIENT: create a ride request ----------


async def new_ride_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_blocked(update, update.effective_user):
        return ConversationHandler.END
    context.user_data.clear()
    service_type = "delivery" if "Жеткізу" in update.message.text else "taxi"
    context.user_data["service_type"] = service_type
    await update.message.reply_text(
        "Бағытты таңда:",
        reply_markup=route_keyboard(),
    )
    return ASK_ROUTE


async def back_to_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бағытты таңда:", reply_markup=route_keyboard())
    return ASK_ROUTE


async def choose_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_user = update.effective_user

    if query.data.startswith("route_city_") and not db.has_intercity_access(tg_user.id):
        city_id = int(query.data.split("_")[2])
        city = next((c for c in db.get_cities() if c["id"] == city_id), None)
        city_name = city["name"] if city else "қалааралық"
        user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
        context.user_data["pending_route_city_id"] = city_id
        await query.edit_message_text(
            f"🛣 'Жанақала — {city_name}' бағыты бойынша тапсырыс беру үшін әкімшінің рұқсаты керек.\n"
            "Сұранысыңызды әкімшіге жібердім, растаған соң хабарласамын."
        )
        if ADMIN_CHAT_ID:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Рұқсат беру", callback_data=f"icgrant_{tg_user.id}"),
                InlineKeyboardButton("❌ Бас тарту", callback_data=f"icdeny_{tg_user.id}"),
            ]])
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"🔔 {contact_name(user)} 'Жанақала — {city_name}' бағытына рұқсат сұрап тұр.\n"
                        f"Қызмет түрі: {SERVICE_LABEL.get(context.user_data.get('service_type'), '-')}"
                    ),
                    reply_markup=kb,
                )
            except Exception as e:
                logger.warning(f"Could not notify admin: {e}")
        else:
            logger.warning("ADMIN_CHAT_ID орнатылмаған — intercity сұранысы ешкімге жіберілмеді.")
        return ConversationHandler.END

    if query.data.startswith("route_city_"):
        city_id = int(query.data.split("_")[2])
        city = next((c for c in db.get_cities() if c["id"] == city_id), None)
        context.user_data["route"] = f"city:{city['name'] if city else 'қала'}"
    else:
        context.user_data["route"] = "local"

    if context.user_data.get("service_type") == "delivery":
        await query.edit_message_text("Бағыт таңдалды.")
        await query.message.reply_text(ASK_ITEM_PROMPT, reply_markup=nav_keyboard())
        return ASK_ITEM

    await query.edit_message_text("Бағыт таңдалды.")
    await query.message.reply_text("👥 Неше адамсыздар?", reply_markup=PASSENGER_COUNT_KEYBOARD)
    return PASSENGER_COUNT


PASSENGER_COUNT_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton(str(n), callback_data=f"pax_{n}") for n in (1, 2, 3, 4)
]])


async def choose_passenger_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["passenger_count"] = int(query.data.split("_")[1])
    await query.edit_message_text(f"👥 Адам саны: {context.user_data['passenger_count']}")
    await query.message.reply_text(
        ASK_FROM_PROMPT,
        reply_markup=nav_keyboard(extra_rows=[[KeyboardButton("📍 Локация жіберу", request_location=True)]]),
    )
    return ASK_FROM



ASK_FROM_PROMPT = "Қайдан шығасың? 📍 Локацияңды жібер немесе мекенжайды өзің жаз (мыс. Жанақала, орталық):"
ASK_FROM_ITEM_PROMPT = "Затты қайдан алу керек? 📍 Локацияңды жібер немесе мекенжайды өзің жаз:"
ASK_TO_PROMPT = "Қайда барасың? 📍 Локация жіберуге болады немесе мекенжайды жаз:"
ASK_TIME_PROMPT = "Қай уақытта? (мыс. бүгін 14:00)"
ASK_ITEM_PROMPT = "Не жеткізу керек? (қысқаша сипаттама жаз)"


def _from_prompt(context):
    return ASK_FROM_ITEM_PROMPT if context.user_data.get("service_type") == "delivery" else ASK_FROM_PROMPT


async def back_to_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(ASK_ITEM_PROMPT, reply_markup=nav_keyboard())
    return ASK_ITEM


async def back_from_ask_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """From ASK_FROM, 'back' goes to ASK_ITEM for delivery orders, or to route choice for taxi."""
    if context.user_data.get("service_type") == "delivery":
        return await back_to_item(update, context)
    return await back_to_route(update, context)


async def back_to_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        _from_prompt(context),
        reply_markup=nav_keyboard(extra_rows=[[KeyboardButton("📍 Локация жіберу", request_location=True)]]),
    )
    return ASK_FROM


async def back_to_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        ASK_TO_PROMPT,
        reply_markup=nav_keyboard(extra_rows=[[KeyboardButton("📍 Локация жіберу", request_location=True)]]),
    )
    return ASK_TO


async def back_to_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(ASK_TIME_PROMPT, reply_markup=nav_keyboard())
    return ASK_TIME


async def ask_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["item_note"] = update.message.text
    await update.message.reply_text(
        ASK_FROM_ITEM_PROMPT,
        reply_markup=nav_keyboard(extra_rows=[[KeyboardButton("📍 Локация жіберу", request_location=True)]]),
    )
    return ASK_FROM


async def ask_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.location:
        loc = update.message.location
        context.user_data["from"] = "📍 Локация (карта арқылы)"
        context.user_data["from_lat"] = loc.latitude
        context.user_data["from_lon"] = loc.longitude
    else:
        context.user_data["from"] = update.message.text
        context.user_data["from_lat"] = None
        context.user_data["from_lon"] = None
    await update.message.reply_text(
        ASK_TO_PROMPT,
        reply_markup=nav_keyboard(extra_rows=[[KeyboardButton("📍 Локация жіберу", request_location=True)]]),
    )
    return ASK_TO


async def ask_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.location:
        loc = update.message.location
        context.user_data["to"] = "📍 Локация (карта арқылы)"
        context.user_data["to_lat"] = loc.latitude
        context.user_data["to_lon"] = loc.longitude
    else:
        context.user_data["to"] = update.message.text
        context.user_data["to_lat"] = None
        context.user_data["to_lon"] = None
    await update.message.reply_text(ASK_TIME_PROMPT, reply_markup=nav_keyboard())
    return ASK_TIME


async def ask_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["time"] = update.message.text

    # Fare estimate, if the admin has set a tariff for this route and we know the distance.
    price_prompt = "Жол ақысын өзің жаз (теңге):"
    from_lat, from_lon = context.user_data.get("from_lat"), context.user_data.get("from_lon")
    to_lat, to_lon = context.user_data.get("to_lat"), context.user_data.get("to_lon")
    if None not in (from_lat, from_lon, to_lat, to_lon):
        distance_km = round(db._haversine_km(from_lat, from_lon, to_lat, to_lon), 1)
        estimate = db.estimate_price(context.user_data.get("route", "local"), distance_km)
        if estimate:
            price_prompt = (
                f"📏 Қашықтық: ~{distance_km} км\n"
                f"💡 Ұсынылатын баға: ~{estimate} тг\n\n"
                f"Жол ақысын жаз (теңге), ұсынылған бағаны қолдансаң, дәл сол санды жаз:"
            )
    await update.message.reply_text(price_prompt, reply_markup=nav_keyboard())
    return ASK_PRICE


async def ask_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["price"] = update.message.text
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)

    service_type = context.user_data.get("service_type", "taxi")
    route = context.user_data.get("route", "local")
    item_note = context.user_data.get("item_note")
    passenger_count = context.user_data.get("passenger_count", 1)

    ride_id = db.create_ride(
        client_id=user["id"],
        from_location=context.user_data["from"],
        to_location=context.user_data["to"],
        ride_time=context.user_data["time"],
        price=context.user_data["price"],
        from_lat=context.user_data.get("from_lat"),
        from_lon=context.user_data.get("from_lon"),
        to_lat=context.user_data.get("to_lat"),
        to_lon=context.user_data.get("to_lon"),
        service_type=service_type,
        route=route,
        item_note=item_note,
        passenger_count=passenger_count,
    )
    from_link = maps_link(context.user_data.get("from_lat"), context.user_data.get("from_lon"))
    to_link = maps_link(context.user_data.get("to_lat"), context.user_data.get("to_lon"))

    header = f"{SERVICE_LABEL.get(service_type)} | {route_label(route)}"
    item_line = f"\n📦 Не: {item_note}" if item_note else ""
    pax_line = f"\n👥 Адам саны: {passenger_count}" if service_type == "taxi" else ""

    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Болдырмау", callback_data=f"clientcancel_{ride_id}")]])
    await update.message.reply_text(
        f"✅ Тапсырыс жасалды (№{ride_id})! {header}{item_line}{pax_line}\n"
        f"{context.user_data['from']} → {context.user_data['to']}\n"
        f"Уақыты: {context.user_data['time']}\n"
        f"Бағасы: {context.user_data['price']} тг\n\n"
        f"Онлайн жүргізушілерге жіберілді, жауап күтеміз.",
        reply_markup=CLIENT_MENU,
    )
    await update.message.reply_text("Тапсырысты болдырмау керек болса:", reply_markup=cancel_kb)

    # broadcast to all online drivers
    driver_ids = db.get_online_driver_telegram_ids()
    text = (
        f"🆕 Жаңа тапсырыс №{ride_id} — {header}{item_line}{pax_line}\n"
        f"{context.user_data['from']} → {context.user_data['to']}\n"
        f"Уақыты: {context.user_data['time']}\n"
        f"Бағасы: {context.user_data['price']} тг"
    )
    if from_link:
        text += f"\n📍 Қайдан алу керек: {from_link}"
    if to_link:
        text += f"\n🏁 Бару керек жер: {to_link}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Қабылдау", callback_data=f"accept_{ride_id}")]])
    for driver_tg_id in driver_ids:
        try:
            await context.bot.send_message(chat_id=driver_tg_id, text=text, reply_markup=kb)
        except Exception as e:
            logger.warning(f"Could not notify driver {driver_tg_id}: {e}")

    return ConversationHandler.END


async def my_rides(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    rides = [r for r in db.get_all_rides() if r["client_id"] == user["id"]]
    if not rides:
        await update.message.reply_text("Сенде әлі тапсырыс жоқ.")
        return
    status_names = {
        "open": "🕓 күтуде", "accepted": "🚕 жолда", "arrived": "📍 жетті",
        "completed": "🏁 аяқталды", "cancelled": "❌ болдырылмады",
    }
    lines = []
    for r in rides[:10]:
        tag = SERVICE_LABEL.get(r["service_type"], "🚕 Такси")
        if r.get("route") and r.get("route") != "local":
            tag += f" ({route_label(r['route']).split('— ')[-1]})"
        line = f"№{r['id']} {tag} {r['from_location']} → {r['to_location']} | {r['price']}тг | {status_names.get(r['status'], r['status'])}"
        if r["status"] == "completed" and r["rating"]:
            line += f" | {'⭐' * r['rating']}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines))


async def client_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ride_id = int(query.data.split("_")[1])
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)

    success = db.cancel_ride_by_client(ride_id, user["id"])
    if not success:
        await query.edit_message_text("Бұл тапсырысты болдырмау мүмкін емес (жол басталып кетуі мүмкін).")
        return
    await query.edit_message_text(f"❌ №{ride_id} тапсырысы болдырылмады.")

    ride = db.get_ride(ride_id)
    if ride.get("driver_id"):
        driver = db.get_user_by_id(ride["driver_id"])
        try:
            await context.bot.send_message(chat_id=driver["telegram_id"], text=f"⚠️ №{ride_id} тапсырысын жолаушы болдырмады.")
        except Exception as e:
            logger.warning(f"Could not notify driver of cancel: {e}")

    for other_id in db.cancel_offers_for_ride(ride_id):
        other_driver = db.get_user_by_id(other_id)
        if other_driver:
            try:
                await context.bot.send_message(other_driver["telegram_id"], f"❌ №{ride_id} тапсырысын жолаушы болдырмады.")
            except Exception:
                pass


# ---------- DRIVER: browse, accept, and progress an order ----------

def _verified_online_guard_ok(user: dict) -> bool:
    return user.get("verification_status") == "approved"


def _ride_card_text(r: dict, distance_km=None) -> str:
    tag = f"{SERVICE_LABEL.get(r['service_type'], '🚕 Такси')} | {route_label(r.get('route'))}"
    item_line = f"\n📦 Не: {r['item_note']}" if r.get("item_note") else ""
    dist_line = f"\n📏 Қашықтық: ~{distance_km} км" if distance_km is not None else ""
    text = (
        f"№{r['id']} — {tag}{item_line}{dist_line}\n"
        f"{r['from_location']} → {r['to_location']}\n"
        f"Уақыты: {r['ride_time']}\n"
        f"Бағасы: {r['price']} тг"
    )
    from_link = maps_link(r.get("from_lat"), r.get("from_lon"))
    to_link = maps_link(r.get("to_lat"), r.get("to_lon"))
    if from_link:
        text += f"\n📍 Қайдан алу керек: {from_link}"
    if to_link:
        text += f"\n🏁 Бару керек жер: {to_link}"
    return text


async def open_rides(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if await reject_if_blocked(update, tg_user):
        return
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    if not _verified_online_guard_ok(user):
        await update.message.reply_text("🔒 Расталмайынша Online режиміне кіре алмайсың.")
        return
    if not user["is_online"]:
        await update.message.reply_text("Алдымен 🟢 Online болу керексің.")
        return

    active = db.get_driver_active_ride(user["id"])
    if active:
        await update.message.reply_text(f"Сенде әлі аяқталмаған тапсырыс бар: №{active['id']}. Алдымен соны аяқта.")
        return

    rides = db.get_open_rides()
    if not rides:
        await update.message.reply_text("Қазір ашық тапсырыс жоқ.")
        return
    for r in rides:
        text = _ride_card_text(r)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Қабылдау", callback_data=f"accept_{r['id']}"),
            InlineKeyboardButton("💰 Баға ұсыну", callback_data=f"offer_{r['id']}"),
        ]])
        await update.message.reply_text(text, reply_markup=keyboard)


async def nearby_rides(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """3rd-stage feature: sort open orders by distance from driver's last known location."""
    tg_user = update.effective_user
    if await reject_if_blocked(update, tg_user):
        return
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    if not _verified_online_guard_ok(user):
        await update.message.reply_text("🔒 Расталмайынша Online режиміне кіре алмайсың.")
        return
    if not user["is_online"]:
        await update.message.reply_text("Алдымен 🟢 Online болу керексің.")
        return

    if user.get("last_lat") is None:
        await update.message.reply_text(
            "Ең жақын тапсырыстарды көру үшін алдымен өз локацияңды жібер 📍",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("📍 Локациямды жіберу", request_location=True)]],
                resize_keyboard=True, one_time_keyboard=True,
            ),
        )
        return

    rides = db.get_open_rides_near(user["last_lat"], user["last_lon"])
    if not rides:
        await update.message.reply_text("Қазір ашық тапсырыс жоқ.")
        return
    for r in rides:
        text = _ride_card_text(r, distance_km=r.get("_distance_km"))
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Қабылдау", callback_data=f"accept_{r['id']}"),
            InlineKeyboardButton("💰 Баға ұсыну", callback_data=f"offer_{r['id']}"),
        ]])
        await update.message.reply_text(text, reply_markup=keyboard)


async def driver_location_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles a plain (or live) location message from a driver -> stores it and, if there's
    an active ride, forwards it to the client so they can track the driver (live map)."""
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    if user["role"] != "driver":
        return
    loc = update.message.location
    db.set_driver_location(tg_user.id, loc.latitude, loc.longitude)

    active = db.get_driver_active_ride(user["id"])
    if active:
        client = db.get_user_by_id(active["client_id"])
        try:
            await context.bot.send_location(chat_id=client["telegram_id"], latitude=loc.latitude, longitude=loc.longitude)
            await context.bot.send_message(chat_id=client["telegram_id"], text=f"🗺 Жүргізушінің №{active['id']} тапсырысы бойынша ағымдағы орны жіберілді.")
        except Exception as e:
            logger.warning(f"Could not forward driver location: {e}")
    else:
        await update.message.reply_text("📍 Локация сақталды. Жақын тапсырыстарды енді көре аласың.")


async def driver_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    status_label = {
        "none": "Тіркелмеген", "pending": "🟡 Верификация күтуде",
        "approved": "✅ Расталған", "rejected": "❌ Қабылданбады",
    }.get(user.get("verification_status") or "none", "-")
    rating = db.get_driver_rating(user["id"])
    text = (
        f"👤 Профиль\n"
        f"Аты: {user.get('full_name') or '-'}\n"
        f"📱 Телефон: {user.get('phone') or '-'}\n"
        f"🚗 Көлік: {db.vehicle_str(user)}\n"
        f"Статус: {status_label}\n"
    )
    if user.get("verification_status") == "rejected" and user.get("reject_reason"):
        text += f"Себебі: {user['reject_reason']}\n"
    if rating["count"]:
        text += f"⭐ Рейтинг: {rating['avg']} ({rating['count']} баға)\n"
    await update.message.reply_text(text, reply_markup=driver_menu(user))


async def driver_rating_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    rating = db.get_driver_rating(user["id"])
    if not rating["count"]:
        await update.message.reply_text("Сенде әлі баға жоқ.")
        return
    await update.message.reply_text(f"⭐ Орташа рейтинг: {rating['avg']} ({rating['count']} баға негізінде)")


def _build_driver_accept_view(ride, client):
    ride_id = ride["id"]
    from_link = maps_link(ride.get("from_lat"), ride.get("from_lon"))
    accept_text = (
        f"✅ №{ride_id} тапсырысын алдың.\n"
        f"👤 Жолаушы: {client.get('full_name') or '-'}\n"
        f"📱 Телефон: {client.get('phone') or '-'}\n"
        f"{ride['from_location']} → {ride['to_location']}\n\n"
        f"💬 Жазба/дауыс хабарлама жіберсең, жолаушыға анонимді түрде жетеді.\n"
        f"Жолаушыны алуға жеткенде батырманы бас:"
    )
    if from_link:
        accept_text = f"📍 Алу орны: {from_link}\n\n" + accept_text

    driver_buttons = [[InlineKeyboardButton("📍 Жеттім", callback_data=f"arrived_{ride_id}")]]
    action_row = []
    if client.get("phone"):
        action_row.append(InlineKeyboardButton("📞 Жолаушыға қоңырау", url=f"tel:{client['phone']}"))
    if from_link:
        action_row.append(InlineKeyboardButton("🗺 Навигация", url=from_link))
    if action_row:
        driver_buttons.insert(0, action_row)
    driver_buttons.append([
        InlineKeyboardButton("❌ Бас тарту", callback_data=f"decline_{ride_id}"),
        InlineKeyboardButton("🔄 Басқаға беру", callback_data=f"transfer_{ride_id}"),
    ])
    return accept_text, InlineKeyboardMarkup(driver_buttons)


def _build_client_accept_view(ride, driver):
    ride_id = ride["id"]
    to_link = maps_link(ride.get("to_lat"), ride.get("to_lon"))
    client_text = (
        f"🚕 №{ride_id} тапсырысыңызды жүргізуші қабылдады!\n"
        f"👤 Жүргізуші: {driver.get('full_name') or '-'} {rating_str(driver['id'])}\n"
        f"📱 Телефон: {driver.get('phone') or '-'}\n"
        f"🚗 Көлік: {db.vehicle_str(driver)}\n"
        f"💬 Жазба/дауыс хабарлама жіберсеңіз, жүргізушіге анонимді түрде жетеді.\n"
        f"Жолда келе жатыр."
    )
    client_buttons = []
    if driver.get("phone"):
        client_buttons.append(InlineKeyboardButton("📞 Жүргізушіге қоңырау", url=f"tel:{driver['phone']}"))
    if to_link:
        client_buttons.append(InlineKeyboardButton("🗺 Навигация (бару керек жер)", url=to_link))
    return client_text, (InlineKeyboardMarkup([client_buttons]) if client_buttons else None)


async def _finalize_ride_acceptance(context, ride_id, driver, client, ride):
    """Shared by direct-accept and offer-accept: sends the driver + client their
    'ride confirmed' views, plus the pickup location pin and car photo."""
    client_text, client_kb = _build_client_accept_view(ride, driver)
    try:
        await context.bot.send_message(chat_id=client["telegram_id"], text=client_text, reply_markup=client_kb)
        if driver.get("car_photo"):
            await context.bot.send_photo(chat_id=client["telegram_id"], photo=driver["car_photo"], caption="🚗 Жүргізушінің көлігі")
    except Exception as e:
        logger.warning(f"Could not notify client: {e}")

    if ride.get("from_lat") is not None and ride.get("from_lon") is not None:
        try:
            await context.bot.send_location(chat_id=driver["telegram_id"], latitude=ride["from_lat"], longitude=ride["from_lon"])
        except Exception as e:
            logger.warning(f"Could not send location pin: {e}")


async def accept_ride_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ride_id = int(query.data.split("_")[1])

    tg_user = update.effective_user
    if db.is_blocked(tg_user.id):
        await query.edit_message_text("🚫 Сіз әкімші тарапынан бұғатталғансыз.")
        return
    driver = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)

    if not _verified_online_guard_ok(driver):
        await query.edit_message_text("🔒 Расталмайынша тапсырыс қабылдай алмайсың.")
        return

    if not driver["is_online"]:
        await query.edit_message_text("Алдымен 🟢 Online болу керексің.")
        return

    if not is_admin_tg_id(tg_user.id) and db.get_balance(driver["id"]) < db.get_min_balance():
        await query.edit_message_text(
            f"⚠️ Балансыңыз жеткіліксіз ({db.get_balance(driver['id'])} ₸).\n"
            f"Тапсырыс қабылдау үшін балансыңызда кемінде {db.get_min_balance():g} ₸ болуы керек.\n"
            "Алдымен балансты толтырыңыз: 💳 Баланс толтыру."
        )
        return

    active = db.get_driver_active_ride(driver["id"])
    if active:
        await query.edit_message_text(f"Сенде әлі аяқталмаған тапсырыс бар: №{active['id']}.")
        return

    success = db.accept_ride(ride_id, driver["id"])
    if not success:
        await query.edit_message_text("Кешіріңіз, бұл тапсырысты басқа жүргізуші алып қойды.")
        return

    ride = db.get_ride(ride_id)
    client = db.get_user_by_id(ride["client_id"])

    accept_text, driver_kb = _build_driver_accept_view(ride, client)
    await query.edit_message_text(accept_text, reply_markup=driver_kb)
    await _finalize_ride_acceptance(context, ride_id, driver, client, ride)

    # Any other driver who had made a counter-offer on this ride needs to know it's gone.
    other_driver_ids = db.cancel_offers_for_ride(ride_id)
    for other_id in other_driver_ids:
        other_driver = db.get_user_by_id(other_id)
        if other_driver:
            try:
                await context.bot.send_message(
                    other_driver["telegram_id"], f"❌ №{ride_id} тапсырысы басқа жүргізушіге берілді."
                )
            except Exception:
                pass


RESERVED_MENU_TEXTS = {
    "🚕 Такси шақыру", "📦 Жеткізу шақыру", "🆘 Қолдау қызметі", "📋 Менің сұраныстарым",
    "💳 Баланс толтыру", "💸 Ақша шығару", "📋 Ашық тапсырыстар", "📍 Жақын тапсырыстар",
    "🟢 Online болу", "🔴 Offline болу", "📊 Табысым", "💰 Баланс", "📜 Тарихым",
    "👤 Профилім", "⭐ Рейтингім", "🚨 SOS", BACK_BTN, MENU_BTN,
}


def _active_ride_and_partner(user):
    """Finds the user's current accepted/arrived ride and who the 'other side' is,
    whichever role (client or driver) the user currently has."""
    ride = db.get_client_active_ride(user["id"])
    if ride:
        partner = db.get_user_by_id(ride["driver_id"]) if ride.get("driver_id") else None
        return ride, partner, "client"
    ride = db.get_driver_active_ride(user["id"])
    if ride:
        partner = db.get_user_by_id(ride["client_id"])
        return ride, partner, "driver"
    return None, None, None


async def chat_relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Anonymous in-bot chat: forwards text/voice between the client and driver of an
    active ride without revealing either side's real Telegram account."""
    msg = update.message
    if not msg or (msg.text and msg.text.strip() in RESERVED_MENU_TEXTS):
        return
    if not (msg.text or msg.voice):
        return

    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    ride, partner, role = _active_ride_and_partner(user)
    if not ride or not partner:
        return  # no active ride to relay through — let other handlers deal with it

    label = "🧍 Жолаушыдан" if role == "client" else "🚕 Жүргізушіден"
    prefix = f"{label} (№{ride['id']}):"
    try:
        if msg.voice:
            await context.bot.send_voice(chat_id=partner["telegram_id"], voice=msg.voice.file_id, caption=prefix)
        else:
            await context.bot.send_message(chat_id=partner["telegram_id"], text=f"{prefix}\n{msg.text}")
    except Exception as e:
        logger.warning(f"chat relay failed: {e}")
        await msg.reply_text("⚠️ Хабарлама жіберілмеді, серіктес қолжетімсіз болуы мүмкін.")


async def sos_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🚨 SOS — alerts the admin immediately with ride/contact details and last known location."""
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    ride, partner, role = _active_ride_and_partner(user)

    await update.message.reply_text(
        "🚨 SOS сигналы әкімшіге жіберілді.\n"
        "☎️ Қауіп төнсе, дереу 112-ге қоңырау шалыңыз.",
    )

    if not ADMIN_CHAT_ID:
        return
    lines = [f"🚨 <b>SOS СИГНАЛЫ!</b>", f"👤 Жіберген: {contact_name(user)} ({'жолаушы' if role != 'driver' else 'жүргізуші'})"]
    if user.get("phone"):
        lines.append(f"📱 {user['phone']}")
    if ride:
        lines.append(f"🚕 Сапар №{ride['id']}: {ride.get('from_location')} → {ride.get('to_location')}")
        if partner:
            lines.append(f"↔️ Серігі: {contact_name(partner)} ({partner.get('phone') or '—'})")
    if user.get("last_lat") and user.get("last_lon"):
        lines.append(f"📍 Соңғы орны: {maps_link(user['last_lat'], user['last_lon'])}")
    try:
        await context.bot.send_message(ADMIN_CHAT_ID, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"SOS notify admin failed: {e}")


async def arrived_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ride_id = int(query.data.split("_")[1])

    tg_user = update.effective_user
    driver = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)

    success = db.mark_arrived(ride_id, driver["id"])
    if not success:
        await query.edit_message_text("Бұл әрекетті орындау мүмкін емес.")
        return

    ride = db.get_ride(ride_id)
    client = db.get_user_by_id(ride["client_id"])

    finish_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏁 Сапарды аяқтау", callback_data=f"finish_{ride_id}")]])
    await query.edit_message_text(f"📍 №{ride_id}: жолаушыға жеттің. Сапар аяқталғанда батырманы бас:", reply_markup=finish_kb)

    try:
        await context.bot.send_message(chat_id=client["telegram_id"], text=f"📍 Жүргізуші №{ride_id} тапсырысы бойынша жеткен жерге келді!")
    except Exception as e:
        logger.warning(f"Could not notify client: {e}")


# ---------- DRIVER: decline (with reason), transfer to another driver, price offer ----------

async def decline_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Driver gives up an already-accepted ride -> asks for a reason first."""
    query = update.callback_query
    await query.answer()
    ride_id = int(query.data.split("_")[1])
    context.user_data["decline_ride_id"] = ride_id
    await query.message.reply_text("Бас тарту себебін таңда:", reply_markup=DECLINE_REASON_KEYBOARD)


async def decline_reason_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ride_id = context.user_data.get("decline_ride_id")
    reason = DECLINE_REASON_TEXT.get(query.data, "Себебі көрсетілмеді")
    if ride_id is None:
        await query.edit_message_text("Әрекет мерзімі өтіп кетті.")
        return

    tg_user = update.effective_user
    driver = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    success = db.decline_ride(ride_id, driver["id"], reason)
    if not success:
        await query.edit_message_text("Бұл әрекетті орындау мүмкін емес.")
        return

    await query.edit_message_text(f"❌ №{ride_id} тапсырысынан бас тарттың. Себебі: {reason}")

    ride = db.get_ride(ride_id)
    client = db.get_user_by_id(ride["client_id"])
    try:
        await context.bot.send_message(
            chat_id=client["telegram_id"],
            text=f"⚠️ Жүргізуші №{ride_id} тапсырысынан бас тартты ({reason}). Басқа жүргізуші іздестіреміз.",
        )
    except Exception as e:
        logger.warning(f"Could not notify client of decline: {e}")

    # re-broadcast to online drivers
    driver_ids = db.get_online_driver_telegram_ids()
    text = _ride_card_text(ride)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Қабылдау", callback_data=f"accept_{ride_id}"),
        InlineKeyboardButton("💰 Баға ұсыну", callback_data=f"offer_{ride_id}"),
    ]])
    for driver_tg_id in driver_ids:
        try:
            await context.bot.send_message(chat_id=driver_tg_id, text=f"🆕 (қайта) {text}", reply_markup=kb)
        except Exception as e:
            logger.warning(f"Could not notify driver {driver_tg_id}: {e}")


async def transfer_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Driver hands an active ride off to another online, verified driver."""
    query = update.callback_query
    await query.answer()
    ride_id = int(query.data.split("_")[1])
    tg_user = update.effective_user
    driver = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)

    others = db.get_other_online_drivers(driver["id"])
    if not others:
        await query.message.reply_text("Қазір басқа онлайн жүргізуші жоқ.")
        return
    buttons = [
        [InlineKeyboardButton(f"{o.get('full_name') or o.get('phone') or o['id']}", callback_data=f"transferto_{ride_id}_{o['id']}")]
        for o in others
    ]
    await query.message.reply_text("Кімге бересің?", reply_markup=InlineKeyboardMarkup(buttons))


async def transfer_to_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, ride_id, to_driver_id = query.data.split("_")
    ride_id, to_driver_id = int(ride_id), int(to_driver_id)

    tg_user = update.effective_user
    from_driver = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)

    success = db.transfer_ride(ride_id, from_driver["id"], to_driver_id)
    if not success:
        await query.edit_message_text("Бұл әрекетті орындау мүмкін емес.")
        return

    await query.edit_message_text(f"🔄 №{ride_id} тапсырысын басқа жүргізушіге бердің.")

    ride = db.get_ride(ride_id)
    client = db.get_user_by_id(ride["client_id"])
    to_driver = db.get_user_by_id(to_driver_id)

    progress_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📍 Жеттім", callback_data=f"arrived_{ride_id}")]])
    try:
        await context.bot.send_message(
            chat_id=to_driver["telegram_id"],
            text=(
                f"🔄 Саған №{ride_id} тапсырысы берілді.\n"
                f"👤 Жолаушы: {client.get('full_name') or '-'}\n"
                f"📱 Телефон: {client.get('phone') or '-'}\n"
                f"{ride['from_location']} → {ride['to_location']}"
            ),
            reply_markup=progress_kb,
        )
        await context.bot.send_message(
            chat_id=client["telegram_id"],
            text=f"🔄 №{ride_id} тапсырысыңызды жаңа жүргізуші жалғастырады: {to_driver.get('full_name') or '-'} {rating_str(to_driver['id'])}",
        )
    except Exception as e:
        logger.warning(f"Could not notify parties of transfer: {e}")


async def offer_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Driver wants to propose a different price for an open order."""
    query = update.callback_query
    await query.answer()
    ride_id = int(query.data.split("_")[1])
    context.user_data["offer_ride_id"] = ride_id
    await query.message.reply_text(f"№{ride_id} тапсырысы үшін өз бағаңды жаз (теңге):")
    return OFFER_PRICE


async def offer_price_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ride_id = context.user_data.get("offer_ride_id")
    price = update.message.text
    if ride_id is None:
        await update.message.reply_text("Әрекет мерзімі өтіп кетті.")
        return ConversationHandler.END

    tg_user = update.effective_user
    driver = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    ride = db.get_ride(ride_id)
    if not ride or ride["status"] != "open":
        await update.message.reply_text("Бұл тапсырыс енді қолжетімсіз.")
        return ConversationHandler.END

    offer_id = db.create_offer(ride_id, driver["id"], price)
    await update.message.reply_text(f"💰 №{ride_id} үшін {price} тг ұсынысың жолаушыға жіберілді.", reply_markup=driver_menu(driver))

    client = db.get_user_by_id(ride["client_id"])
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Келісемін", callback_data=f"offeraccept_{offer_id}"),
        InlineKeyboardButton("❌ Жоқ", callback_data=f"offerreject_{offer_id}"),
    ]])
    try:
        await context.bot.send_message(
            chat_id=client["telegram_id"],
            text=f"💰 №{ride_id} тапсырысыңызға жүргізуші {contact_name(driver)} {price} тг ұсынды. Келісесіз бе?",
            reply_markup=kb,
        )
    except Exception as e:
        logger.warning(f"Could not notify client of price offer: {e}")
    return ConversationHandler.END


async def offer_accept_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    offer_id = int(query.data.split("_")[1])

    offer = db.accept_offer(offer_id)
    if not offer:
        await query.edit_message_text("Кешіріңіз, бұл ұсыныс енді жарамсыз (тапсырыс алынып қойды немесе бас тартылды).")
        return

    ride = db.get_ride(offer["ride_id"])
    driver = db.get_user_by_id(offer["driver_id"])
    client = db.get_user_by_id(ride["client_id"])

    client_text, client_kb = _build_client_accept_view(ride, driver)
    await query.edit_message_text(f"✅ №{ride['id']} үшін {offer['price']} тг бағаға келістіңіз.\n\n{client_text}", reply_markup=client_kb)

    driver_text, driver_kb = _build_driver_accept_view(ride, client)
    try:
        await context.bot.send_message(chat_id=driver["telegram_id"], text=f"🎉 Клиент ұсынысыңызды қабылдады!\n\n{driver_text}", reply_markup=driver_kb)
    except Exception as e:
        logger.warning(f"Could not notify driver of offer acceptance: {e}")
    await _finalize_ride_acceptance(context, ride["id"], driver, client, ride)

    # Every other driver who offered a price on this same ride is now out of luck.
    for other_id in offer.get("other_driver_ids", []):
        other_driver = db.get_user_by_id(other_id)
        if other_driver:
            try:
                await context.bot.send_message(
                    other_driver["telegram_id"], f"❌ №{ride['id']} тапсырысы басқа жүргізушінің бағасымен берілді."
                )
            except Exception:
                pass


async def offer_reject_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    offer_id = int(query.data.split("_")[1])
    offer = db.get_offer(offer_id)
    db.reject_offer(offer_id)
    await query.edit_message_text("❌ Ұсыныс қабылданбады. Бастапқы баға қалады.")
    if offer:
        driver = db.get_user_by_id(offer["driver_id"])
        if driver:
            try:
                await context.bot.send_message(
                    driver["telegram_id"], f"❌ №{offer['ride_id']} тапсырысы үшін {offer['price']} тг ұсынысыңыз қабылданбады."
                )
            except Exception:
                pass


async def finish_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ride_id = int(query.data.split("_")[1])

    tg_user = update.effective_user
    driver = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)

    success = db.complete_ride_by_driver(ride_id, driver["id"])
    if not success:
        await query.edit_message_text("Бұл әрекетті орындау мүмкін емес.")
        return

    ride = db.get_ride(ride_id)
    client = db.get_user_by_id(ride["client_id"])

    # --- automatic commission deduction (InDrive-style: client pays driver directly,
    #     driver's service balance is charged the commission %) ---
    commission_line = ""
    price_digits = "".join(ch for ch in (ride.get("price") or "") if ch.isdigit())
    if price_digits and is_admin_tg_id(driver["telegram_id"]):
        # Admin's own driver account has an unlimited balance — no commission is deducted.
        commission_line = (
            f"\n\n💰 Сапар бағасы: {price_digits} ₸\n"
            f"💳 Балансыңыз: ♾️ Шексіз (әкімші)"
        )
    elif price_digits:
        price_num = int(price_digits)
        percent = db.get_commission_percent()
        commission = round(price_num * percent / 100, 2)
        db.record_commission(driver["id"], ride_id, commission)
        new_balance = db.get_balance(driver["id"])
        commission_line = (
            f"\n\n💰 Сапар бағасы: {price_num} ₸\n"
            f"⚙️ Комиссия: {percent}% = {commission} ₸\n"
            f"💳 Балансыңыз: {new_balance} ₸"
        )
        if new_balance < 0:
            commission_line += "\n⚠️ Балансыңыз теріс. Балансты толтырыңыз."

    client_stars_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐" * n, callback_data=f"clientrate_{ride_id}_{n}") for n in range(1, 6)
    ]])
    await query.edit_message_text(f"🏁 №{ride_id} сапары аяқталды. Рахмет!" + commission_line)
    await context.bot.send_message(
        chat_id=driver["telegram_id"], text="Жолаушыны бағалаңыз:", reply_markup=client_stars_kb
    )

    stars_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐" * n, callback_data=f"rate_{ride_id}_{n}") for n in range(1, 6)
    ]])
    try:
        await context.bot.send_message(
            chat_id=client["telegram_id"],
            text=f"🏁 №{ride_id} сапарыңыз аяқталды. Сау жол!\nЖүргізушіні бағалаңыз:",
            reply_markup=stars_kb,
        )
    except Exception as e:
        logger.warning(f"Could not notify client: {e}")


async def rate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, ride_id, stars = query.data.split("_")
    ride_id, stars = int(ride_id), int(stars)

    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)

    success = db.rate_ride(ride_id, user["id"], stars)
    if not success:
        await query.edit_message_text("Бұл тапсырысты бағалау мүмкін емес (бұрын бағаланған болуы мүмкін).")
        return
    await query.edit_message_text(f"Рахмет! Сіз №{ride_id} үшін {'⭐' * stars} қойдыңыз.")


async def rate_client_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Driver rates the client/passenger after finishing a ride."""
    query = update.callback_query
    await query.answer()
    _, ride_id, stars = query.data.split("_")
    ride_id, stars = int(ride_id), int(stars)

    tg_user = update.effective_user
    driver = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)

    success = db.rate_client(ride_id, driver["id"], stars)
    if not success:
        await query.edit_message_text("Бұл жолаушыны бағалау мүмкін емес (бұрын бағаланған болуы мүмкін).")
        return
    await query.edit_message_text(f"Рахмет! Сіз №{ride_id} жолаушысына {'⭐' * stars} қойдыңыз.")


async def earnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    stats = db.get_driver_earnings(user["id"])
    daily = db.get_driver_daily_earnings(user["id"])
    rating = db.get_driver_rating(user["id"])
    text = (
        f"💰 Бүгін: {daily['count']} сапар, {daily['total']} тг\n"
        f"📊 Барлық уақыт: {stats['count']} сапар, {stats['total']} тг"
    )
    if rating["count"]:
        text += f"\n⭐ Рейтинг: {rating['avg']} ({rating['count']} баға)"
    await update.message.reply_text(text)


# ---------- DRIVER: balance / commission wallet ----------

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    if is_admin_tg_id(tg_user.id):
        await update.message.reply_text(
            "💳 <b>Балансыңыз: ♾️ Шексіз (әкімші)</b>\nӘкімші аккаунтынан комиссия ешқашан ұсталмайды.",
            parse_mode="HTML",
        )
        return
    balance = db.get_balance(user["id"])
    tx = db.get_user_transactions(user["id"], limit=5)
    lines = [f"💳 <b>Балансыңыз: {balance} ₸</b>"]
    if balance < 0:
        lines.append("⚠️ Балансыңыз теріс — жаңа тапсырыс қабылдамас бұрын толтырыңыз.")
    if tx:
        lines.append("\nСоңғы операциялар:")
        icon = {"commission": "➖", "topup": "➕", "withdrawal": "💸", "adjustment": "⚙️"}
        status_icon = {"pending": "⏳", "completed": "✅", "rejected": "❌"}
        for t in tx:
            lines.append(
                f"{icon.get(t['type'], '•')} {t['amount']:+.0f} ₸ "
                f"{status_icon.get(t['status'], '')} ({t['type']})"
            )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def topup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    if user.get("role") != "driver":
        return ConversationHandler.END
    await update.message.reply_text(
        "💳 Қанша теңге толтырғыңыз келеді?\n"
        "Соманы Kaspi/қолма-қол арқылы әкімшіге төлеп, содан кейін соманы осында санмен жазыңыз "
        "(мысалы: 5000). Әкімші растаған соң балансыңызға қосылады.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return TOPUP_AMOUNT


async def topup_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    digits = "".join(ch for ch in (update.message.text or "") if ch.isdigit())
    if not digits or int(digits) <= 0:
        await update.message.reply_text("Санмен жазыңыз, мысалы: 5000")
        return TOPUP_AMOUNT
    amount = int(digits)
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    tx_id = db.request_topup(user["id"], amount, note="Kaspi/қолма-қол")
    await update.message.reply_text(
        f"🟡 {amount} ₸ толтыру сұрауы жіберілді. Әкімші растағанша күтіңіз.",
        reply_markup=driver_menu(db.get_user_by_id(user["id"])),
    )
    if ADMIN_CHAT_ID:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Растау", callback_data=f"adm_tx_approve_{tx_id}"),
            InlineKeyboardButton("❌ Бас тарту", callback_data=f"adm_tx_reject_{tx_id}"),
        ]])
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"💳 <b>Баланс толтыру сұрауы</b>\n👤 {contact_name(user)}\n💰 Сома: {amount} ₸",
                reply_markup=kb, parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"topup notify admin failed: {e}")
    return ConversationHandler.END


async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    if user.get("role") != "driver":
        return ConversationHandler.END
    balance = db.get_balance(user["id"])
    if balance <= 0:
        await update.message.reply_text(f"💳 Балансыңызда шығаратын қаражат жоқ ({balance} ₸).")
        return ConversationHandler.END
    await update.message.reply_text(
        f"💸 Балансыңызда {balance} ₸ бар. Қанша теңге шығарғыңыз келеді? (санмен жазыңыз)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return WITHDRAW_AMOUNT


async def withdraw_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    digits = "".join(ch for ch in (update.message.text or "") if ch.isdigit())
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    balance = db.get_balance(user["id"])
    if not digits or int(digits) <= 0:
        await update.message.reply_text("Санмен жазыңыз, мысалы: 3000")
        return WITHDRAW_AMOUNT
    amount = int(digits)
    if amount > balance:
        await update.message.reply_text(f"⚠️ Балансыңызда тек {balance} ₸ бар. Азырақ сома жазыңыз.")
        return WITHDRAW_AMOUNT
    tx_id = db.request_withdrawal(user["id"], amount, note="Қолма-қол/Kaspi")
    await update.message.reply_text(
        f"🟡 {amount} ₸ шығару сұрауы жіберілді. Әкімші растаған соң ақша қолма-қол/Kaspi арқылы беріледі.",
        reply_markup=driver_menu(db.get_user_by_id(user["id"])),
    )
    if ADMIN_CHAT_ID:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Растау", callback_data=f"adm_tx_approve_{tx_id}"),
            InlineKeyboardButton("❌ Бас тарту", callback_data=f"adm_tx_reject_{tx_id}"),
        ]])
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"💸 <b>Ақша шығару сұрауы</b>\n👤 {contact_name(user)}\n📱 {user.get('phone') or '—'}\n💰 Сома: {amount} ₸",
                reply_markup=kb, parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"withdraw notify admin failed: {e}")
    return ConversationHandler.END


async def driver_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    rides = db.get_driver_history(user["id"])
    if not rides:
        await update.message.reply_text("Әлі аяқталған сапарың жоқ.")
        return
    lines = []
    for r in rides:
        stars = "⭐" * r["rating"] if r["rating"] else "—"
        lines.append(f"№{r['id']} {r['from_location']} → {r['to_location']} | {r['price']}тг | {stars}")
    await update.message.reply_text("📜 Соңғы сапарларың:\n" + "\n".join(lines))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Болдырылмады.")
    return ConversationHandler.END


# ---------- Support / help chat (client or driver <-> admin) ----------

async def support_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if await reject_if_blocked(update, tg_user):
        return ConversationHandler.END
    await update.message.reply_text(
        "🆘 Сұрағыңызды немесе мәселеңізді жазыңыз, әкімшіге жібереміз:",
        reply_markup=nav_keyboard(include_back=False),
    )
    return SUPPORT_MSG


async def support_message_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
    db.add_support_message(user["id"], "in", update.message.text)

    menu = CLIENT_MENU if user["role"] == "client" else driver_menu(user)
    await update.message.reply_text(
        "✅ Хабарламаңыз әкімшіге жіберілді, жақын арада жауап береді.", reply_markup=menu
    )

    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"🆘 Қолдау сұрауы\n👤 {contact_name(user)} ({'жүргізуші' if user['role'] == 'driver' else 'жолаушы'})\n"
                    f"Хабарлама: {update.message.text}\n\n"
                    f"Жауап беру үшін админ панельдегі 'Қолдау' бетін ашыңыз."
                ),
            )
        except Exception as e:
            logger.warning(f"Could not notify admin of support message: {e}")
    else:
        logger.warning("ADMIN_CHAT_ID орнатылмаған — қолдау хабары ешкімге жіберілмеді.")
    return ConversationHandler.END


# ---------- ADMIN: approve/deny intercity access requests (via Telegram) ----------

async def _is_admin(update: Update) -> bool:
    return bool(ADMIN_CHAT_ID) and str(update.effective_user.id) == str(ADMIN_CHAT_ID)


async def intercity_grant_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await _is_admin(update):
        await query.answer("Бұл әрекет тек әкімшіге арналған.", show_alert=True)
        return
    target_tg_id = int(query.data.split("_")[1])
    user = db.get_user_by_telegram_id(target_tg_id)
    if not user:
        await query.edit_message_text("Пайдаланушы табылмады.")
        return
    db.set_intercity_access(user["id"], True)
    await query.edit_message_text(f"✅ {contact_name(user)} үшін қалааралық бағыттарға рұқсат берілді.")
    try:
        await context.bot.send_message(
            chat_id=target_tg_id,
            text="✅ Сізге қалааралық (Жанақала — қала/ауыл) бағыттар бойынша тапсырыс беруге рұқсат берілді! Тапсырысты қайта бастап көріңіз.",
        )
    except Exception as e:
        logger.warning(f"Could not notify user of intercity grant: {e}")


async def intercity_deny_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await _is_admin(update):
        await query.answer("Бұл әрекет тек әкімшіге арналған.", show_alert=True)
        return
    target_tg_id = int(query.data.split("_")[1])
    user = db.get_user_by_telegram_id(target_tg_id)
    if not user:
        await query.edit_message_text("Пайдаланушы табылмады.")
        return
    db.set_intercity_access(user["id"], False)
    await query.edit_message_text(f"❌ {contact_name(user)} үшін сұраныс қабылданбады.")
    try:
        await context.bot.send_message(
            chat_id=target_tg_id,
            text="❌ Өкінішке орай, қалааралық бағыт бойынша сұранысыңыз қабылданбады.",
        )
    except Exception as e:
        logger.warning(f"Could not notify user of intercity denial: {e}")


# ---------- ADMIN: approve/reject a driver's vehicle verification (via Telegram) ----------

VERIFY_REJECT_KEYBOARD_BUILDER = lambda user_id: InlineKeyboardMarkup([
    [InlineKeyboardButton("📸 Фото анық емес", callback_data=f"vrejreason_{user_id}_photo")],
    [InlineKeyboardButton("📄 Деректер сәйкес емес", callback_data=f"vrejreason_{user_id}_data")],
    [InlineKeyboardButton("❓ Басқа себеп", callback_data=f"vrejreason_{user_id}_other")],
])

VERIFY_REJECT_REASON_TEXT = {
    "photo": "Фото анық емес, қайта жүктеңіз",
    "data": "Көлік деректері сәйкес емес",
    "other": "Талапқа сай емес",
}


async def verify_approve_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await _is_admin(update):
        await query.answer("Бұл әрекет тек әкімшіге арналған.", show_alert=True)
        return
    user_id = int(query.data.split("_")[1])
    user = db.get_user_by_id(user_id)
    if not user:
        await query.edit_message_caption("Пайдаланушы табылмады.")
        return
    db.set_verification_status(user_id, "approved")
    await query.edit_message_caption(f"✅ {contact_name(user)} расталды.")
    try:
        await context.bot.send_message(
            chat_id=user["telegram_id"],
            text="✅ Құжаттарың расталды! Енді 🟢 Online режиміне кіре аласың.",
            reply_markup=driver_menu(db.get_user_by_id(user_id)),
        )
    except Exception as e:
        logger.warning(f"Could not notify driver of approval: {e}")


async def verify_reject_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await _is_admin(update):
        await query.answer("Бұл әрекет тек әкімшіге арналған.", show_alert=True)
        return
    user_id = int(query.data.split("_")[1])
    await query.message.reply_text("Қабылдамау себебін таңда:", reply_markup=VERIFY_REJECT_KEYBOARD_BUILDER(user_id))


async def verify_reject_reason_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await _is_admin(update):
        await query.answer("Бұл әрекет тек әкімшіге арналған.", show_alert=True)
        return
    _, user_id, reason_key = query.data.split("_")
    user_id = int(user_id)
    reason = VERIFY_REJECT_REASON_TEXT.get(reason_key, "Талапқа сай емес")
    user = db.get_user_by_id(user_id)
    if not user:
        await query.edit_message_text("Пайдаланушы табылмады.")
        return
    db.set_verification_status(user_id, "rejected", reason)
    await query.edit_message_text(f"❌ {contact_name(user)} қабылданбады. Себебі: {reason}")
    try:
        await context.bot.send_message(
            chat_id=user["telegram_id"],
            text=f"❌ Құжаттарың қабылданбады. Себебі: {reason}\n🔄 Деректерді түзетіп қайта тіркеле аласың.",
            reply_markup=driver_menu(db.get_user_by_id(user_id)),
        )
    except Exception as e:
        logger.warning(f"Could not notify driver of rejection: {e}")


def build_application():
    """Builds and returns the configured telegram Application (handlers wired up),
    without starting polling/webhook. Used both by main() and by a combined
    app.py that also runs the admin panel in the same process."""
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    back_filter = filters.Regex(f"^{BACK_BTN}$")
    menu_filter = filters.Regex(f"^{MENU_BTN}$")
    plain_text = filters.TEXT & ~filters.COMMAND & ~back_filter & ~menu_filter

    onboarding = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^🔄 Қайта тіркелу$"), reregister_start),
        ],
        states={
            CHOOSE_ROLE: [
                CallbackQueryHandler(choose_role, pattern="^role_"),
            ],
            SHARE_PHONE: [MessageHandler(filters.CONTACT, share_phone)],
            ID_PHOTO: [MessageHandler(filters.PHOTO | plain_text, ask_id_photo)],
            CAR_BRAND: [MessageHandler(menu_filter, go_main_menu), MessageHandler(plain_text, ask_car_brand)],
            CAR_MODEL: [
                MessageHandler(back_filter, make_vehicle_back_handler(CAR_BRAND)),
                MessageHandler(menu_filter, go_main_menu),
                MessageHandler(plain_text, ask_car_model),
            ],
            CAR_PLATE: [
                MessageHandler(back_filter, make_vehicle_back_handler(CAR_MODEL)),
                MessageHandler(menu_filter, go_main_menu),
                MessageHandler(plain_text, ask_car_plate),
            ],
            CAR_COLOR: [
                MessageHandler(back_filter, make_vehicle_back_handler(CAR_PLATE)),
                MessageHandler(menu_filter, go_main_menu),
                MessageHandler(plain_text, ask_car_color),
            ],
            CAR_YEAR: [
                MessageHandler(back_filter, make_vehicle_back_handler(CAR_COLOR)),
                MessageHandler(menu_filter, go_main_menu),
                MessageHandler(plain_text, ask_car_year),
            ],
            CAR_PHOTO: [
                MessageHandler(back_filter, make_vehicle_back_handler(CAR_YEAR)),
                MessageHandler(menu_filter, go_main_menu),
                MessageHandler(filters.PHOTO | plain_text, ask_car_photo),
            ],
            CAR_DOC_PHOTO: [MessageHandler(filters.PHOTO | plain_text, ask_car_doc_photo)],
            WORK_CITY: [CallbackQueryHandler(choose_work_city_cb, pattern="^workcity_")],
            AGREE_RULES: [CallbackQueryHandler(agree_rules_cb, pattern="^agree_rules_yes$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(menu_filter, go_main_menu)],
    )

    new_ride_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🚕 Такси шақыру$"), new_ride_start),
            MessageHandler(filters.Regex("^📦 Жеткізу шақыру$"), new_ride_start),
        ],
        states={
            ASK_ROUTE: [
                CallbackQueryHandler(go_main_menu_cb, pattern=f"^{NAV_MENU_CB}$"),
                CallbackQueryHandler(choose_route, pattern="^route_"),
            ],
            PASSENGER_COUNT: [
                CallbackQueryHandler(choose_passenger_count, pattern="^pax_"),
            ],
            ASK_ITEM: [
                MessageHandler(back_filter, back_to_route),
                MessageHandler(menu_filter, go_main_menu),
                MessageHandler(plain_text, ask_item),
            ],
            ASK_FROM: [
                MessageHandler(back_filter, back_from_ask_from),
                MessageHandler(menu_filter, go_main_menu),
                MessageHandler(filters.LOCATION | plain_text, ask_from),
            ],
            ASK_TO: [
                MessageHandler(back_filter, back_to_from),
                MessageHandler(menu_filter, go_main_menu),
                MessageHandler(filters.LOCATION | plain_text, ask_to),
            ],
            ASK_TIME: [
                MessageHandler(back_filter, back_to_to),
                MessageHandler(menu_filter, go_main_menu),
                MessageHandler(plain_text, ask_time),
            ],
            ASK_PRICE: [
                MessageHandler(back_filter, back_to_time),
                MessageHandler(menu_filter, go_main_menu),
                MessageHandler(plain_text, ask_price),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(menu_filter, go_main_menu)],
    )

    offer_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(offer_cb, pattern="^offer_")],
        states={
            OFFER_PRICE: [
                MessageHandler(menu_filter, go_main_menu),
                MessageHandler(plain_text, offer_price_received),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(menu_filter, go_main_menu)],
    )

    support_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🆘 Қолдау қызметі$"), support_start)],
        states={
            SUPPORT_MSG: [
                MessageHandler(menu_filter, go_main_menu),
                MessageHandler(plain_text, support_message_received),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(menu_filter, go_main_menu)],
    )

    app.add_handler(onboarding)

    balance_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^💳 Баланс толтыру$"), topup_start),
            MessageHandler(filters.Regex("^💸 Ақша шығару$"), withdraw_start),
        ],
        states={
            TOPUP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, topup_amount_received)],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(menu_filter, go_main_menu)],
    )
    app.add_handler(balance_conv)
    app.add_handler(new_ride_conv)
    app.add_handler(offer_conv)
    app.add_handler(support_conv)
    # Safety net: if someone taps Back/Main menu outside any active conversation.
    app.add_handler(MessageHandler(menu_filter, go_main_menu))
    app.add_handler(MessageHandler(filters.Regex("^📋 Менің сұраныстарым$"), my_rides))
    app.add_handler(MessageHandler(filters.Regex("^📋 Ашық тапсырыстар$"), open_rides))
    app.add_handler(MessageHandler(filters.Regex("^📍 Жақын тапсырыстар$"), nearby_rides))
    app.add_handler(MessageHandler(filters.Regex("^(🟢 Online болу|🔴 Offline болу)$"), toggle_online))
    app.add_handler(MessageHandler(filters.Regex("^📊 Табысым$"), earnings))
    app.add_handler(MessageHandler(filters.Regex("^💰 Баланс$"), show_balance))
    app.add_handler(MessageHandler(filters.Regex("^📜 Тарихым$"), driver_history))
    app.add_handler(MessageHandler(filters.Regex("^👤 Профилім$"), driver_profile))
    app.add_handler(MessageHandler(filters.Regex("^⭐ Рейтингім$"), driver_rating_view))
    app.add_handler(MessageHandler(filters.Regex("^🚨 SOS$"), sos_cb))
    # Anonymous in-bot chat relay between client <-> driver during an active ride.
    # Own low-priority group so it never blocks the conversation handlers above;
    # it self-filters (RESERVED_MENU_TEXTS + "must have an active ride") before doing anything.
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.VOICE, chat_relay), group=10)
    app.add_handler(MessageHandler(filters.LOCATION, driver_location_update))
    app.add_handler(CallbackQueryHandler(accept_ride_cb, pattern="^accept_"))
    app.add_handler(CallbackQueryHandler(arrived_cb, pattern="^arrived_"))
    app.add_handler(CallbackQueryHandler(finish_cb, pattern="^finish_"))
    app.add_handler(CallbackQueryHandler(rate_cb, pattern="^rate_"))
    app.add_handler(CallbackQueryHandler(rate_client_cb, pattern="^clientrate_"))
    app.add_handler(CallbackQueryHandler(client_cancel_cb, pattern="^clientcancel_"))
    app.add_handler(CallbackQueryHandler(decline_cb, pattern="^decline_"))
    app.add_handler(CallbackQueryHandler(decline_reason_cb, pattern="^dreason_"))
    app.add_handler(CallbackQueryHandler(transfer_cb, pattern="^transfer_"))
    app.add_handler(CallbackQueryHandler(transfer_to_cb, pattern="^transferto_"))
    app.add_handler(CallbackQueryHandler(offer_accept_cb, pattern="^offeraccept_"))
    app.add_handler(CallbackQueryHandler(offer_reject_cb, pattern="^offerreject_"))
    app.add_handler(CallbackQueryHandler(intercity_grant_cb, pattern="^icgrant_"))
    app.add_handler(CallbackQueryHandler(intercity_deny_cb, pattern="^icdeny_"))
    app.add_handler(CallbackQueryHandler(verify_approve_cb, pattern="^vapprove_"))
    app.add_handler(CallbackQueryHandler(verify_reject_cb, pattern="^vreject_"))
    app.add_handler(CallbackQueryHandler(verify_reject_reason_cb, pattern="^vrejreason_"))

    # Telegram ішіндегі толық әкімші панелі (/admin командасы, тек ADMIN_CHAT_ID үшін)
    if ADMIN_CHAT_ID:
        admin_telegram.register(app, ADMIN_CHAT_ID, contact_name, driver_menu)
    else:
        logger.warning("ADMIN_CHAT_ID орнатылмаған — Telegram әкімші панелі іске қосылмайды.")

    return app


def run_bot_blocking(in_thread=False):
    """Starts the bot and blocks. If in_thread=True (called from a background
    thread inside app.py, alongside the Flask admin panel), we disable PTB's
    signal-handler setup, since signals only work on the main thread."""
    db.init_db()
    app = build_application()
    logger.info("Bot started")

    webhook_url = None if in_thread else os.environ.get("WEBHOOK_URL")
    if webhook_url:
        # Webhook mode: for free hosts like Render, which only offer a free *Web Service*
        # (needs to bind to a port and receive HTTP requests) rather than a free background worker.
        port = int(os.environ.get("PORT", "10000"))
        logger.info(f"Running in WEBHOOK mode on port {port} -> {webhook_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=BOT_TOKEN,
            webhook_url=f"{webhook_url.rstrip('/')}/{BOT_TOKEN}",
        )
    else:
        # Polling mode: simplest option, works anywhere with outbound internet, and the
        # only mode used when running combined with the admin panel in one process/thread.
        logger.info("Running in POLLING mode")
        if in_thread:
            app.run_polling(stop_signals=None)
        else:
            app.run_polling()


def main():
    run_bot_blocking(in_thread=False)


if __name__ == "__main__":
    main()
