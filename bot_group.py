import os
import re
import json
import logging
import httpx
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
from sheets import SheetsDB

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
WEIGHER_IDS = [int(x) for x in os.environ.get("WEIGHER_IDS", "0").split(",") if x.strip()]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

(
    DEALER_MENU, ENTER_CAR, ENTER_TONS, CONFIRM_ORDER,
    WEIGHER_MENU, ENTER_REAL_WEIGHT,
    ADMIN_MENU
) = range(7)

db = SheetsDB()

def is_admin(user_id):
    return user_id == ADMIN_ID

def is_weigher(user_id):
    return user_id in WEIGHER_IDS or user_id == ADMIN_ID

def format_order(order):
    status_emoji = {"Ожидание": "🟡", "Уехал": "🚛", "Завершён": "✅", "Расхождение": "⚠️"}
    e = status_emoji.get(order.get("status", ""), "❓")
    lines = [
        f"{e} Заявка №{order['id']}",
        f"📅 {order['date']}",
        f"👤 Дилер: {order['dealer_name']}",
        f"🚗 Машина: {order['car_number']}",
        f"⚖️ Заявлено: {order['tons_requested']} тонн",
    ]
    if order.get("tons_actual"):
        lines.append(f"⚖️ Фактически: {order['tons_actual']} тонн")
        try:
            diff = float(order['tons_actual']) - float(order['tons_requested'])
            if abs(diff) > 0.5:
                lines.append(f"{'📈' if diff > 0 else '📉'} Расхождение: {diff:+.1f} т")
        except:
            pass
    lines.append(f"📊 Статус: {order.get('status','?')}")
    return "\n".join(lines)

# ─────────────────────────────────────────────
# AI ПАРСЕР — читает любой текст дилера
# ─────────────────────────────────────────────

async def parse_order_with_ai(text: str) -> dict | None:
    """Используем Claude API чтобы извлечь номер машины и тонны из любого текста"""
    if not ANTHROPIC_API_KEY:
        # Fallback: простой regex если нет API ключа
        return parse_order_regex(text)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 200,
                    "system": """Ты помощник на цементном заводе. Из текста заявки извлеки номер машины и количество тонн.
Отвечай ТОЛЬКО в формате JSON: {"car": "номер машины", "tons": число}
Если не можешь извлечь — {"car": null, "tons": null}
Номер машины может быть в любом формате: 01A123BA, 60 A 123 BA, и т.д.
Тонны — число, может быть написано как "20 тонн", "20т", "20", "двадцать тонн".""",
                    "messages": [{"role": "user", "content": f"Заявка: {text}"}]
                }
            )
        data = response.json()
        result_text = data["content"][0]["text"].strip()
        # Убираем markdown если есть
        result_text = result_text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(result_text)
        if parsed.get("car") and parsed.get("tons"):
            return {"car": str(parsed["car"]).upper(), "tons": float(parsed["tons"])}
    except Exception as e:
        logger.error(f"AI парсер ошибка: {e}")

    # Fallback на regex
    return parse_order_regex(text)

def parse_order_regex(text: str) -> dict | None:
    """Простой парсер без AI"""
    text_upper = text.upper()
    # Ищем тонны: число перед словом "тонн/т/ton"
    tons_match = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:ТОНН|ТОН|ТН|Т\b|TON)', text_upper)
    if not tons_match:
        # Просто число
        nums = re.findall(r'\b(\d+(?:[.,]\d+)?)\b', text)
        tons_num = None
        for n in nums:
            val = float(n.replace(",", "."))
            if 1 <= val <= 200:
                tons_num = val
                break
        if not tons_num:
            return None
        tons = tons_num
    else:
        tons = float(tons_match.group(1).replace(",", "."))

    # Ищем номер машины (буквы+цифры минимум 4 символа)
    car_match = re.search(r'\b([A-ZА-Я0-9]{2,}[\s\-]?[A-ZА-Я0-9]{1,}[\s\-]?[A-ZА-Я0-9]{2,})\b', text_upper)
    if not car_match:
        return None
    car = car_match.group(1).strip()

    return {"car": car, "tons": tons}

