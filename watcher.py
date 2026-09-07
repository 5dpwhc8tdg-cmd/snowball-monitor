#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-бот: мониторит публичный портфель snowball-income.com
и присылает уведомление о каждой новой сделке (покупка/продажа)
со всеми параметрами: актив, ISIN, дата, количество, цена,
комиссия, сумма, прибыль (в % и в рублях).

Режимы источника:
  source=api   - запрос к JSON-API сайта (шаблон запроса определяется
                 автоматически через --discover на базе Playwright)
  source=html  - рендер страницы в headless-браузере + парсинг таблиц
                 (или обычный HTTP-запрос, если сервер отдаёт строки в HTML)

Команды бота: /start /stop /status /test /interval
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import shlex
import requests

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    print("Нужен пакет beautifulsoup4:  pip install -r requirements.txt", file=sys.stderr)
    raise

log = logging.getLogger("snowball")

BASE_URL = "https://snowball-income.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TX_HEADERS = ["Операция", "Актив", "Дата", "Количество", "Цена", "Комиссия", "Сумма", "Прибыль"]


# --------------------------------------------------------------------------- #
#  Модели
# --------------------------------------------------------------------------- #
@dataclass
class Transaction:
    operation: str = ""       # Покупка / Продажа
    asset: str = ""           # название бумаги
    isin: str = ""
    date: str = ""            # как на сайте, дд.мм.гггг
    qty: str = ""
    price: str = ""
    commission: str = ""
    amount: str = ""
    profit_pct: str = ""
    profit_abs: str = ""
    portfolio: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Отпечаток сделки для дедупликации (порядок строк в выдаче может
        меняться, поэтому хеш считаем только по содержательным полям)."""
        raw = "|".join([
            self.portfolio, self.operation, self.asset, self.isin, self.date,
            self.qty, self.price, self.commission, self.amount, self.profit_abs,
        ])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_message(self, tx_url: str) -> str:
        icon = {"покупка": "🟢", "продажа": "🔴"}.get(self.operation.lower(), "⚪️")
        lines = [f"{icon} <b>{esc(self.operation or 'Сделка')}</b> — {esc(self.portfolio)}", ""]
        lines.append(f"<b>{esc(self.asset)}</b>" + (f"  <code>{esc(self.isin)}</code>" if self.isin else ""))
        rows = [
            ("Дата", self.date), ("Количество", self.qty), ("Цена", self.price),
            ("Комиссия", self.commission), ("Сумма", self.amount),
        ]
        profit = " ".join(x for x in (self.profit_pct, self.profit_abs) if x and x != "-")
        if profit:
            rows.append(("Прибыль", profit))
        for name, val in rows:
            if val and val != "-":
                lines.append(f"{name}: <b>{esc(val)}</b>")
        lines.append("")
        lines.append(f'<a href="{tx_url}">Открыть сделки портфеля</a>')
        return "\n".join(lines)


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- #
#  Разбор HTML
# --------------------------------------------------------------------------- #
def _lines(cell) -> list[str]:
    return [t.strip() for t in cell.get_text("\n", strip=True).split("\n") if t.strip()]


def parse_tx_table(html: str) -> list[Transaction]:
    """Достаёт сделки из HTML публичной страницы портфеля."""
    soup = BeautifulSoup(html, "lxml")
    out: list[Transaction] = []
    for table in soup.find_all("table"):
        heads = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        if heads[:3] != TX_HEADERS[:3]:          # сделки, а не начисления
            continue
        body = table.find("tbody")
        if not body:
            continue
        for tr in body.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < len(TX_HEADERS):
                continue
            c = [_lines(td) for td in cells]
            isin = ""
            link = cells[1].find("a")
            if link:
                m = re.search(r"(RU|US|KYG|XS|NL)\w{6,12}", link.get_text(" ", strip=True))
                if m:
                    isin = m.group(0)
                elif c[1]:
                    isin = c[1][-1]
            tx = Transaction(
                operation=c[0][0] if c[0] else "",
                asset=c[1][0] if c[1] else "",
                isin=isin,
                date=c[2][0] if c[2] else "",
                qty=c[3][0] if c[3] else "",
                price=c[4][0] if c[4] else "",
                commission=c[5][0] if c[5] else "",
                amount=c[6][0] if c[6] else "",
                profit_pct=c[7][0] if c[7] else "",
                profit_abs=c[7][1] if len(c[7]) > 1 else "",
            )
            if tx.operation and tx.asset:
                out.append(tx)
    return out


def _to_number(s: str) -> float | None:
    """'111 712,06 ₽' -> 111712.06 (nbsp, пробелы, запятая-десятичная)."""
    if not s:
        return None
    t = (s.replace("\xa0", "").replace(" ", "").replace("\u2009", "")
          .replace("₽", "").replace("KZT", "").replace("$", ""))
    t = t.replace("−", "-")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    m = re.search(r"-?\d+(\.\d+)?", t)
    return float(m.group(0)) if m else None


def parse_next_data(html: str) -> dict:
    """Метаданные портфеля из SSR-блока __NEXT_DATA__ (работают без браузера)."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        p = data["props"]["pageProps"]["portfolio"] or {}
    except Exception:
        return {}
    return {
        "name": p.get("name", ""),
        "transactions_count": p.get("transactionsCount"),
        "last_transaction_date": (p.get("lastTransactionDate") or "")[:10],
        "asset_count": p.get("assetCount"),
        "hide_transactions": p.get("hideTransactions"),
    }


