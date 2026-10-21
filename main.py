"""HDU seat booking. All scheduling and booking dates use Asia/Shanghai."""
import argparse
import base64
import binascii
from contextlib import contextmanager
from collections import deque
from datetime import datetime, timedelta, timezone
from html import unescape
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit, parse_qsl

import requests
import yaml
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from seat_plan import custom_candidates, daily_targets

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / '.local'
SHANGHAI = timezone(timedelta(hours=8), 'Asia/Shanghai')
BASE_URL = 'https://hdu.huitu.zhishulib.com'
SSO_ORIGIN = 'https://sso.hdu.edu.cn'
SSO_LOGIN_ENTRY = BASE_URL + '/User/Index/hduCASLogin?forward=%2FSeat%2FIndex%2Findex'
BOOK_PATH = '/Seat/Index/bookSeats?LAB_JSON=1'
WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
ACTIVE_STATUSES = {'0', '1', '2', '8'}
LOG = logging.getLogger('hdu')


class BookingError(RuntimeError):
    pass


class AlreadyRunning(BookingError):
    pass


class NoSeats(BookingError):
    """A live map reports no free seats in the allowed fourth-floor pool."""


class CandidatesUnavailable(BookingError):
    """Candidates lost a race; another scheduled round may try a fresh map."""


def now():
    return datetime.now(SHANGHAI)


def log_time(timestamp):
    return datetime.fromtimestamp(timestamp, SHANGHAI).timetuple()


def read_yaml(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        return yaml.safe_load(stream)


def get_time_slots(config):
    if '时间段' in config:
        if '开始时间' in config or '持续小时数' in config:
            raise ValueError('时间段与开始时间/持续小时数不能同时配置')
        if not config['时间段']:
            raise ValueError('时间段不能为空')
        slots = [(x['开始时间'], x['持续小时数']) for x in config['时间段']]
    else:
        slots = [(config['开始时间'], config['持续小时数'])]
    for start, hours in slots:
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
               for x in (start, hours)):
            raise ValueError('开始时间和持续小时数必须是有限数字')
        if start < 0 or hours <= 0 or start + hours > 24:
            raise ValueError('时段必须位于同一天内且持续时间大于零')
        if start * 3600 % 1 or hours * 3600 % 1:
            raise ValueError('时间精度最多到秒')
    slots = sorted(slots)
    for (start, hours), (following, _) in zip(slots, slots[1:]):
        if following < start + hours:
            raise ValueError('时间段不能重合')
    return slots


def booking_token(data):
    # Match the official web client's ordered MD5 hex -> base64 signature.
    order = ['api_time', 'beginTime', 'duration', 'is_recommend', 'seatBookers[0]', 'seats[0]']
    canonical = 'post&' + BOOK_PATH + '&' + '&'.join(k + str(data[k]) for k in order)
    return base64.b64encode(hashlib.md5(canonical.encode()).hexdigest().encode()).decode()


def credentials():
    username, password = os.environ.get('SCHOOL_ID'), os.environ.get('PASSWORD')
    if username and password:
        return username.strip(), password
    path = LOCAL / 'credentials.xml'
    if os.name == 'nt' and path.exists():
        command = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
                   "$c=Import-Clixml -LiteralPath $env:HDU_CREDENTIAL_FILE; "
                   "@{SCHOOL_ID=$c.UserName;PASSWORD=$c.GetNetworkCredential().Password} | ConvertTo-Json -Compress")
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                env=dict(os.environ, HDU_CREDENTIAL_FILE=str(path)),
                                capture_output=True, timeout=15, check=False)
        if result.returncode == 0:
            value = json.loads(result.stdout.decode('utf-8-sig'))
            return value['SCHOOL_ID'], value['PASSWORD']
        raise BookingError('无法解密本机凭据，请重新运行 Save-Credential.ps1')
    raise BookingError('请设置 SCHOOL_ID、PASSWORD 环境变量或运行 Save-Credential.ps1')


