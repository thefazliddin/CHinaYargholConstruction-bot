import os
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
from sheets import SheetsDB

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
WEIGHER_IDS = [int(x) for x in os.environ.get("WEIGHER_IDS", "0").split(",") if x.strip()]

(
    DEALER_MENU, ENTER_CAR, ENTER_TONS, CONFIRM_ORDER,
    WEIGHER_MENU, ENTER_KG,
    ADMIN_MENU
) = range(7)

db = SheetsDB()

def is_admin(uid): return uid == ADMIN_ID
def is_weigher(uid): return uid in WEIGHER_IDS or uid == ADMIN_ID

def format_order(o):
    e = {"Ожидание":"🟡","Уехал":"🚛","Завершён":"✅","Расхождение":"⚠️"}.get(o.get("status",""),"❓")
    lines = [
        f"{e} Заявка №{o['id']}",
        f"📅 {o['date']}",
        f"👤 {o['dealer_name']}",
        f"🚗 {o['car_number']}",
        f"⚖️ Заявлено: {o['tons_requested']} т",
    ]
    if o.get("tons_actual"):
        lines.append(f"⚖️ Факт: {o['tons_actual']} т")
        try:
            diff = float(o['tons_actual']) - float(o['tons_requested'])
            if abs(diff) > 0.05:
                lines.append(f"{'📈' if diff>0 else '📉'} Разница: {diff:+.2f} т")
        except: pass
    lines.append(f"📊 {o.get('status','?')}")
    return "\n".join(lines)

# ─── START ───
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    logger.info(f"START uid={uid}")
    if is_admin(uid):
        await show_admin_menu(update, context)
        return ADMIN_MENU
    elif is_weigher(uid):
        await show_weigher_menu(update, context)
        return WEIGHER_MENU
    else:
        db.ensure_dealer(uid, update.effective_user.full_name)
        await show_dealer_menu(update, context)
        return DEALER_MENU

# ─── ДИЛЕР ───
async def show_dealer_menu(update, context):
    kb = ReplyKeyboardMarkup([["📋 Новая заявка"], ["📊 Мои заявки"]], resize_keyboard=True)
    msg = update.message or update.callback_query.message
    await msg.reply_text("👋 Меню дилера:", reply_markup=kb)

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
            for o in orders: await update.message.reply_text(format_order(o))
        return DEALER_MENU
    await show_dealer_menu(update, context)
    return DEALER_MENU

async def enter_car(update: Update, context: ContextTypes.DEFAULT_TYPE):
    car = update.message.text.strip().upper()
    if len(car) < 3:
        await update.message.reply_text("❌ Слишком короткий. Попробуйте ещё раз:")
        return ENTER_CAR
    context.user_data["car"] = car
    await update.message.reply_text(f"✅ Машина: {car}\n\n⚖️ Сколько тонн хотите взять?")
    return ENTER_TONS

async def enter_tons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tons = float(update.message.text.replace(",", "."))
        if tons <= 0 or tons > 200: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите число (например: 25):")
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
    if query.data == "confirm_no":
        await query.message.reply_text("❌ Отменено.")
        await show_dealer_menu(update, context)
        return DEALER_MENU
    user = update.effective_user
    car = context.user_data["car"]
    tons = context.user_data["tons"]
    try:
        order_id = db.add_order(dealer_id=user.id, dealer_name=user.full_name, car_number=car, tons_requested=tons)
        await query.message.reply_text(f"✅ Заявка №{order_id} создана!\n🚗 {car}\n⚖️ {tons} тонн\n📊 Ожидание")
        msg = f"🔔 Новая заявка №{order_id}\n👤 {user.full_name}\n🚗 {car}\n⚖️ {tons} тонн"
        for wid in WEIGHER_IDS:
            try: await context.bot.send_message(wid, msg)
            except Exception as e: logger.error(f"Весовщик {wid}: {e}")
        if ADMIN_ID and ADMIN_ID not in WEIGHER_IDS:
            try: await context.bot.send_message(ADMIN_ID, f"📋 {msg}")
            except: pass
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка: {e}")
    await show_dealer_menu(update, context)
    return DEALER_MENU

# ─── ВЕСОВЩИК (упрощённый) ───
async def show_weigher_menu(update, context):
    kb = ReplyKeyboardMarkup([
        ["⚖️ Машина уехала — ввести вес"],
        ["📋 Активные заявки"]
    ], resize_keyboard=True)
    msg = update.message or update.callback_query.message
    await msg.reply_text(
        "⚖️ Меню весовщика:\n\n"
        "Нажми «Машина уехала» → выбери машину → введи вес в кг",
        reply_markup=kb
    )

async def weigher_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "⚖️ Машина уехала — ввести вес":
        orders = db.get_orders_by_status("Ожидание")
        if not orders:
            await update.message.reply_text("🟡 Нет заявок в ожидании.")
            return WEIGHER_MENU
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"🚗 {o['car_number']}  |  {o['dealer_name']}  |  {o['tons_requested']} т",
                callback_data=f"pick_{o['id']}"
            )
        ] for o in orders])
        await update.message.reply_text("Выберите машину которая уезжает:", reply_markup=kb)

    elif text == "📋 Активные заявки":
        orders = db.get_active_orders()
        if not orders:
            await update.message.reply_text("Нет активных заявок.")
        else:
            for o in orders: await update.message.reply_text(format_order(o))

    return WEIGHER_MENU

