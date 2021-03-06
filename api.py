#!/usr/bin/env python
from os import getenv
from flask import Flask, jsonify, request
from flask_pymongo import PyMongo
from flask_redis import FlaskRedis
from flask_cors import CORS, cross_origin
from types import *
from pprint import pprint
from time import time
from requests import get as requests_get
from json import loads, dumps
from slugify import slugify
import arrow
from haversine import haversine

app = Flask(__name__)
CORS(app, resources={r"*": {"origins": "*"}})
app.config['MONGO_URI'] = getenv('MONGO_URI', getenv('MONGO_URL', 'mongodb://localhost:27017/bm'))
app.config['REDIS_URL'] = getenv('REDIS_URL', 'redis://@localhost:6379/1')
app.config['LIST_LIMIT'] = getenv('LIST_LIMIT', '50')
app.config['LIST_DEFAULT_COUNT'] = getenv('LIST_DEFAULT_COUNT', '10')
app.config['GOOGLE_GEOCODER_TIMEOUT'] = getenv('GOOGLE_GEOCODER_TIMEOUT', '0.10')
app.config['DISTANCE_DEFAULT'] = getenv('DISTANCE_DEFAULT', '10000')  # 10km
app.config['DISTANCE_LIMIT'] = getenv('DISTANCE_LIMIT', '50000')  # 50km
mongo = PyMongo(app)
redis_store = FlaskRedis(app)


@app.route('/')
@cross_origin()
def index(): return jsonify(status='ok')


@app.route('/stations', methods=['GET'])
def list_stations():
    start = time()
    keys = ['key', 'loc', 'address', 'updated_at', 'prices', 'scraped_url']

    # If prices are specified then we can perform much faster and less general query
    if request.args.get('prices', None):
        keys.extend(['prices.' + p.replace('_', '-') for p in request.args.get('prices').split(',')])
        keys.remove('prices')

    # This is default lookup dict
    where = {'company': 'petrol'}

    # Coordinates if present are converted into floats.
    raw_at = request.args.get('at', '')
    at = [float(n) for n in raw_at.split(',') if n != '']
    at = None if at is [] else at

    # If "near" is present
    near = request.args.get('near', None)
    if near: at = geocode(near)

    # If at is present.
    if at: where['loc'] = {
        '$near': {'$geometry': {'type': "Point", 'coordinates': at}, '$maxDistance': safe_distance()}
    }

    # Execute lookup agains MongoDB
    included_keys = keys + ['prices', 'distance']
    stations = [simple_station(station, included_keys, {
        'prices': lambda x: enrich_prices(x),
        'updated_at': lambda x: arrow.get(x).isoformat(),
    }) for station in mongo.db.stations
                    .find(where, {key: 1 for key in keys})
                    .limit(safe_limit())]

    if at: stations = [compute_distance(station, at) for station in stations]

    # Jsonify results
    return jsonify({
        'status': 'ok',
        'stations': stations,
        'executed_in': (time() - start)
    })


def geocode(address, cache=True, http_timeout=0.10, expire_ttl=86400):
    """ Computes location for address"""
    key = 'address:%s' % slugify(address, max_length=100)

    if cache is True:
        cache_value = redis_store.get(key)
        if cache_value is not None:
            redis_store.expire(key, expire_ttl)
            return loads(cache_value.decode('utf8'))

    # Do actual request to execute address encoding.
    response = requests_get('http://maps.googleapis.com/maps/api/geocode/json', {
        'sensor': 'false', 'address': address
    })

    reply = response.json()

    try:
        location = reply['results'][0]['geometry']['location']
        raw_location = [location['lng'], location['lat']]
        if cache: redis_store.set(key, dumps(raw_location), expire_ttl)
        return raw_location
    except IndexError:
        return Exception("Can't get location %s." % address)


def compute_distance(station, at):
    """Compute distance between 'at' and station. """
    station['distance'] = haversine(
        tuple(station['loc']['coordinates']),
        tuple(at)
    ) * 1000.0

    return station


def unslugify_dict(dic):
    """ Converts dict with keys that have '-' in them to keys with '_' in them. """
    return {key.replace('-', '_'): value for key, value in dic.items()}


def enrich_prices(dic):
    prices = unslugify_dict(dic)
    return [{'type': k, 'price': v} for k, v in prices.items()]


def simple_station(station, included_keys=None, mapping=None):
    """ Post-processing of MongoDB record """
    include_keys = [] if included_keys is None else included_keys
    mapping = {} if mapping is None else mapping
    return {key: result_of(mapping, key, value) for key, value in station.items() if
            ('.' in key) or (key in include_keys)}


def result_of(mapping, key, value):
    """ Map lambda function to result if present. """
    result = mapping.get(key, value)
    return result if not isinstance(result, LambdaType) else result(value)


def safe_limit(key='LIST_LIMIT', default_key='LIST_DEFAULT_COUNT', query_param='limit'):
    """ Hard limits so that people don't go wild. """
    limit = int(request.args.get(query_param, default=app.config.get(default_key)))
    return limit if limit < int(app.config.get(key)) else int(app.config.get(key))


def safe_distance():
    return safe_limit('DISTANCE_LIMIT', 'DISTANCE_DEFAULT', 'maxDistance')


if __name__ == '__main__':
    app.run(**{
        'host': '0.0.0.0',
        'debug': getenv('DEBUG', 'True') == 'True',
        'port': int(getenv('PORT', "7766"))
    })
