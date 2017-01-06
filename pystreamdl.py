#!/usr/bin/env python

import shutil
import threading
import json
import copy
import time
import os
import pycurl
import io

class StreamDLError(Exception):
    pass

class StreamDLCorruptionError(StreamDLError):
    pass

class StreamDLAPIError(StreamDLError):
    pass

class StreamDLDownloadError(StreamDLError):
    pass
    
size_suffixes = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
speed_suffixes = ['Bit/s', 'Kibit/s', 'Mibit/s', 'Gibit/s', 'Tibit/s', 'Pibit/s']
def human_size(nbytes, suffixes=size_suffixes, fraction_digits=0):
    if nbytes is None:
        return None
    if nbytes == 0: return '0 ' + suffixes[0]
    i = 0
    while nbytes >= 1024 and i < len(suffixes)-1:
        nbytes /= 1024.
        i += 1
    f = ('{0:.'+ str(fraction_digits) +'f}').format(nbytes).rstrip('.')
    return '%s %s' % (f, suffixes[i])


class StreamDLDownloadStatus(object):
    def __init__(self, progress, complete, clean, speed, elapsed_time, size, expected_size, eta):
        self.progress = progress
        self.complete = complete
        self.clean = clean
        self.elapsed_time = elapsed_time
        self.size = size
        self.expected_size = expected_size
        self.eta = eta
        self.speed = speed
    
    def __str__(self):
        return "Progress: {0:.2f}%, {1}/{2} @{3}, Time: {4:.1f}s, ETA: {5:.1f}s, Complete?: {6}, Clean?: {7}".format(
            self.progress * 100,
            human_size(self.size),
            human_size(self.expected_size),
            human_size(self.speed*8, suffixes=speed_suffixes, fraction_digits=2),
            self.elapsed_time,
            self.eta,
            self.complete,
            self.clean)


class StreamDL(object):
    def __init__(self, url, filename, throttle_bytes_sec=-1, auto_cleanup=True):
        self.url = url
        self.filename = filename
        self.cfgfilename = filename + '.pystreamdl'
        self.throttle_bytes_sec = throttle_bytes_sec
        self.exit_event = threading.Event()
        self.auto_cleanup = auto_cleanup
        self.clean = False
    
    def verify(self):
        if self.is_running():
            return False
        try:
            filesize = os.path.getsize(self.filename)
        except OSError, e:
            return False
        return self.thread.get_cfg().verify_complete(filesize)
    
    def cleanup(self):
        if self.running():
            raise StreamDLAPIError("Can only cleanup after download thread stopped")
        self.thread.get_cfg().remove()
        self.clean = True
    
    def verify_cleanup(self):
        v = self.verify()
        if v == True and self.auto_cleanup:
            self.cleanup()
        return v
    
    def init_thread(self):
        self.thread = StreamDLDownloadThread(self.url, self.filename, self.cfgfilename, self.exit_event, try_continue=True, throttle_bytes_sec=self.throttle_bytes_sec)
    
    def start(self):
        self.init_thread()

        verification_result = self.verify_cleanup()
        if verification_result == True:
            return # we are already done here

        self.thread.start()
        self.started_download = True
    
    def is_finished(self):
        finished = not self.is_running() and self.thread.started
        if finished:
            raise
        return finished
    
    def is_successful(self):
        return self.verify()
    
    def status(self):
        cfg = self.thread.get_cfg()
        progress = cfg.get_progress()
        
        if not self.is_running() and self.thread.started:
            self.verify_cleanup()
        
        verification_result = self.verify()
        complete = verification_result
        elapsed_time = self.thread.get_cfg().get_elapsed_time()
        
        speed = self.thread.average_speed
        expected_size = self.thread.get_cfg().get_expected_size()
        size = self.thread.get_cfg().get_size()
        if expected_size and size and speed > 0:
            eta = (expected_size - size) / speed
        else:
            eta = 0
        
        return StreamDLDownloadStatus(progress, verification_result, self.clean, speed, elapsed_time, size, expected_size, eta)
    
    def is_running(self):
        return self.thread.is_alive()
    
    def stop(self):
        self.exit_event.set()
    
    def stop_blocking(self, timeout=None):
        self.stop()
        self.thread.join(timeout)
    
    def limit_speed(self, bytes_sec):
        if self.thread:
            self.thread.throttle_bytes_sec = bytes_sec


