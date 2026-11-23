// 全局变量
let hourlyChartData, dailyChartData, dailyUsageData = [];
let currentChart = 'daily'; // 当前显示的图表类型
let dailyUsageRange = 7; // 每日用电量柱状图的时间范围（天）
let chartHistory;
let chartHistoryLoading = false;
let chartHistoryFailed = false;
const chartInspectors = new Map();
const chartRangeStorageKey = 'electricity-chart-ranges';

function readBrowserSetting(key) {
    try {
        return localStorage.getItem(key);
    } catch {
        return null;
    }
}

function saveBrowserSetting(key, value) {
    try {
        if (value === null) localStorage.removeItem(key);
        else localStorage.setItem(key, value);
    } catch {
        // 存储不可用时保留当前页面的状态。
    }
}

// ── 主题（color-scheme meta 方案）─────────────────────────────
// 三态数据模型：localStorage 无值 = 跟随系统；'light'/'dark' = 手动固定。
// CSS 通过 :root:has(head meta[content=...]) 跟随 meta；light-dark() token
// 随 color-scheme 自动切换，因此主题切换只需要改 meta。
function storedColorScheme() {
    const scheme = readBrowserSetting('color-scheme');
    return scheme === 'light' || scheme === 'dark' ? scheme : null;
}

const systemColorScheme = window.matchMedia('(prefers-color-scheme: dark)');
let colorScheme = storedColorScheme();

function systemScheme() {
    return systemColorScheme.matches ? 'dark' : 'light';
}

// 当前实际渲染的模式：手动覆盖优先，否则跟随系统
function resolvedScheme() {
    return colorScheme ?? systemScheme();
}

