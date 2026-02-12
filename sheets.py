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

def update_feedback_row(feedback_id: int, date_str: str, dish: str, guest_comment: str, kitchen_reply: str | None):
    ws = _ws()
    cell = ws.find(str(feedback_id))   # ищем ID в таблице
    row = cell.row
    ws.update(
        f"A{row}:E{row}",
        [[str(feedback_id), date_str, dish, guest_comment, kitchen_reply or ""]],
        value_input_option="USER_ENTERED",
    )
