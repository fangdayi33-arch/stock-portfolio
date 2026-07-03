# -*- coding: utf-8 -*-
"""
港股打新 (IPO) 自动化数据抓取与量化评级引擎
数据源：致富证券新股页面 + 腾讯股票行情 API
"""

import requests
import json
import re
from datetime import datetime

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://www.chiefgroup.com.hk/cn/securities/hk-ipo/dp'
}

# A+H 股票映射：港股代码 → A股代码（腾讯格式）
AH_MAP = {
    "02475": "sz002475",  # 立讯精密
    "02249": "sh688249",  # 晶合集成
    "06951": "sz300408",  # 三环集团
    "00537": "sh688337",  # 普源精电
    "01377": "sh688981",  # 中泰高科 (不确定)
    # 可继续补充
}

def get_stock_sector(name):
    """根据公司名关键词判断行业（简化版）"""
    kw_map = {
        "医药|生物|医疗|医养|药": "医药健康",
        "科技|半导体|芯片|电子|智能|机器|AI|数据|算力": "科技/半导体",
        "汽车|新能源|锂电|光伏": "新能源汽车",
        "银行|保险|证券|基金": "金融",
        "食品|饮料|酒|餐饮": "食品饮料",
        "地产|物业|房产": "房地产",
        "教育": "教育",
        "能源|石油|煤炭|电力": "能源",
        "消费|零售|电商|购物": "消费零售",
        "材料|化工|钢铁": "材料/化工",
        "建筑|基建|工程": "建筑/基建",
        "通信|5G|互联": "通信/互联网",
        "制造|机械|设备|工业": "工业制造",
        "航空|物流|运输|交通": "物流/交通",
        "农业|畜牧|水产|养殖": "农业",
    }
    for kw, sector in kw_map.items():
        import re
        if re.search(kw, name):
            return sector
    return "待更新"

def fetch_chief_page():
    """抓取致富证券新股列表页面"""
    resp = requests.get(
        "https://www.chiefgroup.com.hk/cn/securities/hk-ipo/dp",
        headers=HEADERS, timeout=15
    )
    resp.encoding = 'utf-8'
    return resp.text

def parse_upcoming_ipos(html):
    """解析招股中新股表格 (Table 4)
    列: 股票编号 | 股票名称 | 发售股份 | 票面值 | 招股价 | 上市日期
    """
    tables = re.findall(r'(<table[^>]*>.*?</table>)', html, re.DOTALL)
    table4 = tables[4]  # 第5个 table 是招股中的

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table4, re.DOTALL)
    ipos = []

    for row in rows[1:]:  # 跳过表头
        cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', row, re.DOTALL)
        cleaned = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        if len(cleaned) < 6:
            continue

        code = cleaned[0].strip()
        name = cleaned[1].strip()
        shares_str = cleaned[2].strip().replace(',', '')
        price_str = cleaned[4].strip().replace(',', '')
        list_date = cleaned[5].strip()

        # 过滤无效行
        if not code.isdigit() or len(code) != 5:
            continue

        # 计算流通盘 (亿港元)
        try:
            shares = float(shares_str) if shares_str.replace('.','',1).isdigit() else 0
            offer_price = float(price_str) if price_str.replace('.','',1).replace('N/A','').strip() else 0
        except ValueError:
            continue
        if shares <= 0 or offer_price <= 0:
            continue

        float_cap_hkd = round(shares * offer_price / 100000000, 2)

        ipos.append({
            "status": "active",
            "code": code,
            "name": name,
            "listDate": list_date,
            "offerPrice": offer_price,
            "sharesOffered": int(shares),
            "floatCap": float_cap_hkd,
        })

    return ipos

