// 全局变量
let hourlyChartData, dailyChartData, dailyUsageData = [];
let currentChart = 'daily'; // 当前显示的图表类型
let dailyUsageRange = 7; // 每日用电量柱状图的时间范围（天）
let chartHistory;
let chartHistoryLoading = false;
let chartHistoryFailed = false;
const chartRangeStorageKey = 'electricity-chart-ranges';

// ── 主题（color-scheme meta 方案）─────────────────────────────
// 三态数据模型：localStorage 无值 = 跟随系统；'light'/'dark' = 手动固定。
// CSS 通过 :root:has(head meta[content=...]) 跟随 meta；light-dark() token
// 随 color-scheme 自动切换，因此主题切换只需要改 meta。
function storedColorScheme() {
    try {
        const v = localStorage.getItem('color-scheme');
        return (v === 'light' || v === 'dark') ? v : null;
    } catch (e) { return null; }
}

function systemScheme() {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

// 当前实际渲染的模式：手动覆盖优先，否则跟随系统
function resolvedScheme() {
    return storedColorScheme() ?? systemScheme();
}

function applyColorScheme() {
    const stored = storedColorScheme();
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.content = stored ?? 'light dark';
    // data-scheme 只服务"颜色以外"的分支（切换按钮显示哪个图标/文案）
    document.documentElement.dataset.scheme = resolvedScheme();
    const metaTheme = document.querySelector('meta[name="theme-color"]');
    if (metaTheme) metaTheme.setAttribute('content', resolvedScheme() === 'dark' ? '#0d1417' : '#f2f5f6');
}

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', function() {
    // 从数据属性中获取图表数据
    const chartDataEl = document.getElementById('chartData');
    if (chartDataEl) {
        try {
            hourlyChartData = JSON.parse(chartDataEl.dataset.hourly || '[]');
            dailyChartData = JSON.parse(chartDataEl.dataset.daily || '[]');
            dailyUsageData = JSON.parse(chartDataEl.dataset.dailyUsage || '[]');
        } catch (e) {
            console.error('图表数据解析错误:', e);
            hourlyChartData = [];
            dailyChartData = [];
            dailyUsageData = [];
        }
    }

    // 初始化图表和设置
    restoreChartRanges();
    updateSampleFreshness();
    loadSettings();

    const setupForm = document.getElementById('setupForm');
    if (setupForm) {
        setupForm.addEventListener('submit', submitSetup);
        openSetup();
    }

    // 设置表单提交（与首屏初始化共用同一个 DOMContentLoaded 监听）
    const settingsForm = document.getElementById('settingsForm');
    if (settingsForm) {
        settingsForm.addEventListener('submit', function(e) {
            e.preventDefault();

            const btn = this.querySelector('button[type="submit"]');
            setButtonLoading(btn, true);

            const formData = {
                threshold: parseFloat(document.getElementById('threshold').value),
                emails: document.getElementById('emails').value.split('\n').filter(email => email.trim()),
                check_frequency: parseInt(document.getElementById('checkFrequency').value)
            };

            fetch('/api/config', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(formData)
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showAlert('设置保存成功，页面即将刷新！', 'success');
                    // 保存后整页刷新，让阈值/横幅/预测面板立即反映新配置
                    setTimeout(() => {
                        location.reload();
                    }, 1000);
                } else {
                    showAlert(data.message || '保存失败', 'error');
                }
            })
            .catch(error => {
                console.error('保存设置失败:', error);
                showAlert('保存失败，请稍后重试', 'error');
            })
            .finally(() => {
                setButtonLoading(btn, false);
            });
        });
    }
});

function openSetup() {
    const dialog = document.getElementById('setupModal');
    if (dialog && !dialog.open) dialog.showModal();
}

async function submitSetup(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type="submit"]');
    const status = document.getElementById('setupStatus');
    setButtonLoading(button, true);
    status.hidden = false;
    status.textContent = '正在核对学校宿舍信息…';
    try {
        const response = await fetch('/api/setup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(Object.fromEntries(new FormData(form))),
        });
        if (!response.ok) throw new Error('Setup request failed');
        const data = await response.json();
        if (data.success) {
            location.reload();
            return;
        }
        status.textContent = data.message;
    } catch {
        status.textContent = '暂时无法保存宿舍设置，请稍后重试。';
    } finally {
        setButtonLoading(button, false);
    }
}

// 显示提示信息
function showAlert(message, type = 'success') {
    const alertEl = document.createElement('div');
    alertEl.className = `alert alert-${type}`;
    alertEl.textContent = message;

    const toastRegion = document.getElementById('toastRegion');
    if (!toastRegion) return;
    toastRegion.appendChild(alertEl);

    setTimeout(() => {
        alertEl.remove();
    }, 5000);
}

// 设置按钮加载状态
function setButtonLoading(button, loading) {
    if (!button) return;

    if (loading) {
        button.disabled = true;
        button.classList.add('loading');
    } else {
        button.disabled = false;
        button.classList.remove('loading');
    }
}

