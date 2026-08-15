"""Ustaz Qömekşi — Telegram-бот для учителей (@ustaz_agent_bot).

Работает на тестовых данных (mock_data.py) — реального подключения
к Kundelik.kz здесь нет. Цель файла — дать учителю пощупать сценарии
живьём в Telegram: язык обучения, оценки, посещаемость, КСП/КТП, аналитика.

AI-логика (ai_generate.py) работает через облачный DeepSeek API вместо Claude,
который использовался в исходном kundelik-teacher-bot — остальная логика та же.
"""

from __future__ import annotations

import asyncio
import datetime
import html
import logging
import os
import secrets
import string

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from dotenv import load_dotenv

from mock_data import (
    CLASSES,
    SUBJECT_IDS,
    SUBJECT_LABELS,
    SUBJECT_STEMS,
    ROSTERS,
    TODAY_ABSENT,
    CLASS_AVERAGES,
    TODAY_SCHEDULE,
)
from parsing import (
    TOKEN_RE,
    parse_grades,
    parse_attendance,
    detect_homework,
    detect_class,
    detect_subject,
    detect_date,
    detect_hours_per_week,
    detect_quarter,
    detect_academic_year,
    detect_school_year,
    detect_start_date,
    default_ktp_school_year,
)
from i18n import t, btn_variants, LANG_NAMES, DEFAULT_LANG
from docx_export import build_ktp_docx, build_ksp_docx, resolve_ksp_content, lesson_weekdays
import ai_generate
import cloud_upload
import kundelik_api

load_dotenv()
logging.basicConfig(level=logging.INFO)

router = Router()

JOURNAL: list[dict] = []
ATTENDANCE_LOG: list[dict] = []
HOMEWORK_LOG: list[dict] = []
ACTIVITY_LOG: list[dict] = []
# облако для КСП/КТП — у каждого учителя своё, выбирается в Настройках
TEACHER_CLOUD: dict[int, dict] = {}  # user_id -> {"provider": ..., "token": ...}
CLOUD_LINKS: list[dict] = []  # метаданные вместо папок — учитель/год/предмет/класс/четверть/дата/тема/ссылка
USER_CONTEXT: dict[int, dict] = {}
USER_LANG: dict[int, str] = {}
KTP_DOCS: dict[int, bytes] = {}
KSP_DOCS: dict[int, bytes] = {}
# Разобранные по урокам КТП — источник тем для КСП: учитель выбирает урок
# кнопкой из уже составленного КТП, а не печатает тему заново, поэтому КСП
# физически не может разойтись с темой в КТП.
KTP_OUTLINES: dict[int, dict[tuple[str, str], dict]] = {}  # user_id -> {(cls, subject_label): record}
# Структурированное содержимое последнего сгенерированного КСП/КТП — источник
# и для просмотра (ksp:view/ktp:view), и для редактирования по полям
# (ksp:edit/ktp:edit): docx каждый раз перестраивается из этих же данных,
# поэтому просмотр, документ и .docx никогда не расходятся.
KSP_CONTENT: dict[int, dict] = {}  # user_id -> {"cls","subject_label","topic","teacher_name","content"}
KTP_CONTENT: dict[int, dict] = {}  # user_id -> {"cls","subject_label","hours_per_week","total_hours","outline"}

# Сессия Kundelik.kz учителя, полученная через расширение (см. extension/) —
# cookie QundelikAuth_a + id школы со страницы расписания. Пароль расширение
# никогда не видит.
KUNDELIK_SESSIONS: dict[int, dict] = {}  # user_id -> {"cookie": ..., "school": str | None}
PENDING_PAIR_CODES: dict[str, int] = {}  # код привязки -> user_id
PAIR_WEB_PORT = 8092

QUESTION_WORDS = ("вопрос", "сұрақ")
TEST_WORDS = ("тест",)
HOMEWORK_WORDS = ("домашн", "тапсырма")

# Короткие подписи дней недели (0=Пн..4=Пт) для кнопок выбора дней уроков в КТП.
WEEKDAY_LABELS = {
    "ru": ["Пн", "Вт", "Ср", "Чт", "Пт"],
    "kk": ["Дс", "Сс", "Ср", "Бс", "Жм"],
}

# Подписи всех кнопок главного меню (на всех языках) — reply-клавиатура остаётся
# видимой в любом состоянии FSM, поэтому хендлеры, ловящие "любой текст" внутри
# состояния (ввод темы КСП/КТП, оценок, токена), должны явно её игнорировать —
# иначе нажатие другой кнопки меню посреди диалога проглатывается как ввод
# (подтверждённый баг: "КСП" → "Напишите тему" → нажали "КТП" → бот сгенерировал
# КСП по теме «КТП» вместо перехода в раздел КТП).
_MENU_BUTTON_KEYS = (
    "btn_today", "btn_myclasses", "btn_grades", "btn_attendance",
    "btn_ksp", "btn_ktp", "btn_ai", "btn_settings",
)
ALL_MENU_BUTTONS: set[str] = set().union(*(btn_variants(k) for k in _MENU_BUTTON_KEYS))


def get_lang(user_id: int) -> str:
    return USER_LANG.get(user_id, DEFAULT_LANG)


def log_activity(icon: str, summary: str) -> None:
    ACTIVITY_LOG.append({"icon": icon, "summary": summary, "ts": datetime.datetime.now().strftime("%d.%m.%Y · %H:%M")})


async def _send_chunked(message_target: Message, text: str, limit: int = 3500) -> None:
    """Отправляет текст несколькими сообщениями, если он не помещается в лимит
    Telegram (4096 символов) — актуально для просмотра КТП на весь год."""
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > limit and chunk:
            await message_target.answer(chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        await message_target.answer(chunk)


# ---------- клавиатуры ----------

def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru")],
            [InlineKeyboardButton(text="🇰🇿 Қазақша", callback_data="lang:kk")],
        ]
    )


def main_menu_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(lang, "btn_today")), KeyboardButton(text=t(lang, "btn_myclasses"))],
            [KeyboardButton(text=t(lang, "btn_grades")), KeyboardButton(text=t(lang, "btn_attendance"))],
            [KeyboardButton(text=t(lang, "btn_ksp")), KeyboardButton(text=t(lang, "btn_ktp"))],
            [KeyboardButton(text=t(lang, "btn_ai")), KeyboardButton(text=t(lang, "btn_settings"))],
        ],
        resize_keyboard=True,
    )


def classes_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=c, callback_data=f"{prefix}:{c}")] for c in CLASSES]
    )


def subjects_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=SUBJECT_LABELS[lang][sid], callback_data=f"subj:{sid}")]
            for sid in SUBJECT_IDS
        ]
    )


def cloud_provider_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"cloudprov:{key}")]
            for key, label in cloud_upload.PROVIDER_LABELS.items()
        ]
    )


def quick_actions_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "quick_btn_grades"), callback_data="quick:grades")],
            [InlineKeyboardButton(text=t(lang, "quick_btn_attendance"), callback_data="quick:attendance")],
            [InlineKeyboardButton(text=t(lang, "quick_btn_ksp"), callback_data="quick:ksp")],
            [InlineKeyboardButton(text=t(lang, "quick_btn_test"), callback_data="quick:test")],
            [InlineKeyboardButton(text=t(lang, "quick_btn_analytics"), callback_data="quick:analytics")],
            [InlineKeyboardButton(text=t(lang, "quick_btn_recent"), callback_data="quick:recent")],
        ]
    )


# ---------- состояния ----------

class LangFlow(StatesGroup):
    choosing = State()


class GradeFlow(StatesGroup):
    choosing_class = State()
    choosing_subject = State()
    entering_grades = State()
    clarify_class = State()
    clarify_subject = State()
    resolving = State()
    confirming = State()


class AttendanceFlow(StatesGroup):
    choosing_class = State()
    confirming = State()


class AttendanceTextFlow(StatesGroup):
    """Свободный ввод посещаемости текстом ('Иванов отсутствует, Петров болел') —
    отдельно от AttendanceFlow (меню с моковыми данными отсутствующих)."""

    clarify_class = State()
    resolving = State()
    confirming = State()


class HomeworkFlow(StatesGroup):
    clarify_class = State()
    clarify_subject = State()
    confirming = State()


class KSPFlow(StatesGroup):
    entering_topic = State()
    choosing_ktp = State()
    choosing_section = State()
    choosing_lesson = State()


class KTPFlow(StatesGroup):
    entering_params = State()
    choosing_school_year = State()
    entering_textbook = State()
    choosing_weekdays = State()


class DocEditFlow(StatesGroup):
    """Точечное редактирование уже сгенерированных КСП/КТП по полям —
    учитель выбирает, что менять, кнопками, и присылает новый текст, после
    чего .docx перестраивается из обновлённых данных (KSP_CONTENT/KTP_CONTENT)."""

    ksp_entering_value = State()
    ktp_entering_value = State()


