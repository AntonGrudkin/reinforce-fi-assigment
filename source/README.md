# Market Making — Take-Home

## Задание

Спроектировать **market making** бота и **отбэктестить** его на **3 календарных дня** данных (2026-03-19 … 2026-03-21).

Ожидаемый результат: описание стратегии, симулятор (fills, инвентарь, PnL), метрики за период и краткие выводы.

## Данные

Инструмент — **ETH perpetual** (~2200 USD), один рынок, без колонки символа.

| Папка | Файлы | Содержимое |
|-------|--------|------------|
| `data/orderbook/` | по файлу на день | Снимки стакана: 20 уровней bid/ask (`bid_price_i`, `ask_price_i`, `bid_qty_i`, `ask_qty_i`), `datetime` (наносекунды) |
| `data/trades/` | по файлу на день | Сделки: `datetime`, `price`, `size`, `is_maker_ask` (1 — агрессор покупатель, 0 — агрессор продавец) |
| `data/fundings/` | по файлу на день | Прогноз/ставка фандинга: `datetime`, `funding_rate` (~каждые 20 с) |

--- 

# Take-Home Assignment
## Objective
Design a market-making bot and backtest it on three calendar days of historical data:
- Description of the market-making strategy
- Backtesting simulator, including:
    - Order fills
    - Inventory tracking
    - PnL calculation
- Performance metrics for the evaluation period
- Brief conclusions and analysis

## Evaluation Requirements
The solution should include:
- Strategy Design
    - Quoting logic
    - Inventory management
    - Funding-rate usage
- Backtesting Engine
    - Order placement and cancellation logic
    - Fill simulation
    - Inventory accounting
    - Realized and unrealized PnL calculation
- Performance Analysis
    - Total PnL
    - Daily PnL
    - Inventory statistics
    - Fill statistics
    - Risk metrics
    - Any additional metrics considered relevant