function getActionElement(event) {
    return event?.currentTarget || event?.target?.closest('button, a') || null;
}

// ── 通用确认对话框（替代 window.confirm）─────────────────────
// 原生 <dialog> 提供 top layer / 焦点圈定 / Esc；confirmDialog 的 close
// 事件里读取 returnValue 并复位（指南要求，避免旧值串场）。
const confirmDialog = document.getElementById('confirmDialog');
let confirmResolver = null;

if (confirmDialog) {
    confirmDialog.addEventListener('close', () => {
        const value = confirmDialog.returnValue;
        confirmDialog.returnValue = ''; // 复位：Esc 关闭不带值，浏览器对旧值行为不一
        const resolve = confirmResolver;
        confirmResolver = null;
        if (resolve) resolve(value === 'confirm');
    });
}

// options: { title, message, confirmText } → Promise<boolean>
function confirmAction(options = {}) {
    const { title = '确认操作', message = '', confirmText = '确定' } = options;
    if (!confirmDialog) {
        return Promise.resolve(window.confirm(message)); // 极端兜底
    }
    if (confirmDialog.open) return Promise.resolve(false);

    document.getElementById('confirmTitle').textContent = title;
    document.getElementById('confirmMessage').textContent = message;
    const confirmBtn = confirmDialog.querySelector('[value="confirm"]');
    if (confirmBtn) confirmBtn.textContent = confirmText;

    return new Promise((resolve) => {
        confirmResolver = resolve;
        confirmDialog.showModal(); // autofocus 在"取消"上，破坏性操作默认落点是安全动作
    });
}

// 立即检查
function checkNow(event) {
    const btn = getActionElement(event);
    setButtonLoading(btn, true);

    fetch('/api/check', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showAlert('检查完成！', 'success');
                setTimeout(() => {
                    location.reload();
                }, 1000);
            } else {
                showAlert(data.message || '检查失败', 'error');
            }
        })
        .catch(error => {
            console.error('检查失败:', error);
            showAlert('检查失败，请稍后重试', 'error');
        })
        .finally(() => {
            setButtonLoading(btn, false);
        });
}


// 刷新预测数据
function refreshPrediction(event) {
    const btn = getActionElement(event);
    setButtonLoading(btn, true);

    fetch('/api/prediction')
        .then(response => response.json())
        .then(data => {
            const rendered = document.getElementById('last-update').textContent.trim();
            if (data.sample_time && data.sample_time !== rendered) {
                location.reload();
                return;
            }
            updatePredictionDisplay(data);
            updateSampleFreshness();
            showAlert('预测数据已更新！', 'success');
        })
        .catch(error => {
            console.error('刷新预测失败:', error);
            showAlert('刷新预测失败，请稍后重试', 'error');
        })
        .finally(() => {
            setButtonLoading(btn, false);
        });
}

function updateSampleFreshness() {
    const notice = document.getElementById('sample-stale');
    const staleAt = Number(notice.dataset.staleAt);
    const stale = staleAt > 0 && Date.now() >= staleAt;
    notice.hidden = !stale;

    const lowBalance = document.getElementById('lowBalanceBanner');
    lowBalance.hidden = stale || lowBalance.dataset.low !== 'true';
    document.getElementById('current-balance').classList.toggle('text-danger', !lowBalance.hidden);
}

// 更新预测显示
function updatePredictionDisplay(prediction) {
    if (!prediction) return;

    // 更新预警阈值
    if (prediction.threshold !== undefined) {
        document.getElementById('prediction-threshold').textContent = prediction.threshold.toFixed(1) + '度';
    }



    // 更新估计日均与已观测时长
    document.getElementById('prediction-rate').textContent =
        (prediction.daily_usage_avg ?? 0).toFixed(2) + '度';
    const observedDays = prediction.data_points ? prediction.data_points.observed_days : null;
    document.getElementById('prediction-observed').textContent =
        (observedDays !== null && observedDays !== undefined) ? observedDays + '天' : '--';

    // 更新预计剩余天数
    const daysElement = document.getElementById('prediction-days');
    if (prediction.days_remaining !== null && prediction.days_remaining !== undefined) {
        daysElement.textContent = prediction.days_remaining.toFixed(1) + '天';

        // 根据剩余天数设置颜色
        daysElement.className = 'forecast-days';
        if (prediction.days_remaining <= 3) {
            daysElement.classList.add('text-danger');
        } else if (prediction.days_remaining <= 7) {
            daysElement.classList.add('text-warning');
        }
    } else {
        daysElement.textContent = '--';
        daysElement.className = 'forecast-days';
    }

    // 更新预计到达日期
    document.getElementById('prediction-date').textContent = prediction.predicted_date || '--';

    // 更新预测方式与可信度（合成一格显示，避免 prediction-metrics 网格出现半行）
    const methodElement = document.getElementById('prediction-method');
    if (methodElement) {
        methodElement.textContent = predictionMethodLabel(prediction);
    }
}

