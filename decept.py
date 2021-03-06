#!/usr/bin/python
#------------------------------------------------------------------
# Author: Lilith Wyatt <(^,^)>
#------------------------------------------------------------------
# November 2015, created within ASIG
# Author: Lilith Wyatt (liwyatt)  
#
# This is the main proxy, Decept (formerly sadsoxxy [;.;]) 
# Created with portability in mind, so you can set it and 
# forget it, only uses as standard python libraries as 
# possible.
#
# Can dump .fuzzer files too if you have mutiny_fuzzing_framework
# in an adjecent directory (i.e. ../mutiny_fuzzing_framework)
#
# Based off of the tcp proxy.py from Black Hat Python by Justin Seitz
#
# Feature List:
# - SSL support
# - Pcap dumping (sorta hacky)
# - IPV6/Unix_socket/Abstract support
# - fuzzer file dumping for Mutiny
# - Dumb args mode (portability,argparse removed)
# - polling w/select
# - L3 captures/proxying
# - L2 captures/proxying
# - UDP currently busted, lol
#------------------------------------------------------------------
# Copyright (c) 2015-2017 by Cisco Systems, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
# 3. Neither the name of the Cisco Systems, Inc. nor the
#    names of its contributors may be used to endorse or promote products
#    derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS "AS IS" AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDERS BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#------------------------------------------------------------------

import ssl
import sys
import socket
import struct
import select
import multiprocessing

try:
    # windows 
    import fcntl 
except:
    pass

from ctypes import *
from re import match
from time import time
from os.path import join
from platform import system
from os import mkdir,getcwd,remove

DEBUGGING = False
if '-d' in sys.argv:
    DEBUGGING = True

try:
    sys.path.append(join("..","mutiny_fuzzing_framework")) 
    import backend.fuzzerdata as mutiny
except ImportError:
    pass
    
# minor hack to get decept to run on osx 
if "darwin" in system().lower():
    socket.AF_PACKET = -1

