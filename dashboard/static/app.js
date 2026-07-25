const API_URL = '/api/status';

const colors = {
    "Momentum RSI+EMA": "var(--color-momentum)",
    "VWAP Mean Reversion": "var(--color-vwap)",
    "Order Book Imbalance": "var(--color-obi)",
    "Bollinger Breakout": "var(--color-bollinger)",
    "Implied Prob. Arbitrage": "var(--color-arb)",
    "Microstructure Alpha": "var(--color-micro)",
    "Learned Alpha": "var(--color-learned)"
};

// Bir stratejinin işlemlerini (açık + çözülmüş) satır satır render eder.
// Açık işlemler önce (PENDING), sonra çözülmüş işlemler (WIN/LOSE + P&L).
function renderStrategyTrades(port) {
    const open = (port.open_trades || []).slice().reverse();       // en yeni önce
    const done = (port.last_10_trades || []).slice().reverse();    // en yeni önce
    const rows = [];

    const rowHtml = (t, status) => {
        const sideClass = t.side === 'UP' ? 'side-up' : 'side-down';
        let statusBadge, pnlHtml;
        if (status === 'OPEN') {
            statusBadge = `<span class="badge pending">OPEN</span>`;
            pnlHtml = `<span class="trade-pnl monospace muted">…</span>`;
        } else {
            const pnl = (typeof t.pnl === 'number') ? t.pnl : 0;
            const cls = t.result === 'WIN' ? 'win' : 'lose';
            const pnlCls = pnl >= 0 ? 'positive' : 'negative';
            statusBadge = `<span class="badge ${cls}">${t.result}</span>`;
            pnlHtml = `<span class="trade-pnl monospace ${pnlCls}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</span>`;
        }
        return `<div class="trade-row">
            <span class="side-badge ${sideClass}">${t.side}</span>
            <span class="trade-entry monospace">@${(t.entry_price ?? 0).toFixed(2)}</span>
            <span class="trade-amt monospace muted">$${t.amount}</span>
            ${statusBadge}
            ${pnlHtml}
        </div>`;
    };

    open.forEach(t => rows.push(rowHtml(t, 'OPEN')));
    done.forEach(t => rows.push(rowHtml(t, 'DONE')));

    if (rows.length === 0) {
        return `<div class="no-trades">Henüz işlem yok</div>`;
    }
    return rows.slice(0, 12).join('');
}

// Bakiye geçmişinden P&L sparkline (SVG) çizer.
function renderSparkline(history, initial) {
    const W = 280, H = 46, pad = 3;
    const hist = history || [];
    const vals = [initial, ...hist.map(h => h[1])];
    const n = vals.length;
    const min = Math.min(...vals), max = Math.max(...vals);
    const range = (max - min) || 1;
    const x = i => pad + (n <= 1 ? 0 : (i / (n - 1)) * (W - 2 * pad));
    const y = v => H - pad - ((v - min) / range) * (H - 2 * pad);
    const d = vals.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
    const last = vals[n - 1];
    const color = last >= initial ? 'var(--up-color)' : 'var(--down-color)';
    const yBase = y(initial).toFixed(1);
    const area = `${d} L${x(n - 1).toFixed(1)},${H - pad} L${pad},${H - pad} Z`;
    return `<svg class="sparkline" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        <path d="${area}" fill="${color}" opacity="0.10"/>
        <line x1="0" y1="${yBase}" x2="${W}" y2="${yBase}" stroke="rgba(255,255,255,0.15)" stroke-dasharray="3 3" stroke-width="0.7"/>
        <path d="${d}" fill="none" stroke="${color}" stroke-width="1.5"/>
    </svg>`;
}

let lastData = null;

