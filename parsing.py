"""Модуль парсинга и скачивания книг с сайта urait.ru.

Все сетевые методы принимают авторизованный HTTP-клиент.
Страницы скачиваются во временную папку temp рядом со скриптом,
затем конвертируются в один PDF, после чего temp удаляется.
"""

import asyncio
import logging
import os
import re
import shutil
from asyncio import Semaphore, as_completed
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import as_completed as process_completed
from io import BytesIO
from pathlib import Path

from httpx import AsyncClient
from pypdf import PdfWriter
from reportlab.graphics import renderPDF
from svglib.svglib import svg2rlg

CONTENTS_URL = 'https://urait.ru/api/contents/{book_id}'
VIEWER_DATA_URL = 'https://urait.ru/viewer/data/{book_uuid}/{page}'
VIEWER_PAGE_URL = 'https://urait.ru/viewer/page/{book_uuid}/{page}'
PAGES_SEMAPHORE = Semaphore(5)
SCRIPT_DIR = Path(__file__).resolve().parent
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def parse_bcode_url(url: str) -> str:
    """Извлекает id книги из ссылки на карточку учебника.

    Args:
        url: Ссылка вида https://urait.ru/bcode/<id>.

    Returns:
        Числовой id книги.

    Raises:
        ValueError: в ссылке нет числового id.
    """
    slug = url.removeprefix('https://urait.ru/bcode/').strip('/')
    match = re.fullmatch(r'(\d+)', slug)
    if not match:
        raise ValueError(f'В ссылке карточки нет id книги: {url}')
    return match.group(1)


def parse_viewer_url(url: str) -> str:
    """Извлекает id книги из ссылки на просмотр.

    Args:
        url: Ссылка вида https://urait.ru/viewer/<slug>-<id>[#page/N].

    Returns:
        Числовой id книги.

    Raises:
        ValueError: в ссылке нет числового id.
    """
    slug = url.removeprefix('https://urait.ru/viewer/').split('#')[0].split('/')[0]
    match = re.search(r'-(\d+)$', slug)
    if not match:
        raise ValueError(f'В ссылке просмотра нет id книги: {url}')
    return match.group(1)


URL_PARSERS = (
    ('https://urait.ru/bcode/', parse_bcode_url),
    ('https://urait.ru/viewer/', parse_viewer_url),
)


def extract_book_id(url: str) -> str:
    """Роутер: выбирает метод парсинга id по виду пользовательской ссылки.

    Args:
        url: Ссылка вида https://urait.ru/bcode/<id> или https://urait.ru/viewer/<slug>-<id>.

    Returns:
        Числовой id книги.

    Raises:
        ValueError: ссылка не распознана.
    """
    clean = url.strip()
    for prefix, parser in URL_PARSERS:
        if clean.startswith(prefix):
            return parser(clean)
    raise ValueError(f'Неизвестный тип ссылки: {url}')


async def get_contents_data(book_id: str, client: AsyncClient) -> dict:
    """Запрашивает содержимое книги в API и возвращает блок data.

    Raises:
        RuntimeError: сервер вернул ошибку или в ответе нет data.
    """
    logging.info(f'Запрос содержимого книги {book_id}...')
    response = await client.get(CONTENTS_URL.format(book_id=book_id))
    if not response.is_success:
        raise RuntimeError(f'API вернул HTTP {response.status_code} для книги {book_id}')
    data = response.json().get('data')
    if not isinstance(data, dict):
        raise RuntimeError(f'В ответе API нет data для книги {book_id}')
    return data


async def fetch_sef_url(book_id: str, client: AsyncClient) -> str:
    """Достаёт из API содержимого книги её sef_url.

    Raises:
        RuntimeError: сервер вернул ошибку или в ответе нет sef_url.
    """
    data = await get_contents_data(book_id, client)
    sef_url = data.get('sef_url')
    if not sef_url:
        raise RuntimeError(f'В ответе API нет sef_url для книги {book_id}')
    return sef_url


async def fetch_book_uuid(book_id: str, client: AsyncClient) -> str:
    """Достаёт из API содержимого книги её id (UUID).

    Raises:
        RuntimeError: сервер вернул ошибку или в ответе нет id.
    """
    data = await get_contents_data(book_id, client)
    book_uuid = data.get('id')
    if not book_uuid:
        raise RuntimeError(f'В ответе API нет id для книги {book_id}')
    return book_uuid


async def get_viewer_data(book_uuid: str, client: AsyncClient) -> dict:
    """Запрашивает данные первой страницы просмотра книги и возвращает JSON.

    Raises:
        RuntimeError: сервер вернул ошибку или не-JSON данные.
    """
    response = await client.get(VIEWER_DATA_URL.format(book_uuid=book_uuid, page=1))
    if not response.is_success:
        raise RuntimeError(f'API просмотра вернул HTTP {response.status_code} для книги {book_uuid}')
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f'В ответе API просмотра нет данных для книги {book_uuid}')
    return data


async def fetch_page_count(book_uuid: str, client: AsyncClient) -> int:
    """Достаёт количество страниц книги из API просмотра.

    Raises:
        RuntimeError: сервер вернул ошибку или в ответе нет page_count.
    """
    data = await get_viewer_data(book_uuid, client)
    page_count = data.get('page_count')
    if not isinstance(page_count, int):
        raise RuntimeError(f'В ответе API просмотра нет page_count для книги {book_uuid}')
    return page_count


