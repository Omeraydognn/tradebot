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

    // 1b. Veri bütünlüğü, mikroyapı, öğrenen model ve birleşik equity curve
    renderIntegrity(data.integrity, data.polymarket, data.persistence);
    renderMicro(data.microstructure);
    renderModel(data.online_model);
    renderEquityChart(data.strategies);

    // 1c. Bot işlem defteri (bot listesi ilk yüklemede kurulur)
    loadLedgerBots(data.strategies);

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
                ${strat.silence_reason ? `
                <div class="silence-box" title="Bu bot şu anda neden hiç sinyal üretmiyor">
                    🔇 Sessiz: ${strat.silence_reason}
                </div>` : ''}
                ${strat.persona ? `
                <div class="persona-box">
                    <div class="persona-head">
                        <span class="persona-title">🧠 ${strat.persona.title}</span>
                        <span class="muted">risk: ${strat.persona.risk}${strat.ai_initiated ? ` · ${strat.ai_initiated} kendi işlemi` : ''}</span>
                    </div>
                    ${strat.thesis ? `<div class="persona-thesis">"${strat.thesis}"</div>`
                                   : `<div class="persona-thesis muted">Henüz tez yazmadı (deneyim birikiyor…)</div>`}
                </div>` : ''}
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

// ==================================================================== //
//  VERİ BÜTÜNLÜĞÜ PANELİ                                               //
//  Sistemin gerçek veriyle çalışıp çalışmadığını gösterir. Bir kaynak   //
//  düştüğünde veya kaynaklar ayrıştığında bunu SAKLAMAZ — kırmızı yanar.//
// ==================================================================== //
function renderIntegrity(integrity, poly, persistence) {
    const grid = document.getElementById('integrity-grid');
    if (!grid || !integrity) return;

    const cl = integrity.chainlink || {};
    const cards = [];

    // 0) KALICILIK — deploy'dan sağ çıkacak mı? En kritik kart, en başta.
    if (persistence && persistence.backend) {
        const durable = persistence.durable;
        const saveAgo = persistence.last_save_ago_sec;
        cards.push({
            label: durable ? 'Kalıcılık: Postgres' : 'Kalıcılık: SADECE DOSYA',
            sub: durable ? 'deploy\'dan sağ çıkar' : 'bulutta deploy\'da SİLİNİR',
            value: persistence.loaded
                ? `${persistence.restored_trades} işlem geri geldi`
                : 'temiz başladı',
            note: saveAgo != null
                ? `son kayıt ${saveAgo.toFixed(0)}s önce · ${persistence.save_count} kayıt`
                : 'henüz kaydedilmedi',
            state: durable ? 'ok' : 'bad',
        });
    }

    // 1) Chainlink — resolution kaynağı
    cards.push({
        label: 'Chainlink BTC/USD',
        sub: 'Polymarket bununla çözer',
        value: cl.price != null ? `$${cl.price.toLocaleString('en-US', {minimumFractionDigits: 2})}` : 'YOK',
        note: cl.is_live ? `${cl.feed} · ${cl.age_sec != null ? cl.age_sec.toFixed(0) : '?'}s` : 'bağlantı yok',
        state: cl.is_live ? 'ok' : 'bad',
    });

    // 2) Binance — order flow kaynağı
    cards.push({
        label: 'Binance spot',
        sub: 'order flow / CVD kaynağı',
        value: integrity.binance_price ? `$${integrity.binance_price.toLocaleString('en-US', {minimumFractionDigits: 2})}` : 'YOK',
        note: 'karar için tek başına yetmez',
        state: integrity.binance_price ? 'ok' : 'bad',
    });

    // 3) Ayrışma — asıl risk göstergesi
    const dbps = integrity.divergence_bps;
    let divState = 'ok';
    if (dbps == null) divState = 'bad';
    else if (Math.abs(dbps) > 15) divState = 'bad';
    else if (Math.abs(dbps) > 6) divState = 'warn';
    cards.push({
        label: 'Kaynak ayrışması',
        sub: 'Binance − Chainlink',
        value: dbps != null ? `${dbps > 0 ? '+' : ''}${dbps.toFixed(1)} bps` : '—',
        note: integrity.divergence_usd != null
            ? `${integrity.divergence_usd > 0 ? '+' : ''}$${integrity.divergence_usd.toFixed(2)}`
            : 'ölçülemiyor',
        state: divState,
    });

    // 4) Pencere doğruluğu
    cards.push({
        label: 'Oynanan pencere',
        sub: poly && poly.window_label ? poly.window_label : '5dk',
        value: integrity.window_ok ? 'ŞU ANKİ' : 'İLERİ PENCERE',
        note: poly && poly.seconds_left != null ? `${poly.seconds_left}s kaldı` : '—',
        state: integrity.window_ok ? 'ok' : 'bad',
    });

    // 5) Emir defteri — dolum simülasyonu mümkün mü
    const depth = poly ? (poly.up_book_depth_usd || 0) + (poly.down_book_depth_usd || 0) : 0;
    cards.push({
        label: 'Emir defteri',
        sub: 'gerçek dolum için gerekli',
        value: integrity.book_live ? `$${depth.toFixed(0)}` : 'YOK',
        note: integrity.book_live ? 'derinlik toplamı' : 'dolum simüle edilemez',
        state: integrity.book_live ? 'ok' : 'bad',
    });

    grid.innerHTML = cards.map(c => `
        <div class="integrity-card ${c.state}">
            <div class="ic-label">${c.label}</div>
            <div class="ic-sub">${c.sub}</div>
            <div class="ic-value monospace">${c.value}</div>
            <div class="ic-note">${c.note}</div>
        </div>
    `).join('');
}

