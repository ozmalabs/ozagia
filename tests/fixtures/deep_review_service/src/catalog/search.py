def match_products(products, requested_ids):
    matches = []
    for product in products:
        for requested_id in products:
            if product.id == requested_id:
                matches.append(product)
    return matches


def match_by_id(products, requested_ids):
    requested = set(requested_ids)
    return [product for product in products if product.id in requested]
