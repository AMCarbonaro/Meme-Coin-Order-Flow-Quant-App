/**
 * Meme Quant - Order Flow Scanner
 * Frontend Application
 */

// Dynamic URLs based on current host (works for localhost and deployed)
const API_BASE = window.location.origin;
const WS_URL = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`;

// State
let ws = null;
let contracts = [];
let watching = {};
let alertsBySymbol = {};  // alerts keyed by symbol
let currentFilter = 'new';

// DOM Elements
const elements = {
    contractCount: document.getElementById('contractCount'),
    connectionStatus: document.getElementById('connectionStatus'),
    searchInput: document.getElementById('searchInput'),
    coinList: document.getElementById('coinList'),
    watchingGrid: document.getElementById('watchingGrid'),
    watchingCount: document.getElementById('watchingCount'),
    alertsList: document.getElementById('alertsList'),
    alertCount: document.getElementById('alertCount'),
};

// Utility Functions
function formatUSD(n) {
    if (n >= 1000000) return '$' + (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return '$' + (n / 1000).toFixed(0) + 'K';
    return '$' + n.toFixed(0);
}

function formatPrice(price) {
    if (!price) return '...';
    if (price < 0.0001) return price.toExponential(2);
    if (price < 1) return price.toFixed(6);
    if (price < 100) return price.toFixed(4);
    return price.toFixed(2);
}

// API Functions
async function loadContracts() {
    try {
        const endpoint = currentFilter === 'new'
            ? `${API_BASE}/api/contracts/new?limit=150`
            : `${API_BASE}/api/contracts?limit=500${currentFilter !== 'all' ? '&exchange=' + currentFilter : ''}`;
        
        const resp = await fetch(endpoint);
        contracts = await resp.json();
        renderCoinList();
        
        // Update contract count
        const countResp = await fetch(`${API_BASE}/`);
        const countData = await countResp.json();
        elements.contractCount.textContent = countData.contracts?.toLocaleString() || '-';
    } catch (err) {
        console.error('Failed to load contracts:', err);
        elements.coinList.innerHTML = '<div class="loading">Failed to load contracts</div>';
    }
}

async function toggleWatch(exchange, symbol) {
    const key = `${exchange}:${symbol}`;
    
    try {
        if (watching[key]) {
            await fetch(`${API_BASE}/api/watch/${exchange}/${symbol}`, { method: 'DELETE' });
            delete watching[key];
            
            // Clear alerts for this coin
            delete alertsBySymbol[symbol];
        } else {
            await fetch(`${API_BASE}/api/watch/${exchange}/${symbol}`, { method: 'POST' });
            watching[key] = { symbol, exchange, imbalance_ratio: 1, signal: null };
        }
        
        renderCoinList();
        renderWatching();
    } catch (err) {
        console.error('Failed to toggle watch:', err);
    }
}

// Render Functions
function renderCoinList() {
    const search = elements.searchInput.value.toLowerCase();
    const filtered = contracts.filter(c =>
        c.symbol.toLowerCase().includes(search) ||
        c.base_coin.toLowerCase().includes(search)
    );
    
    if (filtered.length === 0) {
        elements.coinList.innerHTML = '<div class="loading">No coins found</div>';
        return;
    }
    
    elements.coinList.innerHTML = filtered.map(c => {
        const key = `${c.exchange}:${c.symbol}`;
        const isWatching = watching[key];
        
        return `
            <div class="coin-item ${isWatching ? 'watching' : ''} ${c.is_new ? 'new' : ''}"
                 onclick="toggleWatch('${c.exchange}', '${c.symbol}')">
                <div class="coin-header">
                    <span class="exchange-badge ${c.exchange}">${c.exchange}</span>
                    <span class="coin-symbol">${c.symbol}</span>
                </div>
                <div class="coin-meta">
                    Listed: ${c.list_date} (${c.age_days}d ago) • ${c.max_leverage}x leverage
                </div>
            </div>
        `;
    }).join('');
}

function renderSuggestion(suggestion, mode) {
    const modeLabel = mode === 'scalp' ? 'Scalp' : 'Reversal';
    
    if (!suggestion) {
        // Empty placeholder that maintains structure
        return `
            <div class="suggestion-card empty">
                <div class="suggestion-header">
                    <span class="suggestion-action neutral">—</span>
                    <span class="suggestion-mode">${modeLabel}</span>
                </div>
                <div class="suggestion-levels">
                    <div class="suggestion-level">
                        <div class="suggestion-level-label">Entry</div>
                        <div class="suggestion-level-value">—</div>
                    </div>
                    <div class="suggestion-level">
                        <div class="suggestion-level-label">Target</div>
                        <div class="suggestion-level-value" style="color: var(--accent-green)">—</div>
                    </div>
                    <div class="suggestion-level">
                        <div class="suggestion-level-label">Stop</div>
                        <div class="suggestion-level-value" style="color: var(--accent-red)">—</div>
                    </div>
                </div>
                <div class="suggestion-reason">Waiting for signal...</div>
            </div>
        `;
    }
    
    const actionClass = suggestion.action === 'LONG' ? 'long' : 'short';
    
    return `
        <div class="suggestion-card ${actionClass}">
            <div class="suggestion-header">
                <span class="suggestion-action ${actionClass}">${suggestion.action}</span>
                <span class="suggestion-mode">${suggestion.mode}</span>
            </div>
            <div class="suggestion-levels">
                <div class="suggestion-level">
                    <div class="suggestion-level-label">Entry</div>
                    <div class="suggestion-level-value">${formatPrice(suggestion.entry_price)}</div>
                </div>
                <div class="suggestion-level">
                    <div class="suggestion-level-label">Target</div>
                    <div class="suggestion-level-value" style="color: var(--accent-green)">${formatPrice(suggestion.target_price)}</div>
                </div>
                <div class="suggestion-level">
                    <div class="suggestion-level-label">Stop</div>
                    <div class="suggestion-level-value" style="color: var(--accent-red)">${formatPrice(suggestion.stop_price)}</div>
                </div>
            </div>
            <div class="suggestion-reason">${suggestion.reason}</div>
        </div>
    `;
}

function renderLiquidityZones(zones, type) {
    // Always render 3 zone slots to maintain consistent height
    const items = [];
    for (let i = 0; i < 3; i++) {
        const z = zones && zones[i];
        if (z) {
            items.push(`
                <div class="zone-item ${type} ${z.is_major ? 'major' : ''}">
                    <span class="zone-price">${formatPrice(z.price)}</span>
                    <span class="zone-volume">${formatUSD(z.volume_usd)}</span>
                    <span class="zone-distance">${z.distance_pct.toFixed(1)}%</span>
                </div>
            `);
        } else {
            items.push(`
                <div class="zone-item ${type} empty">
                    <span class="zone-price">—</span>
                    <span class="zone-volume">—</span>
                    <span class="zone-distance">—</span>
                </div>
            `);
        }
    }
    return items.join('');
}

function renderCardAlerts(symbol) {
    const symbolAlerts = alertsBySymbol[symbol] || [];
    // Always render 4 alert slots to maintain consistent height
    const items = [];
    for (let i = 0; i < 4; i++) {
        const a = symbolAlerts[i];
        if (a) {
            items.push(`
                <div class="zone-item ${a.side === 'buy' ? 'support' : 'resistance'}">
                    <span class="zone-price">${a.side.toUpperCase()}</span>
                    <span class="zone-volume">${formatUSD(a.value_usd)}</span>
                    <span class="zone-distance">${a.time}</span>
                </div>
            `);
        } else {
            items.push(`
                <div class="zone-item empty">
                    <span class="zone-price">—</span>
                    <span class="zone-volume">—</span>
                    <span class="zone-distance">—</span>
                </div>
            `);
        }
    }
    return items.join('');
}

function renderWatching() {
    const keys = Object.keys(watching);
    elements.watchingCount.textContent = keys.length;
    
    if (keys.length === 0) {
        elements.watchingGrid.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">👆</div>
                <h3>Select coins to watch</h3>
                <p>Click on any coin from the left panel to start watching its order flow</p>
            </div>
        `;
        // Clear alerts when nothing is being watched
        alerts = [];
        renderAlerts();
        return;
    }
    
    elements.watchingGrid.innerHTML = keys.map(key => {
        const w = watching[key];
        const s = w.signal || {};
        const score = s.score || 0;
        const scoreClass = score > 10 ? 'positive' : (score < -10 ? 'negative' : 'neutral');
        const reasons = s.reasons || [];
        const zones = s.liquidity_zones || {};
        const suggestions = s.suggestions || {};
        const hasData = w.last_price || s.score !== undefined;
        
        return `
            <div class="watch-card">
                <div class="watch-card-header">
                    <div>
                        <div class="watch-card-symbol">${w.symbol}</div>
                        <div class="watch-card-price">${formatPrice(w.last_price)}</div>
                    </div>
                    <button class="unwatch-btn" onclick="toggleWatch('${w.exchange}', '${w.symbol}')">×</button>
                </div>
                
                <div class="signal-display">
                    <div class="signal-score ${scoreClass}">${hasData ? (score > 0 ? '+' : '') + score.toFixed(0) : '—'}</div>
                    <div class="signal-badge ${s.signal || 'NEUTRAL'}">${s.signal || 'WAITING'}</div>
                    <div class="signal-confidence">${hasData ? (s.confidence || 0).toFixed(0) + '% confidence' : 'Loading...'}</div>
                </div>
                
                <div class="volume-display">
                    <span class="volume-bid">Bids: ${hasData ? formatUSD(w.bid_volume_usd || 0) : '—'}</span>
                    <span class="volume-ask">Asks: ${hasData ? formatUSD(w.ask_volume_usd || 0) : '—'}</span>
                </div>
                
                <div class="signal-reasons">
                    ${[0,1,2,3].map(i => reasons[i] 
                        ? `<div>${reasons[i]}</div>` 
                        : `<div class="empty-reason">—</div>`
                    ).join('')}
                </div>
                
                <div class="suggestions-section">
                    <div class="liquidity-title">📍 Trade Suggestions</div>
                    ${renderSuggestion(suggestions.scalp, 'scalp')}
                    ${renderSuggestion(suggestions.reversal, 'reversal')}
                </div>
                
                <div class="liquidity-section">
                    <div class="liquidity-title">🎯 Liquidity Zones (Reversal Levels)</div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                        <div>
                            <div style="font-size: 10px; color: var(--accent-green); margin-bottom: 4px;">Support</div>
                            <div class="zone-list">${renderLiquidityZones(zones.support, 'support')}</div>
                        </div>
                        <div>
                            <div style="font-size: 10px; color: var(--accent-red); margin-bottom: 4px;">Resistance</div>
                            <div class="zone-list">${renderLiquidityZones(zones.resistance, 'resistance')}</div>
                        </div>
                    </div>
                </div>
                
                <div class="liquidity-section">
                    <div class="liquidity-title">⚡ Near-Price Activity <span style="font-weight: normal; opacity: 0.6;">(2%)</span></div>
                    <div class="zone-list">
                        ${renderCardAlerts(w.symbol)}
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

function renderAlerts() {
    elements.alertCount.textContent = alerts.length;
    
    if (alerts.length === 0) {
        elements.alertsList.innerHTML = `
            <div class="empty-state small">
                <p>Whale alerts will appear here</p>
            </div>
        `;
        return;
    }
    
    elements.alertsList.innerHTML = alerts.slice(0, 30).map(a => `
        <div class="alert-item ${a.side}">
            <div class="alert-content">
                <span class="alert-symbol">${a.symbol}</span>
                <div class="alert-details">${a.side.toUpperCase()} ${formatUSD(a.value_usd)} @ ${a.price}</div>
            </div>
            <div class="alert-time">${a.time}</div>
        </div>
    `).join('');
}

// WebSocket Connection
function connectWebSocket() {
    ws = new WebSocket(WS_URL);
    
    ws.onopen = () => {
        console.log('WebSocket connected');
        elements.connectionStatus.classList.add('connected');
        elements.connectionStatus.querySelector('.text').textContent = 'Connected';
    };
    
    ws.onclose = () => {
        console.log('WebSocket disconnected');
        elements.connectionStatus.classList.remove('connected');
        elements.connectionStatus.querySelector('.text').textContent = 'Disconnected';
        
        // Reconnect after 2 seconds
        setTimeout(connectWebSocket, 2000);
    };
    
    ws.onerror = (err) => {
        console.error('WebSocket error:', err);
    };
    
    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleMessage(msg);
        } catch (err) {
            console.error('Failed to parse message:', err);
        }
    };
}

function handleMessage(msg) {
    switch (msg.type) {
        case 'init':
            // Initialize with existing watching data
            if (msg.watching) {
                msg.watching.forEach(w => {
                    const key = `${w.exchange}:${w.symbol}`;
                    watching[key] = w;
                });
                renderWatching();
            }
            break;
            
        case 'stats':
            // Update stats for a coin
            if (msg.key && watching[msg.key]) {
                watching[msg.key] = { ...watching[msg.key], ...msg.data };
                renderWatching();
            }
            break;
            
        case 'alert':
            // New whale alert - store by symbol
            if (msg.data && msg.data.symbol) {
                const sym = msg.data.symbol;
                if (!alertsBySymbol[sym]) alertsBySymbol[sym] = [];
                alertsBySymbol[sym].unshift(msg.data);
                if (alertsBySymbol[sym].length > 10) alertsBySymbol[sym].pop();
                renderWatching();  // Re-render cards to show new alert
            }
            break;
            
        case 'heartbeat':
            // Keep-alive, ignore
            break;
            
        default:
            console.log('Unknown message type:', msg.type);
    }
}

// Event Listeners
function setupEventListeners() {
    // Filter tabs
    document.querySelectorAll('.filter-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            currentFilter = tab.dataset.filter;
            loadContracts();
        });
    });
    
    // Search input
    elements.searchInput.addEventListener('input', () => {
        renderCoinList();
    });
}

// Initialize
function init() {
    setupEventListeners();
    loadContracts();
    connectWebSocket();
    
    // Refresh contracts every 5 minutes
    setInterval(loadContracts, 5 * 60 * 1000);
}

// Make toggleWatch available globally for onclick handlers
window.toggleWatch = toggleWatch;

// Start the app
init();