// 预测方式 + 可信度的中文文案（模板首屏与"重新计算"共用同一套说法）
// prediction_method：recent = 主窗口（默认近3天），extended = 样本不足已放宽窗口
function predictionMethodLabel(prediction) {
    if (!prediction) return '--';
    const win = prediction.window_days;
    let methodText;
    if (prediction.prediction_method === 'extended') {
        methodText = '近' + (win || '?') + '天（已放宽）';
    } else {
        methodText = '近' + (win || 3) + '天';
    }
    const confidenceText = { high: '高', medium: '中', low: '低' }[prediction.prediction_confidence] || '低';
    return methodText + ' · 可信度' + confidenceText;
}

// 清空所有记录
async function clearAllRecords(event) {
    if (!(await confirmAction({
        title: '清空全部历史记录',
        message: '将删除全部历史检查记录，此操作不可恢复。确定继续吗？',
        confirmText: '清空',
    }))) {
        return;
    }

    const btn = getActionElement(event);
    setButtonLoading(btn, true);

    fetch('/api/records', { method: 'DELETE' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showAlert('记录清空成功！', 'success');
                setTimeout(() => {
                    location.reload();
                }, 1000);
            } else {
                showAlert(data.message || '清空失败', 'error');
            }
        })
        .catch(error => {
            console.error('清空失败:', error);
            showAlert('清空失败，请稍后重试', 'error');
        })
        .finally(() => {
            setButtonLoading(btn, false);
        });
}

// 删除单条记录
async function deleteRecord(recordId, event) {
    if (!(await confirmAction({
        title: '删除这条记录',
        message: '删除后无法恢复（不影响已保存的其他记录）。确定删除吗？',
        confirmText: '删除',
    }))) {
        return;
    }

    fetch(`/api/records/${recordId}`, { method: 'DELETE' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showAlert('记录删除成功！', 'success');
                // 移除表格行
                event.target.closest('tr').remove();
            } else {
                showAlert(data.message || '删除失败', 'error');
            }
        })
        .catch(error => {
            console.error('删除失败:', error);
            showAlert('删除失败，请稍后重试', 'error');
        });
}

// 打开设置模态框（原生 dialog：焦点圈定 / Esc / 焦点归还均由浏览器处理）
function openSettings() {
    const modal = document.getElementById('settingsModal');
    if (modal && !modal.open) modal.showModal();
    loadSettings();
}

// 关闭设置模态框
function closeSettings() {
    const modal = document.getElementById('settingsModal');
    if (modal && modal.open) modal.close();
}

function loadSettings() {
    fetch('/api/config')
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                document.getElementById('threshold').value = data.config.threshold ?? 10.00;
                document.getElementById('emails').value = (data.config.emails || []).join('\n');
                document.getElementById('checkFrequency').value = data.config.check_frequency || 30;
            }
        })
        .catch(error => {
            console.error('加载设置失败:', error);
        });
}

function openUpdateDialog() {
    document.getElementById('updateDialog').showModal();
    checkForUpdates();
}

async function checkForUpdates() {
    const button = document.getElementById('checkUpdateButton');
    if (button.disabled) return;

    const status = document.getElementById('updateStatus');
    const releaseLink = document.getElementById('updateReleaseLink');
    setButtonLoading(button, true);
    status.textContent = '正在检查 GitHub 发布版本…';
    releaseLink.hidden = true;
    releaseLink.removeAttribute('href');

    try {
        const response = await fetch('/api/update', { cache: 'no-store' });
        if (!response.ok) throw new Error('Update request failed');
        const data = await response.json();
        status.textContent = data.message;
        if (data.release_url) {
            releaseLink.href = data.release_url;
            releaseLink.hidden = false;
        }
    } catch {
        status.textContent = '暂时无法检查更新，请稍后重试。';
    } finally {
        setButtonLoading(button, false);
    }
}

function restoreChartRanges() {
    let saved;
    try {
        saved = JSON.parse(sessionStorage.getItem(chartRangeStorageKey));
    } catch {
        // 浏览器禁用存储或过往数据损坏时使用默认范围。
    }
    const balance = ['hourly', 'daily', 'monthly', 'all'].includes(saved?.balance) ? saved.balance : 'daily';
    const usage = [7, 15, 30, 'all'].includes(saved?.usage) ? saved.usage : 7;
    switchChart(balance);
    switchDailyUsageRange(usage);
}

function rememberChartRanges() {
    try {
        sessionStorage.setItem(chartRangeStorageKey, JSON.stringify({ balance: currentChart, usage: dailyUsageRange }));
    } catch {
        // 禁用存储时仍可切换当前页面的范围。
    }
}