async def fetch_book_title(book_uuid: str, client: AsyncClient) -> str:
    """Достаёт название книги из API просмотра.

    Если поля title нет — берётся sef_url, если и его нет — '{book_uuid}.pdf'.
    """
    data = await get_viewer_data(book_uuid, client)
    return data.get('title') or data.get('sef_url') or f'{book_uuid}.pdf'


def temp_svg_path(page: int) -> Path:
    """Путь к SVG-файлу страницы во временной папке рядом со скриптом."""
    return SCRIPT_DIR / 'temp' / f'{page}.svg'


async def fetch_page_svg(book_uuid: str, page: int, client: AsyncClient, semaphore: Semaphore) -> int:
    """Скачивает SVG одной страницы и сохраняет её во временную папку.

    Args:
        book_uuid: UUID книги.
        page: Номер страницы (нумерация с 1).
        client: Авторизованный HTTP-клиент.
        semaphore: Ограничитель одновременных запросов.

    Returns:
        Номер скачанной страницы.

    Raises:
        RuntimeError: сервер вернул ошибку.
    """
    async with semaphore:
        response = await client.get(VIEWER_PAGE_URL.format(book_uuid=book_uuid, page=page))
        if not response.is_success:
            raise RuntimeError(f'Не удалось скачать страницу {page}: HTTP {response.status_code}')
        temp_svg_path(page).write_text(response.text, encoding='utf-8')
        return page


def render_progress(label: str, done: int, total: int, width: int = 30) -> None:
    """Перерисовывает статус в текущей строке консоли (ASCII)."""
    filled = width * done // total if total else width
    bar = '#' * filled + '-' * (width - filled)
    percent = 100 * done // total if total else 100
    print(f'\r{label}: {done}/{total} [{bar}] {percent}%', end='', flush=True)


async def fetch_book_pages(book_uuid: str, page_count: int, client: AsyncClient) -> None:
    """Параллельно скачивает все страницы книги в temp, сохраняя их по номерам."""
    logging.info(f'Скачивание {page_count} страниц книги...')
    (SCRIPT_DIR / 'temp').mkdir(parents=True, exist_ok=True)
    tasks = [fetch_page_svg(book_uuid, page, client, PAGES_SEMAPHORE) for page in range(1, page_count + 1)]
    for done, task in enumerate(as_completed(tasks), start=1):
        await task
        render_progress('Скачано страниц', done, page_count)
    print()


def convert_svg_to_pdf_file(svg_path: str, pdf_path: str) -> None:
    """Конвертирует SVG-файл в одностраничный PDF-файл.

    Запускается в отдельном процессе пула, поэтому принимает пути строками.
    """
    renderPDF.drawToFile(svg2rlg(svg_path), pdf_path)


def convert_workers() -> int:
    """Число процессов для параллельной конвертации (по числу ядер минус один)."""
    return max(1, (os.cpu_count() or 1) - 1)


def build_pdf_from_temp(page_count: int) -> bytes:
    """Параллельно конвертирует SVG из temp и собирает из них единый PDF.

    Каждый процесс превращает свою страницу в одностраничный PDF,
    затем главный процесс склеивает страницы через pypdf.

    Returns:
        Байты итогового PDF.
    """
    temp_dir = SCRIPT_DIR / 'temp'
    pages = range(1, page_count + 1)
    with ProcessPoolExecutor(max_workers=convert_workers()) as pool:
        futures = {
            pool.submit(convert_svg_to_pdf_file, str(temp_dir / f'{page}.svg'), str(temp_dir / f'{page}.pdf')): page
            for page in pages
        }
        for done, future in enumerate(process_completed(futures), start=1):
            future.result()
            render_progress('Конвертация страниц в PDF', done, page_count)
    print()
    writer = PdfWriter()
    for page in pages:
        writer.append(str(temp_dir / f'{page}.pdf'))
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def build_filename(title: str, book_uuid: str) -> str:
    """Собирает безопасное имя PDF-файла из названия книги.

    Args:
        title: Название книги или запасное имя.
        book_uuid: UUID книги для запасного имени.

    Returns:
        Имя файла с расширением .pdf.
    """
    name = INVALID_FILENAME_CHARS.sub('-', (title or book_uuid).strip()) or book_uuid
    return name if name.lower().endswith('.pdf') else f'{name}.pdf'


async def download_book(url: str, client: AsyncClient) -> Path:
    """Скачивает книгу по ссылке и сохраняет её PDF рядом со скриптом.

    Страницы сохраняются в temp, затем конвертируются в один PDF в отдельном
    потоке (чтобы не блокировать событийный цикл); после сборки temp удаляется.

    Args:
        url: Ссылка вида https://urait.ru/bcode/<id> или /viewer/<slug>-<id>.
        client: Авторизованный HTTP-клиент.

    Returns:
        Путь к сохранённому PDF.

    Raises:
        ValueError: ссылка не распознана.
        RuntimeError: ошибка API при получении данных книги или страниц.
    """
    book_id = extract_book_id(url)
    book_uuid = await fetch_book_uuid(book_id, client)
    page_count = await fetch_page_count(book_uuid, client)
    title = await fetch_book_title(book_uuid, client)
    try:
        await fetch_book_pages(book_uuid, page_count, client)
        pdf_bytes = await asyncio.to_thread(build_pdf_from_temp, page_count)
        output_path = SCRIPT_DIR / build_filename(title, book_uuid)
        output_path.write_bytes(pdf_bytes)
        return output_path
    finally:
        shutil.rmtree(SCRIPT_DIR / 'temp', ignore_errors=True)
