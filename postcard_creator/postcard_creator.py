import logging
import requests
import json
from bs4 import BeautifulSoup
from requests_toolbelt.utils import dump
import datetime
from PIL import Image
from io import BytesIO
from resizeimage import resizeimage
import pkg_resources
import math
import os
from time import gmtime, strftime
import re

LOGGING_TRACE_LVL = 5
logger = logging.getLogger('postcard_creator')
logging.addLevelName(LOGGING_TRACE_LVL, 'TRACE')
setattr(logger, 'trace', lambda *args: logger.log(LOGGING_TRACE_LVL, *args))


def _trace_request(response):
    data = dump.dump_all(response)
    try:
        logger.trace(data.decode())
    except Exception:
        data = str(data).replace('\\r\\n', '\r\n')
        logger.trace(data)


class PostcardCreatorException(Exception):
    server_response = None


class Token(object):
    def __init__(self, _protocol='https://'):
        self.protocol = _protocol
        self.base = '{}account.post.ch'.format(self.protocol)
        self.swissid = '{}login.swissid.ch'.format(self.protocol)
        self.token_url = '{}postcardcreator.post.ch/saml/SSO/alias/defaultAlias'.format(self.protocol)
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 6.0.1; wv) AppleWebKit/537.36 (KHTML, like Gecko) ' +
                          'Version/4.0 Chrome/52.0.2743.98 Mobile Safari/537.36',
            'Origin': '{}account.post.ch'.format(self.protocol)
        }

        # cache_filename = 'pcc_cache.json'

        self.token = None
        self.token_type = None
        self.token_expires_in = None
        self.token_fetched_at = None
        self.cache_token = False

    def _create_session(self):
        return requests.Session()

    def has_valid_credentials(self, username, password):
        try:
            self.fetch_token(username, password)
            return True
        except PostcardCreatorException:
            return False

    # def store_token_to_cache(self, key, token):
    #
    # def check_token_in_cache(self, username, password):
    #     tmp_dir = tempfile.gettempdir()
    #     tmp_path = os.path.join(tmp_dir, self.cache_filename)
    #     tmp_file = Path(tmp_path)
    #
    #     if tmp_file.exists():
    #         cache_content = open(tmp_file, "r").read()
    #         cache = []
    #         try:
    #             cache = json.load(cache_content)
    #         except Exception:
    #             return None
    #

    def fetch_token(self, username, password):
        logger.debug('fetching postcard account token')

        if username is None or password is None:
            raise PostcardCreatorException('No username/ password given')

        # if self.cache_token:
        #     self.check_token_in_cache(username, password)

        # try first to authenticate with Post account, if it fails, try SwissID
        session = None
        saml_response = None
        try:
            session = self._create_session()
            saml_response = self._get_saml_response(session, username, password)
        except PostcardCreatorException:
            session = self._create_session()
            saml_response = self._swissid_get_saml_response(session, username, password)
        
        payload = {
            'RelayState': '{}postcardcreator.post.ch?inMobileApp=true&inIframe=false&lang=en'.format(self.protocol),
            'SAMLResponse': saml_response
        }

        response = session.post(url=self.token_url, headers=self.headers, data=payload)
        logger.debug(' post {}'.format(self.token_url))
        _trace_request(response)

        try:
            if response.status_code is not 200:
                raise PostcardCreatorException()

            access_token = json.loads(response.text)
            self.token = access_token['access_token']
            self.token_type = access_token['token_type']
            self.token_expires_in = access_token['expires_in']
            self.token_fetched_at = datetime.datetime.now()

        except PostcardCreatorException:
            e = PostcardCreatorException(
                'Could not get access_token. Something broke. '
                'set increase debug verbosity to debug why')
            e.server_response = response.text
            raise e

        logger.debug('username/password authentication was successful')

    def _get_saml_response(self, session, username, password):
        url = '{}/SAML/IdentityProvider/'.format(self.base)
        query = '?login&app=pcc&service=pcc&targetURL=https%3A%2F%2Fpostcardcreator.post.ch' + \
                '&abortURL=https%3A%2F%2Fpostcardcreator.post.ch&inMobileApp=true'
        data = {
            'isiwebuserid': username,
            'isiwebpasswd': password,
            'confirmLogin': ''
        }
        response1 = session.get(url=url + query, headers=self.headers)
        _trace_request(response1)
        logger.debug(' get {}'.format(url))

        response2 = session.post(url=url + query, headers=self.headers, data=data)
        _trace_request(response2)
        logger.debug(' post {}'.format(url))

        response3 = session.post(url=url + query, headers=self.headers)
        _trace_request(response3)
        logger.debug(' post {}'.format(url))

        if any(e.status_code is not 200 for e in [response1, response2, response3]):
            raise PostcardCreatorException('Wrong user credentials')

        soup = BeautifulSoup(response3.text, 'html.parser')
        saml_response = soup.find('input', {'name': 'SAMLResponse'})

        if saml_response is None or saml_response.get('value') is None:
            raise PostcardCreatorException('Username/password authentication failed. '
                                           'Are your credentials valid?.')

        return saml_response.get('value')

    def _swissid_get_saml_response(self, session, username, password):
        url = '{}/SAML/IdentityProvider/'.format(self.base)
        query = '?login&app=pcc&service=pcc&targetURL=https%3A%2F%2Fpostcardcreator.post.ch' + \
                '&abortURL=https%3A%2F%2Fpostcardcreator.post.ch&inMobileApp=true'

        response1 = session.get(url=url + query)
        logger.debug(' step 1, GET {}'.format(url + query))

        data2 = {
            'isPilotPhase': 'true',
            'isiwebuserid': '',
            'isiwebpasswd': '',
            'externalIDP': 'externalIDP',
            'nevisdialog': 'password'
        }
        response2 = session.post(url=url + query, data=data2)
        logger.debug(' step 2, POST {}'.format(url + query))

        # extract this goto parameter from the previous redirection to generate
        # the next url request
        goto_param = re.search('&goto=([^&]+)', response2.history[3].url).group(1)
        newurl = 'https://login.swissid.ch/idp/json/authenticate?realm=/SESAM&locale=en&service=Sesam-LDAP&goto={}&authIndexType=service&authIndexValue=Sesam-LDAP'.format(goto_param)
        response3 = session.post(newurl)
        logger.debug(' step 3, POST {}'.format(newurl))

        # get the JSON blob, update the username and send back
        data4 = response3.json()
        data4['callbacks'][2]['input'][0]['value'] = username
        json_type = {'Content-Type': 'application/json'}
        response4 = session.post(newurl, headers=json_type, data=json.dumps(data4))
        logger.debug(' step 4, POST {}'.format(newurl))

        # get the new JSON blob, update the password and send back
        data5 = response4.json()
        try:
            data5['callbacks'][3]['input'][0]['value'] = password
        except KeyError:
            raise PostcardCreatorException('Oops, is your email valid?')
        response5 = session.post(newurl, headers=json_type, data=json.dumps(data5))
        logger.debug(' step 5, POST {}'.format(newurl))

        # update session with the token we receive and request successUrl
        try:
            session.cookies.update({'swissid': response5.json()['tokenId']})
            success_url = response5.json()['successUrl']
        except KeyError:
            raise PostcardCreatorException('Oops, is your password valid?')
        response6 = session.get(success_url)
        logger.debug(' step 6, GET {}'.format(success_url))

        # final POST request to get the SAMLResponse
        response7 = session.post(url=url + query)
        logger.debug(' step 7, POST {}'.format(url + query))

        if any(e.status_code is not 200 for e in [response1, response2,
            response3, response4, response5, response6, response7]):
            raise PostcardCreatorException('Issue during authentication process, wrong credentials?')

        soup = BeautifulSoup(response7.text, 'html.parser')
        saml_response = soup.find('input', {'name': 'SAMLResponse'})

        if saml_response is None or saml_response.get('value') is None:
            raise PostcardCreatorException('Username/password authentication failed. '
                                           'Are your credentials valid?.')

        return saml_response.get('value')

    def to_json(self):
        return {
            'fetched_at': self.token_fetched_at,
            'token': self.token,
            'expires_in': self.token_expires_in,
            'type': self.token_type,
        }


