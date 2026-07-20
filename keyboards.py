"""
Все клавиатуры бота (inline-кнопки).
Каждая функция возвращает InlineKeyboardMarkup для конкретного экрана.
"""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from utils.text_format import truncate_button


def _international_phone(phone: str) -> str:
    """Преобразует номер в международный формат для tel:/wa.me ссылок."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        return "7" + digits
    return digits


# =============================================================================
# БЫСТРЫЕ КОМАНДЫ (Reply-клавиатура внизу экрана)
# =============================================================================

# Тексты кнопок = команды бота (при нажатии отправляются как сообщение)
QUICK_CMD_START = "/start"
QUICK_CMD_RESTART = "/restart"


def hide_quick_commands_keyboard() -> ReplyKeyboardRemove:
    """Скрывает /start и /restart на время многошаговой записи."""
    return ReplyKeyboardRemove()


def quick_commands_keyboard() -> ReplyKeyboardMarkup:
    """
    Постоянные быстрые команды внизу чата (как кнопка «Прикрепить»).
    /start — главное меню, /restart — сброс текущего диалога.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=QUICK_CMD_START),
                KeyboardButton(text=QUICK_CMD_RESTART),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# =============================================================================
# ГЛАВНОЕ МЕНЮ (клиент)
# =============================================================================


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню клиента — столбиком."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗓 Записаться к Ашуре", callback_data="menu_booking"))
    builder.row(InlineKeyboardButton(text="📋 Мои записи", callback_data="menu_my_bookings"))
    builder.row(InlineKeyboardButton(text="💅 Услуги и цены", callback_data="menu_services"))
    builder.row(InlineKeyboardButton(text="✨ Подбор ухода с ИИ", callback_data="menu_skin_anamnesis"))
    builder.row(InlineKeyboardButton(text="🤖 Помощник ИИ", callback_data="menu_ai_consultant"))
    builder.row(InlineKeyboardButton(text="🎁 Бонусы", callback_data="menu_bonuses"))
    builder.row(InlineKeyboardButton(text="❓ Контакты и FAQ", callback_data="menu_contacts_faq"))
    builder.row(InlineKeyboardButton(text="★ Отзывы", callback_data="menu_reviews"))
    builder.row(InlineKeyboardButton(text="📄 Персональные данные", callback_data="menu_privacy"))
    return builder.as_markup()


def privacy_consent_keyboard(*, for_registration: bool = True) -> InlineKeyboardMarkup:
    """Экран одноразового согласия на обработку ПДн."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📖 Ознакомиться с текстом", callback_data="privacy_read"),
    )
    accept_data = "privacy_accept_reg" if for_registration else "privacy_accept_existing"
    builder.row(
        InlineKeyboardButton(text="✅ Согласен(на)", callback_data=accept_data),
        InlineKeyboardButton(text="❌ Отказаться", callback_data="privacy_decline"),
    )
    return builder.as_markup()


def privacy_after_read_keyboard(*, for_registration: bool = True) -> InlineKeyboardMarkup:
    """Кнопки после показа полного текста согласия."""
    builder = InlineKeyboardBuilder()
    accept_data = "privacy_accept_reg" if for_registration else "privacy_accept_existing"
    builder.row(
        InlineKeyboardButton(text="✅ Согласен(на)", callback_data=accept_data),
    )
    back_data = "privacy_back_reg" if for_registration else "privacy_back_existing"
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data=back_data),
    )
    return builder.as_markup()


def privacy_info_keyboard() -> InlineKeyboardMarkup:
    """Просмотр политики из главного меню (без повторного согласия)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📖 Полный текст согласия", callback_data="privacy_read_info"),
    )
    builder.row(
        InlineKeyboardButton(text="🚫 Отозвать согласие", callback_data="privacy_revoke"),
    )
    builder.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_main"))
    return builder.as_markup()


def ai_consultant_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура в режиме ИИ-консультанта."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Завершить общение", callback_data="ai_exit"),
    )
    return builder.as_markup()