# ─────────────────────────────────────────────
# ГРУППОВЫЕ СООБЩЕНИЯ
# ─────────────────────────────────────────────

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатываем сообщения в группах"""
    if not update.message:
        return

    chat_id = update.message.chat_id
    user = update.effective_user
    text = update.message.text or ""

    # Фото — это чек от весовщика
    if update.message.photo:
        await handle_group_photo(update, context)
        return

    if not text:
        return

    logger.info(f"Группа {chat_id}, текст: {text[:50]}")

    # Пробуем распознать заявку
    parsed = await parse_order_with_ai(text)

    if not parsed:
        # Не похоже на заявку — игнорируем
        return

    car = parsed["car"]
    tons = parsed["tons"]

    # Сохраняем в таблицу
    try:
        order_id = db.add_order(
            dealer_id=user.id,
            dealer_name=user.full_name or user.username or "Дилер",
            car_number=car,
            tons_requested=tons,
            group_id=chat_id
        )

        # Отвечаем в группу
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Верно", callback_data=f"gconfirm_{order_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"gcancel_{order_id}")
        ]])
        await update.message.reply_text(
            f"📋 Заявка принята!\n\n"
            f"🚗 Машина: {car}\n"
            f"⚖️ Тонн: {tons}\n"
            f"👤 {user.full_name}\n\n"
            f"Всё верно?",
            reply_markup=kb
        )
        logger.info(f"Заявка #{order_id} создана из группы")

        # Уведомляем весовщиков
        msg = f"🔔 Новая заявка №{order_id}\n👤 {user.full_name}\n🚗 {car}\n⚖️ {tons} тонн\n📍 Группа: {update.message.chat.title}"
        for wid in WEIGHER_IDS:
            try:
                await context.bot.send_message(wid, msg)
            except Exception as e:
                logger.error(f"Не могу отправить весовщику {wid}: {e}")
        if ADMIN_ID and ADMIN_ID not in WEIGHER_IDS:
            try:
                await context.bot.send_message(ADMIN_ID, f"📋 {msg}")
            except Exception as e:
                logger.error(f"Ошибка уведомления админа: {e}")

    except Exception as e:
        logger.error(f"Ошибка создания заявки из группы: {e}")

async def handle_group_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фото в группе = чек весовщика"""
    user = update.effective_user
    caption = update.message.caption or ""

    # Ищем номер заявки в подписи к фото
    order_id = None
    match = re.search(r'№?(\d+)', caption)
    if match:
        order_id = int(match.group(1))

    # Ищем активную заявку для этой группы
    if not order_id:
        active = db.get_orders_by_status("Уехал")
        group_id = str(update.message.chat_id)
        for o in active:
            if str(o.get("group_id", "")) == group_id:
                order_id = o["id"]
                break

    if not order_id:
        # Попробуем взять последнюю активную
        active = db.get_orders_by_status("Уехал")
        if active:
            order_id = active[-1]["id"]

    if not order_id:
        await update.message.reply_text("⚠️ Не найдена активная заявка. Укажи номер заявки в подписи к фото (например: №5)")
        return

    # Пытаемся извлечь вес из подписи
    tons_actual = None
    tons_match = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:тонн|тон|т\b)', caption.lower())
    if tons_match:
        tons_actual = float(tons_match.group(1).replace(",", "."))

    order = db.get_order(order_id)
    if not order:
        await update.message.reply_text(f"⚠️ Заявка №{order_id} не найдена.")
        return

    if tons_actual:
        # Есть вес — закрываем сразу
        db.update_weight(order_id, tons_actual)
        diff = tons_actual - float(order["tons_requested"])
        msg = f"✅ Чек получен! Заявка №{order_id} закрыта.\n🚗 {order['car_number']}\n⚖️ Факт: {tons_actual} т"
        if abs(diff) > 0.5:
            msg += f"\n⚠️ Расхождение: {diff:+.1f} т"
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ РАСХОЖДЕНИЕ!\nЗаявка №{order_id}\n👤 {order['dealer_name']}\n"
                    f"🚗 {order['car_number']}\nЗаявлено: {order['tons_requested']} т\nФакт: {tons_actual} т\nРазница: {diff:+.1f} т"
                )
            except:
                pass
        await update.message.reply_text(msg)
    else:
        # Нет веса — просим весовщика написать
        db.update_status(order_id, "Чек получен")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Закрыть заявку №{order_id}", callback_data=f"gclose_{order_id}")
        ]])
        await update.message.reply_text(
            f"📸 Чек получен для заявки №{order_id}\n🚗 {order['car_number']}\n"
            f"Напиши вес в тоннах в ответ или нажми кнопку:",
            reply_markup=kb
        )
        context.chat_data["pending_weight_order"] = order_id

