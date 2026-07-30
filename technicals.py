from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

from data_fetching import clean_history

import logging

logger = logging.getLogger(__name__)


# ============================================================
# Helpery wskaźników trendu (czysty pandas/numpy, dane OHLC)
# ============================================================

def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wygładzanie Wildera (równoważne EMA z alpha=1/period)."""
    return series.ewm(alpha=1 / period, adjust=False).mean()


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range (Wilder)."""
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return _wilder_smooth(tr, period)


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> dict:
    """ADX, +DI i -DI (Wilder). Zwraca ostatnie wartości."""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    atr = _atr(high, low, close, period)
    atr_safe = atr.replace(0, np.nan)
    plus_di = 100 * _wilder_smooth(plus_dm, period) / atr_safe
    minus_di = 100 * _wilder_smooth(minus_dm, period) / atr_safe

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx = _wilder_smooth(dx.fillna(0), period)

    def _last(s):
        v = s.iloc[-1]
        return None if pd.isna(v) else float(v)

    return {"adx": _last(adx), "plus_di": _last(plus_di), "minus_di": _last(minus_di)}


def _supertrend(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 10, multiplier: float = 3.0) -> Optional[str]:
    """Supertrend — zwraca 'long' / 'short' na ostatniej sesji."""
    atr = _atr(high, low, close, period)
    hl2 = (high + low) / 2
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    final_upper = upper.copy()
    final_lower = lower.copy()
    direction = pd.Series(index=close.index, dtype="float64")

    for i in range(len(close)):
        if i == 0:
            direction.iloc[i] = 1.0
            continue
        # zaciskanie pasm
        if close.iloc[i - 1] <= final_upper.iloc[i - 1]:
            final_upper.iloc[i] = min(upper.iloc[i], final_upper.iloc[i - 1])
        if close.iloc[i - 1] >= final_lower.iloc[i - 1]:
            final_lower.iloc[i] = max(lower.iloc[i], final_lower.iloc[i - 1])

        prev_dir = direction.iloc[i - 1]
        if close.iloc[i] > final_upper.iloc[i - 1]:
            direction.iloc[i] = 1.0
        elif close.iloc[i] < final_lower.iloc[i - 1]:
            direction.iloc[i] = -1.0
        else:
            direction.iloc[i] = prev_dir

    last = direction.iloc[-1]
    if pd.isna(last):
        return None
    return "long" if last > 0 else "short"


def _ichimoku_cloud(high: pd.Series, low: pd.Series, close: pd.Series) -> Optional[str]:
    """Pozycja ceny względem chmury Ichimoku: 'above' / 'below' / 'inside'."""
    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(26)
    senkou_b = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)

    a = senkou_a.iloc[-1]
    b = senkou_b.iloc[-1]
    price = close.iloc[-1]
    if pd.isna(a) or pd.isna(b) or pd.isna(price):
        return None
    cloud_top = max(a, b)
    cloud_bottom = min(a, b)
    if price > cloud_top:
        return "above"
    if price < cloud_bottom:
        return "below"
    return "inside"


def _donchian_signal(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20) -> Optional[str]:
    """Wybicie kanału Donchiana z ostatnich `period` sesji (bez bieżącej)."""
    if len(close) < period + 1:
        return None
    prior_high = high.shift(1).rolling(period).max().iloc[-1]
    prior_low = low.shift(1).rolling(period).min().iloc[-1]
    price = close.iloc[-1]
    if pd.isna(prior_high) or pd.isna(prior_low) or pd.isna(price):
        return None
    if price >= prior_high:
        return "breakout_up"
    if price <= prior_low:
        return "breakout_down"
    return "inside"


def _linreg_slope_pct(close: pd.Series, window: int = 30) -> Optional[float]:
    """Nachylenie regresji liniowej z ostatnich `window` sesji, jako %/sesję względem ceny."""
    if len(close) < window:
        return None
    y = close.iloc[-window:].to_numpy(dtype="float64")
    x = np.arange(window, dtype="float64")
    if np.isnan(y).any():
        return None
    slope = np.polyfit(x, y, 1)[0]
    base = y.mean()
    if base == 0:
        return None
    return round(slope / base * 100, 3)


