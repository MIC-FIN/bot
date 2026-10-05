"""
Бэктест торговых стратегий на основе Market Profile.

Данные: MoexInterim (MOEX ISS) — бесплатная история 5-минутных свечей фьючерсов
серии BM (мини-фьючерсы на индекс МосБиржи).

Задача: найти стратегию с положительным матожиданием и достаточной частотой сделок.
"""

import io
import sys
import time
import requests
import numpy as np
import pandas as pd

ISS = "https://iss.moex.com/iss/engines/valid/markets/derivatives/securities/{figi}/candles.json"


def fetch_5min_history(figi: str, chunk_days: int = 365, max_back: int = 1200) -> pd.DataFrame:
    """Собирает историю 5-минутных свечей по figi через MOEX ISS (start/trade_date пагинация)."""
    all_rows = []
    end = pd.Timestamp.now(tz="Europe/Moscow").normalize()
    start = end - pd.Timedelta(days=chunk_days)
    guard = 0
    while start.year > end.year - max_back // 365 - 1 and guard < 40:
        guard += 1
        params = {
            "from": start.strftime("%Y-%m-%d"),
            "till": end.strftime("%Y-%m-%d"),
            "interval": 5,
        }
        try:
            r = requests.get(ISS.format(figi=figi), params=params, timeout=30)
            data = r.json()["candles"]["data"]
        except Exception as e:
            print(f"  fetch err {start.date()}: {e}")
            data = []
        if data:
            all_rows.extend(data)
        end = start - pd.Timedelta(days=1)
        start = end - pd.Timedelta(days=chunk_days)
        if end.year < 2018:
            break
    if not all_rows:
        return pd.DataFrame()
    cols = ["trade_date", "begin_time", "end_time", "value", "volume",
            "open", "close", "high", "low", "wapr", "trades_count"]
    df = pd.DataFrame(all_rows, columns=cols[: len(all_rows[0])])
    df["dt"] = pd.to_datetime(df["begin_time"], utc=True).dt.tz_convert("Europe/Moscow")
    for c in ("open", "close", "high", "low", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
    return df


def find_figs():
    """Ищем все контракты серии BM (мини-фьючерс на IMOEX) за последние годы."""
    url = "https://iss.moex.com/iss/engines/valid/markets/derivatives/securities.json"
    out = []
    page = 0
    while True:
        r = requests.get(url, params={"where.secid": "BM", "page": page,
                                      "per_page": 200, "iss.only": "securities"}, timeout=30)
        js = r.json()
        rows = js["securities"]["data"]
        cols = js["securities"]["columns"]
        if not rows:
            break
        for row in rows:
            d = dict(zip(cols, row))
            secid = d.get("secid", "")
            if secid.startswith("BM") and len(secid) == 4 and d.get("is_derivs"):
                out.append((secid, d.get("figi", "")))
        page += 1
        if page > 10:
            break
    return sorted(set(out))


# ────────────────────────────────────────────────
#  MARKET PROFILE
# ────────────────────────────────────────────────

def session_day_profile(day_df: pd.DataFrame, tick: float):
    """Профиль одного торгового дня из 5-минток: POC, VAH, VAL, объём."""
    profile = {}
    for _, r in day_df.iterrows():
        lo = round(r["low"] / tick) * tick
        hi = round(r["high"] / tick) * tick
        levels = np.arange(lo, hi + tick / 2, tick)
        if len(levels) == 0:
            levels = np.array([lo])
        v = r["volume"] / len(levels)
        for lvl in levels:
            k = round(float(lvl), 4)
            profile[k] = profile.get(k, 0.0) + v
    if not profile:
        return None
    items = sorted(profile.items())
    prices = np.array([x[0] for x in items])
    vols = np.array([x[1] for x in items])
    tot = vols.sum()
    if tot <= 0:
        return None
    poc_idx = int(np.argmax(vols))
    va = vols[poc_idx]
    lo_i = hi_i = poc_idx
    while va < tot * 0.70:
        up_ok = hi_i + 1 < len(prices)
        dn_ok = lo_i - 1 >= 0
        if not up_ok and not dn_ok:
            break
        up_v = vols[hi_i + 1] if up_ok else 0
        dn_v = vols[lo_i - 1] if dn_ok else 0
        if up_ok and (not dn_ok or up_v >= dn_v):
            hi_i += 1
        else:
            lo_i -= 1
        va = vols[lo_i:hi_i + 1].sum()
    return {"poc": prices[poc_idx], "vah": prices[hi_i], "val": prices[lo_i],
            "vol": tot}


def build_daily_profiles(df: pd.DataFrame, tick: float) -> pd.DataFrame:
    df = df.copy()
    df["date"] = df["dt"].dt.date
    recs = []
    for d, g in df.groupby("date"):
        p = session_day_profile(g, tick)
        if p:
            p["date"] = d
            p["open"] = g["open"].iloc[0]
            p["close"] = g["close"].iloc[-1]
            p["high"] = g["high"].max()
            p["low"] = g["low"].min()
            recs.append(p)
    out = pd.DataFrame(recs).set_index("date").sort_index()
    return out


# ────────────────────────────────────────────────
#  СТРАТЕГИИ (все генерируют сигналы на баре i, вход по close[i])
#  Каждая возвращает список сделок: dict(entry_date, side, entry, sl, tp, exit_price, pnl_pts)
# ────────────────────────────────────────────────

def run_trades_intrabar(df5: pd.DataFrame, sigs: dict, tick: float,
                        sl_atr_mult=1.5, rr=2.0, max_hold_bars=None):
    """
    sigs: date -> +1 (лонг) / -1 (шорт), сигнал формируется ПОСЛЕДНИМ баром дня D,
    позиция открывается первым баром дня D+1. Внутридневные SL/TP по хай/лоу.
    Если ни SL, ни TP не тронуты до конца дня — выход по закрытию следующего дня? 
    Нет: дневные уровни => держим максимум max_hold_bars баров, иначе выход по close последнего бара дня.
    """
    dates = sorted(sigs.keys())
    trades = []
    arr = df5.reset_index(drop=True)
    arr["date"] = arr["dt"].dt.date
    by_date = {d: g.reset_index(drop=True) for d, g in arr.groupby("date")}
    dl = list(by_date.keys())
    for d in dates:
        side = sigs[d]
        try:
            di = dl.index(d)
        except ValueError:
            continue
        if di + 1 >= len(dl):
            continue
        nd = dl[di + 1]
        prev = by_date[d]
        g = by_date[nd]
        ref_close = prev["close"].iloc[-1]
        atr = (prev["high"] - prev["low"]).mean()
        sl_dist = max(sl_atr_mult * atr, 6 * tick)
        entry = g["open"].iloc[0]
        if side == 1:
            sl, tp = entry - sl_dist, entry + rr * sl_dist
        else:
            sl, tp = entry + sl_dist, entry - rr * sl_dist
        exit_price, reason = None, "EOD"
        n = len(g) if max_hold_bars is None else min(len(g), max_hold_bars)
        for j in range(n):
            r = g.iloc[j]
            if side == 1:
                if r["low"] <= sl:
                    exit_price, reason = sl, "SL"; break
                if r["high"] >= tp:
                    exit_price, reason = tp, "TP"; break
            else:
                if r["high"] >= sl:
                    exit_price, reason = sl, "SL"; break
                if r["low"] <= tp:
                    exit_price, reason = tp, "TP"; break
        if exit_price is None:
            exit_price = g["close"].iloc[n - 1]
        pnl = (exit_price - entry) * side
        trades.append({"date": nd, "side": side, "entry": entry, "pnl": pnl,
                       "reason": reason})
    return trades


def strat_d1_reversion(prof: pd.DataFrame):
    """D-1 реверсия: закрытие выше VAH -> шорт; ниже VAL -> лонг (следующий день)."""
    sigs = {}
    for d, row in prof.iterrows():
        if row["close"] > row["vah"]:
            sigs[d] = -1
        elif row["close"] < row["val"]:
            sigs[d] = +1
    return sigs


def strat_d1_breakout(prof: pd.DataFrame):
    """Пробой VA: закрытие выше VAH -> лонг; ниже VAL -> шорт."""
    sigs = {}
    for d, row in prof.iterrows():
        if row["close"] > row["vah"]:
            sigs[d] = +1
        elif row["close"] < row["val"]:
            sigs[d] = -1
    return sigs


def strat_poc_magnet(prof: pd.DataFrame):
    """Магнит POC: закрытие далеко за пределами VA (>0.5 ширины VA) -> возврат к POC."""
    sigs = {}
    for d, row in prof.iterrows():
        w = max(row["vah"] - row["val"], 1e-9)
        if row["close"] > row["vah"] + 0.5 * w:
            sigs[d] = -1
        elif row["close"] < row["val"] - 0.5 * w:
            sigs[d] = +1
    return sigs


def strat_deviation(prof: pd.DataFrame):
    """Классическая девиация: закрытие ВЫШЕ VAH -> лонг (рынок принял новые цены),
       но только если предыдущий день тоже закрылся выше VAH (два подтверждения)."""
    sigs = {}
    dl = list(prof.index)
    for i in range(1, len(dl)):
        d, p = dl[i], dl[i - 1]
        r, rp = prof.loc[d], prof.loc[p]
        if r["close"] > r["vah"] and rp["close"] > rp["vah"]:
            sigs[d] = +1
        elif r["close"] < r["val"] and rp["close"] < rp["val"]:
            sigs[d] = -1
    return sigs


def evaluate(trades, point_value=1.0, label=""):
    if not trades:
        print(f"{label:38s} сделок: 0")
        return None
    pnls = np.array([t["pnl"] for t in trades]) * point_value
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    ev = pnls.mean()
    total = pnls.sum()
    wr = len(wins) / len(pnls) * 100
    eq = np.cumsum(pnls)
    dd = (np.maximum.accumulate(eq) - eq).max()
    print(f"{label:38s} n={len(pnls):4d} | EV/сделка={ev:+8.1f}₽ | WR={wr:5.1f}% | "
          f"PF={pf:5.2f} | Σ={total:+10.0f}₽ | MaxDD={dd:8.0f}₽")
    return {"n": len(pnls), "ev": ev, "wr": wr, "pf": pf, "total": total, "dd": dd}


def main():
    contracts = find_figs()
    print("Найдено контрактов BM:", [c[0] for c in contracts])
    # Берём самые свежие (последние ~8 тикеров)
    use = contracts[-8:]
    frames = []
    for secid, figi in use:
        if not figi:
            continue
        print(f"Загрузка {secid} ({figi})...")
        df = fetch_5min_history(figi)
        if len(df) > 500:
            df["secid"] = secid
            frames.append(df)
            print(f"  {len(df)} баров, {df['dt'].min()} .. {df['dt'].max()}")
    if not frames:
        print("НЕТ ДАННЫХ"); sys.exit(1)
    big = pd.concat(frames, ignore_index=True)
    big.to_pickle("bm_5min.pkl")
    print(f"Итого баров: {len(big)}")

    point_value = 1.0  # в бэктесте считаем в рублях за пункт условно; для BM реально ~10-15₽ later
    results = {}
    for secid, grp in big.groupby("secid"):
        grp = grp.sort_values("dt").reset_index(drop=True)
        tick = 1.0  # шаг цены BM ≈ 10 руб → для профиля грубо 10; используем медиану минимального шага
        # оценка шага: НОД округлений
        tick = 10.0
        prof = build_daily_profiles(grp, tick=tick)
        if len(prof) < 60:
            continue
        print(f"\n=== {secid}: {len(prof)} торговых дней ===")
        for name, fn in [("D1 реверсия (VA extreme)", strat_d1_reversion),
                         ("D1 пробой VA", strat_break := strat_d1_breakout),
                         ("POC магнит", strat_poc_magnet),
                         ("Девиация 2 подтверждения", strat_deviation)]:
            sigs = fn(prof)
            tr = run_trades_intrabar(grp, sigs, tick=tick, sl_atr_mult=1.5, rr=2.0)
            r = evaluate(tr, point_value, label=f"{secid} {name}")
            results.setdefault(name, []).append(r)
    print("\n=== СВОДКА ПО ВСЕМ КОНТРАКТАМ ===")
    for name, rs in results.items():
        rs = [r for r in rs if r]
        if not rs:
            continue
        n = sum(r["n"] for r in rs)
        tot = sum(r["total"] for r in rs)
        ev = tot / n if n else 0
        print(f"{name:38s} n={n:5d} | Σ={tot:+10.0f}усл.₽ | EV={ev:+7.1f}")


if __name__ == "__main__":
    main()