// 图表切换函数
function switchChart(chartType) {
    // 只在"电量趋势"这组 tab 内切换 active——旧版误用 .chart-tab 会把
    // "每日用电量"的时间范围 tab 一并清掉 active（互踩 bug），已修
    document.querySelectorAll('[data-chart]').forEach(tab => {
        const active = tab.dataset.chart === chartType;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-pressed', active);
    });

    // 切换图表容器显示（hidden 属性替代内联 display 样式）
    document.getElementById('hourlyChartContainer').hidden = chartType !== 'hourly';
    document.getElementById('dailyChartContainer').hidden = chartType !== 'daily';
    document.getElementById('monthlyChartContainer').hidden = chartType !== 'monthly' && chartType !== 'all';

    currentChart = chartType;
    rememberChartRanges();
    if (chartType === 'all') loadChartHistory();
    drawCharts();
}

// 两张图共用一次全量查询，切回短区间时仍可继续浏览。
async function loadChartHistory() {
    if (chartHistory || chartHistoryLoading) return;
    chartHistoryLoading = true;
    chartHistoryFailed = false;
    try {
        const response = await fetch('/api/charts/history', { cache: 'no-store' });
        if (!response.ok) throw new Error('Chart history request failed');
        const data = await response.json();
        if (!Array.isArray(data.balance_trend_daily) || !Array.isArray(data.daily_usage)) {
            throw new Error('Invalid chart history');
        }
        chartHistory = data;
    } catch {
        chartHistoryFailed = true;
    } finally {
        chartHistoryLoading = false;
        drawCharts();
    }
}

function showChartHistoryStatus(canvasId, statusId, needsHistory) {
    const status = document.getElementById(statusId);
    const visible = needsHistory && !chartHistory;
    document.getElementById(canvasId).hidden = visible;
    status.hidden = !visible;
    if (visible) {
        const message = chartHistoryFailed ? '加载失败，点击“全部”重试' : '正在加载全部记录…';
        if (status.textContent !== message) status.textContent = message;
    }
    return visible;
}

// 绘制所有图表
function drawCharts() {
    if (currentChart === 'hourly') {
        drawChart('hourlyChart', hourlyChartData, 'hourly');
    } else if (currentChart === 'daily') {
        drawChart('dailyChart', (dailyChartData || []).slice(-7), 'daily');
    } else {
        const all = currentChart === 'all';
        if (!showChartHistoryStatus('monthlyChart', 'balanceHistoryStatus', all)) {
            const balances = all ? chartHistory.balance_trend_daily : dailyChartData;
            drawChart('monthlyChart', balances, all ? 'all' : 'daily');
        }
    }
    drawDailyUsageChart();
}

// 绘制电量趋势图
// 图表配色取自 CSS token，但 canvas 只接受具体颜色值：自定义属性在
// getPropertyValue 下会原样返回 "light-dark(...)"，嵌套函数不会被解析，
// 传给 canvas 会抛 "could not be parsed as a color"。因此用一个隐藏
// 探针元素把 token 赋给 color/background，让浏览器解析成具体 rgb 再读取。
function chartPalette() {
    const style = getComputedStyle(document.documentElement);
    const probe = document.createElement('span');
    probe.style.position = 'absolute';
    probe.style.opacity = '0';
    probe.style.pointerEvents = 'none';
    document.body.appendChild(probe);

    // 让浏览器把 token（可能是 light-dark(...)）解析为具体颜色字符串
    const resolve = (name, fallback) => {
        const raw = (style.getPropertyValue(name) || '').trim();
        if (!raw) return fallback;
        probe.style.color = '';
        probe.style.color = raw;
        const resolved = getComputedStyle(probe).color;
        return resolved || fallback;
    };

    const palette = {
        text: resolve('--chart-text', '#63757c'),
        grid: resolve('--chart-grid', '#e4ebee'),
        line: resolve('--accent', '#0b7f70'),
        pointFill: resolve('--surface', '#ffffff'),
        pointHover: resolve('--accent-strong', '#096a5e'),
        fillTop: resolve('--accent-fill-top', 'rgba(11, 127, 112, 0.2)'),
        fillBottom: resolve('--accent-fill-bottom', 'rgba(11, 127, 112, 0.015)')
    };
    probe.remove();
    return palette;
}

