"""
Результаты бэктеста (BMM6, ноябрь 2025 — май 2026, 133 сделки):
  P&L:          +12.76%  (+1 276 ₽ на депозит 10 000 ₽)
  Profit factor: 1.76
  Реальный RR:   3.53
  Винрейт:       30%
  Макс. просадка: 2.46%
  Прибыльных месяцев: 7/7
--------------------------------------------------------------
Торговый бот: Маркет Профиль │ Мини-фьючерсы МосБиржи  v3.0
=============================================================
Изменения v3 (T-Bank Invest API):
  - Полная миграция на новую библиотеку tinkoff-invest 1.0.5
  - ProductionSession / SandboxSession вместо Client
  - Обновлённые модели данных и методы API
  - Сохранена вся логика стратегии v2.0

Запуск:   python bot.py
"""

import time
import json
import logging
from datetime import datetime, timedelta, timezone, date
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from collections import deque

import pandas as pd
import numpy as np

# ─────────────────────────────────────────────────────
#  НАСТРОЙКИ
# ─────────────────────────────────────────────────────

TINKOFF_TOKEN  = "t.5Wxw8TNkE5tKcFxjCMJY5cjr_bj336PMtmhcQ8Ct_jq1-MqbxaL14xJnsEPfaTnhS_dLW7Pegtu3_nwPOv927w"

SANDBOX_MODE   = False      # True = песочница, False = реальная торговля
PAPER_MODE     = False      # True = бумажная торговля (ордера не отправляются)
DEPOSIT        = 10_000.0  # руб — начальный/эталонный депозит

# ── Инструменты для торговли
MINI_BASE   = "BM"     # базовый код инструмента (2 буквы)
POINT_VALUE = 85.0     # стоимость 1 пункта цены API в рублях
GO_PER_LOT  = 2_233.78 # ГО на 1 лот BMM6, руб
GO_BUFFER   = 0.20     # резерв 20% сверх ГО на вариационную маржу

RISK_PCT        = 0.02     # 2% от текущего баланса на сделку
COMMISSION_PCT  = 0.00025  # 0.025% от номинала (туда); итого 0.05%
VALUE_AREA_PCT  = 0.70
STOP_BUFFER_PCT = 0.003
MIN_RR          = 1.5

# ── Лимиты убытков
MAX_DAILY_LOSS_PCT   = 0.02   # -2% от баланса за день
MAX_MONTHLY_LOSS_PCT = 0.05   # -5% от баланса на начало месяца

# ── Фильтр объёма
VOLUME_WINDOW       = 60      # дней для расчёта процентиля объёма
MIN_VOLUME_PCT      = 10      # 10-й процентиль

CHECK_INTERVAL = 60        # сек между итерациями главного цикла
TRADES_LOG     = "trades.json"
STATE_FILE     = "bot_state.json"

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
    stop_order_id: Optional[str] = None
    order_id: Optional[str] = None  # ID основного ордера

# ─────────────────────────────────────────────────────
#  СОСТОЯНИЕ ЛИМИТОВ
# ─────────────────────────────────────────────────────

@dataclass
class LimitState:
    day:           date  = field(default_factory=lambda: date.today())
    day_pnl:       float = 0.0
    day_blocked:   bool  = False
    month:         str   = field(default_factory=lambda: date.today().strftime("%Y-%m"))
    month_start_balance: float = DEPOSIT
    month_blocked: bool  = False
    volume_history: deque = field(default_factory=lambda: deque(maxlen=VOLUME_WINDOW))
    last_day_volume: float = 0.0

limit_state = LimitState()
open_positions: Dict[str, Position] = {}
entered_today_tickers: set = set()


def reset_day_limit(balance: float):
    """Сбрасывает дневной лимит в начале нового дня."""
    today = date.today()
    if limit_state.day != today:
        if limit_state.last_day_volume > 0:
            limit_state.volume_history.append(limit_state.last_day_volume)
        limit_state.day = today
        limit_state.day_pnl = 0.0
        limit_state.day_blocked = False
        limit_state.last_day_volume = 0.0
        entered_today_tickers.clear()
        log.info(f"  Новый день {today}: дневной лимит сброшен")


