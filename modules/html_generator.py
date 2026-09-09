def generate_broker_html(broker_records):
    latest_data = broker_records[0] if broker_records else {
        "date": "NA", "top_stocks": [], "top_etfs": [],
        "top_domestic_stocks": [], "top_domestic_volume_stocks": [],
        "top_foreign_stocks": []
    }

    utc_now = datetime.now(timezone.utc)
    tw_now = utc_now + timedelta(hours=8)
    utc_time_str = utc_now.strftime('%Y/%m/%d %H:%M:%S')
    tw_time_str = tw_now.strftime('%Y/%m/%d %H:%M:%S')

    top_stocks_html = render_top_cards(latest_data.get("top_stocks", []), "個股", "indigo")
    top_etfs_html = render_top_cards(latest_data.get("top_etfs", []), "ETF", "indigo")
    top_dom_stocks_html = render_top_cards(latest_data.get("top_domestic_stocks", []), "隔日沖內資個股 (家數)", "purple")
    top_dom_vol_stocks_html = render_top_cards(latest_data.get("top_domestic_volume_stocks", []), "內資總買超張數個股", "emerald")
    top_for_stocks_html = render_top_cards(latest_data.get("top_foreign_stocks", []), "隔日沖外資個股", "blue")

    history_broker_rows = ""
    for record in broker_records[:20]:
        stock_str_list = []
        for s in record.get("top_stocks", [])[:5]:
            p_str = f" (${s['price']})" if s.get("price") is not None else ""
            stock_str_list.append(
                f'<span class="inline-block bg-slate-50 border border-slate-200 px-2 py-1 rounded text-xs mr-1 mb-1"><b>{s["stock"]}</b><span class="text-slate-500 font-normal">{p_str}</span> ({s["count"]}家: +{s["total_net_buy"]:,}張)</span>'
            )
        stocks_display = "".join(stock_str_list) if stock_str_list else '<span class="text-slate-400 text-xs">無紀錄</span>'

        etf_str_list = []
        for e in record.get("top_etfs", [])[:5]:
            ep_str = f" (${e['price']})" if e.get("price") is not None else ""
            etf_str_list.append(
                f'<span class="inline-block bg-indigo-50/50 border border-indigo-100 px-2 py-1 rounded text-xs mr-1 mb-1 text-indigo-900"><b>{e["stock"]}</b><span class="text-slate-500 font-normal">{ep_str}</span> ({e["count"]}家: +{e["total_net_buy"]:,}張)</span>'
            )
        etfs_display = "".join(etf_str_list) if etf_str_list else '<span class="text-slate-400 text-xs">無紀錄</span>'

        dom_str_list = []
        for d in record.get("top_domestic_stocks", [])[:3]:
            dom_str_list.append(
                f'<span class="inline-block bg-purple-50/50 border border-purple-100 px-2 py-1 rounded text-xs mr-1 mb-1 text-purple-900"><b>{d["stock"]}</b> ({d["count"]}家: +{d["total_net_buy"]:,}張)</span>'
            )
        dom_display = "".join(dom_str_list) if dom_str_list else '<span class="text-slate-400 text-xs">無紀錄</span>'

        for_str_list = []
        for f_stk in record.get("top_foreign_stocks", [])[:3]:
            for_str_list.append(
                f'<span class="inline-block bg-blue-50/50 border border-blue-100 px-2 py-1 rounded text-xs mr-1 mb-1 text-blue-900"><b>{f_stk["stock"]}</b> ({f_stk["count"]}家: +{f_stk["total_net_buy"]:,}張)</span>'
            )
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
                    <h1 class="text-2xl font-bold text-slate-800">🏦 12 大關鍵主力券商分點聯合買超分析</h1>
                    <p class="text-slate-500 text-sm mt-1">
                        <b>內資(7家)</b>：凱基台北、元大土城永寧、富邦建國、凱基市政、凱基虎尾、富邦虎尾、凱基松山 ｜ 
                        <b>外資(5家)</b>：摩根大通、美商高盛、新加坡商瑞銀、美林、港商野村
                    </p>
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

        <!-- 區塊一：今日 Top 10 全主力券商買超個股 (不含 ETF) -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h2 class="text-lg font-bold text-slate-800">🔥 今日 12 大主力券商聯合買超 Top 10 個股</h2>
                    <p class="text-xs text-slate-400 mt-0.5">資料日期：{latest_data['date']} | 排除 ETF | 優先比對「買超券商數」，數量相同時比對「總買超張數」</p>
                </div>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
                {top_stocks_html}
            </div>
        </div>

        <!-- 區塊二：今日 Top 10 全主力券商買超 ETF -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h2 class="text-lg font-bold text-indigo-900">📊 今日 12 大主力券商聯合買超 Top 10 ETF</h2>
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
                    <h2 class="text-lg font-bold text-purple-900">⚡ 隔日沖內資分點聯合買超 Top 10 個股 (共買家數)</h2>
                    <p class="text-xs text-slate-400 mt-0.5">資料日期：{latest_data['date']} | 監控分點：<b>凱基台北、元大土城永寧、富邦建國、凱基市政、凱基虎尾、富邦虎尾、凱基松山</b> | 優先比對「買超券商數」</p>
                </div>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
                {top_dom_stocks_html}
            </div>
        </div>

        <!-- 區塊四：內資分點總買超張數 Top 10 個股 (依合計總張數排名) -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-emerald-200">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h2 class="text-lg font-bold text-emerald-900">📈 內資隔日沖分點總買超張數 Top 10</h2>
                    <p class="text-xs text-slate-400 mt-0.5">資料日期：{latest_data['date']} | 依 7 大內資分點「合計總買超張數」高低直接排名 | 排除 ETF</p>
                </div>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
                {top_dom_vol_stocks_html}
            </div>
        </div>

        <!-- 區塊五：隔日沖外資分點聯合買超 Top 10 個股 -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-blue-200">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h2 class="text-lg font-bold text-blue-900">🌍 隔日沖外資分點聯合買超 Top 10 個股分析</h2>
                    <p class="text-xs text-slate-400 mt-0.5">資料日期：{latest_data['date']} | 監控分點：<b>摩根大通、美商高盛、新加坡商瑞銀、美林、港商野村</b> | 排除 ETF</p>
                </div>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
                {top_for_stocks_html}
            </div>
        </div>

        <!-- 區塊六：歷史紀錄表格 -->
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
                            <th class="py-3 px-4">主力券商個股 Top 5</th>
                            <th class="py-3 px-4">主力券商 ETF Top 5</th>
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
    print("成功產生新增全分點的 broker.html！")