// ==================================================================== //
//  BOT İŞLEM DEFTERİ                                                    //
//  Bir bot seç -> o botun TÜM işlemleri, her alanıyla.                  //
// ==================================================================== //
let _ledgerBot = null;
let _ledgerBotsLoaded = false;

async function loadLedgerBots(strategies) {
    const sel = document.getElementById('ledger-bot-select');
    if (!sel || !strategies || !strategies.length) return;
    if (_ledgerBotsLoaded) return;

    sel.innerHTML = strategies.map(s =>
        `<option value="${encodeURIComponent(s.name)}">${s.name}</option>`
    ).join('');
    _ledgerBot = strategies[0].name;
    _ledgerBotsLoaded = true;

    sel.addEventListener('change', () => {
        _ledgerBot = decodeURIComponent(sel.value);
        loadLedger();
    });
    document.getElementById('ledger-filter')
        ?.addEventListener('change', () => renderLedger());
    document.getElementById('ledger-refresh')
        ?.addEventListener('click', () => loadLedger());

    loadLedger();
}

let _ledgerData = null;

async function loadLedger() {
    if (!_ledgerBot) return;
    try {
        const res = await fetch(`/api/trades/${encodeURIComponent(_ledgerBot)}`);
        if (!res.ok) return;
        _ledgerData = await res.json();
        renderLedger();
    } catch (e) {
        console.error('Ledger fetch failed', e);
    }
}

