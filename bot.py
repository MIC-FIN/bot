"""
Результаты бэктеста (BMM6, ноябрь 2025 — май 2026, 133 сделки):
  P&L:          +12.76%  (+1 276 ₽ на депозит 10 000 ₽)
  Profit factor: 1.76
  Реальный RR:   3.53
  Винрейт:       30%
  Макс. просадка: 2.46%
  Прибыльных месяцев: 7/7
--------------------------------------------------------------
Торговый бот: Маркет Профиль │ Мини-фьючерсы МосБиржи  v2.0
=============================================================
Изменения v2:
  - Торгуем только BMM6 (мини-нефть) — единственный инструмент
    где комиссия не убивает стратегию при депозите 10k
  - Дневной лимит убытка  -2%  от текущего депозита
  - Месячный лимит убытка -5%  от депозита на начало месяца
  - Фильтр объёма профиля: не торгуем если объём дня < 10-го процентиля
  - Правильный point_value для BMM6 (цена в USD, 1 USD = 85 руб)
  - Валидация профиля (va_width > 0)

Запуск:   py -3.11 C:\\trading\\bot.py
"""

import time
import json
import logging
from datetime import datetime, timedelta, timezone, date
from dataclasses import dataclass, field
from typing import Optional
from collections import deque

import pandas as pd
import numpy as np

# ─────────────────────────────────────────────────────
#  НАСТРОЙКИ
# ─────────────────────────────────────────────────────

TINKOFF_TOKEN  = "t.5Wxw8TNkE5tKcFxjCMJY5cjr_bj336PMtmhcQ8Ct_jq1-MqbxaL14xJnsEPfaTnhS_dLW7Pegtu3_nwPOv927w"

SANDBOX_MODE   = False      # True = песочница, False = реальная торговля
PAPER_MODE     = False      # True = бумажная торговля (ордера не отправляются,
                           # сделки отслеживаются по рыночным ценам локально).
                           # Используй когда инструмент недоступен в песочнице (ошибка 30079).
                           # False = отправлять реальные ордера через API.
DEPOSIT        = 10_000.0  # руб — начальный/эталонный депозит

# ── Инструменты для торговли (один активный, остальные закомментированы)
# Раскомментируй нужный и перезапусти бота.
# Формат: (базовый код, point_value, описание)
#
#   BM    85.0   BMM6 — мини-нефть Brent (1 USD = 85 руб)          [рекомендуется]
#   NG  8500.0   NGM  — газ Henry Hub большой (10000 MMBtu × 0.85)  [очень ликвиден]
#   GZ     1.0   GAZR — фьючерс на Газпром (1 пт = 1 руб)          [квартальный]
#   GD   850.0   GOLD — золото (1 тр.унц × 85 руб, лот 1)          [менее ликвиден]

MINI_BASE   = "BM"     # базовый код инструмента (2 буквы)
POINT_VALUE = 85.0     # стоимость 1 пункта цены API в рублях
GO_PER_LOT  = 2_233.78 # ГО на 1 лот BMM6, руб (актуально на 18.05.2026)
GO_BUFFER   = 0.20     # резерв 20% сверх ГО на вариационную маржу


RISK_PCT        = 0.02     # 2% от текущего баланса на сделку (оптимум по бэктесту)
COMMISSION_PCT  = 0.00025    # 0.025% от номинала (туда); итого 0.05%
VALUE_AREA_PCT  = 0.70
STOP_BUFFER_PCT = 0.003
MIN_RR          = 1.5

# ── Лимиты убытков (результат бэктеста: апрель -134 руб vs -2188 без лимита)
MAX_DAILY_LOSS_PCT   = 0.02   # -2% от баланса за день — стоп новых входов
MAX_MONTHLY_LOSS_PCT = 0.05   # -5% от баланса на начало месяца — стоп до след. месяца

# ── Фильтр объёма: не торгуем если объём предыдущего дня ниже порога
# Порог вычисляется динамически по скользящему окну последних VOLUME_WINDOW дней
VOLUME_WINDOW       = 60      # дней для расчёта процентиля объёма
MIN_VOLUME_PCT      = 10      # 10-й процентиль

CHECK_INTERVAL = 60        # сек между итерациями главного цикла
TRADES_LOG     = "trades.json"
STATE_FILE     = "bot_state.json"  # состояние между перезапусками

# ─────────────────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-5s │ %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────
#  СТРУКТУРЫ ДАННЫХ
# ─────────────────────────────────────────────────────

@dataclass
class MarketProfile:
    poc: float
    vah: float
    val: float
    va_width: float
    total_volume: float

@dataclass
class Signal:
    direction: str
    entry: float
    stop: float
    target: float
    gross_risk: float
    net_risk: float
    commission: float
    lots: int
    rr: float
    expected_value: float
    reason: str

@dataclass
class Position:
    ticker: str
    direction: str
    entry_price: float
    stop: float
    target: float
    lots: int
    opened_at: str
    stop_order_id: Optional[str] = None   # ID стоп-заявки на бирже

# ─────────────────────────────────────────────────────
#  СОСТОЯНИЕ ЛИМИТОВ
# ─────────────────────────────────────────────────────