def parse_listed_ipos(html):
    """解析已上市新股表格 (Table 5)
    列: 股票编号 | 股票名称 | 上市日期 | ? | 招股价 | 收市价 | 变动%
    """
    tables = re.findall(r'(<table[^>]*>.*?</table>)', html, re.DOTALL)
    table5 = tables[5]

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table5, re.DOTALL)
    ipos = []

    for row in rows[1:]:
        cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', row, re.DOTALL)
        cleaned = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        if len(cleaned) < 7:
            continue

        code = cleaned[0].strip()
        name = cleaned[1].strip()
        list_date = cleaned[2].strip()
        offer_price_str = cleaned[4].strip().replace(',', '')
        current_price_str = cleaned[5].strip().replace(',', '')
        change_str = cleaned[6].strip()

        if not code.isdigit() or len(code) != 5:
            continue

        try:
            offer_price = float(offer_price_str)
            current_price = float(current_price_str)
        except ValueError:
            continue

        # 计算首日表现
        if offer_price > 0:
            pct_change = round((current_price - offer_price) / offer_price * 100, 1)
            sign = "+" if pct_change > 0 else ""
            debut_str = f"{sign}{pct_change}%"
        else:
            debut_str = "N/A"

        ipos.append({
            "status": "listed",
            "code": code,
            "name": name,
            "listDate": list_date,
            "offerPrice": offer_price,
            "currentPrice": current_price,
            "debut": debut_str,
            "profit": "参考首日表现"
        })

    return ipos

def fetch_tencent_quotes(codes):
    """批量获取腾讯行情数据"""
    if not codes:
        return {}

    # 港股 + A股
    query = ",".join([f"hk{c}" for c in codes] + list(AH_MAP.values()))

    try:
        resp = requests.get(f"https://qt.gtimg.cn/q={query}", timeout=10)
        resp.encoding = 'gbk'
        result = {}

        for line in resp.text.strip().split(';'):
            if not line.strip():
                continue
            parts = line.split('~')
            if len(parts) < 5:
                continue

            raw_name = parts[0].split('=')[0].strip()
            var_name = raw_name.lstrip('v_')
            name = parts[1] if len(parts) > 1 else ''
            price_str = parts[3] if len(parts) > 3 else '0'
            prev_close = parts[4] if len(parts) > 4 else '0'

            try:
                price_f = float(price_str)
            except ValueError:
                price_f = 0.0
            try:
                prev_close_f = float(prev_close)
            except ValueError:
                prev_close_f = 0.0

            result[var_name] = {
                "name": name,
                "price": price_f,
                "prevClose": prev_close_f
            }

        return result
    except Exception as e:
        print(f"  腾讯行情获取异常: {e}")
        return {}

def calculate_ah_discount(hk_code, quotes, offer_price=None):
    """计算 A+H 折价率（用招股价或实时股价）"""
    if hk_code not in AH_MAP:
        return None, None

    a_code = AH_MAP[hk_code]
    a_data = quotes.get(a_code)

    if not a_data:
        return None, None

    a_price = a_data.get("price", 0)
    if a_price <= 0:
        return None, a_price

    # 用实时股价(已上市)或招股价(招股中)计算
    hk_data = quotes.get(f"hk{hk_code}")
    hk_price = hk_data.get("price", 0) if hk_data else 0
    if hk_price <= 0 and offer_price:
        hk_price = offer_price

    discount = round((a_price - hk_price) / a_price * 100, 1) if hk_price > 0 else None
    return discount, a_price

def evaluate_ipo(stock):
    """
    量化评分系统
    基于流通盘、A+H折价等公开数据给出评级
    """
    score = 10
    reasons = []
    float_cap = stock.get("floatCap", 0)

    # 流通盘评估
    if float_cap > 100:
        score -= 5
        reasons.append(f"超级大盘股 ({float_cap}亿)，首日涨幅空间有限")
    elif float_cap > 30:
        score += 5
        reasons.append(f"中大盘股 ({float_cap}亿)，走势稳健")
    elif float_cap > 5:
        score += 10
        reasons.append(f"中小盘 ({float_cap}亿)，炒作空间适中")
    elif float_cap > 0:
        score += 15
        reasons.append(f"小盘股 ({float_cap}亿)，易被资金炒作")

    # A+H 折价安全垫
    ah_discount = stock.get("ahDiscount")
    if ah_discount is not None:
        if ah_discount >= 40:
            score += 25
            reasons.append(f"较 A 股折价 {ah_discount}%，安全垫极厚")
        elif ah_discount >= 20:
            score += 15
            reasons.append(f"A/H 折价 {ah_discount}%，有一定安全边际")
        elif ah_discount >= 10:
            score += 5
            reasons.append(f"A/H 折价 {ah_discount}%，安全空间一般")
        else:
            score -= 5
            reasons.append(f"A/H 折价仅 {ah_discount}%，安全空间不足")

    # 定性评级
    rating = "观望"
    if score >= 50:
        rating = "积极关注"
    elif score >= 30:
        rating = "可参与"
    elif float_cap < 3 and float_cap > 0:
        rating = "投机博弈"

    return score, rating, reasons[:2]

