# 🛒 2parser — Amazon Scraper

![Python](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white)
![Marketplaces](https://img.shields.io/badge/Marketplaces-21-orange)
![Proxies](https://img.shields.io/badge/Proxies-2captcha.com-green)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

Боевой Amazon scraper для всех 21 маркетплейса с поддержкой прокси [2captcha.com](https://2captcha.com/proxy).

---

## ✨ Возможности

- **21 маркетплейс** — .com, .de, .co.uk, .fr, .co.jp, .in, .com.br и другие
- **Прокси 2captcha** — формат из ЛК, ротация, health tracking, cooldown на бане
- **Поиск** — пагинация, фильтры цены и сортировки, дозаполнение пропущенных цен
- **Товары** — цена, рейтинг, изображения, bullet points, вариации, продавец
- **Best Sellers** — поддержка 4 форматов DOM (включая актуальный 2024–2025)
- **Отзывы** — автор, рейтинг, текст, верифицированная покупка
- **Anti-bot** — 4 browser fingerprint профиля, per-market Accept-Language, 503/429 backoff
- **Экспорт** — JSON, JSON Lines, CSV (UTF-8-BOM для Excel)

---

## 📦 Установка

```bash
git clone https://github.com/2parser/amazon-scraper
cd amazon-scraper
pip install -r requirements.txt
```

---

## 🔑 Прокси

Прокси берутся из личного кабинета **2captcha.com → Proxy**.  
Скачай список и передай через `--proxies`:

```
# proxies.txt — один прокси на строку (формат из ЛК)
http://1.2.3.4:8080:login:password
http://5.6.7.8:3128:login:password
```

> API-ключ 2captcha для прокси не нужен — они выдаются с логином/паролем.

---

## 🚀 Быстрый старт

```bash
# Поиск — 10 страниц на amazon.de, заполнить пропущенные цены
python main.py search "iphone" -m de \
    --proxies proxies.txt \
    --pages 10 \
    --fill-prices \
    --output results.json

# Один товар по ASIN
python main.py product B08N5WRWNW -m de --proxies proxies.txt

# Best Sellers
python main.py best-sellers "Electronics" -m de --proxies proxies.txt

# Отзывы
python main.py reviews B08N5WRWNW -m de --proxies proxies.txt --pages 5

# Массовый сбор из файла с ASIN (один ASIN на строку)
python main.py bulk asins.txt -m de --proxies proxies.txt --workers 4

# Все поддерживаемые маркетплейсы
python main.py --list-marketplaces
```

---

## 🌍 Маркетплейсы

`-m` / `--marketplace` принимает любой формат:

```bash
-m de            # TLD
-m uk            # alias (→ co.uk)
-m co.jp         # полный TLD
-m amazon.fr     # домен
-m https://www.amazon.it/...  # URL — маркетплейс определяется автоматически
```

| TLD | Страна | Валюта |
|-----|--------|--------|
| com | United States | USD |
| co.uk | United Kingdom | GBP |
| de | Germany | EUR |
| fr | France | EUR |
| it | Italy | EUR |
| es | Spain | EUR |
| nl | Netherlands | EUR |
| pl | Poland | PLN |
| se | Sweden | SEK |
| co.jp | Japan | JPY |
| ca | Canada | CAD |
| com.au | Australia | AUD |
| in | India | INR |
| com.br | Brazil | BRL |
| com.mx | Mexico | MXN |
| sg | Singapore | SGD |
| ae | UAE | AED |
| sa | Saudi Arabia | SAR |
| com.tr | Turkey | TRY |
| com.be | Belgium | EUR |
| eg | Egypt | EGP |

Для каждого маркетплейса автоматически подставляется правильный `Accept-Language`  
и страна для гео-таргетинга прокси.

---

## 💡 Дозаполнение цен (`--fill-prices`)

Вариативные товары (iPhone разных цветов, ноутбуки разных конфигураций)  
не показывают цену в поисковой выдаче — Amazon выводит "Weitere Optionen".

`--fill-prices` делает доп. запрос на `/dp/ASIN` для каждого такого товара:

```
INFO  fill_missing_prices: 3 позиций без цены → запрашиваем страницы товаров
INFO    ✓  B0CHX96JDY → 788.00 EUR
INFO    ✓  B09G995PVT → 303.00 EUR
INFO    ✓  B0CHWWM3JH → 774.00 EUR
INFO  fill_missing_prices: заполнено 3/3 позиций
```

---

## 🐍 Python API

```python
from scraper import AmazonScraper
from proxy_manager import ProxyManager
from output import save_json, save_csv

# Инициализация
scraper = AmazonScraper(
    proxy="http://host:port:login:password",  # один прокси
    # proxy_list=["http://...", "http://..."], # или список
    marketplace="de",
)

# Загрузка прокси из файла
scraper.proxy_manager = ProxyManager.from_file("proxies.txt")
scraper.client.proxy_manager = scraper.proxy_manager

# Поиск
results = scraper.search(
    "iphone",
    pages=10,
    sort_by="review_rank",      # featured|price_asc|price_desc|review_rank|date_rank
    fill_missing_prices=True,
)
save_json([r.to_dict() for r in results], "results.json")
save_csv([r.to_dict() for r in results],  "results.csv")

# Один товар
product = scraper.get_product("B08N5WRWNW")
print(product.title, product.price, product.currency)

# Best Sellers
items = scraper.get_best_sellers("Electronics")

# Отзывы
reviews = scraper.get_reviews("B08N5WRWNW", pages=5)

# Массовый сбор
products = scraper.get_products_bulk(["B08N5WRWNW", "B07VGRJDFY"], workers=3)
```

---

## 📊 Поля данных

### Поиск
| Поле | Тип | Описание |
|------|-----|----------|
| `asin` | str | Идентификатор Amazon |
| `title` | str | Название |
| `price` | float | Цена |
| `currency` | str | Валюта |
| `rating` | float | Рейтинг (1.0–5.0) |
| `reviews_count` | int | Количество отзывов |
| `url` | str | URL товара |
| `sponsored` | bool | Рекламная позиция |
| `prime_eligible` | bool | Prime доставка |
| `image_url` | str | Изображение |

### Страница товара
`asin` · `title` · `brand` · `price` · `currency` · `list_price` · `rating` · `reviews_count` · `availability` · `prime_eligible` · `fulfilled_by_amazon` · `seller_name` · `images[]` · `bullets[]` · `description` · `category_breadcrumb[]` · `variations{}` · `deals` · `url`

### Best Sellers
`rank` · `asin` · `title` · `price` · `currency` · `rating` · `reviews_count` · `image_url` · `url` · `prime_eligible`

### Отзывы
`review_id` · `author` · `rating` · `title` · `date` · `body` · `verified_purchase` · `helpful_votes`

---

## 🛡️ Anti-bot

| Механизм | Детали |
|----------|--------|
| Browser fingerprints | Chrome Win/Mac, Firefox, Safari — реальные UA + matching sec-ch-ua |
| Accept-Language | По маркетплейсу: `de-DE` для .de, `ja-JP` для .co.jp и т.д. |
| Задержки | Random 2–6с, настраивается через `--delay-min` / `--delay-max` |
| 503 / 429 | Backoff 8с → 16с → 30с + смена прокси |
| CAPTCHA / блок | 8 паттернов детекции, ротация UA профиля |
| Proxy health | Round-robin, cooldown 60с после 3 банов, исключение при SR < 20% |

---

## 📁 Структура проекта

```
amazon_scraper/
├── marketplace.py   ← реестр 21 маркетплейса, резолвер форматов
├── proxy_manager.py ← пул прокси 2captcha, ротация, health tracking
├── http_client.py   ← stealth HTTP, retry, 503/429 backoff
├── parser.py        ← lxml парсеры: product / search / reviews / best-sellers
├── scraper.py       ← высокоуровневый API
├── output.py        ← экспорт JSON / JSONL / CSV
├── main.py          ← CLI
└── requirements.txt
```

---

## ⚙️ CLI Reference

```
python main.py [--list-marketplaces]
python main.py <команда> <цель> [опции]

Команды:
  product         Один товар (ASIN или URL)
  search          Поиск
  reviews         Отзывы к товару
  bulk            Массовый сбор из файла с ASIN
  best-sellers    Best Sellers страница

Основные опции:
  -o, --output PATH      Файл вывода (.json | .jsonl | .csv)
  -m, --marketplace      Маркетплейс: de, uk, jp, co.uk, amazon.fr... (default: com)
      --proxies FILE      Файл прокси из ЛК 2captcha (один на строку)
      --proxy STRING      Один прокси: http://host:port:login:password
      --2captcha-key KEY  API ключ — только для решения CAPTCHA

Поиск:
      --pages N           Страниц (default: 1)
      --sort ORDER        featured|price_asc|price_desc|review_rank|date_rank
      --min-price N       Мин. цена
      --max-price N       Макс. цена
      --fill-prices       Дозаполнить цены через /dp/ для вариативных позиций

Производительность:
      --workers N         Потоков для bulk/fill-prices (default: 3)
      --delay-min SEC     Мин. задержка (default: 2.0)
      --delay-max SEC     Макс. задержка (default: 6.0)

Отладка:
      --debug-html PATH   Сохранить raw HTML для анализа DOM
      --list-marketplaces Показать все 21 маркетплейс и выйти
```

---

*Часть проекта [2parser](https://github.com/2parser) — парсеры для популярных сайтов.*
