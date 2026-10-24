#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据存储层 - 集中管理所有 SQLite 访问

所有数据库读写在模块内完成，统一通过 _connect() 上下文管理器
打开/提交/关闭连接，避免异常路径漏关连接。
表结构与旧版 app.py 完全一致，历史数据无需迁移。
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = 'electric_data.db'


@contextmanager
def _connect():
    """连接上下文管理器：退出时自动 commit 并关闭连接"""
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """初始化数据库表（结构与应用旧版完全一致）"""
    with _connect() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS electric_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                balance REAL,
                usage_today REAL,
                usage_month REAL,
                status TEXT,
                raw_data TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                alert_type TEXT,
                message TEXT,
                sent BOOLEAN DEFAULT 0
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS prediction_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                current_balance REAL NOT NULL,
                threshold REAL NOT NULL,
                predicted_days REAL,
                predicted_date TEXT,
                daily_avg REAL,
                weekday_avg REAL,
                weekend_avg REAL,
                prediction_method TEXT,
                confidence TEXT,
                actual_days REAL,
                accuracy_score REAL,
                is_evaluated INTEGER DEFAULT 0
            )
        ''')
    backfill_balance_with_free_pool()


def parse_pools(raw, fallback_balance=None):
    """从原始响应拆出 (付费余额 surplus, 赠送余量 freeEnd)。

    解析失败时回退为 (fallback_balance, 0)，保证旧数据/异常数据仍可参与计算。
    surplus 缺失（None/空串/非数字）同样走回退。
    """
    try:
        data = json.loads(raw).get('d', {}).get('data', {})
        surplus = float(data.get('surplus'))
        return surplus, float(data.get('freeEnd') or 0)
    except (TypeError, ValueError, AttributeError):
        return (fallback_balance if fallback_balance is not None else 0.0), 0.0


def backfill_balance_with_free_pool():
    """把历史记录的 balance 从"仅付费余额"回填为"付费+赠送"总可用电量。

    旧版本只存 surplus；学校赠送（freeEnd）出现后，真实可用电量 = surplus + freeEnd。
    回填依据每条记录自带的 raw_data，幂等：已回填的行（balance 已等于两者之和）不再改动。

    注意：raw_data 里解析不出 surplus 的行**必须原样跳过**，不能当作 0 回填——
    那会把历史余额清零。（parse_pools 的 fallback 是给"参与计算"用的，
    回填场景不能用它兜底，故此处显式判断 surplus 是否为 None。）
    """
    with _connect() as conn:
        rows = conn.execute('SELECT id, balance, raw_data FROM electric_records').fetchall()
        updated = 0
        for record_id, balance, raw in rows:
            if balance is None or not raw:
                continue
            try:
                surplus = float(json.loads(raw).get('d', {}).get('data', {}).get('surplus'))
            except (TypeError, ValueError, AttributeError):
                continue  # 解析失败：跳过，绝不写入
            if surplus is None:
                continue
            _, free = parse_pools(raw)
            expected = surplus + free
            if abs(balance - expected) > 0.001:
                conn.execute('UPDATE electric_records SET balance = ? WHERE id = ?', (expected, record_id))
                updated += 1
        if updated:
            logging.info(f"历史记录回填完成：{updated} 条记录的余额已并入学校赠送池")


def balances_with_raw_since(cutoff):
    """晚于 cutoff 的 (timestamp, balance, raw_data)，按时间升序。

    balance 为总可用电量（付费+赠送）；raw_data 用于拆分两个池子各自的消耗。
    """
    with _connect() as conn:
        return conn.execute('''
            SELECT timestamp, balance, raw_data FROM electric_records
            WHERE balance IS NOT NULL AND timestamp >= ?
            ORDER BY timestamp ASC
        ''', (cutoff,)).fetchall()


# ── electric_records ──────────────────────────────────────────────

def save_record(data):
    """保存一条检查记录"""
    balance = float(data.get('balance', 0)) if data.get('balance') is not None else 0.0
    usage_today = data.get('usage_today')
    usage_month = data.get('usage_month')
    with _connect() as conn:
        conn.execute('''
            INSERT INTO electric_records (timestamp, balance, usage_today, usage_month, status, raw_data)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            balance,
            usage_today,
            usage_month,
            data.get('status', 'success'),
            data.get('raw_data', '')
        ))


