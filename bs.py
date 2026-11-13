"""Black-Scholes helpers (no external dependencies). r defaults to 0 per the case."""
import math

SQRT2 = math.sqrt(2.0)
SQRT2PI = math.sqrt(2.0 * math.pi)


def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def npdf(x):
    return math.exp(-0.5 * x * x) / SQRT2PI


def _d1d2(S, K, T, sigma, r):
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / v
    return d1, d1 - v


def price(flag, S, K, T, sigma, r=0.0):
    """flag: 'c' or 'p'. T in years."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if flag == 'c' else max(0.0, K - S)
    d1, d2 = _d1d2(S, K, T, sigma, r)
    df = math.exp(-r * T)
    if flag == 'c':
        return S * ncdf(d1) - K * df * ncdf(d2)
    return K * df * ncdf(-d2) - S * ncdf(-d1)


def delta(flag, S, K, T, sigma, r=0.0):
    if T <= 0 or sigma <= 0:
        if flag == 'c':
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1, _ = _d1d2(S, K, T, sigma, r)
    return ncdf(d1) if flag == 'c' else ncdf(d1) - 1.0


def vega(S, K, T, sigma, r=0.0):
    """Price change per 1.00 (100 vol points) change in sigma."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1d2(S, K, T, sigma, r)
    return S * npdf(d1) * math.sqrt(T)


def implied_vol(flag, P, S, K, T, r=0.0, lo=1e-4, hi=3.0, iters=80):
    """Bisection. Returns None if P is outside the no-arbitrage range."""
    if T <= 0 or P <= 0:
        return None
    if P <= price(flag, S, K, T, lo, r) or P >= price(flag, S, K, T, hi, r):
        return None
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if price(flag, S, K, T, mid, r) > P:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def gamma(S, K, T, sigma, r=0.0):
    """Change in delta per $1 move in S (per share)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1d2(S, K, T, sigma, r)
    return npdf(d1) / (S * sigma * math.sqrt(T))
