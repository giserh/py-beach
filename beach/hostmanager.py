# Copyright (C) 2015  refractionPOINT
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

'''The main script to run on a beach cluster node, it takes care of everything. Instantiate like:
    python -m beach.hostmanager -h
'''

import sys
import os
import signal
import gevent
import gevent.event
import gevent.pool
import yaml
import multiprocessing
from beach.utils import *
from beach.utils import _getIpv4ForIface
from beach.utils import _ZMREQ
from beach.utils import _ZMREP
import time
import uuid
import random
from sets import Set
import logging
import subprocess
import psutil
import collections
import uuid

timeToStopEvent = gevent.event.Event()

def _stop():
    global timeToStopEvent
    timeToStopEvent.set()

class HostManager ( object ):
    
    # The actorList is a list( actorNames, configFile )
    def __init__( self, configFile, iface = None ):
        
        # Setting the signal handler to trigger the stop event
        global timeToStopEvent
        gevent.signal( signal.SIGQUIT, _stop )
        gevent.signal( signal.SIGINT, _stop )

        self._logger = None
        self._initLogging()
        
        self.stopEvent = timeToStopEvent
        self.py_beach_dir = None
        self.configFilePath = os.path.abspath( configFile )
        self.configFile = None
        self.directory = {}
        self.tombstones = {}
        self.actorInfo = {}
        self.ports_available = Set()
        self.nProcesses = 0
        self.processes = []
        self.initialProcesses = False
        self.seedNodes = []
        self.directoryPort = None
        self.opsPort = 0
        self.opsSocket = None
        self.port_range = ( 0, 0 )
        self.interface = None
        self.ifaceIp4 = None
        self.nodes = {}
        self.peer_keepalive_seconds = 0
        self.instance_keepalive_seconds = 0
        self.tombstone_culling_seconds = 0
        self.isActorChanged = gevent.event.Event()
        self.isInstanceChanged = gevent.event.Event()

        # Load default configs
        with open( self.configFilePath, 'r' ) as f:
            self.configFile = yaml.load( f )

        self.py_beach_dir = os.path.dirname( os.path.abspath( __file__ ) )

        os.chdir( os.path.dirname( os.path.abspath( self.configFilePath ) ) )

        self.nProcesses = self.configFile.get( 'n_processes', 0 )
        if self.nProcesses == 0:
            self.nProcesses = multiprocessing.cpu_count()
        self._log( "Using %d instances per node" % self.nProcesses )

        if iface is not None:
            self.interface = iface
        else:
            self.interface = self.configFile.get( 'interface', 'eth0' )
        self.ifaceIp4 = _getIpv4ForIface( self.interface )

        self.seedNodes = self.configFile.get( 'seed_nodes', [] )

        if 0 == len( self.seedNodes ):
            self.seedNodes.append( self.ifaceIp4 )

        for s in self.seedNodes:
            self._log( "Using seed node: %s" % s )

        self.directoryPort = _ZMREP( self.configFile.get( 'directory_port',
                                                         'ipc:///tmp/py_beach_directory_port' ),
                                    isBind = True )
        
        self.opsPort = self.configFile.get( 'ops_port', 4999 )
        self.opsSocket = _ZMREP( 'tcp://%s:%d' % ( self.ifaceIp4, self.opsPort ), isBind = True )
        self._log( "Listening for ops on %s:%d" % ( self.ifaceIp4, self.opsPort ) )
        
        self.port_range = ( self.configFile.get( 'port_range_start', 5000 ), self.configFile.get( 'port_range_end', 6000 ) )
        self.ports_available.update( xrange( self.port_range[ 0 ], self.port_range[ 1 ] + 1 ) )
        
        self.peer_keepalive_seconds = self.configFile.get( 'peer_keepalive_seconds', 60 )
        self.instance_keepalive_seconds = self.configFile.get( 'instance_keepalive_seconds', 60 )
        self.directory_sync_seconds = self.configFile.get( 'directory_sync_seconds', 60 )
        self.tombstone_culling_seconds = self.configFile.get( 'tombstone_culling_seconds', 3600 )
        
        self.instance_strategy = self.configFile.get( 'instance_strategy', 'random' )
        
        # Bootstrap the seeds
        for s in self.seedNodes:
            self._connectToNode( s )
        
        # Start services
        self._log( "Starting services" )
        gevent.spawn( self._svc_directory_requests )
        gevent.spawn( self._svc_instance_keepalive )
        gevent.spawn( self._svc_host_keepalive )
        gevent.spawn( self._svc_directory_sync )
        gevent.spawn( self._svc_cullTombstones )
        gevent.spawn( self._svc_receiveOpsTasks )
        gevent.spawn( self._svc_pushDirChanges )
        
        # Start the instances
        for n in range( self.nProcesses ):
            self._startInstance( isIsolated = False )
        
        # Wait to be signaled to exit
        self._log( "Up and running" )
        timeToStopEvent.wait()
        
        # Any teardown required
        for proc in self.processes:
            self._sendQuitToInstance( proc )
        
        self._log( "Exiting." )

    def _sendQuitToInstance( self, instance ):
        if instance[ 'p' ] is not None:
            instance[ 'p' ].send_signal( signal.SIGQUIT )
            errorCode = instance[ 'p' ].wait()
            if 0 != errorCode:
                self._logCritical( 'actor host exited with error code: %d' % errorCode )

    def _startInstance( self, isIsolated = False ):
        instanceId = str( uuid.uuid4() )
        procSocket = _ZMREQ( 'ipc:///tmp/py_beach_instance_%s' % instanceId, isBind = False )
        instance = { 'socket' : procSocket, 'p' : None, 'isolated' : isIsolated, 'id' : instanceId }
        self.processes.append( instance )
        self._log( "Managing instance at: %s" % ( 'ipc:///tmp/py_beach_instance_%s' % instanceId, ) )
        return instance

    def _removeInstanceIfIsolated( self, instance ):
        if instance[ 'isolated' ]:
            # Isolated instances only have one Actor loaded
            # so this means we can remove this instance
            self._log( "Removing isolated host instance: %s" % instance[ 'id' ] )
            self.processes.remove( instance )
            self._sendQuitToInstance( instance )

    def _connectToNode( self, ip ):
        nodeSocket = _ZMREQ( 'tcp://%s:%d' % ( ip, self.opsPort ), isBind = False )
        self.nodes[ ip ] = { 'socket' : nodeSocket, 'last_seen' : None }

    def _removeUidFromDirectory( self, uid ):
        isFound = False
        for r in self.directory.values():
            for c in r.values():
                if uid in c:
                    del( c[ uid ] )
                    isFound = True
                    break
            if isFound:
                self.tombstones[ uid ] = int( time.time() )
                break

        if uid in self.actorInfo:
            port = self.actorInfo[ uid ][ 'port' ]
            self.ports_available.add( port )
            del( self.actorInfo[ uid ] )

        return isFound

    def _removeInstanceActorsFromDirectory( self, instance ):
        for uid, actor in self.actorInfo.items():
            if actor[ 'instance' ] == instance:
                self._removeUidFromDirectory( uid )
    
    def _getAvailablePortForUid( self, uid ):
        port = None
        
        if 0 != len( self.ports_available ):
            port = self.ports_available.pop()
            self.actorInfo.setdefault( uid, {} )[ 'port' ] =  port
        
        return port
    
    def _getInstanceForActor( self, uid, actorName, realm, isIsolated = False ):
        instance = None

        if isIsolated:
            self.nProcesses += 1
            instance = self._startInstance( isIsolated = True )
            self.isInstanceChanged.set()
        elif self.instance_strategy == 'random':
            instance = random.choice( [ x for x in self.processes if x[ 'isolated' ] is False ] )
        
        if instance is not None:
            self.actorInfo.setdefault( uid, {} )[ 'instance' ] = instance
        
        return instance

    def _updateDirectoryWith( self, curDir, newDir ):
        for k, v in newDir.iteritems():
            if isinstance( v, collections.Mapping ):
                r = self._updateDirectoryWith( curDir.get( k, {} ), v )
                curDir[k] = r
            else:
                curDir[k] = newDir[k]
        return curDir

    def _getDirectoryEntriesFor( self, realm, category ):
        return self.directory.get( realm, {} ).get( category, {} )
    
    def _svc_cullTombstones( self ):
        while not self.stopEvent.wait( 0 ):
            self._log( "Culling tombstones" )
            currentTime = int( time.time() )
            maxTime = self.tombstone_culling_seconds
            nextTime = currentTime
            
            for uid, ts in self.tombstones.items():
                if ts < currentTime - maxTime:
                    del( self.tombstones[ uid ] )
                elif ts < nextTime:
                    nextTime = ts

            gevent.sleep( maxTime - ( currentTime - nextTime ) )
    
    def _svc_receiveOpsTasks( self ):
        z = self.opsSocket.getChild()
        while not self.stopEvent.wait( 0 ):
            data = z.recv()
            if data is not False and 'req' in data:
                action = data[ 'req' ]
                self._log( "Received new ops request: %s" % action )
                if 'keepalive' == action:
                    if 'from' in data and data[ 'from' ] not in self.nodes:
                        self._log( "Discovered new node: %s" % data[ 'from' ] )
                        self._connectToNode( data[ 'from' ] )
                    z.send( successMessage() )
                elif 'start_actor' == action:
                    if 'actor_name' not in data or 'cat' not in data:
                        z.send( errorMessage( 'missing information to start actor' ) )
                    else:
                        actorName = data[ 'actor_name' ]
                        category = data[ 'cat' ]
                        realm = data.get( 'realm', 'global' )
                        parameters = data.get( 'parameters', {} )
                        isIsolated = data.get( 'isolated', False )
                        uid = str( uuid.uuid4() )
                        port = self._getAvailablePortForUid( uid )
                        instance = self._getInstanceForActor( uid, actorName, realm, isIsolated )
                        newMsg = instance[ 'socket' ].request( { 'req' : 'start_actor',
                                                                 'actor_name' : actorName,
                                                                 'realm' : realm,
                                                                 'uid' : uid,
                                                                 'ip' : self.ifaceIp4,
                                                                 'port' : port,
                                                                 'parameters' : parameters,
                                                                 'isolated' : isIsolated },
                                                               timeout = 10 )
                        if isMessageSuccess( newMsg ):
                            self._log( "New actor loaded (isolation = %s), adding to directory" % isIsolated )
                            self.directory.setdefault( realm,
                                                       {} ).setdefault( category,
                                                                        {} )[ uid ] = 'tcp://%s:%d' % ( self.ifaceIp4,
                                                                                                        port )
                            self.isActorChanged.set()
                        else:
                            self._removeUidFromDirectory( uid )
                        z.send( newMsg )
                elif 'kill_actor' == action:
                    if 'uid' not in data:
                        z.send( errorMessage( 'missing information to stop actor' ) )
                    else:
                        uids = data[ 'uid' ]
                        if not isinstance( uids, collections.Iterable ):
                            uids = ( uids, )

                        failed = []

                        for uid in uids:
                            if uid not in self.actorInfo:
                                failed.append( errorMessage( 'actor not found' ) )
                            else:
                                instance = self.actorInfo[ uid ][ 'instance' ]
                                newMsg = instance[ 'socket' ].request( { 'req' : 'kill_actor',
                                                                         'uid' : uid },
                                                                       timeout = 10 )
                                if not isMessageSuccess( newMsg ):
                                    failed.append( newMsg )
                                else:
                                    if not self._removeUidFromDirectory( uid ):
                                        failed.append( errorMessage( 'error removing actor from directory after stop' ) )
                                    else:
                                        self.isActorChanged.set()

                                self._removeInstanceIfIsolated( instance )

                        if 0 != len( failed ):
                            z.send( errorMessage( 'some actors failed to stop', failed ) )
                        else:
                            z.send( successMessage() )
                elif 'remove_actor' == action:
                    if 'uid' not in data:
                        z.send( errorMessage( 'missing information to remove actor' ) )
                    else:
                        uid = data[ 'uid' ]
                        instance = self.actorInfo.get( uid, {} ).get( 'instance', None )
                        if instance is not None and self._removeUidFromDirectory( uid ):
                            z.send( successMessage() )
                            self.isActorChanged.set()
                            self._removeInstanceIfIsolated( instance )
                        else:
                            z.send( errorMessage( 'actor to stop not found' ) )
                elif 'host_info' == action:
                    z.send( successMessage( { 'info' : { 'cpu' : psutil.cpu_percent( percpu = True,
                                                                                     interval = 2 ),
                                                         'mem' : psutil.virtual_memory().percent } } ) )
                elif 'get_full_dir' == action:
                    z.send( successMessage( { 'realms' : self.directory } ) )
                elif 'get_dir' == action:
                    realm = data.get( 'realm', 'global' )
                    if 'cat' in data:
                        z.send( successMessage( data = { 'endpoints' : self._getDirectoryEntriesFor( realm, data[ 'cat' ] ) } ) )
                    else:
                        z.send( errorMessage( 'no category specified' ) )
                elif 'get_nodes' == action:
                    nodeList = {}
                    for k in self.nodes.keys():
                        nodeList[ k ] = { 'last_seen' : self.nodes[ k ][ 'last_seen' ] }
                    z.send( successMessage( { 'nodes' : nodeList } ) )
                elif 'flush' == action:
                    resp = successMessage()
                    for uid, actor in self.actorInfo.items():
                        instance = actor[ 'instance' ]
                        newMsg = instance[ 'socket' ].request( { 'req' : 'kill_actor',
                                                                 'uid' : uid },
                                                               timeout = 10 )
                        if isMessageSuccess( newMsg ):
                            if not self._removeUidFromDirectory( uid ):
                                resp = errorMessage( 'error removing actor from directory after stop' )

                        self._removeInstanceIfIsolated( instance )

                    z.send( resp )

                    if isMessageSuccess( resp ):
                        self.isActorChanged.set()
                elif 'get_dir_sync' == action:
                    z.send( successMessage( { 'directory' : self.directory, 'tombstones' : self.tombstones } ) )
                elif 'push_dir_sync' == action:
                    if 'directory' in data and 'tombstones' in data:
                        self._updateDirectoryWith( self.directory, data[ 'directory' ] )
                        for uid in data[ 'tombstones' ]:
                            self._removeUidFromDirectory( uid )
                        z.send( successMessage() )
                    else:
                        z.send( errorMessage( 'missing information to update directory' ) )
                else:
                    z.send( errorMessage( 'unknown request', data = { 'req' : action } ) )
            else:
                z.send( errorMessage( 'invalid request' ) )
                self._logCritical( "Received completely invalid request" )
    
    def _svc_directory_requests( self ):
        z = self.directoryPort.getChild()
        while not self.stopEvent.wait( 0 ):
            data = z.recv()

            self._log( "Received directory request" )
            
            realm = data.get( 'realm', 'global' )
            if 'cat' in data:
                z.send( successMessage( data = { 'endpoints' : self._getDirectoryEntriesFor( realm, data[ 'cat' ] ) } ) )
            else:
                z.send( errorMessage( 'no category specified' ) )
    
    def _svc_instance_keepalive( self ):
        while not self.stopEvent.wait( 0 ):
            for instance in self.processes:
                self._log( "Issuing keepalive for instance %s" % instance[ 'id' ] )

                if self.initialProcesses and instance[ 'p' ] is not None:
                    # Only attempt keepalive if we know of a pid for it, otherwise it must be new
                    data = instance[ 'socket' ].request( { 'req' : 'keepalive' }, timeout = 5 )
                else:
                    # For first instances, immediately trigger the instance creation
                    data = False

                if not isMessageSuccess( data ):
                    isBrandNew = True
                    if instance[ 'p' ] is not None:
                        instance[ 'p' ].kill()
                        # Instance died, it means all Actors within are no longer reachable
                        self._removeInstanceActorsFromDirectory( instance )
                        self.isActorChanged.set()
                        isBrandNew = False

                    if isBrandNew or not instance[ 'isolated' ]:
                        proc = subprocess.Popen( [ 'python',
                                                   '%s/ActorHost.py' % self.py_beach_dir,
                                                    self.configFilePath,
                                                    instance[ 'id' ] ] )

                        instance[ 'p' ] = proc

                        if not isBrandNew:
                            self._logCritical( "Instance %s died, restarting it, pid %d" % ( instance[ 'id' ], proc.pid ) )
                        else:
                            self._log( "Initial instance %s created with pid %d (isolation = %s)" % ( instance[ 'id' ], proc.pid, instance[ 'isolated' ] ) )
                    else:
                        # This means it's an isolated Actor that died, in this case
                        # we don't restart it, we leave it to higher layers to restarts it
                        # if they want.
                        self.processes.remove( instance )

            if not self.initialProcesses:
                self.initialProcesses = True

            self.isInstanceChanged.wait( self.instance_keepalive_seconds )
            self.isInstanceChanged.clear()
    
    def _svc_host_keepalive( self ):
        while not self.stopEvent.wait( 0 ):
            for nodeName, node in self.nodes.items():
                if nodeName != self.ifaceIp4:
                    self._log( "Issuing keepalive for node %s" % nodeName )
                    data = node[ 'socket' ].request( { 'req' : 'keepalive',
                                                       'from' : self.ifaceIp4 }, timeout = 10 )

                    if isMessageSuccess( data ):
                        node[ 'last_seen' ] = int( time.time() )
                    else:
                        self._log( "Removing node %s because of timeout" % nodeName )
                        del( self.nodes[ nodeName ] )
            
            gevent.sleep( self.peer_keepalive_seconds )
    
    def _svc_directory_sync( self ):
        nextWait = 0
        nextNode = 0
        while not self.stopEvent.wait( nextWait ):
            nNodes = len( self.nodes )
            if nNodes != 0:
                if nextNode > nNodes:
                    nextNode = 0
                # We aim to manually fully sync with the directories
                # of all the nodes in the cluster over directory_sync_seconds seconds.
                nextWait = self.directory_sync_seconds / nNodes

                nodeName = self.nodes.keys()[ nextNode ]
                node = self.nodes[ nodeName ]
                if nodeName != self.ifaceIp4:
                    self._log( "Issuing directory sync with node %s" % nodeName )
                    data = node[ 'socket' ].request( { 'req' : 'get_dir_sync' } )

                    if isMessageSuccess( data ):
                        self._updateDirectoryWith( self.directory, data[ 'directory' ] )
                        for uid in data[ 'tombstones' ]:
                            self._removeUidFromDirectory( uid )
            else:
                nextWait = 1

    def _svc_pushDirChanges( self ):
        while not self.stopEvent.wait( 0 ):
            self.isActorChanged.wait()
            # We "accumulate" updates for 5 seconds once they occur to limit updates pushed
            gevent.sleep( 5 )
            self.isActorChanged.clear()
            for nodeName, node in self.nodes.items():
                if nodeName != self.ifaceIp4:
                    self._log( "Pushing new directory update to %s" % nodeName )
                    node[ 'socket' ].request( { 'req' : 'push_dir_sync',
                                                'directory' : self.directory,
                                                'tombstones' : self.tombstones } )

    def _initLogging( self ):
        logging.basicConfig( format = "%(asctime)-15s %(message)s" )
        self._logger = logging.getLogger()
        self._logger.setLevel( logging.INFO )

    def _log( self, msg ):
        self._logger.info( '%s : %s', self.__class__.__name__, msg )

    def _logCritical( self, msg ):
        self._logger.error( '%s : %s', self.__class__.__name__, msg )
    

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser( prog = 'HostManager' )
    parser.add_argument( 'configFile',
                         type = str,
                         help = 'the main config file defining the beach cluster' )
    parser.add_argument( '--iface', '-i',
                         type = str,
                         required = False,
                         dest = 'iface',
                         help = 'override the interface used for comms found in the config file' )
    args = parser.parse_args()
    hostManager = HostManager( args.configFile, iface = args.iface )