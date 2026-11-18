def load():
    try:
        return read_data()
    except Exception as error:
        report(error)
