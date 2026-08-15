"""Разбор свободного текста ('Иванов 10, Петров 8, Сидоров отсутствует' /
'Иванов 10, Петров 8, Сидоров жоқ') в структурированные записи и сверка
фамилий с реальным списком класса.

Это упрощённый демо-парсер на регулярках — в проде на этом месте будет LLM,
но принцип не меняется: распознанное имя всегда сверяется со списком класса,
а не принимается от модели как есть.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

# Русские и казахские слова, обозначающие отсутствие ученика.
ABSENT_WORDS = ("отсутств", "нет", "болел", "болеет", "жоқ", "ауыр", "қатыспады", "прогул")

# Слова-маркеры причины пропуска (коды Kundelik.kz: П — уважительная причина
# по заявлению/справке/приказу, Н — неуважительная). Болезнь считается
# уважительной причиной сама по себе, даже без явного слова "справка".
EXCUSED_WORDS = ("боле", "ауыр", "справк", "заявлени", "заявлен", "приказ", "уважит", "себепті")

# Кириллица + казахские буквы (ә ғ қ ң ө ұ ү h і), чтобы фамилии на казахском
# ("Нұрлан", "Қайрат") тоже попадали под шаблон имени.
CYR = "А-ЯЁӘҒҚҢӨҰҮҺІа-яёәғқңөұүһі"
CYR_UPPER = "А-ЯЁӘҒҚҢӨҰҮҺІ"
CYR_LOWER = "а-яёәғқңөұүһі"

TOKEN_RE = re.compile(
    rf"([{CYR_UPPER}][{CYR_LOWER}\-]+(?:\s+[{CYR_UPPER}]\.?)?)\s*[-—:]?\s*"
    rf"(\d{{1,2}}(?![{CYR}])|(?i:отсутств\w*|болел\w*|нет\b|жоқ\b|ауыр\w*|қатыспады\w*))"
)


@dataclass
class GradeEntry:
    raw_name: str
    matched_name: str | None
    grade: str | None  # число оценки, либо None если "отсутствует"
    absent: bool
    ambiguous: list[str]


@dataclass
class AttendanceEntry:
    raw_name: str
    matched_name: str | None
    present: bool
    reason: str | None  # код Kundelik.kz "П"/"Н" при отсутствии, иначе None
    ambiguous: list[str]


@dataclass
class HomeworkEntry:
    text: str
    for_next_lesson: bool  # False = на текущий урок, True = на следующий


def _is_absent(word: str) -> bool:
    w = word.lower()
    return any(w.startswith(p) for p in ABSENT_WORDS)


def _detect_reason(segment_lower: str) -> str:
    """Код причины пропуска как в Kundelik.kz: 'П' — уважительная причина
    (заявление/справка/приказ, включая болезнь), 'Н' — неуважительная (в т.ч.
    по умолчанию, если причина не указана)."""
    if any(w in segment_lower for w in EXCUSED_WORDS):
        return "П"
    return "Н"


def _surnames_match(a: str, b: str) -> bool:
    """Сравнивает фамилии с учётом падежных окончаний, включая женские фамилии на -а,
    где меняется сама последняя буква ('Ахметова' / 'Ахметовой'), а не только дописывается
    хвост ('Иванов' / 'Иванову'). Поэтому последняя буква короткой формы не учитывается —
    сравнивается общий префикс длиной (короче слово - 1)."""
    if a == b:
        return True
    n = min(len(a), len(b)) - 1
    if n < 3:
        return False
    return a[:n] == b[:n] and abs(len(a) - len(b)) <= 3


def match_student(raw_name: str, roster: list[str]) -> tuple[str | None, list[str]]:
    """Сверяет распознанную фамилию с реальным списком класса.
    Возвращает (точное совпадение или None, список кандидатов если неоднозначно)."""
    raw_surname = raw_name.strip().lower().split()[0]
    matches = [full for full in roster if _surnames_match(raw_surname, full.lower().split()[0])]
    if len(matches) == 1:
        return matches[0], []
    if len(matches) > 1:
        return None, matches
    return None, []


def detect_class(text: str, classes: list[str]) -> str | None:
    """Ищет явное упоминание класса в тексте ('8А', '8а' и т.п.)."""
    t = text.lower()
    for cls in classes:
        if re.search(rf"(?<![{CYR_LOWER}0-9]){re.escape(cls.lower())}(?![{CYR_LOWER}0-9])", t):
            return cls
    return None


def detect_subject(text: str, subject_stems: dict[str, list[str]]) -> str | None:
    """Ищет упоминание предмета по стему слова, проверяя стемы всех языков сразу."""
    t = text.lower()
    for subject_id, stems in subject_stems.items():
        if any(stem in t for stem in stems):
            return subject_id
    return None


def detect_hours_per_week(text: str) -> int:
    """Ищет 'N час(ов) в неделю' / 'аптасына N сағат'. По умолчанию — 1."""
    m = re.search(r"(\d+)\s*(?:час\w*|сағат)", text.lower())
    return int(m.group(1)) if m else 1


def detect_quarter(date_str: str) -> int | None:
    """Грубая оценка учебной четверти по дате (ДД.ММ.ГГГГ) — для метаданных
    сохранённых ссылок на КСП/КТП, по типичному календарю четвертей в РК.
    Для дат вне учебного года (июнь-август) возвращает None."""
    try:
        month = int(date_str.split(".")[1])
    except (IndexError, ValueError):
        return None
    if month in (9, 10):
        return 1
    if month in (11, 12):
        return 2
    if month in (1, 2, 3):
        return 3
    if month in (4, 5):
        return 4
    return None


def detect_academic_year(date_str: str) -> str | None:
    """Учебный год в формате 'ГГГГ-ГГГГ' по дате (ДД.ММ.ГГГГ): с сентября
    считается началом следующего учебного года."""
    try:
        _, month, year = date_str.split(".")
        month, year = int(month), int(year)
    except (ValueError, IndexError):
        return None
    return f"{year}-{year + 1}" if month >= 9 else f"{year - 1}-{year}"


def detect_school_year(text: str) -> str | None:
    """Явно указанный учителем учебный год вида '2026-2027' / '2026–2027' в
    свободном тексте (например, при генерации КТП). None, если не указан —
    тогда вызывающий код сам решает дефолт (обычно текущий учебный год)."""
    m = re.search(r"\b(20\d{2})\s*[-–—]\s*(20\d{2})\b", text)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def default_ktp_school_year(today: datetime.date | None = None) -> str:
    """Дефолт учебного года для КТП — не то же самое, что 'текущий учебный год
    по календарю' (detect_academic_year): с июня по август учитель обычно уже
    готовит план на СЛЕДУЮЩИЙ учебный год (который начнётся в сентябре этого
    же года), а не отчитывается за только что завершившийся. Поэтому с июня
    порог сдвинут раньше сентября."""
    today = today or datetime.date.today()
    start_year = today.year if today.month >= 6 else today.year - 1
    return f"{start_year}-{start_year + 1}"


def detect_start_date(text: str) -> str | None:
    """Явно указанная дата начала занятий по предмету ('с 02.09', '02.09.2026')
    в свободном тексте при генерации КТП. В отличие от detect_date здесь нет
    фолбэка на сегодня — None означает 'не указано', и вызывающий код подставляет
    осмысленный дефолт (1 сентября выбранного учебного года), а не текущий день."""
    m = re.search(r"\b(\d{1,2})[.\-](\d{1,2})(?:[.\-](\d{2,4}))?\b", text)
    if not m:
        return None
    day, month, year = m.groups()
    day, month = int(day), int(month)
    year = int(year) if year else datetime.date.today().year
    if year < 100:
        year += 2000
    try:
        datetime.date(year, month, day)
    except ValueError:
        return None
    return f"{day:02d}.{month:02d}.{year}"


def detect_date(text: str) -> str:
    """Определяет дату записи: явное число, 'вчера'/'кеше', иначе — сегодня."""
    t = text.lower()
    today = datetime.date.today()
    if "вчера" in t or "кеше" in t:
        return (today - datetime.timedelta(days=1)).strftime("%d.%m.%Y")
    m = re.search(r"\b(\d{1,2})[.\-](\d{1,2})(?:[.\-](\d{2,4}))?\b", text)
    if m:
        day, month, year = m.groups()
        year = year or str(today.year)
        return f"{int(day):02d}.{int(month):02d}.{year}"
    return today.strftime("%d.%m.%Y")


def parse_grades(text: str, roster: list[str]) -> list[GradeEntry]:
    entries: list[GradeEntry] = []
    for raw_name, value in TOKEN_RE.findall(text):
        absent = _is_absent(value)
        grade = None if absent else value
        matched, ambiguous = match_student(raw_name, roster)
        entries.append(
            GradeEntry(
                raw_name=raw_name.strip(),
                matched_name=matched,
                grade=grade,
                absent=absent,
                ambiguous=ambiguous,
            )
        )
    return entries


# ---------- посещаемость ----------

NAME_RE = rf"[{CYR_UPPER}][{CYR_LOWER}\-]+(?:\s+[{CYR_UPPER}]\.?)?"

# Статусные слова посещаемости — переиспользуют ABSENT_WORDS для "нет",
# плюс отдельные слова для "присутствует", т.к. это не то же самое, что оценка.
_PRESENT_WORDS = ("присутств", "бар", "келді")
# Не заякорено на конец сегмента ('$'), т.к. после статуса может идти
# причина пропуска ('отсутствует по справке') — она ищется отдельно, во всём
# сегменте целиком, через _detect_reason.
_ATTENDANCE_STATUS_RE = re.compile(
    rf"(?:{'|'.join(ABSENT_WORDS)}|{'|'.join(_PRESENT_WORDS)})\w*",
    re.IGNORECASE,
)


def _split_names(segment: str) -> list[str]:
    return [n for n in re.split(r"\s+(?:и|және)\s+", segment.strip()) if n]


def parse_attendance(text: str, roster: list[str]) -> list[AttendanceEntry]:
    """Разбирает список посещаемости в свободном тексте. Поддерживает как
    одиночные пары ('Иванов отсутствует, Петров болел'), так и список с
    одним общим статусом в конце ('Иванов, Петров, Сидоров отсутствуют') —
    в этом случае статус применяется ко всем именам, накопленным до него."""
    entries: list[AttendanceEntry] = []
    pending_names: list[str] = []

    for raw_segment in text.split(","):
        segment = raw_segment.strip()
        if not segment:
            continue
        m = _ATTENDANCE_STATUS_RE.search(segment)
        if not m:
            pending_names.extend(_split_names(segment))
            continue

        status_word = m.group(0)
        names_part = segment[: m.start()].strip(" -—:")
        names = pending_names + _split_names(names_part)
        pending_names = []
        present = not _is_absent(status_word)
        reason = None if present else _detect_reason(segment.lower())

        for raw_name in names:
            matched, ambiguous = match_student(raw_name, roster)
            entries.append(
                AttendanceEntry(
                    raw_name=raw_name, matched_name=matched, present=present, reason=reason, ambiguous=ambiguous
                )
            )

    return entries


# ---------- домашнее задание ----------

_HOMEWORK_TRIGGER_RE = re.compile(
    r"домашн\w*\s+задани\w*|домашк\w*|\bдз\b|тапсырма\w*|үй\s+тапсырмас\w*",
    re.IGNORECASE,
)
_NEXT_LESSON_RE = re.compile(r"(?:на\s+)?следующ\w*\s+урок\w*|келесі\s+сабақ\w*", re.IGNORECASE)


def detect_homework(text: str) -> HomeworkEntry | None:
    """Ищет триггер ДЗ и извлекает текст задания после него. Определяет,
    задаётся ли ДЗ на этот урок (по умолчанию) или на следующий."""
    m = _HOMEWORK_TRIGGER_RE.search(text)
    if not m:
        return None
    for_next = bool(_NEXT_LESSON_RE.search(text))
    rest = _NEXT_LESSON_RE.sub("", text[m.end():]).strip(" :–—-")
    if not rest:
        return None
    return HomeworkEntry(text=rest, for_next_lesson=for_next)