class CloudSettingsFlow(StatesGroup):
    """Выбор облачного провайдера для автозагрузки КСП/КТП (у каждого
    учителя свой) и ввод его API-токена — один раз, в Настройках."""

    choosing_provider = State()
    entering_token = State()


# ---------- язык обучения и главное меню ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(LangFlow.choosing)
    await message.answer("🌐 Выберите язык обучения / Оқыту тілін таңдаңыз:", reply_markup=lang_kb())


@router.callback_query(LangFlow.choosing, F.data.startswith("lang:"))
async def lang_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = callback.data.split(":", 1)[1]
    USER_LANG[callback.from_user.id] = lang
    name = html.escape(callback.from_user.first_name or "")
    await callback.message.edit_text(f"🌐 {LANG_NAMES[lang]}")
    await callback.message.answer(t(lang, "welcome", name=name), reply_markup=main_menu_kb(lang))
    await state.clear()
    await callback.answer()


@router.message(F.text.in_(btn_variants("btn_today")))
async def today_schedule(message: Message) -> None:
    lang = get_lang(message.from_user.id)
    session = KUNDELIK_SESSIONS.get(message.from_user.id)

    if session and session.get("school"):
        try:
            data = await kundelik_api.fetch_schedule(session["cookie"], session["school"])
            real_lines = kundelik_api.build_today_lines_or_log(data)
            if real_lines is not None:
                await message.answer("\n".join(real_lines))
                return
            await message.answer(t(lang, "kundelik_schedule_wip"))
        except kundelik_api.KundelikAuthError:
            KUNDELIK_SESSIONS.pop(message.from_user.id, None)
            await message.answer(t(lang, "kundelik_session_expired"))
        except Exception:
            logging.exception("Kundelik schedule fetch failed")
            await message.answer(t(lang, "kundelik_fetch_failed"))

    lines = [t(lang, "today_header")]
    for time_, cls, subj_id, room in TODAY_SCHEDULE:
        lines.append(t(lang, "today_line", time=time_, cls=cls, subject=SUBJECT_LABELS[lang][subj_id], room=room))
    await message.answer("\n".join(lines))


async def _my_classes(message: Message, lang: str) -> None:
    lines = [t(lang, "myclasses_header")]
    for cls in CLASSES:
        avg = CLASS_AVERAGES.get(cls, "—")
        total = len(ROSTERS.get(cls, []))
        lines.append(t(lang, "myclasses_line", cls=cls, avg=avg, total=total))
    await message.answer("\n".join(lines))


@router.message(F.text.in_(btn_variants("btn_myclasses")))
async def my_classes(message: Message) -> None:
    await _my_classes(message, get_lang(message.from_user.id))


@router.message(F.text.in_(btn_variants("btn_settings")))
async def settings(message: Message) -> None:
    lang = get_lang(message.from_user.id)
    name = html.escape(message.from_user.first_name or "")
    cloud = TEACHER_CLOUD.get(message.from_user.id)
    cloud_status = (
        t(lang, "settings_cloud_set", provider=cloud_upload.PROVIDER_LABELS[cloud["provider"]])
        if cloud
        else t(lang, "settings_cloud_unset")
    )
    kundelik_status = (
        t(lang, "settings_kundelik_set")
        if message.from_user.id in KUNDELIK_SESSIONS
        else t(lang, "settings_kundelik_unset")
    )
    lines = [
        t(lang, "settings_title"),
        t(lang, "settings_teacher", name=name),
        t(lang, "settings_classes", classes=", ".join(CLASSES)),
        t(lang, "settings_lang", lang_name=LANG_NAMES[lang]),
        t(lang, "settings_ext"),
        t(lang, "settings_reminders"),
        cloud_status,
        kundelik_status,
    ]
    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t(lang, "settings_cloud_btn"), callback_data="settings:cloud")],
                [InlineKeyboardButton(text=t(lang, "settings_kundelik_btn"), callback_data="settings:kundelik")],
            ]
        ),
    )


def _gen_pair_code() -> str:
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))


@router.callback_query(F.data == "settings:kundelik")
async def settings_kundelik_start(callback: CallbackQuery) -> None:
    lang = get_lang(callback.from_user.id)
    code = _gen_pair_code()
    PENDING_PAIR_CODES[code] = callback.from_user.id
    await callback.message.answer(t(lang, "kundelik_pair_instructions", code=code))
    await callback.answer()


@router.callback_query(F.data == "settings:cloud")
async def settings_cloud_start(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    await state.set_state(CloudSettingsFlow.choosing_provider)
    await callback.message.answer(t(lang, "cloud_choose_provider"), reply_markup=cloud_provider_kb())
    await callback.answer()


@router.callback_query(CloudSettingsFlow.choosing_provider, F.data.startswith("cloudprov:"))
async def cloud_provider_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    provider = callback.data.split(":", 1)[1]
    await state.update_data(provider=provider)
    await state.set_state(CloudSettingsFlow.entering_token)
    await callback.message.edit_text(
        t(lang, "cloud_provider_label", provider=cloud_upload.PROVIDER_LABELS[provider])
    )
    await callback.message.answer(t(lang, "cloud_enter_token"))
    await callback.answer()


@router.message(CloudSettingsFlow.entering_token, ~F.text.in_(ALL_MENU_BUTTONS))
async def cloud_token_entered(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    data = await state.get_data()
    provider = data["provider"]
    token = message.text.strip()
    TEACHER_CLOUD[message.from_user.id] = {"provider": provider, "token": token}
    await state.clear()
    try:
        await message.delete()  # токен не должен оставаться текстом в истории чата
    except Exception:
        logging.exception("Could not delete token message")
    await message.answer(
        t(lang, "cloud_saved", provider=cloud_upload.PROVIDER_LABELS[provider])
    )


@router.message(F.text.in_(btn_variants("btn_ai")))
async def ai_helper_intro(message: Message) -> None:
    await message.answer(t(get_lang(message.from_user.id), "ai_intro"))


# ---------- Оценки ----------

async def _grades_start(message: Message, state: FSMContext, lang: str) -> None:
    await state.set_state(GradeFlow.choosing_class)
    await message.answer(t(lang, "grades_choose_class"), reply_markup=classes_kb("cls"))


@router.message(F.text.in_(btn_variants("btn_grades")))
async def grades_start(message: Message, state: FSMContext) -> None:
    await _grades_start(message, state, get_lang(message.from_user.id))


@router.callback_query(GradeFlow.choosing_class, F.data.startswith("cls:"))
async def grades_class_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    cls = callback.data.split(":", 1)[1]
    await state.update_data(cls=cls)
    await state.set_state(GradeFlow.choosing_subject)
    await callback.message.edit_text(t(lang, "grades_class_label", cls=cls))
    await callback.message.answer(t(lang, "grades_subject_prompt"), reply_markup=subjects_kb(lang))
    await callback.answer()


@router.callback_query(GradeFlow.choosing_subject, F.data.startswith("subj:"))
async def grades_subject_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    subject_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    USER_CONTEXT[callback.from_user.id] = {"cls": data["cls"], "subject": subject_id}
    await state.update_data(subject=subject_id)
    await state.set_state(GradeFlow.entering_grades)
    await callback.message.edit_text(t(lang, "grades_subject_label", subject=SUBJECT_LABELS[lang][subject_id]))
    await callback.message.answer(t(lang, "grades_enter_prompt"))
    await callback.answer()


async def _show_next_ambiguous_or_confirm(message_target: Message, state: FSMContext, lang: str) -> None:
    data = await state.get_data()
    entries = data["entries"]
    cls = data["cls"]

    for idx, entry in enumerate(entries):
        if entry.matched_name is None:
            candidates = entry.ambiguous or ROSTERS[cls]
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=name, callback_data=f"pick:{idx}:{ROSTERS[cls].index(name)}")]
                    for name in candidates
                ]
                + [[InlineKeyboardButton(text=t(lang, "grades_skip_btn"), callback_data=f"pick:{idx}:skip")]]
            )
            await state.set_state(GradeFlow.resolving)
            await message_target.answer(
                t(lang, "grades_ambiguous_prompt", name=html.escape(entry.raw_name), cls=cls),
                reply_markup=kb,
            )
            return

    kept = [e for e in entries if e.matched_name is not None]
    if not kept:
        await message_target.answer(t(lang, "grades_not_recognized"))
        await state.set_state(GradeFlow.entering_grades)
        return

    date_str = data.get("date") or t(lang, "date_today_word")
    subject_label = SUBJECT_LABELS[lang][data["subject"]]
    lines = [
        t(lang, "grades_recognized_header"),
        f"{t(lang, 'field_class')} {cls}",
        f"{t(lang, 'field_subject')} {subject_label}",
        f"{t(lang, 'field_date')} {date_str}",
        "",
    ]
    for e in kept:
        value = f"{t(lang, 'absent_label')} ❌" if e.absent else f"<b>{e.grade}</b> ⭐"
        lines.append(f"👤 {e.matched_name} — {value}")
    lines.append("\n" + t(lang, "grades_confirm_footer"))

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t(lang, "btn_confirm"), callback_data="gr:confirm"),
                InlineKeyboardButton(text=t(lang, "btn_edit"), callback_data="gr:edit"),
            ]
        ]
    )
    await state.set_state(GradeFlow.confirming)
    await message_target.answer("\n".join(lines), reply_markup=kb)


