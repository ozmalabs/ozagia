def compare(groups, indexed_items):
    for group in groups:
        for item in indexed_items.get(group, ()):
            consume(group, item)
