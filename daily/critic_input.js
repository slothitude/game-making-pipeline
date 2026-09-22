/**
 * critic_input.js — the Critic's phone-input simulation layer.
 * Loaded into the game page via browser_evaluate. Simulates:
 *   - dragSteer(x1,y1, x2,y2, duration)  → the tilt-drag fallback
 *   - holdSteer(x, y, duration)           → held thumb (continuous drag)
 *   - tap(x, y)                           → single tap (buttons, marks)
 *   - doubleTap(x, y)                     → quick double-tap
 *   - typeWord(word)                      → keyboard word entry
 *   - pressEnter() / pressEsc() / pressSpace()
 *   - swipeCardinal(dir, dist)            → swipe up/down/left/right
 *   - gyroEmulate(freqHz, amplitude, duration) → simulated tilt oscillation
 *     (drives the drag fallback in a wave pattern — reads as "tilty" to the game)
 *   - getGameState()                      → reads Godot's JS bridge if exposed
 *
 * Usage: browser_evaluate("critic_input.dragSteer(270,600, 270,300, 500)")
 */

// Install as a global — the critic agent calls these from browser_evaluate
window.critic = {
  // --- drag steering (the tilt fallback) ---
  dragSteer: (x1, y1, x2, y2, durationMs = 300) => {
    const steps = Math.max(2, Math.floor(durationMs / 16));
    const dx = (x2 - x1) / steps;
    const dy = (y2 - y1) / steps;
    return new Promise(resolve => {
      // pointer down
      const down = new PointerEvent('pointerdown', {clientX: x1, clientY: y1, pointerId: 1, isPrimary: true, pointerType: 'touch'});
      document.querySelector('canvas')?.dispatchEvent(down);
      let i = 0;
      const step = () => {
        i++;
        const x = x1 + dx * i;
        const y = y1 + dy * i;
        const move = new PointerEvent('pointermove', {clientX: x, clientY: y, pointerId: 1, isPrimary: true, pointerType: 'touch'});
        document.querySelector('canvas')?.dispatchEvent(move);
        if (i < steps) { requestAnimationFrame(step); }
        else {
          const up = new PointerEvent('pointerup', {clientX: x2, clientY: y2, pointerId: 1, isPrimary: true, pointerType: 'touch'});
          document.querySelector('canvas')?.dispatchEvent(up);
          setTimeout(resolve, 50);
        }
      };
      requestAnimationFrame(step);
    });
  },

  // --- held thumb (continuous drag in one direction) ---
  holdSteer: async (x, y, durationMs = 1000) => {
    const canvas = document.querySelector('canvas');
    if (!canvas) return 'no canvas';
    canvas.dispatchEvent(new PointerEvent('pointerdown', {clientX: x, clientY: y, pointerId: 1, isPrimary: true, pointerType: 'touch'}));
    await new Promise(r => setTimeout(r, durationMs));
    canvas.dispatchEvent(new PointerEvent('pointerup', {clientX: x, clientY: y, pointerId: 1, isPrimary: true, pointerType: 'touch'}));
    return `held ${durationMs}ms at (${x},${y})`;
  },

  // --- tap ---
  tap: (x, y) => {
    const canvas = document.querySelector('canvas');
    if (!canvas) return 'no canvas';
    canvas.dispatchEvent(new PointerEvent('pointerdown', {clientX: x, clientY: y, pointerId: 1, isPrimary: true, pointerType: 'touch'}));
    canvas.dispatchEvent(new PointerEvent('pointerup', {clientX: x, clientY: y, pointerId: 1, isPrimary: true, pointerType: 'touch'}));
    return `tap (${x},${y})`;
  },

  // --- cardinal swipe ---
  swipeCardinal: (dir, dist = 100) => {
    const cx = 270, cy = 480; // center of 540x960 design
    const targets = { up: [cx, cy - dist], down: [cx, cy + dist], left: [cx - dist, cy], right: [cx + dist, cy] };
    const [tx, ty] = targets[dir] || targets.up;
    return window.critic.dragSteer(cx, cy, tx, ty, 200);
  },

  // --- gyro oscillation (simulates rhythmic tilt via drag) ---
  gyroEmulate: async (freqHz = 0.5, amplitude = 80, durationMs = 2000) => {
    const cx = 270, cy = 600;
    const period = 1000 / freqHz;
    const steps = Math.floor(durationMs / 50);
    const canvas = document.querySelector('canvas');
    if (!canvas) return 'no canvas';
    canvas.dispatchEvent(new PointerEvent('pointerdown', {clientX: cx, clientY: cy, pointerId: 1, isPrimary: true, pointerType: 'touch'}));
    for (let i = 0; i < steps; i++) {
      const t = i * 50;
      const x = cx + Math.sin(2 * Math.PI * t / period) * amplitude;
      canvas.dispatchEvent(new PointerEvent('pointermove', {clientX: x, clientY: cy, pointerId: 1, isPrimary: true, pointerType: 'touch'}));
      await new Promise(r => setTimeout(r, 50));
    }
    canvas.dispatchEvent(new PointerEvent('pointerup', {clientX: cx, clientY: cy, pointerId: 1, isPrimary: true, pointerType: 'touch'}));
    return `gyro emulated: ${freqHz}Hz ±${amplitude}px for ${durationMs}ms`;
  },

  // --- game state probe ---
  getGameState: () => {
    const c = document.querySelector('canvas');
    if (!c) return { error: 'no canvas' };
    return {
      canvasW: c.width, canvasH: c.height,
      clientW: c.clientWidth, clientH: c.clientHeight,
      title: document.title,
      url: location.href,
      // Godot JS bridge (if the game exposes it):
      engine: typeof engine !== 'undefined' ? 'available' : 'not exposed'
    };
  },

  // --- screenshot helper ---
  snap: () => 'use browser_take_screenshot instead',
};
