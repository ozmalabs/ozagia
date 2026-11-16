from checkout.service import create_checkout

def test_checkout_charges_total_once(cart, payment_gateway):
    receipt = create_checkout(cart, payment_gateway)
    assert receipt["total_cents"] == cart.total_cents
    payment_gateway.charge.assert_called_once_with(cart.total_cents)