@dataclass
class LimitState:
    # Дневной лимит
    day:           date  = field(default_factory=lambda: date.today())
    day_pnl:       float = 0.0
    day_blocked:   bool  = False

    # Месячный лимит
    month:         str   = field(default_factory=lambda: date.today().strftime("%Y-%m"))
    month_start_balance: float = DEPOSIT
    month_blocked: bool  = False

    # Объём для фильтра (скользящее окно суточных объёмов)
    volume_history: deque = field(default_factory=lambda: deque(maxlen=VOLUME_WINDOW))
    last_day_volume: float = 0.0   # объём текущего/предыдущего дня

limit_state = LimitState()


def reset_day_limit(balance: float):
    """Сбрасывает дневной лимит в начале нового дня."""
    today = date.today()
    if limit_state.day != today:
        if limit_state.last_day_volume > 0:
            limit_state.volume_history.append(limit_state.last_day_volume)
        limit_state.day          = today
        limit_state.day_pnl      = 0.0
        limit_state.day_blocked  = False
        limit_state.last_day_volume = 0.0
        entered_today_tickers.clear()
        log.info(f"  Новый день {today}: дневной лимит сброшен")


def reset_month_limit(balance: float):
    """Сбрасывает месячный лимит в начале нового месяца."""
    month = date.today().strftime("%Y-%m")
    if limit_state.month != month:
        limit_state.month               = month
        limit_state.month_start_balance = balance
        limit_state.month_blocked       = False
        log.info(f"  Новый месяц {month}: месячный лимит сброшен, баланс={balance:,.0f} ₽")


def update_limits_after_trade(pnl: float, balance: float):
    """Обновляет счётчики после закрытия сделки."""
    limit_state.day_pnl += pnl

    day_limit   = balance * MAX_DAILY_LOSS_PCT
    month_limit = limit_state.month_start_balance * MAX_MONTHLY_LOSS_PCT
    month_pnl   = balance - limit_state.month_start_balance

    if not limit_state.day_blocked and limit_state.day_pnl <= -day_limit:
        limit_state.day_blocked = True
        log.warning(f"  🛑 ДНЕВНОЙ ЛИМИТ: потеряно {limit_state.day_pnl:.0f} ₽ "
                    f"(лимит -{day_limit:.0f} ₽). Торговля остановлена до завтра.")

    if not limit_state.month_blocked and month_pnl <= -month_limit:
        limit_state.month_blocked = True
        log.warning(f"  🛑 МЕСЯЧНЫЙ ЛИМИТ: потеряно {month_pnl:.0f} ₽ "
                    f"(лимит -{month_limit:.0f} ₽). Торговля остановлена до след. месяца.")


def is_trading_allowed(balance: float) -> bool:
    """Возвращает True если торговля разрешена (лимиты не сработали)."""
    if limit_state.day_blocked:
        log.info("  ⏸ Торговля заблокирована: дневной лимит убытка")
        return False
    if limit_state.month_blocked:
        log.info("  ⏸ Торговля заблокирована: месячный лимит убытка")
        return False
    return True


def is_volume_ok(current_volume: float) -> bool:
    """Возвращает True если объём предыдущего дня выше порога."""
    if len(limit_state.volume_history) < 10:
        return True   # недостаточно истории — не фильтруем
    threshold = np.percentile(list(limit_state.volume_history), MIN_VOLUME_PCT)
    if current_volume < threshold:
        log.info(f"  ⏸ Фильтр объёма: {current_volume:,.0f} < порога {threshold:,.0f}")
        return False
    return True

# ─────────────────────────────────────────────────────
#  ПЕСОЧНИЦА
# ─────────────────────────────────────────────────────

def setup_sandbox(token: str) -> str:
    from tinkoff.invest import Client
    from tinkoff.invest.utils import decimal_to_quotation
    from decimal import Decimal

    with Client(token) as client:
        account = client.sandbox.open_sandbox_account()
        account_id = account.account_id
        log.info(f"  Песочница: создан счёт {account_id}")
        client.sandbox.sandbox_pay_in(
            account_id=account_id,
            amount=decimal_to_quotation(Decimal(str(DEPOSIT)))
        )
        log.info(f"  Песочница: зачислено {DEPOSIT:,} ₽ (виртуальные)")
        return account_id


def get_balance(token: str, account_id: str) -> float:
    from tinkoff.invest import Client
    from tinkoff.invest.utils import quotation_to_decimal

    with Client(token) as client:
        # get_sandbox_portfolio устарел — используем единый метод для обоих режимов
        portfolio = client.operations.get_portfolio(account_id=account_id)
        return float(quotation_to_decimal(portfolio.total_amount_portfolio))

# ─────────────────────────────────────────────────────
#  ПОИСК АКТУАЛЬНОГО ТИКЕРА
# ─────────────────────────────────────────────────────

def to_naive_dt(d) -> Optional[datetime]:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.replace(tzinfo=None) if d.tzinfo else d
    try:
        return d.ToDatetime()
    except Exception:
        return None