def reset_month_limit(balance: float):
    """Сбрасывает месячный лимит в начале нового месяца."""
    month = date.today().strftime("%Y-%m")
    if limit_state.month != month:
        limit_state.month = month
        limit_state.month_start_balance = balance
        limit_state.month_blocked = False
        log.info(f"  Новый месяц {month}: месячный лимит сброшен, баланс={balance:,.0f} ₽")


def update_limits_after_trade(pnl: float, balance: float):
    """Обновляет счётчики после закрытия сделки."""
    limit_state.day_pnl += pnl

    day_limit = balance * MAX_DAILY_LOSS_PCT
    month_limit = limit_state.month_start_balance * MAX_MONTHLY_LOSS_PCT
    month_pnl = balance - limit_state.month_start_balance

    if not limit_state.day_blocked and limit_state.day_pnl <= -day_limit:
        limit_state.day_blocked = True
        log.warning(f"  🛑 ДНЕВНОЙ ЛИМИТ: потеряно {limit_state.day_pnl:.0f} ₽ "
                    f"(лимит -{day_limit:.0f} ₽). Торговля остановлена до завтра.")

    if not limit_state.month_blocked and month_pnl <= -month_limit:
        limit_state.month_blocked = True
        log.warning(f"  🛑 МЕСЯЧНЫЙ ЛИМИТ: потеряно {month_pnl:.0f} ₽ "
                    f"(лимит -{month_limit:.0f} ₽). Торговля остановлена до след. месяца.")


def is_trading_allowed(balance: float) -> bool:
    """Возвращает True если торговля разрешена."""
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
        return True
    threshold = np.percentile(list(limit_state.volume_history), MIN_VOLUME_PCT)
    if current_volume < threshold:
        log.info(f"  ⏸ Фильтр объёма: {current_volume:,.0f} < порога {threshold:,.0f}")
        return False
    return True

# ─────────────────────────────────────────────────────
#  API КЛИЕНТ
# ─────────────────────────────────────────────────────

