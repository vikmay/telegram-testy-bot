from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class StudentAnalytics:
    user_id: int
    full_name: str
    status: str
    total_attempts: int
    best_score: int
    last_score: int
    avg_score: float
    topic_accuracy: Dict[str, float]  # topic_id -> accuracy [0..1]


class StatsService:
    """
    Lightweight analytics + admin UI helpers.

    NOTE: The main bot wraps calls in try/except and falls back to legacy UI,
    so this service must be robust and never throw for missing/empty data.
    """

    def __init__(
        self,
        results_store: Any,
        sessions_store: Any,
        topic_name_fn: Callable[[str], str],
        default_test_length: int,
    ):
        self._results_store = results_store
        self._sessions_store = sessions_store
        self._topic_name_fn = topic_name_fn
        self._default_test_length = int(default_test_length)

    @staticmethod
    def _safe_str(value: Any) -> str:
        return str(value) if value is not None else ""

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def compute_student_analytics(
        self,
        user_id: int,
        full_name: str = "",
        status: str = "",
    ) -> StudentAnalytics:
        user_id = self._safe_int(user_id)
        full_name = full_name or f"ID {user_id}"
        status = status or ""

        total_attempts = 0
        best_score = 0
        last_score = 0
        avg_score = 0.0

        # results_store API (as implemented in bot.py)
        try:
            total_attempts = int(self._results_store.user_result_count(user_id))
        except Exception:
            total_attempts = 0

        try:
            topic_accuracy = self._results_store.topic_accuracy(user_id)
        except Exception:
            topic_accuracy = {}

        try:
            rows = self._results_store.user_results(user_id) or []
        except Exception:
            rows = []

        if rows:
            # Each row shape: (id, user_id, full_name, score, attempts, topic_id, topic_name, created_at)
            scores: List[int] = []
            for row in rows:
                try:
                    scores.append(int(row[3]))
                except Exception:
                    continue
            if scores:
                best_score = max(scores)
                last_score = int(rows[0][3]) if len(rows[0]) > 3 else 0  # ORDER BY id DESC in bot.py
                avg_score = float(sum(scores)) / float(len(scores))

        # best_score/avg_score are in "correct answers" units (0..default_test_length)
        # Keep topic_accuracy as returned (ratio 0..1)
        return StudentAnalytics(
            user_id=user_id,
            full_name=full_name,
            status=status,
            total_attempts=total_attempts,
            best_score=best_score,
            last_score=last_score,
            avg_score=avg_score,
            topic_accuracy={str(k): float(v) for k, v in (topic_accuracy or {}).items()},
        )

    def format_student_profile(self, analytics: StudentAnalytics | Dict[str, Any]) -> str:
        if isinstance(analytics, dict):
            # tolerate old/partial shape
            user_id = self._safe_int(analytics.get("user_id", 0))
            full_name = self._safe_str(analytics.get("full_name", "")) or f"ID {user_id}"
            status = self._safe_str(analytics.get("status", ""))
            total_attempts = self._safe_int(analytics.get("total_attempts", 0))
            best_score = self._safe_int(analytics.get("best_score", 0))
            last_score = self._safe_int(analytics.get("last_score", 0))
            avg_score = float(analytics.get("avg_score", 0.0) or 0.0)
            topic_accuracy = {str(k): float(v) for k, v in (analytics.get("topic_accuracy", {}) or {}).items()}
        else:
            user_id = analytics.user_id
            full_name = analytics.full_name
            status = analytics.status
            total_attempts = analytics.total_attempts
            best_score = analytics.best_score
            last_score = analytics.last_score
            avg_score = analytics.avg_score
            topic_accuracy = analytics.topic_accuracy

        # Topic stats: show top 3 accuracies (if any)
        topic_lines: List[str] = []
        try:
            sorted_items = sorted(topic_accuracy.items(), key=lambda kv: kv[1], reverse=True)
            for topic_id, acc in sorted_items[:3]:
                topic_name = self._topic_name_fn(topic_id) if self._topic_name_fn else topic_id
                percent = int(round(float(acc) * 100))
                topic_lines.append(f"• {topic_name}: {percent}%")
        except Exception:
            topic_lines = []

        percent_best = int(round((best_score / max(1, self._default_test_length)) * 100))
        percent_last = int(round((last_score / max(1, self._default_test_length)) * 100))
        percent_avg = int(round((avg_score / max(1, self._default_test_length)) * 100)) if avg_score else 0

        lines = [
            "📌 Карточка учня:",
            f"• Ім'я: {full_name}",
            f"• ID: {user_id}",
            f"• Статус: {status or '—'}",
            f"• Спроб: {total_attempts}",
            f"• Найкраще: {best_score}/{self._default_test_length} ({percent_best}%)",
            f"• Останній тест: {last_score}/{self._default_test_length} ({percent_last}%)",
            f"• Середній: {avg_score:.1f}/{self._default_test_length} ({percent_avg}%)",
        ]

        if topic_lines:
            lines.append("• Точність по темах (топ):")
            lines.extend(topic_lines)

        return "\n".join(lines)

    def build_students_ranking_buttons(self, students_for_ranking: Iterable[Any]) -> Dict[str, Any]:
        """
        Returns Telegram inline_keyboard structure:
        { "inline_keyboard": [[{"text": ..., "callback_data": ...}], ...] }
        """
        keyboard_rows: List[List[Dict[str, str]]] = []
        try:
            students = list(students_for_ranking or [])
        except Exception:
            students = []

        def score_key(student: Any) -> float:
            user_id = self._safe_int(getattr(student, "user_id", 0))
            try:
                # Prefer avg score for ranking
                analytics = self.compute_student_analytics(user_id=user_id)
                return float(analytics.avg_score)
            except Exception:
                return 0.0

        try:
            students_sorted = sorted(students, key=score_key, reverse=True)
        except Exception:
            students_sorted = list(students)

        for student in students_sorted:
            user_id = self._safe_int(getattr(student, "user_id", 0))
            full_name = self._safe_str(getattr(student, "full_name", "")) or self._safe_str(
                getattr(student, "first_name", "")
            )
            if not full_name:
                full_name = f"{user_id}"

            # status icon
            status = self._safe_str(getattr(student, "status", ""))
            status_icon = (
                "✅" if status == "approved" else
                "⏳" if status == "pending_approval" else
                "🚫" if status == "blocked" else
                "📝"
            )

            total_tests = 0
            best_score = 0
            try:
                total_tests = int(self._results_store.user_result_count(user_id))
                analytics = self.compute_student_analytics(user_id=user_id)
                best_score = analytics.best_score
            except Exception:
                total_tests = 0
                best_score = 0

            label = f"{full_name} | 📚 {total_tests} | {status_icon} | 🏅 {best_score}"
            keyboard_rows.append([{"text": label, "callback_data": f"student:view:{user_id}"}])

        # main menu button (matches bot legacy fallback)
        keyboard_rows.append([{"text": "🏠 Головне меню", "callback_data": "main_menu"}])
        return {"inline_keyboard": keyboard_rows}
