// Dashboard stats polling and alert animations

const POLL_INTERVAL = 1500; // ms

const DIRECTION_ARROWS = {
    'N': '↑', 'NE': '↗', 'E': '→', 'SE': '↘',
    'S': '↓', 'SW': '↙', 'W': '←', 'NW': '↖',
    'Stationary': '◎'
};

const monitorBtn = document.getElementById('start-monitoring-btn');

monitorBtn.addEventListener('click', () => {
    const isActive = monitorBtn.classList.contains('active');
    const url = isActive ? '/api/stop_monitoring' : '/api/start_monitoring';

    fetch(url, { method: 'POST' })
        .then(r => r.json())
        .then(() => {
            if (isActive) {
                setButtonStopped();
            } else {
                setButtonActive();
            }
        });
});

function setButtonActive() {
    monitorBtn.textContent = 'STOP MONITORING';
    monitorBtn.classList.add('active', 'stop');
    monitorBtn.disabled = false;
}

function setButtonStopped() {
    monitorBtn.textContent = 'START MONITORING';
    monitorBtn.classList.remove('active', 'stop');
    monitorBtn.disabled = false;
}

function updateDashboard() {
    fetch('/api/stats')
        .then(r => r.json())
        .then(data => {
            // Sync monitoring button state (handles page refresh)
            if (data.monitoring_active) {
                setButtonActive();
            } else {
                setButtonStopped();
            }

            // Center cell stats
            document.getElementById('total-drones').textContent = data.total_drones;

            const alertBox = document.getElementById('alert-box');
            const alertStatus = document.getElementById('alert-status');
            if (data.any_alert) {
                alertStatus.textContent = 'DETECTED';
                alertBox.classList.add('alert-active');
            } else {
                alertStatus.textContent = 'CLEAR';
                alertBox.classList.remove('alert-active');
            }

            // Compass minimap - cardinal directions (red = drone detected)
            const compassDirs = ['north', 'south', 'east', 'west'];
            compassDirs.forEach(name => {
                const tri = document.getElementById(`compass-${name}`);
                const feedStats = data.feeds[name];
                if (tri && feedStats) {
                    if (feedStats.drone_count > 0) {
                        tri.classList.add('active');
                    } else {
                        tri.classList.remove('active');
                    }
                }
            });

            // Compass minimap - intercardinal directions (yellow = blind spot warning)
            const intercardinals = ['NE', 'SE', 'SW', 'NW'];
            intercardinals.forEach(dir => {
                const tri = document.getElementById(`compass-${dir}`);
                if (tri) tri.classList.remove('warning');
            });
            compassDirs.forEach(name => {
                const feedStats = data.feeds[name];
                if (!feedStats) return;
                const warnings = feedStats.intercardinal_warnings || [];
                warnings.forEach(dir => {
                    const tri = document.getElementById(`compass-${dir}`);
                    if (tri) tri.classList.add('warning');
                });
            });

            // Per-feed updates
            const feedNames = ['north', 'south', 'east', 'west'];
            feedNames.forEach(name => {
                const feedData = data.feeds[name];
                if (!feedData) return;

                const panel = document.getElementById(`panel-${name}`);
                const countEl = document.getElementById(`count-${name}`);
                const dirEl = document.getElementById(`directions-${name}`);

                // Count
                const count = feedData.drone_count;
                countEl.textContent = count === 1 ? '1 drone' : `${count} drones`;

                if (count > 0) {
                    countEl.classList.add('has-drones');
                    panel.classList.add('alert-active');
                } else {
                    countEl.classList.remove('has-drones');
                    panel.classList.remove('alert-active');
                }

                // Direction tags
                dirEl.innerHTML = '';
                const directions = feedData.directions || {};
                Object.entries(directions).forEach(([id, dir]) => {
                    const tag = document.createElement('span');
                    const isHover = dir === 'Stationary';
                    tag.className = isHover ? 'direction-tag hover' : 'direction-tag';
                    const arrow = DIRECTION_ARROWS[dir] || '?';
                    const label = isHover ? 'HOVER' : dir;
                    tag.innerHTML = `<span class="direction-arrow">${arrow}</span> #${id} ${label}`;
                    dirEl.appendChild(tag);
                });

                // Per-feed activity log (scrolling strip in pillarbox)
                const logEl = document.getElementById(`log-${name}`);
                if (logEl && feedData.feed_log) {
                    const wasAtBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 20;
                    logEl.innerHTML = '';
                    feedData.feed_log.forEach(entry => {
                        const div = document.createElement('div');
                        if (entry.includes('All clear')) {
                            div.className = 'feed-log-entry cleared';
                        } else if (entry.includes('detected')) {
                            div.className = 'feed-log-entry detected';
                        } else {
                            div.className = 'feed-log-entry activity';
                        }
                        div.textContent = entry;
                        logEl.appendChild(div);
                    });
                    if (wasAtBottom) {
                        logEl.scrollTop = logEl.scrollHeight;
                    }
                }
            });
        })
        .catch(err => console.error('Stats poll error:', err));
}

// Mark Event button (non-functional — active learning teaser)
function markEvent(btn, feed) {
    btn.textContent = 'EVENT MARKED';
    btn.classList.add('marked');
    setTimeout(() => {
        btn.textContent = 'MARK EVENT';
        btn.classList.remove('marked');
    }, 2000);
}

// Start polling
setInterval(updateDashboard, POLL_INTERVAL);
updateDashboard();
