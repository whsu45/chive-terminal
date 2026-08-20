from datetime import datetime, timezone, timedelta
from .utils import format_signed_num, format_pts_str
from .chart_generator import generate_svg_kline_chart


def generate_index_html(history_records, tw_kline_data, us_kline_data):
    latest_data = history_records[0]

    utc_now = datetime.now(timezone.utc)
    tw_now = utc_now + timedelta(hours=8)
    utc_time_str = utc_now.strftime('%Y/%m/%d %H:%M:%S')
    tw_time_str = tw_now.strftime('%Y/%m/%d %H:%M:%S')

    tw_kline_svg = generate_svg_kline_chart(tw_kline_data, "台股加權指數")
    us_kline_svg = generate_svg_kline_chart(us_kline_data, "美股 NASDAQ 指數")

    scenario_badge_class = "bg-slate-100 text-slate-700 border-slate-200"
    if latest_data["scenario"] == "劇本一":
        scenario_badge_class = "bg-red-50 text-red-700 border-red-200"
    elif latest_data["scenario"] == "劇本二":
        scenario_badge_class = "bg-yellow-50 text-yellow-700 border-yellow-200"
    elif latest_data["scenario"] == "劇本三":
        scenario_badge_class = "bg-green-50 text-green-700 border-green-200"
    elif latest_data["scenario"] == "劇本四":
        scenario_badge_class = "bg-blue-50 text-blue-700 border-blue-200"

    price_color_class = "text-slate-400"
    if str(latest_data["night_price_change"]).startswith("+"):
        price_color_class = "text-red-500"
    elif str(latest_data["night_price_change"]).startswith("-"):
        price_color_class = "text-green-500"

    f_ah_str, f_ah_color = format_signed_num(latest_data.get('foreign_net_ah'))
    t_ah_str, t_ah_color = format_signed_num(latest_data.get('trust_net_ah'))
    d_ah_str, d_ah_color = format_signed_num(latest_data.get('dealer_net_ah'))

    f_full_str, f_full_color = format_signed_num(latest_data.get('foreign_net_full'))

    latest_dji, latest_dji_c = format_pts_str(latest_data.get('us_dji'))
    latest_ixic, latest_ixic_c = format_pts_str(latest_data.get('us_ixic'))
    latest_sox, latest_sox_c = format_pts_str(latest_data.get('us_sox'))

    history_rows_html = ""
    for item in history_records[:20]:
        change_str = str(item['night_price_change'])
        c_color = "text-slate-600"
        if change_str.startswith("+"):
            c_color = "text-red-500 font-bold"
        elif change_str.startswith("-"):
            c_color = "text-green-500 font-bold"

        haf_str, haf_color = format_signed_num(item.get('foreign_net_ah'))
        hat_str, hat_color = format_signed_num(item.get('trust_net_ah'))
        had_str, had_color = format_signed_num(item.get('dealer_net_ah'))

        hff_str, hff_color = format_signed_num(item.get('foreign_net_full'))
        hft_str, hft_color = format_signed_num(item.get('trust_net_full'))
        hfd_str, hfd_color = format_signed_num(item.get('dealer_net_full'))

        hdji, hdji_c = format_pts_str(item.get('us_dji'))
        hixic, hixic_c = format_pts_str(item.get('us_ixic'))
        hsox, hsox_c = format_pts_str(item.get('us_sox'))

        s_badge = "bg-slate-100 text-slate-600"
        if item['scenario'] == '劇本一':
            s_badge = "bg-red-100 text-red-700 font-bold"
        elif item['scenario'] == '劇本二':
            s_badge = "bg-yellow-100 text-yellow-700 font-bold"
        elif item['scenario'] == '劇本三':
            s_badge = "bg-green-100 text-green-700 font-bold"
        elif item['scenario'] == '劇本四':
            s_badge = "bg-blue-100 text-blue-700 font-bold"

        act_change = str(item.get('actual_change', 'NA'))
        act_color = "text-red-500 font-semibold" if act_change.startswith(
            "+") else "text-green-500 font-semibold" if act_change.startswith("-") else "text-slate-400"

        history_rows_html += f"""
        <tr class="hover:bg-slate-50 transition-colors">
            <td class="py-3 px-4 font-semibold text-slate-700">{item['date']}</td>
            <td class="py-3 px-4 {c_color}">{item['night_price_change']}</td>
            <td class="py-3 px-4">
                <div class="font-semibold text-slate-700">{item['night_volume_ratio']}</div>
                <div class="text-[11px] text-slate-400 mt-0.5">{item.get('vol_formula_str', '')}</div>
            </td>
            <td class="py-3 px-4">
                <div class="text-xs space-y-0.5">
                    <div><span class="text-slate-400">外資:</span> <span class="{haf_color}">{haf_str}</span></div>
                    <div><span class="text-slate-400">投信:</span> <span class="{hat_color}">{hat_str}</span></div>
                    <div><span class="text-slate-400">自營:</span> <span class="{had_color}">{had_str}</span></div>
                </div>
            </td>
            <td class="py-3 px-4">
                <div class="text-xs space-y-0.5">
                    <div><span class="text-slate-400">外資:</span> <span class="{hff_color}">{hff_str}</span></div>
                    <div><span class="text-slate-400">投信:</span> <span class="{hft_color}">{hft_str}</span></div>
                    <div><span class="text-slate-400">自營:</span> <span class="{hfd_color}">{hfd_str}</span></div>
                </div>
            </td>
            <td class="py-3 px-4">
                <div class="text-xs space-y-0.5">
                    <div><span class="text-slate-400">道瓊:</span> <span class="{hdji_c}">{hdji}</span></div>
                    <div><span class="text-slate-400">那指:</span> <span class="{hixic_c}">{hixic}</span></div>
                    <div><span class="text-slate-400">費半:</span> <span class="{hsox_c}">{hsox}</span></div>
                </div>
            </td>
            <td class="py-3 px-4"><span class="px-2.5 py-1 rounded-md text-xs {s_badge}">{item['scenario']}</span></td>
            <td class="py-3 px-4 text-center">{item.get('sparkline_svg', '')}<div class="text-[11px] {act_color} mt-0.5">{act_change}</div></td>
            <td class="py-3 px-4"><span class="px-2.5 py-1 rounded-md text-xs {item.get('verify_badge_class', 'bg-slate-100')}">{item.get('verify_status', 'NA')}</span></td>
        </tr>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>台指期開盤走勢預測儀表板</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 min-h-screen p-4 md:p-8 font-sans">
    <div class="max-w-[1400px] mx-auto space-y-6">

        <!-- Header & Nav Tabs -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div>
                    <h1 class="text-2xl font-bold text-slate-800">📊 台指期夜盤三大數據走勢預測</h1>
                    <p class="text-slate-500 text-sm mt-1">分析目標交易日：<span class="font-bold text-slate-800">{latest_data['date']}</span> （對應前一日盤：{latest_data['prev_date']}）</p>
                </div>
                <div class="text-xs text-slate-500 md:text-right space-y-1">
                    <div>頁面產生時間：<span class="font-semibold text-slate-700">{tw_time_str}</span> <span class="text-slate-400">(UTC+8)</span></div>
                    <div>頁面產生時間：<span class="font-semibold text-slate-700">{utc_time_str}</span> <span class="text-slate-400">(UTC)</span></div>
                </div>
            </div>

            <div class="flex space-x-2 mt-6 border-b border-slate-100 pb-2">
                <a href="./index.html" class="px-4 py-2 text-sm font-bold text-indigo-600 bg-indigo-50 rounded-lg border border-indigo-100">📊 台指期夜盤預測</a>
                <a href="./broker.html" class="px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100 rounded-lg transition-colors">🏦 主力券商買超分析</a>
            </div>
        </div>

        <!-- 3個月台股與美股原生 SVG K 線走勢圖區塊 -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <div class="flex items-center justify-between mb-3">
                    <h2 class="text-sm font-bold text-slate-800">🇹🇼 台股加權指數 (TAIEX) 近 3 個月 K 線圖</h2>
                    <span class="text-xs font-semibold text-slate-400">3 Months Candlestick</span>
                </div>
                {tw_kline_svg}
            </div>

            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <div class="flex items-center justify-between mb-3">
                    <h2 class="text-sm font-bold text-slate-800">🇺🇸 美股 NASDAQ 指數 (IXIC) 近 3 個月 K 線圖</h2>
                    <span class="text-xs font-semibold text-slate-400">3 Months Candlestick</span>
                </div>
                {us_kline_svg}
            </div>
        </div>

        <!-- 四大核心指標卡片 Grid -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">1. 夜盤漲跌點數</span>
                <div class="text-3xl font-extrabold mt-2 {price_color_class}">
                    {latest_data['night_price_change']}
                </div>
                <p class="text-xs text-slate-400 mt-2">門檻：超過 ±300 點代表預期大變</p>
            </div>

            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">2. 夜盤量佔比</span>
                <div class="text-3xl font-extrabold text-slate-800 mt-2">
                    {latest_data['night_volume_ratio']}
                </div>
                <p class="text-xs text-slate-500 mt-2">訊號強度：<span class="font-bold text-indigo-600">{latest_data['trust_signal']}</span></p>
                <div class="mt-2 pt-2 border-t border-slate-100 text-[11px] text-slate-400">
                    計算算式：{latest_data.get('vol_formula_str', 'NA')}
                </div>
            </div>

            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">3. 三大法人多空淨額 (夜盤)</span>
                <div class="text-3xl font-extrabold text-slate-800 mt-2">
                    <span class="{f_ah_color}">{f_ah_str}</span> <span class="text-sm font-normal text-slate-500">口 (外資夜盤)</span>
                </div>
                <p class="text-xs text-slate-400 mt-1">劇本對照門檻：超過 ±1,000 口</p>
                <div class="mt-2 pt-2 border-t border-slate-100 flex justify-between text-xs text-slate-500">
                    <div>投信(夜): <span class="{t_ah_color} font-medium">{t_ah_str}</span></div>
                    <div>外資(全): <span class="{f_full_color} font-medium">{f_full_str}</span></div>
                </div>
            </div>

            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">4. 美股三大指數 (點數)</span>
                <div class="mt-2 space-y-1">
                    <div class="flex justify-between items-center text-sm">
                        <span class="text-slate-500 font-medium">道瓊 (DJI):</span>
                        <span class="{latest_dji_c}">{latest_dji}</span>
                    </div>
                    <div class="flex justify-between items-center text-sm">
                        <span class="text-slate-500 font-medium">NASDAQ:</span>
                        <span class="{latest_ixic_c}">{latest_ixic}</span>
                    </div>
                    <div class="flex justify-between items-center text-sm">
                        <span class="text-slate-500 font-medium">費半 (SOX):</span>
                        <span class="{latest_sox_c}">{latest_sox}</span>
                    </div>
                </div>
                <p class="text-[11px] text-slate-400 mt-2">同夜盤時段美股漲跌點數</p>
            </div>
        </div>

        <!-- 今日開盤預測劇本 -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <h2 class="text-lg font-bold text-slate-800 mb-4">🔮 今日開盤預測劇本</h2>

            <div class="p-5 rounded-xl border-2 {scenario_badge_class} mb-6">
                <div class="flex items-center space-x-2">
                    <span class="font-bold text-xl">{latest_data['scenario']}</span>
                </div>
                <p class="mt-2 text-base font-semibold">{latest_data['forecast_desc']}</p>
            </div>

            <!-- 四種劇本對照指南 -->
            <div class="mt-6">
                <h3 class="text-sm font-semibold text-slate-600 mb-3">📋 四種實戰劇本對照指南</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-sm text-left text-slate-600 border-collapse">
                        <thead class="text-xs text-slate-700 uppercase bg-slate-100">
                            <tr>
                                <th class="py-3 px-4 rounded-l-lg">劇本</th>
                                <th class="py-3 px-4">夜盤漲跌</th>
                                <th class="py-3 px-4">外資多空淨額 (夜盤)</th>
                                <th class="py-3 px-4 rounded-r-lg">預期走勢</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-100">
                            <tr class="hover:bg-slate-50 {'bg-red-50/60 font-medium' if latest_data['scenario'] == '劇本一' else ''}">
                                <td class="py-3 px-4 font-bold text-red-600">劇本一</td>
                                <td class="py-3 px-4 text-red-500">上漲</td>
                                <td class="py-3 px-4 text-red-500">正數（看多）</td>
                                <td class="py-3 px-4">漲勢紮實，開盤後容易持續往上走。</td>
                            </tr>
                            <tr class="hover:bg-slate-50 {'bg-yellow-50/60 font-medium' if latest_data['scenario'] == '劇本二' else ''}">
                                <td class="py-3 px-4 font-bold text-yellow-600">劇本二</td>
                                <td class="py-3 px-4 text-red-500">上漲</td>
                                <td class="py-3 px-4 text-green-500">負數（看空）</td>
                                <td class="py-3 px-4">力道不足，容易出現開高走低，隨後殺下來。</td>
                            </tr>
                            <tr class="hover:bg-slate-50 {'bg-green-50/60 font-medium' if latest_data['scenario'] == '劇本三' else ''}">
                                <td class="py-3 px-4 font-bold text-green-600">劇本三</td>
                                <td class="py-3 px-4 text-green-500">下跌</td>
                                <td class="py-3 px-4 text-green-500">負數（看空）</td>
                                <td class="py-3 px-4">跌勢延續，不建議貿然抄底，因為可能會持續下跌。</td>
                            </tr>
                            <tr class="hover:bg-slate-50 {'bg-blue-50/60 font-medium' if latest_data['scenario'] == '劇本四' else ''}">
                                <td class="py-3 px-4 font-bold text-blue-600">劇本四</td>
                                <td class="py-3 px-4 text-red-500">下跌</td>
                                <td class="py-3 px-4 text-red-500">正數（看多）</td>
                                <td class="py-3 px-4">開低反彈，外資未跟著殺盤，反彈機率高。</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 過去 20 個交易日歷史紀錄與當日走勢比對表格 -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-lg font-bold text-slate-800">🗓️ 過去 20 個交易日歷史紀錄與實測比對</h2>
                <a href="./data/history.json" target="_blank" class="text-xs text-indigo-600 hover:underline">📥 下載完整 history.json</a>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-sm text-left text-slate-600 border-collapse">
                    <thead class="text-xs text-slate-700 uppercase bg-slate-100">
                        <tr>
                            <th class="py-3 px-4 rounded-l-lg">交易日期</th>
                            <th class="py-3 px-4">夜盤漲跌</th>
                            <th class="py-3 px-4">夜盤量佔比 (算式)</th>
                            <th class="py-3 px-4">三大法人淨額 (夜盤)</th>
                            <th class="py-3 px-4">三大法人淨額 (全日)</th>
                            <th class="py-3 px-4">美股指數 (對應夜盤)</th>
                            <th class="py-3 px-4">預測劇本</th>
                            <th class="py-3 px-4 text-center">當日加權指數走勢 (09:00~13:30)</th>
                            <th class="py-3 px-4 rounded-r-lg">型態驗證</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-100">
                        {history_rows_html}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Footer -->
        <footer class="text-center text-xs text-slate-400 py-4">
            資料來源：台灣期貨交易所 (TAIFEX) & 台灣證券交易所 (TWSE) & Yahoo Finance | 自動化發布 via GitHub Actions & Pages
        </footer>
    </div>
</body>
</html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("成功產生 index.html！")


def render_top_cards(items_list, category_label, theme="indigo"):
    if not items_list:
        return f'<div class="col-span-full p-8 text-center bg-white rounded-xl text-slate-400 text-sm">今日無{category_label}買超紀錄或目前為休市期間 (NA)</div>'

    badge_bg = f"bg-{theme}-50"
    badge_text = f"text-{theme}-700"
    badge_border = f"border-{theme}-100"
    count_bg = f"bg-{theme}-50"
    count_text = f"text-{theme}-700"

    cards_html = ""
    for rank, item in enumerate(items_list, start=1):
        brokers_badge = "".join([
                                    f'<span class="px-2 py-0.5 text-[11px] {badge_bg} {badge_text} rounded border {badge_border} font-medium">{b}</span>'
                                    for b in item["brokers"]])
        price_str = f" (${item['price']})" if item.get("price") is not None else ""

        cards_html += f"""
        <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200 flex flex-col justify-between">
            <div>
                <div class="flex items-center justify-between mb-2">
                    <span class="px-2.5 py-0.5 text-xs font-bold bg-slate-800 text-white rounded-full">第 {rank} 名</span>
                    <span class="text-xs font-bold {count_text} {count_bg} px-2 py-1 rounded-md">{item['count']} 家券商</span>
                </div>
                <h3 class="text-lg font-extrabold text-slate-800 my-1">{item['stock']} <span class="text-sm font-semibold text-slate-500">{price_str}</span></h3>
                <div class="flex flex-wrap gap-1 my-3">
                    {brokers_badge}
                </div>
            </div>
            <div class="pt-3 border-t border-slate-100 flex justify-between items-center text-xs">
                <span class="text-slate-400">總買超淨張數:</span>
                <span class="text-base font-extrabold text-red-500">+{item['total_net_buy']:,} 張</span>
            </div>
        </div>
        """
    return cards_html


def generate_broker_html(broker_records):
    latest_data = broker_records[0] if broker_records else {"date": "NA", "top_stocks": [], "top_etfs": [],
                                                            "top_domestic_stocks": [], "top_domestic_volume_stocks": [],
                                                            "top_foreign_stocks": []}

    utc_now = datetime.now(timezone.utc)
    tw_now = utc_now + timedelta(hours=8)
    utc_time_str = utc_now.strftime('%Y/%m/%d %H:%M:%S')
    tw_time_str = tw_now.strftime('%Y/%m/%d %H:%M:%S')

    top_stocks_html = render_top_cards(latest_data.get("top_stocks", []), "個股", "indigo")
    top_etfs_html = render_top_cards(latest_data.get("top_etfs", []), "ETF", "indigo")
    top_dom_stocks_html = render_top_cards(latest_data.get("top_domestic_stocks", []), "隔日沖內資個股 (家數)",
                                           "purple")

    # 箭頭處新增區塊：內資三大分點「總買超張數」排行榜
    top_dom_vol_stocks_html = render_top_cards(latest_data.get("top_domestic_volume_stocks", []), "內資總買超張數個股",
                                               "emerald")

    top_for_stocks_html = render_top_cards(latest_data.get("top_foreign_stocks", []), "隔日沖外資個股", "blue")

    history_broker_rows = ""
    for record in broker_records[:20]:
        stock_str_list = []
        for s in record.get("top_stocks", [])[:5]:
            p_str = f" (${s['price']})" if s.get("price") is not None else ""
            stock_str_list.append(
                f'<span class="inline-block bg-slate-50 border border-slate-200 px-2 py-1 rounded text-xs mr-1 mb-1"><b>{s["stock"]}</b><span class="text-slate-500 font-normal">{p_str}</span> ({s["count"]}家: +{s["total_net_buy"]:,}張)</span>')
        stocks_display = "".join(
            stock_str_list) if stock_str_list else '<span class="text-slate-400 text-xs">無紀錄</span>'

        etf_str_list = []
        for e in record.get("top_etfs", [])[:5]:
            ep_str = f" (${e['price']})" if e.get("price") is not None else ""
            etf_str_list.append(
                f'<span class="inline-block bg-indigo-50/50 border border-indigo-100 px-2 py-1 rounded text-xs mr-1 mb-1 text-indigo-900"><b>{e["stock"]}</b><span class="text-slate-500 font-normal">{ep_str}</span> ({e["count"]}家: +{e["total_net_buy"]:,}張)</span>')
        etfs_display = "".join(etf_str_list) if etf_str_list else '<span class="text-slate-400 text-xs">無紀錄</span>'

        dom_str_list = []
        for d in record.get("top_domestic_stocks", [])[:3]:
            dp_str = f" (${d['price']})" if d.get("price") is not None else ""
            dom_str_list.append(
                f'<span class="inline-block bg-purple-50/50 border border-purple-100 px-2 py-1 rounded text-xs mr-1 mb-1 text-purple-900"><b>{d["stock"]}</b> ({d["count"]}家: +{d["total_net_buy"]:,}張)</span>')
        dom_display = "".join(dom_str_list) if dom_str_list else '<span class="text-slate-400 text-xs">無紀錄</span>'

        for_str_list = []
        for f_stk in record.get("top_foreign_stocks", [])[:3]:
            fp_str = f" (${f_stk['price']})" if f_stk.get("price") is not None else ""
            for_str_list.append(
                f'<span class="inline-block bg-blue-50/50 border border-blue-100 px-2 py-1 rounded text-xs mr-1 mb-1 text-blue-900"><b>{f_stk["stock"]}</b> ({f_stk["count"]}家: +{f_stk["total_net_buy"]:,}張)</span>')
        for_display = "".join(for_str_list) if for_str_list else '<span class="text-slate-400 text-xs">無紀錄</span>'

        history_broker_rows += f"""
        <tr class="hover:bg-slate-50 transition-colors">
            <td class="py-3 px-4 font-semibold text-slate-700 whitespace-nowrap">{record['date']}</td>
            <td class="py-3 px-4">{stocks_display}</td>
            <td class="py-3 px-4">{etfs_display}</td>
            <td class="py-3 px-4">{dom_display}</td>
            <td class="py-3 px-4">{for_display}</td>
        </tr>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>主力券商買超個股與 ETF 分析</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 min-h-screen p-4 md:p-8 font-sans">
    <div class="max-w-[1400px] mx-auto space-y-6">

        <!-- Header & Nav Tabs -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div>
                    <h1 class="text-2xl font-bold text-slate-800">🏦 7 大主力券商分點聯合買超分析</h1>
                    <p class="text-slate-500 text-sm mt-1">監控分點：摩根大通、凱基台北、元大土城永寧、富邦建國、高盛、瑞銀、美林</p>
                </div>
                <div class="text-xs text-slate-500 md:text-right space-y-1">
                    <div>頁面產生時間：<span class="font-semibold text-slate-700">{tw_time_str}</span> <span class="text-slate-400">(UTC+8)</span></div>
                    <div>頁面產生時間：<span class="font-semibold text-slate-700">{utc_time_str}</span> <span class="text-slate-400">(UTC)</span></div>
                </div>
            </div>

            <div class="flex space-x-2 mt-6 border-b border-slate-100 pb-2">
                <a href="./index.html" class="px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100 rounded-lg transition-colors">📊 台指期夜盤預測</a>
                <a href="./broker.html" class="px-4 py-2 text-sm font-bold text-indigo-600 bg-indigo-50 rounded-lg border border-indigo-100">🏦 主力券商買超分析</a>
            </div>
        </div>

        <!-- 區塊一：今日 Top 10 全 7 大主力券商買超個股 (不含 ETF) -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h2 class="text-lg font-bold text-slate-800">🔥 今日 7 大主力券商聯合買超 Top 10 個股</h2>
                    <p class="text-xs text-slate-400 mt-0.5">資料日期：{latest_data['date']} | 排除 ETF | 優先比對「買超券商數」，數量相同時比對「總買超張數」</p>
                </div>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
                {top_stocks_html}
            </div>
        </div>

        <!-- 區塊二：今日 Top 10 全 7 大主力券商買超 ETF -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h2 class="text-lg font-bold text-indigo-900">📊 今日 7 大主力券商聯合買超 Top 10 ETF</h2>
                    <p class="text-xs text-slate-400 mt-0.5">資料日期：{latest_data['date']} | 專屬 ETF 排名 | 優先比對「買超券商數」，數量相同時比對「總買超張數」</p>
                </div>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
                {top_etfs_html}
            </div>
        </div>

        <!-- 區塊三：隔日沖內資分點聯合買超 Top 10 個股 (依聯合家數排名) -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-purple-200">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h2 class="text-lg font-bold text-purple-900">⚡ 隔日沖內資分點聯合買超 Top 10 個股分析 (共買家數)</h2>
                    <p class="text-xs text-slate-400 mt-0.5">資料日期：{latest_data['date']} | 監控分點：<b>凱基台北、元大土城永寧、富邦建國</b> | 優先比對「買超券商數」</p>
                </div>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
                {top_dom_stocks_html}
            </div>
        </div>

        <!-- 【箭頭處新增區塊】：內資三大分點總買超張數 Top 10 個股 (依合計總張數排名) -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-emerald-200">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h2 class="text-lg font-bold text-emerald-900">📈 內資三大分點 (凱基台北、元大土城永寧、富邦建國) 總買超張數 Top 10</h2>
                    <p class="text-xs text-slate-400 mt-0.5">資料日期：{latest_data['date']} | 依 3 大內資分點「合計總買超張數」高低直接排名 | 排除 ETF</p>
                </div>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
                {top_dom_vol_stocks_html}
            </div>
        </div>

        <!-- 區塊四：隔日沖外資分點聯合買超 Top 10 個股 -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-blue-200">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h2 class="text-lg font-bold text-blue-900">🌍 隔日沖外資分點聯合買超 Top 10 個股分析</h2>
                    <p class="text-xs text-slate-400 mt-0.5">資料日期：{latest_data['date']} | 監控分點：<b>摩根大通、美商高盛、新加坡商瑞銀、美林</b> | 排除 ETF</p>
                </div>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
                {top_for_stocks_html}
            </div>
        </div>

        <!-- 區塊五：歷史紀錄表格 -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-lg font-bold text-slate-800">🗓️ 過去 20 個交易日主力買超歷史紀錄 (全分流)</h2>
                <a href="./data/broker_history.json" target="_blank" class="text-xs text-indigo-600 hover:underline">📥 下載完整 broker_history.json</a>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-sm text-left text-slate-600 border-collapse">
                    <thead class="text-xs text-slate-700 uppercase bg-slate-100">
                        <tr>
                            <th class="py-3 px-4 rounded-l-lg whitespace-nowrap">交易日期</th>
                            <th class="py-3 px-4">7大券商個股 Top 5</th>
                            <th class="py-3 px-4">7大券商 ETF Top 5</th>
                            <th class="py-3 px-4">內資隔日沖 Top 3</th>
                            <th class="py-3 px-4 rounded-r-lg">外資主力 Top 3</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-100">
                        {history_broker_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Footer -->
        <footer class="text-center text-xs text-slate-400 py-4">
            資料來源：富邦證券 MoneyDJ 分點明細查詢 & 證交所 API | 自動化發布 via GitHub Actions & Pages
        </footer>
    </div>
</body>
</html>
"""
    with open("broker.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("成功產生新增內資總買超張數區塊的 broker.html！")