##########
#! <(^.^)>
class DeceptProxy():

    def __init__(self,lhost,lport,rhost,rport,local_end_type,remote_end_type,receive_first=False):
        self.lhost = lhost
        self.lport = lport
        self.rhost = rhost
        self.rport = rport
        self.srcport = 0 #for UDP when we need src port to send back to locally 
        self.receive_first = receive_first
        self.local_end_type = local_end_type
        self.remote_end_type = remote_end_type
        self.conn = True if "udp" not in local_end_type else False 
        self.protocol_blueprints = None
        self.pkt_count = 0
        self.max_conns = 5
        self.handler_trigger = False

        self.verbose = True

        # don't exit if no data (streaming)
        self.dont_kill = False

        self.udp_port_range = None 

        #! verify this
        if "\\x00" in self.lhost:
            self.lhost = "\x00%s" % self.lhost

        if "\\x00" in self.rhost:
            self.rhost = "\x00%s" % self.rhost

        self.rbind_addr = "0.0.0.0"
        self.rbind_port = 0

        # ssl options for those who care
        #client_context = ssl.create_default_context()
        #client_context.check_hostname = False
        #client_context.verify_mode = ssl.CERT_NONE

        #killswitch for closing sockets
        self.killswitch = multiprocessing.Event() 

        # Timeout for sockets
        self.timeout = 1 

        # Max amount of data to send per packet for 
        # TCP-based protocols, inbound or outbound.
        self.l4_MTU = 30000

        # .pcap/.fuzzer Options
        self.pcap_file = ""
        self.fuzz_file = ""
        self.fuzzerData = ""
        
        # assorted options 
        self.L3_raw = False
        self.dumpraw = ''

        self.l2_mtu = 1500
        self.l2_filter = ""
        self.l2_forward = False
        self.linterface = ""
        self.rinterface = ""
        self.lmac = ""
        self.rmac = ""

        self.pcapdir = ""
        self.PCAP_PER_SESSION = False
        self.PCAP_SNAPLEN = 65535

        # on successful import, these will be the imported
        # functions "inbound_hook()" and "outbound_hook()"
        self.inbound_hook = None
        self.outbound_hook = None
    
        # Load SSL cert if creating local SSL prox
        if self.local_end_type  == "ssl":
            try:
                self.server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                self.server_context.load_cert_chain(certfile="cert.pem",keyfile="key.pem")
            except:
                output("[x.x] Please generate keys before attempting to proxy ssl",RED)
                output("[-_-] Protip: openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -nodes",CYAN)
                sys.exit(0)

    def shutdown(self):
        if self.local_end_type in ConnectionBased:
            server_kill = self.socket_plinko(self.lhost,self.local_end_type)
            if server_kill.type != socket.AF_UNIX:
                server_kill.connect((self.lhost,self.lport))
                server_kill.close()
            elif "unix" in self.local_end_type:
                try:
                    remove(self.lhost)
                except:
                    output("[?.?] Unable to delete Unix Socket: %s"%self.lhost,YELLOW)

        output("[^.^] Thanks for using Decept!")
        sys.exit()


    def get_bytes(self,sock):
        ret = ""
        sock.settimeout(self.timeout)
        try: 
            while True:
                if self.conn:
                    tmp = sock.recv(65535)  
                else:
                    # necessary for connectionless
                    tmp,(_,tmpport) = sock.recvfrom(65535) 
                    if tmpport != self.lport and tmpport != self.rport and not self.srcport: 
                        self.srcport = tmpport
                    if self.udp_port_range and tmpport != self.rport:
                        self.lport = tmpport
                if len(tmp): 
                    ret+=tmp
                else:
                    break
        except Exception as e:
            #output(str(e),YELLOW)
            pass

        return ret 

    # Takes the host (1.1.2.1, FE80::1, ab:bc:cd:ef:ab:ea)
    # and endpoint type (tcp,ssl,unix,udp)
    # and returns the appropriate socket
    def socket_plinko(self,host,endpoint):
        s_fam = socket.AF_INET 
        s_proto = None

        if "stdin" in endpoint:
            return sys.stdin
        if "stdout" in endpoint:
            return sys.stdout
    
        # Test dnslookup first.

        if match(r'\d{1,3}(\.\d{1,3}){3}',host):
            s_fam = socket.AF_INET
        elif match(r'([0-9A-Fa-f]{0,4}:?)(:[0-9A-Fa-f]{1,4}:?)+',host) and host.find("::") == host.rfind("::"):
            s_fam = socket.AF_INET6 
        elif match(r'([0-9A-fa-f]{2}:){5}[0-9A-fa-f]{2}',host):
            if "darwin" in system().lower():
                output("[x.x] RAW packet functionality in OSX not supported.")
                sys.exit()
            # Automatically raw. 
            if endpoint == "passthrough":
                # when we aren't bridging two interfaces
                ret_socket = socket.socket(socket.AF_PACKET,socket.SOCK_RAW,0x0300) 
            #endpoint == "bridge" 
            else: 
                ret_socket = socket.socket(socket.AF_PACKET,socket.SOCK_RAW) 
            return ret_socket

        else:
            s_fam = socket.AF_UNIX  
        
        if endpoint in ConnectionBased:
            ret_socket = socket.socket(s_fam,socket.SOCK_STREAM)
            ret_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) 

            # we don't ssl wrap here since we need to know whether or not
            # the socket is server side or client side ~ <(^.^<)
            if endpoint == "L3_raw":
                ret_socket.setsockopt(socket.IPPROTO_IP,socket.IP_HDRINCL,1)
            return ret_socket

        elif "udp" in endpoint: 
            ret_socket =  socket.socket(s_fam,socket.SOCK_DGRAM)
            
            if self.L3_raw:
                ret_socket.setsockopt(socket.IPPROTO_IP,socket.IP_HDRINCL,1)

            return ret_socket

        else:
            # Valid endpoint choices include anything in ValidEndpoints
            s_type = socket.SOCK_RAW
            if endpoint in PROTO:
                # e.g. if proto == ospf, s_proto => 89
                s_proto = PROTO[endpoint] 
            elif endpoint == "raw":
                s_proto = socket.IPPROTO_RAW
            elif endpoint == "stdin" or endpoint == "stdout":
                return
            else:
                s_proto = int(endpoint) 

            ret_socket = socket.socket(s_fam,s_type,s_proto)    
            # Crafting L2/L3 is hard, yo.
            if self.L3_raw:
                ret_socket.setsockopt(socket.IPPROTO_IP,socket.IP_HDRINCL,1)

            return ret_socket 
       

    def server_socket_init(self):
        if "stdin" in  self.local_end_type:
            return

        try:
            if "windows" in system().lower() or "cygwin" in system().lower():
                self.server_socket.bind((self.lhost,self.lport))
            
            elif self.server_socket.family == socket.AF_PACKET:
                # case normal L2 socket
                if self.server_socket.proto == 0:
                    return
                    
                # case promiscuous socket
                if self.server_socket.proto == 0x300:  
                    return

            elif self.server_socket.family == socket.AF_UNIX:
                self.server_socket.bind((self.lhost)) 
            elif "darwin" not in system().lower() and self.server_socket.family == socket.AF_PACKET:
                self.server_socket.bind((self.lhost,0))
            else:
                self.server_socket.bind((self.lhost,self.lport))
            output("[*.*] Listening on %s:%s" % (self.lhost,str(self.lport)),CYAN)

        except Exception as e:
            output(str(e),YELLOW)
            output("[x.x] Unable to bind to %s:%s" % (self.lhost,str(self.lport)) ,RED)
            sys.exit(0)

        output("[$.$] local:%s|remote:%s" % (self.local_end_type,self.remote_end_type), GREEN)

        if self.local_end_type in ConnectionBased:
            try:
                self.server_socket.listen(self.max_conns)
            except Exception as e:
                output(e)

    
    
    def server_loop(self):

        # prep for dumping raw datagrams
        if self.dumpraw:
            try:
                mkdir(self.dumpraw)
            except Exception as e:
                pass
        
        # If we're attempting to write to a pcap, shouldn't it be required to be L2?
        # # Perhaps make a raw promisc regardless? for when no tcpdump/tshark?
        if self.pcapdir:
                try:
                    mkdir(pcapdir)
                except:
                    pass
                
                PCAP_DIR = join(getcwd(),pcapdir) 
                try:
                    PCAP_FILE = open(join(PCAP_DIR,"testpcap.pcap"),"wb") 
                except:
                    PCAP_FILE = open(testpcap.pcap,"wb")

        if self.l2_filter:
            self.l2_filter = ''.join(chr(int(x,16)) for x in filter(None,l2_filter.split("x")))

        self.server_socket = self.socket_plinko(self.lhost,self.local_end_type) 

        # Alrighty, here's where we start to distinguish 
        # socket Family/type/protocol
        # TODO
        self.server_socket_init()

        if self.fuzz_file: 
            self.fuzzerData = mutiny.FuzzerData()

        if self.pcap_file:
            self.pcap_file.write((pcap_global_hdr().get_byte_array()))

        #! Todo, no loop for UDP...
        while True:
            if self.local_end_type in ConnectionBased:
                csock,addr = self.server_socket.accept()

                #print out conn info
                if addr:
                    output("[>.>] Received Connection from %s" % str(addr),GREEN) 
                else:
                    output("[>.>] Received Connection from UnixSocket",GREEN) 


                if "windows" in system().lower() or "cygwin" in system().lower():
                    import threading 
                    proxy_thread = threading.Thread(target = self.proxy_loop, 
                                                       args = (csock,
                                                               self.rhost,
                                                               self.rport)) 
                else:
                    proxy_thread = multiprocessing.Process(target = self.proxy_loop, 
                                                           args = (csock,
                                                                   self.rhost,
                                                                   self.rport)) 

                proxy_thread.start()

            elif self.local_end_type == "udp":
                self.proxy_loop(self.server_socket,self.rhost,self.rport)
                self.exit_triggers()
                return

            elif self.server_socket == sys.stdin:
                self.proxy_loop(sys.stdin,self.rhost,self.rport)

            elif self.server_socket.family == socket.AF_PACKET: 
                output("[>.>] L2 ready: %s:%s <=> %s:%s" % (str(self.lhost),str(self.lport),str(self.rhost),str(self.rport)),YELLOW) 

                self.raw_proxy_loop(self.rhost,self.rport)


    def proxy_loop(self,local_socket,rhost,rport):

        remote_socket = self.socket_plinko(rhost,self.remote_end_type)
        
        if (self.rbind_addr != "0.0.0.0") or (self.rbind_port > 0):
            output("[!.!] Binding Rsock to %s:%d"%(self.rbind_addr,self.rbind_port),CYAN)
            remote_socket.bind((self.rbind_addr,self.rbind_port))

        # schro == schroedinger
        # simultaneuously ssl wrapped and not until afterwards
        schro_remote = remote_socket
        schro_local = local_socket
        
        try:
            if self.remote_end_type in ConnectionBased:
                if self.local_end_type == "udp" and not self.receive_first:
                    # we need to wait for data before we connect.
                    pass
                elif "windows" in system().lower() or "cygwin" in system().lower():
                    remote_socket.connect((rhost,rport))
                elif remote_socket.family == socket.AF_UNIX:
                    remote_socket.connect((rhost)) 
                elif remote_socket.family == socket.AF_INET6:
                    remote_socket.connect((rhost,rport,0,0))
                # to avoid dumb lack of socket.AF_PACKET in osx
                elif "darwin" not in system().lower() and remote_socket.family != socket.AF_PACKET:
                    remote_socket.connect((rhost,rport))
                else:
                    remote_socket.connect((rhost,rport))

        except Exception as e:
            output(str(e),YELLOW)
            output("[x.x] Unable to connect to %s,%s" % (rhost,str(rport)), RED)
            sys.exit()

        if self.local_end_type == "ssl":
            try:
                schro_local = self.server_context.wrap_socket(local_socket, server_side=True)  
            except ssl.SSLError as e:
                output("[x.x] Unable to do SSL local. Did you send a non-SSL request?",YELLOW)
                output(str(e),RED)
                schro_local.close()
                sys.exit()
        
        # need to wait for udp...
        if self.remote_end_type == "ssl" and self.local_end_type != "udp":
            try: 
                schro_remote = ssl.wrap_socket(remote_socket,cert_reqs=ssl.CERT_NONE) 
            except ssl.SSLError as e:
                output("[x.x] Unable to do SSL remote. Did you send a non-SSL request?",YELLOW)
                output(str(e),RED)
                schro_remote.close()
                sys.exit()
        
        # we can't really know where to send the packet yet if UDP. 
        # maybe save it till we recv? 
        if self.receive_first and self.local_end_type in ConnectionBased:
            remote_buffer = get_bytes(schro_remote)
            if self.verbose:
                hexdump(remote_buffer)  
            remote_buffer = self.inbound_handler(remote_buffer,rhost,self.lhost)
            self.pkt_count+=1
        
            #if data to send to local, do so
            if len(remote_buffer):
                output("[<.<] Sending %d bytes inbound (%s:%d)." % (len(remote_buffer),self.lhost,self.lport),ORANGE)
                if self.local_end_type in ConnectionBased: 
                    self.buffered_send(schro_local,remote_buffer)
                else:   
                    self.buffered_sendto(schro_local,remote_buffer,(self.lhost,self.lport))
        
        elif not self.receive_first and self.local_end_type == "udp":
            # [>_>] port range is going to override schro_local, cuz I'm lazy
            if self.udp_port_range:
                    
                schro_local = [schro_local]
                schro_local_range = validateNumberRange(self.udp_port_range,True) # flatten out number range into list of integers
                output("[!-!] Attempting to bind %d UDP ports : %s" %(len(schro_local_range),self.udp_port_range),GREEN)
                for port in schro_local_range:
                    tmp = self.socket_plinko(self.lhost,self.local_end_type) 
                    # shoulda implimented server_init better to take a socket, eh.
                    try:
                        tmp.bind((self.lhost,port))
                        tmp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) 
                        schro_local.append(tmp) 
                    except:
                        output("[x.x] Unable to bind UDP %s:%d."%(self.lhost,port),YELLOW)
            
                while True:
                    readable,__,__ = select.select(schro_local,[],schro_local,self.timeout)
                    if len(readable) > 0:
                        break

            else: #udp && !udp_port_range
                while True:
                    readable,__,__ = select.select([schro_local],[],[schro_local],self.timeout)
                    if len(readable) > 0:
                        break
        
            if self.remote_end_type != "udp":
                remote_socket.connect((rhost,rport))
            if self.remote_end_type == "ssl":
                try: 
                    schro_remote = ssl.wrap_socket(remote_socket,cert_reqs=ssl.CERT_NONE) 
                except ssl.SSLError as e:
                    output("[x.x] Unable to do SSL remote. Did you send a non-SSL request?",YELLOW)
                    output(str(e),RED)
                    schro_remote.close()
                    sys.exit()
                 

        buf = ""
        # don't know if port range or not
        active_udp = None
        schro_list = []
        try:
            schro_list = schro_local[:] 
            schro_list.append(schro_remote)
            active_udp = schro_local[0] 
        except:
            schro_list = [schro_local, schro_remote]

        try:
            while True: 
                byte_count = 0
                if self.killswitch.is_set():
                    if self.local_end_type == "ssl":
                        try:
                            schro_local.shutdown(socket.SHUT_RDWR) 
                        except:
                            pass
                    schro_local.close()
                    if schro_remote != sys.stdout:
                        schro_remote.close() 
                    break 
                 
                readable, __,exceptional = select.select(schro_list,[],schro_list,self.timeout)    
                
                # if there's any sockets ready to read, get data
                for s in readable:
                    try:
                        buf = self.get_bytes(s)
                    except Exception as e: 
                        raise

                    byte_count += len(buf)

                    if byte_count and self.verbose:
                        hexdump(buf)

                    # readable socket w/no data => closed connection  
                                    # if there's data, perform appropriate handlers, and then send 
                    # Need to see which direction first though
                    if s == schro_local:
                        # Case LOCAL] => [REMOTE
                        buf = self.outbound_handler(buf,self.lhost,self.rhost) 
                        if len(buf):
                            self.pkt_count+=1

                            if self.remote_end_type in ConnectionBased:
                                self.buffered_send(schro_remote,buf)
                            elif self.remote_end_type == "stdout" or self.local_end_type == "stdin": 
                                sys.stdout.write(buf)      
                            else:
                                self.buffered_sendto(schro_remote,buf,(self.rhost,self.rport))

                            output("[o.o] Sent %d bytes to remote (%s:%d)" % (len(buf),self.rhost,self.rport),GREEN)

                    if s == schro_remote:
                        # Case LOCAL] <= [REMOTE 
                        buf = self.inbound_handler(buf,rhost,rport)
                        if len(buf): 
                            self.pkt_count+=1

                            if self.local_end_type in ConnectionBased: 
                                self.buffered_send(schro_local,buf)
                                output("[o.o] Sent %d bytes to local (%s:%d)" % (len(buf),self.lhost,self.lport),CYAN)
                            elif self.remote_end_type == "stdout" or self.local_end_type == "stdin": 
                                sys.stdout.write(buf)      
                            else:   
                                if active_udp == None:
                                    self.buffered_sendto(schro_local,buf,(self.lhost,self.srcport))
                                else:
                                    active_port = active_udp.getsockname()[1]  
                                    self.buffered_sendto(active_udp,buf,(active_udp.getsockname()[0],self.lport))
                                output("[o.o] Sent %d bytes to local (%s:%d)" % (len(buf),self.lhost,self.lport),CYAN)

                    try: #udp port range case
                        if s in schro_local: 
                            buf = self.outbound_handler(buf,self.lhost,self.rhost) 
                            if len(buf):
                                self.pkt_count+=1
                                active_udp = s # so we know where to throw packets back to 
                                self.buffered_sendto(schro_remote,buf,(self.rhost,self.rport))
                                output("[o.o] Sent %d bytes to remote (%s:%d)" % (len(buf),self.rhost,self.rport),GREEN)
                             
                    except Exception as e:
                        pass
                        

                if self.dont_kill:
                    continue 
            
                if not byte_count or len(exceptional):
                    if self.local_end_type == "ssl":
                        try:
                            schro_local.shutdown(socket.SHUT_RDWR) 
                        except:
                            pass

                    
                    if self.local_end_type in ConnectionBased: 
                        schro_local.close()
                        if self.remote_end_type in ConnectionBased:
                            schro_remote.close() 
                        output("[-.-] No more data, exiting",YELLOW)
                        break          

        except KeyboardInterrupt:
            pass

        self.exit_triggers()


    # Do anything that we need to do before exiting
    def exit_triggers(self):

        if self.fuzz_file:
            try:
                self.fuzzerData.port = self.rport 
                self.fuzzerData.setMessagesToFuzzFromString("0")
                self.fuzzerData.writeToFile(self.fuzz_file,defaultComments=True) 
            except Exception as e:
                output(str(e),YELLOW)
                output("[-.-] Couldn't write fuzz data. Where's Mutiny?",RED)


    # will need to filter frames based on mac addresses. 
    # Only want mac addresses destined for either local_interface or remote_interface
    def raw_proxy_loop(self,r_mac,r_int):
        # Code flow is rather different for AF_PACKET L2_raw
        # Constants from /usr/include/linux/if_ether.h
        # ETH_P_ALL = 0x0300
        ETH_P_ALL = 0x0300 
        ETH_P_IP = 0x0008

        LMAC = ''.join([chr(int(i,16)) for i in self.lhost.split(":")])
        RMAC = ''.join([chr(int(i,16)) for i in self.rhost.split(":")])
        LINTERFACE = self.lport 
        RINTERFACE = self.rport

        if self.local_end_type == "passthrough":
            proto = ETH_P_ALL
        elif self.local_end_type == "bridge":
            #non-promisc
            proto = ETH_P_IP
        
        # use a special mac to identify our stuff?
        # --l2_filter for that. Will be special string in frames to check for
        # frame must contain string or else we don't forward

        SIOCGIFHWADDR = 0x8927
        r_sock = socket.socket(socket.AF_PACKET,socket.SOCK_RAW,proto)

        if self.local_end_type == "bridge":
            r_sock.bind((RINTERFACE,0))

        #Get information for interfaces
         
        LOCAL_MAC_IOCTL = fcntl.ioctl(r_sock.fileno(),SIOCGIFHWADDR,struct.pack('256s',LINTERFACE[0:15]))
        LOCAL_INT_MAC = LOCAL_MAC_IOCTL[18:24]
        REMOTE_MAC_IOCTL = fcntl.ioctl(r_sock.fileno(),SIOCGIFHWADDR,struct.pack('256s',RINTERFACE[0:15]))
        REMOTE_INT_MAC = REMOTE_MAC_IOCTL[18:24]

        output("[@.@] Bound raw socket, filtering for (%s,%s)||(%s,%s)" % (LINTERFACE,macdump(LMAC),
                                                                        RINTERFACE,macdump(RMAC)),GREEN)

        self.pkt_count = 0
        # how we determine direction of frame
        # filter = DST + SRC
        inbound_remote = REMOTE_INT_MAC + RMAC 
        outbound_remote = RMAC + REMOTE_INT_MAC
        # Don't care where it's coming from locally
        inbound_local = LOCAL_INT_MAC + LMAC 
        outbound_local = LMAC + LOCAL_INT_MAC
        # Passthrough modes
        passthrough_inbound = RMAC + LMAC
        passthrough_outbound = LMAC + RMAC
    
        # in order to act as a L2 proxy, we need 2 flows....
        # ([us] -> [lo])--Decept-->([eth0] -> [them])
        # ([us] <- [lo])<--Decept--([eth0] <- [them])
        # Also need to support passthrough
        # ([int1] <--Decept--> [int2]
        
        sock_list = [r_sock]
        if self.local_end_type=="bridge":
            sock_list.append(self.server_socket)

        while True:
            buff = ""
            try:
                readable, __,exceptional = select.select(sock_list,[],sock_list,self.timeout)    

                # if there's any sockets ready to read, get data
                if self.local_end_type == "passthrough":
                    for s in readable:
                        buff = self.get_raw_bytes(s) 
                        if not buff:
                            # not meant for us, or filtered
                            break
                        # What about broadcasts??
                        # MAC MTU 1500

                        if buff[0:12] == inbound_remote: 
                                output(macdump(buff[0:6]) + " " + macdump(buff[6:12]) + " << Remote",YELLOW)
                                buff = self.inbound_handler(buff) 
                                if self.L2_forward:
                                    buff = LOCAL_INT_MAC + LMAC + buff[:12]
                                    self.buffered_sendto(r_sock,buff,(LINTERFACE,0))

                        elif buff[0:12] == outbound_remote:
                                output(macdump(buff[0:6]) + " " + macdump(buff[6:12]) + " Remote >>",YELLOW)
                                buff = self.outbound_handler(buff)
                                if self.L2_forward:
                                    buff = RMAC + REMOTE_INT_MAC + buff[:12]
                                    self.buffered_sendto(r_sock,buff,(RINTERFACE,0))

                        elif buff[0:12] == inbound_local: 
                                output(macdump(buff[0:6]) + " " + macdump(buff[6:12]) + " << Local",CYAN)
                                buff = self.inbound_handler(buff) 
                                if self.L2_forward:
                                    buff = LOCAL_INT_MAC + LMAC + buff[:12]
                                    self.buffered_sendto(r_sock,buff,(LINTERFACE,0))

                        elif buff[0:12] == outbound_local:
                                output(macdump(buff[0:6]) + " " + macdump(buff[6:12]) + " Local >>",CYAN)
                                buff = self.outbound_handler(buff)
                                if self.L2_forward:
                                    buff = RMAC + REMOTE_INT_MAC + buff[:12]
                                    self.buffered_sendto(r_sock,buff,(RINTERFACE,0))

                        elif buff[0:12] == passthrough_outbound:
                                output(macdump(buff[0:6]) + " " + macdump(buff[6:12]) + " >> outbound >>",PURPLE)
                                buff = self.outbound_handler(buff)

                        elif buff[0:12] == passthrough_inbound: 
                                output(macdump(buff[0:6]) + " " + macdump(buff[6:12]) + " << inbound <<",PURPLE)
                                buff = self.inbound_handler(buff) 

                        else:
                            if DEBUGGING:
                                print "DST:" + macdump(buff[0:6]) + " SRC:" + macdump(buff[6:12])
                            break

                        self.pkt_count+=1  

                elif self.local_end_type == "bridge":
                    for s in readable:
                        buff = self.get_raw_bytes(s) 
                        if len(buff):
                            if s == self.server_socket:
                                self.outbound_handler(buff)
                                self.buffered_sendto(r_sock,buff,(RINTERFACE,0))
                            elif s == r_sock:
                                self.inbound_handler(buff)
                                self.buffered_sendto(self.server_socket,buff,(LINTERFACE,0))
                            if self.verbose:
                                hexdump(buff)

                if len(exceptional): #error on a socket, break
                    break
             
            except KeyboardInterrupt:
                r_sock.close()
                self.shutdown()

        self.exit_triggers()
        return

    def get_raw_bytes(self,sock):
        ret = ""
        sock.settimeout(self.timeout)
        # examine the mac addresses first
        # ('eth0', 2048, 4, 1, '\x00\x0c)\xc0\\\x89')
        tmp,addr = sock.recvfrom(self.l2_mtu) 
        interface = addr[0]
        dst = addr[4] 

        if interface != self.lport and interface != self.rport:
            return ""
        
        if self.l2_filter and self.l2_filter not in tmp:
            # drop it if we're filtering and no hit
            # perhaps better to do some other method?
            # e.g. re.match/etc
            return ""

            
        return tmp 
             

    def inbound_handler(self,inbound,src="",dst=""):
        #write_packet_header(inbound,src,dst)
        self.write_packet_data(inbound,1) 

        if self.dumpraw:
            dstfile = join(getcwd(),self.dumpraw,"inbound-%d"%self.pkt_count)
            with open(dstfile,'wb') as f:
                f.write(inbound)

        if self.inbound_hook and self.handler_trigger:
            #output("[<.<] Pre-hook datalen: %d" %len(inbound),CYAN)
            inbound = self.inbound_hook(inbound)
            #output("[<.<] Pre-hook datalen: %d" %len(inbound),CYAN)
            self.handler_trigger = False

        return inbound


    def outbound_handler(self,outbound,src="",dst=""):
        #write_packet_header(outbound,src,dst) 
        self.write_packet_data(outbound,0)    

        if self.dumpraw:
            dstfile = join(getcwd(),self.dumpraw,"outbound-%d"%self.pkt_count)
            with open(dstfile,'wb') as f:
                f.write(outbound)

        if self.outbound_hook:
            #output("[>.>] Pre-hook datalen: %d" %len(outbound),CYAN)
            outbound = self.outbound_hook(outbound)
            #output("[>.>] Post-hook datalen: %d" %len(outbound),CYAN)

        if "proxy config" in outbound:
            self.handler_trigger = True

        return outbound


    def write_packet_data(self,packet,direction):
        if not len(packet):
            return    

        if self.fuzzerData:
            m = mutiny.Message()
            m.direction = direction
            # 2 == raw
            try:
                fuzz=False
                if len(self.fuzzerData.messageCollection.messages) == 0:
                    fuzz=True
                m.setMessageFrom(sourceType=2,message=packet,isFuzzed=fuzz)
            except:
                #older verison of mutiny
                m.message = bytearray(packet)

            self.fuzzerData.messageCollection.addMessage(m)
            
        if self.pcap_file:
            packlen = len(packet)
            
            pcap_record = pcap_record_hdr(packlen)
            if PCAP_SNAPLEN < packlen:
                pcap_record.orig_len = PCAP_SNAPLEN
                packlen = PCAP_SNAPLEN 
            
            # write headers for packet
            PCAP_FILE.write(pcap_record.get_byte_array())
            # write packet up to SNAPLEN
            PCAP_FILE.write(packet[0:PCAP_SNAPLEN])


    def buffered_send(self,sock,data):
        send_count = 0
        while send_count < len(data):
            try:
                # if we need a data limit cap
                data_chunk = data[send_count:send_count+self.l4_MTU]
            except:
                data_chunk = data[send_count:]

            send_count += sock.send(data_chunk)

    def buffered_sendto(self,sock,data,dst_tuple):
        send_count = 0
        while send_count < len(data):
            try:
                # if we need a data limit cap
                data_chunk = data[send_count:send_count+self.l4_MTU]
            except:
                data_chunk = data[send_count:]

            send_count += sock.sendto(data_chunk,dst_tuple)

