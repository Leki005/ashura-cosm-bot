# Arena Mode — Полный QA-аудит Telegram-бота
**Проект:** `Desktop/бот.тг/cosmetology_bot` (Кабинет косметолога Ашуры)  
**Стек:** aiogram 3 · SQLAlchemy · SQLite · APScheduler · optional Redis · Grok/xAI  
**Дата:** 2026-07-18  
**Метод:** 4 параллельных Arena-агента (security / flows / admin-AI / prod) + merge  
**Чеклист:** `ц.txt` (Staff+ Telegram Bot QA)

---

## Вердикт одной строкой

**Рабочий MVP для одного салона, НЕ production-ready** для публичной нагрузки, мед.данных и платного LLM без hotfix-пакета P0.

**Оценка готовности: 4/10**

| Секция | Оценка | Статус |
|--------|--------|--------|
| 1. Архитектура | 5/10 | Polling ок, BotFather/deep links/webhook слабые |
| 2. Сценарии | 6/10 | Happy path живой; second booking / notify / double-submit — дыры |
| 3. Edge cases | 4/10 | Stale anamnesis, throttle, FSM |
| 4. Ошибки/надёжность | 5/10 | Alerts есть; silent fails есть |
| 5. Безопасность | 3/10 | Нет SQLi, но throttle мёртв, AI DoS, SSL off, Redis bare |
| 6. Производительность | 2/10 | 1000 concurrent — фантазия |
| 7. UX | 5/10 | Меню есть; Да/Нет инверсия, «.», нет cancel на шагах |
| 8. Telegram-фичи | 4/10 | Inline ок; payments нет; Mini App — муляж |
| 9. Логи/мониторинг | 3/10 | Rotating log + TG ERROR; нет metrics/analytics |
| 10. Prod readiness | 3/10 | Deploy scripts есть; бэкапы/observability — нет |

---

# 1. Архитектура и глобальные настройки

## Что есть
- Polling (`delete_webhook` + long poll), retry на `TelegramNetworkError`
- `set_my_commands`: start / menu / restart / help
- Config из `.env`, validate BOT_TOKEN + ADMIN_ID
- Privacy consent middleware (152-ФЗ каркас)
- Middleware: DbSession, Throttling (сломан — см. §5), Privacy
- FSM: Redis если `REDIS_URL`, иначе MemoryStorage
- APScheduler: reminders 5m, reviews 2m
- Docker + compose + deploy scripts

## Проблемы

| Sev | Finding |
|-----|---------|
| CRITICAL | ThrottlingMiddleware на `Update` — `from_user` всегда None → антифлуд мёртв |
| CRITICAL | `PROXY_SSL_VERIFY` default **false** → MITM на BOT_TOKEN / XAI / ПДн |
| CRITICAL | Redis без пароля + `network_mode: host` |
| HIGH | `drop_pending_updates=True` на каждом старте — теряются updates при deploy |
| HIGH | MemoryStorage fallback при падении Redis — **тихий** деград |
| HIGH | `FSM_TTL_MINUTES=10` объявлен, **нигде не применяется** |
| MED | Нет `set_chat_menu_button`, descriptions, deep-linking `/start payload` |
| MED | Webhook mode отсутствует (горизонталь невозможна) |
| MED | Terms of Service нет (только PD consent) |
| LOW | `.env.example` неполный (ERROR_NOTIFY_ID, REDIS_URL, KIMI_*) |

## Тест-кейсы
1. Рестарт контейнера mid-booking → FSM state? (Memory: lost / Redis: keep)
2. `drop_pending_updates` — сообщение во время deploy исчезает
3. Redis kill → бот продолжает с Memory без hard fail
4. BotFather: commands видны; menu button WebApp — нет

## Рекомендации
- Fail-hard в prod если Redis down
- SSL verify true (исключение Reality — явным env + warning)
- Redis requirepass + bridge network
- Env-флаг `DROP_PENDING_UPDATES` (default false в prod)
- Deep links: `book`, `service_{id}`, `review`

