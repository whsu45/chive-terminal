def generate_svg_sparkline(prices):
    if not prices or len(prices) < 2:
        return '<span class="text-xs text-slate-400">無圖表</span>'
    open_price, close_price = prices[0], prices[-1]
    is_up = close_price >= open_price
    color = "#ef4444" if is_up else "#22c55e"

    min_p, max_p = min(prices), max(prices)
    price_range = (max_p - min_p) if max_p != min_p else 1
    width, height = 110, 28
    points = []
    for idx, p in enumerate(prices):
        x = (idx / (len(prices) - 1)) * width
        y = height - ((p - min_p) / price_range) * (height - 4) - 2
        points.append(f"{x:.1f},{y:.1f}")

    polyline_str = " ".join(points)
    return f'<svg width="{width}" height="{height}" class="inline-block overflow-visible" title="開: {open_price:.0f} | 收: {close_price:.0f}"><polyline fill="none" stroke="{color}" stroke-width="1.8" points="{polyline_str}" /></svg>'


def generate_svg_kline_chart(ohlc_data, title, width=580, height=280):
    if not ohlc_data or len(ohlc_data) < 2:
        return f'''
        <div class="flex items-center justify-center h-[280px] bg-slate-50 text-slate-400 text-xs rounded-lg border border-slate-100">
            ⚠️ 數據載入中或休市無資料
        </div>
        '''

    highs = [d['high'] for d in ohlc_data]
    lows = [d['low'] for d in ohlc_data]
    min_price = min(lows)
    max_price = max(highs)
    price_range = max_price - min_price if max_price != min_price else 1.0

    padding_top, padding_bottom, padding_left, padding_right = 20, 30, 60, 15
    chart_w = width - padding_left - padding_right
    chart_h = height - padding_top - padding_bottom

    num_bars = len(ohlc_data)
    bar_spacing = chart_w / num_bars
    bar_width = max(2.5, bar_spacing * 0.65)

    def price_to_y(p):
        return padding_top + chart_h - ((p - min_price) / price_range) * chart_h

    svg_elements = []
    num_grid_lines = 4
    for i in range(num_grid_lines + 1):
        price_val = min_price + (price_range * i / num_grid_lines)
        y_pos = price_to_y(price_val)
        svg_elements.append(
            f'<line x1="{padding_left}" y1="{y_pos:.1f}" x2="{width - padding_right}" y2="{y_pos:.1f}" stroke="#f1f5f9" stroke-width="1" />')
        svg_elements.append(
            f'<text x="{padding_left - 8}" y="{y_pos + 3.5:.1f}" font-size="10" fill="#94a3b8" text-anchor="end">{price_val:,.0f}</text>')

    if num_bars >= 3:
        sample_indices = [0, num_bars // 2, num_bars - 1]
        for idx in sample_indices:
            x_pos = padding_left + (idx + 0.5) * bar_spacing
            date_label = ohlc_data[idx]['time']
            svg_elements.append(
                f'<text x="{x_pos:.1f}" y="{height - 8}" font-size="10" fill="#94a3b8" text-anchor="middle">{date_label}</text>')

    for idx, d in enumerate(ohlc_data):
        x_center = padding_left + (idx + 0.5) * bar_spacing
        x_left = x_center - (bar_width / 2)

        o_y = price_to_y(d['open'])
        c_y = price_to_y(d['close'])
        h_y = price_to_y(d['high'])
        l_y = price_to_y(d['low'])

        is_up = d['close'] >= d['open']
        color = "#ef4444" if is_up else "#22c55e"

        top_y = min(o_y, c_y)
        body_h = max(abs(c_y - o_y), 1.5)

        tooltip = f"{d['time']} &#10;開: {d['open']:,.1f} &#10;高: {d['high']:,.1f} &#10;低: {d['low']:,.1f} &#10;收: {d['close']:,.1f}"

        svg_elements.append(
            f'<line x1="{x_center:.1f}" y1="{h_y:.1f}" x2="{x_center:.1f}" y2="{l_y:.1f}" stroke="{color}" stroke-width="1.2" />')
        svg_elements.append(
            f'<rect x="{x_left:.1f}" y="{top_y:.1f}" width="{bar_width:.1f}" height="{body_h:.1f}" fill="{color}" rx="0.5"><title>{tooltip}</title></rect>')

    elements_str = "\n".join(svg_elements)
    return f'<svg viewBox="0 0 {width} {height}" class="w-full h-auto overflow-visible font-sans">{elements_str}</svg>'