async def handle_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки в группах"""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("gconfirm_"):
        order_id = int(data.split("_")[1])
        await query.message.edit_text(
            query.message.text + "\n\n✅ Подтверждено! Ожидаем машину на весах."
        )

    elif data.startswith("gcancel_"):
        order_id = int(data.split("_")[1])
        db.update_status(order_id, "Отменён")
        await query.message.edit_text(f"❌ Заявка №{order_id} отменена.")

    elif data.startswith("gclose_"):
        order_id = int(data.split("_")[1])
        context.chat_data["pending_weight_order"] = order_id
        await query.message.reply_text(f"⚖️ Введи фактический вес для заявки №{order_id} (тонн):")

async def handle_weight_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Весовщик вводит вес после фото"""
    pending = context.chat_data.get("pending_weight_order")
    if not pending:
        return

    try:
        tons = float(update.message.text.replace(",", "."))
        if tons <= 0 or tons > 200:
            raise ValueError
    except ValueError:
        return

    order = db.get_order(pending)
    if not order:
        return

    db.update_weight(pending, tons)
    diff = tons - float(order["tons_requested"])
    context.chat_data.pop("pending_weight_order", None)

    msg = f"✅ Заявка №{pending} закрыта!\n🚗 {order['car_number']}\n⚖️ Факт: {tons} т"
    if abs(diff) > 0.5:
        msg += f"\n⚠️ Расхождение: {diff:+.1f} т"
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ РАСХОЖДЕНИЕ!\nЗаявка №{pending}\n👤 {order['dealer_name']}\n"
                f"Заявлено: {order['tons_requested']} т\nФакт: {tons} т\nРазница: {diff:+.1f} т"
            )
        except:
            pass
    await update.message.reply_text(msg)

# ─────────────────────────────────────────────
# ЛИЧНЫЕ СООБЩЕНИЯ (как раньше)
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    logger.info(f"START от user_id={user_id}, chat_type={chat_type}")

    if chat_type in ("group", "supergroup"):
        await update.message.reply_text(
            "👋 Бот активирован в группе!\n\n"
            "📋 Для заявки просто напишите: номер машины и тонны\n"
            "Например: 01A123BA 25 тонн\n\n"
            "📸 Для закрытия заявки: отправьте фото чека"
        )
        return

    if is_admin(user_id):
        await show_admin_menu(update, context)
        return ADMIN_MENU
    elif is_weigher(user_id):
        await show_weigher_menu(update, context)
        return WEIGHER_MENU
    else:
        db.ensure_dealer(user_id, update.effective_user.full_name)
        await show_dealer_menu(update, context)
        return DEALER_MENU

# ─── ДИЛЕР (личные сообщения) ───

async def show_dealer_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup([["📋 Новая заявка"], ["📊 Мои заявки"]], resize_keyboard=True)
    msg = update.message or update.callback_query.message
    await msg.reply_text("👋 Добро пожаловать!\nВыберите действие:", reply_markup=kb)

async def dealer_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📋 Новая заявка":
        await update.message.reply_text("🚗 Введите номер машины:")
        return ENTER_CAR
    elif text == "📊 Мои заявки":
        orders = db.get_dealer_orders(update.effective_user.id, limit=10)
        if not orders:
            await update.message.reply_text("У вас ещё нет заявок.")
        else:
            for o in orders:
                await update.message.reply_text(format_order(o))
        return DEALER_MENU
    await show_dealer_menu(update, context)
    return DEALER_MENU

async def enter_car(update: Update, context: ContextTypes.DEFAULT_TYPE):
    car = update.message.text.strip().upper()
    if len(car) < 3:
        await update.message.reply_text("❌ Номер слишком короткий:")
        return ENTER_CAR
    context.user_data["car"] = car
    await update.message.reply_text(f"✅ Машина: {car}\n\n⚖️ Сколько тонн?")
    return ENTER_TONS

