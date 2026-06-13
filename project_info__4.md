# Bug Analysis: "Лівий стовпець" appearing as an answer option in matching questions

## Summary

The phrase **"Лівий стовпець:"** (a column heading from the DOCX source) is being stored as a real option entry in `questions.json` instead of being filtered out. Because the matching system treats it as the **first left-column item** (index 0), the heading appears as a clickable button that students can select as an answer choice — which is incorrect.

The root cause is **in the DOCX import parsing logic** (`_parse_docx_questions` in `bot.py`), which does not recognize "Лівий стовпець:" (or "Права колонка:", "Ліва колонка:") as a heading/section label. The imported DOCX has these headings as separate paragraphs, and the parser collects them as regular option lines.

## Affected Data

All **10 matching questions** in the `"відповідність"` topic (`data/questions.json`) have this exact problem:

| Question ID | options[0] | Actual left column options |
|---|---|---|
| `84862df6-...` | `"Лівий стовпець:"` | "Об'єкт A", "Об'єкт Б", "Об'єкт В", "Об'єкт Г" |
| `a3a42dfd-...` | `"Лівий стовпець:"` | "Об'єкт A", "Об'єкт Б", "Об'єкт В", "Об'єкт Г" |
| `36000204-...` | `"Лівий стовпець:"` | "Об'єкт A", "Об'єкт Б", "Об'єкт В", "Об'єкт Г" |
| `6be718a6-...` | `"Лівий стовпець:"` | "Об'єкт A", "Об'єкт Б", "Об'єкт В", "Об'єкт Г" |
| `ae361bc8-...` | `"Лівий стовпець:"` | "Об'єкт A", "Об'єкт Б", "Об'єкт В", "Об'єкт Г" |
| `0443632c-...` | `"Лівий стовпець:"` | "Об'єкт A", "Об'єкт Б", "Об'єкт В", "Об'єкт Г" |
| `0a4e81ca-...` | `"Лівий стовпець:"` | "Об'єкт A", "Об'єкт Б", "Об'єкт В", "Об'єкт Г" |
| `d382b772-...` | `"Лівий стовпець:"` | "Об'єкт A", "Об'єкт Б", "Об'єкт В", "Об'єкт Г" |
| `cf834d86-...` | `"Лівий стовпець:"` | "Об'єкт A", "Об'єкт Б", "Об'єкт В", "Об'єкт Г" |

The `answer` array for all of them is `[0, 1, 2, 3]`, meaning the correct left-column items are indices 0, 1, 2, 3. But index 0 is `"Лівий стовпець:"` — a heading, not an actual option.

**What should happen**: the correct left-column items should be:
- "Об'єкт A" (index 0 in the real left column)
- "Об'єкт Б" (index 1)
- "Об'єкт В" (index 2)
- "Об'єкт Г" (index 3)

## Root Cause: Three Problems

### Problem 1: DOCX parser does not filter column headings

In `_parse_docx_questions()` inside `bot.py`, the DOCX text lines include the heading "Лівий стовпець:" as a separate paragraph. The parser's `o_pat` regex:

```python
o_pat = re.compile(r"^(?:[-*•]|\(?\d+\)?|[A-Za-zА-Яа-я]\s*[\).:]|\d+\s*[\).:]|\d+\s*[\-:])\s*(.*)$")
```

This regex checks if a line **starts with common option markers** (bullet, number, letter + dot/paren). The line `"Лівий стовпець:"` accidentally matches one of the alternatives. Specifically:

- The regex has alternative `[A-Za-zА-Яа-я]\s*[\).:]` which matches any Cyrillic/Latin letter followed by optional whitespace then `)`, `.`, or `:`.
- For `"Лівий стовпець:"`, the letter `й` (at position 4 in the string) is followed by a space and then `:`, so the regex matches **"й стовпець:"** with the prefix being `й ` and the captured option text being `"івий стовпець"` (the first letter "Л" gets dropped).

### Problem 2: Matching split uses `half = len(options)//2`

Once options are collected, the matching render code splits them by pure position:

```python
half = len(question.options) // 2
left_options = question.options[:half]
right_options = question.options[half:]
```

For the affected questions:
- Total options = 9 (1 heading + 4 left + 4 right)
- `half = 9 // 2 = 4`
- left = options[0:4] = `["Лівий стовпець:", "Об'єкт A", "Об'єкт Б", "Об'єкт В"]`
- right = options[4:9] = `["Об'єкт Г", "Твердження №1...", "Твердження №2...", "Твердження №3...", "Твердження №4..."]`

So:
- The left column has 4 items, but the first is the heading, and the **fourth actual left item** ("Об'єкт Г") is pushed to the right side
- The right column has 5 items, starting with "Об'єкт Г" (which belongs on the left), followed by the 4 real right items

This is completely misaligned — neither column has the correct content.

### Problem 3: `answer: [0, 1, 2, 3]` is incorrect for the current data

With the current data, `answer: [0, 1, 2, 3]` means:
- left index 0 ("Лівий стовпець:") → right index 0 ("Об'єкт Г") — both are wrong
- left index 1 ("Об'єкт A") → right index 1 ("Твердження №1...") — accidentally correct pairing
- left index 2 ("Об'єкт Б") → right index 2 ("Твердження №2...") — accidentally correct pairing
- left index 3 ("Об'єкт В") → right index 3 ("Твердження №3...") — accidentally correct pairing

So 3 out of 4 pairings happen to be "correct by accident" because the split offset shifted items equally.

But for other questions where right texts are different lengths (like `ae361bc8-...` with `"Опис"`, `"Формула чи"`, `"Приклад застосування -"`, `"Геометрична"`), the accidental correctness may not hold.

## Why the bug manifests for students

When a student sees this matching question in Telegram:
1. The bot renders left buttons numbered 1-4 with texts: "Лівий стовпець:", "Об'єкт A", "Об'єкт Б", "Об'єкт В"
2. The right buttons are labeled a-e with texts: "Об'єкт Г", "Твердження №1...", "Твердження №2...", "Твердження №3...", "Твердження №4..."
3. The student sees "Лівий стовпець:" as item #1 that they're supposed to match — confusing
4. The correct answer according to imported data pairs "Лівий стовпець:" with "Об'єкт Г" — semantically meaningless
5. But because of the offset, items 2-4 accidentally map correctly, masking the bug in grading

## How to fix

### Short-term fix (data.jsons repair)

Edit `data/questions.json` for all 10 matching questions:
1. **Remove** the element `"Лівий стовпець:"` from the `options` array
2. Keep `answer: [0, 1, 2, 3]` — after removing the heading, index 0 = "Об'єкт A", index 1 = "Об'єкт Б", etc.
3. The right-side texts at indices 4-7 (after removal) will be: "Твердження №1...", "Твердження №2...", "Твердження №3...", "Твердження №4..."

The corrected `options` array should have **8 items** (not 9):
```json
"options": [
    "Об'єкт A",
    "Об'єкт Б",
    "Об'єкт В",
    "Об'єкт Г",
    "Твердження №1 - розгорнуте пояснення...",
    "Твердження №2 - розгорнуте пояснення...",
    "Твердження №3 - розгорнуте пояснення...",
    "Твердження №4 - розгорнуте пояснення..."
]
```

### Long-term fix (code fix in bot.py)

In `_parse_docx_questions()` add a filter to skip column heading lines before processing them as options, and also consider parsing column boundaries from the markers in the DOCX rather than splitting by position.

## What needs to change

| File | Change |
|---|---|
| `data/questions.json` | Remove `"Лівий стовпець:"` from all 10 matching questions; verify `options` have 8 elements each |
| `bot.py` → `_parse_docx_questions()` | Add heading filtering to prevent re-import from producing the same bug |

## All 10 affected question IDs

The specific UUIDs that need correction:
- `84862df6-01b9-404c-972f-5b418ff909f7`
- `a3a42dfd-1d76-4f29-b747-5b1bd5320018`
- `36000204-d549-4a00-9b7a-a9b7bd72636b`
- `6be718a6-17f8-4ffb-ab9d-0f51fd8fecb0`
- `ae361bc8-482c-4532-96db-e797da03f186`
- `0443632c-cab2-4030-a3c6-a15823d88ce5`
- `0a4e81ca-3174-4fcb-bd53-840b0a9de549`
- `d382b772-06fc-422a-ad25-3374e15115a3`
- `cf834d86-6490-4cfa-af0a-c4d40cd93da5`