#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
키보드 내려치기 감지 · 도우미 (slam_helper.py)

  - slam_final.html 을 http://127.0.0.1:8765 주소로 열어 줍니다 (크롬 / 엣지).
  - 경고가 뜨면 다른 모든 창보다 앞에 빨간 "그러지 마세요" 전체 화면을 띄웁니다 (항상 위).
  - 키보드를 내려치면 디스코드 웹훅으로 친구들에게 알림을 보냅니다.
  - 카카오톡 "나에게 보내기"로 내 카톡 '나와의 채팅'에도 사진과 함께 보냅니다 (카카오 로그인 필요).

  필요한 것: 파이썬 3.8 이상 (python.org 설치본에는 tkinter 가 들어 있음). 추가 설치 없음.
  실행:      python slam_helper.py              (slam_final.html 과 같은 폴더에 두기)
  옵션:      --port 8765      다른 포트 쓰기
             --no-browser     크롬을 자동으로 열지 않기
             --no-window      항상 위 경고 창 없이 디스코드 알림만 (tkinter 가 없을 때 자동으로 이렇게 됨)

  화면(크롬)과 도우미는 이 컴퓨터 안에서만 이야기합니다 (127.0.0.1). 밖으로 나가는 것은 디스코드 알림뿐입니다.

  창을 닫아도 안 풀립니다:
    - 경고 중에는 도우미가 "잠김" 상태를 기억합니다 (slam_state.json). 도우미를 껐다 켜도 그대로입니다.
    - 크롬을 닫으면 몇 초 뒤 도우미가 크롬을 다시 열고, 화면은 경고를 다시 띄웁니다.
    - 경고 창은 Alt+F4 로 닫히지 않고, 경고 중에는 도우미 창도 닫히지 않습니다.
    - 푸는 방법은 정해진 해제 방법(웃음 / 버튼)이나 비상 해제 버튼뿐입니다.
