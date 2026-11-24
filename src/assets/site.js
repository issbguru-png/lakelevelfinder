/* Lake Level Finder: in-browser staleness check, table sort/filter, lake search and "near me". The page is complete without it. */
(function () {
  var d = document, now = Date.now();
  function ev(name) { if (window.gtag) window.gtag("event", name); }
  // Staleness (plan §7.6): recomputed here from the reading's own time, so a stopped pipeline cannot leave a page looking current.
  d.querySelectorAll(".pn[data-reading-ts]").forEach(function (pn) {
    var t = Date.parse(pn.getAttribute("data-reading-ts"));
    if (isNaN(t)) return;
    var age = (now - t) / 36e5, soft = +pn.getAttribute("data-soft-h") || 48, hard = +pn.getAttribute("data-hard-h") || 96;
    if (age > soft && age <= hard) {
      var s = d.querySelector(".stale"); if (s && s.hidden) { s.hidden = false; ev("stale_shown"); }
      pn.classList.add("pn--delayed");
    }
    if (age > hard && !pn.classList.contains("pn--unknown")) {
      pn.classList.add("pn--unknown"); ev("stale_shown");
      d.querySelectorAll("template.tpl-unk").forEach(function (t) { t.parentNode.replaceChild(t.content.cloneNode(true), t); });
      var q = function (sel) { return pn.querySelector(sel); };
      if (q(".pn__known")) q(".pn__known").hidden = true;
      if (q(".pn__unk")) q(".pn__unk").hidden = false;
      if (q(".pn__last")) q(".pn__last").hidden = false;
      d.querySelectorAll(".lead--known,.spark30,.chips,.intraday,.stale,#changes,#what-it-means,#storage").forEach(function (el) { el.hidden = true; });
      d.querySelectorAll(".lead--unknown").forEach(function (el) { el.hidden = false; });
      d.querySelectorAll(".jump a,.toc a").forEach(function (a) { var s = d.getElementById(a.hash.slice(1)); if (s && s.hidden) a.parentNode.hidden = true; });
    }
  });
  // Table rows (hubs, lists): past the hard limit a row's status becomes Unknown.
  d.querySelectorAll("tr[data-reading-ts]").forEach(function (tr) {
    var t = Date.parse(tr.getAttribute("data-reading-ts")), hard = +tr.getAttribute("data-hard-h") || 96;
    if (isNaN(t) || (now - t) / 36e5 <= hard) return;
    tr.classList.add("is-unknown");
    tr.querySelectorAll(".badge").forEach(function (b) { b.outerHTML = "Unknown"; });
  });
  // Sortable tables
  d.querySelectorAll("table[data-sort]").forEach(function (t) {
    var tb = t.tBodies[0];
    t.querySelectorAll("thead th").forEach(function (th, i) {
      var btn = th.querySelector("button"); if (!btn) return;
      btn.addEventListener("click", function () {
        var asc = th.getAttribute("aria-sort") !== "ascending";
        t.querySelectorAll("thead th").forEach(function (o) { o.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", asc ? "ascending" : "descending");
        var rows = [].slice.call(tb.rows);
        rows.sort(function (a, c) {
          var x = a.cells[i], y = c.cells[i];
          x = x.getAttribute("data-v") || x.textContent; y = y.getAttribute("data-v") || y.textContent;
          var nx = parseFloat(x), ny = parseFloat(y);
          var r = (!isNaN(nx) && !isNaN(ny) && /^-?[\d.]/.test(x) && /^-?[\d.]/.test(y)) ? nx - ny : x.localeCompare(y);
          return asc ? r : -r;
        });
        rows.forEach(function (r) { tb.appendChild(r); });
      });
    });
  });
  d.querySelectorAll("input[data-filter]").forEach(function (inp) {
    var t = d.getElementById(inp.getAttribute("data-filter")); if (!t) return;
    inp.addEventListener("input", function () {
      var q = inp.value.trim().toLowerCase();
      [].forEach.call(t.tBodies[0].rows, function (r) { r.hidden = q && r.textContent.toLowerCase().indexOf(q) < 0; });
    });
  });
  // Homepage cards: past the hard limit a card stops showing its reading as current.
  d.querySelectorAll("[data-hts]").forEach(function (el) {
    var t = Date.parse(el.getAttribute("data-hts")), hard = +el.getAttribute("data-hh") || 96;
    if (isNaN(t) || (now - t) / 36e5 <= hard) return;
    el.classList.add("is-unk");
    var b = el.querySelector(".hg__v,.lkc__v,.lkr__v"); if (b) b.textContent = "Level unknown";
  });
  // Lake page rail: mark the section being read.
  var tl = d.querySelectorAll(".toc a");
  if (tl.length && window.IntersectionObserver) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) tl.forEach(function (a) { a.classList.toggle("on", a.hash === "#" + e.target.id); }); });
    }, { rootMargin: "-12% 0px -72% 0px" });
    tl.forEach(function (a) { var s = d.getElementById(a.hash.slice(1)); if (s) io.observe(s); });
  }
  d.querySelectorAll("a.src-link").forEach(function (a) { a.addEventListener("click", function () { ev("outbound_source_click"); }); });
  // Lake search: a small suggestion list (8 at most), never a giant <datalist>. Index loaded once, on first use.
  var form = d.getElementById("find");
  if (!form) return;
  var input = form.querySelector("input"), msg = form.querySelector(".find__msg"), near = form.querySelector(".near");
  var idx = null, waiting = [], box = d.createElement("ul"), items = [], active = -1, MAX = 8;
  box.id = "find-list"; box.className = "suggest"; box.setAttribute("role", "listbox"); box.hidden = true;
  box.setAttribute("aria-label", "Matching lakes");
  form.appendChild(box);
  input.setAttribute("role", "combobox"); input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false"); input.setAttribute("aria-controls", box.id);
  var norm = function (s) { return (s || "").toLowerCase().replace(/[’'.,()]/g, "").replace(/\s+/g, " ").trim(); };
  function load(cb) {
    if (idx) return cb();
    waiting.push(cb);
    if (waiting.length > 1) return;
    fetch(form.getAttribute("data-index")).then(function (r) { return r.json(); }).then(function (j) {
      idx = j.map(function (r) { return { id: r[0], name: r[1], key: norm(r[1] + " " + r[2]), st: r[3], lat: r[4], lon: r[5], lvl: r[6], band: r[7] }; });
      var w = waiting; waiting = []; w.forEach(function (f) { f(); });
    }).catch(function () { waiting = []; });
  }
  function close() { box.hidden = true; box.textContent = ""; items = []; active = -1; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); }
  function show(list, extra) {
    box.textContent = ""; items = list; active = -1;
    if (!list.length) { close(); return; }
    list.forEach(function (it, k) {
      var li = d.createElement("li"), a = d.createElement("a"), s1 = d.createElement("span"), s2 = d.createElement("span");
      li.id = "fs-" + k; li.setAttribute("role", "option");
      a.href = "/lake/" + it.id + "/"; a.tabIndex = -1;
      s1.textContent = it.name + " (" + it.st.replace(/ /g, ", ") + ")";
      s2.className = "s2"; s2.textContent = extra ? extra(it) : (it.lvl || "");
      a.appendChild(s1); a.appendChild(s2); li.appendChild(a); box.appendChild(li);
    });
    box.hidden = false; input.setAttribute("aria-expanded", "true");
  }
  function setActive(k) {
    if (active >= 0 && box.children[active]) box.children[active].removeAttribute("aria-selected");
    active = k; if (k < 0) return input.removeAttribute("aria-activedescendant");
    box.children[k].setAttribute("aria-selected", "true"); input.setAttribute("aria-activedescendant", box.children[k].id);
  }
  function suggest() {
    var q = norm(input.value);
    if (q.length < 2) { close(); return; }
    var a = [], b = [], sq = " " + q;
    for (var i = 0; i < idx.length && a.length < MAX; i++) {
      var k = idx[i].key, k2 = k.replace(/^lake /, "");
      if (k.indexOf(q) === 0 || k2.indexOf(q) === 0) a.push(idx[i]);
      else if (b.length < MAX && (" " + k).indexOf(sq) >= 0) b.push(idx[i]);
    }
    var list = a.concat(b).slice(0, MAX);
    show(list);
    msg.textContent = list.length ? "" : "No lake matches “" + input.value.trim() + "”.";
    ev("lake_search");
  }
  input.addEventListener("focus", function () { load(function () {}); });
  input.addEventListener("input", function () { load(suggest); });
  input.addEventListener("keydown", function (e) {
    if (box.hidden) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((active + 1) % items.length); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(active <= 0 ? items.length - 1 : active - 1); }
    else if (e.key === "Escape") close();
  });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    load(function () { suggest(); var it = items[active >= 0 ? active : 0]; if (it) location.href = "/lake/" + it.id + "/"; });
  });
  d.addEventListener("click", function (e) { if (!form.contains(e.target)) close(); });
  if (location.hash === "#find") input.focus();
  // Near me: distance from the browser's location (never sent anywhere) to each lake in the index.
  if (near && navigator.geolocation) {
    near.hidden = false;
    near.addEventListener("click", function () {
      msg.textContent = "Finding your location…";
      navigator.geolocation.getCurrentPosition(function (p) {
        load(function () {
          var la = p.coords.latitude * Math.PI / 180, lo = p.coords.longitude * Math.PI / 180;
          var list = idx.filter(function (x) { return x.lat != null; }).map(function (x) {
            var a = x.lat * Math.PI / 180, b = x.lon * Math.PI / 180;
            var h = Math.pow(Math.sin((a - la) / 2), 2) + Math.cos(la) * Math.cos(a) * Math.pow(Math.sin((b - lo) / 2), 2);
            x.mi = 7917.6 * Math.asin(Math.sqrt(h)); return x;
          }).sort(function (x, y) { return x.mi - y.mi; }).slice(0, MAX);
          msg.textContent = "Nearest lakes on this site:";
          show(list, function (x) { return Math.round(x.mi) + " mi" + (x.lvl ? " · " + x.lvl : ""); });
        });
      }, function () { msg.textContent = "Location is not available. Search by name instead."; }, { timeout: 10000, maximumAge: 6e5 });
    });
  }
})();
