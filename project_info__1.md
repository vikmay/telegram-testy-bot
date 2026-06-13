# Matching questions (DOCX import → parsing → Telegram render) — Codebase Overview

## Summary

This repository is a single-file Telegram bot (`bot.py`) that runs a math quiz with multiple question types, including `matching` (pair matching). For admins, `matching` questions are imported from `.docx` files: the bot extracts Word XML text, parses question blocks into the internal `Question` model, and stores them in `data/questions.json`. For students, `matching` questions are rendered as an inline keyboard with a two-column selection flow (pick left number → pick right letter), then submitted and graded against the imported/parsed `question.answer`.

## Architecture

**Primary pattern:** “single-process Telegram bot controller” with all orchestration living in `QuizBot` inside `bot.py`, plus a stats helper in `services/stats_service.py`.

**Major subsystems:**

- **Persistence layer**
    - JSON files in `data/` (`questions.json`, `students.json`, `topics.json`, `state.json`)
    - SQLite DBs in project root (`results.db`, `sessions.db`)
- **Bot runtime / orchestration**
    - `QuizBot.run()` long-poll loop: `getUpdates()` → `process_update()`
    - Message routing by update type:
        - `message` (text or docx)
        - `callback_query` (inline keyboard button presses)
- **DOCX import pipeline (admin-only)**
    - Extract Word XML text + inline images: `_extract_docx_text_with_images()`
    - Parse document into question blocks: `_parse_docx_questions()`
- **Matching question runtime (student)**
    - Render matching UI and shuffle columns: `_send_current_question()` + `_build_keyboard()`
    - Parse “student free-text answer” (fallback): `_parse_matching_answer()`
    - Grade pairs: `_grade_matching_question()`

**Execution start:**

- `main()` → `ensure_default_files()` → `QuizBot()` → `QuizBot.run()`

## Directory Structure

```
project-root/
├── bot.py                  — Entire bot logic (admin/student flows, DOCX parsing, matching render + grading)
├── services/
│   └── stats_service.py    — Analytics + admin UI helpers
├── data/
│   ├── questions.json     — Question bank (includes `matching` questions)
│   ├── topics.json         — Active topic list
│   ├── students.json       — Student profile + approval status
│   ├── state.json          — Bot global settings (duration + reminder config)
│   └── ...docx_images/... — Extracted images cached during DOCX import
└── results.db / sessions.db — Test history + per-user session state
```

## Key Abstractions

### `Question` (dataclass)

- **File**: `bot.py`
- **Responsibility:** Canonical in-memory representation of one quiz question.
- **Interface (fields):**
    - `type`: `"single" | "multi" | "matching" | "text"`
    - `question`: prompt text
    - `options`: list of option strings
    - `answer`: list of integers
        - for `matching`, `answer` is interpreted as a list where `answer[left_index] = right_index`
    - `explanation`: explanation string
    - `image`: optional dict with `{path, position, caption}`
- **Lifecycle:** Created during DOCX import parsing; loaded from `data/questions.json`; used during student test generation and grading.

### `StudentState` (dataclass)

- **File**: `bot.py`
- **Responsibility:** Per-user runtime state persisted in `sessions.db`.
- **Matching-specific fields:**
    - `matching_pairs: Dict[int,int]` — maps chosen left index → chosen right index (both 0-based)
    - `matching_selected_left: Optional[int]` — currently selected left index awaiting right selection
    - `shuffled_matching_left: List[int]` — left-column shuffle permutation indices
    - `shuffled_matching_right: List[int]` — right-column shuffle permutation indices
- **Lifecycle:** Initialized/reset when a matching question is shown and cleared after grading.

### `ResultsStore` (SQLite wrapper)

- **File**: `bot.py`
- **Responsibility:** Store test result history and per-question difficulty stats.
- **Matching interaction:**
    - `record_question_attempt(...canonical_key=...)` is called from `_grade_matching_question()`.
    - Canonical key for matching includes normalized `options` and the `answer` list (without sorting for matching).

### `QuizBot` (main controller)

- **File**: `bot.py`
- **Responsibility:** Entire bot orchestration: parsing DOCX, rendering questions, handling callbacks, grading, persistence.
- **Key matching methods:**
    - DOCX parsing: `_extract_docx_text_with_images()`, `_parse_docx_questions()`
    - Render: `_send_current_question()` and `_build_keyboard()` and `_render_compact_options_text()`
    - Parsing free-text answers: `_parse_matching_answer()`
    - Grading: `_grade_matching_question()`

