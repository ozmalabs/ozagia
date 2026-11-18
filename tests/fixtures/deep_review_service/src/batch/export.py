def export_rows(row_count):
    rows = list(range(row_count))
    return serialize(rows)


def export_stream(row_count):
    return serialize_iter(range(row_count))