---

# 2. Полное покрытие пользовательских сценариев

## Карта flows

| Flow | Статус |
|------|--------|
| Согласие ПДн → регистрация | ✅ |
| `/start` `/menu` `/help` `/restart` | ✅ / ⚠️ restart обходит consent UX |
| Запись (general + from service) | ✅ happy path |
| Анамнез 7 дней | ✅ |
| Merge «добавить услугу» | ✅ |
| Merge «другое время» | ⛔ **CRITICAL — ломается** |
| Мои записи / отмена / rebook | ⚠️ ок, дедлайн 6ч |
| Бонусы (баланс, списание, возврат) | ⚠️ |
| Услуги / FAQ / контакты / отзывы | ✅ |
| AI / skin anamnesis | ⚠️ |
| Оплата (Stars/YooKassa) | ❌ N/A |
| Deep links | ❌ |
| Multi-device session | ⚠️ last-write-wins |

## CRITICAL / HIGH

### C01 — «Записаться на другое время» vs UNIQUE index
- UI предлагает вторую active-запись (`merge_separate`)
- БД: `idx_one_active_booking` — одна active на user
- `_finalize_booking` с `skip_active_booking_check` + notify + success **до** commit middleware
- **Риск:** клиент и админ видят успех, запись откатывается `IntegrityError`

**Тест:** active booking → «другое время» → до финала → проверить БД и уведомления.

### C02 — Double-submit finalize
Double-tap «Пропустить» notes → двойной INSERT / notify.

### C03 — Notify fail silent, клиенту врут
```python
try: await notify_admin_new_booking(...)
except: logger.error(...)
await show_booking_success("✅ ... Ашура получила уведомление")
```
Если админ не стартовал бота / block / wrong OWNER_ID — запись `pending`, админ не знает.

### H02 — Revoke PD silent cancel
Active bookings → cancelled без admin notify, без единого cancel-pipeline (бонусы).

### H03 — Нет deep links
Реклама/Instagram → нельзя `/start book`.

## Негативные сценарии (дыры)
| Сценарий | Проблема |
|----------|----------|
| `book_svc` на inactive service | нет проверки `is_active` |
| merge custom not in catalog | тупик «услуга не найдена» |
| `waiting_merge_note` | state dead — никогда не set |
| cancel < 6h | блок есть, нет «написать Ашуре» deep action |
| general booking без service | бонусы при записи слабо связаны |

---

# 3. Edge cases

| # | Case | Sev | Детали |
|---|------|-----|--------|
| E1 | Stale anamnesis buttons | **CRITICAL** | index++ независимо от key → corrupt медданные |
| E2 | Long notes >500 | MED | trim есть; review text без лимита |
| E3 | HTML/emoji в имени | HIGH | HTML injection в reminders (не всегда escape) |
| E4 | Flood messages | HIGH | throttle silent на Message; middleware мёртв на Update |
| E5 | Flood callbacks | CRITICAL | почти без throttle |
| E6 | Offline / restart mid FSM | HIGH | MemoryStorage loss |
| E7 | Timezone | MED | UTC+4 naive, не подписано «Астрахань» |
| E8 | Manual time 03:00 | MED | format ok, **нет** рабочих часов |
| E9 | Foreign phone | MED | только RU |
| E10 | Contact spoof | LOW | check есть; `user_id is None` legacy |
| E11 | Skin unguarded callbacks | HIGH | `skin_*_skip` без state filter |
| E12 | Double skin finalize | MED | race double notify |
| E13 | Platforms | MED | длинные skin buttons на SE |
| E14 | Concurrent bonus/booking | MED | atomic UPDATE есть (хорошо) |

**Тест stale anamnesis:**
1. 3 ответа → «пройти заново»
2. Тап старой кнопки Да/Нет
3. Проверить JSON анамнеза

---

# 4. Обработка ошибок и надёжность

## Плюсы
- Rotating logs 10MB×5
- `ErrorNotifyHandler` → Telegram
- `dp.errors` handler
- DbSession rollback on exception
- Polling retry ×5 на network
- Broadcast `RetryAfter` handling (частично)

