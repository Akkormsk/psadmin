"""Bounded, temporary LibreOffice conversions shared by preview and extraction."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from .documents import DocumentError, MAX_BYTES

OFFICE_EXTENSIONS = {"doc", "docx", "xls", "xlsx"}


def convert_office(data: bytes, filename: str, target: str) -> bytes:
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in OFFICE_EXTENSIONS or target not in {"pdf", "docx", "xlsx"}:
        raise DocumentError("Формат не поддерживается для преобразования.")
    if not data or len(data) > MAX_BYTES:
        raise DocumentError("Недопустимый размер документа.")
    executable = shutil.which("libreoffice")
    if not executable:
        raise DocumentError("Офисный просмотр пока недоступен на сервере.")
    with tempfile.TemporaryDirectory(prefix="psadmin-office-") as directory:
        root = Path(directory)
        source = root / ("document." + extension)
        source.write_bytes(data)
        output = root / "output"
        output.mkdir()
        profile = root / "profile" / "user"
        profile.mkdir(parents=True)
        (profile / "registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '</item></oor:items>', encoding="utf-8",
        )
        output_format = target
        if target == "pdf" and extension in {"xls", "xlsx"}:
            output_format = 'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}'
        try:
            subprocess.run(
                [executable, "-env:UserInstallation=" + (root / "profile").as_uri(),
                 "--headless", "--convert-to", output_format, "--outdir", str(output), str(source)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60, check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DocumentError("Не удалось преобразовать документ для просмотра.") from exc
        result = output / ("document." + target)
        if not result.is_file() or not 0 < result.stat().st_size <= MAX_BYTES:
            raise DocumentError("Преобразование не создало доступный документ.")
        return result.read_bytes()
