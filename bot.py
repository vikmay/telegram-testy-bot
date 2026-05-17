import csv
import hashlib
import json
import os
import random
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from services.stats_service import StatsService


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
QUESTIONS_FILE = DATA_DIR / "questions.json"
DOCX_FILE = BASE_DIR / "Кути на площині.docx"
STUDENTS_FILE = DATA_DIR / "students.json"
ADMINS_FILE = DATA_DIR / "admins.json"
STATE_FILE = DATA_DIR / "state.json"
TOPICS_FILE = DATA_DIR / "topics.json"
RESULTS_DB_FILE = BASE_DIR / "results.db"
SESSIONS_DB_FILE = BASE_DIR / "sessions.db"

DEFAULT_TEST_LENGTH = 10
DEFAULT_TEST_DURATION_SECONDS = 600
POLL_INTERVAL_SECONDS = 2

DEFAULT_TEST_TOPIC_NAME = "Планіметрія"


@dataclass
class Topic:
    id: str
    name: str
    order: int = 0
    active: bool = True


@dataclass
class Question:
    id: str
    topic_id: str
    type: str
    question: str
    options: List[str]
    answer: List[int]
    explanation: str


@dataclass
class StudentState:
    user_id: int
    chat_id: int
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    status: str = "new"
    score: int = 0
    total_attempts: int = 0
    current_test: List[str] = field(default_factory=list)
    current_index: int = 0
    current_question_id: Optional[str] = None
    current_question_message_id: Optional[int] = None
    awaiting_name: bool = False
    awaiting_question: bool = False
    awaiting_docx_import: bool = False
    awaiting_docx_topic_id: Optional[str] = None
    selected_topic_ids: List[str] = field(default_factory=list)
    topic_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)
    pending_multi_answers: List[int] = field(default_factory=list)
    matching_pairs: Dict[int, int] = field(default_factory=dict)
    matching_selected_left: Optional[int] = None
    shuffled_options: List[int] = field(default_factory=list)
    shuffled_matching_left: List[int] = field(default_factory=list)
    shuffled_matching_right: List[int] = field(default_factory=list)
    current_test_topic_id: Optional[str] = None
    current_test_score: int = 0
    current_test_started_at: Optional[float] = None
    current_test_duration_seconds: Optional[int] = None
    awaiting_topic_action: bool = False
    topic_action_mode: Optional[str] = None
    topic_action_source: Optional[str] = None
    awaiting_delete_action: bool = False
    delete_action_mode: Optional[str] = None
    delete_action_source: Optional[str] = None


class JsonStore:
    def __init__(self, path: Path, default):
        self.path = path
        self.default = default

    def load(self):
        if not self.path.exists():
            return self.default
        with self.path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)


class ResultsStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS test_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    full_name TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    attempts INTEGER NOT NULL,
                    topic_id TEXT,
                    topic_name TEXT,
                    questions_count INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
            cols = {row[1] for row in conn.execute("PRAGMA table_info(test_results)").fetchall()}
            if "topic_id" not in cols:
                conn.execute("ALTER TABLE test_results ADD COLUMN topic_id TEXT")
            if "topic_name" not in cols:
                conn.execute("ALTER TABLE test_results ADD COLUMN topic_name TEXT")
            if "questions_count" not in cols:
                conn.execute("ALTER TABLE test_results ADD COLUMN questions_count INTEGER")
            conn.commit()

    def topic_accuracy(self, user_id: int) -> Dict[str, float]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT COALESCE(topic_id, ''),
                       SUM(COALESCE(score, 0)) AS correct_score,
                       SUM(COALESCE(questions_count, ?)) AS total_questions
                FROM test_results
                WHERE user_id = ?
                GROUP BY COALESCE(topic_id, '')
                """,
                (DEFAULT_TEST_LENGTH, user_id),
            )
            return {
                topic_id: float(correct_score or 0) / float(total_questions or DEFAULT_TEST_LENGTH)
                for topic_id, correct_score, total_questions in cursor.fetchall()
            }

    def user_results(self, user_id: int) -> List[Tuple[int, int, str, int, int, str, str, str]]:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT id, user_id, full_name, score, attempts, topic_id, topic_name, created_at FROM test_results WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            )
            return cursor.fetchall()

    def next_attempt_number(self, user_id: int, topic_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM test_results WHERE user_id = ? AND COALESCE(topic_id, '') = ?",
                (user_id, topic_id),
            )
            return int(cursor.fetchone()[0] or 0) + 1

    def add_result(
        self,
        user_id: int,
        full_name: str,
        score: int,
        attempts: int,
        topic_id: str = "",
        topic_name: str = "",
        questions_count: int = DEFAULT_TEST_LENGTH,
    ):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO test_results (user_id, full_name, score, attempts, topic_id, topic_name, questions_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, full_name, score, attempts, topic_id, topic_name, questions_count),
            )
            conn.commit()

    def list_results(self):
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT id, user_id, full_name, score, attempts, topic_id, topic_name, created_at FROM test_results ORDER BY id DESC"
            )
            return cursor.fetchall()

    def user_result_count(self, user_id: int) -> int:
        with self._connect() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM test_results WHERE user_id = ?", (user_id,))
            return int(cursor.fetchone()[0] or 0)

    def delete_student_history(self, user_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM test_results WHERE user_id = ?", (user_id,))
            conn.commit()


class SessionStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS student_sessions (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    current_test TEXT NOT NULL DEFAULT '[]',
                    current_index INTEGER NOT NULL DEFAULT 0,
                    current_question_id TEXT,
                    current_question_message_id INTEGER,
                    pending_multi_answers TEXT NOT NULL DEFAULT '[]',
                    matching_pairs TEXT NOT NULL DEFAULT '{}',
                    matching_selected_left INTEGER,
                    shuffled_options TEXT NOT NULL DEFAULT '[]',
                    shuffled_matching_left TEXT NOT NULL DEFAULT '[]',
                    shuffled_matching_right TEXT NOT NULL DEFAULT '[]',
                    current_test_topic_id TEXT,
                    current_test_score INTEGER NOT NULL DEFAULT 0,
                    current_test_started_at REAL,
                    current_test_duration_seconds INTEGER,
                    selected_topic_ids TEXT NOT NULL DEFAULT '[]',
                    topic_stats TEXT NOT NULL DEFAULT '{}',
                    awaiting_name INTEGER NOT NULL DEFAULT 0,
                    awaiting_question INTEGER NOT NULL DEFAULT 0,
                    awaiting_docx_import INTEGER NOT NULL DEFAULT 0,
                    awaiting_docx_topic_id TEXT,
                    awaiting_topic_action INTEGER NOT NULL DEFAULT 0,
                    topic_action_mode TEXT,
                    topic_action_source TEXT,
                    awaiting_delete_action INTEGER NOT NULL DEFAULT 0,
                    delete_action_mode TEXT,
                    delete_action_source TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
            cols = {row[1] for row in conn.execute("PRAGMA table_info(student_sessions)").fetchall()}
            migrations = [
                ("matching_pairs", "ALTER TABLE student_sessions ADD COLUMN matching_pairs TEXT NOT NULL DEFAULT '{}'"),
                ("matching_selected_left", "ALTER TABLE student_sessions ADD COLUMN matching_selected_left INTEGER"),
                ("shuffled_options", "ALTER TABLE student_sessions ADD COLUMN shuffled_options TEXT NOT NULL DEFAULT '[]'"),
                ("shuffled_matching_left", "ALTER TABLE student_sessions ADD COLUMN shuffled_matching_left TEXT NOT NULL DEFAULT '[]'"),
                ("shuffled_matching_right", "ALTER TABLE student_sessions ADD COLUMN shuffled_matching_right TEXT NOT NULL DEFAULT '[]'"),
                ("awaiting_docx_import", "ALTER TABLE student_sessions ADD COLUMN awaiting_docx_import INTEGER NOT NULL DEFAULT 0"),
                ("awaiting_docx_topic_id", "ALTER TABLE student_sessions ADD COLUMN awaiting_docx_topic_id TEXT"),
                ("awaiting_topic_action", "ALTER TABLE student_sessions ADD COLUMN awaiting_topic_action INTEGER NOT NULL DEFAULT 0"),
                ("topic_action_mode", "ALTER TABLE student_sessions ADD COLUMN topic_action_mode TEXT"),
                ("topic_action_source", "ALTER TABLE student_sessions ADD COLUMN topic_action_source TEXT"),
                ("awaiting_delete_action", "ALTER TABLE student_sessions ADD COLUMN awaiting_delete_action INTEGER NOT NULL DEFAULT 0"),
                ("delete_action_mode", "ALTER TABLE student_sessions ADD COLUMN delete_action_mode TEXT"),
                ("delete_action_source", "ALTER TABLE student_sessions ADD COLUMN delete_action_source TEXT"),
                ("updated_at", "ALTER TABLE student_sessions ADD COLUMN updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"),
            ]
            for column, sql in migrations:
                if column not in cols:
                    conn.execute(sql)
            conn.commit()

    @staticmethod
    def _dumps(value) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _loads(value, default):
        if value in (None, ""):
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default

    def load_all(self) -> Dict[str, dict]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT user_id, chat_id, current_test, current_index, current_question_id, current_question_message_id,
                       pending_multi_answers, matching_pairs, matching_selected_left, shuffled_options, shuffled_matching_left, shuffled_matching_right, current_test_topic_id, current_test_score, current_test_started_at,
                       current_test_duration_seconds, selected_topic_ids, topic_stats, awaiting_name, awaiting_question,
                       awaiting_docx_import, awaiting_docx_topic_id, awaiting_topic_action, topic_action_mode,
                       topic_action_source, awaiting_delete_action, delete_action_mode, delete_action_source
                FROM student_sessions
                """
            )
            rows = {}
            for row in cursor.fetchall():
                rows[str(row[0])] = {
                    "user_id": row[0],
                    "chat_id": row[1],
                    "current_test": self._loads(row[2], []),
                    "current_index": row[3],
                    "current_question_id": row[4],
                    "current_question_message_id": row[5],
                    "pending_multi_answers": self._loads(row[6], []),
                    "matching_pairs": {
                        int(left): int(right)
                        for left, right in (self._loads(row[7], {}) or {}).items()
                        if str(left).lstrip("-").isdigit() and str(right).lstrip("-").isdigit()
                    },
                    "matching_selected_left": row[8],
                    "shuffled_options": self._loads(row[9], []),
                    "shuffled_matching_left": self._loads(row[10], []),
                    "shuffled_matching_right": self._loads(row[11], []),
                    "current_test_topic_id": row[12],
                    "current_test_score": row[13],
                    "current_test_started_at": row[14],
                    "current_test_duration_seconds": row[15],
                    "selected_topic_ids": self._loads(row[16], []),
                    "topic_stats": self._loads(row[17], {}),
                    "awaiting_name": bool(row[18]),
                    "awaiting_question": bool(row[19]),
                    "awaiting_docx_import": bool(row[20]),
                    "awaiting_docx_topic_id": row[21],
                    "awaiting_topic_action": bool(row[22]),
                    "topic_action_mode": row[23],
                    "topic_action_source": row[24],
                    "awaiting_delete_action": bool(row[25]),
                    "delete_action_mode": row[26],
                    "delete_action_source": row[27],
                }
            return rows

    def save_student(self, state: StudentState):
        payload = {
            "user_id": state.user_id,
            "chat_id": state.chat_id,
            "current_test": self._dumps(state.current_test),
            "current_index": state.current_index,
            "current_question_id": state.current_question_id,
            "current_question_message_id": state.current_question_message_id,
            "pending_multi_answers": self._dumps(state.pending_multi_answers),
            "matching_pairs": self._dumps(state.matching_pairs),
            "matching_selected_left": state.matching_selected_left,
            "shuffled_options": self._dumps(state.shuffled_options),
            "shuffled_matching_left": self._dumps(state.shuffled_matching_left),
            "shuffled_matching_right": self._dumps(state.shuffled_matching_right),
            "current_test_topic_id": state.current_test_topic_id,
            "current_test_score": state.current_test_score,
            "current_test_started_at": state.current_test_started_at,
            "current_test_duration_seconds": state.current_test_duration_seconds,
            "selected_topic_ids": self._dumps(state.selected_topic_ids),
            "topic_stats": self._dumps(state.topic_stats),
            "awaiting_name": int(state.awaiting_name),
            "awaiting_question": int(state.awaiting_question),
            "awaiting_docx_import": int(state.awaiting_docx_import),
            "awaiting_docx_topic_id": state.awaiting_docx_topic_id,
            "awaiting_topic_action": int(state.awaiting_topic_action),
            "topic_action_mode": state.topic_action_mode,
            "topic_action_source": state.topic_action_source,
            "awaiting_delete_action": int(state.awaiting_delete_action),
            "delete_action_mode": state.delete_action_mode,
            "delete_action_source": state.delete_action_source,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO student_sessions (
                    user_id, chat_id, current_test, current_index, current_question_id, current_question_message_id,
                    pending_multi_answers, matching_pairs, matching_selected_left, current_test_topic_id, current_test_score, current_test_started_at,
                    current_test_duration_seconds, selected_topic_ids, topic_stats, awaiting_name, awaiting_question,
                    awaiting_docx_import, awaiting_docx_topic_id, awaiting_topic_action, topic_action_mode,
                    topic_action_source, awaiting_delete_action, delete_action_mode, delete_action_source, updated_at
                ) VALUES (
                    :user_id, :chat_id, :current_test, :current_index, :current_question_id, :current_question_message_id,
                    :pending_multi_answers, :matching_pairs, :matching_selected_left, :current_test_topic_id, :current_test_score, :current_test_started_at,
                    :current_test_duration_seconds, :selected_topic_ids, :topic_stats, :awaiting_name, :awaiting_question,
                    :awaiting_docx_import, :awaiting_docx_topic_id, :awaiting_topic_action, :topic_action_mode,
                    :topic_action_source, :awaiting_delete_action, :delete_action_mode, :delete_action_source, CURRENT_TIMESTAMP
                )
                ON CONFLICT(user_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    current_test=excluded.current_test,
                    current_index=excluded.current_index,
                    current_question_id=excluded.current_question_id,
                    current_question_message_id=excluded.current_question_message_id,
                    pending_multi_answers=excluded.pending_multi_answers,
                    matching_pairs=excluded.matching_pairs,
                    matching_selected_left=excluded.matching_selected_left,
                    shuffled_options=excluded.shuffled_options,
                    shuffled_matching_left=excluded.shuffled_matching_left,
                    shuffled_matching_right=excluded.shuffled_matching_right,
                    current_test_topic_id=excluded.current_test_topic_id,
                    current_test_score=excluded.current_test_score,
                    current_test_started_at=excluded.current_test_started_at,
                    current_test_duration_seconds=excluded.current_test_duration_seconds,
                    selected_topic_ids=excluded.selected_topic_ids,
                    topic_stats=excluded.topic_stats,
                    awaiting_name=excluded.awaiting_name,
                    awaiting_question=excluded.awaiting_question,
                    awaiting_docx_import=excluded.awaiting_docx_import,
                    awaiting_docx_topic_id=excluded.awaiting_docx_topic_id,
                    awaiting_topic_action=excluded.awaiting_topic_action,
                    topic_action_mode=excluded.topic_action_mode,
                    topic_action_source=excluded.topic_action_source,
                    awaiting_delete_action=excluded.awaiting_delete_action,
                    delete_action_mode=excluded.delete_action_mode,
                    delete_action_source=excluded.delete_action_source,
                    updated_at=CURRENT_TIMESTAMP
                """,
                payload,
            )
            conn.commit()

    def get_last_activity_at(self, user_id: int) -> Optional[str]:
        """
        Returns student_sessions.updated_at as string or None.
        Safe fallback for missing column/rows.
        """
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "SELECT updated_at FROM student_sessions WHERE user_id = ?",
                    (user_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                value = row[0]
                if value in (None, ""):
                    return None
                return str(value)
        except sqlite3.Error:
            return None

    def get_updated_at_at(self, user_id: int) -> Optional[str]:
        # compatibility alias
        return self.get_last_activity_at(user_id)

    def delete_student(self, user_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM student_sessions WHERE user_id = ?", (user_id,))
            conn.commit()


class BotApi:
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"

    def request(self, method: str, payload: Optional[dict] = None):
        url = f"{self.base_url}/{method}"
        data = None
        headers = {}
        if payload is not None:
            data = urllib.parse.urlencode(payload, doseq=True).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                raise RuntimeError(f"Telegram API error: {result}")
            return result["result"]

    def get_updates(self, offset: Optional[int] = None):
        payload = {"timeout": 20}
        if offset is not None:
            payload["offset"] = offset
        return self.request("getUpdates", payload)

    def send_message(self, chat_id: int, text: str, reply_markup: Optional[dict] = None):
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        return self.request("sendMessage", payload)

    def answer_callback_query(self, callback_query_id: str, text: str = ""):
        payload = {"callback_query_id": callback_query_id}
        return self.request("answerCallbackQuery", payload)

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: Optional[dict] = None):
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        return self.request("editMessageText", payload)


class QuizBot:
    def __init__(self):
        self.token = self._load_token()
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable or token.txt file is required")
        self.admin_user_ids = self._load_admin_ids()
        self.students_store = JsonStore(STUDENTS_FILE, {})
        self.questions_store = JsonStore(QUESTIONS_FILE, [])
        self.state_store = JsonStore(STATE_FILE, {"test_duration_seconds": DEFAULT_TEST_DURATION_SECONDS})
        self.topics_store = JsonStore(TOPICS_FILE, [])
        self.results_store = ResultsStore(RESULTS_DB_FILE)
        self.sessions_store = SessionStore(SESSIONS_DB_FILE)
        self.stats_service = StatsService(
            results_store=self.results_store,
            sessions_store=self.sessions_store,
            topic_name_fn=lambda topic_id: self._topic_name(topic_id),
            default_test_length=DEFAULT_TEST_LENGTH,
        )
        self.api = BotApi(self.token)
        self.students: Dict[str, StudentState] = {}
        self.questions: List[Question] = []
        self.topics: List[Topic] = []

        # Tombstones: prevents `_get_student()` from recreating a deleted student record.
        raw_state = self.state_store.load()
        deleted_keys_raw = raw_state.get("deleted_student_keys", [])
        self.deleted_student_keys: Set[str] = (
            {str(k) for k in deleted_keys_raw} if isinstance(deleted_keys_raw, list) else set()
        )

        self.test_duration_seconds = self._load_test_duration_seconds()
        self._load_data()

    def _load_token(self) -> str:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if token:
            return token
        token_file = BASE_DIR / "token.txt"
        return token_file.read_text(encoding="utf-8").strip() if token_file.exists() else ""

    def _load_admin_ids(self) -> Set[int]:
        if not ADMINS_FILE.exists():
            return set()
        raw = JsonStore(ADMINS_FILE, {"admin_user_ids": []}).load()
        return {int(user_id) for user_id in raw.get("admin_user_ids", [])}

    def _load_test_duration_seconds(self) -> int:
        raw = self.state_store.load()
        value = raw.get("test_duration_seconds")
        if value is None and "test_duration_minutes" in raw:
            value = int(raw.get("test_duration_minutes", 10)) * 60
        try:
            return max(10, int(value) if value is not None else DEFAULT_TEST_DURATION_SECONDS)
        except (TypeError, ValueError):
            return DEFAULT_TEST_DURATION_SECONDS


    def _persist_state(self):
        # Merge with what is already stored on disk to avoid losing tombstones.
        # This is important because different actions can call _persist_state()
        # at different times.
        try:
            existing = self.state_store.load() or {}
        except Exception:
            existing = {}

        existing_deleted = existing.get("deleted_student_keys", [])
        existing_set: Set[str] = (
            {str(k) for k in existing_deleted} if isinstance(existing_deleted, list) else set()
        )

        current_deleted = getattr(self, "deleted_student_keys", set())
        current_set: Set[str] = (
            {str(k) for k in current_deleted} if isinstance(current_deleted, (set, list)) else set()
        )

        merged = sorted(existing_set | current_set)

        self.state_store.save(
            {
                "test_duration_seconds": self.test_duration_seconds,
                "deleted_student_keys": merged,
            }
        )

    def _migrate_topics_payload(self, raw) -> List[Topic]:
        topics: List[Topic] = []
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                if isinstance(item, dict):
                    topic_id = str(item.get("id", "")).strip()
                    name = str(item.get("name", "")).strip()
                    if topic_id and name:
                        topics.append(
                            Topic(
                                id=topic_id,
                                name=name,
                                order=int(item.get("order", index)),
                                active=bool(item.get("active", True)),
                            )
                        )
                else:
                    name = str(item).strip()
                    if name:
                        topic_id = self._slugify_topic_id(name)
                        topics.append(Topic(id=topic_id, name=name, order=index, active=True))
        return topics

    def _migrate_question_payload(self, raw) -> List[Question]:
        questions: List[Question] = []
        if not isinstance(raw, list):
            return questions
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            topic_id = str(item.get("topic_id", "")).strip()
            if not topic_id:
                legacy_topic = str(item.get("topic", "")).strip()
                topic = self._find_topic_by_name(legacy_topic) if legacy_topic else None
                topic_id = topic.id if topic else ""
            if not topic_id:
                continue
            questions.append(
                Question(
                    id=str(item.get("id", "")).strip() or f"legacy-{index + 1}",
                    topic_id=topic_id,
                    type=str(item.get("type", "single")).strip() or "single",
                    question=str(item.get("question", "")).strip(),
                    options=[str(option).strip() for option in item.get("options", []) if str(option).strip()],
                    answer=[int(answer) for answer in item.get("answer", []) if isinstance(answer, int) or str(answer).isdigit()],
                    explanation=str(item.get("explanation", "")).strip(),
                )
            )
        return questions

    def _topic_by_name_or_create(self, name: str) -> Topic:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Topic name is required")
        found = self._find_topic_by_name(cleaned)
        if found:
            return found
        topic_id = self._slugify_topic_id(cleaned)
        while self._topic_by_id(topic_id):
            topic_id = f"{topic_id}-{random.randint(100, 999)}"
        topic = Topic(id=topic_id, name=cleaned, order=len(self.topics), active=True)
        self.topics.append(topic)
        return topic

    def _slugify_topic_id(self, name: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9а-яА-ЯіїєґІЇЄҐ]+", "-", name.strip().lower()).strip("-")
        return slug or f"topic-{random.randint(1000, 9999)}"

    def _topic_ref(self, topic_id: str) -> str:
        """
        Short deterministic reference for topic_id so Telegram callback_data stays within limits.
        """
        digest = hashlib.sha1(topic_id.encode("utf-8")).hexdigest()
        return digest[:10]

    def _topic_id_from_ref(self, topic_ref: str) -> Optional[str]:
        """
        Resolve a short topic_ref back to full topic_id by scanning active topics.
        """
        for topic in self.topics:
            if self._topic_ref(topic.id) == topic_ref:
                return topic.id
        return None

    def _resolve_topic_id(self, topic_id_or_ref: str) -> Optional[str]:
        """
        Accept either full topic_id (legacy/other flows) or short topic ref
        (admin inline callbacks) and always return full topic_id.
        """
        if self._topic_by_id(topic_id_or_ref):
            return topic_id_or_ref
        return self._topic_id_from_ref(topic_id_or_ref)

    def _load_topics(self) -> List[Topic]:
        raw = self.topics_store.load()
        topics: List[Topic] = []
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                if isinstance(item, dict):
                    topic_id = str(item.get("id", "")).strip()
                    name = str(item.get("name", "")).strip()
                    if topic_id and name:
                        topics.append(Topic(id=topic_id, name=name, order=int(item.get("order", index)), active=bool(item.get("active", True))))
                else:
                    name = str(item).strip()
                    if name:
                        topic_id = self._slugify_topic_id(name)
                        topics.append(Topic(id=topic_id, name=name, order=index, active=True))
        return sorted(topics, key=lambda item: (item.order, item.name.lower()))

    def _save_topics(self, topics: List[Topic]):
        cleaned = []
        for index, topic in enumerate(sorted(topics, key=lambda item: (item.order, item.name.lower()))):
            cleaned.append({"id": topic.id, "name": topic.name, "order": index, "active": topic.active})
        self.topics_store.save(cleaned)
        self.topics = [Topic(**item) for item in cleaned]

    def _topic_by_id(self, topic_id: str) -> Optional[Topic]:
        for topic in self.topics:
            if topic.id == topic_id:
                return topic
        return None

    def _topic_name(self, topic_id: str) -> str:
        topic = self._topic_by_id(topic_id)
        return topic.name if topic else topic_id

    def _visible_topics(self) -> List[Topic]:
        return [topic for topic in self.topics if topic.active]

    def _extract_docx_text(self, path: Path) -> str:
        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read("word/document.xml")
        root = ET.fromstring(document_xml)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = []
        for paragraph in root.findall(".//w:p", ns):
            texts = [node.text for node in paragraph.findall(".//w:t", ns) if node.text]
            if texts:
                paragraphs.append("".join(texts).strip())
        return "\n".join(line for line in paragraphs if line)

    def _parse_docx_questions(self, text: str, topic_id: str) -> List[Question]:
        def normalize(line: str) -> str:
            cleaned = (
                line.replace("\u00ad", "")
                .replace("—", "-")
                .replace("\u00a0", " ")
                .replace("\u200b", "")
                .replace("\ufeff", "")
            )
            return re.sub(r"\s+", " ", cleaned).strip()

        raw_lines = [normalize(line) for line in text.splitlines()]
        lines = [line for line in raw_lines if line]
        questions: List[Question] = []
        current: Optional[Dict[str, object]] = None
        active_topic_id = topic_id
        q_pat = re.compile(r"^(?:Завдання|Питання|№)\s*(\d+)?[.:)]?\s*(.*)$", re.IGNORECASE)
        o_pat = re.compile(r"^(?:[-*•]|\(?\d+\)?|[A-Za-zА-Яа-я]\s*[\).:]|\d+\s*[\).:]|\d+\s*[\-:])\s*(.*)$")
        a_pat = re.compile(r"^(?:Відповідь|Answer|Correct answer)\s*[:\-]?\s*(.*)$", re.IGNORECASE)
        t_pat = re.compile(r"^(?:Тема|Topic)\s*[:\-]?\s*(.*)$", re.IGNORECASE)
        x_pat = re.compile(r"^(?:Тип|Type)\s*[:\-]?\s*(.*)$", re.IGNORECASE)
        e_pat = re.compile(r"^(?:Пояснення|Explanation)\s*[:\-]?\s*(.*)$", re.IGNORECASE)

        def parse_answer_indices(answer_text: str) -> List[int]:
            numbers = [int(item) for item in re.findall(r"\d+", normalize(answer_text))]
            return [num - 1 for num in numbers if num >= 1]

        def parse_matching_answer(answer_text: str) -> List[int]:
            normalized = normalize(answer_text).lower().replace(" ", "")
            matches = re.findall(r"(\d+)\s*[-=:]?\s*([a-zа-яіїєґ]|\d+)", normalized)
            if not matches:
                return []

            alphabet = "abcdefghijklmnopqrstuvwxyzабвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
            mapped: Dict[int, int] = {}
            for left_raw, right_raw in matches:
                left = int(left_raw) - 1
                if right_raw.isdigit():
                    right = int(right_raw) - 1
                else:
                    right = alphabet.find(right_raw)
                    if right >= 26:
                        right -= 26
                if left >= 0 and right >= 0:
                    mapped[left] = right
            return [mapped[index] for index in sorted(mapped)]

        def parse_question_type(raw_type: str) -> str:
            cleaned = raw_type.strip().lower()
            if cleaned in {"single", "one", "одна", "один"}:
                return "single"
            if cleaned in {"multi", "multiple", "many", "кілька", "many-choice"}:
                return "multi"
            if cleaned in {"matching", "match", "зіставлення"}:
                return "matching"
            if cleaned in {"text", "free", "open", "open-ended", "відкрите"}:
                return "text"
            return cleaned or "single"

        def flush():
            nonlocal current
            if not current:
                return
            question_text = str(current.get("question", "")).strip()
            options = [str(option).strip() for option in current.get("options", []) if str(option).strip()]
            answer = [int(index) for index in current.get("answer", []) if isinstance(index, int) and index >= 0]
            current_topic = str(current.get("topic_id", active_topic_id)).strip() or active_topic_id
            question_type = str(current.get("type", "single")).strip() or "single"
            if question_text and current_topic:
                questions.append(
                    Question(
                        id=str(uuid.uuid4()),
                        topic_id=current_topic,
                        type=question_type,
                        question=question_text,
                        options=options,
                        answer=answer,
                        explanation=str(current.get("explanation", "")),
                    )
                )
            current = None

        for line in lines:
            if (m := t_pat.match(line)):
                name = m.group(1).strip()
                topic = self._find_topic_by_name(name)
                if topic:
                    active_topic_id = topic.id
                continue

            match = q_pat.match(line)
            if match:
                flush()
                current = {
                    "id": str(uuid.uuid4()),
                    "topic_id": active_topic_id,
                    "type": "single",
                    "question": match.group(2).strip(),
                    "options": [],
                    "answer": [],
                    "explanation": "",
                }
                continue

            if current is None:
                continue

            if (m := x_pat.match(line)):
                current["type"] = parse_question_type(m.group(1))
                continue

            if (m := a_pat.match(line)):
                answer_text = m.group(1).strip()
                question_type = str(current.get("type", "single")).strip().lower()
                if question_type in {"match", "matching"}:
                    parsed = parse_matching_answer(answer_text)
                else:
                    parsed = parse_answer_indices(answer_text)
                if parsed:
                    current["answer"] = parsed
                continue

            if (m := e_pat.match(line)):
                current["explanation"] = m.group(1).strip()
                continue

            if (m := o_pat.match(line)):
                opt = m.group(1).strip()
                if opt:
                    if str(current.get("type", "single")).strip().lower() == "matching":
                        opt = re.sub(r"^[a-zа-яіїєґ]\s*[\).:-]\s*", "", opt, flags=re.IGNORECASE)
                        opt = re.sub(r"^\d+\s*[\).:-]\s*", "", opt)
                    current["options"].append(opt)
                continue

            numbered_option = re.match(r"^\s*(\d+)\s*[\)\.\:\-]\s*(.+)$", line)
            if numbered_option and str(current.get("type", "single")).strip().lower() in {"single", "multi"}:
                current["options"].append(numbered_option.group(2).strip())
                continue

            if str(current.get("type", "single")).strip().lower() in {"single", "multi"}:
                fallback_opt = re.match(r"^\s*(\d+)\s*[\).:-]\s*(.+)$", line)
                if fallback_opt:
                    current["options"].append(fallback_opt.group(2).strip())
                    continue

            if not current["question"]:
                current["question"] = line
            else:
                question_type = str(current.get("type", "single")).strip().lower()
                option_like = re.match(r"^(?:[-*•]|\(?\d+\)?|[A-Za-zА-Яа-я]\s*[\).:]|\d+\s*[\).:])\s*(.+)$", line)
                if question_type in {"single", "multi", "matching"} and option_like:
                    current["options"].append(option_like.group(1).strip())
                elif question_type == "text":
                    current["explanation"] = f"{current['explanation']} {line}".strip()
                elif not current["options"]:
                    current["options"] = [part.strip() for part in line.split("|") if part.strip()]
                else:
                    current["explanation"] = f"{current['explanation']} {line}".strip()

        flush()
        return questions

    def _find_docx_source(self) -> Optional[Path]:
        if DOCX_FILE.exists():
            return DOCX_FILE
        docx_files = sorted(path for path in BASE_DIR.glob("*.docx") if path.is_file() and not path.name.startswith("~$"))
        return docx_files[0] if docx_files else None

    def _dedupe_question_id(self, question_id: str, existing_ids: Set[str]) -> str:
        base_id = question_id.strip() or str(uuid.uuid4())
        if base_id not in existing_ids:
            return base_id
        return str(uuid.uuid4())

    def _load_questions_from_docx(self) -> List[Question]:
        return []

    def _load_questions_from_uploaded_docx(self, path: Path, fallback_topic_id: str = "") -> List[Question]:
        return self._parse_docx_questions(self._extract_docx_text(path), topic_id=fallback_topic_id)

    def _save_imported_questions(self, imported: List[Question]) -> Tuple[int, int]:
        existing_ids = {question.id for question in self.questions}
        added = 0
        duplicates = 0
        for question in imported:
            unique_id = self._dedupe_question_id(question.id, existing_ids)
            if unique_id != question.id:
                duplicates += 1
                question = Question(
                    id=unique_id,
                    topic_id=question.topic_id,
                    type=question.type,
                    question=question.question,
                    options=question.options,
                    answer=question.answer,
                    explanation=question.explanation,
                )
            if not question.topic_id:
                continue
            self.questions.append(question)
            existing_ids.add(question.id)
            added += 1
        self.questions_store.save([{"id": q.id, "topic_id": q.topic_id, "type": q.type, "question": q.question, "options": q.options, "answer": q.answer, "explanation": q.explanation} for q in self.questions])
        return added, duplicates

    def _download_telegram_file(self, file_id: str) -> Optional[Path]:
        meta = self.api.request("getFile", {"file_id": file_id})
        file_path = meta.get("file_path")
        if not file_path:
            return None
        with urllib.request.urlopen(f"https://api.telegram.org/file/bot{self.token}/{file_path}", timeout=60) as response:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as handle:
                handle.write(response.read())
                return Path(handle.name)

    def _find_topic_by_name(self, name: str) -> Optional[Topic]:
        cleaned = name.strip().casefold()
        for topic in self.topics:
            if topic.name.strip().casefold() == cleaned:
                return topic
        return None

    def _ensure_default_topics(self):
        if not self.topics:
            self._save_topics(self.topics)

    def _load_data(self):
        self.topics = self._migrate_topics_payload(self.topics_store.load())
        self._save_topics(self.topics)

        question_rows = self.questions_store.load()
        if isinstance(question_rows, list) and question_rows:
            self.questions = []
            for row in question_rows:
                if not isinstance(row, dict):
                    continue
                if "topic_id" in row:
                    self.questions.append(Question(**row))
                elif "topic" in row:
                    topic = self._topic_by_name_or_create(str(row.get("topic", "")).strip())
                    self.questions.append(
                        Question(
                            id=str(row.get("id", "")).strip() or f"legacy-{len(self.questions)+1}",
                            topic_id=topic.id,
                            type=str(row.get("type", "single")).strip() or "single",
                            question=str(row.get("question", "")).strip(),
                            options=[str(option).strip() for option in row.get("options", []) if str(option).strip()],
                            answer=[int(answer) for answer in row.get("answer", []) if isinstance(answer, int) or str(answer).isdigit()],
                            explanation=str(row.get("explanation", "")).strip(),
                        )
                    )
            self.questions_store.save([{"id": q.id, "topic_id": q.topic_id, "type": q.type, "question": q.question, "options": q.options, "answer": q.answer, "explanation": q.explanation} for q in self.questions])
        else:
            self.questions = self._load_questions_from_docx()
            self.questions_store.save([{"id": q.id, "topic_id": q.topic_id, "type": q.type, "question": q.question, "options": q.options, "answer": q.answer, "explanation": q.explanation} for q in self.questions])

        if self.topics and any(not topic.id for topic in self.topics):
            self.topics = [topic for topic in self.topics if topic.id]
            self._save_topics(self.topics)

        student_rows = self.students_store.load()
        session_rows = self.sessions_store.load_all()
        for key, row in student_rows.items():
            if not isinstance(row, dict):
                continue
            try:
                user_id_int = int(key)
            except (TypeError, ValueError):
                user_id_int = None
            normalized_row = dict(row)
            legacy_selected_topics = normalized_row.pop("selected_topics", None)
            legacy_current_test_topic = normalized_row.pop("current_test_topic", None)
            legacy_current_test_duration_minutes = normalized_row.pop("current_test_duration_minutes", None)
            if "selected_topic_ids" not in normalized_row and legacy_selected_topics is not None:
                normalized_row["selected_topic_ids"] = legacy_selected_topics
            if "current_test_topic_id" not in normalized_row and legacy_current_test_topic is not None:
                if legacy_current_test_topic:
                    normalized_row["current_test_topic_id"] = self._topic_id_from_menu(self._topic_by_name_or_create(str(legacy_current_test_topic)).id)
                else:
                    normalized_row["current_test_topic_id"] = None
            if "current_test_duration_seconds" not in normalized_row and legacy_current_test_duration_minutes is not None:
                normalized_row["current_test_duration_seconds"] = int(legacy_current_test_duration_minutes or 0) * 60 or None
            self.students[key] = StudentState(**normalized_row)

        for key, session in session_rows.items():
            try:
                user_id_int = int(key)
            except (TypeError, ValueError):
                user_id_int = None
            student = self.students.get(key)
            if not student:
                continue
            student.current_test = session.get("current_test", [])
            student.current_index = session.get("current_index", 0)
            student.current_question_id = session.get("current_question_id")
            student.current_question_message_id = session.get("current_question_message_id")
            student.pending_multi_answers = session.get("pending_multi_answers", [])
            student.matching_pairs = {
                int(left): int(right)
                for left, right in (session.get("matching_pairs", {}) or {}).items()
                if str(left).lstrip("-").isdigit() and str(right).lstrip("-").isdigit()
            }
            student.matching_selected_left = session.get("matching_selected_left")
            student.shuffled_options = session.get("shuffled_options", [])
            student.shuffled_matching_left = session.get("shuffled_matching_left", [])
            student.shuffled_matching_right = session.get("shuffled_matching_right", [])
            raw_current_test_topic_id = session.get("current_test_topic_id")
            normalized_current_topic_ids = (
                self._normalize_topic_ids([raw_current_test_topic_id]) if raw_current_test_topic_id else []
            )
            student.current_test_topic_id = normalized_current_topic_ids[0] if normalized_current_topic_ids else None
            student.current_test_score = session.get("current_test_score", 0)
            student.current_test_started_at = session.get("current_test_started_at")
            student.current_test_duration_seconds = session.get("current_test_duration_seconds") or self.test_duration_seconds
            student.selected_topic_ids = self._normalize_topic_ids(session.get("selected_topic_ids", student.selected_topic_ids or []))
            student.topic_stats = session.get("topic_stats", student.topic_stats or {})
            student.awaiting_name = session.get("awaiting_name", student.awaiting_name)
            student.awaiting_question = session.get("awaiting_question", student.awaiting_question)
            student.awaiting_docx_import = session.get("awaiting_docx_import", student.awaiting_docx_import)
            student.awaiting_docx_topic_id = session.get("awaiting_docx_topic_id", student.awaiting_docx_topic_id)
            student.awaiting_topic_action = session.get("awaiting_topic_action", student.awaiting_topic_action)
            student.topic_action_mode = session.get("topic_action_mode", student.topic_action_mode)
            student.topic_action_source = session.get("topic_action_source", student.topic_action_source)
            student.awaiting_delete_action = session.get("awaiting_delete_action", student.awaiting_delete_action)
            student.delete_action_mode = session.get("delete_action_mode", student.delete_action_mode)
            student.delete_action_source = session.get("delete_action_source", student.delete_action_source)
        for student in self.students.values():
            student.selected_topic_ids = self._normalize_topic_ids(student.selected_topic_ids or [])
            student.matching_pairs = {
                int(left): int(right)
                for left, right in (student.matching_pairs or {}).items()
                if isinstance(left, int) or str(left).lstrip("-").isdigit()
            }
            student.matching_selected_left = int(student.matching_selected_left) if student.matching_selected_left is not None and str(student.matching_selected_left).lstrip("-").isdigit() else None
            student.shuffled_options = [int(index) for index in student.shuffled_options if isinstance(index, int) or str(index).isdigit()]
            student.shuffled_matching_left = [int(index) for index in student.shuffled_matching_left if isinstance(index, int) or str(index).isdigit()]
            student.shuffled_matching_right = [int(index) for index in student.shuffled_matching_right if isinstance(index, int) or str(index).isdigit()]
            # Keep topic_stats consistent with the normalized selected_topic_ids
            # (drop stale keys that no longer exist as topics)
            if student.topic_stats:
                student.topic_stats = {
                    topic_id: student.topic_stats.get(topic_id, {"correct": 0, "total": 0})
                    for topic_id in student.selected_topic_ids
                }
            else:
                student.topic_stats = {topic_id: {"correct": 0, "total": 0} for topic_id in student.selected_topic_ids}
            student.current_test_duration_seconds = student.current_test_duration_seconds or self.test_duration_seconds
            if student.user_id in self.admin_user_ids and student.status in {"new", "awaiting_name", "pending_approval"}:
                student.status = "approved"

    def _persist_students(self):
        payload = {}
        for key, state in self.students.items():
            # Tombstone placeholder: don't persist deleted records.
            if state.status == "deleted":
                continue

            payload[key] = {
                "user_id": state.user_id,
                "chat_id": state.chat_id,
                "first_name": state.first_name,
                "last_name": state.last_name,
                "full_name": state.full_name,
                "status": state.status,
                "score": state.score,
                "total_attempts": state.total_attempts,
            }
        self.students_store.save(payload)
        for state in self.students.values():
            if state.status == "deleted":
                continue
            self.sessions_store.save_student(state)


    def _notify_admins(self, text: str, reply_markup: Optional[dict] = None):
        admin_chat_ids: Set[int] = set()
        for state in self.students.values():
            if state.status == "deleted":
                continue
            if state.user_id in self.admin_user_ids:
                try:
                    if state.chat_id is not None:
                        admin_chat_ids.add(int(state.chat_id))
                except (TypeError, ValueError):
                    continue

        if not admin_chat_ids:
            return

        for admin_chat_id in admin_chat_ids:
            try:
                self.api.send_message(admin_chat_id, text, reply_markup=reply_markup)
            except RuntimeError as exc:
                print(f"[admin notify] failed chat_id={admin_chat_id}: {exc}")

    def _student_key(self, user_id: int) -> str:
        return str(user_id)

    def _get_student(self, user_id: int, chat_id: int) -> StudentState:
        key = self._student_key(user_id)
        if key in getattr(self, "deleted_student_keys", set()):
            # Tombstone: don't recreate in-memory students after delete.
            # For admins, we *clear* the tombstone so they remain fully usable
            # (e.g. DOCX import + topic/question delete).
            if user_id in getattr(self, "admin_user_ids", set()):
                self.deleted_student_keys.remove(key)
                self._persist_state()
            else:
                # Use a non-empty label so admin lists/cards don't show "Р±РµР· С–РјРµРЅС–".
                return StudentState(
                    user_id=user_id,
                    chat_id=chat_id,
                    status="deleted",
                    full_name=f"РЈС‡РµРЅСЊ {user_id}",
                )

        if key not in self.students:
            self.students[key] = StudentState(user_id=user_id, chat_id=chat_id)
            self._persist_students()

        student = self.students[key]
        student.chat_id = chat_id
        return student

    def _find_question(self, question_id: str) -> Optional[Question]:
        return next((question for question in self.questions if question.id == question_id), None)

    def _available_questions(self, topic_ids: List[str]) -> List[Question]:
        return [question for question in self.questions if question.topic_id in topic_ids]

    def _choose_question(self, topic_ids: List[str], used_ids: List[str]) -> Optional[Question]:
        available = [question for question in self.questions if question.topic_id in topic_ids and question.id not in used_ids]
        if not available:
            available = self._available_questions(topic_ids)
        return random.choice(available) if available else None

    def _build_keyboard(
        self,
        options: List[str],
        question_type: str = "single",
        selected_indexes: Optional[Set[int]] = None,
        matching_pairs: Optional[Dict[int, int]] = None,
        matching_selected_left: Optional[int] = None,
        matching_left_map: Optional[List[int]] = None,
        matching_right_map: Optional[List[int]] = None,
    ):
        keyboard = []
        selected_indexes = selected_indexes or set()
        matching_pairs = matching_pairs or {}
        matching_left_map = matching_left_map or list(range(len(options) // 2 if question_type == "matching" else len(options)))
        matching_right_map = matching_right_map or list(range(len(options) - len(matching_left_map))) if question_type == "matching" else []

        def trim_label(text: str, max_length: int = 32) -> str:
            cleaned = re.sub(r"\s+", " ", str(text)).strip()
            return cleaned if len(cleaned) <= max_length else cleaned[: max_length - 1].rstrip() + "…"

        if question_type == "matching":
            half = len(options) // 2
            left_options = [re.sub(r"^\s*[\.\-•]+\s*", "", str(option).strip()) for option in (options[:half] if half else options)]
            right_options = [re.sub(r"^\s*[\.\-•]+\s*", "", str(option).strip()) for option in (options[half:] if half else [])]
            row_count = max(len(left_options), len(right_options))
            for index in range(row_count):
                row = []
                if index < len(left_options):
                    left_mark = "✅ " if index in matching_pairs else ("👉 " if matching_selected_left == index else "")
                    left_label = f"{left_mark}{index + 1}. {trim_label(left_options[index], 22)}"
                    row.append({"text": left_label, "callback_data": f"answer:left:{index}"})
                if index < len(right_options):
                    right_mark = "✅ " if index in matching_pairs.values() else ""
                    right_label = f"{right_mark}{chr(ord('a') + index)}. {trim_label(right_options[index], 22)}"
                    row.append({"text": right_label, "callback_data": f"answer:right:{index}"})
                keyboard.append(row)
            keyboard.append([{"text": "▶️ Підтвердити вибір", "callback_data": "answer:submit"}])
            keyboard.append([{"text": "🧹 Скинути", "callback_data": "answer:reset"}])
            return {"inline_keyboard": keyboard}
        if question_type == "multi":
            for index, option in enumerate(options, start=1):
                label = f"{index}. {trim_label(option, 34)}"
                if (index - 1) in selected_indexes:
                    label = f"✅ {label}"
                keyboard.append([{"text": label, "callback_data": f"answer:{index-1}"}])
            keyboard.append([{"text": "▶️ Підтвердити вибір", "callback_data": "answer:submit"}])
        else:
            for index, option in enumerate(options, start=1):
                label = f"{index}. {trim_label(option, 34)}"
                keyboard.append([{"text": label, "callback_data": f"answer:{index-1}"}])
        return {"inline_keyboard": keyboard}

    def _get_topics(self) -> List[Topic]:
        return [topic for topic in self.topics if topic.active]

    def _topic_id_from_menu(self, topic_id: str) -> str:
        return topic_id if self._topic_by_id(topic_id) else ""

    def _normalize_topic_ids(self, topic_ids: List[str]) -> List[str]:
        """
        Normalizes a list of raw topic identifiers into valid topic_id values that:
        - exist in self.topics
        - are active
        - are unique (preserve order)
        
        Also supports legacy state where value could be stored as topic name instead of topic_id.
        """
        cleaned: List[str] = []
        if not topic_ids:
            return cleaned

        topic_by_id: Dict[str, Topic] = {t.id: t for t in self.topics}
        topic_by_name: Dict[str, Topic] = {t.name.casefold(): t for t in self.topics if t.name}

        for raw in topic_ids:
            if raw is None:
                continue
            raw_str = str(raw).strip()
            if not raw_str:
                continue

            topic = topic_by_id.get(raw_str)
            if not topic:
                topic = topic_by_name.get(raw_str.casefold())

            if topic and topic.active and topic.id not in cleaned:
                cleaned.append(topic.id)

        return cleaned

    def _add_topic(self, name: str) -> bool:
        cleaned = name.strip()
        if not cleaned:
            return False
        if self._find_topic_by_name(cleaned):
            return False
        topic_id = self._slugify_topic_id(cleaned)
        while self._topic_by_id(topic_id):
            topic_id = f"{topic_id}-{random.randint(100, 999)}"
        self.topics.append(Topic(id=topic_id, name=cleaned, order=len(self.topics), active=True))
        self._save_topics(self.topics)
        return True

    def _rename_topic(self, topic_id: str, new_name: str) -> bool:
        topic = self._topic_by_id(topic_id)
        if not topic:
            return False
        cleaned = new_name.strip()
        if not cleaned:
            return False
        topic.name = cleaned
        self._save_topics(self.topics)
        return True

    def _delete_topic(self, topic_id: str) -> int:
        topic = self._topic_by_id(topic_id)
        if not topic:
            return 0
        self.topics = [item for item in self.topics if item.id != topic_id]
        self._save_topics(self.topics)
        return 1

    def _delete_question(self, question_id: str) -> int:
        return self._delete_questions_by_filter(lambda question: question.id == question_id)

    def _delete_questions_by_filter(self, predicate) -> int:
        before = len(self.questions)
        self.questions = [question for question in self.questions if not predicate(question)]
        deleted = before - len(self.questions)
        if deleted <= 0:
            return 0
        self.questions_store.save([{"id": q.id, "topic_id": q.topic_id, "type": q.type, "question": q.question, "options": q.options, "answer": q.answer, "explanation": q.explanation} for q in self.questions])
        return deleted

    def _delete_topic_questions(self, topic_id: str) -> int:
        return self._delete_questions_by_filter(lambda question: question.topic_id == topic_id)

    def _restore_topic(self, topic_id: str) -> bool:
        topic = self._topic_by_id(topic_id)
        if not topic:
            return False
        topic.active = True
        self._save_topics(self.topics)
        return True

    def _topic_question_count(self, topic_id: str) -> int:
        return sum(1 for question in self.questions if question.topic_id == topic_id)

    def _build_questions_keyboard(self, topic_id: Optional[str] = None):
        keyboard = []
        _ = [question for question in self.questions if topic_id is None or question.topic_id == topic_id]
        keyboard.append([{"text": "⬅️ До тем", "callback_data": "admin:topics"}])
        keyboard.append([{"text": "🏠 Головне меню", "callback_data": "main_menu"}])
        return {"inline_keyboard": keyboard}

    def _build_topics_keyboard(self):
        keyboard = []
        for topic in self._get_topics():
            keyboard.append([{"text": topic.name, "callback_data": f"topic:{topic.id}"}])
        keyboard.append([{"text": "🏠 Головне меню", "callback_data": "main_menu"}])
        return {"inline_keyboard": keyboard}

    def _build_admin_keyboard(self):
        return {"inline_keyboard": [[{"text": "👥 Учні", "callback_data": "admin:students"}], [{"text": "📊 Результати", "callback_data": "admin:results"}], [{"text": "⏱ -30 с", "callback_data": "admin:time:-30"}, {"text": "⏱ +30 с", "callback_data": "admin:time:+30"}], [{"text": "📥 Імпорт DOCX", "callback_data": "admin:importdocx"}], [{"text": "⚙️ Налаштування тем", "callback_data": "admin:topics"}]]}

    def _show_admin_topics_menu(self, chat_id: int):
        keyboard = []
        for topic in self.topics:
            if topic.active:
                keyboard.append([
                    {"text": f"✏️ {topic.name}", "callback_data": f"admin:topic:edit:{self._topic_ref(topic.id)}"},
                    {"text": "Видалити тему", "callback_data": f"admin:topic:delete:{self._topic_ref(topic.id)}"},
                    {"text": "🗑 Усі питання", "callback_data": f"admin:topic:delete_questions:{self._topic_ref(topic.id)}"},
                ])
            else:
                keyboard.append([
                    {"text": f"♻️ {topic.name} (вимкнена)", "callback_data": f"admin:topic:restore:{self._topic_ref(topic.id)}"},
                    {"text": "🗑 Видалити тему", "callback_data": f"admin:topic:purge:{self._topic_ref(topic.id)}"},
                ])
        keyboard.append([{"text": "➕ Додати тему", "callback_data": "topic:add"}])
        keyboard.append([{"text": "📥 Імпорт питань у тему", "callback_data": "admin:importdocx"}])
        keyboard.append([{"text": "🏠 Головне меню", "callback_data": "main_menu"}])
        self.api.send_message(chat_id, "Керування темами:", reply_markup={"inline_keyboard": keyboard})

    def _build_students_keyboard(self):
        # Modern compact ranking (Duolingo/Quizizz style)
        keyboard_rows = []
        used_stats_service = False
        try:
            students_for_ranking = {
                k: s
                for k, s in self.students.items()
                if getattr(s, "status", "new") != "deleted" and str(getattr(s, "user_id")) not in self.deleted_student_keys
            }
            ranking_payload = self.stats_service.build_students_ranking_buttons(students_for_ranking.values())
            if isinstance(ranking_payload, dict):
                keyboard_rows = ranking_payload.get("inline_keyboard", []) or []
            elif isinstance(ranking_payload, list):
                keyboard_rows = ranking_payload
            used_stats_service = True
        except Exception:
            # Fallback to legacy list (must never break admin UI)
            keyboard_rows = []
            for student in sorted(self.students.values(), key=lambda item: item.user_id):
                status_icon = (
                    "✅" if student.status == "approved" else
                    "⏳" if student.status == "pending_approval" else
                    "🚫" if student.status == "blocked" else
                    "📝"
                )
                total_tests = self.results_store.user_result_count(student.user_id)
                name_label = (
                    f"{student.full_name or 'Без імені'} | "
                    f"📚 {total_tests} | {status_icon}"
                )
                keyboard_rows.append([{"text": name_label, "callback_data": f"student:view:{student.user_id}"}])

        # StatsService.build_students_ranking_buttons() already includes main menu row.
        if not used_stats_service:
            keyboard_rows.append([{"text": "🏠 Головне меню", "callback_data": "main_menu"}])

        return {"inline_keyboard": keyboard_rows}

    def _show_student_details(self, chat_id: int, student: StudentState):
        # New modern profile card
        full_name = (
            student.full_name
            or f"{student.first_name} {student.last_name}".strip()
            or str(student.user_id)
        )

        try:
            analytics = self.stats_service.compute_student_analytics(
                user_id=student.user_id,
                full_name=full_name,
                status=student.status,
            )
            text = self.stats_service.format_student_profile(analytics)
        except Exception:
            # Fallback to legacy (never break)
            total_tests = self.results_store.user_result_count(student.user_id)
            lines = [
                "📌 Карточка учня:",
                f"• Ім'я: {full_name}",
                f"• ID: {student.user_id}",
                f"• Тестів: {total_tests}",
            ]
            text = "\n".join(lines)

        keyboard = {"inline_keyboard": []}
        if student.status == "approved":
            keyboard["inline_keyboard"].append(
                [{"text": "🚫 Заблокувати", "callback_data": f"student:block:{student.user_id}"}]
            )
        else:
            keyboard["inline_keyboard"].append(
                [{"text": "✅ Схвалити", "callback_data": f"student:approve:{student.user_id}"}]
            )

        keyboard["inline_keyboard"].append(
            [{"text": "🗑 Видалити учня", "callback_data": f"student:delete:{student.user_id}"}]
        )
        keyboard["inline_keyboard"].append(
            [{"text": "⬅️ До списку учнів", "callback_data": "admin:students"}]
        )
        keyboard["inline_keyboard"].append(
            [{"text": "🏠 Головне меню", "callback_data": "main_menu"}]
        )

        self.api.send_message(chat_id, text, reply_markup=keyboard)

    def _build_post_test_keyboard(self):
        return {"inline_keyboard": [[{"text": "🔁 Пройти ще раз", "callback_data": "restart_test"}], [{"text": "⬅️ До тем", "callback_data": "back_to_topics"}], [{"text": "🏠 Головне меню", "callback_data": "main_menu"}]]}

    def _build_back_to_main_keyboard(self):
        return {"inline_keyboard": [[{"text": "🏠 Головне меню", "callback_data": "main_menu"}]]}

    def _build_import_topics_keyboard(self):
        keyboard = []
        for topic in self._get_topics():
            keyboard.append([{"text": topic.name, "callback_data": f"import_topic:{topic.id}"}])
        keyboard.append([{"text": "🏠 Головне меню", "callback_data": "main_menu"}])
        return {"inline_keyboard": keyboard}

    def _send_chunked_message(self, chat_id: int, text: str, reply_markup: Optional[dict] = None):
        chunks = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > 3500 and current:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}".strip("\n") if current else line
        if current:
            chunks.append(current)
        for index, chunk in enumerate(chunks):
            self.api.send_message(chat_id, chunk, reply_markup=reply_markup if index == len(chunks) - 1 else None)

    def _format_duration(self, total_seconds: int) -> str:
        minutes, seconds = divmod(max(0, total_seconds), 60)
        return f"{minutes} хв {seconds} с" if seconds else f"{minutes} хв"

    def _show_main_menu(self, chat_id: int, user_id: int):
        is_admin = user_id in self.admin_user_ids
        self.api.send_message(chat_id, f"Головне меню:\nПоточний час тесту: {self._format_duration(self.test_duration_seconds)}.", reply_markup=self._build_main_menu_keyboard(is_admin))

    def _build_main_menu_keyboard(self, is_admin: bool):
        keyboard = []
        if is_admin:
            keyboard.extend([[{"text": "👥 Учні", "callback_data": "admin:students"}], [{"text": "📊 Результати", "callback_data": "admin:results"}], [{"text": "⏱ -30 с", "callback_data": "admin:time:-30"}, {"text": "⏱ +30 с", "callback_data": "admin:time:+30"}], [{"text": "⚙️ Налаштування тем", "callback_data": "admin:topics"}]])
        keyboard.append([{"text": "📚 Пройти Тести", "callback_data": "back_to_topics"}])
        return {"inline_keyboard": keyboard}

    def _time_is_up(self, student: StudentState) -> bool:
        return student.current_test_started_at is not None and student.current_test_duration_seconds is not None and (time.time() - float(student.current_test_started_at)) >= float(student.current_test_duration_seconds)

    def _finish_test_due_to_timeout(self, student: StudentState):
        student.current_question_id = None
        student.current_question_message_id = None
        student.current_test_topic_id = None
        student.current_test_score = 0
        student.current_test_started_at = None
        student.current_test_duration_seconds = None
        student.matching_pairs = {}
        student.matching_selected_left = None
        student.shuffled_options = []
        student.shuffled_matching_left = []
        student.shuffled_matching_right = []
        self._persist_students()
        self.api.send_message(student.chat_id, "⏰ Час тесту вийшов. Тест завершено.", reply_markup=self._build_post_test_keyboard())

    def _handle_start(self, message: dict):
        chat = message["chat"]
        user = message["from"]
        user_id = user["id"]

        # remove tombstone on /start and switch deleted->new
        key = self._student_key(user_id)
        if key in getattr(self, "deleted_student_keys", set()):
            self.deleted_student_keys.remove(key)
            self._persist_state()

        student = self._get_student(user_id, chat["id"])

        # /start should reset any "awaiting ..." admin workflows,
        # so we don't end up showing stale "theme not found" messages.
        student.awaiting_docx_import = False
        student.awaiting_docx_topic_id = None
        student.awaiting_topic_action = False
        student.topic_action_mode = None
        student.topic_action_source = None
        student.awaiting_delete_action = False
        student.delete_action_mode = None
        student.delete_action_source = None
        self._persist_students()

        if student.status == "deleted":
            student.status = "new"
            self._persist_students()

        if student.status in {"new", "awaiting_name"}:
            # Restart onboarding: clear possibly stale names from previous partial state.
            student.last_name = ""
            student.first_name = ""
            student.full_name = ""

            student.awaiting_name = True
            student.status = "awaiting_name"
            self._persist_students()
            self.api.send_message(chat["id"], "Введи прізвище.")
            return
        if student.status != "approved":
            self.api.send_message(chat["id"], "Твоя анкета ще не схвалена адміністрацією.")
            return
        self._show_main_menu(chat["id"], user["id"])

    def _handle_text(self, message: dict):
        chat = message["chat"]
        user = message["from"]
        text = message.get("text", "").strip()
        student = self._get_student(user["id"], chat["id"])

        try:
            user_id_int = int(user["id"])
        except (TypeError, ValueError):
            user_id_int = None

        if text.startswith("/start"):
            self._handle_start(message)
            return

        if user_id_int is not None and user_id_int in self.admin_user_ids and student.awaiting_topic_action:
            mode = student.topic_action_mode
            source = student.topic_action_source

            if text.startswith("/"):
                self.api.send_message(chat["id"], "Команда не може бути назвою теми.", reply_markup=self._build_back_to_main_keyboard())
                return

            student.awaiting_topic_action = False
            student.topic_action_mode = None
            student.topic_action_source = None
            self._persist_students()

            if mode == "rename" and source:
                if self._rename_topic(source, text):
                    self.api.send_message(chat["id"], f"Тему перейменовано на «{text}».", reply_markup=self._build_back_to_main_keyboard())
                    self._show_admin_topics_menu(chat["id"])
                else:
                    self.api.send_message(chat["id"], "Не вдалося перейменувати тему.", reply_markup=self._build_back_to_main_keyboard())
                return

            if self._add_topic(text):
                self.api.send_message(chat["id"], f"Тему «{text}» додано.", reply_markup=self._build_back_to_main_keyboard())
                self._show_admin_topics_menu(chat["id"])
            else:
                self.api.send_message(chat["id"], "Тема вже існує або порожня.", reply_markup=self._build_back_to_main_keyboard())
            return

        if student.awaiting_docx_topic_id and user["id"] in self.admin_user_ids:
            topic = self._topic_by_id(student.awaiting_docx_topic_id)
            if not topic or not topic.active:
                self.api.send_message(chat["id"], "Тему не знайдено.")
                return
            student.awaiting_docx_import = True
            self._persist_students()
            self.api.send_message(chat["id"], "Надішли .docx файлом. Питання будуть імпортовані у вибрану тему.")
            return

        if student.awaiting_name:
            # If user sends a command like /start while onboarding expects a name,
            # don't treat it as a surname and don't persist it.
            if text.startswith("/"):
                self.api.send_message(chat["id"], "Команда не може бути назвою. Введи прізвище.")
                return

            cleaned = " ".join(text.split())
            if not cleaned:
                self.api.send_message(chat["id"], "Введи текст.")
                return

            # Step 1 (surname)
            if not student.last_name:
                student.last_name = " ".join(word.capitalize() for word in cleaned.split())
                self._persist_students()
                self.api.send_message(chat["id"], "Введи ім'я.")
                return

            # Step 2 (given name)
            student.first_name = " ".join(word.capitalize() for word in cleaned.split())
            student.full_name = f"{student.last_name} {student.first_name}".strip()
            student.awaiting_name = False
            if user_id_int is not None and user_id_int in self.admin_user_ids:
                student.status = "approved"
                self.api.send_message(chat["id"], "Запит прийнято. Твій анкетний запис схвалено адміністрацією.", reply_markup=self._build_back_to_main_keyboard())
            else:
                student.status = "pending_approval"
                self.api.send_message(chat["id"], "Запит прийнято. Адміністрація розгляне твою заявку та надішле відповідь.", reply_markup=self._build_back_to_main_keyboard())
                self._notify_admins(
                    f"🆕 Нова заявка на схвалення: {student.full_name or f'Учень {student.user_id}'} (ID: {student.user_id})",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": "✅ Схвалити", "callback_data": f"student:approve:{student.user_id}"}],
                            [{"text": "🚫 Відхилити", "callback_data": f"student:block:{student.user_id}"}],
                        ]
                    },
                )
            self._persist_students()
            return

        if student.current_question_id:
            question = self._find_question(student.current_question_id)
            if not question:
                self.api.send_message(chat["id"], "Питання не знайдено.")
                return
            if question.type == "matching":
                self._grade_matching_question(student, self._parse_matching_answer(text))
            else:
                self.api.send_message(chat["id"], "Для відповіді користуйся кнопками.")
            return

        self.api.send_message(chat["id"], "Натисни /start, щоб почати роботу.")

    def _handle_document(self, message: dict):
        chat = message["chat"]
        user = message["from"]
        student = self._get_student(user["id"], chat["id"])
        document = message.get("document")
        if not document:
            return
        if user["id"] not in self.admin_user_ids:
            self.api.send_message(chat["id"], "Документ можуть імпортувати лише адміністратори.")
            return
        if not student.awaiting_docx_import:
            self.api.send_message(chat["id"], "Спочатку обери тему для імпорту DOCX.")
            return
        file_name = document.get("file_name", "")
        if not file_name.lower().endswith(".docx"):
            self.api.send_message(chat["id"], "Потрібен файл .docx.")
            return
        downloaded = self._download_telegram_file(document["file_id"])
        if downloaded is None:
            self.api.send_message(chat["id"], "Не вдалося завантажити файл.")
            return

        added = 0
        imported: List[Question] = []
        try:
            imported = self._load_questions_from_uploaded_docx(downloaded, topic_id=student.awaiting_docx_topic_id or "")
            added, _ = self._save_imported_questions(imported)
        except Exception as exc:
            self.api.send_message(chat["id"], f"Помилка імпорту DOCX: {exc}")
            return
        finally:
            try:
                downloaded.unlink(missing_ok=True)
            except OSError:
                pass

        student.awaiting_docx_import = False
        student.awaiting_docx_topic_id = None
        self._persist_students()

        if added == 0:
            self.api.send_message(chat["id"], f"Файл «{file_name}» отримано, але придатних питань не знайдено.")
            return

        self.api.send_message(
            chat["id"],
            f"Імпорт із «{file_name}» завершено.\nЗнайдено питань: {len(imported)}.\nДодано питань: {added}.",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "⬅️ До тем", "callback_data": "back_to_topics"}],
                    [{"text": "🏠 Головне меню", "callback_data": "main_menu"}],
                ]
            },
        )

    def _load_questions_from_uploaded_docx(self, path: Path, topic_id: str = "") -> List[Question]:
        return self._parse_docx_questions(self._extract_docx_text(path), topic_id=topic_id)

    def _send_admin_help(self, chat_id: int):
        lines = [
            "Команди адміністратора:",
            "/students — список учнів",
            "/approve <user_id> — надати доступ",
            "/results — усі результати",
            "/results <user_id> — результати конкретного учня",
            "/settime <minutes> — встановити час тесту",
            f"Поточний час тесту: {self._format_duration(self.test_duration_seconds)}.",
            "Для імпорту: спочатку вибери тему в меню тем, потім надішли DOCX.",
        ]
        self.api.send_message(chat_id, "\n".join(lines))

    def _send_results_report(
        self,
        chat_id: int,
        target_user_id: Optional[int] = None,
        page: int = 0,
        limit: int = 15,
    ):
        """
        Admin "results" dashboard:
        - Global (target_user_id=None): weak topics + weak students summary (dashboard) with pagination over topics
        - Per-user (target_user_id=<id>): compact last results with percent
        """
        limit = max(1, int(limit))
        page = max(0, int(page))

        def score_emoji(score_value: int) -> str:
            if int(score_value) >= 7:
                return "🟢"
            if 4 <= int(score_value) <= 6:
                return "🟡"
            return "🔴"

        def score_percent(score_value: int) -> int:
            # score is number correct out of DEFAULT_TEST_LENGTH (usually 10)
            denom = max(1, DEFAULT_TEST_LENGTH)
            return int(round((float(score_value) / float(denom)) * 100.0))

        if target_user_id is None:
            all_rows = self.results_store.list_results()
            if not all_rows:
                self.api.send_message(
                    chat_id,
                    "📊 Дашборд результатів\n\nНемає результатів.",
                    reply_markup=self._build_back_to_main_keyboard(),
                )
                return

            # ---- Aggregate topics globally ----
            topic_aggs: Dict[str, Dict[str, float]] = {}
            for _rid, _uid, _full_name, score, _attempts, topic_id, topic_name, _created_at in all_rows:
                tid = topic_id or ""
                if not tid:
                    continue
                key = tid
                if key not in topic_aggs:
                    topic_aggs[key] = {"sum": 0.0, "count": 0.0}
                topic_aggs[key]["sum"] += float(score)
                topic_aggs[key]["count"] += 1.0

            topic_list: List[Tuple[str, str, float, int]] = []
            for tid, agg in topic_aggs.items():
                count = int(agg["count"])
                if count <= 0:
                    continue
                avg = float(agg["sum"]) / float(count)
                label = (self._topic_name(tid) if tid else "—")
                topic_list.append((tid, label, avg, count))

            topic_list.sort(key=lambda x: (x[2], -x[3], x[1].lower()))  # weak -> strong
            total_topics = len(topic_list)
            page_count = (total_topics + limit - 1) // limit
            page = min(page, max(0, page_count - 1))
            start = page * limit
            shown_topics = topic_list[start : start + limit]

            # ---- Weak students summary ----
            # group by user_id across all_rows
            student_aggs: Dict[int, Dict[str, float]] = {}
            student_names: Dict[int, str] = {}
            for _rid, uid, full_name, score, _attempts, _topic_id, _topic_name, _created_at in all_rows:
                uid_int = int(uid)
                # If admin deleted this student (tombstone), don't include it in ranking lists.
                if str(uid_int) in getattr(self, "deleted_student_keys", set()):
                    continue

                student_names[uid_int] = full_name or student_names.get(uid_int, "") or str(uid_int)
                if uid_int not in student_aggs:
                    student_aggs[uid_int] = {"sum": 0.0, "count": 0.0}
                student_aggs[uid_int]["sum"] += float(score)
                student_aggs[uid_int]["count"] += 1.0

            students_list: List[Tuple[int, str, float, int]] = []
            for uid, agg in student_aggs.items():
                cnt = int(agg["count"])
                if cnt <= 0:
                    continue
                avg = float(agg["sum"]) / float(cnt)
                students_list.append((uid, student_names.get(uid, f"Учень {uid}"), avg, cnt))

            # weak students -> low avg first
            # "weak students" = ті, хто:
            # 1) не проходив тести (tests_count = 0)
            # 2) або має дуже низький avg (avg <= 3/10)
            # Візуально це відповідає легенді 🔴, а не "топ-10 найнижчих серед тих, хто має результати".
            approved_students = [
                s
                for s in self.students.values()
                if getattr(s, "status", "new") == "approved"
                and str(getattr(s, "user_id")) not in getattr(self, "deleted_student_keys", set())
            ]
            approved_student_ids = {int(s.user_id) for s in approved_students}
            student_name_map = {
                int(s.user_id): (
                    (s.full_name if s.full_name and s.full_name not in {"Без імені", "Без имени"} else "")
                    or f"{s.first_name} {s.last_name}".strip()
                    or f"Учень {s.user_id}"
                )
                for s in approved_students
            }

            passed_ids = {t[0] for t in students_list}
            ordered_students = sorted(students_list, key=lambda x: (x[2], -x[3], x[1].lower()))

            not_passed_students: List[Tuple[int, str, float, int]] = []
            for uid in approved_student_ids:
                if uid not in passed_ids:
                    not_passed_students.append((uid, student_name_map.get(uid, f"Учень {uid}"), 0.0, 0))

            low_students = [t for t in ordered_students if t[2] < 4.0]

            weak_students = (not_passed_students + low_students)[:10]


            legend = "🟢 7-10/10 — добре; 🟡 4-7/10 — середній рівень; 🔴 avg<4/10 — потрібно підтягнути. Також показані ті, хто НЕ проходив тести."
            # counts for "учнів у групах"
            total_users_in_results = len(student_aggs)
            lines = [
                f"📊 Аналіз результатів: лідери та підтримка",
                legend,
                "",
                f"👥 Показано: ? / {total_users_in_results} (учні з results.db)",
                "",
                f"📖 Найслабші теми (сторінка {page+1}/{page_count})",
            ]
            if not shown_topics:
                lines.append("—")
            for idx, (_tid, label, avg, cnt) in enumerate(shown_topics, start=1 + start):
                avg_int = int(round(avg))
                emoji = score_emoji(avg_int)
                lines.append(f"{idx}) {label} — {avg:.1f}/{DEFAULT_TEST_LENGTH} {emoji} (спроб: {cnt})")

            lines.append("")
            # 3 groups: leaders -> mid -> help-needed (each up to 10 rows)
            leaders_students = [t for t in ordered_students if t[2] >= 7.0]
            leaders_students = sorted(leaders_students, key=lambda x: (-x[2], -x[3], x[1].lower()))[:10]

            mid_students = [t for t in ordered_students if 4.0 <= t[2] < 7.0]
            mid_students = sorted(mid_students, key=lambda x: (-x[2], -x[3], x[1].lower()))[:10]

            help_students = weak_students[:10]

            # fill in "Показано" line (leaders/mid/help must exist first)
            shown_in_groups = len(leaders_students) + len(mid_students) + len(help_students)
            for i, line in enumerate(lines):
                if line.startswith("👥 Показано:"):
                    lines[i] = f"👥 Показано: {shown_in_groups} / {total_users_in_results} (учні з results.db)"
                    break

            lines.append("🏆 Лідери")
            for rank, (uid, nm, avg, cnt) in enumerate(leaders_students, start=1):
                avg_int = int(round(avg))
                emoji = score_emoji(avg_int)
                acc = score_percent(avg_int)
                lines.append(f"{rank}) {nm} — {avg:.1f}/{DEFAULT_TEST_LENGTH} {emoji} — 🎯{acc}% (тести: {cnt})")

            lines.append("")
            if mid_students:
                lines.append("🟡 Середній рівень")
            for rank, (uid, nm, avg, cnt) in enumerate(mid_students, start=1):
                avg_int = int(round(avg))
                emoji = score_emoji(avg_int)
                acc = score_percent(avg_int)
                lines.append(f"{rank}) {nm} — {avg:.1f}/{DEFAULT_TEST_LENGTH} {emoji} — 🎯{acc}% (тести: {cnt})")

            lines.append("")
            lines.append("👥 Учні, яким потрібна допомога")
            for rank, (uid, nm, avg, cnt) in enumerate(help_students, start=1):
                avg_int = int(round(avg))
                emoji = score_emoji(avg_int)
                acc = score_percent(avg_int)
                lines.append(f"{rank}) {nm} — {avg:.1f}/{DEFAULT_TEST_LENGTH} {emoji} — 🎯{acc}% (тести: {cnt})")

            # pagination buttons for global topics
            inline_keyboard = []
            nav = []
            if page > 0:
                nav.append({"text": "⬅️", "callback_data": f"admin:results:page:{page-1}"})
            if page + 1 < page_count:
                nav.append({"text": "➡️", "callback_data": f"admin:results:page:{page+1}"})
            if nav:
                inline_keyboard.append(nav)

            inline_keyboard.append([{"text": "🏠 Головне меню", "callback_data": "main_menu"}])
            self.api.send_message(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": inline_keyboard})
            return

        # ---- per-user: keep list but add percent ----
        rows = self.results_store.user_results(target_user_id)
        if not rows:
            student = self.students.get(str(target_user_id))
            student_name = (
                getattr(student, "full_name", "").strip()
                or f"{getattr(student, 'first_name', '').strip()} {getattr(student, 'last_name', '').strip()}".strip()
                or str(target_user_id)
            ) if student else str(target_user_id)

            self.api.send_message(
                chat_id,
                f"📊 Результати учня {student_name}\n\nНе здавав тести: 0 спроб.",
                reply_markup=self._build_back_to_main_keyboard(),
            )
            return

        full_name = rows[0][2] or f"Учень {target_user_id}"
        total = len(rows)
        page_count = (total + limit - 1) // limit
        page = min(page, max(0, page_count - 1))
        start = page * limit
        shown = rows[start : start + limit]

        lines = [f"📊 Останні результати: {full_name} (сторінка {page+1}/{page_count})"]
        for _result_id, _uid, _nm, score, _attempts, topic_id, topic_name, created_at in shown:
            score_int = int(score)
            emoji = score_emoji(score_int)
            percent = score_percent(score_int)
            date_short = str(created_at)[:10].replace("-", ".") if created_at else "—"
            t_label = topic_name or self._topic_name(topic_id)
            lines.append(f"{emoji} {date_short} — {t_label} — {score_int}/{DEFAULT_TEST_LENGTH} ({percent}%)")

        inline_keyboard = []
        nav = []
        if page > 0:
            nav.append({"text": "⬅️", "callback_data": f"admin:results:user:{target_user_id}:page:{page-1}"})
        if page + 1 < page_count:
            nav.append({"text": "➡️", "callback_data": f"admin:results:user:{target_user_id}:page:{page+1}"})
        if nav:
            inline_keyboard.append(nav)

        inline_keyboard.append([{"text": "⬅️ До учнів", "callback_data": "admin:students"}])
        inline_keyboard.append([{"text": "🏠 Головне меню", "callback_data": "main_menu"}])

        self.api.send_message(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": inline_keyboard})

    def _start_test(self, student: StudentState, topic_id: Optional[str] = None):
        student.awaiting_topic_action = False
        student.topic_action_mode = None
        student.topic_action_source = None
        student.awaiting_delete_action = False
        student.delete_action_mode = None
        student.delete_action_source = None
        student.matching_pairs = {}
        student.matching_selected_left = None

        # Guard: не дозволяємо стартувати тест, якщо студент не схвалений
        # (без винятків для адмінів — адмін має пройти реєстрацію як учень)
        if student.status != "approved":
            student.current_test = []
            student.current_index = 0
            student.current_question_id = None
            student.current_question_message_id = None
            self._persist_students()
            self.api.send_message(student.chat_id, "Доступ заборонено")
            return

        # Resolve topic_id safely:
        # - prefer explicit topic_id if it exists in self.topics
        # - otherwise use student.selected_topic_ids
        # - otherwise fallback to DEFAULT_TEST_TOPIC_NAME (mapped to its topic.id)
        # This prevents "topic name/id" mixups causing tests to start from a wrong topic.
        default_topic = self._find_topic_by_name(DEFAULT_TEST_TOPIC_NAME)
        default_topic_id = default_topic.id if default_topic else (self.topics[0].id if self.topics else None)

        resolved_topic_id: Optional[str] = None
        if topic_id and self._topic_by_id(topic_id):
            resolved_topic_id = topic_id
        elif topic_id:
            matched_by_name = self._find_topic_by_name(str(topic_id))
            resolved_topic_id = matched_by_name.id if matched_by_name else None

        if resolved_topic_id:
            topic_ids = [resolved_topic_id]
        else:
            topic_ids = student.selected_topic_ids or ([default_topic_id] if default_topic_id else [])

        available = self._available_questions(topic_ids)
        if not available:
            self.api.send_message(student.chat_id, "Немає питань для обраної теми.")
            return
        student.current_test = []
        student.pending_multi_answers = []
        student.score = 0
        student.current_test_score = 0
        student.current_test_topic_id = topic_ids[0]
        student.current_test_started_at = time.time()
        student.current_test_duration_seconds = self.test_duration_seconds
        used_ids: List[str] = []
        accuracy = self.results_store.topic_accuracy(student.user_id)
        bias_topics = sorted(topic_ids, key=lambda tid: accuracy.get(tid, 0.0))
        for _ in range(DEFAULT_TEST_LENGTH):
            question = self._choose_question(bias_topics, used_ids)
            if not question:
                break
            student.current_test.append(question.id)
            used_ids.append(question.id)
        if not student.current_test:
            self.api.send_message(student.chat_id, "Не вдалося сформувати тест.")
            return
        student.current_index = 0
        student.current_question_id = student.current_test[0]
        self._persist_students()
        self._send_current_question(student)

    def _send_current_question(self, student: StudentState):
        question = self._find_question(student.current_question_id or "")
        if not question:
            self.api.send_message(student.chat_id, "Помилка вибору питання.")
            return
        if self._time_is_up(student):
            self._finish_test_due_to_timeout(student)
            return
        text = f"Питання {student.current_index + 1}/{len(student.current_test)}\n\n{question.question}"
        if student.current_test_started_at is not None and student.current_test_duration_seconds is not None:
            elapsed = int(time.time() - float(student.current_test_started_at))
            remaining = max(0, int(student.current_test_duration_seconds) - elapsed)
            text += f"\n⏳ Залишилось часу: {remaining // 60} хв {remaining % 60} с"
        if question.type == "matching":
            half = len(question.options) // 2
            left_options = question.options[:half] if half else question.options
            right_options = question.options[half:] if half else []
            student.shuffled_matching_left = random.sample(list(range(len(left_options))), len(left_options)) if left_options else []
            student.shuffled_matching_right = random.sample(list(range(len(right_options))), len(right_options)) if right_options else []
            shuffled_left_options = [left_options[index] for index in student.shuffled_matching_left] if left_options else []
            shuffled_right_options = [right_options[index] for index in student.shuffled_matching_right] if right_options else []
            block_lines = ["Ліва колонка:"]
            block_lines.extend([f"{i + 1}. {opt}" for i, opt in enumerate(shuffled_left_options)])
            block_lines.append("")
            block_lines.append("Права колонка:")
            block_lines.extend([f"{chr(ord('a') + i)}. {opt}" for i, opt in enumerate(shuffled_right_options)])
            text += "\n\nЗістав пари: натискай спочатку лівий номер, потім праву букву. Пару можна змінювати до підтвердження.\n\n" + "\n".join(block_lines)
            student.matching_pairs = {}
            student.matching_selected_left = None
            sent_message = self.api.send_message(
                student.chat_id,
                text,
                reply_markup=self._build_keyboard(
                    shuffled_left_options + shuffled_right_options,
                    question_type="matching",
                    matching_pairs=student.matching_pairs,
                    matching_selected_left=student.matching_selected_left,
                ),
            )
            if isinstance(sent_message, dict) and "message_id" in sent_message:
                student.current_question_message_id = sent_message["message_id"]
                self._persist_students()
            return
        if question.type == "multi":
            student.shuffled_options = random.sample(list(range(len(question.options))), len(question.options)) if question.options else []
            shuffled_options = [question.options[index] for index in student.shuffled_options]
            text += "\n\nВибрано: нічого"
            sent_message = self.api.send_message(student.chat_id, text, reply_markup=self._build_keyboard(shuffled_options, question_type=question.type, selected_indexes={student.shuffled_options.index(i) for i in student.pending_multi_answers if i in student.shuffled_options}))
        else:
            student.shuffled_options = random.sample(list(range(len(question.options))), len(question.options)) if question.options else []
            shuffled_options = [question.options[index] for index in student.shuffled_options]
            sent_message = self.api.send_message(student.chat_id, text, reply_markup=self._build_keyboard(shuffled_options, question_type=question.type, selected_indexes=None))
        if isinstance(sent_message, dict) and "message_id" in sent_message:
            student.current_question_message_id = sent_message["message_id"]
            self._persist_students()

    def _check_answer(self, question: Question, answer_indexes: Set[int]) -> bool:
        return set(question.answer) == answer_indexes

    def _parse_matching_answer(self, text: str) -> List[Tuple[int, int]]:
        pairs = []
        normalized = text.lower().replace(";", ",").replace(" ", "")
        alphabet = "abcdefghijklmnopqrstuvwxyzабвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
        for chunk in normalized.split(","):
            match = re.match(r"^(\d+)([a-zа-яіїєґ])$", chunk)
            if not match:
                match = re.match(r"^(\d+)[-=:](\d+|[a-zа-яіїєґ])$", chunk)
            if not match:
                continue
            left_raw, right_raw = match.groups()
            left = int(left_raw)
            if right_raw.isdigit():
                right = int(right_raw)
            else:
                right = alphabet.find(right_raw)
                if right >= 26:
                    right -= 26
                right += 1
            if left > 0 and right > 0:
                pairs.append((left, right))
        return pairs

    def _grade_question(self, student: StudentState, selected_indexes: Set[int]):
        question = self._find_question(student.current_question_id or "")
        if not question:
            self.api.send_message(student.chat_id, "Питання не знайдено.")
            return
        correct = self._check_answer(question, selected_indexes)
        student.total_attempts += 1
        stats = student.topic_stats.setdefault(question.topic_id, {"correct": 0, "total": 0})
        stats["total"] += 1
        selected_text = ", ".join(question.options[index] for index in sorted(selected_indexes) if 0 <= index < len(question.options)) or "без вибору"
        correct_text = ", ".join(question.options[index] for index in question.answer if 0 <= index < len(question.options)) or "немає даних"
        if correct:
            student.score += 1
            student.current_test_score += 1
            stats["correct"] += 1
            self.api.send_message(student.chat_id, f"🟩 Правильно.\nТвоя відповідь: {selected_text}\n\n{question.explanation}")
        else:
            self.api.send_message(student.chat_id, f"🟥 Неправильно.\nТвоя відповідь: {selected_text}\nПравильна відповідь: 🟩 {correct_text}\n\n{question.explanation}")
        self._advance_after_answer(student, question)

    def _grade_matching_question(self, student: StudentState, pairs: List[Tuple[int, int]]):
        question = self._find_question(student.current_question_id or "")
        if not question:
            self.api.send_message(student.chat_id, "Питання не знайдено.")
            return

        def letter(value: int) -> str:
            alphabet = "abcdefghijklmnopqrstuvwxyz"
            return alphabet[value - 1] if 1 <= value <= len(alphabet) else str(value)

        expected_pairs = {(index + 1, value + 1) for index, value in enumerate(question.answer)}
        provided_pairs = {(left, right) for left, right in pairs if left > 0 and right > 0}
        correct = provided_pairs == expected_pairs

        student.total_attempts += 1
        stats = student.topic_stats.setdefault(question.topic_id, {"correct": 0, "total": 0})
        stats["total"] += 1

        if correct:
            student.score += 1
            student.current_test_score += 1
            stats["correct"] += 1
            provided_text = ", ".join(f"{left}{letter(right)}" for left, right in sorted(provided_pairs)) or "без вибору"
            self.api.send_message(student.chat_id, f"🟩 Правильно.\nТвоя відповідь: {provided_text}\n\n{question.explanation}")
        else:
            expected_text = ", ".join(f"{left}{letter(right)}" for left, right in sorted(expected_pairs)) or "немає даних"
            provided_text = ", ".join(f"{left}{letter(right)}" for left, right in sorted(provided_pairs)) or "без вибору"
            self.api.send_message(student.chat_id, f"🟥 Неправильно.\nТвоя відповідь: {provided_text}\nПравильна відповідь: 🟩 {expected_text}\n\n{question.explanation}")

        self._advance_after_answer(student, question)

    def _advance_after_answer(self, student: StudentState, question: Question):
        student.current_index += 1
        student.pending_multi_answers = []
        student.matching_pairs = {}
        student.matching_selected_left = None
        student.shuffled_options = []
        student.shuffled_matching_left = []
        student.shuffled_matching_right = []
        if student.current_index >= len(student.current_test):
            topic_id = student.current_test_topic_id or question.topic_id
            attempts = self.results_store.next_attempt_number(student.user_id, topic_id)
            self.results_store.add_result(
                student.user_id,
                student.full_name or f"{student.first_name} {student.last_name}".strip() or str(student.user_id),
                student.current_test_score,
                attempts,
                topic_id=topic_id,
                topic_name=self._topic_name(topic_id),
                questions_count=len(student.current_test) if isinstance(student.current_test, list) else DEFAULT_TEST_LENGTH,
            )
            student.current_question_id = None
            student.current_question_message_id = None
            student.current_test_topic_id = None
            student.current_test_score = 0
            self._persist_students()
            self.api.send_message(student.chat_id, f"Тест завершено. Твої бали: {student.score}", reply_markup=self._build_post_test_keyboard())
            return
        student.current_question_id = student.current_test[student.current_index]
        student.current_question_message_id = None
        self._persist_students()
        time.sleep(1)
        self._send_current_question(student)

    def _handle_callback(self, callback_query: dict):
        user = callback_query["from"]
        message = callback_query.get("message")
        if not message:
            self.api.answer_callback_query(callback_query["id"], "")
            return
        chat_id = message["chat"]["id"]
        data = callback_query.get("data", "")
        student = self._get_student(user["id"], chat_id)

        if data.startswith("admin:"):
            try:
                user_id_int = int(user["id"])
            except (TypeError, ValueError):
                self.api.answer_callback_query(callback_query["id"], "РќРµРјР°С” РР”")
                return
            is_admin = user_id_int in self.admin_user_ids
            if user_id_int not in self.admin_user_ids:
                self.api.answer_callback_query(callback_query["id"], "Р СњР ВµР СР В°РЎвЂќ Р С—РЎР‚Р В°Р Р†")
                return
            action = data.split(":", 1)[1].strip()
            if action == "students":
                self._handle_admin_command({"chat": message["chat"], "from": user, "text": "/students"})
                self.api.answer_callback_query(callback_query["id"], "Відкрито список учнів")
                return

            # Results: supports pagination
            # admin:results -> page 0
            # admin:results:page:<n> -> global results page n
            # admin:results:user:<user_id> -> user results page 0
            # admin:results:user:<user_id>:page:<n> -> user results page n
            if action == "results":
                self._send_results_report(chat_id, target_user_id=None, page=0)
                self.api.answer_callback_query(callback_query["id"], "Відкрито результати")
                return

            if action.startswith("results:page:"):
                try:
                    page = int(action.split(":", 2)[2])
                except (IndexError, ValueError):
                    page = 0
                self._send_results_report(chat_id, target_user_id=None, page=page)
                self.api.answer_callback_query(callback_query["id"], "Перегляд сторінки")
                return

            if action.startswith("results:user:"):
                parts = action.split(":")
                # results:user:<user_id>[:page:<n>]
                if len(parts) >= 3:
                    user_part = parts[2]
                    try:
                        target_user_id = int(user_part)
                    except ValueError:
                        self.api.answer_callback_query(callback_query["id"], "Невірний user_id")
                        return

                    page = 0
                    if len(parts) >= 5 and parts[3] == "page":
                        try:
                            page = int(parts[4])
                        except (ValueError, IndexError):
                            page = 0

                    self._send_results_report(chat_id, target_user_id=target_user_id, page=page)
                    self.api.answer_callback_query(callback_query["id"], "Перегляд результатів")
                    return

            if action.startswith("time:"):
                time_action = action.split(":", 1)[1]
                if time_action == "show":
                    self.api.send_message(chat_id, f"Поточний час тесту: {self._format_duration(self.test_duration_seconds)}.")
                    self.api.answer_callback_query(callback_query["id"], "Показано час")
                    return
                deltas = {"-30": -30, "+30": 30}
                if time_action not in deltas:
                    self.api.answer_callback_query(callback_query["id"], "Невідома дія")
                    return
                self.test_duration_seconds = max(10, self.test_duration_seconds + deltas[time_action])
                self._persist_state()
                self.api.send_message(chat_id, f"Час тесту оновлено: {self._format_duration(self.test_duration_seconds)}.")
                self.api.answer_callback_query(callback_query["id"], "Час оновлено")
                return
            if action == "topics":
                try:
                    self._show_admin_topics_menu(chat_id)
                except Exception as e:
                    print(f"[ERROR] _show_admin_topics_menu failed: {e}")
                    raise
                self.api.answer_callback_query(callback_query["id"], "Відкрито теми")
                return
            if action == "importdocx":
                student.awaiting_docx_import = False
                student.awaiting_docx_topic_id = None
                self._persist_students()
                self.api.send_message(chat_id, "Оберіть тему для імпорту DOCX:", reply_markup=self._build_import_topics_keyboard())
                self.api.answer_callback_query(callback_query["id"], "Оберіть тему")
                return
            if action.startswith("topic:edit:"):
                topic_ref = action.split(":", 2)[2]
                resolved_topic_id = self._resolve_topic_id(topic_ref)
                if not resolved_topic_id:
                    self.api.answer_callback_query(callback_query["id"], "Тему не знайдено")
                    return
                student.awaiting_topic_action = True
                student.topic_action_mode = "rename"
                student.topic_action_source = resolved_topic_id
                self._persist_students()
                self.api.send_message(chat_id, "Введи нову назву теми одним повідомленням.")
                self.api.answer_callback_query(callback_query["id"], "Очікую нову назву")
                return

            if action.startswith("topic:delete:"):
                topic_ref = action.split(":", 2)[2]
                resolved_topic_id = self._resolve_topic_id(topic_ref)
                topic = self._topic_by_id(resolved_topic_id) if resolved_topic_id else None
                if topic:
                    student.awaiting_delete_action = True
                    student.delete_action_mode = "topic"
                    student.delete_action_source = topic_ref
                    self._persist_students()
                    self.api.send_message(
                        chat_id,
                        f"Видалити тему «{topic.name}»? Використай кнопку нижче.",
                        reply_markup={
                            "inline_keyboard": [
                                [{"text": "Так, видалити", "callback_data": f"admin:confirm_delete:topic:{topic_ref}"}, {"text": "Ні", "callback_data": "admin:cancel_delete"}]
                            ]
                        },
                    )
                self.api.answer_callback_query(callback_query["id"], "")
                return

            if action.startswith("topic:questions:"):
                topic_ref = action.split(":", 2)[2]
                resolved_topic_id = self._resolve_topic_id(topic_ref)
                topic = self._topic_by_id(resolved_topic_id) if resolved_topic_id else None
                if topic:
                    self.api.send_message(chat_id, f"Питання теми «{topic.name}»:", reply_markup=self._build_questions_keyboard(resolved_topic_id))
                self.api.answer_callback_query(callback_query["id"], "Відкрито питання теми")
                return

            if action.startswith("topic:delete_questions:"):
                topic_ref = action.split(":", 2)[2]
                resolved_topic_id = self._resolve_topic_id(topic_ref)
                topic = self._topic_by_id(resolved_topic_id) if resolved_topic_id else None
                if topic:
                    student.awaiting_delete_action = True
                    student.delete_action_mode = "topic_questions"
                    student.delete_action_source = topic_ref
                    self._persist_students()
                    self.api.send_message(
                        chat_id,
                        f"Видалити всі питання теми «{topic.name}»?",
                        reply_markup={
                            "inline_keyboard": [
                                [
                                    {"text": "Так, видалити", "callback_data": f"admin:confirm_delete:topic_questions:{topic_ref}"},
                                    {"text": "Ні", "callback_data": "admin:cancel_delete"},
                                ]
                            ]
                        },
                    )
                self.api.answer_callback_query(callback_query["id"], "")
                return

            if action.startswith("topic:purge:"):
                topic_ref = action.split(":", 2)[2]
                resolved_topic_id = self._resolve_topic_id(topic_ref)
                topic = self._topic_by_id(resolved_topic_id) if resolved_topic_id else None
                if topic:
                    student.awaiting_delete_action = True
                    student.delete_action_mode = "purge_topic"
                    student.delete_action_source = topic_ref
                    self._persist_students()
                    self.api.send_message(
                        chat_id,
                        f"Назавжди видалити тему «{topic.name}»? Питання теж буде видалено.",
                        reply_markup={
                            "inline_keyboard": [
                                [{"text": "Так, видалити", "callback_data": f"admin:confirm_delete:purge_topic:{topic_ref}"}, {"text": "Ні", "callback_data": "admin:cancel_delete"}]
                            ]
                        },
                    )
                self.api.answer_callback_query(callback_query["id"], "")
                return

            if action.startswith("topic:restore:"):
                topic_ref = action.split(":", 2)[2]
                resolved_topic_id = self._resolve_topic_id(topic_ref)
                topic = self._topic_by_id(resolved_topic_id) if resolved_topic_id else None
                if topic and self._restore_topic(resolved_topic_id):
                    self.api.send_message(chat_id, f"Тему «{topic.name}» увімкнено.")
                    self._show_admin_topics_menu(chat_id)
                self.api.answer_callback_query(callback_query["id"], "Тему увімкнено")
                return
            if action.startswith("question:delete:"):
                question_id = action.split(":", 2)[2]
                question = self._find_question(question_id)
                if question:
                    self.api.send_message(
                        chat_id,
                        f"Видалити питання «{question.question}»?",
                        reply_markup={
                            "inline_keyboard": [
                                [
                                    {"text": "Так, видалити", "callback_data": f"admin:confirm_delete:question:{question_id}"},
                                    {"text": "Ні", "callback_data": "admin:cancel_delete"},
                                ]
                            ]
                        },
                    )
                self.api.answer_callback_query(callback_query["id"])
                return
            if action == "cancel_delete":
                prev_mode = student.delete_action_mode
                student.awaiting_delete_action = False
                student.delete_action_mode = None
                student.delete_action_source = None
                self._persist_students()

                # Navigation fix: return to correct admin menu depending on what was being deleted
                if prev_mode in {"topic", "topic_questions", "purge_topic"}:
                    self._show_admin_topics_menu(chat_id)

                self.api.answer_callback_query(callback_query["id"], "Скасовано")
                return
            if action.startswith("confirm_delete:"):
                _, mode, target_id = action.split(":", 2)
                if mode == "student":
                    try:
                        target_id_int = int(target_id)
                    except (TypeError, ValueError):
                        self.api.answer_callback_query(callback_query["id"], "Некоректний ID")
                        return

                    target_student = self.students.get(target_id)
                    if target_student and target_id in self.students:
                        # Tombstone only for non-admins; admins must stay fully usable for DOCX/import/delete flows.
                        if target_id not in {str(i) for i in self.admin_user_ids}:
                            self.deleted_student_keys.add(target_id)
                            self._persist_state()

                        target_student.status = "deleted"
                        target_student.full_name = target_student.full_name or f"Учень {target_student.user_id}"

                        # Cleanup external storage (so it won't come back after restart)
                        self.sessions_store.delete_student(target_id_int)
                        self.results_store.delete_student_history(target_id_int)

                        self._persist_students()
                        self.api.send_message(chat_id, "Учня видалено.", reply_markup=self._build_back_to_main_keyboard())
                        self.api.answer_callback_query(callback_query["id"], "Учня видалено")
                    else:
                        self.api.answer_callback_query(callback_query["id"], "Учня не знайдено")
                    return

                if mode == "question":
                    deleted = self._delete_question(target_id)
                    if deleted:
                        self.api.send_message(chat_id, "Питання видалено.")
                        self._show_admin_topics_menu(chat_id)
                    self.api.answer_callback_query(callback_query["id"], "Питання видалено" if deleted else "Не знайдено")
                # Safety: ensure Telegram always gets answerCallbackQuery for confirm_delete callbacks.
                self.api.answer_callback_query(callback_query["id"], "")
                if mode == "topic_questions":
                    if not student.awaiting_delete_action or student.delete_action_mode != "topic_questions" or student.delete_action_source != target_id:
                        self.api.answer_callback_query(callback_query["id"], "Немає підтвердження")
                        return
                    resolved_topic_id = self._resolve_topic_id(target_id)
                    if not resolved_topic_id:
                        self.api.answer_callback_query(callback_query["id"], "Не знайдено")
                        return
                    deleted = self._delete_topic_questions(resolved_topic_id)
                    topic = self._topic_by_id(resolved_topic_id)
                    if topic:
                        self.api.send_message(chat_id, f"Усі питання теми «{topic.name}» видалено.")
                        self._show_admin_topics_menu(chat_id)
                    self.api.answer_callback_query(callback_query["id"], "Питання видалено" if deleted else "Немає питань")
                    student.awaiting_delete_action = False
                    student.delete_action_mode = None
                    student.delete_action_source = None
                    self._persist_students()
                    return
                if not student.awaiting_delete_action:
                    self.api.answer_callback_query(callback_query["id"], "Немає підтвердження")
                    return
                if student.delete_action_mode == "topic" and mode == "topic" and student.delete_action_source == target_id:
                    resolved_topic_id = self._resolve_topic_id(target_id)
                    topic = self._topic_by_id(resolved_topic_id) if resolved_topic_id else None

                    deleted = False
                    questions_count = self._topic_question_count(resolved_topic_id) if resolved_topic_id else 0

                    if topic:
                        if questions_count > 0:
                            self.api.send_message(
                                chat_id,
                                "Тему не можна видалити, поки в ній є питання. Спочатку видали питання.",
                            )
                            self._show_admin_topics_menu(chat_id)
                            self.api.answer_callback_query(callback_query["id"], "Є питання")

                            student.awaiting_delete_action = False
                            student.delete_action_mode = None
                            student.delete_action_source = None
                            self._persist_students()
                            return

                        deleted = self._delete_topic(resolved_topic_id)
                        if deleted:
                            self.api.send_message(chat_id, "Тему видалено.")
                            self._show_admin_topics_menu(chat_id)

                        self.api.answer_callback_query(callback_query["id"], "Тему видалено" if deleted else "Не знайдено")
                    else:
                        self.api.answer_callback_query(callback_query["id"], "Не знайдено")

                    student.awaiting_delete_action = False
                    student.delete_action_mode = None
                    student.delete_action_source = None
                    self._persist_students()
                    return

                elif student.delete_action_mode == "purge_topic" and mode == "purge_topic" and student.delete_action_source == target_id:
                    resolved_topic_id = self._resolve_topic_id(target_id)
                    topic = self._topic_by_id(resolved_topic_id) if resolved_topic_id else None
                    if topic:
                        self.questions = [question for question in self.questions if question.topic_id != resolved_topic_id]
                        self.questions_store.save([{"id": q.id, "topic_id": q.topic_id, "type": q.type, "question": q.question, "options": q.options, "answer": q.answer, "explanation": q.explanation} for q in self.questions])
                        deleted = self._delete_topic(resolved_topic_id)
                        if deleted:
                            self.api.send_message(chat_id, "Тему та всі питання в ній видалено.")
                            self._show_admin_topics_menu(chat_id)
                        self.api.answer_callback_query(callback_query["id"], "Видалено" if deleted else "Не знайдено")
                student.awaiting_delete_action = False
                student.delete_action_mode = None
                student.delete_action_source = None
                self._persist_students()
                return

        if data == "main_menu":
            self._show_main_menu(chat_id, user["id"])
            self.api.answer_callback_query(callback_query["id"], "Відкрито меню")
            return
        if data == "back_to_topics":
            self.api.send_message(chat_id, "Оберіть тему:", reply_markup=self._build_topics_keyboard())
            self.api.answer_callback_query(callback_query["id"], "Відкрито список тем")
            return
        if data.startswith("import_topic:"):
            try:
                user_id_int = int(user["id"])
            except (TypeError, ValueError):
                self.api.answer_callback_query(callback_query["id"], "Немає прав")
                return
            if user_id_int not in self.admin_user_ids:
                self.api.answer_callback_query(callback_query["id"], "Немає прав")
                return
            topic_id = data.split(":", 1)[1]
            topic = self._topic_by_id(topic_id)
            if not topic or not topic.active:
                self.api.answer_callback_query(callback_query["id"], "Тему не знайдено")
                return
            student.awaiting_docx_import = True
            student.awaiting_docx_topic_id = topic_id
            self._persist_students()
            self.api.send_message(chat_id, f"Надішли .docx файлом для теми «{topic.name}».")
            self.api.answer_callback_query(callback_query["id"], f"Очікую DOCX для {topic.name}")
            return
        if data == "topic:add":
            try:
                user_id_int = int(user["id"])
            except (TypeError, ValueError):
                self.api.answer_callback_query(callback_query["id"], "Немає прав")
                return
            if user_id_int not in self.admin_user_ids:
                self.api.answer_callback_query(callback_query["id"], "Немає прав")
                return
            student.awaiting_topic_action = True
            student.topic_action_mode = "add"
            student.topic_action_source = None
            self._persist_students()
            self.api.send_message(chat_id, "Введи назву нової теми")
            self.api.answer_callback_query(callback_query["id"], "Очікую назву теми")
            return
        if data.startswith("topic:"):
            topic_id = data.split(":", 1)[1]
            student.selected_topic_ids = [topic_id]
            self._persist_students()
            self._start_test(student, topic_id)
            self.api.answer_callback_query(callback_query["id"], f"Починаю тему: {self._topic_name(topic_id)}")
            return
        if data == "restart_test":
            topic_id = student.current_test_topic_id or (student.selected_topic_ids[0] if student.selected_topic_ids else None)
            self._start_test(student, topic_id)
            self.api.answer_callback_query(callback_query["id"], "Тест перезапущено")
            return
        if data.startswith("student:view:"):
            try:
                user_id_int = int(user["id"])
            except (TypeError, ValueError):
                self.api.answer_callback_query(callback_query["id"], "Немає прав")
                return
            if user_id_int not in self.admin_user_ids:
                self.api.answer_callback_query(callback_query["id"], "Немає прав")
                return
            target_id = data.split(":", 2)[2]
            target_student = self.students.get(target_id)
            if target_student:
                self._show_student_details(chat_id, target_student)
                self.api.answer_callback_query(callback_query["id"], "Відкрито картку учня")
            else:
                self.api.answer_callback_query(callback_query["id"], "Учня не знайдено")
            return
        if data.startswith("student:approve:"):
            if user["id"] not in self.admin_user_ids:
                self.api.answer_callback_query(callback_query["id"], "Немає прав")
                return
            target_id = data.split(":", 2)[2]
            target_student = self.students.get(target_id)
            if target_student:
                target_student.status = "approved"
                self._persist_students()
                self._show_student_details(chat_id, target_student)

                # notify student
                if getattr(target_student, "chat_id", None) is not None:
                    try:
                        self.api.send_message(
                            target_student.chat_id,
                            "Р”Р°РЅС– РѕС‚СЂРёРјР°РЅРѕ. РўРёРјР°С”С€ Р°РґРјС–РЅ-РґРѕСЃСѓС‚Сƒї.",
                            reply_markup=self._build_back_to_main_keyboard(),
                        )
                    except RuntimeError as exc:
                        print(f"[student approve notify] failed chat_id={target_student.chat_id}: {exc}")

                # notify all admins
                self._notify_admins(
                    f"✅ Учня схвалено: {target_student.full_name or f'Учень {target_student.user_id}'} (ID: {target_student.user_id})"
                )
            self.api.answer_callback_query(callback_query["id"], "Учня схвалено")
            return
        if data.startswith("student:block:"):
            if user["id"] not in self.admin_user_ids:
                self.api.answer_callback_query(callback_query["id"], "Немає прав")
                return

            target_id_raw = data.split(":", 2)[2]
            try:
                target_id_int = int(target_id_raw)
                target_id = str(target_id_int)
            except (TypeError, ValueError):
                target_id_int = None
                target_id = target_id_raw

            # allow blocking even other admins

            target_student = self.students.get(target_id)
            if not target_student:
                self.api.answer_callback_query(callback_query["id"], "Учня не знайдено")
                return

            target_student.status = "blocked"
            # stop active test for this student
            target_student.current_test = []
            target_student.current_index = 0
            target_student.current_question_id = None
            target_student.current_question_message_id = None
            print(f"[admin] block student target_id={target_id} -> current_question_id=None")
            self._persist_students()
            self._show_student_details(chat_id, target_student)
            self.api.answer_callback_query(callback_query["id"], "Учня заблоковано")
            return
        if data.startswith("student:delete:"):
            if user["id"] not in self.admin_user_ids:
                self.api.answer_callback_query(callback_query["id"], "Немає прав")
                return

            target_id_raw = data.split(":", 2)[2]
            try:
                target_id = str(int(target_id_raw))
            except (TypeError, ValueError):
                target_id = target_id_raw

            target_student = self.students.get(target_id)
            if not target_student:
                self.api.answer_callback_query(callback_query["id"], "Учня не знайдено")
                return

            # Don't allow deleting an admin account.
            try:
                target_id_int = int(target_id)
            except (TypeError, ValueError):
                target_id_int = None

            self.api.send_message(
                chat_id,
                f"Видалити учня «{target_student.full_name or 'без імені'}» (ID: {target_student.user_id})?",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "Так, видалити", "callback_data": f"admin:confirm_delete:student:{target_id}"}],
                        [{"text": "Ні", "callback_data": f"student:view:{target_id}"}],
                    ]
                },
            )
            self.api.answer_callback_query(callback_query["id"], "")
            return

        if data.startswith("admin:confirm_delete:student:"):
            if user["id"] not in self.admin_user_ids:
                self.api.answer_callback_query(callback_query["id"], "Немає прав")
                return

            target_id_raw = data.split(":", 3)[3]
            try:
                target_id = str(int(target_id_raw))
                target_id_int = int(target_id)
            except (TypeError, ValueError):
                self.api.answer_callback_query(callback_query["id"], "Некоректний ID")
                return

            target_student = self.students.get(target_id)
            if target_student and target_id in self.students:
                # Soft-delete: keep record in-memory (and in students.json filtered by status),
                # so admin IDs won't get recreated by _get_student() auto-approve logic.
                self.deleted_student_keys.add(target_id)
                self._persist_state()

                target_student.status = "deleted"
                target_student.full_name = target_student.full_name or f"Учень {target_student.user_id}"

                # Cleanup external storage (so it won't come back after restart)
                self.sessions_store.delete_student(target_id_int)
                self.results_store.delete_student_history(target_id_int)

                self._persist_students()
                self.api.send_message(chat_id, "Учня видалено.", reply_markup=self._build_back_to_main_keyboard())
                self.api.answer_callback_query(callback_query["id"], "Учня видалено")
                return
        if data.startswith("answer:"):
            if not student.current_question_id:
                self.api.answer_callback_query(callback_query["id"], "Немає активного питання")
                return

            if student.status != "approved":
                self.api.answer_callback_query(callback_query["id"], "Доступ заборонено")
                return

            if self._time_is_up(student):
                self._finish_test_due_to_timeout(student)
                self.api.answer_callback_query(callback_query["id"], "Час вийшов")
                return
            question = self._find_question(student.current_question_id)
            if not question:
                self.api.answer_callback_query(callback_query["id"], "Питання не знайдено")
                return

            raw = data.split(":", 1)[1]
            if question.type == "matching":
                left_count = len(question.options) // 2 if len(question.options) // 2 else len(question.options)
                if left_count > 0 and len(student.matching_pairs) >= left_count and raw not in {"submit", "reset"}:
                    self.api.answer_callback_query(callback_query["id"], "Усі пари вже зіставлено. Використай «Підтвердити вибір» або «Скинути».")
                    return
            if question.type == "matching":
                if raw == "submit":
                    pairs = [(left + 1, right + 1) for left, right in sorted(student.matching_pairs.items())]
                    self._grade_matching_question(student, pairs)
                    student.matching_pairs = {}
                    student.matching_selected_left = None
                    self._persist_students()
                    self.api.answer_callback_query(callback_query["id"], "Відповідь отримано")
                    return

                if raw == "reset":
                    student.matching_pairs = {}
                    student.matching_selected_left = None
                    self._persist_students()
                    self.api.edit_message_text(
                        chat_id,
                        message["message_id"],
                        f"Питання {student.current_index + 1}/{len(student.current_test)}\n\n{question.question}\n\nСтан зіставлення скинуто.",
                        reply_markup=self._build_keyboard(
                            question.options,
                            question_type="matching",
                            matching_pairs=student.matching_pairs,
                            matching_selected_left=student.matching_selected_left,
                        ),
                    )
                    self.api.answer_callback_query(callback_query["id"], "Скинуто")
                    return

                parts = data.split(":")
                if len(parts) != 3:
                    self.api.answer_callback_query(callback_query["id"], "Невідома кнопка")
                    return

                side, raw_index = parts[1], parts[2]
                try:
                    index = int(raw_index)
                except ValueError:
                    self.api.answer_callback_query(callback_query["id"], "Невідома кнопка")
                    return

                half = len(question.options) // 2
                left_count = half if half else len(question.options)
                right_count = len(question.options) - left_count
                left_map = student.shuffled_matching_left if student.shuffled_matching_left else list(range(left_count))
                right_map = student.shuffled_matching_right if student.shuffled_matching_right else list(range(right_count))

                if side == "left":
                    if index < 0 or index >= left_count:
                        self.api.answer_callback_query(callback_query["id"], "Невірний номер")
                        return
                    if left_count > 0 and len(student.matching_pairs) >= left_count:
                        self.api.answer_callback_query(callback_query["id"], "Усі пари вже зіставлено. Використай «Підтвердити вибір» або «Скинути».")
                        return
                    if student.matching_selected_left == index:
                        student.matching_selected_left = None
                        self._persist_students()
                        self.api.edit_message_text(
                            chat_id,
                            message["message_id"],
                            f"Питання {student.current_index + 1}/{len(student.current_test)}\n\n{question.question}",
                            reply_markup=self._build_keyboard(
                                (question.options[:half] if half else question.options) + (question.options[half:] if half else []),
                                question_type="matching",
                                matching_pairs=student.matching_pairs,
                                matching_selected_left=student.matching_selected_left,
                            ),
                        )
                        self.api.answer_callback_query(callback_query["id"], "Знято вибір")
                        return

                    student.matching_selected_left = index
                    self._persist_students()
                    self.api.edit_message_text(
                        chat_id,
                        message["message_id"],
                        f"Питання {student.current_index + 1}/{len(student.current_test)}\n\n{question.question}\n\nОберіть праву букву для {index + 1}.",
                        reply_markup=self._build_keyboard(
                            (question.options[:half] if half else question.options) + (question.options[half:] if half else []),
                            question_type="matching",
                            matching_pairs=student.matching_pairs,
                            matching_selected_left=student.matching_selected_left,
                        ),
                    )
                    self.api.answer_callback_query(callback_query["id"], f"Обрано {index + 1}")
                    return

                if side == "right":
                    if index < 0 or index >= right_count:
                        self.api.answer_callback_query(callback_query["id"], "Невірна літера")
                        return
                    if left_count > 0 and len(student.matching_pairs) >= left_count:
                        self.api.answer_callback_query(callback_query["id"], "Усі пари вже зіставлено. Використай «Підтвердити вибір» або «Скинути».")
                        return

                    chosen_left = next((left_index for left_index, chosen_right in student.matching_pairs.items() if chosen_right == index), None)
                    if student.matching_selected_left is None:
                        if len(student.matching_pairs) >= left_count and left_count > 0:
                            self.api.answer_callback_query(callback_query["id"], "Усі пари вже зіставлено. Використай «Підтвердити вибір» або «Скинути».")
                        else:
                            self.api.answer_callback_query(callback_query["id"], "Спочатку обери номер зліва")
                        return

                    if student.matching_selected_left in student.matching_pairs and student.matching_pairs[student.matching_selected_left] == index:
                        self.api.answer_callback_query(callback_query["id"], "Ця пара вже зафіксована")
                        return

                    if chosen_left is not None:
                        self.api.answer_callback_query(callback_query["id"], "Ця літера вже використана. Натисни «Скинути», щоб змінити.")
                        return
                    if len(student.matching_pairs) >= left_count and left_count > 0:
                        self.api.answer_callback_query(callback_query["id"], "Усі пари вже зіставлено. Використай «Підтвердити вибір» або «Скинути».")
                        return

                    student.matching_pairs[student.matching_selected_left] = index
                    student.matching_selected_left = None
                    self._persist_students()

                    all_paired = len(student.matching_pairs) >= left_count and left_count > 0
                    pairs_text = ", ".join(
                        f"{left + 1}{chr(ord('a') + right)}"
                        for left, right in sorted(student.matching_pairs.items())
                    ) or "нічого"
                    question_text = f"Питання {student.current_index + 1}/{len(student.current_test)}\n\n{question.question}\n\nПари: {pairs_text}"
                    if all_paired:
                        question_text += "\n\n✅ Усі пари відмічено. Тепер доступне лише підтвердження або скидання."

                    self.api.edit_message_text(
                        chat_id,
                        message["message_id"],
                        question_text,
                        reply_markup=self._build_keyboard(
                            (question.options[:half] if half else question.options) + (question.options[half:] if half else []),
                            question_type="matching",
                            matching_pairs=student.matching_pairs,
                            matching_selected_left=student.matching_selected_left,
                            matching_left_map=left_map,
                            matching_right_map=right_map,
                        ),
                    )
                    self.api.answer_callback_query(callback_query["id"], f"Пара: {pairs_text}")
                    return

                self.api.answer_callback_query(callback_query["id"], "Невідома кнопка")
                return

            if raw == "submit":
                if question.type == "multi":
                    self._grade_question(student, set(student.pending_multi_answers))
                    student.pending_multi_answers = []
                    self._persist_students()
                self.api.answer_callback_query(callback_query["id"], "Відповідь отримано")
                return

            try:
                selected = int(raw)
            except ValueError:
                self.api.answer_callback_query(callback_query["id"], "Невідома кнопка")
                return
            if question.type == "multi":
                original_selected = student.shuffled_options[selected] if 0 <= selected < len(student.shuffled_options) else selected
                if original_selected in student.pending_multi_answers:
                    student.pending_multi_answers.remove(original_selected)
                else:
                    student.pending_multi_answers.append(original_selected)
                self._persist_students()
                chosen_text = ", ".join(question.options[i] for i in sorted(student.pending_multi_answers) if 0 <= i < len(question.options)) or "нічого"
                updated_text = f"Питання {student.current_index + 1}/{len(student.current_test)}\n\n{question.question}\n\nОбрано: {chosen_text}"
                self.api.edit_message_text(chat_id, message["message_id"], updated_text, reply_markup=self._build_keyboard([question.options[i] for i in student.shuffled_options], question_type="multi", selected_indexes={student.shuffled_options.index(i) for i in student.pending_multi_answers if i in student.shuffled_options}))
                self.api.answer_callback_query(callback_query["id"], f"Обрано: {chosen_text}")
                return
            original_selected = student.shuffled_options[selected] if 0 <= selected < len(student.shuffled_options) else selected
            self._grade_question(student, {original_selected})
            self.api.answer_callback_query(callback_query["id"], "Відповідь отримано")
            return
        self.api.answer_callback_query(callback_query["id"], "Невідома кнопка")

    def _handle_admin_command(self, message: dict):
        chat = message["chat"]
        user = message["from"]
        text = message.get("text", "")
        if user["id"] not in self.admin_user_ids:
            return
        if text.startswith("/students"):
            self.api.send_message(chat["id"], "🏆 Рейтинг учнів", reply_markup=self._build_students_keyboard())
        elif text.startswith("/approve"):
            parts = text.split()
            if len(parts) != 2:
                self.api.send_message(chat["id"], "Використання: /approve <user_id>")
                return
            student = self.students.get(parts[1])
            if not student:
                self.api.send_message(chat["id"], "Учня не знайдено.")
                return
            student.status = "approved"
            self._persist_students()
            self.api.send_message(chat["id"], f"Учню {student.full_name} надано доступ.")
        elif text.startswith("/results"):
            parts = text.split()
            if len(parts) == 1:
                self._send_results_report(chat["id"])
            elif len(parts) == 2 and parts[1].isdigit():
                self._send_results_report(chat["id"], int(parts[1]))
            else:
                self.api.send_message(chat["id"], "Використання: /results або /results <user_id>")
        elif text.startswith("/settime"):
            parts = text.split()
            if len(parts) != 2 or not parts[1].isdigit():
                self.api.send_message(chat["id"], "Використання: /settime <minutes>")
                return
            minutes = max(1, int(parts[1]))
            self.test_duration_seconds = minutes * 60
            self._persist_state()
            self.api.send_message(chat["id"], f"Час тесту встановлено на {minutes} хв.")
        else:
            self._send_admin_help(chat["id"])

    def process_update(self, update: dict):
        if "message" in update:
            message = update["message"]
            if message.get("document"):
                self._handle_document(message)
                return
            text = message.get("text", "").strip()
            student = self._get_student(message["from"]["id"], message["chat"]["id"])
            if text.startswith("/start"):
                self._handle_start(message)
                return
            if student.awaiting_topic_action or student.awaiting_name or student.awaiting_docx_topic_id:
                self._handle_text(message)
                return
            if text.startswith("/menu"):
                if student.status == "new":
                    self.api.send_message(message["chat"]["id"], "Спочатку натисни /start один раз.")
                elif student.status == "approved":
                    self._show_main_menu(message["chat"]["id"], message["from"]["id"])
                else:
                    self.api.send_message(message["chat"]["id"], "Твоя анкета ще не схвалена адміністрацією.")
                return
            if text.startswith("/students") or text.startswith("/approve") or text.startswith("/results") or text.startswith("/settime") or text.startswith("/admin"):
                self._handle_admin_command(message)
                return
            if student.current_question_id:
                question = self._find_question(student.current_question_id)
                if question and question.type == "matching":
                    self._grade_matching_question(student, self._parse_matching_answer(text))
                else:
                    self.api.send_message(message["chat"]["id"], "Для відповіді користуйся кнопками.")
                return
            self._handle_text(message)
            return
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])

    def run(self):
        offset = None
        while True:
            try:
                updates = self.api.get_updates(offset)
                for update in updates:
                    offset = update["update_id"] + 1
                    self.process_update(update)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    print("Telegram conflict: another bot instance is already running.")
                time.sleep(POLL_INTERVAL_SECONDS)
            except (urllib.error.URLError, Exception):
                time.sleep(POLL_INTERVAL_SECONDS)


def ensure_default_files():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not QUESTIONS_FILE.exists():
        QUESTIONS_FILE.write_text("[]", encoding="utf-8")
    if not ADMINS_FILE.exists():
        ADMINS_FILE.write_text(json.dumps({"admin_user_ids": []}, ensure_ascii=False, indent=2), encoding="utf-8")
    if not STUDENTS_FILE.exists():
        STUDENTS_FILE.write_text("{}", encoding="utf-8")
    if not STATE_FILE.exists():
        STATE_FILE.write_text("{}", encoding="utf-8")
    if not TOPICS_FILE.exists():
        TOPICS_FILE.write_text("[]", encoding="utf-8")


def main():
    ensure_default_files()
    bot = QuizBot()
    bot.run()


if __name__ == "__main__":
    main()
