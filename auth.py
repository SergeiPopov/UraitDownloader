"""Модуль авторизации пользователя на сайте urait.ru.

Данные аккаунта хранятся в зашифрованном JSON-конфиге (chipher_account_data),
доступ к которому открывается пин-кодом. Если данных нет, их можно ввести
интерактивно — тогда они проверяются авторизацией и сохраняются в конфиг.
"""

import json
import logging
import os
from base64 import b64decode, b64encode, urlsafe_b64encode
from dataclasses import asdict, dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from httpx import AsyncClient, RequestError, Response

LOGIN_URL = 'https://urait.ru/api/login'
CONFIG_FILE = Path('chipher_account_data')
PBKDF2_ITERATIONS = 600_000
DEFAULT_HEADERS = {
    'Host': 'urait.ru',
    'Origin': 'https://urait.ru',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 YaBrowser/24.7.0.0 Safari/537.36'
}


@dataclass
class Credentials:
    """Данные аккаунта urait.ru, держатся в памяти и сохраняются в конфиг."""

    email: str
    password: str

    @classmethod
    def from_json(cls, raw: str) -> 'Credentials':
        """Собирает объект из JSON-строки."""
        return cls(**json.loads(raw))

    def to_json(self) -> str:
        """Сериализует объект в JSON-строку."""
        return json.dumps(asdict(self), ensure_ascii=False)


def _pin_to_key(pin: str, salt: bytes) -> bytes:
    """Выводит ключ Fernet из пин-кода и соли."""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS)
    return urlsafe_b64encode(kdf.derive(pin.encode()))


def encrypt_config(data: str, pin: str) -> str:
    """Шифрует строку пин-кодом.

    Returns:
        JSON-контейнер вида {"salt": "...", "data": "<токен>"} для записи в файл.
    """
    salt = os.urandom(16)
    token = Fernet(_pin_to_key(pin, salt)).encrypt(data.encode())
    return json.dumps({'salt': b64encode(salt).decode(), 'data': token.decode()})


def decrypt_config(raw: str, pin: str) -> str:
    """Расшифровывает содержимое конфига пин-кодом.

    Raises:
        ValueError: неверный пин-код или данные конфига повреждены.
    """
    try:
        payload = json.loads(raw)
        token = Fernet(_pin_to_key(pin, b64decode(payload['salt']))).decrypt(payload['data'].encode())
        return token.decode()
    except (InvalidToken, KeyError, ValueError) as e:
        raise ValueError('Неверный пин-код или повреждён файл конфигурации') from e


def create_config(credentials: Credentials, pin: str) -> None:
    """Шифрует данные пользователя и записывает файл конфигурации."""
    CONFIG_FILE.write_text(encrypt_config(credentials.to_json(), pin), encoding='utf-8')
    logging.info(f'Конфигурация сохранена в {CONFIG_FILE}')


def read_config(pin: str) -> Credentials:
    """Читает и расшифровывает данные пользователя из файла конфигурации.

    Raises:
        FileNotFoundError: файл конфигурации отсутствует.
        ValueError: неверный пин-код или данные конфига повреждены.
    """
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f'Файл конфигурации {CONFIG_FILE} не найден')
    return Credentials.from_json(decrypt_config(CONFIG_FILE.read_text(encoding='utf-8'), pin))


def load_credentials(pin: str) -> Credentials:
    """Возвращает данные аккаунта из расшифрованного конфига.

    Raises:
        FileNotFoundError: файл конфигурации отсутствует.
        ValueError: неверный пин-код или данные конфига повреждены.
    """
    return read_config(pin)


def read_pin(confirm: bool = False) -> str:
    """Запрашивает пин-код, при confirm — дважды.

    Raises:
        ValueError: пин-код пуст или не совпал при подтверждении.
    """
    prompt = 'Придумайте пин-код для шифрования своих данных: ' if confirm else 'Вы уже авторизовывались. Введите пин-код от своих данных: '
    pin = input(prompt)
    if confirm and pin != input('Повторите пин-код: '):
        raise ValueError('Пин-коды не совпадают')
    if not pin:
        raise ValueError('Пин-код не может быть пустым')
    return pin


def read_account_input() -> Credentials:
    """Запрашивает у пользователя email и пароль от urait.ru."""
    email = input('Email от аккаунта urait.ru: ').strip()
    return Credentials(email=email, password=input('Пароль: '))


def raise_if_login_failed(response: Response) -> None:
    """Проверяет ответ API авторизации и выбрасывает понятную ошибку.

    Raises:
        RuntimeError: если сервер отклонил авторизацию.
    """
    try:
        data = response.json()
    except ValueError:
        data = {}
    if data.get('status') == 'error' or not response.is_success:
        raise RuntimeError('Авторизация отклонена, проверьте данные аккаунта')


async def login(client: AsyncClient, credentials: Credentials) -> None:
    """Авторизует пользователя через API urait.ru, куки сессии сохраняются в client.

    Raises:
        RuntimeError: ошибка сети или отказ сервера в авторизации.
    """
    logging.info("Авторизация пользователя...")
    try:
        response = await client.post(
            LOGIN_URL, json={'email': credentials.email, 'password': credentials.password}
        )
    except RequestError as e:
        raise RuntimeError(f'Ошибка сети при авторизации: {e}') from e
    raise_if_login_failed(response)
    logging.info('Авторизация прошла успешно')


async def _try_login(client: AsyncClient, credentials: Credentials) -> bool:
    """Пробует авторизоваться, возвращает результат вместо исключения."""
    try:
        await login(client, credentials)
        return True
    except RuntimeError as e:
        logging.error(e)
        return False


def _unlock_config() -> Credentials | None:
    """Расшифровывает конфиг введённым пин-кодом; при ошибке возвращает None."""
    try:
        return load_credentials(read_pin())
    except ValueError as e:
        logging.error(e)
        return None


async def _register(client: AsyncClient) -> Credentials:
    """В цикле запрашивает новые данные, проверяет их и создаёт зашифрованный конфиг."""
    while True:
        try:
            credentials = read_account_input()
            await login(client, credentials)
            create_config(credentials, read_pin(confirm=True))
            return credentials
        except (RuntimeError, ValueError) as e:
            logging.error(f'{e}. Попробуйте ещё раз.')


async def authorize(client: AsyncClient) -> Credentials:
    """Подбирает валидные данные аккаунта и оставляет client авторизованным.

    Порядок: зашифрованный конфиг (по пинкоду) → ввод и регистрация новых данных.
    """
    credentials = _unlock_config()
    if credentials and await _try_login(client, credentials):
        return credentials
    return await _register(client)