// ---------- Mikroyapı paneli ----------
// Her gösterge: etiket, biçim, ve "yukarı mı iyi" yönü (renklendirme için)
const MICRO_SPEC = [
    { key: 'cvd_ratio_60s',      label: 'CVD 60s',        fmt: v => (v>=0?'+':'') + v.toFixed(2), signed: true,
      hint: 'Agresif alış/satış dengesi (+ = alıcılar agresif)' },
    { key: 'book_imbalance',     label: 'Emir Defteri',   fmt: v => (v>=0?'+':'') + v.toFixed(2), signed: true,
      hint: 'Binance derinlik dengesizliği (+ = kalın bid)' },
    { key: 'funding_rate',       label: 'Funding',        fmt: v => (v*100).toFixed(4) + '%', signed: true,
      hint: 'Perp funding — yüksek = long kalabalık (kontrarian)' },
    { key: 'basis_pct',          label: 'Basis',          fmt: v => (v>=0?'+':'') + v.toFixed(3) + '%', signed: true,
      hint: 'Perp primi (mark - spot)' },
    { key: 'oi_change_5m',       label: 'OI Δ5dk',        fmt: v => (v>=0?'+':'') + v.toFixed(3) + '%', signed: true,
      hint: 'Açık pozisyon değişimi — kaldıraç birikimi' },
    { key: 'momentum_5m',        label: 'Momentum 5dk',   fmt: v => (v>=0?'+':'') + v.toFixed(3) + '%', signed: true,
      hint: 'Son 5 dakikalık fiyat değişimi' },
    { key: 'tf_alignment',       label: 'TF Uyum',        fmt: v => (v>=0?'+':'') + v.toFixed(2), signed: true,
      hint: '1/3/5dk momentum aynı yöne mi bakıyor' },
    { key: 'realized_vol',       label: 'Oynaklık',       fmt: v => v.toFixed(3) + '%', signed: false,
      hint: 'Gerçekleşen oynaklık (5dk)' },
    { key: 'spread_bps',         label: 'Spread',         fmt: v => v.toFixed(3) + ' bps', signed: false,
      hint: 'Binance bid-ask spread' },
    { key: 'liquidation_flow_5m',label: 'Likidasyon',     fmt: v => (v>=0?'+$':'-$') + Math.abs(v).toLocaleString('en-US',{maximumFractionDigits:0}), signed: true,
      hint: '+ = short likidasyonu (yukarı itiş)' },
];

function renderMicro(micro) {
    const grid = document.getElementById('micro-grid');
    if (!grid) return;
    if (!micro || Object.keys(micro).length === 0) {
        grid.innerHTML = '<div class="muted" style="padding:8px;">Veri toplanıyor…</div>';
        return;
    }
    grid.innerHTML = MICRO_SPEC.map(s => {
        const v = micro[s.key];
        if (v === null || v === undefined) {
            return `<div class="micro-cell" title="${s.hint}">
                <span class="micro-label">${s.label}</span>
                <span class="micro-val muted monospace">—</span></div>`;
        }
        const cls = s.signed ? (v > 0 ? 'positive' : (v < 0 ? 'negative' : '')) : '';
        return `<div class="micro-cell" title="${s.hint}">
            <span class="micro-label">${s.label}</span>
            <span class="micro-val monospace ${cls}">${s.fmt(v)}</span></div>`;
    }).join('');
}

// ---------- Kalibrasyon rozeti (strateji kartında) ----------
// "Bu strateji %60 dediğinde gerçekten %60 tutturuyor mu?"
function renderCalibration(c) {
    if (!c) return '';
    if (!c.samples) {
        return `<div class="calib muted">🎯 Kalibrasyon: veri bekleniyor</div>`;
    }
    if (!c.active) {
        return `<div class="calib muted">🎯 Kalibrasyon: ${c.samples}/15 örnek (henüz ham güven kullanılıyor)</div>`;
    }
    const pred = c.mean_predicted, act = c.empirical_accuracy;
    const gap = (pred != null && act != null) ? (act - pred) : null;
    // Tahmin ile gerçek arasındaki fark küçükse "dürüst" model
    const cls = gap == null ? '' : (Math.abs(gap) < 0.06 ? 'positive' : 'negative');
    const label = gap == null ? '' :
        (Math.abs(gap) < 0.06 ? 'dürüst' : (gap < 0 ? 'fazla iyimser' : 'fazla temkinli'));
    const brierTxt = c.brier != null
        ? `Brier ${c.brier.toFixed(3)}${c.brier_calibrated != null ? ' → ' + c.brier_calibrated.toFixed(3) : ''}`
        : '';
    return `<div class="calib">
        🎯 <strong>Kalibrasyon aktif</strong> (${c.samples} örnek)
        <div class="calib-row">
            <span>Dediği: <span class="monospace">${pred != null ? (pred*100).toFixed(0)+'%' : '—'}</span></span>
            <span>Gerçek: <span class="monospace">${act != null ? (act*100).toFixed(0)+'%' : '—'}</span></span>
            <span class="${cls}">${label}</span>
        </div>
        <div class="muted" style="font-size:0.68rem;">${brierTxt} <span style="opacity:.7">(düşük iyi, 0.25=rastgele)</span></div>
    </div>`;
}

