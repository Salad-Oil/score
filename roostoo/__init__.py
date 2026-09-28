"""Roostoo quantitative trading bot.

A dependency-free, strategy-agnostic harness for the Roostoo mock exchange:

    client      signed REST access (HMAC-SHA256) + offline simulator
    indicators  pure-python technical analysis over rolling windows
    metrics     Sharpe / Sortino / Calmar -- the hackathon's scoring formula
    risk        position sizing, exposure caps, stops, kill switch
    strategies  pluggable signal generators
    engine      the autonomous decision loop
    journal     append-only audit trail (trade-log integrity for judging)
"""

__version__ = "0.1.0"
