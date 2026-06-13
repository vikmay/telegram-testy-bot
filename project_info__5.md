## Чому на кнопках не повна відповідь? — Аналіз логіки `_build_keyboard` та `compact_mode` для matching

Так, ви маєте рацію: коли `compact_mode = True`, текст на кнопках **обрізається до номера/букви** замість повного тексту. Але є **два різні механізми**, які впливають на видимий текст, і варто розрізняти:

---

### 1. Як `_build_keyboard` формує текст на кнопках (рядки ~1250-1310)

Функція `_build_keyboard` приймає параметр `compact_mode: bool = False`. Всередині неї є блок для `question_type == "matching"`:

```python
if compact_mode:
    left_label = f"{left_mark}{index + 1}".strip()
else:
    left_label = f"{left_mark}{index + 1}. {trim_label(left_options[index], 22)}"
```

Аналогічно для правих кнопок:

```python
if compact_mode:
    right_label = f"{right_mark}{chr(ord('a') + index)}".strip()
else:
    right_label = f"{right_mark}{chr(ord('a') + index)}. {trim_label(right_options[index], 22)}"
```

**Тобто:**
- **Без compact_mode** (`compact_mode = False`): кнопка показує `"1. Об'єкт А"` (номер + текст, обрізаний до 22 символів)
- **З compact_mode** (`compact_mode = True`): кнопка показує лише `"1"` (тільки номер, без тексту)

---

### 2. Хто вирішує, чи вмикати compact_mode

Функція `_compact_mode_for_question` (рядки ~1315-1325):

```python
def _compact_mode_for_question(self, question: Question) -> bool:
    if question.type == "text":
        return False
    threshold = 32
    try:
        return any(len(str(opt)) > threshold for opt in (question.options or []) if opt is not None)
    except Exception:
        return False
```

**Логіка:** `compact_mode` вмикається, **якщо хоча б один варіант відповіді довший за 32 символи**. 

Для ваших matching-питань з `data/questions.json`, де:
- `"Лівий стовпець:"` (15 символів, не перевищує 32)
- `"Об'єкт A"` (8 символів)
- `"Твердження №1 - розгорнуте пояснення з математичного аналізу..."` (значно > 32!)

**Будь-яке питання, що має хоча б один довгий варіант (>32 символи), активує `compact_mode = True` для всього matching-питання.**

---

### 3. Як `compact_mode` викликається для matching

У `_send_current_question` (рядок ~1475):

```python
compact_mode = self._compact_mode_for_question(question)

if question.type == "matching":
    # ...
    sent_message = self.api.send_message(
        student.chat_id,
        text,
        reply_markup=self._build_keyboard(
            shuffled_left_options + shuffled_right_options,
            question_type="matching",
            matching_pairs={},
            matching_selected_left=None,
            compact_mode=compact_mode,   # <-- ось як compact_mode передається
        ),
    )
```

Аналогічно в `_handle_callback` при кожному оновленні кнопок (reset, left click, right click) — скрізь передається `compact_mode=compact_mode`.

---

### 4. Чому це проблема саме для matching?

Для `single` та `multi` питань, коли `compact_mode=True`, теж показується тільки номер кнопки (`"1"`, `"2"`, ...). Але для цих типів є **компенсація** — повний текст варіантів виводиться в текстовому повідомленні:

```python
if compact_mode:
    compact_pattern = r'^[\s\.\:\-•\u2013\u2014]+\s*'
    opts_lines = "\n".join(
        f"{i + 1}) {re.sub(compact_pattern, '', str(opt)).strip()}"
        for i, opt in enumerate(shuffled_options)
    )
    text += f"\n\nВаріанти:\n{opts_lines}"
```

Для matching ця компенсація **теж є**, але вона додається в текст повідомлення через `_render_compact_options_text`:

```python
if compact_mode:
    text += f"\n\n{self._render_compact_options_text(question, matching_left_map=..., matching_right_map=...)}"
```

Функція `_render_compact_options_text` (рядки ~1330-1365) генерує:

```
Ліва колонка:
1) Об'єкт А
2) Об'єкт Б
3) Об'єкт В
4) Об'єкт Г

Права колонка:
a) Твердження №1 - розгорнуте пояснення...
b) Твердження №2 - розгорнуте пояснення...
```

**Але!** Цей текст з'являється **тільки** коли compact_mode активовано І код спрацьовує на:
- `_send_current_question` (початкове відправлення)
- `_handle_callback` при `reset`, `left`, `right` (коли compact_mode = True)

---

### 5. Конкретна відповідь: чому на кнопках не повний текст

**Причина:** Умова `compact_mode` активується через `_compact_mode_for_question()`, яка перевіряє, чи **будь-який** варіант довший за 32 символи. Якщо так — вона вмикає `compact_mode = True` для **всього** matching-питання.

**Чи мали б питання розпарситись у режимі compact?** — Так, **якщо вони пройшли перевірку `any(len(opt) > 32)`**. Це означає, що питання з довгими варіантами (наприклад, "Твердження №1 - розгорнуте пояснення з математичного аналізу...") ВЖЕ працюють у compact-режимі, де кнопки показують лише номери 1, 2, 3, ..., а повний текст — у текстовому блоці над кнопками.

**Якщо питання має лише короткі варіанти (`"Об'єкт А"`, `"Твердження №1"`, `"12 кутів"`), то `compact_mode = False`** і кнопки показують `"1. Об'єкт А"`, `"a. Твердження №1"` — повний текст (обрізаний до 22 символів).

---

### 6. Чи є баг?

**Можливий баг:** якщо у питанні є варіанти, які довші за 32 символи, але **студенту потрібно бачити текст на кнопках**, то `compact_mode` це запобігає. 

Однак **це дизайн-рішення**, а не помилка: compact-mode був запроваджений, щоб кнопки не займали забагато місця на мобільному екрані. Без compact-mode на кожній кнопці було б 22 символи тексту, що дає дуже високі кнопки на телефоні.

**Як перевірити, чи active compact_mode для конкретного питання:** подивіться в `data/questions.json` на поле `options`. Якщо будь-який `option` має довжину >32 символів, то для цього питання увімкнеться compact-mode.

---

### Підсумок

| Питання | Варіанти | compact_mode | Кнопки показують |
|---------|----------|:------------:|------------------|
| Короткі (<32 симв) | "Об'єкт А", "Твердження №1" | ❌ Ні | `"1. Об'єкт А"` |
| Довгі (>32 симв) | "Твердження №1 - розгорнуте пояснення..." | ✅ Так | `"1"` (текст у повідомленні) |

Якщо ви хочете, щоб **повний текст завжди був на кнопках**, треба або:
- Збільшити `threshold` у `_compact_mode_for_question` (наприклад, до 100)
- Або прибрати умову, яка перевіряє довжину, і завжди повертати `False`