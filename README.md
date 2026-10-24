# 北邮宿舍电费监控系统

定时登录北邮统一身份认证，查询宿舍剩余电量，提供 Web 仪表盘、用电预测与邮件预警。
Windows 本机单实例部署的个人自用工具，非生产级服务。

> 改代码前请先读 [`AGENTS.md`](AGENTS.md)。本项目大量逻辑依赖学校接口的特殊字段语义，
> 凭直觉改数字极易出错（详见下方「领域语义」）。

## 功能

- **智能登录**：先探测电费页是否已登录，仅在需要时才走统一身份认证
- **双池电量**：同时统计付费余额（`surplus`）与学校赠送池（`freeEnd`），赠送期间用量依然准确
- **用电预测**：取最近 3 天用电速率（样本不足自动放宽窗口）→ 线性外推，给出「电量耗尽天数」与「降到预警线的日期」
- **邮件预警**：低余额预警 + 预测预警，各自 24 小时去重（邮件功能当前未启用，见下）
- **Web 仪表盘**：余额趋势（24 小时 / 7 天 / 30 天）、每日用电量柱状图、历史记录、日志面板
- **深色模式**：跟随系统偏好，可手动切换并记忆
- **SQLite 存储**：原始接口响应完整入库（`raw_data` 是真相源），历史零迁移
- **UI 内改配置**：阈值 / 邮箱 / 检查频率可在页面里改，保存后即时重新调度

## 系统要求

- Windows + Python 3.11（系统自带即可）
- 依赖见 `requirements.txt`：Flask / requests / beautifulsoup4 / APScheduler
- 能访问 `auth.bupt.edu.cn` 与 `app.bupt.edu.cn`
- 邮箱账号（可选，仅预警邮件需要）

## 快速部署

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 生成配置（交互式向导，会保留已有宿舍字段）
python setup_config.py

# 3. 启动
python app.py
```

Windows 日常使用直接双击 **`启动电费监控.bat`**：启动服务，5 秒后自动打开
<http://localhost:5100>；停止用 `停止电费监控.bat`，或直接关闭黑色日志窗口。

这是本项目唯一入口。`start.bat` / `start.sh` 是早期 conda 方案遗留，已于 2026-10-23 删除。

## 配置说明

配置由 `config.py` 承载，三种修改方式：

| 方式 | 生效条件 |
|---|---|
| 页面「提醒设置」保存 | 立即生效（后端 `importlib.reload` 并重新调度） |
| 手工编辑 `config.py` | 需重启服务 |
| `python setup_config.py` 向导 | 需重启服务 |

关键配置项：

```python
# 宿舍信息（缺失时程序会查询错误的默认房间！）
# 实际值请用 python setup_config.py 自动查询生成，不要手抄他人的配置
AREA_ID = 1                                   # 1=西土城, 2=沙河
APARTMENT_ID = "你的公寓楼ID"
FLOOR_ID = "你的楼层"
ROOM_NUMBER = "你的房间号"
EXPECTED_APARTMENT_NAME = "你的公寓名"        # 防串房校验，不匹配则拒收该条数据

# 预警阈值（度）：全系统唯一的阈值
LOW_BALANCE_THRESHOLD = 20

# 检查频率（分钟）：设为 60 则每小时整点检查
CHECK_FREQUENCY_MINUTES = 30

