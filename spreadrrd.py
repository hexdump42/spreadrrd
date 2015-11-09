#! /usr/bin/env python
__doc__ = """Consumer to store data sent via Spread messaging system 
into RRDtool databases.

Based on Chris Miles elvinrrd listener.
"""

__version__ = """0.1"""

__copyright__ = """Copyright (C) 2001-2009 Chris Miles, 2015 Mark Rees"""

__license__ = """
    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    You should have received a copy of the GNU General Public License
    along with this program; if not, write to the Free Software
    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
"""

import sys

PY2 = sys.version_info[0] == 2
PY3 = sys.version_info[0] == 3

import traceback
import re
import string
import os
import getopt
import time
if PY2:
    import cPickle as pickle
    from cStringIO import StringIO as StringIO
else:
    import pickle
    from io import StringIO

options = None

import argparse

# https://github.com/stephan-hof/rrdtool_cffi
import rrdtoolcffi as rrdtool

try:
    # Spread module from https://github.com/gyepisam/spread-python
    import spread
except:
    raise Exception("You must install the Spread module (https://github.com/gyepisam/spread-python) for Python.")

# Default Spread host/port
SPREAD_SERVER='localhost'
SPREAD_PORT=4803
SPREAD_GROUP='elvinrrd'

class RRDstore:
    """Object with details of what to match in the Elvin message and
    how to store the data in RRD.
    """
    
    def __init__(self, elvinrrd, rrdfile, store, create):
        self.elvinrrd = elvinrrd
        self.rrdfile = rrdfile
        self.store = store
        self.create = create
        
        self.regexp = None
        if '*' in self.elvinrrd:        # if a wildcard, create a reg-exp
            self.regexp = string.replace( self.elvinrrd, '*', "(.*)" )
        
        global options
        if options.debug:
            log( "Created RRDstore object %s" % (self) )
    
    def __str__(self):
        return "[elvinrrd=%s rrdfile=%s store=%s create=%s]" % (self.elvinrrd, self.rrdfile, self.store, self.create)
    

class Base(object):
    def storeRRD(self, msg):
        """This method handles any received "ELVINRRD" messages.
        It parses a valid message and stores the information in the appropriate
        RRD database, as defined by the elvinrrd configuration.
        
        Returns 0 if successful;
        Returns 1 if there were any problems.
        """
        
        r = None
        inx = None
        try:
            r = self.rrddict[msg[u'ELVINRRD']]
        except KeyError:
            # no direct match, try to match wildcard entries
            for x in self.rrddict.keys():
                inx = re.match( self.rrddict[x].regexp, msg[u'ELVINRRD'] )
                if inx:
                    r = self.rrddict[x]
                    break
        
        if r == None:
            log( "warning: No match for message %s" % (msg) )
            return 1
        
        rrdfile = r.rrdfile
        store = r.store
        create = r.create
        
        if inx:
            # wildcard match - substitute in other variables as appropriate
            if '*' in r.rrdfile:
                # replace all '*' with first string from match
                rrdfile = str(string.replace( r.rrdfile, '*', inx.group(1) ))
            if '*' in r.store:
                # replace all '*' with first string from match
                store = str(string.replace( r.store, '*', inx.group(1) ))
            if '*' in r.create:
                # replace all '*' with first string from match
                create = str(string.replace( r.create, '*', inx.group(1) ))
        
        # Use timestamp if part of message, otherwise rrdtool will use current time
        if 'timestamp' in msg.keys():
            timestamp = str(msg[u'timestamp']).strip()
        else:
            timestamp = 'N'
        
        if len(store) == 1:
            # only one variable to store, use default method
            try:
                val = msg[u'%s'%(store[0])]
            except KeyError as err:
                log( "KeyError: %s, message %s" % (err, msg) )
                return 1
            
            u = (rrdfile, "%s:%s" % (timestamp, str(val)))
        else:
            # multiple variables to store - must name them
            ds = "-t"
            n = "%s:" %timestamp
            for s in store:
                try:
                    val = msg[u'%s'%(s)]
                except KeyError as err:
                    log( "KeyError: %s, message %s" % (err, msg) )
                    return 1
                ds = "%s%s:" % (ds,s)
                n = "%s%s:" % (n,str(val))
            
            ds = ds[:-1]    # remove ':' from end
            n = n[:-1]      # remove ':' from end
            u = (rrdfile, ds, n)
        
        if options.debug:
            log( 'rrd.update( %s )' % (u,) )
        
        try:
            self.rrd.update( *u )
        except rrdtool.error as err:
            if str(err).find('No such file or directory') != -1:
                if os.path.exists( rrdfile ):
                    # file exists, despite the error...
                    sys.stderr.write( "IOError: %s, message %s\n" % (err, msg) )
                    if options.logfile:
                        log( "IOError: %s, message %s" % (err, msg) )
                else:
                    rrd_dir = os.path.dirname( rrdfile )
                    
                    if not os.path.exists( rrd_dir ):
                        if options.verbose:
                            log( "Creating directory '%s'" % (rrd_dir) )
                        os.makedirs( rrd_dir )
                    
                    createargs = (rrdfile,) + tuple(create.split())
                    if options.verbose:
                        log( "Creating rrd: %s" % (createargs,) )
                    self.rrd.create( *createargs )
                    try:
                        self.rrd.update( *u )
                    except rrdtool.error as e:
                        logmsg = "RRD update after create failed for %s: %s" % (msg,str(e))
                        sys.stderr.write(logmsg)
                        if options.logfile:
                            log(logmsg)
            else:
                sys.stderr.write( "IOError: %s, message %s\n" % (err, msg) )
                if options.logfile:
                    log( "IOError: %s, message %s" % (err, msg) )
        
        return 0


