import re
import time
import base64
import logging
from hashlib import sha256
from collections import deque
from http.cookiejar import LWPCookieJar, LoadError

import requests
from requests.exceptions import JSONDecodeError

from lib import tp_text
from gazelle import torrent_info
from gazelle.tracker_data import tr


class RequestFailure(Exception):
    pass

report = logging.getLogger(__name__)


class BaseApi:
    def __init__(self, tracker, **kwargs):
        assert tracker in tr, 'Unknown Tracker'  # TODO uitext
        self.tr = tracker
        self.url = self.tr.site
        self.session = requests.Session()
        self.last_x_reqs = deque([.0], maxlen=self.tr.req_limit)
        self.authenticate(kwargs)
        self._account_info = None

    def _rate_limit(self):
        t = time.time() - self.last_x_reqs[0]
        if t <= 10:
            time.sleep(10 - t)

    def authenticate(self, _):
        return NotImplementedError

    @property
    def announce(self):
        return self.tr.tracker.format(**self.account_info)

    @ property
    def account_info(self):
        if not self._account_info:
            self._account_info = self.get_account_info()

        return self._account_info

    def get_account_info(self):
        r = self.request('index')
        return {k: v for k, v in r.copy().items() if k in ('authkey', 'passkey', 'id', 'username')}

    def request(self, url_suffix, data=None, files=None, **kwargs):
        url = self.url + url_suffix + '.php'
        report.debug(f'{url_suffix} {kwargs}')
        req_method = 'POST' if data or files else 'GET'

        self._rate_limit()
        r = self.session.request(req_method, url, params=kwargs, data=data, files=files)
        self.last_x_reqs.append(time.time())

        try:
            r_dict = r.json()
        except JSONDecodeError:
            if 'application/x-bittorrent' in r.headers['content-type']:
                return r.content
            else:
                raise RequestFailure('no json, no torrent')
        else:
            status = r_dict.get('status')
            if status == 'success':
                return r_dict['response']
            elif status == 'failure':
                raise RequestFailure(r_dict['error'])

            raise RequestFailure(r_dict)

    def torrent_info(self, **kwargs):
        r = self.request('torrent', **kwargs)

        return torrent_info.tr_map[self.tr](r)

    def upload(self, data, files, dest_group=None):
        data_dict = data.upl_dict(self.tr, dest_group)
        upl_files = files.files_list(self.announce, self.tr.name)
        return self._uploader(data_dict, upl_files)

    def _uploader(self, data, files):
        r = self.request('upload', data=data, files=files)

        return self.upl_response_handler(r)

    def upl_response_handler(self, r):
        raise NotImplementedError


class KeyApi(BaseApi):

    def authenticate(self, kwargs):
        key = kwargs['key']
        self.session.headers.update({"Authorization": key})

    def request(self, action, data=None, files=None, **kwargs):
        kwargs.update(action=action)
        return super().request('ajax', data=data, files=files, **kwargs)

    def upl_response_handler(self, r):
        raise NotImplementedError


class CookieApi(BaseApi):

    def authenticate(self, kwargs):
        self.session.cookies = LWPCookieJar(f'cookie{self.tr.name}.txt')
        if not self._load_cookie():
            self._login(kwargs)

    def _load_cookie(self):
        jar = self.session.cookies
        try:
            jar.load()
            session_cookie = [c for c in jar if c.name == "session"][0]
            assert not session_cookie.is_expired()
        except (FileNotFoundError, LoadError, IndexError, AssertionError):
            return False

        return True

    def _login(self, kwargs):
        username, password = kwargs['f']()
        data = {'username': username,
                'password': password,
                'keeplogged': '1'}
        self.session.cookies.clear()
        self.request('login', data=data)
        assert [c for c in self.session.cookies if c.name == 'session']
        self.session.cookies.save()

    def request(self, action, data=None, files=None, **kwargs):
        if action in ('upload', 'login'):  # TODO download?
            url_addon = action
        else:
            url_addon = 'ajax'
            kwargs.update(action=action)

        return super().request(url_addon, data=data, files=files, **kwargs)

    def _uploader(self, data, files):
        data['submit'] = True
        super()._uploader(data, files)

    def upl_response_handler(self, r):
        if 'torrents.php' not in r.url:
            warning = re.search(r'<p style="color: red;text-align:center;">(.+?)</p>', r.text)
            raise RequestFailure(f"{warning.group(1) if warning else r.url}")
        return r.url  # TODO re torrentid from url and return


class HtmlApi(CookieApi):

    def get_account_info(self):
        r = self.session.get(self.url + 'index.php')
        return {
            'authkey': re.search(r"authkey=(.+?)[^a-zA-Z0-9]", r.text).group(1),
            'passkey': re.search(r"passkey=(.+?)[^a-zA-Z0-9]", r.text).group(1),
            'id': int(re.search(r"useri?d?=(.+?)[^0-9]", r.text).group(1))
        }

    def torrent_info(self, **kwargs):
        raise AttributeError(f'{self.tr.name} does not provide torrent info')


class RedApi(KeyApi):
    def __init__(self, key=None):
        super().__init__(tr.RED, key=key)

    def _uploader(self, data, files):
        unknown = False
        if data.get('unknown'):
            del data['unknown']
            unknown = True
        torrent_id, group_id = super()._uploader(data, files)
        if unknown:
            try:
                self.request('torrentedit', id=torrent_id, data={'unknown': True})
                report.info(tp_text.upl_to_unkn)
            except (RequestFailure, requests.HTTPError) as e:
                report.error(f'{tp_text.edit_fail}{str(e)}')
        return torrent_id, group_id, self.url + f"torrents.php?id={group_id}&torrentid={torrent_id}"

    def upl_response_handler(self, r):
        return r.get('torrentid'), r.get('groupid')


class OpsApi(KeyApi):
    def __init__(self, key=None):
        super().__init__(tr.OPS, key=f"token {key}")

    def upl_response_handler(self, r):
        group_id = r.get('groupId')
        torrent_id = r.get('torrentId')

        return torrent_id, group_id, self.url + f"torrents.php?id={group_id}&torrentid={torrent_id}"

    def get_riplog(self, tor_id: int, log_id: int):
        r: dict = self.request('riplog', id=tor_id, logid=log_id)
        log_bytes = base64.b64decode(r['log'])
        log_checksum = sha256(log_bytes).hexdigest()
        assert log_checksum == r['log_sha256']
        return log_bytes


def sleeve(trckr, **kwargs):
    api_map = {
        tr.RED: RedApi,
        tr.OPS: OpsApi
    }
    return api_map[trckr](**kwargs)