def latest_record():
    """最新一条有效记录（完整行元组）"""
    with _connect() as conn:
        return conn.execute('''
            SELECT * FROM electric_records
            WHERE balance IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT 1
        ''').fetchone()


def recent_records(limit=20):
    """最近 N 条记录（时间倒序）"""
    with _connect() as conn:
        return conn.execute('''
            SELECT * FROM electric_records
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (limit,)).fetchall()


def balance_trend(hour_expr, group_expr, since_expr, drop_early_tail=False):
    """按时间粒度聚合的余额趋势：每个分组的点是该组**最晚一条记录**的余额
    （当天结束时剩多少度），而不是全组平均——平均值会产生一天中任何
    时刻都不存在的数字，且充值日会被严重拉偏。

    drop_early_tail=True（仅按天分组用）：跳过"当日最晚记录在中午之前"的停服日
    ——这种日子的最晚记录其实是当天起点，画出来会造成假平台（如整天停服时
    日末余额未知）；跳过后折线直接跨过停服日。最新一天（进行中）始终保留。
    """
    tail_filter = ''
    params = [since_expr]
    if drop_early_tail:
        tail_filter = '''
              AND NOT (
                  CAST(strftime('%H', timestamp) AS INTEGER) < 12
                  AND label <> (SELECT MAX({g}) FROM electric_records
                                WHERE timestamp > datetime('now', 'localtime', ?)
                                AND balance IS NOT NULL)
              )'''.format(g=group_expr)
        params.append(since_expr)
    with _connect() as conn:
        return conn.execute(f'''
            SELECT label, balance FROM (
                SELECT {group_expr} AS label, balance, timestamp,
                       ROW_NUMBER() OVER (
                           PARTITION BY {hour_expr}
                           ORDER BY timestamp DESC
                       ) AS rn
                FROM electric_records
                WHERE timestamp > datetime('now', 'localtime', ?)
                AND balance IS NOT NULL
            )
            WHERE rn = 1 {tail_filter}
            ORDER BY label
        ''', params).fetchall()


def latest_month_usage():
    """本月最新一条非空 usage_month"""
    with _connect() as conn:
        row = conn.execute('''
            SELECT usage_month FROM electric_records
            WHERE timestamp >= datetime('now', 'localtime', 'start of month')
            AND usage_month IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT 1
        ''').fetchone()
    return row[0] if row else None


def max_record_timestamp():
    """最新有效记录的时间戳（前端版本探针用）"""
    with _connect() as conn:
        row = conn.execute('''
            SELECT MAX(timestamp) FROM electric_records WHERE balance IS NOT NULL
        ''').fetchone()
    return row[0] if row and row[0] else None


def clear_all_records():
    """清空三张业务表"""
    with _connect() as conn:
        conn.execute('DELETE FROM electric_records')
        conn.execute('DELETE FROM alerts')
        conn.execute('DELETE FROM prediction_records')


def delete_record(record_id):
    """删除单条记录，返回是否删除成功"""
    with _connect() as conn:
        cursor = conn.execute('DELETE FROM electric_records WHERE id = ?', (record_id,))
        return cursor.rowcount > 0


# ── alerts ────────────────────────────────────────────────────────

def has_recent_alert(alert_type, hours=24):
    """最近 N 小时内是否已成功发送过同类预警（去重）"""
    with _connect() as conn:
        row = conn.execute('''
            SELECT COUNT(*) FROM alerts
            WHERE alert_type = ?
            AND timestamp > datetime('now', 'localtime', ?)
            AND sent = 1
        ''', (alert_type, f'-{hours} hours')).fetchone()
    return (row[0] or 0) > 0


def record_alert(alert_type, message, sent=1):
    """记录一次预警发送"""
    with _connect() as conn:
        conn.execute('''
            INSERT INTO alerts (alert_type, message, sent)
            VALUES (?, ?, ?)
        ''', (alert_type, message, sent))


# ── prediction_records ────────────────────────────────────────────

def save_prediction_record(prediction):
    """保存预测记录用于后续准确性评估"""
    with _connect() as conn:
        conn.execute('''
            INSERT INTO prediction_records
            (timestamp, current_balance, threshold, predicted_days, predicted_date,
             daily_avg, weekday_avg, weekend_avg, prediction_method, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            prediction.get('current_balance', 0),
            prediction.get('threshold', 10),
            # 准确率评估的基准是"实际降到阈值以下的时间"，因此存 days_to_threshold
            prediction.get('days_to_threshold'),
            prediction.get('predicted_date'),
            prediction.get('daily_usage_avg', 0),
            prediction.get('weekday_avg', 0),
            prediction.get('weekend_avg', 0),
            prediction.get('prediction_method', 'recent'),
            prediction.get('prediction_confidence', 'low')
        ))


