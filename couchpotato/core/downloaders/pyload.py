from base64 import b16encode, b32decode
from datetime import timedelta
from hashlib import sha1
from requests.utils import quote
import cookielib
import httplib
import json
import os
import re
import stat
import time
import urllib
import urllib2
import requests

from couchpotato.core._base.downloader.main import DownloaderBase, ReleaseDownloadList
from couchpotato.core.helpers.encoding import isInt, ss, sp
from couchpotato.core.helpers.variable import tryInt, tryFloat, cleanHost
from couchpotato.core.logger import CPLog

log = CPLog(__name__)

autoload = 'pyload'


class pyload(DownloaderBase):

    protocol = ['torrent', 'torrent_magnet']
    pyload_api = None
    status_flags = {
        'STARTED': 1,
        'CHECKING': 2,
        'CHECK-START': 4,
        'CHECKED': 8,
        'ERROR': 16,
        'PAUSED': 32,
        'QUEUED': 64,
        'LOADED': 128
    }

    def connect(self):
        # Load host from config and split out port.
        host = cleanHost(self.conf('host'), protocol=False).split(':')
        if not isInt(host[1]):
            log.error(
                'Config properties are not filled in correctly, port is missing.')
            return False

        self.pyload_api = pyloadAPI(host[0], port=host[1], username=self.conf(
            'username'), password=self.conf('password'))

        self.pyload_api.connect()

        return self.pyload_api

    def download(self, data=None, media=None, filedata=None):

        if not media:
            media = {}
        if not data:
            data = {}

        log.debug("Sending '%s' (%s) to pyload.",
                  (data.get('name'), data.get('protocol')))

        if not self.connect():
            return False

        torrent_params = {}
        if self.conf('label'):
            torrent_params['label'] = self.conf('label')

        if not filedata and data.get('protocol') == 'torrent':
            log.error('Failed sending torrent, no data')
            return False

        torrent_filename = self.createFileName(data, filedata, media)

        # Send request to pyload
        packageId = ''
        if data.get('protocol') == 'torrent_magnet':
            packageId = self.pyload_api.add_torrent_uri(
                torrent_filename, data.get('url'))
        else:
            packageId = self.pyload_api.add_torrent_file(
                torrent_filename, filedata)
        
        if packageId != -1:
            return self.downloadReturnId(packageId)

        return False

    def test(self):
        """ Check if connection works
        :return: bool
        """

        if self.connect():
            return self.pyload_api.connected

        return False

    def getAllDownloadStatus(self, ids):
        """ Get status of all active downloads

        :param ids: list of (mixed) downloader ids
            Used to match the releases for this downloader as there could be
            other downloaders active that it should ignore
        :return: list of releases
        """

        log.debug('Checking pyload download status.')

        if not self.connect():
            return []

        release_downloads = ReleaseDownloadList(self)

        data = self.pyload_api.get_status()
        if not data:
            log.error('Error getting data from pyload')
            return []

        queue = json.loads(data)
        if queue.get('error'):
            log.error('Error getting data from pyload: %s', queue.get('error'))
            return []

        if not queue.get('torrents'):
            log.debug('Nothing in queue')
            return []

        # Get torrents
        for torrent in queue['torrents']:
            if torrent[0] in ids:

                #Get files of the torrent
                torrent_files = []
                try:
                    torrent_files = json.loads(
                        self.pyload_api.get_files(torrent[0]))
                    torrent_files = [sp(os.path.join(torrent[26], torrent_file[0]))
                                     for torrent_file in torrent_files['files'][1]]
                except:
                    log.debug(
                        'Failed getting files from torrent: %s', torrent[2])

                status = 'busy'
                if (torrent[1] & self.status_flags['STARTED'] or torrent[1] & self.status_flags['QUEUED']) and torrent[4] == 1000:
                    status = 'seeding'
                elif torrent[1] & self.status_flags['ERROR'] and 'There is not enough space on the disk' not in torrent[21]:
                    status = 'failed'
                elif torrent[4] == 1000:
                    status = 'completed'

                if not status == 'busy':
                    self.removeReadOnly(torrent_files)

                release_downloads.append({
                    'id': torrent[0],
                    'name': torrent[2],
                    'status': status,
                    'seed_ratio': float(torrent[7]) / 1000,
                    'original_status': torrent[1],
                    'timeleft': str(timedelta(seconds=torrent[10])),
                    'folder': sp(torrent[26]),
                    'files': torrent_files
                })

        return release_downloads

    def pause(self, release_download, pause=True):
        if not self.connect():
            return False
        return self.pyload_api.pause_torrent(release_download['id'], pause)

    def removeFailed(self, release_download):
        log.info('%s failed downloading, deleting...',
                 release_download['name'])
        if not self.connect():
            return False
        return self.pyload_api.remove_torrent(release_download['id'], remove_data=True)

    def processComplete(self, release_download, delete_files=False):
        log.debug('Requesting pyload to remove the torrent %s%s.',
                  (release_download['name'], ' and cleanup the downloaded files' if delete_files else ''))
        if not self.connect():
            return False
        return self.pyload_api.remove_torrent(release_download['id'], remove_data=delete_files)

    def removeReadOnly(self, files):
        #Removes all read-on ly flags in a for all files
        for filepath in files:
            if os.path.isfile(filepath):
                #Windows only needs S_IWRITE, but we bitwise-or with current perms to preserve other permission bits on Linux
                os.chmod(filepath, stat.S_IWRITE | os.stat(filepath).st_mode)