#####################################################
### End class DeceptProxy() 
#####################################################

#assume all unsigned
def raw_to_cstruct_args(raw_bytes,cstruct):
    if len(raw_bytes) != sizeof(cstruct):
        output("raw_to_cstruct: len mismatch: %d,%d"%(len(raw_bytes),sizeof(cstruct)),YELLOW)
        raise 

    fmtstr = ""
    for f in cstruct._fields_:
        type_size = sizeof(f[1])

        if type_size == 1:
                fmtstr+="B"
        elif type_size == 2:
                fmtstr+="H"
        elif type_size == 4 or type_size == 8:
                fmtstr+="L"
        else:
           fmtstr+="%ds" % type_size

    return struct.unpack(fmtstr,raw_bytes)


def macdump(src):
    return ':'.join(["%02x" % ord(c) for c in src])


def hexdump(src,length=16):
    # Licensed with PSF
    # http://code.activestate.com/recipes/142812-hex-dumper
    # with minor edits
    result=[]
    digits = 4 if isinstance(src,unicode) else 2
    for i in xrange(0,len(src),length):
        
        s=src[i:i+length]
        hexa = b' '.join(["%0*x" %(digits,ord(x)) for x in s]) 

        text = b''.join([x if 0x20 <= ord(x) < 0x7f else b'.' for x in s])
        result.append(b"%04x   %-*s   %s" % (i,length*(digits+1),hexa, text))
        
    output(b'\n'.join(result))


