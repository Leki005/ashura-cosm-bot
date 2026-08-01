"""
Google Sheets интеграция: автоматическая синхронизация клиентов с Google Таблицей.
Архитектура: dirty flag + single writer job (каждые 5 мин).
Все Google I/O через asyncio.to_thread (не блокирует event loop).
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update as sa_update, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from config import Config
from database import Booking, User

logger = logging.getLogger(__name__)

# Column headers for the sheet
SHEET_HEADERS = [
    "TG ID", "Телефон", "Имя", "Визитов",
    "Последняя процедура", "Дата последней", "Сумма последней",
    "Всего потратил", "Следующий визит", "Статус",
    "Бот писал", "Заметки"
]

# Status derivation from DB
def _derive_status(user, active_booking, completed_count, days_since_last):
    if active_booking:
        return "Запись"
    if completed_count == 0:
        return "Новый"
    if days_since_last is not None and days_since_last >= 60:
        return "60+ дней"
    if days_since_last is not None and days_since_last >= 30:
        return "30+ дней"
    if completed_count > 0:
        return "Клиент"
    return "Новый"


# Module-level cache for Google Sheets client (avoids re-auth on every sync)
_gc_cache = None
_gc_creds_cache = None
_gc_cache_time = 0


def _get_sheets_client():
    """Returns gspread client or None if not configured. Cached for 1 hour."""
    global _gc_cache, _gc_creds_cache, _gc_cache_time
    import time

    if _gc_cache and (time.time() - _gc_cache_time) < 3600:
        return _gc_cache, _gc_creds_cache

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        logger.warning("gspread/google-auth not installed")
        return None, None

    enabled = os.getenv("GOOGLE_SHEETS_ENABLED", "false").lower() in ("true", "1", "yes")
    if not enabled:
        return None, None

    cred_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    if not cred_file:
        return None, None

    # Resolve path — check multiple locations
    candidates = [
        cred_file,  # Absolute path as-is
        os.path.join(os.getcwd(), cred_file),  # Relative to working dir (/app)
        os.path.join("/app", cred_file),  # Docker container path
        os.path.join("/opt/ashura-bot", cred_file),  # Host path
    ]
    cred_path = None
    for c in candidates:
        if os.path.exists(c):
            cred_path = c
            break

    if not cred_path:
        logger.warning("Google credentials not found. Tried: %s", candidates)
        return None, None

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(cred_path, scopes=scopes)
        gc = gspread.authorize(creds)
        _gc_cache = gc
        _gc_creds_cache = creds
        _gc_cache_time = time.time()
        return gc, creds
    except Exception as e:
        logger.error("Failed to create Google Sheets client: %s", e)
        return None, None


def _get_spreadsheet(gc):
    """Opens or creates the spreadsheet."""
    sheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
    if sheet_id:
        try:
            return gc.open_by_key(sheet_id)
        except Exception as e:
            logger.warning("Cannot open sheet by ID %s: %s", sheet_id, e)

    # Create new spreadsheet
    try:
        title = f"Клиенты Ашуры — {datetime.now().strftime('%d.%m.%Y')}"
        sh = gc.create(title)
        logger.info("Created new spreadsheet: %s (id=%s)", title, sh.id)

        # Store the ID for future use
        logger.info("NEW SPREADSHEET CREATED: id=%s. Set GOOGLE_SHEETS_SPREADSHEET_ID in .env!", sh.id)

        # Share with admin email if configured
        admin_email = os.getenv("GOOGLE_SHEETS_ADMIN_EMAIL", "")
        if admin_email:
            sh.share(admin_email, perm_type="user", role="writer")
            logger.info("Shared with admin: %s", admin_email)

        # Setup headers
        ws = sh.sheet1
        ws.update("A1", [SHEET_HEADERS])
        ws.freeze(rows=1)

        # Format header row
        ws.format("A1:L1", {
            "textFormat": {"bold": True, "fontSize": 11},
            "backgroundColor": {"red": 0.2, "green": 0.6, "blue": 0.8},
            "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True},
        })

        return sh
    except Exception as e:
        logger.error("Failed to create spreadsheet: %s", e)
        return None


async def build_client_row(session: AsyncSession, user) -> list:
    """Builds a single row for the sheet from user data."""
    from utils.helpers import format_phone, now_salon

    # Visit count
    result = await session.execute(
        select(sa_func.count(Booking.id))
        .where(Booking.user_id == user.id, Booking.status == "completed")
    )
    completed_count = result.scalar() or 0

    # Last visit
    result = await session.execute(
        select(Booking)
        .where(Booking.user_id == user.id, Booking.status == "completed")
        .order_by(Booking.completed_at.desc())
        .limit(1)
    )
    last_booking = result.scalar_one_or_none()

    # Total spent
    result = await session.execute(
        select(sa_func.coalesce(sa_func.sum(Booking.total_amount), 0))
        .where(Booking.user_id == user.id, Booking.status == "completed")
    )
    total_spent = result.scalar() or 0

    # Active booking
    from utils.helpers import ACTIVE_BOOKING_STATUSES
    result = await session.execute(
        select(Booking)
        .where(Booking.user_id == user.id, Booking.status.in_(ACTIVE_BOOKING_STATUSES))
        .limit(1)
    )
    active_booking = result.scalar_one_or_none()

    # Days since last visit
    days_since = None
    if last_booking and last_booking.completed_at:
        days_since = (now_salon().date() - last_booking.completed_at.date()).days

    # Build row
    phone = format_phone(user.phone) if user.phone else ""
    name = user.name if user.name else ""
    last_proc = ""
    last_date = ""
    last_amount = ""
    if last_booking:
        from database import Service
        if last_booking.service_id:
            svc_result = await session.execute(select(Service).where(Service.id == last_booking.service_id))
            svc = svc_result.scalar_one_or_none()
            last_proc = svc.name if svc else ""
        last_date = last_booking.completed_at.strftime("%d.%m.%Y") if last_booking.completed_at else ""
        last_amount = str(last_booking.total_amount) if last_booking.total_amount else ""

    next_visit = ""
    if user.next_visit_at:
        manual = " (ручная)" if user.next_visit_manual else ""
        next_visit = user.next_visit_at.strftime("%d.%m.%Y") + manual

    status = _derive_status(user, active_booking, completed_count, days_since)

    bot_wrote = ""
    if user.revisit_reminder_sent_for:
        bot_wrote = user.revisit_reminder_sent_for.strftime("%d.%m.%Y")

    return [
        str(user.telegram_id), phone, name, str(completed_count),
        last_proc, last_date, last_amount,
        str(total_spent), next_visit, status,
        bot_wrote, ""
    ]


async def mark_dirty(session: AsyncSession, user_id: int, reason: str = "") -> None:
    """Marks a user as needing sync to Google Sheets."""
    try:
        await session.execute(
            sa_update(User)
            .where(User.id == user_id)
            .values(sheets_dirty=True)
        )
        logger.debug("Marked dirty: user_id=%d reason=%s", user_id, reason)
    except Exception as e:
        logger.warning("Failed to mark dirty: %s", e)


def _sync_batch_sync(rows_data, spreadsheet):
    """Synchronous batch update - runs in thread pool.
    Двусторонняя синхронизация: колонка 'Заметки' (L) не затирается ботом.
    """
    import gspread
    try:
        ws = spreadsheet.sheet1

        # Get existing telegram_ids in sheet
        try:
            existing_ids = ws.col_values(1)  # Column A = TG ID
        except Exception:
            existing_ids = []

        # Build index of existing rows
        id_to_row = {}
        for i, tid in enumerate(existing_ids):
            if tid and i > 0:  # Skip header
                id_to_row[tid] = i + 1  # 1-indexed

        # Читаем существующие заметки (колонка L) — чтобы не затереть ручные правки
        existing_notes = {}
        try:
            notes_col = ws.col_values(12)  # Column L = Заметки
            for i, note in enumerate(notes_col):
                if i > 0 and note.strip():  # Skip header
                    tid = existing_ids[i] if i < len(existing_ids) else ""
                    if tid:
                        existing_notes[tid] = note
        except Exception:
            pass

        # Update or append rows
        updates = []
        appends = []
        for row_data in rows_data:
            tg_id = row_data[0]
            if tg_id in id_to_row:
                row_num = id_to_row[tg_id]
                # Сохраняем заметки из таблицы, если бот не записал новых
                sheet_notes = existing_notes.get(tg_id, "")
                bot_notes = row_data[11] if len(row_data) > 11 else ""
                if sheet_notes and not bot_notes:
                    # Ручные правки в таблице — не затираем
                    row_data = list(row_data)
                    row_data[11] = sheet_notes
                updates.append((row_num, row_data))
            else:
                appends.append(row_data)

        # Batch update existing rows — пишем A-K (11 колонок), колонку L не трогаем
        # NOTE: Per-row ws.update() makes 1 API call per row. gspread doesn't support
        # batch_update for non-contiguous ranges. For large syncs this is slow.
        # TODO: Consider using gspread.utils.batch_update or raw Sheets API batchUpdate.
        if updates:
            for row_num, row_data in updates:
                ws.update(f"A{row_num}:K{row_num}", [row_data[:11]])

        # Append new rows — пишем всё A-L
        if appends:
            ws.append_rows(appends)

        return len(updates) + len(appends)
    except Exception as e:
        logger.error("Sync batch failed: %s", e)
        return 0


async def flush_dirty(session: AsyncSession, limit: int = 50) -> int:
    """Syncs dirty users to Google Sheets. Returns count of synced rows."""
    gc, creds = _get_sheets_client()
    if not gc:
        return 0

    spreadsheet = _get_spreadsheet(gc)
    if not spreadsheet:
        return 0

    # Get dirty users
    result = await session.execute(
        select(User)
        .where(User.sheets_dirty.is_(True))
        .where(User.pd_consent_at.isnot(None))
        .where(User.name.notlike("Удалён_%"))
        .limit(limit)
    )
    users = result.scalars().all()

    if not users:
        return 0

    # Build rows
    rows = []
    user_ids = []
    for user in users:
        row = await build_client_row(session, user)
        rows.append(row)
        user_ids.append(user.id)

    # Sync in thread pool (non-blocking)
    synced = await asyncio.to_thread(_sync_batch_sync, rows, spreadsheet)

    # Clear dirty flags
    if synced > 0:
        await session.execute(
            sa_update(User)
            .where(User.id.in_(user_ids))
            .values(sheets_dirty=False, sheets_last_synced=datetime.now())
        )
        await session.flush()
        logger.info("Google Sheets sync: %d rows updated", synced)

        # Run visual CRM update after sync
        try:
            await update_crm_visuals()
        except Exception as e:
            logger.warning("CRM visuals update failed after sync: %s", e)

    return synced


async def sync_all(session: AsyncSession) -> int:
    """Full sync of all users with consent. Admin command."""
    gc, creds = _get_sheets_client()
    if not gc:
        return -1

    spreadsheet = _get_spreadsheet(gc)
    if not spreadsheet:
        return -1

    # Get all users with consent
    result = await session.execute(
        select(User)
        .where(User.pd_consent_at.isnot(None))
        .where(User.name.notlike("Удалён_%"))
    )
    users = result.scalars().all()

    if not users:
        return 0

    # Build rows
    rows = []
    for user in users:
        row = await build_client_row(session, user)
        rows.append(row)

    # Sync
    synced = await asyncio.to_thread(_sync_batch_sync, rows, spreadsheet)

    # Clear all dirty flags
    await session.execute(
        sa_update(User)
        .where(User.pd_consent_at.isnot(None))
        .values(sheets_dirty=False, sheets_last_synced=datetime.now())
    )
    await session.flush()
    logger.info("Google Sheets full sync: %d rows", synced)

    # Run visual CRM update after full sync
    try:
        await update_crm_visuals()
    except Exception as e:
        logger.warning("CRM visuals update failed after full sync: %s", e)

    return synced


async def delete_client_row(telegram_id: int) -> bool:
    """Deletes a client row from Google Sheets (on privacy revoke)."""
    gc, creds = _get_sheets_client()
    if not gc:
        return False

    spreadsheet = _get_spreadsheet(gc)
    if not spreadsheet:
        return False

    def _delete():
        try:
            ws = spreadsheet.sheet1
            cell = ws.find(str(telegram_id))
            if cell:
                ws.delete_rows(cell.row)
                return True
        except Exception as e:
            logger.error("Failed to delete row: %s", e)
        return False

    return await asyncio.to_thread(_delete)


# =============================================================================
# ВИЗУАЛЬНАЯ АВТОМАТИЗАЦИЯ: update_crm_visuals()
# =============================================================================

# Цветовые схемы (RGB 0-1)
COLORS = {
    "green": {"red": 0.85, "green": 0.94, "blue": 0.85},    # Зеленый — Запись/Подтвержден
    "red": {"red": 1.0, "green": 0.85, "blue": 0.85},        # Красный — Отменен
    "yellow": {"red": 1.0, "green": 0.95, "blue": 0.8},      # Желтый — Ожидает/Прогрев
    "blue": {"red": 0.85, "green": 0.92, "blue": 1.0},       # Голубой — Выполнен
    "gray": {"red": 0.92, "green": 0.92, "blue": 0.92},      # Серый — 60+ дней
    "white": {"red": 1.0, "green": 1.0, "blue": 1.0},        # Белый — по умолчанию
}

STATUS_COLORS = {
    "Запись": "green",
    "Записан": "green",
    "Подтвержден": "green",
    "New": "yellow",
    "Выполнен": "blue",
    "Фоллоу-ап": "blue",
    "Отменен": "red",
    "Новый": "yellow",
    "Ожидает": "yellow",
    "Прогрев 3 мес": "yellow",
    "30+ дней": "yellow",
    "60+ дней": "gray",
    "Клиент": "white",
}

# Листы для разделения
CONFIRMED_SHEET = "Подтвержденные"
CANCELLED_SHEET = "Отмененные"
WAITING_SHEET = "Ожидающие"

# CRM-Доска (канбан)
CRM_BOARD_SHEET = "CRM-Доска"
CRM_BOARD_HEADERS = [
    "🟡 Ожидают / Новые",
    "🔵 Прогрев / Консультация",
    "🟢 Подтвержденные записи",
    "🔴 Отмены",
]

# Маппинг статусов → колонки канбана (0=A, 1=B, 2=C, 3=D)
CRM_COLUMN_MAP = {
    "Новый": 0,
    "Ожидает": 0,
    "New": 0,
    "Прогрев 3 мес": 1,
    "Фоллоу-ап": 1,
    "30+ дней": 1,
    "60+ дней": 1,
    "Клиент": 1,
    "Запись": 2,
    "Записан": 2,
    "Подтвержден": 2,
    "Выполнен": 2,
    "Отменен": 3,
}

# Цвета колонок CRM-Доски (заливка заголовков)
CRM_COL_COLORS = [
    {"red": 1.0, "green": 0.95, "blue": 0.8},       # A — жёлтый
    {"red": 0.85, "green": 0.92, "blue": 1.0},       # B — голубой
    {"red": 0.85, "green": 0.94, "blue": 0.85},      # C — зелёный
    {"red": 1.0, "green": 0.85, "blue": 0.85},       # D — красный
]

# Фон карточек (светлее заголовков)
CRM_CARD_COLORS = [
    {"red": 1.0, "green": 0.98, "blue": 0.92},       # A — светло-жёлтый
    {"red": 0.93, "green": 0.96, "blue": 1.0},       # B — светло-голубой
    {"red": 0.93, "green": 0.98, "blue": 0.93},      # C — светло-зелёный
    {"red": 1.0, "green": 0.93, "blue": 0.93},       # D — светло-красный
]

# Сколько дней считать "актуальным" для CRM-Доски
CRM_RECENT_DAYS = 30


def _get_or_create_worksheet(spreadsheet, title: str):
    """Gets or creates a worksheet by title."""
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        ws = spreadsheet.add_worksheet(title=title, rows=100, cols=12)
        # Copy headers
        ws.update("A1", [SHEET_HEADERS])
        ws.freeze(rows=1)
        ws.format("A1:L1", {
            "backgroundColor": {"red": 0.15, "green": 0.5, "blue": 0.7},
            "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True, "fontSize": 11},
        })
        return ws


def _batch_format_rows(ws, row_indices: list, color_name: str):
    """Applies background color to multiple rows via batchUpdate."""
    if not row_indices:
        return
    color = COLORS.get(color_name, COLORS["white"])
    requests = []
    for row_idx in row_indices:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": row_idx - 1,  # 0-indexed
                    "endRowIndex": row_idx,
                    "startColumnIndex": 0,
                    "endColumnIndex": 12,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": color,
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        })
    # Add auto-resize
    requests.append({
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": ws.id,
                "dimension": "COLUMNS",
                "startIndex": 0,
                "endIndex": 12,
            }
        }
    })
    if requests:
        ws.spreadsheet.batch_update({"requests": requests})


def _copy_rows_to_sheet(source_ws, target_ws, row_indices: list, source_data: list):
    """Copies specific rows from source to target worksheet."""
    if not row_indices:
        return
    # Clear target (except header)
    try:
        target_ws.clear()
        target_ws.update("A1", [SHEET_HEADERS])
        target_ws.freeze(rows=1)
        target_ws.format("A1:L1", {
            "backgroundColor": {"red": 0.15, "green": 0.5, "blue": 0.7},
            "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True, "fontSize": 11},
        })
    except Exception:
        pass

    rows_to_copy = []
    for idx in row_indices:
        # idx is 1-indexed sheet row, source_data is 0-indexed list (header at 0)
        list_idx = idx - 1
        if 0 <= list_idx < len(source_data):
            rows_to_copy.append(source_data[list_idx])

    if rows_to_copy:
        target_ws.update("A2", rows_to_copy)


def update_crm_visuals_sync(spreadsheet):
    """
    Synchronous function that runs the full visual CRM update.
    Called from flush_dirty() after data sync.
    """
    try:
        ws = spreadsheet.sheet1

        # 1. Read all data from main sheet
        all_data = ws.get_all_values()
        if len(all_data) <= 1:
            return

        # 2. Find status column (J = index 9)
        status_col = 9  # Column J

        # 3. Group rows by status
        green_rows = []
        red_rows = []
        yellow_rows = []
        blue_rows = []
        gray_rows = []

        confirmed_rows_data = []
        cancelled_rows_data = []
        waiting_rows_data = []

        for i, row in enumerate(all_data[1:], start=2):  # Skip header, 1-indexed
            if len(row) <= status_col:
                continue
            status = row[status_col].strip()
            color_key = STATUS_COLORS.get(status, "white")

            if color_key == "green":
                green_rows.append(i)
                confirmed_rows_data.append(row)
            elif color_key == "red":
                red_rows.append(i)
                cancelled_rows_data.append(row)
            elif color_key == "yellow":
                yellow_rows.append(i)
                waiting_rows_data.append(row)
            elif color_key == "blue":
                blue_rows.append(i)
            elif color_key == "gray":
                gray_rows.append(i)

        # 4. Apply traffic light colors to main sheet
        _batch_format_rows(ws, green_rows, "green")
        _batch_format_rows(ws, red_rows, "red")
        _batch_format_rows(ws, yellow_rows, "yellow")
        _batch_format_rows(ws, blue_rows, "blue")
        _batch_format_rows(ws, gray_rows, "gray")

        # 5. Auto-resize columns
        try:
            ws.spreadsheet.batch_update({"requests": [{
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": 12,
                    }
                }
            }]})
        except Exception:
            pass

        # 6. Copy confirmed clients to separate sheet
        confirmed_ws = _get_or_create_worksheet(spreadsheet, CONFIRMED_SHEET)
        _copy_rows_to_sheet(ws, confirmed_ws, green_rows, all_data)
        # Apply green formatting to confirmed sheet
        if len(confirmed_rows_data) > 0:
            _batch_format_rows(confirmed_ws, list(range(2, len(confirmed_rows_data) + 2)), "green")

        # 7. Copy cancelled clients to separate sheet
        cancelled_ws = _get_or_create_worksheet(spreadsheet, CANCELLED_SHEET)
        _copy_rows_to_sheet(ws, cancelled_ws, red_rows, all_data)
        if len(cancelled_rows_data) > 0:
            _batch_format_rows(cancelled_ws, list(range(2, len(cancelled_rows_data) + 2)), "red")

        # 8. Copy waiting/warming clients to separate sheet
        waiting_ws = _get_or_create_worksheet(spreadsheet, WAITING_SHEET)
        _copy_rows_to_sheet(ws, waiting_ws, yellow_rows, all_data)
        if len(waiting_rows_data) > 0:
            _batch_format_rows(waiting_ws, list(range(2, len(waiting_rows_data) + 2)), "yellow")

        logger.info("CRM visuals updated: %d green, %d red, %d yellow, %d blue, %d gray",
                     len(green_rows), len(red_rows), len(yellow_rows), len(blue_rows), len(gray_rows))

    except Exception as e:
        logger.error("Failed to update CRM visuals: %s", e)


async def update_crm_visuals():
    """
    Main async entry point for visual CRM update.
    Runs in thread pool to avoid blocking event loop.
    """
    gc, creds = _get_sheets_client()
    if not gc:
        return

    spreadsheet = _get_spreadsheet(gc)
    if not spreadsheet:
        return

    await asyncio.to_thread(update_crm_visuals_sync, spreadsheet)
    await asyncio.to_thread(update_crm_board_sync, spreadsheet)
    logger.info("CRM visuals + board refresh completed")


# =============================================================================
# CRM-ДОСКА: КАНБАН С 4 КОЛОНКАМИ
# =============================================================================

def _build_card_text(row: list) -> str:
    """Формирует текст карточки клиента из строки таблицы.
    Колонки: TG ID, Телефон, Имя, Визитов, Последняя процедура,
             Дата последней, Сумма последней, Всего потратил,
             Следующий визит, Статус, Бот писал, Заметки
    """
    name = row[2] if len(row) > 2 else "?"
    phone = row[1] if len(row) > 1 else ""
    last_proc = row[4] if len(row) > 4 else ""
    next_visit = row[8] if len(row) > 8 else ""
    status = row[9] if len(row) > 9 else ""
    notes = row[11] if len(row) > 11 else ""

    parts = [f"👤 {name}"]
    if phone:
        parts.append(f"📞 {phone}")
    if last_proc:
        parts.append(f"💅 {last_proc}")
    if next_visit:
        parts.append(f"📅 {next_visit}")
    if notes:
        parts.append(f"📝 {notes}")
    return "\n".join(parts)


def update_crm_board_sync(spreadsheet):
    """
    Синхронно обновляет лист 'CRM-Доска' — канбан с 4 колонками.
    Берёт только актуальных клиентов (активность за CRM_RECENT_DAYS).
    Ручные правки в колонке 'Заметки' (L) не затираются.
    """
    from datetime import timedelta
    try:
        # 1. Читаем данные из основного листа
        ws = spreadsheet.sheet1
        all_data = ws.get_all_values()
        if len(all_data) <= 1:
            return

        # 2. Определяем колонки
        #    A=0:TG ID, B=1:Телефон, C=2:Имя, E=4:Последняя процедура,
        #    F=5:Дата последней, I=8:Следующий визит, J=9:Статус, L=11:Заметки

        # 3. Группируем по колонкам канбана, фильтруем по актуальности
        columns = [[], [], [], []]  # 4 колонки
        now = datetime.now()

        for row in all_data[1:]:  # Пропускаем заголовок
            if len(row) < 10:
                continue
            status = row[9].strip()
            if not status:
                continue

            col_idx = CRM_COLUMN_MAP.get(status, -1)
            if col_idx < 0:
                continue

            # Фильтр актуальности: проверяем дату последней процедуры или следующего визита
            is_recent = False
            # Проверяем дату последней процедуры
            last_date_str = row[5] if len(row) > 5 else ""
            if last_date_str:
                try:
                    last_date = datetime.strptime(last_date_str, "%d.%m.%Y")
                    if (now - last_date).days <= CRM_RECENT_DAYS:
                        is_recent = True
                except ValueError:
                    pass
            # Проверяем следующий визит
            next_str = row[8] if len(row) > 8 else ""
            if next_str and not is_recent:
                try:
                    next_clean = next_str.replace(" (ручная)", "").strip()
                    next_date = datetime.strptime(next_clean, "%d.%m.%Y")
                    if (next_date - now).days <= CRM_RECENT_DAYS:
                        is_recent = True
                except ValueError:
                    pass
            # Новые клиенты (без дат) — тоже актуальны
            if not last_date_str and not next_str:
                is_recent = True

            # Отменённые — показываем только за последние 14 дней
            if col_idx == 3:
                if last_date_str:
                    try:
                        last_date = datetime.strptime(last_date_str, "%d.%m.%Y")
                        if (now - last_date).days > 14:
                            continue
                    except ValueError:
                        continue

            if not is_recent:
                continue

            card_text = _build_card_text(row)
            columns[col_idx].append(card_text)

        # 4. Получаем или создаём лист CRM-Доска
        try:
            board_ws = spreadsheet.worksheet(CRM_BOARD_SHEET)
        except Exception:
            board_ws = spreadsheet.add_worksheet(title=CRM_BOARD_SHEET, rows=200, cols=4)

        # 5. Очищаем лист
        board_ws.clear()

        # 6. Записываем заголовки
        board_ws.update("A1", [CRM_BOARD_HEADERS])
        board_ws.freeze(rows=1)

        # 7. Форматируем заголовки (цвет + жирный)
        header_requests = []
        for i, color in enumerate(CRM_COL_COLORS):
            header_requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": board_ws.id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": i,
                        "endColumnIndex": i + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color,
                            "textFormat": {"bold": True, "fontSize": 11},
                            "horizontalAlignment": "CENTER",
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
                }
            })

        # 8. Заполняем колонки карточками
        max_cards = max(len(col) for col in columns) if columns else 0
        if max_cards > 0:
            rows_to_write = []
            for row_idx in range(max_cards):
                row = []
                for col_idx in range(4):
                    if row_idx < len(columns[col_idx]):
                        row.append(columns[col_idx][row_idx])
                    else:
                        row.append("")
                rows_to_write.append(row)

            board_ws.update("A2", rows_to_write)

            # 9. Форматируем карточки (цвет фона)
            for col_idx in range(4):
                for row_idx in range(len(columns[col_idx])):
                    header_requests.append({
                        "repeatCell": {
                            "range": {
                                "sheetId": board_ws.id,
                                "startRowIndex": row_idx + 1,  # +1 для заголовка
                                "endRowIndex": row_idx + 2,
                                "startColumnIndex": col_idx,
                                "endColumnIndex": col_idx + 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": CRM_CARD_COLORS[col_idx],
                                    "wrapStrategy": "WRAP",
                                    "verticalAlignment": "TOP",
                                }
                            },
                            "fields": "userEnteredFormat(backgroundColor,wrapStrategy,verticalAlignment)",
                        }
                    })

        # 10. Ширина колонок
        header_requests.append({
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": board_ws.id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 4,
                }
            }
        })

        # 11. Батч-запрос
        if header_requests:
            spreadsheet.batch_update({"requests": header_requests})

        logger.info("CRM board updated: A=%d B=%d C=%d D=%d",
                     len(columns[0]), len(columns[1]), len(columns[2]), len(columns[3]))

    except Exception as e:
        logger.error("Failed to update CRM board: %s", e)


# =============================================================================
# ДВУСТОРОННЯЯ СИНХРОНИЗАЦИЯ: чтение заметок из таблицы
# =============================================================================

def read_notes_from_sheet(spreadsheet) -> dict:
    """
    Читает колонку 'Заметки' (L) из Google Sheets.
    Возвращает {telegram_id: notes_text}.
    Ручные правки админа в таблице не затираются ботом.
    """
    try:
        ws = spreadsheet.sheet1
        all_data = ws.get_all_values()
        notes_map = {}
        for row in all_data[1:]:  # Пропускаем заголовок
            if len(row) >= 12:
                tg_id = row[0].strip()
                notes = row[11].strip()
                if tg_id and notes:
                    notes_map[tg_id] = notes
        return notes_map
    except Exception as e:
        logger.warning("Failed to read notes from sheet: %s", e)
        return {}


def read_sheet_changes(spreadsheet) -> dict:
    """
    Читает ручные изменения из Google Sheets (для двусторонней синхронизации).
    Возвращает {telegram_id: {field: value}} для полей, изменённых вручную.
    Сейчас считывает: Заметки (L), Следующий визит (I).
    """
    try:
        ws = spreadsheet.sheet1
        all_data = ws.get_all_values()
        changes = {}
        for row in all_data[1:]:
            if len(row) < 12:
                continue
            tg_id = row[0].strip()
            if not tg_id:
                continue
            entry = {}
            # Заметки (L)
            notes = row[11].strip()
            if notes:
                entry["notes"] = notes
            # Следующий визит (I) — может быть выставлен вручную
            next_visit = row[8].strip()
            if next_visit:
                entry["next_visit"] = next_visit
            if entry:
                changes[tg_id] = entry
        return changes
    except Exception as e:
        logger.warning("Failed to read sheet changes: %s", e)
        return {}


# =============================================================================
# ПРОВЕРКА КОНФЛИКТОВ РАСПИСАНИЯ
# =============================================================================

async def check_schedule_conflict(
    session: AsyncSession,
    preferred_date: str,
    preferred_time: str,
    exclude_booking_id: int = 0,
) -> tuple[bool, str]:
    """
    Проверяет, нет ли уже подтверждённой записи на это же время.
    Возвращает (has_conflict, description).
    exclude_booking_id — ID записи, которую не учитывать (при редактировании).
    Проверяет ТОЛЬКО confirmed — pending-записи не считаются конфликтом.
    """
    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.user))
        .where(Booking.preferred_date == preferred_date)
        .where(Booking.preferred_time == preferred_time)
        .where(Booking.status == "confirmed")
        .where(Booking.id != exclude_booking_id)
    )
    conflicts = result.scalars().all()

    if not conflicts:
        return False, ""

    names = []
    for b in conflicts:
        user_name = b.user.name if b.user else "Неизвестный"
        names.append(user_name)

    return True, f"На {preferred_date} {preferred_time} уже записан(ы): {', '.join(names)}"
