import io
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
import dateutil.tz
import json
import logging
import threading
from urllib.parse import urlparse, urlencode, urljoin
import urllib.request
import twitter
import pymongo
import pyrite
import flask
import configparser
import werkzeug.exceptions
from xml.etree import ElementTree


UA = 'Mozilla/5.0 (X11; Linux x86_64; rv:108.0) Gecko/20100101 Firefox/108.0'
TWITTER_DOMAIN = 'twitter.com'
SRC_MASTODON = 'm'
SRC_TWITTER = 't'


def time_to_str(t):
    time = t.replace(tzinfo=dateutil.tz.tzutc())
    local_time = time.astimezone(dateutil.tz.tzlocal())

    return local_time.strftime('%d.%m.%Y %H:%M')


def read_file(name):
    with open(name, 'rb') as f:
        return f.read()


def http_get(uri):
    req = urllib.request.Request(uri)
    req.add_header('User-Agent', UA)
    with urllib.request.urlopen(req) as r:
        return r.status, r.reason, r.read()


def twitter_oembed(user, status, tm=None):
    params = {'url': 'https://twitter.com/{}/status/{}'.format(user, status),
              'partner': '',
              'hide_thread': 'false'}
    uri = 'https://publish.twitter.com/oembed?' + urlencode(params)
    st, reason, body = http_get(uri)
    if st != 200:
        raise Exception('Twitter response: {}: {}'.format(st, reason))
    o = json.loads(body)
    html = o['html']
    now = datetime.now()
    if not tm:
        tm = now

    return Status(int(status), now, user, tm, html)


def mastodon_oembed(user, status, url, tm):
    uri = 'https://{}/api/oembed?{}'.format(urlparse(url).hostname,
                                            urlencode({'url': url}))
    st, reason, body = http_get(uri)
    if st != 200:
        raise Exception('Mastodon response: {}: {}'.format(st, reason))
    o = json.loads(body)
    html = o['html']
    now = datetime.now()

    return Status(status, now, user, tm, html)


class Ref:
    def __init__(self, value=None):
        self.lock = threading.Lock()
        self._value = value

    @property
    def value(self):
        with self.lock:
            return self._value

    @value.setter
    def value(self, v):
        with self.lock:
            self._value = v


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

    @property
    def source(self):
        id, domain = self.id_domain()
        if domain == TWITTER_DOMAIN:
            return SRC_TWITTER
        else:
            return SRC_MASTODON

    @property
    def profile_url(self):
        id, domain = self.id_domain()
        if domain == TWITTER_DOMAIN:
            return 'https://{}/{}'.format(domain, id)
        else:
            return 'https://{}/@{}'.format(domain, id)

    def id_domain(self):
        s = self.id.split('@', 1)
        if len(s) == 1 or s[1] == TWITTER_DOMAIN:
            return (s[0], TWITTER_DOMAIN)
        else:
            return (s[0], s[1])


class Status:
    def __init__(self, id, fetch_time, user, time, html):
        self.id = id
        self.fetch_time = fetch_time
        self.user = user
        self.time = time
        self.html = html


class Source:
    def get_timeline(user, since=None):
        raise NotImplementedError()


class APITwitterSource(Source):
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


class WebTwitterSource(Source):
    idre = re.compile(r'id="stream-item-tweet-(\d+)"')

    def __init__(self, db):
        self.db = db

    def get_timeline(self, user, since=None):
        st, reason, body = http_get(urljoin(self.host, user))
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
                statuses.append(twitter_oembed(user, id))

        return statuses


class NitterTwitterSource(Source):
    def __init__(self, host, db):
        self.host = host
        self.db = db

    def get_timeline(self, user, since=None):
        st, reason, body = http_get('https://nitter.net/' + user)
        if st != 200:
            raise Exception('Nitter response: {}: {}'.format(st, reason))
        html = body.decode('utf-8')
        idre = re.compile(r'class="tweet-link" href="/(\w+)/status/(\d+)#m"')
        tmre = re.compile(r'class="tweet-date"><a href="/(\w+)/status/(\d+)#m" title="([^"]+)">')
        pos = 0
        ids = []
        while True:
            m = idre.search(html, pos=pos)
            if not m:
                break
            id = m.group(2)

            m = tmre.search(html, pos=m.end())
            if not m:
                raise Exception('tweet timestamp not found')
            tm = self.parse_time(m.group(3))
            pos = m.end()

            ids.append((id, tm))

        ids = list(reversed(list(ids)))
        logging.info('Got %d statuses from Nitter.', len(ids))

        statuses = []
        for id, tm in ids:
            if db.statuses.find_one({'_id': int(id)}):
                logging.info('Known status %s. Ignoring', id)
            else:
                logging.info('New status %s. Saving.', id)
                statuses.append(twitter_oembed(user, id, tm))

        return statuses

    def parse_time(self, s):
        t = datetime.strptime(s, '%b %d, %Y · %I:%M %p %Z')
        return datetime(t.year, t.month, t.day, t.hour, t.minute, t.second,
                        tzinfo=timezone.utc)