def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def configured_seats(region, seat_config, custom):
    if region == '自定义':
        return custom_candidates(custom)
    else:
        names = [region] if region != '不限' else [
            name for name, cfg in seat_config.items() if cfg.get('type') == '自习室' and 'ranges' in cfg]
        ids = list(dict.fromkeys(seat for name in names
                   for start, end in seat_config[name]['ranges'] for seat in range(start, end + 1)))
        random.shuffle(ids)
    if not ids:
        raise BookingError('配置内没有候选座位')
    return [{'id': seat, 'number': str(seat), 'room': region} for seat in ids]


def page_field(html_text, element_id):
    """Read a hidden <p id="...">value</p> block rendered by the CAS page."""
    match = re.search(r'<p\s+id="' + re.escape(element_id) + r'"[^>]*>(.*?)</p>',
                      html_text, re.S)
    return unescape(match.group(1)).strip() if match else ''


def encrypt_password(password, key):
    """Reproduce the CAS client: base64(AES-128-ECB(PKCS7(password), base64key))."""
    try:
        raw = base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError):
        raise BookingError('统一身份认证加密参数格式异常') from None
    if len(raw) not in (16, 24, 32):
        raise BookingError('统一身份认证加密参数长度异常')
    return base64.b64encode(AES.new(raw, AES.MODE_ECB).encrypt(
        pad(password.encode('utf-8'), 16))).decode()


def login_failure_reason(html_text, username, password):
    """Explain a rejected CAS login without echoing any secret."""
    for pattern in (r'id="login-error"[^>]*>(.*?)</', r'class="[^"]*error[^"]*"[^>]*>(.*?)</',
                    r'id="msg"[^>]*>(.*?)</'):
        match = re.search(pattern, html_text, re.S)
        if match:
            text = re.sub(r'<[^>]+>', ' ', match.group(1)).strip()
            for secret in (username, password):
                if secret:
                    text = text.replace(secret, '[已隐藏]')
            if text:
                return text[:150]
    if 'captcha' in html_text.lower():
        return '需要验证码，无法无人值守登录'
    return '用户名或密码错误，或账号被限制（页面未给出原因）'


def write_summary(text):
    """Append a human-readable result block to the GitHub Actions run page."""
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    try:
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(text + '\n')
    except OSError:
        LOG.warning('无法写入 GitHub 步骤摘要')


def wait_until(target, wall=time.time, monotonic=time.perf_counter, sleep=time.sleep):
    deadline = monotonic() + target.timestamp() - wall()
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            # Recheck the wall clock too: the two initial clock reads can differ.
            if wall() >= target.timestamp():
                return
            deadline = monotonic() + target.timestamp() - wall()
            continue
        sleep(min(remaining / 2, 0.01) if remaining < 0.1 else min(remaining - 0.05, 1.0))


@contextmanager
def single_instance():
    LOCAL.mkdir(exist_ok=True)
    lock = (LOCAL / 'run.lock').open('a+b')
    try:
        # Windows byte locks also prohibit reads by the watchdog process.
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise AlreadyRunning('已有预约进程在运行，备用任务正常跳过') from None
    try:
        yield
    finally:
        lock.close()