### DOCX parsing helpers inside `_parse_docx_questions()`

- **File**: `bot.py` (nested functions within `_parse_docx_questions`)
- **Responsibility:** Convert extracted DOCX plain text lines into `Question` blocks.
- **Matching answer parser:** `parse_matching_answer(answer_text: str) -> List[int]`
    - Intended mapping: left-side index (0-based) → right-side index (0-based)
    - Current implementation assumes left side is **numeric** (e.g. `1-2`), not letters (e.g. `А-2`).

## Data Flow (Concrete `matching` path)

### 1) Admin imports DOCX → internal `Question` objects

1. Admin chooses topic via inline menu (`student.awaiting_docx_import` + `awaiting_docx_topic_id`).
2. Admin sends a DOCX file → `_handle_document()`
3. Bot downloads the DOCX and calls:
    - `_extract_docx_text_with_images(path)`
        - Reads `word/document.xml` from the DOCX zip.
        - Builds `extracted_text` line-by-line from `w:p` paragraphs (`w:t` runs).
        - Inserts image markers like `[[IMG<n>]]` (not directly involved in matching logic unless image is present).
    - `_parse_docx_questions(extracted_text, topic_id=...)`
        - Normalizes lines (removes soft hyphen, normalizes dash characters, collapses whitespace).
        - Detects question start using `q_pat`:
            - `Завдання|Питання|№ ...`
        - Detects question type line using `x_pat`:
            - `Тип: matching` → sets `current["type"]="matching"`
        - Parses options with `o_pat`:
            - Recognizes bullet/letter/number prefixes and captures the rest as option text.
            - **When `current.type == "matching"`**, it further strips a leading left/right marker prefix from option lines:
                - removes patterns like `A)`, `A.` and also numeric prefixes like `1)`.
        - Parses answer line with `a_pat`:
            - If type is matching → calls `parse_matching_answer(answer_text)`.

4. After collecting `question`, `options`, and `answer`, `_parse_docx_questions()` calls `flush()`:
    - Creates `Question(id=uuid, topic_id=..., type=..., question=..., options=..., answer=..., explanation=...)`
5. Saved into `data/questions.json` by `_save_imported_questions()`.

### 2) Students render matching question in Telegram

1. When test starts or advances, `_send_current_question(student)` runs.
2. For `question.type == "matching"`:
    - Split by position, not by explicit left/right labels:
        - `half = len(question.options)//2`
        - `left_options = question.options[:half]`
        - `right_options = question.options[half:]`
    - Shuffle each side independently:
        - `student.shuffled_matching_left = random.sample(range(len(left_options)), len(left_options))`
        - `student.shuffled_matching_right = random.sample(range(len(right_options)), len(right_options))`
    - Render UI:
        - Human-readable instruction text:
            - “Натискай спочатку лівий номер, потім праву букву.”
        - Inline keyboard built by `_build_keyboard(..., question_type="matching")`:
            - For each row index:
                - left button callback: `answer:left:{index}` where index is 0-based in the **current left array**
                - right button callback: `answer:right:{index}` where index is 0-based in the **current right array**
            - Bottom buttons:
                - `answer:submit` (“Підтвердити вибір”)
                - `answer:reset` (“Скинути”)

3. Student state is reset before rendering:
    - `student.matching_pairs = {}`
    - `student.matching_selected_left = None`

### 3) Student selects pairs → callback state machine

1. Student clicks a **left** button (`answer:left:{index}`):
    - `_handle_callback()` sets:
        - `student.matching_selected_left = index` (0-based)
2. Student clicks a **right** button (`answer:right:{index}`):
    - `_handle_callback()` verifies:
        - a left is selected
        - the chosen right index isn’t already used by another left selection
    - Then stores:
        - `student.matching_pairs[left_index] = right_index`
    - Also clears `matching_selected_left = None`.
3. Keyboard is re-rendered with “✅” markers:
    - left “✅” if already matched
    - right “✅” if already used (taken from `matching_pairs.values()`)

### 4) Submit → parse pairs → grade

1. Student clicks `answer:submit`.
2. `_handle_callback()` creates final pairs list:
    - `pairs = [(left+1, right+1) for left, right in sorted(student.matching_pairs.items())]`
3. Calls `_grade_matching_question(student, pairs)`:
    - `expected_pairs = {(index+1, value+1) for index, value in enumerate(question.answer)}`
    - `provided_pairs = {(left, right) for left, right in pairs if left > 0 and right > 0}`
    - Correct if `provided_pairs == expected_pairs` (exact pair set equality).

