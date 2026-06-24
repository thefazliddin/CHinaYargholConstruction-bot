import os
import json
import logging
import gspread
from datetime import datetime
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "")


class SheetsDB:
    def __init__(self):
        try:
            logger.info(f"Подключаемся к Google Sheets. SHEET_ID: {SHEET_ID[:20]}...")
            if CREDS_JSON:
                creds_dict = json.loads(CREDS_JSON)
            else:
                with open("credentials.json") as f:
                    creds_dict = json.load(f)

            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
            self.gc = gspread.authorize(creds)
            self.sh = self.gc.open_by_key(SHEET_ID)
            logger.info("✅ Google Sheets подключён успешно!")
            self._init_sheets()
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к Google Sheets: {e}")
            raise

    def _init_sheets(self):
        try:
            existing = [ws.title for ws in self.sh.worksheets()]
            logger.info(f"Листы в таблице: {existing}")

            if "Заявки" not in existing:
                ws = self.sh.add_worksheet("Заявки", rows=1000, cols=10)
                ws.append_row([
                    "ID", "Дата", "Дилер", "Dealer_ID",
                    "Номер машины", "Заявлено (т)", "Факт (т)",
                    "Расхождение (т)", "Статус", "Время"
                ])
                ws.format("A1:J1", {"textFormat": {"bold": True}})
                logger.info("✅ Лист 'Заявки' создан")

            if "Дилеры" not in existing:
                ws = self.sh.add_worksheet("Дилеры", rows=100, cols=4)
                ws.append_row(["Dealer_ID", "Имя", "Username", "Дата регистрации"])
                ws.format("A1:D1", {"textFormat": {"bold": True}})
                logger.info("✅ Лист 'Дилеры' создан")
        except Exception as e:
            logger.error(f"❌ Ошибка создания листов: {e}")
            raise

    def _orders_ws(self):
        return self.sh.worksheet("Заявки")

    def _dealers_ws(self):
        return self.sh.worksheet("Дилеры")

    def _next_order_id(self, ws):
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return 1
        return len(rows)

    def ensure_dealer(self, user_id, full_name, username=None):
        try:
            ws = self._dealers_ws()
            records = ws.get_all_records()
            for r in records:
                if str(r.get("Dealer_ID")) == str(user_id):
                    return
            ws.append_row([
                str(user_id),
                full_name,
                username or "",
                datetime.now().strftime("%Y-%m-%d %H:%M")
            ])
            logger.info(f"✅ Дилер добавлен: {full_name}")
        except Exception as e:
            logger.error(f"❌ Ошибка ensure_dealer: {e}")

    def add_order(self, dealer_id, dealer_name, car_number, tons_requested):
        try:
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
                "",
                "",
                "Ожидание",
                now.strftime("%H:%M")
            ])
            logger.info(f"✅ Заявка #{order_id} добавлена в таблицу")
            return order_id
        except Exception as e:
            logger.error(f"❌ Ошибка add_order: {e}")
            raise

    def _find_row(self, ws, order_id):
        ids = ws.col_values(1)
        for i, val in enumerate(ids):
            if str(val) == str(order_id):
                return i + 1
        return None

    def update_status(self, order_id, status):
        try:
            ws = self._orders_ws()
            row = self._find_row(ws, order_id)
            if row:
                ws.update_cell(row, 9, status)
                logger.info(f"✅ Статус заявки #{order_id} → {status}")
        except Exception as e:
            logger.error(f"❌ Ошибка update_status: {e}")

    def update_weight(self, order_id, actual_tons):
        try:
            ws = self._orders_ws()
            row = self._find_row(ws, order_id)
            if not row:
                return
            requested = float(ws.cell(row, 6).value or 0)
            diff = round(actual_tons - requested, 2)
            status = "Расхождение" if abs(diff) > 0.5 else "Завершён"
            ws.update(f"G{row}:I{row}", [[actual_tons, diff, status]])
            logger.info(f"✅ Вес заявки #{order_id} обновлён: {actual_tons}т")
        except Exception as e:
            logger.error(f"❌ Ошибка update_weight: {e}")

    def get_order(self, order_id):
        try:
            ws = self._orders_ws()
            records = ws.get_all_records()
            for r in records:
                if str(r.get("ID")) == str(order_id):
                    return self._normalize(r)
        except Exception as e:
            logger.error(f"❌ Ошибка get_order: {e}")
        return None

    def get_all_orders(self, limit=50):
        try:
            ws = self._orders_ws()
            records = ws.get_all_records()
            result = [self._normalize(r) for r in records if r.get("ID")]
            return list(reversed(result))[:limit]
        except Exception as e:
            logger.error(f"❌ Ошибка get_all_orders: {e}")
            return []

    def get_dealer_orders(self, dealer_id, limit=10):
        try:
            ws = self._orders_ws()
            records = ws.get_all_records()
            result = [
                self._normalize(r) for r in records
                if str(r.get("Dealer_ID")) == str(dealer_id)
            ]
            return list(reversed(result))[:limit]
        except Exception as e:
            logger.error(f"❌ Ошибка get_dealer_orders: {e}")
            return []

    def get_orders_by_status(self, status):
        try:
            ws = self._orders_ws()
            records = ws.get_all_records()
            return [self._normalize(r) for r in records if r.get("Статус") == status]
        except Exception as e:
            logger.error(f"❌ Ошибка get_orders_by_status: {e}")
            return []

    def get_orders_by_date(self, date_str):
        try:
            ws = self._orders_ws()
            records = ws.get_all_records()
            return [self._normalize(r) for r in records if r.get("Дата") == date_str]
        except Exception as e:
            logger.error(f"❌ Ошибка get_orders_by_date: {e}")
            return []

    def get_active_orders(self):
        try:
            ws = self._orders_ws()
            records = ws.get_all_records()
            return [
                self._normalize(r) for r in records
                if r.get("Статус") in ("Ожидание", "Уехал")
            ]
        except Exception as e:
            logger.error(f"❌ Ошибка get_active_orders: {e}")
            return []

    def get_mismatched_orders(self):
        try:
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
        except Exception as e:
            logger.error(f"❌ Ошибка get_mismatched_orders: {e}")
            return []

    def get_dealer_stats(self):
        try:
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
        except Exception as e:
            logger.error(f"❌ Ошибка get_dealer_stats: {e}")
            return []

    def _normalize(self, r):
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
