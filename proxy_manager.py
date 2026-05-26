"""
proxy_manager.py
~~~~~~~~~~~~~~~~
Proxy pool for 2captcha.com proxies.

Proxies берутся из личного кабинета 2captcha.com → раздел Proxy.
Формат из ЛК:  http://host:port:login:password
               https://host:port:login:password
               socks5://host:port:login:password

Также поддерживаются стандартные форматы:
               host:port:login:password
               login:password@host:port
               host:port  (без авторизации)

Использование
-------------
# Один прокси (ротируется автоматически при каждом запросе)
pm = ProxyManager("http://1.2.3.4:8080:mylogin:mypass")

# Несколько прокси из файла (один на строку)
pm = ProxyManager.from_file("proxies.txt")

# Список строк
pm = ProxyManager.from_list([
    "http://1.2.3.4:8080:login:pass",
    "http://5.6.7.8:3128:login:pass",
])
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ProxyStats:
    success: int = 0
    fail: int = 0
    ban_count: int = 0
    last_used: float = 0.0

    @property
    def success_rate(self) -> float:
        total = self.success + self.fail
        return self.success / total if total else 1.0

    @property
    def is_burned(self) -> bool:
        """Прокси считается сгоревшим после 3 банов подряд."""
        return self.ban_count >= 3


@dataclass
class Proxy:
    host: str
    port: int
    login: str = ""
    password: str = ""
    scheme: str = "http"
    stats: ProxyStats = field(default_factory=ProxyStats)

    def as_requests_dict(self) -> dict:
        """Возвращает словарь для параметра proxies= в requests."""
        if self.login:
            url = f"{self.scheme}://{self.login}:{self.password}@{self.host}:{self.port}"
        else:
            url = f"{self.scheme}://{self.host}:{self.port}"
        return {"http": url, "https": url}

    # Оставляем старое имя как алиас для совместимости
    def as_dict(self) -> dict:
        return self.as_requests_dict()

    @classmethod
    def from_string(cls, raw: str) -> "Proxy":
        """
        Парсит прокси-строку в любом из поддерживаемых форматов.

        Форматы (в порядке приоритета):
          http://host:port:login:password    ← формат 2captcha ЛК
          https://host:port:login:password
          socks5://host:port:login:password
          host:port:login:password           ← без схемы
          login:password@host:port           ← стандартный URL-формат
          host:port                          ← без авторизации
        """
        raw = raw.strip()
        if not raw:
            raise ValueError("Empty proxy string")

        # Извлекаем схему если есть
        scheme = "http"
        scheme_match = re.match(r"^(https?|socks5)://", raw, re.I)
        if scheme_match:
            scheme = scheme_match.group(1).lower()
            raw = raw[len(scheme_match.group(0)):]  # убираем "http://" из строки

        # Теперь raw это одно из:
        #   host:port:login:password
        #   login:password@host:port
        #   host:port

        if "@" in raw:
            # login:password@host:port
            credentials, address = raw.rsplit("@", 1)
            login, _, password = credentials.partition(":")
            host, _, port_s = address.rpartition(":")
        else:
            parts = raw.split(":")
            if len(parts) == 4:
                # host:port:login:password  ← формат 2captcha
                host, port_s, login, password = parts
            elif len(parts) == 2:
                # host:port
                host, port_s = parts
                login = password = ""
            else:
                raise ValueError(
                    f"Cannot parse proxy {raw!r}. "
                    "Expected: http://host:port:login:pass  or  host:port:login:pass"
                )

        return cls(
            host=host.strip(),
            port=int(port_s.strip()),
            login=login.strip(),
            password=password.strip(),
            scheme=scheme,
        )

    def __str__(self) -> str:
        auth = f"{self.login}:***@" if self.login else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"


class ProxyManager:
    """
    Пул прокси с ротацией и health-tracking.

    При бане прокси уходит на COOLDOWN_SECONDS паузу, потом возвращается.
    Прокси с success_rate < MIN_SUCCESS_RATE исключаются из ротации.
    """

    COOLDOWN_SECONDS = 60
    MIN_SUCCESS_RATE = 0.20

    def __init__(self, proxy_string: str = ""):
        """
        proxy_string: одна строка прокси в формате 2captcha ЛК:
            http://host:port:login:password
        Для нескольких прокси используйте from_list() или from_file().
        """
        self._proxies: list[Proxy] = []
        self._lock = Lock()
        self._index = 0

        if proxy_string:
            self.add(proxy_string)

    # ------------------------------------------------------------------ #
    # Фабричные методы
    # ------------------------------------------------------------------ #

    @classmethod
    def from_list(cls, proxy_strings: list[str]) -> "ProxyManager":
        """Создаёт менеджер из списка строк."""
        pm = cls()
        for s in proxy_strings:
            pm.add(s)
        return pm

    @classmethod
    def from_file(cls, path: str) -> "ProxyManager":
        """
        Загружает прокси из файла — один прокси на строку.
        Строки начинающиеся с # игнорируются.

        Файл можно выгрузить прямо из ЛК 2captcha.com.
        """
        pm = cls()
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        loaded = 0
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if pm.add(line):
                loaded += 1
        logger.info("Loaded %d proxies from %s", loaded, path)
        return pm

    # ------------------------------------------------------------------ #
    # Добавление
    # ------------------------------------------------------------------ #

    def add(self, proxy_string: str) -> bool:
        """Добавляет один прокси. Возвращает True при успехе."""
        try:
            proxy = Proxy.from_string(proxy_string)
            with self._lock:
                self._proxies.append(proxy)
            logger.debug("Added proxy: %s", proxy)
            return True
        except Exception as exc:
            logger.warning("Cannot parse proxy %r: %s", proxy_string, exc)
            return False

    # ------------------------------------------------------------------ #
    # Ротация
    # ------------------------------------------------------------------ #

    def get(self) -> Optional[Proxy]:
        """
        Возвращает следующий живой прокси (round-robin).
        При каждом вызове — следующий из списка.
        """
        with self._lock:
            healthy = self._healthy_proxies()
            if not healthy:
                if self._proxies:
                    logger.warning(
                        "All %d proxies are on cooldown — waiting for recovery",
                        len(self._proxies),
                    )
                return None
            proxy = healthy[self._index % len(healthy)]
            self._index = (self._index + 1) % len(healthy)
            proxy.stats.last_used = time.time()
            return proxy

    def _healthy_proxies(self) -> list[Proxy]:
        now = time.time()
        result = []
        for p in self._proxies:
            if p.stats.is_burned:
                if now - p.stats.last_used < self.COOLDOWN_SECONDS:
                    continue
                else:
                    p.stats.ban_count = 0   # восстановился после паузы
            if p.stats.success_rate >= self.MIN_SUCCESS_RATE:
                result.append(p)
        return result

    # ------------------------------------------------------------------ #
    # Отчёты
    # ------------------------------------------------------------------ #

    def report_success(self, proxy: Proxy) -> None:
        proxy.stats.success += 1
        proxy.stats.ban_count = 0

    def report_fail(self, proxy: Proxy) -> None:
        proxy.stats.fail += 1

    def report_ban(self, proxy: Proxy) -> None:
        proxy.stats.fail += 1
        proxy.stats.ban_count += 1
        proxy.stats.last_used = time.time()
        logger.debug("Proxy %s banned (%dx)", proxy, proxy.stats.ban_count)

    # ------------------------------------------------------------------ #
    # Статистика
    # ------------------------------------------------------------------ #

    def stats_summary(self) -> dict:
        with self._lock:
            healthy = self._healthy_proxies()
            return {
                "total": len(self._proxies),
                "healthy": len(healthy),
                "burned": sum(1 for p in self._proxies if p.stats.is_burned),
                "avg_success_rate": (
                    sum(p.stats.success_rate for p in self._proxies) / len(self._proxies)
                    if self._proxies else 0.0
                ),
            }

    def __bool__(self) -> bool:
        return len(self._proxies) > 0
