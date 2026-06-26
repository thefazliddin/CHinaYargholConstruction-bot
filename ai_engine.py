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
            logger.error(f"Claude API: {data['error']}")
            return None
        return data["content"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Claude API exception: {e}")
        return None

def _regex_parse_order(text: str) -> dict:
    """Парсер заявок без AI"""
    t = text.upper().strip()
    tons = None
    # Тонны с единицами
    for pat in [
        r'(\d+(?:[.,]\d+)?)\s*(?:ТОНН|ТОН|ТОННА|ТОННЕ|TON|TONNA|ТБ|ТН)\b',
        r'\b(\d+(?:[.,]\d+)?)\s*T\b',
    ]:
        m = re.search(pat, t)
        if m:
            tons = float(m.group(1).replace(",","."))
            break

    # Номер машины
    car = None
    for pat in [
        r'\b(\d{3,6}[A-ZА-Я]{2,4})\b',
        r'\b([A-ZА-Я]{1,3}\d{3,6}[A-ZА-Я]{0,3})\b',
        r'\b(\d{2}[A-ZА-Я]\d{3}[A-ZА-Я]{2})\b',
    ]:
        m = re.search(pat, t)
        if m:
            car = m.group(1)
            break

    # Если тонны не нашли — ищем просто число
    if not tons:
        nums = re.findall(r'\b(\d+(?:[.,]\d+)?)\b', t)
        for n in nums:
            val = float(n.replace(",","."))
            if 1 <= val <= 200:
                if car and n in car:
                    continue
                tons = val
                break

    if car and tons:
        return {"car": car, "tons": tons, "product": "Цемент"}
    return {"car": None, "tons": None, "product": None}

def _regex_parse_weight(text: str) -> dict:
    """
    Парсер для канала весовой.
    Поддерживает форматы:
    - Узбекский: Машина: 50048YUK, СОФ ВАЗ: 28340.00 кг
    - Китайский: 车牌: 50048YUK, 净重: 28340.00 公斤
    - Русский: Машина: 50048YUK, Нетто: 28340 кг
    """
    t = text.upper()

    # Машина
    car = None
    for pat in [
        r'МАШИНА[:\s]+([A-Z0-9]+)',
        r'车牌[:\s]+([A-Z0-9]+)',
        r'CAR[:\s]+([A-Z0-9]+)',
        r'НОМЕР[:\s]+([A-Z0-9]+)',
        r'\b([A-Z0-9]{5,10}YUK)\b',
        r'\b([A-Z0-9]{5,10}[A-Z]{2,3})\b',
    ]:
        m = re.search(pat, t)
        if m:
            car = m.group(1).strip()
            break

    # Нетто вес (реальный вес цемента)
    kg_net = None
    for pat in [
        r'СОФ\s*ВАЗ[:\s]+([\d.]+)',
        r'净重[:\s]*([\d.]+)',
        r'НЕТТО[:\s]+([\d.]+)',
        r'NET[:\s]+([\d.]+)',
        r'ЧИСТЫЙ[:\s]+([\d.]+)',
    ]:
        m = re.search(pat, t)
        if m:
            kg_net = float(m.group(1))
            break

    # Брутто
    kg_brutto = None
    for pat in [r'БРУТТО[:\s]+([\d.]+)', r'毛重[:\s]*([\d.]+)', r'GROSS[:\s]+([\d.]+)']:
        m = re.search(pat, t)
        if m:
            kg_brutto = float(m.group(1))
            break

    # Тара
    kg_tara = None
    for pat in [r'ТАРА[:\s]+([\d.]+)', r'皮重[:\s]*([\d.]+)', r'TARE[:\s]+([\d.]+)']:
        m = re.search(pat, t)
        if m:
            kg_tara = float(m.group(1))
            break

    # Если нетто не нашли — считаем из брутто - тара
    if not kg_net and kg_brutto and kg_tara:
        kg_net = kg_brutto - kg_tara

    # Дата
    date = None
    m = re.search(r'(\d{2}[./]\d{2}[./]\d{4})', text)
    if m: date = m.group(1)

    # Номер чека
    check_num = None
    m = re.search(r'ЧЕК[:\s]+(\S+)', t)
    if m: check_num = m.group(1)

    if car and kg_net:
        tons = round(kg_net / 1000, 3)
        return {
            "car": car,
            "kg": int(kg_net),
            "tons": tons,
            "kg_brutto": int(kg_brutto) if kg_brutto else None,
            "kg_tara": int(kg_tara) if kg_tara else None,
            "date": date,
            "check_num": check_num
        }
    return {"car": None, "kg": None, "tons": None}

async def parse_order(text: str, dealer_name: str = "") -> dict:
    """Распознаёт заявку — сначала regex, потом AI"""
    result = _regex_parse_order(text)
    if result.get("car") and result.get("tons"):
        logger.info(f"Regex заявка: {result}")
        return result

    if not ANTHROPIC_API_KEY:
        return {"car": None, "tons": None, "product": None}

    system = """Ты помощник на цементном заводе. Из текста извлеки данные заявки.
Отвечай ТОЛЬКО JSON без markdown:
{"car": "номер машины или null", "tons": число или null, "product": "Цемент"}
Номер машины: любой формат (50048YUK, 50711VBA, 01A123BA и т.д.)
Тонны: число (20 тонн, 20 ton, 20 t, 20т, йигирма тонна, twenty tons)
Если не заявка — {"car": null, "tons": null, "product": null}"""
    result = await ask_claude(system, f"Дилер: {dealer_name}\nТекст: {text}")
    if not result:
        return {"car": None, "tons": None, "product": None}
    try:
        parsed = json.loads(result.replace("```json","").replace("```","").strip())
        if parsed.get("car") and parsed.get("tons"):
            return {"car": str(parsed["car"]).upper(), "tons": float(parsed["tons"]), "product": parsed.get("product","Цемент")}
    except Exception as e:
        logger.error(f"AI parse_order: {e}")
    return {"car": None, "tons": None, "product": None}

async def parse_weight_channel(text: str) -> dict:
    """Читает канал весовой — сначала regex, потом AI"""
    result = _regex_parse_weight(text)
    if result.get("car") and result.get("kg"):
        logger.info(f"Regex весовая: {result}")
        return result

    if not ANTHROPIC_API_KEY:
        return {"car": None, "kg": None, "tons": None}

    system = """Читаешь сообщения с весовой станции цементного завода.
Сообщения могут быть на узбекском, китайском или русском языке.
Нужно найти: номер машины, НЕТТО вес (СОФ ВАЗ / 净重 / Нетто) в кг.
Отвечай ТОЛЬКО JSON: {"car": "номер или null", "kg": нетто_кг или null, "tons": нетто_тонн или null}"""
    result = await ask_claude(system, text)
    if not result:
        return {"car": None, "kg": None, "tons": None}
    try:
        parsed = json.loads(result.replace("```json","").replace("```","").strip())
        if parsed.get("kg") and not parsed.get("tons"):
            parsed["tons"] = round(parsed["kg"]/1000, 3)
        return parsed
    except:
        return {"car": None, "kg": None, "tons": None}

async def parse_check_photo(image_data: bytes) -> dict:
    """Читает фото чека"""
    if not ANTHROPIC_API_KEY:
        return {"car": None, "kg": None, "tons": None}
    b64 = base64.standard_b64encode(image_data).decode()
    system = """Читаешь фото чека весовой станции. Найди номер машины и НЕТТО вес.
Отвечай ТОЛЬКО JSON: {"car": "номер или null", "kg": нетто_кг или null, "tons": нетто_тонн или null}"""
    result = await ask_claude(system, "Прочитай чек:", image_base64=b64)
    if not result:
        return {"car": None, "kg": None, "tons": None}
    try:
        parsed = json.loads(result.replace("```json","").replace("```","").strip())
        if parsed.get("kg") and not parsed.get("tons"):
            parsed["tons"] = round(parsed["kg"]/1000, 3)
        return parsed
    except:
        return {"car": None, "kg": None, "tons": None}

async def generate_report(stats: dict, debts: list) -> str:
    """Генерирует отчёт"""
    lines = [f"📊 Отчёт за {stats.get('date','')}"]
    lines.append(f"Заявок: {stats['orders']} | Завершено: {stats['done']}")
    lines.append(f"Тонн: {stats['tons']:.2f} т | {int(stats['tons']*1000):,} кг")
    lines.append(f"Сумма: {stats['sum']:,.0f} сум")
    debt_count = len([d for d in debts if float(d.get('Баланс',0) or 0) < 0])
    if debt_count:
        lines.append(f"⚠️ Дилеров с долгом: {debt_count}")
    total_debt = sum(abs(float(d.get('Баланс',0) or 0)) for d in debts if float(d.get('Баланс',0) or 0) < 0)
    if total_debt:
        lines.append(f"💸 Общий долг: {total_debt:,.0f} сум")

    if not ANTHROPIC_API_KEY:
        return "\n".join(lines)

    system = "Ты помощник директора цементного завода. Добавь краткий анализ к отчёту (2-3 строки)."
    debt_text = "\n".join([f"- {d.get('Дилер')}: {float(d.get('Баланс',0) or 0):,.0f} сум" for d in debts[:5]])
    extra = await ask_claude(system, f"Данные:\n{chr(10).join(lines)}\nДилеры:\n{debt_text}")
    if extra:
        lines.append(f"\n🤖 {extra}")
    return "\n".join(lines)
