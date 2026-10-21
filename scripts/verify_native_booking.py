"""Accept only orders created and verified by this very GitHub Actions run."""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8), 'Asia/Shanghai')


def verify_native_booking(report_dir, event, run_id, allowed_events=('schedule',),
                          begin=None, duration=None):
    if event not in allowed_events or not run_id:
        raise ValueError('验收失败：必须是 %s 触发的运行，实际为 %s'
                         % (' 或 '.join(allowed_events), event))
    reports = []
    for path in Path(report_dir).glob('booking-*.json'):
        reports.extend(json.loads(path.read_text(encoding='utf-8')))
    fourth_floor = {str(seat_id) for seat_id in range(62565, 62897)}
    confirmed = [order for order in reports
                 if order.get('github_run_id') == run_id
                 and order.get('trigger_event') == event
                 and order.get('submission_confirmed') is True
                 and order.get('already_existed') is False
                 and str(order.get('seat_id')) in fourth_floor
                 and (begin is None
                      or int(datetime.fromisoformat(order['begin']).timestamp()) == begin)
                 and (duration is None or int(order['duration']) == duration)]
    if not confirmed:
        raise ValueError('验收失败：本次没有确认新建目标座位订单；'
                         '已有预约或提交结果不确定均不算通过')
    return confirmed


def main():
    parser = argparse.ArgumentParser(description='校验本次云端运行确实新建并核实了预约')
    parser.add_argument('--date', type=lambda value: datetime.strptime(value, '%Y-%m-%d').date())
    parser.add_argument('--start', type=float)
    parser.add_argument('--hours', type=float)
    parser.add_argument('--event', default='schedule',
                        help='期望的触发事件，可写 schedule 或 schedule,workflow_dispatch')
    args = parser.parse_args()
    begin = duration = None
    if args.date and args.start is not None:
        midnight = datetime.combine(args.date, datetime.min.time(), SHANGHAI)
        begin = int((midnight + timedelta(hours=args.start)).timestamp())
        duration = int(round((args.hours if args.hours is not None else 1) * 3600))
    orders = verify_native_booking(
        Path(__file__).resolve().parents[1] / '.local',
        os.environ.get('GITHUB_EVENT_NAME'), os.environ.get('GITHUB_RUN_ID'),
        [value.strip() for value in args.event.split(',') if value.strip()],
        begin, duration)
    print('新建预约验收通过：' + json.dumps(orders, ensure_ascii=False))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as stream:
            stream.write('本次运行新建预约并查单确认成功。\n\n')
            for order in orders:
                stream.write('- 订单 %s；%s %s号；%s\n' % (
                    order['booking_id'], order['room'], order['seat'], order['begin']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