class TInvestAPI:
    """Обёртка над tinkoff-invest API для работы с песочницей и боевым режимом."""
    
    def __init__(self, token: str, sandbox: bool = False):
        from tinkoff_invest import ProductionSession, SandboxSession
        
        self.sandbox = sandbox
        if sandbox:
            self.session = SandboxSession(token)
            log.info("  Подключена ПЕСОЧНИЦА T-Bank Invest")
        else:
            self.session = ProductionSession(token)
            log.info("  Подключён РЕАЛЬНЫЙ счёт T-Bank Invest")
        
        self.account_id = ""
        if sandbox:
            self._setup_sandbox()
    
    def _setup_sandbox(self):
        """Инициализация песочницы."""
        try:
            # В новой API песочница активируется автоматически через SandboxSession
            log.info(f"  Песочница активирована")
        except Exception as e:
            log.error(f"  Ошибка инициализации песочницы: {e}")
            raise
    
    def get_balance(self) -> float:
        """Получение баланса счёта."""
        try:
            portfolio = self.session.get_portfolio()
            
            # Считаем общую стоимость портфеля в рублях
            total = 0.0
            
            # Позиции по бумагам
            for pos in portfolio.positions:
                # Получаем текущую цену инструмента
                try:
                    instrument = self.session.get_instrument_by_ticker(pos.ticker)
                    # Для простоты используем среднюю цену позиции
                    avg_price = pos.average_price.value if hasattr(pos.average_price, 'value') else float(pos.average_price)
                    total += avg_price * pos.lots * POINT_VALUE
                except:
                    pass
            
            # Валютные позиции
            for curr in portfolio.currencies:
                if curr.name.currency == 'RUB':
                    total += curr.balance.value
            
            return round(total, 2)
        except Exception as e:
            log.error(f"  Ошибка получения баланса: {e}")
            return DEPOSIT
    
    def get_positions(self) -> Dict[str, dict]:
        """Получение открытых позиций."""
        try:
            portfolio = self.session.get_portfolio()
            positions = {}
            
            for pos in portfolio.positions:
                if pos.lots != 0:
                    direction = "BUY" if pos.lots > 0 else "SELL"
                    positions[pos.ticker] = {
                        'figi': pos.figi,
                        'lots': abs(pos.lots),
                        'direction': direction,
                        'avg_price': pos.average_price.value if hasattr(pos.average_price, 'value') else float(pos.average_price)
                    }
            
            return positions
        except Exception as e:
            log.error(f"  Ошибка получения позиций: {e}")
            return {}
    
    def get_orders(self) -> List[dict]:
        """Получение активных ордеров."""
        try:
            orders = self.session.get_orders()
            result = []
            for order in orders:
                result.append({
                    'id': order.id,
                    'figi': order.figi,
                    'operation': order.operation.value,
                    'lots': order.requested_lots,
                    'price': order.price,
                    'status': order.status.value,
                    'type': order.type.value
                })
            return result
        except Exception as e:
            log.error(f"  Ошибка получения ордеров: {e}")
            return []
    
    def create_market_order(self, operation: str, ticker: str, lots: int) -> Optional[str]:
        """Создание рыночного ордера. Возвращает ID ордера."""
        try:
            from tinkoff_invest.models.types import OperationType
            
            # Получаем FIGI по тику
            instrument = self.session.get_instrument_by_ticker(ticker)
            figi = instrument.figi
            
            op_type = OperationType.BUY if operation == "BUY" else OperationType.SELL
            
            if PAPER_MODE:
                log.info(f"  [PAPER] Рыночный ордер: {operation} {lots}×{ticker} @ market")
                return f"paper_{time.time()}"
            
            order = self.session.create_market_order(op_type, figi, lots)
            log.info(f"  ✓ Рыночный ордер создан: {operation} {lots}×{ticker}, ID={order.id}")
            return order.id
        except Exception as e:
            log.error(f"  ✗ Ошибка создания ордера: {e}")
            return None
    
    def create_limit_order(self, operation: str, ticker: str, price: float, lots: int) -> Optional[str]:
        """Создание лимитного ордера. Возвращает ID ордера."""
        try:
            from tinkoff_invest.models.types import OperationType
            
            instrument = self.session.get_instrument_by_ticker(ticker)
            figi = instrument.figi
            
            op_type = OperationType.BUY if operation == "BUY" else OperationType.SELL
            
            if PAPER_MODE:
                log.info(f"  [PAPER] Лимитный ордер: {operation} {lots}×{ticker} @ {price}")
                return f"paper_{time.time()}"
            
            order = self.session.create_limit_order(op_type, figi, price, lots)
            log.info(f"  ✓ Лимитный ордер создан: {operation} {lots}×{ticker} @ {price}, ID={order.id}")
            return order.id
        except Exception as e:
            log.error(f"  ✗ Ошибка создания лимитного ордера: {e}")
            return None
    
    def cancel_order(self, order_id: str) -> bool:
        """Отмена ордера."""
        try:
            if order_id.startswith('paper_'):
                log.info(f"  [PAPER] Отмена ордера: {order_id}")
                return True
            
            self.session.cancel_order(order_id)
            log.info(f"  ✓ Ордер отменён: {order_id}")
            return True
        except Exception as e:
            log.error(f"  ✗ Ошибка отмены ордера: {e}")
            return False
    
    def get_candles(self, ticker: str, days: int = 60, interval: str = "5min") -> pd.DataFrame:
        """Получение свечей за период."""
        try:
            from tinkoff_invest.models.types import SubscriptionInterval
            
            instrument = self.session.get_instrument_by_ticker(ticker)
            figi = instrument.figi
            
            # Маппинг интервалов
            interval_map = {
                "1min": SubscriptionInterval.MINUTES_1,
                "5min": SubscriptionInterval.MINUTES_5,
                "15min": SubscriptionInterval.MINUTES_15,
                "1hour": SubscriptionInterval.HOUR_1,
                "1day": SubscriptionInterval.DAY
            }
            si = interval_map.get(interval, SubscriptionInterval.MINUTES_5)
            
            end_time = datetime.now()
            start_time = end_time - timedelta(days=days)
            
            candles = self.session.get_candles(figi, start_time, end_time, si)
            
            if not candles:
                return pd.DataFrame()
            
            # Преобразуем в DataFrame
            data = []
            for c in candles:
                data.append({
                    'time': c.time,
                    'open': c.open_price,
                    'high': c.highest_price,
                    'low': c.lowest_price,
                    'close': c.close_price,
                    'volume': c.volume
                })
            
            df = pd.DataFrame(data)
            if not df.empty:
                df.set_index('time', inplace=True)
            
            return df
        except Exception as e:
            log.error(f"  Ошибка получения свечей: {e}")
            return pd.DataFrame()
    
    def get_all_futures(self) -> List[dict]:
        """Получение списка всех фьючерсов."""
        try:
            # В новой API нет отдельного метода для фьючерсов
            # Используем поиск по тику или stocks
            instruments_dict = self.session.stocks
            
            futures = []
            for ticker, inst in instruments_dict.items():
                # Фильтруем по базовому коду
                if ticker.upper().startswith(MINI_BASE.upper()) and len(ticker) == len(MINI_BASE) + 2:
                    futures.append({
                        'ticker': ticker,
                        'figi': inst.figi,
                        'name': inst.name
                    })
            
            return futures
        except Exception as e:
            log.error(f"  Ошибка получения списка фьючерсов: {e}")
            return []
    
    def close(self):
        """Закрытие сессии."""
        try:
            # В новой API нет явного метода закрытия
            pass
        except:
            pass