class Sender(object):
    def __init__(self, prename, lastname, street, zip_code, place, company='', country=''):
        self.prename = prename
        self.lastname = lastname
        self.street = street
        self.zip_code = zip_code
        self.place = place
        self.company = company
        self.country = country

    def is_valid(self):
        return all(field for field in [self.prename, self.lastname, self.street, self.zip_code, self.place])


class Recipient(object):
    def __init__(self, prename, lastname, street, zip_code, place, company='', company_addition='', salutation=''):
        self.salutation = salutation
        self.prename = prename
        self.lastname = lastname
        self.street = street
        self.zip_code = zip_code
        self.place = place
        self.company = company
        self.company_addition = company_addition

    def is_valid(self):
        return all(field for field in [self.prename, self.lastname, self.street, self.zip_code, self.place])

    def to_json(self):
        return {'recipientFields': [
            {'name': 'Salutation', 'addressField': 'SALUTATION'},
            {'name': 'Given Name', 'addressField': 'GIVEN_NAME'},
            {'name': 'Family Name', 'addressField': 'FAMILY_NAME'},
            {'name': 'Company', 'addressField': 'COMPANY'},
            {'name': 'Company', 'addressField': 'COMPANY_ADDITION'},
            {'name': 'Street', 'addressField': 'STREET'},
            {'name': 'Post Code', 'addressField': 'ZIP_CODE'},
            {'name': 'Place', 'addressField': 'PLACE'}],
            'recipients': [
                [self.salutation, self.prename,
                 self.lastname, self.company,
                 self.company_addition, self.street,
                 self.zip_code, self.place]]}