### 5) Free-text matching parsing (fallback)

- If a student types a message while `student.current_question_id` is matching, `_handle_text()` calls:
    - `_parse_matching_answer(text)` → list of `(left, right)` tuples
- However, in the normal UI flow, students are expected to use buttons; free-text format is more brittle than the callback flow.

## Non-Obvious Behaviors & Design Decisions

### A) DOCX matching answer parsing assumes a numeric left side

- In `_parse_docx_questions()`, the nested `parse_matching_answer()`:
    - Uses regex `re.findall(r"(\d+)\s*[-=:]?\s*([a-zа-яіїєґ]|\d+)", normalized)`
    - This **only matches when the left part starts with digits** (e.g. `1-2`, `2:4`, `3=А`, etc.).
- But your provided DOCX examples use **letter-left** on the left column:
    - `Відповідь: А-2, Б-4, В-1, Г-3`
- With the current regex, the left-side letters (`А`, `Б`, `В`, `Г`) will not match `(\d+)`, so `parse_matching_answer()` will likely return `[]`.
- **Meaning:** imported `matching` questions can end up with an empty or wrong `question.answer`, which makes grading incorrect even if the options were imported correctly.

### B) Matching uses `half = len(options)//2` to split left vs right

- Both render and grading rely on the positional split:
    - left = first half of `question.options`
    - right = second half
- There is no explicit parsing of “Лівий стовпець / Правий стовпець” into two separate arrays.
- **Meaning:** if DOCX parsing accidentally includes headings, extra whitespace lines, or mis-detects option lines, left/right ordering breaks and the shuffle + grading mapping becomes wrong.

### C) Student callback indices are 0-based, grading converts to 1-based for comparison

- Callback stores `matching_pairs[left_index] = right_index` (0-based).
- Submit converts to `(left+1, right+1)`.
- Expected pairs are also built as `(index+1, answer_value+1)`.
- **Meaning:** This consistency is deliberate; any future change to callback numbering must preserve the `+1` contract in grading.

### D) Right-column “letters” in the UI are always Latin (`a`, `b`, ...)

- `_build_keyboard()` for matching right buttons displays:
    - `chr(ord('a') + index)`
- `_grade_matching_question()` uses `alphabet = "abcdefghijklmnopqrstuvwxyz"` for displaying correct/provided pair text.
- **Meaning:** if you ever need to display Cyrillic letters (А/Б/В…), the UI currently won’t do it; it only uses Latin letters for the right side.

### E) Compact mode modifies text but not the underlying callback mapping

- `compact_mode` changes how options are printed and what the student sees (and sometimes which “preview” text is used),
- but callback data remains `answer:left:{index}` and `answer:right:{index}`.
- **Meaning:** compact mode is safe for grading, but it can confuse developers if they look only at message text instead of internal indices.

## Module Reference (important files/functions)

| File                        | Purpose                                                                                                           |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `bot.py`                    | Entire system: DOCX extraction + parsing, Telegram UI rendering, callback handling, matching grading              |
| `services/stats_service.py` | Admin/statistics helpers (not directly involved in matching parsing/grading)                                      |
| `data/questions.json`       | Persisted question bank; `matching` questions appear here as `type:"matching"` with `options` and `answer` arrays |

## Suggested Reading Order (for a new engineer)

1. `QuizBot._parse_docx_questions()` (matching-specific nested helpers: `parse_matching_answer`, option parsing rules)
2. `QuizBot._send_current_question()` (matching render: split/shuffle/instruction)
3. `QuizBot._build_keyboard()` (matching inline keyboard layout + callback_data contract)
4. `QuizBot._handle_callback()` (left→right selection state machine + submit/reset)
5. `QuizBot._grade_matching_question()` (expected vs provided pair equality logic)

## What’s most likely “the nuance to fix” (based on your DOCX samples)

Your DOCX answers format uses **letters on the left** (`А-2, Б-4, ...`) and **numbers on the right**.  
But the current DOCX matching answer parser only recognizes **numbers on the left**. That mismatch is non-obvious because:

- options parsing strips left markers and collects option texts,
- but answer parsing uses a different regex contract than the DOCX template.

So the likely fix is to extend `parse_matching_answer()` so it can parse patterns where the **left side is a letter (Latin or Cyrillic)** and the **right side is a number**, and then map both sides into 0-based indices consistent with `expected_pairs` in `_grade_matching_question()`.
