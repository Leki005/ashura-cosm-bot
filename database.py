"""
Модели базы данных и функции для работы с SQLite.
Используется SQLAlchemy 2.x + aiosqlite (async).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import Config, SALON_TZ

logger = logging.getLogger(__name__)

# --- Движок БД ---
engine = create_async_engine(Config.DATABASE_URL, echo=False)

# WAL mode + busy_timeout + foreign_keys — критично для параллельного доступа
from sqlalchemy import event, text as sql_text

@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA cache_size=-64000")      # 64MB кеш
    cursor.execute("PRAGMA mmap_size=268435456")     # 256MB memory-map
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """Базовый класс для всех моделей."""
    pass


class User(Base):
    """Пользователь бота (клиент или админ)."""
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("bonus_balance >= 0", name="ck_user_bonus_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    bonus_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Анамнез клиента — переиспользуется 7 дней без повторного опроса
    anamnesis_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    anamnesis_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Анамнез кожи — подбор ухода с ИИ (7 блоков)
    skin_anamnesis_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    skin_anamnesis_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Согласие на обработку ПДн (152-ФЗ) — один раз, хранится для подтверждения
    pd_consent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    pd_consent_version: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(SALON_TZ).replace(tzinfo=None))

    # CRM: рекомендуемый следующий визит
    next_visit_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_visit_manual: Mapped[bool] = mapped_column(Boolean, default=False)  # True = ручная установка, не пересчитывать
    # CRM: идемпотентность напоминаний (за какую next_visit_at уже отправлено)
    revisit_reminder_sent_for: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # CRM: идемпотентность churn-алертов владельцу
    churn_alert_30_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    churn_alert_60_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    # CRM: клиент отключил напоминания
    revisit_reminder_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # CRM: счётчик неотвеченных напоминаний (авто-отключение после 2)
    revisit_reminder_no_response: Mapped[int] = mapped_column(Integer, default=0)
    # Google Sheets sync
    sheets_dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    sheets_last_synced: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Связи
    bookings: Mapped[list["Booking"]] = relationship("Booking", back_populates="user")
    reviews: Mapped[list["Review"]] = relationship("Review", back_populates="user")
    bonus_transactions: Mapped[list["BonusTransaction"]] = relationship(
        "BonusTransaction", back_populates="user"
    )


class PersonalDataConsentLog(Base):
    """Журнал согласий на обработку ПДн (для подтверждения при проверках)."""
    __tablename__ = "pd_consent_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(20), nullable=False)
    consented_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(SALON_TZ).replace(tzinfo=None))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)


class Service(Base):
    """Услуга косметолога."""
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    duration: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # CRM: рекомендуемый интервал повторного визита (дни)
    revisit_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class Booking(Base):
    """Запись на приём."""
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("bonus_used >= 0", name="ck_booking_bonus_used_nonneg"),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'completed', 'cancelled', 'expired')",
            name="ck_booking_status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    service_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("services.id", ondelete="SET NULL"), nullable=True
    )
    preferred_date: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    preferred_time: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending, confirmed, completed, cancelled
    anamnesis_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Доп. услуги, объединённые с основной записью: JSON [{id, name, price}]
    extra_services_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    total_amount: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bonus_used: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Когда отправлен опрос «как прошла процедура» (~1 ч после визита)
    followup_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Флаги отправки напоминаний (чтобы не спамить)
    reminder_24h_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    reminder_2h_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(SALON_TZ).replace(tzinfo=None))

    # Связи
    user: Mapped["User"] = relationship("User", back_populates="bookings")
    service: Mapped[Optional["Service"]] = relationship("Service")


class WaitingList(Base):
    """Лист ожидания — клиент ждёт свободный слот на дату."""
    __tablename__ = "waiting_list"
    __table_args__ = (
        CheckConstraint("status IN ('waiting', 'notified', 'booked', 'expired')",
                        name="ck_waiting_list_status_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    service_id: Mapped[Optional[int]] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"), nullable=True)
    preferred_date: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="waiting")
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(SALON_TZ).replace(tzinfo=None))

    user: Mapped["User"] = relationship("User")
    service: Mapped[Optional["Service"]] = relationship("Service")


class Review(Base):
    """Отзыв клиента."""
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_review_rating_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-5
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(SALON_TZ).replace(tzinfo=None))

    # Связи
    user: Mapped["User"] = relationship("User", back_populates="reviews")


class FAQ(Base):
    """Часто задаваемые вопросы."""
    __tablename__ = "faq"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class BonusTransaction(Base):
    """История операций с бонусами (защита от накрутки)."""
    __tablename__ = "bonus_transactions"

    # Типы транзакций
    TX_CONFIRM_BONUS = "CONFIRM_BONUS"   # Бонус за лояльность при подтверждении
    TX_VISIT_BONUS = "VISIT_BONUS"       # Бонус за завершённый визит
    TX_SPEND = "SPEND"                   # Списание при записи
    TX_REFUND = "REFUND"                 # Возврат при отмене
    TX_REVOKE = "REVOKE"                 # Отзыв начисления
    TX_MANUAL = "MANUAL"                 # Ручная корректировка админом

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # + начисление, - списание
    tx_type: Mapped[str] = mapped_column(String(20), nullable=False, default="MANUAL")
    booking_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(SALON_TZ).replace(tzinfo=None))

    # Связи
    user: Mapped["User"] = relationship("User", back_populates="bonus_transactions")


class AdminSettings(Base):
    """Настройки администратора."""
    __tablename__ = "admin_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)


# =============================================================================
# Функции для работы с БД
# =============================================================================


async def init_db() -> None:
    """
    Создаёт все таблицы в базе данных.
    Вызывается один раз при старте бота.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("База данных инициализирована.")


