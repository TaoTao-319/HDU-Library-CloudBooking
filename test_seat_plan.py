"""Offline priority, range, no-seat, and five-round integration regressions."""
from datetime import datetime, timedelta
from pathlib import Path
import copy
import tempfile
import unittest
from unittest.mock import Mock, patch

import main
from seat_plan import custom_candidates, daily_targets


class SeatPlanTests(unittest.TestCase):
    def setUp(self):
        self.config = main.read_yaml(main.ROOT / 'user_config.yml')
        self.custom = self.config['自定义']

    def test_full_mapping_tier_order_and_randomization(self):
        previous = None
        for _ in range(5):
            seats = custom_candidates(self.custom)
            ids = [seat['id'] for seat in seats]
            self.assertEqual(set(ids), set(range(62565, 62897)))
            self.assertEqual(len(ids), 332)
            self.assertEqual(set(ids[:42]), set(range(62581, 62623)))
            self.assertEqual(set(ids[42:68]), set(range(62871, 62897)))
            self.assertEqual(set(ids[68:]), set(range(62565, 62581)) | set(range(62623, 62871)))
            self.assertEqual([seat['priority'] for seat in seats], sorted(seat['priority'] for seat in seats))
            for seat in seats:
                self.assertEqual(int(seat['number']) + 62564, seat['id'])
            if previous is not None:
                self.assertNotEqual(ids[:42], previous[:42])
            previous = ids

    def test_invalid_ranges_rejected_before_booking(self):
        for ranges in ([[62564, 62565]], [[62896, 62897]], [[62622, 62581]],
                       [[62581, 62622], [62622, 62623]], [[True, 62622]]):
            custom = copy.deepcopy(self.custom)
            custom['优先级'][0]['区间'] = ranges
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                custom_candidates(custom)

    def test_filtered_map_keeps_tiers_and_excludes_other_rooms(self):
        client = main.SeatAutoBooker({'max-retry': 332})
        self.addCleanup(client.close)
        client.categories = {'自习室': '/Seat/Index/searchSeats?a=1', '阅览室': '/never'}
        def room(room_id, ids):
            return {'seatMap': {'info': {'id': room_id, 'title': 'room'}, 'POIs':
                [{'id': str(seat), 'title': str(seat - 62564), 'state': '0'} for seat in ids]}}
        client.request = Mock(return_value={'content': [room('1558', [62565, 62581, 62871]),
                                                       room('1524', [62582])]})
        result = client.available_seats('自定义', 100, 200, main.read_yaml(main.ROOT / 'config/seat_config.yml'), self.custom)
        self.assertEqual([seat['id'] for seat in result], [62581, 62871, 62565])
        self.assertEqual(client.request.call_count, 1)

    def test_fast_first_post_then_live_map_and_no_other_floor_fallback(self):
        client = main.SeatAutoBooker({'max-retry': 332})
        self.addCleanup(client.close)
        target = datetime(2026, 10, 21, 20, tzinfo=main.SHANGHAI)
        seats = custom_candidates(self.custom)
        client.book_slot = Mock(side_effect=[main.CandidatesUnavailable('占用'), {'booking_id': '9'}])
        client.load_categories = Mock()
        client.available_seats = Mock(return_value=[{'id': 62871}])
        with patch('main.now', return_value=target):
            result = client.book_fourth_floor(100, 200, seats, self.custom, {}, target + timedelta(seconds=4))
        self.assertEqual(result['booking_id'], '9')
        self.assertEqual(client.book_slot.call_args_list[0].args[2], seats[:1])
        self.assertEqual(client.book_slot.call_args_list[1].args[2], [{'id': 62871}])
        client.available_seats.return_value = []
        client.book_slot.reset_mock(side_effect=True)
        with self.assertRaises(main.NoSeats):
            client.book_fourth_floor(100, 200, seats, self.custom, {})
        client.book_slot.assert_not_called()

    def test_five_targets_validated(self):
        day = datetime(2026, 10, 21, 19, 55, tzinfo=main.SHANGHAI)
        result = daily_targets(day, self.config['自动预约'])
        self.assertEqual([t.strftime('%H:%M:%S') for t in result],
                         ['20:00:00', '20:00:30', '20:01:30', '20:04:00', '20:10:00'])
        for times in (['20:00:00'] * 5, ['20:00:00', '20:01:00'],
                      ['20:00:00', '20:01:00', '20:02:00', '20:03:00', '20:16:00']):
            with self.assertRaises(ValueError):
                daily_targets(day, {'运行时间': times})

    def simulate(self, side_effect, repeat=False):
        start = datetime(2026, 10, 21, 19, 55, tzinfo=main.SHANGHAI)
        clock = [start]
        client = Mock()
        def wait(target):
            clock[0] = max(clock[0], target)
        def reserve(*args):
            clock[0] = max(clock[0], args[5])
            value = next(effects)
            if isinstance(value, Exception):
                raise value
            return value
        effects = iter(side_effect)
        with tempfile.TemporaryDirectory() as folder, patch('main.LOCAL', Path(folder)), \
                patch('main.now', side_effect=lambda: clock[0]), patch('main.wait_until', side_effect=wait), \
                patch('main.reserve_day', side_effect=reserve) as runner:
            result = main.run_daily(client, Mock(), {}, [], start.date(), {}, self.config)
            calls = runner.call_count
            if repeat:
                main.run_daily(client, Mock(), {}, [], start.date(), {}, self.config)
                self.assertEqual(runner.call_count, calls)
        return result, calls, client

    def test_five_failures_reuse_one_login_and_restart_does_not_repeat(self):
        result, calls, client = self.simulate([main.BookingError('网络暂时失败')] * 5, repeat=True)
        self.assertEqual((result, calls), (1, 5))
        client.login.assert_called_once()

    def test_success_and_full_floor_end_remaining_rounds(self):
        for outcome in (0, main.NoSeats('四楼无座')):
            result, calls, client = self.simulate([outcome], repeat=True)
            self.assertEqual((result, calls), (0, 1))
            client.login.assert_called_once()

    def test_expired_plan_never_logs_in_or_submits(self):
        end = datetime(2026, 10, 21, 20, 16, tzinfo=main.SHANGHAI)
        client = Mock()
        with tempfile.TemporaryDirectory() as folder, patch('main.LOCAL', Path(folder)), \
                patch('main.now', return_value=end), patch('main.reserve_day') as reserve:
            self.assertEqual(main.run_daily(client, Mock(), {}, [], end.date(), {}, self.config), 1)
        client.login.assert_not_called()
        reserve.assert_not_called()

    def test_second_round_uses_live_map_path_without_opening_fast_post(self):
        target = datetime(2026, 10, 21, 20, 0, 30, tzinfo=main.SHANGHAI)
        client = Mock()
        client.bookings.return_value = []
        client.covered_booking.return_value = None
        client.book_fourth_floor.return_value = {'booking_id': '9', 'room': '四楼',
                'seat': '17', 'seat_id': 62581, 'begin': '2026-10-23T13:00:00+08:00'}
        with tempfile.TemporaryDirectory() as folder, patch('main.LOCAL', Path(folder)), \
                patch('main.now', return_value=target), patch('main.wait_until'):
            self.assertEqual(main.reserve_day(client, Mock(check=False), {'name': '自定义'}, [(13, 9)],
                             target.date(), target, {}, self.config), 0)
        self.assertIsNone(client.book_fourth_floor.call_args.args[-1])

    def test_map_error_or_missing_floor_is_not_reported_as_no_seats(self):
        client = main.SeatAutoBooker({'max-retry': 332})
        self.addCleanup(client.close)
        client.categories = {'自习室': '/map'}
        seat_config = main.read_yaml(main.ROOT / 'config/seat_config.yml')
        for payload in ({'CODE': 'error', 'MESSAGE': '服务异常'}, {'content': []}):
            client.request = Mock(return_value=payload)
            with self.subTest(payload=payload), self.assertRaises(main.BookingError) as caught:
                client.available_seats('自定义', 100, 200, seat_config, self.custom)
            self.assertNotIsInstance(caught.exception, main.NoSeats)
        client.request = Mock(return_value={'content': [{'seatMap': {
            'info': {'id': '1558', 'title': '四楼'},
            'POIs': [{'id': '62581', 'title': '17', 'state': '1'}]}}]})
        self.assertEqual(client.available_seats('自定义', 100, 200, seat_config, self.custom), [])

    def test_round_deadline_blocks_further_submission(self):
        client = main.SeatAutoBooker({'max-retry': 332})
        self.addCleanup(client.close)
        client.round_deadline = main.now() - timedelta(seconds=1)
        client.request = Mock()
        with self.assertRaisesRegex(main.BookingError, '时间预算'):
            client.book_slot(100, 200, custom_candidates(self.custom))
        client.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
