# telegram-testy-bot

Telegram math quiz bot for **students** and **administrators**.

## Main features

### Student flow

- Entry via **/start**
- Student profile collection (name/surname) and **admin approval** before access
- **Topic-based** randomized tests
- Question types:
    - `single` (single choice)
    - `multi` (multiple choice)
    - `matching` (pair matching)
    - `text` (free text)
- Immediate feedback (right/wrong) and scoring

### Admin flow

- View students, approve/block access
- Manage topics and questions
- **Import questions from a DOCX file** into a selected topic
- Configure test duration and **reminder times** (optional scheduled reminders)

## Requirements

- Python 3.10+
- A Telegram Bot token (BotFather)

## Configuration (token)

The bot reads the token from one of these places:

- Environment variable: `TELEGRAM_BOT_TOKEN`
- Or `token.txt` in the project root

## Run

```bash
python bot.py
```

## Data storage

### JSON files (`data/`)

- `students.json` — student records + status (`new/approved/...`) + chat_id
- `admins.json` — admin Telegram `user_id`s
- `topics.json` — list of topics (active/inactive, order)
- `questions.json` — question bank
- `state.json` — global bot settings (test duration, reminder settings, tombstones)

### SQLite databases (project root)

- `sessions.db` — per-user dialog/test state (what the bot expects next)
- `results.db` — test result history + per-question difficulty stats (used for admin ratings)

## Question format (`data/questions.json`)

Each question object looks like this:

```json
{
    "id": "pl-1",
    "topic_id": "planimeteria",
    "type": "single",
    "question": "Question text",
    "options": ["A", "B", "C", "D"],
    "answer": [0],
    "explanation": "Short explanation"
}
```

Supported `type` values:

- `single`
- `multi`
- `matching`
- `text`

**Notes**

- The current code uses `topic_id`.
- For backward compatibility, legacy imports may contain `topic` instead of `topic_id` (the bot migrates it internally).

## Admin commands (by text commands)

The bot provides admin commands such as:

- `/students` — list students
- `/approve <user_id>` — approve a student
- `/results` — view results
- `/settime` — configure test duration / reminder-related settings
- `/hardest` — show **top-20 hardest questions globally**  
  (filters: ≥5 graded attempts per question; metric: **Точність %** = `correct_attempts / total_attempts * 100` (rounded to an integer percent); sorting: lowest accuracy first, then total attempts DESC)
- `/admin` — admin help / menu

Additionally, a lot of admin actions are done via **inline buttons/menus** (topics, DOCX import, etc.).

## DOCX import

Admins can upload a `.docx` file to import questions into a selected topic.
The bot parses the DOCX contents and converts them into the internal question model.

## Notes

This repository is a single-binary style project:

- all runtime logic is in `bot.py`
- statistics/accuracy logic is in `services/stats_service.py`
- test message