# --------------------------------------------------------------------------- #
#  Извлечение JSON из ответа API
# --------------------------------------------------------------------------- #
def extract_rows(payload: Any) -> list[dict]:
    """Ищет в произвольном JSON самый длинный список объектов-сделок."""
    best: list[dict] = []

    def walk(o):
        nonlocal best
        if isinstance(o, list):
            dicts = [x for x in o if isinstance(x, dict)]
            if len(dicts) > len(best) and dicts and _looks_like_tx(dicts[0]):
                best = dicts
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)

    walk(payload)
    return best


def _looks_like_tx(d: dict) -> bool:
    keys = {k.lower() for k in d}
    return bool(keys & {"ticker", "isin", "name", "description", "quantity",
                        "qty", "price", "portfoliooperationtype", "type",
                        "transactiontype", "date", "amount", "commission"})


def format_money(value: float, with_rub: bool = True) -> str:
    """1736.81 -> '1 736,81 ₽' (русский формат: пробелы-тысячи, запятая)."""
    sign = "-" if value < 0 else ("+" if value > 0 else "")
    s = f"{abs(value):,.2f}".replace(",", " ").replace(".", ",")
    return f"{sign}{s}" + (" ₽" if with_rub else "")


def transaction_from_json(d: dict, portfolio: str) -> Transaction:
    def get(*names, default=""):
        for n in names:
            if n in d and d[n] not in (None, ""):
                return d[n]
        return default

    raw_type = str(get("transactionType", "portfolioOperationType", "type", "operationType",
                       "operation", "status"))
    tmap = {"0": "Покупка", "1": "Продажа", "buy": "Покупка", "purchase": "Покупка",
            "sell": "Продажа", "2": "Купон", "3": "Дивиденд"}
    operation = tmap.get(raw_type.lower(), raw_type)
    for k in ("operationName", "transactionTypeName", "typeTitle", "title"):
        if get(k):
            operation = str(get(k))
            break
    qty = get("quantity", "qty", "count", "sharesCount")
    price = get("price", "pricePerShare", "pricePerUnit")
    amount = get("portfolioValue", "totalValue", "amount", "sum", "total")
    comm = get("commission", "fees", "fee", "commissionValue")
    date = get("date", "transactionDate", "tradeDate", "execDate")
    if isinstance(date, str):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date)
        if m:
            date = f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
    isin = get("isin", "assetIsin", "securityIsin")
    name = get("name", "assetName", "ticker", "securityName", "description", "isin")
    profit_pct = get("profitability", "profitPercent", "yieldPct", "profitabilityPct")
    profit_abs = get("profit", "profitValue", "absoluteProfit", "profitAbs")
    if profit_abs and profit_abs != "-":
        n = _to_number(str(profit_abs))
        if n is not None:
            profit_abs = format_money(n)
    return Transaction(
        operation=str(operation), asset=str(name), isin=str(isin), date=str(date),
        qty=str(qty), price=str(price), commission=str(comm), amount=str(amount),
        profit_pct=(f"{profit_pct}%" if profit_pct not in ("", None) and "%" not in str(profit_pct)
                    else str(profit_pct or "")),
        profit_abs=str(profit_abs or ""), portfolio=portfolio,
    )