class StreamDLConfig(object):
    """Config file for each download file"""
    def __init__(self, cfgfilename):
        self.cfgfilename = cfgfilename
        self.new()
        self.elapsed_time_loaded = False
    
    def new(self):
        self.cfgdict = {"chunks": [], "elapsed_time": 0.0}
    
    def load(self):
        with open(self.cfgfilename, 'r') as f:
            self.cfgdict = json.load(f)
    
    def get_chunk(self, offset):
        for idx, chunk in enumerate(self.cfgdict['chunks']):
            if chunk['start'] <= offset and chunk['start'] + chunk['size'] > offset:
                return (idx, chunk)
        return None
    
    def get_first_chunk(self):
        for idx, chunk in enumerate(self.cfgdict['chunks']):
            if chunk['start'] == 0:
                return chunk
        return None
    
    def add_chunk(self, start, size):
        for chunk in self.cfgdict['chunks']:
            if chunk['start'] + chunk['size'] == start:
                chunk['size'] += size
                return
            elif self.get_chunk(start):
                raise AttributeError("Downloaded Chunk already exists!")
        # else no chunk exists
        self.cfgdict['chunks'].append({'start': start, 'size': size})
    
    def set_headers(self, headers):
        self.cfgdict['headers'] = dict(headers)
    
    def get_headers(self):
        if 'headers' in self.cfgdict:
            return self.cfgdict['headers']
        return None
    
    def get_expected_size(self):
        try:
            return int(self.cfgdict['headers']['content-length'])
        except KeyError, e:
            return None
    
    def get_size(self):
        size = 0
        for chunk in self.cfgdict['chunks']:
            size += chunk['size']
        return size
    
    def get_progress(self):
        size = self.get_size()
        headersize = self.get_expected_size()
        if headersize:
            return float(size) / float(headersize)
        return 0.0
    
    def verify_complete(self, filesize):
        size = 0
        maxoff = 0
        for chunk in self.cfgdict['chunks']:
            size += chunk['size']
            off = chunk['start'] + chunk['size']
            if maxoff < off:
                maxoff = off
        expected_size = self.get_expected_size()
        if expected_size and expected_size != filesize:
            return False
        return size == filesize and maxoff == filesize
    
    def set_elapsed_time(self, seconds):
        if 'elapsed_time' in self.cfgdict:
            if not self.elapsed_time_loaded:
                self.elapsed_time_old = self.cfgdict['elapsed_time']
        else:
            self.elapsed_time_old = 0
        self.elapsed_time_loaded = True
        
        self.cfgdict['elapsed_time'] = self.elapsed_time_old + seconds
    
    def get_elapsed_time(self):
        return self.cfgdict['elapsed_time']
    
    def try_load(self):
        try:
            self.load()
        except IOError, e:
            return False
        return True
    
    def write(self):
        with open(self.cfgfilename, 'w') as f:
            json.dump(self.cfgdict, f)
    
    def remove(self):
        try:
            os.remove(self.cfgfilename)
        except IOError, e:
            pass


class StreamDLWriter(object):
    def __init__(self, offset, filep, cfg):
        self.filep = filep
        self.cfg = cfg
        self.offset = offset
        filep.seek(self.offset)
        self.terminate = False
        self.last_write = time.time()
        self.buffer = io.BytesIO()
    
    def write(self, recvdata):
        self.buffer.write(recvdata)
        
        now = time.time()
        if now - self.last_write > 0.5:
            self.flush()

        if self.terminate:
            return 0
        return len(recvdata)
    
    def flush(self):
        self.last_write = time.time()
        data = self.buffer.getvalue()
        self.filep.write(data)
        size = len(data)
        self.cfg.add_chunk(self.offset, size)
        self.cfg.write()
        self.offset += size
        self.buffer = io.BytesIO()

    def close(self):
        self.flush()
        self.filep.close()
        self.buffer.close()


