#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
邮件通知模块 - 低余额预警与预测预警

统一收件人解析（ALERT_EMAILS）、统一 SMTP 发送流程、统一 24 小时去重（由 storage 提供）。
单位统一为"度"。
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

import config
import storage


def get_recipients():
    """解析预警收件人列表（统一使用 ALERT_EMAILS）"""
    emails = getattr(config, 'ALERT_EMAILS', None)
    if emails:
        return list(emails)
    return []


def _send(subject, body):
    """
    通过 SMTP 把邮件发给所有收件人

    Returns:
        int: 成功发送的邮件数量
    """
    recipients = get_recipients()
    if not recipients:
        logging.warning("未配置预警邮箱，跳过邮件发送")
        return 0

    server = smtplib.SMTP(config.EMAIL_SMTP_SERVER, config.EMAIL_SMTP_PORT)
    sent_count = 0
    try:
        server.starttls()
        server.login(config.EMAIL_USERNAME, config.EMAIL_PASSWORD)

        for email in recipients:
            try:
                msg = MIMEMultipart()
                msg['From'] = config.EMAIL_USERNAME
                msg['To'] = email
                msg['Subject'] = subject
                msg.attach(MIMEText(body, 'plain', 'utf-8'))
                server.sendmail(config.EMAIL_USERNAME, email, msg.as_string())
                sent_count += 1
                logging.info(f"预警邮件已发送到: {email}")
            except Exception as e:
                logging.error(f"发送邮件到 {email} 失败: {str(e)}")
    finally:
        server.quit()
    return sent_count


def send_low_balance_alert(balance, threshold):
    """发送低余额预警邮件（24小时内只发一次）"""
    try:
        if storage.has_recent_alert('low_balance', hours=24):
            logging.info("24小时内已发送过预警邮件，跳过")
            return

        body = f"""
        您好！

        您的宿舍剩余电量不足，请及时充值购电：

        剩余电量: {balance:.2f} 度
        预警阈值: {threshold} 度
        查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

        请及时前往 https://app.bupt.edu.cn/buptdf/wap/default/chong 进行充值。

        ---
        电费自动提醒系统
        """

        sent_count = _send("⚠️ 电费余额不足预警", body)
        if sent_count > 0:
            storage.record_alert(
                'low_balance',
                f'剩余电量不足预警: {balance}度，已发送{sent_count}封邮件',
                1
            )
            logging.info(f"预警邮件发送完成，成功发送{sent_count}封邮件，当前剩余电量: {balance}度")

    except Exception as e:
        logging.error(f"发送预警邮件时出现错误: {str(e)}")


def send_prediction_alert(prediction_data, alert_days=7):
    """
    发送预测预警邮件（预计 alert_days 天内将低于阈值时提醒，24小时内只发一次）

    Args:
        prediction_data: predict() 返回的预测结果字典
        alert_days: 提前预警天数
    """
    try:
        if not prediction_data or not prediction_data.get('success'):
            return

        days_to_threshold = prediction_data.get('days_to_threshold')
        if days_to_threshold is None or days_to_threshold > alert_days:
            return

        if storage.has_recent_alert('prediction_warning', hours=24):
            logging.info("24小时内已发送过预测预警，跳过发送")
            return

        current_balance = prediction_data.get('current_balance', 0)
        threshold = prediction_data.get('threshold', 10)
        predicted_date = prediction_data.get('predicted_date', '未知')
        daily_avg = prediction_data.get('daily_usage_avg', 0)
        confidence = prediction_data.get('prediction_confidence', 'low')

        confidence_text = {'high': '高', 'medium': '中', 'low': '低'}.get(confidence, '低')
        urgency_level = '🔴 紧急' if days_to_threshold <= 3 else '🟡 提醒'

        body = f"""
您好！

根据您的用电模式分析，预测您的宿舍剩余电量可能即将不足：

{urgency_level} 预测预警
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📊 当前状况：
• 剩余电量：{current_balance:.2f} 度
• 预警阈值：{threshold:.1f} 度
• 日均用电量：{daily_avg:.2f} 度

🔮 预测结果：
• 预计剩余天数：{days_to_threshold:.1f} 天（降到预警线）
• 预计到达日期：{predicted_date}
• 预测可信度：{confidence_text}

💡 建议：
{"• 建议您尽快充值，避免断电影响生活" if days_to_threshold <= 3 else "• 请关注剩余电量，适时进行充值"}

🔗 充值链接：
https://app.bupt.edu.cn/buptdf/wap/default/chong

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
查询时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
本预测基于最近的用电历史数据分析得出，仅供参考。

---
电费自动提醒系统 - 智能预测服务
        """

        subject = f"🔮 电量预测预警 - 预计{days_to_threshold:.1f}天后不足"
        sent_count = _send(subject, body)
        if sent_count > 0:
            storage.record_alert(
                'prediction_warning',
                f'预测预警: 预计{days_to_threshold:.1f}天后剩余电量降到{threshold}度以下，已发送{sent_count}封邮件',
                1
            )
            logging.info(f"预测预警邮件发送完成，成功发送{sent_count}封邮件，预计{days_to_threshold:.1f}天后电量低于阈值")

    except Exception as e:
        logging.error(f"发送预测预警失败: {str(e)}")