class Postcard(object):
    def __init__(self, sender, recipient, picture_stream, message=''):
        self.recipient = recipient
        self.message = message
        self.picture_stream = picture_stream
        self.sender = sender
        self.frontpage_layout = pkg_resources.resource_string(__name__, 'page_1.svg').decode('utf-8')
        self.backpage_layout = pkg_resources.resource_string(__name__, 'page_2.svg').decode('utf-8')

    def is_valid(self):
        return self.recipient is not None \
               and self.recipient.is_valid() \
               and self.sender is not None \
               and self.sender.is_valid()

    def validate(self):
        if self.recipient is None or not self.recipient.is_valid():
            raise PostcardCreatorException('Not all required attributes in recipient set')
        if self.recipient is None or not self.recipient.is_valid():
            raise PostcardCreatorException('Not all required attributes in sender set')

    def get_frontpage(self, asset_id):
        return self.frontpage_layout.replace('{asset_id}', str(asset_id))

    def get_backpage(self):
        svg = self.backpage_layout
        return svg \
            .replace('{first_name}', self.recipient.prename) \
            .replace('{last_name}', self.recipient.lastname) \
            .replace('{company}', self.recipient.company) \
            .replace('{company_addition}', self.recipient.company_addition) \
            .replace('{street}', self.recipient.street) \
            .replace('{zip_code}', str(self.recipient.zip_code)) \
            .replace('{place}', self.recipient.place) \
            .replace('{sender_company}', self.sender.company) \
            .replace('{sender_name}', self.sender.prename + ' ' + self.sender.lastname) \
            .replace('{sender_address}', self.sender.street) \
            .replace('{sender_zip_code}', str(self.sender.zip_code)) \
            .replace('{sender_place}', self.sender.place) \
            .replace('{sender_country}', self.sender.country) \
            .replace('{message}',
                     self.message.encode('ascii', 'xmlcharrefreplace').decode('utf-8'))  # escape umlaute


def _send_free_card_defaults(func):
    def wrapped(*args, **kwargs):
        kwargs['image_target_width'] = kwargs.get('image_target_width') or 154
        kwargs['image_target_height'] = kwargs.get('image_target_height') or 111
        kwargs['image_quality_factor'] = kwargs.get('image_quality_factor') or 20
        kwargs['image_rotate'] = kwargs.get('image_rotate') or True
        kwargs['image_export'] = kwargs.get('image_export') or False
        return func(*args, **kwargs)

    return wrapped