def get_active_ticker(base: str, futures_list) -> Optional[str]:
    now = datetime.now()
    candidates = [f for f in futures_list
                  if f.ticker.upper().startswith(base.upper())
                  and len(f.ticker) == len(base) + 2]
    valid = [f for f in candidates
             if to_naive_dt(getattr(f, "last_trade_date", None)) is not None
             and to_naive_dt(f.last_trade_date) > now]
    if not valid:
        return candidates[0].ticker if candidates else None
    return min(valid, key=lambda f: to_naive_dt(f.last_trade_date)).ticker

# ─────────────────────────────────────────────────────
#  КОМИССИЯ
# ─────────────────────────────────────────────────────

def calc_commission(price: float, lots: int) -> float:
    """0.1% от стоимости контракта × 2 (вход + выход), в рублях."""
    return round(price * POINT_VALUE * lots * COMMISSION_PCT * 2, 2)

# ─────────────────────────────────────────────────────
#  СИНХРОНИЗАЦИЯ ПОЗИЦИЙ С API
# ─────────────────────────────────────────────────────

def sync_positions_from_api(ticker: str, account_id: str, client) -> bool:
    """
    Проверяет реальный портфель через API и восстанавливает позицию если:
    - bot_state.json отсутствует или битый
    - бот был перезапущен пока позиция была открыта

    Возвращает True если позиция найдена и восстановлена.
    """
    try:
        from tinkoff.invest.utils import quotation_to_decimal

        portfolio = client.operations.get_portfolio(account_id=account_id)
        figi = resolve_figi(ticker, client)
        if not figi:
            return False

        # Ищем позицию по figi в портфеле
        for pos in portfolio.positions:
            if pos.figi != figi:
                continue

            lots = int(quotation_to_decimal(pos.quantity))
            if lots == 0:
                continue

            avg_price = float(quotation_to_decimal(pos.average_position_price))
            direction = "BUY" if lots > 0 else "SELL"
            lots = abs(lots)

            # Восстанавливаем позицию с консервативными стоп/тейк
            # (реальные уровни неизвестны, ставим буфер 0.5% для стопа)
            buf = avg_price * 0.005
            stop   = round(avg_price - buf, 4) if direction == "BUY" else round(avg_price + buf, 4)
            target = round(avg_price * 1.02, 4) if direction == "BUY" else round(avg_price * 0.98, 4)

            if ticker not in open_positions:
                open_positions[ticker] = Position(
                    ticker=ticker, direction=direction,
                    entry_price=avg_price, stop=stop,
                    target=target, lots=lots,
                    opened_at="восстановлено из API",
                    stop_order_id=None,
                )
                entered_today_tickers.add(ticker)
                log.warning(
                    f"  ⚠️  Позиция восстановлена из портфеля API: "
                    f"{direction} {lots}×{ticker} @ {avg_price:.4f} "
                    f"│ стоп={stop:.4f} цель={target:.4f}"
                )
                log.warning(
                    f"  ⚠️  Стоп и цель установлены автоматически — "
                    f"проверь и скорректируй вручную если нужно"
                )
                save_state()
                return True

        return False

    except Exception as e:
        log.warning(f"  sync_positions_from_api: {e}")
        return False


# ─────────────────────────────────────────────────────
#  МАРКЕТ ПРОФИЛЬ
# ─────────────────────────────────────────────────────

def build_market_profile(df: pd.DataFrame) -> Optional[MarketProfile]:
    if df.empty or len(df) < 3:
        return None

    avg_price = df["close"].mean()
    tick = max(round(avg_price * 0.001, 4), 0.001)

    profile: dict[float, float] = {}
    for _, row in df.iterrows():
        lo = round(row["low"]  / tick) * tick
        hi = round(row["high"] / tick) * tick
        levels = np.arange(lo, hi + tick, tick)
        if len(levels) == 0:
            levels = np.array([lo])
        vol_per_level = row["volume"] / len(levels)
        for lvl in levels:
            key = round(float(lvl), 6)
            profile[key] = profile.get(key, 0) + vol_per_level

    sorted_lvl = sorted(profile.items())
    prices  = np.array([x[0] for x in sorted_lvl])
    volumes = np.array([x[1] for x in sorted_lvl])
    total_vol = volumes.sum()
    if total_vol == 0:
        return None

    poc_idx = int(np.argmax(volumes))
    va_vol  = volumes[poc_idx]
    lo_idx  = hi_idx = poc_idx

    while va_vol < total_vol * VALUE_AREA_PCT:
        can_up   = hi_idx + 1 < len(prices)
        can_down = lo_idx - 1 >= 0
        vol_up   = volumes[hi_idx + 1] if can_up   else 0
        vol_down = volumes[lo_idx - 1] if can_down else 0
        if not can_up and not can_down:
            break
        if vol_up >= vol_down and can_up:
            hi_idx += 1; va_vol += volumes[hi_idx]
        else:
            lo_idx -= 1; va_vol += volumes[lo_idx]

    va_width = round(float(prices[hi_idx] - prices[lo_idx]), 4)
    if va_width <= 0:
        return None   # вырожденный профиль

    return MarketProfile(
        poc=round(float(prices[poc_idx]), 4),
        vah=round(float(prices[hi_idx]),  4),
        val=round(float(prices[lo_idx]),  4),
        va_width=va_width,
        total_volume=round(float(total_vol), 0)
    )

