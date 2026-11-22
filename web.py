#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web 层 - 全部 Flask 路由

create_app(monitor, reschedule) 返回配置好的 Flask 应用：
- monitor: ElectricMonitor 实例（数据采集/预测/统计）
- reschedule: 配置保存后重新调度定时任务的回调（由 app.py 提供）

约定：
- 手动检查与定时检查共用 monitor.run_check，不另写检查逻辑。
- 全系统只有一个阈值 config.LOW_BALANCE_THRESHOLD。
- /api/config POST 会整文件重写 config.py，新增配置项必须同步改
  web.py 模板、setup_config.py 模板、config.py.example 三处。
"""

import bisect
import json
import logging
import math
import os
import re
from datetime import datetime
from threading import Lock

import config
from flask import Flask, abort, jsonify, render_template, request
from werkzeug.sansio.utils import host_is_trusted

from config_io import check_frequency_minutes, loopback_host, setup_required, write_config
from monitor import CheckInProgress, run_check, failed_prediction
from setup_config import RoomLookupError, find_room

import releases
import storage


def create_app(monitor, reschedule=None):
    app = Flask(__name__)
    config_save_lock = Lock()
    app_version = releases.read_version()

    @app.before_request
    def require_local_host():
        # 避免浏览器经非本机域名访问回环地址上的管理接口。
        if not host_is_trusted(request.host, ('localhost', '127.0.0.1', '[::1]')):
            abort(400)

    # 模板过滤器：把列表/字典序列化为 JSON（index.html 的图表数据用）
    @app.template_filter('tojsonfilter')
    def to_json_filter(value):
        return json.dumps(value, ensure_ascii=False)

    # 模板过滤器：把数据库里的记录状态翻译成中文。
    # 库里存的是 'success' 这类英文枚举（历史 480+ 条，不动数据），只在渲染层翻译。
    @app.template_filter('status_label')
    def status_label(value):
        return STATUS_LABELS.get(str(value).strip().lower(), str(value or '未知'))

    # ── 页面 ─────────────────────────────────────────────────────

    @app.route('/')
    def index():
        """主页"""
        stats = monitor.get_statistics()
        # 按与用量卡片相同的自然日口径分摊，一次切区间，按时间戳查表。
        month_start = datetime.now().strftime('%Y-%m-01 00:00:00')
        curve_ends, curve_cum = monitor.usage_curve_since(month_start)
        records = []
        for r in storage.recent_records(10):
            r = list(r)
            record_time = str(r[1])
            if record_time >= month_start:
                idx = bisect.bisect_right(curve_ends, record_time) - 1
                r[4] = curve_cum[idx] if idx >= 0 else 0.0
            records.append(r)
        return render_template(
            'index.html',
            stats=stats,
            records=records,
            app_version=app_version,
            setup_needed=setup_required(config),
            schedule_label=_schedule_label()
        )

    # ── 数据 API ─────────────────────────────────────────────────

    @app.route('/api/stats')
    def api_stats():
        """获取统计数据API"""
        return jsonify(monitor.get_statistics())

    @app.route('/api/charts/history')
    def api_chart_history():
        """获取电量趋势和每日用量的全量日序列。"""
        return jsonify(
            balance_trend_daily=storage.balance_trend("date(timestamp)", drop_early_tail=True),
            daily_usage=monitor.daily_usage_trend(None),
        )

    @app.route('/api/version')
    def api_version():
        """轻量版本探针：仅返回最新记录时间戳，供前端判断是否有新数据"""
        try:
            return jsonify({'version': storage.max_record_timestamp()})
        except Exception:
            return jsonify({'version': None})

    @app.route('/api/update')
    def api_update():
        """按需查询软件正式发布。"""
        return jsonify(releases.check_for_update(app_version))

    @app.route('/api/records')
    def api_records():
        """获取记录数据API"""
        limit = request.args.get('limit', 20, type=int)
        return jsonify({'records': storage.recent_records(limit)})

    @app.route('/api/check', methods=['POST'])
    def api_check():
        """立即检查电费API（与定时任务共用同一条检查流程）"""
        if setup_required(config):
            return jsonify({'success': False, 'message': '请先完成宿舍设置'})
        try:
            result = run_check(monitor)
            if result:
                return jsonify({
                    'success': True,
                    'message': '检查完成',
                    'data': result['data']
                })
            return jsonify({'success': False, 'message': '获取数据失败'})
        except CheckInProgress:
            return jsonify({'success': False, 'message': '已有检查正在进行，请稍后重试'}), 409
        except Exception as e:
            logging.error(f"API检查失败: {str(e)}")
            return jsonify({'success': False, 'message': f'检查失败: {str(e)}'})

    @app.route('/api/records', methods=['DELETE'])
    def api_clear_records():
        """清空所有记录API"""
        try:
            storage.clear_all_records()
            logging.info("用户清空了所有记录")
            return jsonify({'success': True, 'message': '记录已清空'})
        except Exception as e:
            logging.error(f"清空记录失败: {str(e)}")
            return jsonify({'success': False, 'message': f'清空失败: {str(e)}'})

    @app.route('/api/records/<int:record_id>', methods=['DELETE'])
    def api_delete_record(record_id):
        """删除单条记录API"""
        try:
            if storage.delete_record(record_id):
                logging.info(f"用户删除了记录ID: {record_id}")
                return jsonify({'success': True, 'message': '记录已删除'})
            return jsonify({'success': False, 'message': '记录不存在'})
        except Exception as e:
            logging.error(f"删除记录失败: {str(e)}")
            return jsonify({'success': False, 'message': f'删除失败: {str(e)}'})

    # ── 预测 API ─────────────────────────────────────────────────

    @app.route('/api/prediction', methods=['GET'])
    def api_get_prediction():
        """获取余额预测API（threshold 参数可选，默认使用预警阈值）。

        只重算不写库：预测结果按当前数据即时计算，页面反复调用也没有副作用。
        """
        try:
            threshold = float(request.args.get('threshold', config.LOW_BALANCE_THRESHOLD))
            prediction = monitor.predict(threshold)
            return jsonify(prediction)
        except Exception as e:
            logging.error(f"获取预测失败: {str(e)}")
            # 与 monitor.predict 的失败兜底共用同一份默认字典（阈值取 config 而非旧的硬编码 10.0）
            return jsonify(failed_prediction(f'获取预测失败: {str(e)}'))

    # ── 配置 API ─────────────────────────────────────────────────

    def _save_web_config(updates):
        """用同一配置模板保存首次设置和提醒设置。"""
        values = vars(config) | updates
        config_content = f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电费自动提醒系统配置文件
"""