class SpreadStore(Base):
    '''Handles receiving elvinrrd messages from a Spread service.
    '''
    
    def __init__(self, spread_server='localhost', spread_port=spread.DEFAULT_SPREAD_PORT):
        if spread_port is None:
            spread_port = spread.DEFAULT_SPREAD_PORT
        elif type(spread_port) != type(1):
            spread_port = int(spread_port)
        
        self.spread_server = spread_server
        self.spread_port = spread_port
        self.connected = False
        
        self.connect()
    
    def connect(self):
        waittime = 1        # time to wait before re-connecting
        server = "%d@%s" % (self.spread_port, self.spread_server)
        
        while not self.connected:
            try:
                log("Connecting to Spread server: %s" % server)
                self._connection = spread.connect(server)
                self.connected = True
                log("Connected to Spread server: %s" % server)
            except spread.error as err:
                log("Spread connection failed: %s - waiting %s seconds before retry" % (err,waittime))
                time.sleep(waittime)
                waittime = min( waittime * 2, 60*10 ) # inc wait time but max 10 minutes
        
        self.register()
    
    def register(self):
        '''Register with Spread service for group "elvinrrd".
        '''
        log("Joining Spread group: %s" % SPREAD_GROUP)
        self._connection.join(SPREAD_GROUP)
    
    def run(self):
        while True:
            if self.connected:
                try:
                    m = self._connection.receive()
                except spread.error as err:
                    log("Spread connection lost: %s" % err)
                    self.connected = False
                else:
                    if m and hasattr(m, 'message'):
                        # RegularMsgType
                        mio = StringIO(m.message)
                        up = pickle.Unpickler(mio)
                        msg = up.load()
                        self.storeRRD(msg)
            else:   # not connected
                self.connect()
    