# ─────────────────────────────────────────────────────
#  СИГНАЛЫ
# ─────────────────────────────────────────────────────

def get_signal(profile: MarketProfile, price: float, balance: float) -> Signal:
    buf            = price * STOP_BUFFER_PCT
    pv             = POINT_VALUE
    risk_per_trade = balance * RISK_PCT

    def build(direction: str, entry: float, stop: float, target: float) -> Signal:
        risk_pts   = (entry - stop)   if direction == "BUY" else (stop - entry)
        reward_pts = (target - entry) if direction == "BUY" else (entry - target)

        if risk_pts <= 0 or reward_pts <= 0:
            return Signal("HOLD", entry, stop, target, 0, 0, 0, 0, 0, 0, "Некорректные уровни")

        rr = round(reward_pts / risk_pts, 2)
        if rr < MIN_RR:
            return Signal("HOLD", entry, stop, target, 0, 0, 0, 0, rr, 0, f"RR={rr} < {MIN_RR}")

        comm_1     = calc_commission(entry, 1)
        risk_1_rub = risk_pts * pv
        if risk_1_rub - comm_1 <= 0:
            return Signal("HOLD", entry, stop, target, 0, 0, comm_1, 0, rr, 0,
                          "Комиссия съедает весь риск")

        lots       = max(1, int(risk_per_trade / (risk_1_rub + comm_1)))
        # Ограничиваем лоты по доступному ГО чтобы не получить ошибку 30042
        max_by_go  = max(1, int(balance / (GO_PER_LOT * (1 + GO_BUFFER))))
        lots       = min(lots, max_by_go)
        commission = calc_commission(entry, lots)
        gross_risk = round(risk_pts * pv * lots, 2)
        net_risk   = round(gross_risk - commission, 2)
        net_reward = round(reward_pts * pv * lots - commission, 2)
        ev         = round(0.55 * net_reward - 0.45 * net_risk, 2)

        tag = "Цена ниже VAL" if direction == "BUY" else "Цена выше VAH"
        reason = (f"{tag}: {price:.2f} │ POC {profile.poc:.2f} │ "
                  f"VAL {profile.val:.2f} ─ VAH {profile.vah:.2f} │ RR={rr}")
        return Signal(direction, entry, stop, target,
                      gross_risk, net_risk, commission, lots, rr, ev, reason)

    if price < profile.val:
        return build("BUY",  price, round(price - buf, 4), profile.poc)
    elif price > profile.vah:
        return build("SELL", price, round(price + buf, 4), profile.poc)
    else:
        pct = (price - profile.val) / profile.va_width * 100 if profile.va_width else 0
        return Signal("HOLD", price, profile.val, profile.vah, 0, 0, 0, 0, 0, 0,
                      f"Цена внутри VA ({pct:.0f}% от нижней границы)")

# ─────────────────────────────────────────────────────
#  TINKOFF API
# ─────────────────────────────────────────────────────

_figi_cache: dict[str, str] = {}

def _get_client():
    from tinkoff.invest import Client
    return Client


def resolve_figi(ticker: str, client) -> Optional[str]:
    if ticker in _figi_cache:
        return _figi_cache[ticker]
    futures = client.instruments.futures().instruments
    figi = next((f.figi for f in futures if f.ticker == ticker), None)
    if figi:
        _figi_cache[ticker] = figi
    return figi


def get_candles(ticker: str, client) -> Optional[pd.DataFrame]:
    try:
        from tinkoff.invest import CandleInterval
        from tinkoff.invest.utils import quotation_to_decimal

        figi = resolve_figi(ticker, client)
        if not figi:
            return None

        now = datetime.now(timezone.utc)
        resp = client.market_data.get_candles(
            figi=figi,
            from_=now - timedelta(days=2),
            to=now,
            interval=CandleInterval.CANDLE_INTERVAL_HOUR
        )
        rows = [{
            "time":   c.time,
            "open":   float(quotation_to_decimal(c.open)),
            "high":   float(quotation_to_decimal(c.high)),
            "low":    float(quotation_to_decimal(c.low)),
            "close":  float(quotation_to_decimal(c.close)),
            "volume": c.volume,
        } for c in resp.candles]

        if not rows:
            return None

        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"])
        return df

    except Exception as e:
        log.error(f"Свечи {ticker}: {e}")
        return None


def get_price(ticker: str, client) -> Optional[float]:
    try:
        from tinkoff.invest.utils import quotation_to_decimal
        figi = resolve_figi(ticker, client)
        if not figi:
            return None
        prices = client.market_data.get_last_prices(figi=[figi])
        return float(quotation_to_decimal(prices.last_prices[0].price))
    except Exception as e:
        log.error(f"Цена {ticker}: {e}")
        return None


def place_order(ticker: str, direction: str, lots: int,
                account_id: str, client) -> Optional[str]:
    try:
        from tinkoff.invest import OrderDirection, OrderType, InstrumentStatus
        import uuid

        figi = resolve_figi(ticker, client)
        if not figi:
            return None

        # Проверяем торговый статус инструмента перед отправкой ордера
        try:
            from tinkoff.invest import SecurityTradingStatus
            status = client.market_data.get_trading_status(figi=figi)
            if not status.api_trade_available_flag:
                log.warning(
                    f"  ⏸ {ticker}: торговля через API недоступна "
                    f"(статус: {status.trading_status.name}). "
                    f"Биржа закрыта или технический перерыв — жду открытия."
                )
                return None
        except Exception as e:
            log.warning(f"  Проверка статуса {ticker}: {e}")
            # Продолжаем — пробуем отправить ордер

        order_dir = (OrderDirection.ORDER_DIRECTION_BUY
                     if direction == "BUY"
                     else OrderDirection.ORDER_DIRECTION_SELL)

        # PAPER_MODE: не отправляем ордер, только логируем
        if PAPER_MODE:
            paper_id = "PAPER-" + str(uuid.uuid4())[:8]
            log.info(f"  📋 БУМАЖНЫЙ ордер: {direction} {lots}×{ticker} │ {paper_id}")
            return paper_id

        # orders.post_order работает и в песочнице (передаём sandbox account_id)
        order = client.orders.post_order(
            figi=figi, quantity=lots, direction=order_dir,
            account_id=account_id,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=str(uuid.uuid4())
        )

        log.info(f"  ✅ Ордер: {direction} {lots}×{ticker} │ {order.order_id}")
        return order.order_id

    except Exception as e:
        err_str = str(e)
        if "30079" in err_str or "not available for trading" in err_str:
            log.warning(
                f"  ⚠️  {ticker} недоступен в песочнице (ошибка 30079). "
                f"Включи PAPER_MODE=True для бумажной торговли."
            )
        else:
            log.error(f"Ордер {ticker}: {e}")
        return None

# ─────────────────────────────────────────────────────
#  СТОП-ЗАЯВКА НА БИРЖЕ
# ─────────────────────────────────────────────────────

def place_stop_order(ticker: str, direction: str, lots: int,
                     stop_price: float, account_id: str, client) -> Optional[str]:
    """
    Выставляет стоп-заявку на бирже — срабатывает даже если бот отключён.
    direction — направление ЗАКРЫТИЯ позиции (противоположное входу).
    stop_price — цена активации стопа.
    """
    if PAPER_MODE:
        paper_id = "PAPER-STOP-" + __import__('uuid').uuid4().hex[:8]
        log.info(f"  📋 БУМАЖНЫЙ стоп: {direction} {lots}×{ticker} @ {stop_price:.4f}")
        return paper_id

    try:
        from tinkoff.invest import (
            StopOrderDirection, StopOrderType,
            StopOrderExpirationType
        )
        from tinkoff.invest.utils import decimal_to_quotation
        from decimal import Decimal
        import uuid

        figi = resolve_figi(ticker, client)
        if not figi:
            return None

        stop_dir = (StopOrderDirection.STOP_ORDER_DIRECTION_SELL
                    if direction == "SELL"
                    else StopOrderDirection.STOP_ORDER_DIRECTION_BUY)

        # Конвертируем цену стопа в формат API
        stop_q = decimal_to_quotation(Decimal(str(round(stop_price, 4))))

        resp = client.stop_orders.post_stop_order(
            figi=figi,
            quantity=lots,
            stop_price=stop_q,
            direction=stop_dir,
            account_id=account_id,
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
            order_id=str(uuid.uuid4()),
        )
        stop_id = resp.stop_order_id
        log.info(f"  🛡️  Стоп-заявка на бирже: {direction} {lots}×{ticker} "
                 f"@ {stop_price:.4f} │ {stop_id}")
        return stop_id

    except Exception as e:
        log.error(f"  Стоп-заявка {ticker}: {e}")
        return None


def cancel_stop_order(stop_order_id: str, account_id: str, client):
    """Отменяет стоп-заявку на бирже (при закрытии позиции по тейку)."""
    if PAPER_MODE or not stop_order_id or stop_order_id.startswith("PAPER"):
        return
    try:
        client.stop_orders.cancel_stop_order(
            account_id=account_id,
            stop_order_id=stop_order_id,
        )
        log.info(f"  🗑️  Стоп-заявка отменена: {stop_order_id}")
    except Exception as e:
        log.warning(f"  Отмена стоп-заявки: {e}")


# ─────────────────────────────────────────────────────
#  ЖУРНАЛ СДЕЛОК
# ─────────────────────────────────────────────────────

