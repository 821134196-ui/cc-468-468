"""模拟天气适配器：返回雨量(mm/h)及各入口水位(cm)。

适配器为演示数据：内置一段"上涨—峰值—回落"的确定性降雨过程，
也允许人工注入雨量用于演示/测试；水位由雨量结合入口地势系数推算并带滞后。
"""


class MockWeatherAdapter:
    # 18 个节拍的脚本：晴 → 蓝 → 黄 → 橙 → 红 → 回落
    SCRIPT = [0, 3, 9, 16, 24, 33, 42, 55, 48, 36, 26, 18, 12, 7, 3, 1, 0, 0]

    def __init__(self):
        self.tick = 0
        self.manual = None          # 人工注入雨量；None 表示走脚本
        # 各入口地势系数：越大越易积水
        self.factors = {}

    def set_factor(self, entrance_code, factor):
        self.factors[entrance_code] = factor

    def rainfall(self):
        if self.manual is not None:
            return float(self.manual)
        return float(self.SCRIPT[self.tick % len(self.SCRIPT)])

    def set_manual(self, mm):
        self.manual = None if mm is None else float(mm)

    def advance(self):
        self.tick += 1
        return self.rainfall()

    def water_levels(self, entrances, prev):
        """prev: {entrance_id: 上次水位cm}；雨量向水位传导并带排水滞后。"""
        mm = self.rainfall()
        out = {}
        for e in entrances:
            f = self.factors.get(e["code"], 0.30)
            target = mm * f
            last = prev.get(e["id"], 0.0)
            # 上涨跟得快，回落靠排水较慢
            nxt = target if target >= last else last * 0.7 + target * 0.3
            out[e["id"]] = round(max(0.0, nxt), 1)
        return out
