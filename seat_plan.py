"""Inclusive fourth-floor ranges and tier-preserving random selection."""
import random
from datetime import datetime


def custom_candidates(custom):
    if isinstance(custom, list):
        return [{'id': seat, 'number': str(seat), 'room': '自定义'}
                for seat in dict.fromkeys(int(value) for value in custom)]
    if not isinstance(custom, dict) or custom.get('房间') != '四楼':
        raise ValueError('分级自定义配置必须指定 房间: 四楼')
    groups = custom.get('优先级', [])
    if not groups:
        raise ValueError('自定义优先级不能为空')
    result, seen = [], set()
    for rank, group in enumerate(groups):
        ids = []
        for interval in group['区间']:
            if (not isinstance(interval, list) or len(interval) != 2
                    or any(type(value) is not int for value in interval)):
                raise ValueError('区间必须写成 [起始后台ID, 结束后台ID]')
            first, last = interval
            if not 62565 <= first <= last <= 62896:
                raise ValueError('自定义区间必须位于四楼 62565–62896 内')
            for seat in range(first, last + 1):
                if seat in seen:
                    raise ValueError('自定义优先级区间不能重复或重叠')
                seen.add(seat)
                ids.append(seat)
        random.shuffle(ids)
        result.extend({'id': seat, 'number': str(seat - 62564), 'room': '四楼',
                       'priority': rank, 'tier': group['名称']} for seat in ids)
    if not result:
        raise ValueError('自定义候选座位不能为空')
    return result


def daily_targets(day, config):
    values = config['运行时间']
    if not isinstance(values, list) or len(values) != 5:
        raise ValueError('每日必须配置五个运行时间')
    times = [datetime.strptime(value, '%H:%M:%S').time() for value in values]
    if times != sorted(set(times)) or times[0].isoformat() != '20:00:00':
        raise ValueError('五轮时间必须严格递增且第一轮为20:00:00')
    if times[-1].isoformat() > '20:15:00':
        raise ValueError('最后一轮不得晚于20:15:00')
    return [datetime.combine(day.date(), value, day.tzinfo) for value in times]