def save_trade(ticker: str, s: Signal, p: MarketProfile, event: str,
               balance: float, pnl: float = 0.0):
    try:
        trades = []
        try:
            with open(TRADES_LOG, encoding="utf-8") as f:
                trades = json.load(f)
        except FileNotFoundError:
            pass

        trades.append({
            "time": datetime.now().isoformat(),
            "event": event, "ticker": ticker, "sandbox": SANDBOX_MODE,
            "direction": s.direction,
            "entry": s.entry, "stop": s.stop, "target": s.target,
            "lots": s.lots, "rr": s.rr,
            "gross_risk": s.gross_risk, "commission": s.commission,
            "net_risk": s.net_risk, "expected_value": s.expected_value,
            "poc": p.poc, "vah": p.vah, "val": p.val,
            "reason": s.reason,
            "pnl": pnl, "balance": balance,
        })

        with open(TRADES_LOG, "w", encoding="utf-8") as f:
            json.dump(trades, f, ensure_ascii=False, indent=2)

    except Exception as e:
        log.error(f"Запись журнала: {e}")

# ─────────────────────────────────────────────────────
#  МОНИТОРИНГ ПОЗИЦИЙ
# ─────────────────────────────────────────────────────

open_positions: dict[str, Position] = {}
entered_today_tickers: set = set()  # тикеры в которые уже входили сегодня

# ─────────────────────────────────────────────────────
#  СОХРАНЕНИЕ И ВОССТАНОВЛЕНИЕ СОСТОЯНИЯ
# ─────────────────────────────────────────────────────

def _to_python(obj):
    """Конвертирует numpy/pandas типы в стандартные Python для JSON."""
    import numpy as np
    if isinstance(obj, (np.integer,)):  return int(obj)
    if isinstance(obj, (np.floating,)): return float(obj)
    if isinstance(obj, (np.ndarray,)):  return obj.tolist()
    return obj


