# MyTestStudent

Telegram math quiz bot for students and administrators.

## What it does

- one-time student identification
- admin approval before access
- topic-based randomized tests
- single-choice, multiple-choice, and matching questions
- immediate right/wrong feedback
- score tracking
- admin result viewing
- question bank stored in JSON
- optional GPT-based expansion can be added later

## Requirements

- Python 3.10+
- Telegram bot token from BotFather

## Setup

1. Set the token environment variable:

    Windows PowerShell:

    ```powershell
    $env:TELEGRAM_BOT_TOKEN="your_token_here"
    ```

    Command Prompt:

    ```cmd
    set TELEGRAM_BOT_TOKEN=your_token_here
    ```

2. Run the bot:

    ```bash
    python bot.py
    ```

## Files

- `bot.py` — main bot implementation
- `data/questions.json` — question bank
- `data/students.json` — student records
- `data/admins.json` — admin user IDs
- `data/state.json` — reserved for future state persistence

## Admin commands

- `/students` — list students
- `/approve <user_id>` — approve a student
- `/results` — view scores
- `/admin` — admin help

## Question format

Each question in `data/questions.json` uses this structure:

```json
{
    "id": "pl-1",
    "topic": "Планіметрія",
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

## Notes

This is a working rebuild based on the provided requirements, because the archive contained only a compiled `.exe` and no source project.
