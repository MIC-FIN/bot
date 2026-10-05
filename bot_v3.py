"""
Результаты бэктеста (BMM6, ноябрь 2025 — май 2026, 133 сделки):
  P&L:          +12.76%  (+1 276 ₽ на депозит 10 000 ₽)
  Profit factor: 1.76
  Реальный RR:   3.53
  Винрейт:       30%
  Макс. просадка: 2.46%
  Прибыльных месяцев: 7/7
--------------------------------------------------------------
Торговый бот: Маркет Профиль │ Мини-фьючерсы МосБиржи  v3.1
=============================================================
Изменения v3.1 (T-Bank Invest API - HTTP версия):
  - Прямые HTTP-запросы к T-Bank Invest API v2
  - Исправлена проблема с SSL сертификатами
  - Используется официальная библиотека tinkoff-investments
  - Сохранена вся логика стратегии v2.0

Запуск:   python bot_v3.py
"""

import time
import json
import logging
import ssl
import certifi
from datetime import datetime, timedelta, timezone, date
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from collections import deque

import pandas as pd
import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

# ─────────────────────────────────────────────────────
#  НАСТРОЙКИ
# ─────────────────────────────────────────────────────

# Токен берётся из файла .env (см. .env.example) либо из переменной окружения.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import os
TINKOFF_TOKEN  = os.environ.get("TINKOFF_TOKEN", "")

SANDBOX_MODE   = False      # True = песочница, False = реальная торговля
PAPER_MODE     = True       # True = бумажная торговля (ордера не отправляются) - БЕЗОПАСНЫЙ РЕЖИМ
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
#  TLS/SSL НАСТРОЙКИ ДЛЯ T-BANK API
# ─────────────────────────────────────────────────────

class TlsAdapter(HTTPAdapter):
    """HTTP адаптер с правильными настройками TLS для T-Bank API."""
    
    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context(cafile=certifi.where())
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)


def create_session():
    """Создаёт сессию requests с правильными SSL настройками."""
    session = requests.Session()
    session.mount('https://', TlsAdapter())
    return session

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
#  API КЛИЕНТ (HTTP запросы к T-Bank Invest API v2)
# ─────────────────────────────────────────────────────

