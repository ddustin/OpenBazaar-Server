__author__ = 'chris'
import mock
import time

import nacl.signing, nacl.encoding, nacl.hash

from binascii import unhexlify

from txrudp import connection, rudp, packet, constants

from twisted.trial import unittest
from twisted.internet import task, reactor, address, udp

from dht.protocol import KademliaProtocol
from dht.utils import digest
from dht.storage import ForgetfulStorage
from dht.node import Node
from dht import kprotocol


class KademliaProtocolTest(unittest.TestCase):

    def setUp(self):
        self.public_ip = '123.45.67.89'
        self.port = 12345
        self.own_addr = (self.public_ip, self.port)
        self.addr1 = ('132.54.76.98', 54321)
        self.addr2 = ('231.76.45.89', 15243)

        self.clock = task.Clock()
        connection.REACTOR.callLater = self.clock.callLater

        self.proto_mock = mock.Mock(spec_set=rudp.ConnectionMultiplexer)
        self.handler_mock = mock.Mock(spec_set=connection.Handler)
        self.con = connection.Connection(
            self.proto_mock,
            self.handler_mock,
            self.own_addr,
            self.addr1
        )

        valid_key = "1a5c8e67edb8d279d1ae32fa2da97e236b95e95c837dc8c3c7c2ff7a7cc29855"
        self.signing_key = nacl.signing.SigningKey(valid_key, encoder=nacl.encoding.HexEncoder)
        verify_key = self.signing_key.verify_key
        signed_pubkey = self.signing_key.sign(str(verify_key))
        h = nacl.hash.sha512(signed_pubkey)
        self.storage = ForgetfulStorage()
        self.node = Node(unhexlify(h[:40]), self.public_ip, self.port, signed_pubkey, True, 1234, kprotocol.TCP)
        self.protocol = KademliaProtocol(self.node, self.storage, 20)

        self.handler = self.protocol.RPCHandler(False, 5, self.protocol._outstanding, self.protocol)
        self.handler.connection = self.con

        transport = mock.Mock(spec_set=udp.Port)
        ret_val = address.IPv4Address('UDP', self.public_ip, self.port)
        transport.attach_mock(mock.Mock(return_value=ret_val), 'getHost')
        self.protocol.makeConnection(transport)

    def tearDown(self):
        self.con.shutdown()
        self.protocol.shutdown()

    def test_invalid_datagram(self):
        self.assertFalse(self.handler.receive_message("hi"))
        self.assertFalse(self.handler.receive_message("hihihihihihihihihihihihihihihihihihihihih"))

    def test_rpc_ping(self):
        self._connecting_to_connected()

        m = kprotocol.Message()
        m.messageID = digest("msgid")
        m.sender.MergeFrom(self.protocol.sourceNode.proto)
        m.command = kprotocol.Command.Value("PING")
        data = m.SerializeToString()
        m.arguments.append(self.protocol.sourceNode.proto.SerializeToString())
        expected_message = m.SerializeToString()
        self.handler.receive_message(data)

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        m_calls = self.proto_mock.send_datagram.call_args_list
        sent_packet = packet.Packet.from_bytes(self.proto_mock.send_datagram.call_args_list[0][0][0])
        received_message = sent_packet.payload

        self.assertEqual(received_message, expected_message)
        self.assertEqual(len(m_calls), 2)

    def test_rpc_store(self):
        self._connecting_to_connected()

        m = kprotocol.Message()
        m.messageID = digest("msgid")
        m.sender.MergeFrom(self.protocol.sourceNode.proto)
        m.command = kprotocol.Command.Value("STORE")
        m.arguments.extend(["Keyword", "Key", self.protocol.sourceNode.proto.SerializeToString()])
        data = m.SerializeToString()
        for i in range(0, 3):
            del m.arguments[-1]
        m.arguments.append("True")
        expected_message = m.SerializeToString()
        self.handler.receive_message(data)

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        m_calls = self.proto_mock.send_datagram.call_args_list
        sent_packet = packet.Packet.from_bytes(self.proto_mock.send_datagram.call_args_list[0][0][0])
        received_message = sent_packet.payload
        self.assertEqual(received_message, expected_message)
        self.assertEqual(len(m_calls), 2)
        self.assertTrue(self.storage.getSpecific("Keyword", "Key") == self.protocol.sourceNode.proto.SerializeToString())

    def test_rpc_delete(self):
        self._connecting_to_connected()

        # Set a keyword to store
        m = kprotocol.Message()
        m.messageID = digest("msgid")
        m.sender.MergeFrom(self.protocol.sourceNode.proto)
        m.command = kprotocol.Command.Value("STORE")
        m.arguments.extend(["Keyword", "Key", self.protocol.sourceNode.proto.SerializeToString()])
        data = m.SerializeToString()
        for i in range(0, 3):
            del m.arguments[-1]
        m.arguments.append("True")
        expected_message1 = m.SerializeToString()
        self.handler.receive_message(data)
        self.assertTrue(self.storage.getSpecific("Keyword", "Key") == self.protocol.sourceNode.proto.SerializeToString())

        # Test bad signature
        m = kprotocol.Message()
        m.messageID = digest("msgid")
        m.sender.MergeFrom(self.protocol.sourceNode.proto)
        m.command = kprotocol.Command.Value("DELETE")
        m.arguments.extend(["Keyword", "Key", "Bad Signature"])
        data = m.SerializeToString()
        for i in range(0, 3):
            del m.arguments[-1]
        m.arguments.append("False")
        expected_message2 = m.SerializeToString()
        self.handler.receive_message(data)
        self.assertTrue(self.storage.getSpecific("Keyword", "Key") == self.protocol.sourceNode.proto.SerializeToString())

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        sent_packets = tuple(
            packet.Packet.from_bytes(call[0][0])
            for call in self.proto_mock.send_datagram.call_args_list
        )
        self.assertEqual(sent_packets[0].payload, expected_message1)
        self.assertEqual(sent_packets[1].payload, expected_message2)
        self.proto_mock.send_datagram.call_args_list = []

        # Test good signature
        m = kprotocol.Message()
        m.messageID = digest("msgid")
        m.sender.MergeFrom(self.protocol.sourceNode.proto)
        m.command = kprotocol.Command.Value("DELETE")
        m.arguments.extend(["Keyword", "Key", self.signing_key.sign("Key")[:64]])
        data = m.SerializeToString()
        for i in range(0, 3):
            del m.arguments[-1]
        m.arguments.append("True")
        expected_message3 = m.SerializeToString()
        self.handler.receive_message(data)
        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        sent_packet = packet.Packet.from_bytes(self.proto_mock.send_datagram.call_args_list[0][0][0])
        self.assertEqual(sent_packet.payload, expected_message3)
        self.assertTrue(self.storage.getSpecific("Keyword", "Key") is None)

    def test_rpc_stun(self):
        self._connecting_to_connected()

        m = kprotocol.Message()
        m.messageID = digest("msgid")
        m.sender.MergeFrom(self.protocol.sourceNode.proto)
        m.command = kprotocol.Command.Value("STUN")
        data = m.SerializeToString()
        m.arguments.extend([self.addr1[0], str(self.addr1[1])])
        expected_message = m.SerializeToString()
        self.handler.receive_message(data)

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        m_calls = self.proto_mock.send_datagram.call_args_list
        sent_packet = packet.Packet.from_bytes(self.proto_mock.send_datagram.call_args_list[0][0][0])
        received_message = sent_packet.payload

        self.assertEqual(received_message, expected_message)
        self.assertEqual(len(m_calls), 2)

    def test_rpc_find_node(self):
        self._connecting_to_connected()

        node1 = Node(digest("id1"), "127.0.0.1", 12345, digest("key1"))
        node2 = Node(digest("id2"), "127.0.0.1", 22222, digest("key2"))
        node3 = Node(digest("id3"), "127.0.0.1", 77777, digest("key3"))
        self.protocol.router.addContact(node1)
        self.protocol.router.addContact(node2)
        self.protocol.router.addContact(node3)
        m = kprotocol.Message()
        m.messageID = digest("msgid")
        m.sender.MergeFrom(self.protocol.sourceNode.proto)
        m.command = kprotocol.Command.Value("FIND_NODE")
        m.arguments.append(digest("nodetofind"))
        data = m.SerializeToString()
        del m.arguments[-1]
        m.arguments.extend([node3.proto.SerializeToString(), node2.proto.SerializeToString(), node1.proto.SerializeToString()])
        expected_message = m.SerializeToString()
        self.handler.receive_message(data)

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        m_calls = self.proto_mock.send_datagram.call_args_list
        sent_packet = packet.Packet.from_bytes(self.proto_mock.send_datagram.call_args_list[0][0][0])
        received_message = sent_packet.payload

        self.assertEqual(received_message, expected_message)
        self.assertEqual(len(m_calls), 2)

    def test_rpc_find_value(self):
        self._connecting_to_connected()

        # Set a value to find
        m = kprotocol.Message()
        m.messageID = digest("msgid")
        m.sender.MergeFrom(self.protocol.sourceNode.proto)
        m.command = kprotocol.Command.Value("STORE")
        m.arguments.extend(["Keyword", "Key", self.protocol.sourceNode.proto.SerializeToString()])
        data = m.SerializeToString()
        self.handler.receive_message(data)
        self.assertTrue(self.storage.getSpecific("Keyword", "Key") == self.protocol.sourceNode.proto.SerializeToString())

        # Send the find_value rpc
        m = kprotocol.Message()
        m.messageID = digest("msgid")
        m.sender.MergeFrom(self.protocol.sourceNode.proto)
        m.command = kprotocol.Command.Value("FIND_VALUE")
        m.arguments.append("Keyword")
        data = m.SerializeToString()
        self.handler.receive_message(data)

        del m.arguments[-1]
        value = kprotocol.Value()
        value.contractID = "Key"
        value.serializedNode = self.protocol.sourceNode.proto.SerializeToString()
        m.arguments.append("value")
        m.arguments.append(value.SerializeToString())
        expected_message = m.SerializeToString()

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        m_calls = self.proto_mock.send_datagram.call_args_list
        sent_packets = tuple(
            packet.Packet.from_bytes(call[0][0])
            for call in self.proto_mock.send_datagram.call_args_list
        )
        received_message = sent_packets[1].payload

        self.assertEqual(received_message, expected_message)
        self.assertEqual(len(m_calls), 3)

    def test_rpc_find_without_value(self):
        self._connecting_to_connected()

        node1 = Node(digest("id1"), "127.0.0.1", 12345, digest("key1"))
        node2 = Node(digest("id2"), "127.0.0.1", 22222, digest("key2"))
        node3 = Node(digest("id3"), "127.0.0.1", 77777, digest("key3"))
        self.protocol.router.addContact(node1)
        self.protocol.router.addContact(node2)
        self.protocol.router.addContact(node3)
        m = kprotocol.Message()
        m.messageID = digest("msgid")
        m.sender.MergeFrom(self.protocol.sourceNode.proto)
        m.command = kprotocol.Command.Value("FIND_VALUE")
        m.arguments.append(digest("Keyword"))
        data = m.SerializeToString()
        self.handler.receive_message(data)

        del m.arguments[-1]
        m.arguments.extend([node2.proto.SerializeToString(), node3.proto.SerializeToString(), node1.proto.SerializeToString()])
        expected_message = m.SerializeToString()

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        m_calls = self.proto_mock.send_datagram.call_args_list
        sent_packet = packet.Packet.from_bytes(self.proto_mock.send_datagram.call_args_list[0][0][0])
        received_message = sent_packet.payload

        m = kprotocol.Message()
        m.ParseFromString(received_message)

        self.assertEqual(received_message, expected_message)
        self.assertEqual(len(m_calls), 2)

    def test_callPing(self):
        self._connecting_to_connected()

        n = Node(digest("S"), self.addr1[0], self.addr1[1])
        self.protocol[self.addr1] = self.con
        self.protocol.callPing(n)

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        sent_packet = packet.Packet.from_bytes(self.proto_mock.send_datagram.call_args_list[0][0][0])
        sent_message = sent_packet.payload

        m = kprotocol.Message()
        m.ParseFromString(sent_message)
        self.assertTrue(len(m.messageID) == 20)
        self.assertEqual(self.protocol.sourceNode.proto.guid, m.sender.guid)
        self.assertEqual(self.protocol.sourceNode.proto.signedPublicKey, m.sender.signedPublicKey)
        self.assertTrue(m.command == kprotocol.PING)
        self.assertEqual(self.proto_mock.send_datagram.call_args_list[0][0][1], self.addr1)

    def test_callStore(self):
        self._connecting_to_connected()

        n = Node(digest("S"), self.addr1[0], self.addr1[1])
        self.protocol[self.addr1] = self.con
        self.protocol.callStore(n, digest("Keyword"), digest("Key"), self.protocol.sourceNode.proto.SerializeToString())

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        sent_packet = packet.Packet.from_bytes(self.proto_mock.send_datagram.call_args_list[0][0][0])
        sent_message = sent_packet.payload

        m = kprotocol.Message()
        m.ParseFromString(sent_message)
        self.assertTrue(len(m.messageID) == 20)
        self.assertEqual(self.protocol.sourceNode.proto.guid, m.sender.guid)
        self.assertEqual(self.protocol.sourceNode.proto.signedPublicKey, m.sender.signedPublicKey)
        self.assertTrue(m.command == kprotocol.STORE)
        self.assertEqual(self.proto_mock.send_datagram.call_args_list[0][0][1], self.addr1)
        self.assertEqual(m.arguments[0], digest("Keyword"))
        self.assertEqual(m.arguments[1], digest("Key"))
        self.assertEqual(m.arguments[2], self.protocol.sourceNode.proto.SerializeToString())

    def test_callFindValue(self):
        self._connecting_to_connected()

        n = Node(digest("S"), self.addr1[0], self.addr1[1])
        self.protocol[self.addr1] = self.con
        keyword = Node(digest("Keyword"))
        self.protocol.callFindValue(n, keyword)

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        sent_packet = packet.Packet.from_bytes(self.proto_mock.send_datagram.call_args_list[0][0][0])
        sent_message = sent_packet.payload

        m = kprotocol.Message()
        m.ParseFromString(sent_message)
        self.assertTrue(len(m.messageID) == 20)
        self.assertEqual(self.protocol.sourceNode.proto.guid, m.sender.guid)
        self.assertEqual(self.protocol.sourceNode.proto.signedPublicKey, m.sender.signedPublicKey)
        self.assertTrue(m.command == kprotocol.FIND_VALUE)
        self.assertEqual(self.proto_mock.send_datagram.call_args_list[0][0][1], self.addr1)
        self.assertEqual(m.arguments[0], keyword.id)

    def test_callFindNode(self):
        self._connecting_to_connected()

        n = Node(digest("S"), self.addr1[0], self.addr1[1])
        self.protocol[self.addr1] = self.con
        keyword = Node(digest("nodetofind"))
        self.protocol.callFindNode(n, keyword)

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        sent_packet = packet.Packet.from_bytes(self.proto_mock.send_datagram.call_args_list[0][0][0])
        sent_message = sent_packet.payload

        m = kprotocol.Message()
        m.ParseFromString(sent_message)
        self.assertTrue(len(m.messageID) == 20)
        self.assertEqual(self.protocol.sourceNode.proto.guid, m.sender.guid)
        self.assertEqual(self.protocol.sourceNode.proto.signedPublicKey, m.sender.signedPublicKey)
        self.assertTrue(m.command == kprotocol.FIND_NODE)
        self.assertEqual(self.proto_mock.send_datagram.call_args_list[0][0][1], self.addr1)
        self.assertEqual(m.arguments[0], keyword.id)

    def test_callDelete(self):
        self._connecting_to_connected()

        n = Node(digest("S"), self.addr1[0], self.addr1[1])
        self.protocol[self.addr1] = self.con
        self.protocol.callDelete(n, digest("Keyword"), digest("Key"), digest("Signature"))

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        sent_packet = packet.Packet.from_bytes(self.proto_mock.send_datagram.call_args_list[0][0][0])
        sent_message = sent_packet.payload

        m = kprotocol.Message()
        m.ParseFromString(sent_message)
        self.assertEqual(self.proto_mock.send_datagram.call_args_list[0][0][1], self.addr1)
        self.assertTrue(len(m.messageID) == 20)
        self.assertEqual(self.protocol.sourceNode.proto.guid, m.sender.guid)
        self.assertEqual(self.protocol.sourceNode.proto.signedPublicKey, m.sender.signedPublicKey)
        self.assertTrue(m.command == kprotocol.DELETE)
        self.assertEqual(m.arguments[0], digest("Keyword"))
        self.assertEqual(m.arguments[1], digest("Key"))
        self.assertEqual(m.arguments[2], digest("Signature"))

    def test_acceptResponse(self):
        self._connecting_to_connected()

        def handle_response(resp):
            self.assertTrue(resp[0])
            self.assertEqual(resp[1][0], self.protocol.sourceNode.id)

        n = Node(digest("S"), self.addr1[0], self.addr1[1])
        self.protocol[self.addr1] = self.con
        d = self.protocol.callPing(n)

        self.clock.advance(1)
        connection.REACTOR.runUntilCurrent()
        sent_packet = packet.Packet.from_bytes(self.proto_mock.send_datagram.call_args_list[0][0][0])
        sent_message = sent_packet.payload

        m = kprotocol.Message()
        m.ParseFromString(sent_message)
        timeout = reactor.callLater(5, self.protocol._timeout, m.messageID)
        self.handler._outstanding[m.messageID] = (d, timeout)
        m.arguments.append(self.protocol.sourceNode.id)
        self.handler.receive_message(m.SerializeToString())

        return d.addCallback(handle_response)

    def test_unknownRPC(self):
        self.assertFalse(self.handler._acceptRequest(digest("msgid"), "unknown", [digest("argument")], Node(digest("nodeid"))))

    def test_timeout(self):
        self._connecting_to_connected()
        self.protocol[self.addr1] = self.con

        def test_remove_outstanding():
            self.assertTrue(len(self.protocol._outstanding) == 0)

        def test_deffered(d):
            self.assertFalse(d[0])
            test_remove_outstanding()

        n = Node(digest("S"), self.addr1[0], self.addr1[1])
        d = self.protocol.callPing(n)
        self.clock.advance(6)
        return d.addCallback(test_deffered)

    def test_transferKeyValues(self):
        self._connecting_to_connected()
        self.protocol[self.addr1] = self.con

        self.protocol.storage[digest("keyword")] = (digest("key"), self.protocol.sourceNode.proto.SerializeToString())
        self.protocol.transferKeyValues(Node(digest("id"), self.addr1[0], self.addr1[1]))

        self.clock.advance(1)
        connection.REACTOR.runUntilCurrent()
        sent_packet = packet.Packet.from_bytes(self.proto_mock.send_datagram.call_args_list[0][0][0])
        sent_message = sent_packet.payload
        x = kprotocol.Message()
        x.ParseFromString(sent_message)

        m = kprotocol.Message()
        m.sender.MergeFrom(self.protocol.sourceNode.proto)
        m.command = kprotocol.Command.Value("STORE")
        m.arguments.append(digest("keyword"))
        m.arguments.append(digest("key"))
        m.arguments.append(self.protocol.sourceNode.proto.SerializeToString())
        self.assertEqual(x.sender, m.sender)
        self.assertEqual(x.command, m.command)
        self.assertEqual(x.arguments[0], m.arguments[0])
        self.assertEqual(x.arguments[1], m.arguments[1])
        self.assertEqual(x.arguments[2], m.arguments[2])

    def test_refreshIDs(self):
        node1 = Node(digest("id1"), "127.0.0.1", 12345, signed_pubkey=digest("key1"))
        node2 = Node(digest("id2"), "127.0.0.1", 22222, signed_pubkey=digest("key2"))
        node3 = Node(digest("id3"), "127.0.0.1", 77777, signed_pubkey=digest("key3"))
        self.protocol.router.addContact(node1)
        self.protocol.router.addContact(node2)
        self.protocol.router.addContact(node3)
        for b in self.protocol.router.buckets:
            b.lastUpdated = (time.time() - 5000)
        ids = self.protocol.getRefreshIDs()
        self.assertTrue(len(ids) == 1)

    def _connecting_to_connected(self):
        remote_synack_packet = packet.Packet.from_data(
            42,
            self.con.own_addr,
            self.con.dest_addr,
            ack=0,
            syn=True
        )
        self.con.receive_packet(remote_synack_packet)

        self.clock.advance(0)
        connection.REACTOR.runUntilCurrent()

        self.next_remote_seqnum = 43

        m_calls = self.proto_mock.send_datagram.call_args_list
        sent_syn_packet = packet.Packet.from_bytes(m_calls[0][0][0])
        seqnum = sent_syn_packet.sequence_number

        self.handler_mock.reset_mock()
        self.proto_mock.reset_mock()

        self.next_seqnum = seqnum + 1