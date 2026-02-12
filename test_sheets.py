import os
from dotenv import load_dotenv
import sheets

load_dotenv(dotenv_path=".env")

def main():
    fid = 999001
    date_str = "12/02/26"
    dish = "Тест блюдо"
    comment = "Тест комментарий гостя"
    reply = ""

    sheets.append_feedback_row(fid, date_str, dish, comment, reply)
    print("OK: appended row with ID =", fid)

if __name__ == "__main__":
    main()