def evaluate_prediction_accuracy():
    """
    评估历史预测的准确性：对未评估的预测记录，回看实际到达阈值的时间并打分

    Returns:
        dict: 预测准确性统计（与旧版 app.py 输出结构一致）
    """
    try:
        with _connect() as conn:
            unevaluated = conn.execute('''
                SELECT id, timestamp, current_balance, threshold, predicted_days,
                       predicted_date, prediction_method
                FROM prediction_records
                WHERE is_evaluated = 0 AND predicted_days IS NOT NULL
                ORDER BY timestamp ASC
            ''').fetchall()

            evaluated_count = 0
            for (pred_id, pred_timestamp, _balance, threshold,
                 predicted_days, _predicted_date, _method) in unevaluated:
                actual = conn.execute('''
                    SELECT timestamp FROM electric_records
                    WHERE timestamp > ? AND balance <= ?
                    ORDER BY timestamp ASC
                    LIMIT 1
                ''', (pred_timestamp, threshold)).fetchone()

                if not actual:
                    continue

                pred_dt = datetime.strptime(pred_timestamp, '%Y-%m-%d %H:%M:%S')
                actual_dt = datetime.strptime(actual[0], '%Y-%m-%d %H:%M:%S')
                actual_days = (actual_dt - pred_dt).total_seconds() / (24 * 3600)

                if predicted_days > 0:
                    error_ratio = abs(actual_days - predicted_days) / predicted_days
                    accuracy_score = max(0, 100 - error_ratio * 100)
                else:
                    accuracy_score = 0

                conn.execute('''
                    UPDATE prediction_records
                    SET actual_days = ?, accuracy_score = ?, is_evaluated = 1
                    WHERE id = ?
                ''', (actual_days, accuracy_score, pred_id))
                evaluated_count += 1

            method_stats = conn.execute('''
                SELECT
                    prediction_method,
                    COUNT(*) as total_predictions,
                    AVG(accuracy_score) as avg_accuracy,
                    MIN(accuracy_score) as min_accuracy,
                    MAX(accuracy_score) as max_accuracy,
                    COUNT(CASE WHEN accuracy_score >= 80 THEN 1 END) as high_accuracy_count
                FROM prediction_records
                WHERE is_evaluated = 1 AND accuracy_score IS NOT NULL
                GROUP BY prediction_method
            ''').fetchall()

            overall = conn.execute('''
                SELECT
                    COUNT(*) as total_evaluated,
                    AVG(accuracy_score) as overall_accuracy,
                    COUNT(CASE WHEN accuracy_score >= 80 THEN 1 END) as high_accuracy_total
                FROM prediction_records
                WHERE is_evaluated = 1 AND accuracy_score IS NOT NULL
            ''').fetchone()

        total = overall[0] if overall else 0
        result = {
            'success': True,
            'evaluated_count': evaluated_count,
            'overall_stats': {
                'total_predictions': total,
                'average_accuracy': round(overall[1], 2) if overall and overall[1] else 0,
                'high_accuracy_rate': round((overall[2] / total * 100), 2) if total > 0 else 0
            },
            'method_stats': []
        }
        for method, m_total, avg_acc, min_acc, max_acc, high_acc_count in method_stats:
            result['method_stats'].append({
                'method': method,
                'total_predictions': m_total,
                'average_accuracy': round(avg_acc, 2) if avg_acc else 0,
                'min_accuracy': round(min_acc, 2) if min_acc else 0,
                'max_accuracy': round(max_acc, 2) if max_acc else 0,
                'high_accuracy_rate': round((high_acc_count / m_total * 100), 2) if m_total > 0 else 0
            })

        if evaluated_count > 0:
            logging.info(f"预测准确性评估完成，评估了{evaluated_count}个预测记录")
        return result

    except Exception as e:
        logging.error(f"评估预测准确性失败: {str(e)}")
        return {
            'success': False,
            'message': f'评估失败: {str(e)}',
            'evaluated_count': 0,
            'overall_stats': {'total_predictions': 0, 'average_accuracy': 0, 'high_accuracy_rate': 0},
            'method_stats': []
        }