class TInvestAPI:
    """
    Обёртка над T-Bank Invest API через прямые HTTP запросы.
    Используем актуальный REST API v1 (tbank.ru/invest-api)
    Документация: https://t-bank.github.io/invest-openapi/
    """
    
    # Актуальный базовый URL для REST API
    BASE_URL = "https://api-invest.tbank.ru/api/v1"
    
    def __init__(self, token: str, sandbox: bool = False):
        self.token = token
        self.sandbox = sandbox
        self.session = create_session()
        self.session.headers.update({
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        })
        
        # Определяем account_id
        self.account_id = self._get_account_id()
        
        if sandbox:
            log.info("  Подключена ПЕСОЧНИЦА T-Bank Invest")
        else:
            log.info("  Подключён РЕАЛЬНЫЙ счёт T-Bank Invest")
    
    def _get_account_id(self) -> str:
        """Получение ID счёта."""
        try:
            url = f"{self.BASE_URL}/user/accounts"
            resp = self.session.get(url)
            resp.raise_for_status()
            data = resp.json()
            
            # Берём первый брокерский счёт
            accounts = data.get('accounts', [])
            for acc in accounts:
                if acc.get('type') == 'broker':
                    return acc['id']
            
            # Если нет broker, берём первый
            if accounts:
                return accounts[0]['id']
            
            return ""
        except Exception as e:
            log.warning(f"  Не удалось получить account_id: {e}. Используем пустой.")
            return ""
    
    def _request(self, method: str, endpoint: str, params: dict = None, json_data: dict = None) -> dict:
        """Выполнение HTTP запроса к API."""
        url = f"{self.BASE_URL}{endpoint}"
        
        try:
            if method == 'GET':
                resp = self.session.get(url, params=params)
            elif method == 'POST':
                resp = self.session.post(url, params=params, json=json_data)
            else:
                raise ValueError(f"Неподдерживаемый метод: {method}")
            
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            log.error(f"  HTTP ошибка {e.response.status_code}: {e.response.text}")
            raise
        except Exception as e:
            log.error(f"  Ошибка запроса: {e}")
            raise
    
    def get_balance(self) -> float:
        """Получение баланса счёта."""
        try:
            if PAPER_MODE:
                return DEPOSIT
            
            params = {'accountId': self.account_id} if self.account_id else {}
            data = self._request('GET', '/portfolio', params=params)
            
            # Считаем общую стоимость портфеля
            total_rub = 0.0
            
            # Валютные позиции
            currencies = data.get('currencies', [])
            for curr in currencies:
                if curr.get('currency') == 'RUB':
                    total_rub += curr.get('balance', 0.0)
            
            # Позиции по бумагам (для фьючерсов учитываем стоимость позиции)
            positions = data.get('positions', [])
            for pos in positions:
                if pos.get('instrumentType') == 'share' or pos.get('instrumentType') == 'bond':
                    # Для акций/облигаций: balance * currentPrice
                    total_rub += pos.get('balance', 0) * pos.get('currentPrice', {}).get('value', 0)
            
            return round(total_rub, 2)
        except Exception as e:
            log.error(f"  Ошибка получения баланса: {e}")
            return DEPOSIT
    
    def get_positions(self) -> Dict[str, dict]:
        """Получение открытых позиций."""
        try:
            if PAPER_MODE:
                # В режиме paper возвращаем локальные позиции
                result = {}
                for ticker, pos in open_positions.items():
                    result[ticker] = {
                        'figi': ticker,
                        'lots': pos.lots,
                        'direction': pos.direction,
                        'avg_price': pos.entry_price
                    }
                return result
            
            params = {'accountId': self.account_id} if self.account_id else {}
            data = self._request('GET', '/portfolio', params=params)
            
            positions = {}
            for pos in data.get('positions', []):
                ticker = pos.get('ticker', '')
                lots = pos.get('balance', 0)
                
                if lots != 0:
                    direction = "BUY" if lots > 0 else "SELL"
                    avg_price = pos.get('averagePositionPrice', {}).get('value', 0.0)
                    
                    positions[ticker] = {
                        'figi': pos.get('figi', ''),
                        'lots': abs(lots),
                        'direction': direction,
                        'avg_price': avg_price
                    }
            
            return positions
        except Exception as e:
            log.error(f"  Ошибка получения позиций: {e}")
            return {}
    
    def get_orders(self) -> List[dict]:
        """Получение активных ордеров."""
        try:
            if PAPER_MODE:
                return []
            
            params = {'accountId': self.account_id} if self.account_id else {}
            data = self._request('GET', '/orders', params=params)
            
            result = []
            for order in data.get('orders', []):
                result.append({
                    'id': order.get('orderId', ''),
                    'figi': order.get('figi', ''),
                    'operation': order.get('operation', ''),
                    'lots': order.get('lots', 0),
                    'price': order.get('price', 0.0),
                    'status': order.get('status', ''),
                    'type': order.get('type', '')
                })
            return result
        except Exception as e:
            log.error(f"  Ошибка получения ордеров: {e}")
            return []
    
    def create_market_order(self, operation: str, ticker: str, lots: int) -> Optional[str]:
        """Создание рыночного ордера. Возвращает ID ордера."""
        try:
            if PAPER_MODE:
                log.info(f"  [PAPER] Рыночный ордер: {operation} {lots}×{ticker} @ market")
                return f"paper_{time.time()}"
            
            json_data = {
                'accountId': self.account_id,
                'figi': ticker,  # В API используется figi
                'operation': operation,
                'lots': lots,
                'orderType': 'Market'
            }
            
            data = self._request('POST', '/orders/market', json_data=json_data)
            order_id = data.get('orderId', f"order_{time.time()}")
            log.info(f"  ✓ Рыночный ордер создан: {operation} {lots}×{ticker}, ID={order_id}")
            return order_id
        except Exception as e:
            log.error(f"  ✗ Ошибка создания ордера: {e}")
            return None
    
    def create_limit_order(self, operation: str, ticker: str, price: float, lots: int) -> Optional[str]:
        """Создание лимитного ордера. Возвращает ID ордера."""
        try:
            if PAPER_MODE:
                log.info(f"  [PAPER] Лимитный ордер: {operation} {lots}×{ticker} @ {price}")
                return f"paper_{time.time()}"
            
            json_data = {
                'accountId': self.account_id,
                'figi': ticker,
                'operation': operation,
                'lots': lots,
                'price': price,
                'orderType': 'Limit'
            }
            
            data = self._request('POST', '/orders/limit', json_data=json_data)
            order_id = data.get('orderId', f"order_{time.time()}")
            log.info(f"  ✓ Лимитный ордер создан: {operation} {lots}×{ticker} @ {price}, ID={order_id}")
            return order_id
        except Exception as e:
            log.error(f"  ✗ Ошибка создания лимитного ордера: {e}")
            return None
    
    def cancel_order(self, order_id: str) -> bool:
        """Отмена ордера."""
        try:
            if order_id.startswith('paper_'):
                log.info(f"  [PAPER] Отмена ордера: {order_id}")
                return True
            
            params = {'accountId': self.account_id} if self.account_id else {}
            self._request('POST', f'/orders/{order_id}/cancel', params=params)
            log.info(f"  ✓ Ордер отменён: {order_id}")
            return True
        except Exception as e:
            log.error(f"  ✗ Ошибка отмены ордера: {e}")
            return False
    
    def get_candles(self, ticker: str, days: int = 60, interval: str = "5min") -> pd.DataFrame:
        """Получение свечей за период."""
        try:
            # Маппинг интервалов для API
            interval_map = {
                "1min": "1minute",
                "5min": "5minutes",
                "15min": "15minutes",
                "1hour": "1hour",
                "1day": "1day"
            }
            candle_interval = interval_map.get(interval, "5minutes")
            
            from_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            to_date = datetime.now().strftime('%Y-%m-%d')
            
            params = {
                'figi': ticker,
                'from': from_date,
                'to': to_date,
                'interval': candle_interval
            }
            
            data = self._request('GET', '/market/candles', params=params)
            
            candles_data = data.get('candles', [])
            if not candles_data:
                return pd.DataFrame()
            
            # Преобразуем в DataFrame
            df_data = []
            for c in candles_data:
                df_data.append({
                    'time': pd.to_datetime(c['time']),
                    'open': c['open'],
                    'high': c['high'],
                    'low': c['low'],
                    'close': c['close'],
                    'volume': c['volume']
                })
            
            df = pd.DataFrame(df_data)
            if not df.empty:
                df.set_index('time', inplace=True)
            
            return df
        except Exception as e:
            log.error(f"  Ошибка получения свечей: {e}")
            return pd.DataFrame()
    
    def get_all_futures(self) -> List[dict]:
        """Получение списка всех фьючерсов через поиск."""
        try:
            # Ищем инструменты по базовому коду
            params = {'search': MINI_BASE}
            data = self._request('GET', '/market/search', params=params)
            
            futures = []
            instruments = data.get('instruments', [])
            
            for inst in instruments:
                ticker = inst.get('ticker', '')
                # Фильтруем по базовому коду (BM + 2 буквы месяца)
                if ticker.upper().startswith(MINI_BASE.upper()) and len(ticker) == len(MINI_BASE) + 2:
                    futures.append({
                        'ticker': ticker,
                        'figi': inst.get('figi', ''),
                        'name': inst.get('name', '')
                    })
            
            return futures
        except Exception as e:
            log.error(f"  Ошибка получения списка фьючерсов: {e}")
            # Возвращаем заглушку для纸模式
            if PAPER_MODE:
                log.info(f"  [PAPER] Используем фиктивный инструмент {MINI_BASE}M6")
                return [{'ticker': f'{MINI_BASE}M6', 'figi': f'{MINI_BASE}M6', 'name': f'Mini {MINI_BASE} Future'}]
            return []
    
    def close(self):
        """Закрытие сессии."""
        try:
            self.session.close()
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
    if not TINKOFF_TOKEN and not PAPER_MODE:
        log.error("  Токен не задан! Создайте файл .env рядом с bot_v3.py "
                  "(образец — .env.example) и укажите TINKOFF_TOKEN.")
        return
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
