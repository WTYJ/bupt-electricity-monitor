#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
监控核心模块 - 登录认证、电费数据采集、用电预测、统计汇总

数据读写一律通过 storage 模块；阈值统一使用 config.LOW_BALANCE_THRESHOLD
（旧版的双重阈值配置 PREDICTION_THRESHOLD 已废弃，不再读取）。
"""

import json
import logging
import math
from datetime import datetime, timedelta
from threading import Lock

import requests
from bs4 import BeautifulSoup

import config
import notifier
import storage
from config_io import check_frequency_minutes

ELECTRIC_PAGE_URL = "https://app.bupt.edu.cn/buptdf/wap/default/chong"
ELECTRIC_SEARCH_URL = "https://app.bupt.edu.cn/buptdf/wap/default/search"
LOGIN_URL = "https://auth.bupt.edu.cn/authserver/login"

# 校园电费充值固定档位（度）。充值后到下次查询之间仍在用电，
# 观测到的余额增量会略小于实际购电量，故按"向上取最近档位"还原真实充值额。
RECHARGE_TIERS = (25, 50, 75, 100, 150, 200)
_check_lock = Lock()


class CheckInProgress(RuntimeError):
    """同一监控实例已有检查在运行。"""


def snap_recharge_amount(diff):
    """把观测到的充值增量向上对齐到最近的充值档位"""
    for tier in RECHARGE_TIERS:
        if diff <= tier:
            return tier
    return round(diff, 1)  # 超过单档上限（可能多笔合并），如实保留


class ElectricMonitor:
    """北邮电费查询与会话管理"""

    def __init__(self):
        self.session = requests.Session()
        self.collection_error = None
        self.last_check = None

    def login_bupt(self):
        """登录北邮统一身份认证"""
        try:
            response = self.session.get(LOGIN_URL, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            # 提取登录页必要的隐藏字段
            type_input = soup.find('input', {'name': 'type'})
            execution_input = soup.find('input', {'name': 'execution'})
            _eventId_input = soup.find('input', {'name': '_eventId'})

            if not type_input or not execution_input or not _eventId_input:
                self.collection_error = '学校登录页面格式异常，请稍后重试。'
                logging.error("无法找到登录所需的隐藏字段，页面结构可能已变化")
                # 保存页面内容用于调试
                with open('debug_login_page.html', 'w', encoding='utf-8') as f:
                    f.write(response.text)
                logging.info("登录页面内容已保存到debug_login_page.html")
                return False

            login_data = {
                'username': config.BUPT_USERNAME,
                'password': config.BUPT_PASSWORD,
                'type': type_input.get('value', ''),
                'execution': execution_input.get('value', ''),
                '_eventId': _eventId_input.get('value', '')
            }

            response = self.session.post(LOGIN_URL, data=login_data, timeout=10)
            response.raise_for_status()

            if 'CAS Login' in response.text or '统一身份认证' in response.text:
                self.collection_error = '学校账号登录失败，请核对统一身份认证配置。'
                logging.error("登录失败，请检查用户名和密码")
                return False

            logging.info("登录成功")
            return True

        except requests.Timeout:
            self.collection_error = '学校登录接口超时，请稍后重试。'
            logging.warning(self.collection_error)
            return False
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else '未知'
            self.collection_error = f'学校登录服务返回 HTTP {status}，请稍后重试。'
            logging.warning(self.collection_error)
            return False
        except requests.RequestException:
            self.collection_error = '无法连接学校登录接口，请检查网络。'
            logging.warning(self.collection_error)
            return False
        except Exception as e:
            logging.error(f"登录过程中出现错误: {str(e)}")
            return False

    def check_login_needed(self, response):
        """检查响应是否需要登录"""
        if (response.status_code == 302 or
            'auth.bupt.edu.cn' in response.url or
            'CAS Login' in response.text or
            '统一身份认证' in response.text or
                'login' in response.url.lower()):
            logging.info("检测到需要登录")
            return True
        return False

    def get_electric_data(self):
        """获取电费数据 - 智能登录检查"""
        self.collection_error = None
        try:
            # 先尝试直接访问电费查询页面，检查是否需要登录
            response = self.session.get(ELECTRIC_PAGE_URL, timeout=10, allow_redirects=True)

            logging.info(f"访问电费页面: 状态码={response.status_code}, URL={response.url}")

            # 检查是否被重定向到登录页面
            if self.check_login_needed(response):
                logging.info("需要登录，开始登录流程...")
                if not self.login_bupt():
                    logging.error("登录失败")
                    self.collection_error = self.collection_error or '学校登录未完成，请查看系统日志。'
                    return None

                # 重新访问电费页面
                response = self.session.get(ELECTRIC_PAGE_URL, timeout=10)
                if response.status_code != 200:
                    logging.error(f"登录后访问电费页面失败，状态码: {response.status_code}")
                    self.collection_error = '学校电费页面暂时不可用，请稍后重试。'
                    return None
                logging.info("登录成功，重新访问电费页面成功")
            else:
                logging.info("无需登录，直接查询电费数据")

            if response.status_code != 200:
                logging.error(f"访问电费页面失败，状态码: {response.status_code}")
                self.collection_error = '学校电费页面暂时不可用，请稍后重试。'
                return None

            logging.info("开始查询电费数据...")

            # 配置查询参数
            area_id = getattr(config, 'AREA_ID', 2)  # 默认沙河校区
            apartment_id = getattr(config, 'APARTMENT_ID', '沙河校区雁北园A楼')
            floor_id = getattr(config, 'FLOOR_ID', '1层')
            room_number = getattr(config, 'ROOM_NUMBER', '190807009132')

            logging.info(f"查询参数: 校区ID={area_id}, 公寓={apartment_id}, 楼层={floor_id}, 房间号={room_number}")

            # 查询电费数据
            response = self.session.post(ELECTRIC_SEARCH_URL, data={
                'partmentId': apartment_id,
                'floorId': floor_id,
                'dromNumber': room_number,
                'areaid': area_id
            }, timeout=10)

            if response.status_code != 200:
                logging.error(f"电费查询接口访问失败，状态码: {response.status_code}")
                self.collection_error = '学校电费接口暂时不可用，请稍后重试。'
                return None

            # 解析JSON响应
            try:
                electric_data = response.json()
                logging.info(f"收到电费数据响应: {electric_data}")

                if electric_data.get('e') != 0:
                    logging.error(f"电费查询失败: {electric_data.get('m', '未知错误')}")
                    self.collection_error = '学校拒绝了电费查询，请核对宿舍配置或稍后重试。'
                    return None

                # 提取数据
                raw_data = electric_data.get('d', {}).get('data', {})

                # 校验公寓名，防止接口偶发返回其他房间的数据污染记录
                expected_name = getattr(config, 'EXPECTED_APARTMENT_NAME', '')
                returned_name = str(raw_data.get('parName', ''))
                if expected_name and returned_name != expected_name:
                    logging.error(f"接口返回的公寓名不匹配: 期望[{expected_name}], 实际[{returned_name}]，本次数据不保存")
                    self.collection_error = '学校返回的公寓与配置不一致，请核对宿舍信息。'
                    return None

                # 学校赠送池（freeEnd）优先扣费：真实可用电量 = 付费余额 + 赠送余量
                surplus_value = raw_data.get('surplus')
                if surplus_value is None or isinstance(surplus_value, bool):
                    raise ValueError('缺少有效付费余量 surplus')
                surplus = float(surplus_value)
                free_pool = float(raw_data.get('freeEnd') or 0)
                balance = surplus + free_pool
                if not math.isfinite(balance):
                    raise ValueError('付费或赠送余量不是有限数值')
                data = {
                    'balance': balance,  # 总可用电量(度)
                    'free_pool': free_pool,  # 其中学校赠送部分(度)
                    'total_usage': float(raw_data.get('totalActiveDisp') or 0),  # 电表累计示数(度)。注意: vTotal是电压不是用电量
                    'power': float(raw_data.get('pTotal') or 0),  # 实时有功功率(千瓦)
                    'usage_today': None,  # 今日用电量（从原始数据无法直接获取，设为None）
                    'usage_month': None,  # 本月用电量（从原始数据无法直接获取，设为None）
                    'price': float(raw_data.get('price', 0.48)),  # 电价
                    'query_time': raw_data.get('time', ''),  # 查询时间
                    'apartment': raw_data.get('parName', ''),  # 公寓名称
                    'floor': raw_data.get('floorName', ''),  # 楼层
                    'status': 'success',
                    'raw_data': json.dumps(electric_data, ensure_ascii=False),
                    'timestamp': datetime.now()
                }

                logging.info(f"解析电费数据成功: 总可用电量={data['balance']}度(含赠送{free_pool}度), "
                             f"累计用电={data['total_usage']}度, 实时功率={data['power']}千瓦, 购电单价={data['price']}元/度")

                # 计算今日和月度用电量
                data = self.calculate_usage_data(data)

                return data

            except json.JSONDecodeError as e:
                logging.error(f"电费数据JSON解析失败: {e}")
                logging.error(f"响应内容: {response.text[:500]}")
                self.collection_error = '学校返回的电费数据无法读取，请稍后重试。'
                return None
            except (TypeError, ValueError, KeyError) as e:
                logging.error(f"电费数据解析错误: {e}")
                self.collection_error = '学校返回的电费数据不完整，请稍后重试。'
                return None

        except requests.Timeout:
            self.collection_error = '学校电费接口超时，请稍后重试。'
            logging.warning(self.collection_error)
            return None
        except requests.RequestException:
            self.collection_error = '无法连接学校电费接口，请检查网络。'
            logging.warning(self.collection_error)
            return None
        except Exception as e:
            logging.error(f"获取电费数据时出现错误: {str(e)}")
            return None

    def calculate_usage_data(self, data):
        """根据历史数据计算今日和月度用电量（充值区间按档位还原，充值后数值依然正确）"""
        try:
            if data['balance'] is not None:
                today_usage = self.usage_since(datetime.now().strftime('%Y-%m-%d 00:00:00'))
                month_usage = self.usage_since(datetime.now().strftime('%Y-%m-01 00:00:00'))
                if today_usage is not None:
                    data['usage_today'] = today_usage
                if month_usage is not None:
                    data['usage_month'] = month_usage

            logging.info(f"计算用电量：今日={data.get('usage_today')}度，本月={data.get('usage_month')}度")
            return data
        except Exception as e:
            logging.error(f"计算用电量时出错: {str(e)}")
            return data

    def get_statistics(self):
        """获取仪表盘统计数据"""
        latest = storage.latest_record()

        # 充值/赠送检测：充值看付费池(surplus)上升，学校到账看赠送池(freeEnd)上升，两者不混为一谈
        cutoff = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        last_recharge = None
        last_grant = None
        free_pool = 0.0
        prev_surplus = None
        prev_free = None
        for ts, balance, raw in storage.balances_with_raw_since(cutoff):
            surplus, free = storage.parse_pools(raw, float(balance))
            free_pool = free
            if prev_surplus is not None:
                if surplus - prev_surplus >= 1.0:
                    last_recharge = {'time': ts, 'amount': snap_recharge_amount(surplus - prev_surplus)}
                if free - prev_free >= 1.0:
                    last_grant = {'time': ts, 'amount': round(free - prev_free, 1)}
            prev_surplus, prev_free = surplus, free

        # 日均用电量（度/天）：优先近 7 天，样本不足时放宽窗口。
        # 动机：间隔很久才打开时，7 天窗口里往往只有刚采到的一条记录，
        # 配不出区间（区间需要相邻两条），会显示 0。沿用预测那边的「逐级放宽」
        # 思路，放宽到能凑出区间为止——速率跨空洞按时间加权平均是合理的估计。
        usage_avg, observed_days, avg_window_days = self.avg_daily_usage(7)

        # 本月用量：与「每日用电量」图表同一套口径——区间按时间占比分摊到自然日，
        # 只累加落在本月的部分。这样即使本月只有一条记录（上一条在上个月），
        # 跨月区间也能按天拆分，不会因为配不出区间而显示 0。
        month_usage = self.month_usage_allocated()

        # 余额趋势：24小时按小时、30天按天
        # （曾有一个 12 个月的 monthly 序列，前端从未使用，已删除）
        balance_trend_hourly = storage.balance_trend(
            "strftime('%Y-%m-%d %H:00:00', timestamp)",
            '-24 hours')
        balance_trend_daily = storage.balance_trend(
            "date(timestamp)",
            '-30 days',
            drop_early_tail=True)  # 停服日（日末无读数）不画点，避免假平台

        # 每日用电量（柱状图用）：区间按时间占比分摊到天，空档天由前后区间线性估算
        daily_usage = self.daily_usage_trend(30)

        # 实时功率（来自最新记录原始数据中的pTotal，单位千瓦；vTotal是电压，不是用电量）
        current_power = None
        if latest and latest[-1]:
            try:
                meter = json.loads(latest[-1]).get('d', {}).get('data', {}) or {}
                pt = meter.get('pTotal')
                current_power = float(pt) if pt not in (None, '') else None
            except (ValueError, TypeError):
                current_power = None

        # 电量预测（阈值统一使用 LOW_BALANCE_THRESHOLD）
        prediction = self.predict()

        return {
            'latest': latest,
            'current_power': current_power,
            'free_pool': free_pool,
            'last_grant': last_grant,
            'usage_avg_7d': usage_avg if usage_avg is not None else 0.0,
            'observed_days_7d': observed_days,
            'usage_avg_window_days': avg_window_days,
            'last_recharge': last_recharge,
            'month_usage': month_usage if month_usage is not None else 0.0,
            'balance_trend_hourly': balance_trend_hourly,
            'balance_trend_daily': balance_trend_daily,
            'daily_usage': daily_usage,
            'prediction': prediction
        }

    def avg_daily_usage(self, preferred_days=7):
        """日均用电量（度/天）：优先最近 preferred_days 天，样本不足时逐级放宽。

        动机：间隔很久才打开时，短窗口里可能只有刚采到的一条记录。区间需要
        相邻两条记录才能配对，单条记录会得到 0——这是用户实际遇到的问题
        （距上次记录隔了十天，首屏日均显示 0，采到第二条才恢复）。

        日均是**速率**，跨数据空洞按时间加权平均是合理估计（与 predict 的
        窗口放宽同思路），所以这里放宽窗口而不是显示 0。

        Returns:
            (日均速率 or None, 实际观测天数, 采用的窗口天数)
        """
        candidates = []
        for d in (preferred_days, max(preferred_days, 14), 30):
            if d not in candidates:
                candidates.append(d)
        for days in candidates:
            samples = self.collect_usage_intervals(days)
            if not samples:
                continue
            total_drop = sum(s['drop'] for s in samples)
            total_span = sum(s['span'] for s in samples)
            if total_span > 0:
                return round(total_drop / total_span, 2), round(min(total_span, days), 2), days
        return None, 0.0, preferred_days

    def month_usage_allocated(self):
        """本月累计用量（度）：区间按时间占比分摊到自然日，只累加本月部分。

        与 daily_usage_trend（「每日用电量」柱状图）完全同一套口径：
        - 记录密集时是精确值；
        - 停服/长时间未打开时，跨越空洞的区间按天数线性均摊；
        - 跨月区间按天拆分，本月只拿属于自己的那部分（不把上月用电算进本月）。

        这样即使本月只有一条记录（上一条在上个月），也能给出合理估计，
        而不会因为「本月窗口里配不出区间」显示 0。

        Returns:
            float: 本月累计度数；本月完全没有可用区间时才返回 0.0
        """
        month_start = datetime.combine(datetime.now().date().replace(day=1), datetime.min.time())
        daily = self._daily_usage_from(month_start)
        prefix = month_start.strftime('%Y-%m-')
        return round(sum(v for k, v in daily if k.startswith(prefix)), 2)

    def collect_usage_intervals(self, lookback_days=30, *, sampled_at=None):
        """读取以当前时间或指定采样时间为终点的用电样本。"""
        end = sampled_at if sampled_at is not None else datetime.now()
        cutoff = (end - timedelta(days=lookback_days)).strftime('%Y-%m-%d %H:%M:%S')
        until = sampled_at.strftime('%Y-%m-%d %H:%M:%S') if sampled_at is not None else None
        return self._samples_since(cutoff, until)

    def daily_usage_trend(self, days=30):
        """最近 N 天的每日用电量序列，[[label, 度], ...] 按日期升序。
        days 为 None 时返回完整历史。

        区间用电按时间占比线性分摊到所跨越的每个自然日：
        - 记录密集的时段（每30分钟一条）得到精确值
        - 服务停跑等数据空档，由跨越空档的区间按天数线性均摊（即用前后数据估算）
        - 消耗口径为双池拆分（见 _pool_intervals），赠送期间用量同样正确
        """
        if days is None:
            return self._daily_usage_from(None)
        start_date = datetime.now().date() - timedelta(days=days - 1)
        return self._daily_usage_from(datetime.combine(start_date, datetime.min.time()))

    def _daily_usage_from(self, cutoff):
        alloc = {}
        for day, _, usage in self._usage_segments(cutoff):
            alloc[day] = alloc.get(day, 0.0) + usage
        return [[day, round(usage, 2)] for day, usage in alloc.items()]

    def _usage_segments(self, cutoff):
        """按自然日分摊窗口内的用电区间，产出日期、片段终点和用量。"""
        cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S') if cutoff is not None else None
        for start, end, _, drop in self._pool_intervals(cutoff_str, include_previous=True):
            span_seconds = (end - start).total_seconds()
            segment_start = max(start, cutoff) if cutoff is not None else start
            while segment_start < end:
                day = segment_start.date()
                midnight = datetime.combine(day + timedelta(days=1), datetime.min.time())
                segment_end = min(end, midnight)
                seconds = (segment_end - segment_start).total_seconds()
                yield day.isoformat(), segment_end, drop * seconds / span_seconds
                segment_start = segment_end

    def usage_since(self, since_str, until_str=None):
        """计算自 since_str（可选到 until_str）以来的用电量（度）。

        复用区间采样（含充值还原与异常过滤），返回 None 表示窗口内没有数据。
        """
        samples = self._samples_since(since_str, until_str)
        if not samples:
            return None
        return round(sum(s['drop'] for s in samples), 2)

    def usage_curve_since(self, since_str):
        """返回按时间升序的片段终点和累计用量，供最近记录表一次切区间后查表。"""
        ends, cums = [], []
        previous_day = None
        completed_days = 0.0
        day_usage = 0.0
        for day, end, usage in self._usage_segments(datetime.fromisoformat(since_str)):
            if day != previous_day:
                completed_days += round(day_usage, 2)
                day_usage = 0.0
                previous_day = day
            day_usage += usage
            ends.append(end.strftime('%Y-%m-%d %H:%M:%S'))
            cums.append(round(completed_days + round(day_usage, 2), 2))
        return ends, cums

    def _pool_intervals(self, cutoff_str, until_str=None, *, include_previous=False):
        """相邻记录对的用电区间：(start, end, span天, 消耗度数)。

        学校赠送池（freeEnd）优先扣费，消耗按两个池子分别计算再相加：
        - 付费池下降 = 付费消耗；付费池上升 = 充值，按固定档位还原后计入
        - 赠送池下降 = 赠送消耗；赠送池上升 = 学校到账，该区间赠送消耗记 0（误差<1度/区间）
        - 折算日均超过 PREDICTION_MAX_DAILY_RATE 的区间视为异常丢弃
        """
        try:
            rows = storage.balances_with_raw_since(cutoff_str, include_previous=include_previous)
        except Exception as e:
            logging.error(f"读取历史记录失败: {str(e)}")
            return []
        if until_str:
            rows = [r for r in rows if str(r[0]) <= until_str]

        max_daily_rate = float(getattr(config, 'PREDICTION_MAX_DAILY_RATE', 50.0))
        intervals = []
        prev = None  # (time, surplus, free)
        for ts, balance, raw in rows:
            try:
                cur_time = datetime.fromisoformat(str(ts))
            except ValueError:
                prev = None
                continue
            cur_surplus, cur_free = storage.parse_pools(raw, float(balance))
            if prev is not None:
                span = (cur_time - prev[0]).total_seconds() / 86400.0
                paid = prev[1] - cur_surplus
                if paid < 0:
                    rise = -paid
                    paid = snap_recharge_amount(rise) - rise
                free = max(0.0, prev[2] - cur_free)
                drop = max(paid, 0.0) + free
                if span > 0 and drop <= max_daily_rate * span:
                    intervals.append((prev[0], cur_time, span, drop))
            prev = (cur_time, cur_surplus, cur_free)
        return intervals

    def _samples_since(self, cutoff_str, until_str=None):
        """读取历史记录并切分成相邻记录对，用于估计用电速率。

        每对相邻记录产生一个样本：消耗度数 drop 与时间跨度 span（天），
        消耗口径见 _pool_intervals（双池拆分，含充值还原与赠送到账处理）。

        由于按"消耗量/时间差"计算，只要有两分钟以上的两条记录就能估计速率，
        不要求凑齐整天，也不要求每天至少两条记录。

        Returns:
            list[dict]: [{span, drop, end}, ...]，按时间升序
        """
        samples = []
        for _start, end, span, drop in self._pool_intervals(cutoff_str, until_str):
            samples.append({
                'span': span,
                'drop': drop,
                'end': end.strftime('%Y-%m-%d %H:%M:%S'),  # 区间终点（调试/日志用）
            })
        return samples

    @staticmethod
    def _weighted_avg_rate(samples):
        """按时间加权计算日均用电量：总下降量 / 总观测天数。

        时间加权可以让跨度只有几分钟的短间隔（余额只有0.01级跳变，
        量化噪声大）在更长间隔面前几乎不产生的影响，天然抑制噪声。

        Returns:
            (日均用量, 总观测天数)
        """
        total_drop = sum(s['drop'] for s in samples)
        total_span = sum(s['span'] for s in samples)
        if total_span <= 0:
            return 0.0, 0.0
        return total_drop / total_span, total_span

    def _select_prediction_window(self, sampled_at):
        """选择预测窗口：优先最短窗口，样本不足时逐级放宽。

        动机：宿舍用电受气温/空调驱动，用电状态变化很快（如降温后停用空调）。
        用 30 天均值会把历史上的高耗时段也算进平均，高估消耗、低估可用天数
        （实测短窗口日均仅为 30 天均值的约六折）。

        候选窗口按「主窗口 → 2× → 4× → 全窗口」逐级放宽，
        取第一个满足「区间数 ≥ PREDICTION_MIN_SAMPLES 且观测时长 ≥
        PREDICTION_MIN_OBSERVED_DAYS」的窗口。

        Returns:
            (samples, window_days, used_fallback)
            samples 为空表示连最长窗口都没有可用区间；
            used_fallback=True 表示主窗口样本不足、已放宽到更长窗口。
        """
        primary = max(1, int(getattr(config, 'PREDICTION_RECENT_WINDOW_DAYS', 3)))
        lookback = max(primary, int(getattr(config, 'PREDICTION_LOOKBACK_DAYS', 30)))
        min_samples = max(2, int(getattr(config, 'PREDICTION_MIN_SAMPLES', 12)))
        min_observed = float(getattr(config, 'PREDICTION_MIN_OBSERVED_DAYS', 0.5))

        candidates = []
        for days in (primary, primary * 2, primary * 4, lookback):
            if days <= lookback and days not in candidates:
                candidates.append(days)
        candidates.sort()

        for days in candidates:
            samples = self.collect_usage_intervals(days, sampled_at=sampled_at)
            if not samples:
                continue
            total_span = sum(s['span'] for s in samples)
            if len(samples) >= min_samples and total_span >= min_observed:
                return samples, days, days != primary

        # 全部不满足：返回最长窗口的原始样本，由调用方按观测时长判定是否可用
        return self.collect_usage_intervals(lookback, sampled_at=sampled_at), lookback, True

    def predict(self, threshold=None):
        """预测剩余电量消耗节奏（统一入口，按数据成熟度自动选择精度）。

        字段语义：
        - days_remaining：预计电量**耗尽**（降到 0）的天数
        - days_to_threshold：预计降到阈值以下的天数（预测预警邮件用它触发）
        - predicted_date：从最后一次成功采样外推的预警日期
        - data_stale：距最后一次成功采样达到两个检查周期
        - window_days：本次实际使用的观测窗口天数

        窗口策略（宿舍用电主要由气温/空调驱动，星期几规律弱，不做日历分型）：
        - 优先只看最近 PREDICTION_RECENT_WINDOW_DAYS 天（默认 3 天）
        - 该窗口样本不足（停服、刚部署）时自动放宽到 7 / 12 / 全窗口
        - 窗口内部一律用简单时间加权（总消耗 ÷ 总天数），不叠加近因权重
          ——短窗口本身已经足够"新"，再加时间衰减只会多一个没人调的参数

        Args:
            threshold: 预警阈值（度），默认取 config.LOW_BALANCE_THRESHOLD

        Returns:
            dict: prediction_method 字段为 'recent'（主窗口）或
            'extended'（样本不足、已放宽窗口）
        """
        try:
            if threshold is None:
                threshold = float(config.LOW_BALANCE_THRESHOLD)
            threshold = float(threshold)

            latest = storage.latest_record()

            # 失败兜底与成功路径共用同一份字典骨架，逐字段填成功值
            result = failed_prediction('', threshold)

            if not latest or latest[2] is None:
                result['message'] = '无法获取当前剩余电量，预测失败'
                return result

            sampled_at = datetime.fromisoformat(str(latest[1]))
            now = datetime.now()
            stale_at = sampled_at + timedelta(minutes=2 * check_frequency_minutes(config))
            elapsed_days = max(0.0, (now - sampled_at).total_seconds() / 86400)
            current_balance = float(latest[2])
            result.update(
                current_balance=current_balance,
                sample_time=str(latest[1]),
                sample_stale_at=int(stale_at.timestamp() * 1000),
                data_stale=now >= stale_at,
            )

            # ── 窗口选择：优先用最近的短窗口，样本不足时自动放宽 ──
            samples, window_days, used_fallback = self._select_prediction_window(sampled_at)
            result['window_days'] = window_days

            if not samples:
                result['message'] = '历史数据不足：至少需要两条查询记录才能估计用电速率'
                return result

            overall_avg, observed_days = self._weighted_avg_rate(samples)

            if observed_days <= 0:
                result['message'] = '历史数据异常：有效观测时长为零'
                return result

            # 观测时长过短时任何速率都是噪声（例如只有几条相邻记录，只覆盖几分钟）
            min_observed = float(getattr(config, 'PREDICTION_MIN_OBSERVED_DAYS', 0.5))
            if observed_days < min_observed:
                hours = observed_days * 24
                need_hours = min_observed * 24
                result['message'] = (f'历史数据不足：目前只积累了 {hours:.1f} 小时的有效观测，'
                                     f'至少需要 {need_hours:.0f} 小时才能估计用电速率')
                return result

            result['daily_usage_avg'] = round(overall_avg, 2)
            result['data_points'] = {
                'intervals': len(samples),
                'observed_days': round(observed_days, 2),
                'window_days': window_days,
                'fallback': used_fallback,
            }

            method = 'extended' if used_fallback else 'recent'

            # 观测期内几乎没有消耗（如假期、或刚换到赠送池），如实告知而不是编一个默认值
            if overall_avg <= 0.0001:
                result.update({
                    'success': True,
                    'message': f'最近 {window_days} 天几乎没有用电消耗，无法估计耗尽时间',
                    'prediction_method': method,
                    'window_days': window_days,
                })
                return result

            remaining = current_balance - threshold

            # 窗口内部一律简单时间加权：短窗口已经足够"新"，不再叠加近因权重
            # （rate == overall_avg，且上方已排除 overall_avg <= 0.0001 的情形）
            rate = overall_avg

            # 可信度 = 窗口覆盖度；放宽过窗口说明主窗口样本不够，可信度打折
            coverage = min(observed_days / max(window_days, 1), 1.0)
            quality = coverage * (0.6 if used_fallback else 1.0)
            confidence = 'high' if quality > 0.7 else ('medium' if quality > 0.4 else 'low')

            # 从采样时刻外推日期，再扣除采样后已经过去的时间。
            days_from_sample_to_deplete = max(current_balance, 0.0) / rate
            days_from_sample_to_threshold = max(remaining, 0.0) / rate

            if days_from_sample_to_deplete > 3650:
                result.update({
                    'success': True,
                    'message': f'按日均{rate:.2f}度消耗，剩余电量一年以上才会耗尽',
                    'prediction_confidence': confidence,
                    'prediction_method': method,
                })
                return result

            predicted_date = (sampled_at + timedelta(days=days_from_sample_to_threshold)).strftime('%Y-%m-%d')
            days_to_deplete = max(0.0, days_from_sample_to_deplete - elapsed_days)
            days_to_threshold = max(0.0, days_from_sample_to_threshold - elapsed_days)

            logging.info(f"电量预测完成({method}, 窗口{window_days}天): 剩余电量={current_balance}度, "
                         f"估计日均={rate:.2f}度(基于{len(samples)}个记录间隔/共{observed_days:.2f}天), "
                         f"预计{days_to_deplete:.1f}天后电量耗尽, {days_to_threshold:.1f}天后降到{threshold}度以下")

            result.update({
                'success': True,
                'message': f'按最近{window_days}天估计日均{rate:.2f}度，预计{days_to_deplete:.1f}天后电量耗尽，'
                           f'{days_to_threshold:.1f}天后低于{threshold}度',
                'days_remaining': round(days_to_deplete, 1),
                'days_to_threshold': round(days_to_threshold, 1),
                'predicted_date': predicted_date,
                'prediction_confidence': confidence,
                'prediction_method': method,
                'window_days': window_days,
            })
            return result

        except Exception as e:
            logging.error(f"电量预测时出错: {str(e)}")
            return failed_prediction(f'预测失败: {str(e)}')


def failed_prediction(message, threshold=None):
    """构造统一形状的预测失败结果（predict 内部与 /api/prediction 兜底共用）。

    threshold 为 None 时回落到 config.LOW_BALANCE_THRESHOLD；配置损坏时
    再回落到 10.0，保证失败路径本身永不抛错。
    """
    if threshold is None:
        try:
            threshold = float(config.LOW_BALANCE_THRESHOLD)
        except (TypeError, ValueError):
            threshold = 10.0
    return {
        'success': False,
        'message': message,
        'current_balance': 0.0,
        'sample_time': None,
        'sample_stale_at': None,
        'data_stale': False,
        'threshold': threshold,
        'days_remaining': None,
        'days_to_threshold': None,
        'predicted_date': None,
        'daily_usage_avg': 0.0,
        'prediction_confidence': 'low',
        'prediction_method': 'recent',
        'window_days': None,
    }


def run_check(monitor):
    """执行一次完整的电费检查：采集 → 入库 → 按需预警（低余额 + 预测）。

    手动检查（/api/check）与定时任务共用这一条流程，避免两处重复逻辑各自漂移。

    Returns:
        dict: {'data': 采集数据, 'prediction': 预测结果}，采集失败返回 None
    """
    if not _check_lock.acquire(blocking=False):
        raise CheckInProgress
    try:
        try:
            data = monitor.get_electric_data()
            if not data:
                monitor.last_check = {
                    'at': datetime.now().isoformat(timespec='seconds'),
                    'error': getattr(monitor, 'collection_error', None) or '电费采集失败，请查看系统日志。',
                }
                return None
            storage.save_record(data)
        except Exception:
            monitor.last_check = {'at': datetime.now().isoformat(timespec='seconds'), 'error': '检查记录未能保存，请查看系统日志。'}
            raise
        monitor.last_check = {'at': datetime.now().isoformat(timespec='seconds'), 'error': None}

        threshold = float(config.LOW_BALANCE_THRESHOLD)
        balance = float(data['balance'])

        # 传统低余额预警
        if balance < threshold:
            notifier.send_low_balance_alert(balance, threshold)

        # 预测预警
        prediction = monitor.predict(threshold)

        alert_days = int(getattr(config, 'PREDICTION_ALERT_DAYS', 7))
        if (prediction.get('success') and
                not prediction.get('data_stale') and
                prediction.get('days_to_threshold') is not None and
                prediction.get('days_to_threshold') <= alert_days):
            notifier.send_prediction_alert(prediction, alert_days)

        logging.info(f"检查完成 - 剩余电量: {balance}度")
        return {'data': data, 'prediction': prediction}
    finally:
        _check_lock.release()
