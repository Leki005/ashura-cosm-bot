"""
FSM-состояния (Finite State Machine) для многошаговых процессов.
Каждый класс — отдельный сценарий в боте.
"""

from aiogram.fsm.state import State, StatesGroup


class ConsentState(StatesGroup):
    """Согласие на обработку персональных данных (один раз)."""
    waiting_accept = State()


class RegistrationState(StatesGroup):
    """Состояния регистрации нового пользователя."""
    waiting_name = State()      # Ожидание ввода имени
    waiting_phone = State()     # Ожидание телефона (контакт или текст)


class AnamnesisState(StatesGroup):
    """Состояния прохождения анкеты анамнеза."""
    in_progress = State()       # Процесс ответа на вопросы
    completed = State()         # Анкета заполнена


class BookingState(StatesGroup):
    """Состояния создания заявки на запись."""
    waiting_anamnesis = State()     # Ожидание анамнеза
    waiting_procedure = State()     # Выбор процедуры из списка
    waiting_procedure_custom = State()  # «Другое» — ввод вручную
    waiting_date = State()          # Ожидание желаемой даты
    waiting_time = State()          # Ожидание желаемого времени
    waiting_message = State()       # Доп. пожелания (необязательно)
    waiting_merge_note = State()    # Текст при объединении общей заявки
    waiting_merge_procedure = State()  # Выбор процедуры для объединения с записью
    waiting_merge_custom = State()  # «Другое» при объединении — ввод вручную
    waiting_bonus_amount = State()  # Ожидание выбора количества бонусов
    confirm = State()               # Подтверждение заявки


class ReviewState(StatesGroup):
    """Состояния оставления отзыва."""
    waiting_rating = State()    # Ожидание выбора рейтинга
    waiting_text = State()      # Ожидание текстового отзыва (опционально)


class ConsultationState(StatesGroup):
    """Состояния консультации."""
    waiting_question = State()      # Ожидание вопроса
    waiting_photo = State()         # Ожидание фото


class AdminReplyState(StatesGroup):
    """Состояния ответа админа клиенту."""
    waiting_reply = State()     # Ожидание текста ответа


class BroadcastState(StatesGroup):
    """Состояния создания рассылки."""
    waiting_segment = State()       # Выбор сегмента аудитории
    waiting_message = State()       # Ожидание текста рассылки
    waiting_confirm = State()       # Подтверждение перед отправкой


class AdminAcceptState(StatesGroup):
    """Состояния принятия заявки админом (ввод даты/времени)."""
    waiting_datetime = State()  # Ожидание ввода даты и времени приёма


class AdminRejectState(StatesGroup):
    """Состояния отклонения заявки админом (причина для клиента)."""
    waiting_reason = State()


class AdminCompleteState(StatesGroup):
    """Состояния завершения заявки админом (ввод суммы для бонусов)."""
    waiting_amount = State()    # Ожидание ввода итоговой суммы


class AdminFaqAddState(StatesGroup):
    """Добавление вопроса в FAQ."""
    waiting_question = State()
    waiting_answer = State()


class AdminServiceAddState(StatesGroup):
    """Добавление услуги в каталог."""
    waiting_name = State()
    waiting_category = State()
    waiting_price = State()


class AdminBonusGrantState(StatesGroup):
    """Ручное начисление бонусов клиенту."""
    waiting_user = State()      # Telegram ID клиента
    waiting_amount = State()    # Количество бонусов


class AdminBonusRevokeState(StatesGroup):
    """Отмена ошибочного начисления бонусов."""
    waiting_user = State()      # Поиск клиента
    waiting_amount = State()    # Ручное списание суммы


class PostProcedureState(StatesGroup):
    """Ответ клиента на опрос после процедуры."""
    waiting_feedback = State()


class AIConsultantState(StatesGroup):
    """Режим общения с ИИ-консультантом (Grok API)."""
    chatting = State()


class SkinAnamnesisState(StatesGroup):
    """Подбор ухода за кожей с помощью ИИ — пошаговый анамнез."""
    in_progress = State()       # Ответы на вопросы (block_index + question_index в state data)
    waiting_photo = State()     # Ожидание фото для AI Vision анализа
    waiting_text = State()      # Ожидание свободного текста (аллергии, лекарства)
