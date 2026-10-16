/**
 * EIA student picker
 * Type a few letters of a name (or a class) and pick the full name from a list.
 *
 *   var picker = StudentPicker.attach({
 *     input:    document.getElementById('student_name'),
 *     data:     [{n: 'Mukasa Grace', c: 'P.5', ...}, ...],
 *     onSelect: function (item) { ... },     // a student was chosen
 *     onClear:  function () { ... }          // the box was edited after choosing
 *   });
 *
 * Works with the mouse, touch and keyboard (arrows, Enter, Esc).
 * If this file is missing the form still works as a plain text box.
 */
(function () {
  var CSS =
    '.sp-wrap{position:relative}' +
    '.sp-list{position:absolute;left:0;right:0;top:calc(100% + 4px);z-index:60;margin:0;padding:.3rem;' +
    'list-style:none;background:#fff;border:1px solid #d9deea;border-radius:12px;' +
    'box-shadow:0 18px 40px rgba(6,40,80,.18);max-height:300px;overflow-y:auto;display:none}' +
    '.sp-list.open{display:block}' +
    '.sp-item{display:flex;align-items:center;justify-content:space-between;gap:.75rem;' +
    'padding:.55rem .7rem;border-radius:8px;cursor:pointer;font-size:.92rem;color:#14213d}' +
    '.sp-item.active,.sp-item:hover{background:#eaf1fb}' +
    '.sp-item b{color:#063a6f;font-weight:800}' +
    '.sp-cls{flex:none;font-size:.72rem;font-weight:700;color:#063a6f;background:#e3ecf8;' +
    'border-radius:999px;padding:.12rem .55rem}' +
    '.sp-sub{display:block;font-size:.74rem;color:#6b7690;font-weight:500}' +
    '.sp-empty{padding:.7rem .8rem;font-size:.85rem;color:#6b7690}' +
    '.sp-tag{flex:none;font-size:.68rem;font-weight:800;border-radius:999px;padding:.12rem .5rem;' +
    'background:#fde8ea;color:#b4232f}';

  function injectCss() {
    if (document.getElementById('sp-css')) return;
    var st = document.createElement('style');
    st.id = 'sp-css';
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  function norm(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ''); }
  // the words someone might start typing: "Mary-Ann O'Brien" -> mary-ann, maryann, mary, ann, o'brien, obrien, o, brien
  function nameWords(s) {
    var out = [];
    String(s || '').toLowerCase().split(/\s+/).forEach(function (w) {
      if (!w) return;
      out.push(w);
      var flat = w.replace(/[.'’\-]/g, '');
      if (flat !== w) out.push(flat);
      w.split(/[.'’\-]+/).forEach(function (part) { if (part && part !== w) out.push(part); });
    });
    return out;
  }
  function queryTokens(q) { return String(q || '').toLowerCase().split(/\s+/).filter(Boolean); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // lower score = better match; null = no match
  function score(item, toks, qn) {
    var cls = norm(item.c);
    // typed a class on its own, e.g. "p6" or "p.6"
    if (cls && qn.length >= 2 && /\d/.test(qn) && toks.length === 1 && cls.indexOf(qn) === 0) return 1;
    var words = nameWords(item.n), total = 0;
    for (var i = 0; i < toks.length; i++) {
      var t = toks[i], tf = t.replace(/[.'’\-]/g, ''), best = -1;
      for (var w = 0; w < words.length; w++) {
        if (words[w].indexOf(t) === 0 || (tf && words[w].indexOf(tf) === 0)) { best = Math.min(w, 4); break; }
      }
      if (best < 0 && cls && norm(t) && cls.indexOf(norm(t)) === 0) best = 5;   // a class word among the name words
      if (best < 0) return null;
      total += best;
    }
    if (String(item.n).toLowerCase().indexOf(toks.join(' ')) === 0) total -= 3;   // whole-name prefix first
    return total;
  }

  function highlight(name, toks) {
    return name.split(/(\s+)/).map(function (part) {
      var low = part.toLowerCase();
      for (var i = 0; i < toks.length; i++) {
        if (toks[i] && low.indexOf(toks[i]) === 0) {
          return '<b>' + esc(part.slice(0, toks[i].length)) + '</b>' + esc(part.slice(toks[i].length));
        }
      }
      return esc(part);
    }).join('');
  }

  function attach(opts) {
    injectCss();
    var input = opts.input, data = opts.data || [], max = opts.max || 8;
    if (!input) return null;
    var wrap = document.createElement('div');
    wrap.className = 'sp-wrap';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    var list = document.createElement('ul');
    list.className = 'sp-list';
    list.setAttribute('role', 'listbox');
    wrap.appendChild(list);
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('aria-autocomplete', 'list');

    var matches = [], active = -1, chosen = null;

    function close() { list.classList.remove('open'); active = -1; }
    function paint() {
      var kids = list.children;
      for (var i = 0; i < kids.length; i++) kids[i].classList.toggle('active', i === active);
      if (active >= 0 && kids[active]) kids[active].scrollIntoView({ block: 'nearest' });
    }
    function choose(item) {
      chosen = item;
      input.value = item.n;
      close();
      if (opts.onSelect) opts.onSelect(item);
    }
    function render() {
      var q = input.value.trim();
      var toks = queryTokens(q), qn = norm(q);
      if (!toks.length) { close(); return; }
      matches = data.map(function (it) { return { it: it, s: score(it, toks, qn) }; })
        .filter(function (m) { return m.s !== null; })
        .sort(function (a, b) { return a.s - b.s || a.it.n.localeCompare(b.it.n); })
        .slice(0, max).map(function (m) { return m.it; });
      if (!matches.length) {
        list.innerHTML = '<li class="sp-empty">' + esc(opts.noMatch || 'No match in the register') + '</li>';
        list.classList.add('open');
        active = -1;
        return;
      }
      list.innerHTML = matches.map(function (it, i) {
        var sub = it.sub ? '<span class="sp-sub">' + esc(it.sub) + '</span>' : '';
        var tag = it.tag ? '<span class="sp-tag">' + esc(it.tag) + '</span>' : '';
        return '<li class="sp-item" role="option" data-i="' + i + '"><span>' +
          highlight(it.n, toks) + sub + '</span><span>' + tag +
          (it.c ? ' <span class="sp-cls">' + esc(it.c) + '</span>' : '') + '</span></li>';
      }).join('');
      list.classList.add('open');
      active = 0;
      paint();
    }

    input.addEventListener('input', function () {
      if (chosen && input.value !== chosen.n) {
        chosen = null;
        if (opts.onClear) opts.onClear();
      }
      render();
    });
    input.addEventListener('focus', function () { if (input.value.trim() && !chosen) render(); });
    input.addEventListener('keydown', function (e) {
      var open = list.classList.contains('open') && matches.length;
      if (e.key === 'ArrowDown' && open) { e.preventDefault(); active = (active + 1) % matches.length; paint(); }
      else if (e.key === 'ArrowUp' && open) { e.preventDefault(); active = (active - 1 + matches.length) % matches.length; paint(); }
      else if (e.key === 'Enter' && open && active >= 0) { e.preventDefault(); choose(matches[active]); }
      else if (e.key === 'Escape') { close(); }
    });
    // mousedown (not click) so the list is not closed by the input losing focus first
    list.addEventListener('mousedown', function (e) {
      var li = e.target.closest('.sp-item');
      if (li) { e.preventDefault(); choose(matches[+li.getAttribute('data-i')]); }
    });
    input.addEventListener('blur', function () { setTimeout(close, 120); });

    return {
      select: choose,
      clear: function () { chosen = null; input.value = ''; close(); if (opts.onClear) opts.onClear(); },
      selected: function () { return chosen; }
    };
  }

  window.StudentPicker = { attach: attach };
})();
