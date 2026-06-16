import os
import json
from datetime import date, timedelta, datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SHEET_NAME = "Prospect Tracking"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ── Column indices (0-based) ───────────────────────────────────────────────────
COL_VISIT_DATE    = 0   # A – Date
# B=Time, C=TT (not used)
COL_PROVIDER      = 3   # D – Svc Provider
COL_NAME          = 4   # E – Clients Non-members!!
# F=NCT, G=Rebook (not used)
COL_SIGNED_UP     = 7   # H – Sign up
COL_POTENTIAL     = 8   # I – Potential Member
# J=Not Interested no f/u, K=Notes (not used)
COL_FU1_DATE      = 11  # L – Follow Up #1
COL_FU1_RESULT    = 12  # M – Follow Up #1 Result
COL_FU2_DATE      = 13  # N – Follow Up #2
COL_FU2_RESULT    = 14  # O – Follow Up #2 Result
COL_FU3_DATE      = 15  # P – Follow Up #3
COL_FU3_RESULT    = 16  # Q – Follow Up #3 Result

DATA_RANGE = f"{SHEET_NAME}!A3:Q"   # data rows start at row 3

app = FastAPI(title="Elements Massage Follow-Up Dashboard")
templates = Jinja2Templates(directory="templates")


def get_sheets_service():
    raw_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw_json:
        info = json.loads(raw_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file(
            os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials.json"), scopes=SCOPES
        )
    return build("sheets", "v4", credentials=creds)


def parse_date(value: str) -> Optional[date]:
    if not value or not value.strip():
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def compute_followups(row: list, row_index: int) -> Optional[dict]:
    def cell(i):
        return row[i].strip() if i < len(row) and row[i] else ""

    signed_up = cell(COL_SIGNED_UP).upper()
    if signed_up in ("Y", "YES"):
        return None

    visit_date = parse_date(cell(COL_VISIT_DATE))
    if not visit_date:
        return None

    today = date.today()

    fu1_date_raw = cell(COL_FU1_DATE)
    fu1_due      = parse_date(fu1_date_raw) or (visit_date + timedelta(days=3))
    fu1_result   = cell(COL_FU1_RESULT)
    # Done if result logged OR a manual date was set that has already passed
    fu1_done     = bool(fu1_result) or (bool(fu1_date_raw) and fu1_due <= today)

    fu2_date_raw = cell(COL_FU2_DATE)
    fu2_due      = parse_date(fu2_date_raw) or (fu1_due + timedelta(days=7))
    fu2_result   = cell(COL_FU2_RESULT)
    fu2_done     = bool(fu2_result) or (bool(fu2_date_raw) and fu2_due <= today)

    fu3_date_raw = cell(COL_FU3_DATE)
    fu3_due      = parse_date(fu3_date_raw) or (fu2_due + timedelta(days=7))
    fu3_result   = cell(COL_FU3_RESULT)
    fu3_done     = bool(fu3_result) or (bool(fu3_date_raw) and fu3_due <= today)

    contacts_made = sum(1 for d in [fu1_done, fu2_done, fu3_done] if d)
    last_result   = fu3_result or fu2_result or fu1_result

    followups = [
        (1, fu1_due, fu1_done, COL_FU1_RESULT),
        (2, fu2_due, fu2_done, COL_FU2_RESULT),
        (3, fu3_due, fu3_done, COL_FU3_RESULT),
    ]

    for num, due, done, result_col in followups:
        if done:
            continue

        base = {
            "row_index":      row_index,
            "name":           cell(COL_NAME),
            "visit_date":     visit_date.strftime("%m/%d/%Y"),
            "provider":       cell(COL_PROVIDER),
            "followup_num":   num,
            "contacts_made":  contacts_made,
            "last_result":    last_result,
            "due_date":       due.strftime("%m/%d/%Y"),
            "result_col_index": result_col,
        }

        if due <= today:
            return {**base, "status": "due", "days_overdue": (today - due).days}
        elif contacts_made > 0:
            return {**base, "status": "pending", "days_overdue": 0}
        else:
            return None

    # All three follow-ups complete (fu1_done, fu2_done, fu3_done all true)
    if contacts_made > 0:
        return {
            "row_index":        row_index,
            "name":             cell(COL_NAME),
            "visit_date":       visit_date.strftime("%m/%d/%Y"),
            "provider":         cell(COL_PROVIDER),
            "followup_num":     None,
            "contacts_made":    contacts_made,
            "last_result":      last_result,
            "due_date":         fu3_due.strftime("%m/%d/%Y"),
            "result_col_index": None,
            "status":           "complete",
            "days_overdue":     0,
        }

    return None


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/followups")
async def get_followups():
    try:
        service = get_sheets_service()
        sheet = service.spreadsheets()
        result = sheet.values().get(
            spreadsheetId=SHEET_ID,
            range=DATA_RANGE,
        ).execute()
        rows = result.get("values", [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read sheet: {e}")

    today = date.today()
    due_today = []
    overdue   = []
    contacted = []

    for i, row in enumerate(rows):
        sheet_row = i + 3
        entry = compute_followups(row, sheet_row)
        if entry is None:
            continue
        if entry["status"] == "due":
            if entry["days_overdue"] == 0:
                due_today.append(entry)
            else:
                overdue.append(entry)
        else:
            contacted.append(entry)

    overdue.sort(key=lambda x: x["days_overdue"])

    return {
        "today":     today.strftime("%B %d, %Y"),
        "due_today": due_today,
        "overdue":   overdue,
        "contacted": contacted,
    }


class ResultUpdate(BaseModel):
    row_index: int          # 1-based sheet row number
    result_col_index: int   # 0-based column index
    result_text: str


def col_index_to_letter(index: int) -> str:
    """Convert 0-based column index to A1 notation letter."""
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


@app.post("/api/result")
async def save_result(update: ResultUpdate):
    col_letter = col_index_to_letter(update.result_col_index)
    cell_range = f"{SHEET_NAME}!{col_letter}{update.row_index}"
    try:
        service = get_sheets_service()
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=cell_range,
            valueInputOption="USER_ENTERED",
            body={"values": [[update.result_text]]},
        ).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write result: {e}")
    return {"ok": True, "cell": cell_range}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
