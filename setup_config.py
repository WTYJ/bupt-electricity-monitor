#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置向导 - 帮助用户设置实际凭据
"""

import os
import shutil
import getpass
from datetime import datetime


def _load_existing_config():
    """读取现有 config.py 中的大写字段，没有则返回空字典"""
    if not os.path.exists('config.py'):
        return {}
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('_old_config', 'config.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return {k: getattr(module, k) for k in dir(module) if k.isupper()}
    except Exception:
        return {}


def _lookup_room():
    """交互式查询房间信息（校区+房间名 → 公寓ID/楼层/房间号）"""
    import requests
    from bs4 import BeautifulSoup

    s = requests.Session()
    try:
        login_url = "https://auth.bupt.edu.cn/authserver/login"
        r = s.get(login_url, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        fd = lambda n: (soup.find('input', {'name': n}) or {}).get('value', '')
        r = s.post(login_url, data={
            'username': _lookup_room.username,
            'password': _lookup_room.password,
            'type': fd('type'), 'execution': fd('execution'), '_eventId': fd('_eventId'),
        }, timeout=15)
        if 'CAS Login' in r.text:
            print("   ❌ 统一身份认证登录失败，无法自动查询房间")
            return None
        s.get("https://app.bupt.edu.cn/buptdf/wap/default/chong", timeout=15)
    except Exception as e:
        print(f"   ❌ 网络错误: {e}")
        return None

    area = (_lookup_room.area or '1').strip()
    target = (_lookup_room.room_name or '').strip()
    if not target:
        print("   ❌ 未输入房间名")
        return None

    r = s.post("https://app.bupt.edu.cn/buptdf/wap/default/part", data={'areaid': area}, timeout=15)
    apartments = r.json()['d']['data']
    for apt in apartments:
        apt_id = apt['partmentId']
        r = s.post("https://app.bupt.edu.cn/buptdf/wap/default/floor",
                   data={'partmentId': apt_id, 'areaid': area}, timeout=15)
        floors = r.json()['d']['data']
        for f in floors:
            r = s.post("https://app.bupt.edu.cn/buptdf/wap/default/drom",
                       data={'partmentId': apt_id, 'floorId': f['floorId'], 'areaid': area}, timeout=15)
            for room in r.json()['d']['data']:
                if target.lower() in str(room.get('dromNum', '')).lower() + str(room.get('dromName', '')).lower():
                    print(f"   ✅ 找到: {apt['partmentName']} {f['floorName']}层 {room['dromName']}")
                    return {
                        'AREA_ID': int(area),
                        'APARTMENT_ID': apt_id,
                        'FLOOR_ID': str(f['floorId']),
                        'ROOM_NUMBER': room['dromNum'],
                        'EXPECTED_APARTMENT_NAME': apt['partmentName'],
                    }
    print(f"   ❌ 未找到房间 [{target}]，请确认房间名格式（如: 10-1001）")
    return None


def setup_config():
    """配置向导"""
    print("=" * 60)
    print("              电费监控系统配置向导")
    print("=" * 60)
    print()

    config_file = "config.py"
    example_file = "config.py.example"

    if not os.path.exists(example_file):
        print(f"❌ 找不到模板文件 {example_file}")
        return False

    if os.path.exists(config_file):
        choice = input(f"配置文件 {config_file} 已存在，是否覆盖？ (y/N): ").strip().lower()
        if choice != 'y':
            print("配置取消")
            return False
        backup_file = f"{config_file}.backup"
        shutil.copy2(config_file, backup_file)
        print(f"已备份现有配置到 {backup_file}")

    old = _load_existing_config()

    print("请填写以下配置信息：")
    print()

    # 北邮账号信息
    print("1. 北邮统一身份认证信息")
    default_user = old.get('BUPT_USERNAME', '')
    tip = f" [默认: {default_user}]" if default_user else ""
    username = input(f"   学号{tip}: ").strip() or default_user
    password = getpass.getpass("   密码: ").strip() or old.get('BUPT_PASSWORD', '')
    print()

    # 邮箱信息
    print("2. 邮件发送配置")
    print("   注意：如使用QQ邮箱，密码应为授权码，不是QQ密码")
    default_email = old.get('EMAIL_USERNAME', '')
    tip = f" [默认: {default_email}]" if default_email else ""
    email_user = input(f"   发送邮箱{tip}: ").strip() or default_email
    email_pass = getpass.getpass("   邮箱密码/授权码: ").strip() or old.get('EMAIL_PASSWORD', '')
    old_emails = old.get('ALERT_EMAILS') or ['no@email.com']
    alert_email = input(f"   接收预警邮箱 [默认: {old_emails[0]}]: ").strip() or str(old_emails[0])

    email_domain = email_user.split('@')[-1].lower()
    if 'qq.com' in email_domain:
        smtp_server, smtp_port = "smtp.qq.com", 587
    elif '163.com' in email_domain:
        smtp_server, smtp_port = "smtp.163.com", 25
    elif '126.com' in email_domain:
        smtp_server, smtp_port = "smtp.126.com", 25
    elif 'gmail.com' in email_domain:
        smtp_server, smtp_port = "smtp.gmail.com", 587
    else:
        smtp_server = input("   SMTP服务器: ").strip()
        smtp_port = int(input("   SMTP端口: ").strip())
    print(f"   自动检测SMTP: {smtp_server}:{smtp_port}")
    print()

    # 宿舍信息：优先沿用现有配置，缺失/要求更换时通过接口自动查询
    print("3. 宿舍信息")
    dorm_keys = ['AREA_ID', 'APARTMENT_ID', 'FLOOR_ID', 'ROOM_NUMBER', 'EXPECTED_APARTMENT_NAME']
    has_dorm = all(old.get(k) for k in dorm_keys)
    if has_dorm:
        print(f"   检测到现有配置: {old['EXPECTED_APARTMENT_NAME']} "
              f"{old['ROOM_NUMBER']}（校区{old['AREA_ID']}），将原样保留")
        redetect = input("   是否重新查询宿舍信息? (y/N): ").strip().lower() == 'y'
    else:
        print("   ⚠️ 现有配置中没有宿舍信息，必须查询（否则程序会查到错误的房间！）")
        redetect = True

    if redetect:
        _lookup_room.username = username
        _lookup_room.password = password
        _lookup_room.area = input("   校区 (1=西土城, 2=沙河) [默认: 1]: ").strip() or \
            (str(old.get('AREA_ID', 1)) if old.get('AREA_ID') else '1')
        _lookup_room.room_name = input("   房间名 (如: 10-1001): ").strip()
        dorm = _lookup_room()
        if dorm is None:
            print("   ❌ 宿舍信息查询失败，配置取消（可重新运行本向导）")
            return False
    else:
        dorm = {k: old[k] for k in dorm_keys}
    print()

    # 其他设置
    print("4. 其他设置")
    threshold = input(f"   电量预警阈值（度）[默认: {old.get('LOW_BALANCE_THRESHOLD', 10.0)}]: ").strip()
    low_threshold = float(threshold) if threshold else float(old.get('LOW_BALANCE_THRESHOLD', 10.0))
    freq_default = old.get('CHECK_FREQUENCY_MINUTES', 60)
    freq_raw = input(f"   检查频率（分钟，60=每小时整点）[默认: {freq_default}]: ").strip()
    check_frequency = int(freq_raw) if freq_raw else int(freq_default)
    port = input(f"   Web服务端口 [默认: {old.get('WEB_PORT', 5100)}]: ").strip()
    web_port = int(port) if port else int(old.get('WEB_PORT', 5100))
    print()

    # 确认配置
    print("配置信息确认：")
    print(f"学号: {username}")
    print(f"发送邮箱: {email_user}")
    print(f"接收邮箱: {alert_email}")
    print(f"SMTP服务器: {smtp_server}:{smtp_port}")
    print(f"公寓: {dorm['EXPECTED_APARTMENT_NAME']}  楼层: {dorm['FLOOR_ID']}  房间: {dorm['ROOM_NUMBER']}")
    print(f"电量预警阈值: {low_threshold} 度")
    print(f"检查频率: {check_frequency} 分钟")
    print(f"Web端口: {web_port}")
    print()

    confirm = input("确认无误并保存配置？ (Y/n): ").strip().lower()
    if confirm == 'n':
        print("配置取消")
        return False

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    config_content = f"""# 配置文件 - 电费自动提醒系统
# 自动生成于 {now}

