from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

DEFAULT_TEST_LENGTH_FALLBACK = 10


@dataclass(frozen=True)
class StudentTopicAvg:
    topic_id: str
    topic_name: str
    avg_score: float
    tests_count: int


@dataclass(frozen=True)
class StudentLastResult:
    created_at: str
    topic_name: str
    score: int
    attempts: int


@dataclass(frozen=True)
class StudentAnalytics:
    user_id: int
    full_name: str
    status: str

    tests_count: int
    avg_score: float
    best_score: int

    # accuracy in percent 0..100
    accuracy_percent: int

    last_result_score: Optional[int]
    last_result_topic_name: Optional[str]

    # streak: consecutive tests with score>0 (fallback)
    streak: int

    # topics
    topics: List[StudentTopicAvg]

    # progress (last N scores)
    progress_scores: List[int]


class StatsService:
    def __init__(
        self,
        results_store,
        sessions_store,
        topic_name_fn: Callable[[str], str],
        default_test_length: int = DEFAULT_TEST_LENGTH_FALLBACK,
    ):
        self._results_store = results_store
        self._sessions_store = sessions_store
        self._topic_name_fn = topic_name_fn
        self._default_test_length = max(1, int(default_test_length))

    # -------------------------
    # Time / activity
    # -------------------------
    def last_activity_days(self, user_id: int) -> Optional[float]:
        """
        Returns days since last activity or None if unknown.
        Uses student_sessions.updated_at when available.
        """
        # session_store may provide updated_at via direct SQL
        try:
            updated_at = self._sessions_store.get_updated_at_at(user_id)
        except AttributeError:
            updated_at = self._sessions_store.get_last_activity_at(user_id)  # legacy fallback name

        if not updated_at:
            return None
        if isinstance(updated_at, (int, float)):
            # assume unix timestamp
            last_dt = datetime.fromtimestamp(float(updated_at))
        else:
            # sqlite CURRENT_TIMESTAMP stored as 'YYYY-MM-DD HH:MM:SS'
            last_dt = self._parse_datetime(str(updated_at))
            if last_dt is None:
                return None
        now = datetime.now()
        return (now - last_dt).total_seconds() / 86400.0

    def activity_status_emoji(self, user_id: int) -> str:
        days = self.last_activity_days(user_id)
        if days is None:
            return "🕒"  # unknown
        if days <= 3:
            return "🟢"
        if days <= 14:
            return "🟡"
        return "🔴"

    # -------------------------
    # Core metrics from results.db
    # -------------------------
    def _fetch_user_results_scores(
        self,
        user_id: int,
    ) -> List[Tuple[int, int, str, int, str, str, str]]:
        """
        Returns rows as provided by results_store.user_results():
        (id, user_id, full_name, score, attempts, topic_id, topic_name, created_at)
        """
        return self._results_store.user_results(user_id)

    def compute_student_analytics(self, user_id: int, full_name: str, status: str) -> StudentAnalytics:
        rows = self._fetch_user_results_scores(user_id)
        tests_count = len(rows)

        if tests_count == 0:
            return StudentAnalytics(
                user_id=user_id,
                full_name=full_name,
                status=status,
                tests_count=0,
                avg_score=0.0,
                best_score=0,
                accuracy_percent=0,
                last_result_score=None,
                last_result_topic_name=None,
                streak=0,
                topics=[],
                progress_scores=[],
            )

        # rows are ordered by id desc in user_results()
        total_score = sum(int(score) for _, _, _, score, _, _, _, _ in rows)
        best_score = max(int(score) for _, _, _, score, _, _, _, _ in rows)

        avg_score = float(total_score) / float(tests_count)

        # accuracy based on per-test score out of N questions.
        # score is number of correct answers, so accuracy = sum(score) / (tests_count*N)
        total_questions = float(tests_count * self._default_test_length)
        accuracy_percent = 0
        if total_questions > 0:
            accuracy_percent = int(round((float(total_score) / total_questions) * 100.0))
            accuracy_percent = max(0, min(100, accuracy_percent))

        last_row = rows[0]
        last_result_score = int(last_row[3]) if last_row[3] is not None else None
        last_result_topic_name = last_row[6] or (self._topic_name_fn(last_row[5]) if last_row[5] else None)

        # streak: consecutive tests from newest backwards where score>0
        streak = 0
        for _, _, _, score, _, _, _, _ in rows:
            if int(score) > 0:
                streak += 1
            else:
                break

        topics = self._compute_topics_avg(rows)

        progress_scores = [int(r[3]) for r in rows[:7]]  # newest first; UI can reverse if needed

        return StudentAnalytics(
            user_id=user_id,
            full_name=full_name,
            status=status,
            tests_count=tests_count,
            avg_score=avg_score,
            best_score=best_score,
            accuracy_percent=accuracy_percent,
            last_result_score=last_result_score,
            last_result_topic_name=last_result_topic_name,
            streak=streak,
            topics=topics,
            progress_scores=progress_scores,
        )

    def _compute_topics_avg(self, rows: Sequence[Tuple[int, int, str, int, int, str, str, str]]) -> List[StudentTopicAvg]:
        # aggregate by topic_id
        by_topic: Dict[str, Dict[str, float]] = {}
        for row in rows:
            topic_id = row[5] or ""
            if not topic_id:
                continue
            score = int(row[3])
            if topic_id not in by_topic:
                by_topic[topic_id] = {"sum": 0.0, "count": 0.0}
            by_topic[topic_id]["sum"] += float(score)
            by_topic[topic_id]["count"] += 1.0

        result: List[StudentTopicAvg] = []
        for topic_id, agg in by_topic.items():
            count = int(agg["count"])
            if count <= 0:
                continue
            avg = agg["sum"] / float(count)
            result.append(
                StudentTopicAvg(
                    topic_id=topic_id,
                    topic_name=self._topic_name_fn(topic_id),
                    avg_score=avg,
                    tests_count=count,
                )
            )

        # only top 5 topics
        result.sort(key=lambda x: (-x.avg_score, -x.tests_count, x.topic_name.lower()))
        return result[:5]

    # -------------------------
    # Ranking / buttons
    # -------------------------
    def build_students_ranking_buttons(self, students: Dict[str, object]) -> List[List[Dict[str, str]]]:
        """
        Returns inline_keyboard rows:
        [
          [{"text": "1) Ім’я | ⭐8.0 | 📚6 | 🎯78%", "callback_data": "student:view:<id>"}],
          ...
        ]
        """
        entries: List[Tuple[int, float, int, str, str]] = []
        # sort key:
        # - higher avg_score
        # - more tests
        # - users with no tests at the bottom
        for key, st in students.items():
            user_id = int(getattr(st, "user_id"))
            full_name = (
                getattr(st, "full_name", "")
                or getattr(st, "first_name", "")
                or f"Учень {user_id}"
            )
            status = getattr(st, "status", "new")
            # compute light metrics without heavy work:
            rows = self._results_store.user_results(user_id)
            tests_count = len(rows)
            if tests_count == 0:
                avg = -1.0
            else:
                total_score = sum(int(r[3]) for r in rows)
                avg = float(total_score) / float(tests_count)
            entries.append((user_id, avg, tests_count, full_name, status))

        entries.sort(key=lambda x: (x[1] < 0, -x[1], -x[2], x[3].lower()))

        keyboard: List[List[Dict[str, str]]] = []
        rank = 1
        for user_id, avg, tests_count, full_name, status in entries:
            status_icon = (
                "✅" if status == "approved" else
                "⏳" if status == "pending_approval" else
                "🚫" if status == "blocked" else
                "📝"
            )

            if tests_count == 0:
                label = f"{rank}) {status_icon} {full_name} | ⭐ — | 📚0"
            else:
                # accuracy percent
                total_score = sum(int(r[3]) for r in self._results_store.user_results(user_id))
                total_questions = tests_count * self._default_test_length
                accuracy_percent = 0
                if total_questions > 0:
                    accuracy_percent = int(round((total_score / float(total_questions)) * 100.0))
                    accuracy_percent = max(0, min(100, accuracy_percent))
                label = f"{rank}) {status_icon} {full_name} | ⭐ {avg:.1f} | 📚{tests_count} | 🎯{accuracy_percent}%"

            keyboard.append([{"text": self._trim(label, 60), "callback_data": f"student:view:{user_id}"}])
            rank += 1
        return keyboard

    # -------------------------
    # UI text renderers
    # -------------------------
    def format_student_profile(self, student_analytics: StudentAnalytics) -> str:
        status_label = self._status_label(student_analytics.status)

        # activity
        days = self.last_activity_days(student_analytics.user_id)
        activity_emoji = self.activity_status_emoji(student_analytics.user_id)
        activity_label = self._activity_status_label(days)

        last_dt_str = self._format_activity_datetime(self._get_last_activity_raw(student_analytics.user_id))

        lines = [
            f"👤 {student_analytics.full_name}",
            "",
            f"✅ Статус: {status_label}",
            f"📚 Пройдено тестів: {student_analytics.tests_count}",
            f"{activity_emoji} Активність: {activity_label}",
            f"🕒 Остання активність: {last_dt_str}",
        ]

        if student_analytics.tests_count > 0:
            lines.append(f"⭐ Середній бал: {student_analytics.avg_score:.1f}")
            lines.append(f"🏆 Найкращий бал: {student_analytics.best_score}")
            last_res = "—" if student_analytics.last_result_score is None else str(student_analytics.last_result_score)
            if student_analytics.last_result_topic_name:
                lines.append(f"📈 Останній результат: {last_res} ({student_analytics.last_result_topic_name})")
            else:
                lines.append(f"📈 Останній результат: {last_res}")
            lines.append(f"🎯 Точність: {student_analytics.accuracy_percent}%")
            if student_analytics.streak > 0:
                lines.append(f"🔥 Серія проходжень: {student_analytics.streak}")
            else:
                lines.append("🔥 Серія проходжень: 0")
        else:
            lines.append("⭐ Середній бал: —")
            lines.append("🏆 Найкращий бал: —")
            lines.append("📈 Останній результат: —")
            lines.append("🎯 Точність: —")
            lines.append("🔥 Серія проходжень: 0")

        # progress section
        if student_analytics.progress_scores:
            lines.append("")
            lines.append("📈 Прогрес:")
            lines.append(self.format_progress(student_analytics.progress_scores))

        # topics
        if student_analytics.topics:
            lines.append("")
            lines.append("📖 Теми:")
            for t in student_analytics.topics:
                lines.append(f"• {t.topic_name} — {t.avg_score:.1f}")

        return "\n".join(lines)

    def format_last_results(
        self,
        user_id: int,
        limit: int = 10,
    ) -> Tuple[str, List[int]]:
        rows = self._results_store.user_results(user_id)
        if not rows:
            return ("📊 Останні результати\n\nНемає результатів.", [])
        shown = rows[: max(1, limit)]
        lines = ["📊 Останні результати"]
        for r in shown:
            created_at = r[7]
            topic_name = r[6] or (self._topic_name_fn(r[5]) if r[5] else "—")
            score = int(r[3])
            attempts = int(r[4]) if r[4] is not None else 0

            # Color thresholds for 10-point tests:
            # 🟢 >= 7 ; 🟡 4-6 ; 🔴 < 4
            emoji = "🔴"
            if score >= 7:
                emoji = "🟢"
            elif 4 <= score <= 6:
                emoji = "🟡"

            date_short = self._format_date_short(created_at)
            lines.append(f"{emoji} {date_short} — {topic_name} — {score}/{self._default_test_length}")

        return ("\n".join(lines), [int(r[3]) for r in shown])

    def format_progress(self, scores_newest_first: Sequence[int]) -> str:
        if not scores_newest_first:
            return "📈 Прогрес: —"
        # show oldest -> newest
        scores = list(scores_newest_first)[::-1]
        arrows = []
        for i in range(1, len(scores)):
            if scores[i] > scores[i - 1]:
                arrows.append("⬆")
            elif scores[i] < scores[i - 1]:
                arrows.append("⬇")
            else:
                arrows.append("➡")
        seq = " → ".join(str(s) for s in scores)
        arrow_str = " ".join(arrows) if arrows else ""
        if arrow_str:
            return f"📈 Прогрес: {seq}\n{arrow_str}"
        return f"📈 Прогрес: {seq}"

    # -------------------------
    # Tops (basic)
    # -------------------------
    def compute_top_average_score(self, students: Dict[str, object], top_n: int = 5) -> List[Tuple[str, float, int, int]]:
        """
        Returns list of (full_name, avg_score, tests_count, accuracy_percent)
        """
        analytics: List[Tuple[str, float, int, int]] = []
        for _key, st in students.items():
            user_id = int(getattr(st, "user_id"))
            full_name = getattr(st, "full_name", "") or "Без імені"
            status = getattr(st, "status", "new")
            a = self.compute_student_analytics(user_id, full_name, status)
            if a.tests_count <= 0:
                continue
            analytics.append((full_name, a.avg_score, a.tests_count, a.accuracy_percent))
        analytics.sort(key=lambda x: (-x[1], -x[2], x[0].lower()))
        return analytics[:top_n]

    # -------------------------
    # Helpers
    # -------------------------
    def _parse_datetime(self, value: str) -> Optional[datetime]:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None

    def _format_date_short(self, value: str) -> str:
        dt = self._parse_datetime(value)
        if not dt:
            # best-effort: take first 10 chars
            return str(value)[:10]
        return dt.strftime("%d.%m")

    def _trim(self, text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "…"

    def _status_label(self, status: str) -> str:
        if status == "approved":
            return "схвалено"
        if status == "pending_approval":
            return "очікує схвалення"
        if status == "blocked":
            return "заблоковано"
        return "новий"

    def _get_last_activity_raw(self, user_id: int) -> Optional[str]:
        """
        Returns updated_at/last activity raw string via sessions_store.
        Safe: never throws.
        """
        try:
            return self._sessions_store.get_last_activity_at(user_id)
        except Exception:
            return None

    def _activity_status_label(self, days: Optional[float]) -> str:
        if days is None:
            return "невідомо"
        if days <= 3:
            return "активний"
        if days <= 14:
            return "давно не заходив"
        return "неактивний"

    def _format_activity_datetime(self, raw: Optional[str]) -> str:
        if not raw:
            return "—"
        dt = self._parse_datetime(str(raw))
        if not dt:
            # fallback: try first 16 chars
            s = str(raw).strip()
            if len(s) >= 16:
                return s[:16].replace("T", " ")
            return s[:10]
        return dt.strftime("%d.%m.%Y %H:%M")