def restart_confirm_keyboard() -> InlineKeyboardMarkup:
    """Подтверждение рестарта при активной записи."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да, перезапустить", callback_data="restart_confirm"),
        InlineKeyboardButton(text="❌ Нет, остаться", callback_data="restart_cancel"),
    )
    return builder.as_markup()


# =============================================================================
# АНАМНЕЗ (опросник ДА/НЕТ)
# =============================================================================

ANAMNESIS_QUESTIONS = [
    ("Есть ли аллергия на косметику?", "allergy"),
    ("Беременность / кормление грудью?", "pregnancy"),
    ("Принимаете ли антикоагулянты?", "anticoagulants"),
    ("Есть ли герпес в активной фазе?", "herpes"),
    ("Есть ли воспаления на коже?", "inflammation"),
    ("Есть ли шрамы в зоне процедуры?", "scars"),
    ("Делали ли инъекции/пилинги менее 2 недель назад?", "recent_procedures"),
    ("Есть ли сахарный диабет?", "diabetes"),
    ("Есть ли онкологические заболевания?", "oncology"),
    ("Есть ли эпилепсия?", "epilepsy"),
]


def anamnesis_keyboard(
    question_index: int, answers: dict, *, anam_token: str = "",
) -> InlineKeyboardMarkup:
    """
    Клавиатура анамнеза — только кнопки Да/Нет.
    Полный текст вопросов в теле сообщения (не в кнопках), чтобы не обрезалось на телефоне.
    anam_token — токен сессии для инвалидации старых кнопок.
    """
    builder = InlineKeyboardBuilder()
    # Префикс с токеном: anam_{token}_{key}_{yes|no}
    prefix = f"anam_{anam_token}_" if anam_token else "anam_"

    if question_index < len(ANAMNESIS_QUESTIONS):
        _, q_key = ANAMNESIS_QUESTIONS[question_index]
        builder.row(
            InlineKeyboardButton(text="✅ Нет, всё ок", callback_data=f"{prefix}{q_key}_no"),
            InlineKeyboardButton(text="⚠️ Да, есть проблема", callback_data=f"{prefix}{q_key}_yes"),
        )

    return builder.as_markup()


def anamnesis_summary_keyboard() -> InlineKeyboardMarkup:
    """Кнопки после завершения анамнеза."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Продолжить запись", callback_data="booking_continue"))
    builder.row(InlineKeyboardButton(text="🔄 Пройти анкету заново", callback_data="booking_restart_anam"))
    return builder.as_markup()


# =============================================================================
# УСЛУГИ
# =============================================================================


# Маппинг категорий: callback_key -> отображаемое название
CATEGORY_MAP = {
    "face": "Уход за лицом",
    "peel": "Пилинги",
    "inject": "Инъекции",
    "body": "Уход за телом",
    "extra": "Дополнительно",
}


def services_categories_keyboard() -> InlineKeyboardMarkup:
    """Категории услуг."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="😊 Уход за лицом", callback_data="cat_face"))
    builder.row(InlineKeyboardButton(text="✨ Пилинги", callback_data="cat_peel"))
    builder.row(InlineKeyboardButton(text="💉 Инъекции", callback_data="cat_inject"))
    builder.row(InlineKeyboardButton(text="🦵 Уход за телом", callback_data="cat_body"))
    builder.row(InlineKeyboardButton(text="🎁 Дополнительно", callback_data="cat_extra"))
    builder.row(InlineKeyboardButton(text="📋 Все услуги", callback_data="cat_all"))
    builder.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_main"))
    return builder.as_markup()


def services_list_keyboard(services: list, category: str = "") -> InlineKeyboardMarkup:
    """Список услуг в категории."""
    builder = InlineKeyboardBuilder()
    for svc in services:
        price = f"{svc.price:,} ₽".replace(",", " ")
        label = truncate_button(f"{svc.name} — {price}", max_len=38)
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=f"svc_{svc.id}",
            )
        )
    builder.row(InlineKeyboardButton(text="🔙 Назад к категориям", callback_data="menu_services"))
    return builder.as_markup()


def service_detail_keyboard(service_id: int) -> InlineKeyboardMarkup:
    """Детальная карточка услуги."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🗓 Записаться", callback_data=f"book_svc_{service_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Назад к списку", callback_data="menu_services")
    )
    return builder.as_markup()


# =============================================================================
# ЗАПИСЬ (booking)
# =============================================================================

# Будни для подписи кнопок даты
_WEEKDAYS_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

def booking_date_keyboard() -> InlineKeyboardMarkup:
    """Быстрый выбор даты — без прошлого; сегодня только если есть слоты."""
    from utils.helpers import get_booking_date_choices

    builder = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []

    for day in get_booking_date_choices(7):
        wd = _WEEKDAYS_RU[day.weekday()]
        label = f"{day.strftime('%d.%m')} {wd}"
        row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"bdate_{day.strftime('%Y%m%d')}",
            )
        )
        if len(row) == 2:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)

    builder.row(
        InlineKeyboardButton(text="✏️ Другая дата", callback_data="bdate_manual"),
    )
    return builder.as_markup()


