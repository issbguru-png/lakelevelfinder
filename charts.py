"""Inline SVG line charts and sparklines, made at build time (Python standard library only).

The plot is an SVG stretched to its box (preserveAspectRatio="none", non-scaling strokes), so it fills any width
at a fixed height; axis labels are HTML spans placed by percentage, so text stays crisp and readable on a phone.
Gaps in a series are drawn as breaks, never as a straight line across the missing days.

  line_chart(points, ...)   -> <figure> with the SVG, labels, and a caption
  sparkline(points, ...)    -> small <svg> for headline panels and table rows
Points are (datetime.date, float) pairs in date order.
"""
import datetime as dt
import html
import itertools
import math

E = html.escape
VW, VH = 1000, 300          # SVG user space of every plot
_ids = itertools.count(1)


def _fmt(v, dec=1):
    s = f"{v:,.{dec}f}"
    return s


def nice_step(span, target=4):
    """A round tick step giving about `target` ticks across `span`."""
    if span <= 0:
        return 1.0
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def segments(points, max_gap_days):
    """Split the series wherever two consecutive points are more than max_gap_days apart."""
    out, cur, prev = [], [], None
    for d, v in points:
        if v is None:
            if cur:
                out.append(cur)
            cur, prev = [], None
            continue
        if prev is not None and (d - prev).days > max_gap_days:
            out.append(cur)
            cur = []
        cur.append((d, v))
        prev = d
    if cur:
        out.append(cur)
    return out


def downsample(points, days=7):
    """Mean of each `days`-day bucket (for 5-year and full-record charts). Buckets keep their first date."""
    if not points:
        return []
    start = points[0][0]
    buckets = {}
    for d, v in points:
        if v is None:
            continue
        k = (d - start).days // days
        buckets.setdefault(k, [d, []])[1].append(v)
    return [(b[0], sum(b[1]) / len(b[1])) for _, b in sorted(buckets.items())]


def thin(points, every):
    return points if every <= 1 else [p for i, p in enumerate(points) if i % every == 0 or i == len(points) - 1]


class Scale:
    def __init__(self, d0, d1, lo, hi):
        self.d0, self.days = d0, max(1, (d1 - d0).days)
        self.lo, self.hi = lo, hi

    def x(self, d):
        return round((d - self.d0).days / self.days * VW)

    def y(self, v):
        return round(VH - (v - self.lo) / (self.hi - self.lo) * VH)

    def ypct(self, v):
        return (self.hi - v) / (self.hi - self.lo) * 100


def _path(seg, sc):
    pts = " ".join(f"{sc.x(d)} {sc.y(v)}" for d, v in seg)
    return "M" + pts if len(seg) > 1 else f"M{sc.x(seg[0][0])} {sc.y(seg[0][1])}h1"


def _range(values, pool, pool_fit=True):
    lo, hi = min(values), max(values)
    span = max(hi - lo, 0.5)
    pool_in = False
    if pool is not None and pool_fit and (lo - 0.6 * span) <= pool <= (hi + 1.6 * span):
        lo, hi, pool_in = min(lo, pool), max(hi, pool), True
    span = max(hi - lo, 0.5)
    return lo - span * 0.14, hi + span * 0.17, pool_in


