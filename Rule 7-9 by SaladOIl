def calculate_position_size(
    NAV,
    entry_price,
    current_price,
    current_quantity,
    current_gross_exposure,
    risk_per_trade=0.005,      # 0.5%
    max_asset_weight=0.15,     # 15%
    max_gross_exposure=0.60    # 60%
):
    """
    Returns:
        quantity: 最终允许交易的币数量
        position_value: 最终仓位价值
    """

    # -------------------------
    # Rule 7: 每笔最多亏 0.5% NAV
    # -------------------------
    max_loss = NAV * risk_per_trade
    current_loss = (entry_price-current_price) * current_quantity

    if current_loss>=max_loss:
        return True    #平仓

    return False
    # -------------------------
    # Rule 8: 单币最多 15% NAV
    # -------------------------
    max_asset_value = NAV * max_asset_weight    #最多能买多少钱的

    # -------------------------
    # Rule 9: Gross Exposure 最多 60% NAV
    # -------------------------
    max_total_gross = NAV * max_gross_exposure

    remaining_gross_capacity = max_total_gross - current_gross_exposure

    # 如果已经达到总仓位上限，则不能开新仓
    if remaining_gross_capacity <= 0:
        return 0.0, 0.0

    # -------------------------
    # 最终允许的仓位价值
    # -------------------------
    final_position_value = min(
        max_asset_value,
        remaining_gross_capacity
    )

    quantity = final_position_value / entry_price

    return quantity, final_position_value
