"""
Auto-CRM Simulator: Прогон 3-месячного сценария за 30 секунд.

Демонстрирует полный User Journey:
  1. Клиент записывается на "Ботокс фулл фейс" → Google Sheets: "Записан"
  2. Админ подтверждает → Google Sheets: "Подтвержден"
  3. Процедура завершена → Google Sheets: "Выполнен"
  4. Через 7 дней → Follow-up через Grok → Google Sheets: "Фоллоу-ап"
  5. Через 3 месяца → Re-engagement через Grok → Google Sheets: "Прогрев 3 мес"

Запуск: python auto_crm_simulator.py
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from database import Booking, Service, User, BonusTransaction


# =============================================================================
# МОК-ОБЪЕКТЫ
# =============================================================================

class MockBot:
    """Мок Telegram бота для симуляции."""
    sent_messages = []

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.sent_messages.append({
            "chat_id": chat_id,
            "text": text,
            "reply_markup": str(reply_markup) if reply_markup else None,
            "parse_mode": parse_mode,
        })
        return {"message_id": len(self.sent_messages)}

    async def send_chat_action(self, chat_id, action):
        pass

    def reset(self):
        self.sent_messages = []


class MockUser:
    """Мок пользователя."""
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.telegram_id = kwargs.get("telegram_id", 123456789)
        self.name = kwargs.get("name", "Юлия")
        self.phone = kwargs.get("phone", "+79275555494")
        self.bonus_balance = kwargs.get("bonus_balance", 0)
        self.next_visit_at = kwargs.get("next_visit_at", None)
        self.next_visit_manual = kwargs.get("next_visit_manual", False)
        self.revisit_reminder_disabled = kwargs.get("revisit_reminder_disabled", False)
        self.pd_consent_at = kwargs.get("pd_consent_at", datetime.now())
        self.sheets_dirty = kwargs.get("sheets_dirty", False)


class MockService:
    """Мок услуги."""
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.name = kwargs.get("name", "Ботокс — полное лицо")
        self.price = kwargs.get("price", 15000)
        self.revisit_days = kwargs.get("revisit_days", 120)
        self.duration = kwargs.get("duration", 45)
        self.category = kwargs.get("category", "Инъекции")


class MockBooking:
    """Мок записи."""
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 42)
        self.user_id = kwargs.get("user_id", 1)
        self.service_id = kwargs.get("service_id", 1)
        self.preferred_date = kwargs.get("preferred_date", "26.07.2026")
        self.preferred_time = kwargs.get("preferred_time", "14:00")
        self.status = kwargs.get("status", "pending")
        self.total_amount = kwargs.get("total_amount", None)
        self.bonus_used = kwargs.get("bonus_used", 0)
        self.completed_at = kwargs.get("completed_at", None)
        self.followup_sent_at = kwargs.get("followup_sent_at", None)
        self.reminder_24h_sent = kwargs.get("reminder_24h_sent", False)
        self.reminder_2h_sent = kwargs.get("reminder_2h_sent", False)
        self.service = kwargs.get("service", None)
        self.user = kwargs.get("user", None)
        self.notes = kwargs.get("notes", "")
        self.anamnesis_json = kwargs.get("anamnesis_json", None)
        self.extra_services_json = kwargs.get("extra_services_json", None)
        self.confirmed_at = kwargs.get("confirmed_at", None)


# =============================================================================
# СИМУЛЯТОР
# =============================================================================

class AutoCRMSimulator:
    """Симулятор полного 3-месячного сценария."""

    def __init__(self):
        self.bot = MockBot()
        self.user = MockUser()
        self.service = MockService()
        self.booking = MockBooking(service=self.service, user=self.user)
        self.sheet_data = {}  # Имитация Google Sheets
        self.timeline = []    # Лог событий
        self.step = 0

    def log(self, event, details=""):
        """Логирует событие."""
        self.step += 1
        ts = datetime.now().strftime("%H:%M:%S")
        entry = {
            "step": self.step,
            "time": ts,
            "event": event,
            "details": details,
            "sheet_status": self.sheet_data.get("status", "—"),
        }
        self.timeline.append(entry)
        print(f"  [{self.step:2d}] {event}")
        if details:
            print(f"       {details}")

    def update_sheet(self, **kwargs):
        """Обновляет Google Sheets."""
        self.sheet_data.update(kwargs)
        status = kwargs.get("status", self.sheet_data.get("status", "—"))
        print(f"       📊 Google Sheets → Статус: {status}")
        for k, v in kwargs.items():
            if k != "status" and v:
                print(f"       📊   {k}: {v}")

    async def simulate(self):
        """Запускает полную симуляцию."""
        print("=" * 70)
        print("  AUTO-CRM SIMULATOR — 3-МЕСЯЧНЫЙ СЦЕНАРИЙ")
        print("  Клиент: Юлия | Услуга: Ботокс — полное лицо | Цена: 15 000₽")
        print("=" * 70)
        print()

        # Инициализация Google Sheets
        self.sheet_data = {
            "tg_id": str(self.user.telegram_id),
            "phone": self.user.phone,
            "name": self.user.name,
            "visits": "0",
            "last_procedure": "",
            "last_date": "",
            "last_amount": "",
            "total_spent": "0",
            "next_visit": "",
            "status": "Новый",
            "bot_wrote": "",
            "notes": "",
        }
        self.log("Инициализация", f"Клиент {self.user.name} ({self.user.phone})")
        print()

        # === ШАГ 1: ЗАПИСЬ ===
        print("-" * 50)
        print("ШАГ 1: Клиент записывается через бота")
        print("-" * 50)
        await self._step1_booking()
        print()

        # === ШАГ 2: ПОДТВЕРЖДЕНИЕ АДМИНОМ ===
        print("-" * 50)
        print("ШАГ 2: Админ подтверждает запись")
        print("-" * 50)
        await self._step2_confirmation()
        print()

        # === ШАГ 3: ЗАВЕРШЕНИЕ ПРОЦЕДУРЫ ===
        print("-" * 50)
        print("ШАГ 3: Процедура завершена")
        print("-" * 50)
        await self._step3_completion()
        print()

        # === ШАГ 4: FOLLOW-UP ЧЕРЕЗ 7 ДНЕЙ ===
        print("-" * 50)
        print("ШАГ 4: Follow-up через 7 дней (Grok)")
        print("-" * 50)
        await self._step4_followup()
        print()

        # === ШАГ 5: RE-ENGAGEMENT ЧЕРЕЗ 3 МЕСЯЦА ===
        print("-" * 50)
        print("ШАГ 5: Re-engagement через 3 месяца (Grok)")
        print("-" * 50)
        await self._step5_reengagement()
        print()

        # === ИТОГОВАЯ ТАБЛИЦА ===
        print("=" * 70)
        print("  ИТОГОВОЕ СОСТОЯНИЕ GOOGLE SHEETS")
        print("=" * 70)
        self._print_final_table()
        print()

        # === ЛОГ СОБЫТИЙ ===
        print("=" * 70)
        print("  ЛОГ СОБЫТИЙ (ТАЙМЛАЙН)")
        print("=" * 70)
        for entry in self.timeline:
            print(f"  [{entry['step']:2d}] {entry['time']} | {entry['event']}")
            if entry['details']:
                print(f"       {entry['details']}")
        print()

        # === СООБЩЕНИЯ БОТА ===
        print("=" * 70)
        print("  СООБЩЕНИЯ БОТА КЛИЕНТУ")
        print("=" * 70)
        for i, msg in enumerate(self.bot.sent_messages, 1):
            text_preview = msg['text'][:150].replace('\n', ' ')
            print(f"  [{i}] → {msg['chat_id']}: {text_preview}...")
        print()

        print("=" * 70)
        print("  СИМУЛЯЦИЯ ЗАВЕРШЕНА УСПЕШНО!")
        print("=" * 70)

    async def _step1_booking(self):
        """Шаг 1: Клиент записывается."""
        self.booking.status = "pending"
        self.booking.preferred_date = "26.07.2026"
        self.booking.preferred_time = "14:00"
        self.booking.notes = ""

        self.log("Клиент нажал /start", "Запуск бота")
        self.log("Выбор процедуры", f"{self.service.name} — {self.service.price}₽")
        self.log("Выбор даты", self.booking.preferred_date)
        self.log("Выбор времени", self.booking.preferred_time)
        self.log("Запись создана", f"Заявка #{self.booking.id} (pending)")

        self.update_sheet(
            status="Записан",
            notes=f"Запись на {self.booking.preferred_date} {self.booking.preferred_time}",
        )

        # Уведомление админу
        self.log("Уведомление админу", f"Новая заявка от {self.user.name}")

    async def _step2_confirmation(self):
        """Шаг 2: Админ подтверждает."""
        self.booking.status = "confirmed"
        self.booking.confirmed_at = datetime.now()

        self.log("Админ нажал 'Принять'", f"Заявка #{self.booking.id} подтверждена")
        self.log("Начисление бонусов", f"3% от {self.service.price}₽ = {int(self.service.price * 0.03)} бонусов")
        self.user.bonus_balance = int(self.service.price * 0.03)

        self.update_sheet(
            status="Подтвержден",
            notes=f"Подтверждено: {self.booking.preferred_date} {self.booking.preferred_time}",
        )

        # Уведомление клиенту
        text = (
            f"✅ Здравствуйте, {self.user.name}!\n\n"
            f"Ваша запись подтверждена:\n"
            f"💅 Услуга: {self.service.name}\n"
            f"📅 Дата: {self.booking.preferred_date}\n"
            f"⏰ Время: {self.booking.preferred_time}\n\n"
            f"📍 Адрес: ул. Примерная, 12\n"
            f"📱 Телефон: +7 (XXX) XXX-XX-XX\n\n"
            f"🎁 Вам начислено {int(self.service.price * 0.03)} бонусов (3% скидка)!\n"
            f"💰 Баланс: {self.user.bonus_balance} бонусов\n\n"
            f"Ждем вас! 💫"
        )
        self.bot.send_message(chat_id=self.user.telegram_id, text=text)
        self.log("Уведомление клиенту", "Запись подтверждена + бонусы начислены")

    async def _step3_completion(self):
        """Шаг 3: Процедура завершена."""
        self.booking.status = "completed"
        self.booking.completed_at = datetime.now()
        self.booking.total_amount = self.service.price

        self.log("Процедура завершена", f"{self.service.name} — {self.service.price}₽")
        self.log("CRM: next_visit_at", f"{(datetime.now() + timedelta(days=self.service.revisit_days)).strftime('%d.%m.%Y')} (через {self.service.revisit_days} дней)")

        self.user.next_visit_at = datetime.now() + timedelta(days=self.service.revisit_days)
        self.user.next_visit_manual = False
        self.user.bonus_balance += int(self.service.price * 0.03)  # Бонус за визит
        self.user.sheets_dirty = True

        self.update_sheet(
            status="Выполнен",
            last_procedure=self.service.name,
            last_date=datetime.now().strftime("%d.%m.%Y"),
            last_amount=str(self.service.price),
            total_spent=str(self.service.price),
            visits="1",
            next_visit=self.user.next_visit_at.strftime("%d.%m.%Y"),
            notes="",
        )

    async def _step4_followup(self):
        """Шаг 4: Follow-up через 7 дней."""
        # Имитация fast-forward на 7 дней
        future = datetime.now() + timedelta(days=7)
        self.log("⏰ Fast-forward", f"Симуляция: {future.strftime('%d.%m.%Y')} (через 7 дней)")

        # Генерация текста через Grok (имитация)
        followup_text = (
            f"Здравствуйте, {self.user.name}!\n\n"
            f"Прошла неделя после вашей процедуры ({self.service.name}). "
            f"Как вы себя чувствуете? Всё ли хорошо? 💫\n\n"
            f"Если есть вопросы — напишите, я помогу!"
        )

        self.log("Grok: генерация текста", "Follow-up сообщение сгенерировано")
        self.bot.send_message(chat_id=self.user.telegram_id, text=followup_text)
        self.log("Отправка клиенту", "Follow-up отправлен")

        # Имитация ответа клиента
        self.log("Клиент отвечает", "Всё ок, спасибо!")

        self.update_sheet(
            status="Фоллоу-ап",
            bot_wrote=f"Follow-up {future.strftime('%d.%m.%Y')}",
            notes=f"Ответ клиента: Всё ок, спасибо!",
        )

    async def _step5_reengagement(self):
        """Шаг 5: Re-engagement через 3 месяца."""
        future = datetime.now() + timedelta(days=90)
        self.log("⏰ Fast-forward", f"Симуляция: {future.strftime('%d.%m.%Y')} (через 3 месяца)")

        # Проверка: пора ли напоминать
        days_since = 90
        self.log("Проверка условий", f"Прошло {days_since} дней, рекомендуемый интервал: {self.service.revisit_days} дней")

        # Генерация текста через Grok (имитация)
        reengagement_text = (
            f"Здравствуйте, {self.user.name}! 💫\n\n"
            f"Прошло уже {days_since} дней с момента вашей процедуры ({self.service.name}). "
            f"Эффект от ботокса может постепенно спадать — это естественно.\n\n"
            f"Если захотите освежить результат или попробовать что-то новое — "
            f"буду рада помочь! Записаться можно через /start 🌸"
        )

        self.log("Grok: генерация текста", "Re-engagement сообщение сгенерировано")
        self.bot.send_message(chat_id=self.user.telegram_id, text=reengagement_text)
        self.log("Отправка клиенту", "Re-engagement отправлен")

        # Обновление Google Sheets
        self.update_sheet(
            status="Прогрев 3 мес",
            bot_wrote=f"Прогрев 3 мес {future.strftime('%d.%m.%Y')}",
            notes=f"Re-engagement: {self.service.name}, {days_since} дней",
        )

    def _print_final_table(self):
        """Печатает итоговую таблицу Google Sheets."""
        headers = [
            "TG ID", "Телефон", "Имя", "Визитов",
            "Последняя процедура", "Дата", "Сумма",
            "Всего потратил", "Следующий визит",
            "Статус", "Бот писал", "Заметки"
        ]
        values = [
            self.sheet_data.get("tg_id", ""),
            self.sheet_data.get("phone", ""),
            self.sheet_data.get("name", ""),
            self.sheet_data.get("visits", ""),
            self.sheet_data.get("last_procedure", ""),
            self.sheet_data.get("last_date", ""),
            self.sheet_data.get("last_amount", ""),
            self.sheet_data.get("total_spent", ""),
            self.sheet_data.get("next_visit", ""),
            self.sheet_data.get("status", ""),
            self.sheet_data.get("bot_wrote", ""),
            self.sheet_data.get("notes", ""),
        ]

        # Print header
        print()
        for h, v in zip(headers, values):
            print(f"  {h:20s} | {v}")
        print()


# =============================================================================
# MAIN
# =============================================================================

async def main():
    simulator = AutoCRMSimulator()
    await simulator.simulate()


if __name__ == "__main__":
    asyncio.run(main())