def booking_procedure_keyboard() -> InlineKeyboardMarkup:
    """Выбор процедуры после анамнеза."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Консультация", callback_data="proc_consult"),
    )
    # --- Ботулинотерапия ---
    builder.row(
        InlineKeyboardButton(text="💉 Ботокс", callback_data="proc_botox"),
    )
    builder.row(
        InlineKeyboardButton(text="Лоб", callback_data="proc_botox_forehead"),
        InlineKeyboardButton(text="Межбровка", callback_data="proc_botox_glabella"),
    )
    builder.row(
        InlineKeyboardButton(text="Гусиные лапки", callback_data="proc_botox_crows"),
        InlineKeyboardButton(text="Фул фейс", callback_data="proc_botox_fullface"),
    )
    builder.row(
        InlineKeyboardButton(text="Гипергидроз", callback_data="proc_botox_hyperhidrosis"),
    )
    # --- Филлеры ---
    builder.row(
        InlineKeyboardButton(text="💋 Увеличение губ", callback_data="proc_lips"),
        InlineKeyboardButton(text="Липолитики", callback_data="proc_lipolytics"),
    )
    # --- Аппаратные ---
    builder.row(
        InlineKeyboardButton(text="⚡ Morpheus8", callback_data="proc_morpheus8"),
        InlineKeyboardButton(text="☀️ BBL", callback_data="proc_bbl"),
    )
    # --- Лазер ---
    builder.row(
        InlineKeyboardButton(text="🔬 Лазерная депиляция", callback_data="proc_laser_hair_removal"),
    )
    builder.row(
        InlineKeyboardButton(text="Лицо", callback_data="proc_laser_face"),
        InlineKeyboardButton(text="Тело", callback_data="proc_laser_body"),
    )
    builder.row(
        InlineKeyboardButton(text="✏️ Другое", callback_data="proc_other"),
    )
    return builder.as_markup()


def booking_time_keyboard(date_str: str | None = None) -> InlineKeyboardMarkup:
    """Быстрый выбор времени; на сегодня — только слоты с запасом 15 мин."""
    from utils.helpers import BOOKING_TIME_SLOTS, get_available_time_slots_for_date

    slots = (
        get_available_time_slots_for_date(date_str)
        if date_str
        else list(BOOKING_TIME_SLOTS)
    )
    builder = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []

    for slot in slots:
        hhmm = slot.replace(":", "")
        row.append(
            InlineKeyboardButton(text=slot, callback_data=f"btime_{hhmm}"),
        )
        if len(row) == 3:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)

    builder.row(
        InlineKeyboardButton(text="✏️ Другое время", callback_data="btime_manual"),
    )
    builder.row(
        InlineKeyboardButton(text="◀️ Изменить дату", callback_data="booking_edit_date"),
    )
    return builder.as_markup()


def active_booking_choice_keyboard(
    booking_id: int, service_id: int = 0,
) -> InlineKeyboardMarkup:
    """
    Выбор при активной записи: совместить услуги или перейти к записям.
    service_id=0 — общая заявка без конкретной услуги.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🔗 Совместить с текущей записью",
            callback_data=f"merge_combine_{booking_id}_{service_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📋 Мои записи",
            callback_data="menu_my_bookings",
        )
    )
    builder.row(
        InlineKeyboardButton(text="❌ Отмена", callback_data="menu_main"),
    )
    return builder.as_markup()


def merge_confirm_keyboard(booking_id: int, service_id: int) -> InlineKeyboardMarkup:
    """Подтверждение объединения услуги с существующей записью."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Да, добавить к записи",
            callback_data=f"merge_confirm_{booking_id}_{service_id}",
        ),
        InlineKeyboardButton(text="❌ Нет", callback_data="menu_main"),
    )
    return builder.as_markup()


def booking_skip_notes_keyboard() -> InlineKeyboardMarkup:
    """Пропуск пожеланий + возврат к дате/времени."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⏩ Пропустить", callback_data="booking_skip_notes"),
    )
    builder.row(
        InlineKeyboardButton(text="◀️ Изменить дату", callback_data="booking_edit_date"),
        InlineKeyboardButton(text="◀️ Изменить время", callback_data="booking_edit_time"),
    )
    return builder.as_markup()


# =============================================================================
# АДМИН: Заявки
# =============================================================================


