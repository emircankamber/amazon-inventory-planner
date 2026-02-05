import math
import statistics

def compute_from_last_months(
    lead_time: int,
    z: float,
    monthly_units: list[int],
    fba_stock: int,
    inbound_stock: int
) -> dict:
    """
    monthly_units: son 3 takvim ayından bulunan satış adetleri (0 dahil olabilir).
                 Eğer veri yoksa boş liste gelebilir.
    """
    if not monthly_units:
        return {
            "daily_velocity": 0.0,
            "std_daily": 0.0,
            "safety_stock": 0.0,
            "rop": 0.0,
            "order_qty": 0.0,
        }

    mean_month = sum(monthly_units) / len(monthly_units)
    daily_velocity = mean_month / 30.0

    if len(monthly_units) < 2:
        std_daily = 0.0
    else:
        std_daily = statistics.stdev(monthly_units) / 30.0

    safety_stock = z * std_daily * math.sqrt(lead_time)
    rop = daily_velocity * lead_time + safety_stock
    order_qty = max(0.0, daily_velocity * 60 + safety_stock - (fba_stock + inbound_stock))

    return {
        "daily_velocity": daily_velocity,
        "std_daily": std_daily,
        "safety_stock": safety_stock,
        "rop": rop,
        "order_qty": order_qty,
    }