def line_chart(points, *, title, desc, full_pool=None, pool_label="Full pool", overlay=None, overlay_label=None,
               max_gap_days=1, height=200, xticks="auto", caption="", unit="ft", label_dec=None, line_label=None, marks=True):
    """A responsive line chart. `overlay` is drawn behind in grey (e.g. last year's values shifted by a year).
    The highest and lowest points carry a dot and a value label, the last point a ring; `marks=False` leaves them out."""
    pts = [(d, v) for d, v in points if v is not None]
    if len(pts) < 2:
        return ""
    cid = f"c{next(_ids)}"
    d0, d1 = pts[0][0], pts[-1][0]
    vals = [v for _, v in pts] + [v for d, v in (overlay or []) if v is not None and d0 <= d <= d1]
    lo, hi, pool_in = _range(vals, full_pool)
    sc = Scale(d0, d1, lo, hi)
    step = nice_step(hi - lo)
    dec = label_dec if label_dec is not None else (0 if step >= 1 else 1 if step >= 0.1 else 2)
    ticks = []
    t = math.ceil(lo / step) * step
    while t <= hi:
        ticks.append(t)
        t += step
    segs = segments(pts, max_gap_days)
    gap = len(segs) > 1
    svg = [f'<svg viewBox="0 0 {VW} {VH}" preserveAspectRatio="none" role="img" aria-labelledby="{cid}t {cid}d">'
           f'<title id="{cid}t">{E(title)}</title><desc id="{cid}d">{E(desc)}</desc>']
    svg.append('<path class="gr" d="' + "".join(f"M0 {sc.y(t)}H{VW}" for t in ticks) + '"/>')
    if overlay:
        ov = [(d, v) for d, v in overlay if v is not None and d0 <= d <= d1]
        osegs = segments(ov, max_gap_days)
        if osegs:
            svg.append('<path class="ov" d="' + "".join(_path(s, sc) for s in osegs) + '"/>')
    if pool_in:
        svg.append(f'<path class="pool" d="M0 {sc.y(full_pool)}H{VW}"/>')
    if gap:
        svg.append('<path class="ln" d="' + "".join(_path(s, sc) for s in segs) + '"/>')
    else:  # one path filled to the bottom; the closing edges run outside the view box so only the line is stroked
        s = segs[0]
        svg.append(f'<linearGradient id="{cid}g" x2="0" y2="1"><stop class="g0"/><stop class="g1" offset="1"/></linearGradient>'
                   f'<path class="ln ar" style="fill:url(#{cid}g)" d="{_path(s, sc)} L{VW + 20} {sc.y(s[-1][1])} L{VW + 20} {VH + 20} '
                   f'L-20 {VH + 20} L-20 {sc.y(s[0][1])}Z"/>')
    svg.append("</svg>")
    hi_p = max(pts, key=lambda p: p[1])
    lo_p = min(pts, key=lambda p: p[1])
    xp = lambda p: sc.x(p[0]) / 10
    off = ""
    ylabels = [f'<span class="yl" style="top:{sc.ypct(t):.1f}%">{_fmt(t, dec)}</span>' for t in ticks
               if 4 < sc.ypct(t) < 96]
    labels = []
    if pool_in:
        side = " pl--r" if xp(hi_p) < 42 else ""      # keep the pool label clear of the high-point label
        labels.append(f'<span class="pl{side}" style="top:{sc.ypct(full_pool):.1f}%">{E(pool_label)} {_fmt(full_pool, 1)}</span>')
    elif full_pool is not None:
        where = "above" if full_pool > hi else "below"
        off = (f'<p class="chart__off">{"↑" if where == "above" else "↓"} {E(pool_label)}, {_fmt(full_pool, 1)} {unit}, is '
               f'{_fmt(abs(full_pool - (hi if where == "above" else lo)), 1)} {unit} {where} this chart.</p>')
    # high and low: a dot and a value label each (labels lean inward near the edges); the last point gets a ring
    if marks:
        for p, cls, word in ((hi_p, "hi", "High"), (lo_p, "lo", "Low")):
            if cls == "lo" and hi_p[1] == lo_p[1]:
                break
            x = xp(p)
            pos = f"left:{x:.1f}%;top:{sc.ypct(p[1]):.1f}%"
            lean = ";--tx:-8px" if x < 9 else ";--tx:calc(8px - 100%)" if x > 91 else ""
            labels.append(f'<i class="dot dot--{cls}" style="{pos}"></i>'
                          f'<span class="dl dl--{cls}" style="{pos}{lean}">{word} {_fmt(p[1], 2)}</span>')
        if pts[-1] not in (hi_p, lo_p):
            labels.append(f'<i class="dot dot--now" style="left:100%;top:{sc.ypct(pts[-1][1]):.1f}%"></i>')
    xl = _xlabels(d0, d1, xticks)
    xs = "".join(f'<span class="{c}" style="left:{(d - d0).days / max(1, (d1 - d0).days) * 100:.1f}%">{E(txt)}</span>'
                 for d, txt, c in xl)
    leg = ""
    if line_label or (overlay and overlay_label):
        leg = f'<span class="lg"><i class="k k--ln"></i>{E(line_label or title.split(",")[0])}</span>'
    if overlay and overlay_label:
        leg += f'<span class="lg"><i class="k k--ov"></i>{E(overlay_label)}</span>'
    if pool_in and leg:
        leg += f'<span class="lg"><i class="k k--pool"></i>{E(pool_label)}</span>'
    gut = max([len(_fmt(t, dec)) for t in ticks] + [3]) * 6.4 + 14
    return (f'<figure class="chart" style="--g:{gut:.0f}px"><div class="chart__c"><div class="chart__plot" style="height:{height}px"><div class="chart__in">{"".join(svg)}{"".join(labels)}</div>'
            f'<div class="chart__y" aria-hidden="true">{"".join(ylabels)}</div></div>'
            f'<div class="chart__x" aria-hidden="true"><div class="chart__xi">{xs}</div></div>'
            + (f'<div class="chart__leg" aria-hidden="true">{leg}</div>' if leg else "") + off + "</div>"
            + (f"<figcaption>{caption}</figcaption>" if caption else "") + "</figure>")