def admin_contact_client_button(
    *, booking_id: int | None = None, telegram_id: int | None = None,
) -> InlineKeyboardButton:
    """Кнопка «Связаться с клиентом» — по заявке или по Telegram ID."""
    if booking_id is not None:
        callback_data = f"admin_msg_{booking_id}"
    elif telegram_id is not None:
        callback_data = f"admin_contact_{telegram_id}"
    else:
        raise ValueError("Нужен booking_id или telegram_id")
    return InlineKeyboardButton(
        text="📞 Связаться с клиентом",
        callback_data=callback_data,
    )


def admin_accept_choice_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    """Подтвердить на дату клиента или указать другую."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Подтвердить на эту дату",
            callback_data=f"admin_accept_quick_{booking_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📅 Указать другую дату",
            callback_data=f"admin_accept_custom_{booking_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_back")
    )
    return builder.as_markup()


def admin_booking_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    """Кнопки для админа уведомления о новой заявке."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Принять", callback_data=f"admin_accept_{booking_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject_{booking_id}"),
    )
    builder.row(
        admin_contact_client_button(booking_id=booking_id),
        InlineKeyboardButton(text="🎁 Бонусы", callback_data=f"bonus_grant_bk_{booking_id}"),
    )
    return builder.as_markup()


def admin_booking_confirmed_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    """Кнопки для принятой заявки (отметить выполненной)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Выполнено", callback_data=f"admin_done_{booking_id}")
    )
    builder.row(
        admin_contact_client_button(booking_id=booking_id),
        InlineKeyboardButton(text="🎁 Бонусы", callback_data=f"bonus_grant_bk_{booking_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"admin_reject_{booking_id}"),
    )
    return builder.as_markup()


def admin_bonuses_menu_keyboard() -> InlineKeyboardMarkup:
    """Меню управления бонусами."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="💰 Начислить бонусы", callback_data="bonus_grant")
    )
    builder.row(
        InlineKeyboardButton(
            text="↩️ Отменить начисление",
            callback_data="bonus_revoke",
        )
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back"))
    return builder.as_markup()


def admin_bonus_revoke_tx_keyboard(
    transactions: list,
    telegram_id: int,
) -> InlineKeyboardMarkup:
    """Список начислений для отмены."""
    builder = InlineKeyboardBuilder()
    for tx in transactions:
        label = truncate_button(
            f"+{tx.amount} — {tx.description[:28]}", 42,
        )
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=f"bonus_revoke_tx_{tx.id}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="✏️ Другая сумма",
            callback_data=f"bonus_revoke_custom_{telegram_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="admin_bonuses")
    )
    return builder.as_markup()