"""
import argparse
import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = 1
HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, 'slam_final.html')
CONFIG_PATH = os.path.join(HERE, 'slam_config.json')
STATE_PATH = os.path.join(HERE, 'slam_state.json')     # 경고 중인지 (도우미를 다시 켜도 유지)
LOG_PATH = os.path.join(HERE, 'slam_helper.log')
KAKAO_PATH = os.path.join(HERE, 'slam_kakao.json')       # 카카오 키와 로그인 토큰 (남에게 주지 말 것)
PAGE_SILENT_S = 75           # 화면이 이만큼 아무 소식 없으면 닫힌 것으로 봄 (크롬이 뒤에 있으면 타이머가 느려짐)
PAGE_GONE_S = 4              # 화면이 "닫혀요"라고 알린 뒤 이만큼 안 돌아오면 다시 엶
REOPEN_EVERY_S = 30          # 크롬 다시 열기는 이 간격보다 자주 하지 않음

WEBHOOK_PREFIXES = (
    'https://discord.com/api/webhooks/',
    'https://discordapp.com/api/webhooks/',
    'https://ptb.discord.com/api/webhooks/',
    'https://canary.discord.com/api/webhooks/',
)
DEFAULT_CFG = {'webhook_url': '', 'name': '', 'send_photo': False, 'mention_here': False, 'cooldown_sec': 20}


def find_html():
    """화면 파일 찾기. 이름이 slam_final.html 이 아니어도 (예: 'slam_final (1).html') 이 화면이면 찾음."""
    if os.path.exists(HTML_PATH):
        return HTML_PATH
    found = []
    try:
        for name in os.listdir(HERE):
            full = os.path.join(HERE, name)
            if name.lower().endswith(('.html', '.htm')) and os.path.isfile(full):
                try:
                    with open(full, 'rb') as f:
                        if b'slamFinalPrefs' in f.read():            # 이 화면에만 있는 글자
                            found.append(full)
                except OSError:
                    pass
    except OSError:
        pass
    return max(found, key=os.path.getmtime) if found else None    # 여러 개면 가장 최근 것


def log(*a):
    line = ' '.join([time.strftime('[%H:%M:%S]')] + [str(x) for x in a])
    if sys.stdout is not None:                       # pythonw 로 실행하면 콘솔이 없음
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d ') + line + '\n')
    except Exception:
        pass


# ======================================================================
#  설정 파일 (slam_config.json) — 웹훅 주소는 여기에만 저장됩니다
# ======================================================================
class Config:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = dict(DEFAULT_CFG)
        try:
            with open(path, encoding='utf-8') as f:
                saved = json.load(f)
            for k in DEFAULT_CFG:
                if k in saved:
                    self.data[k] = saved[k]
        except FileNotFoundError:
            pass
        except Exception as e:
            log('설정 파일을 읽지 못해 기본값을 씁니다:', e)

    def get(self):
        with self.lock:
            return dict(self.data)

    def update(self, req):
        """화면에서 온 설정. 잘못된 값은 (값, 오류문구) 로 알려 줌."""
        err = None
        with self.lock:
            d = self.data
            if req.get('clear_webhook'):
                d['webhook_url'] = ''
            url = (req.get('webhook_url') or '').strip()
            if url:
                if url.startswith(WEBHOOK_PREFIXES) and len(url) < 300 and ' ' not in url:
                    d['webhook_url'] = url
                else:
                    err = '웹훅 주소가 아닌 것 같아요. https://discord.com/api/webhooks/ 로 시작하는 주소를 붙여 넣으세요.'
            if 'name' in req:
                d['name'] = str(req.get('name') or '').strip()[:32]
            if 'send_photo' in req:
                d['send_photo'] = bool(req.get('send_photo'))
            if 'mention_here' in req:
                d['mention_here'] = bool(req.get('mention_here'))
            if 'cooldown_sec' in req:
                try:
                    d['cooldown_sec'] = max(0, min(3600, int(req.get('cooldown_sec'))))
                except (TypeError, ValueError):
                    pass
            snapshot = dict(d)
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
        except Exception as e:
            err = err or ('설정을 저장하지 못했습니다: %s' % e)
        return err


def hook_hint(url):
    if not url:
        return ''
    return '(…%s)' % url.rstrip('/')[-4:]


# ======================================================================
#  공통: HTTP 보내기
# ======================================================================
def multipart(fields, files):
    """fields: {이름: 문자열}, files: {이름: (파일이름, 바이트, 종류)} -> (body, content-type)"""
    boundary = '----slamguard' + uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n'
                    'Content-Type: application/json\r\n\r\n' % (boundary, k)).encode())
        out += [v.encode('utf-8'), b'\r\n']
    for k, (fname, data, ctype) in files.items():
        out.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                    'Content-Type: %s\r\n\r\n' % (boundary, k, fname, ctype)).encode())
        out += [data, b'\r\n']
    out.append(('--%s--\r\n' % boundary).encode())
    return b''.join(out), 'multipart/form-data; boundary=' + boundary


def http_post(url, body, headers, timeout=10):
    """(상태코드, 응답 바이트). 연결 자체가 안 되면 (0, 오류문구)."""
    h = {'User-Agent': 'SlamGuard (local helper, %d)' % VERSION}
    h.update(headers)
    req = urllib.request.Request(url, data=body, method='POST', headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode('utf-8', 'replace')


# ======================================================================
#  디스코드 웹훅
# ======================================================================
def discord_post(url, payload, jpeg=None):
    """웹훅으로 보내기. (성공?, 설명) 을 돌려줌. 사진이 있으면 첨부 파일로 보냄."""
    if jpeg:
        boundary = '----slamguard' + uuid.uuid4().hex
        body = b''.join([
            ('--%s\r\nContent-Disposition: form-data; name="payload_json"\r\n'
             'Content-Type: application/json\r\n\r\n' % boundary).encode(),
            json.dumps(payload, ensure_ascii=False).encode('utf-8'), b'\r\n',
            ('--%s\r\nContent-Disposition: form-data; name="files[0]"; filename="slam.jpg"\r\n'
             'Content-Type: image/jpeg\r\n\r\n' % boundary).encode(),
            jpeg, b'\r\n',
            ('--%s--\r\n' % boundary).encode(),
        ])
        ctype = 'multipart/form-data; boundary=' + boundary
    else:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        ctype = 'application/json'
    target = url + ('&' if '?' in url else '?') + 'wait=true'
    for _ in range(3):
        req = urllib.request.Request(target, data=body, method='POST', headers={
            'Content-Type': ctype, 'User-Agent': 'SlamGuard (local helper, %d)' % VERSION})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return True, 'HTTP %d' % r.status
        except urllib.error.HTTPError as e:
            text = e.read().decode('utf-8', 'replace')
            if e.code == 429:                                   # 너무 자주 보냄: 잠깐 기다렸다 다시
                try:
                    wait = float(json.loads(text).get('retry_after', 1))
                except Exception:
                    wait = 2
                time.sleep(min(max(wait, 0.5), 10))
                continue
            if e.code in (401, 404):
                return False, '웹훅을 찾을 수 없어요 (%d). 디스코드에서 웹훅이 지워졌거나 주소가 틀렸습니다.' % e.code
            return False, 'HTTP %d %s' % (e.code, text[:160])
        except Exception as e:
            return False, '보내기 실패: %s' % e
    return False, '디스코드가 너무 자주 보냈다고 거절했어요 (429). 잠시 뒤 다시 해 보세요.'


# ======================================================================
#  카카오톡 "나에게 보내기"
#   1) 카카오 로그인 (talk_message 동의) -> 토큰을 slam_kakao.json 에 저장, 만료되면 자동 갱신
#   2) 사진은 카카오 서버에 올려 주소를 받고 (이 컴퓨터 주소는 카톡이 못 읽음)
#   3) 기본 템플릿(feed)으로 '나와의 채팅'에 보냄. 친구에게 보내기는 카카오 권한 승인이 따로 필요해서 넣지 않음
# ======================================================================
KAUTH = 'https://kauth.kakao.com'
KAPI = 'https://kapi.kakao.com'


class Kakao:
    FIELDS = ('rest_key', 'client_secret', 'access_token', 'refresh_token', 'expires_at',
              'refresh_expires_at', 'enabled', 'send_photo')

    def __init__(self, path, port, http=http_post):
        self.path = path
        self.port = port
        self.http = http
        self.lock = threading.Lock()
        self.d = {'rest_key': '', 'client_secret': '', 'access_token': '', 'refresh_token': '',
                  'expires_at': 0, 'refresh_expires_at': 0, 'enabled': True, 'send_photo': True}
        self.state = None
        self.last_result = ''
        try:
            with open(path, encoding='utf-8') as f:
                saved = json.load(f)
            for k in self.FIELDS:
                if k in saved:
                    self.d[k] = saved[k]
        except FileNotFoundError:
            pass
        except Exception as e:
            log('카카오 설정 파일을 읽지 못했습니다:', e)

    # ---------- 상태 ----------
    @property
    def redirect_uri(self):
        return 'http://localhost:%d/kakao/callback' % self.port

    def logged_in(self):
        d = self.d
        return bool(d['access_token'] and d['refresh_token'] and
                    (not d['refresh_expires_at'] or d['refresh_expires_at'] > time.time()))

    def ready(self):
        return bool(self.d['rest_key'] and self.d['enabled'] and self.logged_in())

    def _save(self):
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self.d, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log('카카오 설정을 저장하지 못했습니다:', e)

    def public(self):
        d = self.d
        left = int((d['refresh_expires_at'] - time.time()) / 86400) if d['refresh_expires_at'] else None
        return {'key_set': bool(d['rest_key']), 'key_hint': ('(…%s)' % d['rest_key'][-4:]) if d['rest_key'] else '',
                'secret_set': bool(d['client_secret']), 'logged_in': self.logged_in(),
                'login_days_left': left, 'enabled': bool(d['enabled']), 'send_photo': bool(d['send_photo']),
                'redirect_uri': self.redirect_uri, 'site': 'http://localhost:%d' % self.port,
                'last': self.last_result}

    def update(self, req):
        with self.lock:
            key = str(req.get('rest_key') or '').strip()
            if key:
                if not key.isalnum() or len(key) > 64:
                    return 'REST API 키가 아닌 것 같아요. 카카오디벨로퍼스 [앱 키]의 "REST API 키"를 그대로 붙여 넣으세요.'
                if key != self.d['rest_key']:                   # 다른 앱 키 = 로그인도 새로
                    self.d.update(rest_key=key, access_token='', refresh_token='', expires_at=0, refresh_expires_at=0)
            sec = str(req.get('client_secret') or '').strip()
            if sec:
                self.d['client_secret'] = sec[:128]
            if req.get('clear_secret'):
                self.d['client_secret'] = ''
            for k in ('enabled', 'send_photo'):
                if k in req:
                    self.d[k] = bool(req[k])
            self._save()
        return None

    def logout(self):
        with self.lock:
            tok = self.d['access_token']
            self.d.update(access_token='', refresh_token='', expires_at=0, refresh_expires_at=0)
            self._save()
        if tok:
            self.http(KAPI + '/v1/user/logout', b'', {'Authorization': 'Bearer ' + tok})

    # ---------- 로그인 ----------
    def login_url(self):
        if not self.d['rest_key']:
            return None
        self.state = uuid.uuid4().hex
        q = urllib.parse.urlencode({'client_id': self.d['rest_key'], 'redirect_uri': self.redirect_uri,
                                    'response_type': 'code', 'scope': 'talk_message', 'state': self.state})
        return KAUTH + '/oauth/authorize?' + q

    def _token(self, params):
        params = dict(params, client_id=self.d['rest_key'])
        if self.d['client_secret']:
            params['client_secret'] = self.d['client_secret']
        st, body = self.http(KAUTH + '/oauth/token', urllib.parse.urlencode(params).encode(),
                             {'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'})
        try:
            j = json.loads(body.decode('utf-8'))
        except Exception:
            j = {}
        if st != 200 or 'access_token' not in j:
            return None, kakao_error(st, j, body)
        now = time.time()
        with self.lock:
            self.d['access_token'] = j['access_token']
            self.d['expires_at'] = now + int(j.get('expires_in', 21599))
            if j.get('refresh_token'):                          # 갱신 때는 만료가 가까울 때만 새로 옴
                self.d['refresh_token'] = j['refresh_token']
                self.d['refresh_expires_at'] = now + int(j.get('refresh_token_expires_in', 5184000))
            self._save()
        return j, None

    def callback(self, query):
        q = urllib.parse.parse_qs(query)
        if q.get('error'):
            return False, '카카오 로그인이 취소되었거나 거절되었습니다: ' + (q.get('error_description') or q['error'])[0]
        if not self.state or (q.get('state') or [''])[0] != self.state:
            return False, '로그인 요청이 맞지 않습니다. 화면에서 [카카오 로그인]을 다시 누르세요.'
        self.state = None
        code = (q.get('code') or [''])[0]
        j, err = self._token({'grant_type': 'authorization_code', 'redirect_uri': self.redirect_uri, 'code': code})
        if err:
            return False, err
        scope = j.get('scope', '')
        if scope and 'talk_message' not in scope.split():
            return False, ('로그인은 됐지만 "카카오톡 메시지 전송"에 동의하지 않았어요. '
                           '카카오디벨로퍼스 [동의항목]에서 카카오톡 메시지 전송을 켜고, 다시 로그인해 동의해 주세요.')
        log('카카오 로그인 완료')
        return True, '카카오 로그인 완료'

    def _access(self, force=False):
        if not self.logged_in():
            return None, '카카오 로그인이 필요해요. 화면의 [카카오 로그인]을 누르세요.'
        if force or time.time() > self.d['expires_at'] - 300:
            _, err = self._token({'grant_type': 'refresh_token', 'refresh_token': self.d['refresh_token']})
            if err:
                with self.lock:
                    self.d.update(access_token='', refresh_token='')
                    self._save()
                return None, '카카오 로그인이 만료되었어요. 다시 로그인하세요. (' + err + ')'
        return self.d['access_token'], None

    # ---------- 보내기 ----------
    def _call(self, url, body, ctype):
        tok, err = self._access()
        if err:
            return None, err
        for attempt in (0, 1):
            st, raw = self.http(url, body, {'Authorization': 'Bearer ' + tok, 'Content-Type': ctype})
            try:
                j = json.loads(raw.decode('utf-8'))
            except Exception:
                j = {}
            if st == 401 and attempt == 0:                      # 토큰이 갑자기 만료됨: 한 번 갱신 후 다시
                tok, err = self._access(force=True)
                if err:
                    return None, err
                continue
            if st != 200:
                return None, kakao_error(st, j, raw)
            return j, None
        return None, '카카오 인증 실패'

    def upload_image(self, jpeg):
        """사진을 카카오 서버에 올리고 주소를 받음 (REST API 키로 인증)"""
        body, ctype = multipart({}, {'file': ('slam.jpg', jpeg, 'image/jpeg')})
        st, raw = self.http(KAPI + '/v2/api/talk/message/image/upload', body,
                            {'Authorization': 'KakaoAK ' + self.d['rest_key'], 'Content-Type': ctype}, 20)
        try:
            j = json.loads(raw.decode('utf-8'))
            info = j['infos']['original']
            return info['url'], int(info.get('width') or 0), int(info.get('height') or 0), None
        except Exception:
            return None, 0, 0, '사진 올리기 실패: ' + kakao_error(st, None, raw)

    def send_memo(self, template):
        body = urllib.parse.urlencode({'template_object': json.dumps(template, ensure_ascii=False)}).encode()
        j, err = self._call(KAPI + '/v2/api/talk/memo/default/send', body,
                            'application/x-www-form-urlencoded;charset=utf-8')
        if err:
            return False, err
        if j.get('result_code', 0) != 0:
            return False, '카카오가 거절함: %s' % j
        return True, '보냄'

    def _link(self):
        site = 'http://localhost:%d' % self.port
        return {'web_url': site, 'mobile_web_url': site}

    def send_slam(self, who, ev, skipped):
        lines = ['오늘 %s번째' % ev.get('count', '?')]
        if ev.get('face'):
            lines.append('표정: %s' % ev['face'])
        lines.append('압력 %s · 진동 %s' % (ev.get('press', 0), ev.get('shake', 0)))
        if skipped:
            lines.append('그 사이 %d번 더' % skipped)
        title = ('%s님이 키보드를 내려쳤어요!' % who) if who else '키보드를 내려쳤어요!'
        img_err = None
        if self.d['send_photo'] and ev.get('shotJpeg'):
            try:
                jpeg = base64.b64decode(ev['shotJpeg'])
            except Exception:
                jpeg = None
            if jpeg:
                url, w, h, img_err = self.upload_image(jpeg)
                if url:
                    content = {'title': title, 'description': ' · '.join(lines), 'image_url': url, 'link': self._link()}
                    if w and h:
                        content.update(image_width=w, image_height=h)
                    ok, why = self.send_memo({'object_type': 'feed', 'content': content,
                                              'buttons': [{'title': '화면 열기', 'link': self._link()}]})
                    return self._result(ok, why)
        ok, why = self.send_memo({'object_type': 'text', 'text': title + '\n' + '\n'.join(lines),
                                  'link': self._link(), 'button_title': '화면 열기'})
        if ok and img_err:
            why = '보냄 (사진 없이: %s)' % img_err
        return self._result(ok, why)

    def test(self):
        if not self.d['rest_key']:
            return self._result(False, '먼저 REST API 키를 저장하세요.')
        ok, why = self.send_memo({'object_type': 'text', 'link': self._link(), 'button_title': '화면 열기',
                                  'text': '키보드 지킴이 연결 테스트예요. 키보드를 내려치면 여기로 알려 드릴게요.'})
        return self._result(ok, why)

    def _result(self, ok, why):
        self.last_result = time.strftime('%H:%M:%S ') + (why if ok else '실패: ' + why)
        log('카카오톡:', self.last_result)
        return ok, why


def kakao_error(status, j, raw):
    """카카오 오류를 알아듣게"""
    if status == 0:
        return '인터넷 연결 실패: ' + raw.decode('utf-8', 'replace')[:120]
    if j is None:
        try:
            j = json.loads(raw.decode('utf-8'))
        except Exception:
            j = {}
    code = j.get('code')
    desc = j.get('error_description') or j.get('msg') or raw.decode('utf-8', 'replace')[:160]
    hints = {
        'KOE006': '리다이렉트 URI 가 등록되지 않았어요. 화면에 나온 주소를 카카오디벨로퍼스에 그대로 등록하세요.',
        'KOE010': '클라이언트 시크릿이 맞지 않아요. 카카오디벨로퍼스의 값과 화면의 값을 확인하세요.',
        'KOE101': 'REST API 키가 맞지 않아요.',
        'KOE320': '로그인 코드가 만료됐어요. [카카오 로그인]을 다시 누르세요.',
    }
    for k, v in hints.items():
        if k in desc or j.get('error_code') == k:
            return v
    if code == -402:
        return '"카카오톡 메시지 전송" 동의가 없어요. 동의항목을 켜고 다시 로그인하세요.'
    if code == -401:
        return '카카오 로그인이 만료됐어요. 다시 로그인하세요.'
    return 'HTTP %s %s' % (status, desc)


class Notifier:
    """내려치기 알림 (디스코드 + 카카오톡 나에게). 너무 자주 가지 않게 간격(cooldown)을 둠."""

    def __init__(self, cfg, post=discord_post, kakao=None):
        self.cfg = cfg
        self.post = post
        self.kakao = kakao
        self.lock = threading.Lock()
        self.last_sent = 0.0
        self.skipped = 0
        self.last_result = ''

    def _result(self, ok, why):
        self.last_result = time.strftime('%H:%M:%S ') + ('보냄' if ok else '실패: ' + why)
        log('디스코드:', self.last_result)

    def slam(self, ev):
        c = self.cfg.get()
        kakao_on = bool(self.kakao and self.kakao.ready())
        if ev.get('test') or not (c['webhook_url'] or kakao_on):
            return
        with self.lock:
            now = time.time()
            if now - self.last_sent < c['cooldown_sec']:
                self.skipped += 1
                return
            self.last_sent = now
            skipped, self.skipped = self.skipped, 0
        # 디스코드와 카카오는 각자 따로 (한쪽이 느리거나 실패해도 다른 쪽과 감지·경고는 그대로)
        if kakao_on:
            threading.Thread(target=self._send_kakao, args=(c, ev, skipped), daemon=True).start()
        if c['webhook_url']:
            threading.Thread(target=self._send_slam, args=(c, ev, skipped), daemon=True).start()

    def _send_kakao(self, c, ev, skipped):
        try:
            self.kakao.send_slam(c['name'], ev, skipped)
        except Exception as e:
            self.kakao._result(False, '예상 못 한 오류: %s' % e)

    def _send_slam(self, c, ev, skipped):
        try:
            self._send_discord(c, ev, skipped)
        except Exception as e:
            self._result(False, '예상 못 한 오류: %s' % e)

    def _send_discord(self, c, ev, skipped):
        who = c['name'] or '친구'
        content = '**%s**님이 방금 키보드를 쾅 내려쳤어요! 🔨' % who
        if c['mention_here']:
            content = '@here ' + content
        fields = [{'name': '오늘 몇 번째', 'value': '%s번째' % ev.get('count', '?'), 'inline': True}]
        if ev.get('face'):
            fields.append({'name': '그때 표정', 'value': str(ev['face'])[:40], 'inline': True})
        fields.append({'name': '세기', 'value': '압력 %s · 진동 %s' % (ev.get('press', 0), ev.get('shake', 0)), 'inline': True})
        if skipped:
            fields.append({'name': '그 사이', 'value': '알림 없이 %d번 더 내려쳤어요' % skipped, 'inline': False})
        embed = {'title': '키보드 내려침 감지', 'color': 0xC8261B, 'fields': fields,
                 'description': '진정할 수 있게 한마디 해 주세요.',
                 'timestamp': datetime.now(timezone.utc).isoformat()}
        jpeg = None
        if c['send_photo'] and ev.get('shotJpeg'):
            try:
                jpeg = base64.b64decode(ev['shotJpeg'])
                embed['image'] = {'url': 'attachment://slam.jpg'}
            except Exception:
                jpeg = None
        payload = {'username': '키보드 지킴이', 'content': content, 'embeds': [embed],
                   'allowed_mentions': {'parse': ['everyone'] if c['mention_here'] else []}}
        ok, why = self.post(c['webhook_url'], payload, jpeg)
        self._result(ok, why)

    def test(self):
        c = self.cfg.get()
        if not c['webhook_url']:
            return False, '먼저 웹훅 주소를 저장하세요.'
        who = c['name'] or '친구'
        ok, why = self.post(c['webhook_url'], {
            'username': '키보드 지킴이',
            'content': '연결 테스트예요. 앞으로 **%s**님이 키보드를 내려치면 여기로 알려 드릴게요.' % who,
            'allowed_mentions': {'parse': []}})
        self._result(ok, why)
        return ok, why

    def public(self):
        c = self.cfg.get()
        return {'configured': bool(c['webhook_url']), 'hint': hook_hint(c['webhook_url']), 'name': c['name'],
                'send_photo': c['send_photo'], 'mention_here': c['mention_here'],
                'cooldown_sec': c['cooldown_sec'], 'last': self.last_result}


# ======================================================================
#  화면(크롬) <-> 도우미 연결 (이 컴퓨터 안에서만)
# ======================================================================
class Bridge:
    def __init__(self, notifier, ui_queue, state_path=None):
        self.notifier = notifier
        self.ui = ui_queue
        self.lock = threading.Lock()
        self.cmds = []
        self.last_contact = 0.0
        self.page_gone_t = 0.0
        self.ui_beat = 0.0                       # 경고 창(tkinter)이 살아 있는지: 0.04초마다 갱신
        self.ui_error = ''
        self.state_path = state_path
        self.locked = None                       # 경고 중이면 {'msg','sub','since'}
        if state_path:
            try:
                with open(state_path, encoding='utf-8') as f:
                    st = json.load(f)
                if isinstance(st, dict) and st.get('locked'):
                    self.locked = st['locked']
                    log('지난번 경고가 아직 안 풀려 있습니다. 다시 띄웁니다.')
            except FileNotFoundError:
                pass
            except Exception as e:
                log('상태 파일을 읽지 못했습니다:', e)

    def touch(self):
        self.last_contact = time.time()

    def _save(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path, 'w', encoding='utf-8') as f:
                json.dump({'locked': self.locked}, f, ensure_ascii=False)
        except Exception as e:
            log('상태를 저장하지 못했습니다:', e)

    def set_lock(self, ev):
        with self.lock:
            self.locked = {'msg': str(ev.get('msg') or '그러지 마세요')[:60], 'sub': str(ev.get('sub') or '')[:60],
                           'holdMs': int(ev.get('holdMs') or 3000), 'since': time.time()}
            self._save()

    def clear_lock(self):
        with self.lock:
            was = self.locked is not None
            self.locked = None
            self._save()
        return was

    def page_missing(self):
        """크롬 화면이 닫힌 것 같으면 True"""
        now = time.time()
        if self.page_gone_t and self.last_contact <= self.page_gone_t and now - self.page_gone_t > PAGE_GONE_S:
            return True
        return now - self.last_contact > PAGE_SILENT_S

    def push_cmd(self, cmd):
        with self.lock:
            self.cmds.append(cmd)

    def window_state(self):
        if self.ui is None:
            return {'ok': False, 'why': 'no-tkinter'}
        if time.time() - self.ui_beat > 3:
            return {'ok': False, 'why': 'stuck', 'error': self.ui_error}
        return {'ok': True, 'error': self.ui_error}

    def take_cmds(self):
        with self.lock:
            c, self.cmds = self.cmds, []
            return c


def html_escape(t):
    return str(t).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def kakao_public(bridge):
    k = bridge.notifier.kakao
    return k.public() if k else None


def make_handler(bridge, port):
    allowed_hosts = {'127.0.0.1:%d' % port, 'localhost:%d' % port}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'SlamHelper/%d' % VERSION

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype='application/json; charset=utf-8'):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            return (self.headers.get('Host') or '') in allowed_hosts      # 다른 사이트가 몰래 부르지 못하게

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {'ok': False})
            path = self.path.split('?')[0]
            if path in ('/', '/index.html', '/slam_final.html'):
                html = find_html()
                try:
                    with open(html, 'rb') as f:
                        data = f.read()
                except (TypeError, OSError):
                    try:
                        names = sorted(os.listdir(HERE))
                    except OSError:
                        names = []
                    msg = ('화면 파일(slam_final.html)을 찾지 못했습니다.\n\n'
                           '도우미가 보고 있는 폴더: %s\n\n이 폴더에 있는 파일:\n  %s\n\n'
                           '받은 slam_final.html 을 이 폴더에 넣고 이 페이지를 새로 고치세요(F5).'
                           % (HERE, '\n  '.join(names) or '(없음)'))
                    return self._send(404, msg.encode('utf-8'), 'text/plain; charset=utf-8')
                return self._send(200, data, 'text/html; charset=utf-8')
            if path == '/api/hello':
                bridge.touch()
                return self._send(200, {'ok': True, 'version': VERSION, 'window': bridge.ui is not None,
                                        'win': bridge.window_state(),
                                        'locked': bridge.locked, 'discord': bridge.notifier.public(),
                                        'kakao': kakao_public(bridge)})
            k = bridge.notifier.kakao
            if path == '/kakao/login' and k:
                url = k.login_url()
                if not url:
                    return self._send(400, '먼저 화면에서 카카오 REST API 키를 저장하세요.'.encode('utf-8'), 'text/plain; charset=utf-8')
                self.send_response(302)
                self.send_header('Location', url)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if path == '/kakao/callback' and k:
                ok, msg = k.callback(self.path.split('?', 1)[1] if '?' in self.path else '')
                page = ('<!doctype html><meta charset="utf-8"><title>카카오 로그인</title>'
                        '<body style="font-family:sans-serif;padding:40px;line-height:1.6">'
                        '<h2>%s</h2><p>%s</p><p>이 탭을 닫고 키보드 화면으로 돌아가세요.</p></body>'
                        % ('카카오 로그인 완료' if ok else '카카오 로그인 실패', html_escape(msg)))
                return self._send(200, page.encode('utf-8'), 'text/html; charset=utf-8')
            return self._send(404, {'ok': False})

        def do_POST(self):
            path = self.path.split('?')[0]
            if path == '/api/bye':           # 화면이 닫히는 중 (sendBeacon 이라 머리글을 못 붙임: 같은 주소에서 온 것만)
                if self._host_ok() and (self.headers.get('Origin') or '') in ('http://' + h for h in allowed_hosts):
                    bridge.page_gone_t = time.time()
                return self._send(200, {'ok': True})
            # 화면만 붙이는 머리글. 다른 사이트는 이 머리글을 붙여 보낼 수 없음.
            if not self._host_ok() or self.headers.get('X-Slam') != '1':
                return self._send(403, {'ok': False})
            try:
                n = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                n = 0
            if n > 8_000_000:
                return self._send(413, {'ok': False})
            try:
                data = json.loads(self.rfile.read(n) or b'{}')
                if not isinstance(data, dict):
                    raise ValueError
            except Exception:
                return self._send(400, {'ok': False})
            bridge.touch()
            ui = bridge.ui
            if path == '/api/slam':
                bridge.take_cmds()
                bridge.set_lock(data)
                if ui is not None and data.get('top', True):
                    ui.put(('slam', data))
                bridge.notifier.slam(data)
                return self._send(200, {'ok': True})
            if path == '/api/status':
                if ui is not None:
                    ui.put(('status', data))
                return self._send(200, {'ok': True, 'cmds': bridge.take_cmds(), 'discord': bridge.notifier.public(),
                                        'win': bridge.window_state(),
                                        'kakao': kakao_public(bridge)})
            if path == '/api/clear':
                if bridge.clear_lock():
                    log('경고가 풀렸습니다.')
                if ui is not None:
                    ui.put(('clear', None))
                return self._send(200, {'ok': True})
            if path == '/api/relock':        # 다시 연 화면이 경고를 다시 띄움 -> 도우미 창도
                if ui is not None and bridge.locked and data.get('top', True):
                    ui.put(('slam', dict(bridge.locked, **data)))
                return self._send(200, {'ok': True, 'locked': bridge.locked})
            if path == '/api/config':
                err = bridge.notifier.cfg.update(data)
                return self._send(200, {'ok': err is None, 'error': err, 'discord': bridge.notifier.public()})
            k = bridge.notifier.kakao
            if path == '/api/kakao/config' and k:
                err = k.update(data)
                return self._send(200, {'ok': err is None, 'error': err, 'kakao': k.public()})
            if path == '/api/kakao/test' and k:
                ok, why = k.test()
                return self._send(200, {'ok': ok, 'error': None if ok else why, 'kakao': k.public()})
            if path == '/api/kakao/logout' and k:
                k.logout()
                return self._send(200, {'ok': True, 'kakao': k.public()})
            if path == '/api/discord-test':
                ok, why = bridge.notifier.test()
                return self._send(200, {'ok': ok, 'error': None if ok else why, 'discord': bridge.notifier.public()})
            return self._send(404, {'ok': False})

    return Handler


def watchdog(bridge, url, opener):
    """경고 중에 크롬 화면이 닫히면 다시 연다."""
    last_open = 0.0
    while True:
        time.sleep(1)
        if bridge.locked and bridge.page_missing() and time.time() - last_open > REOPEN_EVERY_S:
            last_open = time.time()
            bridge.page_gone_t = 0.0
            log('경고 중에 화면(크롬)이 닫혀서 다시 엽니다.')
            try:
                opener(url)
            except Exception as e:
                log('크롬을 다시 열지 못했습니다:', e)


# ======================================================================
#  항상 위 경고 창 (tkinter)
# ======================================================================
def windows_setup():
    """화면 배율(125%, 150%)에서도 흐리지 않고 정확한 크기로."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def win_force_front(tkwin):
    """윈도우: 경고 창을 '항상 위' 맨 앞으로 + 키보드 초점까지 가져오기.
    윈도우는 다른 프로그램이 앞 창을 마음대로 바꾸지 못하게 막아서, Alt 키를 한 번 눌렀다 뗀 것처럼 해야 초점이 넘어옴."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.GetParent(tkwin.winfo_id()) or tkwin.winfo_id()
        HWND_TOPMOST, SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = -1, 0x0001, 0x0002, 0x0040
        u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        if u.GetForegroundWindow() != hwnd:
            VK_MENU, KEYUP = 0x12, 0x0002
            u.keybd_event(VK_MENU, 0, 0, 0)
            u.keybd_event(VK_MENU, 0, KEYUP, 0)
            u.SetForegroundWindow(hwnd)
    except Exception:
        pass


def screen_boxes(root):
    """(모든 모니터를 덮는 상자, 주 모니터 상자). 윈도우가 아니면 주 모니터만."""
    pw, ph = root.winfo_screenwidth(), root.winfo_screenheight()
    if sys.platform == 'win32':
        try:
            import ctypes
            u = ctypes.windll.user32
            vx, vy, vw, vh = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))
            if vw > 0 and vh > 0:
                return (vx, vy, vw, vh), (0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1))
        except Exception:
            pass
    return (0, 0, pw, ph), (0, 0, pw, ph)


def run_window(root_factory, bridge, ui_queue, url, open_page):
    import tkinter as tk

    RED, WHITE, DARK, FAINT = '#c8261b', '#ffffff', '#2b0f0c', '#e07a71'
    FONT = 'Malgun Gothic' if sys.platform == 'win32' else ('Apple SD Gothic Neo' if sys.platform == 'darwin' else 'Noto Sans CJK KR')

    root = root_factory()
    root.title('키보드 내려치기 도우미')
    root.resizable(False, False)
    root.configure(padx=16, pady=14)

    # ---------------- 작은 조작 창 ----------------
    tk.Label(root, text='키보드 내려치기 도우미 실행 중', font=(FONT, 12, 'bold')).pack(anchor='w')
    tk.Label(root, text='화면 주소: ' + url, font=(FONT, 10)).pack(anchor='w', pady=(4, 0))
    st_page = tk.Label(root, font=(FONT, 10), fg='#5c6763')
    st_page.pack(anchor='w')
    st_dc = tk.Label(root, font=(FONT, 10), fg='#5c6763', wraplength=420, justify='left')
    st_dc.pack(anchor='w')
    row = tk.Frame(root)
    row.pack(anchor='w', pady=(10, 0))
    tk.Button(row, text='크롬으로 화면 열기', command=lambda: open_page(url)).pack(side='left')
    tk.Button(row, text='경고 창 테스트 (10초)', command=lambda: ui_queue.put(('test', None))).pack(side='left', padx=(8, 0))
    st_close = tk.Label(root, text='이 창을 닫으면 도우미가 꺼집니다. 경고 중에는 닫히지 않습니다.', font=(FONT, 9), fg='#5c6763')
    st_close.pack(anchor='w', pady=(8, 0))

    # ---------------- 경고 창 ----------------
    win = tk.Toplevel(root)
    win.withdraw()
    win.overrideredirect(True)              # 제목 표시줄 없음
    win.configure(bg=RED, cursor='arrow')
    win.attributes('-topmost', True)        # 항상 위
    win.protocol('WM_DELETE_WINDOW', lambda: None)      # Alt+F4 로 안 닫힘
    win.bind('<Alt-F4>', lambda e: 'break')
    try:
        # 완전히 불투명하면 윈도우가 뒤의 크롬을 "가려짐"으로 보고 카메라 인식을 멈출 수 있어서 아주 살짝 투명하게
        win.attributes('-alpha', 0.97)
    except tk.TclError:
        pass

    box = tk.Frame(win, bg=RED)
    msg = tk.Label(box, text='그러지 마세요', fg=WHITE, bg=RED)
    sub = tk.Label(box, fg=WHITE, bg=RED)
    media = tk.Frame(box, bg=RED)
    shot_img = tk.Label(media, bg=DARK, bd=0)
    shot_cap = tk.Label(media, text='방금 당신 표정', fg=WHITE, bg=RED, font=(FONT, 14, 'bold'))
    live_img = tk.Label(media, bg=DARK, bd=0)
    smile_bar = tk.Canvas(media, width=320, height=12, bg=FAINT, highlightthickness=0)
    live_cap = tk.Label(media, text='', fg=WHITE, bg=RED, font=(FONT, 14, 'bold'))
    btn_row = tk.Frame(box, bg=RED)
    btn_lbls = [tk.Label(btn_row, text=t, fg=WHITE, bg=RED, font=(FONT, 15, 'bold'), padx=18, pady=6,
                         highlightthickness=2, highlightbackground=WHITE) for t in ('웃음', '왼쪽 버튼', '오른쪽 버튼')]
    hold_bar = tk.Canvas(box, width=560, height=22, bg=FAINT, highlightthickness=0)
    hold_txt = tk.Label(box, fg=WHITE, bg=RED, font=(FONT, 17, 'bold'))
    mood = tk.Label(box, fg=WHITE, bg='#a81f15', font=(FONT, 18), padx=16, pady=8, wraplength=1000)
    emg = tk.Label(box, text='비상 해제: 여기를 마우스로 누르고 있기', fg=WHITE, bg=RED, font=(FONT, 11),
                   padx=12, pady=6, highlightthickness=1, highlightbackground=WHITE, cursor='hand2')

    msg.pack()
    sub.pack(pady=(6, 0))
    media.pack(pady=(16, 0))
    btn_row.pack(pady=(18, 0))
    for b in btn_lbls:
        b.pack(side='left', padx=6)
    hold_bar.pack(pady=(10, 0))
    hold_txt.pack(pady=(8, 0))
    mood.pack(pady=(14, 0))
    emg.pack(pady=(14, 0))

    state = {'shown': False, 'hold_ms': 3000, 'emg_t': 0.0, 'shot': None, 'live': None, 'shown_t': 0.0}

    def bar(canvas, frac, mark=None):
        canvas.delete('all')
        w, h = int(canvas['width']), int(canvas['height'])
        canvas.create_rectangle(0, 0, max(0, min(1, frac)) * w, h, fill=WHITE, width=0)
        if mark is not None:
            x = max(0, min(1, mark)) * w
            canvas.create_rectangle(x - 1, 0, x + 2, h, fill=DARK, width=0)

    def photo(b64):
        try:
            return tk.PhotoImage(data=b64)     # PNG
        except Exception:
            return None

    def layout():
        (vx, vy, vw, vh), (px, py, pw, ph) = screen_boxes(root)
        win.geometry('%dx%d%+d%+d' % (vw, vh, vx, vy))
        msg.config(font=(FONT, -max(48, int(min(pw * 0.085, ph * 0.16))), 'bold'))
        sub.config(font=(FONT, -max(18, int(pw * 0.018))))
        # 내용은 주 모니터 가운데에
        box.place(x=px - vx + pw // 2, y=py - vy + ph // 2, anchor='center')

    def place_media():
        for w in (shot_img, shot_cap, live_img, smile_bar, live_cap):
            w.grid_forget()
        col = 0
        if state['shot'] is not None:
            shot_img.config(image=state['shot'])
            shot_img.grid(row=0, column=col, padx=10)
            shot_cap.grid(row=2, column=col)
            col += 1
        if state['live'] is not None:
            live_img.config(image=state['live'])
            live_img.grid(row=0, column=col, padx=10)
            smile_bar.grid(row=1, column=col, pady=(6, 0))
            live_cap.grid(row=2, column=col)

    def show(ev):
        state['shot'] = photo(ev['shotPng']) if ev.get('shotPng') else None
        state['live'] = None
        state['hold_ms'] = int(ev.get('holdMs') or 3000)
        msg.config(text=ev.get('msg') or '그러지 마세요')
        sub.config(text=ev.get('sub') or '')
        hold_txt.config(text='')
        mood.pack_forget()
        bar(hold_bar, 0)
        place_media()
        layout()
        state['shown'] = True
        state['shown_t'] = time.time()
        win.deiconify()
        keep_on_top()
        log('경고 창을 띄웠습니다.')

    def hide(reason=''):
        if not state['shown']:
            return
        state['shown'] = False
        state['emg_t'] = 0.0
        win.withdraw()
        log('경고 창을 닫았습니다.' + (' (' + reason + ')' if reason else ''))

    def keep_on_top():
        if not state['shown']:
            return
        win.attributes('-topmost', True)
        win.lift()
        try:
            win.focus_force()
        except tk.TclError:
            pass
        win_force_front(win)
        if bridge.locked and bridge.page_missing() and not state['emg_t']:
            hold_txt.config(text='크롬 화면이 닫혀 있어요. 다시 여는 중입니다. 창을 닫아도 안 풀려요.')
        win.after(600, keep_on_top)

    def apply_status(s):
        if not state['shown'] or not s.get('active'):
            return
        if s.get('msg'):
            msg.config(text=s['msg'])
        sub.config(text=s.get('sub') or '')
        if s.get('holdMs'):
            state['hold_ms'] = int(s['holdMs'])
        if s.get('live'):
            img = photo(s['live'])
            if img is not None:
                first = state['live'] is None
                state['live'] = img
                if first:
                    place_media()
                else:
                    live_img.config(image=img)
        if s.get('smile') is not None:
            bar(smile_bar, float(s['smile']), float(s.get('smileTh') or 0.55))
        else:
            bar(smile_bar, 0, float(s.get('smileTh') or 0.55))
        live_cap.config(text=s.get('liveCap') or '')
        btns = s.get('btn') or []
        vis = tuple(bool(b.get('show')) for b in btns)
        if len(vis) == len(btn_lbls) and vis != state.get('btn_vis'):
            state['btn_vis'] = vis
            for lbl in btn_lbls:
                lbl.pack_forget()
            for lbl, v in zip(btn_lbls, vis):
                if v:
                    lbl.pack(side='left', padx=6)
        for lbl, b in zip(btn_lbls, btns):
            lbl.config(text=b.get('label') or lbl['text'],
                       bg=WHITE if b.get('on') else RED, fg=RED if b.get('on') else WHITE)
        m = s.get('mood')
        if m and m.get('say'):
            mood.config(text='[%s]  %s' % (m.get('name') or '', m['say']))
            mood.pack(pady=(14, 0), before=emg)
        else:
            mood.pack_forget()
        if not state['emg_t']:
            hold_txt.config(text=s.get('text') or '')
            bar(hold_bar, float(s.get('frac') or 0))

    # 비상 해제: 눌러서 정해진 시간 버티기
    def emg_tick():
        if not state['emg_t'] or not state['shown']:
            return
        held = (time.time() - state['emg_t']) * 1000
        if held >= state['hold_ms']:
            hide('비상 해제')
            bridge.clear_lock()                  # 크롬이 닫혀 있어도 풀림
            bridge.push_cmd('unlock')
            return
        hold_txt.config(text='비상 해제 중… %.1f초 남음' % ((state['hold_ms'] - held) / 1000))
        bar(hold_bar, held / state['hold_ms'])
        win.after(50, emg_tick)

    def emg_down(_e):
        state['emg_t'] = time.time()
        emg_tick()

    def emg_up(_e):
        state['emg_t'] = 0.0

    emg.bind('<ButtonPress-1>', emg_down)
    emg.bind('<ButtonRelease-1>', emg_up)
    emg.bind('<Leave>', emg_up)

    def report(where, e=None):
        msg = '%s: %s' % (where, e if e is not None else '')
        bridge.ui_error = time.strftime('%H:%M:%S ') + msg
        log('경고 창 오류 (계속 동작함) ->', msg)
        log(traceback.format_exc())

    # 화면 그리기 중 어떤 오류가 나도 창이 멈추지 않게 (기록만 남기고 계속)
    root.report_callback_exception = lambda et, ev, tb: report('tk', ev)

    def pump():
        bridge.ui_beat = time.time()
        try:
            while True:
                try:
                    kind, data = ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == 'slam':
                        show(data)
                    elif kind == 'status':
                        apply_status(data)
                    elif kind == 'clear':
                        hide()
                    elif kind == 'test':
                        show({'msg': '그러지 마세요', 'sub': '경고 창 테스트 · 10초 뒤 저절로 닫힘', 'holdMs': 3000})
                        win.after(10000, lambda: (not bridge.locked) and hide('테스트 끝'))
                except Exception as e:
                    report(kind, e)
                    if kind == 'slam' and not state['shown']:      # 그래도 빨간 화면은 띄움
                        try:
                            state['shown'] = True
                            win.deiconify()
                            keep_on_top()
                        except Exception as e2:
                            report('slam-fallback', e2)
            update_status_text()
        except Exception as e:
            report('pump', e)
        finally:
            root.after(40, pump)

    def update_status_text():
        ago = time.time() - bridge.last_contact
        st_page.config(text='화면(크롬): ' + ('연결됨' if ago < 8 else '연결 안 됨 (위 주소를 크롬으로 여세요)'))
        d = bridge.notifier.public()
        k = bridge.notifier.kakao
        kp = k.public() if k else None
        st_dc.config(text='디스코드: ' + ('웹훅 저장됨 %s' % d['hint'] if d['configured'] else '설정 안 됨 (화면의 "도우미" 칸에서 설정)')
                     + ('\n마지막 알림: ' + d['last'] if d['last'] else '')
                     + ('\n카카오톡 나에게: ' + ('켜짐' if k.ready() else '로그인됨, 꺼 둠' if kp['logged_in'] else '설정 안 됨')
                        + (' · 마지막: ' + kp['last'] if kp['last'] else '') if kp else ''))

    def on_close():
        if bridge.locked:
            st_close.config(text='경고 중에는 닫을 수 없습니다. 먼저 경고를 푸세요.', fg='#c8261b')
            root.bell()
            return
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', on_close)
    if bridge.locked and bridge.ui is not None:           # 지난번 경고가 안 풀린 채 도우미를 다시 켬
        ui_queue.put(('slam', dict(bridge.locked)))
    root.after(40, pump)
    root.mainloop()


# ======================================================================
#  시작
# ======================================================================
def find_browser():
    cands = []
    if sys.platform == 'win32':
        for env in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            base = os.environ.get(env)
            if base:
                cands += [os.path.join(base, 'Google', 'Chrome', 'Application', 'chrome.exe'),
                          os.path.join(base, 'Microsoft', 'Edge', 'Application', 'msedge.exe')]
    elif sys.platform == 'darwin':
        cands += ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge']
    else:
        cands += [shutil.which(n) or '' for n in ('google-chrome', 'chromium', 'chromium-browser', 'microsoft-edge')]
    return next((c for c in cands if c and os.path.exists(c)), None)


def open_page(url):
    exe = find_browser()
    try:
        if exe:
            # 탭이 아니라 전용 창으로 (다른 탭 사이에 묻혀 꺼지거나 절전 탭이 되는 일 방지)
            subprocess.Popen([exe, '--app=' + url, '--window-size=1100,900'])
            return
    except Exception:
        pass
    webbrowser.open(url)        # 크롬/엣지를 못 찾으면 기본 브라우저 (연결 기능은 크롬/엣지만 됨)


def main():
    ap = argparse.ArgumentParser(description='키보드 내려치기 감지 도우미')
    ap.add_argument('--port', type=int, default=int(os.environ.get('SLAM_PORT', '8765')))
    ap.add_argument('--no-browser', action='store_true', help='크롬을 자동으로 열지 않기')
    ap.add_argument('--no-window', action='store_true', help='항상 위 경고 창 없이 디스코드 알림만')
    args = ap.parse_args()

    use_window = not args.no_window
    if use_window:
        try:
            import tkinter  # noqa: F401
        except ImportError:
            use_window = False
            log('tkinter 가 없어 항상 위 경고 창은 끄고 디스코드 알림만 합니다. (python.org 의 파이썬을 설치하면 생깁니다)')

    windows_setup()
    ui_queue = queue.Queue() if use_window else None
    notifier = Notifier(Config(CONFIG_PATH), kakao=Kakao(KAKAO_PATH, args.port))
    bridge = Bridge(notifier, ui_queue, STATE_PATH)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(bridge, args.port))
    except OSError:
        why = '포트 %d 를 쓸 수 없습니다. 도우미가 이미 켜져 있을 수 있어요(작업 표시줄 확인). 아니면 --port 8766 처럼 다른 번호로 실행하세요.' % args.port
        log(why)
        if use_window:
            try:
                import tkinter as tk
                from tkinter import messagebox
                r = tk.Tk(); r.withdraw(); messagebox.showerror('키보드 내려치기 도우미', why); r.destroy()
            except Exception:
                pass
        sys.exit(1)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = 'http://127.0.0.1:%d/' % args.port
    log('도우미를 켰습니다. 화면 주소:', url)
    html = find_html()
    if not html:
        log('주의: slam_final.html 이 이 폴더에 없습니다 ->', HERE)
    elif html != HTML_PATH:
        log('화면 파일로 이것을 씁니다:', os.path.basename(html))
    if not args.no_browser:
        open_page(url)
    threading.Thread(target=watchdog, args=(bridge, url, open_page), daemon=True).start()

    try:
        if use_window:
            import tkinter as tk
            run_window(tk.Tk, bridge, ui_queue, url, open_page)
        else:
            log('끄려면 Ctrl+C 를 누르세요.')
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        log('도우미를 껐습니다.')


if __name__ == '__main__':
    main()
