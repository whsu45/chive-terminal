def verify_match(scenario, actual_info):
    open_p, close_p = actual_info.get("open"), actual_info.get("close")
    high_p, low_p = actual_info.get("high"), actual_info.get("low")
    prices = actual_info.get("prices", [])

    if not scenario or scenario == "NA" or None in [open_p, close_p, high_p, low_p] or len(prices) < 2:
        return "尚未驗證", "bg-slate-100 text-slate-500"

    total_pts = len(prices)
    idx_high, idx_low = prices.index(high_p), prices.index(low_p)
    high_time_ratio, low_time_ratio = idx_high / total_pts, idx_low / total_pts
    total_range = high_p - low_p if high_p != low_p else 1.0

    # 收盤價在當天高低振幅中的相對位置 (0.0: 收在最低, 1.0: 收在最高)
    close_position = (close_p - low_p) / total_range
    change = close_p - open_p

    # --- 劇本一：開高走高 / 漲勢紮實 ---
    if scenario == "劇本一":
        if change > 0 and close_position >= 0.4:
            return "✅ 符合 (一路走高)", "bg-red-100 text-red-700 font-bold"
        elif change > 0:
            return "✅ 符合 (收紅上漲)", "bg-red-50 text-red-600"
        return "❌ 走勢分歧", "bg-slate-100 text-slate-600"

    # --- 劇本二：力道不足 / 開高走低 / 衝高回落 ---
    elif scenario == "劇本二":
        # 情況 A: 標準收黑開高走低
        if change < 0 and high_time_ratio <= 0.6:
            return "✅ 符合 (開高走低)", "bg-green-100 text-green-700 font-bold"
        # 情況 B: 盤中衝高後大幅殺回，收在當日振幅中下緣 (如 9/9 盤型)
        elif high_time_ratio <= 0.6 and close_position <= 0.5:
            return "✅ 符合 (衝高回落)", "bg-yellow-100 text-yellow-800 font-bold"
        elif change < 0:
            return "✅ 符合 (收黑走低)", "bg-green-50 text-green-600"
        return "❌ 走勢分歧", "bg-slate-100 text-slate-600"

    # --- 劇本三：跌勢延續 ---
    elif scenario == "劇本三":
        if change < 0 and close_position <= 0.6:
            return "✅ 符合 (跌勢延續)", "bg-green-100 text-green-700 font-bold"
        elif change < 0:
            return "✅ 符合 (收黑下跌)", "bg-green-50 text-green-600"
        return "❌ 走勢分歧", "bg-slate-100 text-slate-600"

    # --- 劇本四：開低反彈 / 探底拉升 ---
    elif scenario == "劇本四":
        # 情況 A: 標準收紅開低反彈
        if change > 0 and low_time_ratio <= 0.6:
            return "✅ 符合 (開低反彈)", "bg-red-100 text-red-700 font-bold"
        # 情況 B: 早盤殺低後一路反彈，收在當日振幅中上緣
        elif low_time_ratio <= 0.6 and close_position >= 0.5:
            return "✅ 符合 (探底回升)", "bg-blue-100 text-blue-800 font-bold"
        elif change > 0:
            return "✅ 符合 (收紅反彈)", "bg-red-50 text-red-600"
        return "❌ 走勢分歧", "bg-slate-100 text-slate-600"

    return "NA", "bg-slate-100 text-slate-500"