def admin_bonus_clients_keyboard(clients: list[tuple[int, str, int]]) -> InlineKeyboardMarkup:
    """
    Выбор клиента для начисления бонусов.
    clients: [(telegram_id, name, bonus_balance), ...]
    """
    builder = InlineKeyboardBuilder()
    for tg_id, name, balance in clients:
        label = truncate_button(f"{name} ({balance} б.)", 40)
        builder.row(
            InlineKeyboardButton(text=label, callback_data=f"bonus_pick_{tg_id}")
        )
    builder.row(
        InlineKeyboardButton(text="🔍 Ввести ID или телефон", callback_data="bonus_grant_manual")
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_bonuses"))
    return builder.as_markup()


def admin_bonus_revoke_clients_keyboard(
    clients: list[tuple[int, str, int]],
) -> InlineKeyboardMarkup:
    """Выбор клиента для отмены начисления."""
    builder = InlineKeyboardBuilder()
    for tg_id, name, balance in clients:
        label = truncate_button(f"{name} ({balance} б.)", 40)
        builder.row(
            InlineKeyboardButton(
                text=label, callback_data=f"bonus_revoke_pick_{tg_id}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="🔍 Ввести ID или телефон",
            callback_data="bonus_revoke_manual",
        )
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_bonuses"))
    return builder.as_markup()


def admin_visit_bonus_keyboard(
    booking_id: int, suggested_bonus: int | None,
) -> InlineKeyboardMarkup:
    """Кнопки после завершения визита: начислить бонусы или пропустить."""
    builder = InlineKeyboardBuilder()
    if suggested_bonus and suggested_bonus > 0:
        builder.row(
            InlineKeyboardButton(
                text=f"✅ Начислить +{suggested_bonus}",
                callback_data=f"bonus_auto_{booking_id}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="🎁 Другая сумма",
            callback_data=f"bonus_grant_bk_{booking_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⏭ Пропустить",
            callback_data=f"bonus_skip_{booking_id}",
        )
    )
    return builder.as_markup()


def admin_bonus_amount_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    """Быстрый выбор суммы начисления бонусов."""
    builder = InlineKeyboardBuilder()
    presets = (50, 100, 250, 500, 1000)
    builder.row(
        *[
            InlineKeyboardButton(
                text=f"+{amount}",
                callback_data=f"bonus_add_{telegram_id}_{amount}",
            )
            for amount in presets[:3]
        ]
    )
    builder.row(
        *[
            InlineKeyboardButton(
                text=f"+{amount}",
                callback_data=f"bonus_add_{telegram_id}_{amount}",
            )
            for amount in presets[3:]
        ]
    )
    builder.row(
        InlineKeyboardButton(
            text="✏️ Другая сумма",
            callback_data=f"bonus_add_custom_{telegram_id}",
        )
    )
    builder.row(InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_bonuses"))
    return builder.as_markup()


def post_procedure_feedback_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    """Кнопки ответа на опрос «как прошла процедура»."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="😊 Всё отлично!",
            callback_data=f"followup_great_{booking_id}",
        ),
        InlineKeyboardButton(
            text="🙂 Хорошо",
            callback_data=f"followup_good_{booking_id}",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="💬 Рассказать подробнее",
            callback_data=f"followup_text_{booking_id}",
        ),
        InlineKeyboardButton(
            text="❓ Есть вопрос",
            callback_data=f"followup_question_{booking_id}",
        ),
    )
    builder.row(
        InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data="review_start")
    )
    builder.row(InlineKeyboardButton(text="🔙 В меню", callback_data="menu_main"))
    return builder.as_markup()


def admin_consult_contact_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    """Кнопка связи с клиентом под вопросом/фото консультации."""
    builder = InlineKeyboardBuilder()
    builder.row(admin_contact_client_button(telegram_id=telegram_id))
    return builder.as_markup()


# =============================================================================
# БОНУСЫ
# =============================================================================


def bonuses_menu_keyboard(balance: int) -> InlineKeyboardMarkup:
    """Меню бонусной программы."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=f"💰 Баланс: {balance} бонусов", callback_data="bonus_balance")
    )
    if balance > 0:
        builder.row(
            InlineKeyboardButton(text="🗓 Записать и потратить бонусы", callback_data="menu_booking")
        )
    builder.row(
        InlineKeyboardButton(text="📜 История операций", callback_data="bonus_history")
    )
    builder.row(
        InlineKeyboardButton(text="ℹ️ Как это работает?", callback_data="bonus_info")
    )
    builder.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_main"))
    return builder.as_markup()


def use_bonuses_keyboard(service_price: int, bonus_balance: int) -> InlineKeyboardMarkup:
    """Кнопки для использования бонусов при записи."""
    builder = InlineKeyboardBuilder()
    max_discount = min(bonus_balance, service_price * Config.BONUS_MAX_DISCOUNT_PERCENT // 100)

    # Варианты списания (шаги)
    steps = [500, 1000, 2000, 5000]
    for step in steps:
        if step <= max_discount:
            builder.row(
                InlineKeyboardButton(
                    text=f"Списать {step} бонусов (-{step:,}₽)".replace(",", " "),
                    callback_data=f"bonus_use_{step}",
                )
            )

    if max_discount > 0:
        builder.row(
            InlineKeyboardButton(
                text=f"Списать максимум ({max_discount} бонусов)",
                callback_data=f"bonus_use_{max_discount}",
            )
        )

    builder.row(
        InlineKeyboardButton(text="❌ Не использовать бонусы", callback_data="bonus_use_0")
    )
    return builder.as_markup()


# =============================================================================
# ОТЗЫВЫ
# =============================================================================


def review_rating_keyboard() -> InlineKeyboardMarkup:
    """Кнопки выбора рейтинга (1-5 звёзд)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="★", callback_data="rate_1"),
        InlineKeyboardButton(text="★★", callback_data="rate_2"),
        InlineKeyboardButton(text="★★★", callback_data="rate_3"),
        InlineKeyboardButton(text="★★★★", callback_data="rate_4"),
        InlineKeyboardButton(text="★★★★★", callback_data="rate_5"),
    )
    return builder.as_markup()


def review_skip_text_keyboard() -> InlineKeyboardMarkup:
    """Кнопка пропуска текстового отзыва."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⏩ Пропустить текст", callback_data="review_skip_text")
    )
    return builder.as_markup()


# =============================================================================
# FAQ / КОНСУЛЬТАЦИЯ
# =============================================================================


def faq_menu_keyboard() -> InlineKeyboardMarkup:
    """Меню FAQ и консультации."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📋 Частые вопросы", callback_data="faq_list"))
    builder.row(InlineKeyboardButton(text="💬 Задать вопрос Ашуре", callback_data="consult_ask"))
    builder.row(InlineKeyboardButton(text="📸 Отправить фото для консультации", callback_data="consult_photo"))
    builder.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_main"))
    return builder.as_markup()


def faq_list_keyboard(faqs: list, page: int = 0) -> InlineKeyboardMarkup:
    """Список вопросов FAQ."""
    builder = InlineKeyboardBuilder()
    per_page = 5
    start = page * per_page
    end = start + per_page
    page_faqs = faqs[start:end]

    for i, faq in enumerate(page_faqs, start=start + 1):
        # Короткая подпись на кнопке — полный вопрос в ответе по клику
        label = truncate_button(f"{i}. {faq.question}", max_len=36)
        builder.row(
            InlineKeyboardButton(text=label, callback_data=f"faq_{faq.id}")
        )

    # Пагинация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"faq_page_{page - 1}"))
    if end < len(faqs):
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"faq_page_{page + 1}"))
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_faq"))
    return builder.as_markup()


def faq_detail_keyboard() -> InlineKeyboardMarkup:
    """Кнопка возврата из детального FAQ."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 К списку вопросов", callback_data="faq_list"))
    return builder.as_markup()


