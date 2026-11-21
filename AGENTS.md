# AGENTS.md — 北邮宿舍电费监控系统

给接手 agent 的项目说明书。改代码前先读完「领域语义」和「测试约定」两节——
本项目大量逻辑依赖学校接口的特殊字段语义，凭直觉改数字极易出错。

## 项目定位

个人自用工具：定时登录北邮统一身份认证查询宿舍剩余电量，提供 Web 仪表盘、邮件预警、用电预测。
Windows 本机单实例部署，非生产级服务。代码源自 GitHub 开源模板，经多轮重构后已与上游无实质关联。

## 运行环境

- Windows + 系统自带 Python 3.11；依赖见 `requirements.txt`（Flask / requests / beautifulsoup4 / APScheduler）
- 启动：双击 `启动电费监控.bat`（内部即 `python app.py`，端口 5100，5 秒后自动开浏览器）
- 停止：`停止电费监控.bat`（按端口 5100 与 `app.py` 进程精准结束），或关闭黑色日志窗口
- 改后端/模板代码**必须重启服务**；改 `static/` 只需浏览器 Ctrl+F5
- `config.py` 的 `DEBUG_MODE` 必须保持 `False`：为 `True` 时 Flask reloader 会重启进程，
  导致 APScheduler 跑两份、重复查询学校接口

## 模块地图

| 文件 | 职责 | 关键入口 |
|---|---|---|
| `app.py` | 薄入口：装配 + 定时调度 | `set_monitor()`、`setup_scheduler()`、`scheduled_check()` |
| `storage.py` | SQLite 访问层（连接统一上下文管理） | `init_db()`（含历史回填）、`parse_pools()`、`balances_with_raw_since()`、`balance_trend()` |
| `monitor.py` | 登录/采集/预测/统计 + 完整检查流程 | `ElectricMonitor.get_electric_data()`、`.predict()`、`.get_statistics()`、`run_check()`、`_pool_intervals()`、`daily_usage_trend()`、`snap_recharge_amount()` |
| `notifier.py` | 邮件预警（24 小时去重） | `send_low_balance_alert()`、`send_prediction_alert()` |
| `web.py` | 全部 Flask 路由 | `create_app(monitor, reschedule)` |
| `setup_config.py` | 交互式配置向导（会保留宿舍字段） | — |
| `templates/index.html` + `static/js/main.js` + `static/css/style.css` | 前端，**原生 canvas 画图，无图表库** | `drawChart()`、`drawDailyUsageChart()`、`updatePredictionDisplay()` |

定时检查与手动「立即检查」共用 `monitor.run_check()` 一条流程，不要另写检查逻辑。

## 领域语义（改任何数字前必读）

学校接口字段（`raw_data` 列保存完整响应，是真相源）：

- `surplus` = 付费剩余电量（**度，不是金额**）
- `freeEnd` = 学校赠送池余量（度）。学期初赠每人 40 度、宿舍共 240 度，**赠送池优先扣费**，
  赠送期间 `surplus` 冻结不变
- `totalActiveDisp` = 电表累计示数（度）；`pTotal` = 实时功率 kW；
  `vTotal` = 电压（**不是用电量**）；`price` = 购电单价

双池模型：

- 入库/展示/预测使用的 `balance` = `surplus + freeEnd`（总可用电量）
- 消耗按池拆分（`_pool_intervals`）：付费池下降=付费消耗（付费池上升=充值，按固定档位还原）；
  赠送池下降=赠送消耗（赠送池上升=学校到账，该区间赠送消耗记 0，误差 <1 度/区间）
- 历史数据由 `storage.backfill_balance_with_free_pool()` 在 `init_db` 时**幂等**回填
  （依据每行 raw_data），不要手动改库
- **回填时必须显式跳过解析不出 `surplus` 的行**，不能用 `parse_pools` 的 fallback 兜底——
  那会把历史余额静默改写成 0（已用 11 组用例验证过，见「变更管理」）
- 充值档位固定 25/50/75/100/150/200 度，`snap_recharge_amount()` 向上取最近档；
  `last_recharge` 只看付费池上升、`last_grant` 只看赠送池上升，**两者不可混用**

阈值与预测：

- 全系统只有一个阈值 `config.LOW_BALANCE_THRESHOLD`（`PREDICTION_THRESHOLD` 已删除，勿复活双阈值）
- **预测窗口优先取最近 3 天**（`PREDICTION_RECENT_WINDOW_DAYS`），窗口内部用简单时间加权
  （总消耗 ÷ 总天数）。**不做近因加权、不做日历分型**——
  用户实测：工作日/周末无差异（用电支配变量是气温/空调），且短窗口已足够「新」，
  再叠加时间衰减只会多一个没人调的参数（`PREDICTION_RECENCY_HALF_LIFE` 已删除，勿复活）
- **自动回退**：主窗口若区间数 < `PREDICTION_MIN_SAMPLES`（12）或观测时长 <
  `PREDICTION_MIN_OBSERVED_DAYS`（0.5 天），按 3→7→12→30 天逐级放宽，
  取第一个满足门槛的；放宽时 `prediction_method='extended'` 且可信度打 6 折
- 字段语义：`days_remaining` = 电量**耗尽**天数；`days_to_threshold` = 降到阈值天数
  （预测预警邮件用它触发）；`predicted_date` = 到阈值日期；
  `window_days` = 本次实际使用的窗口天数；`prediction_method` = `recent`/`extended`
- 停服日处理：余额**日线**用 `balance_trend(..., drop_early_tail=True)` 跳过
  「当日最晚记录在中午之前」的天（日末无读数会画出假平台）；
  用量柱状图与预测基于区间分摊，停服日用电仍被正确计入，不受影响

仪表盘「日均用量」「本月用量」（易踩坑，已踩过一次）：

