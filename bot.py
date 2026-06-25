import os, logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, ContextTypes, filters
from sheets import SheetsDB
from ai_engine import parse_order, parse_weight_channel, parse_check_photo, generate_report

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN      = os.environ.get("BOT_TOKEN","")
ADMIN_ID       = int(os.environ.get("ADMIN_ID","0"))
WEIGHER_IDS    = [int(x) for x in os.environ.get("WEIGHER_IDS","0").split(",") if x.strip()]
ACCOUNTANT_IDS = [int(x) for x in os.environ.get("ACCOUNTANT_IDS","0").split(",") if x.strip()]
WEIGHT_CHANNEL = int(os.environ.get("WEIGHT_CHANNEL_ID","0"))

(ADMIN_MENU, ACCT_MENU, ACCT_PAYMENT_DEALER, ACCT_PAYMENT_AMOUNT,
 ACCT_PAYMENT_TYPE, ACCT_SET_PRICE_DEALER, ACCT_SET_PRICE_VAL) = range(7)

db = SheetsDB()

def is_admin(uid):      return uid == ADMIN_ID
def is_weigher(uid):    return uid in WEIGHER_IDS or uid == ADMIN_ID
def is_accountant(uid): return uid in ACCOUNTANT_IDS or uid == ADMIN_ID

def fmt_money(n):
    try: return f"{float(n):,.0f} сум"
    except: return "0 сум"

def fmt_order(o):
    e = {"Ожидание":"🟡","Уехал":"🚛","Завершён":"✅","Расхождение":"⚠️","Отменён":"❌"}.get(str(o.get("Статус","")),"❓")
    lines = [
        f"{e} Заявка №{o.get('ID')}",
        f"📅 {o.get('Дата')} {o.get('Время','')}",
        f"👤 {o.get('Дилер')}",
        f"🚗 {o.get('Машина')}",
        f"⚖️ Заявлено: {o.get('Тонн_заявлено')} т",
    ]
    if o.get("Тонн_факт"):
        lines.append(f"⚖️ Факт: {o.get('Тонн_факт')} т ({o.get('КГ_факт','')} кг)")
    if o.get("Сумма") and float(o.get("Сумма",0) or 0) > 0:
        lines.append(f"💰 Сумма: {fmt_money(o.get('Сумма'))}")
    lines.append(f"📊 {o.get('Статус')}")
    return "\n".join(lines)

# ════════════════════════════════════════
# ГРУППА — кнопки для дилера
# ════════════════════════════════════════

async def show_group_menu(chat_id, context, dealer_name=""):
    """Показываем кнопки в группе"""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Новая заявка", callback_data="group_new"),
         InlineKeyboardButton("📊 Мои заявки", callback_data="group_myorders")],
        [InlineKeyboardButton("🔄 Незавершённые", callback_data="group_active"),
         InlineKeyboardButton("💳 Мой баланс", callback_data="group_balance")],
    ])
    text = "🏭 Меню дилера\nНапишите заявку в чат или нажмите кнопку:"
    await context.bot.send_message(chat_id, text, reply_markup=kb)

