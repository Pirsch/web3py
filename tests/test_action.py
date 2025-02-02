import multiprocessing
import os
import time
import unittest
import uuid

import mechanize

from web3py import action, DAL, Field, Session, Cache
from web3py.core import bottle

os.environ['WEB3PY_APPS_FOLDER'] = os.path.sep.join(os.path.normpath(__file__).split(os.path.sep)[:-2])

db = DAL('sqlite://storage_%s' % uuid.uuid4(), folder='/tmp/')
db.define_table('thing', Field('name'))
session = Session(secret='my secret')
cache = Cache()

action.app_name = 'tests'

@action('index')
@cache.memoize(expiration=1)
@action.uses(db, session)
def index():
    db.thing.insert(name='test')
    session['number'] = session.get('number', 0) + 1
    return 'ok %s %s' % (session['number'], db(db.thing).count())

class CacheAction(unittest.TestCase):

    def setUp(self):
        self.server = multiprocessing.Process(target=lambda: bottle.run(host='localhost', port=8001))
        self.server.start()
        self.browser = mechanize.Browser()
        time.sleep(1)

    def tearDown(self):
        self.server.terminate()

    def test_action(self):
        res = self.browser.open('http://127.0.0.1:8001/tests/index')
        self.assertEqual(res.read(), b'ok 1 1')

        res = self.browser.open('http://127.0.0.1:8001/tests/index')
        self.assertEqual(res.read(), b'ok 1 1')

        time.sleep(2)
        
        res = self.browser.open('http://127.0.0.1:8001/tests/index')
        self.assertEqual(res.read(), b'ok 2 2')