# 预测系统
PREDICTION_ALERT_DAYS = 7                     # 提前多少天发预测预警
PREDICTION_LOOKBACK_DAYS = 30                 # 历史总窗口上限（回退用的最长窗口）
PREDICTION_MAX_DAILY_RATE = 50.0              # 单区间折算日耗超此值视为异常丢弃
PREDICTION_RECENT_WINDOW_DAYS = 3             # 主窗口：优先只看最近 N 天
PREDICTION_MIN_SAMPLES = 12                   # 主窗口区间数下限，不足则放宽窗口
PREDICTION_MIN_OBSERVED_DAYS = 0.5            # 有效观测时长下限（天），低于此值不预测
```

> **`DEBUG_MODE` 必须保持 `False`**：为 `True` 时 Flask reloader 会重启进程，
> 导致 APScheduler 跑两份、重复查询学校接口。

> **新增配置项必须同时改三处**：`web.py` 的 `api_save_config` 模板、
> `setup_config.py` 模板、`config.py.example`。`/api/config` POST 会**整文件重写**
> `config.py`，漏掉任何一处，用户在 UI 保存一次就会丢失该配置。

### 邮箱配置

目前 `EMAIL_USERNAME` / `EMAIL_PASSWORD` 仍是占位符（`no@email.com`），
**邮件功能处于未启用状态**。如需启用：

1. 登录 QQ 邮箱 → 设置 → 开启 SMTP 服务
2. 生成**授权码**（不是 QQ 密码）
3. 填入 `EMAIL_USERNAME` 与 `EMAIL_PASSWORD`

收件人统一用 `ALERT_EMAILS` 列表（旧的单值 `ALERT_EMAIL` 已移除）。
其他邮箱：163 `smtp.163.com:25`、126 `smtp.126.com:25`、Gmail `smtp.gmail.com:587`。

## 使用方法

启动后访问 <http://localhost:5100>。页面每 60 秒轮询 `/api/version`（只查最新记录时间戳，
近零开销），时间戳变化才整页刷新；设置保存成功后 1 秒整页刷新。

### API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Web 仪表盘 |
| GET | `/api/stats` | 完整统计数据（含趋势、预测） |
| GET | `/api/version` | 轻量版本探针，仅返回最新记录时间戳 |
| GET | `/api/records?limit=N` | 历史记录 |
| POST | `/api/check` | 立即检查（会真实登录并查询学校接口） |
| GET | `/api/prediction?threshold=N` | 即时重算预测（无副作用，不写库） |
| GET | `/api/config` | 读取可编辑配置 |
| POST | `/api/config` | 保存配置并重新调度 |
| GET | `/api/logs?limit=N&level=all` | 日志（容错非 UTF-8 历史行） |
| DELETE | `/api/records`、`/api/records/<id>`、`/api/logs` | 清空记录 / 删单条 / 清空日志 |

## 领域语义（改任何数字前必读）

学校接口字段（`raw_data` 列保存完整响应，是真相源）：

- `surplus` = 付费剩余电量（**度，不是金额**）
- `freeEnd` = 学校赠送池余量（度）。学期初赠每人 40 度、宿舍共 240 度，**赠送池优先扣费**，
  赠送期间 `surplus` 冻结不变
- `totalActiveDisp` = 电表累计示数（度）
- `pTotal` = 实时功率（kW）；`vTotal` = 电压（**不是用电量**）；`price` = 购电单价

**双池模型**：入库/展示/预测的 `balance` = `surplus + freeEnd`（总可用电量）。
消耗按池拆分（`_pool_intervals`）：付费池下降=付费消耗（上升=充值，按固定档位还原）；
赠送池下降=赠送消耗（上升=学校到账，该区间赠送消耗记 0）。充值档位固定
25/50/75/100/150/200 度，`snap_recharge_amount()` 向上取最近档。

**预测语义**：`days_remaining` = 电量**耗尽**天数；`days_to_threshold` = 降到阈值天数
（预测预警邮件用它触发）；`predicted_date` = 到阈值日期；`window_days` = 实际使用的窗口天数。

**窗口策略**：优先只看最近 3 天的用电速率（简单时间加权：总消耗 ÷ 总天数）。
若该窗口区间数不足 12 条或观测不到 0.5 天（停服、刚部署），自动放宽到 7 / 12 / 30 天，
取第一个满足门槛的窗口，此时 `prediction_method='extended'` 且可信度打折。
**不做近因加权、不做日历分型**——短窗口本身已足够「新」，实测工作日/周末无差异，
用电支配变量是气温/空调。

## 项目结构

```
电费/
├── app.py                 # 薄入口：装配 + 定时调度
├── storage.py             # SQLite 访问层（含幂等历史回填）
├── monitor.py             # 登录/采集/预测/统计 + run_check 完整检查流程
├── notifier.py            # 邮件预警（24 小时去重）
├── web.py                 # 全部 Flask 路由
├── setup_config.py        # 交互式配置向导（含宿舍信息自动查询）
├── config.py              # 实际配置（已 gitignore）
├── config.py.example      # 配置模板
├── requirements.txt       # 依赖
├── templates/index.html   # 页面模板
├── static/js/main.js      # 原生 canvas 绘图与交互
├── static/css/style.css   # 双套主题色板
├── electric_data.db       # SQLite（自动创建，已 gitignore）
├── electric_monitor.log   # 运行日志（自动创建，已 gitignore）
├── AGENTS.md              # 给接手开发者的说明书（先读这个）
└── 部署说明.md            # 项目变更日志（新章节加在顶部）
```

## 自动化行为

- **定时检查**：按 `CHECK_FREQUENCY_MINUTES` 间隔轮询；设为 60 则每小时整点。
  服务启动与保存设置后都会**立即执行首次检查**
- **低余额预警**：`balance < LOW_BALANCE_THRESHOLD` 时发邮件，24 小时内只发一次
- **预测预警**：预计 `days_to_threshold ≤ PREDICTION_ALERT_DAYS` 时发邮件，同样 24 小时去重

定时检查与手动「立即检查」共用 `monitor.run_check()` 一条流程，不要另写检查逻辑。

## 常见问题

**登录失败** — 检查学号密码、网络连通性，看 `electric_monitor.log`。

**查到的不是自己宿舍** — `config.py` 的宿舍四字段（`AREA_ID`/`APARTMENT_ID`/`FLOOR_ID`/
`ROOM_NUMBER`）缺失时程序会落到内置默认房间。重跑 `python setup_config.py` 并在
「宿舍信息」环节选择重新查询即可。`EXPECTED_APARTMENT_NAME` 是第二道防线。

**改了代码没生效** — 后端/模板改动需重启服务；`static/` 改动只需浏览器 Ctrl+F5。

**端口 5100 被占用** — 双击 `停止电费监控.bat`（按端口与 `app.py` 进程精准结束，
不影响其他 Python 程序）。

**测试时改了没生效（5101）** — Windows 的 `SO_REUSEADDR` 允许多个进程同时绑定同一端口，
旧僵尸实例会截走请求。复测前必须清掉所有 5101 `LISTENING` 进程。

## 安全说明

`config.py`、`*.db`、`*.log` 均已在 `.gitignore` 中，不会提交到仓库。
但注意 `config.py` 里是**明文密码**，请勿分享整个项目目录。

## 变更历史

变更日志见 **[部署说明.md](部署说明.md)**（新章节加在文件顶部标题下，写清动机/改法/验证数据）。

## 来源与致谢

本项目基于 [Nemoyuzx/------](https://github.com/Nemoyuzx/------) 二次开发，
原始版本以 GNU GPL v3 发布，原作者联系方式：2099905168@qq.com。

当前版本已大幅重构，与上游实现差异很大：拆分为 `app` / `storage` / `monitor` /
`notifier` / `web` 五个模块；预测改为「近 3 天窗口 + 自动回退」
（删除工作日/周末日历分型与近因加权）；引入双池模型（付费余额 + 学校赠送池）；
删除冗余工具脚本与死代码。
原始设计的功劳归于原作者，重构部分的改动与问题由本仓库维护者负责。

## 许可证

GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007

原始版权归原作者所有；本项目继续以同一许可证发布，衍生作品同样受 GPL v3 约束。

## 免责声明

本工具仅供学习和个人使用，请遵守相关网站的使用条款。使用本工具造成的任何后果由用户自行承担。