def dumb_arg_helper(option,default=None,required=False):
    try:
        optionIndex = sys.argv.index(option)
        # first 4 args should be lhost/lport/rhost/rport
        if optionIndex <= 4:
            output("[>.>] Missing Option: %s" % (option),RED)
            sys.exit()

        if "--" in sys.argv[optionIndex+1] and required:
            output("[>.>] Please provide valid type of %s" % (option),RED)
            sys.exit()

        return sys.argv[optionIndex+1]

    except:
        if default and not required:
            return default
        elif default == 0:
            return default
        elif required and option in sys.argv:
            output("[>.>] Please provide valid type of %s" % (option),RED)
            sys.exit()
            
        
        
def main():
    output("[<_<] Decept proxy/sniffer [>_>]\n",CYAN)

    if len(sys.argv[1:]) < 4:
        output(usage)
        output("[^.^] /Decept Proxy/sniffer [^.^]",CYAN)
        sys.exit(0)
    

    lhost = sys.argv[1] 
    rhost = sys.argv[3] 
    try:
        lport = int(sys.argv[2])
        rport = int(sys.argv[4]) 
    except:
        lport = sys.argv[2]
        rport = sys.argv[4]

    
    try:

        # grab/validate the endpoints
        remote_end_type = dumb_arg_helper("-r","tcp") 
        if remote_end_type not in ValidEndpoints and remote_end_type not in PROTO.keys():
            output("[x.x] Invalid remoteEnd given, exiting!") 
            sys.exit()

        local_end_type = dumb_arg_helper("-l","tcp")
        if local_end_type not in ValidEndpoints and local_end_type not in PROTO.keys():
            output("[x.x] Invalid localEnd given, exiting!",RED) 
            sys.exit()

        proxy = DeceptProxy(lhost,lport,rhost,rport,local_end_type,remote_end_type)

        # Take care of all switches
        proxy.recv_first = True if "--recv_first" in sys.argv else False   
        proxy.pps = True if "--pps" in sys.argv else False
        proxy.loglast = True if "--loglast" in sys.argv else False
        proxy.L2_forward = True if "--L2_forward" in sys.argv else False

        proxy.verbose = False if "--quiet" in sys.argv else True
        proxy.dont_kill = True if "--dont_kill" in sys.argv else False

        #next, ints and strings that don't require processing
        proxy.timeout = float(dumb_arg_helper("--timeout",2))
        proxy.pcapdir = dumb_arg_helper("--pcapdir","",True)
        proxy.snaplen = int(dumb_arg_helper("--snaplen",65535)) 
        proxy.tcp_MTU = int(dumb_arg_helper("--max-packet-len",30000))
        proxy.fuzz_file = dumb_arg_helper("--fuzzer","",True)  
        proxy.dumpraw = dumb_arg_helper("--dumpraw","",True) 
        proxy.l2_MTU = int(dumb_arg_helper("--mtu",1500))
        proxy.l2_filter = dumb_arg_helper("--l2_filter")
        proxy.rbind_addr = dumb_arg_helper("--rbind_addr") or "0.0.0.0"
        proxy.rbind_port = int(dumb_arg_helper("--rbind_port",0)) 
        proxy.udp_port_range = dumb_arg_helper("--udppr")
        

        # look for and parse the files first...
        inbound_hook = dumb_arg_helper("--inhook")
        outbound_hook = dumb_arg_helper("--outhook")

        if inbound_hook or outbound_hook:
            import imp

            # if inbound_hook == outbound_hook file, no biggie
            if inbound_hook:
                try: 
                    imp.load_source("in_hook",inbound_hook) 
                    proxy.inbound_hook = sys.modules["in_hook"].inbound_hook
                    #output("[!.!] inbound_hook imported!")
                except:
                    output("Could not import inbound hook: %s" % inbound_hook,YELLOW)


            if outbound_hook:
                try: 
                    imp.load_source("out_hook",outbound_hook) 
                    proxy.outbound_hook = sys.modules["out_hook"].outbound_hook
                    #output("[!.!] outbound_hook imported!")
                except:
                    output("Could not import outbound hook: %s" % outbound_hook,YELLOW)
              

        for arg in sys.argv[4:]:
            if "-" in arg and arg not in ValidCmdlineOptions: 
                output("[?.?] Option %s not valid option. Typo?"%arg,YELLOW)
                
        proxy.server_loop()
    except KeyboardInterrupt:
        output("\n[!.!] Interrupt received, exiting!\n",CYAN) 
        proxy.killswitch.set()
        
    proxy.shutdown()
    sys.exit(0)