# ─────────────────────────────────────────────────────
#  ПОИСК АКТУАЛЬНОГО ТИКЕРА
# ─────────────────────────────────────────────────────

def get_active_ticker(base: str, futures_list: List[dict], api: TInvestAPI) -> Optional[str]:
    """Поиск активного фьючерса по базовому коду."""
    now = datetime.now()
    
    # Сортируем по имени (последний по дате экспирации)
    candidates = [f for f in futures_list
                  if f['ticker'].upper().startswith(base.upper())
                  and len(f['ticker']) == len(base) + 2]
    
    if not candidates:
        return None
    
    # Берём последний по алфавиту (обычно самый дальний)
    candidates.sort(key=lambda x: x['ticker'], reverse=True)
    return candidates[0]['ticker']


# ─────────────────────────────────────────────────────
#  КОМИССИЯ
# ─────────────────────────────────────────────────────

def calc_commission(price: float, lots: int) -> float:
    """Расчёт комиссии: 0.025% от номинала × 2 (вход + выход)."""
    return round(price * POINT_VALUE * lots * COMMISSION_PCT * 2, 2)


# ─────────────────────────────────────────────────────
#  МАРКЕТ ПРОФИЛЬ
# ─────────────────────────────────────────────────────

def build_market_profile(df: pd.DataFrame) -> Optional[MarketProfile]:
    """Построение рыночного профиля по свечам."""
    if df.empty or len(df) < 3:
        return None
    
    avg_price = df["close"].mean()
    tick = max(round(avg_price * 0.001, 4), 0.001)
    
    profile: Dict[float, float] = {}
    for _, row in df.iterrows():
        lo = round(row["low"] / tick) * tick
        hi = round(row["high"] / tick) * tick
        levels = np.arange(lo, hi + tick, tick)
        if len(levels) == 0:
            levels = np.array([lo])
        vol_per_level = row["volume"] / len(levels)
        for lvl in levels:
            key = round(float(lvl), 6)
            profile[key] = profile.get(key, 0) + vol_per_level
    
    sorted_lvl = sorted(profile.items())
    prices = np.array([x[0] for x in sorted_lvl])
    volumes = np.array([x[1] for x in sorted_lvl])
    total_vol = volumes.sum()
    
    if total_vol == 0:
        return None
    
    poc_idx = int(np.argmax(volumes))
    va_vol = volumes[poc_idx]
    lo_idx = hi_idx = poc_idx
    
    while va_vol < total_vol * VALUE_AREA_PCT:
        can_up = hi_idx + 1 < len(prices)
        can_down = lo_idx - 1 >= 0
        vol_up = volumes[hi_idx + 1] if can_up else 0
        vol_down = volumes[lo_idx - 1] if can_down else 0
        
        if not can_up and not can_down:
            break
        
        if vol_up >= vol_down and can_up:
            hi_idx += 1
            va_vol += volumes[hi_idx]
        else:
            lo_idx -= 1
            va_vol += volumes[lo_idx]
    
    va_width = round(float(prices[hi_idx] - prices[lo_idx]), 4)
    if va_width <= 0:
        return None
    
    return MarketProfile(
        poc=round(float(prices[poc_idx]), 4),
        vah=round(float(prices[hi_idx]), 4),
        val=round(float(prices[lo_idx]), 4),
        va_width=va_width,
        total_volume=round(float(total_vol), 0)
    )


