# Bug Analysis: "Лівий стовпець" appearing as an answer option in matching questions

## Summary

The phrase **"Лівий стовпець:"** (a column heading from the DOCX source) is being stored as a real option entry in `questions.json` instead of being filtered out. Because the matching system treats it as the **first left-column item** (index 0), the heading appears as a clickable button that students can select as an answer choice — which is incorrect.

The root cause is **in the DOCX import parsing logic** (`_parse_docx_questions` in `bot.py`), which does not recognize "Лівий стовпець:" (or "Права колонка:", "Ліва колонка:") as a heading/section label. The imported DOCX has these headings as separate paragraphs, and the parser collects them as regular option lines.

## Affected Data

All **10 matching questions** in the `"відповідність"` topic (`data/questions.json`) have this exact problem:

| Question ID    | options[0]          | Actual left column options                     |
| -------------- | ------------------- | ---------------------------------------------- |
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

And the correct `answer` should be `[0, 1, 2, 3]` mapping to the **actual** left options (Об'єкт А→Твердження №1, etc.).

## Root Cause: Three Problems

### Problem 1: DOCX parser does not filter column headings

In `_parse_docx_questions()` inside `bot.py`, the DOCX text lines include the heading "Лівий стовпець:" as a separate paragraph. The parser's `o_pat` regex:

```python
o_pat = re.compile(r"^(?:[-*•]|\(?\d+\)?|[A-Za-zА-Яа-я]\s*[\).:]|\d+\s*[\).:]|\d+\s*[\-:])\s*(.*)$")
```

This regex checks if a line **starts with common option markers** (bullet, number, letter + dot/paren). The line `"Лівий стовпець:"` starts with a Cyrillic letter "Л" followed by a space, which **does match** the pattern `[A-Za-zА-Яа-я]\s*[\).:]` because:

- `[A-Za-zА-Яа-я]` matches `Л`
- `\s*` matches the space
- `[\).:]` matches `:`

So the parser treats `"Лівий стовпець:"` as an option, capturing just `""` (empty string) as the option text after stripping the prefix.

But wait — let me re-check: the regex captures `(.*)` after the prefix. For `"Лівий стовпець:"`, the match would be:

- `[A-Za-zА-Яа-я]` → `Л`
- `\s*` → space
- `[\).:]` → `:`
- `(.*)` → `"івий стовпець"`

Actually no, let me check more carefully. The prefix `[A-Za-zА-Яа-я]\s*[\).:]` would match:

- `Л` (Cyrillic letter)
- `\s*` would match empty or space
- `[\).:]` would match `:` after the space

But the first space after "Лівий" is after the full word, not after just "Л". Let me re-examine:

`"Лівий стовпець:"` — the regex `[A-Za-zА-Яа-я]\s*[\).:]` would match `й \s* :` actually... no. Let me trace through character by character:

- First attempt: `[A-Za-zА-Яа-я]` matches `Л`, then `\s*` matches empty, then `[\).:]` expects `)` or `.` or `:`. The next char is `і` (Cyrillic), which does NOT match `[\).:]`. So this attempt fails.
- The regex engine backtracks and tries starting from the next characters...
- Eventually it matches: `й` (Cyrillic) at position 4, then `\s*` matches the space, then `[\).:]` matches `:`, and `(.*)` captures `"івий стовпець"`.

So the parser extracts `"івий стовпець"` as an option text (with the first letter missing!). That's even worse.

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

### Problem 3: `answer: [0, 1, 2, 3]` is incorrect

With the current data, `answer: [0, 1, 2, 3]` means:

- left index 0 ("Лівий стовпець:") → right index 0 ("Об'єкт Г") — both are wrong
- left index 1 ("Об'єкт A") → right index 1 ("Твердження №1...") — accidentally correct pairing
- left index 2 ("Об'єкт Б") → right index 2 ("Твердження №2...") — accidentally correct pairing
- left index 3 ("Об'єкт В") → right index 3 ("Твердження №3...") — accidentally correct pairing

So 3 out of 4 pairings happen to be "correct by accident" because the split offset shifted items equally.

But for other questions where right texts are different lengths (like `ae361bc8-...` with `"Опис"`, `"Формула чи"`, `"Приклад застосування -"`, `"Геометрична"`), the accidental correctness may not hold.

## Why the bug manifests

When a student sees this matching question in Telegram:

1. The bot renders left buttons numbered 1-4 with texts: "Лівий стовпець:", "Об'єкт A", "Об'єкт Б", "Об'єкт В"
2. The right buttons are labeled a-e with the remaining option texts
3. The student sees "Лівий стовпець:" as an option they're supposed to match — confusing
4. The correct answer according to the imported data pairs "Лівий стовпець:" with "Об'єкт Г", which is semantically meaningless
5. Some pairs accidentally match correctly (due to the offset), masking the problem in grading

## How to fix

**Short-term fix (data repair)** — edit `data/questions.json` to:

1. Remove the heading `"Лівий стовпець:"` from each matching question's `options` array
2. Adjust the `answer` array to be `[0, 1, 2, 3]` mapping from the corrected left options (now indices 0-3 = Об'єкт А, Б, В, Г) to the correct right options

**Long-term fix (code fix in `_parse_docx_questions`)** — add filtering logic to skip lines that match known column heading patterns:

```python
# Skip column headings in matching questions
if re.match(r"^(Лівий стовпець|Права колонка|Ліва колонка)[\s:]*", line, re.IGNORECASE):
    continue
```

Or better: detect column boundaries explicitly by recognizing "Лівий стовпець:" / "Права колонка:" / "Ліва колонка:" markers and splitting the options list based on these markers rather than by position.

## What needs to change

| File                                       | Change                                                                                    |
| ------------------------------------------ | ----------------------------------------------------------------------------------------- |
| `data/questions.json`                      | Remove `"Лівий стовпець:"` entries from all 10 matching questions; verify `answer` arrays |
| `bot.py` → `_parse_docx_questions()`       | Add heading filtering to prevent re-import from producing the same bug                    |
| `bot.py` → matching split logic (optional) | Consider using column markers for splitting instead of positional `half`                  |

## Affected questions in the database

All 10 questions in topic `"відповідність"` (IDs listed above). These need their `options` and `answer` fields corrected.
