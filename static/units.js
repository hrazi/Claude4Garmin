/*
 * Shared distance-unit preference.
 *
 * One source of truth for km vs mi across every page. The choice is kept in
 * localStorage for instant rendering and mirrored to the server so the AI
 * coach describes distances in the same units the athlete reads on screen.
 *
 * Usage:
 *   Units.get()                -> "km" | "mi"
 *   Units.distance(meters)     -> number in the active unit
 *   Units.fmtDistance(meters)  -> "12.4 mi"
 *   Units.pace(secondsPerKm)   -> pace value in the active unit
 *   Units.fmtPace(minPerKm)    -> "8:12 /mi"
 *   Units.segment(onChange)    -> a wired <div> toggle you can insert
 *   Units.onChange(fn)         -> run fn whenever the unit changes
 */
(function (global) {
  const KEY = 'unitPreference';
  const LEGACY_KEY = 'trainingLogUnits';
  const M_PER_MI = 1609.344;
  const M_PER_KM = 1000;
  const listeners = [];

  function read() {
    // Migrate the Training Log's original per-page key so an existing
    // preference is not silently reset the first time this module loads.
    let v = localStorage.getItem(KEY);
    if (!v) {
      v = localStorage.getItem(LEGACY_KEY);
      if (v) localStorage.setItem(KEY, v);
    }
    return v === 'km' ? 'km' : 'mi';
  }

  let current = read();

  // ?units=km deep-links a view in a specific unit, matching the ?view= style
  // of deep-linking the analytics page already uses.
  const q = new URLSearchParams(global.location.search).get('units');
  if (q === 'km' || q === 'mi') {
    current = q;
    localStorage.setItem(KEY, q);
    localStorage.setItem(LEGACY_KEY, q);
  } else if (!localStorage.getItem(KEY)) {
    // On a browser that has never chosen, the saved server preference wins, so
    // the pages agree with the coach instead of silently defaulting to miles.
    fetch('/api/units')
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        if (d && d.units && d.units !== current) set(d.units, { persist: false });
      })
      .catch(() => {});
  }

  function set(next, opts) {
    next = next === 'km' ? 'km' : 'mi';
    if (next === current && !(opts && opts.force)) return;
    current = next;
    localStorage.setItem(KEY, next);
    localStorage.setItem(LEGACY_KEY, next);   // keep older pages in step
    listeners.forEach(fn => { try { fn(next); } catch (e) {} });
    // Persist for the coach. Failure is non-fatal: the UI is already correct.
    // Callers whose own form already writes the setting pass persist:false.
    if (opts && opts.persist === false) return;
    fetch('/api/units', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ units: next }),
    }).catch(() => {});
  }

  const perUnit = () => (current === 'mi' ? M_PER_MI : M_PER_KM);

  const Units = {
    get: () => current,
    set,
    label: () => current,
    longLabel: () => (current === 'mi' ? 'miles' : 'kilometres'),
    onChange(fn) { listeners.push(fn); },

    /** Metres -> the active unit. */
    distance(meters) {
      if (meters == null) return null;
      return meters / perUnit();
    },

    /** Kilometres -> the active unit (most APIs here already return km). */
    fromKm(km) {
      if (km == null) return null;
      return current === 'mi' ? km * (M_PER_KM / M_PER_MI) : km;
    },

    fmtDistance(meters, digits) {
      const v = Units.distance(meters);
      if (v == null) return '–';
      const d = digits == null ? (v >= 100 ? 0 : 1) : digits;
      return v.toFixed(d) + ' ' + current;
    },

    fmtFromKm(km, digits) {
      const v = Units.fromKm(km);
      if (v == null) return '–';
      const d = digits == null ? (v >= 100 ? 0 : 1) : digits;
      return v.toFixed(d) + ' ' + current;
    },

    /** Minutes-per-km -> minutes per the active unit. */
    pace(minPerKm) {
      if (minPerKm == null) return null;
      return current === 'mi' ? minPerKm * (M_PER_MI / M_PER_KM) : minPerKm;
    },

    /** Minutes-per-km -> "8:12 /mi". */
    fmtPace(minPerKm) {
      const v = Units.pace(minPerKm);
      if (v == null) return '–';
      const m = Math.floor(v);
      const s = Math.round((v - m) * 60);
      const carry = s === 60;
      return `${m + (carry ? 1 : 0)}:${String(carry ? 0 : s).padStart(2, '0')} /${current}`;
    },

    paceLabel: () => `Pace (min/${current})`,

    /**
     * Build a wired km/mi segmented control. Pass a callback to re-render.
     * Returns the element so the caller decides where it goes.
     */
    segment(onChange, className) {
      const wrap = document.createElement('div');
      wrap.className = className || 'u-seg';
      wrap.title = 'Distance units';
      wrap.innerHTML = ['mi', 'km'].map(u =>
        `<button type="button" data-u="${u}" class="${u === current ? 'active' : ''}">${u}</button>`
      ).join('');
      wrap.addEventListener('click', e => {
        const b = e.target.closest('button[data-u]');
        if (!b) return;
        set(b.dataset.u);
        wrap.querySelectorAll('button').forEach(x =>
          x.classList.toggle('active', x.dataset.u === current));
        if (onChange) onChange(current);
      });
      listeners.push(u => {
        wrap.querySelectorAll('button').forEach(x =>
          x.classList.toggle('active', x.dataset.u === u));
      });
      return wrap;
    },
  };

  // A change made in another tab should not leave this one stale.
  global.addEventListener('storage', e => {
    if (e.key === KEY && e.newValue && e.newValue !== current) {
      current = e.newValue === 'km' ? 'km' : 'mi';
      listeners.forEach(fn => { try { fn(current); } catch (err) {} });
    }
  });

  global.Units = Units;
})(window);
