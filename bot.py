import os
import logging
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

# ─── START ───

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"START от user_id={user_id}, ADMIN_ID={ADMIN_ID}")

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

# ─── ДИЛЕР ───

async def show_dealer_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup([
        ["📋 Новая заявка"],
        ["📊 Мои заявки"]
    ], resize_keyboard=True)
    msg = update.message or update.callback_query.message
    await msg.reply_text("👋 Добро пожаловать!\nВыберите действие:", reply_markup=kb)

async def dealer_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    logger.info(f"dealer_menu: {text}")
    if text == "📋 Новая заявка":
        await update.message.reply_text("🚗 Введите номер машины (пример: 01A123BA):")
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
    logger.info(f"enter_car: {car}")
    if len(car) < 3:
        await update.message.reply_text("❌ Номер слишком короткий. Попробуйте ещё раз:")
        return ENTER_CAR
    context.user_data["car"] = car
    await update.message.reply_text(f"✅ Машина: {car}\n\n⚖️ Сколько тонн хотите взять?")
    return ENTER_TONS

async def enter_tons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"enter_tons: {update.message.text}")
    try:
        tons = float(update.message.text.replace(",", "."))
        if tons <= 0 or tons > 200:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите корректное число (например: 25):")
        return ENTER_TONS

    context.user_data["tons"] = tons
    car = context.user_data["car"]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="confirm_no")
    ]])
    await update.message.reply_text(
        f"📋 Проверьте заявку:\n\n🚗 Машина: {car}\n⚖️ Тонн: {tons}\n\nВсё верно?",
        reply_markup=kb
    )
    return CONFIRM_ORDER

async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    logger.info(f"confirm_order: {query.data}")

    if query.data == "confirm_no":
        await query.message.reply_text("❌ Заявка отменена.")
        await show_dealer_menu(update, context)
        return DEALER_MENU

    user = update.effective_user
    car = context.user_data["car"]
    tons = context.user_data["tons"]

    try:
        order_id = db.add_order(
            dealer_id=user.id,
            dealer_name=user.full_name,
            car_number=car,
            tons_requested=tons
        )
        await query.message.reply_text(
            f"✅ Заявка №{order_id} создана!\n\n🚗 {car}\n⚖️ {tons} тонн\n📊 Статус: Ожидание"
        )
        # Уведомляем весовщиков и админа
        msg = f"🔔 Новая заявка №{order_id}\n👤 {user.full_name}\n🚗 {car}\n⚖️ {tons} тонн"
        for wid in WEIGHER_IDS:
            try:
                await context.bot.send_message(wid, msg)
            except Exception as e:
                logger.error(f"Не могу отправить весовщику {wid}: {e}")
        if ADMIN_ID and ADMIN_ID not in WEIGHER_IDS:
            try:
                await context.bot.send_message(ADMIN_ID, f"📋 {msg}")
            except Exception as e:
                logger.error(f"Не могу отправить админу: {e}")
    except Exception as e:
        logger.error(f"Ошибка создания заявки: {e}")
        await query.message.reply_text(f"❌ Ошибка при сохранении заявки: {e}")

    await show_dealer_menu(update, context)
    return DEALER_MENU

