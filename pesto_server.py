#!/usr/bin/env python
import json
import subprocess
from typing import Optional

from pytarallo import Tarallo
from dotenv import load_dotenv
from io import StringIO
import os
from twisted.internet import reactor, protocol
from twisted.protocols.basic import LineOnlyReceiver
import threading
import logging

NAME = "turbofresa"

clients = dict()
clients_lock = threading.Lock()

running_commands = set()
running_commands_lock = threading.Lock()

TARALLO = None
disks_lock = threading.RLock()
disks = {}


class Disk:
    def __init__(self, lsblk, tarallo: Optional[Tarallo.Tarallo]):
        self._lsblk = lsblk
        if "path" not in self._lsblk:
            raise RuntimeError("lsblk did not provide path for this disk: " + self._lsblk)
        self._path = str(self._lsblk["path"])
        self._code = None
        self._item = None

        self._tarallo = tarallo
        self._get_code(False)
        self._get_item()

    def update_if_needed(self):
        if not self._code:
            self._get_code(True)
        self._get_item()

    def serialize_disk(self):
        result = self._lsblk
        result["code"] = self._code
        return result

    def update_status(self, status: str):
        if self._tarallo and self._code:
            self._tarallo.update_item_features(self._code, {"smart-data": status})

    def _get_code(self, stop_on_error: bool = True):
        if not self._tarallo:
            self._code = None
            return
        if "serial" not in self._lsblk:
            self._code = None
            if stop_on_error:
                raise ErrorThatCanBeManuallyFixed(f"Disk {self._path} has no serial number")

        sn = self._lsblk["serial"]
        sn: str
        if sn.startswith('WD-'):
            sn = sn[3:]

        codes = self._tarallo.get_codes_by_feature("sn", sn)
        if len(codes) <= 0:
            self._code = None
            logging.debug(f"Disk {sn} not found in tarallo")
        elif len(codes) == 1:
            self._code = codes[0]
            logging.debug(f"Disk {sn} found as {self._code}")
        else:
            self._code = None
            if stop_on_error:
                raise ErrorThatCanBeManuallyFixed(f"Duplicate codes for {self._path}: {' '.join(codes)}, S/N is {sn}")

    def _get_item(self):
        if self._tarallo and self._code:
            self._item = self._tarallo.get_item(self._code, 0)
        else:
            self._item = None


class ErrorThatCanBeManuallyFixed(BaseException):
    pass


