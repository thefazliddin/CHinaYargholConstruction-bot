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
WEIGHT_CHANNEL = int(os.environ.get("WEIGHT_CHANNEL_ID","0"))  # ID канала весовой

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
    e = {"Ожидание":"🟡","Уехал":"🚛","Завершён":"✅","Расхождение":"⚠️"}.get(str(o.get("Статус","")),"❓")
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
# ГРУППОВЫЕ СООБЩЕНИЯ — заявки дилеров
# ════════════════════════════════════════

async def handle_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    user = update.effective_user
    text = update.message.text

    # Определяем дилера по группе
    dealer = db.get_dealer_by_group(str(chat.id))
    dealer_name = dealer["Имя"] if dealer else (chat.title or user.full_name)

    logger.info(f"Группа [{chat.title}] {user.full_name}: {text[:60]}")

    # AI распознаёт заявку
    parsed = await parse_order(text, dealer_name)
    if not parsed.get("car") or not parsed.get("tons"):
        return  # не заявка — игнорируем

    car = parsed["car"]
    tons = parsed["tons"]
    product = parsed.get("product", "Цемент")

    # Финансы дилера
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
            f"📋 Заявка принята!\n\n"
            f"🏭 {product}\n"
            f"🚗 Машина: {car}\n"
            f"⚖️ Тонн: {tons}\n"
            f"👤 {dealer_name}\n"
        )
        if price:
            msg += f"💰 Сумма: {fmt_money(summa)}\n"
        if balance < 0:
            msg += f"⚠️ Долг дилера: {fmt_money(abs(balance))}\n"
        elif balance > 0:
            msg += f"✅ Предоплата: {fmt_money(balance)}\n"
        msg += "\nВсё верно?"
        await update.message.reply_text(msg, reply_markup=kb)

        # Уведомляем весовщиков
        notif = f"🔔 Новая заявка №{oid}\n👤 {dealer_name}\n🚗 {car}\n⚖️ {tons} т\n📍 {chat.title}"
        for wid in WEIGHER_IDS:
            try: await context.bot.send_message(wid, notif)
            except: pass
        if ADMIN_ID and ADMIN_ID not in WEIGHER_IDS:
            try: await context.bot.send_message(ADMIN_ID, f"📋 {notif}")
            except: pass

    except Exception as e:
        logger.error(f"Ошибка создания заявки: {e}")