def _xlabels(d0, d1, mode):
    days = (d1 - d0).days
    out = []
    if mode == "auto":
        mode = "days" if days <= 62 else "months" if days <= 800 else "years"
    if mode == "days":
        n = 4
        for i in range(n + 1):
            d = d0 + dt.timedelta(days=round(days * i / n))
            out.append((d, f"{d:%b} {d.day}", "xa" if i == 0 else "xz" if i == n else "xo" if i % 2 else ""))
    elif mode == "months":
        d = dt.date(d0.year, d0.month, 1)
        k = 0
        while d <= d1:
            if d >= d0 + dt.timedelta(days=6) and d <= d1 - dt.timedelta(days=6):
                lab = f"{d:%b}" + (f" ’{d:%y}" if d.month == 1 else "")
                out.append((d, lab, "xo" if k % 2 else ""))
                k += 1
            d = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    else:
        years = d1.year - d0.year
        every = 1 if years <= 6 else 5 if years <= 40 else 10
        for y in range(d0.year + 1, d1.year + 1):
            if y % every == 0 or every == 1:
                d = dt.date(y, 1, 1)
                if (d - d0).days > days * 0.03 and (d1 - d).days > days * 0.03:
                    out.append((d, str(y), "xo" if every == 1 and y % 2 else ""))
    return out


def sparkline(points, *, full_pool=None, width=None, height=56, max_gap_days=1, title=None, cls="spark"):
    """A small trend line (no labels). With `title` it is an image with a name; without, decorative."""
    pts = [(d, v) for d, v in points if v is not None]
    if len(pts) < 2:
        return ""
    vals = [v for _, v in pts]
    lo, hi, pool_in = _range(vals, full_pool)
    sc = Scale(pts[0][0], pts[-1][0], lo, hi)
    segs = segments(pts, max_gap_days)
    a11y = (f' role="img" aria-label="{E(title)}"' if title else ' aria-hidden="true"')
    size = f' width="{width}" height="{height}"' if width else ""
    body = ""
    if pool_in:
        body += f'<path class="pool" d="M0 {sc.y(full_pool)}H{VW}"/>'
    if len(segs) == 1:
        s = segs[0]
        body += (f'<path class="ln ar" d="{_path(s, sc)} L{VW + 20} {sc.y(s[-1][1])} L{VW + 20} {VH + 20} '
                 f'L-20 {VH + 20} L-20 {sc.y(s[0][1])}Z"/>')
    else:
        body += '<path class="ln" d="' + "".join(_path(s, sc) for s in segs) + '"/>'
    return (f'<svg class="{cls}" viewBox="0 0 {VW} {VH}" preserveAspectRatio="none"{size}{a11y}>{body}</svg>')