def read_config( filename ):
    """Read the configuration from the given filename.
    """
    
    if options.verbose:
        log( "Reading config from '%s'" % (filename) )
    
    rrddict = {}
    
    try:
        fp = open(filename, 'r')
    except IOError:
        sys.stderr.write( "Cannot open configuration file '%s', exiting" % (filename) )
        if options.logfile:
            log( "Cannot open configuration file '%s', exiting" % (filename) )
        sys.exit(1)
    
    re_comment = "^\s*#.*$"
    re_empty = "^\s*$"
    re_line = "^\s*(.+)=(.+?)$"
    
    sre_comment = re.compile(re_comment)
    sre_empty = re.compile(re_empty)
    sre_line = re.compile(re_line)
    
    line = fp.readline()
    entry = 0   # not processing an entry yet
    elvinrrd = None
    rrdfile = None
    store = None
    create = None
    while len(line) > 0:
        if sre_comment.match(line) or sre_empty.match(line):
            # commented or empty lines are ignored
            # if we were processing an entry, store that entry
            if entry == 1:
                # create new store object
                rrdobj = RRDstore( elvinrrd, rrdfile, store, create )
                rrddict[elvinrrd] = rrdobj
                entry = 0
                elvinrrd = None
                rrdfile = None
                store = None
                create = None
        else:
            inx = sre_line.match(line)
            if inx == None:
                print("Parse error, invalid line follows:\n%s" % (line))
                sys.exit(1)
            else:
                entry = 1       # we are processing an entry
                if inx.group(1) == 'elvinrrd':
                    elvinrrd = inx.group(2)
                elif inx.group(1) == 'rrdfile':
                    rrdfile = inx.group(2)
                elif inx.group(1) == 'store':
                    store = inx.group(2)
                    if ',' in store:            # list of multiple store keys
                        store = string.split(store, ',')
                    else:
                        store = [store,]
                elif inx.group(1) == 'create':
                    create = inx.group(2)
                else:
                    sys.stderr.write( "Parse error, unknown keyword '%s' on following line:\n%s" % (inx.group(1),line) )
                    if options.logfile:
                        log( "Parse error, unknown keyword '%s' on following line:\n%s" % (inx.group(1),line) )
                    sys.exit(1)
        
        line = fp.readline()
    
    if entry == 1:
        # create new store object
        rrdobj = RRDstore( elvinrrd, rrdfile, store, create )
        rrddict[elvinrrd] = rrdobj
    
    fp.close()
    
    if options.verbose:
        log( "%s parsed, %d entries in config" % (filename,len(rrddict.keys())) )
    
    return rrddict

def log( text ):
    """Log text to either logfile (if defined) or stdout.
    """
    
    if options.logfile != None:
        try:
            fp = open(options.logfile, 'a')
        except IOError as err:
            sys.stderr.write( "error: IOError opening '%s', %s\n" % (options.logfile, err) )
            sys.exit(1)
        t = "%04d-%02d-%02d %02d:%02d:%02d" % (time.localtime()[0:6])
        fp.write( "%s %s\n" % (t, text) )
        fp.close()
    else:
        sys.stdout.write( "%s\n" % (text) )

def main():
    usage_short = "[-hvd] [-S spread_server] [-P spread_port] [-l logfile] -c spreadrrd.cf"
    usage = "usage: %s %s" % (sys.argv[0], usage_short)
    
    if len(sys.argv) <= 1:
        print(usage)
        sys.exit(1)
    
    # Parse command-line arguments
    parser = argparse.ArgumentParser(usage=usage, version=None)
    parser.add_argument('-v', '--verbose', action="store_true",   \
                help="Enable verbose output")
    parser.add_argument('-d', '--debug', action="store_true",     \
                help="Enable verbose output")
    parser.add_argument('-S', '--spreadserver', dest='spread_server',     \
            metavar="HOSTNAME", help="Use Spread server at HOSTNAME")
    parser.add_argument('-P', '--spreadport', dest='spread_port', \
            metavar="PORT", help="Use Spread port number PORT")
    parser.add_argument('-l', '--logfile', dest='logfile',        \
                        metavar="FILE", help="Log to FILE")
    parser.add_argument('-c', '--configfile', dest='configfile',  \
                        metavar="FILE", help="Load config from FILE")
    parser.set_defaults(verbose=False, debug=False)
    
    global options
    options = parser.parse_args()
    
    # --debug implies --verbose
    if options.debug == True:
        options.verbose = True
    
    if options.verbose:
        log( "spreadrrd version %s starting" % (__version__) )
    
    # Build config
    if options.configfile == None:
        sys.stderr.write( "error: No configfile defined\n" )
        sys.exit(1)
    
    rrddict = read_config( options.configfile )
    if rrddict == None or rrddict == {}:
        sys.stderr.write( "error: configuration is empty\n" )
        sys.exit(1)
    
    # Create pointer to rrdtool module
    rrd = rrdtool
    
    if options.spread_server or options.spread_port:
        e = SpreadStore(options.spread_server, options.spread_port)
    else:
        raise Exception("Options for Spread messaging must be supplied.")
        
    e.rrd = rrd
    e.rrddict = rrddict
    if options.verbose:
        log( "Starting main loop" )
    e.run()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except:
        e = sys.exc_info()
        tb = traceback.format_list( traceback.extract_tb( e[2] ) )
        errstr = "Uncaught exception:\ %s, %s\n%s" % (e[0], e[1], tb)
        sys.stderr.write( "spreadrrd.py: " + errstr + "\n" )
        if options.logfile:
            log( errstr )
        sys.exit(1)