function applyColorScheme() {
    const scheme = resolvedScheme();
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.content = colorScheme ?? 'light dark';
    // data-scheme 只服务"颜色以外"的分支（切换按钮显示哪个图标/文案）
    document.documentElement.dataset.scheme = scheme;
    const metaTheme = document.querySelector('meta[name="theme-color"]');
    if (metaTheme) metaTheme.content = scheme === 'dark' ? '#0d1417' : '#f2f5f6';
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
    bindChartInteractions();
    restoreChartRanges();
    updateSampleFreshness();
    loadUpdateStatus();

    const setupForm = document.getElementById('setupForm');
    if (setupForm) {
        setupForm.addEventListener('submit', submitSetup);
        openSetup();
    }

    document.querySelectorAll('form[data-settings-page]').forEach(form => {
        form.addEventListener('submit', submitSettings);
    });
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
const confirmButton = confirmDialog?.querySelector('[value="confirm"]');
const confirmVerification = document.getElementById('confirmVerification');
const confirmVerificationLabel = document.getElementById('confirmVerificationLabel');
const confirmVerificationInput = document.getElementById('confirmVerificationInput');
let confirmResolver = null;
let confirmWord = '';

function matchesConfirmation() {
    return !confirmWord || confirmVerificationInput?.value.trim() === confirmWord;
}

if (confirmDialog) {
    confirmVerificationInput?.addEventListener('input', () => {
        confirmButton.disabled = !matchesConfirmation();
    });
    confirmVerificationInput?.addEventListener('keydown', event => {
        if (event.key === 'Enter' && !event.isComposing) event.preventDefault();
    });
    confirmDialog.addEventListener('close', () => {
        const confirmed = confirmDialog.returnValue === 'confirm' && matchesConfirmation();
        confirmDialog.returnValue = ''; // 复位：Esc 关闭不带值，浏览器对旧值行为不一
        const resolve = confirmResolver;
        confirmResolver = null;
        confirmWord = '';
        if (confirmVerificationInput) confirmVerificationInput.value = '';
        if (resolve) resolve(confirmed);
    });
}

// verificationWord 用于需要输入指定文字的操作。
function confirmAction(options = {}) {
    const { title = '确认操作', message = '', confirmText = '确定', verificationWord = '' } = options;
    if (!confirmDialog) {
        return Promise.resolve(!verificationWord && window.confirm(message));
    }
    if (confirmDialog.open || confirmResolver || (verificationWord && !confirmVerificationInput)) {
        return Promise.resolve(false);
    }

    document.getElementById('confirmTitle').textContent = title;
    document.getElementById('confirmMessage').textContent = message;
    confirmWord = verificationWord;
    if (confirmVerificationInput) {
        confirmVerification.hidden = !verificationWord;
        confirmVerificationInput.value = '';
        confirmVerificationLabel.textContent = verificationWord ? `输入「${verificationWord}」确认` : '';
    }
    confirmButton.textContent = confirmText;
    confirmButton.disabled = !matchesConfirmation();

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
            const rendered = document.getElementById('last-update').dateTime;
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

    // 更新预计剩余天数和预警日期。
    const daysElement = document.getElementById('prediction-days');
    const days = prediction.days_remaining;
    const hasDays = Number.isFinite(days);
    daysElement.textContent = hasDays ? days.toFixed(1) : '--';
    daysElement.classList.toggle('text-danger', hasDays && days <= 3);
    daysElement.classList.toggle('text-warning', hasDays && days > 3 && days <= 7);
    document.getElementById('prediction-unit').hidden = !hasDays;
    document.getElementById('prediction-date').textContent = prediction.predicted_date || '--';
    document.getElementById('prediction-date-row').hidden = !prediction.predicted_date;

    // 更新预测方式与可信度。
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
        message: '全部电费历史和预警记录将被永久删除。',
        confirmText: '清空全部记录',
        verificationWord: '清空',
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
    if (settingsModal.open) return;
    const selected = settingsNav.querySelector('[aria-current="true"]') ?? settingsNav.firstElementChild;
    openSettingsPage(selected);
    settingsModal.showModal();
}

const settingsModal = document.getElementById('settingsModal');
const settingsNav = document.getElementById('settingsNav');
const settingsContent = document.getElementById('settingsContent');
const settingsPageTitle = document.getElementById('settingsPageTitle');
let settingsLoad;
let settingsSaving = false;

settingsModal.addEventListener('close', () => {
    if (settingsModal.open) return;
    settingsLoad?.abort();
    const password = document.getElementById('smtpPassword');
    if (password) password.value = '';
});

function closeSettings() {
    if (settingsModal.open) settingsModal.close();
}

function openSettingsPage(trigger) {
    const previous = settingsNav.querySelector('[aria-current="true"]');
    if (previous === trigger && settingsModal.open) return;
    settingsLoad?.abort();
    if (previous) {
        previous.removeAttribute('aria-current');
        const previousPage = document.getElementById(previous.getAttribute('aria-controls'));
        previousPage.hidden = true;
        if (previousPage.elements?.password) previousPage.elements.password.value = '';
    }

    const page = document.getElementById(trigger.getAttribute('aria-controls'));
    trigger.setAttribute('aria-current', 'true');
    page.hidden = false;
    settingsPageTitle.textContent = trigger.textContent.trim();
    settingsContent.scrollTop = 0;
    if (settingsModal.open) settingsPageTitle.focus({ preventScroll: true });
    if (page instanceof HTMLFormElement) loadSettings(page);
    if (page.dataset.settingsPage === 'data') refreshLogs(null);
}

async function loadSettings(form) {
    settingsLoad?.abort();
    const controller = new AbortController();
    settingsLoad = controller;
    const button = form.querySelector('button[type="submit"]');
    const status = form.querySelector('[data-settings-status]');
    button.disabled = true;
    status.hidden = true;
    try {
        const response = await fetch('/api/config', { cache: 'no-store', signal: controller.signal });
        const data = await response.json();
        if (!data.success) throw new Error('Settings request failed');
        const { threshold, emails, check_frequency, smtp } = data.config;
        const fields = form.elements;
        if (form.dataset.settingsPage === 'monitoring') {
            fields.threshold.value = threshold ?? 10;
            fields.check_frequency.value = check_frequency || 30;
        } else {
            fields.emails.value = (emails || []).join('\n');
            fields.username.value = smtp.username ?? '';
            fields.server.value = smtp.server ?? '';
            fields.port.value = smtp.port ?? '';
            fields.password.value = '';
            document.getElementById('smtp-password-help').textContent = smtp.has_password
                ? '已保存，留空保留。更换发件邮箱或服务器时请重新填写。'
                : '填写邮箱服务提供的 SMTP 授权码或密码。';
        }
        button.disabled = false;
    } catch {
        if (controller.signal.aborted) return;
        status.textContent = '加载设置失败，请切换分类后重试。';
        status.classList.add('text-danger');
        status.hidden = false;
    }
}

async function submitSettings(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type="submit"]');
    if (settingsSaving || button.disabled || form.hidden) return;
    const status = form.querySelector('[data-settings-status]');
    const fields = form.elements;
    const section = form.dataset.settingsPage;
    const payload = section === 'monitoring'
        ? { section, threshold: fields.threshold.valueAsNumber, check_frequency: fields.check_frequency.valueAsNumber }
        : {
            section,
            emails: fields.emails.value.split('\n').filter(email => email.trim()),
            smtp: {
                username: fields.username.value,
                password: fields.password.value,
                server: fields.server.value,
                port: fields.port.valueAsNumber,
            },
        };

    settingsSaving = true;
    status.hidden = true;
    setButtonLoading(button, true);
    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!data.success) {
            status.textContent = data.message || '保存失败。';
        } else {
            if (fields.password) fields.password.value = '';
            status.textContent = '设置已保存，页面即将刷新。';
            status.classList.remove('text-danger');
            status.hidden = false;
            setTimeout(() => location.reload(), 1000);
            return;
        }
    } catch {
        status.textContent = '保存失败，请稍后重试。';
    }
    status.classList.add('text-danger');
    status.hidden = false;
    settingsSaving = false;
    setButtonLoading(button, false);
}