def calc_relative_strength(ticker: str, benchmark: str, period: str = "1mo") -> float | None:
    """Calculate relative strength versus a benchmark over a selected period.

    Krańce okna to średnie z 3 sesji (nie pojedyncze punkty) — jeden skrajny
    dzień na początku/końcu okna nie przestawia wyniku o kilka punktów proc.
    """
    try:
        t_stock = yf.Ticker(ticker)
        t_bench = yf.Ticker(benchmark)
        stock = clean_history(ticker, t_stock.history(period=period), t_stock)
        bench = clean_history(benchmark, t_bench.history(period=period), t_bench)
        if len(stock) >= 6 and len(bench) >= 6:
            stock_ret = (stock["Close"].iloc[-3:].mean() / stock["Close"].iloc[:3].mean() - 1) * 100
            bench_ret = (bench["Close"].iloc[-3:].mean() / bench["Close"].iloc[:3].mean() - 1) * 100
            return round(stock_ret - bench_ret, 2)
        if len(stock) >= 2 and len(bench) >= 2:
            # Za krótka historia na wygładzanie — wariant punktowy
            stock_ret = (stock["Close"].iloc[-1] / stock["Close"].iloc[0] - 1) * 100
            bench_ret = (bench["Close"].iloc[-1] / bench["Close"].iloc[0] - 1) * 100
            return round(stock_ret - bench_ret, 2)
    except Exception:
        pass
    return None


def _empty_technicals() -> dict:
    return {
        "rsi_14": None,
        "price_vs_sma20": None,
        "price_vs_sma50": None,
        "volume_ratio_10d": None,
        "change_5d": None,
        "macd": None,
        "macd_signal": None,
        "macd_histogram": None,
        "macd_trend": None,
        "bollinger_position": None,
        "bollinger_bandwidth": None,
        "bollinger_signal": None,
        # --- Wskaźniki trendu ---
        "ema_20": None,
        "ema_50": None,
        "ema_200": None,
        "sma_100": None,
        "sma_200": None,
        "ema_stack": None,
        "adx": None,
        "plus_di": None,
        "minus_di": None,
        "atr": None,
        "atr_pct": None,
        "donchian_signal": None,
        "reg_slope_pct": None,
        "supertrend_dir": None,
        "ichimoku_cloud": None,
    }


