#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电费自动提醒系统 - 程序入口

模块划分：
- storage.py  SQLite 数据访问层
- monitor.py  登录/采集/预测/统计 + 完整检查流程 run_check
- notifier.py 邮件预警
- web.py      Flask 路由
- app.py      本文件：装配与定时任务调度
"""

import argparse
import logging
import socket
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.serving import ThreadedWSGIServer
from config_io import check_frequency_minutes, initial_config, loopback_host, setup_required
from instance import InstanceAlreadyRunning, active_dashboard_url, register_instance

PROJECT_DIR = Path(__file__).resolve().parent

# 首次启动先加载内存配置，浏览器完成宿舍设置后才落盘。
try:
    import config
except ModuleNotFoundError as exc:
    if exc.name != 'config':
        raise
    config = initial_config(PROJECT_DIR / 'config.py')
    sys.modules['config'] = config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        # encoding='utf-8' 必须显式指定：Windows 下 FileHandler 默认按 locale（GBK）写盘，
        # 日志里出现 emoji/生僻字就会抛 UnicodeEncodeError 或写出混编码行。
        # /api/logs 侧的 errors='replace' 只是对历史脏数据的容错，不能替代这里的正确编码。
        logging.FileHandler('electric_monitor.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

scheduler = BackgroundScheduler()

# 全局监控实例：由 __main__ 块或 set_monitor() 注入。
# 不放在 __main__ 块里，否则 scheduled_check 被 import 调用时会 NameError。
monitor = None


def set_monitor(instance):
    """注入监控实例（定时任务与 Web 层共用同一个会话对象）"""
    global monitor
    monitor = instance
    return monitor


def setup_scheduler():
    """按配置设置定时检查任务（可重复调用，用于配置变更后重新调度）"""
    if setup_required(config):
        logging.info("请在仪表盘完成首次设置后启动自动检查")
        return
    frequency = check_frequency_minutes(config)

    job_kwargs = dict(
        func=scheduled_check,
        id='electric_check',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        next_run_time=datetime.now(),  # 启动/改配置后立即执行首次检查
    )
    if frequency != 60:
        scheduler.add_job(trigger='interval', minutes=frequency, **job_kwargs)
        logging.info(f"定时任务已设置：每{frequency}分钟检查剩余电量，首次检查立即执行")
    else:
        scheduler.add_job(trigger='cron', minute=0, **job_kwargs)
        logging.info("定时任务已设置：每小时整点检查剩余电量，首次检查立即执行")


def scheduled_check():
    """定时检查电费（与手动 /api/check 共用 monitor.run_check 流程）"""
    logging.info("开始定时检查电费...")
    if monitor is None:
        logging.error("定时检查跳过 - 监控实例尚未初始化")
        return
    from monitor import CheckInProgress, run_check
    try:
        result = run_check(monitor)
        if not result:
            logging.error("定时检查失败 - 无法获取数据")
    except CheckInProgress:
        logging.info("已有检查正在进行，本次定时检查跳过")
    except Exception as e:
        logging.error(f"定时检查出错: {str(e)}")


class LocalDashboardServer(ThreadedWSGIServer):
    # Windows SO_REUSEADDR 允许同一地址出现两个监听者，仪表盘需要独占监听端口。
    allow_reuse_address = not hasattr(socket, 'SO_EXCLUSIVEADDRUSE')

    def server_bind(self):
        if not self.allow_reuse_address:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def open_dashboard(url):
    try:
        if webbrowser.open(url):
            return
    except (OSError, webbrowser.Error):
        pass
    logging.info("请在浏览器中打开 %s", url)


def open_running_dashboard():
    deadline = monotonic() + 5
    while monotonic() < deadline:
        url = active_dashboard_url(PROJECT_DIR)
        if url:
            open_dashboard(url)
            return True
        sleep(0.1)
    logging.info("电费监控的网页尚未就绪")
    return False


def main(registration=None, open_browser=False):
    # 初始化数据库并创建监控实例
    import storage
    storage.init_db()

    from monitor import ElectricMonitor
    from web import create_app

    set_monitor(ElectricMonitor())

    app = create_app(monitor, reschedule=setup_scheduler)
    app.debug = getattr(config, 'DEBUG_MODE', False)
    configured_host = getattr(config, 'WEB_HOST', None)
    host = loopback_host(configured_host)
    if configured_host is not None and configured_host != host:
        logging.info("仪表盘仅监听本机，已使用 %s", host)
    with LocalDashboardServer(host, int(getattr(config, 'WEB_PORT', 5100)), app) as server:
        address = server.server_address[0]
        url_host = f'[{address}]' if ':' in address else address
        url = f'http://{url_host}:{server.server_port}/'
        setup_scheduler()
        scheduler.start()
        logging.info("定时任务调度器已启动")
        try:
            server.log_startup()
            if registration is not None:
                with registration.open('a', encoding='ascii') as handle:
                    handle.write(f'{url}\n')
            if open_browser:
                Thread(target=open_dashboard, args=(url,), daemon=True).start()
            server.serve_forever()
        finally:
            scheduler.shutdown()


def launch():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--open-browser', action='store_true')
    mode.add_argument('--open-existing', action='store_true')
    args = parser.parse_args()
    if args.open_existing:
        sys.exit(0 if open_running_dashboard() else 1)

    try:
        with register_instance(PROJECT_DIR) as registration:
            main(registration, open_browser=args.open_browser)
    except InstanceAlreadyRunning:
        logging.info("本项目的电费监控已在运行")
        if args.open_browser:
            sys.exit(0 if open_running_dashboard() else 1)


if __name__ == '__main__':
    launch()
