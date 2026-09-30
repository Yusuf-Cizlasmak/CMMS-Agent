"""Basit, thread-safe TTL önbellek.

Bakım verisi saniyeler içinde değişmez. Aynı soruyu (veya aynı araç+argümanı)
2 dakika içinde tekrar soran ikinci kullanıcı Elastic'e hiç gitmez.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Hashable


class TTLCache:
    def __init__(self, ttl_s: int = 120, max_items: int = 256):
        self.ttl = ttl_s
        self.max = max_items
        self._d: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Any | None:
        if self.ttl <= 0:
            return None
        with self._lock:
            item = self._d.get(key)
            if not item:
                return None
            ts, val = item
            if time.monotonic() - ts > self.ttl:
                self._d.pop(key, None)
                return None
            self._d.move_to_end(key)
            return val

    def set(self, key: Hashable, value: Any) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            self._d[key] = (time.monotonic(), value)
            self._d.move_to_end(key)
            while len(self._d) > self.max:
                self._d.popitem(last=False)