const autoUpdateToggle = document.getElementById('autoUpdateToggle');
const availableUpdate = document.getElementById('availableUpdate');
let latestAvailableVersion;

function renderAvailableUpdate() {
    const visible = autoUpdateToggle.checked && latestAvailableVersion;
    availableUpdate.hidden = !visible;
    availableUpdate.textContent = visible ? `发现新版本 ${latestAvailableVersion}` : '';
}

async function loadUpdateStatus() {
    try {
        const response = await fetch('/api/update/settings', { cache: 'no-store' });
        if (!response.ok) return;
        const data = await response.json();
        latestAvailableVersion = data.latest_version;
        renderAvailableUpdate();
    } catch {
        // 保留现有提示，仍可手动检查更新。
    }
}

autoUpdateToggle.addEventListener('change', async () => {
    const enabled = autoUpdateToggle.checked;
    const status = document.getElementById('autoUpdateStatus');
    autoUpdateToggle.disabled = true;
    status.hidden = true;
    try {
        const response = await fetch('/api/update/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled }),
        });
        if (!response.ok) throw new Error('Update preference save failed');
        if (enabled) loadUpdateStatus();
    } catch {
        autoUpdateToggle.checked = !enabled;
        status.textContent = '自动检查更新设置保存失败，请重试。';
        status.hidden = false;
    } finally {
        autoUpdateToggle.disabled = false;
        renderAvailableUpdate();
    }
});

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
        if (typeof data.update_available === 'boolean') {
            latestAvailableVersion = data.update_available ? data.latest_version : null;
            renderAvailableUpdate();
        }
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
        } else {
            chartInspectors.get('monthlyChart')?.setPlot(null);
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
    const inspector = chartInspectors.get(canvasId);
    inspector?.setPlot(null);

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
        .map(item => ({ label: String(item[0]), value: Number(item[1]) }))
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
    const padding = (dataMax - dataMin || Math.max(Math.abs(dataMax) * 0.08, 1)) * 0.12;
    const lower = dataMin < 0 ? dataMin - padding : Math.max(0, dataMin - padding);
    const upper = dataMax + padding;
    const tickStep = chartAxisStep(upper - lower);
    const firstTick = Math.floor(lower / tickStep);
    const lastTick = Math.ceil(upper / tickStep);
    const maxBalance = lastTick * tickStep;
    const balanceRange = (lastTick - firstTick) * tickStep;
    const pointY = value => margin.top + (maxBalance - value) / balanceRange * chartHeight;

    // 横轴：优先按真实时间等比分布（无记录的小时/日期不再被压缩），
    // 时间解析失败或只有一个点时退回等间距
    const times = points.map(({label}) => Date.parse(label.replace(' ', 'T')));
    const start = times[0];
    const end = times.at(-1);
    const span = end - start;
    const useTime = span > 0 && times.every(Number.isFinite);
    const stepX = chartWidth / Math.max(points.length - 1, 1);
    const xForTime = time => margin.left + (span > 0 ? (time - start) / span * chartWidth : 0);
    const positions = times.map((time, index) => useTime ? xForTime(time) : margin.left + stepX * index);
    const pointX = index => positions[index];
    const plot = {
        points, left: margin.left, right: margin.left + chartWidth,
        top: margin.top, bottom: margin.top + chartHeight, width, height, pointY,
        centerX: pointX, findIndex: x => nearestChartPoint(positions, x), markPoint: true,
    };
    inspector?.setPlot(plot);
    drawChartGrid(ctx, plot, tickStep, firstTick, lastTick, p);

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

    if (chartType === 'all' && Number.isFinite(start) && Number.isFinite(end)) {
        drawHistoryLabels(ctx, start, end, plot, p, xForTime);
        return;
    }

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

// 每日用电量柱状图从零起，全部记录按自然日定位。
function drawDailyUsageChart() {
    const canvas = document.getElementById('dailyUsageChart');
    if (!canvas) return;
    const inspector = chartInspectors.get(canvas.id);
    inspector?.setPlot(null);
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

    const maxUsage = points.reduce((max, point) => Math.max(max, point.value), 0);
    const { step, ticks } = usageScale(maxUsage);
    const yMax = step * ticks;
    const pointY = value => margin.top + (1 - value / yMax) * chartHeight;
    const plotBottom = margin.top + chartHeight;

    const n = points.length;
    const firstDay = Date.parse(points[0].label);
    const days = all ? points.map(point => (Date.parse(point.label) - firstDay) / 86400000) : null;
    const slot = chartWidth / (days ? days[n - 1] + 1 : n);
    const slotLeft = index => margin.left + slot * (days ? days[index] : index);
    const barWidth = Math.min(slot * 0.62, 46);
    const pointBySlot = new Map(points.map((_, index) => [days ? days[index] : index, index]));
    const plot = {
        points, left: margin.left, right: margin.left + chartWidth,
        top: margin.top, bottom: plotBottom, width, height, pointY,
        centerX: index => slotLeft(index) + slot / 2,
        findIndex: x => pointBySlot.get(Math.floor((x - margin.left) / slot)),
    };
    inspector?.setPlot(plot);
    drawChartGrid(ctx, plot, step, 0, ticks, p);

    points.forEach((point, index) => {
        const x = slotLeft(index) + (slot - barWidth) / 2;
        const y = pointY(point.value);
        const h = plotBottom - y;

        ctx.fillStyle = index === n - 1 ? p.pointHover : p.line;
        ctx.beginPath();
        if (ctx.roundRect) {
            ctx.roundRect(x, y, barWidth, h, [3, 3, 0, 0]);
        } else {
            ctx.rect(x, y, barWidth, h);
        }
        ctx.fill();
    });

    if (all) {
        const xForTime = time => margin.left + ((time - firstDay) / 86400000 + 0.5) * slot;
        drawHistoryLabels(ctx, firstDay, Date.parse(points[n - 1].label), plot, p, xForTime);
        return;
    }

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

    const keepLabel = (index, step) => index % step === 0 || index === n - 1;
    points.forEach((point, index) => {
        if (!keepLabel(index, valueStep)) return;
        const x = slotLeft(index) + slot / 2;
        ctx.fillText(point.value.toFixed(1), x, pointY(point.value) - 6);
    });

    // 短区间继续显示逐日标签，最后一根对应今天时才标为「今日」。
    ctx.font = '10px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.fillStyle = p.text;
    const today = new Date().toDateString();
    const dateLabels = points.map((point, index) => {
        const date = new Date(point.label + 'T00:00:00');
        if (index === n - 1 && date.toDateString() === today) return '今日';
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

function chartAxisStep(span) {
    const target = span / 5;
    const magnitude = 10 ** Math.floor(Math.log10(target));
    const normalized = target / magnitude;
    const step = [1, 2, 2.5, 5, 10].reduce((closest, candidate) =>
        Math.abs(candidate - normalized) < Math.abs(closest - normalized) ? candidate : closest);
    return step * magnitude;
}

function usageScale(maxUsage) {
    const ceiling = Math.max(maxUsage * 1.05, 1);
    const step = chartAxisStep(ceiling);
    return { step, ticks: Math.ceil(ceiling / step) };
}

function drawChartGrid(ctx, plot, step, firstTick, lastTick, palette) {
    ctx.lineWidth = 1;
    ctx.font = '11px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.fillStyle = palette.text;
    ctx.strokeStyle = palette.grid;
    ctx.textAlign = 'right';
    ctx.fillText('度', plot.left - 10, 13);
    for (let i = lastTick; i >= firstTick; i--) {
        const value = step * i;
        const y = plot.pointY(value);
        ctx.beginPath();
        ctx.moveTo(plot.left, y);
        ctx.lineTo(plot.right, y);
        ctx.stroke();
        ctx.fillText(String(Number(value.toFixed(4))), plot.left - 10, y + 4);
    }
}

function drawHistoryLabels(ctx, firstDay, lastDay, plot, palette, xForTime) {
    const dayMs = 86400000;
    const span = (lastDay - firstDay) / dayMs + 1;
    const fits = Math.max(1, Math.floor((plot.right - plot.left) / 64));
    const first = new Date(firstDay);
    const last = new Date(lastDay);
    const singleYear = first.getUTCFullYear() === last.getUTCFullYear();
    const times = [firstDay];
    const showMonths = span >= 45;
    if (showMonths) {
        const months = (last.getUTCFullYear() - first.getUTCFullYear()) * 12
            + last.getUTCMonth() - first.getUTCMonth();
        const stride = Math.max(1, Math.ceil((months + 1) / fits));
        for (let offset = stride; offset <= months; offset += stride) {
            times.push(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + offset, 1));
        }
    } else {
        const stride = Math.max(1, Math.ceil(span / fits));
        for (let offset = stride; offset < span; offset += stride) times.push(firstDay + offset * dayMs);
    }

    ctx.fillStyle = palette.text;
    ctx.strokeStyle = palette.grid;
    ctx.font = '11px -apple-system, BlinkMacSystemFont, sans-serif';
    if (singleYear) {
        ctx.textAlign = 'right';
        ctx.fillText(`${first.getUTCFullYear()}年`, plot.right, 13);
    }
    ctx.textAlign = 'center';
    let previousRight = -Infinity;
    let previousYear;
    for (const time of times) {
        const date = new Date(time);
        const year = date.getUTCFullYear();
        const prefix = !singleYear && year !== previousYear ? `${year}年` : '';
        const day = showMonths ? '' : `${date.getUTCDate()}日`;
        const text = `${prefix}${date.getUTCMonth() + 1}月${day}`;
        const halfWidth = ctx.measureText(text).width / 2;
        const tickX = xForTime(time);
        const x = Math.max(plot.left + halfWidth, Math.min(tickX, plot.right - halfWidth));
        if (x - halfWidth < previousRight + 10) continue;
        ctx.beginPath();
        ctx.moveTo(tickX, plot.bottom);
        ctx.lineTo(tickX, plot.bottom + 5);
        ctx.stroke();
        ctx.fillText(text, x, plot.bottom + 24);
        previousRight = x + halfWidth;
        previousYear = year;
    }
}

function nearestChartPoint(positions, x) {
    let first = 0;
    let last = positions.length - 1;
    while (first < last) {
        const middle = Math.floor((first + last) / 2);
        if (x <= (positions[middle] + positions[middle + 1]) / 2) last = middle;
        else first = middle + 1;
    }
    return first;
}

function bindChartInteractions() {
    for (const canvas of document.querySelectorAll('[data-chart-inspect]')) {
        chartInspectors.set(canvas.id, bindChartInteraction(canvas));
    }
    document.addEventListener('pointerdown', event => {
        for (const [id, inspector] of chartInspectors) {
            if (event.target.id !== id) inspector.hideReading();
        }
    });
}

function bindChartInteraction(canvas) {
    const guide = canvas.parentElement.querySelector('.chart-guide');
    const reading = canvas.parentElement.querySelector('.chart-reading');
    const readingDate = reading.querySelector('.chart-reading-date');
    const readingValue = reading.querySelector('.chart-reading-value');
    let plot;
    let selected = null;

    function hideReading() {
        if (selected === null) return;
        selected = null;
        guide.hidden = true;
        reading.hidden = true;
    }

    function showReading(index) {
        if (index === selected) return;
        selected = index;
        const point = plot.points[index];
        const x = plot.centerX(index);
        const y = plot.pointY(point.value);
        guide.style.left = `${x}px`;
        guide.style.top = `${plot.top}px`;
        guide.style.height = `${plot.bottom - plot.top}px`;
        guide.toggleAttribute('data-point', Boolean(plot.markPoint));
        if (plot.markPoint) guide.style.setProperty('--chart-point-y', `${y - plot.top}px`);
        guide.hidden = false;

        readingDate.textContent = point.label.slice(0, 16);
        readingValue.textContent = `${point.value.toFixed(2)} 度`;
        reading.hidden = false;
        const left = Math.max(8, Math.min(x - reading.offsetWidth / 2, plot.width - reading.offsetWidth - 8));
        const above = y - reading.offsetHeight - 9;
        const top = above >= 8 ? above : Math.min(y + 9, plot.height - reading.offsetHeight - 8);
        reading.style.left = `${left}px`;
        reading.style.top = `${top}px`;
    }

    function inspect(event) {
        if (!plot) return;
        const rect = canvas.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        if (x < plot.left || x > plot.right || y < plot.top || y > plot.bottom) {
            hideReading();
            return;
        }
        const index = plot.findIndex(x);
        if (index === undefined) hideReading();
        else showReading(index);
    }

    canvas.addEventListener('pointermove', event => {
        if (event.pointerType !== 'touch') inspect(event);
    });
    canvas.addEventListener('pointerdown', inspect);
    canvas.addEventListener('pointerleave', event => {
        if (event.pointerType !== 'touch') hideReading();
    });
    canvas.addEventListener('pointercancel', hideReading);
    canvas.addEventListener('blur', hideReading);
    canvas.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
            hideReading();
            return;
        }
        if (!plot) return;
        const last = plot.points.length - 1;
        const index = selected ?? last;
        const keys = { Home: 0, End: last, ArrowLeft: Math.max(0, index - 1), ArrowRight: Math.min(last, index + 1) };
        if (!Object.hasOwn(keys, event.key)) return;
        event.preventDefault();
        showReading(keys[event.key]);
    });

    return {
        hideReading,
        setPlot(next) {
            hideReading();
            plot = next;
            canvas.tabIndex = plot ? 0 : -1;
        },
    };
}