async def finalize_grade_text(
    message_target: Message, state: FSMContext, text: str, cls: str, subject: str, date: str, lang: str
) -> None:
    entries = parse_grades(text, ROSTERS[cls])
    if not entries:
        await message_target.answer(t(lang, "grades_not_recognized"))
        await state.update_data(cls=cls, subject=subject)
        await state.set_state(GradeFlow.entering_grades)
        return
    await state.update_data(cls=cls, subject=subject, date=date, entries=entries)
    await _show_next_ambiguous_or_confirm(message_target, state, lang)


@router.message(GradeFlow.entering_grades, ~F.text.in_(ALL_MENU_BUTTONS))
async def grades_text_received(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    data = await state.get_data()
    text = message.text or ""
    await finalize_grade_text(message, state, text, data["cls"], data["subject"], detect_date(text), lang)


# --- свободный ввод без меню: "Иванову 10 по информатике в 8А вчера" ---

@router.message(StateFilter(None), F.text.regexp(TOKEN_RE))
async def freeform_grade_start(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    text = message.text or ""
    ctx = USER_CONTEXT.get(message.from_user.id, {})
    cls = detect_class(text, CLASSES) or ctx.get("cls")
    subject = detect_subject(text, SUBJECT_STEMS) or ctx.get("subject")
    date = detect_date(text)

    if not cls:
        await state.update_data(pending_text=text, pending_subject=subject, pending_date=date)
        await state.set_state(GradeFlow.clarify_class)
        await message.answer(t(lang, "freeform_clarify_class"), reply_markup=classes_kb("freecls"))
        return

    if not subject:
        await state.update_data(pending_text=text, pending_cls=cls, pending_date=date)
        await state.set_state(GradeFlow.clarify_subject)
        await message.answer(t(lang, "freeform_clarify_subject", cls=cls), reply_markup=subjects_kb(lang))
        return

    await finalize_grade_text(message, state, text, cls, subject, date, lang)


@router.callback_query(GradeFlow.clarify_class, F.data.startswith("freecls:"))
async def freeform_class_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    cls = callback.data.split(":", 1)[1]
    data = await state.get_data()
    text, subject, date = data["pending_text"], data.get("pending_subject"), data.get("pending_date")

    if not subject:
        await state.update_data(pending_cls=cls)
        await state.set_state(GradeFlow.clarify_subject)
        await callback.message.edit_text(f"{t(lang, 'field_class')} {cls}")
        await callback.message.answer(t(lang, "grades_subject_prompt"), reply_markup=subjects_kb(lang))
        await callback.answer()
        return

    await callback.message.edit_text(f"{t(lang, 'field_class')} {cls}")
    await finalize_grade_text(callback.message, state, text, cls, subject, date, lang)
    await callback.answer()


@router.callback_query(GradeFlow.clarify_subject, F.data.startswith("subj:"))
async def freeform_subject_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    subject = callback.data.split(":", 1)[1]
    data = await state.get_data()
    cls = data.get("pending_cls") or data.get("cls")
    text, date = data["pending_text"], data.get("pending_date")

    await callback.message.edit_text(t(lang, "grades_subject_label", subject=SUBJECT_LABELS[lang][subject]))
    await finalize_grade_text(callback.message, state, text, cls, subject, date, lang)
    await callback.answer()


@router.callback_query(GradeFlow.resolving, F.data.startswith("pick:"))
async def grades_resolve_pick(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    _, idx_str, choice = callback.data.split(":", 2)
    idx = int(idx_str)
    data = await state.get_data()
    entries = data["entries"]
    cls = data["cls"]

    if choice == "skip":
        entries[idx].matched_name = "__SKIP__"
    else:
        entries[idx].matched_name = ROSTERS[cls][int(choice)]

    entries = [e for e in entries if e.matched_name != "__SKIP__"]
    await state.update_data(entries=entries)
    await callback.message.edit_text("✓")
    await _show_next_ambiguous_or_confirm(callback.message, state, lang)
    await callback.answer()


@router.callback_query(GradeFlow.confirming, F.data == "gr:edit")
async def grades_edit(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    await state.set_state(GradeFlow.entering_grades)
    await callback.message.edit_text(t(lang, "grades_edit_prompt"))
    await callback.answer()


@router.callback_query(GradeFlow.confirming, F.data == "gr:confirm")
async def grades_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    data = await state.get_data()
    cls, subject, entries, date = data["cls"], data["subject"], data["entries"], data.get("date")
    subject_label = SUBJECT_LABELS[lang][subject]
    kept = [e for e in entries if e.matched_name]
    for e in kept:
        JOURNAL.append(
            {"cls": cls, "subject": subject, "student": e.matched_name, "grade": e.grade, "absent": e.absent, "date": date}
        )
        value = t(lang, "absent_label") if e.absent else e.grade
        log_activity("✅", f"{e.matched_name} — {value} ({subject_label}, {cls})")
    USER_CONTEXT[callback.from_user.id] = {"cls": cls, "subject": subject}

    summary = ", ".join(f"{e.matched_name} — {t(lang, 'absent_label') if e.absent else e.grade}" for e in kept)
    await callback.message.edit_text(
        f"{t(lang, 'grades_success_title')}\n\n{cls} · {subject_label}\n{summary}\n{date}"
    )
    await callback.message.answer(t(lang, "grades_success_note"))
    await callback.message.answer(t(lang, "quick_more"), reply_markup=quick_actions_kb(lang))
    await state.clear()
    await callback.answer()


# ---------- Посещаемость ----------

async def _attendance_start(message: Message, state: FSMContext, lang: str) -> None:
    await state.set_state(AttendanceFlow.choosing_class)
    await message.answer(t(lang, "grades_choose_class"), reply_markup=classes_kb("att"))


@router.message(F.text.in_(btn_variants("btn_attendance")))
async def attendance_start(message: Message, state: FSMContext) -> None:
    await _attendance_start(message, state, get_lang(message.from_user.id))


@router.callback_query(AttendanceFlow.choosing_class, F.data.startswith("att:"))
async def attendance_show(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    cls = callback.data.split(":", 1)[1]
    absent = TODAY_ABSENT.get(cls, [])
    total = len(ROSTERS.get(cls, []))

    if not absent:
        await callback.message.edit_text(t(lang, "attendance_all_present", cls=cls))
        await state.clear()
        await callback.answer()
        return

    lines = [t(lang, "attendance_header", cls=cls), t(lang, "attendance_absent_label")]
    lines += [f"❌ {name}" for name in absent]
    lines.append(t(lang, "attendance_total", n=len(absent), total=total))

    await state.update_data(cls=cls)
    await state.set_state(AttendanceFlow.confirming)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t(lang, "attendance_mark_btn"), callback_data="att:mark")]]
    )
    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await callback.answer()


@router.callback_query(AttendanceFlow.confirming, F.data == "att:mark")
async def attendance_mark(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    data = await state.get_data()
    cls = data["cls"]
    log_activity("👥", f"{cls}")
    await callback.message.edit_text(t(lang, "attendance_success", cls=cls))
    await callback.message.answer(t(lang, "quick_more"), reply_markup=quick_actions_kb(lang))
    await state.clear()
    await callback.answer()


# ---------- Посещаемость свободным текстом ----------
# ("Иванов отсутствует, Петров болел" / "Иванов, Петров жоқ") — отдельно от
# AttendanceFlow выше (тот работает с моковыми данными через меню).

def _attendance_status_text(lang: str, entry, emoji: bool = False) -> str:
    if entry.present:
        label = t(lang, "present_label")
        return f"{label} ✅" if emoji else label
    label = f"{t(lang, 'absent_label')} ({entry.reason})"
    return f"{label} ❌" if emoji else label


async def _show_next_attendance_ambiguous_or_confirm(message_target: Message, state: FSMContext, lang: str) -> None:
    data = await state.get_data()
    entries = data["att_entries"]
    cls = data["cls"]

    for idx, entry in enumerate(entries):
        if entry.matched_name is None:
            candidates = entry.ambiguous or ROSTERS[cls]
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=name, callback_data=f"attpick:{idx}:{ROSTERS[cls].index(name)}")]
                    for name in candidates
                ]
                + [[InlineKeyboardButton(text=t(lang, "grades_skip_btn"), callback_data=f"attpick:{idx}:skip")]]
            )
            await state.set_state(AttendanceTextFlow.resolving)
            await message_target.answer(
                t(lang, "grades_ambiguous_prompt", name=html.escape(entry.raw_name), cls=cls),
                reply_markup=kb,
            )
            return

    kept = [e for e in entries if e.matched_name is not None]
    if not kept:
        await message_target.answer(t(lang, "attx_not_recognized"))
        await state.clear()
        return

    lines = [t(lang, "attx_recognized_header"), f"{t(lang, 'field_class')} {cls}", ""]
    for e in kept:
        lines.append(f"👤 {e.matched_name} — {_attendance_status_text(lang, e, emoji=True)}")
    if any(not e.present for e in kept):
        lines.append(t(lang, "attx_reason_legend"))
    lines.append("\n" + t(lang, "grades_confirm_footer"))

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t(lang, "btn_confirm"), callback_data="attx:confirm"),
                InlineKeyboardButton(text=t(lang, "btn_edit"), callback_data="attx:edit"),
            ]
        ]
    )
    await state.set_state(AttendanceTextFlow.confirming)
    await message_target.answer("\n".join(lines), reply_markup=kb)