class pyloadAPI(object):

    def __init__(self, host='192.168.1.40', port=8000, username=None, password=None):

        super(pyloadAPI, self).__init__()

        self.url = 'http://' + str(host) + ':' + str(port) + '/api/'
        self.token = ''
        self.last_time = time.time()
        self.username = username
        self.password = password
        self.session = requests.session()
        self.connected = False

    def _request(self, request, vars):
		try:
			response = self.session.post(request, data=vars)
			return response
		except:
			return None

    def request(self, request, vars={}):
		response = self._request(request, vars)
		if response != None and response.status_code == 403:
			self.connect()
			response = self._request(request, vars)
		return response

    def connect(self):
        self.session = requests.session()
        payload = {"username": self.username, "password": self.password}

        result = self.session.post(self.url+"login", data=payload)

        if result == None:
			return 12

        if result.status_code != 200:
			return 11

        if self.session == False:
			return 10

        self.connected = True

        return 0

    def get_api(self, api, vars={}):
        result = self.request(self.url+api, vars)
        if result == None:
            return -1

        if result.status_code != 200:
            return -1
        return json.loads(result.text)

    def pause_torrent(self, pid, pause=False):
        return ""

    def remove_torrent(self, pid, remove_data=False):
        vars = {'pids': json.dumps(pid)}
        return self.get_api('deletePackages', vars)

    def add_torrent_uri(self, filename, torrent, add_folder=False):
        payload = {'name': filename, 'links': [quote(torrent, safe='')]}
        payloadJSON = {k: json.dumps(v) for k, v in payload.items()}
        return self.get_api('addPackage', payloadJSON)

    def add_torrent_file(self, filename, filedata, add_folder=False):
        vars = {'filename': json.dumps(filename)}
        vars['data'] = (ss(filename), filedata)
        return self.get_api('uploadContainer', vars)

    def get_status(self):
        self.get_api('statusServer')

    def get_files(self, hash):
        action = 'action=getfiles&hash=%s' % hash
        return -1


config = [{
    'name': 'pyload',
    'groups': [
        {
            'tab': 'downloaders',
            'list': 'download_providers',
            'name': 'pyload',
            'label': 'pyload',
            'description': 'Use <a href="http://www.pyload.com/" target="_blank">pyload</a> (3.0+) to download torrents.',
            'wizard': True,
            'options': [
                {
                    'name': 'enabled',
                    'default': 0,
                    'type': 'enabler',
                    'radio_group': 'torrent',
                },
                {
                    'name': 'host',
                    'default': 'localhost:8000',
                    'description': 'Port can be found in settings when enabling WebUI.',
                },
                {
                    'name': 'username',
                },
                {
                    'name': 'password',
                    'type': 'password',
                },
                {
                    'name': 'label',
                    'description': 'Label to add torrent as.',
                },
                {
                    'name': 'remove_complete',
                    'label': 'Remove torrent',
                    'default': True,
                    'advanced': True,
                    'type': 'bool',
                    'description': 'Remove the torrent from pyload after it finished seeding.',
                },
                {
                    'name': 'delete_files',
                    'label': 'Remove files',
                    'default': True,
                    'type': 'bool',
                    'advanced': True,
                    'description': 'Also remove the leftover files.',
                },
                {
                    'name': 'paused',
                    'type': 'bool',
                    'advanced': True,
                    'default': False,
                    'description': 'Add the torrent paused.',
                },
                {
                    'name': 'manual',
                    'default': 0,
                    'type': 'bool',
                    'advanced': True,
                    'description': 'Disable this downloader for automated searches, but use it when I manually send a release.',
                },
                {
                    'name': 'delete_failed',
                    'default': True,
                    'advanced': True,
                    'type': 'bool',
                    'description': 'Delete a release after the download has failed.',
                },
            ],
        }
    ],
}]