function drawChart(canvasId, chartData, chartType) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    const width = canvas.offsetWidth;
    const height = canvas.offsetHeight;
    if (!width || !height) return;

    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const p = chartPalette();

    const points = (chartData || [])
        .map(item => ({ label: item[0], value: Number(item[1]) }))
        .filter(item => Number.isFinite(item.value));

    if (points.length === 0) {
        ctx.fillStyle = p.text;
        ctx.font = '13px -apple-system, BlinkMacSystemFont, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('积累数据后，这里会显示电量趋势', width / 2, height / 2);
        return;
    }

    const compact = width < 620;
    const margin = {
        top: 26,
        right: compact ? 16 : 24,
        bottom: 40,
        left: compact ? 48 : 58
    };
    const chartWidth = width - margin.left - margin.right;
    const chartHeight = height - margin.top - margin.bottom;
    const balances = points.map(item => item.value);
    const dataMax = Math.max(...balances);
    const dataMin = Math.min(...balances);
    const rawRange = dataMax - dataMin || Math.max(dataMax * 0.08, 1);
    const maxBalance = dataMax + rawRange * 0.12;
    const minBalance = Math.max(0, dataMin - rawRange * 0.12);
    const balanceRange = maxBalance - minBalance || 1;
    const pointY = value => margin.top + (maxBalance - value) / balanceRange * chartHeight;

    // 横轴：优先按真实时间等比分布（无记录的小时/日期不再被压缩），
    // 时间解析失败或只有一个点时退回等间距
    const timeOf = label => {
        const s = String(label);
        return s.includes(' ') ? new Date(s.replace(' ', 'T')).getTime()
                               : new Date(s + 'T00:00:00').getTime();
    };
    const times = points.map(item => timeOf(item.label));
    const stepX = chartWidth / Math.max(points.length - 1, 1);
    let pointX = index => margin.left + stepX * index;
    let spanMs = 0;
    if (points.length >= 2 && times.every(Number.isFinite)) {
        const tMin = Math.min(...times);
        const tMax = Math.max(...times);
        spanMs = tMax - tMin;
        if (spanMs > 0) {
            pointX = index => margin.left + (times[index] - tMin) / spanMs * chartWidth;
        }
    }

    // 克制的横向网格，减少折线图的视觉噪音。
    ctx.lineWidth = 1;
    ctx.font = '11px -apple-system, BlinkMacSystemFont, sans-serif';
    for (let i = 0; i <= 4; i++) {
        const y = margin.top + (chartHeight / 4) * i;
        ctx.strokeStyle = p.grid;
        ctx.beginPath();
        ctx.moveTo(margin.left, y);
        ctx.lineTo(margin.left + chartWidth, y);
        ctx.stroke();

        const value = maxBalance - (balanceRange / 4) * i;
        ctx.fillStyle = p.text;
        ctx.textAlign = 'right';
        ctx.fillText(value.toFixed(1), margin.left - 10, y + 4);
    }

    const buildLinePath = () => {
        ctx.beginPath();
        points.forEach((point, index) => {
            const x = pointX(index);
            const y = pointY(point.value);
            if (index === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
    };

    // 面积渐变让趋势更易扫读，但保持透明，不遮盖刻度。
    buildLinePath();
    ctx.lineTo(pointX(points.length - 1), margin.top + chartHeight);
    ctx.lineTo(pointX(0), margin.top + chartHeight);
    ctx.closePath();
    const fill = ctx.createLinearGradient(0, margin.top, 0, margin.top + chartHeight);
    fill.addColorStop(0, p.fillTop);
    fill.addColorStop(1, p.fillBottom);
    ctx.fillStyle = fill;
    ctx.fill();

    buildLinePath();
    ctx.strokeStyle = p.line;
    ctx.lineWidth = 2.5;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();

    // 仅突出最后一个点，避免 30 天数据全部打标签。
    const lastIndex = points.length - 1;
    const lastPoint = points[lastIndex];
    ctx.fillStyle = p.pointFill;
    ctx.strokeStyle = p.line;
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.arc(pointX(lastIndex), pointY(lastPoint.value), 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = p.pointHover;
    ctx.font = '600 11px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(`${lastPoint.value.toFixed(2)}`, pointX(lastIndex), Math.max(16, pointY(lastPoint.value) - 12));

    // X 轴标签：先为每个点生成「文本 + 是否完整日期」，再用实测宽度贪心放置。
    //
    // 背景：横轴按真实时间等比分布，服务停跑时会出现大段空洞（点少且中间
    // 空窗十几个小时），此时两端点位挤在一起。若每个标签都写成
    // "M/D HH时"（约 55px 宽），相邻标签必然重叠、难以分辨。
    //
    // 策略：
    // 1. 只有「首个点」和「跨天边界」才带日期，同日内只写 "HH时"（约 21px）
    // 2. 用 measureText 实测每段宽度，从右往左贪心放置，会重叠就跳过
    // 3. 最后一个点（最新数据）必标，且优先保留带日期的形式
    const LABEL_GAP_X = 6; // 相邻标签之间至少留出的水平间隙

    const buildLabel = (index) => {
        if (chartType === 'all') return { text: points[index].label, full: false };
        if (chartType === 'hourly') {
            const date = new Date(String(points[index].label).replace(' ', 'T'));
            if (!Number.isFinite(date.getTime())) return { text: '', full: false };
            const hh = date.getHours();
            // 跨天时，首个点和换日的那个点带上日期
            const prev = index > 0
                ? new Date(String(points[index - 1].label).replace(' ', 'T'))
                : null;
            const isDayStart = !prev || !Number.isFinite(prev.getTime())
                || prev.getDate() !== date.getDate()
                || prev.getMonth() !== date.getMonth();
            const text = isDayStart
                ? (date.getMonth() + 1) + '/' + date.getDate() + ' ' + hh + '时'
                : hh + '时';
            return { text, full: isDayStart };
        }
        const date = new Date(points[index].label + 'T00:00:00');
        return Number.isFinite(date.getTime())
            ? { text: (date.getMonth() + 1) + '/' + date.getDate(), full: false }
            : { text: '', full: false };
    };

    ctx.fillStyle = p.text;
    ctx.font = '10px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'center';

    const builtLabels = points.map((_, i) => buildLabel(i));
    const labelW = builtLabels.map(l => (l.text ? ctx.measureText(l.text).width : 0));

    // 放置顺序：带日期的标签优先（它们是时间轴的锚点），其次从右往左，
    // 保证最新点在右、且日期锚点不会被同日的纯小时标签挤掉。
    const order = [];
    for (let i = points.length - 1; i >= 0; i--) {
        if (!labelW[i]) continue;
        if (builtLabels[i].full) order.push(i);
    }
    for (let i = points.length - 1; i >= 0; i--) {
        if (!labelW[i]) continue;
        if (!builtLabels[i].full) order.push(i);
    }
    // 最新点提到最前，确保它一定被放置
    const lastIdx = points.length - 1;
    const li = order.indexOf(lastIdx);
    if (li > 0) {
        order.splice(li, 1);
        order.unshift(lastIdx);
    }

    const keep = new Array(points.length).fill(false);
    const placed = []; // [{left, right}]
    order.forEach((i) => {
        const w = labelW[i];
        const cx = pointX(i);
        let left = cx - w / 2;
        let right = cx + w / 2;
        // 首尾标签贴边时把文本整体内移，避免被画布边缘裁掉
        if (left < margin.left - 8) {
            right += (margin.left - 8) - left;
            left = margin.left - 8;
        } else if (right > margin.left + chartWidth + 8) {
            left -= right - (margin.left + chartWidth + 8);
            right = margin.left + chartWidth + 8;
        }
        const collides = placed.some(
            (p) => !(right + LABEL_GAP_X <= p.left || left >= p.right + LABEL_GAP_X));
        if (!collides) {
            keep[i] = true;
            placed.push({ left, right });
        }
    });

    points.forEach((point, index) => {
        if (!keep[index] || !builtLabels[index].text) return;
        // 与放置阶段一致：贴边时把文本中心内移
        const w = labelW[index];
        const cx = pointX(index);
        let drawX = cx;
        if (cx - w / 2 < margin.left - 8) {
            drawX = margin.left - 8 + w / 2;
        } else if (cx + w / 2 > margin.left + chartWidth + 8) {
            drawX = margin.left + chartWidth + 8 - w / 2;
        }
        ctx.fillText(builtLabels[index].text, drawX, margin.top + chartHeight + 24);
    });
}

let resizeTimer;
window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(drawCharts, 120);
});

// 短区间按需切片，“全部”使用完整日序列。
function switchDailyUsageRange(days) {
    document.querySelectorAll('[data-durange]').forEach(tab => {
        const active = tab.dataset.durange === String(days);
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-pressed', active);
    });
    dailyUsageRange = days;
    rememberChartRanges();
    if (days === 'all') loadChartHistory();
    drawDailyUsageChart();
}

// 每日用电量柱状图：纵轴从0起（柱状图如实反映比例），最后一天是今日的累计
function drawDailyUsageChart() {
    const canvas = document.getElementById('dailyUsageChart');
    if (!canvas) return;
    const all = dailyUsageRange === 'all';
    if (showChartHistoryStatus('dailyUsageChart', 'usageHistoryStatus', all)) return;

    const ctx = canvas.getContext('2d');
    const width = canvas.offsetWidth;
    const height = canvas.offsetHeight;
    if (!width || !height) return;

    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const p = chartPalette();

    const usage = all ? chartHistory.daily_usage : (dailyUsageData || []).slice(-dailyUsageRange);
    const points = usage
        .map(item => ({ label: String(item[0]), value: Number(item[1]) }))
        .filter(item => Number.isFinite(item.value));

    if (points.length === 0) {
        ctx.fillStyle = p.text;
        ctx.font = '13px -apple-system, BlinkMacSystemFont, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('积累数据后，这里会显示每日用电量', width / 2, height / 2);
        return;
    }

    const compact = width < 620;
    const margin = {
        top: 26,
        right: compact ? 16 : 24,
        bottom: 40,
        left: compact ? 48 : 58
    };
    const chartWidth = width - margin.left - margin.right;
    const chartHeight = height - margin.top - margin.bottom;

    const maxUsage = Math.max(...points.map(item => item.value));
    const yMax = Math.max(maxUsage * 1.15, 1);
    const pointY = value => margin.top + (1 - value / yMax) * chartHeight;

    // 横向网格与Y轴刻度，样式与趋势折线图一致
    ctx.lineWidth = 1;
    ctx.font = '11px -apple-system, BlinkMacSystemFont, sans-serif';
    for (let i = 0; i <= 4; i++) {
        const y = margin.top + (chartHeight / 4) * i;
        ctx.strokeStyle = p.grid;
        ctx.beginPath();
        ctx.moveTo(margin.left, y);
        ctx.lineTo(margin.left + chartWidth, y);
        ctx.stroke();

        const value = (yMax / 4) * (4 - i);
        ctx.fillStyle = p.text;
        ctx.textAlign = 'right';
        ctx.fillText(value.toFixed(1), margin.left - 10, y + 4);
    }

    const n = points.length;
    const firstDay = Date.parse(points[0].label);
    const days = all ? points.map(point => (Date.parse(point.label) - firstDay) / 86400000) : null;
    const slot = chartWidth / (days ? days[n - 1] + 1 : n);
    const slotLeft = index => margin.left + slot * (days ? days[index] : index);
    const barWidth = Math.min(slot * 0.62, 46);

    points.forEach((point, index) => {
        const x = slotLeft(index) + (slot - barWidth) / 2;
        const y = pointY(point.value);
        const h = margin.top + chartHeight - y;

        ctx.fillStyle = index === n - 1 ? p.pointHover : p.line;
        ctx.beginPath();
        if (ctx.roundRect) {
            ctx.roundRect(x, y, barWidth, h, [3, 3, 0, 0]);
        } else {
            ctx.rect(x, y, barWidth, h);
        }
        ctx.fill();
    });

    // 数值与日期标注密度：用 canvas 实测文字宽度 + 最小间隙来判断能否每天都标，
    // 而不是猜固定像素阈值（高分屏/不同字号下固定值会误判，导致柱宽够却隔天标）
    const LABEL_GAP = 6; // 相邻标注之间至少留出的视觉间隙

    ctx.font = '600 10px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillStyle = p.text;
    // 取最长数值的宽度（如 "16.9"）作为单条数值标注的占位宽度
    const widestValue = points.reduce(
        (max, pt) => Math.max(max, ctx.measureText(pt.value.toFixed(1)).width), 0);
    const valueNeeded = widestValue + LABEL_GAP;
    const valueStep = Math.max(1, Math.ceil(valueNeeded / slot));

    const keepLabel = (index, step) => days
        ? (days[n - 1] - days[index]) % step === 0
        : index % step === 0 || index === n - 1;
    points.forEach((point, index) => {
        if (!keepLabel(index, valueStep)) return;
        const x = slotLeft(index) + slot / 2;
        ctx.fillText(point.value.toFixed(1), x, pointY(point.value) - 6);
    });

    // 全量视图显示年份；同样用实测宽度决定日期标签密度。
    ctx.font = '10px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.fillStyle = p.text;
    const today = new Date().toDateString();
    const dateLabels = points.map((point, index) => {
        const date = new Date(point.label + 'T00:00:00');
        if (index === n - 1 && (!all || date.toDateString() === today)) return '今日';
        if (all) return point.label;
        return Number.isFinite(date.getTime())
            ? (date.getMonth() + 1) + '/' + date.getDate()
            : '';
    });
    const widestDate = dateLabels.reduce(
        (max, label) => Math.max(max, label ? ctx.measureText(label).width : 0), 0);
    const dateNeeded = widestDate + LABEL_GAP;
    const dateStep = Math.max(1, Math.ceil(dateNeeded / slot));

    points.forEach((point, index) => {
        if (!keepLabel(index, dateStep)) return;
        const x = slotLeft(index) + slot / 2;
        const label = dateLabels[index];
        if (label) ctx.fillText(label, x, margin.top + chartHeight + 24);
    });
}

// 数据新鲜度探针（每60秒）：只拉最新记录时间戳（/api/version 近零开销），
// 发现新数据才整页刷新。任何原生对话框打开时都不打断（含确认框）。
setInterval(() => {
    updateSampleFreshness();
    if (document.querySelector('dialog[open]')) return;
    fetch('/api/version')
        .then(response => response.json())
        .then(v => {
            const rendered = ((document.getElementById('last-update') || {}).textContent || '').trim();
            if (v.version && rendered && v.version !== rendered) {
                location.reload();
            }
        })
        .catch(error => console.error('数据探针失败:', error));
}, 60000); // 1分钟

// 日志相关函数

// 切换日志显示
function toggleLogs(event) {
    const logsCard = document.getElementById('logsCard');
    const btn = getActionElement(event);
    const btnText = btn ? btn.querySelector('.btn-text') : null;

    if (logsCard.hidden) {
        logsCard.hidden = false;
        if (btnText) btnText.textContent = '隐藏日志';
        refreshLogs(null);

        // 滚动到日志模块
        logsCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else {
        logsCard.hidden = true;
        if (btnText) btnText.textContent = '查看日志';
    }
}

// 刷新日志
async function refreshLogs(event) {
    const btn = getActionElement(event);
    if (btn) setButtonLoading(btn, true);

    try {
        const limit = document.getElementById('logLimit')?.value || 50;
        const level = document.getElementById('logLevel')?.value || 'all';

        const response = await fetch(`/api/logs?limit=${limit}&level=${level}`);
        const data = await response.json();

        if (data.success) {
            displayLogs(data.logs);
        } else {
            document.getElementById('log-content').innerHTML =
                '<div class="log-empty">加载日志失败: ' + (data.message || '未知错误') + '</div>';
        }
    } catch (error) {
        console.error('获取日志失败:', error);
        document.getElementById('log-content').innerHTML =
            '<div class="log-empty">网络错误，无法获取日志</div>';
    } finally {
        if (btn) setButtonLoading(btn, false);
    }
}

// 显示日志
function displayLogs(logs) {
    const logContent = document.getElementById('log-content');

    if (!logs || logs.length === 0) {
        logContent.innerHTML = '<div class="log-empty">暂无日志记录</div>';
        return;
    }

    const logHtml = logs.map(log => {
        const timestamp = log.timestamp || '';
        const level = log.level || 'INFO';
        const message = log.message || '';

        return `
            <div class="log-entry ${level}">
                <span class="log-timestamp">${timestamp}</span>
                <span class="log-level ${level}">[${level}]</span>
                <span class="log-message">${escapeHtml(message)}</span>
            </div>
        `;
    }).join('');

    logContent.innerHTML = logHtml;

    // 日志按最新在前渲染，保持在顶部即可看到最新条目
    logContent.scrollTop = 0;
}

// 过滤日志
function filterLogs() {
    refreshLogs(null);
}

// 清空日志
async function clearLogs(event) {
    if (!(await confirmAction({
        title: '清空全部日志',
        message: '日志文件将被清空，此操作不可恢复！确定继续吗？',
        confirmText: '清空',
    }))) {
        return;
    }

    const btn = getActionElement(event);
    if (btn) setButtonLoading(btn, true);

    try {
        const response = await fetch('/api/logs', {
            method: 'DELETE'
        });

        const data = await response.json();

        if (data.success) {
            showAlert('日志已清空', 'success');
            document.getElementById('log-content').innerHTML =
                '<div class="log-empty">暂无日志记录</div>';
        } else {
            showAlert('清空日志失败: ' + (data.message || '未知错误'), 'error');
        }
    } catch (error) {
        console.error('清空日志失败:', error);
        showAlert('网络错误，清空日志失败', 'error');
    } finally {
        if (btn) setButtonLoading(btn, false);
    }
}

// HTML转义函数
function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, function(m) { return map[m]; });
}

