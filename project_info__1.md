# telegram-testy-bot — Codebase Overview + Flow “new student → approval”

## Summary

Це Telegram-бот на Python (`bot.py`), який дає студентам проходити topic-based тести з автоперевіркою (single/multi/matching) і накопичує результати в SQLite (`results.db`). Для доступу студент проходить онбординг через `/start` (прізвище → ім’я), а далі його статус “chekкає” адміністратор: без схвалення тести недоступні. Адмін керує учнями через inline-кнопки або команди (`/students`, `/approve <user_id>`) і може схвалити/заблокувати доступ.

Нижче — покроково саме Flow створення нового учня від початку заявки до його схвалення.

## Architecture

**Патерн/стиль:** “single-binary” / монолітний бот: майже вся логіка зібрана в `bot.py`, а статистика винесена в `services/stats_service.py`.  
**Технології:**

- Python 3.10+ (стандартна бібліотека)
- Telegram Bot API через прямі HTTP виклики (`urllib`)
- JSON-файли в `data/` для довготривалих станів/довідників (`students.json`, `topics.json`, `state.json`, тощо)
- SQLite в корені для історії/агрегацій (`results.db`) та для state діалогу (`sessions.db`)

**Як стартує виконання / runtime loop:**

1. `main()` → `ensure_default_files()` → `QuizBot()` → `bot.run()`
2. `QuizBot.run()` робить нескінченний polling:
    - `updates = api.get_updates(offset)`
    - `_check_and_send_reminders()` (опційно)
    - `for update in updates: process_update(update)`
3. `process_update()` маршрутизує:
    - `message` → `_handle_start`, `_handle_text`, `_handle_document`, або `_handle_admin_command`
    - `callback_query` → `_handle_callback`

## Directory Structure

```
project-root/
├── bot.py                  — вся логіка бота: онбординг, тести, адмінка, IPC через Telegram callbacks
├── services/
│   └── stats_service.py   — обчислення аналітики/ренкінгів для адмінського UI
├── data/
│   ├── questions.json     — банк питань
│   ├── students.json      — записи студентів + статуси + chat_id
│   ├── admins.json         — admin user_id
│   ├── topics.json         — теми (active/inactive, порядок)
│   ├── state.json          — глобальні налаштування (тривалість, reminders, tombstones)
│   ├── sessions.db (але фактично sessions.db — в корені)
│   └── ... (інше)
├── results.db             — SQLite: тестові результати + question stats
├── sessions.db            — SQLite: persisted student dialog/test state
├── README.md
└── token.txt              — Telegram token (як альтернатива env var)
```

## Key Abstractions

### `QuizBot`

- **File**: `bot.py` (клас починається на ~рядках 150+)
- **Responsibility**: головний оркестратор. Тримайте тут invariants flow-логіки: створення student-record, онбординг, старт тесту лише після approval, адмінка.
- **Ключові “вузли”**:
    - Онбординг: `_handle_start()`, `_handle_text()` (гілка `student.awaiting_name`)
    - Approval: `_handle_callback()` → `student:approve:{id}` або текстова команда `/approve`
    - Старт тесту: `_start_test()` (guard: `student.status == "approved"`)
- **Lifecycle**: створюється один раз у `main()`, живе весь час polling-loop.

### `StudentState` (датаклас)

- **File**: `bot.py`
- **Responsibility**: структура стану конкретного user’а (student/admin) в контексті бота.
- **Важливі поля для Flow approval**:
    - `status`: `"new" | "awaiting_name" | "pending_approval" | "approved" | "blocked" | "deleted"`
    - `awaiting_name`: чи очікуємо прізвище/ім’я
    - `first_name`, `last_name`, `full_name`
    - `chat_id`: щоб адмін міг писати, а reminders могли розсилати
- **Lifecycle**: завантажується з `students.json` + `sessions.db` через `_load_data()` і `_get_student()`.

### `JsonStore`

- **File**: `bot.py`
- **Responsibility**: простий JSON read/write з дефолтами.

### `SessionStore`

- **File**: `bot.py`
- **Responsibility**: SQLite persisted dialog/test state `student_sessions`. Використовується для переживання рестарту.
- **Для approval-flow:** тут зберігаються “awaiting\_\*” прапорці, але ключове онбординг-поведінка — теж прив’язана до `StudentState.status/awaiting_name`.