class CommandRunner(threading.Thread):
    def __init__(self, cmd: str, args: str, the_id: int):
        threading.Thread.__init__(self)
        self._cmd = cmd
        self._args = args
        self._the_id = the_id
        self._go = False
        with running_commands_lock:
            running_commands.add(self)

    def run(self):
        try:
            self.exec_command(self._cmd, self._args, self._the_id)
        except BaseException as e:
            logging.getLogger(NAME).error(f"[{self._the_id}] BIG ERROR in command thread", exc_info=e)
        with running_commands_lock:
            running_commands.remove(self)

    def stop_asap(self):
        # This is completely pointless unless the command checks self._go
        # (none of them does, for now)
        self._go = False

    def exec_command(self, cmd: str, args: str, the_id: int):
        logging.getLogger(NAME)\
            .debug(f"[{the_id}] Received command {cmd}{' with args' if len(args) > 0 else ''}")  # in {self.getName()}
        param = None
        if cmd == 'smartctl':
            param = self.get_smartctl(args, the_id)
        elif cmd == 'get_disks':
            param = self.get_disks_to_send(the_id)
        elif cmd == 'get_disks_win':
            param = get_disks_win()
        elif cmd == 'ping':
            cmd = "pong"
        else:
            param = {"message": "Unrecognized command", "command": cmd}
            # Do not move this line above the other, cmd has to be overwritten here
            cmd = "error"
        self.send_msg(the_id, cmd, param)

    def get_smartctl(self, dev: str, the_id: int):
        pipe = subprocess\
            .Popen(("sudo", "smartctl", "-a", dev), shell=True, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        output = pipe.stdout.read().decode('utf-8')
        stderr = pipe.stderr.read().decode('utf-8')
        exitcode = pipe.wait()

        updated = False
        if exitcode == 0:
            status = get_smartctl_status(output)
            with disks_lock:
                if dev in disks:
                    self.update_disk_if_needed(the_id, disks[dev])
                    # noinspection PyBroadException
                    try:
                        disks[dev].update_status(status)
                        updated = True
                    except BaseException as e:
                        logging.warning(f"[{the_id}] Cannot update status of {dev} on tarallo", exc_info=e)
        else:
            status = None
        return {
            "disk": dev,
            "status": status,
            "updated": updated,
            "exitcode": exitcode,
            "output": output,
            "stderr": stderr,
        }

    def update_disk_if_needed(self, the_id: int, disk: Disk):
        # TODO: a more granular lock is possible, here. But is it really needed?
        with disks_lock:
            # noinspection PyBroadException
            try:
                disk.update_if_needed()
            except ErrorThatCanBeManuallyFixed as e:
                self.send_msg(the_id, "error_that_can_be_manually_fixed", {"message": str(e), "disk": disk})
            except BaseException:
                pass

    @staticmethod
    def _encode_param(param):
        return json.dumps(param, separators=(',', ':'), indent=None)

    def send_msg(self, client_id: int, cmd: str, param=None):
        with clients_lock:
            thread = clients.get(client_id)
            if thread is None:
                logging.getLogger(NAME)\
                    .info(f"[{client_id}] Connection already closed while trying to send {cmd}")
            else:
                thread: TurboProtocol
                if param is None:
                    response_string = cmd
                else:
                    j_param = self._encode_param(param)
                    response_string = f"{cmd} {j_param}"
                # It's there but pycharm doesn't believe it
                # noinspection PyUnresolvedReferences
                reactor.callFromThread(TurboProtocol.send_msg, thread, response_string)

    def get_disks_to_send(self, the_id: int):
        result = []
        with disks_lock:
            for disk in disks:
                if disks[disk] is None:
                    lsblk = get_disks(disk)
                    if len(lsblk) > 0:
                        lsblk = lsblk[0]
                    # noinspection PyBroadException
                    try:
                        disks[disk] = Disk(lsblk, TARALLO)
                    except BaseException as e:
                        logging.warning(f"Error with disk {disk} still remains", exc_info=e)
                if disks[disk] is not None:
                    self.update_disk_if_needed(the_id, disks[disk])
                    result.append(disks[disk].serialize_disk())
        return result


class TurboProtocol(LineOnlyReceiver):
    def __init__(self):
        self._id = -1
        self.delimiter = b'\n'
        self._delimiter_found = False

    def connectionMade(self):
        self._id = self.factory.conn_id
        self.factory.conn_id += 1
        with running_commands_lock:
            with clients_lock:
                clients[self._id] = self
        logging.getLogger(NAME).debug(f"[{str(self._id)}] Client connected")

    def connectionLost(self, reason=protocol.connectionDone):
        logging.getLogger(NAME).debug(f"[{str(self._id)}] Client disconnected")
        with running_commands_lock:
            with clients_lock:
                del clients[self._id]

    def lineReceived(self, line):
        try:
            line = line.decode('utf-8')
        except UnicodeDecodeError as e:
            logging.getLogger(NAME).warning(f"[{str(self._id)}] Oh no, UnicodeDecodeError!", exc_info=e)
            return

        # \n is stripped by twisted, but with \r\n the \r is still there
        if not self._delimiter_found:
            if len(line) > 0 and line[-1] == '\r':
                self.delimiter = b'\r\n'
                logging.getLogger(NAME).debug(f"[{str(self._id)}] Client has delimiter \\r\\n")
            else:
                logging.getLogger(NAME).debug(f"[{str(self._id)}] Client has delimiter \\n")
            self._delimiter_found = True

        # Strip \r on first message (if \r\n) and any trailing whitespace
        line = line.strip()
        if line.startswith('exit'):
            self.transport.loseConnection()
        else:
            parts = line.split(' ', 1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ''
            cr = CommandRunner(cmd, args, self._id)
            with running_commands_lock:
                running_commands.add(cr)
            cr.start()

    def send_msg(self, response: str):
        if self._delimiter_found:
            self.sendLine(response.encode('utf-8'))
        else:
            logging.getLogger(NAME)\
                .warning(f"[{str(self._id)}] Cannot send command to client due to unknown delimiter: {response}")


def scan_for_disks():
    logging.debug("Scanning for disks")
    disks_lsblk = get_disks()
    for disk in disks_lsblk:
        if "path" not in disk:
            logging.warning("Disk has no path, ignoring: " + disk)
            continue
        path = disk["path"]
        # noinspection PyBroadException
        try:
            with disks_lock:
                disks[path] = Disk(disk, TARALLO)
        except BaseException as e:
            logging.warning("Exception while scanning disk, skipping", exc_info=e)


def main():
    load_settings()
    scan_for_disks()
    ip = os.getenv("IP")
    port = os.getenv("PORT")

    try:
        factory = protocol.ServerFactory()
        factory.protocol = TurboProtocol
        factory.conn_id = 0

        logging.getLogger(NAME).info(f"Listening on {ip} port {port}")
        # noinspection PyUnresolvedReferences
        reactor.listenTCP(int(port), factory, interface=ip)

        # noinspection PyUnresolvedReferences
        reactor.run()
    except KeyboardInterrupt:
        print("KeyboardInterrupt, terminating")
    finally:
        # TODO: reactor has already stopped here, but threads may send messages... what happens? A big crash, right?
        while len(running_commands) > 0:
            with running_commands_lock:
                thread_to_stop = next(iter(running_commands))
            thread_to_stop: CommandRunner
            thread_to_stop.stop_asap()
            thread_to_stop.join()


def load_settings():
    # Load in order each file if exists, variables are not overwritten
    load_dotenv('.env')
    load_dotenv(f'~/.conf/WEEE-Open/{NAME}.conf')
    load_dotenv(f'/etc/{NAME}.conf')
    # Defaults
    config = StringIO("IP=127.0.0.1\nPORT=1030\nLOGLEVEL=INFO")
    load_dotenv(stream=config)

    logging.basicConfig(format='%(message)s', level=getattr(logging, os.getenv("LOGLEVEL").upper()))

    url = os.getenv('TARALLO_URL') or logging.warning('TARALLO_URL is not set, tarallo will be unavailable')
    token = os.getenv('TARALLO_TOKEN') or logging.warning('TARALLO_TOKEN is not set, tarallo will be unavailable')

    if url and token:
        global TARALLO
        TARALLO = Tarallo.Tarallo(url, token)


def get_disks_win():
    label = []
    size = []
    drive = []
    for line in subprocess.getoutput("wmic logicaldisk get caption").splitlines():
        if line.rstrip() != 'Caption' and line.rstrip() != '':
            label.append(line.rstrip())
    for line in subprocess.getoutput("wmic logicaldisk get size").splitlines():
        if line.rstrip() != 'Size' and line.rstrip() != '':
            size.append(line)
    for idx, line in enumerate(size):
        drive += [[label[idx], line]]
    return drive


def get_smartctl_status(output):
    # TODO: implement (move code from client here)
    return 'old'


def find_mounts(el: dict):
    mounts = []
    if el["mountpoint"] is not None:
        mounts.append(el["mountpoint"])
    if "children" in el:
        children = el["children"]
        for child in children:
            mounts += find_mounts(child)
    return mounts


def get_disks(path: Optional[str] = None) -> list:
    # Name is required, otherwise the tree is flattened
    output = subprocess\
        .getoutput(f"lsblk -o NAME,PATH,VENDOR,MODEL,SERIAL,HOTPLUG,ROTA,MOUNTPOINT -J {path if path else ''}")
    jsonized = json.loads(output)
    if "blockdevices" in jsonized:
        result = jsonized["blockdevices"]
    else:
        result = []
    for el in result:
        mounts = find_mounts(el)
        if "children" in el:
            del el["children"]
        if "name" in el:
            del el["name"]
        el["mountpoint"] = mounts

    return result


if __name__ == '__main__':
    main()