async def handle_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки в группе"""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat = update.effective_chat
    user = update.effective_user

    dealer = db.get_dealer_by_group(str(chat.id))
    dealer_name = dealer["Имя"] if dealer else user.full_name

    if data == "group_new":
        await query.message.reply_text(
            "📋 Напишите заявку в чат:\n\n"
            "Формат: <b>номер_машины количество_тонн</b>\n"
            "Пример: <b>50711VBA 25 т</b>",
            parse_mode="HTML"
        )

    elif data == "group_myorders":
        orders = db.get_dealer_orders(dealer_name, limit=10)
        if not orders:
            await query.message.reply_text("У вас ещё нет заявок.")
            return
        await query.message.reply_text(f"📊 Последние заявки ({dealer_name}):")
        for o in orders:
            await query.message.reply_text(fmt_order(o))

    elif data == "group_active":
        orders = db.get_dealer_orders(dealer_name, limit=50)
        active = [o for o in orders if o.get("Статус") in ("Ожидание","Уехал")]
        if not active:
            await query.message.reply_text("✅ Нет незавершённых заявок.")
            return
        await query.message.reply_text(f"🔄 Незавершённые заявки ({len(active)}):")
        for o in active:
            await query.message.reply_text(fmt_order(o))

    elif data == "group_balance":
        fin = db.get_dealer_finance(dealer_name)
        bal = fin["balance"]
        msg = f"💳 Баланс дилера: {dealer_name}\n\n"
        if bal < 0:
            msg += f"🔴 Долг: {fmt_money(abs(bal))}"
        elif bal > 0:
            msg += f"🟢 Предоплата: {fmt_money(bal)}"
        else:
            msg += "⚪ Баланс: 0"
        msg += f"\n💰 Цена за тонну: {fmt_money(fin['price'])}"
        await query.message.reply_text(msg)

    elif data.startswith("gok_"):
        oid = int(data.split("_")[1])
        await query.message.edit_text(
            query.message.text.replace("Всё верно?", "✅ Подтверждено! Ожидаем машину на весах.")
        )
        # Показываем меню снова
        await show_group_menu(chat.id, context, dealer_name)

    elif data.startswith("gno_"):
        oid = int(data.split("_")[1])
        db.update_status(oid, "Отменён")
        await query.message.edit_text(f"❌ Заявка №{oid} отменена.")
        await show_group_menu(chat.id, context, dealer_name)

# ════════════════════════════════════════
# ГРУППА — текстовые сообщения (заявки)
# ════════════════════════════════════════

async def handle_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    user = update.effective_user
    text = update.message.text

    # Игнорируем команды
    if text.startswith("/"):
        return

    dealer = db.get_dealer_by_group(str(chat.id))
    dealer_name = dealer["Имя"] if dealer else (chat.title or user.full_name)

    logger.info(f"Группа [{chat.title}] {user.full_name}: {text[:60]}")

    # AI/regex распознаёт заявку
    parsed = await parse_order(text, dealer_name)
    if not parsed.get("car") or not parsed.get("tons"):
        logger.info(f"Не заявка: {text[:40]}")
        return

    car = parsed["car"]
    tons = parsed["tons"]

    finance = db.get_dealer_finance(dealer_name)
    price = finance["price"]
    summa = round(tons * price, 2) if price else 0
    balance = finance["balance"]

    try:
        oid = db.add_order(dealer_name, user.id, car, tons, group_id=chat.id, source=f"группа:{chat.title}")

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Верно", callback_data=f"gok_{oid}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"gno_{oid}")
        ]])

        msg = (
            f"📋 Заявка №{oid} принята!\n\n"
            f"🚗 Машина: {car}\n"
            f"⚖️ Тонн: {tons}\n"
            f"👤 {dealer_name}\n"
        )
        if price:
            msg += f"💰 Сумма: {fmt_money(summa)}\n"
        if balance < 0:
            msg += f"⚠️ Долг: {fmt_money(abs(balance))}\n"
        elif balance > 0:
            msg += f"✅ Предоплата: {fmt_money(balance)}\n"
        msg += "\nВсё верно?"

        await update.message.reply_text(msg, reply_markup=kb)

        # Уведомляем весовщиков и админа
        notif = f"🔔 Новая заявка №{oid}\n👤 {dealer_name}\n🚗 {car}\n⚖️ {tons} т\n📍 {chat.title}"
        for wid in WEIGHER_IDS:
            try: await context.bot.send_message(wid, notif)
            except: pass
        if ADMIN_ID and ADMIN_ID not in WEIGHER_IDS:
            try: await context.bot.send_message(ADMIN_ID, f"📋 {notif}")
            except: pass

    except Exception as e:
        logger.error(f"Ошибка создания заявки: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")

# ════════════════════════════════════════
# ГРУППА — фото чека
# ════════════════════════════════════════

async def handle_group_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return
    chat = update.effective_chat
    caption = update.message.caption or ""

    await update.message.reply_text("📸 Читаю чек...")

    # Скачиваем фото
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_data = await file.download_as_bytearray()

    # AI читает чек
    parsed = await parse_check_photo(bytes(image_data))
    logger.info(f"Чек распознан: {parsed}")

    car = parsed.get("car","")
    kg = parsed.get("kg", 0)
    tons = parsed.get("tons", round(kg/1000, 3) if kg else 0)

    # Если AI не прочитал — просим ввести вручную
    if not car and not kg:
        # Ищем активную заявку в этой группе
        active = db.get_active_orders()
        group_orders = [o for o in active if str(o.get("Group_ID","")) == str(chat.id)]
        if group_orders:
            o = group_orders[-1]
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"✅ Закрыть заявку №{o['ID']} ({o['Машина']})", callback_data=f"photo_close_{o['ID']}")
            ]])
            context.chat_data["last_photo_kg"] = 0
            await update.message.reply_text(
                "⚠️ Не смог прочитать чек автоматически.\n\n"
                f"Активная заявка: №{o['ID']} — {o['Машина']} ({o['Тонн_заявлено']} т)\n\n"
                "Напиши вес в кг (например: 24500) или нажми кнопку:",
                reply_markup=kb
            )
            context.chat_data["pending_close_order"] = o["ID"]
        else:
            await update.message.reply_text(
                "⚠️ Нет активных заявок. Напиши номер машины и вес:\n"
                "Пример: 50711VBA 24500 кг"
            )
        return

    # Ищем заявку по номеру машины
    order = db.find_order_by_car(car) if car else None

    # Если не нашли по машине — берём последнюю активную в группе
    if not order:
        active = db.get_active_orders()
        group_orders = [o for o in active if str(o.get("Group_ID","")) == str(chat.id)]
        if group_orders:
            order = group_orders[-1]

    if not order:
        await update.message.reply_text(
            f"📸 Чек прочитан:\n🚗 {car or '?'}\n⚖️ {kg} кг\n\n"
            "⚠️ Не найдена активная заявка.\n"
            "Напиши: /close [номер_заявки] [кг]"
        )
        return

    oid = order["ID"]
    result = db.close_order(oid, kg, tons)
    if not result:
        return

    requested = float(order.get("Тонн_заявлено",0) or 0)
    diff_kg = kg - int(requested * 1000)

    msg = (
        f"✅ Чек принят! Заявка №{oid} закрыта.\n\n"
        f"🚗 {order.get('Машина')}\n"
        f"📋 Заявлено: {int(requested*1000)} кг\n"
        f"⚖️ Факт: {kg} кг\n"
    )
    if result.get("summa") and result["summa"] > 0:
        msg += f"💰 Сумма: {fmt_money(result['summa'])}\n"
    if abs(diff_kg) > 50:
        msg += f"⚠️ Расхождение: {diff_kg:+} кг\n"
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ РАСХОЖДЕНИЕ!\nЗаявка №{oid}\n"
                f"👤 {order.get('Дилер')}\n🚗 {order.get('Машина')}\n"
                f"Заявлено: {int(requested*1000)} кг\nФакт: {kg} кг\nРазница: {diff_kg:+} кг"
            )
        except: pass
    else:
        msg += "✅ Вес в норме\n"

    await update.message.reply_text(msg)

    # Показываем меню снова
    dealer = db.get_dealer_by_group(str(chat.id))
    dealer_name = dealer["Имя"] if dealer else ""
    await show_group_menu(chat.id, context, dealer_name)

    # Уведомление дилеру в личку
    if dealer and dealer.get("TG_ID"):
        fin = db.get_dealer_finance(dealer["Имя"])
        try:
            await context.bot.send_message(
                int(dealer["TG_ID"]),
                f"✅ Груз оформлен!\nЗаявка №{oid}\n🚗 {order.get('Машина')}\n"
                f"⚖️ {tons} т ({kg} кг)\n💰 {fmt_money(result.get('summa',0))}\n"
                f"💳 Баланс: {fmt_money(fin['balance'])}"
            )
        except: pass

# ════════════════════════════════════════
# КАНАЛ ВЕСОВОЙ
# ════════════════════════════════════════

async def handle_weight_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.channel_post:
        return
    if WEIGHT_CHANNEL and update.channel_post.chat.id != WEIGHT_CHANNEL:
        return
    text = update.channel_post.text or ""
    if not text:
        return
    logger.info(f"Канал весовой: {text[:80]}")
    parsed = await parse_weight_channel(text)
    if not parsed.get("car") or not parsed.get("kg"):
        return
    car = parsed["car"]
    kg = parsed["kg"]
    tons = parsed["tons"]
    order = db.find_order_by_car(car)
    if not order:
        return
    oid = order["ID"]
    result = db.close_order(oid, kg, tons)
    if not result:
        return
    requested = float(order.get("Тонн_заявлено",0) or 0)
    diff_kg = kg - int(requested * 1000)
    msg = (
        f"🤖 Авто-закрытие №{oid}\n\n"
        f"👤 {order.get('Дилер')}\n🚗 {car}\n"
        f"📋 Заявлено: {int(requested*1000)} кг\n"
        f"⚖️ Факт: {kg} кг\n"
    )
    if result.get("summa"):
        msg += f"💰 {fmt_money(result['summa'])}\n"
    if abs(diff_kg) > 50:
        msg += f"⚠️ Расхождение: {diff_kg:+} кг"
    try: await context.bot.send_message(ADMIN_ID, msg)
    except: pass

# ════════════════════════════════════════
# БУХГАЛТЕР
# ════════════════════════════════════════

async def show_acct_menu(update, context):
    kb = ReplyKeyboardMarkup([
        ["💰 Добавить оплату","💳 Установить цену"],
        ["📊 Долги дилеров","📋 История оплат"],
    ], resize_keyboard=True)
    msg = update.message or update.callback_query.message
    await msg.reply_text("💼 Меню бухгалтера:", reply_markup=kb)

async def acct_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "💰 Добавить оплату":
        dealers = db.get_all_dealers()
        if not dealers:
            await update.message.reply_text("Нет дилеров.")
            return ACCT_MENU
        lines = ["Напишите имя дилера:\n"]
        for d in dealers:
            fin = db.get_dealer_finance(d["Имя"])
            bal = fin["balance"]
            bal_str = f"🔴 долг {fmt_money(abs(bal))}" if bal < 0 else f"🟢 {fmt_money(bal)}"
            lines.append(f"• {d['Имя']} — {bal_str}")
        await update.message.reply_text("\n".join(lines))
        return ACCT_PAYMENT_DEALER
    elif text == "💳 Установить цену":
        dealers = db.get_all_dealers()
        lines = ["Напишите имя дилера:\n"]
        for d in dealers:
            lines.append(f"• {d['Имя']} — {fmt_money(d.get('Цена_за_тонну',0))}/т")
        await update.message.reply_text("\n".join(lines))
        return ACCT_SET_PRICE_DEALER
    elif text == "📊 Долги дилеров":
        debts = db.get_all_debts()
        if not debts:
            await update.message.reply_text("Нет данных.")
            return ACCT_MENU
        lines = ["📊 Финансы:\n"]
        for d in debts:
            bal = float(d.get("Баланс",0) or 0)
            if bal < 0: lines.append(f"🔴 {d['Дилер']}: долг {fmt_money(abs(bal))}")
            elif bal > 0: lines.append(f"🟢 {d['Дилер']}: предоплата {fmt_money(bal)}")
            else: lines.append(f"⚪ {d['Дилер']}: 0")
        await update.message.reply_text("\n".join(lines))
    elif text == "📋 История оплат":
        payments = db.get_payments(limit=15)
        if not payments:
            await update.message.reply_text("Оплат нет.")
            return ACCT_MENU
        lines = ["📋 Оплаты:\n"]
        for p in payments:
            lines.append(f"✅ {p['Дата']} | {p['Дилер']} | {fmt_money(p['Сумма'])}")
        await update.message.reply_text("\n".join(lines))
    return ACCT_MENU

async def acct_payment_dealer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dealer = db.get_dealer_by_name(update.message.text.strip())
    if not dealer:
        await update.message.reply_text("❌ Не найден. Напишите ещё раз:")
        return ACCT_PAYMENT_DEALER
    context.user_data["pay_dealer"] = dealer["Имя"]
    fin = db.get_dealer_finance(dealer["Имя"])
    await update.message.reply_text(
        f"👤 {dealer['Имя']}\n💳 Баланс: {fmt_money(fin['balance'])}\n\nВведите сумму оплаты:"
    )
    return ACCT_PAYMENT_AMOUNT

async def acct_payment_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(" ","").replace(",","."))
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите сумму:")
        return ACCT_PAYMENT_AMOUNT
    context.user_data["pay_amount"] = amount
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💵 Наличные", callback_data="pay_cash"),
        InlineKeyboardButton("🏦 Перевод", callback_data="pay_transfer"),
        InlineKeyboardButton("📱 Карта", callback_data="pay_card"),
    ]])
    await update.message.reply_text(f"Сумма: {fmt_money(amount)}\nТип оплаты:", reply_markup=kb)
    return ACCT_PAYMENT_TYPE

async def acct_payment_type_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    types = {"pay_cash":"Наличные","pay_transfer":"Перевод","pay_card":"Карта"}
    pay_type = types.get(query.data,"Другое")
    dealer = context.user_data["pay_dealer"]
    amount = context.user_data["pay_amount"]
    pid = db.add_payment(dealer, amount, pay_type, "", update.effective_user.full_name)
    fin = db.get_dealer_finance(dealer)
    await query.message.reply_text(
        f"✅ Оплата №{pid}!\n👤 {dealer}\n💰 {fmt_money(amount)} ({pay_type})\n💳 Баланс: {fmt_money(fin['balance'])}"
    )
    try: await context.bot.send_message(ADMIN_ID, f"💰 Оплата: {dealer} — {fmt_money(amount)} ({pay_type})")
    except: pass
    await show_acct_menu(update, context)
    return ACCT_MENU

async def acct_set_price_dealer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dealer = db.get_dealer_by_name(update.message.text.strip())
    if not dealer:
        await update.message.reply_text("❌ Не найден:")
        return ACCT_SET_PRICE_DEALER
    context.user_data["price_dealer"] = dealer["Имя"]
    await update.message.reply_text(f"👤 {dealer['Имя']}\nТекущая цена: {fmt_money(dealer.get('Цена_за_тонну',0))}/т\n\nНовая цена за тонну:")
    return ACCT_SET_PRICE_VAL

async def acct_set_price_val(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.replace(" ","").replace(",","."))
        if price <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите цену:")
        return ACCT_SET_PRICE_VAL
    dealer = context.user_data["price_dealer"]
    db.set_dealer_price(dealer, price)
    await update.message.reply_text(f"✅ Цена для {dealer}: {fmt_money(price)}/т")
    await show_acct_menu(update, context)
    return ACCT_MENU

# ════════════════════════════════════════
# АДМИН
# ════════════════════════════════════════

async def show_admin_menu(update, context):
    kb = ReplyKeyboardMarkup([
        ["📊 Сегодня","🚛 Активные"],
        ["👥 По дилерам","💰 Финансы"],
        ["⚠️ Расхождения","🤖 AI Отчёт"],
        ["➕ Добавить дилера"],
    ], resize_keyboard=True)
    msg = update.message or update.callback_query.message
    await msg.reply_text("🏭 Панель директора — Цементный завод", reply_markup=kb)

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 Сегодня":
        stats = db.get_stats_today()
        await update.message.reply_text(
            f"📊 {stats['date']}\nЗаявок: {stats['orders']} | Завершено: {stats['done']}\n"
            f"Тонн: {stats['tons']:.2f} т\nСумма: {fmt_money(stats['sum'])}"
        )
        for o in db.get_orders_by_date(stats['date']):
            await update.message.reply_text(fmt_order(o))
    elif text == "🚛 Активные":
        orders = db.get_active_orders()
        if not orders:
            await update.message.reply_text("Нет активных заявок.")
        else:
            await update.message.reply_text(f"🚛 Активных: {len(orders)}")
            for o in orders: await update.message.reply_text(fmt_order(o))
    elif text == "👥 По дилерам":
        dealers = db.get_all_dealers()
        if not dealers:
            await update.message.reply_text("Нет дилеров.")
            return ADMIN_MENU
        lines = ["👥 Дилеры:\n"]
        for d in dealers:
            orders = db.get_dealer_orders(d["Имя"], limit=100)
            total = sum(float(o.get("Тонн_факт",0) or 0) for o in orders)
            lines.append(f"👤 {d['Имя']} | Заявок: {len(orders)} | {total:.1f} т | {fmt_money(d.get('Цена_за_тонну',0))}/т")
        await update.message.reply_text("\n".join(lines))
    elif text == "💰 Финансы":
        debts = db.get_all_debts()
        if not debts:
            await update.message.reply_text("Нет данных.")
            return ADMIN_MENU
        lines = ["💰 Финансы:\n"]
        total_debt = total_pre = 0
        for d in debts:
            bal = float(d.get("Баланс",0) or 0)
            if bal < 0:
                lines.append(f"🔴 {d['Дилер']}: долг {fmt_money(abs(bal))}")
                total_debt += abs(bal)
            elif bal > 0:
                lines.append(f"🟢 {d['Дилер']}: предоплата {fmt_money(bal)}")
                total_pre += bal
            else:
                lines.append(f"⚪ {d['Дилер']}: 0")
        lines.append(f"\n📉 Долгов всего: {fmt_money(total_debt)}")
        lines.append(f"📈 Предоплат всего: {fmt_money(total_pre)}")
        await update.message.reply_text("\n".join(lines))
    elif text == "⚠️ Расхождения":
        orders = [o for o in db._orders_ws().get_all_records() if o.get("Статус")=="Расхождение"]
        if not orders:
            await update.message.reply_text("✅ Расхождений нет!")
        else:
            for o in orders: await update.message.reply_text(fmt_order(o))
    elif text == "🤖 AI Отчёт":
        await update.message.reply_text("🤖 Генерирую...")
        report = await generate_report(db.get_stats_today(), db.get_all_debts())
        await update.message.reply_text(f"🤖 Отчёт:\n\n{report}")
    elif text == "➕ Добавить дилера":
        await update.message.reply_text("Команда: /addealer Имя Цена\nПример: /addealer Алишер 850000")
    return ADMIN_MENU

async def cmd_add_dealer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Формат: /addealer Имя Цена")
        return
    name = args[0]
    try: price = float(args[1])
    except: price = 0
    db.add_dealer(name, price=price)
    await update.message.reply_text(f"✅ Дилер {name} добавлен. Цена: {fmt_money(price)}/т")

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Формат: /close [номер] [кг]")
        return
    try:
        oid = int(args[0]); kg = int(args[1])
        tons = round(kg/1000, 3)
        result = db.close_order(oid, kg, tons)
        if result:
            await update.message.reply_text(f"✅ Заявка №{oid} закрыта! {kg} кг ({tons} т)")
        else:
            await update.message.reply_text(f"❌ Заявка №{oid} не найдена.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

# ════════════════════════════════════════
# START
# ════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat = update.effective_chat

    if chat.type in ("group","supergroup"):
        dealer = db.get_dealer_by_group(str(chat.id))
        if not dealer:
            db.update_dealer_group(uid, chat.id, chat.title or "Группа")
        await show_group_menu(chat.id, context)
        return

    if is_admin(uid):
        await show_admin_menu(update, context)
        return ADMIN_MENU
    elif is_accountant(uid):
        await show_acct_menu(update, context)
        return ACCT_MENU
    else:
        db.ensure_dealer(uid, update.effective_user.full_name)
        await update.message.reply_text(
            "👋 Для заявок пишите в группу вашего дилера.\n"
            "Бот автоматически распознает заявку."
        )

# ════════════════════════════════════════
# MAIN
# ════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ADMIN_MENU:  [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, admin_menu)],
            ACCT_MENU:   [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, acct_menu)],
            ACCT_PAYMENT_DEALER: [MessageHandler(filters.TEXT & ~filters.COMMAND, acct_payment_dealer)],
            ACCT_PAYMENT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, acct_payment_amount)],
            ACCT_PAYMENT_TYPE:   [CallbackQueryHandler(acct_payment_type_cb, pattern="^pay_")],
            ACCT_SET_PRICE_DEALER:[MessageHandler(filters.TEXT & ~filters.COMMAND, acct_set_price_dealer)],
            ACCT_SET_PRICE_VAL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, acct_set_price_val)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True, per_message=False,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("addealer", cmd_add_dealer))
    app.add_handler(CommandHandler("close", cmd_close))

    # Кнопки в группах
    app.add_handler(CallbackQueryHandler(handle_group_callback, pattern="^(group_|gok_|gno_|photo_close_)"))

    # Сообщения в группах
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        handle_group_text
    ))
    app.add_handler(MessageHandler(
        filters.PHOTO & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        handle_group_photo
    ))

    # Канал весовой
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, handle_weight_channel))

    logger.info("🤖 AI-бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
