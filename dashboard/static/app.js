const API_URL = '/api/status';

const colors = {
    "Momentum RSI+EMA": "var(--color-momentum)",
    "VWAP": "var(--color-vwap)",
    "OBI": "var(--color-obi)",
    "Bollinger": "var(--color-bollinger)",
    "Arb": "var(--color-arb)"
};

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
                <div class="strat-signal">
                    <div><span class="bold">Last Signal:</span> ${strat.last_signal?.direction || 'N/A'} (Conf: ${strat.last_signal?.confidence?.toFixed(2) || '0'})</div>
                    <div><span class="bold">EV:</span> ${strat.last_ev?.toFixed(3) || '0'}</div>
                    <div style="font-size: 0.75rem; margin-top: 5px; color: var(--text-muted);">${strat.last_signal?.reasoning || ''}</div>
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
        tradesData.slice(0, 20).forEach(trade => {
            const tr = document.createElement('tr');
            const resClass = trade.result === 'WIN' ? 'win' : 'lose';
            const pnlClass = trade.pnl >= 0 ? 'positive' : 'negative';
            tr.innerHTML = `
                <td>${trade.strategy}</td>
                <td><span style="color: ${trade.side === 'UP' ? 'var(--up-color)' : 'var(--down-color)'}">${trade.side}</span></td>
                <td class="monospace">${trade.entry_price.toFixed(2)}</td>
                <td class="monospace">${trade.amount}</td>
                <td><span class="badge ${resClass}">${trade.result}</span></td>
                <td class="monospace ${pnlClass}">${trade.pnl >= 0 ? '+' : ''}$${trade.pnl.toFixed(2)}</td>
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