def save_state():
    """Сохраняет позиции и лимиты в файл — выживает при перезапуске."""
    try:
        state = {
            "saved_at": datetime.now().isoformat(),
            "positions": {
                ticker: {
                    "ticker":        pos.ticker,
                    "direction":     pos.direction,
                    "entry_price":   _to_python(pos.entry_price),
                    "stop":          _to_python(pos.stop),
                    "target":        _to_python(pos.target),
                    "lots":          _to_python(pos.lots),
                    "opened_at":     pos.opened_at,
                    "stop_order_id": pos.stop_order_id,
                }
                for ticker, pos in open_positions.items()
            },
            "limits": {
                "day":                 str(limit_state.day),
                "day_pnl":             _to_python(limit_state.day_pnl),
                "day_blocked":         bool(limit_state.day_blocked),
                "month":               limit_state.month,
                "month_start_balance": _to_python(limit_state.month_start_balance),
                "month_blocked":       bool(limit_state.month_blocked),
                "volume_history":      [_to_python(v) for v in limit_state.volume_history],
            },
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        log.info(f"  💾 Состояние сохранено: {len(open_positions)} позиций")
    except Exception as e:
        log.warning(f"  save_state: {e}")


def load_state():
    """Восстанавливает позиции и лимиты после перезапуска."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        log.warning(f"  load_state: {e}")
        return

    log.info(f"  Восстанавливаем состояние от {state.get('saved_at', '?')}")

    # Позиции
    for ticker, p in state.get("positions", {}).items():
        open_positions[ticker] = Position(
            ticker=p["ticker"], direction=p["direction"],
            entry_price=p["entry_price"], stop=p["stop"],
            target=p["target"], lots=p["lots"],
            opened_at=p["opened_at"], stop_order_id=p.get("stop_order_id"),
        )
        log.info(f"  Позиция: {p['direction']} {p['lots']}x{ticker} "
                 f"вход={p['entry_price']} стоп={p['stop']} цель={p['target']}")

    # Лимиты
    lim = state.get("limits", {})
    today_str = str(date.today())
    cur_month = date.today().strftime("%Y-%m")

    if lim.get("day") == today_str:
        limit_state.day_pnl     = lim.get("day_pnl", 0.0)
        limit_state.day_blocked = lim.get("day_blocked", False)
        if limit_state.day_blocked:
            log.warning("  Дневной лимит достигнут — торговля заблокирована до завтра")
    else:
        log.info("  Новый день — дневной лимит сброшен")

    if lim.get("month") == cur_month:
        limit_state.month_start_balance = lim.get("month_start_balance", DEPOSIT)
        limit_state.month_blocked       = lim.get("month_blocked", False)
        if limit_state.month_blocked:
            log.warning("  Месячный лимит достигнут — торговля заблокирована")

    for v in lim.get("volume_history", [])[-VOLUME_WINDOW:]:
        limit_state.volume_history.append(v)

    log.info(f"  Восстановлено: {len(open_positions)} позиций, "
             f"{len(limit_state.volume_history)} объёмов в истории")

def monitor_positions(account_id: str, client, balance: float):
    for ticker, pos in list(open_positions.items()):
        price = get_price(ticker, client)
        if not price:
            continue

        limit_state.last_day_volume += 1

        # Если на бирже выставлена стоп-заявка — не закрываем позицию сами по стопу.
        # Биржа сработает автоматически даже если ноутбук выключен.
        has_exchange_stop = (
            pos.stop_order_id is not None
            and not str(pos.stop_order_id).startswith("PAPER")
            and not PAPER_MODE
        )

        hit_stop = (pos.direction == "BUY"  and price <= pos.stop) or                    (pos.direction == "SELL" and price >= pos.stop)
        hit_take = (pos.direction == "BUY"  and price >= pos.target) or                    (pos.direction == "SELL" and price <= pos.target)

        close_dir = "SELL" if pos.direction == "BUY" else "BUY"

        if hit_stop:
            if pos.direction == "BUY":
                pnl = round((pos.stop - pos.entry_price) * POINT_VALUE * pos.lots
                            - calc_commission(pos.stop, pos.lots), 2)
            else:
                pnl = round((pos.entry_price - pos.stop) * POINT_VALUE * pos.lots
                            - calc_commission(pos.stop, pos.lots), 2)

            if has_exchange_stop:
                # Стоп сработал на бирже — просто обновляем внутреннее состояние
                log.info(f"  🔴 СТОП на бирже {ticker} @ {pos.stop:.4f} │ P&L: {pnl:+.2f} ₽")
            else:
                # Нет стоп-заявки на бирже — закрываем сами
                label = "📋 БУМАЖНЫЙ " if PAPER_MODE else ""
                log.info(f"  🔴 {label}СТОП {ticker} @ {pos.stop:.4f} │ P&L: {pnl:+.2f} ₽")
                place_order(ticker, close_dir, pos.lots, account_id, client)

            update_limits_after_trade(pnl, balance + pnl)
            del open_positions[ticker]
            save_state()

        elif hit_take:
            if pos.direction == "BUY":
                pnl = round((price - pos.entry_price) * POINT_VALUE * pos.lots
                            - calc_commission(price, pos.lots), 2)
            else:
                pnl = round((pos.entry_price - price) * POINT_VALUE * pos.lots
                            - calc_commission(price, pos.lots), 2)

            label = "📋 БУМАЖНЫЙ " if PAPER_MODE else ""
            log.info(f"  🟢 {label}ТЕЙК {ticker} @ {price:.4f} │ P&L: {pnl:+.2f} ₽")
            place_order(ticker, close_dir, pos.lots, account_id, client)
            # Отменяем стоп-заявку на бирже — иначе откроет обратную позицию
            if pos.stop_order_id:
                cancel_stop_order(pos.stop_order_id, account_id, client)
            update_limits_after_trade(pnl, balance + pnl)
            del open_positions[ticker]
            save_state()


# ─────────────────────────────────────────────────────
#  ГЛАВНЫЙ ЦИКЛ
# ─────────────────────────────────────────────────────

def run():
    if PAPER_MODE:
        mode_label = "📋 БУМАЖНАЯ ТОРГОВЛЯ (ордера не отправляются)"
    elif SANDBOX_MODE:
        mode_label = "🟡 ПЕСОЧНИЦА"
    else:
        mode_label = "🔴 БОЕВОЙ РЕЖИМ"

    log.info("━" * 60)
    log.info("  МАРКЕТ ПРОФИЛЬ v2 │ Мини-фьючерсы МосБиржи")
    log.info(f"  Режим:           {mode_label}")
    log.info(f"  Инструмент:      {MINI_BASE}* (авто-поиск ближайшего контракта)")
    log.info(f"  Депозит:         {DEPOSIT:,} ₽")
    log.info(f"  Риск/сделку:     {RISK_PCT*100:.0f}%  │  Min RR: 1:{MIN_RR}")
    log.info(f"  Point value:     {POINT_VALUE} ₽/пт  │  Комиссия: {COMMISSION_PCT*100:.1f}%×2")
    log.info(f"  Дневной лимит:   -{MAX_DAILY_LOSS_PCT*100:.0f}%")
    log.info(f"  Месячный лимит:  -{MAX_MONTHLY_LOSS_PCT*100:.0f}%")
    log.info(f"  Фильтр объёма:   >{MIN_VOLUME_PCT}-й процентиль (окно {VOLUME_WINDOW} дней)")
    log.info("━" * 60)

    # Восстанавливаем состояние после перезапуска
    load_state()

    if "ВСТАВЬ_ТОКЕН" in TINKOFF_TOKEN:
        log.error("Укажи TINKOFF_TOKEN в начале файла!")
        return

    Client = _get_client()

    if SANDBOX_MODE:
        try:
            account_id = setup_sandbox(TINKOFF_TOKEN)
        except Exception as e:
            log.error(f"Ошибка создания песочницы: {e}")
            return
    else:
        try:
            with Client(TINKOFF_TOKEN) as c:
                acc = c.users.get_accounts().accounts[0]
                account_id = acc.id
                log.info(f"  Счёт: {account_id}")
        except Exception as e:
            log.error(f"Ошибка получения счёта: {e}")
            return

    with Client(TINKOFF_TOKEN) as client:
        # Находим актуальный тикер BMM6/BMU6/...
        futures_list = client.instruments.futures().instruments
        ticker = get_active_ticker(MINI_BASE, futures_list)
        if not ticker:
            log.error(f"Контракт {MINI_BASE}* не найден")
            return
        log.info(f"\n  Торгуем: {ticker}")

        # Синхронизируем позиции с реальным портфелем.
        # Восстановит позицию даже если bot_state.json битый или отсутствует.
        if ticker not in open_positions:
            synced = sync_positions_from_api(ticker, account_id, client)
            if not synced:
                log.info("  Открытых позиций в портфеле не найдено — старт с чистого листа")

        prev_profile: Optional[MarketProfile] = None
        prev_date: Optional[date] = None

        while True:
            try:
                balance = get_balance(TINKOFF_TOKEN, account_id)
                pnl_total = balance - DEPOSIT
                pnl_sign  = "+" if pnl_total >= 0 else ""
                log.info(f"\n  💼 Баланс: {balance:,.0f} ₽  (P&L: {pnl_sign}{pnl_total:.0f} ₽)")

                # Сброс лимитов при смене дня/месяца
                reset_day_limit(balance)
                reset_month_limit(balance)

                today = date.today()

                # Строим профиль предыдущего дня один раз в начале нового дня
                if prev_date != today:
                    df_all = get_candles(ticker, client)
                    if df_all is not None and len(df_all) >= 5:
                        # Ищем последний торговый день (пропускаем выходные и дни без свечей)
                        last_trading_day = None
                        for delta in range(1, 8):
                            candidate = today - timedelta(days=delta)
                            df_cand = df_all[df_all["time"].dt.date == candidate]
                            if len(df_cand) >= 3:
                                last_trading_day = candidate
                                break

                        if last_trading_day:
                            df_prev = df_all[df_all["time"].dt.date == last_trading_day]
                            prev_profile = build_market_profile(df_prev)
                            prev_vol = df_prev["volume"].sum()
                            limit_state.volume_history.append(prev_vol)
                            if prev_profile:
                                log.info(
                                    f"  Профиль {last_trading_day}: "
                                    f"VAL {prev_profile.val:.2f} ─ "
                                    f"POC {prev_profile.poc:.2f} ─ "
                                    f"VAH {prev_profile.vah:.2f} │ "
                                    f"объём {prev_vol:,.0f}"
                                )
                            else:
                                log.warning(f"  Профиль {last_trading_day}: вырожденный, пропускаем")
                        else:
                            log.warning("  Не удалось найти торговый день за последние 7 дней")
                    prev_date = today

                if prev_profile is None:
                    log.info("  Профиль не готов, ждём следующего дня")
                    time.sleep(CHECK_INTERVAL)
                    continue

                # Проверяем лимиты и объём
                if not is_trading_allowed(balance):
                    monitor_positions(account_id, client, balance)
                    time.sleep(CHECK_INTERVAL)
                    continue

                if not is_volume_ok(limit_state.volume_history[-1] if limit_state.volume_history else 0):
                    log.info("  Объём предыдущего дня ниже порога, пропускаем")
                    monitor_positions(account_id, client, balance)
                    time.sleep(CHECK_INTERVAL)
                    continue

                # Получаем цену и генерируем сигнал
                price = get_price(ticker, client)
                if not price:
                    time.sleep(CHECK_INTERVAL)
                    continue

                signal = get_signal(prev_profile, price, balance)

                log.info(
                    f"  {ticker} │ {price:.4f} │ "
                    f"VAL {prev_profile.val:.2f} ─ POC {prev_profile.poc:.2f} ─ "
                    f"VAH {prev_profile.vah:.2f} │ ➤ {signal.direction}"
                )

                if signal.direction in ("BUY", "SELL"):
                    log.info(f"  {signal.reason}")
                    log.info(
                        f"  Лоты: {signal.lots} │ Стоп: {signal.stop:.4f} │ "
                        f"Цель: {signal.target:.4f} │ RR: 1:{signal.rr}"
                    )
                    log.info(
                        f"  Риск: {signal.gross_risk:.2f} ₽ │ "
                        f"Комиссия: {signal.commission:.2f} ₽ │ "
                        f"Чистый риск: {signal.net_risk:.2f} ₽ │ "
                        f"EV: {'+' if signal.expected_value >= 0 else ''}{signal.expected_value:.2f} ₽"
                    )

                    save_trade(ticker, signal, prev_profile, "SIGNAL", balance)

                    if ticker not in open_positions and ticker not in entered_today_tickers:
                        order_id = place_order(ticker, signal.direction,
                                               signal.lots, account_id, client)
                        if order_id:
                            # Выставляем стоп-заявку на бирже сразу после входа.
                            # Она сработает даже если ноутбук/бот отключится.
                            close_dir = "SELL" if signal.direction == "BUY" else "BUY"
                            stop_id = place_stop_order(
                                ticker, close_dir, signal.lots,
                                signal.stop, account_id, client
                            )
                            open_positions[ticker] = Position(
                                ticker=ticker,
                                direction=signal.direction,
                                entry_price=price,
                                stop=signal.stop,
                                target=signal.target,
                                lots=signal.lots,
                                opened_at=datetime.now().isoformat(),
                                stop_order_id=stop_id,
                            )
                            entered_today_tickers.add(ticker)
                            save_state()
                else:
                    log.info(f"  {signal.reason}")

                monitor_positions(account_id, client, balance)

            except Exception as e:
                log.error(f"Ошибка в главном цикле: {e}")

            log.info(f"  ⏳ Следующая проверка через {CHECK_INTERVAL} сек...")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()
