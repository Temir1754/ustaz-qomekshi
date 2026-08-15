"""LLM-генерация контента через облачный DeepSeek API (OpenAI-совместимый).

Тот же принцип, что и в kundelik-teacher-bot (там был Claude): структура
документов (шапка, колонки, порядок этапов урока) всегда задаётся кодом в
docx_export.py — LLM заполняет только содержательную часть. Официальные коды
целей обучения ("10.4.1.25" и т.п.) модель НЕ генерирует — это нормативные
коды из реальной учебной программы, выдумывать их нельзя.

LLM в classify_intent только маршрутизирует, какой сценарий запускать —
реальные данные (ФИО, оценка, текст ДЗ) извлекает и сверяет с реальным
списком класса детерминированный код в parsing.py, а не то, что вернула
модель напрямую.
"""

from __future__ import annotations

import json
import os

from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    return _client


def _model() -> str:
    return os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def _lang_name(lang: str) -> str:
    return "казахском" if lang == "kk" else "русском"


async def _json_completion(prompt: str, max_tokens: int) -> dict:
    client = _get_client()
    response = await client.chat.completions.create(
        model=_model(),
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(response.choices[0].message.content)


async def generate_ksp_content(topic: str, cls: str, subject_label: str, lang: str) -> dict:
    prompt = (
        f"Ты — методист. Составь содержание урока (тезисно, не полный конспект) "
        f"для краткосрочного плана (ҚМЖ) по теме «{topic}», предмет «{subject_label}», класс {cls}.\n"
        "Нужно ровно 4 этапа урока в фиксированном порядке:\n"
        "1) Ұйымдастыру кезеңі (оргмомент, 5 мин)\n"
        "2) Сабақтың басы (индивидуальная работа — вводное задание по теме)\n"
        "3) Сабақтың ортасы (парная/групповая работа — задание с дифференциацией по уровню)\n"
        "4) Сабақтың соңы (обратная связь, домашнее задание)\n"
        "Для каждого этапа: действие учителя, действие ученика, критерий оценивания "
        "(словами, без баллов и без официальных кодов целей обучения), ресурсы.\n"
        f"Пиши весь текст на {_lang_name(lang)} языке. Кратко, по существу, без вступлений.\n\n"
        "Ответь СТРОГО в формате JSON (без markdown-разметки) по схеме:\n"
        '{"goal": "цель урока, 1-2 предложения", '
        '"stages": [{"teacher_action": "...", "student_action": "...", '
        '"assessment": "...", "resources": "..."}, ... ровно 4 элемента]}'
    )
    return await _json_completion(prompt, max_tokens=2000)


async def generate_ktp_outline(subject_label: str, cls: str, total_hours: int, lang: str) -> list[tuple[str, str]]:
    """Возвращает список (раздел, тема_урока) длиной ровно total_hours."""
    prompt = (
        f"Составь укрупнённый тематический план на учебный год по предмету «{subject_label}» "
        f"для {cls} класса, всего {total_hours} уроков в году.\n"
        "Раздели материал на разделы, типичные для школьной программы этого предмета и класса. "
        "Для каждого раздела укажи количество уроков (lesson_count) и список названий отдельных "
        "уроков внутри раздела (по одному короткому названию на урок). Сумма lesson_count по всем "
        f"разделам должна быть как можно ближе к {total_hours}.\n"
        "Не указывай официальные коды целей обучения — только названия тем.\n"
        f"Пиши на {_lang_name(lang)} языке. Кратко, без вступлений.\n\n"
        "Ответь СТРОГО в формате JSON (без markdown-разметки) по схеме:\n"
        '{"sections": [{"name": "название раздела", "lesson_count": число, '
        '"lessons": ["тема урока", ...]}, ...]}'
    )
    data = await _json_completion(prompt, max_tokens=6000)

    outline: list[tuple[str, str]] = []
    for section in data["sections"]:
        lessons = section.get("lessons") or []
        count = section.get("lesson_count") or len(lessons)
        for i in range(count):
            name = lessons[i] if i < len(lessons) else f"{section['name']} — сабақ {i + 1}"
            outline.append((section["name"], name))

    if not outline:
        return outline
    if len(outline) > total_hours:
        outline = outline[:total_hours]
    else:
        last_section = outline[-1][0]
        while len(outline) < total_hours:
            outline.append((last_section, "Қайталау / практикалық сабақ"))
    return outline


async def classify_intent(text: str, lang: str) -> str:
    """Определяет намерение учителя по свободному тексту: оценка, посещаемость,
    домашнее задание, запрос КСП или обычный вопрос/диалог.

    LLM здесь только маршрутизирует — какой из четырёх сценариев запускать.
    Сами данные (ФИО, оценка, текст ДЗ) извлекает детерминированный парсер
    в parsing.py и всегда сверяет их с реальным списком класса, а не
    принимает от модели как есть — тот же принцип, что и в остальном боте."""
    prompt = (
        "Определи, что из перечисленного хочет сделать учитель, написав это сообщение боту:\n"
        "- grade — выставить оценку ученику/ученикам\n"
        "- attendance — отметить присутствие/отсутствие учеников на уроке\n"
        "- homework — задать домашнее задание\n"
        "- ksp — попросить составить краткосрочный план урока (КСП/ҚМЖ)\n"
        "- question — что-то другое: вопрос, просьба составить тест, свободный диалог\n\n"
        f"Сообщение: «{text}»\n\n"
        'Ответь СТРОГО в формате JSON: {"action": "одно из значений выше"}'
    )
    data = await _json_completion(prompt, max_tokens=100)
    return data["action"]


async def generate_ai_reply(user_text: str, lang: str) -> str:
    prompt = (
        f"Ты — AI-помощник учителя в Telegram-боте Ustaz Qömekşi. Учитель написал: «{user_text}».\n"
        f"Ответь по существу и кратко (это чат, не эссе), на {_lang_name(lang)} языке. "
        "Если просят вопросы, тест или домашнее задание — сразу дай готовый результат, "
        "а не план о том, как его составить."
    )
    client = _get_client()
    response = await client.chat.completions.create(
        model=_model(),
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content