// ── 主题切换（三态：跟随系统 / 固定浅色 / 固定深色）──────────
const themeToggle = document.getElementById('theme-toggle');
if (themeToggle) {
    applyColorScheme();

    themeToggle.addEventListener('click', () => {
        // 目标 = 当前渲染模式的相反面（用户意图是"切换我看到的"，而非"反转系统默认"）
        const target = resolvedScheme() === 'dark' ? 'light' : 'dark';
        try {
            if (target === systemScheme()) {
                // 目标与系统偏好一致 = 用户在撤销手动覆盖：删除存储值，
                // 之后的系统切换（如白天/黑夜自动切换）能继续生效
                localStorage.removeItem('color-scheme');
            } else {
                localStorage.setItem('color-scheme', target);
            }
        } catch (e) { /* 存储被禁用时仅本次会话生效 */ }
        applyColorScheme();
        drawCharts(); // canvas 不随 CSS 自动变色，需显式重绘
    });

    // 系统偏好变化：无手动覆盖时立即跟随
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
        applyColorScheme();
        drawCharts();
    });

    // 其他标签页改了主题：同步（storage 事件在本标签页不触发）
    window.addEventListener('storage', (e) => {
        if (e.key === 'color-scheme' || e.key === null) {
            applyColorScheme();
            drawCharts();
        }
    });
}

// ── closedby 渐进增强 ────────────────────────────────────────
// settingsModal 标了 closedby="any"（点击外部可关）。不支持 closedby 的
// 浏览器（Safari）模态默认只有 Esc 能关，这里补上点击外部关闭。
// 注意检测指针位置而非 event.target：<dialog> 自身的 padding 也算 target。
if (!('closedBy' in HTMLDialogElement.prototype)) {
    const outside = (e, dialog) => {
        const r = dialog.getBoundingClientRect();
        return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    };
    for (const dialog of document.querySelectorAll('dialog[closedby="any"]')) {
        let pressedOutside = false;
        dialog.addEventListener('pointerdown', (e) => { pressedOutside = outside(e, dialog); });
        // 按下与抬起都在外部才关闭：文本选择拖拽结束在遮罩上时不应误关
        dialog.addEventListener('pointerup', (e) => { if (pressedOutside && outside(e, dialog)) dialog.close(); });
    }
}