async def seed_data(session: AsyncSession) -> None:
    """
    Наполняет базу начальными данными (услуги, FAQ, настройки).
    Вызывается при первом запуске.
    """
    # Проверяем, есть ли уже услуги
    result = await session.execute(select(Service))
    if result.scalars().first():
        logger.info("База уже наполнена, пропускаем seed.")
        return

    # --- Услуги ---
    services = [
        # Уход за лицом
        Service(
            name="Чистка лица (механическая)",
            category="Уход за лицом",
            description="Глубокое очищение пор, удаление комедонов, маска по типу кожи.",
            price=3500,
            duration=90,
        ),
        Service(
            name="Чистка лица (ультразвуковая)",
            category="Уход за лицом",
            description="Мягкое очищение ультразвуком, подходит для чувствительной кожи.",
            price=4000,
            duration=75,
        ),
        Service(
            name="Комбинированная чистка лица",
            category="Уход за лицом",
            description="Сочетание механической и ультразвуковой чистки. Максимальный результат.",
            price=5000,
            duration=120,
        ),
        # Пилинги
        Service(
            name="Пилинг Salicylic 30%",
            category="Пилинги",
            description="Салициловый пилинг для жирной и проблемной кожи. Очищает поры, снижает воспаления.",
            price=3000,
            duration=45,
        ),
        Service(
            name="Пилинг Mandelic 40%",
            category="Пилинги",
            description="Миндальный пилинг для чувствительной кожи. Мягкое обновление без шелушения.",
            price=3200,
            duration=45,
        ),
        Service(
            name="Пилинг Jessner",
            category="Пилинги",
            description="Срединный пилинг для выраженного омоложения и лечения постакне.",
            price=4500,
            duration=60,
        ),
        Service(
            name="PRX-T33 (биоревитализация без инъекций)",
            category="Пилинги",
            description="Инновационный пилинг с эффектом биоревитализации. Без восстановительного периода.",
            price=5500,
            duration=45,
        ),
        # Инъекции
        Service(
            name="Увеличение губ (филлер)",
            category="Инъекции",
            description="Коррекция объёма и контура губ гиалуроновым филлером.",
            price=12000,
            duration=45,
        ),
        Service(
            name="Мезотерапия лица",
            category="Инъекции",
            description="Витаминный коктейль для увлажнения и сияния кожи.",
            price=6000,
            duration=40,
        ),
        Service(
            name="Ботулинотерапия (Ботокс)",
            category="Инъекции",
            description="Коррекция мимических морщин (межбровье, лоб, гусиные лапки).",
            price=15000,
            duration=30,
        ),
        Service(
            name="Контурная пластика скул",
            category="Инъекции",
            description="Увеличение объёма скул филлером для выразительности лица.",
            price=18000,
            duration=45,
        ),
        Service(
            name="Липолитики",
            category="Инъекции",
            description="Инъекции для расщепления локальных жировых отложений.",
            price=8000,
            duration=40,
        ),
        # Уход за телом
        Service(
            name="Лазерная эпиляция (зона на выбор)",
            category="Уход за телом",
            description="Александритовый лазер. Одна зона: подмышки, голени или бикини.",
            price=2500,
            duration=30,
        ),
        Service(
            name="Лазерная эпиляция (всё тело)",
            category="Уход за телом",
            description="Комплексная эпиляция всех зон. Выгодный пакет.",
            price=8000,
            duration=120,
        ),
        Service(
            name="Карбокситерапия (лицо)",
            category="Уход за телом",
            description="Насыщение кожи CO2 для улучшения цвета и тургора.",
            price=3500,
            duration=40,
        ),
        # Дополнительно
        Service(
            name="Консультация косметолога",
            category="Дополнительно",
            description="Разбор состояния кожи, составление плана процедур, рекомендации.",
            price=4000,
            duration=30,
        ),
        Service(
            name="Уходовый комплекс 'Сияние'",
            category="Дополнительно",
            description="Комплекс: чистка + пилинг + маска + массаж. Подарочный формат.",
            price=7000,
            duration=120,
        ),
    ]

    for s in services:
        session.add(s)

    # --- FAQ ---
    faqs = [
        FAQ(
            question="Сколько держится эффект от филеров губ?",
            answer="Обычно 6-12 месяцев в зависимости от препарата и организма. Рекомендую повторную процедуру раз в 8-10 месяцев для поддержания объёма.",
            order=1,
        ),
        FAQ(
            question="Больно ли делать чистку лица?",
            answer="Механическая чистка может быть немного неприятной, но я работаю максимально аккуратно. Ультразвуковая и комбинированная — практически безболезненные. При необходимости использую обезболивающий крем.",
            order=2,
        ),
        FAQ(
            question="Какой пилинг выбрать для первого раза?",
            answer="Для первого раза рекомендую миндальный пилинг 40% — он мягкий, подходит для чувствительной кожи, нет шелушения. После консультации подберу индивидуально.",
            order=3,
        ),
        FAQ(
            question="Когда виден результат после ботокса?",
            answer="Первый эффект виден через 3-5 дней, финальный результат — через 14 дней. Длится 4-6 месяцев.",
            order=4,
        ),
        FAQ(
            question="Можно ли беременным делать процедуры?",
            answer="Большинство косметологических процедур противопоказаны при беременности и кормлении грудью. Разрешены только базовые уходовые процедуры — уточняйте индивидуально.",
            order=5,
        ),
        FAQ(
            question="Нужна ли подготовка к лазерной эпиляции?",
            answer="Да! За 2 недели до процедуры нельзя выщипывать волосы (брить можно). За 3 дня не загорать. Перед первым сеансом — обязательная консультация.",
            order=6,
        ),
        FAQ(
            question="Есть ли реабилитация после пилингов?",
            answer="Зависит от типа пилинга: поверхностные — нет, срединные (Jessner) — шелушение 3-5 дней. Я даю подробные рекомендации по уходу после каждой процедуры.",
            order=7,
        ),
        FAQ(
            question="Как записаться на приём?",
            answer="Нажмите '🗓 Записаться к Ашуре' в меню, заполните анкету и напишите свою заявку. Я свяжусь с вами для согласования удобного времени.",
            order=8,
        ),
        FAQ(
            question="Принимаете ли вы по воскресеньям?",
            answer="По воскресеньям я обычно не работаю, но в исключительных случаях возможен приём. Уточняйте — иногда открываю slots на выходные.",
            order=9,
        ),
        FAQ(
            question="Что такое PRX-T33?",
            answer="Это инновационный пилинг-биоревитализант без инъекций. Стимулирует выработку коллагена, улучшает тургор и цвет лица. Нет шелушения, можно делать перед важными событиями.",
            order=10,
        ),
    ]

    for f in faqs:
        session.add(f)

    # --- Настройки ---
    settings = [
        AdminSettings(key="salon_name", value=Config.SALON_NAME),
        AdminSettings(key="welcome_message", value=f"Добро пожаловать в {Config.SALON_NAME}! 💫"),
    ]
    for s in settings:
        session.add(s)

    await session.commit()
    logger.info("База наполнена начальными данными: %d услуг, %d FAQ.", len(services), len(faqs))


