def submit(payment):
    try:
        return send_payment(payment)
    except Exception:
        pass


def submit_with_context(payment):
    try:
        return send_payment(payment)
    except TimeoutError as error:
        record_failure(error)
        raise