async def _finalize_attendance_text(message_target: Message, state: FSMContext, text: str, cls: str, lang: str) -> None:
    entries = parse_attendance(text, ROSTERS[cls])
    if not entries:
        await message_target.answer(t(lang, "attx_not_recognized"))
        return
    await state.update_data(cls=cls, att_entries=entries)
    await _show_next_attendance_ambiguous_or_confirm(message_target, state, lang)


async def _handle_freeform_attendance(message: Message, state: FSMContext, lang: str, text: str) -> None:
    ctx = USER_CONTEXT.get(message.from_user.id, {})
    cls = detect_class(text, CLASSES) or ctx.get("cls")
    if not cls:
        await state.update_data(pending_text=text)
        await state.set_state(AttendanceTextFlow.clarify_class)
        await message.answer(t(lang, "freeform_clarify_class"), reply_markup=classes_kb("attcls"))
        return
    await _finalize_attendance_text(message, state, text, cls, lang)


@router.callback_query(AttendanceTextFlow.clarify_class, F.data.startswith("attcls:"))
async def attendance_text_class_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    cls = callback.data.split(":", 1)[1]
    data = await state.get_data()
    await callback.message.edit_text(f"{t(lang, 'field_class')} {cls}")
    await _finalize_attendance_text(callback.message, state, data["pending_text"], cls, lang)
    await callback.answer()


@router.callback_query(AttendanceTextFlow.resolving, F.data.startswith("attpick:"))
async def attendance_text_resolve_pick(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    _, idx_str, choice = callback.data.split(":", 2)
    idx = int(idx_str)
    data = await state.get_data()
    entries = data["att_entries"]
    cls = data["cls"]

    if choice == "skip":
        entries[idx].matched_name = "__SKIP__"
    else:
        entries[idx].matched_name = ROSTERS[cls][int(choice)]

    entries = [e for e in entries if e.matched_name != "__SKIP__"]
    await state.update_data(att_entries=entries)
    await callback.message.edit_text("✓")
    await _show_next_attendance_ambiguous_or_confirm(callback.message, state, lang)
    await callback.answer()


@router.callback_query(AttendanceTextFlow.confirming, F.data == "attx:edit")
async def attendance_text_edit(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    await callback.message.edit_text(t(lang, "edit_retry_prompt"))
    await state.clear()
    await callback.answer()


@router.callback_query(AttendanceTextFlow.confirming, F.data == "attx:confirm")
async def attendance_text_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    data = await state.get_data()
    cls, entries = data["cls"], data["att_entries"]
    kept = [e for e in entries if e.matched_name]
    for e in kept:
        ATTENDANCE_LOG.append({"cls": cls, "student": e.matched_name, "present": e.present, "reason": e.reason})
        log_activity("👥", f"{e.matched_name} — {_attendance_status_text(lang, e)} ({cls})")
    USER_CONTEXT[callback.from_user.id] = {**USER_CONTEXT.get(callback.from_user.id, {}), "cls": cls}

    summary = ", ".join(f"{e.matched_name} — {_attendance_status_text(lang, e)}" for e in kept)
    await callback.message.edit_text(f"{t(lang, 'attx_success_title')}\n\n{cls}\n{summary}")
    await callback.message.answer(t(lang, "attx_success_note"))
    await callback.message.answer(t(lang, "quick_more"), reply_markup=quick_actions_kb(lang))
    await state.clear()
    await callback.answer()


# ---------- Домашнее задание ----------

async def _show_homework_confirm(message_target: Message, state: FSMContext, lang: str) -> None:
    data = await state.get_data()
    cls, subject, entry = data["cls"], data["subject"], data["hw_entry"]
    subject_label = SUBJECT_LABELS[lang][subject]
    target = t(lang, "hw_target_next") if entry.for_next_lesson else t(lang, "hw_target_this")

    lines = [
        t(lang, "hw_recognized_header"),
        f"{t(lang, 'field_class')} {cls}",
        f"{t(lang, 'field_subject')} {subject_label}",
        f"{t(lang, 'hw_field_target')} {target}",
        f"{t(lang, 'hw_field_text')} {html.escape(entry.text)}",
        "\n" + t(lang, "grades_confirm_footer"),
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t(lang, "btn_confirm"), callback_data="hw:confirm"),
                InlineKeyboardButton(text=t(lang, "btn_edit"), callback_data="hw:edit"),
            ]
        ]
    )
    await state.set_state(HomeworkFlow.confirming)
    await message_target.answer("\n".join(lines), reply_markup=kb)


async def _handle_freeform_homework(message: Message, state: FSMContext, lang: str, text: str) -> bool:
    entry = detect_homework(text)
    if entry is None:
        return False

    ctx = USER_CONTEXT.get(message.from_user.id, {})
    cls = detect_class(text, CLASSES) or ctx.get("cls")
    subject = detect_subject(text, SUBJECT_STEMS) or ctx.get("subject")

    if not cls:
        await state.update_data(pending_hw_entry=entry, pending_subject=subject)
        await state.set_state(HomeworkFlow.clarify_class)
        await message.answer(t(lang, "freeform_clarify_class"), reply_markup=classes_kb("hwcls"))
        return True

    if not subject:
        await state.update_data(cls=cls, pending_hw_entry=entry)
        await state.set_state(HomeworkFlow.clarify_subject)
        await message.answer(t(lang, "freeform_clarify_subject", cls=cls), reply_markup=subjects_kb(lang))
        return True

    await state.update_data(cls=cls, subject=subject, hw_entry=entry)
    await _show_homework_confirm(message, state, lang)
    return True


@router.callback_query(HomeworkFlow.clarify_class, F.data.startswith("hwcls:"))
async def homework_class_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    cls = callback.data.split(":", 1)[1]
    data = await state.get_data()
    entry = data["pending_hw_entry"]
    subject = data.get("pending_subject")
    await callback.message.edit_text(f"{t(lang, 'field_class')} {cls}")

    if not subject:
        await state.update_data(cls=cls, pending_hw_entry=entry)
        await state.set_state(HomeworkFlow.clarify_subject)
        await callback.message.answer(t(lang, "grades_subject_prompt"), reply_markup=subjects_kb(lang))
        await callback.answer()
        return

    await state.update_data(cls=cls, subject=subject, hw_entry=entry)
    await _show_homework_confirm(callback.message, state, lang)
    await callback.answer()


@router.callback_query(HomeworkFlow.clarify_subject, F.data.startswith("subj:"))
async def homework_subject_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    subject = callback.data.split(":", 1)[1]
    data = await state.get_data()
    cls, entry = data["cls"], data["pending_hw_entry"]
    await callback.message.edit_text(t(lang, "grades_subject_label", subject=SUBJECT_LABELS[lang][subject]))
    await state.update_data(subject=subject, hw_entry=entry)
    await _show_homework_confirm(callback.message, state, lang)
    await callback.answer()


