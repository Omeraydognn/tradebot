const API_URL = '/api/status';

const colors = {
    "Momentum RSI+EMA": "var(--color-momentum)",
    "VWAP Mean Reversion": "var(--color-vwap)",
    "Order Book Imbalance": "var(--color-obi)",
    "Bollinger Breakout": "var(--color-bollinger)",
    "Implied Prob. Arbitrage": "var(--color-arb)"
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
                    <div class="strat-status ${strat.is_active ? 'active' : ''}">${strat.is_active ? 'ACTIVE' : 'PAUSED'}</div>
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
                </div>
                <div class="strat-signal">
                    <div><span class="bold">Last Signal:</span> ${strat.last_signal?.direction || 'N/A'} (Conf: ${strat.last_signal?.confidence?.toFixed(2) || '0'})</div>
                    <div><span class="bold">EV:</span> ${strat.last_ev?.toFixed(3) || '0'}</div>
                    <div style="font-size: 0.75rem; margin-top: 5px; color: var(--text-muted);">${strat.last_signal?.reasoning || ''}</div>
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
    
    let allDecisions = [];
    if (data.strategies) {
        data.strategies.forEach(s => {
            if(s.recent_decisions) {
                s.recent_decisions.forEach(d => {
                    allDecisions.push({ ...d, strategy: s.name });
                });
            }
        });
    }

    if (allDecisions.length > 0) {
        allDecisions.forEach((d) => {
            const el = document.createElement('div');
            el.className = 'feed-item';
            const color = colors[d.strategy] || '#ffffff';
            el.style.borderLeftColor = color;
            el.innerHTML = `
                <div class="feed-meta">
                    <span style="color: ${color}; font-weight: 600;">${d.strategy}</span>
                    <span>Conf: ${d.confidence?.toFixed(2)} | EV: ${d.ev?.toFixed(3)}</span>
                </div>
                <div class="feed-reason">
                    <strong style="color: ${d.direction === 'UP' ? 'var(--up-color)' : 'var(--down-color)'}">${d.action} ${d.direction}</strong> - 
                    ${d.reasoning}
                </div>
            `;
            feed.appendChild(el);
        });
    } else {
        feed.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding-top: 20px;">No decisions yet</div>';
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
