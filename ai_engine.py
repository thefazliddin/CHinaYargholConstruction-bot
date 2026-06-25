import os, json, logging, re, httpx, base64
logger = logging.getLogger(__name__)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY","")

async def ask_claude(system, user_text, image_base64=None, image_type="image/jpeg"):
    if not ANTHROPIC_API_KEY:
        return None
    content = []
    if image_base64:
        content.append({"type":"image","source":{"type":"base64","media_type":image_type,"data":image_base64}})
    content.append({"type":"text","text":user_text})
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
                json={"model":"claude-haiku-4-5-20251001","max_tokens":300,"system":system,"messages":[{"role":"user","content":content}]}
            )
        data = r.json()
        if "error" in data:
            logger.error(f"Claude API error: {data['error']}")
            return None
        return data["content"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Claude API exception: {e}")
        return None

def _regex_parse(text: str) -> dict:
    """Парсер без AI — работает всегда"""
    t = text.upper().strip()

    # Ищем тонны: число + слово тонн/т/ton/tonna/тонна
    tons = None
    tons_patterns = [
        r'(\d+(?:[.,]\d+)?)\s*(?:ТОНН|ТОН|ТОННА|ТН|ТБ|TON|TONNA|Т)\b',
        r'\b(\d+(?:[.,]\d+)?)\s*T\b',
    ]
    for pat in tons_patterns:
        m = re.search(pat, t)
        if m:
            tons = float(m.group(1).replace(",","."))
            break

    # Ищем номер машины — буквы+цифры от 5 символов
    car = None
    # Паттерн типа 50711VBA или 01A123BA
    car_patterns = [
        r'\b(\d{2,5}[A-ZА-Я]{2,4}\d{0,3})\b',
        r'\b([A-ZА-Я]{1,3}\d{3,5}[A-ZА-Я]{0,3})\b',
        r'\b(\d{2}[A-ZА-Я]\d{3}[A-ZА-Я]{2})\b',
    ]
    for pat in car_patterns:
        m = re.search(pat, t)
        if m:
            car = m.group(1)
            break

    # Если тонны не нашли через слова — ищем просто число (1-200)
    if not tons:
        nums = re.findall(r'\b(\d+(?:[.,]\d+)?)\b', t)
        for n in nums:
            val = float(n.replace(",","."))
            if 1 <= val <= 200:
                # Проверим что это не часть номера машины
                if car and n in car:
                    continue
                tons = val
                break

    if car and tons:
        return {"car": car, "tons": tons, "product": "Цемент"}
    return {"car": None, "tons": None, "product": None}

async def parse_order(text: str, dealer_name: str = "") -> dict:
    """Распознаёт заявку — сначала AI, потом regex"""
    # Сначала пробуем regex (быстро и надёжно)
    regex_result = _regex_parse(text)

    # Если regex нашёл — возвращаем сразу
    if regex_result.get("car") and regex_result.get("tons"):
        logger.info(f"Regex распознал: {regex_result}")
        return regex_result

    # Если regex не справился — пробуем AI
    if not ANTHROPIC_API_KEY:
        logger.info("Нет API ключа, regex не справился")
        return {"car": None, "tons": None, "product": None}

    system = """Ты помощник на цементном заводе. Из текста извлеки данные заявки.
Отвечай ТОЛЬКО JSON без markdown:
{"car": "номер машины или null", "tons": число или null, "product": "Цемент"}
Номер машины: любой формат (50711VBA, 01A123BA, 60B234CA и т.д.)
Тонны: число (20 тонн, 20 ton, 20 t, 20т, йигирма тонна)
Если не заявка — {"car": null, "tons": null, "product": null}"""

    result = await ask_claude(system, f"Дилер: {dealer_name}\nТекст: {text}")
    if not result:
        return {"car": None, "tons": None, "product": None}
    try:
        clean = result.replace("```json","").replace("```","").strip()
        parsed = json.loads(clean)
        if parsed.get("car") and parsed.get("tons"):
            logger.info(f"AI распознал: {parsed}")
            return {"car": str(parsed["car"]).upper(), "tons": float(parsed["tons"]), "product": parsed.get("product","Цемент")}
    except Exception as e:
        logger.error(f"parse_order AI error: {e}")
    return {"car": None, "tons": None, "product": None}

async def parse_weight_channel(text: str) -> dict:
    """Читает сообщение из канала весовой"""
    # Сначала regex
    t = text.upper()
    kg = None
    tons = None
    car = None

    # Ищем вес в кг
    m = re.search(r'(\d{4,6})\s*КГ', t)
    if m: kg = int(m.group(1))

    # Ищем вес в тоннах
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:ТОН|Т\b|TON)', t)
    if m: tons = float(m.group(1).replace(",","."))

    # Ищем машину
    m = re.search(r'\b(\d{2,5}[A-ZА-Я]{2,4}\d{0,3})\b', t)
    if m: car = m.group(1)

    if kg and not tons: tons = round(kg/1000, 3)
    if tons and not kg: kg = int(tons * 1000)

    if car and (kg or tons):
        return {"car": car, "kg": kg, "tons": tons}

    # AI если regex не справился
    if not ANTHROPIC_API_KEY:
        return {"car": None, "kg": None, "tons": None}

    system = """Читаешь сообщения с весовой станции. Извлеки данные.
Отвечай ТОЛЬКО JSON: {"car": "номер машины или null", "kg": число_кг или null, "tons": число_тонн или null}"""
    result = await ask_claude(system, text)
    if not result:
        return {"car": None, "kg": None, "tons": None}
    try:
        clean = result.replace("```json","").replace("```","").strip()
        parsed = json.loads(clean)
        if parsed.get("kg") and not parsed.get("tons"):
            parsed["tons"] = round(parsed["kg"]/1000, 3)
        elif parsed.get("tons") and not parsed.get("kg"):
            parsed["kg"] = int(parsed["tons"]*1000)
        return parsed
    except:
        return {"car": None, "kg": None, "tons": None}

