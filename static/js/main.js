// 全局变量
let hourlyChartData, dailyChartData, dailyUsageData = [];
let currentChart = 'daily'; // 当前显示的图表类型
let dailyUsageRange = 7; // 每日用电量柱状图的时间范围（天）

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
    drawCharts();
    loadSettings();
});

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
            updatePredictionDisplay(data);
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
function clearAllRecords(event) {
    if (!confirm('确定要清空所有历史记录吗？此操作不可恢复。')) {
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
function deleteRecord(recordId, event) {
    if (!confirm('确定要删除这条记录吗？')) {
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

// 打开设置模态框
function openSettings() {
    document.getElementById('settingsModal').style.display = 'block';
    loadSettings();
}

// 关闭设置模态框
function closeSettings() {
    document.getElementById('settingsModal').style.display = 'none';
}

function loadSettings() {
    fetch('/api/config')
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                document.getElementById('threshold').value = data.config.threshold || 10.00;
                document.getElementById('emails').value = (data.config.emails || []).join('\n');
                document.getElementById('checkFrequency').value = data.config.check_frequency || 30;
            }
        })
        .catch(error => {
            console.error('加载设置失败:', error);
        });
}

// 图表切换函数
function switchChart(chartType) {
    // 更新标签状态
    document.querySelectorAll('.chart-tab').forEach(tab => {
        tab.classList.remove('active');
    });
    document.querySelector(`[data-chart="${chartType}"]`).classList.add('active');
    
    // 切换图表容器显示
    document.getElementById('hourlyChartContainer').style.display = chartType === 'hourly' ? 'block' : 'none';
    document.getElementById('dailyChartContainer').style.display = chartType === 'daily' ? 'block' : 'none';
    document.getElementById('monthlyChartContainer').style.display = chartType === 'monthly' ? 'block' : 'none';
    
    currentChart = chartType;
    drawCharts();
}

// 绘制所有图表
// 三个 tab 共用同一份按天序列：7 天取尾部 7 点，30 天取全量（后端 -30 days）
function drawCharts() {
    if (currentChart === 'hourly') {
        drawChart('hourlyChart', hourlyChartData, 'hourly');
    } else if (currentChart === 'daily') {
        drawChart('dailyChart', (dailyChartData || []).slice(-7), 'daily');
    } else {
        drawChart('monthlyChart', dailyChartData, 'daily');
    }
    drawDailyUsageChart();
}

// 绘制电量趋势图
function chartPalette() {
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    return {
        text: dark ? '#76809a' : '#8f98aa',
        grid: dark ? '#262f45' : '#e7eaf1',
        line: dark ? '#8677f2' : '#5b4bd8',
        pointFill: dark ? '#161d2d' : '#ffffff',
        pointHover: dark ? '#c9c2fb' : '#4338ca',
        fillTop: dark ? 'rgba(134, 119, 242, 0.25)' : 'rgba(91, 75, 216, 0.20)',
        fillBottom: dark ? 'rgba(134, 119, 242, 0.02)' : 'rgba(91, 75, 216, 0.015)'
    };
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
        if (chartType === 'monthly') {
            return { text: String(points[index].label), full: true };
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

// 切换每日用电量柱状图的时间范围（数据始终是近30天序列，按需切片）
function switchDailyUsageRange(days) {
    document.querySelectorAll('[data-durange]').forEach(tab => tab.classList.remove('active'));
    document.querySelector(`[data-durange="${days}"]`).classList.add('active');
    dailyUsageRange = days;
    drawDailyUsageChart();
}

// 每日用电量柱状图：纵轴从0起（柱状图如实反映比例），最后一天是今日的累计
function drawDailyUsageChart() {
    const canvas = document.getElementById('dailyUsageChart');
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

    const points = (dailyUsageData || [])
        .slice(-dailyUsageRange)
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
    const slot = chartWidth / n;
    const barWidth = Math.min(slot * 0.62, 46);

    points.forEach((point, index) => {
        const x = margin.left + slot * index + (slot - barWidth) / 2;
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

    points.forEach((point, index) => {
        if (index % valueStep !== 0 && index !== n - 1) return;
        const x = margin.left + slot * index + slot / 2;
        ctx.fillText(point.value.toFixed(1), x, pointY(point.value) - 6);
    });

    // X轴日期标签：M/D，最后一根标"今日"；同样用实测宽度决定密度
    ctx.font = '10px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.fillStyle = p.text;
    const dateLabels = points.map((point, index) => {
        if (index === n - 1) return '今日';
        const date = new Date(point.label + 'T00:00:00');
        return Number.isFinite(date.getTime())
            ? (date.getMonth() + 1) + '/' + date.getDate()
            : '';
    });
    const widestDate = dateLabels.reduce(
        (max, label) => Math.max(max, label ? ctx.measureText(label).width : 0), 0);
    const dateNeeded = widestDate + LABEL_GAP;
    const dateStep = Math.max(1, Math.ceil(dateNeeded / slot));

    points.forEach((point, index) => {
        if (index % dateStep !== 0 && index !== n - 1) return;
        const x = margin.left + slot * index + slot / 2;
        const label = dateLabels[index];
        if (label) ctx.fillText(label, x, margin.top + chartHeight + 24);
    });
}

// 数据新鲜度探针（每60秒）：只拉最新记录时间戳（/api/version 近零开销），
// 发现新数据才整页刷新，代替原来每10分钟拉全量统计的重轮询
setInterval(() => {
    const modal = document.getElementById('settingsModal');
    if (modal && modal.style.display === 'block') return; // 设置弹窗打开时不打断
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

// 点击模态框外部关闭
window.addEventListener('click', function(event) {
    const settingsModal = document.getElementById('settingsModal');
    if (event.target === settingsModal) {
        closeSettings();
    }
});

window.addEventListener('keydown', function(event) {
    if (event.key === 'Escape') {
        closeSettings();
    }
});

// 日志相关函数

// 切换日志显示
function toggleLogs(event) {
    const logsCard = document.getElementById('logsCard');
    const btn = getActionElement(event);
    const btnText = btn.querySelector('.btn-text');
    
    if (logsCard.style.display === 'none') {
        logsCard.style.display = 'block';
        btnText.textContent = '隐藏日志';
        refreshLogs(null);
        
        // 滚动到日志模块
        logsCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else {
        logsCard.style.display = 'none';
        btnText.textContent = '查看日志';
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
    if (!confirm('确定要清空所有日志吗？此操作不可恢复！')) {
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

// 设置表单提交处理
document.addEventListener('DOMContentLoaded', function() {
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

// ── 深色模式切换 ──
const themeToggle = document.getElementById('theme-toggle');
if (themeToggle) {
    const applyThemeIcon = () => {
        const current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
        themeToggle.textContent = current === 'dark' ? '🌙' : '☀️';
        themeToggle.setAttribute('aria-label', current === 'dark' ? '切换浅色模式' : '切换深色模式');
        const meta = document.querySelector('meta[name="theme-color"]');
        if (meta) meta.setAttribute('content', current === 'dark' ? '#0e1320' : '#312e81');
    };
    applyThemeIcon();
    themeToggle.addEventListener('click', () => {
        const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        try { localStorage.setItem('theme', next); } catch (e) {}
        applyThemeIcon();
        drawCharts();
    });
}