class StreamDLDownloadThread(threading.Thread):
    def __init__(self, url, filename, cfgfilename, exit_event, try_continue=False, throttle_bytes_sec=-1):
        self.url = url
        self.filename = filename
        self.cfg = StreamDLConfig(cfgfilename)
        self.cfg.try_load()
        self.try_continue = try_continue
        self.exit_event = exit_event
        self.throttle_bytes_sec = throttle_bytes_sec
        self.current_throttle = throttle_bytes_sec
        self.average_speed = 0
        self.started = False
        threading.Thread.__init__(self)
        
    def dummy_writedata(self, data):
        return len(data)

    
    def get_headers(self):
        c = pycurl.Curl()
        sio = io.BytesIO()
        c.setopt(c.URL, self.url)
        c.setopt(c.HEADERFUNCTION, sio.write)
        c.setopt(c.WRITEFUNCTION, self.dummy_writedata)
        c.setopt(c.HEADER, True)
        c.setopt(c.NOBODY, True)
        c.perform()
        
        data = sio.getvalue()
        header = data.decode('utf-8')
        headers = {}
        
        for line in header.splitlines():
            elems = line.split(':')
            if len(elems) == 2:
                headers[elems[0].lower()] = elems[1].lower()
        return headers

    def run(self):
        self.started = True
        
        if self.try_continue:
            cont = True
            if not self.cfg.try_load():
                cont = False
                self.cfg.new()
        else:
            cont = False
            cfg = self.cfg.new()
        
        head = dict(self.get_headers())
        try:
            oldhead = self.cfg.get_headers()
            if head and oldhead:
                if int(head['content-length']) != int(oldhead['content-length']):
                    print("Header size doesn't match. Will download from beginning.")
        except KeyError, AttributeError:
            cont = False
            pass
        
        self.cfg.set_headers(head)
        self.cfg.write()
        
        offset = 0
        
        if cont:
            c = self.cfg.get_first_chunk()
            if c:
                offset = c['start'] + c['size']
                if self.cfg.get_expected_size() <= offset:
                    return
                
        if os.path.exists(self.filename):
            mode = "r+b"
        else:
            mode = "w+"
            cont = False
            offset = 0

        with open(self.filename, mode) as f:
            f.seek(offset)

            headers = {}
            if offset != 0:
                headers['Range'] = "bytes=" + str(offset) + "-"
            
            last_clock = time.time()
            average_speed = 0
            speed_size = 0
            sleep_time = 0
            iter_content_execution_time_start = time.time()
            iter_content_execution_time_avg = 0
            execution_count = 1
            sleep_overhead = 0
            
            writer = StreamDLWriter(offset, f, self.cfg)
            multi = pycurl.CurlMulti()
            multi.handles = []
            self.curl = pycurl.Curl()
            c = self.curl
            c.setopt(c.URL, self.url)
            c.setopt(c.WRITEDATA, writer)
            c.setopt(c.FOLLOWLOCATION, True)
            c.setopt(c.MAXREDIRS, 5)
            c.setopt(c.CONNECTTIMEOUT, 30)
            c.setopt(c.TIMEOUT, 300)
            c.setopt(c.NOSIGNAL, 1)
            c.setopt(pycurl.MAX_RECV_SPEED_LARGE, self.throttle_bytes_sec)
            if cont:
                c.setopt(pycurl.RESUME_FROM_LARGE, offset)
            multi.add_handle(c)
            
            while not self.exit_event.is_set():
                ret, num_handles = multi.perform()
                if self.throttle_bytes_sec != -1 and self.current_throttle != self.throttle_bytes_sec:
                    self.current_throttle = self.throttle_bytes_sec
                    multi.remove_handle(c)
                    c.setopt(pycurl.MAX_RECV_SPEED_LARGE, self.throttle_bytes_sec)
                    # remove and add the handle again to force the option to stick
                    multi.add_handle(c)
                
                self.average_speed = c.getinfo(pycurl.SPEED_DOWNLOAD)
                self.cfg.set_elapsed_time(c.getinfo(pycurl.TOTAL_TIME))
                num_q, ok_list, err_list = multi.info_read()
                if c in ok_list:
                    self.exit_event.set()

            c.close()
            multi.close()
            writer.close()

    def get_cfg(self):
        return copy.deepcopy(self.cfg)


def main():
    import sys
    url = sys.argv[1]
    filename = sys.argv[2]
    dl = StreamDL(url, filename, throttle_bytes_sec=-1)
    try:
        dl.start()
        slept = 0
        while(dl.is_running()):
            print(dl.status())
            time.sleep(0.5)
            slept += 0.5
        print(dl.status())
    except KeyboardInterrupt, e:
        dl.stop()

if __name__ == '__main__':
    main()  
