"""Запросы к реальному Kundelik.kz от имени учителя через его сессионную
cookie (QundelikAuth_a), полученную через расширение (см. extension/).

Официальной документации API у Kundelik.kz нет, поэтому структура ответа
/v2/schedules/ здесь не разбирается вслепую — build_today_lines() честно
логирует сырой JSON и возвращает None, если не узнаёт в нём список уроков.
Как только увидим реальный ответ в логах, здесь дописывается разбор под
точную схему вместо угадывания."""

from __future__ import annotations

import datetime
import logging

import httpx

BASE_URL = "https://schools.kundelik.kz"


class KundelikAuthError(Exception):
    """Сессия истекла или cookie недействительна — нужно переподключиться через расширение."""


def current_academic_start_year(today: datetime.date | None = None) -> int:
    """Год начала текущего учебного года — с сентября это today.year, до сентября — today.year - 1."""
    today = today or datetime.date.today()
    return today.year if today.month >= 9 else today.year - 1


async def fetch_schedule(cookie: str, school: str, year: int | None = None, tab: str = "groups") -> dict:
    year = year or current_academic_start_year()
    url = f"{BASE_URL}/v2/schedules/"
    params = {"school": school, "tab": tab, "year": year}
    headers = {
        "Cookie": f"QundelikAuth_a={cookie}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Referer": f"{url}?school={school}&tab={tab}&year={year}",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, params=params, headers=headers)
    if response.status_code in (401, 403):
        raise KundelikAuthError(f"Kundelik вернул {response.status_code}")
    response.raise_for_status()
    return response.json()


def build_today_lines(data: dict) -> list[str] | None:
    """Пытается собрать строки расписания на сегодня из ответа Kundelik.
    Схема ответа неизвестна (нет официальной документации) — пока не найдём
    в data распознаваемый список уроков, возвращает None, а сырой ответ
    логируется в build_today_lines_or_log() для последующего разбора."""
    return None


def build_today_lines_or_log(data: dict) -> list[str] | None:
    lines = build_today_lines(data)
    if lines is None:
        logging.info("Kundelik schedule response (не распознана схема): %s", data)
    return lines