##########
##DEFINES#
##########
#colors
RED='\033[31m'
ORANGE='\033[91m'
GREEN='\033[92m'
LIME='\033[99m'
YELLOW='\033[93m'
BLUE='\033[94m'
PURPLE='\033[95m'
CYAN='\033[96m'
CLEAR='\033[00m' 

def output(inp,color=None):
    if color:
        sys.__stdout__.write("%s%s%s\n" % (color,str(inp),CLEAR)) 
        sys.__stdout__.flush()
    else:
        sys.__stdout__.write(str(inp)+"\n")
        sys.__stdout__.flush()

# Taken from socket module itself, doesn't seem like any
# other L3 protocols are supported....
PROTO = {
    "ip":0,
    "icmp":1,
    "ggp":3,
    "ipv4":4,
    "ipip":4,
    "tcp":6,
    "udp":17,
    "dstopts":60,
    "egp":8,
    "eon":80,
    "esp":50,
    "fragment":44,
    "hello":63,
    "hopopts":0,
    "icmpv6":58,
    "idp":22,
    "igmp":2,
    "ipcomp":108,
    "ah":51,
    "ipv6":41,
    "gre":47,
    "max":256,
    "nd":77,
    "none":59,
    "pim":103,
    "pup":12,
    "raw":255,
    "routing":43,
    "rsvp":46,
    "tp":29,
    "xtp":36,
}

