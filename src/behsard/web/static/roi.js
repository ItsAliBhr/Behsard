// Draw the watch region (ROI) with the mouse on a snapshot.
// Markup: <div id="roi-wrap"><img id="snap"><canvas id="roi-canvas"></canvas></div>
// and a form #roi-form with hidden inputs x, y, w, h (fractions 0..1).
(function () {
  const img = document.getElementById('snap'), cv = document.getElementById('roi-canvas'),
        f = document.getElementById('roi-form');
  if (!img || !cv || !f) return;
  let r = ['x', 'y', 'w', 'h'].map(k => parseFloat(f.elements[k].value)), start = null;
  function draw() {
    cv.width = cv.clientWidth; cv.height = cv.clientHeight; const c = cv.getContext('2d');
    c.clearRect(0, 0, cv.width, cv.height);
    c.fillStyle = 'rgba(0,0,0,.35)'; c.fillRect(0, 0, cv.width, cv.height);
    c.clearRect(r[0] * cv.width, r[1] * cv.height, r[2] * cv.width, r[3] * cv.height);
    c.strokeStyle = '#00c8ff'; c.lineWidth = 3;
    c.strokeRect(r[0] * cv.width, r[1] * cv.height, r[2] * cv.width, r[3] * cv.height);
    c.strokeStyle = '#ff3030'; c.beginPath(); const lx = (r[0] + r[2] / 2) * cv.width;
    c.moveTo(lx, r[1] * cv.height); c.lineTo(lx, (r[1] + r[3]) * cv.height); c.stroke();
    ['x', 'y', 'w', 'h'].forEach((k, i) => f.elements[k].value = r[i].toFixed(4));
    const t = document.getElementById('roi-text');
    if (t) t.textContent = r.map(v => Math.round(v * 100) + '%').join(' , ');
  }
  function pos(e) {
    const b = cv.getBoundingClientRect();
    return [Math.min(Math.max((e.clientX - b.left) / b.width, 0), 1), Math.min(Math.max((e.clientY - b.top) / b.height, 0), 1)];
  }
  cv.addEventListener('mousedown', e => { start = pos(e); });
  cv.addEventListener('mousemove', e => {
    if (!start) return; const p = pos(e);
    r = [Math.min(start[0], p[0]), Math.min(start[1], p[1]), Math.abs(p[0] - start[0]), Math.abs(p[1] - start[1])]; draw();
  });
  window.addEventListener('mouseup', () => { start = null; });
  img.addEventListener('load', draw); window.addEventListener('resize', draw);
  document.body.addEventListener('htmx:afterSwap', () => {
    const s = document.getElementById('new-snap');
    if (s) { img.src = s.dataset.src; img.style.display = ''; const n = document.getElementById('no-snap'); if (n) n.remove(); }
  });
  if (img.complete) draw();
})();