## Минусы
| Sev | Finding |
|-----|---------|
| CRITICAL | Success UI до commit + swallow notify errors |
| HIGH | Global error handler `return True` — клиент часто без «что-то сломалось» |
| HIGH | `ERROR_NOTIFY_ID` не в `.env.example` — легко забыть |
| HIGH | BOT_TOKEN не redact в error notify (XAI/KIMI — да) |
| MED | Нет circuit breaker «техработы» при SQLite lock |
| MED | Redis fail → silent Memory |
| MED | Нет graceful drain AI/broadcast на SIGTERM |
| MED | Docker healthcheck = «cmdline app.py», не getMe/DB |

---

# 5. Безопасность (критично)

## Хорошо
- ORM / параметризованные SQL — **SQLi практически нет**
- Admin checks на большинстве handlers
- IDOR cancel/merge/followup — scope by user_id
- Contact spoof check
- Bonus atomic `WHERE balance >=`
- Partial unique active booking
- html_escape в **многих** (не всех) местах
- Privacy middleware блокирует бизнес-flow

## CRITICAL

| ID | Issue |
|----|-------|
| S1 | **ThrottlingMiddleware мёртв** (Update without from_user) |
| S2 | **AI cost DoS**: global `AI_DAILY_LIMIT=500`, in-RAM, photo session reset on re-enter, video без limit |
| S3 | Redis bare + host network |
| S4 | SSL verify default false |
| S5 | Revoke PD неполный (pd_consent_logs, reviews, bonus_tx, logs) |
| S6 | BOT_TOKEN leak risk в error notify |

## HIGH

| ID | Issue |
|----|-------|
| S7 | HTML injection: `user.name` в reminders / collision без escape |
| S8 | Admin broadcast: preview escape, **send raw HTML** → phishing blast radius |
| S9 | Dockerfile `COPY . .` без надёжного `.dockerignore` → `.env` / `bot.db` в image |
| S10 | `bot.db.backup_*` не в gitignore; pack может утащить ПДн |
| S11 | Privacy policy врёт про «не обучение ИИ» + нет xAI/трансгранички/биометрии |
| S12 | Prompt injection: user text/photo as-is, «дерзкий» system prompt |
| S13 | Admin = single TG ID, no 2FA, no router-level middleware |

## MEDIUM
- Нет CAPTCHA / antibot на регистрацию
- SQLite file = full PII at rest unencrypted
- Admin action audit trail слабый
- Mini App `initDataUnsafe` без HMAC (сейчас mock — dormant bomb)

## Attack playbooks
1. **Callback flood** 50 RPS → SQLite + CPU
2. **AI burn:** enter → 10 photos → exit → repeat → $ + global limit
3. **Lost booking:** wrong OWNER / blocked bot → client «успех»
4. **Name XSS-HTML:** `</b><a href=evil>` в followup/admin
5. **Broadcast phishing** при угоне ADMIN_ID
6. **Prompt jailbreak** → ложные цены/медсоветы
7. **Future Mini App IDOR** без initData HMAC

---

# 6. Производительность

## Честная ёмкость
| Профиль | Вердикт |
|---------|---------|
| 1 кабинет, <500 clients, <20 peak, AI rare | Условно OK + Redis + backups |
| 1k DAU / 100 concurrent menu | Нужен Postgres + session fix |
| **1000 concurrent** | **HARD FAIL** |

## Критические антипаттерны
1. **DbSession на весь handler включая Grok 60–300s** — SQLite connection hold
2. SQLite 1 writer + busy_timeout 30s
3. Reminders: SELECT all confirmed → parse string dates в Python каждые 5 мин
4. Нет AI concurrency semaphore
5. Compose 512MB + vision base64 = OOM risk
6. Polling single process; dual instance = dual reminders / dual poll hell
7. Broadcast: load all users + 0.5s sleep sequential

