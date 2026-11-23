#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web 层 - 全部 Flask 路由

create_app(monitor, reschedule, updates, next_check) 返回配置好的 Flask 应用：
- monitor: ElectricMonitor 实例（数据采集/预测/统计）
- reschedule: 配置保存后重新调度定时任务的回调（由 app.py 提供）
- updates: 服务启动与网页检查共用的软件更新状态
- next_check: 获取下一次定时采集时间的回调

约定：
- 手动检查与定时检查共用 monitor.run_check，不另写检查逻辑。
- 全系统只有一个阈值 config.LOW_BALANCE_THRESHOLD。
- /api/config POST 会整文件重写 config.py，新增配置项必须同步改
  web.py 模板、setup_config.py 模板、config.py.example 三处。
"""

import bisect
import csv
import io
import json
import logging
import math
import re
from datetime import datetime
from ipaddress import IPv6Address
from threading import Lock

import config
from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.sansio.utils import host_is_trusted

from config_io import check_frequency_minutes, loopback_host, setup_required, write_config
from log_access import clear_logs, recent_logs
from monitor import CheckInProgress, run_check, failed_prediction
from setup_config import RoomLookupError, find_room

import releases
import storage


def create_app(monitor, reschedule=None, *, updates=None, next_check=None):
    app = Flask(__name__)
    config_save_lock = Lock()
    if updates is None:
        updates = releases.SoftwareUpdates()

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
        context = _dashboard_context()
        return render_template(
            'index.html',
            **context,
            app_version=updates.current_version,
            auto_check_updates=getattr(config, 'AUTO_CHECK_UPDATES', True),
            dorm_info=None if context['setup_needed'] else _dorm_info(),
        )

    def _check_status():
        return {
            'last_check': getattr(monitor, 'last_check', None),
            'next_check': next_check() if next_check else None,
        }

    def _dashboard_context():
        revision = storage.dashboard_revision()
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
        return dict(stats=stats, records=records, revision=revision,
                    setup_needed=setup_required(config), schedule_label=_schedule_label(), **_check_status())

    # ── 数据 API ─────────────────────────────────────────────────

    @app.get('/api/dashboard')
    def api_dashboard():
        context = _dashboard_context()
        stats = context['stats']
        return jsonify(
            revision=context['revision'],
            overview=render_template('dashboard_overview.html', **context),
            records=render_template('dashboard_records.html', **context),
            hourly=stats['balance_trend_hourly'],
            daily=stats['balance_trend_daily'],
            daily_usage=stats['daily_usage'],
        )

    @app.get('/api/dashboard/status')
    def api_dashboard_status():
        return jsonify(revision=storage.dashboard_revision(), **_check_status())

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
        if request.args.get('startup') == '1':
            return jsonify(updates.check_on_launch() if getattr(config, 'AUTO_CHECK_UPDATES', True) else {})
        return jsonify(updates.check())

    @app.route('/api/update/settings', methods=['GET', 'POST'])
    def api_update_settings():
        if request.method == 'GET':
            enabled = getattr(config, 'AUTO_CHECK_UPDATES', True)
            return jsonify(enabled=enabled, latest_version=updates.available_version() if enabled else None)

        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get('enabled'), bool):
            return jsonify(message='无效的自动检查更新设置'), 400

        enabled = data['enabled']
        try:
            with config_save_lock:
                _save_web_config({'AUTO_CHECK_UPDATES': enabled}, reschedule_checks=False)
        except OSError:
            logging.exception('保存自动检查更新设置失败')
            return jsonify(message='无法保存自动检查更新设置，请检查本机配置文件权限。'), 500
        return jsonify(enabled=enabled)

    @app.route('/api/records')
    def api_records():
        """获取记录数据API"""
        limit = request.args.get('limit', 20, type=int)
        return jsonify({'records': storage.recent_records(limit)})

    @app.get('/api/records/export.csv')
    def api_export_records():
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['采样时间', '剩余电量（度）', '功率（kW）', '状态'])
        for timestamp, balance, status, raw in storage.export_records():
            label = STATUS_LABELS.get(str(status or '').strip().lower(), status)
            writer.writerow([_csv_value(value) for value in (timestamp, balance, _export_power(raw), label)])
        return send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')), mimetype='text/csv',
                         as_attachment=True, download_name=f'electric-records-{datetime.now():%Y%m%d-%H%M%S}.csv')

    @app.get('/api/records/backup')
    def api_backup_records():
        return send_file(io.BytesIO(storage.backup_database()), mimetype='application/vnd.sqlite3',
                         as_attachment=True, download_name=f'electric-data-{datetime.now():%Y%m%d-%H%M%S}.db')

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
                    'data': result['data'],
                    **_check_status(),
                })
            status = _check_status()
            return jsonify(success=False, message=(status['last_check'] or {}).get('error') or '获取数据失败', **status)
        except CheckInProgress:
            return jsonify({'success': False, 'message': '已有检查正在进行，请稍后重试'}), 409
        except Exception as e:
            logging.error(f"API检查失败: {str(e)}")
            return jsonify(success=False, message='检查失败，请查看系统日志。', **_check_status())

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

    def _save_web_config(updates, *, reschedule_checks=True):
        """用同一配置模板保存网页设置。"""
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
AUTO_CHECK_UPDATES = {values.get('AUTO_CHECK_UPDATES', True)!r}

# 预测系统配置
PREDICTION_ALERT_DAYS = {values.get('PREDICTION_ALERT_DAYS', 7)}
PREDICTION_LOOKBACK_DAYS = {values.get('PREDICTION_LOOKBACK_DAYS', 30)}
PREDICTION_MAX_DAILY_RATE = {values.get('PREDICTION_MAX_DAILY_RATE', 50.0)}
PREDICTION_RECENT_WINDOW_DAYS = {values.get('PREDICTION_RECENT_WINDOW_DAYS', 3)}
PREDICTION_MIN_SAMPLES = {values.get('PREDICTION_MIN_SAMPLES', 12)}
PREDICTION_MIN_OBSERVED_DAYS = {values.get('PREDICTION_MIN_OBSERVED_DAYS', 0.5)}
'''
        vars(config).update(write_config(config.__file__, config_content))
        if reschedule_checks and reschedule:
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
                'emails': [email for email in (getattr(config, 'ALERT_EMAILS', None) or [])
                           if email not in ('no@email.com', '你的接收邮箱@qq.com')],
                'check_frequency': check_frequency_minutes(config),
                'smtp': _smtp_settings(),
            }
            return jsonify({'success': True, 'config': config_data})
        except Exception as e:
            logging.error(f"获取配置失败: {str(e)}")
            return jsonify({'success': False, 'message': f'获取配置失败: {str(e)}'})

    @app.route('/api/config', methods=['POST'])
    def api_save_config():
        """原子保存设置；采集设置变化时重新调度。"""
        try:
            data = request.get_json()
            if not isinstance(data, dict) or not data:
                return jsonify({'success': False, 'message': '无效的配置数据'})

            section = data.get('section')
            if section not in (None, 'monitoring', 'email'):
                return jsonify({'success': False, 'message': '无效的设置分类'})
            if section == 'monitoring' and not {'threshold', 'check_frequency'} <= data.keys():
                return jsonify({'success': False, 'message': '请填写预警阈值和检查频率'})
            if section == 'email' and not {'emails', 'smtp'} <= data.keys():
                return jsonify({'success': False, 'message': '请提供完整的邮箱设置'})
            save_monitoring = section != 'email'
            save_email = section != 'monitoring'

            updates = {}
            try:
                if save_monitoring:
                    updates.update(_monitoring_updates(data))
                if save_email:
                    updates['ALERT_EMAILS'] = _validated_recipients(data.get('emails', []))
            except ValueError as exc:
                return jsonify({'success': False, 'message': str(exc)})

            with config_save_lock:
                if setup_required(config):
                    return jsonify({'success': False, 'message': '请先完成宿舍设置'})
                if save_email and 'smtp' in data:
                    try:
                        updates.update(_smtp_updates(data['smtp'], updates['ALERT_EMAILS']))
                    except ValueError as exc:
                        return jsonify({'success': False, 'message': str(exc)})
                _save_web_config(updates, reschedule_checks=save_monitoring)

            logging.info('配置已更新：%s', section or '采集与邮件')
            return jsonify({'success': True, 'message': '配置保存成功'})

        except Exception as e:
            logging.error(f"保存配置失败: {str(e)}")
            return jsonify({'success': False, 'message': f'保存失败: {str(e)}'})

    # ── 日志 API ─────────────────────────────────────────────────

    @app.get('/api/logs')
    def api_get_logs():
        """从当前日志及轮转文件倒序读取指定条数。"""
        limit = max(1, min(500, request.args.get('limit', 50, type=int)))
        level = request.args.get('level', 'all')
        try:
            return jsonify(success=True, logs=recent_logs(limit, level))
        except OSError:
            logging.exception('获取日志失败')
            return jsonify(success=False, message='无法读取本机日志。')

    @app.delete('/api/logs')
    def api_clear_logs():
        try:
            clear_logs()
            logging.info('系统日志已被用户清空')
            return jsonify(success=True, message='日志已清空')
        except OSError:
            logging.exception('清空日志失败')
            return jsonify(success=False, message='无法清空本机日志。')

    # ── 内部辅助 ─────────────────────────────────────────────────

    def _schedule_label():
        if setup_required(config):
            return "待设置宿舍"
        frequency = check_frequency_minutes(config)
        return f"每{frequency}分钟检查" if frequency != 60 else "每小时整点检查"

    return app


EMAIL_PATTERN = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
SMTP_HOST_LABEL = r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?'
SMTP_HOST_PATTERN = rf'(?:{SMTP_HOST_LABEL}\.)*{SMTP_HOST_LABEL}\.?'


def _csv_value(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def _export_power(raw):
    try:
        power = float(json.loads(raw)['d']['data']['pTotal'])
        return power if math.isfinite(power) else None
    except (TypeError, ValueError, KeyError, OverflowError):
        return None


def _dorm_info():
    campus = {'1': '西土城校区', '2': '沙河校区'}
    apartment = str(getattr(config, 'EXPECTED_APARTMENT_NAME', '') or '').strip()
    if apartment in ('', '你的公寓名'):
        apartment_id = str(getattr(config, 'APARTMENT_ID', '')).strip()
        # 部分旧配置直接用公寓中文名作为查询参数。
        apartment = apartment_id if re.search(r'[\u3400-\u9fff]', apartment_id) else '未记录'
    return {
        'campus': campus.get(str(config.AREA_ID), '未记录'),
        'apartment': apartment,
        'floor': str(config.FLOOR_ID).strip(),
        'room': str(config.ROOM_NUMBER).strip(),
    }


def _monitoring_updates(data):
    try:
        threshold = float(data.get('threshold', 10.0))
    except (TypeError, ValueError, OverflowError):
        raise ValueError('无效的预警阈值') from None
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError('预警阈值必须是不小于 0 的数字')

    try:
        frequency = int(data.get('check_frequency', 60))
    except (TypeError, ValueError, OverflowError):
        raise ValueError('无效的检查频率') from None
    if not 5 <= frequency <= 1440:
        raise ValueError('检查频率需在 5～1440 分钟之间')

    return {'LOW_BALANCE_THRESHOLD': threshold, 'CHECK_FREQUENCY_MINUTES': frequency}


def _validated_recipients(emails):
    if not isinstance(emails, list):
        raise ValueError('无效的提醒邮箱列表')
    addresses = [str(email).strip() for email in emails]
    valid = [email for email in addresses if re.match(EMAIL_PATTERN, email)]
    if emails and not valid:
        raise ValueError('请填写有效的邮箱地址')
    return valid


def _smtp_settings():
    username = getattr(config, 'EMAIL_USERNAME', '')
    password = getattr(config, 'EMAIL_PASSWORD', '')
    return {
        'username': '' if username in ('no@email.com', '你的邮箱@qq.com') else username,
        'server': getattr(config, 'EMAIL_SMTP_SERVER', 'smtp.qq.com'),
        'port': getattr(config, 'EMAIL_SMTP_PORT', 587),
        'has_password': isinstance(password, str) and password.strip() not in ('', 'no@email.com', '你的邮箱授权码'),
    }


def _smtp_updates(data, recipients):
    if not isinstance(data, dict) or any(not isinstance(data.get(key), str) for key in ('username', 'server')):
        raise ValueError('无效的邮箱提醒配置')

    username, server = data['username'].strip(), data['server'].strip()
    port, password = data.get('port'), data.get('password', '')
    if not isinstance(password, str):
        raise ValueError('无效的 SMTP 授权码')
    if username and (username == 'no@email.com' or not re.fullmatch(EMAIL_PATTERN, username)):
        raise ValueError('请填写有效的发件邮箱地址')
    if len(server) > 253 or not re.fullmatch(SMTP_HOST_PATTERN, server):
        try:
            IPv6Address(server)
        except ValueError:
            raise ValueError('请填写有效的 SMTP 服务器主机名或 IP 地址') from None
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('SMTP 端口需在 1～65535 之间')

    if not password.strip():
        saved = _smtp_settings()
        same_account = username == saved['username'] and server.lower() == str(saved['server']).lower()
        password = config.EMAIL_PASSWORD if same_account and saved['has_password'] else ''
    if recipients and (not username or not password):
        raise ValueError('启用邮箱提醒需填写发件邮箱和 SMTP 授权码')

    return {
        'EMAIL_USERNAME': username,
        'EMAIL_PASSWORD': password,
        'EMAIL_SMTP_SERVER': server,
        'EMAIL_SMTP_PORT': port,
    }


# 记录状态的中文显示（库内存英文枚举，仅渲染时翻译；未收录的值原样显示）
STATUS_LABELS = {
    'success': '成功',
    'failed': '失败',
    'fail': '失败',
    'error': '出错',
    'pending': '等待中',
}
