import logging
import re
from pymemcache.client import base
from pymemcache import serde
from flask import current_app
from pymongo import MongoClient, ASCENDING
from .restriction import RestrictionInventory

RESTRICTED_INVENTORY = None

PROJ = {
    "_id": 0,
    "net": 1,
    "sta": 1,
    "loc": 1,
    "cha": 1,
    "qlt": 1,
    "srate": 1,
    "ts": 1,
    "te": 1,
    "created": 1,
    "restr": 1,
    "count": 1,
}

def mongo_request(paramslist):
    """Build and run WFCatalog MongoDB queries using request query parameters

    Args:
        paramslist ([]): List of lists containing URL query parameters

    Returns:
        []: MongoDB queries used to obtain data
        []: List of metrics extracted from the MongoDB
    """
    db_host = current_app.config["MONGODB_HOST"]
    db_port = current_app.config["MONGODB_PORT"]
    db_usr = current_app.config["MONGODB_USR"]
    db_pwd = current_app.config["MONGODB_PWD"]
    db_name = current_app.config["MONGODB_NAME"]
    # db_max_rows = current_app.config["MONGODB_MAX_ROWS"]

    result = []
    network = "*"
    station = "*"
    location = "*"
    # channel = "*"
    quality = "*"

    # List of queries executed agains the DB, let's keep it for logging
    qries = []

    for params in paramslist:
        qry = {}
        if params["network"] != "*":
            network = {"$in": params["network"].split(",")}
            qry["net"] = network
        if params["station"] != "*":
            station = _query_params_to_regex(params["station"])
            qry["sta"] = station
        if params["location"] != "*":
            location = _query_params_to_regex(params["location"])
            qry["loc"] = location
        if params["channel"] != "*":
            qry["cha"] = _query_params_to_regex(params["channel"])
        if params["quality"] != "*":
            quality = {"$in": params["quality"].split(",")}
            qry["qlt"] = quality
        if params["start"]:
            ts = {"$gte": params["start"]}
            qry["ts"] = ts
        if params["end"]:
            te = {"$lte": params["end"]}
            qry["te"] = te

        # Let's memorize this new query for logging purposes
        qries.append(qry)

        db = MongoClient(
            db_host,
            db_port,
            username=db_usr,
            password=db_pwd,
            authSource=db_name,
        ).get_database(db_name)

        cursor = db.availability.find(qry, projection=PROJ)

        # cursor = db.availability.find(qry, projection=PROJ).sort(
        #     [("net", ASCENDING),
        #     ("sta", ASCENDING),
        #     ("loc", ASCENDING),
        #     ("cha", ASCENDING),
        #     ("qlt", ASCENDING),
        #     ("srate", ASCENDING),
        #     ("ts", ASCENDING),
        #     ("te", ASCENDING)]
        # )

        # Eagerly load the rows from the DB
        rows = list(cursor)

        # Flatten list of objects to list of arrays
        result += [[row[key] for key in row.keys()] for row in rows]

    # Result needs to be sorted, this seems to be required by the fusion step
    result.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7]))

    return qries, result


def _query_params_to_regex(str):
    """Parse list of params into a regular expression

    Args:
        str (string): Comma-separated parameters

    Returns:
        obj: Compiled regular expression
    """
    # Split the string by comma and put in in a list
    split = str.split(",")
    # Replace question marks with regexp equivalent
    split = [s.replace("?", ".") for s in split]
    # Replace wildcards marks with regexp equivalent
    split = [s.replace("*", ".*") for s in split]
    # Add start and end of string
    regex = "^" + "|".join(split) + "$"
    # Compile and return
    return re.compile(regex, re.IGNORECASE)


def _get_restricted_status(daily_stream):
    """Gets the restricted status of provided daily stream.

    Args:
        daily_stream (dict): Daily stream dictionary from WFCatalog DB.

    Returns:
        string: Restricted status, `None` if unknown.
    """
    global RESTRICTED_INVENTORY

    if not RESTRICTED_INVENTORY:
        fdsnws_station_url = current_app.config["FDSNWS_STATION_URL"]
        RESTRICTED_INVENTORY = RestrictionInventory(fdsnws_station_url)

    r = RESTRICTED_INVENTORY.is_restricted(
        f"{daily_stream['net']}.{daily_stream['sta']}..{daily_stream['cha']}",
        daily_stream["ts"].date(),
        daily_stream["te"].date(),
    )

    if r:
        return r.name
    else:
        return None


def collect_data(params):
    """Get the result of the Mongo query."""
    cache_host = current_app.config["CACHE_HOST"]
    cache_port = current_app.config["CACHE_PORT"]
    cache_short_inv_period = current_app.config["CACHE_SHORT_INV_PERIOD"]

    client = base.Client((cache_host, cache_port), serde=serde.pickle_serde)
    CACHED_REQUEST_KEY = str(hash(str(params)))

    # Try to get cached response for given params
    if client.get(CACHED_REQUEST_KEY):
        return client.get(CACHED_REQUEST_KEY)

    data = None
    logging.debug("Start collecting data from WFCatalog DB...")
    qry, data = mongo_request(params)
    client.set(CACHED_REQUEST_KEY, data, cache_short_inv_period)

    logging.debug(qry)

    return data