class SeatAutoBooker:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/json, text/plain, */*',
            'User-Agent': 'Mozilla/5.0 Chrome/130.0.0.0 Safari/537.36',
            'Origin': BASE_URL, 'Referer': BASE_URL + '/Space/Category/list',
        })
        self.uid = None
        self.categories = {}

    def close(self):
        self.session.close()

    def request(self, method, path, **kwargs):
        if not path.startswith('/') or path.startswith('//'):
            raise BookingError('API 路径无效')
        started = time.perf_counter()
        kwargs.setdefault('timeout', (5, 15))
        deadline = getattr(self, 'round_deadline', None)
        if deadline and any(name in path for name in ('/bookSeats', '/searchSeats')):
            remaining = (deadline - now()).total_seconds()
            if remaining <= 0:
                raise BookingError('本轮时间预算已用完，停止新增预约请求')
            kwargs['timeout'] = (min(2, remaining / 2), min(15, remaining / 2))
        response = self.session.request(method, BASE_URL + path, **kwargs)
        elapsed = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        if urlsplit(response.url).hostname != urlsplit(BASE_URL).hostname:
            raise BookingError('会话失效，API 已跳转到登录页')
        try:
            payload = response.json()
        except ValueError:
            raise BookingError('API 未返回 JSON，可能是会话失效或服务异常') from None
        if not isinstance(payload, dict):
            raise BookingError('API 响应结构不正确')
        LOG.info('%s %s 耗时 %.1f ms', method, path.split('?')[0], elapsed)
        return payload

    def cas_login(self, username, password):
        """Authenticate against the school CAS without launching a browser."""
        LOG.info('开始学校统一身份认证')
        response = self.session.get(SSO_LOGIN_ENTRY, timeout=(5, 20))
        host = urlsplit(response.url).hostname
        if host != urlsplit(SSO_ORIGIN).hostname:
            raise BookingError('未进入学校统一身份认证页面：' + str(host))
        html_text = response.text
        key = page_field(html_text, 'login-croypto')
        flowkey = page_field(html_text, 'login-page-flowkey')
        login_type = page_field(html_text, 'current-login-type') or 'UsernamePassword'
        if not key or not flowkey:
            raise BookingError('未获取到统一身份认证登录参数，页面可能已改版')
        data = {
            'username': username, 'type': login_type, '_eventId': 'submit',
            'geolocation': '', 'execution': flowkey, 'captcha_code': '',
            'croypto': key, 'password': encrypt_password(password, key),
            'captcha_payload': '',
        }
        response = self.session.post(
            SSO_ORIGIN + '/login', data=data, timeout=(5, 20), allow_redirects=True,
            headers={'Content-Type': 'application/x-www-form-urlencoded',
                     'Origin': SSO_ORIGIN, 'Referer': response.url})
        if urlsplit(response.url).hostname != urlsplit(BASE_URL).hostname:
            raise BookingError('统一身份认证被拒绝：' + login_failure_reason(response.text,
                                                                    username, password))
        LOG.info('已通过统一身份认证并返回图书馆')

    def login(self, load_categories=True):
        username, password = credentials()
        error = None
        for attempt in range(3):
            try:
                self.cas_login(username, password)
                self.get_user_info()
                error = None
                break
            except requests.RequestException as failure:
                error = BookingError('统一身份认证网络异常：' + type(failure).__name__)
            except BookingError as failure:
                error = failure
                if any(word in str(failure) for word in
                       ('被拒绝', '密码', '账号', '锁定', '禁用', '验证码', '限制')):
                    raise
            if attempt < 2:
                LOG.warning('统一身份认证第 %d 次失败：%s；1 秒后重试', attempt + 1, error)
                time.sleep(1.0 + attempt)
        if error is not None:
            raise error
        if load_categories:
            self.load_categories()
        LOG.info('登录验证成功，已复用 HTTP 会话')

    def get_user_info(self, timeout=None):
        payload = self.request('GET', '/Seat/Index/index?LAB_JSON=1',
                               **({'timeout': timeout} if timeout else {}))
        data = payload.get('DATA', {})
        if payload.get('CODE') != 'ok' or not data.get('is_login') or not data.get('uid'):
            raise BookingError('登录状态验证失败')
        self.uid = int(data['uid'])

    def load_categories(self):
        payload = self.request('GET', '/Space/Category/list?LAB_JSON=1')
        self.categories = {item['name']: item['link']['url'] for item in walk(payload)
                           if item.get('ui_type') == 'ht.space.SpaceReservationListItem'
                           and not item.get('disabled') and item.get('link', {}).get('url')}
        if not self.categories:
            raise BookingError('未找到可预约区域分类')

    def bookings(self):
        items, seen = [], set()
        path = '/Seat/Index/myBookingList?LAB_JSON=1'
        for _ in range(30):
            if path in seen:
                raise BookingError('预约记录分页循环')
            seen.add(path)
            payload = self.request('GET', path)
            for item in walk(payload):
                if item.get('ui_type') == 'ht.Seat.OrderListItem':
                    items.append(item)
            path = payload.get('content', {}).get('defaultNextUrl')
            if not path:
                return items
        raise BookingError('预约记录超过分页上限，无法可靠确认重复预约')

    @staticmethod
    def covered_booking(items, begin, duration):
        for item in items:
            if str(item.get('status')) not in ACTIVE_STATUSES:
                continue
            existing = int(item['time'])
            end = existing + int(item['duration'])
            if existing <= begin and end >= begin + duration:
                return item
            if existing < begin + duration and end > begin:
                raise BookingError('与已有预约部分重叠，请先调整配置或预约')
        return None

    def available_seats(self, region, begin, duration, seat_config, custom):
        if region == '自定义':
            ordered = custom_candidates(custom)
            allowed = [seat['id'] for seat in ordered]
            if not allowed:
                raise BookingError('自定义座位列表为空')
            # Restrict custom IDs to their configured category; retain a fallback
            # for IDs unknown to the static room mapping.
            known = {seat for seat in allowed for cfg in seat_config.values()
                     for first, last in cfg.get('ranges', []) if first <= seat <= last}
            types = {cfg['type'] for seat in allowed for cfg in seat_config.values()
                     for first, last in cfg.get('ranges', []) if first <= seat <= last}
            category_names = ([name for name in self.categories if name in types]
                              if known == set(allowed) else list(self.categories))
            room_id = 1558 if isinstance(custom, dict) else None
        elif region == '不限':
            allowed, room_id = None, None
            category_names = [x for x in ('自习室', '阅览室', '生活区') if x in self.categories]
        else:
            if region not in seat_config:
                raise BookingError('未知区域：' + region)
            cfg = seat_config[region]
            category_names, room_id, allowed = [cfg['type']], int(cfg['room_id']), None
        seats = {}
        seen_allowed_ids = set()
        for category in category_names:
            if category not in self.categories:
                continue
            category_url = urlsplit(self.categories[category])
            data = dict(parse_qsl(category_url.query))
            data.update({'beginTime': begin, 'duration': duration, 'num': 1})
            payload = self.request('POST', category_url.path + '?LAB_JSON=1', data=data)
            if payload.get('CODE') not in (None, 'ok'):
                raise BookingError(str(payload.get('MESSAGE') or '座位地图查询失败'))
            if payload.get('ui_type') == 'com.Message':
                raise BookingError(payload.get('MESSAGE', '无法查询座位'))
            if payload.get('IsImgCode'):
                raise BookingError('该区域需要人工验证码，无法无人值守预约')
            for item in walk(payload):
                seat_map = item.get('seatMap', {})
                if not isinstance(seat_map, dict):
                    continue
                info = seat_map.get('info', {})
                if not info or (room_id and int(info['id']) != room_id):
                    continue
                for poi in seat_map.get('POIs', []):
                    seat_id = int(poi['id'])
                    if allowed is not None and seat_id in allowed:
                        seen_allowed_ids.add(seat_id)
                    free = str(poi.get('state')) == '0' or (str(poi.get('state')) == '2' and poi.get('recommend'))
                    if free and (allowed is None or seat_id in allowed):
                        seats[seat_id] = {'id': seat_id, 'number': poi['title'], 'room': info['title']}
            if seats and region == '不限':
                break
        if allowed:
            if isinstance(custom, dict) and not seen_allowed_ids:
                raise BookingError('未获取到有效的四楼座位地图，不能判定满座')
            metadata = {seat['id']: seat for seat in ordered}
            return [dict(metadata[x], **seats[x]) for x in allowed if x in seats]
        result = list(seats.values())
        random.shuffle(result)
        return result

    def verify(self, booking_id, begin=None, duration=None, seat_id=None):
        payload = self.request('GET', f'/Seat/Index/bookingInfo?bookingId={int(booking_id)}&LAB_JSON=1')
        booking = payload.get('data', {}).get('booking', {})
        if (str(booking.get('id')) != str(booking_id)
                or str(booking.get('status')) not in ACTIVE_STATUSES
                or int(booking.get('user', {}).get('id', 0)) != self.uid):
            raise BookingError('订单详情验证失败')
        if begin is not None and int(booking['begin_time']) != begin:
            raise BookingError('订单开始时间不符')
        if duration is not None and int(booking['duration']) != duration:
            raise BookingError('订单持续时间不符')
        if seat_id is not None and int(booking.get('seat', {}).get('id', 0)) != seat_id:
            raise BookingError('订单座位不符')
        return {'booking_id': str(booking_id), 'room': booking['space']['title'],
                'seat': booking['seat']['title'], 'seat_id': booking['seat']['id'],
                'begin': datetime.fromtimestamp(int(booking['begin_time']), SHANGHAI).isoformat(),
                'duration': int(booking['duration']), 'status': str(booking['status'])}

    def book_slot(self, begin, duration, seats, opening_deadline=None):
        attempts = min(int(self.config.get('max-retry', 5)), len(seats))
        if attempts <= 0:
            raise BookingError('没有符合条件的空闲座位')
        candidates = deque(seats[:attempts])
        window_retries = 0
        while candidates:
            deadline = getattr(self, 'round_deadline', None)
            if deadline and now() >= deadline:
                raise BookingError('本轮时间预算已用完，等待下一轮')
            seat = candidates.popleft()
            data = {'api_time': int(time.time()), 'beginTime': begin, 'duration': duration,
                    'is_recommend': 0, 'seatBookers[0]': self.uid, 'seats[0]': seat['id']}
            try:
                payload = self.request('POST', BOOK_PATH, data=data, headers={'Api-Token': booking_token(data)})
            except requests.RequestException:
                existing = self.covered_booking(self.bookings(), begin, duration)
                if existing:
                    return self.verify(existing['id'])
                raise BookingError('预约网络异常，查单未发现订单；停止提交，等待下一轮补跑') from None
            result = payload.get('DATA', {})
            if payload.get('CODE') == 'ok' and result.get('result') == 'success' and result.get('bookingId'):
                verified = self.verify(result['bookingId'], begin, duration, seat['id'])
                verified['submission_confirmed'] = True
                LOG.info('新预约提交已获服务器确认并通过查单：订单 %s', result['bookingId'])
                return verified
            message = str(result.get('msg') or payload.get('MESSAGE') or '预约失败')
            if any(key in message for key in ('频繁', '人数过多', '稍后')):
                raise BookingError('服务限流，等待下一轮补跑：' + message)
            if (opening_deadline and now() < opening_deadline and window_retries < 3
                    and any(word in message for word in ('超出可预约座位时间范围', '未开放', '未到预约'))):
                delay = min(0.4 * (2 ** window_retries), (opening_deadline - now()).total_seconds())
                if delay > 0:
                    LOG.warning('开放边界拒绝，%.1f 秒后重试同一座位：%s', delay, message)
                    time.sleep(delay)
                    candidates.appendleft(seat)
                    window_retries += 1
                    continue
            if any(key in message for key in ('已有预约', '重复预约', '已存在预约', '请先取消')):
                existing = self.covered_booking(self.bookings(), begin, duration)
                if existing:
                    return self.verify(existing['id'])
                raise BookingError('已有其他预约，请核对预约记录：' + message)
            if any(key in message for key in ('已被', '占用', '不可用', '不存在', '冲突', '已预约')):
                LOG.warning('座位 %s 不可约：%s', seat['number'], message)
                time.sleep(0.3)
                continue
            raise BookingError('预约被拒绝：' + message)
        raise CandidatesUnavailable('候选座位均已不可用，等待下一轮补跑')

    def book_fourth_floor(self, begin, duration, seats, custom, seat_config, opening_deadline=None):
        rejected = set()
        # At release, immediately try one random top-tier seat. A collision
        # triggers one live map, rather than hundreds of speculative POSTs.
        if opening_deadline and now() < opening_deadline:
            try:
                return self.book_slot(begin, duration, seats[:1], opening_deadline)
            except CandidatesUnavailable:
                rejected.add(seats[0]['id'])
        if not self.categories:
            self.load_categories()
        available = self.available_seats('自定义', begin, duration, seat_config, custom)
        if not available:
            raise NoSeats('四楼该时段没有可用座位，结束今日预约')
        available = [seat for seat in available if seat['id'] not in rejected]
        if not available:
            raise CandidatesUnavailable('地图候选刚被占用，等待下一轮刷新')
        return self.book_slot(begin, duration, available)


def parse_args():
    parser = argparse.ArgumentParser(description='杭电图书馆预约，时区固定为北京时间')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='只验证登录和座位，不提交')
    mode.add_argument('--test-booking', action='store_true', help='忽略星期开关，真实预约一个测试时段')
    mode.add_argument('--daily', action='store_true', help='复用会话执行配置的五轮预约')
    parser.add_argument('--date', type=lambda s: datetime.strptime(s, '%Y-%m-%d').date())
    parser.add_argument('--start', type=float, default=7)
    parser.add_argument('--hours', type=float, default=1)
    parser.add_argument('--region', default='自定义')
    parser.add_argument('--at', choices=['20:00', '20:02'], help='提前登录，等到今天的目标时刻提交')
    parser.add_argument('--backup-at', choices=['20:02'], help='失败后复用当前会话等待补跑')
    return parser.parse_args()


def reserve_day(client, args, day, slots, target_date, target_time, seat_config, user_config):
    existing = client.bookings()
    prepared = []
    midnight = datetime.combine(target_date, datetime.min.time(), SHANGHAI)
    for start, hours in slots:
        begin = int((midnight + timedelta(hours=start)).timestamp())
        duration = int(hours * 3600)
        duplicate = client.covered_booking(existing, begin, duration)
        if duplicate:
            LOG.info('时段已有预约，核验订单 %s', duplicate['id'])
            verified = client.verify(duplicate['id'])
            if (isinstance(user_config.get('自定义'), dict)
                    and int(verified.get('seat_id', 0)) not in
                    {seat['id'] for seat in custom_candidates(user_config['自定义'])}):
                raise BookingError('已有覆盖订单不在当前四楼范围，请先在网页处理')
            prepared.append((begin, duration, [], verified))
            continue
        if (target_time or isinstance(user_config.get('自定义'), dict)) and not args.check:
            # Candidate IDs are sufficient for submission; the server decides
            # availability. Avoid slow maps and stale pre-release snapshots.
            seats = configured_seats(day['name'], seat_config, user_config.get('自定义', []))
        else:
            seats = client.available_seats(day['name'], begin, duration, seat_config, user_config.get('自定义', []))
        LOG.info('%s %g:00 起 %g 小时，%s 候选座位 %d 个', target_date, start, hours, day['name'], len(seats))
        prepared.append((begin, duration, seats, None))
    if args.check:
        LOG.info('只读检查完成，未提交预约')
        return 0
    if target_time and any(duplicate is None for _, _, _, duplicate in prepared):
        # GitHub's scheduler can start the job minutes late. A late start still
        # books: the server, not the local clock, decides availability.
        late = now() > target_time + timedelta(seconds=30)
        if late:
            LOG.warning('本轮启动晚于目标时间 %.1f 秒，立即提交而不是等待',
                        (now() - target_time).total_seconds())
        else:
            LOG.info('登录、查单及候选座位准备已完成；等待 %s', target_time.isoformat())
            wait_until(max(target_time - timedelta(seconds=10), now()))
            try:
                client.get_user_info(timeout=(2, 3))
            except requests.RequestException as error:
                LOG.warning('连接预热失败（%s），仍按目标时间尝试提交', type(error).__name__)
            wait_until(target_time)
            LOG.info('触发偏差 %.3f ms', (now() - target_time).total_seconds() * 1000)
    results = []
    for begin, duration, seats, duplicate in prepared:
        opening_deadline = (target_time + timedelta(seconds=4)
                            if target_time and target_time.strftime('%H:%M:%S') == '20:00:00' else None)
        custom = user_config.get('自定义', [])
        if duplicate:
            result = duplicate
        elif day['name'] == '自定义' and isinstance(custom, dict):
            result = client.book_fourth_floor(begin, duration, seats, custom, seat_config, opening_deadline)
        else:
            result = client.book_slot(begin, duration, seats, opening_deadline=opening_deadline)
        result['already_existed'] = duplicate is not None
        result.setdefault('submission_confirmed', False)
        result['trigger_event'] = os.environ.get('GITHUB_EVENT_NAME', 'local')
        result['github_run_id'] = os.environ.get('GITHUB_RUN_ID', '')
        LOG.info('预约已核实：%s %s号座，%s，订单 %s', result['room'], result['seat'], result['begin'], result['booking_id'])
        minutes = int(result.get('duration', 0)) // 60
        write_summary(
            '### 预约成功\n\n'
            f"- 座位：{result.get('room', '?')} {result.get('seat', '?')}号\n"
            f"- 开始：{result.get('begin', '?')}\n"
            f"- 时长：{minutes} 分钟\n"
            f"- 订单：{result.get('booking_id', '?')}\n"
            f"- 触发：{result['trigger_event']}；运行 {result['github_run_id'] or '本机'}\n"
            f"- 原本已有预约：{'是' if result['already_existed'] else '否'}\n")
        results.append(result)
        LOCAL.mkdir(exist_ok=True)
        report = LOCAL / ('booking-' + now().strftime('%Y%m%d-%H%M%S-%f') + '.json')
        report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


def login_for_target(client, target_time, load_categories):
    for attempt in range(3):
        try:
            client.login(load_categories=load_categories)
            return
        except requests.RequestException:
            if attempt == 2:
                raise
        except BookingError as error:
            if '网络异常' not in str(error) or attempt == 2:
                raise
        LOG.warning('统一身份认证暂时失败，%d 秒后重试', 2)
        time.sleep(2)


def run_daily(client, args, day, slots, target_date, seat_config, user_config):
    targets = daily_targets(now(), user_config['自动预约'])
    cutoff = now().replace(hour=20, minute=15, second=0, microsecond=0)
    LOCAL.mkdir(exist_ok=True)
    state_path = LOCAL / ('daily-state-' + now().strftime('%Y-%m-%d') + '.json')
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'attempted': []}
    def checkpoint():
        temporary = state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, state_path)
    if state.get('finished'):
        LOG.info('今日计划已结束：%s', state['finished'])
        return 0
    logged_in = False
    for index, target in enumerate(targets, 1):
        key = target.isoformat()
        if key in state['attempted']:
            LOG.info('第 %d/5 轮已执行过，避免进程重启后重复运行', index)
            continue
        if index > 1 and now() < target - timedelta(seconds=20):
            wait_until(target - timedelta(seconds=20))
        deadline = min(target + timedelta(seconds=20), cutoff)
        if now() >= deadline:
            # GitHub 的 cron 可能比计划晚几十分钟才把 Job 跑起来，上一轮也可能
            # 用完预算。只要还在当天的报名窗口内，就立即补跑而不是跳过整天。
            if now() >= cutoff:
                LOG.warning('第 %d/5 轮已过期，跳过 %s', index, key)
                continue
            LOG.warning('第 %d/5 轮已过计划时刻 %s，立即补跑', index, key)
            target = now()
            deadline = min(target + timedelta(seconds=15), cutoff)
        state['attempted'].append(key)
        checkpoint()
        client.round_deadline = deadline
        LOG.info('第 %d/5 轮，目标 %s；复用会话=%s', index, target.isoformat(), logged_in)
        try:
            if not logged_in:
                login_for_target(client, target, load_categories=True)
                logged_in = True
            if now() >= deadline:
                raise BookingError('本轮准备超时，留待下一轮')
            result = reserve_day(client, args, day, slots, target_date, target, seat_config, user_config)
            state['finished'] = '预约已确认或已有覆盖订单'
            checkpoint()
            return result
        except NoSeats as error:
            if index == 1 and now() < cutoff:
                # 首轮地图可能在开放窗口生效前被查询，不能据此结束整天。
                LOG.warning('首轮地图显示无位，可能是窗口尚未生效，继续下一轮：%s', error)
                continue
            state['finished'] = '四楼无可用座位'
            checkpoint()
            LOG.info('%s', error)
            return 0
        except (BookingError, requests.RequestException) as error:
            LOG.warning('第 %d/5 轮未成功：%s', index, error)
            if any(word in str(error) for word in (
                    '统一身份认证失败', '统一身份认证被拒绝', '加密参数', '页面可能已改版',
                    '验证码', '无法解密', '请设置 SCHOOL_ID', '已有覆盖订单不在',
                    '与已有预约部分重叠', '已有其他预约')):
                raise
            if '会话失效' in str(error) or '登录状态验证失败' in str(error):
                logged_in = False
    LOG.error('今日五轮已结束，没有确认新预约')
    return 1


def run(args):
    user_config = read_yaml(ROOT / 'user_config.yml')
    basic = read_yaml(ROOT / 'config/basic_config.yml')['SeatAutoBooker']
    if basic.get('cloud-only', False) and os.environ.get('GITHUB_ACTIONS') != 'true':
        LOG.info('本机执行已停用：当前仅允许 GitHub Actions 云端运行，未登录、未提交预约')
        return 0
    LOG.info('执行环境：%s', 'GitHub Actions 云端' if os.environ.get('GITHUB_ACTIONS') == 'true' else '本机')
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        LOG.info('GitHub 触发事件=%s；运行编号=%s', os.environ.get('GITHUB_EVENT_NAME', 'unknown'),
                 os.environ.get('GITHUB_RUN_ID', 'unknown'))
    seat_config = read_yaml(ROOT / 'config/seat_config.yml')
    target_date = args.date or (now() + timedelta(days=2)).date()
    target_time = None
    if args.check or args.test_booking:
        day = {'name': args.region, '开始时间': args.start, '持续小时数': args.hours}
    else:
        weekday = WEEKDAYS[target_date.weekday()]
        day = user_config[weekday]
        if day.get('启用') is not True:
            LOG.info('%s %s 预约未启用；请在 user_config.yml 设置日常规则', target_date, weekday)
            return 0
    if args.at:
        hour, minute = map(int, args.at.split(':'))
        target_time = now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now() > target_time + timedelta(seconds=30):
            raise BookingError('定时目标已过期超过 30 秒，本轮退出')
    slots = get_time_slots(day)
    daily = getattr(args, 'daily', False) is True
    if daily and (args.at or isinstance(getattr(args, 'backup_at', None), str)):
        raise BookingError('--daily 不能与 --at/--backup-at 同用')
    custom = user_config.get('自定义', [])
    if daily and (day['name'] != '自定义' or not isinstance(custom, dict)):
        raise BookingError('每日五轮只允许四楼分级区间配置')
    if isinstance(custom, dict):
        custom_candidates(custom)  # Validate the full pool before any login.
        if not args.check and day['name'] != '自定义':
            raise BookingError('当前配置只允许预约四楼分级自定义座位')
    client = SeatAutoBooker(basic)
    try:
        if daily:
            return run_daily(client, args, day, slots, target_date, seat_config, user_config)
        login_for_target(client, target_time,
                         load_categories=target_time is None or args.check or isinstance(custom, dict))
        targets = [target_time]
        backup = getattr(args, 'backup_at', None)
        if isinstance(backup, str):
            if args.at != '20:00' or args.check:
                raise BookingError('--backup-at 仅用于 --at 20:00 的真实预约')
            targets.append(target_time.replace(hour=20, minute=2))
        for index, target in enumerate(targets):
            try:
                return reserve_day(client, args, day, slots, target_date, target, seat_config, user_config)
            except NoSeats as error:
                LOG.info('%s', error)
                return 0
            except (BookingError, requests.RequestException) as error:
                if index == len(targets) - 1:
                    raise
                LOG.warning('本轮失败：%s；保留登录会话，等待 20:02 查单后补跑', error)
                wait_until(targets[index + 1] - timedelta(seconds=20))
    finally:
        client.close()


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    (ROOT / 'logs').mkdir(exist_ok=True)
    logging.Formatter.converter = staticmethod(log_time)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s.%(msecs)03d %(levelname)s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S', handlers=[logging.StreamHandler(),
                        logging.FileHandler(ROOT / 'logs' / (now().strftime('%Y-%m-%d') + '.log'), encoding='utf-8')])
    args = parse_args()
    try:
        with single_instance():
            return run(args)
    except AlreadyRunning as error:
        LOG.info('%s', error)
        return 0
    except (BookingError, ValueError, KeyError, requests.RequestException) as error:
        LOG.error('%s', error)
        return 1
    except Exception as error:
        LOG.error('运行异常：%s（不输出可能包含凭据的浏览器堆栈）', type(error).__name__)
        return 1


if __name__ == '__main__':
    sys.exit(main())