# --------------------------------------------------------------------------- #
#  Клиент сайта
# --------------------------------------------------------------------------- #
class Site:
    def __init__(self, share_key: str, source: str, api_template: dict | None,
                 pages: int = 1, use_browser: bool = True):
        self.share_key = share_key
        self.source = source
        self.api = api_template or {}
        self.pages = max(1, pages)
        self.use_browser = use_browser
        self.page_url = f"{BASE_URL}/public/portfolios/{share_key}"
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})

    # ---- низкоуровневое получение HTML ---------------------------------- #
    def fetch_html_http(self) -> str:
        r = self.s.get(self.page_url, timeout=40)
        r.raise_for_status()
        return r.text

    def fetch_html_browser(self) -> str:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch(args=["--no-sandbox"])
            pg = b.new_page(user_agent=UA)
            pg.goto(self.page_url, wait_until="networkidle", timeout=90_000)
            pg.wait_for_timeout(1500)
            html = pg.content()
            b.close()
        return html

    # ---- API -------------------------------------------------------------- #
    def _api_url_and_params(self, page: int) -> tuple[str, dict]:
        tpl_url: str = self.api.get("url", "")
        params = dict(self.api.get("params", {}) or {})
        blob = json.dumps(params, ensure_ascii=False)
        if "SHARE_KEY" in blob or "portfolioId" in params:
            params = json.loads(blob.replace("SHARE_KEY", self.share_key))
        for k, v in list(params.items()):
            if isinstance(v, str) and v.lower().startswith("page"):
                params[k] = page
            if k.lower() in ("page", "pagenumber", "pageindex"):
                params[k] = page
        params.setdefault("page", page)
        return tpl_url, params

    def fetch_via_api(self) -> tuple[list[Transaction], dict]:
        url, params = self._api_url_and_params(1)
        txs: list[Transaction] = []
        meta = {}
        method = (self.api.get("method") or "GET").upper()
        headers = {"Referer": self.page_url, "Accept": "application/json"}
        for page in range(1, self.pages + 1):
            _, p = self._api_url_and_params(page)
            if method == "POST":
                r = self.s.post(url, json=p, headers=headers, timeout=40)
            else:
                r = self.s.request(method, url, params=p, headers=headers, timeout=40)
            if r.status_code != 200:
                raise RuntimeError(f"API вернул {r.status_code} для {url}")
            payload = r.json()
            rows = extract_rows(payload)
            if not rows:
                break
            if isinstance(payload, dict):
                meta = {k: payload[k] for k in ("totalCount", "totalSize") if k in payload}
            name = self.api.get("portfolio_name", "")
            for d in rows:
                txs.append(transaction_from_json(d, name))
            if len(rows) < int(p.get("pageSize", 25) or 25):
                break
        return txs, meta

    # ---- HTML ------------------------------------------------------------- #
    def fetch_via_html(self) -> tuple[list[Transaction], dict]:
        html = ""
        if self.use_browser:
            try:
                html = self.fetch_html_browser()
            except Exception as e:  # noqa: BLE001
                log.warning("Playwright недоступен (%s), пробую обычный HTTP", e)
        if not html:
            html = self.fetch_html_http()
        meta = parse_next_data(html)
        name = meta.get("name") or ""
        txs = parse_tx_table(html)
        for t in txs:
            t.portfolio = name
        return txs, meta

    # ---- общая точка входа ------------------------------------------------ #
    def fetch(self) -> tuple[list[Transaction], dict]:
        api_error = None
        if self.source == "api" and self.api.get("url"):
            try:
                txs, meta = self.fetch_via_api()
            except Exception as e:  # noqa: BLE001
                api_error = e
                txs, meta = [], {}
            if txs:
                try:
                    m2 = parse_next_data(self.fetch_html_http())
                    m2.update(meta)
                    meta = m2
                except Exception:  # noqa: BLE001
                    pass
                return txs, meta
            log.warning("API не дал сделок%s — пробую HTML",
                        f" ({e})" if (e := api_error) else "")
        return self.fetch_via_html()


