import io
import time
from datetime import datetime
import dateutil.tz
import json
import logging
import threading
import http.client
import twitter
import pymongo
import flask
import configparser
import werkzeug.exceptions


def time_to_str(t):
    time = t.replace(tzinfo=dateutil.tz.tzutc())
    local_time = time.astimezone(dateutil.tz.tzlocal())

    return local_time.strftime('%d.%m.%Y %H:%M')


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
    def __init__(self, id, user, time, html):
        self.id = id
        self.user = user
        self.time = time
        self.html = html


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
        statuses.append(Status(st['_id'], st['user'], st['time'], st['html']))

    return statuses


def get_timeline(client, user, since):
    sts = client.GetUserTimeline(screen_name=user, since_id=since,
                                 trim_user=True)
    statuses = []
    for st in sts:
        e = client.GetStatusOembed(st.id)
        create_at = datetime.strptime(st.created_at, '%a %b %d %H:%M:%S %z %Y')
        statuses.append(Status(st.id, user, create_at, e['html']))

    return statuses


def save_status(db, st):
    update = {'user': st.user, 'time': st.time, 'html': st.html}
    db.statuses.update_one({'_id': st.id}, {'$set': update}, True)

    return st


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
                                             (('time', -1),), 0, 1)
                    last_st = None
                    if last_sts:
                        last_st = last_sts[0].id

                    for st in get_timeline(self.client, u.id, last_st):
                        logging.info('Received new status %d.', st.id)
                        save_status(self.db, st)

                    u.sync_time = datetime.utcnow()
                    save_user(db, u)
            except Exception as e:
                logging.exception('Synchronization failed.')

            time.sleep(self.sync_delay)


CONF_FILE = '/etc/croak.conf'
TEMPLATE_DIR = '/usr/share/croak/templates'


config = read_config(CONF_FILE)
client = pymongo.MongoClient(config['db.host'], int(config['db.port']))
db = client[config['db.name']]
app = flask.Flask('Croak', template_folder=TEMPLATE_DIR)


logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.basicConfig(level=logging.WARN,
                    format='%(asctime)s %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')


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
    sts = find_statuses(db, filter, (('time', -1),), offset, limit)
    prev = None
    if offset >= limit:
        prev = offset - limit
    next = offset + limit

    return flask.render_template('timeline.html', statuses=sts, user=user,
                                 prev=prev, next=next)


if __name__ == '__main__':
    # TODO: Create indexes.

    client = twitter.Api(consumer_key=config['twitter.consumer-key'],
                         consumer_secret=config['twitter.consumer-secret'],
                         access_token_key=config['twitter.access-token-key'],
                         access_token_secret=config['twitter.access-token-secret'])
    sync = Synchronizer(db, client, int(config['twitter.sync-users']),
                        int(config['twitter.sync-delay']))
    sync.start()
    # TODO: Stop synchronizer and application.
    app.run(config['http.address'], int(config['http.port']), debug=False)