async def handle_group_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фото чека в группе дилера"""
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

    if not parsed.get("car") and not parsed.get("kg"):
        await update.message.reply_text("⚠️ Не смог прочитать чек. Напиши номер машины и вес вручную.")
        return

    car = parsed.get("car","")
    kg = parsed.get("kg", 0)
    tons = parsed.get("tons", round(kg/1000, 3) if kg else 0)

    # Ищем заявку по номеру машины
    order = db.find_order_by_car(car) if car else None

    if not order:
        await update.message.reply_text(
            f"📸 Чек прочитан:\n🚗 {car}\n⚖️ {kg} кг ({tons} т)\n\n"
            f"⚠️ Не найдена активная заявка для этой машины.\n"
            f"Укажи номер заявки в ответном сообщении: /close [номер] [кг]"
        )
        return

    oid = order["ID"]
    result = db.close_order(oid, kg, tons)
    if not result:
        return

    # Формируем ответ
    requested = float(order.get("Тонн_заявлено",0) or 0)
    diff_kg = kg - int(requested * 1000)
    msg = (
        f"✅ Чек принят! Заявка №{oid} закрыта.\n\n"
        f"🚗 {car}\n"
        f"📋 Заявлено: {requested} т ({int(requested*1000)} кг)\n"
        f"⚖️ Факт: {tons} т ({kg} кг)\n"
    )
    if result.get("summa") and result["summa"] > 0:
        msg += f"💰 Сумма: {fmt_money(result['summa'])}\n"
    if abs(diff_kg) > 50:
        msg += f"⚠️ Расхождение: {diff_kg:+} кг\n"
        # Уведомляем тебя
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ РАСХОЖДЕНИЕ ВЕСА!\n\nЗаявка №{oid}\n"
                f"👤 {order.get('Дилер')}\n🚗 {car}\n"
                f"Заявлено: {int(requested*1000)} кг\nФакт: {kg} кг\n"
                f"Разница: {diff_kg:+} кг"
            )
        except: pass
    else:
        msg += "✅ Вес в норме\n"

    await update.message.reply_text(msg)

    # Уведомление дилеру
    dealer = db.get_dealer_by_group(str(chat.id))
    if dealer and dealer.get("TG_ID"):
        finance = db.get_dealer_finance(dealer["Имя"])
        try:
            await context.bot.send_message(
                int(dealer["TG_ID"]),
                f"✅ Груз оформлен!\nЗаявка №{oid}\n🚗 {car}\n"
                f"⚖️ {tons} т ({kg} кг)\n💰 {fmt_money(result.get('summa',0))}\n"
                f"💳 Баланс: {fmt_money(finance['balance'])}"
            )
        except: pass

async def handle_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("gok_"):
        oid = int(data.split("_")[1])
        await query.message.edit_text(query.message.text.replace("Всё верно?","✅ Подтверждено! Ожидаем машину."))
    elif data.startswith("gno_"):
        oid = int(data.split("_")[1])
        db.update_status(oid, "Отменён")
        await query.message.edit_text(f"❌ Заявка №{oid} отменена.")

# ════════════════════════════════════════
# КАНАЛ ВЕСОВОЙ — автоматическое чтение
# ════════════════════════════════════════

async def handle_weight_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Читаем канал весовой — автоматически закрываем заявки"""
    if not update.channel_post:
        return
    if update.channel_post.chat.id != WEIGHT_CHANNEL:
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
        logger.info(f"Машина {car} — заявка не найдена")
        return

    oid = order["ID"]
    result = db.close_order(oid, kg, tons)
    if not result:
        return

    requested = float(order.get("Тонн_заявлено",0) or 0)
    diff_kg = kg - int(requested * 1000)

    # Уведомляем тебя
    msg = (
        f"🤖 Канал весовой → Заявка №{oid} закрыта!\n\n"
        f"👤 {order.get('Дилер')}\n🚗 {car}\n"
        f"📋 Заявлено: {int(requested*1000)} кг\n"
        f"⚖️ Факт: {kg} кг\n"
    )
    if result.get("summa"):
        msg += f"💰 Сумма: {fmt_money(result['summa'])}\n"
    if abs(diff_kg) > 50:
        msg += f"⚠️ Расхождение: {diff_kg:+} кг"
    try:
        await context.bot.send_message(ADMIN_ID, msg)
    except: pass

    # Уведомляем весовщиков
    for wid in WEIGHER_IDS:
        try: await context.bot.send_message(wid, f"✅ Авто-закрыто №{oid}: {car} — {kg} кг")
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
            await update.message.reply_text("Нет дилеров в базе.")
            return ACCT_MENU
        lines = ["Выберите дилера (напишите имя или номер):"]
        for i,d in enumerate(dealers,1):
            fin = db.get_dealer_finance(d["Имя"])
            bal = fin["balance"]
            bal_str = f"🔴 долг {fmt_money(abs(bal))}" if bal < 0 else f"🟢 предоплата {fmt_money(bal)}"
            lines.append(f"{i}. {d['Имя']} — {bal_str}")
        await update.message.reply_text("\n".join(lines))
        return ACCT_PAYMENT_DEALER

    elif text == "💳 Установить цену":
        dealers = db.get_all_dealers()
        lines = ["Для кого установить цену? (напишите имя):"]
        for d in dealers:
            lines.append(f"• {d['Имя']} — {fmt_money(d.get('Цена_за_тонну',0))} за тонну")
        await update.message.reply_text("\n".join(lines))
        return ACCT_SET_PRICE_DEALER

    elif text == "📊 Долги дилеров":
        debts = db.get_all_debts()
        if not debts:
            await update.message.reply_text("Нет данных о задолженностях.")
            return ACCT_MENU
        lines = ["📊 Финансы по дилерам:\n"]
        for d in debts:
            bal = float(d.get("Баланс",0) or 0)
            if bal < 0:
                lines.append(f"🔴 {d['Дилер']}: долг {fmt_money(abs(bal))}")
            elif bal > 0:
                lines.append(f"🟢 {d['Дилер']}: предоплата {fmt_money(bal)}")
            else:
                lines.append(f"⚪ {d['Дилер']}: баланс 0")
        await update.message.reply_text("\n".join(lines))

    elif text == "📋 История оплат":
        payments = db.get_payments(limit=15)
        if not payments:
            await update.message.reply_text("Оплат нет.")
            return ACCT_MENU
        lines = ["📋 Последние оплаты:\n"]
        for p in payments:
            lines.append(f"✅ {p['Дата']} | {p['Дилер']} | {fmt_money(p['Сумма'])} | {p['Тип']}")
        await update.message.reply_text("\n".join(lines))

    return ACCT_MENU

