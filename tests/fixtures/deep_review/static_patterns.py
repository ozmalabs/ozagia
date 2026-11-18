"""Small examples for conservative deep review static checks."""


def shared_default(items=[]):
    return items


def nested_scan(items):
    for item in items:
        for other in items:
            if item == other:
                continue


def hidden_failure():
    try:
        work()
    except:
        pass


def eager_range(size):
    return list(range(size))