### `ResultsStore`

- **File**: `bot.py`
- **Responsibility**: SQLite `test_results`, а також `question_stats` і `canonical_question_stats`.
- **Для approval-flow:** approval не пише в results.db; guard старту тесту дивиться тільки на `student.status`.

### `BotApi`

- **File**: `bot.py`
- **Responsibility**: низькорівневі запити до Telegram (getUpdates/sendMessage/editMessageText/answerCallbackQuery).

### `StatsService`

- **File**: `services/stats_service.py`
- **Responsibility**: формування аналітики/ренкінгу для admin UI (картка учня, кнопкиランキング).
- **Для approval-flow:** не “затверджує” студентів; але адмінські списки/картки використовують `student.status`.

## Data Flow (Flow “new student → approval”)

### 0) Передумови

- Адмін задається через `data/admins.json` (які Telegram `user_id` дозволені).
- Студент має розпочати з чату та зробити `/start`.

### 1) Перший клік студента: `/start` → створення запису + перехід у `awaiting_name`

**Вхід:** `process_update()` отримує `message` з текстом `/start` → `_handle_start(message)`

**Дії:**

1. `_handle_start()` бере `user_id`, `chat_id` і викликає `_get_student(user_id, chat_id)`:
    - якщо запису ще нема — створює `StudentState(user_id, chat_id)` і одразу `_persist_students()` (тобто з’являється `students.json`).
2. `/start` також “обнуляє” admin-воркфлоу прапорці для `docx/import/topic action/delete action` (щоб не залишались “stale state”).
3. Якщо `student.status` в `{ "new", "awaiting_name" }`:
    - скидає `first_name/last_name/full_name`
    - встановлює:
        - `student.awaiting_name = True`
        - `student.status = "awaiting_name"`
    - пише студенту: **“Введи прізвище.”**

**Інваріант:** на цьому етапі бот гарантує, що наступне текстове повідомлення буде інтерпретовано як частина онбордингу, а не як тест-відповідь.

### 2) Крок 1 онбордингу: студент надсилає прізвище

**Вхід:** `process_update()` → `_handle_text(message)`  
Умова: `if student.awaiting_name: ...`

**Дії:**

- якщо `student.last_name` ще порожній:
    - бот нормалізує текст (залишає слова, робить `capitalize()`)
    - зберігає в `student.last_name`
    - робить `_persist_students()`
    - просить: **“Введи ім'я.”**

**Стан після кроку:** `status` лишається `awaiting_name`, але `last_name` вже заповнений.

### 3) Крок 2 онбордингу: студент надсилає ім’я → заявка на approval

**Вхід:** знову `_handle_text()` у гілці `student.awaiting_name`

**Дії:**

- якщо `student.last_name` вже заповнений:
    1. зберігає `student.first_name`
    2. формує `student.full_name = "{last_name} {first_name}"`
    3. вимикає `student.awaiting_name = False`
    4. далі розвилка:

#### 3a) Якщо користувач є адміном (`user_id ∈ admin_user_ids`)

- `student.status = "approved"`
- бот пише:
    - **“Дані отримано. Ти маєш адмін-доступ.”** (або схоже повідомлення)

> Окремий “прихований” механізм: навіть після рестарту `_load_data()` може перевести admin-акаунти в `approved`, якщо їх статус був `"new/awaiting_name/pending_approval"`.

#### 3b) Інакше (звичайний студент)

- `student.status = "pending_approval"`
- бот пише:
    - **“Запит прийнято. Адміністрація розгляне…”**
- запускає `_notify_admins()` з повідомленням про нову заявку і inline-кнопками:
    - `✅ Схвалити` → callback `student:approve:{user_id}`
    - `🚫 Відхилити` → callback `student:block:{user_id}`
- робить `_persist_students()`.

**Стан після кроку:** `pending_approval`, доступ до тестів ще закритий.

### 4) Розгляд заявки адміном: approval callback → `approved`

**Вхід:** адмін натискає кнопку → `process_update()` отримує `callback_query` → `_handle_callback(callback_query)`

**Гілка:** `if data.startswith("student:approve:")`:

**Дії:**