async def weigher_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split("_")[1])
    order = db.get_order(order_id)
    context.user_data["weigh_order_id"] = order_id

    # Сразу меняем статус на Уехал
    db.update_status(order_id, "Уехал")

    await query.message.reply_text(
        f"🚛 Машина уехала!\n\n"
        f"Заявка №{order_id}\n"
        f"🚗 {order['car_number']}\n"
        f"👤 {order['dealer_name']}\n"
        f"📋 Заявлено: {order['tons_requested']} т\n\n"
        f"⚖️ Введите фактический вес в КГ:"
    )

    # Уведомляем дилера что машина уехала
    try:
        await context.bot.send_message(
            int(order["dealer_id"]),
            f"🚛 Ваша машина {order['car_number']} уехала с завода.\nЗаявка №{order_id}"
        )
    except: pass

    return ENTER_KG

async def enter_kg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        kg = float(update.message.text.replace(",", ".").replace(" ", ""))
        if kg <= 0 or kg > 200000: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите вес в КГ (например: 24500):")
        return ENTER_KG

    order_id = context.user_data.get("weigh_order_id")
    order = db.get_order(order_id)
    tons_actual = round(kg / 1000, 3)
    tons_requested = float(order["tons_requested"])
    diff_tons = tons_actual - tons_requested
    diff_kg = kg - (tons_requested * 1000)

    db.update_weight(order_id, tons_actual)

    # Сообщение весовщику
    msg = (
        f"✅ Заявка №{order_id} закрыта!\n\n"
        f"🚗 {order['car_number']}\n"
        f"👤 {order['dealer_name']}\n\n"
        f"📋 Заявлено: {tons_requested} т ({int(tons_requested*1000)} кг)\n"
        f"⚖️ Факт:     {tons_actual} т ({int(kg)} кг)\n"
    )
    if abs(diff_kg) > 50:
        msg += f"\n⚠️ Расхождение: {diff_kg:+.0f} кг ({diff_tons:+.3f} т)"
    else:
        msg += f"\n✅ В норме (разница {diff_kg:+.0f} кг)"

    await update.message.reply_text(msg)

    # Уведомление тебе если расхождение
    if abs(diff_kg) > 50:
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ РАСХОЖДЕНИЕ ВЕСА!\n\n"
                f"Заявка №{order_id}\n"
                f"👤 {order['dealer_name']}\n"
                f"🚗 {order['car_number']}\n"
                f"📋 Заявлено: {int(tons_requested*1000)} кг\n"
                f"⚖️ Факт: {int(kg)} кг\n"
                f"❗ Разница: {diff_kg:+.0f} кг"
            )
        except: pass

    # Уведомление дилеру
    try:
        dealer_msg = (
            f"✅ Ваш груз оформлен!\n\n"
            f"Заявка №{order_id}\n"
            f"🚗 {order['car_number']}\n"
            f"⚖️ Отгружено: {int(kg)} кг ({tons_actual} т)"
        )
        if abs(diff_kg) > 50:
            dealer_msg += f"\n⚠️ Заявлено было: {int(tons_requested*1000)} кг (разница {diff_kg:+.0f} кг)"
        await context.bot.send_message(int(order["dealer_id"]), dealer_msg)
    except: pass

    await show_weigher_menu(update, context)
    return WEIGHER_MENU

# ─── АДМИН ───
async def show_admin_menu(update, context):
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
            f"📊 Сегодня {today}\n\n"
            f"Всего заявок: {len(orders)}\n"
            f"Завершено: {done}\n"
            f"Заявлено: {total_req:.1f} т ({int(total_req*1000)} кг)\n"
            f"Отгружено: {total_act:.1f} т ({int(total_act*1000)} кг)"
        )
        for o in orders: await update.message.reply_text(format_order(o))
    elif text == "📅 Все заявки":
        orders = db.get_all_orders(limit=20)
        if not orders:
            await update.message.reply_text("Заявок нет.")
            return ADMIN_MENU
        for o in orders: await update.message.reply_text(format_order(o))
    elif text == "👥 По дилерам":
        stats = db.get_dealer_stats()
        if not stats:
            await update.message.reply_text("Нет данных.")
            return ADMIN_MENU
        lines = ["👥 Статистика по дилерам:\n"]
        for s in stats:
            lines.append(f"👤 {s['dealer_name']}\n   Заявок: {s['count']} | Тонн: {s['total_tons']:.2f} т\n")
        await update.message.reply_text("\n".join(lines))
    elif text == "⚠️ Расхождения":
        orders = db.get_mismatched_orders()
        if not orders:
            await update.message.reply_text("✅ Расхождений нет!")
            return ADMIN_MENU
        await update.message.reply_text(f"⚠️ Расхождений: {len(orders)}")
        for o in orders: await update.message.reply_text(format_order(o))
    elif text == "🚛 Активные":
        orders = db.get_active_orders()
        if not orders:
            await update.message.reply_text("Нет активных заявок.")
            return ADMIN_MENU
        for o in orders: await update.message.reply_text(format_order(o))
    return ADMIN_MENU

# ─── MAIN ───
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            DEALER_MENU:   [MessageHandler(filters.TEXT & ~filters.COMMAND, dealer_menu)],
            ENTER_CAR:     [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_car)],
            ENTER_TONS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_tons)],
            CONFIRM_ORDER: [CallbackQueryHandler(confirm_order, pattern="^confirm_")],
            WEIGHER_MENU:  [
                MessageHandler(filters.TEXT & ~filters.COMMAND, weigher_menu),
                CallbackQueryHandler(weigher_pick_callback, pattern="^pick_"),
            ],
            ENTER_KG:      [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_kg)],
            ADMIN_MENU:    [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_menu)],
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