# 北邮统一身份认证用户名和密码
BUPT_USERNAME = "{username}"
BUPT_PASSWORD = "{password}"

# 宿舍信息（缺少这些字段时程序会查询错误的默认房间！）
AREA_ID = {dorm['AREA_ID']}
APARTMENT_ID = "{dorm['APARTMENT_ID']}"
FLOOR_ID = "{dorm['FLOOR_ID']}"
ROOM_NUMBER = "{dorm['ROOM_NUMBER']}"
EXPECTED_APARTMENT_NAME = "{dorm['EXPECTED_APARTMENT_NAME']}"

# 邮件发送配置
EMAIL_SMTP_SERVER = "{smtp_server}"
EMAIL_SMTP_PORT = {smtp_port}
EMAIL_USERNAME = "{email_user}"
EMAIL_PASSWORD = "{email_pass}"
ALERT_EMAILS = ["{alert_email}"]

# 电量预警阈值（度）：低于此值发邮件，页面显示/横幅/预测预警均用此值
LOW_BALANCE_THRESHOLD = {low_threshold}

# 检查频率（分钟）：设为 60 则每小时整点检查
CHECK_FREQUENCY_MINUTES = {check_frequency}

# Web服务配置
WEB_HOST = "0.0.0.0"
WEB_PORT = {web_port}
DEBUG_MODE = False