def fetch_technicals(ticker: str) -> dict:
    """Calculate RSI, SMA, MACD, Bollinger Bands and trend indicators from price history."""
    try:
        t = yf.Ticker(ticker)
        # 2 lata historii — potrzebne dla SMA200 / EMA200 i wygrzania wskaźników trendu
        hist = clean_history(ticker, t.history(period="2y"), t)
        if hist.empty or len(hist) < 14:
            return _empty_technicals()

        close = hist["Close"]
        high = hist["High"]
        low = hist["Low"]
        price = close.iloc[-1]

        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -1 * delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        sma_20 = close.rolling(20).mean().iloc[-1]
        sma_50 = close.rolling(50).mean().iloc[-1]

        change_5d = None
        if len(hist) >= 6:
            price_5d_ago = close.iloc[-6]
            if price_5d_ago and not pd.isna(price_5d_ago):
                change_5d = round(((price - price_5d_ago) / price_5d_ago) * 100, 2)

        try:
            avg_vol = t.fast_info["tenDayAverageVolume"]
        except Exception:
            avg_vol = hist["Volume"].rolling(10).mean().iloc[-1]
        last_vol = hist["Volume"].iloc[-1]
        vol_ratio = last_vol / avg_vol if avg_vol and avg_vol > 0 else None

        ema_12 = close.ewm(span=12, adjust=False).mean()
        ema_26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema_12 - ema_26
        macd_signal_line = macd_line.ewm(span=9, adjust=False).mean()
        macd_histogram = macd_line - macd_signal_line
        macd_value = macd_line.iloc[-1]
        macd_signal_value = macd_signal_line.iloc[-1]
        macd_hist_value = macd_histogram.iloc[-1]
        prev_macd_hist_value = macd_histogram.iloc[-2] if len(macd_histogram) >= 2 else None

        macd_trend = None
        if not pd.isna(macd_hist_value):
            if prev_macd_hist_value is not None and not pd.isna(prev_macd_hist_value):
                if prev_macd_hist_value <= 0 < macd_hist_value:
                    macd_trend = "bullish_cross"
                elif prev_macd_hist_value >= 0 > macd_hist_value:
                    macd_trend = "bearish_cross"
                elif macd_hist_value > prev_macd_hist_value:
                    macd_trend = "improving"
                elif macd_hist_value < prev_macd_hist_value:
                    macd_trend = "weakening"
            if macd_trend is None:
                macd_trend = "positive" if macd_hist_value > 0 else "negative"

        rolling_std_20 = close.rolling(20).std().iloc[-1]
        bollinger_position = None
        bollinger_bandwidth = None
        bollinger_signal = None
        if sma_20 and not pd.isna(sma_20) and rolling_std_20 and not pd.isna(rolling_std_20):
            upper_band = sma_20 + (2 * rolling_std_20)
            lower_band = sma_20 - (2 * rolling_std_20)
            band_range = upper_band - lower_band
            if band_range:
                bollinger_position = (price - lower_band) / band_range
                bollinger_bandwidth = (band_range / sma_20) * 100
                if price > upper_band:
                    bollinger_signal = "above_upper_band"
                elif price < lower_band:
                    bollinger_signal = "below_lower_band"
                elif bollinger_position >= 0.8:
                    bollinger_signal = "near_upper_band"
                elif bollinger_position <= 0.2:
                    bollinger_signal = "near_lower_band"
                else:
                    bollinger_signal = "inside_bands"

        # ===== Wskaźniki trendu =====
        def _last_val(series):
            v = series.iloc[-1]
            return None if pd.isna(v) else float(v)

        ema_20 = _last_val(close.ewm(span=20, adjust=False).mean())
        ema_50 = _last_val(close.ewm(span=50, adjust=False).mean())
        ema_200 = _last_val(close.ewm(span=200, adjust=False).mean())
        sma_100 = _last_val(close.rolling(100).mean())
        sma_200 = _last_val(close.rolling(200).mean())

        ema_stack = None
        if None not in (ema_20, ema_50, sma_200):
            if ema_20 > ema_50 > sma_200:
                ema_stack = "strong_up"
            elif ema_20 < ema_50 < sma_200:
                ema_stack = "strong_down"
            else:
                ema_stack = "mixed"

        adx_data = _adx(high, low, close, 14)
        atr_series = _atr(high, low, close, 14)
        atr_value = _last_val(atr_series)
        atr_pct = round(atr_value / price * 100, 2) if atr_value and price else None

        supertrend_dir = _supertrend(high, low, close, 10, 3.0)
        ichimoku_cloud = _ichimoku_cloud(high, low, close)
        donchian_signal = _donchian_signal(high, low, close, 20)
        reg_slope_pct = _linreg_slope_pct(close, 30)

        return {
            "rsi_14": round(rsi.iloc[-1], 1) if not pd.isna(rsi.iloc[-1]) else None,
            "price_vs_sma20": round((price / sma_20 - 1) * 100, 2) if sma_20 and not pd.isna(sma_20) else None,
            "price_vs_sma50": round((price / sma_50 - 1) * 100, 2) if sma_50 and not pd.isna(sma_50) else None,
            "volume_ratio_10d": round(vol_ratio, 2) if vol_ratio and not pd.isna(vol_ratio) else None,
            "change_5d": change_5d,
            "macd": round(macd_value, 3) if not pd.isna(macd_value) else None,
            "macd_signal": round(macd_signal_value, 3) if not pd.isna(macd_signal_value) else None,
            "macd_histogram": round(macd_hist_value, 3) if not pd.isna(macd_hist_value) else None,
            "macd_trend": macd_trend,
            "bollinger_position": round(bollinger_position, 2) if bollinger_position is not None and not pd.isna(bollinger_position) else None,
            "bollinger_bandwidth": round(bollinger_bandwidth, 2) if bollinger_bandwidth is not None and not pd.isna(bollinger_bandwidth) else None,
            "bollinger_signal": bollinger_signal,
            # --- Wskaźniki trendu ---
            "ema_20": round(ema_20, 2) if ema_20 is not None else None,
            "ema_50": round(ema_50, 2) if ema_50 is not None else None,
            "ema_200": round(ema_200, 2) if ema_200 is not None else None,
            "sma_100": round(sma_100, 2) if sma_100 is not None else None,
            "sma_200": round(sma_200, 2) if sma_200 is not None else None,
            "ema_stack": ema_stack,
            "adx": round(adx_data["adx"], 1) if adx_data["adx"] is not None else None,
            "plus_di": round(adx_data["plus_di"], 1) if adx_data["plus_di"] is not None else None,
            "minus_di": round(adx_data["minus_di"], 1) if adx_data["minus_di"] is not None else None,
            "atr": round(atr_value, 2) if atr_value is not None else None,
            "atr_pct": atr_pct,
            "donchian_signal": donchian_signal,
            "reg_slope_pct": reg_slope_pct,
            "supertrend_dir": supertrend_dir,
            "ichimoku_cloud": ichimoku_cloud,
        }
    except Exception as e:
        logger.warning(f"Blad technicals dla {ticker}: {e}")
        return _empty_technicals()