# --------------------------------------------------------------------------- #
#  Состояние (дедупликация)
# --------------------------------------------------------------------------- #
class State:
    def __init__(self, path: Path, max_seen: int = 2000):
        self.path = path
        self.max_seen = max(200, int(max_seen))
        self.data = {"seen": {}, "meta": {}, "started": int(time.time())}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                log.warning("Файл состояния повреждён, начинаю с чистого листа")
        self.data.setdefault("seen", {})
        self.data.setdefault("meta", {})

    def load_template(self) -> dict | None:
        return self.data.get("api_template")

    def save_template(self, tpl: dict) -> None:
        self.data["api_template"] = tpl
        self._flush()

    def first_run(self) -> bool:
        return self.data.get("meta", {}).get("baseline_done") is not True

    def mark_baseline(self) -> None:
        self.data["meta"]["baseline_done"] = True
        self._flush()

    def filter_new(self, txs: list[Transaction], pages_scanned: int) -> list[Transaction]:
        """Сделки, которых не было в предыдущем снимке.

        seen = старые отпечатки + текущие, с LRU-обрезкой по max_seen.
        Порядок dict сохраняется при слиянии, поэтому обрезаются именно самые
        давние записи: строка, на цикл выпавшая из выдачи (сбой API, сортировка),
        не будет повторно распознана как новая сделка.
        """
        fresh = [t for t in txs if t.key not in self.data["seen"]]
        merged = dict(self.data["seen"])
        merged.update({t.key: 1 for t in txs})
        keep = list(merged)
        if len(keep) > self.max_seen:
            keep = keep[-self.max_seen:]
        self.data["seen"] = {k: 1 for k in keep}
        return fresh

    def update_meta(self, meta: dict) -> None:
        self.data["meta"].update({k: v for k, v in meta.items() if v is not None})
        self.data["meta"]["last_check"] = int(time.time())
        self.data["meta"]["checks"] = int(self.data["meta"].get("checks", 0)) + 1

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def save(self) -> None:
        self._flush()


# --------------------------------------------------------------------------- #
#  Telegram
# --------------------------------------------------------------------------- #
class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}"
        self.s = requests.Session()

    def call(self, method: str, **kw):
        r = self.s.post(f"{self.api}/{method}", data=kw, timeout=45)
        try:
            j = r.json()
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"{method}: HTTP {r.status_code}: {r.text[:200]}")
        if not j.get("ok"):
            desc = j.get("description", "")
            if "retry_after" in desc.lower() or "Too Many Requests" in desc:
                wait = re.search(r"after (\d+)", desc)
                time.sleep(int(wait.group(1)) + 1 if wait else 2)
                return self.call(method, **kw)
            raise RuntimeError(f"{method}: {desc}")
        return j["result"]

    def send(self, chat_id: str, text: str, disable_preview: bool = False):
        return self.call("sendMessage", chat_id=chat_id, text=text,
                         parse_mode="HTML", disable_web_page_preview="true",
                         disable_notification="true" if disable_preview else "false")

    def get_updates(self, offset: int = 0):
        return self.call("getUpdates", offset=offset, timeout=25,
                         allowed_updates='["message"]')



# --------------------------------------------------------------------------- #
#  Импорт реального запроса из DevTools («Copy as cURL»)
# --------------------------------------------------------------------------- #
_curl_re = re.compile(r"curl", re.M)