function renderLedger() {
    if (!_ledgerData) return;
    const tbody = document.querySelector('#ledger-table tbody');
    const sumEl = document.getElementById('ledger-summary');
    const filter = document.getElementById('ledger-filter')?.value || 'all';
    if (!tbody) return;

    const s = _ledgerData.summary || {};
    if (sumEl) {
        const warn = [];
        if (s.future_window_trades > 0)
            warn.push(`<span class="lg-warn">⚠ ${s.future_window_trades} işlem İLERİ pencereye</span>`);
        if (s.liquidity_capped_trades > 0)
            warn.push(`<span class="lg-warn">⚠ ${s.liquidity_capped_trades} işlemde likidite bahsi kıstı</span>`);

        sumEl.innerHTML = `
            <span class="lg-stat">Toplam <b>${s.total ?? 0}</b></span>
            <span class="lg-stat">Açık <b>${s.open ?? 0}</b></span>
            <span class="lg-stat">K/Z <b class="positive">${s.wins ?? 0}</b>/<b class="negative">${s.losses ?? 0}</b></span>
            <span class="lg-stat">İsabet <b>${s.win_rate != null ? s.win_rate + '%' : '—'}</b></span>
            <span class="lg-stat">Bakiye <b class="monospace">$${(s.balance ?? 0).toFixed(2)}</b></span>
            <span class="lg-stat">P&L <b class="monospace ${(s.total_pnl ?? 0) >= 0 ? 'positive' : 'negative'}">${(s.total_pnl ?? 0) >= 0 ? '+' : ''}$${(s.total_pnl ?? 0).toFixed(2)}</b></span>
            <span class="lg-stat" title="Polymarket'in kote ettiği ortalama oran">Ort. piyasa oranı <b class="monospace">${s.avg_mid_price != null ? '$' + s.avg_mid_price.toFixed(4) : '—'}</b></span>
            <span class="lg-stat" title="Defter yürütülerek gerçekte ödenen ortalama oran">Ort. ödenen <b class="monospace">${s.avg_entry_price != null ? '$' + s.avg_entry_price.toFixed(4) : '—'}</b></span>
            <span class="lg-stat" title="Spread + defter kayması — Polymarket'te komisyon yok, gerçek maliyet budur">Ort. maliyet <b class="monospace ${(s.avg_total_cost_bps ?? 0) > 300 ? 'negative' : ''}">${s.avg_total_cost_bps != null ? s.avg_total_cost_bps.toFixed(0) + ' bps' : '—'}</b></span>
            ${warn.join(' ')}
        `;
    }

    // HER İŞLEMDEN ÖĞRENİLEN TABLO — hangi fiyat diliminde gerçekten
    // kazanıyoruz? Uçtaki fiyatlardan da işlem açılan modda bu ayrım şart:
    // $0.05'ten alınan yüzlerce işlem $0.50'dekilerin istatistiğini bozmasın.
    const bEl = document.getElementById('ledger-buckets');
    if (bEl) {
        const bs = _ledgerData.price_buckets || [];
        if (!bs.length) {
            bEl.innerHTML = '';
        } else {
            bEl.innerHTML = `
                <div class="bucket-head">Giriş fiyatı dilimine göre öğrenilenler
                    <span class="muted">— her sonuçlanan işlem buraya yazılır</span></div>
                <div class="bucket-row-wrap">
                ${bs.map(b => {
                    const wr = b.win_rate;
                    const cls = wr == null ? '' : (wr >= 50 ? 'positive' : 'negative');
                    const pcls = b.pnl >= 0 ? 'positive' : 'negative';
                    return `<div class="bucket-cell" title="${b.n} işlem, toplam P&L $${b.pnl}">
                        <div class="bk-range monospace">$${b.bucket}</div>
                        <div class="bk-wr monospace ${cls}">${wr != null ? wr.toFixed(0) + '%' : '—'}</div>
                        <div class="bk-n muted">${b.n} işlem</div>
                        <div class="bk-pnl monospace ${pcls}">${b.pnl >= 0 ? '+' : ''}$${b.pnl.toFixed(2)}</div>
                    </div>`;
                }).join('')}
                </div>`;
        }
    }

    let rows = _ledgerData.trades || [];
    const totalAll = rows.length;
    if (filter === 'open') rows = rows.filter(t => t.result == null);
    else if (filter === 'WIN' || filter === 'LOSE') rows = rows.filter(t => t.result === filter);

    // Kaç satırın çizildiğini açıkça yaz — "hepsi mi görünüyor?" sorusu
    // tahmine kalmasın.
    const cntEl = document.getElementById('ledger-count');
    if (cntEl) {
        cntEl.textContent = (filter === 'all')
            ? `${totalAll} işlemin tamamı gösteriliyor`
            : `${rows.length} / ${totalAll} işlem (filtre: ${filter})`;
    }

    if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="22" style="text-align:center;color:var(--text-muted);">Bu filtreye uyan işlem yok</td></tr>';
        return;
    }

    tbody.innerHTML = rows.map(t => {
        const pending = t.result == null;
        const resCls = pending ? 'pending' : (t.result === 'WIN' ? 'win' : 'lose');
        const resTxt = pending ? 'AÇIK' : (t.result === 'WIN' ? 'KAZANDI' : 'KAYBETTİ');
        const pnlTxt = (typeof t.pnl === 'number')
            ? `${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)}` : '…';
        const pnlCls = (typeof t.pnl === 'number')
            ? (t.pnl >= 0 ? 'positive' : 'negative') : 'muted';

        const winLabel = t.is_future_window
            ? `<span class="lg-future" title="Bu işlem, açıldığı anda henüz başlamamış bir pencereye yapıldı">${t.window_label} ⏭+${t.window_offset}</span>`
            : t.window_label;

        const amtLabel = t.liquidity_capped
            ? `<span class="lg-capped" title="Defter yetmedi: istenen $${t.requested_amount}">$${t.amount.toFixed(2)}⚠</span>`
            : `$${t.amount.toFixed(2)}`;

        // Piyasanın kote ettiği orana (midpoint) göre TOPLAM işlem maliyeti:
        // spread (mid -> en iyi ask) + defter kayması (ask -> ortalama dolum).
        // Kârı yiyen gerçek maliyet budur; Polymarket'te ayrıca komisyon yoktur.
        const totalCostBps = (t.mid_price > 0)
            ? (t.entry_price - t.mid_price) / t.mid_price * 10000
            : null;

        // Erken çıkış: pozisyonu pencere sonunu beklemeden bid'e sattık mı?
        let exitTypeHtml, salvageHtml;
        if (t.is_early_exit) {
            exitTypeHtml = `<span class="lg-exit" title="${(t.exit_reason || '').replace(/"/g,'&quot;')}">ERKEN ${t.exit_at_str || ''}</span>`;
            // Maliyetin yüzde kaçını geri aldık — "ne kurtarırsak kâr"
            const pct = t.amount > 0 ? (t.exit_proceeds / t.amount * 100) : 0;
            salvageHtml = `$${t.exit_proceeds.toFixed(2)}<span class="muted"> (%${pct.toFixed(0)})</span>`;
        } else if (t.exit_blocked_reason) {
            exitTypeHtml = `<span class="lg-warn" title="${t.exit_blocked_reason.replace(/"/g,'&quot;')}">ÇIKAMADI</span>`;
            salvageHtml = '—';
        } else if (t.result == null) {
            exitTypeHtml = '<span class="muted">açık</span>';
            salvageHtml = '—';
        } else {
            exitTypeHtml = '<span class="muted">çözüm</span>';
            salvageHtml = '—';
        }

        return `<tr class="lg-row ${resCls}">
            <td class="muted">${t.date_str}</td>
            <td class="monospace">${t.time_str}</td>
            <td class="monospace">${winLabel}</td>
            <td><span class="side-${t.side}">${t.side}</span></td>
            <td class="monospace" title="Polymarket'in o andaki kote ettiği oran (midpoint = zımni olasılık)">${t.mid_price ? '$' + t.mid_price.toFixed(4) : '—'}</td>
            <td class="monospace" title="Defter yürütülerek GERÇEKTE ödenen ağırlıklı ortalama oran">$${t.entry_price.toFixed(4)}</td>
            <td class="monospace muted">$${t.top_ask.toFixed(4)}</td>
            <td class="monospace ${totalCostBps > 300 ? 'negative' : 'muted'}" title="Midpoint'e göre toplam işlem maliyeti (spread + defter kayması)">${totalCostBps != null ? totalCostBps.toFixed(0) + 'bps' : '—'}</td>
            <td class="monospace">${t.shares.toFixed(1)}</td>
            <td class="monospace">${amtLabel}</td>
            <td class="monospace">${(t.calibrated_confidence * 100).toFixed(0)}%<span class="muted"> (ham ${(t.raw_confidence * 100).toFixed(0)}%)</span></td>
            <td class="monospace ${t.ev >= 0 ? 'positive' : 'negative'}">${t.ev >= 0 ? '+' : ''}${t.ev.toFixed(3)}</td>
            <td class="monospace muted">${t.btc_price_at_entry ? '$' + t.btc_price_at_entry.toLocaleString('en-US') : '—'}</td>
            <td class="monospace">${t.chainlink_at_entry ? '$' + t.chainlink_at_entry.toLocaleString('en-US') : '—'}</td>
            <td class="monospace muted">${t.cl_divergence_bps ? t.cl_divergence_bps.toFixed(1) + 'bps' : '—'}</td>
            <td class="monospace muted">${t.resolve_at_str || '—'}</td>
            <td>${exitTypeHtml}</td>
            <td class="monospace">${t.is_early_exit ? '$' + t.exit_price.toFixed(4) + `<span class="muted"> (bid ${t.exit_top_bid.toFixed(2)})</span>` : '—'}</td>
            <td class="monospace">${salvageHtml}</td>
            <td><span class="result-${resCls}">${resTxt}</span>${t.outcome ? `<span class="muted"> (${t.outcome})</span>` : ''}</td>
            <td class="monospace ${pnlCls}">${pnlTxt}</td>
            <td class="lg-reason" title="${(t.reasoning || '').replace(/"/g, '&quot;')}">${t.reasoning || '—'}</td>
        </tr>`;
    }).join('');
}

// Initial fetch
fetchData();

// Poll every 3 seconds
setInterval(fetchData, 3000);
// İşlem defterini 10 saniyede bir tazele (daha ağır bir sorgu)
setInterval(loadLedger, 10000);