def run_pipeline():
    print("启动 2026 港股打新量化分析抓取流水线...")

    # 1. 抓取汇率
    exchange_rate = 0.9125
    try:
        rate_res = requests.get("https://qt.gtimg.cn/q=fx_shkhkd", headers={
            'User-Agent': HEADERS['User-Agent']
        }, timeout=10)
        rate_res.encoding = 'gbk'
        parts = rate_res.text.split('~')
        if len(parts) > 1:
            exchange_rate = float(parts[1])
            print(f"实时汇率同步成功：1 港元 = {exchange_rate} 人民币")
    except Exception as e:
        print(f"汇率获取异常，使用默认值: {e}")

    # 2. 抓取致富证券新股页面
    print("正在抓取致富证券新股列表...")
    try:
        html = fetch_chief_page()
        print("  页面获取成功")
    except Exception as e:
        print(f"  页面获取失败: {e}")
        return

    # 3. 解析招股中新股
    upcoming = parse_upcoming_ipos(html)
    print(f"  解析到 {len(upcoming)} 只招股中新股")

    # 4. 解析已上市新股 (取最近20只)
    listed_all = parse_listed_ipos(html)
    listed = listed_all[:20]
    print(f"  解析到 {len(listed)} 只已上市新股参考")

    # 5. 获取腾讯行情数据
    all_codes = list(set([s["code"] for s in upcoming] + [s["code"] for s in listed]))
    print(f"正在获取 {len(all_codes)} 只股票的行情数据...")
    quotes = fetch_tencent_quotes(all_codes)
    print(f"  获取到 {len(quotes)} 条行情数据")

    # 6. 丰富招股中新股数据
    active_count = 0
    for stock in upcoming:
        hk_data = quotes.get(f"hk{stock['code']}", {})
        stock["sectorName"] = get_stock_sector(stock["name"])

        # A+H 折价计算
        discount, a_price = calculate_ah_discount(stock["code"], quotes, stock.get("offerPrice"))
        stock["isAH"] = discount is not None
        stock["ahDiscount"] = discount
        stock["aSharePrice"] = a_price

        # 评分
        score, rating, reasons = evaluate_ipo(stock)
        stock["score"] = score
        stock["rating"] = rating
        stock["reasons"] = reasons

        # 标记数据完整度
        stock["dataQuality"] = "partial"  # 部分数据来自公开源

        active_count += 1

    # 7. 丰富已上市数据
    for stock in listed:
        hk_data = quotes.get(f"hk{stock['code']}", {})
        # 如果有实时价格，覆盖当前价
        if hk_data and hk_data.get("price", 0) > 0:
            stock["currentPrice"] = hk_data["price"]
            if stock.get("offerPrice", 0) > 0:
                pct = round((hk_data["price"] - stock["offerPrice"]) / stock["offerPrice"] * 100, 1)
                sign = "+" if pct > 0 else ""
                stock["debut"] = f"{sign}{pct}%"
        stock["sectorName"] = get_stock_sector(stock["name"])
        # 已上市不参与评分
        stock.pop("score", None)
        stock.pop("rating", None)
        stock.pop("reasons", None)

    # 8. 合并输出（前端兼容的平铺数组格式）
    output = upcoming + listed

    # 附加元数据
    meta = {
        "_meta": {
            "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "exchangeRate": exchange_rate,
            "source": "致富证券新股列表 + 腾讯行情"
        }
    }

    with open("ipo_daily_report.json", "w", encoding="utf-8") as f:
        json.dump(output + [meta], f, ensure_ascii=False, indent=4)

    print(f"\n数据清洗分析完毕")
    print(f"  招股中: {active_count} 只")
    print(f"  已上市参考: {len(listed)} 只")
    print(f"  目标文件 'ipo_daily_report.json' 已生成")

if __name__ == "__main__":
    run_pipeline()
