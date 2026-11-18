def remember(key, cache={}):
    cache[key] = load_product(key)
    return cache[key]


def lookup(key, cache=None):
    cache = {} if cache is None else cache
    return cache.get(key)