# ─────────────────────────────────────────────────────
#  СИГНАЛЫ
# ─────────────────────────────────────────────────────

def get_signal(profile: MarketProfile, price: float, balance: float) -> Signal:
    """Генерация торгового сигнала на основе профиля."""
    buf = price * STOP_BUFFER_PCT
    pv = POINT_VALUE
    risk_per_trade = balance * RISK_PCT
    
    def build(direction: str, entry: float, stop: float, target: float) -> Signal:
        risk_pts = (entry - stop) if direction == "BUY" else (stop - entry)
        reward_pts = (target - entry) if direction == "BUY" else (entry - target)
        
        if risk_pts <= 0 or reward_pts <= 0:
            return Signal("HOLD", entry, stop, target, 0, 0, 0, 0, 0, 0, "Некорректные уровни")
        
        rr = round(reward_pts / risk_pts, 2)
        if rr < MIN_RR:
            return Signal("HOLD", entry, stop, target, 0, 0, 0, 0, rr, 0, f"RR={rr} < {MIN_RR}")
        
        comm_1 = calc_commission(entry, 1)
        risk_1_rub = risk_pts * pv
        
        if risk_1_rub - comm_1 <= 0:
            return Signal("HOLD", entry, stop, target, 0, 0, comm_1, 0, rr, 0,
                         "Комиссия съедает риск")
        
        lots = int(risk_per_trade / (risk_1_rub - comm_1))
        if lots < 1:
            return Signal("HOLD", entry, stop, target, 0, 0, comm_1, 0, rr, 0,
                         f"Недостаточно средств: нужно {risk_per_trade:.0f}₽, есть на 1 лот {risk_1_rub - comm_1:.0f}₽")
        
        # Проверка ГО
        go_required = GO_PER_LOT * lots * (1 + GO_BUFFER)
        if go_required > balance:
            max_lots = int(balance / GO_PER_LOT / (1 + GO_BUFFER))
            if max_lots < 1:
                return Signal("HOLD", entry, stop, target, 0, 0, comm_1, 0, rr, 0,
                             "Недостаточно ГО")
            lots = max_lots
        
        gross_risk = round(risk_pts * pv * lots, 2)
        commission_total = calc_commission(entry, lots)
        net_risk = round(gross_risk + commission_total, 2)
        ev = round((reward_pts * rr - risk_pts) * pv * lots, 2)
        
        return Signal(
            direction=direction,
            entry=round(entry, 4),
            stop=round(stop, 4),
            target=round(target, 4),
            gross_risk=gross_risk,
            net_risk=net_risk,
            commission=commission_total,
            lots=lots,
            rr=rr,
            expected_value=ev,
            reason=f"Пробой VA {'вверх' if direction == 'BUY' else 'вниз'}"
        )
    
    # Логика входа
    if price > profile.vah:
        return build("BUY", price, profile.vah - buf, profile.vah + profile.va_width)
    elif price < profile.val:
        return build("SELL", price, profile.val + buf, profile.val - profile.va_width)
    else:
        return Signal("HOLD", price, 0, 0, 0, 0, 0, 0, 0, 0, "Цена внутри VA")


# ─────────────────────────────────────────────────────
#  СОХРАНЕНИЕ/ЗАГРУЗКА СОСТОЯНИЯ
# ─────────────────────────────────────────────────────

def save_state():
    """Сохранение состояния бота."""
    state = {
        'positions': {k: vars(v) for k, v in open_positions.items()},
        'limits': {
            'day': limit_state.day.isoformat(),
            'day_pnl': limit_state.day_pnl,
            'day_blocked': limit_state.day_blocked,
            'month': limit_state.month,
            'month_start_balance': limit_state.month_start_balance,
            'month_blocked': limit_state.month_blocked,
        },
        'entered_today': list(entered_today_tickers)
    }
    
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, default=str)


