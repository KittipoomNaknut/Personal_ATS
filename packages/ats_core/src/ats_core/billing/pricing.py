"""Exact cost of a Claude API call, from its reported token usage.

All money is :class:`~decimal.Decimal`; nothing is rounded until it is shown to a
person. The model used for pricing is the one that *answered* (``response.model``),
which can differ from the one requested when a server-side fallback happens.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from importlib import resources
from typing import Any, Final

__all__ = [
    "CostBreakdown",
    "ModelPrices",
    "PriceTable",
    "PricingError",
    "Usage",
    "default_price_table",
]

_PER_TOKEN: Final = Decimal(1_000_000)


class PricingError(ValueError):
    """A call cannot be priced reliably (unknown model, unpublished price, bad data)."""


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts as reported by the API's ``usage`` object.

    ``input_tokens`` excludes cached tokens, as in the API. ``output_tokens``
    is what the API reports and bills, thinking included.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_write_5m_tokens",
            "cache_write_1h_tokens",
            "cache_read_tokens",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PricingError(f"{name} must be a non-negative integer, got {value!r}")

    @property
    def prompt_tokens(self) -> int:
        """Every input token of the request, as used for long-context tiers."""
        return (
            self.input_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
            + self.cache_read_tokens
        )


@dataclass(frozen=True, slots=True)
class _Rates:
    input: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal
    output: Decimal


@dataclass(frozen=True, slots=True)
class ModelPrices:
    id: str
    name: str
    standard: _Rates
    long_context: _Rates | None = None
    long_context_threshold: int | None = None

    def rates_for(self, usage: Usage) -> _Rates:
        if (
            self.long_context is not None
            and self.long_context_threshold is not None
            and usage.prompt_tokens > self.long_context_threshold
        ):
            return self.long_context
        return self.standard


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    model: str
    pricing_version: str
    input: Decimal
    cache_write: Decimal
    cache_read: Decimal
    output: Decimal
    long_context: bool

    @property
    def total(self) -> Decimal:
        return self.input + self.cache_write + self.cache_read + self.output


def _decimal(value: Any, where: str) -> Decimal:
    if not isinstance(value, str):
        # Prices are strings in the TOML so they are never parsed as binary floats.
        raise PricingError(f"{where}: price must be a quoted string, got {value!r}")
    try:
        price = Decimal(value)
    except ArithmeticError as exc:
        raise PricingError(f"{where}: not a number: {value!r}") from exc
    if not price.is_finite() or price < 0:
        raise PricingError(f"{where}: price must be finite and non-negative: {value!r}")
    return price


def _rates(entry: dict[str, Any], where: str) -> _Rates:
    try:
        return _Rates(
            input=_decimal(entry["input"], f"{where}.input"),
            cache_write_5m=_decimal(entry["cache_write_5m"], f"{where}.cache_write_5m"),
            cache_write_1h=_decimal(entry["cache_write_1h"], f"{where}.cache_write_1h"),
            cache_read=_decimal(entry["cache_read"], f"{where}.cache_read"),
            output=_decimal(entry["output"], f"{where}.output"),
        )
    except KeyError as exc:
        raise PricingError(f"{where}: missing price {exc}") from exc


class PriceTable:
    def __init__(
        self,
        version: str,
        models: dict[str, ModelPrices],
        geo_multipliers: dict[str, Decimal],
    ) -> None:
        if not version:
            raise PricingError("price table needs a version")
        self.version = version
        self._models = models
        self._geo = geo_multipliers

    @classmethod
    def from_toml(cls, text: str) -> PriceTable:
        data = tomllib.loads(text)
        if data.get("currency") != "USD":
            raise PricingError("price table currency must be USD")
        models: dict[str, ModelPrices] = {}
        for entry in data.get("model", []):
            model_id = str(entry.get("id", ""))
            if not model_id:
                raise PricingError("model entry without id")
            if model_id in models:
                raise PricingError(f"duplicate model {model_id}")
            long_entry = entry.get("long_context")
            models[model_id] = ModelPrices(
                id=model_id,
                name=str(entry.get("name", model_id)),
                standard=_rates(entry, model_id),
                long_context=_rates(long_entry, f"{model_id}.long_context") if long_entry else None,
                long_context_threshold=int(long_entry["threshold_tokens"]) if long_entry else None,
            )
        geo = {
            region: _decimal(value, f"geo_multiplier.{region}")
            for region, value in data.get("geo_multiplier", {"global": "1"}).items()
        }
        return cls(str(data.get("version", "")), models, geo)

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(self._models)

    def __contains__(self, model: object) -> bool:
        return model in self._models

    def prices(self, model: str) -> ModelPrices:
        try:
            return self._models[model]
        except KeyError:
            raise PricingError(f"no price for model {model!r} in table {self.version}") from None

    def cost(
        self,
        model: str,
        usage: Usage,
        *,
        inference_geo: str = "global",
        speed: str = "standard",
        batch: bool = False,
    ) -> CostBreakdown:
        """Cost of one call. Raises :class:`PricingError` rather than guessing."""
        if speed != "standard":
            raise PricingError(f"speed {speed!r}: cache prices are not published; not supported")
        if batch:
            raise PricingError("Batch API cache prices are not published; not supported")
        try:
            multiplier = self._geo[inference_geo]
        except KeyError:
            raise PricingError(f"no price multiplier for inference_geo {inference_geo!r}") from None
        prices = self.prices(model)
        rates = prices.rates_for(usage)

        def charge(tokens: int, rate: Decimal) -> Decimal:
            return Decimal(tokens) * rate * multiplier / _PER_TOKEN

        return CostBreakdown(
            model=model,
            pricing_version=self.version,
            input=charge(usage.input_tokens, rates.input),
            cache_write=charge(usage.cache_write_5m_tokens, rates.cache_write_5m)
            + charge(usage.cache_write_1h_tokens, rates.cache_write_1h),
            cache_read=charge(usage.cache_read_tokens, rates.cache_read),
            output=charge(usage.output_tokens, rates.output),
            long_context=rates is not prices.standard,
        )


@cache
def default_price_table() -> PriceTable:
    text = resources.files(__package__).joinpath("pricing.toml").read_text(encoding="utf-8")
    return PriceTable.from_toml(text)
