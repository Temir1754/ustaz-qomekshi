"""Загрузка сгенерированных файлов (КСП/КТП) в облако учителя и получение
публичной ссылки — той самой, которую учитель вставляет в поле «Ссылка на
КСП» на странице урока / в календарном планировании Kundelik.kz.

У каждого учителя своё облако — какое именно, не имеет значения для Kundelik.kz
(нужна просто открывающаяся по ссылке страница), поэтому провайдер выбирает
сам учитель в Настройках бота (см. `PROVIDER_LABELS`), а бот заливает файл
через API выбранного провайдера.

Реализовано: **yandex_disk** и **dropbox** — оба берут личный API-токен
учителя без отдельной регистрации приложения у провайдера.

**google_drive**, **onedrive**, **mailru** — не реализованы: требуют
полноценного OAuth-флоу (регистрация приложения в консоли разработчика,
redirect URI, client_id/client_secret). Это отдельная работа вне Claude
Code — сначала нужно самостоятельно завести приложение у провайдера, потом
можно реализовать обмен кода на токен и сам аплоад. `upload_to_cloud()`
поднимает `NotImplementedError` для них.

Токен хранится в `TEACHER_CLOUD` (bot.py, в памяти процесса — для реальной
эксплуатации нужно шифрованное персистентное хранилище, не plain-текст).
"""

from __future__ import annotations

import json
import uuid

import httpx

# Отображаемые названия провайдеров — используются в клавиатуре выбора в Настройках.
PROVIDER_LABELS = {
    "yandex_disk": "Яндекс.Диск",
    "dropbox": "Dropbox",
    "google_drive": "Google Drive",
    "onedrive": "OneDrive",
    "mailru": "Облако Mail.ru",
}

YANDEX_DISK_API = "https://cloud-api.yandex.net/v1/disk"
DROPBOX_UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"
DROPBOX_SHARE_URL = "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings"

CLOUD_FOLDER = "/UstazQomeksi"


def _unique_path(filename: str) -> str:
    return f"{CLOUD_FOLDER}/{uuid.uuid4().hex[:10]}_{filename}"


async def _upload_yandex_disk(file_bytes: bytes, filename: str, token: str) -> str:
    path = _unique_path(filename)
    headers = {"Authorization": f"OAuth {token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        # папка должна существовать заранее — Яндекс.Диск не создаёт её сама при загрузке файла
        mkdir_resp = await client.put(f"{YANDEX_DISK_API}/resources", params={"path": CLOUD_FOLDER}, headers=headers)
        if mkdir_resp.status_code not in (201, 409):
            mkdir_resp.raise_for_status()

        upload_resp = await client.get(
            f"{YANDEX_DISK_API}/resources/upload", params={"path": path, "overwrite": "true"}, headers=headers
        )
        upload_resp.raise_for_status()
        href = upload_resp.json()["href"]

        put_resp = await client.put(href, content=file_bytes)
        put_resp.raise_for_status()

        publish_resp = await client.put(f"{YANDEX_DISK_API}/resources/publish", params={"path": path}, headers=headers)
        publish_resp.raise_for_status()

        info_resp = await client.get(f"{YANDEX_DISK_API}/resources", params={"path": path}, headers=headers)
        info_resp.raise_for_status()
        return info_resp.json()["public_url"]


async def _upload_dropbox(file_bytes: bytes, filename: str, token: str) -> str:
    path = _unique_path(filename)
    upload_headers = {
        "Authorization": f"Bearer {token}",
        "Dropbox-API-Arg": json.dumps({"path": path, "mode": "add"}),
        "Content-Type": "application/octet-stream",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        upload_resp = await client.post(DROPBOX_UPLOAD_URL, headers=upload_headers, content=file_bytes)
        upload_resp.raise_for_status()

        share_resp = await client.post(
            DROPBOX_SHARE_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"path": path},
        )
        share_resp.raise_for_status()
        url = share_resp.json()["url"]
    return url.replace("?dl=0", "?dl=1")


_UPLOADERS = {
    "yandex_disk": _upload_yandex_disk,
    "dropbox": _upload_dropbox,
}


async def upload_to_cloud(file_bytes: bytes, filename: str, provider: str, token: str) -> str:
    """Загружает файл в облако выбранного учителем провайдера и возвращает
    публичную ссылку на него."""
    uploader = _UPLOADERS.get(provider)
    if uploader is None:
        raise NotImplementedError(f"Загрузка в {provider} ещё не реализована")
    return await uploader(file_bytes, filename, token)
