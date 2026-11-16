"""Payment gateway interface for checkout."""

class PaymentGateway:
    def charge(self, amount_cents):
        raise NotImplementedError