async def enter_tons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tons = float(update.message.text.replace(",", "."))
        if tons <= 0 or tons > 200:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите корректное число:")
        return ENTER_TONS
    context.user_data["tons"] = tons
    car = context.user_data["car"]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="confirm_no")
    ]])
    await update.message.reply_text(
        f"📋 Проверьте:\n\n🚗 {car}\n⚖️ {tons} тонн\n\nВсё верно?",
        reply_markup=kb
    )
    return CONFIRM_ORDER

async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_no":
        await query.message.reply_text("❌ Отменено.")
        await show_dealer_menu(update, context)
        return DEALER_MENU
    user = update.effective_user
    car = context.user_data["car"]
    tons = context.user_data["tons"]
    try:
        order_id = db.add_order(dealer_id=user.id, dealer_name=user.full_name, car_number=car, tons_requested=tons)
        await query.message.reply_text(f"✅ Заявка №{order_id} создана!\n🚗 {car}\n⚖️ {tons} тонн")
        msg = f"🔔 Новая заявка №{order_id}\n👤 {user.full_name}\n🚗 {car}\n⚖️ {tons} тонн"
        for wid in WEIGHER_IDS:
            try:
                await context.bot.send_message(wid, msg)
            except:
                pass
        if ADMIN_ID and ADMIN_ID not in WEIGHER_IDS:
            try:
                await context.bot.send_message(ADMIN_ID, f"📋 {msg}")
            except:
                pass
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка: {e}")
    await show_dealer_menu(update, context)
    return DEALER_MENU

# ─── ВЕСОВЩИК (личные сообщения) ───

async def show_weigher_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup([
        ["🚛 Машина уехала"],
        ["⚖️ Ввести реальный вес"],
        ["📋 Активные заявки"]
    ], resize_keyboard=True)
    msg = update.message or update.callback_query.message
    await msg.reply_text("⚖️ Меню весовщика:", reply_markup=kb)

async def weigher_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "🚛 Машина уехала":
        orders = db.get_orders_by_status("Ожидание")
        if not orders:
            await update.message.reply_text("Нет заявок в статусе 'Ожидание'.")
            return WEIGHER_MENU
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"№{o['id']} — {o['car_number']} ({o['dealer_name']})", callback_data=f"dep_{o['id']}")
        ] for o in orders])
        await update.message.reply_text("Выберите заявку:", reply_markup=kb)
    elif text == "⚖️ Ввести реальный вес":
        orders = db.get_orders_by_status("Уехал")
        if not orders:
            await update.message.reply_text("Нет заявок в статусе 'Уехал'.")
            return WEIGHER_MENU
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"№{o['id']} — {o['car_number']} {o['tons_requested']}т", callback_data=f"weigh_{o['id']}")
        ] for o in orders])
        await update.message.reply_text("Выберите заявку:", reply_markup=kb)
    elif text == "📋 Активные заявки":
        orders = db.get_active_orders()
        if not orders:
            await update.message.reply_text("Нет активных заявок.")
        else:
            for o in orders:
                await update.message.reply_text(format_order(o))
    return WEIGHER_MENU

async def weigher_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("dep_"):
        order_id = int(data.split("_")[1])
        db.update_status(order_id, "Уехал")
        order = db.get_order(order_id)
        await query.message.reply_text(f"✅ Заявка №{order_id} — 🚛 Уехал")
        if order:
            try:
                await context.bot.send_message(int(order["dealer_id"]), f"🚛 Машина {order['car_number']} уехала. Заявка №{order_id}")
            except:
                pass
        return WEIGHER_MENU
    elif data.startswith("weigh_"):
        order_id = int(data.split("_")[1])
        context.user_data["weigh_order_id"] = order_id
        order = db.get_order(order_id)
        await query.message.reply_text(
            f"⚖️ Заявка №{order_id}\n🚗 {order['car_number']}\nЗаявлено: {order['tons_requested']} т\n\nВведите фактический вес:"
        )
        return ENTER_REAL_WEIGHT
    return WEIGHER_MENU