@router.callback_query(HomeworkFlow.confirming, F.data == "hw:edit")
async def homework_edit(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    await callback.message.edit_text(t(lang, "edit_retry_prompt"))
    await state.clear()
    await callback.answer()


@router.callback_query(HomeworkFlow.confirming, F.data == "hw:confirm")
async def homework_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    data = await state.get_data()
    cls, subject, entry = data["cls"], data["subject"], data["hw_entry"]
    subject_label = SUBJECT_LABELS[lang][subject]
    target = t(lang, "hw_target_next") if entry.for_next_lesson else t(lang, "hw_target_this")

    HOMEWORK_LOG.append(
        {"cls": cls, "subject": subject, "text": entry.text, "for_next_lesson": entry.for_next_lesson}
    )
    log_activity("📋", f"ДЗ {subject_label} ({cls}, {target})")
    USER_CONTEXT[callback.from_user.id] = {"cls": cls, "subject": subject}

    await callback.message.edit_text(
        f"{t(lang, 'hw_success_title')}\n\n{cls} · {subject_label} · {target}\n{html.escape(entry.text)}"
    )
    await callback.message.answer(t(lang, "hw_success_note"))
    await callback.message.answer(t(lang, "quick_more"), reply_markup=quick_actions_kb(lang))
    await state.clear()
    await callback.answer()


async def _upload_and_remember(
    message: Message,
    lang: str,
    teacher_id: int,
    docx_bytes: bytes,
    filename: str,
    doc_type: str,
    cls: str,
    subject_label: str,
    topic: str,
) -> None:
    """Заливает сгенерированный КСП/КТП в облако учителя (см. Настройки) и
    запоминает ссылку вместе с метаданными — учитель/год/предмет/класс/
    четверть/дата/тема — вместо организации файлов по папкам."""
    cloud = TEACHER_CLOUD.get(teacher_id)
    if not cloud:
        await message.answer(
            t(lang, "cloud_not_configured"),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=t(lang, "settings_cloud_btn"), callback_data="settings:cloud")]
                ]
            ),
        )
        return

    try:
        link = await cloud_upload.upload_to_cloud(docx_bytes, filename, cloud["provider"], cloud["token"])
    except Exception:
        logging.exception("Cloud upload failed")
        await message.answer(t(lang, "cloud_upload_failed"))
        return

    date_str = datetime.date.today().strftime("%d.%m.%Y")
    CLOUD_LINKS.append(
        {
            "teacher_id": teacher_id,
            "doc_type": doc_type,
            "cls": cls,
            "subject": subject_label,
            "topic": topic,
            "date": date_str,
            "quarter": detect_quarter(date_str),
            "academic_year": detect_academic_year(date_str),
            "link": link,
            "provider": cloud["provider"],
        }
    )
    provider_label = cloud_upload.PROVIDER_LABELS[cloud["provider"]]
    log_activity("🔗", f"{doc_type.upper()} → {provider_label} ({cls}, {subject_label})")
    await message.answer(t(lang, "cloud_link_ready", provider=provider_label, url=link))


# ---------- КСП ----------

def _ksp_lesson_kb(outline: list[tuple[str, str]], section: str | None) -> InlineKeyboardMarkup:
    """Кнопки уроков из КТП. section=None — без фильтра (все уроки одним списком),
    иначе — только уроки выбранного раздела. callback_data несёт индекс в outline,
    поэтому тема берётся напрямую из КТП, а не перепечатывается учителем."""
    rows = [
        [InlineKeyboardButton(text=f"№{idx + 1}. {name}", callback_data=f"kspless:{idx}")]
        for idx, (sec, name) in enumerate(outline)
        if section is None or sec == section
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ksp_start(message: Message, state: FSMContext, lang: str, teacher_id: int) -> None:
    records = list(KTP_OUTLINES.get(teacher_id, {}).values())
    if not records:
        await state.set_state(KSPFlow.entering_topic)
        await message.answer(t(lang, "ksp_prompt"))
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{r['cls']} · {r['subject_label']}", callback_data=f"kspktp:{i}")]
            for i, r in enumerate(records)
        ]
        + [[InlineKeyboardButton(text=t(lang, "ksp_manual_topic_btn"), callback_data="kspktp:manual")]]
    )
    await state.set_state(KSPFlow.choosing_ktp)
    await message.answer(t(lang, "ksp_choose_ktp"), reply_markup=kb)


@router.message(F.text.in_(btn_variants("btn_ksp")))
async def ksp_start(message: Message, state: FSMContext) -> None:
    await _ksp_start(message, state, get_lang(message.from_user.id), message.from_user.id)


@router.callback_query(KSPFlow.choosing_ktp, F.data == "kspktp:manual")
async def ksp_ktp_manual(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    await state.set_state(KSPFlow.entering_topic)
    await callback.message.edit_text(t(lang, "ksp_prompt"))
    await callback.answer()


@router.callback_query(KSPFlow.choosing_ktp, F.data.startswith("kspktp:"))
async def ksp_ktp_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    teacher_id = callback.from_user.id
    idx = int(callback.data.split(":", 1)[1])
    records = list(KTP_OUTLINES.get(teacher_id, {}).values())
    if idx >= len(records):
        await callback.answer(t(lang, "ksp_ktp_stale"), show_alert=True)
        await state.clear()
        return
    record = records[idx]
    await state.update_data(ktp_idx=idx)

    header = f"{t(lang, 'field_class')} {record['cls']}\n{t(lang, 'field_subject')} {record['subject_label']}"
    sections = list(dict.fromkeys(sec for sec, _ in record["outline"] if sec))

    if len(sections) <= 1:
        await state.set_state(KSPFlow.choosing_lesson)
        await callback.message.edit_text(header)
        await callback.message.answer(t(lang, "ksp_lesson_prompt"), reply_markup=_ksp_lesson_kb(record["outline"], None))
        await callback.answer()
        return

    await state.update_data(ktp_sections=sections)
    await state.set_state(KSPFlow.choosing_section)
    await callback.message.edit_text(header)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=s, callback_data=f"kspsec:{i}")] for i, s in enumerate(sections)]
    )
    await callback.message.answer(t(lang, "ksp_section_prompt"), reply_markup=kb)
    await callback.answer()


@router.callback_query(KSPFlow.choosing_section, F.data.startswith("kspsec:"))
async def ksp_section_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    teacher_id = callback.from_user.id
    sec_idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    records = list(KTP_OUTLINES.get(teacher_id, {}).values())
    ktp_idx = data.get("ktp_idx")
    sections = data.get("ktp_sections") or []
    if ktp_idx is None or ktp_idx >= len(records) or sec_idx >= len(sections):
        await callback.answer(t(lang, "ksp_ktp_stale"), show_alert=True)
        await state.clear()
        return

    record = records[ktp_idx]
    section = sections[sec_idx]
    await state.set_state(KSPFlow.choosing_lesson)
    await callback.message.edit_text(f"{t(lang, 'field_subject')} {section}")
    await callback.message.answer(t(lang, "ksp_lesson_prompt"), reply_markup=_ksp_lesson_kb(record["outline"], section))
    await callback.answer()


async def _generate_and_send_ksp(
    message_target: Message,
    state: FSMContext,
    lang: str,
    teacher_id: int,
    teacher_name: str,
    raw_topic: str,
    cls: str,
    subject_label: str,
) -> None:
    topic = html.escape(raw_topic)

    thinking_msg = await message_target.answer(t(lang, "ai_thinking"))
    ai_content = None
    try:
        ai_content = await ai_generate.generate_ksp_content(topic=raw_topic, cls=cls, subject_label=subject_label, lang=lang)
    except Exception:
        logging.exception("KSP AI generation failed")
    await thinking_msg.delete()

    content = resolve_ksp_content(raw_topic, ai_content, lang)
    docx_bytes = build_ksp_docx(
        cls=cls, subject_label=subject_label, topic=raw_topic, teacher_name=teacher_name, ai_content=content, lang=lang
    )
    KSP_DOCS[teacher_id] = docx_bytes
    KSP_CONTENT[teacher_id] = {
        "cls": cls,
        "subject_label": subject_label,
        "topic": raw_topic,
        "teacher_name": teacher_name,
        "content": content,
        "lang": lang,
    }
    log_activity("📝", f"КСП «{topic}»")

    success_key = "ksp_success" if ai_content else "ksp_success_fallback"
    await message_target.answer(t(lang, success_key, topic=topic), reply_markup=_ksp_result_kb(lang))

    await _upload_and_remember(message_target, lang, teacher_id, docx_bytes, "KSP.docx", "ksp", cls, subject_label, raw_topic)

    await message_target.answer(t(lang, "quick_more"), reply_markup=quick_actions_kb(lang))
    await state.clear()


