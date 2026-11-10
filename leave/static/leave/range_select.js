/*
 * Select a range of days on the calendar and book leave for it.
 *
 *  - Mouse: press on a day and drag across the range, then release.
 *  - Touch / click: click the first day, then the last day (the same day twice
 *    books a single day).
 *  - Keyboard: Tab to a day and press Enter or Space; Escape clears.
 *
 * Only bookable working days count. Weekends, holidays and past days inside the
 * range are skipped automatically and marked as skipped. The server re-checks
 * everything when the form is submitted.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api; // Node, for the unit tests
  } else {
    root.RangeSelect = api;
    var start = function () { api.init(root.document); };
    if (root.document.readyState === 'loading') {
      root.document.addEventListener('DOMContentLoaded', start);
    } else {
      start();
    }
  }
}(typeof window !== 'undefined' ? window : this, function () {
  'use strict';

  /**
   * cells: [{date: 'YYYY-MM-DD', selectable: boolean}] in calendar order.
   * Returns the bookable days between a and b (either order) and the skipped ones.
   */
  function computeSelection(cells, a, b) {
    var lo = a < b ? a : b;
    var hi = a < b ? b : a;
    var inRange = cells.filter(function (c) { return c.date >= lo && c.date <= hi; });
    var chosen = inRange.filter(function (c) { return c.selectable; });
    var skipped = inRange.filter(function (c) { return !c.selectable; }).map(function (c) { return c.date; });
    if (!chosen.length) {
      return { start: null, end: null, days: [], skipped: skipped };
    }
    return {
      start: chosen[0].date,
      end: chosen[chosen.length - 1].date,
      days: chosen.map(function (c) { return c.date; }),
      skipped: skipped
    };
  }

  function fmt(iso) {
    var d = new Date(iso + 'T00:00:00Z');
    return d.toLocaleDateString('en-US', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
  }

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  function init(doc) {
    var table = doc.querySelector('[data-range-select]');
    if (!table) { return; }
    var checkUrl = table.getAttribute('data-check-url');
    var tds = Array.prototype.slice.call(table.querySelectorAll('td[data-date]'));
    var cells = tds.map(function (td) {
      return { date: td.getAttribute('data-date'), selectable: td.hasAttribute('data-selectable') };
    });
    var form = doc.getElementById('range-form');
    var summary = doc.getElementById('range-summary');
    var check = doc.getElementById('range-check');
    var typeSelect = doc.getElementById('range-type');
    var startInput = doc.getElementById('range-start');
    var endInput = doc.getElementById('range-end');
    var submit = doc.getElementById('range-submit');
    var clearButton = doc.getElementById('range-clear');
    var hint = doc.getElementById('range-hint');
    var defaultHint = hint ? hint.textContent : '';

    var state = { pending: null, dragFrom: null, moved: false, suppressClick: false, sel: null };
    var timer = null;

    function paint() {
      var days = state.sel ? state.sel.days : [];
      var skipped = state.sel ? state.sel.skipped : [];
      tds.forEach(function (td) {
        var date = td.getAttribute('data-date');
        var picked = days.indexOf(date) !== -1;
        td.classList.toggle('sel', picked);
        td.classList.toggle('skip', !picked && skipped.indexOf(date) !== -1);
        if (td.hasAttribute('data-selectable')) { td.setAttribute('aria-selected', picked ? 'true' : 'false'); }
      });
    }

    function showCheck(text, bad) {
      check.textContent = text;
      check.className = bad ? 'warn' : 'muted';
    }

    function runCheck() {
      clearTimeout(timer);
      var sel = state.sel;
      if (!sel || !sel.start || !checkUrl) { showCheck('', false); return; }
      timer = setTimeout(function () {
        var key = sel.start + sel.end + typeSelect.value;
        var qs = new URLSearchParams({ start: sel.start, end: sel.end, leave_type: typeSelect.value });
        fetch(checkUrl + '?' + qs, { credentials: 'same-origin' })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (!state.sel || key !== state.sel.start + state.sel.end + typeSelect.value) { return; } // stale
            if (d.error) { showCheck('', false); return; }
            if (d.problem) { showCheck(d.problem, true); return; }
            if (d.violations.length) {
              showCheck('Not enough cover on: ' + d.violations.map(function (v) { return v.date; }).join(', ') + '.', true);
            } else if (d.status === 'pending') {
              showCheck('Your team has cover. This request needs your manager\'s approval.', false);
            } else {
              showCheck('Your team has cover. This will be approved immediately.', false);
            }
          })
          .catch(function () { showCheck('', false); });
      }, 200);
    }

    function updatePanel() {
      var sel = state.sel;
      if (!form) { return; }
      if (!sel) { form.hidden = true; return; }
      form.hidden = false;
      if (!sel.start) {
        summary.textContent = 'There are no bookable working days in that selection.';
        startInput.value = '';
        endInput.value = '';
        submit.disabled = true;
        showCheck('', false);
        return;
      }
      var inside = sel.skipped.filter(function (d) { return d > sel.start && d < sel.end; }).length;
      var text = fmt(sel.start) + (sel.end !== sel.start ? ' to ' + fmt(sel.end) : '') +
        ': ' + plural(sel.days.length, 'working day');
      if (inside) { text += '. ' + plural(inside, 'non-working or unavailable day') + ' in between will be skipped'; }
      summary.textContent = text + '.';
      startInput.value = sel.start;
      endInput.value = sel.end;
      submit.disabled = false;
      runCheck();
    }

    function apply(a, b) {
      state.sel = computeSelection(cells, a, b);
      paint();
      updatePanel();
    }

    function clear() {
      state.pending = null;
      state.sel = null;
      if (hint) { hint.textContent = defaultHint; }
      paint();
      updatePanel();
    }

    function choose(date) {
      if (state.pending) {
        var first = state.pending;
        state.pending = null;
        if (hint) { hint.textContent = defaultHint; }
        apply(first, date);
      } else {
        state.pending = date;
        if (hint) { hint.textContent = 'Now click the last day, or the same day again to book just that day.'; }
        apply(date, date);
      }
    }

    function cellOf(target) {
      return target && target.closest ? target.closest('td[data-date]') : null;
    }

    table.addEventListener('pointerdown', function (e) {
      if (e.button !== 0 || e.pointerType === 'touch') { return; } // touch uses tap-tap
      var td = cellOf(e.target);
      if (!td || !td.hasAttribute('data-selectable')) { return; }
      state.dragFrom = td.getAttribute('data-date');
      state.moved = false;
    });

    table.addEventListener('pointerover', function (e) {
      if (!state.dragFrom) { return; }
      var td = cellOf(e.target);
      if (!td) { return; }
      var date = td.getAttribute('data-date');
      if (date === state.dragFrom && !state.moved) { return; }
      state.moved = true;
      state.pending = null;
      if (hint) { hint.textContent = defaultHint; }
      apply(state.dragFrom, date);
    });

    function endDrag(e) {
      if (state.dragFrom && state.moved && e.type === 'pointerup') {
        // The browser still sends a click after a drag; ignore that one.
        state.suppressClick = true;
        setTimeout(function () { state.suppressClick = false; }, 0);
      }
      state.dragFrom = null;
    }
    doc.addEventListener('pointerup', endDrag);
    doc.addEventListener('pointercancel', endDrag);

    table.addEventListener('click', function (e) {
      if (state.suppressClick) { state.suppressClick = false; return; }
      var td = cellOf(e.target);
      if (!td || !td.hasAttribute('data-selectable')) { return; }
      choose(td.getAttribute('data-date'));
    });

    table.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') { return; }
      var td = cellOf(e.target);
      if (!td || !td.hasAttribute('data-selectable')) { return; }
      e.preventDefault();
      choose(td.getAttribute('data-date'));
    });

    doc.addEventListener('keydown', function (e) { if (e.key === 'Escape') { clear(); } });
    if (clearButton) { clearButton.addEventListener('click', clear); }
    if (typeSelect) { typeSelect.addEventListener('change', runCheck); }
  }

  return { computeSelection: computeSelection, init: init };
}));