// 数据新鲜度探针（每60秒）：只拉最新记录时间戳（/api/version 近零开销），
// 发现新数据才整页刷新。任何原生对话框打开时都不打断（含确认框）。
setInterval(() => {
    updateSampleFreshness();
    if (document.querySelector('dialog[open]')) return;
    fetch('/api/version')
        .then(response => response.json())
        .then(v => {
            const rendered = document.getElementById('last-update').dateTime;
            if (v.version && v.version !== rendered) {
                location.reload();
            }
        })
        .catch(error => console.error('数据探针失败:', error));
}, 60000); // 1分钟

// 日志相关函数

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
    logContent.parentElement.scrollTop = 0;
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
    const status = document.getElementById('logStatus');
    status.hidden = true;

    try {
        const response = await fetch('/api/logs', {
            method: 'DELETE'
        });

        const data = await response.json();

        status.classList.toggle('text-danger', !data.success);
        if (data.success) {
            status.textContent = '日志已清空。';
            displayLogs([]);
        } else {
            status.textContent = data.message || '清空日志失败。';
        }
    } catch (error) {
        console.error('清空日志失败:', error);
        status.textContent = '网络错误，清空日志失败。';
        status.classList.add('text-danger');
    } finally {
        status.hidden = false;
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
const autoThemeToggle = document.getElementById('autoThemeToggle');

function refreshTheme() {
    autoThemeToggle.checked = colorScheme === null;
    applyColorScheme();
    drawCharts(); // canvas 不随 CSS 自动变色。
}

function setColorScheme(scheme) {
    colorScheme = scheme;
    saveBrowserSetting('color-scheme', scheme);
    refreshTheme();
}

autoThemeToggle.checked = colorScheme === null;
applyColorScheme();
autoThemeToggle.addEventListener('change', () => {
    setColorScheme(autoThemeToggle.checked ? null : resolvedScheme());
});
themeToggle.addEventListener('click', () => {
    setColorScheme(resolvedScheme() === 'dark' ? 'light' : 'dark');
});
systemColorScheme.addEventListener('change', () => {
    if (colorScheme === null) refreshTheme();
});
window.addEventListener('storage', event => {
    if (event.key === 'color-scheme' || event.key === null) {
        colorScheme = storedColorScheme();
        refreshTheme();
    }
});

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