# 北邮统一身份认证账号
BUPT_USERNAME = {values.get('BUPT_USERNAME', '')!r}
BUPT_PASSWORD = {values.get('BUPT_PASSWORD', '')!r}

# 宿舍信息（缺失时程序会查询错误的默认房间！）
AREA_ID = {values.get('AREA_ID', 1)}
APARTMENT_ID = {values.get('APARTMENT_ID', '')!r}
FLOOR_ID = {values.get('FLOOR_ID', '')!r}
ROOM_NUMBER = {values.get('ROOM_NUMBER', '')!r}
EXPECTED_APARTMENT_NAME = {values.get('EXPECTED_APARTMENT_NAME', '')!r}

# 邮箱配置
EMAIL_USERNAME = {values.get('EMAIL_USERNAME', '')!r}
EMAIL_PASSWORD = {values.get('EMAIL_PASSWORD', '')!r}
EMAIL_SMTP_SERVER = {values.get('EMAIL_SMTP_SERVER', 'smtp.qq.com')!r}
EMAIL_SMTP_PORT = {values.get('EMAIL_SMTP_PORT', 587)}

# 提醒邮箱列表
ALERT_EMAILS = {values.get('ALERT_EMAILS', [])!r}

# 预警阈值（度）：低于此值发邮件，页面显示/横幅/预测预警均使用此值
LOW_BALANCE_THRESHOLD = {values.get('LOW_BALANCE_THRESHOLD', 10.0)!r}

# 检查频率（分钟）：设置后按此间隔检查；删除此行则每小时整点检查
CHECK_FREQUENCY_MINUTES = {values.get('CHECK_FREQUENCY_MINUTES', check_frequency_minutes(config))!r}

