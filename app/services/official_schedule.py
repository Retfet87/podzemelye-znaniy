"""Manual AKVT discovery. TLS + public, pinned DNS; no arbitrary download hosts."""
import http.client
import socket
import ssl
from html.parser import HTMLParser
from ipaddress import ip_address
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urljoin, urlsplit

from app.services.schedule_parser import parse_xls

PAGE = 'https://www.akvt.ru/teachers/raspisanie-zanyatij/'
HOSTS = {'www.akvt.ru', 'akvt.ru'}
XLS_LIMIT = 8 * 1024 * 1024


def target(url):
    try:
        p = urlsplit(url)
        if (not isinstance(url, str) or len(url) > 2000 or p.scheme != 'https'
                or p.hostname not in HOSTS or p.username is not None
                or p.port not in (None, 443) or p.fragment
                or any(c.isspace() or ord(c) < 32 or c in '\\' for c in url)):
            raise ValueError
        return p.hostname, quote(p.path or '/', safe='/%:@-._~!$&()*+,;=') + ('?' + p.query if p.query else '')
    except (ValueError, TypeError):
        raise ValueError('Разрешены только HTTPS-адреса официального сайта АКВТ.') from None


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=10, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def fetch(url, *, limit):
    for _ in range(4):
        host, path = target(url)
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
            if not addresses or any(not ip_address(address).is_global for address in addresses):
                raise ValueError('Сайт вернул непубличный сетевой адрес. Загрузка запрещена.')
            # DNS cannot change the destination between validation and connection.
            connection = PinnedHTTPS(host, min(addresses))
            try:
                connection.request('GET', path, headers={'Accept-Encoding': 'identity', 'User-Agent': 'Podzemelye/manual-schedule-check'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.getheader('Location', ''))
                    target(url)
                    continue
                length = response.getheader('Content-Length')
                if (response.status != 200 or response.getheader('Content-Encoding') not in (None, 'identity')
                        or length is not None and (not length.isascii() or not length.isdecimal()
                                                  or len(length) > 12 or int(length) > limit)):
                    raise ValueError('Сайт не передал файл допустимого размера.')
                data = response.read(limit + 1)
                if len(data) > limit or length is not None and len(data) != int(length):
                    raise ValueError('Файл слишком большой или загружен не полностью.')
                return data, url
            finally:
                connection.close()
        except (OSError, http.client.HTTPException):
            raise ValueError('Сайт АКВТ недоступен. Сохранённое расписание не изменено.') from None
    raise ValueError('Слишком много перенаправлений сайта АКВТ.')


class Links(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.urls = base, set()

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        href = dict(attrs).get('href', '')
        url = urljoin(self.base, href)
        try:
            target(url)
        except ValueError:
            return
        if urlsplit(url).path.lower().endswith('.xls'):
            self.urls.add(url)
            if len(self.urls) > 64:
                raise ValueError('Слишком много XLS-ссылок. Нужна ручная проверка страницы.')


def discover():
    data, url = fetch(PAGE, limit=1024 * 1024)
    parser = Links(url)
    parser.feed(data.decode('utf-8-sig'))
    if not parser.urls:
        raise ValueError('На странице не найдены XLS. Сохранённое расписание не изменено.')
    return sorted(parser.urls)


def download(url, available, *, gray_other_building=False):
    if url not in available:
        raise ValueError('Выбери точную XLS-ссылку из результата текущей проверки сайта.')
    target(url)
    data, final = fetch(url, limit=XLS_LIMIT)
    if not urlsplit(final).path.lower().endswith('.xls') or not data.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        raise ValueError('Сайт не передал ожидаемый BIFF XLS.')
    with TemporaryDirectory(prefix='akvt-check-') as directory:
        path = Path(directory) / 'schedule.xls'
        path.write_bytes(data)
        return parse_xls(path, gray_other_building=gray_other_building)