def generate_technical_signal(
    rsi: Optional[float],
    vs_sma20: Optional[float],
    vs_sma50: Optional[float],
    macd_trend: Optional[str] = None,
    bollinger_signal: Optional[str] = None,
) -> str:
    """Generate a compact technical status for the portfolio dashboard."""
    if macd_trend == "bullish_cross":
        return "MACD: pozytywne przeciecie"
    if macd_trend == "bearish_cross":
        return "MACD: negatywne przeciecie"
    if bollinger_signal == "below_lower_band":
        return "ponizej dolnej wstegi"
    if bollinger_signal == "above_upper_band":
        return "powyzej gornej wstegi"

    if rsi is not None:
        if rsi < 30:
            return "technicznie wyprzedane"
        if rsi > 70:
            return "technicznie wykupione"

    if vs_sma20 is not None and vs_sma50 is not None:
        if vs_sma20 > 0 and vs_sma50 > 0:
            return "trend pozytywny"
        if vs_sma20 < 0 and vs_sma50 < 0:
            return "trend negatywny"

    return "neutralny"


def trend_label(technicals: dict) -> str:
    """Zwięzła etykieta trendu do dziennej tabeli (układ średnich + ADX/DI)."""
    technicals = technicals or {}
    ema_stack = technicals.get("ema_stack")
    adx = technicals.get("adx")
    plus_di = technicals.get("plus_di")
    minus_di = technicals.get("minus_di")

    # Kierunek: priorytet układu średnich, w razie 'mixed' rozstrzyga DI
    if ema_stack == "strong_up":
        arrow = "↑"
    elif ema_stack == "strong_down":
        arrow = "↓"
    elif plus_di is not None and minus_di is not None:
        arrow = "↑" if plus_di > minus_di else "↓"
    else:
        arrow = "→"

    if adx is None:
        return arrow if arrow != "→" else "b/d"

    if adx < 20:
        strength = "brak"
        arrow = "→"
    elif adx < 25:
        strength = "słaby"
    elif adx < 40:
        strength = "silny"
    else:
        strength = "b. silny"

    return f"{arrow} {strength} (ADX {adx:.0f})"


def generate_alerts(ticker: str, details: dict, prev_snapshot: Optional[dict] = None) -> list[str]:
    """Generate daily alerts from technical, fundamental and event data."""
    alerts = []
    technicals = details.get("technicals") or {}

    rsi = technicals.get("rsi_14")
    if rsi is not None:
        if rsi > 70:
            alerts.append(f"technicznie wykupiona (RSI {rsi})")
        elif rsi < 30:
            alerts.append(f"technicznie wyprzedana (RSI {rsi})")

    sma50 = technicals.get("price_vs_sma50")
    if sma50 is not None and prev_snapshot is not None:
        prev_sma50 = prev_snapshot.get("price_vs_sma50")
        if prev_sma50 is not None:
            if prev_sma50 < 0 <= sma50:
                alerts.append("Wybicie ponad SMA50")
            elif prev_sma50 > 0 >= sma50:
                alerts.append("Spadek ponizej SMA50")

    macd_trend = technicals.get("macd_trend")
    macd_hist = technicals.get("macd_histogram")
    if macd_trend == "bullish_cross":
        alerts.append(f"MACD: pozytywne przeciecie (hist. {macd_hist})")
    elif macd_trend == "bearish_cross":
        alerts.append(f"MACD: negatywne przeciecie (hist. {macd_hist})")

    bollinger_signal = technicals.get("bollinger_signal")
    bollinger_pos = technicals.get("bollinger_position")
    if bollinger_signal == "above_upper_band":
        alerts.append(f"Kurs powyzej gornej wstegi Bollingera (poz. {bollinger_pos})")
    elif bollinger_signal == "below_lower_band":
        alerts.append(f"Kurs ponizej dolnej wstegi Bollingera (poz. {bollinger_pos})")

    vol_ratio = technicals.get("volume_ratio_10d")
    if vol_ratio is not None and vol_ratio >= 2.0:
        alerts.append(f"Wysoki wolumen ({vol_ratio:.1f}x srednia 10d)")

    quote = details.get("quote") or {}
    change_pct = quote.get("change_pct")
    if change_pct is not None:
        if change_pct >= 5.0:
            alerts.append(f"Ekstremalny wzrost ({change_pct:+.2f}%)")
        elif change_pct <= -5.0:
            alerts.append(f"Ekstremalny spadek ({change_pct:+.2f}%)")

    fundamentals = details.get("fundamentals") or {}
    short_pct = fundamentals.get("short_pct_float")
    if short_pct is not None:
        pct_val = short_pct if short_pct > 1.0 else short_pct * 100.0
        if pct_val >= 10.0:
            alerts.append(f"Wysoki short interest ({pct_val:.1f}%)")

    insider_signal = fundamentals.get("insider_signal")
    if insider_signal and insider_signal != "neutral":
        alerts.append(f"Insiderzy/smart money: {insider_signal}")

    earnings = details.get("earnings") or {}
    days_until = earnings.get("days_until")
    if days_until is not None and 0 <= days_until <= 7:
        alerts.append(f"Publikacja wynikow za {days_until} dni")

    return alerts
