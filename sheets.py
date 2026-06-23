import os
import json
import gspread
from datetime import datetime
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "YOUR_SHEET_ID")
CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "")  # JSON строкой в переменной окружения


class SheetsDB:
    def __init__(self):
        if CREDS_JSON:
            creds_dict = json.loads(CREDS_JSON)
        else:
            # Локальный файл для разработки
            with open("credentials.json") as f:
                creds_dict = json.load(f)

        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sh = self.gc.open_by_key(SHEET_ID)
        self._init_sheets()

    def _init_sheets(self):
        """Создаём листы если их нет"""
        existing = [ws.title for ws in self.sh.worksheets()]

        if "Заявки" not in existing:
            ws = self.sh.add_worksheet("Заявки", rows=1000, cols=10)
            ws.append_row([
                "ID", "Дата", "Дилер", "Dealer_ID",
                "Номер машины", "Заявлено (т)", "Факт (т)",
                "Расхождение (т)", "Статус", "Время"
            ])
            # Форматируем заголовок жирным
            ws.format("A1:J1", {"textFormat": {"bold": True}})

        if "Дилеры" not in existing:
            ws = self.sh.add_worksheet("Дилеры", rows=100, cols=4)
            ws.append_row(["Dealer_ID", "Имя", "Username", "Дата регистрации"])
            ws.format("A1:D1", {"textFormat": {"bold": True}})

    def _orders_ws(self):
        return self.sh.worksheet("Заявки")

    def _dealers_ws(self):
        return self.sh.worksheet("Дилеры")

    def _next_order_id(self, ws):
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return 1
        return len(rows)  # ID = строка - 1 (строка 1 — заголовок)

    def ensure_dealer(self, user_id, full_name, username=None):
        ws = self._dealers_ws()
        records = ws.get_all_records()
        for r in records:
            if str(r.get("Dealer_ID")) == str(user_id):
                return  # уже есть
        ws.append_row([
            str(user_id),
            full_name,
            username or "",
            datetime.now().strftime("%Y-%m-%d %H:%M")
        ])

    def add_order(self, dealer_id, dealer_name, car_number, tons_requested):
        ws = self._orders_ws()
        order_id = self._next_order_id(ws)
        now = datetime.now()
        ws.append_row([
            order_id,
            now.strftime("%Y-%m-%d"),
            dealer_name,
            str(dealer_id),
            car_number,
            tons_requested,
            "",   # факт
            "",   # расхождение
            "Ожидание",
            now.strftime("%H:%M")
        ])
        return order_id

    def _find_row(self, ws, order_id):
        """Возвращает номер строки (1-based) для заявки"""
        ids = ws.col_values(1)
        for i, val in enumerate(ids):
            if str(val) == str(order_id):
                return i + 1
        return None

    def update_status(self, order_id, status):
        ws = self._orders_ws()
        row = self._find_row(ws, order_id)
        if row:
            ws.update_cell(row, 9, status)  # колонка 9 = Статус

    def update_weight(self, order_id, actual_tons):
        ws = self._orders_ws()
        row = self._find_row(ws, order_id)
        if not row:
            return
        # Читаем заявленный вес
        requested = float(ws.cell(row, 6).value or 0)
        diff = round(actual_tons - requested, 2)
        status = "Расхождение" if abs(diff) > 0.5 else "Завершён"
        ws.update(f"G{row}:I{row}", [[actual_tons, diff, status]])

    def get_order(self, order_id):
        ws = self._orders_ws()
        records = ws.get_all_records()
        for r in records:
            if str(r.get("ID")) == str(order_id):
                return self._normalize(r)
        return None

    def get_all_orders(self, limit=50):
        ws = self._orders_ws()
        records = ws.get_all_records()
        result = [self._normalize(r) for r in records if r.get("ID")]
        return list(reversed(result))[:limit]

    def get_dealer_orders(self, dealer_id, limit=10):
        ws = self._orders_ws()
        records = ws.get_all_records()
        result = [
            self._normalize(r) for r in records
            if str(r.get("Dealer_ID")) == str(dealer_id)
        ]
        return list(reversed(result))[:limit]

    def get_orders_by_status(self, status):
        ws = self._orders_ws()
        records = ws.get_all_records()
        return [self._normalize(r) for r in records if r.get("Статус") == status]

    def get_orders_by_date(self, date_str):
        ws = self._orders_ws()
        records = ws.get_all_records()
        return [self._normalize(r) for r in records if r.get("Дата") == date_str]

    def get_active_orders(self):
        ws = self._orders_ws()
        records = ws.get_all_records()
        return [
            self._normalize(r) for r in records
            if r.get("Статус") in ("Ожидание", "Уехал")
        ]

    def get_mismatched_orders(self):
        ws = self._orders_ws()
        records = ws.get_all_records()
        result = []
        for r in records:
            diff = r.get("Расхождение (т)", "")
            try:
                if abs(float(diff)) > 0.5:
                    result.append(self._normalize(r))
            except (ValueError, TypeError):
                pass
        return result

    def get_dealer_stats(self):
        ws = self._orders_ws()
        records = ws.get_all_records()
        stats = {}
        for r in records:
            name = r.get("Дилер", "")
            if not name:
                continue
            if name not in stats:
                stats[name] = {"dealer_name": name, "count": 0, "total_tons": 0.0}
            stats[name]["count"] += 1
            try:
                actual = r.get("Факт (т)", "") or r.get("Заявлено (т)", 0)
                stats[name]["total_tons"] += float(actual)
            except (ValueError, TypeError):
                pass
        return sorted(stats.values(), key=lambda x: x["total_tons"], reverse=True)

    def _normalize(self, r):
        """Приводим ключи к единому виду"""
        return {
            "id": r.get("ID"),
            "date": r.get("Дата"),
            "dealer_name": r.get("Дилер"),
            "dealer_id": r.get("Dealer_ID"),
            "car_number": r.get("Номер машины"),
            "tons_requested": r.get("Заявлено (т)"),
            "tons_actual": r.get("Факт (т)") or None,
            "diff": r.get("Расхождение (т)") or None,
            "status": r.get("Статус"),
            "time": r.get("Время"),
        }
