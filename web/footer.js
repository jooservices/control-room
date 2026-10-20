// Collector self-health footer: lag from meta.asOf, version from meta.collector.version.
(function () {
  'use strict';
  var REFRESH_MS = 30000;
  var STALE_SECONDS = 900;
  var OLD_SECONDS = 3600;

  function formatLag(seconds) {
    if (seconds < 90) return Math.round(seconds) + 's';
    if (seconds < 5400) return Math.round(seconds / 60) + 'm';
    return Math.round(seconds / 3600) + 'h';
  }

  function render(meta) {
    var footer = document.getElementById('collector-footer');
    var lag = document.getElementById('collector-lag');
    var version = document.getElementById('collector-version');
    var asOf = meta && meta.asOf ? Date.parse(meta.asOf) : NaN;
    if (isNaN(asOf)) {
      lag.textContent = 'Collector lag: unknown';
      footer.className = 'old';
    } else {
      var seconds = Math.max(0, (Date.now() - asOf) / 1000);
      lag.textContent = 'Collector lag: ' + formatLag(seconds);
      footer.className = seconds > OLD_SECONDS ? 'old' : seconds > STALE_SECONDS ? 'stale' : '';
    }
    var collector = meta && meta.collector;
    version.textContent = 'version ' + (collector && collector.version ? collector.version : 'unknown');
  }

  function refresh() {
    fetch('/api/snapshot', { cache: 'no-store' })
      .then(function (response) { return response.json(); })
      .then(function (snapshot) { render(snapshot.meta); })
      .catch(function () { render(null); });
  }

  refresh();
  setInterval(refresh, REFRESH_MS);
})();