## Load-test matrix (ожидание today)
| Test | Expect |
|------|--------|
| Smoke funnel | PASS |
| 50 menu concurrent | Marginal |
| 30 concurrent AI | FAIL |
| 10 skin photos concurrent | FAIL risk |
| 5k confirmed reminder job | FAIL |
| Redis kill mid-dialog | FAIL (Memory) |
| 1000 concurrent target | HARD FAIL |

---

# 7. Usability и UX

| Sev | Issue |
|-----|-------|
| HIGH | Кнопки анамнеза **«✅ Нет» / «❌ Да»** — когнитивный перевёртыш |
| HIGH | `_hide_quick_commands` шлёт **«.»** в чат; reply-keyboard **не восстанавливается** |
| HIGH | Нет `cancel_fsm` на procedure/date/anam — легко застрять |
| HIGH | Главное меню 9 пунктов столбиком |
| MED | TZ не подписан на слотах |
| MED | Back navigation inconsistency (service detail → categories) |
| MED | Long skin/budget button labels без truncate |
| MED | Free-text вне FSM → только «кнопки меню» |
| MED | `/help` доступен без consent (UX leak) |
| LOW | Rating 5 stars в один ряд — скукоживаются |
| ✅ | `wrap_lines` / `split_message` / `truncate_button` — осознанная mobile-защита |

---

# 8. Telegram-специфические фичи

| Фича | Статус |
|------|--------|
| Inline keyboards | ✅ массово |
| Reply keyboards | ⚠️ contact + quick cmds, hide ломает UX |
| Force reply | ❌ |
| Photos | ✅ AI + skin + forward admin |
| Video/voice | ⚠️ AI thumb / skin; rate limit слаб |
| Docs/stickers/locations | ❌ |
| Payments Stars/YooKassa | ❌ **не реализованы** |
| Web Apps / Mini Apps | ⚠️ `webapp/index.html` — **демо-муляж** (fake balance 1475, no API, no initData HMAC, не wired в bot) |
| Reactions / topics / stories | ❌ |
| Deep linking | ❌ |

**Payments:** отсутствие — плюс (нет дыр в деньгах); минус — если webapp рисует «₽» как реальные.

---

# 9. Логирование, аналитика, мониторинг

| Capability | Status |
|------------|--------|
| Rotating file log | ✅ |
| Stdout | ✅ |
| ERROR → Telegram | ✅ (если ERROR_NOTIFY_ID) |
| Secret redaction | ⚠️ partial |
| Structured JSON logs | ❌ |
| Correlation / request id | ❌ |
| Prometheus / OTel / Sentry | ❌ |
| Product analytics | ❌ |
| Health (real) | ❌ process name only |
| Backup alerts | ❌ |
| PII retention policy | ❌ |
| Business funnel metrics | ❌ |

**PII в логах:** `telegram_id` повсеместно; мед.контент в admin messages; risk token в exceptions.

---

# 10. Заключение

## Общий вердикт

Бот **не «дырявый SQL-injection мусор»**. Есть зрелые куски: consent middleware, atomic bonuses, IDOR checks, WAL, admin keyboard flows, mobile text wrap.

Но для **косметологии + фото кожи + платный LLM + записи клиентов** критично провалены:

1. Антиабьюз (throttle dead + AI cost bomb)  
2. Достоверность записи (notify silent fail, success-before-commit, second booking vs UNIQUE)  
3. Целостность медданных (stale anam callbacks, admin-plan клиенту)  
4. 152-ФЗ erase + биометрия/трансграничка в политике  
5. Ops: backups path, observability, SSL, Redis, scale story  

**К продакшену «включили и забыли» — НЕ готов.**  
**К пилоту 1 кабинет с ручным контролем админа — условно да после P0.**

---

## Топ-10 критических рисков