# ─── ВЕСОВЩИК ───

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
    logger.info(f"weigher_menu: {text}")

    if text == "🚛 Машина уехала":
        orders = db.get_orders_by_status("Ожидание")
        if not orders:
            await update.message.reply_text("Нет заявок в статусе 'Ожидание'.")
            return WEIGHER_MENU
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"№{o['id']} — {o['car_number']} ({o['dealer_name']})",
                callback_data=f"dep_{o['id']}"
            )
        ] for o in orders])
        await update.message.reply_text("Выберите заявку:", reply_markup=kb)

    elif text == "⚖️ Ввести реальный вес":
        orders = db.get_orders_by_status("Уехал")
        if not orders:
            await update.message.reply_text("Нет заявок в статусе 'Уехал'.")
            return WEIGHER_MENU
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"№{o['id']} — {o['car_number']} {o['tons_requested']}т",
                callback_data=f"weigh_{o['id']}"
            )
        ] for o in orders])
        await update.message.reply_text("Выберите заявку для ввода веса:", reply_markup=kb)

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
    logger.info(f"weigher_callback: {data}")

    if data.startswith("dep_"):
        order_id = int(data.split("_")[1])
        db.update_status(order_id, "Уехал")
        order = db.get_order(order_id)
        await query.message.reply_text(f"✅ Заявка №{order_id} — 🚛 Уехал")
        if order:
            try:
                await context.bot.send_message(
                    int(order["dealer_id"]),
                    f"🚛 Ваша машина {order['car_number']} уехала с завода.\nЗаявка №{order_id}"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления дилера: {e}")
        return WEIGHER_MENU

    elif data.startswith("weigh_"):
        order_id = int(data.split("_")[1])
        context.user_data["weigh_order_id"] = order_id
        order = db.get_order(order_id)
        await query.message.reply_text(
            f"⚖️ Заявка №{order_id}\n🚗 {order['car_number']}\n"
            f"📋 Заявлено: {order['tons_requested']} тонн\n\nВведите фактический вес (тонн):"
        )
        return ENTER_REAL_WEIGHT

    return WEIGHER_MENU

async def enter_real_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"enter_real_weight: {update.message.text}")
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
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ РАСХОЖДЕНИЕ ВЕСА!\n\nЗаявка №{order_id}\n"
                f"👤 {order['dealer_name']}\n🚗 {order['car_number']}\n"
                f"📋 Заявлено: {requested} т\n⚖️ Факт: {actual} т\nРазница: {diff:+.1f} т"
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления админа: {e}")

    await update.message.reply_text(msg)

    try:
        dealer_msg = f"✅ Ваш груз оформлен!\n\nЗаявка №{order_id}\n🚗 {order['car_number']}\n⚖️ Отгружено: {actual} тонн"
        if abs(diff) > 0.5:
            dealer_msg += f"\n⚠️ Заявлено было: {requested} т (разница {diff:+.1f} т)"
        await context.bot.send_message(int(order["dealer_id"]), dealer_msg)
    except Exception as e:
        logger.error(f"Ошибка уведомления дилера: {e}")

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
    logger.info(f"admin_menu: {text}")

    if text == "📊 Сегодня":
        today = datetime.now().strftime("%Y-%m-%d")
        orders = db.get_orders_by_date(today)
        if not orders:
            await update.message.reply_text(f"Сегодня ({today}) заявок нет.")
            return ADMIN_MENU
        total_req = sum(float(o["tons_requested"]) for o in orders if o.get("tons_requested"))
        total_act = sum(float(o["tons_actual"]) for o in orders if o.get("tons_actual"))
        done = len([o for o in orders if o["status"] == "Завершён"])
        await update.message.reply_text(
            f"📊 Сегодня {today}\n\nВсего заявок: {len(orders)}\n"
            f"Завершено: {done}\nЗаявлено тонн: {total_req:.1f}\nОтгружено тонн: {total_act:.1f}"
        )
        for o in orders:
            await update.message.reply_text(format_order(o))

    elif text == "📅 Все заявки":
        orders = db.get_all_orders(limit=20)
        if not orders:
            await update.message.reply_text("Заявок нет.")
            return ADMIN_MENU
        await update.message.reply_text(f"Последние {len(orders)} заявок:")
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
        await update.message.reply_text(f"⚠️ Найдено расхождений: {len(orders)}")
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

# ─── MAIN ───

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            DEALER_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, dealer_menu)],
            ENTER_CAR:   [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_car)],
            ENTER_TONS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_tons)],
            CONFIRM_ORDER: [CallbackQueryHandler(confirm_order, pattern="^confirm_")],
            WEIGHER_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, weigher_menu),
                CallbackQueryHandler(weigher_callback, pattern="^(dep_|weigh_)"),
            ],
            ENTER_REAL_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_real_weight)],
            ADMIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_menu)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(conv)
    logger.info("Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