def parse_curl(text: str, share_key: str = "") -> dict:
    """Превращает «Copy as cURL» из DevTools в шаблон запроса для state.json.

    Поддерживает: -X/--request, -H/--header (Content-Type), --data/-d/--data-raw,
    -G/--get, 'url'. Тело запроса и query-строка разбираются в params.
    """
    m = re.search(r"curl\s", text)
    if not m:
        raise ValueError("в тексте нет команды curl — скопируйте её целиком")
    body = text[m.start():]
    # склеиваем переносы строк с обратным слэшем
    body = re.sub(r"\\\s*\n\s*", " ", body)
    method = "GET"
    url = ""
    headers: dict[str, str] = {}
    data = None
    force_get = False
    tokens = shlex.split(body)[1:]
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-X", "--request") and i + 1 < len(tokens):
            method = tokens[i + 1].upper(); i += 2; continue
        if t in ("-H", "--header") and i + 1 < len(tokens):
            k, _, v = tokens[i + 1].partition(":")
            headers[k.strip().lower()] = v.strip(); i += 2; continue
        if t in ("--data", "--data-raw", "--data-binary", "--data-urlencode", "-d") \
                and i + 1 < len(tokens):
            data = tokens[i + 1]; i += 2; continue
        if t in ("-G", "--get"):
            force_get = True; i += 1; continue
        if t.startswith("-"):
            i += 1; continue
        if not url and t.startswith("http"):
            url = t
        i += 1
    if not url:
        raise ValueError("в команде curl не нашёлся URL")

    parsed = urlparse(url)
    params: dict[str, Any] = {k: (v[0] if len(v) == 1 else v)
                             for k, v in parse_qs(parsed.query).items()}
    if data:
        try:
            params.update(json.loads(data))
        except Exception:  # noqa: BLE001
            params.update({k: (v[0] if len(v) == 1 else v)
                           for k, v in parse_qs(data).items()})
    if force_get and data:
        method = "GET"
    if not params:
        raise ValueError("не удалось извлечь параметры из curl")
    if share_key:
        params = {k: (v.replace(share_key, "SHARE_KEY") if isinstance(v, str) else v)
                  for k, v in params.items()}
    url_clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    tpl = {"method": method, "url": url_clean, "params": params}
    if headers.get("content-type"):
        tpl["content_type"] = headers["content-type"]
    return tpl


# --------------------------------------------------------------------------- #
#  Автоопределение эндпоинта (Playwright + перехват сети)
# --------------------------------------------------------------------------- #
def discover_api(share_key: str, headful: bool = False) -> dict | None:
    """Открывает страницу портфеля, проваливается на вкладку «Сделки» и
    вытаскивает реальный запрос к api/public/transactions."""
    from playwright.sync_api import sync_playwright

    captured: list[dict] = []
    url_re = re.compile(r"/api/public/transactions", re.I)

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=not headful, args=["--no-sandbox"])
        pg = b.new_page(user_agent=UA)

        def on_req(req):
            if url_re.search(req.url):
                body = req.post_data
                params = {k: v[0] for k, v in parse_qs(urlparse(req.url).query).items()}
                captured.append({
                    "method": req.method,
                    "url": req.url.split("?")[0],
                    "params": params,
                    "post_body": json.loads(body) if body else None,
                })

        pg.on("request", on_req)
        pg.goto(f"{BASE_URL}/public/portfolios/{share_key}",
                wait_until="networkidle", timeout=90_000)
        try:
            pg.get_by_role("link", name="Сделки").first.click(timeout=5000)
            pg.wait_for_timeout(2500)
        except Exception:  # noqa: BLE001
            pass
        meta = parse_next_data(pg.content())
        b.close()

    if not captured:
        return None
    tpl = captured[0]
    if tpl.get("post_body"):
        tpl["params"] = tpl.pop("post_body")
    # заменяем id портфеля на плейсхолдер, чтобы шаблон был переносимым
    def sub(v):
        if isinstance(v, str):
            for cand in (share_key,):
                if cand in v:
                    return v.replace(cand, "SHARE_KEY")
        return v
    tpl["params"] = {k: sub(v) for k, v in (tpl.get("params") or {}).items()}
    tpl["portfolio_name"] = meta.get("name", "")
    return tpl