// ---------- Öğrenen model paneli ----------
function renderModel(m) {
    const host = document.getElementById('model-body');
    if (!host) return;
    if (!m) { host.innerHTML = '<div class="muted">Model yok.</div>'; return; }

    if (!m.ready) {
        const need = 25 - (m.n_updates || 0);
        host.innerHTML = `<div class="model-learning">
            📚 Öğreniyor… <strong>${m.n_updates || 0}</strong> pencere işlendi.
            Tahmin vermeye başlamak için <strong>${need > 0 ? need : 0}</strong> pencere daha gerekiyor.
            <div class="muted" style="margin-top:4px;font-size:0.74rem;">
                Model, her 5dk penceresinin başındaki mikroyapıdan sonucu öğrenir. Kural yazılmamıştır.
            </div>
        </div>`;
        return;
    }

    const acc = m.accuracy, ll = m.log_loss, good = m.beats_random;
    const verdictCls = good ? 'positive' : 'negative';
    const verdictTxt = good
        ? 'Rastgeleden İYİ — gerçek öngörü sinyali var'
        : 'Henüz rastgele seviyesinde — bu yüzden işlem açmıyor';

    const feats = (m.top_features || []).map(f => {
        const cls = f.weight >= 0 ? 'positive' : 'negative';
        const w = Math.min(Math.abs(f.weight) / 0.5, 1) * 100;
        return `<div class="feat-row">
            <span class="feat-name">${f.feature}</span>
            <div class="feat-bar"><div class="feat-fill ${cls}" style="width:${w}%"></div></div>
            <span class="feat-w monospace ${cls}">${f.weight >= 0 ? '+' : ''}${f.weight.toFixed(3)}</span>
        </div>`;
    }).join('');

    host.innerHTML = `
        <div class="model-stats">
            <div class="model-stat"><span class="micro-label">Öğrenilen Pencere</span>
                <span class="micro-val monospace">${m.n_updates}</span></div>
            <div class="model-stat"><span class="micro-label">İsabet</span>
                <span class="micro-val monospace">${acc != null ? (acc*100).toFixed(1)+'%' : '—'}</span></div>
            <div class="model-stat"><span class="micro-label">Son 100</span>
                <span class="micro-val monospace">${m.recent_accuracy != null ? (m.recent_accuracy*100).toFixed(1)+'%' : '—'}</span></div>
            <div class="model-stat"><span class="micro-label">Log-Loss</span>
                <span class="micro-val monospace ${verdictCls}">${ll != null ? ll.toFixed(4) : '—'}</span>
                <span class="muted" style="font-size:0.65rem;">rastgele = 0.693</span></div>
        </div>
        <div class="model-verdict ${verdictCls}">${good ? '✅' : '⚠️'} ${verdictTxt}</div>
        <div class="feat-title">Modelin öğrendiği en etkili sinyaller</div>
        <div class="feat-list">${feats || '<span class="muted">—</span>'}</div>`;
}

// ---------- Birleşik equity curve (tüm stratejiler) ----------
function renderEquityChart(strategies) {
    const host = document.getElementById('equity-chart');
    const legend = document.getElementById('equity-legend');
    if (!host) return;

    const series = (strategies || []).map(s => {
        const p = s.portfolio || {};
        const init = p.initial_balance || 1000;
        const pts = [init, ...((p.balance_history || []).map(h => h[1]))];
        return { name: s.name, color: colors[s.name] || '#fff', pts, init };
    }).filter(s => s.pts.length > 0);

    if (series.length === 0) { host.innerHTML = ''; return; }

    const W = 1000, H = 170, pad = 8;
    const maxLen = Math.max(...series.map(s => s.pts.length), 2);
    const allVals = series.flatMap(s => s.pts);
    const min = Math.min(...allVals), max = Math.max(...allVals);
    const range = (max - min) || 1;
    const x = i => pad + (maxLen <= 1 ? 0 : (i / (maxLen - 1)) * (W - 2 * pad));
    const y = v => H - pad - ((v - min) / range) * (H - 2 * pad);
    const init = series[0].init;

    const paths = series.map(s => {
        const d = s.pts.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
        return `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="1.8" opacity="0.9"/>`;
    }).join('');

    host.innerHTML = `<svg class="equity-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        <line x1="0" y1="${y(init).toFixed(1)}" x2="${W}" y2="${y(init).toFixed(1)}"
              stroke="rgba(255,255,255,0.2)" stroke-dasharray="4 4" stroke-width="0.8"/>
        ${paths}
    </svg>`;

    if (legend) {
        legend.innerHTML = series.map(s => {
            const last = s.pts[s.pts.length - 1];
            const pnl = last - s.init;
            const cls = pnl >= 0 ? 'positive' : 'negative';
            return `<div class="legend-item">
                <span class="legend-dot" style="background:${s.color}"></span>
                <span class="legend-name">${s.name}</span>
                <span class="legend-val monospace ${cls}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</span>
            </div>`;
        }).join('');
    }
}