async def acct_payment_dealer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    dealer = db.get_dealer_by_name(name)
    if not dealer:
        await update.message.reply_text("❌ Дилер не найден. Напишите имя ещё раз:")
        return ACCT_PAYMENT_DEALER
    context.user_data["pay_dealer"] = dealer["Имя"]
    fin = db.get_dealer_finance(dealer["Имя"])
    await update.message.reply_text(
        f"👤 {dealer['Имя']}\n"
        f"💳 Баланс: {fmt_money(fin['balance'])}\n"
        f"💵 Долг: {fmt_money(fin['debt'])}\n\n"
        f"Введите сумму оплаты (в сумах):"
    )
    return ACCT_PAYMENT_AMOUNT

async def acct_payment_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(" ","").replace(",","."))
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите корректную сумму:")
        return ACCT_PAYMENT_AMOUNT
    context.user_data["pay_amount"] = amount
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💵 Наличные", callback_data="pay_cash"),
        InlineKeyboardButton("🏦 Перевод", callback_data="pay_transfer"),
        InlineKeyboardButton("📱 Карта", callback_data="pay_card"),
    ]])
    await update.message.reply_text(f"Сумма: {fmt_money(amount)}\nВыберите тип оплаты:", reply_markup=kb)
    return ACCT_PAYMENT_TYPE

async def acct_payment_type_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    types = {"pay_cash":"Наличные","pay_transfer":"Перевод","pay_card":"Карта"}
    pay_type = types.get(query.data, "Другое")
    dealer = context.user_data["pay_dealer"]
    amount = context.user_data["pay_amount"]
    pid = db.add_payment(dealer, amount, pay_type, "", update.effective_user.full_name)
    fin = db.get_dealer_finance(dealer)
    await query.message.reply_text(
        f"✅ Оплата №{pid} записана!\n\n"
        f"👤 {dealer}\n💰 {fmt_money(amount)} ({pay_type})\n"
        f"💳 Новый баланс: {fmt_money(fin['balance'])}"
    )
    # Уведомляем тебя
    try: await context.bot.send_message(ADMIN_ID, f"💰 Оплата от {dealer}: {fmt_money(amount)} ({pay_type})")
    except: pass
    await show_acct_menu(update, context)
    return ACCT_MENU

async def acct_set_price_dealer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    dealer = db.get_dealer_by_name(name)
    if not dealer:
        await update.message.reply_text("❌ Дилер не найден:")
        return ACCT_SET_PRICE_DEALER
    context.user_data["price_dealer"] = dealer["Имя"]
    await update.message.reply_text(
        f"👤 {dealer['Имя']}\nТекущая цена: {fmt_money(dealer.get('Цена_за_тонну',0))} за тонну\n\nВведите новую цену за тонну:"
    )
    return ACCT_SET_PRICE_VAL

async def acct_set_price_val(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.replace(" ","").replace(",","."))
        if price <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите корректную цену:")
        return ACCT_SET_PRICE_VAL
    dealer = context.user_data["price_dealer"]
    db.set_dealer_price(dealer, price)
    await update.message.reply_text(f"✅ Цена для {dealer} установлена: {fmt_money(price)} за тонну")
    await show_acct_menu(update, context)
    return ACCT_MENU

# ════════════════════════════════════════
# ADMIN — ТЫ
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
            f"📊 Сегодня {stats['date']}\n\n"
            f"Заявок: {stats['orders']} | Завершено: {stats['done']}\n"
            f"Тонн отгружено: {stats['tons']:.2f} т\n"
            f"Сумма: {fmt_money(stats['sum'])}"
        )
        orders = db.get_orders_by_date(stats['date'])
        for o in orders: await update.message.reply_text(fmt_order(o))

    elif text == "🚛 Активные":
        orders = db.get_active_orders()
        if not orders:
            await update.message.reply_text("Нет активных заявок.")
        else:
            await update.message.reply_text(f"🚛 Активных заявок: {len(orders)}")
            for o in orders: await update.message.reply_text(fmt_order(o))

    elif text == "👥 По дилерам":
        dealers = db.get_all_dealers()
        if not dealers:
            await update.message.reply_text("Нет дилеров.")
            return ADMIN_MENU
        lines = ["👥 Все дилеры:\n"]
        for d in dealers:
            orders = db.get_dealer_orders(d["Имя"], limit=100)
            total = sum(float(o.get("Тонн_факт",0) or 0) for o in orders)
            lines.append(f"👤 {d['Имя']}\n   Заявок: {len(orders)} | Тонн: {total:.1f} т | Цена: {fmt_money(d.get('Цена_за_тонну',0))}/т\n")
        await update.message.reply_text("\n".join(lines))

    elif text == "💰 Финансы":
        debts = db.get_all_debts()
        if not debts:
            await update.message.reply_text("Нет финансовых данных.")
            return ADMIN_MENU
        lines = ["💰 Финансы по дилерам:\n"]
        total_debt = 0
        total_prepay = 0
        for d in debts:
            bal = float(d.get("Баланс",0) or 0)
            if bal < 0:
                lines.append(f"🔴 {d['Дилер']}: долг {fmt_money(abs(bal))}")
                total_debt += abs(bal)
            elif bal > 0:
                lines.append(f"🟢 {d['Дилер']}: предоплата {fmt_money(bal)}")
                total_prepay += bal
            else:
                lines.append(f"⚪ {d['Дилер']}: 0")
        lines.append(f"\n📉 Всего долгов: {fmt_money(total_debt)}")
        lines.append(f"📈 Всего предоплат: {fmt_money(total_prepay)}")
        await update.message.reply_text("\n".join(lines))

    elif text == "⚠️ Расхождения":
        orders = [o for o in db._orders_ws().get_all_records() if o.get("Статус")=="Расхождение"]
        if not orders:
            await update.message.reply_text("✅ Расхождений нет!")
        else:
            await update.message.reply_text(f"⚠️ Расхождений: {len(orders)}")
            for o in orders: await update.message.reply_text(fmt_order(o))

    elif text == "🤖 AI Отчёт":
        await update.message.reply_text("🤖 Генерирую отчёт...")
        stats = db.get_stats_today()
        debts = db.get_all_debts()
        report = await generate_report(stats, debts)
        await update.message.reply_text(f"🤖 AI Отчёт:\n\n{report}")

    elif text == "➕ Добавить дилера":
        await update.message.reply_text(
            "Напишите данные дилера в формате:\n"
            "/addealer Имя Цена\n\n"
            "Например: /addealer Алишер 850000"
        )

    return ADMIN_MENU

