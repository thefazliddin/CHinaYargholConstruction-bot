import os, json, logging
from datetime import datetime
from google.oauth2.service_account import Credentials
import gspread

logger = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID","")
CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON","")

class SheetsDB:
    def __init__(self):
        try:
            creds_dict = json.loads(CREDS_JSON) if CREDS_JSON else json.load(open("credentials.json"))
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
            self.gc = gspread.authorize(creds)
            self.sh = self.gc.open_by_key(SHEET_ID)
            logger.info("✅ Google Sheets подключён")
            self._init()
        except Exception as e:
            logger.error(f"❌ Sheets error: {e}"); raise

    def _init(self):
        existing = [ws.title for ws in self.sh.worksheets()]
        def make(name, headers):
            if name not in existing:
                ws = self.sh.add_worksheet(name, rows=1000, cols=len(headers))
                ws.append_row(headers)
                ws.format(f"A1:{chr(64+len(headers))}1", {"textFormat":{"bold":True}})
                logger.info(f"✅ Лист '{name}' создан")
        make("Дилеры",    ["ID","Имя","TG_ID","Group_ID","Цена_за_тонну","Баланс","Долг","Дата"])
        make("Заявки",    ["ID","Дата","Время","Дилер","TG_ID","Машина","Тонн_заявлено","Тонн_факт","КГ_факт","Сумма","Статус","Group_ID","Источник"])
        make("Оплаты",    ["ID","Дата","Дилер","Сумма","Тип","Комментарий","Бухгалтер"])
        make("Долги",     ["Дилер","Баланс","Долг","Предоплата","Последнее_обновление"])
        make("Продукты",  ["Название","Цена_по_умолчанию","Единица"])
        # Дефолтный продукт
        ws = self.sh.worksheet("Продукты")
        if len(ws.get_all_records()) == 0:
            ws.append_row(["Цемент", "0", "тонна"])

    # ── ДИЛЕРЫ ──
    def get_all_dealers(self):
        return self.sh.worksheet("Дилеры").get_all_records()

    def get_dealer_by_group(self, group_id):
        for d in self.get_all_dealers():
            if str(d.get("Group_ID","")) == str(group_id):
                return d
        return None

    def get_dealer_by_tg(self, tg_id):
        for d in self.get_all_dealers():
            if str(d.get("TG_ID","")) == str(tg_id):
                return d
        return None

    def get_dealer_by_name(self, name):
        name_lower = name.lower()
        for d in self.get_all_dealers():
            if name_lower in d.get("Имя","").lower():
                return d
        return None

    def add_dealer(self, name, tg_id="", group_id="", price=0):
        ws = self.sh.worksheet("Дилеры")
        records = ws.get_all_records()
        new_id = len(records) + 1
        ws.append_row([new_id, name, str(tg_id), str(group_id), price, 0, 0, datetime.now().strftime("%Y-%m-%d")])
        return new_id

    def update_dealer_group(self, tg_id, group_id, group_name):
        ws = self.sh.worksheet("Дилеры")
        records = ws.get_all_records()
        for i, r in enumerate(records, 2):
            if str(r.get("TG_ID","")) == str(tg_id):
                ws.update_cell(i, 4, str(group_id))
                return True
        # Создаём нового дилера по группе
        self.add_dealer(group_name, tg_id, group_id)
        return False

    def get_dealer_price(self, dealer_name):
        for d in self.get_all_dealers():
            if dealer_name.lower() in d.get("Имя","").lower():
                try: return float(d.get("Цена_за_тонну", 0))
                except: return 0
        return 0

    def set_dealer_price(self, dealer_name, price):
        ws = self.sh.worksheet("Дилеры")
        for i, r in enumerate(ws.get_all_records(), 2):
            if dealer_name.lower() in r.get("Имя","").lower():
                ws.update_cell(i, 5, price)
                return True
        return False

    # ── ЗАЯВКИ ──
    def _orders_ws(self): return self.sh.worksheet("Заявки")

    def add_order(self, dealer_name, tg_id, car, tons, group_id="", source="бот"):
        ws = self._orders_ws()
        oid = len(ws.get_all_records()) + 1
        now = datetime.now()
        price = self.get_dealer_price(dealer_name)
        summa = round(tons * price, 2) if price else 0
        ws.append_row([
            oid, now.strftime("%Y-%m-%d"), now.strftime("%H:%M"),
            dealer_name, str(tg_id), car.upper(),
            tons, "", "", summa, "Ожидание", str(group_id), source
        ])
        logger.info(f"✅ Заявка #{oid} создана")
        return oid

    def find_order_by_car(self, car):
        car = car.upper().replace(" ","")
        for r in self._orders_ws().get_all_records():
            rcar = str(r.get("Машина","")).upper().replace(" ","")
            if car in rcar or rcar in car:
                if r.get("Статус") in ("Ожидание","Уехал"):
                    return r
        return None

    def get_order(self, oid):
        for r in self._orders_ws().get_all_records():
            if str(r.get("ID","")) == str(oid):
                return r
        return None

    def close_order(self, oid, kg_fact, tons_fact):
        ws = self._orders_ws()
        records = ws.get_all_records()
        for i, r in enumerate(records, 2):
            if str(r.get("ID","")) == str(oid):
                price = self.get_dealer_price(str(r.get("Дилер","")))
                summa = round(tons_fact * price, 2) if price else 0
                requested = float(r.get("Тонн_заявлено",0) or 0)
                diff = abs(tons_fact - requested)
                status = "Расхождение" if diff > 0.05 else "Завершён"
                ws.update(f"H{i}:K{i}", [[tons_fact, kg_fact, summa, status]])
                # Обновляем долг дилера
                self._update_debt(str(r.get("Дилер","")), summa)
                return {"status": status, "diff": diff, "summa": summa, "order": r}
        return None

    def update_status(self, oid, status):
        ws = self._orders_ws()
        for i, r in enumerate(ws.get_all_records(), 2):
            if str(r.get("ID","")) == str(oid):
                ws.update_cell(i, 11, status)
                return True
        return False

    def get_active_orders(self):
        return [r for r in self._orders_ws().get_all_records() if r.get("Статус") in ("Ожидание","Уехал")]

    def get_orders_by_date(self, date):
        return [r for r in self._orders_ws().get_all_records() if r.get("Дата")==date]

    def get_dealer_orders(self, dealer_name, limit=10):
        all_orders = [r for r in self._orders_ws().get_all_records() if dealer_name.lower() in r.get("Дилер","").lower()]
        return list(reversed(all_orders))[:limit]

    # ── ФИНАНСЫ ──
    def add_payment(self, dealer_name, amount, pay_type, comment, accountant):
        ws = self.sh.worksheet("Оплаты")
        pid = len(ws.get_all_records()) + 1
        ws.append_row([pid, datetime.now().strftime("%Y-%m-%d"), dealer_name, amount, pay_type, comment, accountant])
        self._update_balance(dealer_name, amount)
        return pid

    def _update_debt(self, dealer_name, summa):
        ws = self.sh.worksheet("Дилеры")
        for i, r in enumerate(ws.get_all_records(), 2):
            if dealer_name.lower() in r.get("Имя","").lower():
                balance = float(r.get("Баланс",0) or 0)
                debt = float(r.get("Долг",0) or 0)
                new_balance = balance - summa
                new_debt = abs(new_balance) if new_balance < 0 else 0
                ws.update(f"F{i}:G{i}", [[new_balance, new_debt]])
                self._sync_debt_sheet(dealer_name, new_balance, new_debt)
                return

    def _update_balance(self, dealer_name, amount):
        ws = self.sh.worksheet("Дилеры")
        for i, r in enumerate(ws.get_all_records(), 2):
            if dealer_name.lower() in r.get("Имя","").lower():
                balance = float(r.get("Баланс",0) or 0)
                new_balance = balance + amount
                new_debt = abs(new_balance) if new_balance < 0 else 0
                ws.update(f"F{i}:G{i}", [[new_balance, new_debt]])
                self._sync_debt_sheet(dealer_name, new_balance, new_debt)
                return

    def _sync_debt_sheet(self, dealer_name, balance, debt):
        ws = self.sh.worksheet("Долги")
        records = ws.get_all_records()
        prepay = balance if balance > 0 else 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for i, r in enumerate(records, 2):
            if dealer_name.lower() in r.get("Дилер","").lower():
                ws.update(f"B{i}:E{i}", [[balance, debt, prepay, now]])
                return
        ws.append_row([dealer_name, balance, debt, prepay, now])

    def get_dealer_finance(self, dealer_name):
        for d in self.get_all_dealers():
            if dealer_name.lower() in d.get("Имя","").lower():
                return {
                    "balance": float(d.get("Баланс",0) or 0),
                    "debt": float(d.get("Долг",0) or 0),
                    "price": float(d.get("Цена_за_тонну",0) or 0),
                }
        return {"balance":0,"debt":0,"price":0}

    def get_all_debts(self):
        return self.sh.worksheet("Долги").get_all_records()

    def get_payments(self, dealer_name=None, limit=20):
        records = self.sh.worksheet("Оплаты").get_all_records()
        if dealer_name:
            records = [r for r in records if dealer_name.lower() in r.get("Дилер","").lower()]
        return list(reversed(records))[:limit]

    def get_stats_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        orders = self.get_orders_by_date(today)
        total_tons = sum(float(o.get("Тонн_факт",0) or 0) for o in orders)
        total_sum = sum(float(o.get("Сумма",0) or 0) for o in orders if o.get("Статус")=="Завершён")
        done = len([o for o in orders if o.get("Статус")=="Завершён"])
        return {"orders": len(orders), "done": done, "tons": total_tons, "sum": total_sum, "date": today}
