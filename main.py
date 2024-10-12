import os
import re
import shutil
import logging
import asyncio
from asyncio import Semaphore

from aiohttp import ClientSession
from pypdf import PdfWriter
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPDF
from win11toast import toast
import environ


env = environ.Env()
environ.Env.read_env('account.txt')
EMAIL = env('URAIT_EMAIL')
PASSWORD = env('URAIT_PASSWORD')
assert EMAIL and PASSWORD, "Укажите данные от аккаунта ЮРАЙТ в файле account"

DEFAULT_HEADERS = {
    'Host': 'urait.ru',
    'Origin': 'https://urait.ru',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 YaBrowser/24.7.0.0 Safari/537.36'
}


async def login(session: ClientSession, semaphore: Semaphore):
    logging.info("Авторизация пользователя...")
    async with semaphore:
        response = await session.post('https://urait.ru/login', json={'email': EMAIL, 'password': PASSWORD}, allow_redirects=True)
        text = await response.text()
        assert 'Пользователь с указанным логином не зарегистрирован' not in text, f"Нет пользвателя в системе {EMAIL}"
        assert 'Неверный пароль' not in text, f"Неверный пароль для пользователя {EMAIL}"
        logging.info('Авторизация прошла успешно')

async def parse_book_info(book_url: str, session: ClientSession, semaphore: Semaphore) -> dict:
    async with semaphore:
        logging.info("Получение информации о книге, ожидайте...")
        
        response = await session.get(book_url)
        text = await response.text()   
        
        span_with_book_pages = re.search(r'<span class="book-about-produce__info">\d+</span>', text).group()
        pages = int(re.search(r'\d+', span_with_book_pages).group())
        book_title = re.search(r'<h1 class="page-content-head__title book_title">.+</h1>', text).group()
        book_title = re.sub(r'<h1 class="page-content-head__title book_title">|</h1>', '', book_title)

        response = await session.get(book_url.replace('/book/', '/viewer/'))
        book_code = re.search(r"new Viewer\('\S+'", await response.text()).group()
        book_code = re.search(r'\'\S+\'', book_code).group()[1:-1]
        
        logging.info(f'Название книги: {book_title}')
        logging.info(f'Кол-во страниц: {pages}')
        logging.info(f'Код книги: {book_code}')
        return {'pages': pages, 'book_code': book_code, 'book_title': book_title}
        
async def parse_page(pdf_stack: list, book_code: str, page: int, session: ClientSession, semaphore: Semaphore):
    try:
        async with semaphore: 
            response = await session.get(f'https://urait.ru/viewer/page/{book_code}/{page}')
            assert response.status == 200

            svg_file_name = f'temp/{page}.svg'
            pdf_file_name = f'temp/{page}.pdf'
            os.makedirs(os.path.dirname(svg_file_name), exist_ok=True)
            with open(svg_file_name, 'w') as file:
                file.write(await response.text())
        
        logging.disable(logging.ERROR)
        drawing = svg2rlg(svg_file_name)
        renderPDF.drawToFile(drawing, pdf_file_name)
        logging.disable(logging.INFO)

        pdf_stack.append(page)
    except Exception as e:
        logging.info(f'Не удалось получить {page} страницу. Работа программы продолжается...')
        logging.info(f'Ошибка работы\n{e}')

async def process(pdf_stack: list, total_pages: int):
    while True:
        if len(pdf_stack) >= total_pages - 1:
            logging.info("\nКНИГА СКАЧАНА!")
            break
        proc_str = f"\rСкачано страниц {str(len(pdf_stack)).rjust(4, '0')} из {str(total_pages-2).rjust(4, '0')}"
        print(proc_str, end='')
        await asyncio.sleep(2)

def create_pdf(file_name: str, pdf_stack: list):
    pdf_stack.sort()
    merger = PdfWriter()
    for pdf in pdf_stack:
        merger.append(f'./temp/{pdf}.pdf')
    merger.write(f"{file_name}.pdf")
    merger.close()

async def main():
    pdf_stack = list()
    book_url = input("Введите ссылку на книгу (пример - https://urait.ru/book/...)\n")
    try:
        semaphore = Semaphore(4)
        async with ClientSession(headers=DEFAULT_HEADERS) as session:
            await login(session, semaphore)
            book_info = await parse_book_info(book_url, session, semaphore)
            logging.info("Получение книги с сайта, ожидайте...")
            tasks = [asyncio.create_task(parse_page(pdf_stack, book_info['book_code'], p, session, semaphore)) for p in range(1, book_info['pages'])]
            task_proc = asyncio.create_task(process(pdf_stack, book_info['pages']))
            await asyncio.gather(*tasks, task_proc)
            create_pdf(book_info['book_title'], pdf_stack)
            toast(f'\nКнига скачана!\n{book_url}')
    except Exception as e:
        logging.info(f'Ошибка работы программы \n{e}')        
    finally:
        try:
            shutil.rmtree('./temp')
        except:
            pass
        input("\nНажмите Enter для выхода из программы")


logging.basicConfig(level=logging.INFO)
asyncio.run(main())