def consult_after_keyboard() -> InlineKeyboardMarkup:
    """Кнопки после отправки вопроса/фото."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_main"))
    return builder.as_markup()


# =============================================================================
# КОНТАКТЫ
# =============================================================================


def contacts_keyboard() -> InlineKeyboardMarkup:
    """Кнопки контактов (только http/https — tel: в Telegram не работает)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🗺️ Показать на карте",
            url="https://yandex.ru/maps/?text=Астрахань+Кирова+11",
        )
    )
    whatsapp = _international_phone(Config.WHATSAPP_NUMBER)
    builder.row(
        InlineKeyboardButton(text="📱 WhatsApp", url=f"https://wa.me/{whatsapp}")
    )
    builder.row(
        InlineKeyboardButton(text="📷 Instagram", url=Config.INSTAGRAM_LINK)
    )
    builder.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_main"))
    return builder.as_markup()


# =============================================================================
# АДМИНКА (главное меню)
# =============================================================================


def admin_main_keyboard() -> InlineKeyboardMarkup:
    """Главное меню администратора."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📋 Заявки на запись", callback_data="admin_bookings"))
    builder.row(InlineKeyboardButton(text="📅 Сегодня / завтра", callback_data="admin_calendar"))
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
    builder.row(InlineKeyboardButton(text="★ Модерация отзывов", callback_data="admin_reviews"))
    builder.row(InlineKeyboardButton(text="🎁 Управление бонусами", callback_data="admin_bonuses"))
    builder.row(InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"))
    builder.row(InlineKeyboardButton(text="⚙️ Управление услугами", callback_data="admin_services"))
    builder.row(InlineKeyboardButton(text="❓ Управление FAQ", callback_data="admin_faq"))
    return builder.as_markup()


def admin_bookings_filter_keyboard(counts: dict[str, int] | None = None) -> InlineKeyboardMarkup:
    """Фильтры заявок для админа (со счётчиками по статусам)."""
    counts = counts or {}

    def _label(status: str, title: str) -> str:
        n = counts.get(status, 0)
        return f"{title} ({n})"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=_label("pending", "🆕 Новые"),
            callback_data="book_filter_pending",
        ),
        InlineKeyboardButton(
            text=_label("confirmed", "✅ Подтверждённые"),
            callback_data="book_filter_confirmed",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=_label("completed", "🏁 Выполненные"),
            callback_data="book_filter_completed",
        ),
        InlineKeyboardButton(
            text=_label("cancelled", "❌ Отменённые"),
            callback_data="book_filter_cancelled",
        ),
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back"))
    return builder.as_markup()


def admin_empty_confirmed_keyboard(counts: dict[str, int] | None = None) -> InlineKeyboardMarkup:
    """Подсказка, когда подтверждённых записей ещё нет."""
    builder = InlineKeyboardBuilder()
    pending_n = (counts or {}).get("pending", 0)
    if pending_n:
        builder.row(
            InlineKeyboardButton(
                text=f"🆕 Показать новые ({pending_n})",
                callback_data="book_filter_pending",
            )
        )
    builder.row(InlineKeyboardButton(text="🔙 К фильтрам", callback_data="admin_bookings"))
    return builder.as_markup()


def admin_review_moderation_keyboard(review_id: int) -> InlineKeyboardMarkup:
    """Кнопки модерации отзыва."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"rev_pub_{review_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rev_rej_{review_id}"),
    )
    return builder.as_markup()