| # | Риск | Impact |
|---|------|--------|
| 1 | Клиент «успешно записан», админ **не уведомлён** | Потерянные клиенты, репутация |
| 2 | UI second booking + UNIQUE → success/notify/rollback | Хаос в записи |
| 3 | Stale anamnesis → **corrupt medical questionnaire** | Вред здоровью / юр.риск |
| 4 | AI global DoS + vision spam | $$$ + downtime ИИ |
| 5 | Throttle middleware dead | Flood / SQLite lock |
| 6 | SSL verify false + proxy | Token/PII MITM |
| 7 | Фото/медданные → xAI без отдельного opt-in | 152-ФЗ / штрафы |
| 8 | Admin plan процедур отдаётся клиенту | «Назначение» без врача |
| 9 | Нет automated backups (и wrong path в `backup_db.py`) | Потеря всей базы |
| 10 | Broadcast HTML + single ADMIN_ID compromise | Phishing всей базе |

---

## P0 hotfix (сделать до «прода»)

| # | Fix | Effort |
|---|-----|--------|
| 1 | Чинить Throttling: user из `Update.event` + callback limits | 1–2ч |
| 2 | Notify fail → **не врать** клиенту; retry queue; status `notify_failed` | 2–4ч |
| 3 | Second booking: либо запретить UI, либо multi-active; success **после** commit | 2–4ч |
| 4 | Anamnesis session token + invalidate old keyboards | 2–3ч |
| 5 | Skin: `for_admin=False` клиенту; admin plan отдельно | 30м |
| 6 | Per-user AI + vision limits (Redis); semaphore max 3–5 | 4–8ч |
| 7 | Redact BOT_TOKEN; SSL verify true; Redis password | 1–2ч |
| 8 | html.escape **везде** (reminders, collision, broadcast policy) | 1–2ч |
| 9 | Fix backup path + cron `sqlite_maintenance` | 1–2ч |
| 10 | Fail-hard Redis in prod; wire FSM TTL | 1–2ч |

## P1 (неделя)

- Router-level AdminOnlyMiddleware  
- Consent version re-check  
- Full PD erase path  
- Prompt injection hardening + убрать «дерзкий» режим из prod  
- Deep links  
- Fix Да/Нет labels; remove «.»; restore cancel/back на шагах  
- Privacy policy: xAI, биометрия, трансграничка  
- `.dockerignore` + gitignore `*.db*`  
- Real healthcheck getMe + DB ping  

## P2 (масштаб / продукт)

- PostgreSQL + short-lived DB sessions  
- Webhook + workers  
- Datetime columns for bookings  
- Sentry + metrics + funnel analytics  
- Mini App: real API + HMAC initData **или удалить mock**  
- Payments если нужны (Stars/YooKassa) с full PCI-aware design  

---

## Что сделано хорошо (чтобы не врать)

1. Каркас 152-ФЗ consent + middleware  
2. IDOR-защита на клиентских booking actions  
3. Atomic bonus updates  
4. Unique partial index one active booking (идея верная; UI противоречит)  
5. Admin booking keyboards (accept/reject/done/contact)  
6. Contact spoof check  
7. html_escape в большинстве карточек  
8. WAL + busy_timeout + useful indexes  
9. Error notify + rotating logs  
10. Mobile wrap/split utilities  
11. Cancel deadline, lead time, reminders + followup design  
12. Deploy scripts для VPS  

---

## Чеклист ручного прогона (минимум)

- [ ] New user: decline / accept / name edges / phone RU+foreign  
- [ ] Full booking funnel + double-tap skip notes  
- [ ] Active → merge combine / **separate** (expect fix or fail honest)  
- [ ] Cancel >6h / <6h / rebook  
- [ ] Admin not started bot → booking notify  
- [ ] Stale anam buttons after restart questionnaire  
- [ ] Spam 30 callback/s  
- [ ] AI 15 photos + re-enter  
- [ ] Skin full path: client text ≠ admin clinical plan  
- [ ] PD revoke → DB residual PII  
- [ ] Restart mid FSM with/without Redis  
- [ ] Deploy: no .env in image; backup restores  

---

*Arena agents: security · flows/UX · admin/AI/Telegram · prod/perf*  
*Merge: Grok Build · 2026-07-18*