# --------------------------------------------------------------------------- #
#  Основной цикл
# --------------------------------------------------------------------------- #
class Bot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = State(Path(cfg["state_file"]), cfg.get("max_seen", 2000))
        self.tg = Telegram(cfg["bot_token"]) if cfg["bot_token"] else None
        self.site = Site(
            share_key=cfg["share_key"],
            source=cfg["source"],
            api_template=self.state.load_template(),
            pages=cfg["pages"],
            use_browser=cfg["use_browser"],
        )
        self.chat_ids = [c for c in str(cfg["chat_ids"]).split(",") if c.strip()]
        self.offset = 0
        self.stop = False

    # ---- команды ---------------------------------------------------------- #
    def handle_update(self, upd: dict) -> None:
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        cid = str(chat.get("id", ""))
        if not cid:
            return
        cmd = text.split("@")[0].lower()
        if cmd in ("/start", "/start_monitoring"):
            if cid not in self.chat_ids:
                self.chat_ids.append(cid)
                self.cfg["chat_ids"] = ",".join(self.chat_ids)
                self._persist_env(cid)
            self.tg.send(cid,
                         "Готово 👍 Я буду присылать каждую новую покупку и продажу.\n\n"
                         f"Портфель: {self.site.page_url}\n"
                         f"Интервал проверки: {self.cfg['interval']} сек\n"
                         "Команды: /status · /test · /interval <сек> · /stop · /id")
        elif cmd == "/stop":
            if cid in self.chat_ids:
                self.chat_ids.remove(cid)
                self.cfg["chat_ids"] = ",".join(self.chat_ids)
                self._persist_env(cid, remove=True)
            self.tg.send(cid, "Отписал этот чат. /start — подписаться снова.")
        elif cmd == "/status":
            m = self.state.data.get("meta", {})
            self.tg.send(cid,
                         "<b>Статус</b>\n"
                         f"Источник: {self.cfg['source']}\n"
                         f"Подписчиков: {len(self.chat_ids)}\n"
                         f"Проверок: {m.get('checks', 0)}\n"
                         f"Всего сделок у портфеля: {m.get('transactions_count', '?')}\n"
                         f"Последняя дата сделки: {m.get('last_transaction_date', '?')}\n"
                         f"Последняя проверка: {time.strftime('%d.%m %H:%M', time.localtime(m.get('last_check', 0))) if m.get('last_check') else '—'}\n"
                         f"Отпечатков в памяти: {len(self.state.data.get('seen', {}))}")
        elif cmd == "/test":
            try:
                txs, _ = self.site.fetch()
                last = txs[0] if txs else None
                self.tg.send(cid, last.to_message(self.site.page_url) if last
                             else "Не удалось получить список сделок.")
            except Exception as e:  # noqa: BLE001
                self.tg.send(cid, f"Ошибка теста: {esc(str(e)[:600])}")
        elif cmd.startswith("/interval"):
            arg = cmd and text.split()
            if len(arg) >= 2 and arg[1].isdigit():
                self.cfg["interval"] = max(60, int(arg[1]) * 1)
                self.tg.send(cid, f"Новый интервал: {self.cfg['interval']} сек.")
            else:
                self.tg.send(cid, "Используйте: /interval 300  (секунды, минимум 60)")
        elif cmd == "/id":
            title = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
            self.tg.send(cid,
                         f"<b>chat_id этого чата:</b> <code>{esc(cid)}</code>"
                         + (f"\n<b>Название:</b> {esc(str(title))}" if title else "")
                         + "\n\nЕго можно вписать в .env как TELEGRAM_CHAT_IDS "
                           "или использовать при ручном заполнении state/env.")
        elif cmd in ("/help", "/help_monitoring"):
            self.tg.send(cid, "/start — подписаться\n/stop — отписаться\n"
                              "/status — состояние\n/test — прислать последнюю сделку\n"
                              "/interval <сек> — частота опроса\n/id — chat_id этого чата")
        elif cmd.startswith("/"):
            self.tg.send(cid, "Не знаю такую команду. /help — список.")

    def _persist_env(self, cid: str, remove: bool = False) -> None:
        """Сохраняем список чатов в .env, чтобы пережить рестарт контейнера."""
        env = Path(__file__).with_name(".env")
        if not env.exists():
            return
        try:
            lines = env.read_text(encoding="utf-8").splitlines()
            cur = [l for l in lines if l.startswith("TELEGRAM_CHAT_IDS=")]
            if cur:
                lines[lines.index(cur[0])] = f"TELEGRAM_CHAT_IDS={','.join(self.chat_ids)}"
                env.write_text("\n".join(lines) + "\n", encoding="utf-8")
                log.info("CHAT_ID сохранён в .env")
        except Exception as e:  # noqa: BLE001
            log.warning("Не смог записать .env: %s", e)

    # ---- рассылка --------------------------------------------------------- #
    def broadcast(self, text: str, silent: bool = False) -> None:
        if not self.tg:
            log.info("tg> %s", text.replace("\n", " | ")[:300])
            return
        for cid in self.chat_ids:
            try:
                self.tg.send(cid, text, disable_preview=silent)
            except Exception as e:  # noqa: BLE001
                log.error("Не удалось отправить в %s: %s", cid, e)
            time.sleep(0.05)

    # ---- один цикл -------------------------------------------------------- #
    def check_once(self) -> int:
        try:
            txs, meta = self.site.fetch()
        except Exception as e:  # noqa: BLE001
            log.error("Ошибка выборки: %s", e)
            self.state.update_meta({"last_error": str(e)[:300]})
            self.state.save()
            return -1
        self.state.update_meta(meta)
        self.state.data["meta"]["source"] = self.cfg["source"]

        if not txs:
            # Пустая выдача — это не «нет новых сделок», а сломанный источник
            # (сменилась вёрстка / не поднят браузер / сдох API).
            # Baseline в таком случае ставить нельзя, иначе потом шквал уведомлений.
            msg = ("Источник не вернул ни одной сделки — уведомления не рассылаются.\n"
                   f"source={self.cfg['source']}, use_browser={self.cfg['use_browser']}. "
                   "Проверьте `python watcher.py --dump`.")
            self.state.data["meta"]["last_error"] = "empty result"
            self.state.save()
            if not self.state.data["meta"].get("empty_warned_at") or \
               time.time() - self.state.data["meta"]["empty_warned_at"] > 6 * 3600:
                self.state.data["meta"]["empty_warned_at"] = int(time.time())
                self.state.save()
                self.broadcast("⚠️ <b>Мониторинг не видит сделок</b>\n\n" + esc(msg))
            log.warning(msg)
            return -1

        if self.state.first_run():
            self.state.filter_new(txs, self.cfg["pages"])   # снимок + pruning
            self.state.mark_baseline()
            self.state.save()
            log.info("Первичное сохранение: %d сделок приняты как уже известные", len(txs))
            n = meta.get("transactions_count")
            self.broadcast(
                "✅ Мониторинг запущен\n\n"
                f"Портфель: <b>{esc(meta.get('name') or self.site.share_key)}</b>\n"
                f"Всего сделок: {n}\n"
                f"Последняя дата сделки: {esc(str(meta.get('last_transaction_date')))}\n"
                f"Источник данных: {self.cfg['source']}\n\n"
                "Дальше присылаю только новые покупки и продажи.")
            return 0

        new = self.state.filter_new(txs, self.cfg["pages"])
        if new:
            log.info("Новых сделок: %d", len(new))
            limit = self.cfg.get("max_new_per_check", 10)
            for t in reversed(new[:limit]):      # сначала самые ранние из новых
                self.broadcast(t.to_message(self.site.page_url))
            if len(new) > limit:
                self.broadcast(f"…и ещё {len(new) - limit} новых сделок "
                               f"(см. {self.site.page_url}#transactions)")
        self.state.save()
        return len(new)

    # ---- поллинг команд --------------------------------------------------- #
    def poll_telegram(self) -> None:
        if not self.tg:
            return
        try:
            for upd in self.tg.get_updates(self.offset):
                self.offset = max(self.offset, upd["update_id"] + 1)
                try:
                    self.handle_update(upd)
                except Exception as e:  # noqa: BLE001
                    log.error("Обработка апдейта: %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("getUpdates: %s", e)

    def run(self) -> None:
        log.info("Старт. Портфель %s, источник %s, интервал %ds",
                 self.site.share_key, self.cfg["source"], self.cfg["interval"])
        if not self.chat_ids:
            log.info("TELEGRAM_CHAT_IDS пуст — отправьте /start боту в Telegram")
        next_check = 0.0
        while True:
            if time.time() >= next_check:
                try:
                    self.check_once()
                except Exception as e:  # noqa: BLE001
                    log.exception("Цикл проверки: %s", e)
                next_check = time.time() + self.cfg["interval"]
            self.poll_telegram()
            time.sleep(1)


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cfg_from_env(cli: argparse.Namespace) -> dict:
    env = os.environ.get
    url = cli.url or env("PORTFOLIO_URL", "")
    key = cli.share_key or env("PORTFOLIO_SHARE_KEY", "")
    if not key and url:
        m = re.search(r"portfolios/([A-Za-z0-9]+)", url)
        key = m.group(1) if m else ""
    return {
        "share_key": key,
        "bot_token": cli.token or env("TELEGRAM_BOT_TOKEN", ""),
        "chat_ids": cli.chat or env("TELEGRAM_CHAT_IDS", ""),
        "interval": int(cli.interval or env("CHECK_INTERVAL_SEC", "300")),
        "source": cli.source or env("DATA_SOURCE", "api"),
        "pages": int(env("SCAN_PAGES", "1")),
        "max_new_per_check": int(env("MAX_NEW_PER_CHECK", "10")),
        "max_seen": int(env("MAX_SEEN_KEYS", "2000")),
        "use_browser": env("USE_BROWSER", "1") not in ("0", "false", "no"),
        "state_file": cli.state or env("STATE_FILE", "state.json"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Монитор сделок snowball-income.com → Telegram")
    ap.add_argument("--url", help="https://snowball-income.com/public/portfolios/<key>")
    ap.add_argument("--share-key", help="публичный ключ портфеля из URL")
    ap.add_argument("--token", help="токен бота от @BotFather")
    ap.add_argument("--chat", help="chat_id через запятую (или отправьте боту /start)")
    ap.add_argument("--interval", type=int, help="период проверки, сек (по умолч. 300)")
    ap.add_argument("--source", choices=["api", "html"], help="источник данных")
    ap.add_argument("--state", help="файл состояния (по умолч. state.json)")
    ap.add_argument("--once", action="store_true", help="одна проверка и выход")
    ap.add_argument("--discover", action="store_true",
                    help="перехватить реальный запрос к API через Playwright и сохранить шаблон")
    ap.add_argument("--from-curl", metavar="FILE",
                    help="взять шаблон запроса из файла с 'Copy as cURL' (DevTools)")
    ap.add_argument("--dump", action="store_true", help="напечатать разобранные сделки")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_env(Path(__file__).with_name(".env"))
    cfg = cfg_from_env(a)
    if not cfg["share_key"]:
        ap.error("не задан портфель: --url или PORTFOLIO_URL в .env")

    if a.from_curl:
        raw = Path(a.from_curl).read_text(encoding="utf-8")
        tpl = parse_curl(raw, cfg["share_key"])
        tpl.setdefault("portfolio_name", State(Path(cfg["state_file"])).data["meta"].get("name", ""))
        State(Path(cfg["state_file"])).save_template(tpl)
        print(json.dumps(tpl, ensure_ascii=False, indent=2))
        log.info("Шаблон сохранён в %s. Проверьте: --source api --dump", cfg["state_file"])
        return 0

    if a.discover:
        log.info("Определяю эндпоинт API через headless-браузер…")
        tpl = discover_api(cfg["share_key"])
        if not tpl:
            log.error("Запрос к /api/public/transactions не пойман. "
                      "Откройте сайт вручную в DevTools → Network и скопируйте его в state.json")
            return 2
        State(Path(cfg["state_file"])).save_template(tpl)
        print(json.dumps(tpl, ensure_ascii=False, indent=2))
        log.info("Шаблон сохранён в %s → api_template. Теперь: --source api", cfg["state_file"])
        return 0

    bot = Bot(cfg)

    if a.dump:
        txs, meta = bot.site.fetch()
        log.info("meta=%s", meta)
        for t in txs:
            print(json.dumps(asdict(t), ensure_ascii=False))
        print(f"\nВсего распаршено сделок: {len(txs)}")
        return 0 if txs else 1

    if a.once:
        n = bot.check_once()
        # result: new=<число|0>, error=<текст> — читается run_once.py
        err = bot.state.data["meta"].get("last_error", "")
        if n >= 0:
            err = ""
        print(f"RESULT new={max(n, 0)} error={json.dumps(err, ensure_ascii=False)}")
        print("новых сделок:", n)
        return 0 if n >= 0 else 1

    bot.run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nОстановлено")
        sys.exit(0)
