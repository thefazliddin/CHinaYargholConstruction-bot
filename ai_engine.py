import os, json, logging, httpx, base64
logger = logging.getLogger(__name__)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY","")

async def ask_claude(system, user_text, image_base64=None, image_type="image/jpeg"):
    """Универсальный вызов Claude API"""
    content = []
    if image_base64:
        content.append({"type":"image","source":{"type":"base64","media_type":image_type,"data":image_base64}})
    content.append({"type":"text","text":user_text})
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
                json={"model":"claude-haiku-4-5-20251001","max_tokens":500,"system":system,"messages":[{"role":"user","content":content}]}
            )
        data = r.json()
        return data["content"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return None

async def parse_order(text: str, dealer_name: str = "") -> dict:
    """Распознаёт заявку из любого текста на любом языке"""
    system = """Ты помощник на цементном заводе. Из текста извлеки данные заявки.
Отвечай ТОЛЬКО JSON без markdown:
{"car": "номер машины или null", "tons": число или null, "product": "название продукта или Цемент"}
Номер машины: любой формат (01A123BA, 60B234CA и т.д.)
Тонны: число, может быть написано на русском/узбекском/английском (20 тонн, 20 tonna, 20 ton, йигирма тонна)
Если не похоже на заявку — {"car": null, "tons": null, "product": null}"""
    result = await ask_claude(system, f"Дилер: {dealer_name}\nТекст: {text}")
    if not result:
        return {"car": None, "tons": None, "product": None}
    try:
        clean = result.replace("```json","").replace("```","").strip()
        parsed = json.loads(clean)
        if parsed.get("car") and parsed.get("tons"):
            return {"car": str(parsed["car"]).upper(), "tons": float(parsed["tons"]), "product": parsed.get("product","Цемент")}
    except Exception as e:
        logger.error(f"parse_order error: {e}, result: {result}")
    return {"car": None, "tons": None, "product": None}

async def parse_weight_channel(text: str) -> dict:
    """Читает сообщение из канала весовой — извлекает машину и вес"""
    system = """Ты читаешь сообщения с весовой станции цементного завода.
Извлеки данные о взвешивании.
Отвечай ТОЛЬКО JSON без markdown:
{"car": "номер машины или null", "kg": число в кг или null, "tons": число в тоннах или null}
Вес может быть в кг (24500 кг) или тоннах (24.5 т). Приведи оба.
Если нет данных — {"car": null, "kg": null, "tons": null}"""
    result = await ask_claude(system, text)
    if not result:
        return {"car": None, "kg": None, "tons": None}
    try:
        clean = result.replace("```json","").replace("```","").strip()
        parsed = json.loads(clean)
        # Вычисляем недостающее
        if parsed.get("kg") and not parsed.get("tons"):
            parsed["tons"] = round(parsed["kg"] / 1000, 3)
        elif parsed.get("tons") and not parsed.get("kg"):
            parsed["kg"] = int(parsed["tons"] * 1000)
        return parsed
    except Exception as e:
        logger.error(f"parse_weight error: {e}")
    return {"car": None, "kg": None, "tons": None}

async def parse_check_photo(image_data: bytes) -> dict:
    """Читает фото чека — извлекает номер машины, вес, дату"""
    b64 = base64.standard_b64encode(image_data).decode()
    system = """Ты читаешь фото чека с весовой станции цементного завода.
Извлеки данные.
Отвечай ТОЛЬКО JSON без markdown:
{"car": "номер машины или null", "kg": число в кг или null, "tons": число или null, "date": "дата или null", "time": "время или null"}
Если не можешь прочитать — {"car": null, "kg": null, "tons": null, "date": null, "time": null}"""
    result = await ask_claude(system, "Прочитай данные с этого чека весовой станции:", image_base64=b64)
    if not result:
        return {"car": None, "kg": None, "tons": None}
    try:
        clean = result.replace("```json","").replace("```","").strip()
        parsed = json.loads(clean)
        if parsed.get("kg") and not parsed.get("tons"):
            parsed["tons"] = round(parsed["kg"] / 1000, 3)
        elif parsed.get("tons") and not parsed.get("kg"):
            parsed["kg"] = int(parsed["tons"] * 1000)
        return parsed
    except Exception as e:
        logger.error(f"parse_check error: {e}")
    return {"car": None, "kg": None, "tons": None}

async def generate_report(stats: dict, debts: list) -> str:
    """AI генерирует красивый отчёт"""
    system = "Ты помощник директора цементного завода. Составь краткий деловой отчёт на русском языке."
    debt_text = "\n".join([f"- {d.get('Дилер')}: баланс {d.get('Баланс')} сум, долг {d.get('Долг')} сум" for d in debts[:10]])
    user = f"""Данные за сегодня:
Заявок: {stats['orders']}, Завершено: {stats['done']}
Тонн отгружено: {stats['tons']:.2f}
Сумма: {stats['sum']:,.0f} сум

Финансы по дилерам:
{debt_text}

Составь короткий отчёт (5-7 строк) с выводами."""
    result = await ask_claude(system, user)
    return result or "Не удалось сгенерировать отчёт."