# Web服务配置
WEB_HOST = {loopback_host(values.get('WEB_HOST'))!r}
WEB_PORT = {values.get('WEB_PORT', 5100)}
DEBUG_MODE = {values.get('DEBUG_MODE', False)}

# 预测系统配置
PREDICTION_ALERT_DAYS = {values.get('PREDICTION_ALERT_DAYS', 7)}
PREDICTION_LOOKBACK_DAYS = {values.get('PREDICTION_LOOKBACK_DAYS', 30)}
PREDICTION_MAX_DAILY_RATE = {values.get('PREDICTION_MAX_DAILY_RATE', 50.0)}
PREDICTION_RECENT_WINDOW_DAYS = {values.get('PREDICTION_RECENT_WINDOW_DAYS', 3)}
PREDICTION_MIN_SAMPLES = {values.get('PREDICTION_MIN_SAMPLES', 12)}
PREDICTION_MIN_OBSERVED_DAYS = {values.get('PREDICTION_MIN_OBSERVED_DAYS', 0.5)}
'''
        vars(config).update(write_config(config.__file__, config_content))
        if reschedule:
            reschedule()

    @app.route('/api/setup', methods=['POST'])
    def api_setup():
        data = request.get_json(silent=True)
        fields = ('username', 'password', 'apartment', 'room')
        if not isinstance(data, dict) or any(
            not isinstance(data.get(field), str) or not data[field].strip() for field in fields
        ):
            return jsonify({'success': False, 'message': '请填写学号、密码、公寓名称和房间号'})

        with config_save_lock:
            if not setup_required(config):
                return jsonify({'success': False, 'message': '宿舍设置已完成'})
            try:
                room = find_room(
                    data['username'].strip(), data['password'], '1', data['room'],
                    apartment=data['apartment'],
                )
                updates = {
                    **room,
                    'BUPT_USERNAME': data['username'].strip(),
                    'BUPT_PASSWORD': data['password'],
                    'DEBUG_MODE': False,
                }
                if getattr(config, 'EMAIL_USERNAME', '') in ('', '你的邮箱@qq.com'):
                    updates.update(EMAIL_USERNAME='', EMAIL_PASSWORD='', ALERT_EMAILS=[])
                _save_web_config(updates)
            except RoomLookupError as exc:
                return jsonify({'success': False, 'message': str(exc)})
            except Exception:
                logging.error('首次宿舍设置保存失败')
                return jsonify({'success': False, 'message': '无法保存宿舍设置，请检查本机配置文件权限'})
        return jsonify({'success': True})

    @app.route('/api/config', methods=['GET'])
    def api_get_config():
        """获取系统配置API"""
        try:
            config_data = {
                'threshold': getattr(config, 'LOW_BALANCE_THRESHOLD', 10.0),
                'emails': getattr(config, 'ALERT_EMAILS', None) or [],
                'check_frequency': check_frequency_minutes(config)
            }
            return jsonify({'success': True, 'config': config_data})
        except Exception as e:
            logging.error(f"获取配置失败: {str(e)}")
            return jsonify({'success': False, 'message': f'获取配置失败: {str(e)}'})

    @app.route('/api/config', methods=['POST'])
    def api_save_config():
        """保存系统配置API：重新生成 config.py 并即时重新调度定时任务"""
        try:
            data = request.get_json()
            if not data:
                return jsonify({'success': False, 'message': '无效的配置数据'})

            # 输入健壮性：设置表单带 novalidate，浏览器不拦截非法值；
            # 清空阈值输入时 JS 的 parseFloat 会产出 NaN → JSON null，
            # 直接落盘会写成 LOW_BALANCE_THRESHOLD = None，之后所有检查与页面全部崩溃。
            try:
                threshold = float(data.get('threshold', 10.0))
            except (TypeError, ValueError):
                return jsonify({'success': False, 'message': '无效的预警阈值'})
            if not math.isfinite(threshold) or threshold < 0:
                return jsonify({'success': False, 'message': '预警阈值必须是不小于 0 的数字'})

            try:
                check_frequency = int(data.get('check_frequency', 60))
            except (TypeError, ValueError):
                return jsonify({'success': False, 'message': '无效的检查频率'})
            # 与设置弹窗的 min/max 一致；负值会让 APScheduler 在下次启动时崩溃
            if not (5 <= check_frequency <= 1440):
                return jsonify({'success': False, 'message': '检查频率需在 5～1440 分钟之间'})

            emails = data.get('emails', [])
            if not isinstance(emails, list):
                return jsonify({'success': False, 'message': '无效的提醒邮箱列表'})

            # 验证邮箱格式
            valid_emails = []
            for email in emails:
                email = str(email).strip()
                if email and re.match(EMAIL_PATTERN, email):
                    valid_emails.append(email)

            if emails and not valid_emails:
                return jsonify({'success': False, 'message': '请填写有效的邮箱地址'})

            with config_save_lock:
                if setup_required(config):
                    return jsonify({'success': False, 'message': '请先完成宿舍设置'})
                _save_web_config({
                    'LOW_BALANCE_THRESHOLD': threshold,
                    'CHECK_FREQUENCY_MINUTES': check_frequency,
                    'ALERT_EMAILS': valid_emails,
                })

            logging.info(f"配置已更新 - 阈值: {threshold}度, 邮箱数量: {len(valid_emails)}, 检查频率: {check_frequency}分钟")

            return jsonify({'success': True, 'message': '配置保存成功'})

        except Exception as e:
            logging.error(f"保存配置失败: {str(e)}")
            return jsonify({'success': False, 'message': f'保存失败: {str(e)}'})

    # ── 日志 API ─────────────────────────────────────────────────

    @app.route('/api/logs', methods=['GET'])
    def api_get_logs():
        """获取系统日志API"""
        try:
            limit = request.args.get('limit', 50, type=int)
            level = request.args.get('level', 'all')

            log_file_path = 'electric_monitor.log'
            logs = []

            if os.path.exists(log_file_path):
                # 日志文件可能混入历史遗留的非 UTF-8 行（如 GBK 控制台写入），
                # 严格解码会让整个日志面板崩溃，用 errors='replace' 容错
                with open(log_file_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()

                # 倒序读取，最新的在前
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue

                    # 解析日志格式: 2025-06-01 13:51:03,514 - INFO - 消息内容
                    match = re.match(LOG_PATTERN, line)
                    if not match:
                        continue

                    timestamp, log_level, message = match.groups()

                    # 根据级别过滤
                    if level != 'all' and log_level != level:
                        continue

                    logs.append({
                        'timestamp': timestamp,
                        'level': log_level,
                        'message': message
                    })

                    if len(logs) >= limit:
                        break

            return jsonify({'success': True, 'logs': logs})

        except Exception as e:
            logging.error(f"获取日志失败: {str(e)}")
            return jsonify({'success': False, 'message': f'获取日志失败: {str(e)}'})

    @app.route('/api/logs', methods=['DELETE'])
    def api_clear_logs():
        """清空系统日志API"""
        try:
            log_file_path = 'electric_monitor.log'
            if os.path.exists(log_file_path):
                with open(log_file_path, 'w', encoding='utf-8') as f:
                    f.write('')

            logging.info("系统日志已被用户清空")
            return jsonify({'success': True, 'message': '日志已清空'})
        except Exception as e:
            logging.error(f"清空日志失败: {str(e)}")
            return jsonify({'success': False, 'message': f'清空日志失败: {str(e)}'})

    # ── 内部辅助 ─────────────────────────────────────────────────

    def _schedule_label():
        if setup_required(config):
            return "待设置宿舍"
        frequency = check_frequency_minutes(config)
        return f"每{frequency}分钟检查" if frequency != 60 else "每小时整点检查"

    return app


EMAIL_PATTERN = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
LOG_PATTERN = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - (\w+) - (.+)'

# 记录状态的中文显示（库内存英文枚举，仅渲染时翻译；未收录的值原样显示）
STATUS_LABELS = {
    'success': '成功',
    'failed': '失败',
    'fail': '失败',
    'error': '出错',
    'pending': '等待中',
}
