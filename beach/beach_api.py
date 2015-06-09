import yaml
from beach.utils import *
from beach.utils import _ZMREQ
import zmq.green as zmq
import random
import operator
import gevent
import gevent.pool
import gevent.event
from beach.actor import ActorHandle

class Beach ( object ):

    def __init__( self, configFile, realm = 'global', extraTmpSeedNode = None ):
        '''Create a new interface to a beach cluster.

        :param configFile: the path to the config file of the cluster
        :param realm: the realm within the cluster you want to deal with, defaults to global
        :param extraTmpSeedNode: manually specify a seed node to interface with, only use
            if you know why you need it
        '''
        self._configFile = configFile
        self._nodes = {}
        self._realm = realm
        self._opsPort = None
        self._isInited = gevent.event.Event()
        self._vHandles = []
        self._dirCache = {}

        with open( self._configFile, 'r' ) as f:
            self._configFile = yaml.load( f )

        self._seedNodes = self._configFile.get( 'seed_nodes', [] )

        if extraTmpSeedNode is not None:
            self._seedNodes.append( extraTmpSeedNode )

        self._opsPort = self._configFile.get( 'ops_port', 4999 )

        for s in self._seedNodes:
            self._connectToNode( s )

        self._threads = gevent.pool.Group()
        self._threads.add( gevent.spawn( self._updateNodes ) )

        self._isInited.wait( 5 )

        ActorHandle._setHostDirInfo( [ 'tcp://%s:%d' % ( x, self._opsPort ) for x in self._nodes.keys() ] )

    def _connectToNode( self, host ):
        nodeSocket = _ZMREQ( 'tcp://%s:%d' % ( host, self._opsPort ), isBind = False )
        self._nodes[ host ] = { 'socket' : nodeSocket, 'info' : None }
        print( "Connected to node ops at: %s:%d" % ( host, self._opsPort ) )

    def _getHostInfo( self, zSock ):
        info = None
        resp = zSock.request( { 'req' : 'host_info' } )
        if isMessageSuccess( resp ):
            info = resp[ 'info' ]
        return info

    def _updateNodes( self ):
        toQuery = self._nodes.values()[ random.randint( 0, len( self._nodes ) - 1 ) ][ 'socket' ]
        nodes = toQuery.request( { 'req' : 'get_nodes' }, timeout = 10 )
        for k in nodes[ 'nodes' ].keys():
            if k not in self._nodes:
                self._connectToNode( k )

        for nodeName, node in self._nodes.items():
            self._nodes[ nodeName ][ 'info' ] = self._getHostInfo( node[ 'socket' ] )

        tmpDir = self.getDirectory()
        if isMessageSuccess( tmpDir ):
            self._dirCache = tmpDir[ 'realms' ].get( self._realm, {} )

        self._isInited.set()
        gevent.spawn_later( 30, self._updateNodes )

    def close( self ):
        '''Close all threads and resources of the interface.
        '''
        self._threads.kill()

    def setRealm( self, realm ):
        '''Change the realm to interface with.

        :param realm: the new realm to use
        :returns: the old realm used or None if none were specified
        '''
        old = self._realm
        self._realm = realm
        return old

    def addActor( self, actorName, category, strategy = 'random', strategy_hint = None, realm = None ):
        '''Spawn a new actor in the cluster.

        :param actorName: the name of the actor to spawn
        :param category: the category associated with this new actor
        :param strategy: the strategy to use to decide where to spawn the new actor,
            currently supports: random
        :param strategy_hint: a parameter to help choose a node, meaning depends on the strategy
        :param realm: the realm to add the actor in, if different than main realm set
        :returns: returns the reply from the node indicating if the actor was created successfully,
            use beach.utils.isMessageSuccess( response ) to check for success
        '''

        resp = None
        node = None

        thisRealm = realm if realm is not None else self._realm

        if 'random' == strategy or strategy is None:
            node = self._nodes.values()[ random.randint( 0, len( self._nodes ) - 1 ) ][ 'socket' ]
        elif 'resource' == strategy:
            # For now the simple version of this strategy is to just average the CPU and MEM %.
            node = min( self._nodes.values(), key = lambda x: ( sum( x[ 'info' ][ 'cpu' ] ) /
                                                                len( x[ 'info' ][ 'cpu' ] ) +
                                                                x[ 'info' ][ 'mem' ] ) / 2 )[ 'socket' ]
        elif 'affinity' == strategy:
            nodeList = self._dirCache.get( strategy_hint, {} ).values()
            population = {}
            for n in nodeList:
                name = n.split( ':' )[ 1 ][ 2 : ]
                population.setdefault( name, 0 )
                population[ name ] += 1
            if 0 != len( population ):
                affinityNode = max( population.iteritems(), key = operator.itemgetter( 1 ) )[ 0 ]
                node = self._nodes[ affinityNode ].get( 'socket', None )
            else:
                # There is nothing in play, fall back to random
                node = self._nodes.values()[ random.randint( 0, len( self._nodes ) - 1 ) ][ 'socket' ]

        if node is not None:
            resp = node.request( { 'req' : 'start_actor',
                                   'actor_name' : actorName,
                                   'realm' : thisRealm,
                                   'cat' : category }, timeout = 10 )

        return resp

    def getDirectory( self ):
        '''Retrieve the directory from a random node, all nodes have a directory that
            is eventually-consistent.

        :returns: the realm directory of the cluster
        '''
        node = self._nodes.values()[ random.randint( 0, len( self._nodes ) - 1 ) ][ 'socket' ]
        resp = node.request( { 'req' : 'get_full_dir' }, timeout = 10 )
        if resp is not None:
            self._dirCache = resp
        return resp

    def flush( self ):
        '''Unload all actors from the cluster, major operation, be careful.

        :returns: True if all actors were removed normally
        '''
        isFlushed = True
        for node in self._nodes.values():
            resp = node[ 'socket' ].request( { 'req' : 'flush' }, timeout = 30 )
            if not isMessageSuccess( resp ):
                isFlushed = False

        return isFlushed

    def getActorHandle( self, category, mode = 'random' ):
        '''Get a virtual handle to actors in the cluster.

        :param category: the name of the category holding actors to get the handle to
        :param mode: the method actors are queried by the handle, currently
            handles: random
        :returns: an ActorHandle
        '''
        v = ActorHandle( self._realm, category, mode )
        self._vHandles.append( v )
        return v