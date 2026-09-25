"""
AENIDA Price Forecaster
TimesFM-based price forecasting — runs on WORKER PC ONLY.
BUG-20 fix: RAM gate checks 2GB+ available before loading model.
Falls back to simple trend projection if TimesFM unavailable.
"""

import logging
import math
import os
import time
from typing import Any, Dict, List, Optional

MIN_WORKER_RAM_GB = 2.0
MODEL_NAME = "google/timesfm-2.5-200m-pytorch"
FORECAST_HORIZON = 12        # 12 candles ahead
CONTEXT_LENGTH = 100         # last 100 price points


class PriceForecaster:
    """
    TimesFM wrapper.  Loads the model once and keeps it hot.
    Checks RAM before loading (BUG-20).
    """

    __slots__ = ["_model", "_available", "_loaded_at",
                 "_min_ram_gb", "_model_name"]

    def __init__(self, model_name: str = MODEL_NAME,
                 min_ram_gb: float = MIN_WORKER_RAM_GB):
        self._model = None
        self._available: bool = False
        self._loaded_at: Optional[float] = None
        self._min_ram_gb = min_ram_gb
        self._model_name = model_name
        self.load_model()

    # ── RAM gate (BUG-20) ─────────────────────────────────────────
    def _check_ram(self) -> bool:
        try:
            import psutil
            available_gb = psutil.virtual_memory().available / 1e9
            if available_gb < self._min_ram_gb:
                logging.warning(
                    f"[FORECASTER] Insufficient RAM for TimesFM: "
                    f"{available_gb:.1f}GB available, "
                    f"{self._min_ram_gb}GB required — model skipped"
                )
                return False
            return True
        except ImportError:
            logging.warning("[FORECASTER] psutil not installed — skipping RAM check")
            return True

    def load_model(self) -> None:
        """Load TimesFM model. Skips if RAM insufficient (BUG-20)."""
        if not self._check_ram():
            self._available = False
            return

        try:
            import timesfm  # type: ignore
            import torch     # type: ignore

            logging.info(f"[FORECASTER] Loading {self._model_name} ...")
            self._model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
                self._model_name
            )
            self._model.compile(
                timesfm.ForecastConfig(
                    max_context=CONTEXT_LENGTH,
                    max_horizon=FORECAST_HORIZON,
                    normalize_inputs=True,
                    use_continuous_quantile_head=True,
                    force_flip_invariance=True,
                    infer_is_positive=True,
                    fix_quantile_crossing=True,
                )
            )
            self._available = True
            self._loaded_at = time.time()
            logging.info("[FORECASTER] TimesFM loaded successfully")
        except ImportError:
            logging.warning(
                "[FORECASTER] timesfm/torch not installed — "
                "pip install timesfm torch. Using fallback projection."
            )
            self._available = False
        except Exception as e:
            logging.error(f"[FORECASTER] Model load failed: {e}")
            self._available = False

    def is_available(self) -> bool:
        return self._available

    # ── Forecasting ───────────────────────────────────────────────
    def forecast(self, price_history: List[float],
                 horizon: int = FORECAST_HORIZON) -> Dict[str, Any]:
        """
        Forecast future prices.

        Args:
            price_history: List of recent close prices (oldest first)
            horizon: Number of future points to predict

        Returns:
            Dict with point_forecast, confidence_10, confidence_90,
            confidence_summary, horizon_hours
        """
        if len(price_history) < 10:
            return {"error": "Insufficient price history (need 10+)",
                    "available": False}

        # Use TimesFM if available
        if self._available and self._model is not None:
            return self._timesfm_forecast(price_history, horizon)

        # Fallback: simple linear trend projection
        return self._fallback_forecast(price_history, horizon)

    def _timesfm_forecast(self, prices: List[float],
                          horizon: int) -> Dict[str, Any]:
        try:
            import numpy as np  # type: ignore

            inputs = [np.array(prices[-CONTEXT_LENGTH:])]
            point_fc, quantile_fc = self._model.forecast(
                horizon=horizon, inputs=inputs
            )
            # quantile_fc shape: (1, horizon, 10) — 10th to 90th percentile
            point = point_fc[0].tolist()
            q10 = quantile_fc[0, :, 0].tolist()   # 10th percentile
            q90 = quantile_fc[0, :, -1].tolist()  # 90th percentile

            current = prices[-1]
            end_point = point[-1] if point else current
            summary = (
                f"90% chance price stays between "
                f"${min(q10):.0f} – ${max(q90):.0f} "
                f"over next {horizon} hours"
            )
            return {
                "point_forecast": [round(p, 2) for p in point],
                "confidence_10": [round(p, 2) for p in q10],
                "confidence_90": [round(p, 2) for p in q90],
                "horizon_hours": horizon,
                "confidence_summary": summary,
                "current_price": current,
                "predicted_end": round(end_point, 2),
                "available": True,
                "model": "timesfm-2.5",
            }
        except Exception as e:
            logging.error(f"[FORECASTER] TimesFM inference failed: {e}")
            return self._fallback_forecast(prices, horizon)

    def _fallback_forecast(self, prices: List[float],
                           horizon: int) -> Dict[str, Any]:
        """Simple linear regression projection when TimesFM unavailable."""
        n = min(len(prices), 20)
        recent = prices[-n:]
        x_mean = (n - 1) / 2
        y_mean = sum(recent) / n
        num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(recent))
        den = sum((i - x_mean) ** 2 for i in range(n))
        slope = num / den if den != 0 else 0.0
        intercept = y_mean - slope * x_mean

        current = prices[-1]
        point = [intercept + slope * (n + i) for i in range(horizon)]

        # Estimate confidence bands from recent volatility
        std = math.sqrt(
            sum((p - y_mean) ** 2 for p in recent) / n) if n > 1 else 0
        q10 = [p - 1.64 * std for p in point]
        q90 = [p + 1.64 * std for p in point]

        summary = (
            f"Trend projection (no TimesFM): "
            f"${min(q10):.0f} – ${max(q90):.0f} over {horizon} hours"
        )
        return {
            "point_forecast": [round(p, 2) for p in point],
            "confidence_10": [round(p, 2) for p in q10],
            "confidence_90": [round(p, 2) for p in q90],
            "horizon_hours": horizon,
            "confidence_summary": summary,
            "current_price": current,
            "predicted_end": round(point[-1], 2),
            "available": False,
            "model": "linear_fallback",
        }

    def format_for_overlay(self, forecast: Dict[str, Any]) -> str:
        """Human-readable one-liner for overlay display."""
        if "error" in forecast:
            return f"Forecast: {forecast['error']}"
        end = forecast.get("predicted_end", "?")
        curr = forecast.get("current_price", "?")
        hrs = forecast.get("horizon_hours", FORECAST_HORIZON)
        model = forecast.get("model", "?")
        return f"${curr:,.0f} → ${end:,.0f} ({hrs}h) [{model}]"

    def get_status(self) -> Dict[str, Any]:
        return {
            "available": self._available,
            "model": self._model_name,
            "loaded_at": self._loaded_at,
            "min_ram_gb": self._min_ram_gb,
        }


# ── Global instance ───────────────────────────────────────────────
_forecaster: Optional[PriceForecaster] = None


def get_forecaster() -> PriceForecaster:
    global _forecaster
    if _forecaster is None:
        try:
            import config
            model = config.get("timesfm.model", MODEL_NAME)
            min_ram = config.get("timesfm.min_worker_ram_gb", MIN_WORKER_RAM_GB)
        except Exception:
            model, min_ram = MODEL_NAME, MIN_WORKER_RAM_GB
        _forecaster = PriceForecaster(model, min_ram)
    return _forecaster


def load_model() -> None:
    get_forecaster()  # triggers load on first call


def is_available() -> bool:
    return get_forecaster().is_available()


def forecast(price_history: List[float],
             horizon: int = FORECAST_HORIZON) -> Dict[str, Any]:
    return get_forecaster().forecast(price_history, horizon)


def format_for_overlay(fc: Dict[str, Any]) -> str:
    return get_forecaster().format_for_overlay(fc)


def get_status() -> Dict[str, Any]:
    return get_forecaster().get_status()