class PostcardCreator(object):
    def __init__(self, token=None, _protocol='https://'):
        if token.token is None:
            raise PostcardCreatorException('No Token given')
        self.token = token
        self.protocol = _protocol
        self.host = '{}postcardcreator.post.ch/rest/2.1'.format(self.protocol)
        self._session = self._create_session()

    def _get_headers(self):
        return {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 6.0.1; wv) AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Version/4.0 Chrome/52.0.2743.98 Mobile Safari/537.36',
            'Authorization': 'Bearer {}'.format(self.token.token)
        }

    def _create_session(self):
        return requests.Session()

    def _do_op(self, method, endpoint, **kwargs):
        url = self.host + endpoint
        if 'headers' not in kwargs or kwargs['headers'] is None:
            kwargs['headers'] = self._get_headers()

        logger.debug('{}: {}'.format(method, url))
        response = self._session.request(method, url, **kwargs)
        _trace_request(response)

        if response.status_code not in [200, 201, 204]:
            e = PostcardCreatorException('error in request {} {}. status_code: {}'
                                         .format(method, url, response.status_code))
            e.server_response = response.text
            raise e
        return response

    def get_user_info(self):
        logger.debug('fetching user information')
        endpoint = '/users/current'
        return self._do_op('get', endpoint).json()

    def get_billing_saldo(self):
        logger.debug('fetching billing saldo')

        user = self.get_user_info()
        endpoint = '/users/{}/billingOnlineAccountSaldo'.format(user["userId"])
        return self._do_op('get', endpoint).json()

    def get_quota(self):
        logger.debug('fetching quota')

        user = self.get_user_info()
        endpoint = '/users/{}/quota'.format(user["userId"])
        return self._do_op('get', endpoint).json()

    def has_free_postcard(self):
        return self.get_quota()['available']

    @_send_free_card_defaults
    def send_free_card(self, postcard, mock_send=False, **kwargs):
        if not self.has_free_postcard():
            raise PostcardCreatorException('Limit of free postcards exceeded. Try again tomorrow at '
                                           + self.get_quota()['next'])
        if not postcard:
            raise PostcardCreatorException('Postcard must be set')

        postcard.validate()
        user = self.get_user_info()
        user_id = user['userId']
        card_id = self._create_card(user)

        picture_stream = self._rotate_and_scale_image(postcard.picture_stream, **kwargs)
        asset_response = self._upload_asset(user, picture_stream=picture_stream)
        self._set_card_recipient(user_id=user_id, card_id=card_id, postcard=postcard)
        self._set_svg_page(1, user_id, card_id, postcard.get_frontpage(asset_id=asset_response['asset_id']))
        self._set_svg_page(2, user_id, card_id, postcard.get_backpage())

        if mock_send:
            response = False
            logger.debug('postcard was not sent because flag mock_send=True')
        else:
            response = self._do_order(user_id, card_id)
            logger.debug('postcard sent for printout')

        return response

    def _create_card(self, user):
        endpoint = '/users/{}/mailings'.format(user["userId"])

        mailing_payload = {
            'name': 'Mobile App Mailing {}'.format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
            'addressFormat': 'PERSON_FIRST',
            'paid': False
        }

        mailing_response = self._do_op('post', endpoint, json=mailing_payload)
        return mailing_response.headers['Location'].partition('mailings/')[2]

    def _upload_asset(self, user, picture_stream):
        logger.debug('uploading postcard asset')
        endpoint = '/users/{}/assets'.format(user["userId"])

        files = {
            'title': (None, 'Title of image'),
            'asset': ('asset.png', picture_stream, 'image/jpeg')
        }
        headers = self._get_headers()
        headers['Origin'] = 'file://'
        response = self._do_op('post', endpoint, files=files, headers=headers)
        asset_id = response.headers['Location'].partition('user/')[2]

        return {
            'asset_id': asset_id,
            'response': response
        }

    def _set_card_recipient(self, user_id, card_id, postcard):
        logger.debug('set recipient for postcard')
        endpoint = '/users/{}/mailings/{}/recipients'.format(user_id, card_id)
        return self._do_op('put', endpoint, json=postcard.recipient.to_json())

    def _set_svg_page(self, page_number, user_id, card_id, svg_content):
        logger.debug('set svg template ' + str(page_number) + ' for postcard')
        endpoint = '/users/{}/mailings/{}/pages/{}'.format(user_id, card_id, page_number)

        headers = self._get_headers()
        headers['Origin'] = 'file://'
        headers['Content-Type'] = 'image/svg+xml'
        return self._do_op('put', endpoint, data=svg_content, headers=headers)

    def _do_order(self, user_id, card_id):
        logger.debug('submit postcard to be printed and delivered')
        endpoint = '/users/{}/mailings/{}/order'.format(user_id, card_id)
        return self._do_op('post', endpoint, json={})

    def _rotate_and_scale_image(self, file, image_target_width=154, image_target_height=111,
                                image_quality_factor=20, image_rotate=True, image_export=False):

        with Image.open(file) as image:
            if image_rotate and image.width < image.height:
                image = image.rotate(90, expand=True)
                logger.debug('rotating image by 90 degrees')

            if image.width < image_quality_factor * image_target_width \
                    or image.height < image_quality_factor * image_target_height:
                factor_width = math.floor(image.width / image_target_width)
                factor_height = math.floor(image.height / image_target_height)
                factor = min([factor_height, factor_width])

                logger.debug('image is smaller than default for resize/fill. '
                             'using scale factor {} instead of {}'.format(factor, image_quality_factor))
                image_quality_factor = factor

            width = image_target_width * image_quality_factor
            height = image_target_height * image_quality_factor
            logger.debug('resizing image from {}x{} to {}x{}'
                         .format(image.width, image.height, width, height))

            cover = resizeimage.resize_cover(image, [width, height], validate=True)
            with BytesIO() as f:
                cover.save(f, 'PNG')
                scaled = f.getvalue()

            if image_export:
                name = strftime("postcard_creator_export_%Y-%m-%d_%H-%M-%S.jpg", gmtime())
                path = os.path.join(os.getcwd(), name)
                logger.info('exporting image to {} (image_export=True)'.format(path))
                cover.save(path)

        return scaled


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(name)s (%(levelname)s): %(message)s')
    logging.getLogger('postcard_creator').setLevel(logging.DEBUG)