def admin_broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    """Кнопки подтверждения рассылки."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📢 Разослать всем", callback_data="bc_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="bc_cancel"),
    )
    return builder.as_markup()


def admin_stats_period_keyboard() -> InlineKeyboardMarkup:
    """Выбор периода статистики."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Сегодня", callback_data="stats_today"),
        InlineKeyboardButton(text="Неделя", callback_data="stats_week"),
    )
    builder.row(
        InlineKeyboardButton(text="Месяц", callback_data="stats_month"),
        InlineKeyboardButton(text="Всё время", callback_data="stats_all"),
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back"))
    return builder.as_markup()


# =============================================================================
# ОТМЕНА ЗАПИСИ (клиент)
# =============================================================================


def cancel_booking_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    """Кнопка отмены записи."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"cancel_book_{booking_id}")
    )
    return builder.as_markup()


def cancel_confirm_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    """Подтверждение отмены записи."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да, отменить", callback_data=f"cancel_confirm_{booking_id}"),
        InlineKeyboardButton(text="❌ Нет", callback_data="cancel_abort"),
    )
    return builder.as_markup()


# =============================================================================
# МОИ ЗАПИСИ
# =============================================================================


def my_bookings_keyboard() -> InlineKeyboardMarkup:
    """Кнопка возврата из раздела записей."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_main"))
    return builder.as_markup()


# =============================================================================
# УНИВЕРСАЛЬНЫЕ
# =============================================================================


def back_to_main_keyboard() -> InlineKeyboardMarkup:
    """Возврат в главное меню."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_main"))
    return builder.as_markup()


# =============================================================================
# АНАМНЕЗ КОЖИ — Клавиатуры для 7 блоков
# =============================================================================

# Стартовый экран
def skin_start_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Да, поехали! 🚀", callback_data="skin_go"))
    builder.row(InlineKeyboardButton(text="Зачем это нужно? 🤔", callback_data="skin_why"))
    return builder.as_markup()


def skin_why_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Понятно, начинаем! 🚀", callback_data="skin_go"))
    return builder.as_markup()


# Блок 1: Общая информация
SKIN_AGE_CHOICES = [
    ("18-25", "18-25"), ("26-35", "26-35"), ("36-45", "36-45"),
    ("46-55", "46-55"), ("55+", "55+"),
]

SKIN_TYPE_CHOICES = [
    ("Жирная — блеск, поры", "oily"),
    ("Сухая — стянутость, шелушения", "dry"),
    ("Комбинированная — Т-зона + сухие щёки", "combo"),
    ("Нормальная — без проблем", "normal"),
    ("Чувствительная — реагирует на всё", "sensitive"),
    ("Не знаю 🤷‍♀️", "unknown"),
]

SKIN_SEASON_CHOICES = [
    ("Лучше обычного ✨", "better"),
    ("Как всегда", "same"),
    ("Хуже обычного 😕", "worse"),
    ("Катастрофа 🆘", "crisis"),
]


def skin_choice_keyboard(choices: list[tuple[str, str]], prefix: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, value in choices:
        builder.row(InlineKeyboardButton(text=text, callback_data=f"{prefix}_{value}"))
    return builder.as_markup()


def skin_skip_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⏩ Пропустить", callback_data="skin_skip"))
    return builder.as_markup()


def skin_continue_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data="skin_back"),
        InlineKeyboardButton(text="Продолжить ➡️", callback_data="skin_next"),
    )
    return builder.as_markup()


# Блок 2: Проблемы (множественный выбор)
SKIN_PROBLEMS = [
    ("🔴 Акне / прыщи", "acne"),
    ("⚫ Чёрные точки / поры", "comedones"),
    ("💢 Постакне / пятна", "postacne"),
    ("😤 Покраснения / купероз", "redness"),
    ("📉 Потеря упругости", "sagging"),
    ("🧓 Морщины", "wrinkles"),
    ("💧 Обезвоживание", "dehydration"),
    ("😑 Тусклый цвет лица", "dullness"),
    ("👁️ Тёмные круги", "dark_circles"),
    ("⬜ Пигментация", "pigmentation"),
    ("🥴 Жирный блеск", "oiliness"),
    ("🌡️ Чувствительность", "sensitivity"),
    ("😌 Нет проблем", "none"),
]