async def apply_migrations(session: AsyncSession) -> None:
    """Обновляет схему и данные в существующей БД."""
    from sqlalchemy import text

    async def _safe_add_column(table: str, column: str, ddl: str) -> None:
        """Безопасно добавляет колонку — игнорирует если уже существует."""
        # Whitelist таблиц — защита от SQL injection через PRAGMA
        _ALLOWED_TABLES = frozenset({"users", "bookings", "services", "reviews",
                                     "faq", "bonus_transactions", "admin_settings",
                                     "pd_consent_logs"})
        if table not in _ALLOWED_TABLES:
            raise ValueError(f"Table '{table}' not in whitelist")
        cols_result = await session.execute(text(f'PRAGMA table_info("{table}")'))
        existing = {row[1] for row in cols_result.all()}
        if column not in existing:
            await session.execute(text(ddl))
            await session.commit()
            logger.info("Миграция: %s.%s добавлена.", table, column)

    # Колонки bookings
    await _safe_add_column("bookings", "extra_services_json",
                           "ALTER TABLE bookings ADD COLUMN extra_services_json TEXT")
    await _safe_add_column("bookings", "followup_sent_at",
                           "ALTER TABLE bookings ADD COLUMN followup_sent_at DATETIME")
    await _safe_add_column("bookings", "reminder_24h_sent",
                           "ALTER TABLE bookings ADD COLUMN reminder_24h_sent BOOLEAN DEFAULT 0")
    await _safe_add_column("bookings", "reminder_2h_sent",
                           "ALTER TABLE bookings ADD COLUMN reminder_2h_sent BOOLEAN DEFAULT 0")

    # Колонки users
    await _safe_add_column("users", "anamnesis_json",
                           "ALTER TABLE users ADD COLUMN anamnesis_json TEXT")
    await _safe_add_column("users", "anamnesis_updated_at",
                           "ALTER TABLE users ADD COLUMN anamnesis_updated_at DATETIME")
    await _safe_add_column("users", "pd_consent_at",
                           "ALTER TABLE users ADD COLUMN pd_consent_at DATETIME")
    await _safe_add_column("users", "pd_consent_version",
                           "ALTER TABLE users ADD COLUMN pd_consent_version VARCHAR(20)")
    await _safe_add_column("users", "skin_anamnesis_json",
                           "ALTER TABLE users ADD COLUMN skin_anamnesis_json TEXT")
    await _safe_add_column("users", "skin_anamnesis_at",
                           "ALTER TABLE users ADD COLUMN skin_anamnesis_at DATETIME")

    # Таблица pd_consent_logs
    tables_result = await session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'")
    )
    table_names = {row[0] for row in tables_result.all()}
    if "pd_consent_logs" not in table_names:
        await session.execute(
            text(
                """
                CREATE TABLE pd_consent_logs (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    telegram_id BIGINT NOT NULL,
                    policy_version VARCHAR(20) NOT NULL,
                    consented_at DATETIME NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    phone VARCHAR(20) NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
        )
        await session.commit()
        logger.info("Миграция: таблица pd_consent_logs создана.")

    # Колонки reviews
    await _safe_add_column("reviews", "notified_admin",
                           "ALTER TABLE reviews ADD COLUMN notified_admin BOOLEAN DEFAULT 0")

    # CRM миграции (ДО любых SELECT из services/users)
    await _safe_add_column("services", "revisit_days",
                           "ALTER TABLE services ADD COLUMN revisit_days INTEGER")
    await _safe_add_column("users", "next_visit_at",
                           "ALTER TABLE users ADD COLUMN next_visit_at DATETIME")
    await _safe_add_column("users", "next_visit_manual",
                           "ALTER TABLE users ADD COLUMN next_visit_manual BOOLEAN DEFAULT 0")
    await _safe_add_column("users", "revisit_reminder_sent_for",
                           "ALTER TABLE users ADD COLUMN revisit_reminder_sent_for DATETIME")
    await _safe_add_column("users", "churn_alert_30_sent",
                           "ALTER TABLE users ADD COLUMN churn_alert_30_sent BOOLEAN DEFAULT 0")
    await _safe_add_column("users", "churn_alert_60_sent",
                           "ALTER TABLE users ADD COLUMN churn_alert_60_sent BOOLEAN DEFAULT 0")
    await _safe_add_column("users", "revisit_reminder_disabled",
                           "ALTER TABLE users ADD COLUMN revisit_reminder_disabled BOOLEAN DEFAULT 0")
    await _safe_add_column("users", "revisit_reminder_no_response",
                           "ALTER TABLE users ADD COLUMN revisit_reminder_no_response INTEGER DEFAULT 0")
    await _safe_add_column("users", "sheets_dirty",
                           "ALTER TABLE users ADD COLUMN sheets_dirty BOOLEAN DEFAULT 0")
    await _safe_add_column("users", "sheets_last_synced",
                           "ALTER TABLE users ADD COLUMN sheets_last_synced DATETIME")

    # Значения revisit_days по умолчанию для существующих услуг
    _revisit_defaults = [
        ("Чистка лица (механическая)", 28),
        ("Чистка лица (комбинированная)", 28),
        ("Пилинг поверхностный", 21),
        ("Пилинг срединный", 42),
        ("Ботулинотерапия (Ботокс)", 120),
        ("Увеличение губ", 180),
        ("Биоревитализация", 90),
        ("Контурная пластика", 180),
        ("Мезотерапия", 30),
        ("Лазерная депиляция", 42),
    ]
    for svc_name, days in _revisit_defaults:
        await session.execute(
            text("UPDATE services SET revisit_days = :days WHERE name = :name AND revisit_days IS NULL"),
            {"days": days, "name": svc_name},
        )
    await session.commit()

    # Индекс для CRM-запросов
    try:
        await session.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_users_next_visit ON users(next_visit_at) WHERE next_visit_at IS NOT NULL"
        ))
        await session.commit()
    except Exception:
        pass

    # Обновление цены консультации
    result = await session.execute(
        select(Service).where(Service.name == "Консультация косметолога")
    )
    consult = result.scalar_one_or_none()
    if consult and consult.price != 4000:
        consult.price = 4000
        await session.commit()
        logger.info("Миграция: цена консультации обновлена до 4000 ₽.")

    # UNIQUE index: одна активная запись на пользователя (защита от race condition)
    try:
        await session.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_booking "
                "ON bookings(user_id) WHERE status IN ('pending', 'confirmed')"
            )
        )
        await session.commit()
        logger.info("Миграция: UNIQUE index idx_one_active_booking создан.")
    except Exception as e:
        logger.debug("UNIQUE index уже существует или не поддерживается: %s", e)

    # Индексы для производительности
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_bookings_user_status ON bookings(user_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_preferred_date ON bookings(preferred_date)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_created_at ON bookings(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_service_id ON bookings(service_id)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_reminders ON bookings(status, preferred_date) WHERE status = 'confirmed'",
        "CREATE INDEX IF NOT EXISTS idx_bookings_completed_at ON bookings(completed_at)",
        "CREATE INDEX IF NOT EXISTS idx_bonus_tx_booking_id ON bonus_transactions(booking_id)",
        "CREATE INDEX IF NOT EXISTS idx_bonus_tx_booking_amount ON bonus_transactions(booking_id, amount)",
        "CREATE INDEX IF NOT EXISTS idx_bonus_tx_user_id ON bonus_transactions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_pd_consent_user_id ON pd_consent_logs(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_reviews_moderation ON reviews(is_published, notified_admin)",
        "CREATE INDEX IF NOT EXISTS idx_reviews_user_id ON reviews(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_services_name ON services(name)",
        "CREATE INDEX IF NOT EXISTS idx_services_active ON services(is_active)",
        "CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone)",
        "CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at)",
    ]:
        try:
            await session.execute(text(idx_sql))
        except Exception:
            pass
    await session.commit()
    logger.info("Миграция: индексы созданы.")

    # Миграция: добавляем tx_type в bonus_transactions
    await _safe_add_column("bonus_transactions", "tx_type",
                           "ALTER TABLE bonus_transactions ADD COLUMN tx_type VARCHAR(20) NOT NULL DEFAULT 'MANUAL'")

    # Заполняем tx_type для существующих записей на основе description
    _tx_type_updates = [
        ("при подтверждении", "CONFIRM_BONUS"),
        ("за лояльность", "CONFIRM_BONUS"),
        ("выполненную запись", "VISIT_BONUS"),
        ("Списание бонусов", "SPEND"),
        ("Возврат бонусов", "REFUND"),
        ("Отзыв бонуса", "REVOKE"),
        ("Отмена начисления", "REVOKE"),
        ("Корректировка", "MANUAL"),
        ("Ручное начисление", "MANUAL"),
    ]
    for pattern, tx_type_val in _tx_type_updates:
        await session.execute(
            text(f"UPDATE bonus_transactions SET tx_type = :tx_type "
                 f"WHERE description LIKE :pattern AND tx_type = 'MANUAL'"),
            {"tx_type": tx_type_val, "pattern": f"%{pattern}%"},
        )
    await session.commit()

    # Индекс на tx_type
    try:
        await session.execute(text("CREATE INDEX IF NOT EXISTS idx_bonus_tx_type ON bonus_transactions(tx_type)"))
        await session.commit()
    except Exception:
        pass

    # Unique constraint: один CONFIRM_BONUS и один VISIT_BONUS на запись
    try:
        await session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_bonus_tx_unique_grant "
            "ON bonus_transactions(booking_id, tx_type) "
            "WHERE booking_id IS NOT NULL AND tx_type IN ('CONFIRM_BONUS', 'VISIT_BONUS') AND amount > 0"
        ))
        await session.commit()
    except Exception:
        pass

    # REVOKE идемпотентность — через application-level check (description-based),
    # т.к. на одной записи может быть REVOKE для CONFIRM_BONUS и REVOKE для VISIT_BONUS

    # Новые услуги (добавляются если не существуют)
    # Примечание: «Ботулинотерапия (Ботокс)» и «Липолитики» уже в seed_data — не дублируем
    new_services = [
        ("Ботокс — лоб", "Инъекции", "Ботулинотерапия зоны лба.", 5000, 20),
        ("Ботокс — межбровка", "Инъекции", "Ботулинотерапия межбровных морщин.", 4000, 20),
        ("Ботокс — гусиные лапки", "Инъекции", "Ботулинотерапия периорбитальной зоны.", 5000, 20),
        ("Ботокс — полное лицо", "Инъекции", "Ботулинотерапия всех зон лица.", 15000, 45),
        ("Ботокс — гипергидроз (подмышки)", "Инъекции", "Лечение гипергидроза ботулотоксином.", 12000, 30),
        ("Morpheus8 (фракционный RF-лифтинг)", "Аппаратные", "Фракционный RF-лифтинг Morpheus8.", 18000, 60),
        ("BBL (BroadBand Light — фотоомоложение)", "Аппаратные", "Фотоомоложение BBL.", 12000, 45),
        ("Лазерная депиляция", "Лазер", "Лазерная депиляция — общее.", 5000, 30),
        ("Лазерная депиляция — лицо", "Лазер", "Лазерная депиляция лица.", 3000, 20),
        ("Лазерная депиляция — тело", "Лазер", "Лазерная депиляция тела.", 6000, 45),
        ("Консультация косметолога", "Консультации", "Персональная консультация косметолога.", 4000, 30),
    ]
    existing_names = set()
    try:
        result = await session.execute(select(Service.name))
        existing_names = set(result.scalars().all())
    except Exception:
        pass
    added = 0
    for name, cat, desc, price, dur in new_services:
        if name not in existing_names:
            session.add(Service(name=name, category=cat, description=desc, price=price, duration=dur, is_active=True))
            added += 1
    if added:
        await session.commit()
        logger.info("Миграция: добавлено %d новых услуг.", added)
