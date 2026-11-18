def load():
    try:
        return read_data()
    except Exception as error:
        logger.exception("load failed")
        raise RuntimeError("could not load data") from error