def skin_problems_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, value in SKIN_PROBLEMS:
        mark = "✅ " if value in selected else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{text}", callback_data=f"skprob_{value}"))
    builder.row(InlineKeyboardButton(text="✅ Готово", callback_data="skprob_done"))
    return builder.as_markup()


# Локализация
SKIN_AREAS = [
    ("Лоб", "forehead"), ("Нос", "nose"), ("Подбородок", "chin"),
    ("Щёки", "cheeks"), ("Крылья носа", "nose_wings"),
    ("Область вокруг глаз", "eyes"), ("Всё лицо", "full_face"),
    ("Шея / декольте", "neck"),
]


def skin_areas_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, value in SKIN_AREAS:
        mark = "✅ " if value in selected else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{text}", callback_data=f"skarea_{value}"))
    builder.row(InlineKeyboardButton(text="✅ Готово", callback_data="skarea_done"))
    return builder.as_markup()


# Длительность
SKIN_DURATION_CHOICES = [
    ("Менее месяца", "month"),
    ("1-3 месяца", "3months"),
    ("3-12 месяцев", "year"),
    ("Больше года", "over_year"),
    ("Всегда была", "always"),
]


# Блок 3: Уход
SKIN_HOME_CARE = [
    ("Очищение (пенка/гель)", "cleanser"),
    ("Тоник", "toner"),
    ("Сыворотка", "serum"),
    ("Крем дневной", "day_cream"),
    ("Крем ночной", "night_cream"),
    ("Крем для глаз", "eye_cream"),
    ("SPF", "spf_cream"),
    ("Маски", "masks"),
    ("Пилинг / скраб", "peeling"),
    ("Ничего не использую 🙈", "nothing"),
]


def skin_home_care_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, value in SKIN_HOME_CARE:
        mark = "✅ " if value in selected else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{text}", callback_data=f"skcare_{value}"))
    builder.row(InlineKeyboardButton(text="✏️ Написать свой вариант", callback_data="skcare_write"))
    builder.row(InlineKeyboardButton(text="✅ Готово", callback_data="skcare_done"))
    return builder.as_markup()


SKIN_PRO_CARE = [
    ("Чистка лица", "facial"),
    ("Пилинги", "peeling"),
    ("Аппаратные процедуры", "hardware"),
    ("Инъекции (ботокс/филлеры)", "injections"),
    ("Лазер", "laser"),
    ("Биоревитализация", "bior"),
    ("Мезотерапия", "meso"),
    ("Ничего не пробовала", "nothing"),
]


def skin_pro_care_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, value in SKIN_PRO_CARE:
        mark = "✅ " if value in selected else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{text}", callback_data=f"skpro_{value}"))
    builder.row(InlineKeyboardButton(text="✏️ Написать свой вариант", callback_data="skpro_write"))
    builder.row(InlineKeyboardButton(text="✅ Готово", callback_data="skpro_done"))
    return builder.as_markup()


# Блок 4: Аллергии и здоровье
SKIN_ALLERGY_CHOICES = [
    ("Да, знаю на что", "yes_known"),
    ("Нет аллергии", "no"),
    ("Не знаю / не проверяла", "unknown"),
    ("Кожа часто реагирует покраснением", "reactive"),
]


# Блок 6: Цели
SKIN_GOALS = [
    ("Чистая кожа без высыпаний", "clear"),
    ("Увлажнение и сияние", "glow"),
    ("Уменьшение морщин", "anti_wrinkle"),
    ("Подтяжка овала", "lifting"),
    ("Уменьшение пигментации", "depigment"),
    ("Сужение пор", "pores"),
    ("Меньше жирного блеска", "matte"),
    ("Убрать постакне", "no_postacne"),
    ("Профилактика старения", "prevention"),
    ("Правильный уход", "proper_care"),
    ("Довериться Ашуре", "trust_ashura"),
]


def skin_goals_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, value in SKIN_GOALS:
        mark = "✅ " if value in selected else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{text}", callback_data=f"skgoal_{value}"))
    builder.row(InlineKeyboardButton(text="✅ Готово", callback_data="skgoal_done"))
    return builder.as_markup()


# Финал
def skin_final_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗓 Записаться к Ашуре", callback_data="menu_booking"))
    builder.row(InlineKeyboardButton(text="💬 Задать вопрос ИИ", callback_data="menu_ai_consultant"))
    builder.row(InlineKeyboardButton(text="🔄 Пройти заново", callback_data="skin_restart"))
    builder.row(InlineKeyboardButton(text="☰ Главное меню", callback_data="menu_main"))
    return builder.as_markup()