class IP_PACKET(Structure):
    _pack_=1
    _fields_ = [
    ("version", c_ubyte,4),
    ("ihl", c_ubyte,4),
    ("tos", c_ubyte),
    ("length", c_ushort),
    ("id", c_ushort),
    ("flags", c_ubyte,3),
    ("fragOffset", c_ushort,13),
    ("ttl", c_ubyte),
    ("proto", c_ubyte),
    ("checksum", c_ushort),
    ("ipSrc", c_uint),
    ("ipDst", c_uint),
    #("options", c_uint),
    #("padding", c_ubyte * 2)
    ]
    

class ETH(Structure):
    # sooo apparently packed c_ulonglong bitfields just aren't portable:
    # https://mail.python.org/pipermail/python-list/2009-June/540937.html
    # bleh. 

    _pack_=1
    _fields_ = [
    ("ethDstHigh", c_uint),
    ("ethDstLow", c_ushort),
    ("ethSrcHigh", c_uint),
    ("ethSrcLow", c_ushort),
    ("type", c_ushort)
    ]

# End Raw Socket Structs

ValidEndpoints = ["ssl","udp","tcp","bridge","passthrough","stdin","stdout"]
ConnectionBased = ["ssl","tcp"]

usage = '''
usage: decept.py <local_host> <local_port> <remote_host> <remote_port> [OPTIONS]

optional arguments:
  -h, --help            show this help message and exit
  --quiet               Don't show hexdumps
  --recv_first          Receive stuff first?
  --timeout TIMEOUT     Timeout for outbound socket
  --loglast LOGLAST     Log the last packet (unimplimented)
  --fuzzer FUZZFILE     *.fuzzer output for mutiny (extensions required)
  --dumpraw DUMPDIR     Directory to dump raw packet files into
                        (fmt = %d-%s % (pkt_num,[inbound|outbound]))
  --max-packet-len LEN  Max amount of data per packet when sending data
  --dont_kill           For when you don't want the connection to die if
                        neither side sends packets for TIMEOUT seconds

  -l, {ssl,udp,tcp}|[L3 Proto]     Local endpoint type
  -r, {ssl,udp,tcp}|[L3 Proto]     Remote endpoint type

Hook Files:
  Optional function definitions for processing data between inbound
  and outbound endpoints. Look at "inbound_handler"/"outbound_handler" 
  for more information. 

  --outhook HOOKFILE | Function Prototype: string outbound_hook(outbound):
  --inhook  HOOKFILE | Function Prototype: string inbound_hook(inbound):

L2 usage: decept.py <local_int> <local_mac> <remote_int> <remote_mac>

L2 options:
  --l2_filter MACADDR   Ignore inbound traffic except from MACADDR
  --l2_MTU    MTU       Set Maximum Transmision Unit for socket
  --l2_forward          Bridge the local interface and remote interface

  --pcapdir PCAPDIR     Directory to store pcaps (extensions required)
  --pps                 Create a new pcap for each session
  --snaplen SNAPLEN     Length of packet truncation

L4 Usage: decept.py 127.0.0.1 9999 10.0.0.1 8080
L3 Usage: decept.py 127.0.0.1 0 10.0.0.1 0 -l icmp -r icmp 
L2 Usage: decept.py lo 00:00:00:00:00:00 eth0 ff:aa:cc:ee:dd:00 
Unix: decept.py localsocketname 0 remotesocketname 0 
Abstract: decept.py \\x00localsocketname 0 \\x00remotesocketname 0

'''

