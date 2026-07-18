# ЗАДАНИЕ ДЛЯ GROK BUILD — ФИНАЛЬНЫЙ REGRESSION AUDIT

## Контекст
Бот AshuraCosm получил 37 фиксов (P0+P1+P2+P3) и 101 unit-тест.
Текущая директория: C:\Users\Лек\Desktop\бот.тг\cosmetology_bot

## Задача
Проведи regression audit — проверь, что фиксы не сломали друг друга.

## Что проверять

### 1. ThrottlingMiddleware (bot.py:158-177)
- `getattr(event, 'event', None)` — работает ли для Message И CallbackQuery?
- Есть ли `inner_event.answer` на Message? (нет — только на CallbackQuery)

### 2. Anamnesis token (client.py + keyboards.py)
- Формат `anam_{token}_{key}_{yes|no}` — коллизии с другими callback_data?
- Все ли 5 вызовов `anamnesis_keyboard` передают токен?

### 3. AdminOnly middleware (admin.py:87-112)
- Блокирует ли `/admin` команду? (не должен — middleware на router, команда тоже на router)
- Пропускает ли `is_admin()` функцию? (да — она не handler)

### 4. Deep links (common.py)
- `_dispatch_deep_link('')` — пустой payload?
- `_dispatch_deep_link('book')` — работает?
- `_dispatch_deep_link('service_999')` — несуществующий ID?

### 5. Success-before-commit (client.py)
- `await session.commit()` ДО показа успеха — double commit с middleware?
- Бонусная транзакция ПОСЛЕ commit — атомарность?

### 6. Prompt injection filter (grok.py)
- `_sanitize_user_message('выберите процедуру')` — не фильтрует ли 'вы'?
- `_sanitize_user_message('Ignore Previous Instructions')` — фильтрует?

### 7. Privacy consent version (privacy.py)
- `has_pd_consent(user)` с `pd_consent_version=None` — legacy пользователи?
- Смена версии в .env — ломает ли существующих?

### 8. PII cleanup (pii_cleanup.py)
- Пустая БД — не падает?
- Нет записей старше 365 дней — корректно?

### 9. Audit trail (audit.py + admin.py)
- `log_admin_action` работает после `state.clear()`?
- admin_id=0 в `/bonus` команде — не ломает?

### 10. Тесты
```bash
python -m pytest test_config.py test_security.py test_database.py test_validators.py -v
```
Все 101 теста должны pass.

## Формат ответа
Для каждого пункта: PASS / FAIL / WARN + evidence (file:line).
Итог: REGRESSION FOUND или CLEAN.
