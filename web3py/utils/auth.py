import hashlib
import urllib
import uuid

from web3py import redirect, request, response, abort, URL, action
from web3py.core import Fixture, Template
from pydal.validators import IS_EMAIL, CRYPT, IS_NOT_EMPTY, IS_NOT_IN_DB


class AuthEnforcer(Fixture):

    def __init__(self, auth, condition=None):
        self.__prerequisites__ = [auth]
        self.auth = auth
        self.condition = condition
        
    def abort_or_rediect(self, page):
        if request.content_type == 'application/json':
            abort(403)
        redirect(URL(self.auth.route + page))

    def on_request(self):
        user = self.auth.session.get('user')
        if not user or not user.get('id'):
            self.abort_or_rediect('login')
        if callable(self.condition) and not self.condition(user):
            self.abort_or_rediect('not-authorized')

class Auth(Fixture):

    messages = {
        'verify_email': {
            'subject': 'Confirm email',
            'body': 'Welcome {first_name}, click {link} to confirm your email'
            },
        'reset_password': {
            'subject': 'Password reset',
            'body': 'Hello {first_name}, click {link} to change password'
            },
        'unsubscribe': {
            'subject': 'Unsubscribe confirmation',
            'body': 'By {first_name}, you have been erased from our system'
            }
        }
    
    extra_auth_user_fields = []

    def __init__(self, session, db,
                 define_tables=True, 
                 sender=None,
                 registration_requires_confirmation=True,
                 registration_requires_appoval=False):        

        self.__prerequisites__ = []
        if session: self.__prerequisites__.append(session)
        if db: self.__prerequisites__.append(db)
        self.db = db
        self.session = session
        self.sender = sender
        self.route = None
        self.registration_requires_confirmation = registration_requires_confirmation
        self.registration_requires_appoval = registration_requires_appoval
        self._link = None # this variable is not thread safe (only for testing)
        if db and define_tables:
            self.define_tables()
        self.plugins = {}

    def define_tables(self):
        db = self.db
        Field = db.Field
        if not 'auth_user' in db.tables:
            ne = IS_NOT_EMPTY()
            db.define_table(
                'auth_user',
                Field('username', requires=[ne, IS_NOT_IN_DB(db, 'auth_user.username')], unique=True),
                Field('email', requires=(IS_EMAIL(), IS_NOT_IN_DB(db, 'auth_user.email')), unique=True),
                Field('password','password', requires=CRYPT(), readable=False, writable=False),
                Field('first_name', requires=ne),
                Field('last_name', requires=ne),
                Field('sso_id', readable=False, writable=False),
                Field('action_token', readable=False, writable=False),
                *self.extra_auth_user_fields)

    # validation fixtures
    @property
    def user(self):
        """use as @action.uses(auth.user)"""
        return AuthEnforcer(self)

    def condition(self, condition):
        """use as @action.uses(auth.condition(lambda user: True))"""
        return AuthEnforcer(self, condition)

    # utilities
    def get_user(self, safe=True):
        user = self.session.get('user')
        if not user or not isinstance(user, dict) or not 'id' in user:
            return None
        if len(user) == 1 and self.db:
            user = self.db.auth_user(user['id'])
            if safe:
                user = {f.name: user[f.name] for f in self.db.auth_user if f.readable}
        return user

    def register_plugin(self, plugin):
        self.plugins[plugin.name] = plugin

    def enable(self, route='auth/'):
        self.route = route
        """this assumes the bottle framework and exposes all actions as /{app_name}/auth/{path}"""
        def responder(path):
            return self.action(path, request.method, request.query, request.json)        
        action(route + '<path:path>', method=['GET','POST'])(action.uses(self)(responder))

    # handle http requests

    def action(self, path, method, get_vars, post_vars):
        if path.startswith('plugin/'):
            parts = path.split('/',2)
            plugin = self.plugins.get(parts[1])
            if plugin:
                return plugin.handle_request(self, parts[2], request.query, request.json)
            else:
                abort(404)
        if path.startswith('api/'):
            data = {}
            if method == 'GET':
                user = self.get_user(safe=True)
                if not user:
                    data = self._error('not authoried', 401)
                if path == 'api/profile':
                    return {'user': user}
            elif method == 'POST' and self.db:
                vars = dict(post_vars)
                user = self.get_user(safe=False)
                if path == 'api/register':
                    data = self.register(vars, send=True).as_dict()
                elif path == 'api/login':
                    # use PAM or LDAP
                    if 'pam' in self.plugins or 'ldap' in self.plugins:
                        # XXXX
                        username, password = vars.get('email'), vars.get('password')
                        if self.plugins['pam'].check_credentials(username, password):
                            data = {
                                'username': username,
                                'email': username + '@localhost',
                                'sso_id': 'pam:' + username,
                                }
                            # and register the user if we have one, just in case
                            if self.db:
                                data = self.get_or_register_user(data)
                        else:
                            data = self._error('Invalid Credentials')
                    # else use normal login
                    else:
                        user, error = self.login(**vars)
                        if user:
                            self.session['user'] = {'id': user.id}
                            user = {f.name: user[f.name] for f in self.db.auth_user if f.readable}
                            data = {'user': user}
                        else:
                            data = self._error(error)
                elif path == 'api/request_reset_password':
                    if not self.request_reset_password(**vars):
                        data = self._error('invalid user')
                elif path == 'api/reset_password':
                    if not self.reset_password(vars.get('token'), vars.get('new_password')):
                        data = self._error('invalid token, request expired')
                elif user and path == 'api/logout':
                    self.session['user'] = None
                elif user and path == 'api/unsubscribe':
                    self.session['user'] = None
                    self.gdpr_unsubscribe(user, send=True)
                elif user and path == 'api/change_password':
                    data = self.change_password(user, vars.get('new_password'), vars.get('password'))
                elif user and path == 'api/change_email':
                    data = self.change_email(user, vars.get('new_email'), vars.get('password'))
                elif user and path == 'api/update_profile':
                    data = self.update_profile(user, **vars)
                else:
                    data = {'status': 'error', 'message': 'undefined'}
            if not 'status' in data and data.get('errors'):
                data.update(status='error', message='validation errors', code=401)
            elif 'errors' in data and not data['errors']: 
                del data['errors']
            data['status'] = data.get('status', 'success')
            data['code'] = data.get('code', 200)
            return data
        elif path == 'logout':
            self.session['user'] = None
            # somehow call revoke for active plugin
        elif path == 'verify_email' and self.db:
            if self.verify_email(get_vars.get('token')):
                redirect(URL('auth/email_verified'))
            else:
                redirect(URL('auth/token_expired'))
        return Template('auth.html').transform({'path': path})        

    # methods that do not assume a user

    def register(self, fields, send=True):
        fields['username'] = fields.get('username', '').lower()
        fields['email'] = fields.get('email','').lower()
        token = str(uuid.uuid4())
        fields['action_token'] = 'pending-registration:%s' % token
        res = self.db.auth_user.validate_and_insert(**fields)        
        if send and res.get('id'):        
            self._link = link = URL(self.route + 'verify_email?token=' + token, scheme=True)
            self.send('verify_email', fields, link=link)
        return res

    def login(self, email, password):
        db = self.db
        value = email.lower()
        query = (db.auth_user.email == value) if '@' in value else (db.auth_user.username == value)
        user = db(query).select().first()
        if not user: return (None, 'Invalid email')
        if (user.action_token or '').startswith('pending-registration:'):
            return (None, 'Registration is pending')
        if (user.action_token or '').startswith('account-blocked:'):
            return (None, 'Account is blocked')
        if db.auth_user.password.requires(password)[0] == user.password:
            return (user, None)
        return None, 'Invalid Credentials'

    def request_reset_password(self, email, send=True):
        db = self.db
        value = email.lower()
        query = (db.auth_user.email == value) if '@' in value else (db.auth_user.username == value)
        user = db(query).select().first()
        if user and not user.action_token == 'account-blocked':
            token = str(uuid.uuid4())
            user.update_record(action_token='reset-password-request:'+token)
            if send:
                self._link = link = URL(self.route + 'api/reset_password?token=' + token, scheme=True)
                self.send('reset_password', user, link=link)
            return token

    def verify_email(self, token):
        n = self.db(self._query_from_token(token)).update(action_token=None)
        return n>0

    def reset_password(self, token, new_password):
        db = self.db
        query = self._query_from_token(token)
        user = db(query).select().first()
        if user:
            return db(db.auth_user.id==user.id).validate_and_update(password=new_password).as_dict()

    # methods that assume a user

    def change_password(self, user, new_password, password=None, check=True):
        db = self.db
        if check and not db.auth_user.password.requires(password)[0] == user.password:
            return {'errors': {'password': 'invalid'}}
        return db(db.auth_user.id==user.id).validate_and_update(password=new_password).as_dict()

    def change_email(self, user, new_email, password=None, check=True):
        db = self.db
        if check and not db.auth_user.password.requires(password)[0] == user.password:
            return {'errors': {'password': 'invalid'}}
        return db(db.auth_user.id==user.id).validate_and_update(email=new_email).as_dict()

    def update_profile(self, user, **fields):
        db = self.db
        errors = {k: 'invalid' for k in fields if k not in db.auth_user.fields or not db.auth_user[k].writable}
        if errors: return {'errors': errors}
        return db(db.auth_user.id==user.id).validate_and_update(**fields).as_dict()

    def gdpr_unsubscribe(self, user, send=True):
        """GDPR unsubscribe means we delete first_name, last_name,
        replace email with hash of the actual email and notify the user
        Essentially we lose the info about who is who
        Yet we have the ability to verify that a given email has unsubscribed 
        and maybe restore it if it was a mistake.
        Despite unsubscription we retain enought info to be able to comply
        with police audit for illecit activities.
        I am not a lwayer but I believe this to be OK.
        Check with your lwayr before using this feature.
        """
        user = user.as_dict()
        id = user['id']
        token = hashlib.sha1(user['email'].lower()).hexdigest()
        db = self.db
        db(db.auth_user.id==id).update(
            email="%s@example.com" % token,
            password=None,
            first_name='anonymous', 
            last_name='anonymous',
            sso_id=None,
            action_token='gdpr-unsubscribed')
        if send:
            self.send('unsubscribe', user)

    def is_gdpr_unsubscribed(self, email):
        db = self.db
        token = hashlib.sha1(email.lower()).hexdigest()
        email="%s@example.com" % token
        return db(db.auth_user.email==email).count() > 0

    def get_or_register_user(self, user):
        db = self.db
        row = db(db.auth_user.sso_id == user['sso_id']).select(limitby=(0,1)).first()
        if row:
            if any(user[key] != row[key] for key in user):
                row.update_record(**user)
            data['id'] = row['id']
        else:
            data = user
            data['id'] = db.auth_user.insert(**db.auth_user._filter_fields(user))
        return data

    # private methods

    def _query_from_token(self,token):
        query = self.db.auth_user.action_token == 'reset-password-request:' + token
        query |= self.db.auth_user.action_token == 'pending-registration:' + token
        return query

    def _error(self, message, code=400):
        return {'status': 'error', 'message': message, 'code': code}

    # other service methods (that can be overwritten)

    def send(self, name, user, **attrs):
        """extend the object and override this function to send messages with
        twilio or onesignal or alternative method other than email"""
        message = self.messages[name]
        d = dict(user)
        d.update(**attrs)
        email = user['email']
        subject = message['subject'].format(**d)
        body = message['body'].format(**d)        
        if not self.sender:
            print('Mock send to %s subject "%s" body:\n%s\n' % (email, subject, body))
            return True
        return self.sender.send(email, subject=subject, body=body)
