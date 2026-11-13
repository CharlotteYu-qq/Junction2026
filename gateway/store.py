"""Offers and orders, persisted as JSON in data/runtime/ (MVP; swap for SQLite later)."""
from __future__ import annotations

from pathlib import Path

from contracts.models import Offer, Order
from trust.storage import read_json, write_json

MAX_OFFERS = 300


class Store:
    def __init__(self, runtime_dir: Path):
        self.offers_path = runtime_dir / "offers.json"
        self.orders_path = runtime_dir / "orders.json"
        self.offers: dict[str, Offer] = {o["id"]: Offer(**o) for o in read_json(self.offers_path, [])}
        self.orders: dict[str, Order] = {o["id"]: Order(**o) for o in read_json(self.orders_path, [])}

    def save_offer(self, offer: Offer) -> None:
        self.offers[offer.id] = offer
        if len(self.offers) > MAX_OFFERS:
            for key in sorted(self.offers, key=lambda k: self.offers[k].created_at)[: len(self.offers) - MAX_OFFERS]:
                self.offers.pop(key)
        write_json(self.offers_path, [o.model_dump(mode="json") for o in self.offers.values()])

    def next_order_id(self) -> str:
        return f"O-{len(self.orders) + 1:04d}"

    def save_order(self, order: Order) -> None:
        self.orders[order.id] = order
        write_json(self.orders_path, [o.model_dump(mode="json") for o in self.orders.values()])

    def list_orders(self, status: str | None = None) -> list[Order]:
        rows = [o for o in self.orders.values() if status is None or o.status == status]
        return sorted(rows, key=lambda o: o.created_at, reverse=True)

    def reset(self) -> None:
        self.offers, self.orders = {}, {}
        write_json(self.offers_path, [])
        write_json(self.orders_path, [])