1. Перевірка прав:
    - `if user["id"] not in self.admin_user_ids: ... return`
2. `target_student = self.students.get(target_id)`
3. Встановлення:
    - `target_student.status = "approved"`
    - `_persist_students()`
4. Бот показує адміну картку учня через `_show_student_details()`:
    - якщо статус `approved`, адмін бачить кнопку **“🚫 Заблокувати”** (ікони/кнопки відрізняються)
5. Далі розсилка:
    - повідомлення самому студенту (якщо `target_student.chat_id` відомий):
        - **“Твої дані схвалено.Доступ відкрито.”**
    - `_notify_admins()` ще раз повідомляє всім адмінам:
        - **“✅ Учня схвалено: … (ID …)”**
6. `answerCallbackQuery()`.

**Стан після approval:** `status = "approved"`.

### 5) Поведінка після approval: студент може запускати тести

**Важливо:** доступ контролюється не лише UI, а жорстко в `_start_test()`:

- `_start_test()` містить guard:
    - якщо `student.status != "approved"`:
        - скидає поточний тест
        - `_persist_students()`
        - пише студенту **“Доступ заборонено”**
        - return

Тому адмінське схвалення робить студента реально доступним для тестів.

## Non-Obvious Behaviors & Design Decisions

### 1) Статуси онбордингу чітко “прив’язані” до текстових повідомлень

Як тільки `student.awaiting_name=True`, будь-який `text` від користувача інтерпретується як частина онбордингу (прізвище/ім’я), а не як команда/відповідь на тест.

### 2) Адмінські акаунти автоматично можуть отримати доступ

- На етапі введення імені: якщо `user_id` належить `admin_user_ids` → `approved`.
- Після рестарту: `_load_data()` також може принудово перевести адміна в `approved`, якщо він у “ранніх” статусах.

Це важливо, щоб адмін міг робити DOCX import і керування тестами навіть без окремого “approval-кліку”.

### 3) “deleted” не видаляється з історії доступу повністю

Є tombstone-механіка:

- `deleted_student_keys` зберігається в `state.json`
- `_get_student()` може повертати `StudentState(status="deleted")` для не-адмінів, щоби не відновити їх після видалення.
- Для адмінів tombstone чиститься (вони можуть лишатися повністю “робочими” для імпорту/редагування).

Це не стосується approval напряму, але впливає на те, як повторно з’являється “новий” запис при /start.

### 4) У коді є дубль кнопки “Схвалити” в картці

У `_show_student_details()` для не-approved гілки додається `keyboard["inline_keyboard"].append(... "student:approve:...")` двічі. Це не ламає approval-flow, але є UX/cleanliness issue.

## Module Reference

| File                        | Purpose                                                                                                             |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `bot.py`                    | Головна реалізація бота: онбординг (/start + два повідомлення), збереження стану, тести, адмін-меню, approval/block |
| `services/stats_service.py` | Аналітика студента та побудова адмінських кнопок рейтингу/картки                                                    |

## Suggested Reading Order

1. `bot.py` — класи `StudentState`, `JsonStore`, `SessionStore`, `ResultsStore` (щоб розуміти state/зберігання)
2. `bot.py` — `_handle_start()` (початок онбордингу)
3. `bot.py` — `_handle_text()` гілка `if student.awaiting_name:` (де з’являється `pending_approval` і викликається `_notify_admins`)
4. `bot.py` — `_handle_callback()` гілка `student:approve:` (де status стає `approved`)
5. `bot.py` — `_start_test()` guard `student.status != "approved"` (де реально закривається доступ до тестів)
6. `services/stats_service.py` — якщо цікавить адмінський UI після approval

## TODO checklist (для перевірки/документування вашого Flow)

- [x] Знайти точки входу: `/start`, обробка тексту, callback
- [x] Відстежити переходи статусів: `new → awaiting_name → pending_approval → approved`
- [x] Описати повідомлення/запити, які відправляються адміну
- [x] Описати approval механізм: inline callback і side-effects (notify + картка)
- [x] Показати guard, який блокує старт тесту до approval
- [ ] (Опціонально) Перевірити UI/UX: дубль “Схвалити” та повідомлення з кирилицею/кодуванням (якщо потрібно уточнення)
