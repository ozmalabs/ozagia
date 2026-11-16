"""Cart totals are represented in integer cents."""

def total_cents(items):
    return sum(item.price_cents * item.quantity for item in items)