- 二者都基于 `_pool_intervals()` 的**相邻记录配对**，**单条记录配不出区间**。
  间隔很多天再打开时，短窗口内只有刚采到的 1 条记录，会显示 0——已修：
  - 日均（速率）用 `avg_daily_usage()`：7→14→30 天逐级放宽窗口，取到区间为止
  - 本月用量（累计）用 `month_usage_allocated()`：复用 `daily_usage_trend()` 的
    「按时间占比分摊到自然日、只累加本月部分」，跨月区间按天拆分不串月
  - 页面「按近 N 天…」的 N 来自 `usage_avg_window_days`，**不要写死 7**
- 本月用量与「每日用电量」柱状图同源同口径，两者必然对得上；换算法前先读这里

## 配置管理

- `config.py` 会被 `/api/config` POST **整文件重写**（模板在 `web.py api_save_config` 内）。
  新增配置项必须同时加入三处：`web.py` 生成模板、`setup_config.py` 模板、`config.py.example`，
  否则用户在 UI 保存一次就会丢失该配置
- 收件人字段统一用 `ALERT_EMAILS`（列表），**旧的单值 `ALERT_EMAIL` 已移除**，勿复活
- 生效方式：UI 保存会 `importlib.reload(config)` 并重新调度；手工编辑 config.py 需重启服务
- 邮箱配置至今是占位符（`no@email.com`），邮件功能用户明确暂缓——**不要「顺手」去修好它**

## 测试与验证约定（用户硬性要求）

1. 声称完成前必须用浏览器实测：页面渲染正常 + 控制台零报错，并在回复中写明生效条件
   （重启服务 / Ctrl+F5）
2. 后端验证起 **5101 临时实例**：脚本里 `sys.path.insert(0, 项目目录)`；
   需要隔离数据库时先 `cp electric_data.db` 到临时目录再 `chdir` 过去启动
   （`storage.DB_PATH` 是相对路径）
3. **5101 僵尸进程坑**：Windows 的 `SO_REUSEADDR` 允许多个进程同时绑定 5101，
   curl/浏览器会打到旧代码实例，造成「改了没生效」的假象。复测前必须清掉所有 5101 LISTENING 进程
4. 不要拿真实 `electric_data.db` 做写入实验；不要调用 DELETE 类接口（清空记录/清空日志）
5. `/api/check` 会真实登录并查询学校接口，测试不要高频调用

## 前端约定

- 图表是原生 canvas：**新增 canvas 必须把其 id 加入 `style.css` 的画布尺寸规则**
  （`#dailyChart, #monthlyChart, #hourlyChart, #dailyUsageChart { width:100%; height:100% }`）。
  漏加会导致画布显示尺寸跟随绘图缓冲区，越画越大、不随窗口缩放、高分屏发糊（已踩过一次）
- 折线横轴按真实时间等比分布（无记录的小时/日期不压缩）；柱状图纵轴从 0 起；
  数值/日期标注密度按每柱可用宽度自适应
- 图表数据经 `#chartData` 元素的 `data-*` 属性注入（Jinja `tojsonfilter`）
- 「电量趋势」三个 tab 共用同一份按天序列：7 天取尾部 7 点，30 天取全量（后端 `-30 days`）。
  历史上存在过 12 个月的 `data-monthly` 序列，前端从未消费，已删除
- 页面新鲜度：60 秒轮询 `/api/version`（只返回最新记录时间戳），有变化才整页刷新；
  设置保存成功后 1 秒整页刷新

## 变更管理

- **版本号与推送须用户同意（2026-11-21 用户规定）**：确定版本号、`git push` 到远端、
  创建 GitHub Release，都必须先在对话中获得用户明确同意后才可执行；未经同意一律不动远端
- `部署说明.md` 是项目变更日志（新章节加在文件顶部标题下）：每次功能性改动补一节，
  写清动机/改法/验证数据
- 数据库表结构不要改（历史数据零迁移是硬约束）；确需变更时写幂等迁移，
  参考 `backfill_balance_with_free_pool` 的写法
- **删除/重构代码前先写脚本做穷举比对**，不要凭印象判断「这个没用了」：
  CSS 要同时检查静态 class 与 JS 动态拼接（`showAlert` 的 `alert-${type}` 就是静态扫不到的）；
  涉及数据写入的重构要构造边界用例对比新旧实现（`parse_pools` 的 fallback 曾差点把余额清零）

## 已知遗留 / 勿动

- `config.py.backup`：UI 保存配置时的自动备份，每次保存被覆盖
- 数据库里的 `prediction_records` 表（约 490 行）是**已废弃的准确率追踪子系统**遗留。
  准确率评估的样本独立性不成立（490 条预测实际只对应 6 次「跌破阈值」事件，
  平均分 26.9 无统计意义），代码已全部移除；表与数据按「历史零迁移」原则保留不动，
  新库不再创建该表。见到它不要以为还有功能在读它。
- `electric_monitor.log` 混有 GBK 编码历史行：读日志必须 `errors='replace'`
  （`/api/logs` 已容错，新代码勿改回严格解码）。新写入的行已是纯 UTF-8
  （`app.py` 的 FileHandler 显式指定了 encoding）
- 已删除、勿再引入：`room_finder.py`（查房间功能已由 `setup_config.py` 覆盖）、
  `start.bat` / `start.sh`（早期 conda 方案遗留）、
  `bad_records_backup_20260829.json`（他房脏数据备份，事故已处理完毕，含他人数据不宜留存）、
  预测准确率追踪子系统（`save_prediction_record` / `evaluate_prediction_accuracy` /
  `/api/prediction/accuracy`+`/analytics` / `PREDICTION_ACCURACY_EVALUATION`，页面零消费者）
