#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
监控核心模块 - 登录认证、电费数据采集、用电预测、统计汇总

数据读写一律通过 storage 模块；阈值统一使用 config.LOW_BALANCE_THRESHOLD
（旧版的双重阈值配置 PREDICTION_THRESHOLD 已废弃，不再读取）。
"""

import json
import logging
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

import config
import notifier
import storage

ELECTRIC_PAGE_URL = "https://app.bupt.edu.cn/buptdf/wap/default/chong"
ELECTRIC_SEARCH_URL = "https://app.bupt.edu.cn/buptdf/wap/default/search"
LOGIN_URL = "https://auth.bupt.edu.cn/authserver/login"

# 校园电费充值固定档位（度）。充值后到下次查询之间仍在用电，
# 观测到的余额增量会略小于实际购电量，故按"向上取最近档位"还原真实充值额。
RECHARGE_TIERS = (25, 50, 75, 100, 150, 200)


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

    def login_bupt(self):
        """登录北邮统一身份认证"""
        try:
            response = self.session.get(LOGIN_URL, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')

            # 提取登录页必要的隐藏字段
            type_input = soup.find('input', {'name': 'type'})
            execution_input = soup.find('input', {'name': 'execution'})
            _eventId_input = soup.find('input', {'name': '_eventId'})

            if not type_input or not execution_input or not _eventId_input:
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

            if 'CAS Login' in response.text or '统一身份认证' in response.text:
                logging.error("登录失败，请检查用户名和密码")
                return False

            logging.info("登录成功")
            return True

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
        try:
            # 先尝试直接访问电费查询页面，检查是否需要登录
            response = self.session.get(ELECTRIC_PAGE_URL, timeout=10, allow_redirects=True)

            logging.info(f"访问电费页面: 状态码={response.status_code}, URL={response.url}")

            # 检查是否被重定向到登录页面
            if self.check_login_needed(response):
                logging.info("需要登录，开始登录流程...")
                if not self.login_bupt():
                    logging.error("登录失败")
                    return None

                # 重新访问电费页面
                response = self.session.get(ELECTRIC_PAGE_URL, timeout=10)
                if response.status_code != 200:
                    logging.error(f"登录后访问电费页面失败，状态码: {response.status_code}")
                    return None
                logging.info("登录成功，重新访问电费页面成功")
            else:
                logging.info("无需登录，直接查询电费数据")

            if response.status_code != 200:
                logging.error(f"访问电费页面失败，状态码: {response.status_code}")
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
                return None

            # 解析JSON响应
            try:
                electric_data = response.json()
                logging.info(f"收到电费数据响应: {electric_data}")

                if electric_data.get('e') != 0:
                    logging.error(f"电费查询失败: {electric_data.get('m', '未知错误')}")
                    return None

                # 提取数据
                raw_data = electric_data.get('d', {}).get('data', {})

                # 校验公寓名，防止接口偶发返回其他房间的数据污染记录
                expected_name = getattr(config, 'EXPECTED_APARTMENT_NAME', '')
                returned_name = str(raw_data.get('parName', ''))
                if expected_name and returned_name != expected_name:
                    logging.error(f"接口返回的公寓名不匹配: 期望[{expected_name}], 实际[{returned_name}]，本次数据不保存")
                    return None

                # 学校赠送池（freeEnd）优先扣费：真实可用电量 = 付费余额 + 赠送余量
                free_pool = float(raw_data.get('freeEnd') or 0)
                data = {
                    'balance': float(raw_data.get('surplus', 0)) + free_pool,  # 总可用电量(度)
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
                return None
            except (ValueError, KeyError) as e:
                logging.error(f"电费数据解析错误: {e}")
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

        # 近7天日均用电量（度/天）：按实际观测时长折算，记录不足7天时按已有观测计算
        usage_samples = self.collect_usage_intervals(7)
        usage_avg_7d = None
        observed_days_7d = 0.0
        if usage_samples:
            total_drop = sum(s['drop'] for s in usage_samples)
            total_span = sum(s['span'] for s in usage_samples)
            if total_span > 0:
                usage_avg_7d = round(total_drop / total_span, 2)
                observed_days_7d = round(min(total_span, 7.0), 2)

        # 本月用量：实时按区间采样累加（充值还原后数值正确），无数据时回退到存量记录值
        month_usage = self.usage_since(datetime.now().strftime('%Y-%m-01 00:00:00'))
        if month_usage is None:
            month_usage = storage.latest_month_usage()

        # 余额趋势：24小时按小时、30天按天
        # （曾有一个 12 个月的 monthly 序列，前端从未使用，已删除）
        balance_trend_hourly = storage.balance_trend(
            "strftime('%Y-%m-%d %H', timestamp)",
            "strftime('%Y-%m-%d %H:00:00', timestamp)",
            '-24 hours')
        balance_trend_daily = storage.balance_trend(
            "date(timestamp)",
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
            'usage_avg_7d': usage_avg_7d if usage_avg_7d is not None else 0.0,
            'observed_days_7d': observed_days_7d,
            'last_recharge': last_recharge,
            'month_usage': month_usage if month_usage is not None else 0.0,
            'balance_trend_hourly': balance_trend_hourly,
            'balance_trend_daily': balance_trend_daily,
            'daily_usage': daily_usage,
            'prediction': prediction
        }

    def collect_usage_intervals(self, lookback_days=30):
        """读取最近 lookback_days 天的历史记录并切分成用电样本"""
        cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d %H:%M:%S')
        return self._samples_since(cutoff)

    def daily_usage_trend(self, days=30):
        """最近 N 天的每日用电量序列，[[label, 度], ...] 按日期升序。

        区间用电按时间占比线性分摊到所跨越的每个自然日：
        - 记录密集的时段（每30分钟一条）得到精确值
        - 服务停跑等数据空档，由跨越空档的区间按天数线性均摊（即用前后数据估算）
        - 消耗口径为双池拆分（见 _pool_intervals），赠送期间用量同样正确
        """
        # 多取一天的数据，保证窗口首日的用量完整（跨零点的区间能对上）
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d 00:00:00')
        start_label = (datetime.now() - timedelta(days=days - 1)).strftime('%Y-%m-%d')

        alloc = {}
        for start, end, span, drop in self._pool_intervals(cutoff):
            span_seconds = span * 86400.0
            # 按各自然日占该区间的时长比例线性分摊
            d = start.date()
            while d <= end.date():
                seg_start = max(start, datetime.combine(d, datetime.min.time()))
                seg_end = min(end, datetime.combine(d + timedelta(days=1), datetime.min.time()))
                seg = (seg_end - seg_start).total_seconds()
                if seg > 0:
                    key = d.strftime('%Y-%m-%d')
                    alloc[key] = alloc.get(key, 0.0) + drop * seg / span_seconds
                d += timedelta(days=1)
        return [[k, round(v, 2)] for k, v in sorted(alloc.items()) if k >= start_label]

    def usage_since(self, since_str, until_str=None):
        """计算自 since_str（可选到 until_str）以来的用电量（度）。

        复用区间采样（含充值还原与异常过滤），返回 None 表示窗口内没有数据。
        """
        samples = self._samples_since(since_str, until_str)
        if not samples:
            return None
        return round(sum(s['drop'] for s in samples), 2)

    def _pool_intervals(self, cutoff_str, until_str=None):
        """相邻记录对的用电区间：(start, end, span天, 消耗度数)。

        学校赠送池（freeEnd）优先扣费，消耗按两个池子分别计算再相加：
        - 付费池下降 = 付费消耗；付费池上升 = 充值，按固定档位还原后计入
        - 赠送池下降 = 赠送消耗；赠送池上升 = 学校到账，该区间赠送消耗记 0（误差<1度/区间）
        - 折算日均超过 PREDICTION_MAX_DAILY_RATE 的区间视为异常丢弃
        """
        try:
            rows = storage.balances_with_raw_since(cutoff_str)
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

    def _select_prediction_window(self):
        """选择预测窗口：优先最短窗口，样本不足时逐级放宽。

        动机：宿舍用电受气温/空调驱动，用电状态变化很快（如降温后停用空调）。
        用 30 天均值会把历史上的高耗时段也算进平均，高估消耗、低估可用天数。
        实测：近 3 天日均 5.16 度，30 天均值 10.44 度，差一倍。

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
            samples = self.collect_usage_intervals(days)
            if not samples:
                continue
            total_span = sum(s['span'] for s in samples)
            if len(samples) >= min_samples and total_span >= min_observed:
                return samples, days, days != primary

        # 全部不满足：返回最长窗口的原始样本，由调用方按观测时长判定是否可用
        return self.collect_usage_intervals(lookback), lookback, True

    def predict(self, threshold=None):
        """预测剩余电量消耗节奏（统一入口，按数据成熟度自动选择精度）。

        字段语义：
        - days_remaining：预计电量**耗尽**（降到 0）的天数
        - days_to_threshold：预计降到阈值以下的天数（预测预警邮件用它触发）
        - predicted_date：预计降到阈值以下的日期（页面"预计到达预警线"）
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
            'extended'（放宽后的窗口），供准确率评估分组使用
        """
        try:
            if threshold is None:
                threshold = float(config.LOW_BALANCE_THRESHOLD)
            threshold = float(threshold)

            latest = storage.latest_record()

            result = {
                'success': False,
                'message': '',
                'current_balance': 0.0,
                'threshold': threshold,
                'days_remaining': None,
                'days_to_threshold': None,
                'predicted_date': None,
                'daily_usage_avg': 0.0,
                'prediction_confidence': 'low',
                'prediction_method': 'recent',
                'window_days': None,
            }

            if not latest or latest[2] is None:
                result['message'] = '无法获取当前剩余电量，预测失败'
                return result

            current_balance = float(latest[2])
            result['current_balance'] = current_balance

            # 已低于阈值时预警线日期就是今天，但耗尽时间仍需按历史速率估计
            already_below = current_balance <= threshold

            # ── 窗口选择：优先用最近的短窗口，样本不足时自动放宽 ──
            samples, window_days, used_fallback = self._select_prediction_window()
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
            rate = overall_avg

            # 可信度 = 窗口覆盖度；放宽过窗口说明主窗口样本不够，可信度打折
            coverage = min(observed_days / max(window_days, 1), 1.0)
            quality = coverage * (0.6 if used_fallback else 1.0)
            confidence = 'high' if quality > 0.7 else ('medium' if quality > 0.4 else 'low')
            result['daily_usage_avg'] = round(rate, 2)

            # 近期几乎没有消耗（如降温关空调后），换挡后的速率同样如实报告
            if rate <= 0.0001:
                result.update({
                    'success': True,
                    'message': '近期几乎没有用电消耗，无法估计耗尽时间',
                    'prediction_confidence': confidence,
                    'prediction_method': method,
                })
                return result

            # 线性外推：耗尽时间 = 剩余电量 ÷ 估计日均；已低于阈值时预警线日期就是今天
            days_to_deplete = current_balance / rate
            days_to_threshold = 0.0 if already_below else max(remaining, 0.0) / rate

            if days_to_deplete > 3650:
                result.update({
                    'success': True,
                    'message': f'按日均{rate:.2f}度消耗，剩余电量一年以上才会耗尽',
                    'prediction_confidence': confidence,
                    'prediction_method': method,
                })
                return result

            predicted_date = (datetime.now() + timedelta(days=int(days_to_threshold))).strftime('%Y-%m-%d')

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
            if threshold is None:
                threshold = float(getattr(config, 'LOW_BALANCE_THRESHOLD', 10.0))
            return {
                'success': False,
                'message': f'预测失败: {str(e)}',
                'current_balance': 0.0,
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
    """执行一次完整的电费检查：采集 → 入库 → 按需预警 → 预测记录与评估。

    手动检查（/api/check）与定时任务共用这一条流程，避免两处重复逻辑各自漂移。

    Returns:
        dict: {'data': 采集数据, 'prediction': 预测结果}，采集失败返回 None
    """
    data = monitor.get_electric_data()
    if not data:
        return None

    storage.save_record(data)

    threshold = float(config.LOW_BALANCE_THRESHOLD)
    balance = float(data['balance'])

    # 传统低余额预警
    if balance < threshold:
        notifier.send_low_balance_alert(balance, threshold)

    # 预测预警与准确率评估
    prediction = monitor.predict(threshold)
    evaluate_enabled = getattr(config, 'PREDICTION_ACCURACY_EVALUATION', True)
    if evaluate_enabled and prediction.get('success'):
        storage.save_prediction_record(prediction)

    alert_days = int(getattr(config, 'PREDICTION_ALERT_DAYS', 7))
    if (prediction.get('success') and
            prediction.get('days_to_threshold') is not None and
            prediction.get('days_to_threshold') <= alert_days):
        notifier.send_prediction_alert(prediction, alert_days)

    if evaluate_enabled:
        storage.evaluate_prediction_accuracy()

    logging.info(f"检查完成 - 剩余电量: {balance}度")
    return {'data': data, 'prediction': prediction}