// ---------- Strateji aç/kapa ----------
async function toggleStrategy(name) {
    try {
        await fetch(`/api/strategy/${encodeURIComponent(name)}/toggle`, { method: 'POST' });
        fetchData();
    } catch (e) { console.error('toggle hatası', e); }
}
window.toggleStrategy = toggleStrategy;

async function fetchData() {
    try {
        const res = await fetch(API_URL);
        if (!res.ok) throw new Error('Network response was not ok');
        const data = await res.json();
        renderDashboard(data);
    } catch (e) {
        console.error('Error fetching data:', e);
    }
}

function renderDashboard(data) {
    // 1. Update Header
    updateTicker('btc-price', data.btc_price, true);
    
    if (data.polymarket) {
        document.getElementById('pm-question').textContent = data.polymarket.question;
        updateTicker('pm-up-price', data.polymarket.up_price);
        updateTicker('pm-down-price', data.polymarket.down_price);
    }

    // 1b. Mikroyapı, öğrenen model ve birleşik equity curve
    renderMicro(data.microstructure);
    renderModel(data.online_model);
    renderEquityChart(data.strategies);

    // 2. Strategies Grid
    const grid = document.getElementById('strategies-grid');
    grid.innerHTML = '';
    
    if (data.strategies && data.strategies.length > 0) {
        data.strategies.forEach(strat => {
            const color = colors[strat.name] || '#ffffff';
            const port = strat.portfolio;
            
            const pnlClass = port.total_pnl >= 0 ? 'positive' : 'negative';
            const pnlSign = port.total_pnl >= 0 ? '+' : '';
            
            const card = document.createElement('div');
            card.className = 'strategy-card glass-panel';
            
            // Flash if total trades increased
            let oldStrat = lastData?.strategies?.find(s => s.name === strat.name);
            if (oldStrat && port.total_trades > oldStrat.portfolio.total_trades) {
                card.classList.add('flash-update');
            }

            card.innerHTML = `
                <div class="strat-header">
                    <div class="strat-title">
                        <div class="strat-dot" style="color: ${color}; background-color: ${color}"></div>
                        ${strat.name}
                    </div>
                    <button class="strat-status ${strat.is_active ? 'active' : ''}"
                            onclick="toggleStrategy('${strat.name.replace(/'/g, "\\'")}')"
                            title="Stratejiyi durdur/başlat">${strat.is_active ? 'ACTIVE' : 'PAUSED'}</button>
                </div>
                <div class="strat-metrics">
                    <div class="metric">
                        <span class="metric-label">Balance</span>
                        <span class="metric-value monospace">$${port.balance.toFixed(2)}</span>
                    </div>
                    <div class="metric">
                        <span class="metric-label">P&L</span>
                        <span class="metric-value monospace ${pnlClass}">${pnlSign}$${port.total_pnl.toFixed(2)} (${pnlSign}${port.total_pnl_pct.toFixed(2)}%)</span>
                    </div>
                </div>
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div class="metric">
                        <span class="metric-label">Total Trades</span>
                        <span class="metric-value monospace">${port.total_trades}</span>
                    </div>
                    <div class="win-rate-circle" style="--pct: ${port.win_rate}%;">
                        <span class="win-rate-val monospace">${port.win_rate.toFixed(1)}%</span>
                    </div>
                </div>
                <div class="strat-chart">
                    <div class="strat-chart-head">
                        <span>P&L Geçmişi</span>
                        <span class="muted">bet $${(strat.bet_size ?? 10).toFixed(0)} · minEV ${(strat.min_ev ?? 0.02).toFixed(2)}</span>
                    </div>
                    ${renderSparkline(port.balance_history, port.initial_balance)}
                    ${strat.adaptations && strat.adaptations.length ? `<div class="adapt-note">🧠 ${strat.adaptations[strat.adaptations.length-1].note}</div>` : ''}
                    ${renderCalibration(strat.calibration)}
                </div>
                <div class="strat-signal">
                    <div><span class="bold">Last Signal:</span> ${strat.last_signal?.direction || 'N/A'} (Conf: ${strat.last_signal?.confidence?.toFixed(2) || '0'})</div>
                    <div><span class="bold">EV:</span> ${strat.last_ev?.toFixed(3) || '0'}</div>
                    <div style="font-size: 0.75rem; margin-top: 5px; color: var(--text-muted);">${strat.last_signal?.reasoning || ''}</div>
                    ${strat.last_skip_reason ? `<div class="skip-reason">⏭️ İşlem yok: ${strat.last_skip_reason}</div>` : ''}
                </div>
                <div class="strat-trades">
                    <div class="strat-trades-head">
                        <span>İşlemler</span>
                        <span class="muted">${(port.open_trades?.length || 0)} açık · ${port.total_trades} kapalı</span>
                    </div>
                    <div class="trade-list scrollable">
                        ${renderStrategyTrades(port)}
                    </div>
                </div>
            `;
            grid.appendChild(card);
        });
    } else {
        grid.innerHTML = '<div style="color: var(--text-muted);">No strategies data yet.</div>';
    }

    // 3. Leaderboard
    const lbTbody = document.querySelector('#leaderboard-table tbody');
    lbTbody.innerHTML = '';
    
    let leaderboardData = data.leaderboard;
    if (!leaderboardData && data.strategies) {
        // Fallback: build leaderboard from strategies
        leaderboardData = [...data.strategies].map(s => ({
            name: s.name,
            total_pnl: s.portfolio.total_pnl,
            win_rate: s.portfolio.win_rate
        })).sort((a,b) => b.total_pnl - a.total_pnl);
    }

    if (leaderboardData && leaderboardData.length > 0) {
        leaderboardData.forEach(row => {
            const pnlClass = row.total_pnl >= 0 ? 'positive' : 'negative';
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${row.name}</td>
                <td class="monospace ${pnlClass}">${row.total_pnl >= 0 ? '+' : ''}$${row.total_pnl.toFixed(2)}</td>
                <td class="monospace">${row.win_rate.toFixed(1)}%</td>
                <td>
                    <div style="width: 100%; background: rgba(255,255,255,0.1); height: 4px; border-radius: 2px;">
                        <div style="width: ${row.win_rate}%; background: var(--up-color); height: 100%; border-radius: 2px;"></div>
                    </div>
                </td>
            `;
            lbTbody.appendChild(tr);
        });
    } else {
        lbTbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">No data yet</td></tr>';
    }

    // 4. Recent Trades
    const tradesTbody = document.querySelector('#recent-trades-table tbody');
    tradesTbody.innerHTML = '';
    
    let tradesData = data.recent_trades;
    if (!tradesData && data.strategies) {
        // Fallback to collecting from strats if recent_trades array is missing
        tradesData = [];
        data.strategies.forEach(s => {
            if(s.portfolio.last_10_trades) {
                tradesData.push(...s.portfolio.last_10_trades);
            }
        });
    }

    if (tradesData && tradesData.length > 0) {
        // En yeni önce
        tradesData.slice().reverse().slice(0, 20).forEach(trade => {
            const tr = document.createElement('tr');
            const pending = (trade.result == null);
            const resClass = pending ? 'pending' : (trade.result === 'WIN' ? 'win' : 'lose');
            const resText = pending ? 'OPEN' : trade.result;
            const hasPnl = (typeof trade.pnl === 'number');
            const pnlClass = hasPnl ? (trade.pnl >= 0 ? 'positive' : 'negative') : 'muted';
            const pnlText = hasPnl ? `${trade.pnl >= 0 ? '+' : ''}$${trade.pnl.toFixed(2)}` : '…';
            tr.innerHTML = `
                <td>${trade.strategy}</td>
                <td><span style="color: ${trade.side === 'UP' ? 'var(--up-color)' : 'var(--down-color)'}; font-weight:600;">${trade.side}</span></td>
                <td class="monospace">${(trade.entry_price ?? 0).toFixed(2)}</td>
                <td class="monospace">${trade.amount}</td>
                <td><span class="badge ${resClass}">${resText}</span></td>
                <td class="monospace ${pnlClass}">${pnlText}</td>
            `;
            tradesTbody.appendChild(tr);
        });
    } else {
        tradesTbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">No trades yet</td></tr>';
    }

    // 5. Decision Feed
    const feed = document.getElementById('decision-feed');
    feed.innerHTML = '';
    
    let allItems = [];
    if (data.strategies) {
        data.strategies.forEach(s => {
            // Strateji kararları (+ varsa AI review'ı)
            (s.recent_decisions || []).forEach(d => {
                allItems.push({ kind: 'decision', time: d.timestamp || 0, strategy: s.name, d });
            });
            // AI adaptasyonları (kural + Gemini ayarları)
            (s.adaptations || []).forEach(a => {
                allItems.push({ kind: 'adapt', time: a.time || 0, strategy: s.name, a });
            });
        });
    }
    allItems.sort((x, y) => (y.time || 0) - (x.time || 0));  // en yeni önce

    if (allItems.length > 0) {
        allItems.slice(0, 30).forEach(item => {
            const color = colors[item.strategy] || '#ffffff';
            const el = document.createElement('div');
            el.className = 'feed-item';
            el.style.borderLeftColor = color;

            if (item.kind === 'adapt') {
                const isAI = item.a.by === 'gemini';
                el.innerHTML = `
                    <div class="feed-meta">
                        <span style="color:${color}; font-weight:600;">${item.strategy}</span>
                        <span class="badge ${isAI ? 'ai' : 'pending'}">${isAI ? '🤖 AI AYARI' : '🧠 ADAPTASYON'}</span>
                    </div>
                    <div class="feed-reason">${item.a.note || ''}</div>`;
            } else {
                const d = item.d;
                const rev = d.ai_review;
                const actionCls = d.direction === 'UP' ? 'var(--up-color)' : 'var(--down-color)';
                let aiHtml = '';
                if (rev) {
                    const ok = rev.approve !== false;
                    aiHtml = `<div class="ai-review ${ok ? 'ok' : 'block'}">
                        🤖 ${ok ? 'ONAY' : 'RED'}: ${rev.reasoning || ''}</div>`;
                }
                el.innerHTML = `
                    <div class="feed-meta">
                        <span style="color:${color}; font-weight:600;">${item.strategy}</span>
                        <span>Conf: ${(d.confidence ?? 0).toFixed(2)} | EV: ${(d.ev ?? 0).toFixed(3)}</span>
                    </div>
                    <div class="feed-reason">
                        <strong style="color:${actionCls}">${d.action} ${d.direction}</strong> — ${d.reasoning || ''}
                        ${d.skip_reason ? `<div class="muted" style="font-size:0.72rem;margin-top:3px;">⏭️ ${d.skip_reason}</div>` : ''}
                    </div>
                    ${aiHtml}`;
            }
            feed.appendChild(el);
        });
    } else {
        feed.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding-top: 20px;">Henüz karar yok</div>';
    }

    lastData = data;
}

// Utility to animate number changes
function updateTicker(elementId, newValue, isCurrency = false) {
    const el = document.getElementById(elementId);
    if (!el) return;
    
    // Check if empty
    if (el.textContent === '--') {
        el.textContent = (isCurrency ? '$' : '') + newValue.toFixed(isCurrency ? 2 : 3);
        return;
    }
    
    const currentValText = el.textContent.replace(/[^0-9.-]+/g,"");
    const currentVal = parseFloat(currentValText);
    
    if (isNaN(currentVal) || currentVal === newValue) {
        el.textContent = (isCurrency ? '$' : '') + newValue.toFixed(isCurrency ? 2 : 3);
        return;
    }

    if (newValue > currentVal) {
        el.style.color = 'var(--up-color)';
    } else if (newValue < currentVal) {
        el.style.color = 'var(--down-color)';
    }

    el.textContent = (isCurrency ? '$' : '') + newValue.toFixed(isCurrency ? 2 : 3);
    
    setTimeout(() => {
        el.style.color = ''; 
    }, 1000);
}

// Initial fetch
fetchData();

// Poll every 3 seconds
setInterval(fetchData, 3000);