# 预测系统配置
PREDICTION_ALERT_DAYS = 7
PREDICTION_LOOKBACK_DAYS = 30
PREDICTION_MAX_DAILY_RATE = 50.0
PREDICTION_RECENT_WINDOW_DAYS = 3
PREDICTION_MIN_SAMPLES = 12
PREDICTION_MIN_OBSERVED_DAYS = 0.5
"""

    with open(config_file, 'w', encoding='utf-8') as f:
        f.write(config_content)

    print(f"✅ 配置已保存到 {config_file}")
    print()

    test_choice = input("是否测试配置？ (Y/n): ").strip().lower()
    if test_choice != 'n':
        test_configuration()

    return True


def test_configuration():
    """测试配置"""
    print()
    print("测试配置...")

    try:
        import config
        print("✅ 配置文件加载成功")

        # 测试必要字段（宿舍字段缺失会导致查错房间，一并检查）
        required_fields = ['BUPT_USERNAME', 'BUPT_PASSWORD', 'EMAIL_USERNAME', 'EMAIL_PASSWORD',
                           'AREA_ID', 'APARTMENT_ID', 'FLOOR_ID', 'ROOM_NUMBER']
        for field in required_fields:
            if hasattr(config, field) and getattr(config, field):
                print(f"✅ {field} 已配置")
            else:
                print(f"❌ {field} 未配置或为空")

        print()
        print("配置测试完成！")
        print("现在可以运行主程序了：")
        print("  python app.py")

    except Exception as e:
        print(f"❌ 配置测试失败: {str(e)}")


def show_usage():
    """显示使用说明"""
    print()
    print("=" * 60)
    print("使用说明:")
    print("1. 配置完成后，运行主程序：python app.py")
    print("2. 在浏览器中访问 http://localhost:端口号")
    print("3. 系统会每小时自动检查电费")
    print("4. 余额不足时会自动发送邮件预警")
    print()
    print("如需修改配置，重新运行此脚本即可")
    print("=" * 60)


if __name__ == "__main__":
    if setup_config():
        show_usage()
    else:
        print("配置失败或取消")