def load_state():
    """Загрузка состояния бота."""
    global limit_state, open_positions, entered_today_tickers
    
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
        
        # Восстанавливаем позиции
        for ticker, pos_data in state.get('positions', {}).items():
            open_positions[ticker] = Position(**pos_data)
        
        # Восстанавливаем лимиты
        limits = state.get('limits', {})
        limit_state.day = date.fromisoformat(limits.get('day', date.today().isoformat()))
        limit_state.day_pnl = limits.get('day_pnl', 0.0)
        limit_state.day_blocked = limits.get('day_blocked', False)
        limit_state.month = limits.get('month', date.today().strftime("%Y-%m"))
        limit_state.month_start_balance = limits.get('month_start_balance', DEPOSIT)
        limit_state.month_blocked = limits.get('month_blocked', False)
        
        # Восстанавливаем тикеры
        entered_today_tickers = set(state.get('entered_today', []))
        
        log.info(f"  Состояние загружено: {len(open_positions)} позиций")
        return True
    except FileNotFoundError:
        log.info("  Файл состояния не найден — начинаем с нуля")
        return False
    except Exception as e:
        log.error(f"  Ошибка загрузки состояния: {e}")
        return False


def log_trade(signal: Signal, ticker: str, balance: float, exit_price: Optional[float] = None):
    """Логирование сделки."""
    trade = {
        'timestamp': datetime.now().isoformat(),
        'ticker': ticker,
        'direction': signal.direction,
        'entry': signal.entry,
        'exit': exit_price,
        'stop': signal.stop,
        'target': signal.target,
        'lots': signal.lots,
        'rr': signal.rr,
        'reason': signal.reason,
        'balance': balance
    }
    
    trades = []
    try:
        with open(TRADES_LOG, 'r', encoding='utf-8') as f:
            trades = json.load(f)
    except:
        pass
    
    trades.append(trade)
    
    with open(TRADES_LOG, 'w', encoding='utf-8') as f:
        json.dump(trades, f, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────────────────
#  ГЛАВНЫЙ ЦИКЛ
# ─────────────────────────────────────────────────────

def main():
    """Главная функция бота."""
    log.info("=" * 60)
    log.info("Торговый бот v3.0 (T-Bank Invest API) запускается...")
    log.info(f"  Режим: {'ПЕСОЧНИЦА' if SANDBOX_MODE else 'РЕАЛЬНЫЙ'}")
    log.info(f"  Инструмент: {MINI_BASE} (point_value={POINT_VALUE})")
    log.info(f"  Депозит: {DEPOSIT:,.0f} ₽")
    log.info("=" * 60)
    
    # Инициализация API
    try:
        api = TInvestAPI(TINKOFF_TOKEN, sandbox=SANDBOX_MODE)
    except Exception as e:
        log.error(f"  Критическая ошибка подключения: {e}")
        return
    
    # Загрузка состояния
    load_state()
    
    # Проверка лимитов
    balance = api.get_balance()
    reset_day_limit(balance)
    reset_month_limit(balance)
    
    log.info(f"  Баланс: {balance:,.0f} ₽")
    
    # Получение списка инструментов
    futures_list = api.get_all_futures()
    if not futures_list:
        log.error(f"  Не найдено инструментов для {MINI_BASE}")
        api.close()
        return
    
    ticker = get_active_ticker(MINI_BASE, futures_list, api)
    if not ticker:
        log.error(f"  Не найден активный тикер для {MINI_BASE}")
        api.close()
        return
    
    log.info(f"  Активный инструмент: {ticker}")
    
    # Главный цикл
    try:
        while True:
            # Обновление лимитов
            balance = api.get_balance()
            reset_day_limit(balance)
            reset_month_limit(balance)
            
            # Проверка возможности торговли
            if not is_trading_allowed(balance):
                log.info("  ⏸ Торговля暂停 — лимиты сработали")
                time.sleep(CHECK_INTERVAL)
                continue
            
            # Проверка открытых позиций
            api_positions = api.get_positions()
            
            # Синхронизация с API
            for t, pos_data in api_positions.items():
                if t not in open_positions:
                    log.warning(f"  ⚠️  Найдена позиция в API: {pos_data['direction']} {pos_data['lots']}×{t}")
                    # Можно добавить логику восстановления
            
            # Если есть открытая позиция — проверяем выход
            if ticker in open_positions:
                pos = open_positions[ticker]
                
                # Получаем текущую цену
                df = api.get_candles(ticker, days=1, interval="1min")
                if df.empty:
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                current_price = df['close'].iloc[-1]
                
                # Проверка стопа или тейка
                should_close = False
                close_reason = ""
                
                if pos.direction == "BUY":
                    if current_price <= pos.stop:
                        should_close = True
                        close_reason = "STOP"
                    elif current_price >= pos.target:
                        should_close = True
                        close_reason = "TARGET"
                else:  # SELL
                    if current_price >= pos.stop:
                        should_close = True
                        close_reason = "STOP"
                    elif current_price <= pos.target:
                        should_close = True
                        close_reason = "TARGET"
                
                if should_close:
                    log.info(f"  {close_reason}: Закрытие позиции {pos.direction} {pos.lots}×{ticker}")
                    
                    # Закрываем позицию
                    exit_op = "SELL" if pos.direction == "BUY" else "BUY"
                    api.create_market_order(exit_op, ticker, pos.lots)
                    
                    # Расчёт P&L
                    if pos.direction == "BUY":
                        pnl = (current_price - pos.entry_price) * POINT_VALUE * pos.lots
                    else:
                        pnl = (pos.entry_price - current_price) * POINT_VALUE * pos.lots
                    
                    pnl -= calc_commission(pos.entry_price, pos.lots)
                    pnl = round(pnl, 2)
                    
                    log.info(f"  P&L: {pnl:+,.0f} ₽")
                    
                    # Логирование
                    fake_signal = Signal(
                        direction=pos.direction,
                        entry=pos.entry_price,
                        stop=pos.stop,
                        target=pos.target,
                        gross_risk=0,
                        net_risk=0,
                        commission=calc_commission(pos.entry_price, pos.lots),
                        lots=pos.lots,
                        rr=0,
                        expected_value=pnl,
                        reason=close_reason
                    )
                    log_trade(fake_signal, ticker, balance, exit_price=current_price)
                    
                    # Обновление лимитов
                    update_limits_after_trade(pnl, balance)
                    
                    # Удаление позиции
                    del open_positions[ticker]
                    save_state()
                
                time.sleep(CHECK_INTERVAL)
                continue
            
            # Если позиции нет — ищем вход
            if ticker in entered_today_tickers:
                log.info(f"  {ticker}: уже входили сегодня")
                time.sleep(CHECK_INTERVAL)
                continue
            
            # Получение данных
            df = api.get_candles(ticker, days=60, interval="5min")
            if df.empty:
                log.warning(f"  Нет данных для {ticker}")
                time.sleep(CHECK_INTERVAL)
                continue
            
            # Построение профиля
            profile = build_market_profile(df)
            if not profile:
                log.warning(f"  Не удалось построить профиль для {ticker}")
                time.sleep(CHECK_INTERVAL)
                continue
            
            log.info(f"  Профиль: POC={profile.poc:.4f}, VAH={profile.vah:.4f}, VAL={profile.val:.4f}")
            
            # Получение текущей цены
            current_price = df['close'].iloc[-1]
            
            # Проверка объёма
            if not is_volume_ok(profile.total_volume):
                time.sleep(CHECK_INTERVAL)
                continue
            
            # Генерация сигнала
            signal = get_signal(profile, current_price, balance)
            
            if signal.direction == "HOLD":
                log.info(f"  Сигнал: HOLD ({signal.reason})")
            else:
                log.info(f"  Сигнал: {signal.direction} {signal.lots} лотов")
                log.info(f"    Вход: {signal.entry:.4f}, Стоп: {signal.stop:.4f}, Цель: {signal.target:.4f}")
                log.info(f"    RR: {signal.rr}, EV: {signal.expected_value:+,.0f}₽")
                
                # Открытие позиции
                api.create_market_order(signal.direction, ticker, signal.lots)
                
                # Сохранение позиции
                open_positions[ticker] = Position(
                    ticker=ticker,
                    direction=signal.direction,
                    entry_price=signal.entry,
                    stop=signal.stop,
                    target=signal.target,
                    lots=signal.lots,
                    opened_at=datetime.now().isoformat()
                )
                entered_today_tickers.add(ticker)
                save_state()
                
                log_trade(signal, ticker, balance)
            
            time.sleep(CHECK_INTERVAL)
    
    except KeyboardInterrupt:
        log.info("  Остановка бота пользователем...")
    except Exception as e:
        log.error(f"  Критическая ошибка: {e}", exc_info=True)
    finally:
        save_state()
        api.close()
        log.info("  Бот остановлен")


if __name__ == "__main__":
    main()