@router.callback_query(KSPFlow.choosing_lesson, F.data.startswith("kspless:"))
async def ksp_lesson_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    teacher_id = callback.from_user.id
    outline_idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    records = list(KTP_OUTLINES.get(teacher_id, {}).values())
    ktp_idx = data.get("ktp_idx")
    if ktp_idx is None or ktp_idx >= len(records) or outline_idx >= len(records[ktp_idx]["outline"]):
        await callback.answer(t(lang, "ksp_ktp_stale"), show_alert=True)
        await state.clear()
        return

    record = records[ktp_idx]
    _, lesson_name = record["outline"][outline_idx]
    teacher_name = html.escape(callback.from_user.first_name or "—")
    await callback.message.edit_text(t(lang, "ksp_lesson_chosen_label", lesson=html.escape(lesson_name)))
    await callback.answer()
    await _generate_and_send_ksp(
        callback.message, state, lang, teacher_id, teacher_name, lesson_name, record["cls"], record["subject_label"]
    )


@router.message(KSPFlow.entering_topic, ~F.text.in_(ALL_MENU_BUTTONS))
async def ksp_generate(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    raw = message.text.strip()

    ctx = USER_CONTEXT.get(message.from_user.id, {})
    cls = detect_class(raw, CLASSES) or ctx.get("cls") or "—"
    subject_id = detect_subject(raw, SUBJECT_STEMS) or ctx.get("subject")
    subject_label = SUBJECT_LABELS[lang][subject_id] if subject_id else "—"
    teacher_name = html.escape(message.from_user.first_name or "—")

    await _generate_and_send_ksp(message, state, lang, message.from_user.id, teacher_name, raw, cls, subject_label)


@router.callback_query(F.data == "ksp:download_real")
async def ksp_download(callback: CallbackQuery) -> None:
    lang = get_lang(callback.from_user.id)
    data = KSP_DOCS.get(callback.from_user.id)
    if not data:
        await callback.answer(t(lang, "ksp_file_missing"), show_alert=True)
        return
    await callback.message.answer_document(
        BufferedInputFile(data, filename="KSP.docx"),
        caption=t(lang, "ksp_file_caption"),
    )
    await callback.answer()


def _ksp_result_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "btn_download"), callback_data="ksp:download_real")],
            [
                InlineKeyboardButton(text=t(lang, "btn_view"), callback_data="ksp:view"),
                InlineKeyboardButton(text=t(lang, "btn_edit_doc"), callback_data="ksp:edit"),
            ],
        ]
    )


def _format_ksp_view(lang: str, data: dict) -> str:
    content = data["content"]
    lines = [
        t(
            lang,
            "ksp_view_title",
            topic=html.escape(data["topic"]),
            cls=html.escape(data["cls"]),
            subject=html.escape(data["subject_label"]),
        ),
        "",
        f"🎯 <b>{html.escape(t(lang, 'kspedit_field_goal'))}:</b> {html.escape(content['goal'])}",
    ]
    for idx, stage in enumerate(content["stages"]):
        lines.append("")
        lines.append(f"<b>{idx + 1}. {html.escape(t(lang, f'ksp_stage_{idx + 1}'))}</b>")
        lines.append(f"👨‍🏫 {html.escape(stage['teacher_action'])}")
        lines.append(f"🧑‍🎓 {html.escape(stage['student_action'])}")
        lines.append(f"✅ {html.escape(stage['assessment'])}")
        lines.append(f"📦 {html.escape(stage['resources'])}")
    return "\n".join(lines)


@router.callback_query(F.data == "ksp:view")
async def ksp_view(callback: CallbackQuery) -> None:
    lang = get_lang(callback.from_user.id)
    data = KSP_CONTENT.get(callback.from_user.id)
    if not data:
        await callback.answer(t(lang, "doc_stale"), show_alert=True)
        return
    await callback.message.answer(_format_ksp_view(lang, data))
    await callback.answer()


def _ksp_stage_kb(lang: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🎯 {t(lang, 'kspedit_field_goal')}", callback_data="kspeditgoal")]]
    rows += [
        [InlineKeyboardButton(text=f"{i + 1}. {t(lang, f'ksp_stage_{i + 1}')}", callback_data=f"kspeditstage:{i}")]
        for i in range(4)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


_KSP_EDIT_FIELDS = ("teacher", "student", "assessment", "resources")
_KSP_FIELD_ATTR = {
    "teacher": "teacher_action",
    "student": "student_action",
    "assessment": "assessment",
    "resources": "resources",
}


def _ksp_field_kb(lang: str, stage_idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, f"kspedit_field_{f}"), callback_data=f"kspeditfield:{stage_idx}:{f}")]
            for f in _KSP_EDIT_FIELDS
        ]
    )


@router.callback_query(F.data == "ksp:edit")
async def ksp_edit_start(callback: CallbackQuery) -> None:
    lang = get_lang(callback.from_user.id)
    if callback.from_user.id not in KSP_CONTENT:
        await callback.answer(t(lang, "doc_stale"), show_alert=True)
        return
    await callback.message.answer(t(lang, "kspedit_choose_stage"), reply_markup=_ksp_stage_kb(lang))
    await callback.answer()


@router.callback_query(F.data.startswith("kspeditstage:"))
async def ksp_edit_stage(callback: CallbackQuery) -> None:
    lang = get_lang(callback.from_user.id)
    if callback.from_user.id not in KSP_CONTENT:
        await callback.answer(t(lang, "doc_stale"), show_alert=True)
        return
    stage_idx = int(callback.data.split(":", 1)[1])
    stage_label = t(lang, f"ksp_stage_{stage_idx + 1}")
    await callback.message.edit_text(
        t(lang, "kspedit_choose_field", stage=stage_label), reply_markup=_ksp_field_kb(lang, stage_idx)
    )
    await callback.answer()


