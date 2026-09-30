#!/usr/bin/env python
# coding: utf-8

# In[2]:


import pandas as pd
import numpy as np

class Rule456Manager:
    def __init__(self, deviation_threshold: float = 0.006, atr_multiplier: float = 1.5, max_bars: int = 12):
        self.dev_threshold = deviation_threshold  # Rule 4: 0.6%
        self.atr_mult = atr_multiplier            # Rule 5: 1.5 * ATR
        self.max_bars = max_bars                  # Rule 6: 12 bars (6 hours)

    # -------------------------------------------------------------
    # Rule 4 计算 |Price - SMA48| / Price 是否大于 0.6%
    # -------------------------------------------------------------
    def check_rule4_deviation(self, price: float, sma48: float) -> bool:
        if price <= 0:
            return False
        deviation = abs(price - sma48) / price
        return deviation > self.dev_threshold

    # -------------------------------------------------------------
    # Rule 5 & 6 检查是否触发 Rule 5 止损或 Rule 6 时间止损；返回: (是否平仓, 平仓原因)
    # -------------------------------------------------------------
    def check_exit_conditions(
        self, 
        direction: int,          # 1 为 Long, -1 为 Short
        entry_price: float,      # 入场价格
        current_price: float,    # 当前最新价格
        entry_atr: float,        # 入场时的 ATR(14)
        bars_held: int           # 当前持仓的 bar 数量
    ) -> tuple[bool, str]:

        # 1. 计算逆向移动距离 (Adverse Move)
        if direction == 1:
            adverse_move = entry_price - current_price
        else:
            adverse_move = current_price - entry_price

        # 2. Rule 5 判定：Adverse Move > 1.5 * ATR(14)
        stop_loss_distance = self.atr_mult * entry_atr
        if adverse_move > stop_loss_distance:
            return True, f"Rule 5 Triggered: Adverse Move ({adverse_move:.4f}) > 1.5*ATR ({stop_loss_distance:.4f})"

        # 3. Rule 6 判定：持仓超过 12 个 K 线 (6 小时)
        if bars_held > self.max_bars:
            return True, f"Rule 6 Triggered: Hold Time Reached {bars_held} bars (> 12 bars / 6h)"

        return False, "Hold"

"""
# ==========================================
# 示例：验证逻辑运行
# ==========================================
if __name__ == "__main__":
    manager = Rule456Manager()

    # 1. 测试 Rule 4
    p_curr, sma_val = 100.0, 99.2
    rule4_pass = manager.check_rule4_deviation(price=p_curr, sma48=sma_val)
    print(f"Rule 4 Check (|100 - 99.2| / 100 = 0.8% > 0.6%): {rule4_pass}")  # 应返回 True

    # 2. 测试 Rule 5 止损
    # 多头开仓价 100，当前价 97，入场 ATR 为 1.5 -> 逆向变动 3.0 > 1.5 * 1.5 = 2.25
    should_exit, reason = manager.check_exit_conditions(
        direction=1, entry_price=100.0, current_price=97.0, entry_atr=1.5, bars_held=5
    )
    print(f"Rule 5 Exit Check: {should_exit} | Reason: {reason}")

    # 3. 测试 Rule 6 时间止损
    # 持仓达到 13 bars
    should_exit_t, reason_t = manager.check_exit_conditions(
        direction=1, entry_price=100.0, current_price=99.5, entry_atr=1.5, bars_held=13
    )
    print(f"Rule 6 Exit Check: {should_exit_t} | Reason: {reason_t}")
"""


# In[ ]:




