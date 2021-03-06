import io
import re
import sys
import time
from datetime import date, datetime, timedelta
import dateutil.tz
import json
import logging
import threading
from urllib.parse import urlencode
import urllib.request
import twitter
import pymongo
import flask
import configparser
import werkzeug.exceptions


def time_to_str(t):
    time = t.replace(tzinfo=dateutil.tz.tzutc())
    local_time = time.astimezone(dateutil.tz.tzlocal())

    return local_time.strftime('%d.%m.%Y %H:%M')


def read_file(name):
    with open(name, 'rb') as f:
        return f.read()


class User:
    def __init__(self, id, sync_time, enabled):
        self.id = id
        self.sync_time = sync_time
        self.enabled = enabled

    @property
    def sync_time_str(self):
        if self.sync_time:
            return time_to_str(self.sync_time)
        else:
            return ''


class Status:
    def __init__(self, id, fetch_time, user, time, html):
        self.id = id
        self.fetch_time = fetch_time
        self.user = user
        self.time = time
        self.html = html


class TwitterClient:
    def get_timeline(user, since=None):
        raise NotImplementedError()


class APITwitterClient(TwitterClient):
    def __init__(self, consumer_key, consumer_secret,
                 access_token_key, access_token_secret):
        self.api = twitter.Api(consumer_key=consumer_key,
                               consumer_secret=consumer_secret,
                               access_token_key=access_token_key,
                               access_token_secret=access_token_secret,
                               sleep_on_rate_limit=True,
                               timeout=60)

    def get_timeline(self, user, since=None):
        sts = self.api.GetUserTimeline(screen_name=user, since_id=since,
                                       trim_user=True)
        statuses = []
        for st in sts:
            e = self.api.GetStatusOembed(st.id)
            create_at = datetime.strptime(st.created_at,
                                          '%a %b %d %H:%M:%S %z %Y')
            statuses.append(Status(st.id, datetime.now(), user,
                                   create_at, e['html']))

        return statuses


class WebTwitterClient(TwitterClient):
    ua = 'Croak Python-urllib/{}.{}'.format(*sys.version_info[:2])
    idre = re.compile(r'id="stream-item-tweet-(\d+)"')

    def __init__(self, db):
        self.db = db

    def get_timeline(self, user, since=None):
        st, reason, body = self.http_get('https://twitter.com/' + user)
        if st != 200:
            raise Exception('Twitter response: {}: {}: {}'.format(st, reason,
                                                                  b))
        statuses = []
        ids = list(reversed(self.idre.findall(body.decode('utf-8'))))
        logging.info('Got %d statuses from Twitter.', len(ids))

        for id in ids:
            if db.statuses.find_one({'_id': int(id)}):
                logging.info('Known status %s. Ignoring', id)
            else:
                logging.info('New status %s. Saving.', id)
                statuses.append(self.oembed(user, id))

        return statuses

    def oembed(self, user, status):
        params = {'url': 'https://twitter.com/{}/status/{}'.format(user, status),
                  'partner': '',
                  'hide_thread': 'false'}
        uri = 'https://publish.twitter.com/oembed?' + urlencode(params)
        st, reason, body = self.http_get(uri)
        if st != 200:
            raise Exception('Twitter response: {}: {}: {}'.format(st, reason,
                                                                  b))
        o = json.loads(body)
        html = o['html']
        now = datetime.now()

        return Status(int(status), now, user, now, html)

    def http_get(self, uri):
        req = urllib.request.Request(uri)
        req.add_header('User-Agent', self.ua)
        with urllib.request.urlopen(req) as r:
            return r.status, r.reason, r.read()


def read_config(path):
    p = configparser.ConfigParser()

    with open(path) as f:
        s = io.StringIO('[main]\n' + f.read())
        p.read_file(s)

    return p['main']


def save_user(db, user):
    update = {'sync_time': user.sync_time, 'enabled': user.enabled}
    r = db.users.update_one({'_id': user.id}, {'$set': update}, True)

    return user


def find_users(db, filter=None, sort=None, skip=0, limit=0):
    users = []
    for u in db.users.find(filter=filter, sort=sort, skip=skip, limit=limit):
        users.append(User(u['_id'], u['sync_time'], u['enabled']))

    return users


def find_user(db, id):
    u = db.users.find_one({'_id': id})
    if not u:
        raise werkzeug.exceptions.NotFound()

    return User(u['_id'], u['sync_time'], u['enabled'])


def statuses_stats(db):
    return db.statuses.aggregate([{'$sort': {'time': 1}},
                                  {'$group': {'_id': '$user',
                                              'count': {'$sum': 1},
                                              'latest_time': {'$last': '$time'},
                                              'latest_id': {'$last': '$_id'}}}])


def find_statuses(db, filter=None, sort=None, skip=0, limit=0):
    statuses = []
    sts = db.statuses.find(filter=filter, sort=sort, skip=skip, limit=limit)
    for st in sts:
        statuses.append(Status(st['_id'], st['fetch_time'], st['user'],
                               st['time'], st['html']))

    return statuses


def save_status(db, st):
    if not db.statuses.find_one({'_id': st.id}):
        db.statuses.insert_one({'_id': st.id,
                                'fetch_time': st.fetch_time,
                                'user': st.user,
                                'time': st.time,
                                'html': st.html})

    return st


