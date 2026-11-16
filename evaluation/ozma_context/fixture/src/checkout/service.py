"""Checkout workflow used by the deterministic context fixture."""

def create_checkout(cart, payment_gateway):
    """Charge the cart and return a stable checkout receipt."""
    if not cart.items:
        raise ValueError("cannot checkout an empty cart")
    payment = payment_gateway.charge(cart.total_cents)
    return {"checkout_id": payment.reference, "total_cents": cart.total_cents}
