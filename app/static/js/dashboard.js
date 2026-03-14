// Dashboard stats polling and alert animations

const POLL_INTERVAL = 1500; // ms

const DIRECTION_ARROWS = {
    'N': '↑', 'NE': '↗', 'E': '→', 'SE': '↘',
    'S': '↓', 'SW': '↙', 'W': '←', 'NW': '↖',
    'Stationary': '•'
};

function updateDashboard() {
    fetch('/api/stats')
        .then(r => r.json())
        .then(data => {
            // Summary bar
            document.getElementById('total-drones').textContent = data.total_drones;
            document.getElementById('total-unique').textContent = data.total_unique;

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
                    tag.className = 'direction-tag';
                    const arrow = DIRECTION_ARROWS[dir] || '?';
                    tag.innerHTML = `<span class="direction-arrow">${arrow}</span> #${id} ${dir}`;
                    dirEl.appendChild(tag);
                });
            });
        })
        .catch(err => console.error('Stats poll error:', err));
}

// Start polling
setInterval(updateDashboard, POLL_INTERVAL);
updateDashboard();
