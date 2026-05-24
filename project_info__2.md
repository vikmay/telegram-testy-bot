# Flow створення нового учня → до його схвалення (step-by-step)

## Todo (чекліст по Flow)
- [x] Визначити точки входу для онбордингу (`/start`, текст)
- [x] Відстежити створення/перший запис учня
- [x] Показати 2 кроки збору ПІБ
- [x] Описати перехід станів `pending_approval`
- [x] Знайти шлях approval від адміна (callback)
- [x] Пояснити, як approval реально відкриває доступ до тестів (guard в `_start_test`)
- [x] Додати важливі non-obvious деталі (tombstone/адмін акаунт)

---

## 1) Вхід користувача: `process_update()` → `_handle_start()`
**Коли:** студент натискає `/start`.

**Дії:**
1. `process_update()` бачить `message.text.startswith("/start")` → викликає `_handle_start(message)`.
2. `_handle_start()` викликає `_get_student(user_id, chat_id)`:
   - якщо запису в пам’яті/`students.json` ще нема — створює `StudentState(user_id, chat_id)` і одразу **переозберігає** в `students.json` через `_persist_students()`.
3. Далі бот починає онбординг:
   - скидає поля, які могли “застрягти” в admin workflows
   - якщо статус учня `new` або `awaiting_name`:
     - `student.awaiting_name = True`
     - `student.status = "awaiting_name"`
     - відправляє повідомлення: **“Введи прізвище.”**

**Проміжний статус:** `awaiting_name`.

---

## 2) Крок онбордингу №1: студент надсилає прізвище → зберігається
**Коли:** наступне `message` з текстом, і `student.awaiting_name == True`.

**Дії в `_handle_text()`:**
1. `_handle_text()` заходить у гілку `if student.awaiting_name:`
2. Якщо `student.last_name` порожній:
   - нормалізує текст
   - записує `student.last_name`
   - `student.awaiting_name` лишається `True`
   - зберігає `_persist_students()`
   - пише: **“Введи ім'я.”**

**Проміжний стан:** все ще `awaiting_name`, але вже заповнений `last_name`.

---

## 3) Крок онбордингу №2: студент надсилає ім’я → `pending_approval` (або `approved`)
**Коли:** знову текстове повідомлення, все ще `student.awaiting_name == True`, але `last_name` вже заповнений.

**Дії в `_handle_text()`:**
1. Записує:
   - `student.first_name`
   - `student.full_name = "{last_name} {first_name}"`
   - `student.awaiting_name = False`
2. Далі розгалуження:

### 3a) Якщо це адмін (`user_id ∈ admin_user_ids`)
- `student.status = "approved"`
- відправляє повідомлення про доступ адміністратора.

### 3b) Якщо це НЕ адмін
- `student.status = "pending_approval"`
- відправляє студенту: **“Запит прийнято. Адміністрація розгляне…”**
- запускає `_notify_admins(...)`:
  - адміну розсилається повідомлення зі inline-кнопками:
    - `✅ Схвалити` → callback `student:approve:{student.user_id}`
    - `🚫 Відхилити` → callback `student:block:{student.user_id}`
- зберігає `_persist_students()`.

**Проміжний статус:** `pending_approval` (для звичайного студента).

---

## 4) Approval адміном: `callback_query` → `_handle_callback()` → `student.status="approved"`
**Коли:** адмін натискає кнопку `✅ Схвалити`.

**Маршрут:**
- `process_update()` бачить `callback_query` → `_handle_callback(callback_query)`
- у `_handle_callback()` спрацьовує гілка:
  - `if data.startswith("student:approve:")`

**Дії:**
1. Перевіряє права: callback від адміна (`user["id"] in admin_user_ids`)
2. Дістає `target_student`
3. Встановлює:
   - `target_student.status = "approved"`
   - `_persist_students()`
4. Показує адмінам картку через `_show_student_details()`:
   - для `approved` замість “Схвалити” адмін бачить кнопку **“🚫 Заблокувати”**
5. Уведення side-effects:
   - якщо `target_student.chat_id` відомий → повідомляє самому студенту, що дані схвалено
   - `_notify_admins()` → повідомляє всім адмінам, що учня схвалено
6. `answerCallbackQuery()`.

**Фінальний статус approval:** `approved`.

---

## 5) Чому approval реально відкриває доступ: guard в `_start_test()`
Навіть якщо UI ще десь помиляється, бот не дає стартувати тест без approval.

**Важливий guard в `_start_test(student, topic_id)`**:
- якщо `student.status != "approved"`:
  - очищає поточний тест
  - `_persist_students()`
  - відправляє **“Доступ заборонено”**
  - `return`

Тому після встановлення `status="approved"` студент зможе проходити тести.

---

## Non-obvious деталі (які важливо знати)
- **Approval/доступ — це `status`-інваріант.** Єдине “джерело прав” для старту тесту — `student.status == "approved"` (а не наявність кнопок у UI).
- **Адмінський акаунт “сам себе” може стати `approved`:** якщо адміну пройти онбординг як студенту, код ставить `approved` одразу.
- **Є tombstone-механіка delete** (не прямо про approval), яка впливає на повторне “створення нового запису” при `/start`.