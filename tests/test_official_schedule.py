import unittest
from unittest.mock import Mock, patch

from app.services import official_schedule as source


class OfficialScheduleTests(unittest.TestCase):
    def test_only_exact_official_https_and_public_dns(self):
        for url in ('http://akvt.ru/a.xls','https://akvt.ru.evil/a.xls','https://x@akvt.ru/a.xls',
                    'https://akvt.ru:444/a.xls','https://127.0.0.1/a.xls','https://akvt.ru/a.xls#x'):
            with self.subTest(url=url),self.assertRaises(ValueError):source.target(url)
        for address in ('127.0.0.1','::1','169.254.1.1','10.0.0.1','::ffff:127.0.0.1'):
            with patch.object(source.socket,'getaddrinfo',return_value=[(0,0,0,'',(address,443))]), self.assertRaises(ValueError):
                source.fetch(source.PAGE,limit=100)

    def test_pinned_connection_and_no_cross_host_redirect(self):
        response=Mock(status=302)
        response.getheader.side_effect=lambda name,default=None:'https://evil.example/x.xls' if name=='Location' else default
        with patch.object(source.socket,'getaddrinfo',return_value=[(0,0,0,'',('8.8.8.8',443))]),patch.object(source,'PinnedHTTPS') as connection:
            connection.return_value.getresponse.return_value=response
            with self.assertRaises(ValueError):source.fetch(source.PAGE,limit=100)
            connection.assert_called_once_with('www.akvt.ru','8.8.8.8')
            connection.return_value.close.assert_called_once()

    def test_discovery_explicit_selection_no_arbitrary_url(self):
        html=b'<a href="/a.xls">A</a><a href="https://evil.example/b.xls">B</a><a href="/c.xlsx">C</a>'
        with patch.object(source,'fetch',return_value=(html,source.PAGE)):
            self.assertEqual(source.discover(),['https://www.akvt.ru/a.xls'])
        with self.assertRaises(ValueError):source.download('https://akvt.ru/unknown.xls',[])

    def test_signature_size_and_unavailable_site_are_fail_closed(self):
        url='https://www.akvt.ru/a.xls'
        with patch.object(source,'fetch',return_value=(b'html',url)),self.assertRaises(ValueError):source.download(url,[url])
        with patch.object(source.socket,'getaddrinfo',side_effect=OSError),self.assertRaisesRegex(ValueError,'не изменено'):
            source.fetch(source.PAGE,limit=100)
        response=Mock(status=200)
        response.getheader.side_effect=lambda name,default=None:'101' if name=='Content-Length' else default
        with patch.object(source.socket,'getaddrinfo',return_value=[(0,0,0,'',('8.8.8.8',443))]),patch.object(source,'PinnedHTTPS') as connection:
            connection.return_value.getresponse.return_value=response
            with self.assertRaises(ValueError):source.fetch(source.PAGE,limit=100)
            response.read.assert_not_called()