class RSSMastodonSource(Source):
    def __init__(self, db):
        self.db = db

    def get_timeline(self, user, since=None):
        id, host = user.split('@', 1)
        url = 'https://{}/@{}.rss'.format(host, id)
        st, reason, body = http_get(url)
        if st != 200:
            raise Exception('Mastodon response: {}: {}'.format(st, reason))

        xml = ElementTree.fromstring(body)
        statuses = []
        for item in xml.find('channel').findall('item'):
            link = item.find('link').text
            # TODO: Status is not uniq over all Mastodon instances and Twitter.
            #       Looks like we need to introduce compound status value here.
            #       Something like: twitter:12345, inuh.net:12345.
            id = int(link.split('/')[-1])
            tm = datetime.strptime(item.find('pubDate').text,
                                   '%a, %d %b %Y %H:%M:%S %z')
            if db.statuses.find_one({'_id': int(id)}):
                logging.info('Known status %s. Ignoring', id)
            else:
                logging.info('New status %s. Saving.', id)
                statuses.append(mastodon_oembed(user, id, link, tm))

        return statuses


class APIMastodonSource(Source):
    def __init__(self, db):
        self.db = db

    def get_timeline(self, user, since=None):
        acc = self.get_account(user)
        login, host = self.split_user(user)
        urlp = 'https://{}/api/v1/accounts/{}/statuses?exclude_replies=true'
        url = urlp.format(host, acc)
        body = self.http_get(url)
        stats = json.loads(body)
        statuses = []
        for stat in stats:
            s = stat.get('reblog', stat) or stat
            url = s['url']
            tm = datetime.fromisoformat(s['created_at'])
            try:
                id = int(url.split('/')[-1])
            except ValueError:
                # url can be a link to third-party web site. Not sure how
                # to handle it now. So, simply ignore it for now.
                continue
            try:
                statuses.append(mastodon_oembed(user, id, url, tm))
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    # Ignore private Mastodon instances and accounts.
                    pass
                else:
                    raise e

        return statuses

    def get_account(self, user):
        login, host = self.split_user(user)
        url = 'https://{}/api/v1/accounts/lookup?acct={}'.format(host, login)
        body = self.http_get(url)

        return json.loads(body)['id']

    def http_get(self, url):
        st, reason, body = http_get(url)
        if st != 200:
            raise Exception('Mastodon response: {}: {}'.format(st, reason))

        return body

    def split_user(self, user):
        return user.split('@', 1)


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
    def __init__(self, db, graphite, clients, sync_users, sync_delay):
        super().__init__()

        self.db = db
        self.graphite = graphite
        self.clients = clients
        self.sync_users = sync_users
        self.sync_delay = sync_delay

    def run(self):
        stats = Ref()

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

                    client = self.clients[u.source]
                    for st in client.get_timeline(u.id, last_st):
                        save_status(self.db, st)

                    u.sync_time = datetime.utcnow()
                    save_user(db, u)
            except Exception as e:
                logging.exception('Synchronization failed.')

            try:
                sts = {}
                nsts = 0
                for st in statuses_stats(db):
                    sts[st['_id']] = st['count']
                    nsts += st['count']

                stats.value = {'statuses': sts,
                               'nstatuses': nsts,
                               'nusers': len(sts)}
                self.graphite.gauge('user.count',
                                    lambda: stats.value['nusers'])
                self.graphite.gauge('status.count',
                                    lambda: stats.value['nstatuses'])
                for u, st in stats.value['statuses'].items():
                    self.graphite.gauge('status.user-{}.count'.format(u),
                                        lambda u=u: stats.value['statuses'][u])
            except Exception as e:
                logging.exception('Statistics loading failed.')

            time.sleep(self.sync_delay)


CONF_FILE = '/etc/croak.conf'
DATA_DIR = '/usr/share/croak'
TEMPLATE_DIR = DATA_DIR + '/templates'
FAVICON_FILE = DATA_DIR + '/favicon.ico'


config = read_config(CONF_FILE)
db_client = pymongo.MongoClient(config['db.host'], int(config['db.port']))
db = db_client[config['db.name']]
graphite = pyrite.Pyrite(config['graphite.host'],
                         int(config['graphite.port']),
                         prefix=config['graphite.prefix'])
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
    db.statuses.create_index([("fetch_time", pymongo.DESCENDING)])

    tw_client = None
    if config['twitter.client'] == 'web':
        tw_client = WebTwitterSource(db)
    if config['twitter.client'] == 'nitter':
        tw_client = NitterTwitterSource(config['nitter.host'], db)
    elif config['twitter.client'] == 'api':
        tw_client = APITwitterSource(config['twitter.consumer-key'],
                                     config['twitter.consumer-secret'],
                                     config['twitter.access-token-key'],
                                     config['twitter.access-token-secret'])
    else:
        logging.critical('%s: invalid twitter.client value', CONF_FILE)
        sys.exit(1)

    md_client = None
    if config['mastodon.client'] == 'rss':
        md_client = RSSMastodonSource(db)
    elif config['mastodon.client'] == 'api':
        md_client = APIMastodonSource(db)
    else:
        logging.critical('%s: invalid mastodon.client value', CONF_FILE)
        sys.exit(1)

    clients = {SRC_MASTODON: md_client,
               SRC_TWITTER: tw_client}
    sync = Synchronizer(db, graphite, clients,
                        int(config['sync.users']),
                        int(config['sync.delay']))
    sync.start()
    # TODO: Stop synchronizer and application.
    #       graphite.close()
    app.run(config['http.address'], int(config['http.port']), debug=False)