ValidCmdlineOptions = ["--recv_first","--timeout","--loglast",
                       "--udp","--pcapdir","--pps","--snaplen",
                       "--fuzzer","--dumpraw","-l","-r",
                       "--l2_filter","--l2_mtu","--L2_forward", 
                       "--L3_raw","--inhook","--outhook",
                       "--rbind_addr","--rbind_port",
                       "--quiet","--dont_kill","--udppr"]

#####################################
## Global header for pcap file
#####################################

class pcap_hdr_t(LittleEndianStructure):
    _pack_ = 1
    _fields_ = [
        ("magic_number", c_uint32),
        ("version_major", c_ushort),
        ("version_minor", c_ushort),
        ("thiszone", c_int32),
        ("sigfigs", c_uint32),
        ("snaplen", c_uint32),
        ("network", c_uint32),
    ]
  
    def __repr__(self):
        return self.get_byte_str()
       
    def get_byte_str(self):
        buf = ""
        for i in bytearray(self):
            buf+="\\x%02x"%i
        return buf 
   
    def get_byte_array(self):
        return bytearray(self)


def pcap_global_hdr(): 
    pcap_file = pcap_hdr_t(0xa1b2c3d4,0x2,0x4,0x0,0x0,0xffff,0x1)
    return pcap_file

