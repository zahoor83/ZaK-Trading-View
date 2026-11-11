"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type {
  CandlestickData,
  IChartApi,
  UTCTimestamp,
} from "lightweight-charts";

export type MarketCandle = CandlestickData<UTCTimestamp>;

interface MarketChartProps {
  candles: readonly MarketCandle[];
  timeframe: string;
}

function validateCandles(candles: readonly MarketCandle[]): string | null {
  let previousTime = 0;

  for (let index = 0; index < candles.length; index += 1) {
    const candle = candles[index];

    if (!Number.isInteger(candle.time) || candle.time <= 0) {
      return `Candle ${index + 1} has an invalid UTC timestamp.`;
    }

    if (candle.time <= previousTime) {
      return "Candle timestamps must be unique and strictly increasing.";
    }

    previousTime = candle.time;

    const prices = [
      candle.open,
      candle.high,
      candle.low,
      candle.close,
    ];

    if (prices.some((price) => !Number.isFinite(price) || price <= 0)) {
      return `Candle ${index + 1} contains an invalid OHLC price.`;
    }

    if (
      candle.high < Math.max(candle.open, candle.close, candle.low) ||
      candle.low > Math.min(candle.open, candle.close, candle.high)
    ) {
      return `Candle ${index + 1} contains inconsistent OHLC values.`;
    }
  }

  return null;
}

export default function MarketChart({
  candles,
  timeframe,
}: MarketChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [chartError, setChartError] = useState<string | null>(null);

  const dataError = useMemo(
    () => validateCandles(candles),
    [candles]
  );

  useEffect(() => {
    if (candles.length === 0 || dataError) {
      setChartError(null);
      return;
    }

    let cancelled = false;
    let chart: IChartApi | null = null;

    setChartError(null);

    async function initializeChart() {
      try {
        const library = await import("lightweight-charts");

        if (cancelled || !containerRef.current) {
          return;
        }

        chart = library.createChart(containerRef.current, {
          autoSize: true,
          height: 360,
          layout: {
            attributionLogo: true,
            background: {
              type: library.ColorType.Solid,
              color: "#0d131d",
            },
            textColor: "#a7b1c2",
          },
          grid: {
            vertLines: {
              color: "#1b2635",
            },
            horzLines: {
              color: "#1b2635",
            },
          },
          rightPriceScale: {
            borderColor: "#263244",
          },
          timeScale: {
            borderColor: "#263244",
            timeVisible: true,
            secondsVisible: false,
          },
          crosshair: {
            vertLine: {
              color: "#64748b",
            },
            horzLine: {
              color: "#64748b",
            },
          },
        });

        const series = chart.addSeries(library.CandlestickSeries, {
          upColor: "#16a34a",
          downColor: "#dc4444",
          borderUpColor: "#16a34a",
          borderDownColor: "#dc4444",
          wickUpColor: "#16a34a",
          wickDownColor: "#dc4444",
        });

        series.setData([...candles]);
        chart.timeScale().fitContent();
      } catch {
        if (!cancelled) {
          setChartError(
            "Chart initialization failed. Please check the chart library and browser environment."
          );
        }
      }
    }

    void initializeChart();

    return () => {
      cancelled = true;
      chart?.remove();
    };
  }, [candles, dataError]);

  return (
    <section
      className="market-chart-renderer"
      aria-label={`Market candlestick chart, timeframe ${timeframe}`}
    >
      {dataError ? (
        <div className="markets-chart-empty" role="alert">
          <h2>Invalid market data</h2>
          <p>{dataError}</p>
        </div>
      ) : candles.length === 0 ? (
        <div className="markets-chart-empty" role="status">
          <div className="empty-chart-icon" aria-hidden="true">
            &#x2301;
          </div>
          <h2>Market data unavailable</h2>
          <p>
            A verified market-data connection is required before displaying
            historical candles or chart data.
          </p>
          <span>Selected timeframe: {timeframe}</span>
        </div>
      ) : chartError ? (
        <div className="markets-chart-empty" role="alert">
          <h2>Chart unavailable</h2>
          <p>{chartError}</p>
        </div>
      ) : (
        <div
          ref={containerRef}
          className="market-chart-canvas"
          role="img"
          aria-label={`Candlestick price chart for ${timeframe}`}
        />
      )}
    </section>
  );
}
