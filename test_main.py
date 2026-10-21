"""Offline regressions: no credentials, browsers, or real bookings."""
from datetime import datetime, timedelta
import base64
import json
import logging
import subprocess
import sys
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import main
from scripts.verify_native_booking import verify_native_booking


class BookingTests(unittest.TestCase):
    def test_second_process_skips_locked_booking_and_can_run_after_release(self):
        code = """import os
from pathlib import Path
import main
main.LOCAL = Path(os.environ['HDU_TEST_LOCK_DIR'])
try:
    with main.single_instance():
        print('acquired')
except main.AlreadyRunning:
    print('busy')
"""
        with tempfile.TemporaryDirectory() as folder, patch('main.LOCAL', Path(folder)):
            def check():
                return subprocess.run([sys.executable, '-c', code], cwd=main.ROOT,
                    env=dict(os.environ, HDU_TEST_LOCK_DIR=folder),
                    capture_output=True, text=True, check=True, timeout=15).stdout.strip()
            with main.single_instance():
                self.assertEqual(check(), 'busy')
            self.assertEqual(check(), 'acquired')
    def test_utf8_config_independent_of_system_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.yml'
            path.write_text('周三:\n  启用: false\n', encoding='utf-8-sig')
            self.assertFalse(main.read_yaml(path)['周三']['启用'])

    def test_slots_reject_ambiguous_invalid_overlapping_values(self):
        cases = [
            {'时间段': [], '开始时间': 7}, {'时间段': []},
            {'开始时间': 7, '持续小时数': 0}, {'开始时间': 23, '持续小时数': 2},
            {'开始时间': float('nan'), '持续小时数': 1},
            {'开始时间': True, '持续小时数': 1},
            {'时间段': [{'开始时间': 7, '持续小时数': 2}, {'开始时间': 8, '持续小时数': 1}]},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                main.get_time_slots(case)
        self.assertEqual(main.get_time_slots({'时间段': [
            {'开始时间': 8, '持续小时数': 1}, {'开始时间': 7, '持续小时数': 1}]}), [(7, 1), (8, 1)])

    def test_timestamp_uses_china_timezone(self):
        value = datetime(2026, 10, 21, 7, tzinfo=main.SHANGHAI)
        self.assertEqual(int(value.timestamp()), 1792537200)

    def test_log_timestamp_uses_record_time_across_second_boundary(self):
        record = logging.LogRecord('hdu', logging.INFO, '', 0, 'test', (), None)
        record.created = datetime(2026, 10, 20, 19, 59, 59, tzinfo=main.SHANGHAI).timestamp() + 0.999
        with patch.object(logging.Formatter, 'converter', staticmethod(main.log_time)):
            self.assertEqual(logging.Formatter().formatTime(record, '%H:%M:%S'), '19:59:59')

    def test_token_matches_javascript_client(self):
        # Independent JS fixture, captured/calculated from the official canonical string.
        data = {'api_time': 1792413500, 'beginTime': 1792537200, 'duration': 3600,
                'is_recommend': 0, 'seatBookers[0]': 338976, 'seats[0]': 61355}
        self.assertEqual(main.booking_token(data), 'ZWVhNWYxOTAwOTI4M2QzY2UyZWQ1MmQxZDAzNzQyNzQ=')

    def test_cancelled_booking_does_not_prevent_retry(self):
        cancelled = {'id': 1, 'time': 100, 'duration': 100, 'status': '4'}
        self.assertIsNone(main.SeatAutoBooker.covered_booking([cancelled], 100, 100))
        active = dict(cancelled, status='0')
        self.assertEqual(main.SeatAutoBooker.covered_booking([active], 110, 50)['id'], 1)
        with self.assertRaises(main.BookingError):
            main.SeatAutoBooker.covered_booking([active], 150, 100)

    def client(self):
        client = main.SeatAutoBooker({'max-retry': 5})
        self.addCleanup(client.close)
        client.uid = 123
        return client

    def test_http_ok_with_business_failure_is_failure(self):
        client = self.client()
        client.request = Mock(return_value={'CODE': 'ok', 'MESSAGE': '请求成功',
                                           'DATA': {'result': 'failure', 'msg': '预约时长不合法'}})
        with self.assertRaisesRegex(main.BookingError, '时长不合法'):
            client.book_slot(100, 100, [{'id': 1, 'number': '1'}])

    def test_rate_limit_stops_without_hammering(self):
        client = self.client()
        client.request = Mock(return_value={'CODE': 'error', 'MESSAGE': '操作频繁'})
        with self.assertRaisesRegex(main.BookingError, '限流'):
            client.book_slot(100, 100, [{'id': x, 'number': str(x)} for x in range(5)])
        self.assertEqual(client.request.call_count, 1)

    def test_taken_seat_switches_and_success_is_verified(self):
        client = self.client()
        client.request = Mock(side_effect=[{'CODE': 'error', 'MESSAGE': '座位已被占用'},
            {'CODE': 'ok', 'DATA': {'result': 'success', 'bookingId': '9'}}])
        client.verify = Mock(return_value={'booking_id': '9'})
        with patch('main.time.sleep'):
            self.assertEqual(client.book_slot(100, 200, [{'id': 1, 'number': '1'},
                             {'id': 2, 'number': '2'}]), {'booking_id': '9', 'submission_confirmed': True})
        client.verify.assert_called_once_with('9', 100, 200, 2)

    def test_timeout_queries_before_any_second_post(self):
        client = self.client()
        client.request = Mock(side_effect=requests.Timeout())
        client.bookings = Mock(return_value=[{'id': 9, 'time': 100, 'duration': 200, 'status': '0'}])
        client.verify = Mock(return_value={'booking_id': '9'})
        self.assertEqual(client.book_slot(100, 200, [{'id': 1}]), {'booking_id': '9'})
        self.assertEqual(client.request.call_count, 1)

    def test_native_acceptance_rejects_manual_duplicate_and_uncertain_orders(self):
        confirmed = {'github_run_id': '77', 'trigger_event': 'schedule',
                     'submission_confirmed': True, 'already_existed': False, 'seat_id': '62599'}
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / 'booking-test.json'
            report.write_text(json.dumps([confirmed]), encoding='utf-8')
            self.assertEqual(verify_native_booking(folder, 'schedule', '77'), [confirmed])
            for event, run_id in [('workflow_dispatch', '77'), ('push', '77'), ('schedule', '78')]:
                with self.subTest(event=event, run_id=run_id), self.assertRaises(ValueError):
                    verify_native_booking(folder, event, run_id)
            for changes in [{'already_existed': True}, {'submission_confirmed': False},
                            {'seat_id': 62564}, {'seat_id': 62897}, {'trigger_event': 'workflow_dispatch'}]:
                report.write_text(json.dumps([dict(confirmed, **changes)]), encoding='utf-8')
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    verify_native_booking(folder, 'schedule', '77')

    def test_search_uses_form_category_and_ignores_non_map_blocks(self):
        client = self.client()
        client.categories = {'自习室': '/Seat/Index/searchSeats?space_category%5Bcategory_id%5D=591&space_category%5Bcontent_id%5D=3'}
        client.request = Mock(return_value={'content': [{'seatMap': ''}, {'seatMap': {
            'info': {'id': '1524', 'title': '二楼西'}, 'POIs': [
                {'id': '1', 'title': '1', 'state': 0}, {'id': '2', 'title': '2', 'state': '1'},
                {'id': '3', 'title': '3', 'state': 2, 'recommend': True}]}}]})
        seats = client.available_seats('二楼西', 100, 200, {'二楼西': {'type': '自习室', 'room_id': 1524}}, [])
        self.assertEqual({seat['id'] for seat in seats}, {1, 3})
        self.assertEqual(client.request.call_args.kwargs['data']['space_category[content_id]'], '3')
        self.assertEqual(client.request.call_args.args[1], '/Seat/Index/searchSeats?LAB_JSON=1')

    def test_verification_rejects_wrong_owner_or_time(self):
        client = self.client()
        client.request = Mock(return_value={'data': {'booking': {'id': '9', 'status': '0',
                                       'user': {'id': '999'}, 'begin_time': '100'}}})
        with self.assertRaises(main.BookingError):
            client.verify(9, 100)

    def test_monotonic_wait_cannot_submit_early(self):
        elapsed = [0.0]
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            elapsed[0] += max(seconds, 0.00001)
        target = datetime.fromtimestamp(1001, main.SHANGHAI)
        main.wait_until(target, wall=lambda: 1000 + elapsed[0], monotonic=lambda: elapsed[0], sleep=sleep)
        self.assertGreaterEqual(elapsed[0], 1)
        self.assertTrue(all(0 < value <= 1 for value in sleeps))

    def test_cloud_only_blocks_local_before_login(self):
        configs = [{}, {'SeatAutoBooker': {'cloud-only': True}}]
        with patch('main.read_yaml', side_effect=configs), patch('main.os.environ', {}), \
                patch('main.SeatAutoBooker') as client, self.assertLogs('hdu', level='INFO') as logs:
            self.assertEqual(main.run(Mock()), 0)
            client.assert_not_called()
        self.assertTrue(any('本机执行已停用' in line for line in logs.output))

    def test_cloud_only_allows_actions_to_read_day_configuration(self):
        args = Mock(date=datetime(2026, 10, 24).date(), at=None, check=False, test_booking=False)
        configs = [{'周六': {'启用': False}}, {'SeatAutoBooker': {'cloud-only': True}}, {}]
        with patch('main.read_yaml', side_effect=configs), patch('main.os.environ', {'GITHUB_ACTIONS': 'true'}), \
                self.assertLogs('hdu', level='INFO') as logs:
            self.assertEqual(main.run(args), 0)
        self.assertTrue(any('执行环境：GitHub Actions 云端' in line for line in logs.output))
        self.assertTrue(any('周六 预约未启用' in line for line in logs.output))

    def test_disabled_day_never_logs_in(self):
        args = Mock(date=datetime(2026, 10, 21).date(), at='20:00', check=False, test_booking=False)
        configs = [{'周三': {'启用': False}}, {'SeatAutoBooker': {}}, {}]
        with patch('main.read_yaml', side_effect=configs), patch('main.SeatAutoBooker') as client:
            self.assertEqual(main.run(args), 0)
            client.assert_not_called()

    def test_scheduled_flow_warms_connection_and_submits_at_target(self):
        target = datetime(2026, 10, 19, 20, 0, tzinfo=main.SHANGHAI)
        clock = [target - timedelta(seconds=60)]
        events = []
        args = Mock(date=datetime(2026, 10, 21).date(), at='20:00', check=False, test_booking=False)
        configs = [{'周三': {'启用': True, 'name': '二楼西', '开始时间': 7, '持续小时数': 1}},
                   {'SeatAutoBooker': {}}, {'二楼西': {'ranges': [[1, 1]]}}]
        client = Mock()
        client.bookings.return_value = []
        client.covered_booking.return_value = None
        client.available_seats.return_value = [{'id': 1, 'number': '1'}]
        def wait(value):
            clock[0] = max(clock[0], value)
            events.append(('wait', value))
        client.get_user_info.side_effect = lambda **kwargs: events.append(('warm', clock[0]))
        def submit(*args, **kwargs):
            events.append(('submit', clock[0]))
            return {'room': '二楼西', 'seat': '1', 'begin': '2026-10-21T07:00:00+08:00', 'booking_id': '9'}
        client.book_slot.side_effect = submit
        with tempfile.TemporaryDirectory() as folder, patch('main.LOCAL', Path(folder)), \
                patch('main.now', side_effect=lambda: clock[0]), patch('main.read_yaml', side_effect=configs), \
                patch('main.SeatAutoBooker', return_value=client), patch('main.wait_until', side_effect=wait):
            self.assertEqual(main.run(args), 0)
        client.available_seats.assert_not_called()
        self.assertEqual(events, [('wait', target - timedelta(seconds=30)),
                                 ('warm', target - timedelta(seconds=30)),
                                 ('wait', target - timedelta(seconds=3)),
                                 ('warm', target - timedelta(seconds=3)),
                                 ('wait', target), ('submit', target)])

    def test_run_summary_reports_order_and_sign_in_window(self):
        target = datetime(2026, 10, 19, 20, 0, tzinfo=main.SHANGHAI)
        args = Mock(date=datetime(2026, 10, 21).date(), at='20:00', check=False, test_booking=False)
        configs = [{'周三': {'启用': True, 'name': '二楼西', '开始时间': 7, '持续小时数': 1}},
                   {'SeatAutoBooker': {}}, {'二楼西': {'ranges': [[1, 1]]}}]
        client = Mock()
        client.bookings.return_value = []
        client.covered_booking.return_value = None
        client.available_seats.return_value = [{'id': 1, 'number': '1'}]
        client.book_slot.return_value = {'room': '二楼西', 'seat': '1', 'begin': '2026-10-21T07:00:00+08:00',
                                         'duration': 3600, 'booking_id': '9'}
        with tempfile.TemporaryDirectory() as folder:
            summary = Path(folder) / 'summary.md'
            with patch('main.LOCAL', Path(folder)), \
                    patch('main.now', return_value=target - timedelta(seconds=1)), \
                    patch('main.read_yaml', side_effect=configs), \
                    patch('main.SeatAutoBooker', return_value=client), \
                    patch('main.wait_until'), \
                    patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}):
                self.assertEqual(main.run(args), 0)
            text = summary.read_text(encoding='utf-8')
        self.assertIn('预约成功', text)
        self.assertIn('订单：9', text)
        self.assertIn('签到窗口', text)
        self.assertIn('06:30', text)
        self.assertIn('07:30', text)

    def test_opening_rejection_retries_same_seat_and_refreshes_request(self):
        client = self.client()
        target = datetime(2026, 10, 20, 20, tzinfo=main.SHANGHAI)
        client.request = Mock(side_effect=[{'MESSAGE': '超出可预约座位时间范围'},
                                          {'CODE': 'ok', 'DATA': {'result': 'success', 'bookingId': '9'}}])
        client.verify = Mock(return_value={'booking_id': '9'})
        with patch('main.now', return_value=target), patch('main.time.sleep') as sleep:
            result = client.book_slot(100, 200, [{'id': 62599}], target + timedelta(seconds=4))
        self.assertTrue(result['submission_confirmed'])
        # 整点退避改为 1/2/3 秒，避免 0.4 秒就撞上"请求太频繁"。
        sleep.assert_called_once_with(1.0)
        self.assertEqual([call.kwargs['data']['seats[0]'] for call in client.request.call_args_list], [62599, 62599])

    def test_opening_retries_are_bounded_and_not_used_after_window(self):
        for timed in (True, False):
            with self.subTest(timed=timed):
                client = self.client()
                target = datetime(2026, 10, 20, 20, tzinfo=main.SHANGHAI)
                client.request = Mock(return_value={'MESSAGE': '超出可预约座位时间范围'})
                current = target if timed else target + timedelta(seconds=5)
                with patch('main.now', return_value=current), patch('main.time.sleep'), \
                        self.assertRaisesRegex(main.BookingError, '时间范围'):
                    client.book_slot(100, 200, [{'id': 1}], target + timedelta(seconds=4))
                self.assertEqual(client.request.call_count, 4 if timed else 1)

    def test_custom_search_does_not_request_unrelated_categories(self):
        client = self.client()
        client.categories = {'自习室': '/Seat/Index/searchSeats?a=1', '生活区': '/unused', '阅览室': '/unused2'}
        client.request = Mock(return_value={'content': [{'seatMap': {
            'info': {'id': '1558', 'title': '四楼'},
            'POIs': [{'id': '62599', 'title': '35', 'state': '0'}]}}]})
        seats = client.available_seats('自定义', 100, 200,
            {'四楼': {'type': '自习室', 'ranges': [[62565, 62896]]}}, [62599])
        self.assertEqual([seat['id'] for seat in seats], [62599])
        self.assertEqual(client.request.call_count, 1)

    def test_backup_reuses_session_and_rechecks_through_reserve_day(self):
        target = datetime(2026, 10, 20, 20, tzinfo=main.SHANGHAI)
        args = Mock(date=datetime(2026, 10, 22).date(), at='20:00', backup_at='20:02', check=False, test_booking=False)
        configs = [{'周四': {'启用': True, 'name': '自定义', '开始时间': 15, '持续小时数': 7}},
                   {'SeatAutoBooker': {}}, {}]
        client = Mock()
        with patch('main.now', return_value=target - timedelta(minutes=5)), \
                patch('main.read_yaml', side_effect=configs), patch('main.SeatAutoBooker', return_value=client), \
                patch('main.reserve_day', side_effect=[main.BookingError('本轮失败'), 0]) as reserve, \
                patch('main.wait_until') as wait:
            self.assertEqual(main.run(args), 0)
        client.login.assert_called_once_with(load_categories=False)
        client.close.assert_called_once()
        self.assertEqual([call.args[5] for call in reserve.call_args_list], [target, target + timedelta(minutes=2)])
        wait.assert_called_once_with(target + timedelta(minutes=2, seconds=-20))

    def test_warmup_network_failure_does_not_skip_target_submission(self):
        target = datetime(2026, 10, 20, 20, tzinfo=main.SHANGHAI)
        clock = [target - timedelta(seconds=60)]
        client = Mock()
        client.bookings.return_value = []
        client.covered_booking.return_value = None
        client.get_user_info.side_effect = requests.Timeout()
        client.book_slot.return_value = {'room': '四楼', 'seat': '35', 'begin': '2026-10-22T15:00:00+08:00', 'booking_id': '9'}
        def wait(value):
            clock[0] = max(clock[0], value)
        with tempfile.TemporaryDirectory() as folder, patch('main.LOCAL', Path(folder)), \
                patch('main.now', side_effect=lambda: clock[0]), patch('main.wait_until', side_effect=wait):
            self.assertEqual(main.reserve_day(client, Mock(check=False), {'name': '自定义'}, [(15, 7)],
                datetime(2026, 10, 22).date(), target, {}, {'自定义': [62599]}), 0)
        client.available_seats.assert_not_called()
        client.book_slot.assert_called_once()
        self.assertEqual(clock[0], target)

    def test_all_24_candidates_can_be_tried_and_limit_takes_precedence(self):
        client = self.client()
        client.config['max-retry'] = 24
        seats = [{'id': seat_id, 'number': str(seat_id)} for seat_id in range(62599, 62623)]
        client.request = Mock(side_effect=[{'MESSAGE': '座位已被占用'}] * 23 +
                              [{'CODE': 'ok', 'DATA': {'result': 'success', 'bookingId': '9'}}])
        client.verify = Mock(return_value={'booking_id': '9'})
        with patch('main.time.sleep'):
            self.assertTrue(client.book_slot(100, 200, seats)['submission_confirmed'])
        self.assertEqual(client.request.call_count, 24)
        client.verify.assert_called_once_with('9', 100, 200, 62622)
        client.request = Mock(return_value={'MESSAGE': '操作频繁，未开放'})
        target = datetime(2026, 10, 20, 20, tzinfo=main.SHANGHAI)
        with patch('main.now', return_value=target), self.assertRaisesRegex(main.BookingError, '限流'):
            client.book_slot(100, 200, seats, target + timedelta(seconds=4))
        self.assertEqual(client.request.call_count, 1)

    def test_network_login_failure_gets_one_retry_but_rejection_does_not(self):
        for message, calls in [('统一身份认证网络异常：ConnectionError', 2),
                               ('统一身份认证被拒绝：密码错误', 1)]:
            with self.subTest(message=message):
                target = datetime(2026, 10, 20, 19, 55, tzinfo=main.SHANGHAI)
                args = Mock(date=datetime(2026, 10, 22).date(), at='20:00', backup_at=None, check=False, test_booking=False)
                configs = [{'周四': {'启用': True, 'name': '自定义', '开始时间': 15, '持续小时数': 7}},
                           {'SeatAutoBooker': {}}, {}]
                client = Mock()
                client.login.side_effect = [main.BookingError(message), None]
                with patch('main.now', return_value=target), patch('main.read_yaml', side_effect=configs), \
                        patch('main.SeatAutoBooker', return_value=client), patch('main.reserve_day', return_value=0), \
                        patch('main.time.sleep'):
                    if calls == 2:
                        self.assertEqual(main.run(args), 0)
                    else:
                        with self.assertRaisesRegex(main.BookingError, '密码错误'):
                            main.run(args)
                self.assertEqual(client.login.call_count, calls)

    def test_cas_page_fields_are_read_and_unescaped(self):
        html = ('<div style="display: none"><p id="current-login-type">UsernamePassword</p>'
                '<p id="login-croypto">iPHtqot014ZhiQn6oP5xcw==</p>'
                '<p id="login-page-flowkey">9facd71e-6ac6_ZXlK&amp;A</p></div>')
        self.assertEqual(main.page_field(html, 'current-login-type'), 'UsernamePassword')
        self.assertEqual(main.page_field(html, 'login-croypto'), 'iPHtqot014ZhiQn6oP5xcw==')
        self.assertEqual(main.page_field(html, 'login-page-flowkey'), '9facd71e-6ac6_ZXlK&A')
        self.assertEqual(main.page_field(html, 'login-captcha'), '')

    def test_password_encryption_matches_the_cas_client(self):
        key = base64.b64encode(bytes(16)).decode()
        self.assertEqual(main.encrypt_password('abc', key), 'l2qZX5TPTB4ktuL7Bn56aQ==')
        with self.assertRaisesRegex(main.BookingError, '加密参数'):
            main.encrypt_password('abc', 'not-base64!!')
        with self.assertRaisesRegex(main.BookingError, '加密参数'):
            main.encrypt_password('abc', base64.b64encode(bytes(8)).decode())

    def test_login_failure_reason_never_echoes_credentials(self):
        html = '<div class="login-error">密码错误 24060905 zyj</div>'
        reason = main.login_failure_reason(html, '24060905', 'zyj')
        self.assertNotIn('24060905', reason)
        self.assertNotIn('zyj', reason)
        self.assertIn('密码错误', reason)

    def test_late_cloud_start_books_immediately_instead_of_aborting(self):
        target = datetime(2026, 10, 20, 20, 0, tzinfo=main.SHANGHAI)
        args = Mock(check=False, test_booking=False)
        client = Mock()
        client.bookings.return_value = []
        client.covered_booking.return_value = None
        client.book_fourth_floor.return_value = {
            'room': '四楼', 'seat': '35', 'seat_id': 62599,
            'begin': '2026-10-22T15:00:00+08:00', 'duration': 25200, 'booking_id': '9'}
        user_config = {'自定义': {'房间': '四楼', '优先级': [
            {'名称': '特等座', '区间': [[62581, 62622]]}]}}
        day = {'name': '自定义', '开始时间': 15, '持续小时数': 7}
        with tempfile.TemporaryDirectory() as folder, patch('main.LOCAL', Path(folder)), \
                patch('main.now', return_value=target + timedelta(seconds=95)), \
                patch('main.wait_until') as wait:
            main.reserve_day(client, args, day, [(15, 7)], datetime(2026, 10, 22).date(),
                             target, {}, user_config)
        wait.assert_not_called()
        self.assertTrue(client.book_fourth_floor.called)


if __name__ == '__main__':
    unittest.main()