@router.callback_query(F.data == "kspeditgoal")
async def ksp_edit_goal(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    if callback.from_user.id not in KSP_CONTENT:
        await callback.answer(t(lang, "doc_stale"), show_alert=True)
        return
    await state.update_data(ksp_field="goal", ksp_stage_idx=None)
    await state.set_state(DocEditFlow.ksp_entering_value)
    await callback.message.edit_text(t(lang, "kspedit_enter_value", field=t(lang, "kspedit_field_goal")))
    await callback.answer()


@router.callback_query(F.data.startswith("kspeditfield:"))
async def ksp_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    if callback.from_user.id not in KSP_CONTENT:
        await callback.answer(t(lang, "doc_stale"), show_alert=True)
        return
    _, stage_idx_s, field = callback.data.split(":", 2)
    await state.update_data(ksp_field=field, ksp_stage_idx=int(stage_idx_s))
    await state.set_state(DocEditFlow.ksp_entering_value)
    await callback.message.edit_text(t(lang, "kspedit_enter_value", field=t(lang, f"kspedit_field_{field}")))
    await callback.answer()


@router.message(DocEditFlow.ksp_entering_value, ~F.text.in_(ALL_MENU_BUTTONS))
async def ksp_edit_apply(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    teacher_id = message.from_user.id
    data = KSP_CONTENT.get(teacher_id)
    if not data:
        await message.answer(t(lang, "doc_stale"))
        await state.clear()
        return

    fsm_data = await state.get_data()
    field = fsm_data.get("ksp_field")
    stage_idx = fsm_data.get("ksp_stage_idx")
    new_text = message.text.strip()

    if field == "goal":
        data["content"]["goal"] = new_text
    else:
        data["content"]["stages"][stage_idx][_KSP_FIELD_ATTR[field]] = new_text

    docx_bytes = build_ksp_docx(
        cls=data["cls"],
        subject_label=data["subject_label"],
        topic=data["topic"],
        teacher_name=data["teacher_name"],
        ai_content=data["content"],
        lang=data["lang"],
    )
    KSP_DOCS[teacher_id] = docx_bytes
    await state.clear()

    await message.answer(t(lang, "kspedit_saved"), reply_markup=_ksp_result_kb(lang))


# ---------- КТП ----------

@router.message(F.text.in_(btn_variants("btn_ktp")))
async def ktp_start(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    await state.set_state(KTPFlow.entering_params)
    await message.answer(t(lang, "ktp_prompt"))


@router.message(KTPFlow.entering_params, ~F.text.in_(ALL_MENU_BUTTONS))
async def ktp_generate(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    raw = message.text.strip()

    cls = detect_class(raw, CLASSES) or "—"
    subject_id = detect_subject(raw, SUBJECT_STEMS)
    subject_label = SUBJECT_LABELS[lang][subject_id] if subject_id else raw
    hours_per_week = detect_hours_per_week(raw)
    total_hours = hours_per_week * 34  # ~34 учебные недели в году
    # Дату начала учитель может указать прямо в этом же сообщении («с 02.09.2026»);
    # учебный год спрашивается отдельным шагом ниже (ktpyear) — «текущий учебный
    # год по календарю» не годится дефолтом: с июня по август педагог обычно
    # готовит план уже на СЛЕДУЮЩИЙ год (см. default_ktp_school_year).
    start_date_str = detect_start_date(raw)

    await state.update_data(
        ktp_raw=raw, ktp_cls=cls, ktp_subject_label=subject_label,
        ktp_hours_per_week=hours_per_week, ktp_total_hours=total_hours,
        ktp_start_date_str=start_date_str,
    )

    school_year = detect_school_year(raw)
    if school_year:
        await _ktp_school_year_chosen(message, state, lang, school_year)
        return

    await state.set_state(KTPFlow.choosing_school_year)
    await message.answer(t(lang, "ktpyear_prompt"), reply_markup=_school_year_kb(_school_year_candidates()))


def _school_year_candidates() -> list[str]:
    default = default_ktp_school_year()
    start = int(default.split("-")[0])
    return [default, f"{start + 1}-{start + 2}"]


def _school_year_kb(candidates: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=y, callback_data=f"ktpyear:{y}")] for y in candidates])


async def _ktp_school_year_chosen(message_target: Message, state: FSMContext, lang: str, school_year: str) -> None:
    data = await state.get_data()
    start_date_str = data.get("ktp_start_date_str")
    start_date = (
        datetime.datetime.strptime(start_date_str, "%d.%m.%Y").date()
        if start_date_str
        else datetime.date(int(school_year.split("-")[0]), 9, 1)
    )
    await state.update_data(ktp_school_year=school_year, ktp_start_date=start_date.isoformat())
    await state.set_state(KTPFlow.entering_textbook)
    await message_target.answer(t(lang, "ktp_textbook_prompt"))


@router.message(KTPFlow.choosing_school_year, ~F.text.in_(ALL_MENU_BUTTONS))
async def ktp_school_year_text(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    school_year = detect_school_year(message.text.strip())
    if not school_year:
        await message.answer(t(lang, "ktpyear_prompt"), reply_markup=_school_year_kb(_school_year_candidates()))
        return
    await _ktp_school_year_chosen(message, state, lang, school_year)


@router.callback_query(KTPFlow.choosing_school_year, F.data.startswith("ktpyear:"))
async def ktp_school_year_pick(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    school_year = callback.data.split(":", 1)[1]
    await callback.message.edit_text(t(lang, "ktpyear_chosen_label", year=school_year))
    await callback.answer()
    await _ktp_school_year_chosen(callback.message, state, lang, school_year)


async def _ktp_finish_generate(
    message_target: Message, state: FSMContext, lang: str, teacher_id: int,
    raw: str, cls: str, subject_label: str, hours_per_week: int, total_hours: int,
    school_year: str, start_date: datetime.date, weekdays: list[int],
    textbook: str | None,
) -> None:
    params = html.escape(raw)

    thinking_msg = await message_target.answer(t(lang, "ai_thinking"))
    outline = None
    try:
        outline = await ai_generate.generate_ktp_outline(
            subject_label=subject_label, cls=cls, total_hours=total_hours, lang=lang
        )
    except Exception:
        logging.exception("KTP AI generation failed")
    await thinking_msg.delete()

    docx_bytes = build_ktp_docx(
        cls=cls,
        subject_label=subject_label,
        hours_per_week=hours_per_week,
        total_hours=total_hours,
        school_year=school_year,
        outline=outline,
        lang=lang,
        textbook=textbook,
        start_date=start_date,
        weekdays=weekdays,
    )
    KTP_DOCS[teacher_id] = docx_bytes
    log_activity("📖", f"КТП «{params}»")

    if outline:
        record = {
            "cls": cls,
            "subject_label": subject_label,
            "hours_per_week": hours_per_week,
            "total_hours": total_hours,
            "outline": outline,
            "lang": lang,
            "textbook": textbook,
            "school_year": school_year,
            "start_date": start_date.isoformat(),
            "weekdays": weekdays,
        }
        if cls != "—":
            # тот же объект record, что и в KTP_OUTLINES — редактирование урока
            # (ktpedit) сразу видно и там, откуда берутся темы для КСП.
            KTP_OUTLINES.setdefault(teacher_id, {})[(cls, subject_label)] = record
        KTP_CONTENT[teacher_id] = record
    else:
        KTP_CONTENT.pop(teacher_id, None)

    success_key = "ktp_success" if outline else "ktp_success_fallback"
    await message_target.answer(
        t(lang, success_key, params=params, hours=hours_per_week, total=total_hours),
        reply_markup=_ktp_result_kb(lang),
    )

    await _upload_and_remember(
        message_target, lang, teacher_id, docx_bytes, "KTP.docx", "ktp", cls, subject_label, raw
    )

    await message_target.answer(t(lang, "quick_more"), reply_markup=quick_actions_kb(lang))
    await state.clear()


async def _ktp_finish_from_state(message_target: Message, state: FSMContext, lang: str, teacher_id: int, textbook: str | None) -> None:
    data = await state.get_data()
    weekdays = sorted(data.get("ktp_weekdays") or lesson_weekdays(data.get("ktp_hours_per_week", 1)))
    start_date_str = data.get("ktp_start_date")
    start_date = datetime.date.fromisoformat(start_date_str) if start_date_str else datetime.date.today()
    await _ktp_finish_generate(
        message_target, state, lang, teacher_id,
        raw=data.get("ktp_raw", ""), cls=data.get("ktp_cls", "—"), subject_label=data.get("ktp_subject_label", "—"),
        hours_per_week=data.get("ktp_hours_per_week", 0), total_hours=data.get("ktp_total_hours", 0),
        school_year=data.get("ktp_school_year") or "2025-2026", start_date=start_date, weekdays=weekdays,
        textbook=textbook,
    )


def _weekday_kb(lang: str, selected: set[int]) -> InlineKeyboardMarkup:
    labels = WEEKDAY_LABELS.get(lang, WEEKDAY_LABELS[DEFAULT_LANG])
    day_buttons = [
        InlineKeyboardButton(text=f"{'✅' if i in selected else '▫️'} {labels[i]}", callback_data=f"ktpwd:{i}")
        for i in range(5)
    ]
    confirm = InlineKeyboardButton(text=t(lang, "ktpwd_confirm_btn"), callback_data="ktpwd:confirm")
    return InlineKeyboardMarkup(inline_keyboard=[day_buttons, [confirm]])


@router.message(KTPFlow.entering_textbook, ~F.text.in_(ALL_MENU_BUTTONS))
async def ktp_textbook_entered(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    textbook = message.text.strip()
    if not textbook:
        await message.answer(t(lang, "ktp_textbook_prompt"))
        return

    data = await state.get_data()
    default_days = set(lesson_weekdays(data.get("ktp_hours_per_week", 1)))
    await state.update_data(ktp_textbook=textbook, ktp_weekdays=sorted(default_days))
    await state.set_state(KTPFlow.choosing_weekdays)
    await message.answer(t(lang, "ktpwd_prompt"), reply_markup=_weekday_kb(lang, default_days))


@router.callback_query(KTPFlow.choosing_weekdays, F.data.startswith("ktpwd:"))
async def ktp_weekday_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    action = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected = set(data.get("ktp_weekdays") or [])

    if action == "confirm":
        hours_per_week = data.get("ktp_hours_per_week", 1)
        if len(selected) != hours_per_week:
            await callback.answer(t(lang, "ktpwd_mismatch", n=hours_per_week), show_alert=True)
            return
        await callback.answer()
        await _ktp_finish_from_state(callback.message, state, lang, callback.from_user.id, data.get("ktp_textbook"))
        return

    day = int(action)
    selected.symmetric_difference_update({day})
    await state.update_data(ktp_weekdays=sorted(selected))
    await callback.message.edit_reply_markup(reply_markup=_weekday_kb(lang, selected))
    await callback.answer()


@router.callback_query(F.data == "ktp:download")
async def ktp_download(callback: CallbackQuery) -> None:
    lang = get_lang(callback.from_user.id)
    data = KTP_DOCS.get(callback.from_user.id)
    if not data:
        await callback.answer(t(lang, "ktp_file_missing"), show_alert=True)
        return
    await callback.message.answer_document(
        BufferedInputFile(data, filename="KTP.docx"),
        caption=t(lang, "ktp_file_caption"),
    )
    await callback.answer()


def _ktp_result_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "btn_download"), callback_data="ktp:download")],
            [
                InlineKeyboardButton(text=t(lang, "btn_view"), callback_data="ktp:view"),
                InlineKeyboardButton(text=t(lang, "btn_edit_doc"), callback_data="ktp:edit"),
            ],
        ]
    )


def _format_ktp_view(lang: str, data: dict) -> str:
    lines = [
        t(
            lang,
            "ktp_view_title",
            cls=html.escape(data["cls"]),
            subject=html.escape(data["subject_label"]),
            hours=data["hours_per_week"],
            total=data["total_hours"],
        )
    ]
    last_section = None
    for idx, (section, lesson) in enumerate(data["outline"], start=1):
        if section != last_section:
            lines.append("")
            if section:
                lines.append(f"<b>{html.escape(section)}</b>")
            last_section = section
        lines.append(f"{idx}. {html.escape(lesson)}")
    return "\n".join(lines)