async def cmd_add_dealer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Формат: /addealer Имя Цена\nПример: /addealer Алишер 850000")
        return
    name = args[0]
    try: price = float(args[1])
    except: price = 0
    did = db.add_dealer(name, price=price)
    await update.message.reply_text(f"✅ Дилер добавлен!\n👤 {name}\n💰 Цена: {fmt_money(price)}/т")

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручное закрытие заявки: /close [order_id] [kg]"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Формат: /close [номер_заявки] [кг]")
        return
    try:
        oid = int(args[0]); kg = int(args[1])
        tons = round(kg/1000, 3)
        result = db.close_order(oid, kg, tons)
        if result:
            await update.message.reply_text(f"✅ Заявка №{oid} закрыта!\n⚖️ {kg} кг ({tons} т)\n💰 {fmt_money(result.get('summa',0))}")
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
        # Регистрируем группу
        dealer = db.get_dealer_by_group(str(chat.id))
        if not dealer:
            db.update_dealer_group(uid, chat.id, chat.title or "Группа")
        await update.message.reply_text(
            f"🤖 AI-бот активирован в группе {chat.title}!\n\n"
            f"📋 Пишите заявку в любом формате:\n"
            f"Например: «машина 60A123BA 25 тонн» или «60A123BA 25 ton»\n\n"
            f"📸 Для закрытия: отправьте фото чека"
        )
        return

    if is_admin(uid):
        await show_admin_menu(update, context)
        return ADMIN_MENU
    elif is_accountant(uid):
        await show_acct_menu(update, context)
        return ACCT_MENU
    else:
        await update.message.reply_text(
            "👋 Привет!\n\nДля подачи заявки напишите в группу вашего дилера.\n"
            "Бот автоматически распознает заявку."
        )

# ════════════════════════════════════════
# MAIN
# ════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Admin conversation
    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ADMIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, admin_menu)],
            ACCT_MENU:  [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, acct_menu)],
            ACCT_PAYMENT_DEALER: [MessageHandler(filters.TEXT & ~filters.COMMAND, acct_payment_dealer)],
            ACCT_PAYMENT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, acct_payment_amount)],
            ACCT_PAYMENT_TYPE:   [CallbackQueryHandler(acct_payment_type_cb, pattern="^pay_")],
            ACCT_SET_PRICE_DEALER:[MessageHandler(filters.TEXT & ~filters.COMMAND, acct_set_price_dealer)],
            ACCT_SET_PRICE_VAL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, acct_set_price_val)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True, per_message=False,
    )

    app.add_handler(admin_conv)
    app.add_handler(CommandHandler("addealer", cmd_add_dealer))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CallbackQueryHandler(handle_group_callback, pattern="^g(ok|no)_"))

    # Группы дилеров
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        handle_group_text
    ))
    app.add_handler(MessageHandler(
        filters.PHOTO & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        handle_group_photo
    ))

    # Канал весовой
    app.add_handler(MessageHandler(filters.TEXT & filters.UpdateType.CHANNEL_POSTS, handle_weight_channel))

    logger.info("🤖 AI-бот цементного завода запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
