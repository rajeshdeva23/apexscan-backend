# Forward Label Schema — data known only after T (SECTOR-5A)

Labels live in a **separate dataset** from features and use **only** bars with
`end_timestamp > T`. Join back via `(trading_date, observation_time, sector_id[, identity])`.
Price at a future instant T+h = close of the completed 1-minute bar with `end_timestamp ≤ T+h`
(same convention as the feature price at T; see METHODOLOGY §7). Returns are Decimal ratios.
Horizons are measured **from T**, never from session open. No thresholds/binary labels here —
only continuous raw values (5D may derive binaries).

## Horizons

Recommended: **+5m, +15m, +30m, +60m** (primary sector = **+15m**), plus optional
**session_close**. Every horizon is validated against the date's actual session; a horizon
that would cross session end yields `None` with `HORIZON_CROSSES_SESSION_END` (never the next
day's opening price).

## Stock forward labels (per stock × horizon)

| name | formula | direction-normalized | class |
|------|---------|----------------------|-------|
| stock_forward_return_h | `price(T+h)/price(T) − 1` | no | raw |
| stock_aligned_forward_return_h | `+fwd` if observed sector BULLISH; `−fwd` if BEARISH; `None` otherwise | yes | primary(stock) |
| stock_forward_vs_sector_h | `stock_forward_return_h − sector_forward_return_h` | no | primary(stock, relative) |
| stock_aligned_forward_vs_sector_h | sector-signed version of the above | yes | secondary |
| stock_mfe_h / stock_mae_h | see MFE/MAE below | yes (sign-normalized) | diagnostic (path) |

## Sector forward labels (per sector × horizon)

Sector forward return = **equal-weight median of constituent forward returns** over the
constituents that were eligible at T and remain priced through T+h (consistent with the V1
equal-weight, heavyweight-resistant sector method; **no** sector index price in V1).

| name | formula | direction-normalized | class |
|------|---------|----------------------|-------|
| sector_forward_return_h | median over constituents of `price(T+h)/price(T) − 1` | no | raw |
| sector_aligned_forward_return_h | `+`/`−` by observed `raw_sector_direction` (BULLISH/BEARISH); `None` for NEUTRAL/MIXED/INSUFFICIENT | yes | **primary (h=15m)** |
| sector_forward_net_breadth_h | net breadth recomputed at T+h over the same constituents | no | diagnostic (continuation participation) |
| sector_forward_mad_h | MAD of constituent forward returns | no | diagnostic (dispersion evolution) |

## MFE / MAE (path-dependent, per stock and per sector, per horizon window (T, T+h])

Let future prices over the window be `P = {close of each completed bar with T < end ≤ T+h}`,
`p0 = price(T)`.

- **Bullish observed direction:**
  `MFE = max(P)/p0 − 1` (favorable = up), `MAE = min(P)/p0 − 1` (adverse = down, ≤ 0).
- **Bearish observed direction (sign-normalized so favorable is downside):**
  `MFE = −(min(P)/p0 − 1)` (favorable = down move), `MAE = −(max(P)/p0 − 1)` (adverse = up move).
- **NEUTRAL/MIXED/INSUFFICIENT:** `None` (no reference direction).

So a positive `MFE` always means "moved favorably in the observed sector direction" and a
negative `MAE` always means "moved adversely," for both bulls and bears. MFE/MAE are labels
only and are **never** read during feature generation (enforced by dataset separation + the
anti-leakage tests in DATA_QUALITY).

## Reversal evidence

Reversal is expressed through the continuous labels, not a threshold: a bullish observation
with negative `sector_aligned_forward_return_h` (or a large negative `MAE` relative to a small
`MFE`) is raw false-strength evidence. 5C studies the distribution; 5D may define a bound.

## Nullability summary

Any label is `None` (with a typed reason) when: the observed sector direction is
non-directional (for aligned/MFE/MAE), the horizon crosses session end, a required future bar
is missing/stale, or a corporate action makes the future price untrustworthy. Missing labels
are never imputed with 0.
