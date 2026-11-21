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
import importlib
import json
import logging
import math
import os
import re
import shutil
from datetime import datetime

import config
from flask import Flask, jsonify, render_template, request
from monitor import run_check, failed_prediction

import storage


def create_app(monitor, reschedule=None):
    app = Flask(__name__)

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
        # 表格里的"本月用量"按完整历史实时重算（存量记录里充值后的行存的是NULL）。
        # 区间只切一次得到"累计用量曲线"，再按每条记录的时间戳查表——
        # 旧实现逐条调用 usage_since(until)，每行都重读整月历史并重新解析全部
        # raw_data（10 次全量扫描），首页渲染耗时约为现在的两倍。
        # 等价性前提：库内时间戳统一为 19 位 'YYYY-MM-DD HH:MM:SS'（已穷举核实
        # 全表 0 例外），字典序即时间序，"曲线终点 <= 记录时间"的累计值与
        # "逐条 usage_since(month_start, until=记录时间)"严格等价。
        month_start = datetime.now().strftime('%Y-%m-01 00:00:00')
        curve_ends, curve_cum = monitor.usage_curve_since(month_start)
        records = []
        for r in storage.recent_records(10):
            r = list(r)
            idx = bisect.bisect_right(curve_ends, str(r[1])) - 1
            if idx >= 0:
                r[4] = round(curve_cum[idx], 2)
            records.append(r)
        return render_template(
            'index.html',
            stats=stats,
            records=records,
            schedule_label=_schedule_label()
        )

    # ── 数据 API ─────────────────────────────────────────────────

    @app.route('/api/stats')
    def api_stats():
        """获取统计数据API"""
        return jsonify(monitor.get_statistics())

    @app.route('/api/version')
    def api_version():
        """轻量版本探针：仅返回最新记录时间戳，供前端判断是否有新数据"""
        try:
            return jsonify({'version': storage.max_record_timestamp()})
        except Exception:
            return jsonify({'version': None})

    @app.route('/api/records')
    def api_records():
        """获取记录数据API"""
        limit = request.args.get('limit', 20, type=int)
        return jsonify({'records': storage.recent_records(limit)})

    @app.route('/api/check', methods=['POST'])
    def api_check():
        """立即检查电费API（与定时任务共用同一条检查流程）"""
        try:
            result = run_check(monitor)
            if result:
                return jsonify({
                    'success': True,
                    'message': '检查完成',
                    'data': result['data']
                })
            return jsonify({'success': False, 'message': '获取数据失败'})
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

    @app.route('/api/config', methods=['GET'])
    def api_get_config():
        """获取系统配置API"""
        try:
            config_data = {
                'threshold': getattr(config, 'LOW_BALANCE_THRESHOLD', 10.0),
                'emails': getattr(config, 'ALERT_EMAILS', None) or [],
                'check_frequency': getattr(config, 'CHECK_FREQUENCY_MINUTES', 60)
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

            # 验证邮箱格式
            valid_emails = []
            for email in emails:
                email = str(email).strip()
                if email and re.match(EMAIL_PATTERN, email):
                    valid_emails.append(email)

            if not valid_emails:
                return jsonify({'success': False, 'message': '请至少配置一个有效的邮箱地址'})

            # 重新生成配置文件（保留宿舍等敏感字段，只更新 Web 可编辑项）
            config_content = f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电费自动提醒系统配置文件
"""

# 北邮统一身份认证账号
BUPT_USERNAME = "{getattr(config, 'BUPT_USERNAME', '')}"
BUPT_PASSWORD = "{getattr(config, 'BUPT_PASSWORD', '')}"

# 宿舍信息（缺失时程序会查询错误的默认房间！）
AREA_ID = {getattr(config, 'AREA_ID', 1)}
APARTMENT_ID = "{getattr(config, 'APARTMENT_ID', '')}"
FLOOR_ID = "{getattr(config, 'FLOOR_ID', '')}"
ROOM_NUMBER = "{getattr(config, 'ROOM_NUMBER', '')}"
EXPECTED_APARTMENT_NAME = "{getattr(config, 'EXPECTED_APARTMENT_NAME', '')}"

# 邮箱配置
EMAIL_USERNAME = "{getattr(config, 'EMAIL_USERNAME', '')}"
EMAIL_PASSWORD = "{getattr(config, 'EMAIL_PASSWORD', '')}"
EMAIL_SMTP_SERVER = "{getattr(config, 'EMAIL_SMTP_SERVER', 'smtp.qq.com')}"
EMAIL_SMTP_PORT = {getattr(config, 'EMAIL_SMTP_PORT', 587)}

# 提醒邮箱列表
ALERT_EMAILS = {repr(valid_emails)}

# 预警阈值（度）：低于此值发邮件，页面显示/横幅/预测预警均使用此值
LOW_BALANCE_THRESHOLD = {threshold}

# 检查频率（分钟）：设置后按此间隔检查；删除此行则每小时整点检查
CHECK_FREQUENCY_MINUTES = {check_frequency}

# Web服务配置
WEB_HOST = "{getattr(config, 'WEB_HOST', '0.0.0.0')}"
WEB_PORT = {getattr(config, 'WEB_PORT', 5100)}
DEBUG_MODE = {getattr(config, 'DEBUG_MODE', False)}

# 预测系统配置
PREDICTION_ALERT_DAYS = {getattr(config, 'PREDICTION_ALERT_DAYS', 7)}
PREDICTION_LOOKBACK_DAYS = {getattr(config, 'PREDICTION_LOOKBACK_DAYS', 30)}
PREDICTION_MAX_DAILY_RATE = {getattr(config, 'PREDICTION_MAX_DAILY_RATE', 50.0)}
PREDICTION_RECENT_WINDOW_DAYS = {getattr(config, 'PREDICTION_RECENT_WINDOW_DAYS', 3)}
PREDICTION_MIN_SAMPLES = {getattr(config, 'PREDICTION_MIN_SAMPLES', 12)}
PREDICTION_MIN_OBSERVED_DAYS = {getattr(config, 'PREDICTION_MIN_OBSERVED_DAYS', 0.5)}
'''
            # 备份原配置文件
            shutil.copy('config.py', 'config.py.backup')

            # 写入新配置并重新加载
            with open('config.py', 'w', encoding='utf-8') as f:
                f.write(config_content)
            importlib.reload(config)

            logging.info(f"配置已更新 - 阈值: {threshold}度, 邮箱数量: {len(valid_emails)}, 检查频率: {check_frequency}分钟")

            # 按新频率重新调度定时检查
            if reschedule:
                reschedule()

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
        frequency = getattr(config, 'CHECK_FREQUENCY_MINUTES', None)
        try:
            frequency = int(frequency)
        except (TypeError, ValueError):
            frequency = None
        return f"每{frequency}分钟检查" if frequency and frequency != 60 else "每小时整点检查"

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
