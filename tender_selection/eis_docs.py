"""Основной канал скачивания документов извещения — официальный SOAP-сервис ЕИС
(getDocsIP, «получатель машиночитаемых данных»), метод getDocsByReestrNumberRequest.

Публичная ссылка на файл (zakupki.gov.ru/.../filestore/...) на практике стабильно не
отвечает (проверено на проде — таймаут) — портал режет автоматические скачивания.
Поэтому здесь основной путь, а публичная ссылка (documents.fetch_document) — лишь
подстраховка в views.doc_preview на случай, если у ЕИС-токена кончится лимит запросов
(сервис недокументирован по лимитам) или сам сервис ляжет.

Метод возвращает ссылку(и) на ZIP-архив со ВСЕМИ документами извещения (не одним файлом) —
архив качаем отдельным запросом с тем же токеном в заголовке, дальше ищем внутри нужный
файл по имени. `list_archive_files`/`download_archive` намеренно отдельные от «найти один
файл» — это задел под будущий разбор всех документов извещения целиком (не только по клику).
"""
from __future__ import annotations

import datetime
import io
import os
import re
import uuid
import zipfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SOAP_URL = "https://int44.zakupki.gov.ru/eis-integration/services/getDocsIP"
REQUEST_TIMEOUT = 20
ARCHIVE_TIMEOUT = 90
MAX_ARCHIVE_BYTES = 60 * 1024 * 1024

_ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                   xmlns:ws="http://zakupki.gov.ru/fz44/get-docs-ip/ws">
  <soapenv:Header>
    <individualPerson_token>{token}</individualPerson_token>
  </soapenv:Header>
  <soapenv:Body>
    <ws:getDocsByReestrNumberRequest>
      <index>
        <id>{req_id}</id>
        <createDateTime>{created}</createDateTime>
        <mode>PROD</mode>
      </index>
      <selectionParams>
        <subsystemType>PRIZ</subsystemType>
        <reestrNumber>{reestr_number}</reestrNumber>
      </selectionParams>
    </ws:getDocsByReestrNumberRequest>
  </soapenv:Body>
</soapenv:Envelope>"""

_ARCHIVE_URL_RE = re.compile(r"<(?:\w+:)?archiveUrl>(.*?)</(?:\w+:)?archiveUrl>", re.S)
_FAULT_RE = re.compile(r"<(?:\w+:)?faultstring>(.*?)</(?:\w+:)?faultstring>", re.S)
_JUNK_RE = re.compile(r"[^a-zа-яё0-9]+", re.I)


class EisDocsError(RuntimeError):
    pass


def token() -> str:
    return os.getenv("EIS_TOKEN", "").strip()


def _soap_call(reestr_number: str) -> str:
    if not token():
        raise EisDocsError("Не задан EIS_TOKEN.")
    body = _ENVELOPE.format(
        token=token(),
        req_id=uuid.uuid4(),
        created=datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        reestr_number=reestr_number,
    ).encode("utf-8")
    request = Request(SOAP_URL, data=body, method="POST", headers={
        "Content-Type": "text/xml; charset=utf-8",
    })
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        fault = _FAULT_RE.search(detail)
        if fault:
            raise EisDocsError(f"ЕИС отклонил запрос документов: {fault.group(1)}") from exc
        raise EisDocsError(f"ЕИС ответил HTTP {exc.code} на запрос документов.") from exc
    except (URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise EisDocsError("ЕИС недоступен (таймаут) при запросе документов.") from exc


def fetch_archive_urls(reestr_number: str) -> list[str]:
    """Ссылки на архивы документов извещения. Обычно один, но может быть несколько пакетов."""
    text = _soap_call(reestr_number)
    fault = _FAULT_RE.search(text)
    if fault:
        raise EisDocsError(f"ЕИС отклонил запрос документов: {fault.group(1)}")
    urls = _ARCHIVE_URL_RE.findall(text)
    if not urls:
        raise EisDocsError("ЕИС не вернул архив документов для этой закупки.")
    return urls


def download_archive(url: str) -> bytes:
    request = Request(url, headers={"individualPerson_token": token()})
    try:
        with urlopen(request, timeout=ARCHIVE_TIMEOUT) as response:
            data = response.read(MAX_ARCHIVE_BYTES + 1)
    except HTTPError as exc:
        raise EisDocsError(f"ЕИС ответил HTTP {exc.code} при скачивании архива.") from exc
    except (URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise EisDocsError("Не удалось скачать архив документов с ЕИС.") from exc
    if len(data) > MAX_ARCHIVE_BYTES:
        raise EisDocsError("Архив документов слишком большой.")
    return data


def _normalize(name: str) -> str:
    return _JUNK_RE.sub("", name.lower())


def list_archive_files(archive_data: bytes) -> dict[str, bytes]:
    """Все файлы внутри архива (плоско, разворачивая вложенные .zip). Задел под будущий
    разбор всех документов извещения целиком, не только одного по клику."""
    out: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(archive_data)) as zf:
            for n in zf.namelist():
                if n.endswith("/"):
                    continue
                data = zf.read(n)
                if n.lower().endswith(".zip"):
                    out.update(list_archive_files(data))
                else:
                    out[n] = data
    except zipfile.BadZipFile:
        pass
    return out


def find_file(archive_data: bytes, filename: str) -> tuple[bytes, str] | None:
    """Ищем в архиве конкретный файл по имени (без учёта пути и небуквенных различий)."""
    target = _normalize(filename)
    for path, data in list_archive_files(archive_data).items():
        if _normalize(path.rsplit("/", 1)[-1]) == target:
            return data, path
    return None


def fetch_document_via_eis(reestr_number: str, filename: str) -> bytes:
    """Достаём конкретный файл извещения через официальный канал ЕИС, когда публичная
    ссылка не отвечает. Пробует все вернувшиеся архивы, пока не найдёт файл."""
    last_error: EisDocsError | None = None
    for url in fetch_archive_urls(reestr_number):
        try:
            archive = download_archive(url)
        except EisDocsError as exc:
            last_error = exc
            continue
        found = find_file(archive, filename)
        if found:
            return found[0]
    if last_error:
        raise last_error
    raise EisDocsError("Файл не найден в архивах ЕИС для этой закупки.")