def timeline_stats(db, start_date):
    sts = db.statuses.aggregate([
        {'$match': {'time': {'$gt': start_date}}},
        {'$group': {'_id': {'user': '$user',
                            'month': {'$month': '$time'},
                            'day': {'$dayOfMonth': '$time'},
                            'year': {'$year': '$time'}},
                    'count': {'$sum': 1} }}])

    return list(sts)


class Synchronizer(threading.Thread):
    def __init__(self, db, client, sync_users, sync_delay):
        super().__init__()

        self.db = db
        self.client = client
        self.sync_users = sync_users
        self.sync_delay = sync_delay

    def run(self):
        while True:
            try:
                users = find_users(db, filter={'enabled': True},
                                   sort=(('sync_time', 1),),
                                   limit=self.sync_users)
                for u in users:
                    logging.info('Synchronizing user @%s.', u.id)

                    last_sts = find_statuses(db, {'user': u.id},
                                             (('_id', -1),), 0, 1)
                    last_st = None
                    if last_sts:
                        last_st = last_sts[0].id

                    for st in client.get_timeline(u.id, last_st):
                        save_status(self.db, st)

                    u.sync_time = datetime.utcnow()
                    save_user(db, u)
            except Exception as e:
                logging.exception('Synchronization failed.')

            time.sleep(self.sync_delay)


CONF_FILE = '/etc/croak.conf'
DATA_DIR = '/usr/share/croak'
TEMPLATE_DIR = DATA_DIR + '/templates'
FAVICON_FILE = DATA_DIR + '/favicon.ico'


config = read_config(CONF_FILE)
db_client = pymongo.MongoClient(config['db.host'], int(config['db.port']))
db = db_client[config['db.name']]
app = flask.Flask('Croak', template_folder=TEMPLATE_DIR)
favicon_data = read_file(FAVICON_FILE)


logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')


@app.route('/favicon.ico', methods=['GET'])
def favicon():
    return flask.Response(favicon_data, mimetype="image/vnd.microsoft.icon")


@app.route('/users', methods=['GET'])
def users():
    users = find_users(db, sort=(('_id', 1),))
    stats = {}
    for s in statuses_stats(db):
        stats[s['_id']] = {'count': s['count'],
                           'latest': {'id': s['latest_id'],
                                      'time': time_to_str(s['latest_time'])}}

    return flask.render_template('users.html', users=users, stats=stats)


@app.route('/users', methods=['POST'])
def create_user():
    id = flask.request.form['id'].strip().lstrip('@')
    if id:
        save_user(db, User(id, sync_time=datetime.fromtimestamp(0),
                           enabled=True))

    return flask.redirect('/users')


@app.route('/users/<user>/_toggle', methods=['GET'])
def toggle_user(user):
    u = find_user(db, user)
    u.enabled = not u.enabled
    save_user(db, u)

    return flask.redirect('/users')


@app.route('/', methods=['GET'])
@app.route('/timeline/', methods=['GET'])
@app.route('/timeline/<user>', methods=['GET'])
def timeline(user=None):
    offset = int(flask.request.args.get('offset', 0))
    limit = int(config['timeline.page-size'])
    filter = None
    if user:
        filter = {'user': user}
    sts = find_statuses(db, filter, (('fetch_time', -1), ('_id', -1)),
                        offset, limit)
    prev = None
    if offset >= limit:
        prev = offset - limit
    next = offset + limit

    return flask.render_template('timeline.html', statuses=sts, user=user,
                                 prev=prev, next=next)


@app.route('/stats', methods=['GET'])
def stats():
    ndays = 180
    dates = []
    for i in range(ndays):
        dates.insert(0, date.today() - timedelta(days=i))

    users_sts = {}
    for s in timeline_stats(db, datetime.utcnow() - timedelta(days=ndays)):
        key = s['_id']
        u = key['user']
        d = date(key['year'], key['month'], key['day'])

        if u not in users_sts:
            users_sts[u] = {}
        users_sts[u][d] = s['count']

    sts = []
    sts_max = 0
    for u in sorted(users_sts.keys()):
        s = []
        for d in dates:
            c = users_sts[u].get(d, 0)
            sts_max = max(sts_max, c)
            s.append({'date': d, 'count': c})
        sts.append((u, s))

    total = []
    total_max = 0
    for d in dates:
        c = 0
        for s in users_sts.values():
            c += s.get(d, 0)
        total_max = max(total_max, c)
        total.append({'date': d, 'count': c})

    return flask.render_template('stats.html',
                                 stats=sts, stats_max=sts_max,
                                 total=total, total_max=total_max)


if __name__ == '__main__':
    # TODO: Create indexes.

    client = None
    if config['twitter.client'] == 'web':
        client = WebTwitterClient(db)
    elif config['twitter.client'] == 'api':
        client = APITwitterClient(config['twitter.consumer-key'],
                                  config['twitter.consumer-secret'],
                                  config['twitter.access-token-key'],
                                  config['twitter.access-token-secret'])
    else:
        logging.critical('%s: invalid twitter.client value', CONF_FILE)
        sys.exit(1)

    sync = Synchronizer(db, client, int(config['twitter.sync-users']),
                        int(config['twitter.sync-delay']))
    sync.start()
    # TODO: Stop synchronizer and application.
    app.run(config['http.address'], int(config['http.port']), debug=False)
