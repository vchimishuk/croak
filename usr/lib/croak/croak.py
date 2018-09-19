import io
import sys
import time
from datetime import datetime
import dateutil.tz
import json
import logging
import threading
import http.client
import twitter
from pyquery import PyQuery
from html.parser import HTMLParser
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


class TwitterClient:
    def get_timeline(user, since=None):
        raise NotImplementedError()


class APITwitterClient(TwitterClient):
    def __init__(self, consumer_key, consumer_secret,
                 access_token_key, access_token_secret):
        self.api = twitter.Api(consumer_key=consumer_key,
                               consumer_secret=consumer_secret,
                               access_token_key=access_token_key,
                               access_token_secret=access_token_secret)

    def get_timeline(self, user, since=None):
        self.api.GetUserTimeline(screen_name=user, since_id=since,
                                 trim_user=True)
        statuses = []
        for st in sts:
            e = self.api.GetStatusOembed(st.id)
            create_at = datetime.strptime(st.created_at,
                                          '%a %b %d %H:%M:%S %z %Y')
            statuses.append(Status(st.id, user, create_at, e['html']))

        return statuses


class WebTwitterClient(TwitterClient):
    UA = 'Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/61.0'

    OEMBED_FMT = ('<blockquote class="twitter-tweet">'
                  '<p lang="en" dir="ltr">{text}</p>'
                  '&mdash; {name} ({id}) '
                  '<a href="https://twitter.com/{id}/status/{status}">'
                  '{date}'
                  '</a>'
                  '</blockquote>\n'
                  '<script async src="//platform.twitter.com/widgets.js" '
                  'charset="utf-8"></script>')

    class TweetSanitizeParser(HTMLParser):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

            self.res = ''

        def handle_starttag(self, tag, attrs):
            if tag == 'a':
                d = dict(attrs)
                self.res += '<a href="{}">'.format(d['href'])

        def handle_endtag(self, tag):
            if tag == 'a':
                self.res += '</a>'

        def handle_data(self, data):
            self.res += data

        def parse(self, text):
            self.feed(text)

            return self.res

    def sanitize_text(self, text):
        p = __class__.TweetSanitizeParser()

        return p.parse(text)

    def get_timeline(self, user, since=None):
        url = 'https://twitter.com/{}'.format(user)
        pq = PyQuery(url, headers={'User-Agent': __class__.UA})

        statuses = []
        for item in pq('li.stream-item').items():
            status = int(item.attr('data-item-id'))
            text = self.sanitize_text(item.find('p.tweet-text').html())
            date = item.find('a.tweet-timestamp').text()
            ts = int(item.find('a.tweet-timestamp span').attr('data-time'))
            id = item.find('div.original-tweet').attr('data-screen-name')
            name = item.find('div.original-tweet').attr('data-name')
            st = __class__.OEMBED_FMT.format(text=text, name=name, id=id,
                                             status=status, date=date)

            statuses.append(Status(status, user, datetime.fromtimestamp(ts),
                                   st))

        return statuses


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

                    for st in client.get_timeline(u.id, last_st):
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
db_client = pymongo.MongoClient(config['db.host'], int(config['db.port']))
db = db_client[config['db.name']]
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

    client = None
    if config['twitter.client'] == 'web':
        client = WebTwitterClient()
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