async def parse_check_photo(image_data: bytes) -> dict:
    """Читает фото чека"""
    if not ANTHROPIC_API_KEY:
        return {"car": None, "kg": None, "tons": None}
    b64 = base64.standard_b64encode(image_data).decode()
    system = """Читаешь фото чека весовой станции цементного завода.
Отвечай ТОЛЬКО JSON: {"car": "номер машины или null", "kg": число_кг или null, "tons": число_тонн или null, "date": "дата или null"}"""
    result = await ask_claude(system, "Прочитай данные с чека:", image_base64=b64)
    if not result:
        return {"car": None, "kg": None, "tons": None}
    try:
        clean = result.replace("```json","").replace("```","").strip()
        parsed = json.loads(clean)
        if parsed.get("kg") and not parsed.get("tons"):
            parsed["tons"] = round(parsed["kg"]/1000, 3)
        elif parsed.get("tons") and not parsed.get("kg"):
            parsed["kg"] = int(parsed["tons"]*1000)
        return parsed
    except:
        return {"car": None, "kg": None, "tons": None}

async def generate_report(stats: dict, debts: list) -> str:
    if not ANTHROPIC_API_KEY:
        # Простой отчёт без AI
        lines = [f"📊 Отчёт за {stats.get('date','')}"]
        lines.append(f"Заявок: {stats['orders']} | Завершено: {stats['done']}")
        lines.append(f"Тонн отгружено: {stats['tons']:.2f} т")
        lines.append(f"Сумма: {stats['sum']:,.0f} сум")
        debt_count = len([d for d in debts if float(d.get('Баланс',0) or 0) < 0])
        if debt_count:
            lines.append(f"⚠️ Дилеров с долгом: {debt_count}")
        return "\n".join(lines)

    system = "Ты помощник директора цементного завода. Составь краткий деловой отчёт на русском языке."
    debt_text = "\n".join([f"- {d.get('Дилер')}: баланс {d.get('Баланс')} сум" for d in debts[:10]])
    user = f"""Данные за сегодня:
Заявок: {stats['orders']}, Завершено: {stats['done']}
Тонн: {stats['tons']:.2f}, Сумма: {stats['sum']:,.0f} сум
Дилеры: {debt_text}
Составь короткий отчёт (5-7 строк)."""
    result = await ask_claude(system, user)
    return result or "Не удалось сгенерировать отчёт."
