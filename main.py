"""Программа скачивания книг с urait.ru.

При запуске авторизует пользователя, затем в бесконечном цикле принимает
ссылки на книги и сохраняет их PDF-файлы рядом со скриптом.
"""

import asyncio
import logging

from httpx import AsyncClient

from auth import DEFAULT_HEADERS, authorize
from parsing import download_book

LINK_HINT = (
    'Введите ссылку на книгу\n'
    '  https://urait.ru/bcode/<id>  или  https://urait.ru/viewer/<slug>-<id>\n'
    '> '
)
LINK_ERROR = (
    'Нужна только корректная ссылка вида '
    'https://urait.ru/bcode/<id> или https://urait.ru/viewer/<slug>-<id>'
)


def setup_logging() -> None:
    """Настраивает логирование: только уровень и сообщение, без логов httpx."""
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)


async def process_link(url: str, client: AsyncClient) -> None:
    """Скачивает книгу по ссылке и сообщает, куда сохранён файл.

    Args:
        url: Ссылка на книгу.
        client: Авторизованный HTTP-клиент.
    """
    try:
        output_path = await download_book(url, client)
    except ValueError:
        logging.error(LINK_ERROR)
        return
    except Exception as e:
        logging.error(f'Не удалось скачать книгу: {e}')
        return
    print(f'Готово! Файл сохранён: {output_path}')


async def run() -> None:
    """Авторизует пользователя и в цикле обрабатывает введённые ссылки."""
    async with AsyncClient(headers=DEFAULT_HEADERS) as client:
        await authorize(client)
        while True:
            url = input(LINK_HINT).strip()
            if url:
                await process_link(url, client)


if __name__ == '__main__':
    setup_logging()
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, EOFError):
        print('\nВыход.')
