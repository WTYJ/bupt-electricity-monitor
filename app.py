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

import atexit
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

# 导入配置（缺少或未填写时给出明确提示后退出）
try:
    import config
except ImportError:
    print("请先配置config.py文件，填写用户名、密码等信息")
    print("可以复制config.py.example为config.py并填写实际信息")
    exit(1)

if (hasattr(config, 'BUPT_USERNAME') and config.BUPT_USERNAME == "你的学号") or \
   (hasattr(config, 'EMAIL_USERNAME') and config.EMAIL_USERNAME == "你的邮箱@qq.com"):
    print("请先在config.py中填写实际的用户名、密码等信息！")
    print("当前配置文件中仍然是示例值，请修改为实际值")
    exit(1)

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
    # 清除可能存在的旧任务
    for job in scheduler.get_jobs():
        if job.id == 'electric_check':
            scheduler.remove_job('electric_check')

    frequency = getattr(config, 'CHECK_FREQUENCY_MINUTES', None)
    try:
        frequency = int(frequency)
    except (TypeError, ValueError):
        frequency = None

    job_kwargs = dict(
        func=scheduled_check,
        id='electric_check',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        next_run_time=datetime.now(),  # 启动/改配置后立即执行首次检查
    )
    if frequency and frequency != 60:
        scheduler.add_job(trigger='interval', minutes=frequency, **job_kwargs)
        logging.info(f"定时任务已设置：每{frequency}分钟检查剩余电量，首次检查立即执行")
    else:
        scheduler.add_job(trigger='cron', minute=0, **job_kwargs)
        logging.info("定时任务已设置：每小时整点检查剩余电量，首次检查立即执行")

    # 注册退出时关闭调度器（仅注册一次）
    if not getattr(setup_scheduler, '_exit_registered', False):
        atexit.register(lambda: scheduler.shutdown())
        setup_scheduler._exit_registered = True


def scheduled_check():
    """定时检查电费（与手动 /api/check 共用 monitor.run_check 流程）"""
    logging.info("开始定时检查电费...")
    if monitor is None:
        logging.error("定时检查跳过 - 监控实例尚未初始化")
        return
    try:
        from monitor import run_check
        result = run_check(monitor)
        if not result:
            logging.error("定时检查失败 - 无法获取数据")
    except Exception as e:
        logging.error(f"定时检查出错: {str(e)}")


if __name__ == '__main__':
    # 初始化数据库并创建监控实例
    import storage
    storage.init_db()

    from monitor import ElectricMonitor
    from web import create_app

    set_monitor(ElectricMonitor())

    # 启动定时任务调度器
    setup_scheduler()
    if not scheduler.running:
        scheduler.start()
        logging.info("定时任务调度器已启动")

    app = create_app(monitor, reschedule=setup_scheduler)
    debug_mode = getattr(config, 'DEBUG_MODE', False)
    # use_reloader=False：Flask reloader 会重启整个进程，导致调度器双份运行、重复查询
    app.run(host=getattr(config, 'WEB_HOST', '0.0.0.0'),
            port=getattr(config, 'WEB_PORT', 5100),
            debug=debug_mode,
            use_reloader=False)
