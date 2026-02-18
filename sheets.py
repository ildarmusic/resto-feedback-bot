import os
import json
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def _client():
    info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)

def _ws():
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    worksheet_name = os.environ.get("GOOGLE_WORKSHEET", "Sheet1")
    gc = _client()
    sh = gc.open_by_key(sheet_id)
    return sh.worksheet(worksheet_name)

def append_feedback_row(feedback_id: int, date_str: str, dish: str, guest_comment: str, kitchen_reply: str | None):
    ws = _ws()
    ws.append_row(
        [str(feedback_id), date_str, dish, guest_comment, kitchen_reply or ""],
        value_input_option="USER_ENTERED",
    )

def update_feedback_row(fid: int, date_str: str, dish: str, comment: str, reply: str | None):
    ws = _ws()

    target = str(fid).strip()

    # Берём весь столбец A (ID)
    col = ws.col_values(1)  # список строк, включая заголовок
    row_idx = None

    def norm(x: str) -> str:
        x = (x or "").strip()
        # если Google Sheets вернул "123.0"
        if x.endswith(".0") and x.replace(".0", "").isdigit():
            x = x[:-2]
        return x

    for i, v in enumerate(col, start=1):
        if norm(v) == target:
            row_idx = i
            break

    values = [
        str(fid),
        date_str,
        dish,
        comment,
        reply or "",
    ]

    if row_idx is None:
        # Не нашли строку — НЕ теряем данные, добавляем как новую
        ws.append_row(values, value_input_option="USER_ENTERED")
        print(f"[sheets] WARN: row with ID={fid} not found, appended new row")
        return

    # Обновляем диапазон A:E в найденной строке
    ws.update(f"A{row_idx}:E{row_idx}", [values], value_input_option="USER_ENTERED")

