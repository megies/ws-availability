import logging
import re
import time
import threading

from flask import current_app

from pymongo import MongoClient
from bson.objectid import ObjectId

from .restriction import RestrictionInventory

from apps.globals import Error
from apps.utils import overflow_error
from apps.globals import (
    FDSNWS_STATION_URL
)

RESTRICTED = None


def _refresh_restricted_bit_cache():
    global RESTRICTED

    while True:
        logging.info(f"Started rebuilding FDSNWS-Station cache")
        tmp_restr = RestrictionInventory(FDSNWS_STATION_URL)
        # Make sure instance is populated, there might have been a HTTP
        # error when reading the inventory from FDSNWS-Station
        if tmp_restr.is_populated:
            RESTRICTED = tmp_restr
        logging.info(f"Completed rebuilding FDSNWS-Station cache")
        time.sleep(tmp_restr.refresh_timeout)


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
            socketTimeoutMS=5000,
            connectTimeoutMS=5000,
        ).get_database(db_name)

        d_streams = db.daily_streams.find(qry, batch_size=10000)

        for ds in d_streams:
            ds_id = ObjectId(ds["_id"])
            ds_avail = float(ds["avail"])

            if ds_avail >= 100:
                # If availability = 100, just add it to the set (no c_segments present)
                ds_elem, restr = _parse_daily_stream_to_list(ds)
                # If user provided overlapping parameters in the HTTP POST
                # it can be that the same stream or segment has been queried,
                # so we need to make sure final dataset has no duplicates.
                # Also, check if restricted status is known from FDSNWS-Station.
                # Unknown restricted status is not supported yet.
                if restr and not ds_elem in result:
                    result.append(ds_elem)
            else:
                # If availability < 100, collect the continuous segments
                c_segs = db.c_segments.find({"streamId": ds_id})
                for cs in c_segs:
                    c_seg_elem, restr = _parse_c_segment_to_list(ds, cs)
                    # If user provided overlapping parameters in the HTTP POST
                    # it can be that the same stream or segment has been queried,
                    # so we need to make sure final dataset has no duplicates.
                    # Also, check if restricted status is known from FDSNWS-Station.
                    # Unknown restricted status is not supported yet.
                    if restr and not c_seg_elem in result:
                        result.append(c_seg_elem)

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
    wildcards = [s.replace("?", ".") for s in split]
    # Replace wildcards marks with regexp equivalent
    wildcards = [s.replace("*", ".*") for s in split]
    # Add start and end of string
    regex = "^" + "|".join(wildcards) + "$"
    # Compile and return
    return re.compile(regex, re.IGNORECASE)


def _get_restricted_status(daily_stream):
    """Gets the restricted status of provided daily stream.

    Args:
        daily_stream (dict): Daily stream dictionary from WFCatalog DB.

    Returns:
        string: Restricted status, `None` if unknown.
    """
    global RESTRICTED
    
    restricted = None

    r = RESTRICTED.is_restricted(
        f"{daily_stream['net']}.{daily_stream['sta']}..{daily_stream['cha']}",
        daily_stream["ts"].date(),
        daily_stream["te"].date(),
    )

    if r:
        restricted = r.name

    return restricted


def _parse_daily_stream_to_list(daily_stream):
    """Parse the daily stream JSON document.

    Args:
        daily_stream (string): Daily stream representation in JSON format.

    Returns:
        list: List with daily stream metrics.
        str: Restricted status information, `None` if unknown.
    """
    restr = _get_restricted_status(daily_stream)

    result = [
        daily_stream["net"],
        daily_stream["sta"],
        daily_stream["loc"],
        daily_stream["cha"],
        daily_stream["qlt"],
        daily_stream["srate"][0],
        daily_stream["ts"],
        daily_stream["te"],
        daily_stream["created"],
        restr,
        1,
    ]
    return result, restr


def _parse_c_segment_to_list(daily_stream, c_segment):
    """Parse the daily stream and continuous segments JSON documents.

    Args:
        daily_stream (string): Daily stream representation in JSON format.
        c_segment (string): Continuous segment representation in JSON format.

    Returns:
        list: List with continuous segment metrics wrapped around daily stream.
        str: Restricted status information, `None` if unknown.
    """
    restr = _get_restricted_status(daily_stream)

    result = [
        daily_stream["net"],
        daily_stream["sta"],
        daily_stream["loc"],
        daily_stream["cha"],
        daily_stream["qlt"],
        c_segment["srate"],
        c_segment["ts"],
        c_segment["te"],
        daily_stream["created"],
        restr,
        1,
    ]
    return result, restr


# TODO: Flattening function for HTTP POST parameters to minimize duplicate queries
def _flatten_parameters(params):
    """Flatten the list of parameters. HTTP POST method can provide multiple
    rows of parameters like:
        NL DBN01 * * 2018-01-01 2019-01-01
        NL DBN01,DBN02 * * 2016-01-01 2017-01-01
        NL DBN01 * HD? 2016-01-01 2017-01-01

    Args:
        params ([type]): [description]

    Returns:
        [type]: [description]
    """
    raise NotImplementedError()


def collect_data(params):
    """ Get the result of the Mongo query. """
    data = None
    logging.debug("Start collecting data from WFCatalog DB...")
    qry, data = mongo_request(params)

    # if data:
    #     fc = FdsnClient()
    #     data = fc.assign_restricted_statuses(params, data)

    logging.debug(qry)

    return data


# Start cache refresh in a thread
t = threading.Thread(
    name="refresh_restricted_bit_cache", target=_refresh_restricted_bit_cache
)
t.start()