'''
typedef struct pcap_hdr_s {
        guint32 magic_number;   /* magic number */
        guint16 version_major;  /* major version number */
        guint16 version_minor;  /* minor version number */
        gint32  thiszone;       /* GMT to local correction */
        guint32 sigfigs;        /* accuracy of timestamps */
        guint32 snaplen;        /* max length of captured packets, in octets */
        guint32 network;        /* data link type */
} pcap_hdr_t;

pcap_file.magic_number.value = 0xa1b2c3d4 
pcap_file.version_major.value = 0x0002 
pcap_file.version_minor.value = 0x0004
pcap_file.thiszone.value = 0x00000000
pcap_file.sigfigs.value = 0x00000000
pcap_file.snaplen.value = 0x0000ffff
pcap_file.network.value = 0x00000001 
'''

#####################################
## Per-packet header
#####################################

class pcaprec_hdr_t(LittleEndianStructure):
    _pack_ = 1
    _fields_ = [
        ("ts_sec", c_uint32),
        ("ts_usec", c_uint32),
        ("incl_len", c_uint32),
        ("orig_len", c_uint32)
    ]
    
    def __repr__(self):
        return self.get_byte_str()
       
    def get_byte_str(self):
        buf = ""
        for i in bytearray(self):
            buf+="\\x%02x"%i
        return buf 

    def get_byte_array(self):    
        return bytearray(self)

def pcap_record_hdr(packet_len):

    epoch = str(time()).split(".")
    sec = int(epoch[0]) 
    usec = int(epoch[1]) 
    pack_len = packet_len 
    orig_len = packet_len 

    pcap_record = pcaprec_hdr_t(sec,usec,pack_len,orig_len)
    return pcap_record

'''
typedef struct pcaprec_hdr_s {
        guint32 ts_sec;         /* timestamp seconds */
        guint32 ts_usec;        /* timestamp microseconds */
        guint32 incl_len;       /* number of octets of packet saved in file */
        guint32 orig_len;       /* actual length of packet */
} pcaprec_hdr_t;
'''

#########################
# Utility Functions
#########################
# Takes a string of numbers, seperated via commas
# or by hyphens, and generates an appropriate list of
# numbers from it.
# e.g. str("1,2,3-6")  => list([1,2,xrange(3,7)])
#
# If flattenList=True, will return a list of distinct elements
#
# If given an invalid number string, returns None
# (lifted straight from mutiny,lul)
def validateNumberRange(inputStr, flattenList=False):
    retList = []
    tmpList = filter(None,inputStr.split(','))

    # Print msg if invalid chars/typo detected
    for num in tmpList:
        try:
            retList.append(int(num))
        except ValueError:
            if '-' in num:
                intRange = num.split('-')
                # Invalid x-y-z
                if len(intRange) > 2:
                    print "Invalid range given"
                    return None
                try:
                    if not flattenList:
                        # Append iterator with bounds = intRange
                        retList.append(xrange(int(intRange[0]),int(intRange[1])+1))
                    else:
                        # Append individual elements
                        retList.extend(range(int(intRange[0]),int(intRange[1])+1))
                except TypeError:
                    print "Invalid range given"
                    return None
            else:
                print "Invalid number given"
                return None
    # All elements in the range are valid integers or integer ranges
    if flattenList:
        # If list is flattened, every element is an integer
        retList = sorted(list(set(retList)))
    return retList


if __name__ == "__main__":
    main() 