async def enter_real_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        actual = float(update.message.text.replace(",", "."))
        if actual <= 0 or actual > 200:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите корректный вес:")
        return ENTER_REAL_WEIGHT
    order_id = context.user_data.get("weigh_order_id")
    order = db.get_order(order_id)
    requested = float(order["tons_requested"])
    diff = actual - requested
    db.update_weight(order_id, actual)
    msg = f"✅ Заявка №{order_id} завершена.\n🚗 {order['car_number']}\n⚖️ Факт: {actual} т"
    if abs(diff) > 0.5:
        msg += f"\n⚠️ Расхождение: {diff:+.1f} т!"
        try:
            await context.bot.send_message(ADMIN_ID,
                f"⚠️ РАСХОЖДЕНИЕ!\nЗаявка №{order_id}\n👤 {order['dealer_name']}\n"
                f"Заявлено: {requested} т\nФакт: {actual} т\nРазница: {diff:+.1f} т")
        except:
            pass
    await update.message.reply_text(msg)
    try:
        await context.bot.send_message(int(order["dealer_id"]),
            f"✅ Груз оформлен!\nЗаявка №{order_id}\n🚗 {order['car_number']}\n⚖️ {actual} тонн")
    except:
        pass
    await show_weigher_menu(update, context)
    return WEIGHER_MENU

# ─── АДМИН ───

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup([
        ["📊 Сегодня", "📅 Все заявки"],
        ["👥 По дилерам", "⚠️ Расхождения"],
        ["🚛 Активные"]
    ], resize_keyboard=True)
    msg = update.message or update.callback_query.message
    await msg.reply_text("🏭 Панель управления — Цементный завод", reply_markup=kb)

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 Сегодня":
        today = datetime.now().strftime("%Y-%m-%d")
        orders = db.get_orders_by_date(today)
        if not orders:
            await update.message.reply_text(f"Сегодня заявок нет.")
            return ADMIN_MENU
        total_req = sum(float(o["tons_requested"]) for o in orders if o.get("tons_requested"))
        total_act = sum(float(o["tons_actual"]) for o in orders if o.get("tons_actual"))
        done = len([o for o in orders if o["status"] == "Завершён"])
        await update.message.reply_text(
            f"📊 Сегодня {today}\n\nВсего заявок: {len(orders)}\nЗавершено: {done}\n"
            f"Заявлено тонн: {total_req:.1f}\nОтгружено тонн: {total_act:.1f}"
        )
        for o in orders:
            await update.message.reply_text(format_order(o))
    elif text == "📅 Все заявки":
        orders = db.get_all_orders(limit=20)
        if not orders:
            await update.message.reply_text("Заявок нет.")
            return ADMIN_MENU
        for o in orders:
            await update.message.reply_text(format_order(o))
    elif text == "👥 По дилерам":
        stats = db.get_dealer_stats()
        if not stats:
            await update.message.reply_text("Нет данных.")
            return ADMIN_MENU
        lines = ["👥 Статистика по дилерам:\n"]
        for s in stats:
            lines.append(f"👤 {s['dealer_name']}\n   Заявок: {s['count']} | Тонн: {s['total_tons']:.1f}\n")
        await update.message.reply_text("\n".join(lines))
    elif text == "⚠️ Расхождения":
        orders = db.get_mismatched_orders()
        if not orders:
            await update.message.reply_text("✅ Расхождений нет!")
            return ADMIN_MENU
        for o in orders:
            await update.message.reply_text(format_order(o))
    elif text == "🚛 Активные":
        orders = db.get_active_orders()
        if not orders:
            await update.message.reply_text("Нет активных заявок.")
            return ADMIN_MENU
        for o in orders:
            await update.message.reply_text(format_order(o))
    return ADMIN_MENU

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler для личных сообщений
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            DEALER_MENU:      [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, dealer_menu)],
            ENTER_CAR:        [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, enter_car)],
            ENTER_TONS:       [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, enter_tons)],
            CONFIRM_ORDER:    [CallbackQueryHandler(confirm_order, pattern="^confirm_")],
            WEIGHER_MENU:     [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, weigher_menu),
                CallbackQueryHandler(weigher_callback, pattern="^(dep_|weigh_)"),
            ],
            ENTER_REAL_WEIGHT:[MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, enter_real_weight)],
            ADMIN_MENU:       [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, admin_menu)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
        per_message=False,
    )

    # Обработчики для групп
    app.add_handler(conv)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        handle_group_message
    ))
    app.add_handler(MessageHandler(
        filters.PHOTO & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        handle_group_photo
    ))
    app.add_handler(CallbackQueryHandler(handle_group_callback, pattern="^g(confirm|cancel|close)_"))

    logger.info("Бот запущен! Режим: личные сообщения + группы")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