@router.callback_query(F.data == "ktp:view")
async def ktp_view(callback: CallbackQuery) -> None:
    lang = get_lang(callback.from_user.id)
    data = KTP_CONTENT.get(callback.from_user.id)
    if not data:
        await callback.answer(t(lang, "doc_stale"), show_alert=True)
        return
    await _send_chunked(callback.message, _format_ktp_view(lang, data))
    await callback.answer()


def _ktp_lesson_edit_kb(outline: list[tuple[str, str]], section: str | None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"№{idx + 1}. {name}", callback_data=f"ktpeditless:{idx}")]
        for idx, (sec, name) in enumerate(outline)
        if section is None or sec == section
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "ktp:edit")
async def ktp_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    data = KTP_CONTENT.get(callback.from_user.id)
    if not data:
        await callback.answer(t(lang, "doc_stale"), show_alert=True)
        return
    sections = list(dict.fromkeys(sec for sec, _ in data["outline"] if sec))
    if len(sections) <= 1:
        await callback.message.answer(
            t(lang, "ktpedit_choose_lesson"), reply_markup=_ktp_lesson_edit_kb(data["outline"], None)
        )
        await callback.answer()
        return

    await state.update_data(ktpedit_sections=sections)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=s, callback_data=f"ktpeditsec:{i}")] for i, s in enumerate(sections)]
    )
    await callback.message.answer(t(lang, "ktpedit_choose_section"), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ktpeditsec:"))
async def ktp_edit_section_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    data = KTP_CONTENT.get(callback.from_user.id)
    fsm_data = await state.get_data()
    sections = fsm_data.get("ktpedit_sections") or []
    sec_idx = int(callback.data.split(":", 1)[1])
    if not data or sec_idx >= len(sections):
        await callback.answer(t(lang, "doc_stale"), show_alert=True)
        return

    section = sections[sec_idx]
    await callback.message.edit_text(
        t(lang, "ktpedit_choose_lesson"), reply_markup=_ktp_lesson_edit_kb(data["outline"], section)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ktpeditless:"))
async def ktp_edit_lesson_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = get_lang(callback.from_user.id)
    data = KTP_CONTENT.get(callback.from_user.id)
    outline_idx = int(callback.data.split(":", 1)[1])
    if not data or outline_idx >= len(data["outline"]):
        await callback.answer(t(lang, "doc_stale"), show_alert=True)
        return

    await state.update_data(ktpedit_idx=outline_idx)
    await state.set_state(DocEditFlow.ktp_entering_value)
    await callback.message.edit_text(t(lang, "ktpedit_enter_value"))
    await callback.answer()


@router.message(DocEditFlow.ktp_entering_value, ~F.text.in_(ALL_MENU_BUTTONS))
async def ktp_edit_apply(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    teacher_id = message.from_user.id
    data = KTP_CONTENT.get(teacher_id)
    fsm_data = await state.get_data()
    outline_idx = fsm_data.get("ktpedit_idx")
    if not data or outline_idx is None or outline_idx >= len(data["outline"]):
        await message.answer(t(lang, "doc_stale"))
        await state.clear()
        return

    section, _ = data["outline"][outline_idx]
    data["outline"][outline_idx] = (section, message.text.strip())

    start_date_str = data.get("start_date")
    docx_bytes = build_ktp_docx(
        cls=data["cls"],
        subject_label=data["subject_label"],
        hours_per_week=data["hours_per_week"],
        total_hours=data["total_hours"],
        school_year=data.get("school_year") or "2025-2026",
        outline=data["outline"],
        lang=data["lang"],
        textbook=data.get("textbook"),
        start_date=datetime.date.fromisoformat(start_date_str) if start_date_str else None,
        weekdays=data.get("weekdays"),
    )
    KTP_DOCS[teacher_id] = docx_bytes
    await state.clear()

    await message.answer(t(lang, "ktpedit_saved"), reply_markup=_ktp_result_kb(lang))


# ---------- AI Помощник и маршрутизация намерения (свободный текст) ----------

async def _handle_freeform_ksp(message: Message, state: FSMContext) -> None:
    """Тема КСП уже пришла в исходном сообщении — переиспользуем ksp_generate
    напрямую, не прося учителя написать тему ещё раз."""
    await state.set_state(KSPFlow.entering_topic)
    await ksp_generate(message, state)


@router.message(F.text)
async def ai_helper_freeform(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    text = message.text

    # LLM здесь только определяет тип действия (grade/attendance/homework/ksp/
    # question) — реальные данные извлекает и сверяет с реальными данными
    # детерминированный код в parsing.py, как и в остальном боте.
    try:
        action = await ai_generate.classify_intent(text, lang)
    except Exception:
        logging.exception("Intent classification failed")
        action = "question"

    if action == "attendance":
        await _handle_freeform_attendance(message, state, lang, text)
        return
    if action == "homework" and await _handle_freeform_homework(message, state, lang, text):
        return
    if action == "ksp":
        await _handle_freeform_ksp(message, state)
        return

    try:
        reply = await ai_generate.generate_ai_reply(text, lang)
        await message.answer(reply)
        return
    except Exception:
        logging.exception("AI helper generation failed")

    text_lower = text.lower()
    if any(w in text_lower for w in QUESTION_WORDS):
        await message.answer(t(lang, "ai_questions"))
    elif any(w in text_lower for w in TEST_WORDS):
        await message.answer(t(lang, "ai_test"))
    elif any(w in text_lower for w in HOMEWORK_WORDS):
        await message.answer(t(lang, "ai_homework"))
    else:
        await message.answer(t(lang, "ai_fallback"))


# ---------- быстрые действия после подтверждения ----------

@router.callback_query(F.data == "quick:grades")
async def quick_grades(callback: CallbackQuery, state: FSMContext) -> None:
    await _grades_start(callback.message, state, get_lang(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "quick:attendance")
async def quick_attendance(callback: CallbackQuery, state: FSMContext) -> None:
    await _attendance_start(callback.message, state, get_lang(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "quick:ksp")
async def quick_ksp(callback: CallbackQuery, state: FSMContext) -> None:
    await _ksp_start(callback.message, state, get_lang(callback.from_user.id), callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "quick:test")
async def quick_test(callback: CallbackQuery) -> None:
    await callback.message.answer(t(get_lang(callback.from_user.id), "quick_test_stub"))
    await callback.answer()


@router.callback_query(F.data == "quick:analytics")
async def quick_analytics(callback: CallbackQuery) -> None:
    await _my_classes(callback.message, get_lang(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "quick:recent")
async def quick_recent(callback: CallbackQuery) -> None:
    lang = get_lang(callback.from_user.id)
    if not ACTIVITY_LOG:
        await callback.message.answer(t(lang, "recent_empty"))
        await callback.answer()
        return
    lines = [t(lang, "recent_header")]
    for a in ACTIVITY_LOG[-5:][::-1]:
        lines.append(f"{a['icon']} {a['summary']}\n{a['ts']}")
    await callback.message.answer("\n\n".join(lines))
    await callback.answer()


# ---------- приём session id от браузерного расширения ----------

async def _handle_pair(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"}, status=400)

    code = str(data.get("code") or "").strip().upper()
    cookie = data.get("cookie")
    school = data.get("school")
    user_id = PENDING_PAIR_CODES.pop(code, None)
    if not user_id or not cookie:
        return web.json_response({"ok": False, "error": "Неверный или истёкший код"}, status=404)

    KUNDELIK_SESSIONS[user_id] = {"cookie": cookie, "school": school}
    lang = get_lang(user_id)
    try:
        await request.app["bot"].send_message(user_id, t(lang, "kundelik_paired_notify"))
    except Exception:
        logging.exception("Failed to notify teacher about pairing")

    return web.json_response({"ok": True})


@web.middleware
async def _cors_middleware(request: web.Request, handler) -> web.Response:
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


async def start_pair_web_server(bot: Bot) -> None:
    app = web.Application(middlewares=[_cors_middleware])
    app["bot"] = bot
    app.router.add_post("/pair", _handle_pair)
    app.router.add_route("OPTIONS", "/pair", lambda r: web.Response())

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PAIR_WEB_PORT)
    await site.start()
    logging.info(f"Pairing web server started on :{PAIR_WEB_PORT}")


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Не найден BOT_TOKEN — заполните файл .env (см. .env.example)")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await start_pair_web_server(bot)

    print("Бот запущен. Откройте его в Telegram и нажмите /start")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
